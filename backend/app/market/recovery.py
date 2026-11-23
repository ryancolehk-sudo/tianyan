"""Local-data reconciliation. Only queued workers may contact a supplier.

Coverage is a derived, durable ledger, not market evidence. In particular a
successful HTTP/job or the newest row in a partition never proves completeness.
"""
from datetime import datetime, time, timedelta
import logging
from time import monotonic
import uuid

from sqlalchemy import case, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import APIError, require_instance
from app.config import settings
from app.db import engine
from app.evidence.common import content_hash, lock, normalized, now
from app.evidence.models import EvidenceVersion
from app.market.models import SourceSettings, DataFetchRun
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.recovery_models import RecoveryCoverage
from app.models import AuditEvent, Instrument, Job, Provider, User

ACTION = 'market.recovery.reconcile'
MAX_SOURCE_DAYS = 8
MAX_ROUND_SECONDS = 90
MAX_SOURCE_SECONDS = 30
STOP_REASONS = frozenset({'permission_missing', 'configuration_changed', 'schema_changed',
    'upstream_redirect', 'unsafe_upstream_response', 'endpoint_not_allowed', 'credentials_missing',
    'source_disabled', 'source_paused', 'source_route_not_selected', 'points_not_enabled'})


def minute_slots(day, interval, clock):
    """Nominal regular-session observations; gaps do not prove missing trades."""
    step = int(interval.removesuffix('m'))
    slots = set()
    for opening in (time(9, 30), time(13)):
        base = datetime.combine(day, opening, SHANGHAI)
        for offset in range(step, 121, step):
            stamp = base + timedelta(minutes=offset)
            if stamp <= clock - timedelta(seconds=30):
                slots.add(stamp)
    return slots


def ranges(missing, step_minutes):
    result = []
    for value in sorted(missing):
        if result and value == datetime.fromisoformat(result[-1]['end']) + timedelta(minutes=step_minutes):
            result[-1]['end'] = value.isoformat()
            result[-1]['count'] += 1
        else:
            result.append(dict(start=value.isoformat(), end=value.isoformat(), count=1))
    return result


def budget_available(db):
    deadline = db.info.get('recovery.deadline')
    return deadline is None or monotonic() < deadline


def target(db, provider, config, dataset, day, scope, interval, clock):
    from app.market.minute_backfill import configuration_fingerprint
    from app.integrations.credentials import resolve_source_credentials
    # Connection revisions change independently from a source's polling config.
    # Persist only public binding metadata, never credentials or secret hashes.
    cache_key = ('recovery.binding', provider.id)
    if cache_key not in db.info:
        bound = resolve_source_credentials(db, provider.id, provider.family)
        db.info[cache_key] = dict(connection_id=bound.connection_id, revision=bound.revision,
            endpoint=bound.endpoint, enabled=bound.enabled, reason=bound.reason,
            credential_configured=bool(bound.credential))
    signature = content_hash(normalized(dict(configuration=configuration_fingerprint(provider, config),
        binding=db.info[cache_key])))
    key = content_hash(normalized(scope))
    row = db.scalar(select(RecoveryCoverage).where(RecoveryCoverage.owner_id == config.owner_id,
        RecoveryCoverage.source_id == provider.id, RecoveryCoverage.dataset == dataset,
        RecoveryCoverage.scope_key == key, RecoveryCoverage.trade_day == day,
        RecoveryCoverage.interval == interval).with_for_update())
    if row is None:
        row = RecoveryCoverage(owner_id=config.owner_id, source_id=provider.id, dataset=dataset,
            scope_key=key, scope=scope, trade_day=day, interval=interval, state='missing',
            missing_ranges=[], checked_at=clock, configuration_sha256=signature)
        db.add(row)
    elif row.configuration_sha256 != signature:
        row.configuration_sha256 = signature
        row.job_id = None
        row.next_retry_at = None
        row.state, row.reason = 'missing', None
    row.checked_at = clock
    return row


