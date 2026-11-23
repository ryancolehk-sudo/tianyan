"""Cooperative request ordering in isolated PostgreSQL, with no supplier I/O."""
from datetime import UTC, datetime, timedelta
from threading import Event, Thread, current_thread
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market import qt_budget as budget
from app.market.minute_backfill import configuration_fingerprint
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SourceFailure
from app.market.service import source_id
from app.models import AuditEvent, Job, Provider, User
from test_data_sources import configuration, configure, login


@pytest.fixture
def focused_sources(client, monkeypatch):
    headers = login(client)
    configure(client, headers, {**configuration('tencent_market_rt'), 'interval_seconds': 15,
        'collection_mode': 'full_market',
        'rights': {'display': 'allowed', 'cache': 'allowed', 'export': 'denied', 'redistribute': 'denied'}},
        key='fairness-bulk-config')
    configure(client, headers, {**configuration(), 'symbols': [],
        'collection_mode': 'focused', 'interval_seconds': 5}, key='fairness-focus-config')
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    # Only this offline test controls time; production keeps its calendar gate.
    monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime', lambda *args: None)


def running(family, clock, *, automatic=True):
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(family))
        config = db.get(SourceSettings, provider.id)
        job = Job(owner_id=config.owner_id, kind='source.collect', state='running', attempts=1,
            lease_token=uuid.uuid4(), lease_until=clock + timedelta(minutes=5),
            payload={'source_id': str(provider.id), **({'scheduled_realtime': True} if automatic else {})},
            result_ref={'existing': 'preserved'})
        db.add(job)
        db.flush()
        fingerprint = configuration_fingerprint(provider, config)
        db.expunge(job)
    return job, fingerprint


def permits():
    with Session(engine()) as db:
        return list(db.execute(select(AuditEvent.resource_id, AuditEvent.created_at)
            .where(AuditEvent.action == budget.ACTION).order_by(AuditEvent.created_at)))


def test_waiting_focus_gets_next_shared_opportunity_before_bulk_continuation(focused_sources, monkeypatch):
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(budget, 'now', lambda db: clock[0])
    bulk, bulk_configuration = running('tencent_market_rt', clock[0])
    focus, focus_configuration = running('tencent_web', clock[0])
    assert budget.reserve(bulk, expected_configuration=bulk_configuration)

    sleeping, release_focus, focus_done = Event(), Event(), Event()
    outcomes, errors, sleeps = [], [], []
    focus_thread = None

    def sleep(seconds):
        sleeps.append((current_thread().name, seconds))
        if current_thread() is focus_thread:
            if not sleeping.is_set():
                sleeping.set()
                assert release_focus.wait(8), 'focus was never released'
            else:
                clock[0] += timedelta(seconds=seconds)
        else:
            release_focus.set()
            assert focus_done.wait(8), 'focus could not reserve while bulk slept'
            clock[0] += timedelta(seconds=seconds)

    def reserve_focus():
        try:
            outcomes.append(budget.reserve(focus, expected_configuration=focus_configuration))
        except BaseException as error:
            errors.append(error)
        finally:
            focus_done.set()

    monkeypatch.setattr(budget.time, 'sleep', sleep)
    clock[0] += timedelta(seconds=.1)
    focus_thread = Thread(target=reserve_focus, name='waiting-focus')
    focus_thread.start()
    try:
        assert sleeping.wait(8), 'focus never reached the shared spacing wait'
        # The wait must release its row lock as well as the shared permit lock.
        with Session(engine()) as db, db.begin():
            assert db.scalar(select(Job).where(Job.id == focus.id).with_for_update(nowait=True))
        clock[0] += timedelta(seconds=.92)
        assert budget.reserve(bulk, expected_configuration=bulk_configuration, in_progress=True)
    finally:
        release_focus.set()
        focus_thread.join(10)
    assert not focus_thread.is_alive()
    assert not errors
    assert outcomes == [True]
    rows = permits()
    assert [row.resource_id for row in rows] == [bulk.id, focus.id, bulk.id]
    assert all((right.created_at - left.created_at).total_seconds() >= budget.SPACING
        for left, right in zip(rows, rows[1:]))
    with Session(engine()) as db:
        assert db.get(Job, focus.id).result_ref == {'existing': 'preserved'}
        saved = db.get(Job, bulk.id)
        assert saved.state == 'running' and saved.lease_token == bulk.lease_token
        assert saved.result_ref == {'existing': 'preserved'}


