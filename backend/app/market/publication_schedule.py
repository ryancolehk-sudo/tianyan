"""Local polling policy, not an upstream publication-time promise."""
from datetime import datetime, time, timedelta

from app.market.protocol import SHANGHAI, SourceFailure

LIMITS='tushare_market_limits'
WINDOWS={LIMITS:(time(9),time(15,1)),
         'tushare_limit_list_d':(time(16),time(21,30)),
         'tushare_limit_list_ths':(time(16),time(21,30)),
         'tushare_top_list':(time(16,30),time(21,30)),
         'tushare_top_inst':(time(16,30),time(21,30))}
WINDOWS.update({family:(time(16),time(21,30)) for family in (
    'tushare_dc_index','tushare_dc_member','tushare_moneyflow_ind_dc','tushare_moneyflow_dc','tushare_limit_step',
    'tushare_direction_industry','tushare_direction_industry_funds')})
WINDOWS['tushare_stk_premarket']=(time(9),time(21,30))
WINDOWS['tushare_moneyflow_mkt_dc']=(time(16,30),time(21,30))
WINDOWS['tushare_ths_daily']=(time(19),time(21,30))
MINIMUM_INTERVAL=900


def in_publication_window(family, clock):
    if family not in WINDOWS:return True
    start,end=WINDOWS[family]
    return start<=clock.astimezone(SHANGHAI).time()<end


def align_publication(family, clock):
    if family not in WINDOWS:return clock
    local=clock.astimezone(SHANGHAI);start,end=WINDOWS[family]
    if local.time()<start:return datetime.combine(local.date(),start,SHANGHAI)
    if local.time()>=end:return datetime.combine(local.date()+timedelta(days=1),start,SHANGHAI)
    return clock


def publication_spacing(family, interval, *, unpublished=False):
    # An unpublished daily table is not a successful daily refresh. Retry
    # within today's publication window, using the same audited source budget.
    if unpublished and family in WINDOWS and family != LIMITS:
        return MINIMUM_INTERVAL
    return max(interval, MINIMUM_INTERVAL)


def next_publication(family, clock, interval, *, unpublished=False):
    return align_publication(family,clock+timedelta(seconds=publication_spacing(family,interval,unpublished=unpublished)))


def publication_allowed(db, family, clock):
    """Require an observed current open day, never infer weekdays/holidays."""
    if not in_publication_window(family,clock):return False
    from app.market.mairui import pool_day
    try:
        target=pool_day(db,clock,preopen=True) if family in {LIMITS,'tushare_stk_premarket'} else pool_day(db,clock)
        return target==clock.astimezone(SHANGHAI).date()
    except SourceFailure:return False


def deferred_publication(db, family, clock, interval):
    """None means eligible now; otherwise persist a bounded next attempt."""
    if family not in WINDOWS:return None
    if not in_publication_window(family,clock):return align_publication(family,clock)
    if not publication_allowed(db,family,clock):
        # Missing/stale calendars and closed days may be corrected later.
        # Limits need a preopen retry; neither calendar recheck makes HTTP.
        spacing=MINIMUM_INTERVAL if family==LIMITS else 3600
        return align_publication(family,clock+timedelta(seconds=max(spacing,interval)))
    # A complete fetch means one validated table, not complete market coverage.
    # Keep bounded refreshes even after success to observe expanded/corrected
    # tables. Never declare the entire day complete from a nonempty subset.
    return None


def repair_unpublished_waits(db, clock):
    """Recover a pre-upgrade six-hour wait without restarting or editing config."""
    from sqlalchemy import select
    from app.market.models import SourceSettings, DataFetchRun
    from app.market.minute_backfill import live_run_condition
    from app.models import Provider, Job
    candidates = db.execute(select(Provider, SourceSettings).join(SourceSettings,
        SourceSettings.source_id == Provider.id).where(
        Provider.family.in_(set(WINDOWS) - {LIMITS}), Provider.enabled.is_(True),
        SourceSettings.paused_reason.is_(None), SourceSettings.next_run_at > clock))
    for provider, config in candidates:
        if not in_publication_window(provider.family, clock):
            continue
        last = db.scalar(select(DataFetchRun).where(DataFetchRun.source_id == provider.id,
            live_run_condition()).order_by(DataFetchRun.received_at.desc()).limit(1))
        if last is None or last.reason != 'daily_data_not_published' or last.received_at.astimezone(SHANGHAI).date() != clock.astimezone(SHANGHAI).date():
            continue
        pending = db.scalar(select(Job.id).where(Job.kind == 'source.collect',
            Job.payload['source_id'].astext == str(provider.id), Job.state.in_(['queued', 'running'])).limit(1))
        if pending is None:
            config.next_run_at = min(config.next_run_at, max(clock,
                next_publication(provider.family, last.received_at, config.interval_seconds, unpublished=True)))