def pending(db, row, clock):
    if row.job_id:
        job = db.get(Job, row.job_id)
        if job is not None and job.owner_id == row.owner_id:
            if job.state in {'queued', 'running'}:
                row.state = job.state
                row.reason = job.error_code
                row.next_retry_at = job.available_at if job.state == 'queued' else job.lease_until
                return True
            if job.state == 'failed' and job.error_code in STOP_REASONS:
                row.state, row.reason = 'blocked', job.error_code
                return True
            if job.state == 'failed' and row.next_retry_at is None:
                row.next_retry_at = (job.completed_at or clock) + timedelta(minutes=15)
    if row.next_retry_at and row.next_retry_at > clock:
        if row.state != 'blocked':
            row.state = 'waiting'
        return True
    return False


def queue_error(row, error, clock):
    reason = getattr(error, 'code', None) or getattr(error, 'reason', None) or 'recovery_unavailable'
    row.reason = reason
    row.state = 'blocked' if reason in STOP_REASONS else 'waiting'
    row.next_retry_at = clock + timedelta(minutes=15)


def closed_days(db, config, clock):
    from app.market.review_store import available_trade_days
    days = available_trade_days(db, limit=260, as_of_time=clock)
    if not days:
        return []
    first = config.configured_at.astimezone(SHANGHAI).date()
    previous = db.scalar(select(func.min(RecoveryCoverage.trade_day)).where(
        RecoveryCoverage.source_id == config.source_id, RecoveryCoverage.owner_id == config.owner_id))
    if previous:
        first = min(first, previous)
    # A fresh installation also needs the latest two closed sessions to give
    # yesterday's fixed-pool comparisons a real, explicitly dated baseline.
    return sorted(set([day for day in days if day >= first] + days[-2:]), reverse=True)


def bounded_days(db, provider, config, days):
    """Keep two current baselines; rotate older days by persisted check time."""
    ordered = sorted(set(days), reverse=True)
    if len(ordered) <= MAX_SOURCE_DAYS:
        return ordered
    checked = dict(db.execute(select(RecoveryCoverage.trade_day, func.max(RecoveryCoverage.checked_at)).where(
        RecoveryCoverage.source_id == provider.id, RecoveryCoverage.owner_id == config.owner_id,
        RecoveryCoverage.trade_day.in_(ordered)).group_by(RecoveryCoverage.trade_day)).all())
    head, older = ordered[:2], ordered[2:]
    older.sort(key=lambda day: (day in checked, checked[day].timestamp() if day in checked else 0, day))
    return head + older[:MAX_SOURCE_DAYS-len(head)]


def daily_coverage(db, provider, config, dataset, scope, days, clock, allowance):
    from app.market.review_jobs import queue_dataset
    from app.market.review_store import load_dataset
    owner = db.get(User, config.owner_id)
    for day in days:
        if not budget_available(db):
            break
        row = target(db, provider, config, 'review.' + dataset, day, {'scope': scope}, 'day', clock)
        saved = load_dataset(db, dataset, day, scope, as_of_time=clock, include_rows=False)
        row.actual_count = saved.get('row_count')
        row.expected_count = None  # Page completeness is not whole-market coverage.
        row.missing_ranges = []
        if saved['state'] == 'complete' and (saved.get('row_count') or 0) > 0:
            row.state, row.reason, row.next_retry_at = 'complete', None, None
            continue
        if day < clock.astimezone(SHANGHAI).date()-timedelta(days=365):
            row.state, row.reason, row.next_retry_at = 'unsupported', 'local_history_window_expired', None
            continue
        if pending(db, row, clock):
            continue
        row.state, row.reason = 'missing', 'empty_not_confirmed' if saved['state'] == 'complete' else saved['state']
        if allowance[0] <= 0:
            continue
        try:
            with db.begin_nested():
                job = queue_dataset(db, owner, dataset, day, scope=scope, force=True)
            row.job_id, row.state, row.reason = job.id, job.state, None
            row.next_retry_at = clock + timedelta(minutes=15)
            allowance[0] -= 1
        except (APIError, SourceFailure) as error:
            queue_error(row, error, clock)


