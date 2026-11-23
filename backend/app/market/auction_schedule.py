"""Shanghai collection windows; these are polling policy, not a trading calendar.

Days and publication times still come from validated source evidence. A daily
window also runs when calendar coverage is unknown; it never invents an open day.
"""
from datetime import datetime, time, timedelta

from app.market.protocol import SHANGHAI

CURRENT = 'duanxianxia_auction'
MATCHED = 'tushare_stk_auction'
BREADTH = 'ashare_breadth'
WINDOWS = {CURRENT: (time(9, 15), time(9, 35)), MATCHED: (time(9, 26), time(9, 35))}
LIVE_INTERVAL_SECONDS = 3
SCHEMA_INTERVAL_SECONDS = 5
RANKING_RECOVERY = frozenset({'eastmoney_funds','eastmoney_boards'})
DIRECTION_RECOVERY = frozenset({'direction_em_hot', 'direction_kpl_strength'})
FAST_DIRECTION_RECOVERY = frozenset({'direction_kpl_top'})
SCHEMA_RECOVERY = frozenset({CURRENT, BREADTH}) | DIRECTION_RECOVERY | FAST_DIRECTION_RECOVERY
SCHEMA_RECOVERY = SCHEMA_RECOVERY | {'tencent_auction'}


def collection_spacing(family, clock, interval, *, schema_probe=False):
    if family == CURRENT:
        return (SCHEMA_INTERVAL_SECONDS if schema_probe else
                LIVE_INTERVAL_SECONDS if in_window(CURRENT, clock) else max(interval, LIVE_INTERVAL_SECONDS))
    return max(interval, 65) if family in WINDOWS else interval


def in_window(family, clock):
    if family not in WINDOWS:
        return False
    start, end = WINDOWS[family]
    local = clock.astimezone(SHANGHAI).time()
    return start <= local < end


def next_collection(family, clock, interval, *, schema_probe=False, unpublished=False, probe_diagnostic=None):
    """Respect configured spacing and wake for the next auction, even overnight."""
    if family == CURRENT:
        from app.market.realtime_schedule import align_realtime
        local = clock.astimezone(SHANGHAI)
        opening = datetime.combine(local.date(), WINDOWS[CURRENT][0], SHANGHAI)
        if clock < opening:
            return max(opening, clock + timedelta(seconds=collection_spacing(
                family, opening, interval, schema_probe=schema_probe)))
        return align_realtime(family, clock + timedelta(seconds=collection_spacing(
            family, clock, interval, schema_probe=schema_probe)))
    if family == BREADTH and schema_probe:
        from app.market.breadth_recovery import next_probe
        return next_probe(clock, interval)
    if family in RANKING_RECOVERY and schema_probe:
        from app.market.realtime_schedule import align_realtime
        return align_realtime(family, clock + timedelta(seconds=60))
    if family in FAST_DIRECTION_RECOVERY and schema_probe:
        from app.market.realtime_schedule import align_realtime
        from app.market.kpl_order_recovery import interval_seconds
        return align_realtime(family,clock+timedelta(seconds=interval_seconds(family,interval,probe_diagnostic)))
    if family in DIRECTION_RECOVERY and schema_probe:
        from app.market.intraday_schedule import next_intraday
        return next_intraday(clock, max(interval, 900))
    from app.market.publication_schedule import WINDOWS as PUBLICATION_WINDOWS, next_publication
    if family in PUBLICATION_WINDOWS:
        return next_publication(family,clock,interval,unpublished=unpublished)
    from app.market.realtime_schedule import FAMILIES as REALTIME, align_realtime
    if family in REALTIME and family not in WINDOWS:
        from app.market.cadence_policy import automatic_interval
        spacing = automatic_interval(family,collection_spacing(family, clock, interval, schema_probe=schema_probe),clock)
        return align_realtime(family, clock + timedelta(seconds=spacing))
    from app.market.intraday_schedule import FAMILIES as LIVE, next_intraday
    from app.market.eastmoney_trading import FAMILIES as PUBLIC_RANKINGS
    if family in LIVE or family in PUBLIC_RANKINGS:
        return next_intraday(clock, interval, family=family)
    if family not in WINDOWS:
        from app.market.reference_schedule import align_reference
        return align_reference(family, clock + timedelta(seconds=interval))
    local = clock.astimezone(SHANGHAI)
    start, end = WINDOWS[family]
    spacing = collection_spacing(family, clock, interval, schema_probe=schema_probe)
    if not in_window(family, clock):
        spacing = max(spacing, 3600 if family == CURRENT else 21600)
    candidate = clock + timedelta(seconds=spacing)
    opening = datetime.combine(local.date(), start, SHANGHAI)
    if opening <= clock:
        opening += timedelta(days=1)
    # A request just before opening must not push the next poll out by an
    # entire off-hours interval. Respect its permit, then enter this window.
    window_poll = max(opening, clock + timedelta(seconds=collection_spacing(
        family, opening, interval, schema_probe=schema_probe)))
    closing = datetime.combine(opening.date(), end, SHANGHAI)
    if window_poll < closing and window_poll < candidate:
        candidate = window_poll
    return align_realtime(family, candidate)


def schema_recheck(family, reason):
    # Never recover permission failures, changed decoder bytes or
    # unapproved destinations automatically. Every probe still checks the pin,
    # full table contract, lease and permissions. Auction and breadth retain
    # their existing permits; the two direction sources have a 900-second fence.
    # Retry only the original pinned endpoint, never follow a redirect. A
    # transient redirect or malformed response must not permanently stop funds.
    if family in RANKING_RECOVERY:
        return reason in {'schema_changed','upstream_redirect'}
    if family in {'fuyao_quotes','fuyao_indices'}:
        return reason in {'schema_changed','upstream_redirect'}
    return family in SCHEMA_RECOVERY and reason == 'schema_changed'
