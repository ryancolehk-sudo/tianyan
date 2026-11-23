"""Explicit, permission-bound preparation of cached tomorrow-research evidence.

GET readers never call this module. Every physical request has a receipt and a
shared source-budget permit; progress is committed a page at a time so a worker
restart does not replay a completed page. Global selected-stock settings are
never changed by a research request.
"""
import hashlib
import json
import re
import uuid
from time import monotonic
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import APIError
from app.config import settings
from app.db import engine
from app.evidence.common import content_hash, lock, now
from app.evidence.schemas import ReferencePayload
from app.integrations.credentials import (
    assert_source_binding,
    job_context,
    resolve_source_credentials,
)
from app.market.collect import store_calendar_batch, store_observation, store_reference_batch
from app.market.minute_backfill import configuration_fingerprint
from app.market.models import DataFetchRun, SourceSettings
from app.market.preparation_fetch import (
    ANNOUNCEMENT_FIELDS,
    CNINFO_LOOKUP,
    CNINFO_QUERY,
    NEWS_FIELDS,
    announcement_request,
    announcement_rows,
    extract_pdf,
    fetch_document,
    parse_announcements,
    parse_news_window,
    parse_stock_lookup,
    stock_lookup_request,
)
from app.market.protocol import SHANGHAI, InstrumentKey, SourceFailure, fetch_https
from app.market.review_fetch import cls_url, fetch_public
from app.market.review_protocol import DAILY_DATASETS, DATASET_FAMILIES, INDEX_CODES, MAX_NEWS_PAGES
from app.market.review_store import available_trade_days, family_id, load_dataset
from app.market.service import permission_reason
from app.market.tushare import (
    code as tushare_code,
)
from app.market.tushare import (
    day as tushare_day,
)
from app.market.tushare import (
    parameters,
    parse_calendar,
    parse_reference,
    table,
)
from app.market.tushare import (
    request as tushare_request,
)
from app.market.tushare_specs import SPECS
from app.market.tushare_cooldown import run_deadline as tushare_retry_deadline
from app.models import AuditEvent, Job, Provider, User

VERSION = 1
MAX_CANDIDATES = 20
FUTURE_CALENDAR_DAYS = 90
MAX_ANNOUNCEMENT_PAGES = 5
MAX_DOCUMENTS_PER_STOCK = 8
SELECTED_FAMILIES = (
    'tushare_daily_basic', 'tushare_adj_factor', 'tushare_moneyflow',
    'tushare_income', 'tushare_balancesheet', 'tushare_cashflow',
    'tushare_fina_indicator', 'tushare_dividend',
)
OPERATIONS = {
    'news_window': frozenset({'review_cls_news'}),
    'calendar': frozenset({'tushare_calendar_sse', 'tushare_calendar_szse'}),
    'selected': frozenset(SELECTED_FAMILIES),
    'announcements': frozenset({'cninfo_announcements'}),
}
TRANSIENT = frozenset({'timeout', 'transport_error', 'upstream_unavailable', 'rate_limited'})


def candidate_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)', value):
        raise SourceFailure('invalid_preparation_candidate')
    return InstrumentKey({'SH': 'SSE', 'SZ': 'SZSE', 'BJ': 'BSE'}[value[-2:]], value[:6], 'stock')


def _source(db, family, owner_id):
    provider = db.get(Provider, family_id(family))
    config = db.get(SourceSettings, provider.id) if provider else None
    if provider is None or config is None:
        raise SourceFailure('source_not_configured')
    if config.owner_id != owner_id:
        raise SourceFailure('source_owner_mismatch')
    denied = permission_reason(provider)
    if denied:
        raise SourceFailure(denied)
    if config.paused_reason:
        raise SourceFailure('source_paused')
    return provider, config