def minute_coverage(db, provider, config, days, clock, allowance):
    from app.market.minute import FAMILY_INTERVALS
    from app.market.minute_backfill import LOOKBACK_DAYS
    from app.market.minute_read import _clock
    from app.market.service import enqueue_collection
    symbols = config.config.get('symbols', [])
    if len(symbols) != 1:
        return
    symbol = symbols[0]
    instrument = db.scalar(select(Instrument).where(Instrument.exchange == symbol.get('exchange'),
        Instrument.symbol == symbol.get('symbol'), Instrument.asset_type == symbol.get('asset_type', 'stock'),
        Instrument.valid_to.is_(None)).order_by(Instrument.id).limit(1))
    interval = FAMILY_INTERVALS[provider.family]
    for day in days:
        if not budget_available(db):
            break
        row = target(db, provider, config, 'minute.bars', day,
            {'instrument': symbol, 'adjustment': 'unadjusted'}, interval, clock)
        expected = minute_slots(day, interval, clock)
        actual = set()
        if instrument is not None:
            ranked = select(EvidenceVersion.id, func.row_number().over(
                partition_by=EvidenceVersion.source_key,
                order_by=EvidenceVersion.version_number.desc()).label('position')).where(
                EvidenceVersion.source_id == provider.id, EvidenceVersion.instrument_id == instrument.id,
                EvidenceVersion.kind == 'bar', EvidenceVersion.owner_id.is_(None),
                EvidenceVersion.visibility == 'shared', EvidenceVersion.received_at <= clock,
                EvidenceVersion.payload['trade_day'].astext == day.isoformat(),
                EvidenceVersion.payload['interval'].astext == interval,
                EvidenceVersion.payload['adjustment'].astext == 'unadjusted').subquery()
            for (payload,) in db.execute(select(EvidenceVersion.payload).join(ranked,
                ranked.c.id == EvidenceVersion.id).where(ranked.c.position == 1)):
                stamp = _clock(payload)
                if stamp and stamp <= clock:
                    actual.add(stamp)
        missing = expected - actual
        row.expected_count, row.actual_count = len(expected), len(expected & actual)
        row.missing_ranges = ranges(missing, int(interval.removesuffix('m')))
        if expected and not missing:
            row.state, row.reason, row.next_retry_at = 'complete', None, None
            continue
        if (clock.astimezone(SHANGHAI).date() - day).days > LOOKBACK_DAYS:
            row.state, row.reason, row.next_retry_at = 'unsupported', 'historical_window_expired', None
            continue
        if not expected:
            row.state, row.reason = 'waiting', 'session_not_started'
            continue
        if pending(db, row, clock):
            continue
        row.state, row.reason = ('partial' if actual else 'missing'), 'nominal_slots_missing'
        if allowance[0] <= 0:
            continue
        try:
            job = enqueue_collection(db, provider.id, day=day)
            row.job_id, row.state = job.id, job.state
            row.next_retry_at = clock + timedelta(minutes=15)
            allowance[0] -= 1
        except (APIError, SourceFailure) as error:
            queue_error(row, error, clock)


