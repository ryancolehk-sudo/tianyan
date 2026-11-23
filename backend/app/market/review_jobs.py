"""Explicit review jobs. Ordinary live selections, schedules and evidence stay intact."""
import hashlib
import json
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import APIError, require_instance
from app.config import settings
from app.db import engine
from app.evidence.common import lock, now
from app.evidence.models import EvidenceVersion
from app.integrations.credentials import assert_source_binding, job_context, resolve_source_credentials
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.review_fetch import (
    EMOTION_HISTORY_URL,
    emotion_history_page,
    cls_url,
    fetch_public,
    minute_page,
    minute_url,
    news_page,
    parameters,
    validate_table,
)
from app.market.review_protocol import (
    CONTRACT_VERSION,
    DAILY_DATASETS,
    DATASET_FAMILIES,
    INDEX_CODES,
    MAX_MEMBER_PAGES,
    MINUTE_CODES,
    NEWS_PAGE_SIZE,
    PUBLIC_DATASETS,
    ReviewCollectionInput,
)
from app.market.review_store import (
    available_trade_days,
    family_id,
    load_dataset,
    manifest_key,
    persist_pages,
)
from app.models import AuditEvent, Job, Provider, User


class BudgetWait(Exception):
    """The persisted job was safely requeued before an HTTP request."""


def validate_context(db, job, clock):
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.service import permission_reason
    ctx = job.payload.get('review_context')
    if not isinstance(ctx, dict) or ctx.get('version') != CONTRACT_VERSION:
        raise SourceFailure('review_context_invalid')
    if any(key in job.payload for key in ('minute_context', 'reference_context', 'dated_context', 'daily_history')):
        raise SourceFailure('review_context_invalid')
    dataset = ctx.get('dataset')
    if dataset not in DATASET_FAMILIES or set(ctx) != {
        'version', 'dataset', 'day', 'start_day', 'scope', 'configuration_sha256',
    }:
        raise SourceFailure('review_context_invalid')
    try:
        requested = date.fromisoformat(ctx['day'])
        first = date.fromisoformat(ctx['start_day'])
    except (ValueError, TypeError, KeyError):
        raise SourceFailure('review_context_invalid') from None
    today = clock.astimezone(SHANGHAI).date()
    if not today - timedelta(days=365) <= first <= requested <= today:
        raise SourceFailure('review_day_out_of_range')
    if str(family_id(DATASET_FAMILIES[dataset])) != job.payload.get('source_id'):
        raise SourceFailure('review_source_mismatch')
    if job.payload.get('review_latest') is not None:
        latest = available_trade_days(db,limit=1,as_of_time=clock)
        if (job.payload['review_latest'] is not True or dataset not in {'new_highs','new_high_history'}
                or not latest or requested != latest[-1]):
            raise SourceFailure('review_archive_day_mismatch')
    if dataset == 'indices':
        if ctx['scope'] not in INDEX_CODES:
            raise SourceFailure('review_scope_mismatch')
    elif dataset == 'members':
        import re
        if ctx['scope'] != 'all' and not re.fullmatch(r'BK\d{4,8}\.DC', ctx['scope']):
            raise SourceFailure('review_scope_mismatch')
    elif ctx['scope'] != 'all':
        raise SourceFailure('review_scope_mismatch')
    provider = db.get(Provider, family_id(DATASET_FAMILIES[dataset]))
    config = db.get(SourceSettings, provider.id) if provider else None
    if provider is None or config is None or permission_reason(provider) or config.paused_reason:
        raise SourceFailure('permission_missing')
    assert_source_binding(db, provider.id, provider.family, job.payload)
    if configuration_fingerprint(provider, config) != ctx['configuration_sha256']:
        raise SourceFailure('configuration_changed')
    if job.payload.get('scheduled_daily_archive') is True:
        from app.market.review_schedule import DATASETS, archive_deadline, archive_target_day
        # The automatic path can request only one daily table, never a rolling
        # current-minute/news endpoint or a multi-date/manual recovery range.
        if (dataset not in (*DATASETS, 'indices') or first != requested
                or (dataset != 'indices' and ctx['scope'] != 'all')):
            raise SourceFailure('review_context_invalid')
        owner = db.get(User, job.owner_id)
        if owner is None or not owner.enabled or owner.role != 'admin' or config.owner_id != owner.id:
            raise SourceFailure('permission_missing')
        # Outside either publication window the existing budget gate defers the
        # job before any supplier permit. Recheck the proved date at every permit
        # inside a window so an old queue cannot become today's live request.
        if archive_deadline(dataset, clock) is None:
            expected = archive_target_day(db, clock)
            if expected is None:
                raise SourceFailure('calendar_not_ready')
            if requested != expected:
                raise SourceFailure('review_archive_day_mismatch')
    if job.payload.get('postclose_archive') is True:
        from app.market.postclose_schedule import validate_archive
        validate_archive(db, job, config, clock)
    return provider, config, ctx