def _queue(db, owner_id, family, operation, source_day, cutoff, code=None):
    provider, config = _source(db, family, owner_id)
    bound = resolve_source_credentials(db, provider.id, family, refresh=True)
    if bound.reason:
        raise SourceFailure(bound.reason)
    context = dict(version=VERSION, operation=operation, source_day=str(source_day),
        cutoff=cutoff.isoformat(), code=code, configuration_sha256=configuration_fingerprint(provider, config))
    intent = content_hash({'source_id': str(provider.id), 'owner_id': str(owner_id), 'context': context})
    lock(db, 'tomorrow:prepare:' + intent)
    old = db.scalar(select(Job).where(Job.kind == 'tomorrow.collect', Job.owner_id == owner_id,
        Job.payload['preparation_key'].astext == intent,
        Job.state.in_(['queued', 'running', 'succeeded'])).order_by(Job.created_at.desc()).limit(1))
    if old is not None:
        return old
    payload = {'source_id': str(provider.id), 'preparation_context': context, 'preparation_key': intent}
    binding = job_context(bound, provider.id)
    if binding is not None:
        payload['integration_context'] = binding
    job = Job(owner_id=owner_id, kind='tomorrow.collect', payload=payload)
    db.add(job)
    db.flush()
    return job


def queue_preparation(db, owner_id, source_day: date, cutoff: datetime, candidates: list[str] | None = None,
                      *, include_core: bool = True) -> dict:
    """Queue only existing, verified sources; absent permissions are visible gaps.

    ``cutoff`` is the requested acquisition window end, not a fictitious receipt.
    The research snapshot must freeze after these jobs complete. Evidence first
    received later than an older snapshot remains excluded from that snapshot.
    """
    from app.market.review_jobs import queue_dataset
    clock = now(db)
    if (not isinstance(source_day, date) or not isinstance(cutoff, datetime)
            or cutoff.utcoffset() is None or cutoff > clock + timedelta(seconds=1)
            or not clock.astimezone(SHANGHAI).date() - timedelta(days=150) <= source_day <= cutoff.astimezone(SHANGHAI).date()):
        raise APIError(422, 'invalid_preparation_window', '准备须使用最近150日内的来源日和已到达的接收截止。')
    owner = db.get(User, owner_id)
    if owner is None:
        raise APIError(404, 'user_not_found', '账号不存在。')
    candidates = list(dict.fromkeys(candidates or []))
    try:
        for candidate in candidates:
            candidate_key(candidate)
    except SourceFailure:
        raise APIError(422, 'invalid_preparation_candidate', '候选股须使用完整交易所代码。') from None
    limitations = []
    if len(candidates) > MAX_CANDIDATES:
        limitations.append(f'本批最多补齐{MAX_CANDIDATES}只候选股；其余保留数据缺口，可另批准备。')
        candidates = candidates[:MAX_CANDIDATES]
    coverage, ids = [], []

    def add(family, operation, code=None):
        entry = {'dataset': operation, 'family': family, 'scope': code or 'all', 'source_day': str(source_day)}
        try:
            if not settings().data_collection_enabled:
                raise SourceFailure('collection_disabled')
            acquisition_day = max(source_day, cutoff.astimezone(SHANGHAI).date() - timedelta(days=7)) if operation == 'news_window' else source_day
            job = _queue(db, owner_id, family, operation, acquisition_day, cutoff, code)
            ids.append(str(job.id))
            entry.update(state=job.state, job_id=str(job.id))
        except SourceFailure as error:
            entry.update(state='gap', reason=error.reason)
        coverage.append(entry)

    for dataset in ((*DAILY_DATASETS, 'members') if include_core else ()):
        family = DATASET_FAMILIES[dataset]
        # Recent memberships are collected as dated snapshots rather than
        # applying today's membership to every past trading session.
        days = available_trade_days(db, limit=5, end_day=source_day) if dataset == 'members' else [source_day]
        for requested in sorted(set(days + [source_day])):
            entry = {'dataset': 'review.' + dataset, 'family': family, 'scope': 'all', 'source_day': str(requested)}
            saved = load_dataset(db, dataset, requested, include_rows=False)
            entry.update(state=saved['state'], rows=saved['row_count'])
            if saved['state'] != 'complete':
                try:
                    if not settings().data_collection_enabled:
                        raise SourceFailure('collection_disabled')
                    _source(db, family, owner_id)
                    job = queue_dataset(db, owner, dataset, requested)
                    ids.append(str(job.id))
                    entry.update(state=job.state, job_id=str(job.id))
                except (SourceFailure, APIError) as error:
                    entry.update(state='gap', reason=getattr(error, 'reason', getattr(error, 'code', 'source_unavailable')))
            coverage.append(entry)
    for code in (INDEX_CODES if include_core else ()):
        saved = load_dataset(db, 'indices', source_day, code, include_rows=False)
        entry = {'dataset': 'review.indices', 'family': DATASET_FAMILIES['indices'], 'scope': code,
                 'source_day': str(source_day), 'state': saved['state'], 'rows': saved['row_count']}
        if saved['state'] != 'complete':
            try:
                if not settings().data_collection_enabled:
                    raise SourceFailure('collection_disabled')
                _source(db, DATASET_FAMILIES['indices'], owner_id)
                job = queue_dataset(db, owner, 'indices', source_day,
                    start_day=source_day - timedelta(days=150), scope=code)
                ids.append(str(job.id))
                entry.update(state=job.state, job_id=str(job.id))
            except (SourceFailure, APIError) as error:
                entry.update(state='gap', reason=getattr(error, 'reason', getattr(error, 'code', 'source_unavailable')))
        coverage.append(entry)
    for family in (('tushare_calendar_sse', 'tushare_calendar_szse') if include_core else ()):
        add(family, 'calendar')
    if include_core:
        add('review_cls_news', 'news_window')
        if (cutoff.astimezone(SHANGHAI).date() - source_day).days > 7:
            limitations.append('本批资讯只查询最近7日；更早假日期间由每日资讯恢复任务逐日准备，未完成时保留缺口。')
    for code in candidates:
        for family in SELECTED_FAMILIES:
            add(family, 'selected', code)
        add('cninfo_announcements', 'announcements', code)
    if not candidates:
        limitations.append('本批尚无候选股；公司财务、连续资金与公告正文将在候选确定后按股准备。')
    limitations.extend([
        '财经电报采用有界分页并保留窗口完成状态；供应商无法返回的时段不算全天完整。',
        '公告正文每股最多8份、元数据最多5页；PDF无可提取文本不等于没有公告风险。',
        '未来90日仅准备已授权交易日历与分红计划；解禁、减持、会议、主营构成和社会热词尚需独立来源与权限。',
        '当前先补当日及近5个已知交易日成员，历史更长成员缺口保留；补采接收时间不回填为历史已知时间。',
    ])
    db.add(AuditEvent(actor_id=owner_id, action='tomorrow.preparation.request', resource_type='job',
        changed_field_names=['bounded_cache_preparation', 'candidate_scope']))
    return {'job_ids': list(dict.fromkeys(ids)), 'coverage': coverage, 'limitations': limitations}


