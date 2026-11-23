"""Bounded polling for one explicit Shanghai/Shenzhen stock, not tick streaming."""
from datetime import datetime, time, timedelta

from app.market.protocol import SHANGHAI, SourceFailure
from app.market.realtime_schedule import COLLECTION_END

FAMILY = 'tushare_rt_k'
FAMILIES = frozenset({FAMILY,'tushare_market_rt','tushare_rt_indices'})
MINIMUM_INTERVAL = 60
# Current snapshots get a bounded receipt window for the final close batch.
WINDOWS = ((time(9, 15), time(11,31)), (time(13),COLLECTION_END))


def in_intraday_window(clock):
    local = clock.astimezone(SHANGHAI).time()
    return any(start <= local < end for start, end in WINDOWS)


def align_intraday(clock):
    local = clock.astimezone(SHANGHAI)
    for start, end in WINDOWS:
        if local.time() < start:
            return datetime.combine(local.date(), start, SHANGHAI)
        if local.time() < end:
            return clock
    return datetime.combine(local.date() + timedelta(days=1), WINDOWS[0][0], SHANGHAI)


def next_intraday(clock, interval, *, family=None):
    from app.market.cadence_policy import automatic_interval
    return align_intraday(clock + timedelta(seconds=automatic_interval(family, interval, clock)))


def deferred_intraday(db, clock, interval, selection, *, batch=False):
    """No weekday guesses or HTTP without an observed current open day.

    The current shared calendar proves Shanghai/Shenzhen only. Other exchanges
    remain available for explicit manual requests, pending their own calendar.
    """
    if not in_intraday_window(clock):
        return align_intraday(clock)
    from app.market.mairui import pool_day
    try:
        selected = (len(selection) == 1 and selection[0].get('exchange') in {'SSE', 'SZSE'}
                    and selection[0].get('asset_type') == 'stock')
        day = ((pool_day(db,clock,preopen=True) if clock.astimezone(SHANGHAI).time()<time(9,30)
                else pool_day(db,clock)) if selected or batch else None)
        if (selected or batch) and day == clock.astimezone(SHANGHAI).date():
            return None
    except SourceFailure:
        pass
    return align_intraday(clock + timedelta(seconds=max(3600, interval)))
