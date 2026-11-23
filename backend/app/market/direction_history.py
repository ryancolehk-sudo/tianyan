"""Explicit, bounded KPL archive reads; independent of current-list collection.

The job caller must check its saved exchange calendar, provider rights, budget
and configuration binding. This adapter enforces a closed date and never falls
back to a current endpoint when that historical response is unavailable.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.market import direction_sources
from app.market.protocol import SHANGHAI, Response, SourceFailure
from app.market.reference import ReferenceBatch

FAMILY = 'direction_kpl_strength'
MAX_LOOKBACK_DAYS = 365


@dataclass(frozen=True)
class HistoryResult:
    requested_day: date
    response: Response
    batch: ReferenceBatch


def validate_day(day, clock):
    if (type(day) is not date or not isinstance(clock, datetime) or clock.utcoffset() is None):
        raise SourceFailure('invalid_request_day')
    local = clock.astimezone(SHANGHAI)
    if not local.date() - timedelta(days=MAX_LOOKBACK_DAYS) <= day <= local.date():
        raise SourceFailure('invalid_request_day')
    if day == local.date() and local.time() < time(15, 5):
        raise SourceFailure('historical_day_not_closed')
    return day


def fetch_history(day, *, clock=None):
    """One Date-bound HTTPS request and strict source-day/strength validation."""
    requested = validate_day(day, clock or datetime.now(SHANGHAI))
    response = direction_sources.fetch(FAMILY, day=requested)
    try:
        batch = direction_sources.parse(FAMILY, response.body, response.received_at, requested_day=requested)
    except SourceFailure as error:
        # Let the caller save a normal failed-fetch receipt and safe diagnostic,
        # while keeping all response bytes out of exception/log strings.
        error.response = response
        error.request_count = response.request_count
        raise
    return HistoryResult(requested, response, batch)