def fund_coverage(db, provider, config, days, clock):
    from app.market.fund_history import saved_archives, minute_slots as fund_slots
    for day in days:
        if not budget_available(db):
            break
        plans = saved_archives(db, provider, day, clock)
        scope = target(db, provider, config, 'funds.scope', day, {'scope':'configured_source'}, 'day', clock)
        scope.expected_count = None
        scope.actual_count = sum(len(plan['targets']) for plan in plans) if plans else None
        scope.missing_ranges, scope.next_retry_at = [], None
        scope.state, scope.reason = ('complete', None) if plans else ('unsupported', 'historical_scope_unverified')
        checked = dict(db.execute(select(RecoveryCoverage.scope_key, RecoveryCoverage.checked_at).where(
            RecoveryCoverage.source_id==provider.id, RecoveryCoverage.owner_id==config.owner_id,
            RecoveryCoverage.dataset=='funds.minute_history', RecoveryCoverage.trade_day==day)).all())
        for plan in plans:
            def order(code):
                key=content_hash(normalized({'category':plan['category'],'code':code}))
                return key in checked, checked[key].timestamp() if key in checked else 0, code
            for code in sorted(plan['targets'], key=order):
                if not budget_available(db):
                    break
                row = target(db, provider, config, 'funds.minute_history', day,
                    {'category': plan['category'], 'code': code}, '1m', clock)
                saved = plan['series'].get(code)
                # The parser has already checked source identity, calendar,
                # units and all 240 original source slots for complete=True.
                expected={stamp for stamp in fund_slots(day) if stamp<=clock-timedelta(seconds=30)}
                row.expected_count = len(expected)
                stamps = {datetime.fromisoformat(value['source_time']) for value in saved[1]} if saved else set()
                row.actual_count = len(stamps & expected)
                row.missing_ranges = ranges(expected-stamps, 1)
                complete = bool(saved and saved[2])
                row.state = 'complete' if complete else 'partial' if stamps else 'missing'
                row.reason = None if complete else 'session_in_progress' if expected.issubset(stamps) else 'history_not_complete'
                if not expected:
                    row.state,row.reason='waiting','session_not_started'
                if not complete and day < clock.astimezone(SHANGHAI).date():
                    row.state, row.reason = 'unsupported', 'historical_window_unverified'
                elif not complete:
                    latest=db.scalar(select(Job).where(Job.kind=='funds.archive',Job.owner_id==config.owner_id,
                        Job.payload['source_id'].astext==str(provider.id),
                        Job.payload['funds_history_context']['day'].astext==day.isoformat(),
                        Job.payload['funds_history_context']['category'].astext==plan['category'],
                        Job.payload['funds_history_context']['code'].astext==code).order_by(Job.created_at.desc()).limit(1))
                    if latest:
                        row.job_id=latest.id
                        pending(db,row,clock)


def direction_coverage(db, provider, config, days, clock, allowance):
    from app.market.direction_archive import _saved
    from app.market.minute_backfill import configuration_fingerprint
    from app.integrations.credentials import job_context, resolve_source_credentials
    from app.market.review_store import available_trade_days
    for day in days:
        if not budget_available(db):
            break
        row = target(db, provider, config, 'direction.daily_history', day, {'scope':'top10'}, 'day', clock)
        row.expected_count = 10
        if _saved(db, provider.family, day):
            row.state, row.reason, row.actual_count = 'complete', None, 10
            row.next_retry_at = None
            continue
        if pending(db, row, clock) or allowance[0] <= 0:
            continue
        lock(db, 'direction:archive:' + str(provider.id) + ':' + str(day))
        active = db.scalar(select(Job).where(Job.kind == 'direction.archive',
            Job.payload['kpl_counts'].astext.is_distinct_from('true'),
            Job.payload['kpl_leaders'].astext.is_distinct_from('true'),
            Job.owner_id == config.owner_id, Job.payload['source_id'].astext == str(provider.id),
            Job.payload['day'].astext == day.isoformat(), Job.state.in_(['queued','running'])).limit(1))
        if active:
            row.job_id, row.state = active.id, active.state
            continue
        bound = resolve_source_credentials(db,provider.id,provider.family)
        if bound.reason:
            row.state, row.reason = 'blocked', bound.reason
            continue
        previous = available_trade_days(db,limit=1,end_day=day-timedelta(days=1),as_of_time=clock)
        payload = dict(source_id=str(provider.id), day=day.isoformat(),
            previous_day=previous[-1].isoformat() if previous else None,
            configuration_sha256=configuration_fingerprint(provider, config))
        binding = job_context(bound,provider.id)
        if binding is not None:
            payload['integration_context'] = binding
        job = Job(owner_id=config.owner_id, kind='direction.archive', payload=payload)
        db.add(job); db.flush()
        row.job_id, row.state = job.id, 'queued'
        row.next_retry_at = clock+timedelta(minutes=15)
        allowance[0] -= 1


