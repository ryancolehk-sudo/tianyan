"""Daily auction collection and strict recovery regressions; no real HTTP."""
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market.auction_schedule import CURRENT, MATCHED, in_window, next_collection, schema_recheck
from app.market.mairui import choose_pool_day
from app.market.models import SourceSettings
from app.market.service import schedule_sources, source_id
from app.models import AuditEvent, Job, Provider
from app.worker import claim, process
from test_auction_tables import (batches, configure_family, current_body, job_row,
                                response, run, run_failing, runs)
from test_data_sources import login


def at(value):
    return datetime.fromisoformat(value + '+08:00')


@pytest.mark.parametrize('family,clock,expected', [
    (CURRENT, '2026-10-22T08:00:29', '2026-10-22T09:15:00'),
    (CURRENT, '2026-10-22T09:10:00', '2026-10-22T09:15:00'),
    (CURRENT, '2026-10-22T09:14:30', '2026-10-22T09:15:00'),
    (CURRENT, '2026-10-22T09:14:59', '2026-10-22T09:15:02'),
    (CURRENT, '2026-10-22T09:15:00', '2026-10-22T09:15:03'),
    (CURRENT, '2026-10-22T23:50:00', '2026-10-23T09:15:00'),
    (MATCHED, '2026-10-22T08:08:00', '2026-10-22T09:26:00'),
    (MATCHED, '2026-10-22T09:26:00', '2026-10-22T09:27:05'),
    (MATCHED, '2026-10-22T09:25:30', '2026-10-22T09:26:35'),
    (MATCHED, '2026-10-22T09:35:00', '2026-10-23T09:26:00'),
    ('duanxianxia_auction_history', '2026-10-22T09:15:00', '2026-10-22T10:15:00'),
])
def test_next_collection_is_daily_window_aligned(family, clock, expected):
    assert next_collection(family, at(clock), 3600 if family.endswith('_history') else 65) == at(expected)
    assert next_collection(family, at(clock).astimezone(UTC), 3600 if family.endswith('_history') else 65) == at(expected)


def test_schema_probe_is_paced_and_security_failures_do_not_auto_resume():
    clock = at('2026-10-22T09:15:00')
    assert next_collection(CURRENT, clock, 65, schema_probe=True) == clock + timedelta(seconds=5)
    assert schema_recheck(CURRENT, 'schema_changed')
    for reason in ['decoder_changed', 'permission_missing', 'endpoint_not_allowed', 'response_too_large', 'upstream_redirect']:
        assert not schema_recheck(CURRENT, reason)
    assert not schema_recheck('duanxianxia_auction_history', 'schema_changed')
    assert not in_window(MATCHED, at('2026-10-22T09:25:59'))


def test_live_permit_is_persisted_at_three_seconds_while_schema_probe_stays_slow(client, monkeypatch):
    from app.market.budget import reserve_auction_request
    from test_auction_tables import new_job
    headers=login(client); configure_family(client,headers,CURRENT)
    current=[at('2026-10-26T09:20:00')]
    monkeypatch.setattr('app.market.budget.now',lambda db:current[0])
    first_id=new_job(CURRENT);first=claim()
    assert first.id==first_id and reserve_auction_request(first)
    from app.worker import finish
    assert finish(first.id, first.lease_token)
    current[0]+=timedelta(seconds=2)
    second_id=new_job(CURRENT);second=claim()
    assert second.id==second_id and not reserve_auction_request(second)
    waiting=job_row(second_id)
    assert waiting.available_at==at('2026-10-26T09:20:03')
    current[0]+=timedelta(seconds=1)
    second=claim()
    assert second.id==second_id and reserve_auction_request(second)
    assert next_collection(CURRENT,current[0],65,schema_probe=True)==current[0]+timedelta(seconds=5)


def test_matched_auction_requests_today_at_0926_but_other_pools_wait_until_0930():
    today, previous = date(2026, 10, 22), date(2026, 10, 21)
    calendars = {exchange: {today: True, previous: True} for exchange in ['SSE', 'SZSE']}
    clock = at('2026-10-22T09:26:00')
    assert choose_pool_day(calendars, clock, auction=True) == today
    assert choose_pool_day(calendars, clock) == previous
    assert choose_pool_day(calendars, clock - timedelta(seconds=1), auction=True) == previous
    calendars['SZSE'][today] = False
    from app.market.protocol import SourceFailure
    with pytest.raises(SourceFailure, match='calendar_not_ready'):
        choose_pool_day(calendars, clock, auction=True)