def queue_news_window(db, owner_id, day, cutoff):
    """Hourly/overnight news only; no repeated financial or all-market preparation."""
    if cutoff > now(db) or cutoff.astimezone(SHANGHAI).date() != day:
        raise APIError(422, 'invalid_preparation_window', '资讯准备窗口须属于当前已到达的日期。')
    try:
        if not settings().data_collection_enabled:
            raise SourceFailure('collection_disabled')
        job = _queue(db, owner_id, 'review_cls_news', 'news_window', day, cutoff)
        return {'job_ids': [str(job.id)], 'coverage': [{'dataset': 'news_window', 'state': job.state}], 'limitations': []}
    except SourceFailure as error:
        return {'job_ids': [], 'coverage': [{'dataset': 'news_window', 'state': 'gap', 'reason': error.reason}],
                'limitations': ['隔夜资讯来源尚未就绪：' + error.reason]}


def validate_preparation(db, job, clock):
    if not settings().data_collection_enabled:
        raise SourceFailure('collection_disabled')
    ctx = job.payload.get('preparation_context')
    if (job.kind != 'tomorrow.collect' or not isinstance(ctx, dict) or set(ctx) != {
            'version', 'operation', 'source_day', 'cutoff', 'code', 'configuration_sha256'} or ctx['version'] != VERSION
            or ctx['operation'] not in OPERATIONS
            or any(key in job.payload for key in ('review_context', 'minute_context', 'dated_context', 'reference_context', 'daily_history'))):
        raise SourceFailure('invalid_preparation_context')
    try:
        first, cutoff = date.fromisoformat(ctx['source_day']), datetime.fromisoformat(ctx['cutoff'])
        source_id = uuid.UUID(job.payload['source_id'])
        provider = db.get(Provider, source_id)
        if provider is None or provider.family not in OPERATIONS[ctx['operation']]:
            raise ValueError()
        provider, config = _source(db, provider.family, job.owner_id)
        maximum_days = 7 if ctx['operation'] == 'news_window' else 150
        if cutoff.utcoffset() is None or cutoff > clock + timedelta(seconds=1) or not first <= cutoff.astimezone(SHANGHAI).date() <= first + timedelta(days=maximum_days):
            raise ValueError()
        if ctx['operation'] in {'selected', 'announcements'}:
            candidate_key(ctx['code'])
        elif ctx['code'] is not None:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise SourceFailure('invalid_preparation_context') from None
    if configuration_fingerprint(provider, config) != ctx['configuration_sha256']:
        raise SourceFailure('configuration_changed')
    expected = content_hash({'source_id': str(provider.id), 'owner_id': str(job.owner_id), 'context': ctx})
    if expected != job.payload.get('preparation_key'):
        raise SourceFailure('invalid_preparation_context')
    binding = assert_source_binding(db, provider.id, provider.family, job.payload)
    return provider, config, ctx, binding


