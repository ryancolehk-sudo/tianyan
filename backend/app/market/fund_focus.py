"""Selected funds outside the saved ranking, using the existing minute endpoint.

Authenticated read projections express interest only. The independent scheduler
and leased worker own requests; rankings and archive cohorts stay independent.
"""
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.auth import APIError
from app.db import engine
from app.evidence.common import lock, now
from app.evidence.models import EvidenceVersion
from app.integrations.credentials import assert_source_binding, job_context, resolve_source_credentials
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import (SHANGHAI, SourceFailure, SourceRedirect, fetch_https,
    eastmoney_minute_allocation_url, fetch_eastmoney_minute_allocation)
from app.market.redirect_diagnostic import redirect_diagnostic
from app.models import Instrument, Job, Provider, User
from app.read_cache_models import ReadCache

FAMILY = 'eastmoney_funds'
KIND = 'funds.focus'
CAPABILITY = 'funds.selected_detail'
DATASET = 'market.eastmoney.funds.selected_detail'
MAX_TARGETS = 8
INTEREST = timedelta(minutes=5)
RETRY = (5, 15, 30, 60)
RETRYABLE = frozenset({'timeout', 'transport_error', 'upstream_unavailable', 'upstream_redirect',
    'schema_changed', 'unsafe_upstream_response', 'response_too_large', 'historical_series_empty',
    'historical_response_day_mismatch', 'historical_response_time_ahead', 'rate_limited'})


def _entry_query():
    # A board projection can contain thousands of members. Interest polling
    # needs only the selected identity, never those large cached histories.
    return select(ReadCache.key, ReadCache.owner_id, ReadCache.scope, ReadCache.operation,
        ReadCache.params, ReadCache.focused_at, ReadCache.payload['data']['stocks'].label('stocks'),
        ReadCache.payload['data']['sector_code'].label('sector_code'))


def _entry(row):
    return SimpleNamespace(**{name: getattr(row, name) for name in
        ('key', 'owner_id', 'scope', 'operation', 'params', 'focused_at')},
        payload={'data': {'stocks': row.stocks or [], 'sector_code': row.sector_code}})


def _context(entry, category, code, exchange, configuration):
    return dict(version=1, day=entry.params['day'], category=category, code=code, exchange=exchange,
        request_key=entry.key, requester_id=str(entry.owner_id), request_scope=entry.scope,
        configuration_sha256=configuration)


def _notify(db, requester):
    # Only the requester's current overview/funds health captions depend on
    # this key. A failed selection does not invalidate auction/ladder pages.
    db.execute(text('SELECT bump_read_dependency(:key)'), {'key':'funds-selection:'+str(requester)})


def _interest(db, entry, clock):
    from app.read_cache import cache_scope
    user = db.get(User, entry.owner_id)
    return bool(user and user.enabled and entry.operation == 'details' and entry.focused_at
        and clock - INTEREST <= entry.focused_at <= clock + timedelta(seconds=60)
        and entry.params.get('day') == clock.astimezone(SHANGHAI).date().isoformat()
        and not entry.params.get('as_of_time') and entry.scope == cache_scope(db, user))


def _identity(db, entry, clock):
    """Use identities from the permission-checked projection, never names."""
    if not _interest(db, entry, clock) or not entry.payload:
        return None
    data = entry.payload.get('data', {})
    codes = entry.params.get('detail_codes', [])
    if len(codes) == 1 and not entry.params.get('sector'):
        code = codes[0]
        rows = [row for row in data.get('stocks', []) if row.get('code') == code and row.get('instrument_id')]
        if len(rows) != 1:
            return None
        try:
            instrument = db.get(Instrument, uuid.UUID(rows[0]['instrument_id']))
        except (ValueError, TypeError):
            return None
        suffix = {'SSE': 'SH', 'SZSE': 'SZ', 'BSE': 'BJ'}.get(instrument.exchange) if instrument else None
        if instrument and instrument.asset_type == 'stock' and instrument.valid_to is None and suffix and code == instrument.symbol + '.' + suffix:
            return 'stock', instrument.symbol, instrument.exchange
    elif not codes and (sector := entry.params.get('sector')) and data.get('sector_code') == sector:
        # The selected directory may extend beyond the current ranking page.
        parents = db.execute(select(ReadCache.payload['data']['sectors'],
            ReadCache.payload['data']['intraday_funds']['sectors'],
            ReadCache.payload['data']['intraday_funds']['focused_sectors']).where(
            ReadCache.owner_id == entry.owner_id, ReadCache.scope == entry.scope,
            ReadCache.operation == 'workbench', ReadCache.payload['data']['trade_day'].astext == entry.params['day'],
            ReadCache.params['as_of_time'].astext.is_(None))).all()
        categories = {row.get('category') for parent in parents for rows in parent if isinstance(rows, list) for row in rows
            if isinstance(row, dict) and row.get('code') == sector and row.get('category') in {'concept', 'industry'}}
        if len(categories) == 1:
            from app.market.fund_history import _identity as market_identity
            category = categories.pop()
            try:
                market_identity(category, sector.removesuffix('.DC'), None)
            except SourceFailure:
                return None
            return category, sector.removesuffix('.DC'), None
    return None


