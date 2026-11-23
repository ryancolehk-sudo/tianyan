"""Reserve the existing fast slot for bounded reads; offline real PG only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.market.models import SourceSettings
from app.market.service import source_id
from app.models import Job, User
from app.worker import claim, finish
from test_data_resilience import source
from test_data_sources import login


@pytest.fixture
def scopes(client):
    login(client)
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(User.id).where(User.role == 'admin'))
        for family in ('tencent_web', 'tushare_market_rt', 'fuyao_quotes'):
            _, config = source(db, owner, family)
            if family == 'fuyao_quotes':
                config.config = {**config.config, 'collection_mode': 'full_market'}
        return owner


def queued(owner, family, seconds=60):
    with Session(engine()) as db, db.begin():
        job = Job(owner_id=owner, kind='source.collect',
            payload={'source_id': str(source_id(family))},
            available_at=datetime.now(UTC)-timedelta(seconds=seconds))
        db.add(job)
        db.flush()
        return job.id


@pytest.mark.parametrize('family', ['tushare_market_rt', 'fuyao_quotes'])
def test_whole_market_yields_fast_slot_without_stranding_existing_job(scopes, family):
    bulk = queued(scopes, family, 90)
    focus = queued(scopes, 'tencent_web', 5)
    with Session(engine()) as db:
        original = db.get(Job, bulk)
        payload, due = dict(original.payload), original.available_at
    assert claim('realtime', fast_only=True).id == focus
    with Session(engine()) as db:
        waiting = db.get(Job, bulk)
        assert waiting.state == 'queued' and waiting.attempts == 0
        assert waiting.payload == payload and waiting.available_at == due
    # The existing other slot still admits the overdue whole-market job.
    other = claim('realtime')
    assert other.id == bulk and other.attempts == 1
    assert claim('realtime', fast_only=True) is None


@pytest.mark.parametrize('mode', [None, 'selected'])
def test_configured_small_fuyao_quotes_keep_fast_access(scopes, mode):
    with Session(engine()) as db, db.begin():
        config = db.get(SourceSettings, source_id('fuyao_quotes'))
        config.config = {key: value for key, value in config.config.items() if key != 'collection_mode'}
        if mode is not None:
            config.config = {**config.config, 'collection_mode': mode}
    selected = queued(scopes, 'fuyao_quotes')
    assert claim('realtime', fast_only=True).id == selected


def test_mode_change_is_read_at_claim_without_rewriting_queued_intent(scopes):
    selected = queued(scopes, 'fuyao_quotes')
    with Session(engine()) as db:
        before = db.get(Job, selected)
        payload, due = dict(before.payload), before.available_at
    assert claim('realtime', fast_only=True) is None
    with Session(engine()) as db, db.begin():
        config = db.get(SourceSettings, source_id('fuyao_quotes'))
        config.config = {**config.config, 'collection_mode': 'selected'}
    job = claim('realtime', fast_only=True)
    assert job.id == selected and job.payload == payload and job.available_at == due


@pytest.mark.parametrize('family', ['tushare_market_rt', 'fuyao_quotes'])
def test_legacy_quote_only_slot_keeps_its_original_scope(scopes, family):
    selected = queued(scopes, family)
    assert claim('realtime', quotes_only=True).id == selected


def test_continuous_focus_does_not_take_the_other_slots_overdue_bulk(scopes):
    bulk = queued(scopes, 'tushare_market_rt', 90)
    for _ in range(4):
        focus = queued(scopes, 'tencent_web', 5)
        job = claim('realtime', fast_only=True)
        assert job.id == focus and finish(job.id, job.lease_token)
    queued(scopes, 'tencent_web', 5)
    assert claim('realtime').id == bulk


def test_two_existing_slots_take_focus_and_bulk_independently(scopes):
    bulk = queued(scopes, 'tushare_market_rt', 90)
    focus = queued(scopes, 'tencent_web', 5)
    barrier = threading.Barrier(2)
    def take(fast):
        barrier.wait(timeout=5)
        return claim('realtime', fast_only=fast)
    with ThreadPoolExecutor(max_workers=2) as slots:
        futures = [slots.submit(take, fast) for fast in (True, False)]
        jobs = [future.result(timeout=15) for future in futures]
    assert [job.id for job in jobs] == [focus, bulk]
    assert len({job.payload['source_id'] for job in jobs}) == 2
    assert claim('realtime', fast_only=True) is None