def queue_dataset(db, owner, dataset, day, *, start_day=None, scope='all', batch_id=None, force=False, integration_probe=None, scheduled_archive=False, postclose_archive=False, review_latest=False):
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.service import permission_reason
    if dataset not in DATASET_FAMILIES:
        raise APIError(422, 'review_dataset_unknown', '未实现的复盘数据集。')
    provider = db.get(Provider, family_id(DATASET_FAMILIES[dataset]))
    config = db.get(SourceSettings, provider.id) if provider else None
    if provider is None or config is None or permission_reason(provider) or config.paused_reason:
        raise APIError(422, 'review_source_unavailable', '复盘来源尚未配置、未获使用范围或已暂停：' + DATASET_FAMILIES[dataset])
    bound = resolve_source_credentials(db, provider.id, provider.family, allow_disabled=integration_probe is not None, refresh=True)
    if bound.reason:
        raise APIError(422, bound.reason, '请在数据中心核对数据接入开关与凭据。')
    binding_context = job_context(bound, provider.id, probe=integration_probe is not None)
    if integration_probe is not None and integration_probe != binding_context:
        raise APIError(409, 'configuration_changed', '检测接入已变化，请刷新后重试。')
    lock(db, f'review:queue:{provider.id}:{dataset}:{day}:{scope}')
    ctx = dict(version=CONTRACT_VERSION, dataset=dataset, day=day.isoformat(),
               start_day=(start_day or day).isoformat(), scope=scope,
               configuration_sha256=configuration_fingerprint(provider, config))
    pending = db.scalar(select(Job).where(
        Job.kind == 'review.collect', Job.payload['source_id'].astext == str(provider.id),
        Job.payload['review_context']['dataset'].astext == dataset,
        Job.payload['review_context']['day'].astext == day.isoformat(),
        Job.payload['review_context']['scope'].astext == scope,
        Job.state.in_(['queued', 'running']),
    ).order_by(Job.created_at).limit(1))
    if pending:
        if pending.payload['review_context'] != ctx or pending.payload.get('integration_context') != binding_context:
            raise APIError(409, 'collection_intent_conflict', '该复盘日期已有不同配置或范围的采集任务。')
        return pending
    if not force:
        saved = load_dataset(db, dataset, day, scope, include_rows=False)
        if (saved['state'] == 'complete' and saved.get('configuration_sha256') == ctx['configuration_sha256']
            and saved.get('requested_start_day', '9999') <= ctx['start_day']
            and saved.get('requested_end_day', '') >= ctx['day']):
            manifest = db.get(EvidenceVersion, uuid.UUID(saved['manifest_ref']['id']))
            old_id = manifest.payload['rows'][0].get('job_id')
            old = db.get(Job, uuid.UUID(old_id)) if old_id else None
            if old is not None:
                return old
    payload = {'source_id': str(provider.id), 'review_context': ctx}
    if scheduled_archive:
        payload['scheduled_daily_archive'] = True
    if postclose_archive:
        payload['postclose_archive'] = True
    if review_latest:
        payload['review_latest'] = True
    if binding_context is not None:
        payload['integration_context'] = binding_context
    if batch_id is not None:
        payload['review_batch_id'] = str(batch_id)
    job = Job(owner_id=owner.id, kind='review.collect', payload=payload)
    db.add(job)
    db.flush()
    validate_context(db, job, now(db))
    return job