def _ranked(db, provider, context, clock, interval):
    dataset = 'market.eastmoney.' + context['category'] + '_funds'
    evidence = db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id == provider.id,
        EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared',
        EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= clock,
        EvidenceVersion.payload['dataset'].astext == dataset,
        EvidenceVersion.payload['observed_day'].astext == context['day'])
        .order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(1))
    if evidence is None:
        return False
    if not any(column.get('key') == 'net_inflow_cny' and column.get('unit') == 'CNY'
        for column in evidence.payload.get('columns', []) if isinstance(column, dict)):
        return False
    for row in evidence.payload.get('rows', []):
        if row.get('code') != context['code'] or context['category'] == 'stock' and row.get('exchange') != context['exchange']:
            continue
        from app.market.resilience import stamp
        at = stamp(row.get('source_time'))
        if at and row.get('net_inflow_cny') is not None and row.get('trade_day') == context['day'] and at <= clock and (clock - at).total_seconds() <= interval:
            return True
    return False


def _recent_shared(db, provider, context, clock, interval):
    return _shared_observation(db, provider, context, clock, interval=interval)


def _shared_observation(db, provider, context, clock, *, interval=None):
    key = 'reference:funds.selected:' + ':'.join((context['day'], context['category'], context['code']))
    query = select(EvidenceVersion).where(EvidenceVersion.source_id == provider.id,
        EvidenceVersion.source_key == key, EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared',
        EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= clock,
        EvidenceVersion.payload['dataset'].astext == DATASET)
    if interval is not None:
        query = query.where(EvidenceVersion.received_at >= clock - timedelta(seconds=interval))
    evidence = db.scalar(query.order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(1))
    if evidence and evidence.payload.get('rows') and any(column.get('key') == 'net_inflow_cny' and column.get('unit') == 'CNY'
        for column in evidence.payload.get('columns', []) if isinstance(column, dict)):
        from app.market.resilience import stamp
        row = evidence.payload['rows'][-1]
        at = stamp(row.get('source_time'))
        if (row.get('code') == context['code'] and row.get('category') == context['category']
            and row.get('exchange') == context.get('exchange') and row.get('trade_day') == context['day']
            and row.get('net_inflow_cny') is not None and at and at <= clock and at <= evidence.received_at
            and (interval is None or (clock-at).total_seconds() <= interval)):
            return evidence, at
    return None