def breadth_coverage(db, provider, config, days, clock, allowance):
    from app.market.breadth_history import saved_series, enqueue_breadth_archive
    for day in days:
        if not budget_available(db):
            break
        row = target(db, provider, config, 'breadth.minute_history', day, {'scope':'source_universe'}, '1m', clock)
        saved = saved_series(db, day, clock)
        row.expected_count = 240
        if saved and saved['state'] == 'complete':
            row.state, row.reason, row.next_retry_at = 'complete', None, None
            row.actual_count = len(saved['points'])
            continue
        if pending(db,row,clock) or allowance[0] <= 0:
            continue
        try:
            job=enqueue_breadth_archive(db,provider,config,day,recovery=True)
            row.job_id,row.state=job.id,job.state
            row.next_retry_at=clock+timedelta(minutes=15)
            allowance[0]-=1
        except (APIError,SourceFailure) as error:
            queue_error(row,error,clock)


def unsupported_snapshots(db, provider, config, days, clock):
    """Keep the distinction between a historical bar and an old live snapshot.

    None of these configured adapters has a verified general snapshot-replay
    endpoint. A day candle or daily popularity rank cannot fill those events.
    """
    from math import ceil
    from app.market.realtime_schedule import windows
    for day in days:
        if not budget_available(db):
            break
        row=target(db,provider,config,'realtime.snapshots',day,{'scope':'configured_source'},'sample',clock)
        interval=max(1,config.interval_seconds)
        expected=set();actual=set();segments=[]
        for start,end in windows(provider.family):
            opening=datetime.combine(day,start,SHANGHAI)
            ending=min(datetime.combine(day,end,SHANGHAI),clock)
            count=max(0,ceil((ending-opening).total_seconds()/interval))
            segments.append((opening,ending,count))
            expected.update(opening+timedelta(seconds=number*interval) for number in range(count))
        runs=db.scalars(select(DataFetchRun).join(Job,Job.id==DataFetchRun.job_id).where(
            DataFetchRun.source_id==provider.id,DataFetchRun.received_at<=clock,
            DataFetchRun.started_at>=datetime.combine(day,time.min,SHANGHAI),
            DataFetchRun.started_at<datetime.combine(day+timedelta(days=1),time.min,SHANGHAI),
            DataFetchRun.status.in_(['complete','partial']),
            func.coalesce(DataFetchRun.accepted,0)+func.coalesce(DataFetchRun.duplicates,0)>0,
            Job.kind=='source.collect',~Job.payload.has_key('minute_context'),
            ~Job.payload.has_key('daily_history'),~Job.payload.has_key('dated_context'),
            ~Job.payload.has_key('reference_context'),~Job.payload.has_key('full_minute_context')))
        for run in runs:
            for opening,ending,count in segments:
                if opening<=run.started_at<ending:
                    actual.add(opening+timedelta(seconds=int((run.started_at-opening).total_seconds()//interval)*interval))
        missing=expected-actual
        row.expected_count=len(expected)
        row.actual_count=len(expected & actual)
        row.missing_ranges=ranges(missing,interval/60)
        row.state='unsupported' if missing else 'partial'
        row.reason='snapshot_replay_not_verified' if missing else 'snapshot_scope_not_verified'
        row.next_retry_at=None


def emotion_snapshot_coverage(db, provider, config, day, clock):
    from app.market.kpl_emotion import CHART_WINDOWS, DATASET

    row = target(db, provider, config, 'realtime.snapshots', day, {'scope':'configured_source'}, 'sample', clock)
    interval = timedelta(seconds=config.interval_seconds)
    expected = set()
    actual = set()
    start_of_day = datetime.combine(day, time.min, SHANGHAI)
    receipts = list(db.scalars(select(EvidenceVersion.received_at).where(
        EvidenceVersion.source_id == provider.id, EvidenceVersion.kind == 'reference',
        EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared',
        EvidenceVersion.provenance == 'source_reported',
        EvidenceVersion.received_at >= start_of_day, EvidenceVersion.received_at < start_of_day + timedelta(days=1),
        EvidenceVersion.received_at <= clock, EvidenceVersion.payload['dataset'].astext == DATASET,
        EvidenceVersion.payload['observed_day'].astext == day.isoformat())))
    for start, end in CHART_WINDOWS:
        opening = datetime.combine(day, start, SHANGHAI)
        ending = min(clock, datetime.combine(day, end, SHANGHAI))
        # Only elapsed sampling slots are counted, and each receipt stays at its own time.
        count = max(0, (ending - opening) // interval)
        expected.update(opening + interval * i for i in range(count))
        actual.update(opening + interval * ((stamp - opening) // interval)
                      for stamp in receipts if opening <= stamp < opening + interval * count)
    missing = expected - actual
    row.expected_count, row.actual_count = len(expected), len(actual)
    row.missing_ranges = ranges(missing, interval.total_seconds() / 60)
    row.next_retry_at = None
    if missing:
        row.state, row.reason = 'unsupported', 'emotion_snapshot_current_only'
    elif expected:
        row.state, row.reason = 'partial', 'emotion_snapshot_slots_observed'
    else:
        row.state, row.reason = 'waiting', 'session_not_started'


def reconcile(*, force=False, progress=None):
    config_settings = settings()
    if not config_settings.data_collection_enabled or not config_settings.recovery_enabled:
        return
    from app.maintenance_control import admit, paused
    from app.market.minute import FAMILY_INTERVALS
    from app.market.review_protocol import DATASET_FAMILIES, INDEX_CODES
    from app.market.service import permission_reason
    from app.market.session import market_session
    from app.market.realtime_schedule import COLLECTION_END, FAMILIES as LIVE_FAMILIES
    started = monotonic()
    with Session(engine()) as db, db.begin():
        db.execute(text("SET LOCAL lock_timeout = '2s'"))
        db.execute(text("SET LOCAL statement_timeout = '15s'"))
        require_instance(db)
        if paused(db):
            return
        admit(db)
        lock(db, ACTION)
        clock = now(db)
        last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == ACTION))
        cooldown = 30 if force else config_settings.recovery_interval_seconds
        if last and last + timedelta(seconds=cooldown) > clock:
            return
        db.add(AuditEvent(action=ACTION, resource_type='recovery',
            changed_field_names=['startup' if force else 'continuous', 'coverage_reconciliation'], created_at=clock))
        oldest = select(RecoveryCoverage.source_id, func.max(RecoveryCoverage.checked_at).label('checked')).group_by(
            RecoveryCoverage.source_id).subquery()
        source_ids = list(db.scalars(select(Provider.id).join(SourceSettings, SourceSettings.source_id == Provider.id)
            .outerjoin(oldest, oldest.c.source_id == Provider.id).order_by(case((Provider.family.in_(
                ['tushare_calendar_sse','tushare_calendar_szse']),0),else_=1),
                oldest.c.checked.asc().nullsfirst(), Provider.family)))
    if progress:
        progress()
    allowance = config_settings.recovery_jobs_per_round
    datasets = {family: name for name, family in DATASET_FAMILIES.items()
                if name not in {'calendar_sse', 'calendar_szse', 'tencent_minutes', 'sina_minutes', 'news'}}
    for source_id in source_ids:
        if monotonic()-started >= MAX_ROUND_SECONDS:
            break
        local_allowance = [min(3, allowance)]
        initial_allowance = local_allowance[0]
        try:
            # Each source commits independently. A failed later source or a
            # killed scheduler cannot roll back earlier coverage and intents.
            with Session(engine()) as db, db.begin():
                db.execute(text("SET LOCAL lock_timeout = '2s'"))
                db.execute(text("SET LOCAL statement_timeout = '15s'"))
                if paused(db):
                    return
                admit(db)
                lock(db, 'recovery.source:' + str(source_id))
                db.info['recovery.deadline'] = min(started+MAX_ROUND_SECONDS, monotonic()+MAX_SOURCE_SECONDS)
                provider, config = db.get(Provider, source_id), db.get(SourceSettings, source_id)
                if provider is None or config is None:
                    continue
                owner = db.get(User, config.owner_id)
                if owner is None or not owner.enabled or owner.role != 'admin':
                    continue
                clock = now(db)
                history_recheck = provider.family in {'direction_em_hot','direction_kpl_strength'} and config.paused_reason=='schema_changed'
                denied = permission_reason(provider) or (None if history_recheck else config.paused_reason)
                if denied:
                    db.execute(update(RecoveryCoverage).where(RecoveryCoverage.source_id == source_id,
                        RecoveryCoverage.owner_id == config.owner_id).values(state='blocked', reason=denied, checked_at=clock))
                elif provider.family in {'tushare_calendar_sse','tushare_calendar_szse'}:
                    from app.market.recovery_calendar import calendar_coverage
                    calendar_coverage(db, provider, config, clock, local_allowance)
                else:
                    days = closed_days(db, config, clock)
                    session = market_session(db, clock)
                    if provider.family in {*FAMILY_INTERVALS, 'eastmoney_funds', 'direction_kpl_emotion'} and session.current_day_active:
                        days = sorted(set(days+[session.trade_day]), reverse=True)
                    days = bounded_days(db, provider, config, days)
                    if (provider.family == 'direction_kpl_emotion' and session.current_day_active
                            and session.trade_day in days):
                        # A daily archive cannot reconstruct missing live samples.
                        # Check receipts separately and keep current-day archive
                        # recovery after the local close receipt window.
                        emotion_snapshot_coverage(db, provider, config, session.trade_day, clock)
                        if clock.astimezone(SHANGHAI).time() < COLLECTION_END:
                            days = [day for day in days if day != session.trade_day]
                    if provider.family in datasets:
                        dataset = datasets[provider.family]
                        scopes = tuple(INDEX_CODES) if dataset == 'indices' else ('all',)
                        checked = dict(db.execute(select(RecoveryCoverage.scope_key,
                            func.max(RecoveryCoverage.checked_at)).where(RecoveryCoverage.source_id==source_id,
                            RecoveryCoverage.dataset=='review.'+dataset).group_by(RecoveryCoverage.scope_key)).all())
                        def scope_order(value):
                            key = content_hash(normalized({'scope':value}))
                            return key in checked, checked[key].timestamp() if key in checked else 0, value
                        for scope in sorted(scopes, key=scope_order):
                            if not budget_available(db):
                                break
                            daily_coverage(db, provider, config, dataset, scope, days, clock, local_allowance)
                    elif provider.family in FAMILY_INTERVALS:
                        minute_coverage(db, provider, config, days, clock, local_allowance)
                    elif provider.family == 'eastmoney_funds':
                        fund_coverage(db, provider, config, days, clock)
                    elif provider.family in {'direction_em_hot','direction_kpl_strength'}:
                        direction_coverage(db, provider, config, days, clock, local_allowance)
                    elif provider.family == 'xgb_breadth_history':
                        breadth_coverage(db, provider, config, days, clock, local_allowance)
                    elif provider.family == 'direction_kpl_emotion':
                        for day in days:
                            if not budget_available(db):
                                break
                            emotion_snapshot_coverage(db, provider, config, day, clock)
                    elif provider.family in LIVE_FAMILIES:
                        unsupported_snapshots(db, provider, config, days, clock)
            allowance -= initial_allowance-local_allowance[0]
            if progress:
                progress()
        except (SQLAlchemyError, APIError, SourceFailure):
            logging.getLogger('jiulong.recovery').warning(
                'source reconciliation deferred source_id=%s; earlier batches preserved', source_id)
    # A removed configuration can never leave a permanently green ledger.
    with Session(engine()) as db, db.begin():
        db.execute(text("SET LOCAL lock_timeout = '2s'"))
        db.execute(text("SET LOCAL statement_timeout = '15s'"))
        db.execute(update(RecoveryCoverage).where(RecoveryCoverage.source_id.not_in(
            select(SourceSettings.source_id))).values(state='blocked', reason='source_not_configured', checked_at=now(db)))