def enqueue_collection(db, owner, body: ReviewCollectionInput, request_id=None):
    require_instance(db)
    if not settings().data_collection_enabled:
        raise APIError(422, 'collection_disabled', '本机采集未启用。')
    local = now(db).astimezone(SHANGHAI)
    if body.trade_day and (body.trade_day > local.date() or (local.date() - body.trade_day).days > 150):
        raise APIError(422, 'review_day_out_of_range', '复盘截止日须位于最近150个自然日内。')
    batch_id = uuid.uuid4()
    first = (body.trade_day or local.date()) - timedelta(days=150)
    calendars = []
    for dataset in ('calendar_sse', 'calendar_szse'):
        job = queue_dataset(db, owner, dataset, local.date(), start_day=first,
                            batch_id=batch_id, force=body.force)
        calendars.append(str(job.id))
    prepare = Job(owner_id=owner.id, kind='review.prepare', payload={
        'review_batch_id': str(batch_id), 'input': body.model_dump(mode='json'),
        'calendar_jobs': calendars,
    })
    db.add(prepare)
    db.flush()
    db.add(AuditEvent(actor_id=owner.id, action='review.collection.request', request_id=request_id,
                      resource_type='job', resource_id=prepare.id,
                      changed_field_names=['review_batch', 'requested_range']))
    return {'batch_id': str(batch_id), 'prepare_job_id': str(prepare.id),
            'calendar_job_ids': calendars, 'state': 'queued',
            'lookback': body.lookback, 'warmup_sessions': 3}


def enqueue_public_source(db, provider, config, day=None, *, integration_probe=None):
    datasets = {'review_tencent_indices': 'tencent_minutes', 'review_sina_indices': 'sina_minutes',
                'review_cls_news': 'news', 'review_kpl_new_highs': 'new_highs'}
    days = available_trade_days(db, limit=1, end_day=day)
    if not days:
        raise APIError(422, 'calendar_not_ready', '尚无可核实的收盘交易日，请先采集复盘日历。')
    owner = db.get(User, config.owner_id)
    job = queue_dataset(db, owner, datasets[provider.family], days[-1], force=integration_probe is not None,
                        integration_probe=integration_probe,review_latest=provider.family=='review_kpl_new_highs')
    config.next_run_at = now(db) + timedelta(seconds=config.interval_seconds)
    return job


