"""Trading windows do not create clocks, supplier permits or opening calendars."""
from datetime import UTC, date, datetime, timedelta
import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market.auction_schedule import next_collection
from app.market.budget import reserve_tushare_request
from app.market.intraday_schedule import FAMILY, align_intraday, deferred_intraday, in_intraday_window
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SourceFailure
from app.market.service import enqueue_collection, schedule_sources, source_id
from app.models import AuditEvent, Job
from app.auth import APIError
from test_data_sources import configure, login
from test_tushare import TOKEN, source


def at(value):
    return datetime.fromisoformat(value + '+08:00')


@pytest.mark.parametrize('stamp,expected', [
    ('2026-10-23T09:14:59', '2026-10-23T09:15:00'),
    ('2026-10-23T09:15:00', '2026-10-23T09:15:00'),
    ('2026-10-23T09:29:59', '2026-10-23T09:29:59'),
    ('2026-10-23T09:30:00', '2026-10-23T09:30:00'),
    ('2026-10-23T11:30:59', '2026-10-23T11:30:59'),
    ('2026-10-23T11:31:00', '2026-10-23T13:00:00'),
    ('2026-10-23T12:59:59', '2026-10-23T13:00:00'),
    ('2026-10-23T13:00:00', '2026-10-23T13:00:00'),
    ('2026-10-23T15:00:59', '2026-10-23T15:00:59'),
    ('2026-10-23T15:01:00', '2026-10-23T15:01:00'),
    ('2026-10-23T15:04:59', '2026-10-23T15:04:59'),
    ('2026-10-23T15:05:00', '2026-10-24T09:15:00'),
    ('2026-10-23T23:59:59', '2026-10-24T09:15:00'),
])
def test_local_windows_include_final_quote_minute_without_implying_trades(stamp, expected):
    assert align_intraday(at(stamp)) == at(expected)
    assert align_intraday(at(stamp).astimezone(UTC)) == at(expected)
    assert in_intraday_window(at(stamp)) == (stamp == expected)


def test_next_request_preserves_spacing_and_skips_quiet_lunch_and_overnight():
    assert next_collection(FAMILY, at('2026-10-23T10:00:00'), 60) == at('2026-10-23T10:01:00')
    assert next_collection(FAMILY, at('2026-10-23T10:00:00'), 300) == at('2026-10-23T10:05:00')
    assert next_collection(FAMILY, at('2026-10-23T11:30:30'), 60) == at('2026-10-23T13:00:00')
    assert next_collection(FAMILY, at('2026-10-23T15:00:30'), 60) == at('2026-10-23T15:01:30')
    assert next_collection(FAMILY, at('2026-10-23T15:04:30'), 60) == at('2026-10-24T09:15:00')
    assert next_collection(FAMILY, at('2026-10-23T10:00:00'), 21600) == at('2026-10-24T09:15:00')


@pytest.mark.parametrize('observed', [None, date(2026, 10, 22), date(2026, 10, 23)])
def test_current_observed_open_day_required_even_on_a_weekday(monkeypatch, observed):
    def calendar(db, clock):
        if observed is None:
            raise SourceFailure('calendar_not_ready')
        return observed
    monkeypatch.setattr('app.market.mairui.pool_day', calendar)
    result = deferred_intraday(None, at('2026-10-23T10:00:00'), 60, source(FAMILY)['symbols'])
    assert result == (None if observed == date(2026, 10, 23) else at('2026-10-23T11:00:00'))


@pytest.mark.parametrize('selection', [[], [{'exchange':'BSE','asset_type':'stock'}], [{'exchange':'SSE','asset_type':'index'}]])
def test_unproven_exchange_or_identity_cannot_reuse_shanghai_shenzhen_calendar(monkeypatch, selection):
    monkeypatch.setattr('app.market.mairui.pool_day', lambda *args: pytest.fail('Do not infer a different market calendar'))
    assert deferred_intraday(None, at('2026-10-23T10:00:00'), 60, selection) == at('2026-10-23T11:00:00')


def setup_source(client, monkeypatch):
    monkeypatch.setattr(settings(), 'tushare_token', SecretStr(TOKEN))
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    headers = login(client)
    body = source(FAMILY)
    body['interval_seconds'] = 60
    configure(client, headers, body)


