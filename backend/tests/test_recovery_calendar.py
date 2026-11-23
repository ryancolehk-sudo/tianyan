"""Explicit synthetic calendars on the guarded disposable PostgreSQL only.

No fixture here represents a real exchange schedule; no supplier is contacted.
"""
from datetime import UTC, date, datetime, timedelta
import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import SourceVersion
from app.evidence.schemas import CalendarPayload
from app.market import recovery_calendar as calendars
from app.market.collect import store_calendar_batch, store_observation
from app.market.models import SourceSettings
from app.market.protocol import SourceFailure
from app.market.recovery_models import RecoveryCoverage
from app.market.review_store import family_id
from app.models import Job, Provider, User


CLOCK = datetime.fromisoformat('2026-10-26T18:00:00+08:00')


def _setup(monkeypatch, *, clock=CLOCK, configured=date(2026, 10, 26)):
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(settings(), 'tushare_token', SecretStr('synthetic-calendar-recovery-only'))
    monkeypatch.setattr('app.market.review_jobs.now', lambda db: clock)
    def forbidden(*args, **kwargs):
        pytest.fail('calendar reconciliation must never contact a supplier')
    monkeypatch.setattr('app.market.tushare.request', forbidden)
    monkeypatch.setattr('app.market.review_jobs.fetch_public', forbidden)
    with Session(engine()) as db, db.begin():
        owner = User(username='calendar-recovery-' + uuid.uuid4().hex,
                     password_hash='synthetic-disabled-password', role='admin')
        db.add(owner); db.flush()
        for family in calendars.CALENDARS:
            provider = Provider(id=family_id(family), family=family, kind='api',
                name='Synthetic calendar recovery', configured=True, enabled=True,
                entitlement_status='verified', adapter_version='synthetic-calendar-recovery',
                capabilities=['calendar'], rights={'display':'allowed', 'cache':'allowed',
                                                   'export':'unknown', 'redistribute':'unknown'})
            db.add(provider); db.flush()
            db.add(SourceVersion(source_id=provider.id, recorded_at=datetime(1970, 1, 1, tzinfo=UTC),
                descriptor={'configured':True, 'enabled':True, 'entitlement_status':'verified',
                            'rights':provider.rights}, content_hash='a' * 64))
            db.add(SourceSettings(source_id=provider.id, owner_id=owner.id,
                configured_at=datetime.combine(configured, datetime.min.time(), CLOCK.tzinfo),
                next_run_at=clock, interval_seconds=21600, config={},
                rights_basis='Synthetic isolated-test data only'))
        return owner.id


def _seed(first, last, *, family='tushare_calendar_sse', closed=(), omit=(),
          exchange=None, received_at=None):
    # Closed dates are explicitly supplied, never inferred from weekdays.
    rows = [(first + timedelta(days=offset), first + timedelta(days=offset) not in closed)
            for offset in range((last - first).days + 1)
            if first + timedelta(days=offset) not in omit]
    if rows:
        with Session(engine()) as db, db.begin():
            provider = db.get(Provider, family_id(family))
            config = db.get(SourceSettings, provider.id)
            store_calendar_batch(db, provider, config, rows, received_at or CLOCK - timedelta(minutes=1),
                                 exchanges=(exchange or calendars.CALENDARS[family][1],))


def _run(*, clock=CLOCK, allowance=None, first_day=None, family='tushare_calendar_sse'):
    allowance = [10] if allowance is None else allowance
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, family_id(family))
        calendars.calendar_coverage(db, provider, db.get(SourceSettings, provider.id), clock,
                                    allowance, first_day=first_day)
    return allowance


def _rows():
    with Session(engine()) as db:
        return list(db.scalars(select(RecoveryCoverage).order_by(RecoveryCoverage.trade_day)))


def _jobs():
    with Session(engine()) as db:
        return list(db.scalars(select(Job).where(Job.kind == 'review.collect')))