def park_focus(clock, monkeypatch, *, automatic=True):
    """Stop a real reserve call at its first outside-transaction sleep."""
    monkeypatch.setattr(budget, 'now', lambda db: clock[0])
    bulk, bulk_configuration = running('tencent_market_rt', clock[0])
    focus, focus_configuration = running('tencent_web', clock[0], automatic=automatic)
    assert budget.reserve(bulk, expected_configuration=bulk_configuration)
    clock[0] += timedelta(seconds=.1)

    def interrupt(seconds):
        raise InterruptedError('offline parked collector')

    monkeypatch.setattr(budget.time, 'sleep', interrupt)
    with pytest.raises(InterruptedError, match='offline parked collector'):
        budget.reserve(focus, expected_configuration=focus_configuration)
    clock[0] += timedelta(seconds=.92)
    return bulk, bulk_configuration, focus


def test_stalled_focus_delays_a_bulk_page_by_at_most_one_opportunity(focused_sources, monkeypatch):
    clock = [datetime.now(UTC)]
    bulk, fingerprint, focus = park_focus(clock, monkeypatch)
    sleeps = []

    def advance(seconds):
        sleeps.append(seconds)
        # Keep the focused lease and actual wait hint fresh. The bulk page
        # still has to progress after giving it one chance to reserve.
        clock[0] += timedelta(seconds=seconds)
        with Session(engine()) as db, db.begin():
            row = db.get(Job, focus.id)
            hint = {**row.result_ref['tencent_request_wait'], 'observed_at': clock[0].isoformat()}
            row.result_ref = {**row.result_ref, 'tencent_request_wait': hint}

    monkeypatch.setattr(budget.time, 'sleep', advance)
    before = clock[0]
    assert budget.reserve(bulk, expected_configuration=fingerprint, in_progress=True)
    assert len(sleeps) == 1 and (clock[0] - before).total_seconds() <= 1.05
    assert [row.resource_id for row in permits()] == [bulk.id, bulk.id]
    with Session(engine()) as db:
        assert not list(db.scalars(select(DataFetchRun)))
        assert db.get(Job, bulk.id).lease_token == bulk.lease_token


