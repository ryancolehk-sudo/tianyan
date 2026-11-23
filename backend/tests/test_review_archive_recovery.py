"""Synthetic next-session daily recovery; no vendor calls or real market rows."""
import copy
import uuid
from datetime import date, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market import review_schedule as schedule
from app.market.protocol import SourceFailure
from app.models import AuditEvent, Job, Provider, User


def at(value, day='2026-10-27'):
    return datetime.fromisoformat(f'{day}T{value}+08:00')


@pytest.mark.parametrize('dataset,clock,deadline', [
    ('daily', '08:59:59', '09:00:00'),
    ('daily', '09:00:00', None),
    ('members', '09:14:59', None),
    ('lhb', '09:00:00', None),
    ('seats', '09:15:00', '16:30:00'),
    ('daily', '09:15:00', '16:00:00'),
    ('daily', '15:59:59', '16:00:00'),
    ('daily', '16:00:00', None),
    ('lhb', '16:00:00', '16:30:00'),
    ('lhb', '16:30:00', None),
    ('daily', '21:29:59', None),
])
def test_archive_window_bounds(dataset, clock, deadline):
    expected = at(deadline) if deadline else None
    assert schedule.archive_deadline(dataset, at(clock)) == expected


def test_archive_window_bounds_after_close_waits_for_next_preparation():
    assert schedule.archive_deadline('members', at('21:30:00')) == at('09:00:00', '2026-10-28')


def test_calendar_target_uses_proved_previous_date_across_a_holiday(monkeypatch):
    # A deliberately irregular calendar: never infer the preceding weekday.
    previous = date(2026, 10, 20)
    clock = at('09:00:00')
    monkeypatch.setattr('app.market.mairui.pool_day',
        lambda db, stamp, preopen=False: stamp.date() if preopen else previous)
    def saved_days(db, *, limit, end_day, as_of_time):
        assert limit == 1 and end_day == date(2026, 10, 26) and as_of_time == clock
        return [previous]
    monkeypatch.setattr(schedule, 'available_trade_days', saved_days)
    assert schedule.archive_target_day(object(), clock) == previous


@pytest.mark.parametrize('state', ['closed_today', 'unknown_today', 'disagree_previous'])
def test_calendar_target_never_guesses_open_or_previous_day(monkeypatch, state):
    def pool(db, stamp, preopen=False):
        if state == 'unknown_today':
            raise SourceFailure('calendar_not_ready')
        return stamp.date() if preopen and state != 'closed_today' else date(2026, 10, 26)
    monkeypatch.setattr('app.market.mairui.pool_day', pool)
    monkeypatch.setattr(schedule, 'available_trade_days', lambda *a, **k: [date(2026, 10, 23)])
    assert schedule.archive_target_day(object(), at('09:00:00')) is None


def prepare(client, monkeypatch, *, clock=None, today_open=True, include_today=True):
    from app.market.collect import store_calendar_batch
    from app.market.models import SourceSettings
    from app.market.review_store import family_id
    from test_data_sources import configure, login
    from test_tushare import source
    clock = [clock or at('09:00:00')]
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(settings(), 'tushare_token', SecretStr('synthetic-archive-recovery-token'))
    monkeypatch.setattr(schedule, 'now', lambda db: clock[0])
    monkeypatch.setattr('app.market.review_jobs.now', lambda db: clock[0])
    monkeypatch.setattr('app.market.budget.now', lambda db: clock[0])
    def no_vendor(*args, **kwargs):
        pytest.fail('This suite must not contact a vendor')
    monkeypatch.setattr('app.market.tushare.request', no_vendor)
    monkeypatch.setattr('app.market.review_jobs.fetch_public', no_vendor)
    headers = login(client)
    observed = at('08:00:00', '2026-10-20')
    monkeypatch.setattr('app.market.service.now', lambda db: observed)
    families = ('tushare_calendar_sse', 'tushare_calendar_szse', 'tushare_dc_member')
    for family in families:
        configure(client, headers, source(family), key=family)
    rows = [(date(2026, 10, 23), True), (date(2026, 10, 24), False),
            (date(2026, 10, 25), False), (date(2026, 10, 26), True)]
    if include_today:
        rows.append((date(2026, 10, 27), today_open))
    with Session(engine()) as db, db.begin():
        for family, exchange in ((families[0], 'SSE'), (families[1], 'SZSE')):
            provider = db.get(Provider, family_id(family))
            store_calendar_batch(db, provider, db.get(SourceSettings, provider.id), rows,
                                 observed, exchanges=(exchange,))
        ident = family_id('tushare_dc_member')
        config = db.get(SourceSettings, ident)
        original = copy.deepcopy(config.config)
    return ident, clock, original