def prepare_job(job):
    with Session(engine()) as db, db.begin():
        current = db.scalar(select(Job).where(
            Job.id == job.id, Job.state == 'running', Job.lease_token == job.lease_token,
            Job.lease_until > func.now(),
        ).with_for_update())
        if current is None:
            return
        dependencies = [db.get(Job, uuid.UUID(ident)) for ident in current.payload['calendar_jobs']]
        if any(item is None or item.state in {'failed', 'cancelled'} for item in dependencies):
            current.state, current.error_code = 'failed', 'review_calendar_failed'
        elif any(item.state != 'succeeded' for item in dependencies):
            current.state, current.available_at = 'queued', now(db) + timedelta(seconds=5)
            current.attempts -= 1
            current.error_code = 'review_calendar_wait'
        else:
            body = ReviewCollectionInput.model_validate(current.payload['input'])
            days = available_trade_days(db, limit=body.lookback + 3, end_day=body.trade_day)
            if len(days) != body.lookback + 3 or (body.trade_day is not None and days[-1] != body.trade_day):
                current.state, current.error_code = 'failed', 'review_calendar_coverage_missing'
            else:
                owner = db.get(User, current.owner_id)
                count, failures, dependencies = 0, [], set()
                def add(dataset, day, **kwargs):
                    nonlocal count
                    try:
                        queued = queue_dataset(db, owner, dataset, day,
                                               batch_id=current.payload['review_batch_id'],
                                               force=body.force, **kwargs)
                        count += int(queued.state in {'queued', 'running'})
                        dependencies.add(str(queued.id))
                    except APIError as error:
                        failures.append({'dataset': dataset, 'day': day.isoformat(),
                                         'scope': kwargs.get('scope', 'all'), 'reason': error.code})
                # Date-bound jobs are intentionally separate from ordinary live
                # refresh. Existing per-source symbol/sector settings stay exact.
                for code in INDEX_CODES:
                    add('indices', days[-1], start_day=days[0], scope=code)
                add('emotion_history', days[-1], start_day=days[0])
                for dataset in (() if body.source_scope == 'four_sources' else ('tencent_minutes', 'sina_minutes')):
                    add(dataset, days[-1])
                for day in reversed(days):
                    for dataset in DAILY_DATASETS:
                        add(dataset, day)
                    if body.include_members and day in days[-body.member_lookback:]:
                        for sector in body.sector_codes or ['all']:
                            add('members', day, scope=sector)
                    if body.include_news and body.source_scope == 'legacy' and day in days[-5:]:
                        add('news', day)
                current.state = 'succeeded'
                current.result_ref = {'queued_jobs': count, 'unavailable_sources': failures,
                                      'collection_job_ids': sorted(dependencies),
                                      'news_requested_sessions': min(5, len(days)) if body.include_news and body.source_scope == 'legacy' else 0,
                                      'member_requested_sessions': min(body.member_lookback, len(days)) if body.include_members else 0,
                                      'first_day': days[0].isoformat(), 'last_day': days[-1].isoformat(),
                                      'sessions': len(days)}
                current.error_code = 'review_sources_partial' if failures else None
        if current.state != 'queued':
            current.completed_at = now(db)
        current.lease_token = current.lease_until = None


def _reserve_public(job, dataset):
    action = 'source.request.review.' + ('kpl-new-highs' if dataset in {'new_highs','new_high_history'} else 'kpl-emotion' if dataset == 'emotion_history' else 'cls' if dataset == 'news' else
                                        'sina' if dataset == 'sina_minutes' else 'tencent')
    quota = 1200 if dataset == 'news' else 300
    with Session(engine()) as db, db.begin():
        lock(db, action)
        current = db.scalar(select(Job).where(
            Job.id == job.id, Job.state == 'running', Job.lease_token == job.lease_token,
            Job.lease_until > func.now(),
        ).with_for_update())
        if current is None:
            raise SourceFailure('lease_lost')
        clock = now(db)
        if current.payload != job.payload:
            raise SourceFailure('configuration_changed')
        validate_context(db, current, clock)
        if current.payload.get('postclose_archive') is True:
            from app.market.postclose_schedule import defer_archive
            if defer_archive(current, clock):
                return False
        source = uuid.UUID(current.payload['source_id'])
        cooldown = db.scalar(select(func.max(DataFetchRun.retry_not_before)).where(
            DataFetchRun.source_id == source, DataFetchRun.retry_not_before > clock))
        start = datetime.combine(clock.astimezone(SHANGHAI).date(), day_time.min, SHANGHAI)
        used = db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == action, AuditEvent.created_at >= start))
        last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == action))
        deadline = cooldown
        if last and last + timedelta(seconds=2) > clock:
            deadline = max(deadline or clock, last + timedelta(seconds=2))
        if used >= quota:
            deadline = max(deadline or clock, start + timedelta(days=1))
        if deadline:
            current.state, current.available_at = 'queued', deadline
            current.attempts -= 1
            current.error_code = 'source_budget_wait'
            current.lease_token = current.lease_until = None
            return False
        db.add(AuditEvent(actor_id=current.owner_id, action=action, resource_type='job',
                          resource_id=current.id, changed_field_names=['review_request_permit'],
                          created_at=clock))
    return True


