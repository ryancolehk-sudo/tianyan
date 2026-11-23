"""Reuse an idle general slot without bypassing leases or supplier guards."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.market.collection_cadence import HISTORY_KEYS
from app.market.service import source_id
from app.models import Job, User
from app.worker import claim, finish
from test_data_sources import login


@pytest.fixture
def owner(client):
    login(client)
    with Session(engine()) as db:
        return db.scalar(select(User.id).where(User.username == 'owner'))


def queued(owner, family, *, payload=None, kind='source.collect', due=None):
    with Session(engine()) as db, db.begin():
        row = Job(owner_id=owner, kind=kind,
            payload={'source_id': str(source_id(family)), **(payload or {})},
            available_at=due or datetime.now(UTC)-timedelta(seconds=10))
        db.add(row)
        db.flush()
        return row.id


@pytest.mark.parametrize('family', ['tushare_market_rt', 'ths_industry_funds'])
def test_idle_general_collects_while_long_realtime_source_keeps_its_lease(owner, family):
    long_id = queued(owner, 'direction_kpl_strength')
    long = claim('realtime')
    assert long.id == long_id
    ready = queued(owner, family, payload={'scheduled_realtime': True})
    with Session(engine()) as db:
        original = db.get(Job, ready)
        due, payload = original.available_at, dict(original.payload)
    assisted = claim('general', assist_live=True)
    assert assisted.id == ready and assisted.attempts == 1
    assert assisted.available_at == due and assisted.payload == payload
    with Session(engine()) as db:
        still_running = db.get(Job, long.id)
        assert still_running.state == 'running' and still_running.lease_token == long.lease_token
    assert claim('realtime') is None


@pytest.mark.parametrize('own_family,kind', [
    ('tencent_market_rt', 'source.collect'), ('cninfo', 'source.collect'),
    ('tushare_market_rt', 'snapshot.create'),
])
@pytest.mark.parametrize('prefer_background', [False, True])
def test_ready_general_work_has_priority_over_any_older_assistance(owner, own_family, kind, prefer_background):
    assisted = queued(owner, 'tushare_market_rt', payload={'scheduled_realtime': True},
        due=datetime.now(UTC)-timedelta(minutes=10))
    own = queued(owner, own_family, kind=kind)
    assert claim('general', assist_live=True, prefer_background=prefer_background).id == own
    with Session(engine()) as db:
        waiting = db.get(Job, assisted)
        assert waiting.state == 'queued' and waiting.attempts == 0


def test_future_general_work_does_not_reserve_an_idle_slot(owner):
    queued(owner, 'tencent_market_rt', due=datetime.now(UTC)+timedelta(minutes=1))
    ready = queued(owner, 'ths_industry_funds', payload={'scheduled_realtime': True})
    assert claim('general', assist_live=True).id == ready


@pytest.mark.parametrize('family', [
    'tencent_web', 'direction_kpl_top', 'direction_kpl_strength', 'ths_concept_funds',
    'tushare_rt_k', 'tushare_rt_indices', 'fuyao_quotes', 'tdx_quotes',
])
def test_assistance_does_not_take_other_live_families(owner, family):
    ident = queued(owner, family, payload={'scheduled_realtime': True})
    assert claim('general', assist_live=True) is None
    assert claim('realtime').id == ident


@pytest.mark.parametrize('payload', [
    {}, {'scheduled_realtime': False}, {'scheduled_realtime': True, 'schema_probe': True},
    {'scheduled_realtime': True, 'integration_context': {'probe': True}},
    *[{'scheduled_realtime': True, key: {}} for key in sorted(HISTORY_KEYS)],
])
def test_assistance_keeps_manual_probe_and_historical_intent_out(owner, payload):
    ident = queued(owner, 'tushare_market_rt', payload=payload)
    assert claim('general', assist_live=True) is None
    with Session(engine()) as db:
        original = db.get(Job, ident)
        assert original.state == 'queued' and original.attempts == 0


def test_default_general_claim_and_other_worker_scopes_keep_their_existing_contract(owner):
    ident = queued(owner, 'tushare_market_rt', payload={'scheduled_realtime': True})
    assert claim('general') is None
    for role in ('realtime', 'recovery', 'all', 'scheduler'):
        with pytest.raises(ValueError):
            claim(role, assist_live=True)
    assert claim('realtime').id == ident


@pytest.mark.parametrize('family', ['tushare_market_rt', 'ths_industry_funds'])
def test_two_existing_slots_cannot_claim_one_automatic_job_twice(owner, family):
    ident = queued(owner, family, payload={'scheduled_realtime': True})
    barrier = threading.Barrier(2)
    def take(assistance):
        barrier.wait(timeout=5)
        return claim('general', assist_live=True) if assistance else claim('realtime')
    with ThreadPoolExecutor(max_workers=2) as slots:
        futures = [slots.submit(take, mode) for mode in (True, False)]
        result = [future.result(timeout=15) for future in futures]
    assert [job.id for job in result if job] == [ident]


def test_a_valid_source_lease_still_blocks_the_other_slot(owner):
    running_id = queued(owner, 'tushare_market_rt')
    running = claim('realtime')
    assert running.id == running_id
    ready = queued(owner, 'tushare_market_rt', payload={'scheduled_realtime': True})
    assert claim('general', assist_live=True) is None
    assert finish(running.id, running.lease_token)
    assert claim('general', assist_live=True).id == ready


def test_normal_general_process_enables_assistance_without_a_new_process_or_flag(monkeypatch):
    from app import worker
    stop = threading.Event()
    calls = []
    monkeypatch.setattr(worker.threading, 'Event', lambda: stop)
    monkeypatch.setattr(worker.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(worker, 'heartbeat', lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, 'schedule_general', lambda **kwargs: None)
    monkeypatch.setattr('app.worker_watchdog.ProgressWatchdog', lambda role: SimpleNamespace(
        start=lambda: None, advance=lambda: None, close=lambda: None))
    def take(role, **kwargs):
        calls.append((role, kwargs))
        stop.set()
    monkeypatch.setattr(worker, 'claim', take)
    worker.main(['--role', 'general'])
    assert calls == [('general', {'assist_live': True})]