def due(family=CURRENT):
    with Session(engine()) as db, db.begin():
        db.get(SourceSettings, source_id(family)).next_run_at = datetime.now(UTC) - timedelta(seconds=1)


def recovery_window(monkeypatch):
    # These cases exercise schema recovery and pacing. Session/calendar edges
    # are tested independently with synthetic clocks in test_session_collection.
    monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime', lambda *args: None)
    # This file checks recovery state, while the persisted 5-second schema
    # permit is exercised with an advancing clock in test_realtime_fairness.
    monkeypatch.setattr('app.market.collect.reserve_auction_request', lambda job: True)


def test_paused_schema_is_revalidated_and_only_success_resumes(client, monkeypatch):
    recovery_window(monkeypatch)
    headers = login(client)
    configure_family(client, headers, CURRENT)
    monkeypatch.setattr('app.market.budget.AUCTION_INTERVAL_SECONDS', 0)
    run(monkeypatch, CURRENT, current_body())
    before = {row.id for row in batches(CURRENT)}
    run_failing(monkeypatch, CURRENT, 'schema_changed')
    due()
    schedule_sources()
    job = claim()
    assert job and job.payload['schema_probe'] is True
    # A process restart cannot create another pending probe.
    due()
    schedule_sources()
    assert claim() is None
    monkeypatch.setattr('app.market.collect.fetch_auction_table', response(current_body()))
    process(job)
    assert job_row(job.id).state == 'succeeded'
    with Session(engine()) as db:
        assert db.get(SourceSettings, source_id(CURRENT)).paused_reason is None
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'source.schema_recovered'))
    assert {row.id for row in batches(CURRENT)} == before
    assert runs(CURRENT)[-1].duplicates == 2


@pytest.mark.parametrize('reason', ['schema_changed', 'decoder_changed', 'permission_missing'])
def test_failed_probe_keeps_cache_pause_and_does_not_loop(client, monkeypatch, reason):
    recovery_window(monkeypatch)
    headers = login(client)
    configure_family(client, headers, CURRENT)
    monkeypatch.setattr('app.market.budget.AUCTION_INTERVAL_SECONDS', 0)
    run(monkeypatch, CURRENT, current_body())
    before = {row.id for row in batches(CURRENT)}
    run_failing(monkeypatch, CURRENT, 'schema_changed')
    due(); schedule_sources()
    job = claim()
    from app.market.protocol import SourceFailure
    def fail(_kind):
        raise SourceFailure(reason)
    monkeypatch.setattr('app.market.collect.fetch_auction_table', fail)
    process(job)
    schedule_sources()
    assert claim() is None
    with Session(engine()) as db:
        config = db.get(SourceSettings, source_id(CURRENT))
        assert config.paused_reason == reason
        assert config.next_run_at > datetime.now(UTC)
    assert {row.id for row in batches(CURRENT)} == before


def test_revoked_rights_block_recovery_without_http(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    run_failing(monkeypatch, CURRENT, 'schema_changed')
    with Session(engine()) as db, db.begin():
        db.get(Provider, source_id(CURRENT)).rights = {'display': 'denied', 'cache': 'allowed'}
    due(); schedule_sources()
    assert claim() is None
    assert len(runs(CURRENT)) == 1


def test_live_lane_precedes_general_in_legacy_all_mode(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    from app.models import User
    import app.worker as worker
    from app.market import service
    real_clock = datetime.now(UTC)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at('2026-10-22T09:15:00').astimezone(tz or UTC)
    monkeypatch.setattr(worker, 'datetime', Clock)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(service, 'now', lambda _db: real_clock)
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(User).where(User.username == 'owner'))
        older = Job(owner_id=owner.id, kind='system.check', payload={}, available_at=real_clock - timedelta(days=2))
        auction = Job(owner_id=owner.id, kind='source.collect', payload={'source_id': str(source_id(CURRENT))},
                      available_at=at('2026-10-22T09:14:59'))
        db.add_all([older, auction]); db.flush()
        auction_id = auction.id
    assert claim().id == auction_id
