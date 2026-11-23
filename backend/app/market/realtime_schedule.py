"""Automatic sources retain their own windows and a quiet quote lunch break.

Daily publications and explicit historical collection have separate schedules.
Persisted jobs recheck this gate after a restart and before each upstream call.
"""
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import now
from app.market.protocol import SHANGHAI, SourceFailure

FAMILIES = frozenset({
    'direction_kpl_top',
    'direction_kpl_emotion',
    'ths_industry_funds','ths_concept_funds',
    'tushare_rt_k', 'tushare_market_rt', 'tushare_rt_indices',
    'ashare_public', 'ashare_breadth', 'ashare_sector_heat', 'tickflow_live', 'tencent_web', 'tencent_market_rt',
    'eastmoney_funds', 'eastmoney_boards', 'mairui_funds',
    'direction_ths_hot', 'direction_tdx_hot', 'direction_em_hot', 'direction_kpl_strength',
    'sina_minute_1m', 'sina_minute_5m', 'sina_minute_15m', 'sina_minute_30m', 'sina_minute_60m',
    'fuyao_quotes', 'fuyao_indices', 'tdx_quotes', 'tdx_depth5', 'tdx_minute',
    'duanxianxia_auction', 'tushare_stk_auction', 'tencent_auction', 'fuyao_auction',
})

# Queue deadlines give a fast feed a head start, never permanent precedence.
# They are service targets, not a promise about an upstream's publication time.
RETRY_SECONDS = (5, 15, 30, 60)
MAX_LIVE_ATTEMPTS = 5
CALENDAR_RECHECK_SECONDS = 5
# A bounded receipt window lets the final 15:00 batch arrive after market close.
# Trading phase, source clocks, configured spacing and shared quotas stay intact.
COLLECTION_END = time(15, 5)


def service_grace(family):
    if family in {'tencent_web','direction_kpl_top'}:
        return 3
    if family in {'tushare_calendar_sse', 'tushare_calendar_szse'}:
        return 0
    if family in {'duanxianxia_auction','fuyao_auction'}:
        return 3
    if family in {'eastmoney_funds', 'eastmoney_boards', 'mairui_funds'}:
        return 30
    return 15


def retry_seconds(failures):
    return RETRY_SECONDS[min(max(1, failures), len(RETRY_SECONDS)) - 1]


class RealtimeWait(Exception):
    """No request was made; the persisted task is waiting for the next session."""


def windows(family):
    from app.market.cadence_policy import QUOTE_FAMILIES
    if family=='tencent_market_rt':
        return ((time(9,30),time(11,31)),(time(13),COLLECTION_END))
    if family in QUOTE_FAMILIES:
        return ((time(9,15),time(11,31)),(time(13),COLLECTION_END))
    if family == 'fuyao_auction':
        return ((time(9,15),time(9,29)),)
    if family == 'tencent_auction':
        return ((time(9, 15), time(9, 26)),)
    if family in {'direction_kpl_emotion','direction_kpl_top'}:
        return ((time(9, 15), time(11, 30)), (time(13), COLLECTION_END))
    # Matched-auction data still waits for its actual publication window.
    opening = time(9, 26) if family == 'tushare_stk_auction' else time(9, 15)
    # Popularity continues changing after the exchange closes. Quotes retain
    # their own 15:05 gate; ranking snapshots remain bounded to the local day.
    end = time(23) if family in {'direction_ths_hot', 'direction_tdx_hot', 'direction_em_hot'} else COLLECTION_END
    return ((opening, end),)


def align_realtime(family, clock):
    local = clock.astimezone(SHANGHAI)
    for start, end in windows(family):
        if local.time() < start:
            return datetime.combine(local.date(), start, SHANGHAI)
        if local.time() < end:
            return clock
    return datetime.combine(local.date() + timedelta(days=1), windows(family)[0][0], SHANGHAI)


def deferred_realtime(db, family, clock, interval):
    candidate = align_realtime(family, clock)
    if candidate > clock:
        return candidate
    from app.market.mairui import pool_day
    try:
        day = (pool_day(db,clock,preopen=True) if clock.astimezone(SHANGHAI).time()<time(9,30)
               else pool_day(db,clock))
        if day == clock.astimezone(SHANGHAI).date():
            return None
    except SourceFailure:
        pass
    # This is a local calendar check, not a supplier request. An hourly delay
    # would strand a healthy feed after calendar recovery or a late startup.
    return align_realtime(family, clock + timedelta(seconds=CALENDAR_RECHECK_SECONDS))


def wake_session_waits(db, clock):
    """Release only persisted session waits whose current calendar is ready.

    Supplier/backoff waits keep their own available_at and are never advanced.
    The physical request still rechecks the calendar, lease and shared permit.
    """
    from app.market.models import SourceSettings
    from app.market.budget import family_cooldown_until
    from app.models import Job, Provider
    import uuid
    waiting = db.scalars(select(Job).where(Job.kind == 'source.collect',
        Job.state == 'queued', Job.error_code.in_(['intraday_window_wait', 'calendar_not_ready']),
        Job.available_at > clock).with_for_update(skip_locked=True).limit(64))
    for job in waiting:
        if not automatic(job.payload):
            continue
        try:
            source = uuid.UUID(job.payload['source_id'])
        except (KeyError, TypeError, ValueError):
            continue
        provider, config = db.get(Provider, source), db.get(SourceSettings, source)
        if provider is None or config is None or provider.family not in FAMILIES:
            continue
        cooldown = family_cooldown_until(db, provider.family, clock)
        if cooldown is not None:
            job.available_at = max(job.available_at, cooldown)
            config.next_run_at = max(config.next_run_at, cooldown)
            continue
        if deferred_realtime(db, provider.family, clock, config.interval_seconds) is None:
            job.available_at = clock
            config.next_run_at = min(config.next_run_at, clock)


def automatic(payload):
    return any(payload.get(key) is True for key in (
        'scheduled_realtime', 'scheduled_intraday', 'scheduled_minute',
        'scheduled_eastmoney', 'schema_probe'))


def permit_realtime(job, *, expected_configuration=None):
    if not automatic(job.payload):
        return True
    from app.market.models import SourceSettings
    from app.market.minute_backfill import configuration_fingerprint
    from app.models import Job, Provider
    import uuid
    with Session(engine()) as db, db.begin():
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running',
            Job.lease_token == job.lease_token).with_for_update())
        clock = now(db)
        if current is None or current.lease_until is None or current.lease_until <= clock:
            return False
        provider = db.get(Provider, uuid.UUID(current.payload['source_id']))
        if provider is None or provider.family not in FAMILIES:
            return True
        config = db.get(SourceSettings, provider.id)
        if config is None or current.payload != job.payload or current.owner_id != config.owner_id:
            raise SourceFailure('configuration_changed')
        if expected_configuration is not None and configuration_fingerprint(provider, config) != expected_configuration:
            raise SourceFailure('configuration_changed')
        deadline = deferred_realtime(db, provider.family, clock, config.interval_seconds)
        if deadline is None:
            return True
        current.state, current.available_at = 'queued', deadline
        current.attempts = max(0, current.attempts - 1)
        current.error_code = 'intraday_window_wait'
        current.lease_token = current.lease_until = None
        config.next_run_at = max(config.next_run_at, deadline)
        return False