def test_missing_middle_month_is_queued_with_its_exact_calendar_range(monkeypatch):
    _setup(monkeypatch, configured=date(2026, 8, 5))
    _seed(date(2026, 8, 1), date(2026, 8, 31))
    _seed(date(2026, 10, 1), date(2026, 10, 31), closed={date(2026, 10, 3)})
    _run()
    jobs = _jobs()
    assert len(jobs) == 1
    assert jobs[0].payload['review_context']['dataset'] == 'calendar_sse'
    assert jobs[0].payload['review_context']['start_day'] == '2026-09-01'
    assert jobs[0].payload['review_context']['day'] == '2026-09-30'
    rows = {row.scope['month']: row for row in _rows()}
    assert rows['2026-08'].state == rows['2026-10'].state == 'complete'
    assert rows['2026-09'].state == 'queued' and rows['2026-09'].expected_count == 30
    assert rows['2026-09'].missing_ranges == [{'start':'2026-09-01', 'end':'2026-09-30', 'count':30}]


def test_month_start_weekend_still_recovers_the_previous_month(monkeypatch):
    clock = datetime.fromisoformat('2026-11-01T10:00:00+08:00')
    _setup(monkeypatch, clock=clock, configured=clock.date())
    _seed(clock.date(), clock.date(), closed={clock.date()}, received_at=clock - timedelta(minutes=1))
    _run(clock=clock)
    jobs = _jobs()
    assert len(jobs) == 1
    assert jobs[0].payload['review_context']['start_day'] == '2026-10-01'
    assert jobs[0].payload['review_context']['day'] == '2026-10-31'
    current = next(row for row in _rows() if row.trade_day == clock.date())
    assert current.state == 'complete' and current.expected_count == current.actual_count == 1


@pytest.mark.parametrize('opened', [False, True])
def test_every_date_requires_a_receipt_and_closed_receipts_count(monkeypatch, opened):
    _setup(monkeypatch)
    missing = date(2026, 10, 12)
    _seed(date(2026, 9, 1), date(2026, 9, 30))
    _seed(date(2026, 10, 1), CLOCK.date(), closed={date(2026, 10, 3)}, omit={missing})
    _run(allowance=[0])
    current = _rows()[-1]
    assert current.state == 'partial' and current.expected_count == 26 and current.actual_count == 25
    assert current.missing_ranges == [{'start':'2026-10-12', 'end':'2026-10-12', 'count':1}]
    _seed(missing, missing, closed=set() if opened else {missing})
    _run()
    assert _rows()[-1].state == 'complete' and _jobs() == []


def test_allowance_and_progress_survive_sessions_without_duplicate_jobs(monkeypatch):
    _setup(monkeypatch)
    assert _run(allowance=[1]) == [0]
    original = _jobs()[0]
    assert original.payload['review_context']['day'] == str(CLOCK.date())
    assert _run(allowance=[1]) == [0]  # Existing current-month job leaves the permit for September.
    assert len(_jobs()) == 2
    assert _run(allowance=[10]) == [10]
    assert len(_jobs()) == 2 and all(row.job_id and row.state == 'queued' for row in _rows())
    _seed(date(2026, 9, 1), CLOCK.date())
    _run()
    assert all(row.state == 'complete' for row in _rows()) and len(_jobs()) == 2


def test_same_owner_enabled_sources_define_the_earliest_month(monkeypatch):
    owner_id = _setup(monkeypatch)
    with Session(engine()) as db, db.begin():
        other = User(username='other-calendar-' + uuid.uuid4().hex,
                     password_hash='synthetic-disabled-password', role='admin')
        db.add(other); db.flush()
        for name, owner, first, enabled in (
                ('active', owner_id, date(2026, 7, 18), True),
                ('disabled', owner_id, date(2026, 1, 1), False),
                ('foreign', other.id, date(2025, 1, 1), True)):
            provider = Provider(family='synthetic-calendar-' + name, name=name, kind='api',
                                configured=True, enabled=enabled, rights={})
            db.add(provider); db.flush()
            db.add(SourceSettings(source_id=provider.id, owner_id=owner,
                configured_at=datetime.combine(first, datetime.min.time(), CLOCK.tzinfo),
                next_run_at=CLOCK, interval_seconds=3600, config={}, rights_basis='Synthetic fixture'))
    _run(allowance=[0])
    assert [row.scope['month'] for row in _rows()] == ['2026-07','2026-08','2026-09','2026-10']
    assert _jobs() == []


