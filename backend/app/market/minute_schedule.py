"""Minute polling uses observed calendars, never weekday or synthetic candles."""
from app.market.intraday_schedule import align_intraday, deferred_intraday
from app.market.minute import MINIMUM_INTERVALS
from datetime import timedelta


def next_minute(family, clock, interval):
    return align_intraday(clock + timedelta(seconds=max(MINIMUM_INTERVALS[family], interval)))


def deferred_minute(db, family, clock, interval, selection):
    return deferred_intraday(db, clock, max(MINIMUM_INTERVALS[family], interval), selection)