def schedule():
    if not settings().data_collection_enabled:
        return
    from app.maintenance_control import admit, paused
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.realtime_schedule import deferred_realtime
    from app.market.service import permission_reason, source_id
    with Session(engine()) as db, db.begin():
        if paused(db):
            return
        admit(db)
        clock = now(db)
        provider = db.get(Provider, source_id(FAMILY))
        config = db.get(SourceSettings, provider.id) if provider else None
        owner = db.get(User, config.owner_id) if config else None
        if not provider or not config or not owner or not owner.enabled or owner.role != 'admin' or permission_reason(provider) or config.paused_reason:
            return
        if deferred_realtime(db, FAMILY, clock, config.interval_seconds) is not None:
            return
        bound = resolve_source_credentials(db, provider.id, FAMILY, refresh=True)
        if bound.reason:
            return
        configuration = configuration_fingerprint(provider, config)
        lock(db, 'funds.selected.schedule')
        pending = db.scalar(select(func.count()).select_from(Job).where(Job.kind == KIND,
            Job.payload['source_id'].astext == str(provider.id), Job.state.in_(['queued','running'])))
        if pending >= MAX_TARGETS:
            return
        entries = db.execute(_entry_query().where(ReadCache.operation == 'details',
            ReadCache.focused_at >= clock - INTEREST, ReadCache.payload.is_not(None),
            ReadCache.params['day'].astext == clock.astimezone(SHANGHAI).date().isoformat())
            .order_by(ReadCache.focused_at.desc(), ReadCache.key).limit(32))
        seen = set()
        for entry_row in entries:
            entry = _entry(entry_row)
            identity = _identity(db, entry, clock)
            interest = (entry.owner_id, identity)
            if identity is None or interest in seen:
                continue
            seen.add(interest)
            if len(seen) > MAX_TARGETS:
                break
            context = _context(entry, *identity, configuration)
            if _ranked(db, provider, context, clock, config.interval_seconds):
                continue
            latest = db.scalar(select(Job).where(Job.kind == KIND,
                Job.payload['source_id'].astext == str(provider.id),
                Job.payload['funds_focus_context']['day'].astext == context['day'],
                Job.payload['funds_focus_context']['requester_id'].astext == str(entry.owner_id),
                Job.payload['funds_focus_context']['category'].astext == context['category'],
                Job.payload['funds_focus_context']['code'].astext == context['code'])
                .order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
            if latest and (latest.state in {'queued', 'running'} or
                latest.error_code not in {'funds_focus_expired', 'configuration_changed'} and
                (latest.completed_at or latest.created_at) + timedelta(seconds=config.interval_seconds) > clock):
                continue
            payload = {'source_id': str(provider.id), 'funds_focus_context': context}
            if binding := job_context(bound, provider.id):
                payload['integration_context'] = binding
            db.add(Job(owner_id=owner.id, kind=KIND, payload=payload, created_at=clock, available_at=clock))
            _notify(db, entry.owner_id)
            pending += 1
            if pending >= MAX_TARGETS:
                break


def _validate(db, job, clock):
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.service import permission_reason, source_id
    context = job.payload.get('funds_focus_context')
    if not isinstance(context, dict) or context.get('version') != 1 or job.payload.get('source_id') != str(source_id(FAMILY)):
        raise SourceFailure('funds_focus_context_invalid')
    try:
        row = db.execute(_entry_query().where(ReadCache.key == context['request_key'])).first()
        entry = _entry(row) if row else None
        requester = uuid.UUID(context['requester_id'])
    except (KeyError, TypeError, ValueError):
        raise SourceFailure('funds_focus_context_invalid') from None
    if (not entry or entry.owner_id != requester or entry.scope != context.get('request_scope')
        or context.get('day') != clock.astimezone(SHANGHAI).date().isoformat()
        or _identity(db, entry, clock) != (context.get('category'), context.get('code'), context.get('exchange'))):
        raise SourceFailure('funds_focus_expired')
    provider = db.get(Provider, source_id(FAMILY))
    config = db.get(SourceSettings, provider.id) if provider else None
    owner = db.get(User, job.owner_id)
    if not provider or not config or permission_reason(provider) or config.paused_reason or not owner or not owner.enabled or owner.role != 'admin':
        raise SourceFailure('permission_missing')
    if job.owner_id != config.owner_id or configuration_fingerprint(provider, config) != context.get('configuration_sha256'):
        raise SourceFailure('configuration_changed')
    bound = assert_source_binding(db, provider.id, FAMILY, job.payload)
    return provider, config, context, bound


def parse(body, received_at, context):
    return _parse_selected(body, received_at, context, elapsed=False)[0]


def _parse_selected(body, received_at, context, *, elapsed):
    from app.market.fund_history import parse_history, parse_elapsed_history
    identity = {name: context[name] for name in ('day', 'category', 'code', 'exchange')}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    checked = dict(**identity, cohort_sha256=digest, cohort_count=1)
    if elapsed:
        payload, _, rejected = parse_elapsed_history(body, received_at, checked)
    else:
        payload, _ = parse_history(body, received_at, checked)
        rejected = None
    scope = ('现有东方财富资金分钟接口的单个所选对象；金额为元，源分钟和实际接收时间独立。'
             '仅补充该对象，不补排行、不以日频替代、不推导未返回的总流入或总流出。')
    if rejected is not None:
        scope += (f" 本批仅保留接收时刻已到的{rejected['retained_minutes']}个源分钟，"
                  f"{rejected['excluded_future_minutes']}个未来分钟未纳入，不代表完整分钟序列。")
    return payload.model_copy(update={'dataset': DATASET,
        'title': '所选对象资金分钟 · ' + payload.rows[0]['name'], 'scope': scope}), rejected


def process_job(job):
    from app.maintenance_control import admit, paused
    from app.market.budget import reserve_eastmoney_request
    from app.market.collect import store_reference_batch
    from app.market.fund_history import FundHistoryTimeMismatch, history_url
    from app.market.reference import ReferenceBatch
    from app.market.realtime_schedule import deferred_realtime
    from app.worker import finish
    if not settings().data_collection_enabled:
        finish(job.id, job.lease_token, error_code='collection_disabled')
        return
    started = datetime.now(UTC)
    response = payload = bound = None
    future_tail = None
    reason = None
    retry_after = None
    deadline = None
    requests = 0
    last_time_rejection = (job.result_ref or {}).get('last_time_rejection')
    last_transport_rejection = (job.result_ref or {}).get('last_transport_rejection')
    request_url = None
    retry_interval = 0

    def validate_request(db):
        nonlocal bound, provider, config, context, deadline, retry_interval
        if paused(db):
            raise SourceFailure('maintenance_paused')
        admit(db)
        clock = now(db)
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running',
            Job.lease_token == job.lease_token, Job.lease_until > clock).with_for_update())
        if current is None:
            raise SourceFailure('lease_expired')
        if current.payload != job.payload or current.owner_id != job.owner_id:
            raise SourceFailure('configuration_changed')
        provider, config, context, again = _validate(db, current, clock)
        if bound is not None:
            assert_source_binding(db, provider.id, FAMILY, current.payload, frozen=bound)
        bound = again
        retry_interval = config.interval_seconds
        deadline = deferred_realtime(db, FAMILY, clock, config.interval_seconds)
        if deadline is not None:
            raise SourceFailure('intraday_window_wait')
        return current, clock

    provider = config = context = None
    try:
        for reserve in (False, True):
            if reserve:
                reserve_eastmoney_request(job, minimum_interval=10)
            with Session(engine()) as db, db.begin():
                current, clock = validate_request(db)
                if not reserve and (shared := _recent_shared(db, provider, context, clock, config.interval_seconds)):
                    evidence, at = shared
                    current.state, current.completed_at = 'succeeded', clock
                    current.lease_token = current.lease_until = None
                    current.error_code = None
                    current.result_ref = {'source_time': at.isoformat(), 'received_at': evidence.received_at.isoformat(),
                        'selected_detail': True, 'reused_saved_observation': True}
                    if last_time_rejection is not None:
                        current.result_ref['last_time_rejection'] = last_time_rejection
                    if last_transport_rejection is not None:
                        current.result_ref['last_transport_rejection'] = last_transport_rejection
                    _notify(db, context['requester_id'])
                    return
        requests = 1
        request_url = history_url(context['category'], context['code'], context.get('exchange'))
        try:
            response = fetch_https(request_url)
        except SourceRedirect as redirect:
            previous = last_transport_rejection
            last_transport_rejection = redirect_diagnostic(request_url, redirect) or previous
            # The first actual rejection is only recorded. A later normal retry
            # must observe the same permitted shape before attempting one hop.
            target = eastmoney_minute_allocation_url(request_url, redirect, observed_rejection=previous)
            reserve_eastmoney_request(job, minimum_interval=10)
            with Session(engine()) as db, db.begin():
                validate_request(db)
            requests += 1
            response = fetch_eastmoney_minute_allocation(target, original_url=request_url,
                observed_rejection=previous)
        payload, future_tail = _parse_selected(response.body, response.received_at, context, elapsed=True)
        if future_tail is not None:
            last_time_rejection = dict(future_tail, body_hash=hashlib.sha256(response.body).hexdigest())
        if requests == 2:
            payload = payload.model_copy(update={'scope': payload.scope +
                ' 本批经原请求实际返回并再次核对的同路径、同参数服务分配节点取得；仅一次跳转。'})
    except (SourceFailure, APIError) as error:
        reason = error.reason if isinstance(error, SourceFailure) else 'maintenance_paused' if error.code == 'maintenance_active' else error.code
        retry_after = getattr(error, 'retry_after', None)
        if diagnostic := redirect_diagnostic(request_url, error):
            last_transport_rejection = diagnostic
        if isinstance(error, FundHistoryTimeMismatch) and response is not None:
            last_time_rejection = dict(error.diagnostic, body_hash=hashlib.sha256(response.body).hexdigest())
    received = response.received_at if response else datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        clock = now(db)
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running',
            Job.lease_token == job.lease_token, Job.lease_until > clock).with_for_update())
        if current is None:
            return
        if current.payload != job.payload or current.owner_id != job.owner_id:
            reason = 'configuration_changed'
        if reason is None:
            try:
                if paused(db):
                    raise SourceFailure('maintenance_paused')
                admit(db)
                provider, config, context, again = _validate(db, current, clock)
                assert_source_binding(db, provider.id, FAMILY, current.payload, frozen=bound)
            except (SourceFailure, APIError) as error:
                reason = error.reason if isinstance(error, SourceFailure) else 'maintenance_paused' if error.code == 'maintenance_active' else error.code
        accepted = duplicates = 0
        if reason is None and future_tail is not None:
            # A shorter elapsed prefix must not replace a later source minute
            # already saved for this exact date/category/identity.
            previous = _shared_observation(db, provider, context, clock)
            if previous and datetime.fromisoformat(payload.rows[-1]['source_time']) < previous[1]:
                last_time_rejection = dict(last_time_rejection, retained_minutes=0,
                    discarded_older_minutes=len(payload.rows))
                payload = None
                reason = 'historical_response_time_ahead'
        if reason is None:
            key = 'funds.selected:' + ':'.join((context['day'], context['category'], context['code']))
            accepted, duplicates = store_reference_batch(db, provider, config,
                ReferenceBatch(records=[(key, payload, None)], expected=1, requests=requests), received)
        if requests:
            db.add(DataFetchRun(source_id=uuid.UUID(job.payload['source_id']), job_id=current.id,
                capability=CAPABILITY, started_at=started, received_at=received,
                status='error' if reason else 'partial' if future_tail else 'complete',
                reason=reason or ('historical_response_time_ahead' if future_tail else None), expected=1,
                accepted=accepted, duplicates=duplicates, rejected=0, request_count=requests,
                elapsed_ms=response.elapsed_ms if response else None, cost_cny=None,
                retry_not_before=received + timedelta(seconds=max(120, min(86400, retry_after or 300))) if reason == 'rate_limited' else None,
                body_hash=hashlib.sha256(response.body).hexdigest() if response else None))
        if reason in RETRYABLE or reason in {'intraday_window_wait', 'maintenance_paused', 'local_request_wait'}:
            step = min(4, (current.result_ref or {}).get('retry_step', 0) + int(bool(requests)))
            seconds = RETRY[max(0, step - 1)]
            if reason in {'timeout', 'transport_error', 'upstream_unavailable'}:
                seconds = max(seconds, retry_interval)
            if reason == 'local_request_wait':
                seconds = max(2, retry_after or 2)
            if reason == 'rate_limited':
                seconds = max(seconds, max(120, min(86400, retry_after or 300)))
                if not requests:
                    from app.market.budget import eastmoney_cooldown_until
                    deadline = eastmoney_cooldown_until(db, clock, job=current) or deadline
            current.state = 'queued'
            current.available_at = deadline or clock + timedelta(seconds=seconds)
            # Handled network failures have their own persisted retry stage;
            # the lease counter continues to bound actual worker crashes.
            current.attempts = 0
        else:
            current.state = 'failed' if reason else 'succeeded'
            current.completed_at = clock
        current.error_code = reason
        current.lease_token = current.lease_until = None
        current.result_ref = {'source_time': payload.rows[-1]['source_time'] if payload else None,
            'retry_step': step if current.state == 'queued' else 0,
            'received_at': received.isoformat() if requests else None, 'selected_detail': True}
        if last_time_rejection is not None:
            current.result_ref['last_time_rejection'] = last_time_rejection
        if last_transport_rejection is not None:
            current.result_ref['last_transport_rejection'] = last_transport_rejection
        _notify(db, current.payload['funds_focus_context']['requester_id'])