def _retry_deadline(dataset,stamp,reason,seconds):
    if dataset not in PUBLIC_DATASETS:
        from app.market.tushare_cooldown import run_deadline
        return run_deadline(stamp,reason,seconds)
    return stamp+timedelta(seconds=seconds or 60) if reason=='rate_limited' else None


def _attempt(job, dataset, response=None, reason=None, elapsed_ms=None, retry_after=None):
    with Session(engine()) as db, db.begin():
        current = db.scalar(select(Job).where(
            Job.id == job.id, Job.state == 'running', Job.lease_token == job.lease_token,
            Job.lease_until > func.now(),
        ))
        if current is None:
            return
        stamp = response.received_at if response else now(db)
        db.add(DataFetchRun(
            source_id=uuid.UUID(job.payload['source_id']), capability='review.' + dataset,
            job_id=job.id, started_at=stamp - timedelta(milliseconds=elapsed_ms or 0),
            received_at=stamp, status='error' if reason else 'partial',
            reason=reason or 'review_page_pending', expected=None, accepted=None, rejected=None,
            duplicates=None, elapsed_ms=response.elapsed_ms if response else elapsed_ms,
            request_count=1, cost_cny=None,
            retry_not_before=_retry_deadline(dataset,stamp,reason,retry_after),
            body_hash=hashlib.sha256(response.body).hexdigest() if response else None,
        ))