def candidate_parameters(family, key, source_day, cutoff):
    params = parameters(family, [key], cutoff)
    if family in {'tushare_daily_basic', 'tushare_adj_factor', 'tushare_moneyflow'}:
        params.update(start_date=(source_day - timedelta(days=180)).strftime('%Y%m%d'),
                      end_date=source_day.strftime('%Y%m%d'))
    return params


def _filtered_candidate(response, family, key, source_day, cutoff):
    fields, rows = table(response.body)
    spec = SPECS[family]
    if not {'ts_code'}.issubset(fields):
        raise SourceFailure('schema_changed')
    filtered = []
    for row in rows:
        if row['ts_code'] != tushare_code(key):
            raise SourceFailure('unexpected_instrument')
        if 'trade_date' in fields and tushare_day(row['trade_date']) > source_day:
            raise SourceFailure('future_source_time')
        # Financial reports are selected by disclosure, never by report period.
        if spec.api in {'income', 'balancesheet', 'cashflow', 'fina_indicator'}:
            announced = tushare_day(row.get('ann_date'))
            actual = tushare_day(row['f_ann_date']) if row.get('f_ann_date') else announced
            if actual < announced:
                raise SourceFailure('invalid_source_time')
            if max(actual, announced) > cutoff.astimezone(SHANGHAI).date():
                continue
        filtered.append(row)
    encoded = json.dumps({'code': 0, 'data': {'fields': fields,
        'items': [[row.get(field) for field in fields] for row in filtered]}}).encode()
    return parse_reference(encoded, family, response.received_at, key), len(rows)


def _locked(db, job):
    row = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running',
        Job.lease_token == job.lease_token, Job.lease_until > func.now()).with_for_update())
    if row is None:
        raise SourceFailure('lease_lost')
    if row.payload != job.payload:
        raise SourceFailure('configuration_changed')
    return row


def _reserve_cls(job):
    action = 'source.request.review.cls'  # Same shared allowance as review.news.
    with Session(engine()) as db, db.begin():
        lock(db, action)
        current = _locked(db, job)
        validate_preparation(db, current, now(db))
        clock = now(db)
        start = datetime.combine(clock.astimezone(SHANGHAI).date(), time.min, SHANGHAI)
        used = db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == action, AuditEvent.created_at >= start))
        last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == action))
        deadline = db.scalar(select(func.max(DataFetchRun.retry_not_before)).where(
            DataFetchRun.source_id == uuid.UUID(job.payload['source_id']), DataFetchRun.retry_not_before > clock))
        if last and last + timedelta(seconds=2) > clock:
            deadline = max(deadline or clock, last + timedelta(seconds=2))
        if used >= 1200:
            deadline = max(deadline or clock, start + timedelta(days=1))
        if deadline:
            current.state, current.available_at = 'queued', deadline
            current.attempts = max(0, current.attempts - 1)
            current.error_code = 'source_budget_wait'
            current.lease_token = current.lease_until = None
            return False
        db.add(AuditEvent(actor_id=current.owner_id, action=action, resource_type='job',
            resource_id=current.id, changed_field_names=['bounded_news_window_permit'], created_at=clock))
    return True