def saved(db, provider, day, cutoff, owner_id):
    if owner_id is None:
        return [], []
    jobs=list(db.scalars(select(Job).where(Job.kind == KIND,
        Job.payload['source_id'].astext == str(provider.id), Job.created_at <= cutoff,
        Job.payload['funds_focus_context']['requester_id'].astext == str(owner_id),
        Job.payload['funds_focus_context']['day'].astext == day.isoformat())
        .order_by(Job.created_at.desc(), Job.id.desc()).limit(32)))
    selected={}
    for job in jobs:
        context=job.payload['funds_focus_context']
        key=(context['category'],context['code'])
        if key not in selected and len(selected)<MAX_TARGETS:
            selected[key]=job
    allowed=set(selected)
    keys=['reference:funds.selected:'+':'.join((day.isoformat(),category,code)) for category,code in allowed]
    evidence=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == provider.id,
        EvidenceVersion.kind == 'reference', EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.visibility == 'shared', EvidenceVersion.provenance == 'source_reported',
        EvidenceVersion.received_at <= cutoff, EvidenceVersion.source_key.in_(keys), EvidenceVersion.payload['dataset'].astext == DATASET,
        EvidenceVersion.payload['observed_day'].astext == day.isoformat())
        .distinct(EvidenceVersion.source_key).order_by(EvidenceVersion.source_key,
            EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(MAX_TARGETS)))
    evidence=[row for row in evidence if row.payload.get('rows') and
        (row.payload['rows'][0].get('category'),row.payload['rows'][0].get('code')) in allowed]
    return evidence, list(selected.values())