def collect_job(job):
    from app.market.budget import reserve_tushare_request
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.tushare import request as tushare_request
    if not settings().data_collection_enabled:
        raise SourceFailure('collection_disabled')
    started = datetime.now(UTC)
    duration_started = time.monotonic()
    with Session(engine()) as db:
        provider, config, context = validate_context(db, job, now(db))
        fingerprint = configuration_fingerprint(provider, config)
        bound = assert_source_binding(db, provider.id, provider.family, job.payload)
    dataset = context['dataset']
    pages, requests = [], 0
    reason, retry_after = None, None

    def call(url=None, offset=0):
        nonlocal requests
        # A transient error late in a large table must not discard prior pages.
        # Each retry still reserves the audited source budget.
        tries = 3
        for attempt in range(tries):
            if requests:
                time.sleep(2.05)
            with Session(engine()) as check:
                assert_source_binding(check, provider.id, provider.family, job.payload, frozen=bound)
            permitted = (_reserve_public(job, dataset) if dataset in PUBLIC_DATASETS
                         else reserve_tushare_request(job))
            if not permitted:
                raise BudgetWait()
            requests += 1
            clock = time.monotonic()
            try:
                if url is not None:
                    response = fetch_public(url)
                else:
                    api, params, fields = parameters(context, offset=offset)
                    response = tushare_request(api, params, fields, bound.credential, bound.endpoint)
            except SourceFailure as error:
                if url is None:
                    from app.market.tushare_cooldown import record_failure
                    record_failure(job,error)
                _attempt(job, dataset, reason=error.reason,
                         elapsed_ms=int((time.monotonic() - clock) * 1000),
                         retry_after=error.retry_after)
                if attempt + 1 < tries and error.reason in {'transport_error', 'timeout'}:
                    continue
                raise
            _attempt(job, dataset, response=response)
            return response

    try:
        if dataset in {'new_highs', 'new_high_history'}:
            from app.market.new_high_protocol import source_url, history_page, history_rows, group_rows, stocks_page, PAGE_SIZE, MAX_PAGES
            url = source_url('GetDayNewHigh_W28')
            history = call(url)
            if dataset == 'new_high_history':
                pages.append((history_page(history.body, context, url), history.received_at))
            else:
                expected = [r for r in history_rows(history.body) if r['trade_date'] == context['day'].replace('-', '')]
                if len(expected) != 1:
                    raise SourceFailure('review_highs_day_unavailable')
                bodies, count = [], 0
                for index in range(MAX_PAGES):
                    url = source_url('GroupStock_W28', context['day'], index * PAGE_SIZE)
                    response = call(url)
                    current_rows = group_rows(response.body, context['day'])
                    bodies.append(response.body)
                    count += len(current_rows)
                    if count >= int(expected[0]['total']) or not current_rows:
                        break
                pages.append((stocks_page(bodies, history.body, context['day'], url), response.received_at))
        elif dataset == 'emotion_history':
            response = call(EMOTION_HISTORY_URL)
            pages.append((emotion_history_page(response.body, context), response.received_at))
        elif dataset in {'tencent_minutes', 'sina_minutes'}:
            for code in MINUTE_CODES:
                response = call(minute_url(dataset, code))
                pages.append((minute_page(response, dataset, code), response.received_at))
        elif dataset == 'news':
            target = date.fromisoformat(context['day'])
            before = int(datetime.combine(target + timedelta(days=1), day_time.min, SHANGHAI).timestamp())
            response = call(cls_url(before))
            page, _last_stamp, _returned = news_page(response, target)
            if not page.rows:
                raise SourceFailure('review_news_day_unavailable')
            page.scope = f'当日最新最多{NEWS_PAGE_SIZE}条公开电报窗口；不是当日全量或价格变化因果结论。'
            pages.append((page, response.received_at))
        else:
            paged = dataset not in {'indices', 'calendar_sse', 'calendar_szse'}
            page_size = 2500 if dataset == 'pools' else 4000 if dataset == 'pool_details' else 5000
            limit = MAX_MEMBER_PAGES if dataset == 'members' else 8 if paged else 1
            seen, fields = set(), None
            for number in range(limit):
                response = call(offset=number * page_size)
                page = validate_table(response.body, context, allow_empty=number > 0)
                if fields is not None and page.rows and page.fields != fields:
                    raise SourceFailure('schema_changed')
                if fields is not None and not page.rows:
                    page.fields = fields
                fields = page.fields
                if dataset in {'seats', 'lhb'}:
                    # No unique source disclosure ID: retain original rows.
                    # Detect repeated full pages without inventing row identity.
                    digest = hashlib.sha256(json.dumps(
                        {'fields': page.fields, 'rows': page.rows}, sort_keys=True,
                        separators=(',', ':'), ensure_ascii=False,
                    ).encode()).hexdigest()
                    if digest in seen and page.rows:
                        raise SourceFailure('review_pagination_overlap')
                    seen.add(digest)
                else:
                    for row in page.rows:
                        key = (row.get('trade_date') or row.get('cal_date'), row.get('ts_code'),
                               row.get('con_code'))
                        if key in seen:
                            raise SourceFailure('review_pagination_overlap')
                        seen.add(key)
                pages.append((page, response.received_at))
                if not paged or len(page.rows) < page_size:
                    break
            else:
                raise SourceFailure('review_pagination_limit')
    except BudgetWait:
        return
    except SourceFailure as error:
        reason, retry_after = error.reason, error.retry_after

    with Session(engine()) as db, db.begin():
        current = db.scalar(select(Job).where(
            Job.id == job.id, Job.state == 'running', Job.lease_token == job.lease_token,
            Job.lease_until > func.now(),
        ).with_for_update())
        if current is None:
            return
        provider = db.get(Provider, uuid.UUID(job.payload['source_id']))
        config = db.get(SourceSettings, provider.id) if provider else None
        if reason is None:
            try:
                if current.payload != job.payload:
                    raise SourceFailure('configuration_changed')
                provider, config, checked = validate_context(db, current, now(db))
                if fingerprint != configuration_fingerprint(provider, config):
                    raise SourceFailure('configuration_changed')
            except SourceFailure as error:
                reason = error.reason
        calendar_rows = None
        if reason is None and dataset.startswith('calendar_'):
            first, last = date.fromisoformat(context['start_day']), date.fromisoformat(context['day'])
            expected = {first + timedelta(days=i) for i in range((last - first).days + 1)}
            calendar_rows = {datetime.strptime(row['cal_date'], '%Y%m%d').date(): row['is_open'] == '1'
                             for row in pages[0][0].rows}
            if set(calendar_rows) != expected:
                reason = 'review_calendar_coverage_missing'
        total = 0
        if reason is None:
            total = persist_pages(db, provider, config, current, pages)
            if dataset.startswith('calendar_'):
                from app.market.collect import store_calendar_batch
                exchange = 'SSE' if dataset == 'calendar_sse' else 'SZSE'
                store_calendar_batch(db, provider, config, sorted(calendar_rows.items()),
                                     pages[0][1], exchanges=(exchange,))
        received = pages[-1][1] if pages else now(db)
        db.add(DataFetchRun(
            source_id=uuid.UUID(job.payload['source_id']), capability='review.' + dataset, job_id=job.id,
            started_at=started, received_at=received, status='error' if reason else 'complete' if total else 'empty',
            reason=reason, expected=total if not reason else None, accepted=total if not reason else None,
            rejected=0 if not reason else None, duplicates=0 if not reason else None,
            elapsed_ms=max(0,int((time.monotonic() - duration_started) * 1000)),
            request_count=0, cost_cny=None,
            body_hash=hashlib.sha256(json.dumps([page.raw_hash for page, _ in pages]).encode()).hexdigest() if pages else None,
            retry_not_before=_retry_deadline(dataset,received,reason,retry_after),
        ))
        retry = reason in {'timeout', 'transport_error', 'upstream_unavailable', 'rate_limited'} and current.attempts < 3
        current.state = 'queued' if retry else 'failed' if reason else 'succeeded'
        current.error_code = reason
        current.completed_at = None if retry else now(db)
        if retry:
            current.available_at = now(db) + timedelta(seconds=max(60, retry_after or 0))
        else:
            current.result_ref = {**(current.result_ref or {}),'dataset': dataset, 'requested_day': context['day'],
                                  'scope': context['scope'], 'rows': total,
                                  'manifest_key': manifest_key(dataset, context['day'], context['scope']) if not reason else None}
        current.lease_token = current.lease_until = None