def archive_jobs(db):
    return list(db.scalars(select(Job).where(Job.kind == 'review.collect').order_by(Job.created_at, Job.id)))


def save_members(ident, clock, *, empty=False):
    from app.market.models import SourceSettings
    from app.market.review_jobs import queue_dataset
    from app.market.review_protocol import ReviewPage
    from app.market.review_store import persist_pages
    with Session(engine()) as db, db.begin():
        provider, config = db.get(Provider, ident), db.get(SourceSettings, ident)
        job = queue_dataset(db, db.get(User, config.owner_id), 'members', date(2026, 10, 26))
        rows = [] if empty else [{'ts_code': 'BK1184.DC', 'trade_date': '20261026',
                                  'con_code': '000001.SZ'}]
        page = ReviewPage(fields=['ts_code', 'trade_date', 'con_code'], rows=rows,
                         source_url='https://tushare.pro/document/2', raw_hash='a' * 64,
                         scope='Synthetic archive fixture')
        persist_pages(db, provider, config, job, [(page, clock[0] - timedelta(minutes=30))])
        job.state, job.completed_at = 'succeeded', clock[0] - timedelta(minutes=30)


def test_preopen_recovers_only_last_closed_day_and_keeps_source_selection(client, monkeypatch):
    from app.market.models import DataFetchRun, SourceSettings
    ident, clock, original = prepare(client, monkeypatch)
    schedule.schedule_daily_archives()
    schedule.schedule_daily_archives()
    clock[0] += timedelta(minutes=14)
    schedule.schedule_daily_archives()
    with Session(engine()) as db:
        jobs = archive_jobs(db)
        assert len(jobs) == 1
        assert jobs[0].payload['scheduled_daily_archive'] is True
        ctx = jobs[0].payload['review_context']
        assert (ctx['dataset'], ctx['day'], ctx['start_day'], ctx['scope']) == (
            'members', '2026-10-26', '2026-10-26', 'all')
        assert db.get(SourceSettings, ident).config == original
        assert not db.scalar(select(DataFetchRun.id))
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action.like('source.request.%')))


@pytest.mark.parametrize('empty,expected_jobs', [(False, 1), (True, 2)])
def test_preopen_retries_saved_empty_table_but_skips_complete_nonempty(client, monkeypatch, empty, expected_jobs):
    ident, clock, _ = prepare(client, monkeypatch)
    save_members(ident, clock, empty=empty)
    schedule.schedule_daily_archives()
    with Session(engine()) as db:
        assert len(archive_jobs(db)) == expected_jobs


def test_evening_empty_failure_is_retried_on_next_open_preparation(client, monkeypatch):
    _, clock, _ = prepare(client, monkeypatch, clock=at('21:00:00', '2026-10-26'))
    schedule.schedule_daily_archives()
    with Session(engine()) as db, db.begin():
        old = archive_jobs(db)[0]
        old.state, old.error_code, old.completed_at = 'failed', 'review_required_table_empty', clock[0]
    clock[0] = at('09:00:00')
    schedule.schedule_daily_archives()
    with Session(engine()) as db:
        jobs = archive_jobs(db)
        assert len(jobs) == 2
        assert all(job.payload['review_context']['day'] == '2026-10-26' for job in jobs)