@pytest.mark.parametrize('boundary', [
    'expired_hint', 'future_hint', 'naive_hint', 'malformed_time', 'malformed_hint',
    'new_lease', 'expired_lease', 'queued', 'cancelled', 'owner_disabled',
    'owner_changed', 'rights_revoked', 'configuration_changed', 'paused',
    'binding_changed', 'manual', 'string_schedule', 'session_closed',
])
def test_ineligible_or_stale_focus_does_not_delay_bulk(focused_sources, monkeypatch, boundary):
    clock = [datetime.now(UTC)]
    if boundary == 'owner_disabled':
        # Revoking a different focused owner must not revoke the bulk owner.
        with Session(engine()) as db, db.begin():
            owner = User(username='synthetic-focus-owner', password_hash='unusable-fixture', role='viewer')
            db.add(owner)
            db.flush()
            db.get(SourceSettings, source_id('tencent_web')).owner_id = owner.id
    bulk, fingerprint, focus = park_focus(clock, monkeypatch, automatic=boundary != 'manual')
    with Session(engine()) as db, db.begin():
        row = db.get(Job, focus.id)
        config = db.get(SourceSettings, source_id('tencent_web'))
        hint = dict((row.result_ref or {}).get('tencent_request_wait') or {})
        if boundary == 'expired_hint':
            clock[0] += timedelta(seconds=2)
        elif boundary == 'future_hint':
            hint['observed_at'] = (clock[0] + timedelta(seconds=1)).isoformat()
        elif boundary == 'naive_hint':
            hint['observed_at'] = clock[0].replace(tzinfo=None).isoformat()
        elif boundary == 'malformed_time':
            hint['observed_at'] = []
        elif boundary == 'malformed_hint':
            hint = 'invalid'
        elif boundary == 'new_lease':
            row.lease_token = uuid.uuid4()
        elif boundary == 'expired_lease':
            row.lease_until = clock[0]
        elif boundary in {'queued', 'cancelled'}:
            row.state = boundary
        elif boundary == 'owner_disabled':
            db.get(User, focus.owner_id).enabled = False
        elif boundary == 'owner_changed':
            owner = User(username='synthetic-new-owner', password_hash='unusable-fixture', role='viewer')
            db.add(owner)
            db.flush()
            row.owner_id = owner.id
        elif boundary == 'rights_revoked':
            provider = db.get(Provider, source_id('tencent_web'))
            provider.rights = {**provider.rights, 'cache': 'denied'}
        elif boundary == 'configuration_changed':
            config.interval_seconds = 10
        elif boundary == 'paused':
            config.paused_reason = 'schema_changed'
        elif boundary == 'binding_changed':
            row.payload = {**row.payload, 'integration_context': {'revision': 999}}
        elif boundary == 'string_schedule':
            row.payload = {**row.payload, 'scheduled_realtime': 'true'}
        row.result_ref = {**row.result_ref, 'tencent_request_wait': hint}
    if boundary == 'session_closed':
        monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime',
            lambda db, family, *args: clock[0] + timedelta(hours=1) if family == 'tencent_web' else None)
    sleeps = []
    monkeypatch.setattr(budget.time, 'sleep', lambda seconds: sleeps.append(seconds))
    assert budget.reserve(bulk, expected_configuration=fingerprint, in_progress=True)
    assert sleeps == []
    assert [row.resource_id for row in permits()] == [bulk.id, bulk.id]


@pytest.mark.parametrize('boundary', ['supplier_cooldown', 'session_closed', 'local_spacing'])
def test_focus_priority_preserves_gates_and_clears_wait_when_deferred(focused_sources, monkeypatch, boundary):
    clock = [datetime.now(UTC)]
    bulk, _, focus = park_focus(clock, monkeypatch)
    with Session(engine()) as db:
        fingerprint = configuration_fingerprint(db.get(Provider, source_id('tencent_web')),
            db.get(SourceSettings, source_id('tencent_web')))
    deadline = clock[0] + timedelta(days=2)
    if boundary == 'supplier_cooldown':
        budget.record_failure(bulk, SourceFailure('rate_limited', retry_after=172800))
    elif boundary == 'session_closed':
        monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime', lambda *args: deadline)
    else:
        # A persisted permit in the future keeps its real deadline, including
        # during a local clock correction. Priority cannot manufacture capacity.
        with Session(engine()) as db, db.begin():
            db.add(AuditEvent(action=budget.ACTION, created_at=deadline,
                changed_field_names=['synthetic_offline_future_permit']))
        deadline += timedelta(seconds=budget.SPACING)
    monkeypatch.setattr(budget.time, 'sleep', lambda seconds: None)
    assert not budget.reserve(focus, expected_configuration=fingerprint)
    with Session(engine()) as db:
        row = db.get(Job, focus.id)
        assert row.state == 'queued' and row.available_at == deadline
        assert row.attempts == 0 and row.lease_token is None
        assert row.result_ref == {'existing': 'preserved'}
        assert not list(db.scalars(select(DataFetchRun)))