def batch_status(db, owner_id, batch_id):
    rows = list(db.scalars(select(Job).where(
        Job.owner_id == owner_id, Job.payload['review_batch_id'].astext == str(batch_id),
    ).order_by(Job.created_at, Job.id)))
    if not rows:
        raise APIError(404, 'not_found', '找不到本人的复盘采集批次。')
    dependency_ids = set()
    for row in rows:
        if row.kind == 'review.prepare':
            dependency_ids.update(row.payload.get('calendar_jobs', []))
            dependency_ids.update((row.result_ref or {}).get('collection_job_ids', []))
    existing_ids = {str(row.id) for row in rows}
    if dependency_ids - existing_ids:
        rows.extend(db.scalars(select(Job).where(Job.id.in_([uuid.UUID(i) for i in dependency_ids - existing_ids]))))
    counts = {state: sum(row.state == state for row in rows)
              for state in ('queued', 'running', 'succeeded', 'failed', 'cancelled')}
    partial = counts['failed'] or counts['cancelled'] or any(row.error_code for row in rows if row.state == 'succeeded')
    return {'batch_id': str(batch_id), 'counts': counts,
            'state': 'running' if counts['queued'] or counts['running'] else 'partial' if partial else 'complete',
            'jobs': [dict(id=str(row.id), kind=row.kind, state=row.state, error_code=row.error_code,
                          dataset=row.payload.get('review_context', {}).get('dataset'),
                          requested_day=row.payload.get('review_context', {}).get('day'),
                          scope=row.payload.get('review_context', {}).get('scope'),
                          result=row.result_ref) for row in rows]}