def test_preparation_keeps_existing_fifteen_minute_retry_limit(client, monkeypatch):
    _, clock, _ = prepare(client, monkeypatch)
    schedule.schedule_daily_archives()
    with Session(engine()) as db, db.begin():
        old = archive_jobs(db)[0]
        old.state, old.error_code, old.completed_at = 'failed', 'review_required_table_empty', clock[0]
    clock[0] = at('09:14:59')
    schedule.schedule_daily_archives()
    clock[0] = at('09:15:00')
    schedule.schedule_daily_archives()
    with Session(engine()) as db:
        assert len(archive_jobs(db)) == 1


@pytest.mark.parametrize('today_open,include_today', [(False, True), (True, False)])
def test_preopen_requires_both_verified_current_open_calendars(client, monkeypatch, today_open, include_today):
    prepare(client, monkeypatch, today_open=today_open, include_today=include_today)
    schedule.schedule_daily_archives()
    with Session(engine()) as db:
        assert archive_jobs(db) == []


def test_existing_pending_scope_is_not_duplicated_next_morning(client, monkeypatch):
    _, clock, _ = prepare(client, monkeypatch, clock=at('21:00:00', '2026-10-26'))
    schedule.schedule_daily_archives()
    clock[0] = at('09:00:00')
    schedule.schedule_daily_archives()
    with Session(engine()) as db:
        assert len(archive_jobs(db)) == 1


def running_archive(clock):
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        job = archive_jobs(db)[0]
        job.state, job.attempts = 'running', 1
        job.lease_token, job.lease_until = uuid.uuid4(), clock[0] + timedelta(minutes=5)
        return job


@pytest.mark.parametrize('target', ['2026-10-27', '2026-10-23'])
def test_preopen_rejects_today_or_older_queued_date_before_supplier_budget(client, monkeypatch, target):
    from app.market.budget import reserve_tushare_request
    _, clock, _ = prepare(client, monkeypatch)
    schedule.schedule_daily_archives()
    job = running_archive(clock)
    with Session(engine()) as db, db.begin():
        stored = db.get(Job, job.id)
        payload = copy.deepcopy(stored.payload)
        payload['review_context'].update(day=target, start_day=target)
        stored.payload = job.payload = payload
    with pytest.raises(SourceFailure, match='review_archive_day_mismatch'):
        reserve_tushare_request(job)
    with Session(engine()) as db:
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'source.request.tushare'))


def test_preopen_valid_job_uses_existing_budget_and_stops_at_0915(client, monkeypatch):
    from app.market.budget import reserve_tushare_request
    _, clock, _ = prepare(client, monkeypatch)
    schedule.schedule_daily_archives()
    job = running_archive(clock)
    assert reserve_tushare_request(job) is True
    clock[0] = at('09:15:00')
    with Session(engine()) as db, db.begin():
        db.get(Job, job.id).lease_until = clock[0] + timedelta(minutes=5)
    assert reserve_tushare_request(job) is False
    with Session(engine()) as db:
        stored = db.get(Job, job.id)
        assert stored.state == 'queued' and stored.available_at == at('16:00:00')
        permits = list(db.scalars(select(AuditEvent.id).where(AuditEvent.action == 'source.request.tushare')))
        assert len(permits) == 1


@pytest.mark.parametrize('restriction', ['maintenance', 'disabled_owner', 'paused_source'])
def test_preopen_preserves_maintenance_and_source_owner_constraints(client, monkeypatch, restriction):
    from app.market.models import SourceSettings
    ident, _, _ = prepare(client, monkeypatch)
    if restriction == 'maintenance':
        monkeypatch.setattr('app.maintenance_control.paused', lambda db: True)
    else:
        with Session(engine()) as db, db.begin():
            config = db.get(SourceSettings, ident)
            if restriction == 'disabled_owner':
                db.get(User, config.owner_id).enabled = False
            else:
                config.paused_reason = 'schema_changed'
    schedule.schedule_daily_archives()
    with Session(engine()) as db:
        assert archive_jobs(db) == []
