"""Shared cooldown on real disposable PostgreSQL; all upstream data is synthetic."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market.budget import ACTION, ASHARE_FAMILIES, ashare_cooldown_until
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response, SourceFailure
from app.market.service import schedule_sources, source_id
from app.models import AuditEvent, Job
from app.worker import claim, process
from test_data_sources import configuration, configure, login, quote_body
from test_market_breadth import sample


def setup(client, monkeypatch):
    headers = login(client)
    for family in [*sorted(ASHARE_FAMILIES), 'tencent_web']:
        body = configuration(family)
        if family == 'ashare_public':
            body['symbols'] = configuration()['symbols']
        body['interval_seconds'] = 300
        configure(client, headers, body, key=uuid.uuid4().hex)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    return headers


def queue(client, headers, family, expected=202):
    result = client.post(f'/api/v1/data-sources/{source_id(family)}/refresh', json={},
        headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
    assert result.status_code == expected, result.text
    return result


def insert_limit(db, *, family='ashare_public', deadline=None, legacy=False):
    received = datetime.now(UTC) - timedelta(seconds=1)
    row = DataFetchRun(source_id=source_id(family), capability='synthetic', started_at=received,
        received_at=received, status='error', reason='rate_limited', request_count=1,
        retry_not_before=None if legacy else deadline or received + timedelta(hours=1))
    db.add(row)
    db.get(SourceSettings, source_id(family)).next_run_at = deadline or received + timedelta(hours=1)
    if legacy:
        db.get(SourceSettings, source_id(family)).legacy_cooldown_until = deadline or received + timedelta(hours=1)
    db.flush()
    return row


def test_upstream_limit_blocks_already_queued_sibling_without_http_or_retry_spend(client, monkeypatch):
    headers = setup(client, monkeypatch)
    queue(client, headers, 'ashare_breadth')
    first = claim()
    queue(client, headers, 'ashare_sector_heat')
    sibling = claim()
    calls = []
    def limited(url):
        calls.append(url)
        raise SourceFailure('rate_limited', retry_after=3600)
    monkeypatch.setattr('app.market.collect.fetch_https', limited)
    process(first)
    process(sibling)
    assert len(calls) == 1
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun))
        assert run.reason == 'rate_limited' and run.retry_not_before == run.received_at + timedelta(hours=1)
        waiting = db.get(Job, sibling.id)
        assert waiting.state == 'queued' and waiting.attempts == 0 and waiting.lease_token is None
        assert waiting.available_at == run.retry_not_before and waiting.error_code == 'source_budget_wait'
        assert db.get(SourceSettings, source_id('ashare_sector_heat')).next_run_at >= run.retry_not_before
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 1
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == ACTION)) == 1
    sources = client.get('/api/v1/data-sources').json()['data']['items']
    shared = [row for row in sources if row['family'] in ASHARE_FAMILIES]
    assert len({row['cooldown_until'] for row in shared}) == 1
    assert all(row['cooldown_scope'] == 'supplier' and row['next_run_at'] >= row['cooldown_until'] for row in shared)
    assert next(row for row in shared if row['family'] == 'ashare_breadth')['collection_state'] == 'error'
    assert next(row for row in shared if row['family'] == 'ashare_public')['collection_state'] == 'not_checked'
    queue(client, headers, 'ashare_public', expected=429)
    assert client.get('/api/v1/data-fetch-runs').json()['data']['items'][0]['retry_not_before'] == shared[0]['cooldown_until']


@pytest.mark.parametrize('legacy', [False, True])
def test_reconfiguration_and_scheduler_keep_shared_deadline_without_rewriting_logs(client, monkeypatch, legacy):
    headers = setup(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        row = insert_limit(db, legacy=legacy)
        ident = row.id
        deadline = db.get(SourceSettings, source_id('ashare_public')).next_run_at
    for family in sorted(ASHARE_FAMILIES):
        body = configuration(family)
        if family == 'ashare_public': body['symbols'] = configuration()['symbols']
        configure(client, headers, body, key=uuid.uuid4().hex)
        queue(client, headers, family, expected=429)
    with Session(engine()) as db, db.begin():
        # Simulate another worker finding an old due sibling after configuration.
        db.get(SourceSettings, source_id('ashare_sector_heat')).next_run_at = datetime.now(UTC) - timedelta(seconds=1)
    schedule_sources()
    with Session(engine()) as db:
        assert ashare_cooldown_until(db, datetime.now(UTC)) == deadline
        assert db.get(DataFetchRun, ident).retry_not_before == (None if legacy else deadline)
        assert db.get(SourceSettings, source_id('ashare_sector_heat')).next_run_at >= deadline
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 1
        assert db.scalar(select(func.count()).select_from(Job).where(Job.payload['source_id'].astext.in_([str(source_id(f)) for f in ASHARE_FAMILIES]))) == 0


@pytest.mark.parametrize('legacy', [False, True])
def test_calendar_ready_cannot_wake_a_sibling_past_its_shared_cooldown(client, monkeypatch, legacy):
    from app.market.realtime_schedule import wake_session_waits
    setup(client, monkeypatch)
    clock = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        row = insert_limit(db, legacy=legacy)
        run_id = row.id
        deadline = db.get(SourceSettings, source_id('ashare_public')).next_run_at
        config = db.get(SourceSettings, source_id('ashare_sector_heat'))
        config.next_run_at = clock - timedelta(seconds=1)
        waiting = Job(owner_id=config.owner_id, kind='source.collect', payload={
            'source_id': str(config.source_id), 'scheduled_realtime': True},
            state='queued', error_code='intraday_window_wait', available_at=clock + timedelta(minutes=5))
        db.add(waiting); db.flush(); job_id = waiting.id
    monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime', lambda *a: None)
    with Session(engine()) as db, db.begin():
        wake_session_waits(db, clock)
    with Session(engine()) as db:
        assert db.get(Job, job_id).available_at >= deadline
        assert db.get(SourceSettings, source_id('ashare_sector_heat')).next_run_at >= deadline
        assert db.get(DataFetchRun, run_id).retry_not_before == (None if legacy else deadline)
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 1


def test_other_supplier_still_collects_and_does_not_inherit_ashare_cooldown(client, monkeypatch):
    headers = setup(client, monkeypatch)
    with Session(engine()) as db, db.begin(): insert_limit(db)
    calls = []
    def fetch(url):
        calls.append(url)
        return Response(quote_body(), datetime.now(UTC), 1, 200)
    monkeypatch.setattr('app.market.collect.fetch_https', fetch)
    queue(client, headers, 'tencent_web')
    process(claim())
    assert len(calls) == 1
    row = next(row for row in client.get('/api/v1/data-sources').json()['data']['items'] if row['family'] == 'tencent_web')
    assert row['cooldown_until'] is None and row['cooldown_scope'] is None and row['collection_state'] == 'ready'


def test_expired_deadline_allows_normal_collection_without_inventing_a_new_attempt(client, monkeypatch):
    headers = setup(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        old = datetime.now(UTC) - timedelta(hours=2)
        db.add(DataFetchRun(source_id=source_id('ashare_public'), capability='synthetic', started_at=old,
            received_at=old, status='error', reason='rate_limited', request_count=1, retry_not_before=old + timedelta(hours=1)))
    calls = []
    def fetch(url):
        calls.append(url)
        return Response(json.dumps(sample()).encode(), datetime.now(UTC), 1, 200)
    monkeypatch.setattr('app.market.collect.fetch_https', fetch)
    queue(client, headers, 'ashare_breadth')
    process(claim())
    assert len(calls) == 1
    with Session(engine()) as db:
        assert ashare_cooldown_until(db, datetime.now(UTC)) is None
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 2
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == ACTION)) == 1


def test_expired_legacy_cooldown_cannot_be_resurrected_by_normal_queue_scheduling(client, monkeypatch):
    headers = setup(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        old = datetime.now(UTC) - timedelta(hours=2)
        db.add(DataFetchRun(source_id=source_id('ashare_breadth'), capability='synthetic', started_at=old,
            received_at=old, status='error', reason='rate_limited', request_count=1))
        config = db.get(SourceSettings, source_id('ashare_breadth'))
        config.legacy_cooldown_until = old + timedelta(hours=1)
        config.next_run_at = old + timedelta(hours=1)
    calls = []
    def fetch(url):
        calls.append(url)
        return Response(json.dumps(sample()).encode(), datetime.now(UTC), 1, 200)
    monkeypatch.setattr('app.market.collect.fetch_https', fetch)
    queue(client, headers, 'ashare_breadth')
    process(claim())
    assert len(calls) == 1
    with Session(engine()) as db:
        assert ashare_cooldown_until(db, datetime.now(UTC)) is None
        assert db.scalar(select(Job)).state == 'succeeded'


@pytest.mark.parametrize('hint,wait', [(None,300),(0,300),(3600,3600),(1000000,86400)])
def test_ashare_retry_hint_uses_bounded_conservative_fallback(client, monkeypatch, hint, wait):
    headers = setup(client, monkeypatch)
    def limited(*_): raise SourceFailure('rate_limited', retry_after=hint)
    monkeypatch.setattr('app.market.collect.fetch_https', limited)
    queue(client, headers, 'ashare_breadth')
    process(claim())
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun))
        assert run.retry_not_before - run.received_at == timedelta(seconds=wait)


def test_deadline_column_still_obeys_append_only_history_and_database_bounds(client, monkeypatch):
    setup(client, monkeypatch)
    with Session(engine()) as db, db.begin(): ident = insert_limit(db).id
    with pytest.raises(DBAPIError), Session(engine()) as db, db.begin():
        db.execute(update(DataFetchRun).where(DataFetchRun.id == ident).values(retry_not_before=datetime.now(UTC)))
    for seconds in [-1,86401]:
        with pytest.raises(DBAPIError), Session(engine()) as db, db.begin():
            received = datetime.now(UTC)
            db.add(DataFetchRun(source_id=source_id('ashare_breadth'), capability='synthetic', started_at=received,
                received_at=received, status='error', reason='rate_limited', request_count=1,
                retry_not_before=received + timedelta(seconds=seconds)))