def _store_table(db, provider, config, job, dataset, fields, rows, response, slot):
    if len(rows) > 8000:
        raise SourceFailure('preparation_page_limit')
    payload = ReferencePayload(dataset=dataset, title='明日预案 · 研究资料',
        source_url=CNINFO_QUERY if dataset.startswith('tomorrow.announcement') else
            'https://www.cls.cn/telegraph' if dataset == 'tomorrow.news_window' else 'https://tushare.pro/',
        attribution=provider.name, scope='有界采集原文/清单；文本是待验证的外部材料。接收时间由服务器保存，缺失不补造。',
        columns=[{'key': field, 'label': field} for field in fields], rows=rows,
        observed_day=response.received_at.astimezone(SHANGHAI).date())
    source_key = f'tomorrow:{dataset}:{job.id}:{slot}'
    store_observation(db, provider, config, source_key=source_key, kind='reference',
        payload=payload, received_at=response.received_at, eligible=False)


def _manifest(db, provider, config, current, ctx, progress, response=None):
    from app.market.protocol import Response
    response = response or Response(b'', now(db), 0, 200)
    row = {key: str(value).lower() if type(value) is bool else str(value) if value is not None else None for key, value in {
        'kind': ctx['operation'], 'window_start': ctx['source_day'], 'window_end': ctx['cutoff'],
        'complete': progress.get('complete', False), 'reason': progress.get('reason'),
        'requests': progress.get('requests', 0), 'row_count': progress.get('row_count', 0),
        'job_id': str(current.id), 'code': ctx['code'],
        'configuration_sha256': ctx['configuration_sha256'],
    }.items()}
    _store_table(db, provider, config, current, 'tomorrow.preparation_manifest', list(row), [row], response, 'manifest')


