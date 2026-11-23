"""Bounded calendar recovery before enumerating historical trading sessions.

Every calendar date needs an actual source receipt, including closed dates.
Legacy monthly receipts count without requiring a newer review manifest. This
module queues work only; it never contacts a supplier or guesses weekdays.
"""
from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy import func, select

from app.auth import APIError
from app.evidence.models import EvidenceVersion
from app.market.models import SourceSettings
from app.market.protocol import SHANGHAI, SourceFailure
from app.models import Provider, User


CALENDARS = {
    'tushare_calendar_sse': ('calendar_sse', 'SSE'),
    'tushare_calendar_szse': ('calendar_szse', 'SZSE'),
}
LOOKBACK_DAYS = 365  # review_jobs.validate_context's local request boundary.
MAX_MONTHS = 14


def _previous_month(day):
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def _first_day(db, config, clock, first_day):
    if first_day is None:
        first = db.scalar(select(func.min(SourceSettings.configured_at)).join(
            Provider, Provider.id == SourceSettings.source_id).where(
                SourceSettings.owner_id == config.owner_id,
                Provider.configured.is_(True), Provider.enabled.is_(True)))
        first_day = (first or config.configured_at).astimezone(SHANGHAI).date()
    # A month-start installation still needs the preceding month's closed days.
    return min(first_day.replace(day=1), _previous_month(clock.astimezone(SHANGHAI).date()))


def _saved_days(db, provider, exchange, first, last, clock):
    # Canonical calendar keys bound the version scan to this explicit range.
    # Rank before examining payload/provenance: a correction must never revive
    # an older apparently valid receipt for the same source key.
    prefix = 'calendar:' + exchange + ':'
    ranked = select(EvidenceVersion.id, func.row_number().over(
        partition_by=EvidenceVersion.source_key,
        order_by=EvidenceVersion.version_number.desc()).label('position')).where(
            EvidenceVersion.source_id == provider.id, EvidenceVersion.kind == 'calendar',
            EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared',
            EvidenceVersion.received_at <= clock,
            EvidenceVersion.source_key >= prefix + first.isoformat(),
            EvidenceVersion.source_key <= prefix + last.isoformat()).subquery()
    rows = db.scalars(select(EvidenceVersion).join(ranked,
        ranked.c.id == EvidenceVersion.id).where(ranked.c.position == 1,
            EvidenceVersion.provenance == 'source_reported'))
    saved = set()
    for evidence in rows:
        payload = evidence.payload
        if payload.get('exchange') != exchange or type(payload.get('is_open')) is not bool:
            continue
        try:
            day = date.fromisoformat(payload['trade_day'])
        except (KeyError, TypeError, ValueError):
            continue
        if first <= day <= last and evidence.source_key == prefix + day.isoformat():
            saved.add(day)
    return saved


def _missing_ranges(first, last, saved):
    """Represent even a long expired prefix without constructing every date."""
    result, cursor = [], first
    for day in sorted(saved):
        if day < cursor or day > last:
            continue
        if cursor < day:
            result.append(dict(start=cursor.isoformat(), end=(day - timedelta(days=1)).isoformat(),
                               count=(day - cursor).days))
        cursor = day + timedelta(days=1)
    if cursor <= last:
        result.append(dict(start=cursor.isoformat(), end=last.isoformat(), count=(last - cursor).days + 1))
    return result


def calendar_coverage(db, provider, config, clock, allowance, first_day=None):
    """Persist at most 13 monthly checks and one older-range limitation.

    ``first_day`` may supply the caller's already computed earliest enabled
    source date for this owner. Queue permits are shared with other recovery
    families. A process restart can reuse the persisted row/job association.
    """
    from app.market.recovery import budget_available, pending, queue_error, target
    from app.market.review_jobs import queue_dataset

    if provider.family not in CALENDARS:
        return
    dataset, exchange = CALENDARS[provider.family]
    today = clock.astimezone(SHANGHAI).date()
    first = _first_day(db, config, clock, first_day)
    boundary = today - timedelta(days=LOOKBACK_DAYS)
    oldest_month = max(first, boundary.replace(day=1))
    owner = db.get(User, config.owner_id)

    # Current month, previous month, then older months, each with a stable key
    # even as today's requested end date advances inside the current month.
    month, checked = today.replace(day=1), 0
    while month >= oldest_month and checked < MAX_MONTHS - 1:
        if not budget_available(db):
            break
        last = min(today, month.replace(day=monthrange(month.year, month.month)[1]))
        row = target(db, provider, config, 'review.' + dataset, month,
                     {'exchange': exchange, 'month': month.strftime('%Y-%m')}, 'day', clock)
        saved = _saved_days(db, provider, exchange, month, last, clock)
        row.expected_count, row.actual_count = (last - month).days + 1, len(saved)
        row.missing_ranges = _missing_ranges(month, last, saved)
        supported_first = max(month, boundary)
        supported_missing = _missing_ranges(supported_first, last, saved)
        if not row.missing_ranges:
            row.state, row.reason, row.next_retry_at = 'complete', None, None
        elif not supported_missing:
            row.state = 'partial' if saved else 'unsupported'
            row.reason, row.next_retry_at = 'local_history_window_expired', None
        elif not pending(db, row, clock):
            row.state = 'partial' if saved else 'missing'
            row.reason = 'local_history_window_expired' if month < boundary else 'calendar_coverage_missing'
            if allowance[0] > 0:
                try:
                    # queue_dataset validates after flush. A rejected range or
                    # changed binding must not leave an orphan queued job.
                    with db.begin_nested():
                        job = queue_dataset(db, owner, dataset, last,
                                            start_day=supported_first, force=True)
                    row.job_id, row.state = job.id, job.state
                    row.next_retry_at = clock + timedelta(minutes=15)
                    allowance[0] -= 1
                except (APIError, SourceFailure) as error:
                    if getattr(error, 'reason', None) == 'review_day_out_of_range':
                        row.state = 'partial' if saved else 'unsupported'
                        row.reason, row.next_retry_at = 'local_history_window_expired', None
                    else:
                        queue_error(row, error, clock)
        month, checked = _previous_month(month), checked + 1

    # The protocol cannot request the older prefix. Keep its actual locally
    # saved receipts and explicit holes in one durable record, rather than
    # scanning/queuing an unbounded number of expired months every round.
    if first < oldest_month and budget_available(db):
        last = oldest_month - timedelta(days=1)
        row = target(db, provider, config, 'review.' + dataset, first,
                     {'exchange': exchange, 'scope': 'before_local_calendar_window',
                      'start_day': first.isoformat()}, 'day', clock)
        saved = _saved_days(db, provider, exchange, first, last, clock)
        row.expected_count, row.actual_count = (last - first).days + 1, len(saved)
        row.missing_ranges = _missing_ranges(first, last, saved)
        row.state = 'complete' if not row.missing_ranges else 'partial' if saved else 'unsupported'
        row.reason = 'local_history_window_expired' if row.missing_ranges else None
        row.next_retry_at = None