@pytest.mark.parametrize('stamp,expected', [
    ('2026-10-23T09:00:00', '2026-10-23T09:15:00'),
    ('2026-10-23T18:00:00', '2026-10-24T09:15:00'),
])
def test_off_window_due_source_defers_without_jobs_permits_or_http(client, monkeypatch, stamp, expected):
    setup_source(client, monkeypatch)
    clock = at(stamp)
    monkeypatch.setattr('app.market.service.now', lambda db: clock)
    monkeypatch.setattr('app.market.collect.request_tushare', lambda *args: pytest.fail('No HTTP outside window'))
    with Session(engine()) as db, db.begin():
        db.get(SourceSettings, source_id(FAMILY)).next_run_at = clock - timedelta(seconds=1)
    schedule_sources(); schedule_sources()
    with Session(engine()) as db:
        assert db.get(SourceSettings, source_id(FAMILY)).next_run_at == at(expected)
        assert not db.scalar(select(Job.id).where(Job.kind == 'source.collect'))
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'source.request.tushare'))
        assert not db.scalar(select(DataFetchRun.id))


@pytest.mark.parametrize('final_stamp,calendar_open,expected', [
    ('2026-10-23T15:05:00', True, '2026-10-24T09:15:00'),
    ('2026-10-23T10:00:00', False, '2026-10-23T11:00:00'),
])
def test_delayed_automatic_job_rechecks_window_and_calendar_at_final_permit(client, monkeypatch, final_stamp, calendar_open, expected):
    setup_source(client, monkeypatch)
    clock = at('2026-10-23T09:50:00')
    monkeypatch.setattr('app.market.service.now', lambda db: clock)
    monkeypatch.setattr('app.market.mairui.pool_day', lambda db, stamp: stamp.astimezone(at('2026-10-23T00:00:00').tzinfo).date())
    with Session(engine()) as db, db.begin():
        db.get(SourceSettings, source_id(FAMILY)).next_run_at = clock - timedelta(seconds=1)
    schedule_sources(); schedule_sources()
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        jobs = list(db.scalars(select(Job).where(Job.kind == 'source.collect')))
        assert len(jobs) == 1 and jobs[0].payload['scheduled_intraday'] is True
        job = jobs[0]; job.state = 'running'; job.attempts = 1
        job.lease_token = uuid.uuid4(); job.lease_until = at('2026-10-24T23:00:00')
    monkeypatch.setattr('app.market.budget.now', lambda db: at(final_stamp))
    if not calendar_open:
        monkeypatch.setattr('app.market.mairui.pool_day', lambda *args: date(2026, 10, 22))
    assert reserve_tushare_request(job) is False
    with Session(engine()) as db:
        saved = db.get(Job, job.id)
        assert saved.state == 'queued' and saved.attempts == 0 and saved.lease_token is None
        assert saved.available_at == at(expected) and saved.error_code == 'intraday_window_wait'
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'source.request.tushare'))
        assert not db.scalar(select(DataFetchRun.id))


def test_pending_explicit_request_is_not_relabelled_automatic(client, monkeypatch):
    setup_source(client, monkeypatch)
    clock = at('2026-10-23T10:00:00')
    monkeypatch.setattr('app.market.service.now', lambda db: clock)
    monkeypatch.setattr('app.market.mairui.pool_day', lambda *args: date(2026, 10, 23))
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        job = enqueue_collection(db, source_id(FAMILY))
        db.get(SourceSettings, source_id(FAMILY)).next_run_at = clock - timedelta(seconds=1)
    schedule_sources()
    with Session(engine()) as db:
        saved = db.get(Job, job.id)
        assert saved.payload == {'source_id':str(source_id(FAMILY))}
        assert len(list(db.scalars(select(Job).where(Job.kind == 'source.collect')))) == 1


@pytest.mark.parametrize('age,cooling,allowed', [(61, False, True), (30, False, False), (61, True, False)])
def test_explicit_off_window_request_still_enforces_spacing_and_shared_cooldown(client, monkeypatch, age, cooling, allowed):
    setup_source(client, monkeypatch)
    clock = at('2026-10-23T18:00:00')
    monkeypatch.setattr('app.market.service.now', lambda db: clock)
    monkeypatch.setattr('app.market.service.family_cooldown_until', lambda db, family, stamp: stamp + timedelta(minutes=5) if cooling else None)
    with Session(engine()) as db, db.begin():
        config = db.get(SourceSettings, source_id(FAMILY))
        config.next_run_at = at('2026-10-24T09:30:00')
        db.add(DataFetchRun(source_id=config.source_id, capability='quote', job_id=None, started_at=clock-timedelta(seconds=age),
            received_at=clock-timedelta(seconds=age), status='complete', request_count=1))
    with Session(engine()) as db, db.begin():
        if allowed:
            job = enqueue_collection(db, source_id(FAMILY))
            assert 'scheduled_intraday' not in job.payload
            assert db.get(SourceSettings, source_id(FAMILY)).next_run_at == at('2026-10-24T09:15:00')
        else:
            with pytest.raises(APIError) as error:
                enqueue_collection(db, source_id(FAMILY))
            assert error.value.code == 'source_rate_limit'