def test_expired_prefix_is_persisted_and_valid_jobs_are_bounded(monkeypatch):
    _setup(monkeypatch, configured=date(2024, 1, 1))
    _seed(date(2024, 2, 1), date(2024, 2, 2), closed={date(2024, 2, 2)})
    allowance = _run(allowance=[100])
    rows, jobs = _rows(), _jobs()
    assert len(rows) <= calendars.MAX_MONTHS and len(jobs) == 13 and allowance == [87]
    prefix = next(row for row in rows if row.scope.get('scope') == 'before_local_calendar_window')
    assert prefix.state == 'partial' and prefix.actual_count == 2
    assert prefix.reason == 'local_history_window_expired' and prefix.next_retry_at is None
    assert prefix.job_id is None and prefix.missing_ranges[-1]['end'] == '2025-09-30'
    boundary = (CLOCK.date() - timedelta(days=365)).isoformat()
    assert all(job.payload['review_context']['start_day'] >= boundary for job in jobs)
    oldest = min(jobs, key=lambda job: job.payload['review_context']['start_day'])
    assert oldest.payload['review_context']['start_day'] == boundary
    assert oldest.payload['review_context']['day'] == '2025-10-31'
    _run(allowance=[100])
    assert len(_jobs()) == 13 and len(_rows()) <= calendars.MAX_MONTHS


def test_boundary_month_keeps_unrecoverable_days_after_valid_part_is_saved(monkeypatch):
    _setup(monkeypatch, configured=date(2025, 10, 1))
    boundary = CLOCK.date() - timedelta(days=365)
    _seed(boundary, date(2025, 10, 31))
    _run(allowance=[0])
    oldest = _rows()[0]
    assert oldest.state == 'partial' and oldest.reason == 'local_history_window_expired'
    assert oldest.expected_count == 31 and oldest.actual_count == 6
    assert oldest.missing_ranges == [{'start':'2025-10-01', 'end':'2025-10-25', 'count':25}]
    assert oldest.next_retry_at is None and oldest.job_id is None


def test_other_source_and_newer_wrong_exchange_cannot_hide_a_gap(monkeypatch):
    _setup(monkeypatch)
    wrong = date(2026, 10, 12)
    _seed(date(2026, 9, 1), CLOCK.date())
    _seed(wrong, wrong, family='tushare_calendar_szse', exchange='SSE')
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, family_id('tushare_calendar_sse'))
        config = db.get(SourceSettings, provider.id)
        store_observation(db, provider, config, source_key='calendar:SSE:' + str(wrong), kind='calendar',
            payload=CalendarPayload(exchange='SZSE', trade_day=wrong, is_open=False,
                                    calendar_version='synthetic-correction'),
            received_at=CLOCK - timedelta(seconds=30), eligible=True)
    _run(allowance=[0])
    row = _rows()[-1]
    assert row.state == 'partial' and row.actual_count == 25
    assert row.missing_ranges == [{'start':'2026-10-12', 'end':'2026-10-12', 'count':1}]


def test_rejected_queue_validation_rolls_back_its_flushed_job(monkeypatch):
    owner_id = _setup(monkeypatch)
    def rejected(db, owner, dataset, day, **kwargs):
        db.add(Job(owner_id=owner_id, kind='review.collect', payload={}))
        db.flush()
        raise SourceFailure('review_day_out_of_range')
    monkeypatch.setattr('app.market.review_jobs.queue_dataset', rejected)
    _run()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert all(row.state == 'unsupported' and row.reason == 'local_history_window_expired'
               and row.next_retry_at is None for row in _rows())