def process_preparation(job):
    """Process one durable source step. Owns its job state, including requeue."""
    from app.market.budget import reserve_news_request, reserve_tushare_request
    started = datetime.now(UTC)
    duration_started = monotonic()
    with Session(engine()) as db:
        provider, config, ctx, bound = validate_preparation(db, job, now(db))
        progress = dict(job.result_ref or {})
    operation = ctx['operation']
    source_day, cutoff = date.fromisoformat(ctx['source_day']), datetime.fromisoformat(ctx['cutoff'])
    window_start = datetime.combine(source_day, time.min, SHANGHAI)
    response = None
    error = None
    fetched = False
    next_step = False
    rows = None
    parsed = None
    try:
        if operation == 'news_window':
            if not _reserve_cls(job):
                return
            cursor = progress.get('cursor', int(cutoff.timestamp()) + 1)
            fetched = True
            response = fetch_public(cls_url(cursor))
            rows, oldest, count = parse_news_window(response, window_start, cutoff)
            old_ids = set(progress.get('seen_ids', []))
            rows = [row for row in rows if row['id'] not in old_ids]
            page_ids = list(dict.fromkeys(row['id'] for row in rows))
            progress['seen_ids'] = [*old_ids, *page_ids]
            page = progress.get('page', 0) + 1
            progress.update(page=page, cursor=oldest, row_count=progress.get('row_count', 0) + len(page_ids))
            reached = oldest is not None and oldest < int(window_start.timestamp())
            exhausted = count < 20
            stuck = oldest is not None and oldest >= cursor
            next_step = not reached and not exhausted and not stuck and page < MAX_NEWS_PAGES
            progress.update(complete=reached,
                reason=None if reached else 'news_window_unavailable' if exhausted else
                    'news_cursor_stalled' if stuck else 'news_page_limit' if not next_step else 'page_pending')
        elif operation in {'calendar', 'selected'}:
            with Session(engine()) as db:
                validate_preparation(db, job, now(db))
            if not reserve_tushare_request(job):
                return
            spec = SPECS[provider.family]
            if operation == 'calendar':
                last = source_day + timedelta(days=FUTURE_CALENDAR_DAYS)
                params = {'exchange': spec.exchange, 'start_date': source_day.strftime('%Y%m%d'),
                          'end_date': last.strftime('%Y%m%d'), 'limit': 8000}
            else:
                key = candidate_key(ctx['code'])
                params = candidate_parameters(provider.family, key, source_day, cutoff)
            fetched = True
            response = tushare_request(spec.api, params, spec.fields, bound.credential, bound.endpoint)
            if operation == 'calendar':
                parsed = parse_calendar(response.body, spec.exchange, source_day, last)
                progress.update(complete=True, row_count=len(parsed), reason=None)
            else:
                parsed, count = _filtered_candidate(response, provider.family, key, source_day, cutoff)
                limited = count >= params['limit']
                progress.update(complete=not limited, row_count=sum(len(payload.rows) for _, payload, _ in parsed.records),
                    reason='source_row_limit' if limited else None)
        else:
            with Session(engine()) as db:
                validate_preparation(db, job, now(db))
            reserve_news_request(job, expected_configuration=ctx['configuration_sha256'])
            pending = list(progress.get('pending_documents', []))
            if not progress.get('org_id'):
                # Publisher search supplies its opaque organization ID. Guessing
                # one or sending the code alone can silently return an empty list.
                fetched = True
                response = fetch_https(CNINFO_LOOKUP, timeout=15, data=stock_lookup_request(ctx['code']))
                progress['org_id'] = parse_stock_lookup(response, ctx['code'])
                next_step = True
            elif pending:
                metadata = pending[0]
                fetched = True
                response = fetch_document(metadata['source_url'])
                text, text_status = extract_pdf(response.body)
                rows = announcement_rows(metadata, response.body, text=text, status=text_status)
                progress.update(pending_documents=pending[1:], documents=progress.get('documents', 0) + 1)
                if text_status != 'extracted':
                    progress['document_gap'] = text_status
                next_step = bool(pending[1:]) or progress.get('has_more', False)
            else:
                page = progress.get('page', 0) + 1
                fetched = True
                response = fetch_https(CNINFO_QUERY, timeout=15,
                    data=announcement_request(ctx['code'], max(source_day - timedelta(days=7), cutoff.astimezone(SHANGHAI).date() - timedelta(days=7)), cutoff.astimezone(SHANGHAI).date(), page, progress['org_id']))
                metadata, total, more = parse_announcements(response, ctx['code'], max(source_day - timedelta(days=7), cutoff.astimezone(SHANGHAI).date() - timedelta(days=7)),
                    cutoff.astimezone(SHANGHAI).date(), page)
                seen = set(progress.get('announcement_ids', []))
                fresh = [value for value in metadata if value['id'] not in seen]
                remaining = max(0, MAX_DOCUMENTS_PER_STOCK - progress.get('documents', 0))
                selected = fresh[:remaining]
                progress.update(page=page, total=total, has_more=more and page < MAX_ANNOUNCEMENT_PAGES,
                    pending_documents=selected, announcement_ids=[*seen, *(value['id'] for value in fresh)],
                    row_count=progress.get('row_count', 0) + len(fresh))
                if len(fresh) > remaining or total > MAX_DOCUMENTS_PER_STOCK:
                    progress['document_gap'] = 'document_limit_reached'
                if more and page >= MAX_ANNOUNCEMENT_PAGES:
                    progress['document_gap'] = 'announcement_page_limit'
                rows = [announcement_rows(value)[0] for value in fresh]
                next_step = bool(selected) or progress['has_more']
            progress.update(complete=not next_step and not progress.get('document_gap'),
                reason='page_pending' if next_step else progress.get('document_gap'))
    except SourceFailure as caught:
        error = caught
        if fetched and operation in {'calendar','selected'}:
            from app.market.tushare_cooldown import record_failure
            record_failure(job,caught)
    except (ValueError, TypeError, KeyError, OverflowError):
        error = SourceFailure('schema_changed')

    with Session(engine()) as db, db.begin():
        try:
            current = _locked(db, job)
        except SourceFailure:
            return
        provider = db.get(Provider, uuid.UUID(job.payload['source_id']))
        config = db.get(SourceSettings, provider.id) if provider else None
        try:
            validate_preparation(db, current, now(db))
        except SourceFailure as caught:
            error = caught
        handled_document_error = False
        if error is None:
            if operation == 'calendar':
                store_calendar_batch(db, provider, config, parsed, response.received_at, exchanges=(SPECS[provider.family].exchange,))
            elif operation == 'selected':
                store_reference_batch(db, provider, config, parsed, response.received_at)
            elif rows is not None:
                dataset = 'tomorrow.news_window' if operation == 'news_window' else 'tomorrow.announcement_text'
                fields = NEWS_FIELDS if operation == 'news_window' else ANNOUNCEMENT_FIELDS
                slot = str(progress.get('page', 0)) + ':' + str(progress.get('documents', 0))
                _store_table(db, provider, config, current, dataset, fields, rows, response, slot)
        elif operation == 'announcements' and progress.get('pending_documents'):
            # A failed PDF remains an explicit missing document; its title is
            # never substituted for text and the next document can still run.
            pending = list(progress['pending_documents'])
            recoverable_document = error.reason in {'document_not_pdf', 'response_too_large',
                'document_url_not_allowed', 'upstream_redirect', 'unsafe_upstream_response',
                'document_host_not_public'} or (error.reason in {'transport_error', 'timeout', 'upstream_unavailable'} and current.attempts >= 3)
            if fetched and response is None and recoverable_document:
                failed_rows = announcement_rows(pending[0], status=error.reason)
                from app.market.protocol import Response
                stamp = Response(b'', now(db), 0, 200)
                _store_table(db, provider, config, current, 'tomorrow.announcement_text', ANNOUNCEMENT_FIELDS,
                             failed_rows, stamp, 'failed:' + pending[0]['id'])
                progress.update(pending_documents=pending[1:], documents=progress.get('documents', 0) + 1,
                    document_gap=error.reason, complete=False, reason=error.reason)
                next_step = bool(pending[1:]) or progress.get('has_more', False)
                handled_document_error = True
                # Still retain the failed HTTP receipt below.
        stamp = response.received_at if response else now(db)
        progress['requests'] = progress.get('requests', 0) + int(fetched)
        retry = error is not None and not handled_document_error and error.reason in TRANSIENT and current.attempts < 3
        if error is not None and not retry and not handled_document_error:
            progress.update(complete=False, reason=error.reason)
            next_step = False
        retry_deadline=None
        if error:
            if operation in {'calendar','selected'}:
                retry_deadline=tushare_retry_deadline(stamp,error.reason,error.retry_after)
            elif error.reason=='rate_limited':
                retry_deadline=stamp+timedelta(seconds=error.retry_after or 60)
        db.add(DataFetchRun(source_id=provider.id, capability='tomorrow.' + operation, job_id=current.id,
            started_at=started, received_at=stamp, status='error' if error else 'partial' if next_step or not progress.get('complete') else 'complete',
            reason=error.reason if error else progress.get('reason'), expected=None,
            accepted=len(rows or []) if error is None and rows is not None else progress.get('row_count') if error is None else None,
            rejected=None if error else 0, duplicates=None if error else 0,
            elapsed_ms=max(0,int((monotonic() - duration_started) * 1000)), request_count=int(fetched), cost_cny=None,
            body_hash=hashlib.sha256(response.body).hexdigest() if response else None,
            retry_not_before=retry_deadline))
        if 'tushare_cooldown' in (current.result_ref or {}):progress['tushare_cooldown']=current.result_ref['tushare_cooldown']
        current.result_ref = progress
        if retry or next_step:
            current.state = 'queued'
            current.available_at = now(db) + timedelta(seconds=max(60, error.retry_after or 0) if error and not handled_document_error else 2.05)
            if error is None or handled_document_error:
                current.attempts = max(0, current.attempts - 1)
            current.completed_at = None
        else:
            current.state = 'failed' if error and not handled_document_error else 'succeeded'
            current.completed_at = now(db)
            if provider and config and permission_reason(provider) is None:
                _manifest(db, provider, config, current, ctx, progress, response)
        current.error_code = error.reason if error else None
        current.lease_token = current.lease_until = None
