"""A slow saved read must not occupy the visible panel's only execution slot."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import read_cache, read_cache_worker
from app.db import engine
from app.market import fund_projection
from app.models import Job
from app.read_cache_models import ReadCache, ReadCacheDispatch
from test_read_cache import dispatch_queue, enabled


@pytest.mark.parametrize('slow_stage', ['background_build', 'public_funds_prepare'])
def test_visible_publication_completes_while_another_read_is_still_running(client, monkeypatch, slow_stage):
    groups = dispatch_queue(client, monkeypatch)
    slow_key = next(key for key, live in groups.items() if not live)
    with Session(engine()) as db, db.begin():
        slow = db.get(ReadCache, slow_key)
        owner, scope, dependencies = slow.owner_id, slow.scope, list(slow.dependency_keys)
        db.execute(delete(ReadCache).where(ReadCache.key != slow_key))

    started, release, stop = Event(), Event(), Event()

    def compute(db, owner_id, operation, params):
        if operation == 'details' and slow_stage == 'background_build':
            started.set()
            assert release.wait(8), 'test must release its deliberately slow read'
        return {'data': {'completed_operation': operation},
                'quality': {'status': 'empty', 'reasons': []}, 'as_of_time': None}

    class Wakeup:
        def wait(self):
            if stop.wait(.02):
                raise KeyboardInterrupt

        def close(self):
            pass

    monkeypatch.setattr(read_cache, 'compute', compute)
    def warm():
        if slow_stage == 'public_funds_prepare':
            started.set()
            assert release.wait(8)
        return True

    monkeypatch.setattr(fund_projection, 'warm_current_funds', warm)
    monkeypatch.setattr(read_cache_worker, 'prepare_current_workbenches', lambda db: 0)
    monkeypatch.setattr(read_cache_worker, 'heartbeat', lambda identity: None)
    monkeypatch.setattr(read_cache_worker, 'ReadCacheWakeup', Wakeup)

    def run():
        try:
            read_cache_worker.main()
        except KeyboardInterrupt:
            pass

    with ThreadPoolExecutor(max_workers=1) as driver:
        running = driver.submit(run)
        try:
            assert started.wait(3), 'the unrelated slow operation must actually be running'
            moment = datetime.now(UTC)
            params = {'view': 'overview'}
            focus_key = read_cache.fingerprint([scope, 'workbench', params])
            with Session(engine()) as db, db.begin():
                db.add(ReadCache(key=focus_key, owner_id=owner, scope=scope,
                    operation='workbench', params=params, dependency_keys=dependencies,
                    dirty=True, accessed_at=moment, focused_at=moment,
                    refresh_after=moment - timedelta(seconds=1)))
            deadline = time.monotonic() + 3
            published = None
            while time.monotonic() < deadline:
                with Session(engine()) as db:
                    published = db.get(ReadCache, focus_key).payload
                if published is not None:
                    break
                stop.wait(.02)
            assert published is not None, 'visible result waited behind an unrelated slow build'
            assert published['data'] == {'completed_operation': 'workbench'}
            assert not release.is_set()
            with Session(engine()) as db:
                slow = db.get(ReadCache, slow_key)
                assert (slow.lease_token is not None) == (slow_stage == 'background_build')
                assert slow.payload is None
                assert db.scalar(select(func.count()).select_from(Job)) == 0
        finally:
            release.set()
            stop.set()
            running.result(timeout=6)


def work(lane):
    with Session(engine(), expire_on_commit=False) as db:
        return read_cache.run_one(db, lane=lane)


def empty_result(*args):
    return {'data': {'complete': True}, 'quality': {'status': 'empty', 'reasons': []}, 'as_of_time': None}


def test_focused_claim_skips_locked_dispatch_and_retention_rows(client, monkeypatch):
    groups = dispatch_queue(client, monkeypatch)
    focus_key = next(key for key, live in groups.items() if live)
    old_key = next(key for key, live in groups.items() if not live)
    with Session(engine()) as db, db.begin():
        db.get(ReadCache, focus_key).focused_at = datetime.now(UTC)
        db.get(ReadCache, old_key).accessed_at = datetime.now(UTC) - read_cache.RETENTION - timedelta(seconds=1)
    monkeypatch.setattr(read_cache, 'compute', empty_result)
    with Session(engine()) as locked:
        dispatch = locked.get(ReadCacheDispatch, 1, with_for_update=True)
        locked.get(ReadCache, old_key, with_for_update=True)
        original_turn = dispatch.turn
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(work, 'focused')
            try:
                assert running.result(timeout=2), 'focused work waited on ordinary scheduling or cleanup'
            finally:
                locked.rollback()
    with Session(engine()) as db:
        assert db.get(ReadCache, focus_key).payload['data'] == {'complete': True}
        assert db.get(ReadCacheDispatch, 1).turn == original_turn
        assert db.get(ReadCache, old_key) is not None


def test_lane_partition_expires_and_preserves_ordinary_quota(client, monkeypatch):
    groups = dispatch_queue(client, monkeypatch)
    focused = [key for key, live in groups.items() if live][:2]
    with Session(engine()) as db, db.begin():
        for key in focused:
            db.get(ReadCache, key).focused_at = datetime.now(UTC)
    taken = []

    def finish(db, key, token):
        taken.append(key)
        entry = db.get(ReadCache, key)
        entry.lease_token = entry.lease_until = None
        entry.refresh_after = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    monkeypatch.setattr(read_cache, 'build', finish)
    for _ in range(8):
        assert work('focused')
    assert set(taken) == set(focused) and taken[:2] != [taken[0]] * 2
    assert all(taken.count(key) == 4 for key in focused)
    with Session(engine()) as db:
        assert db.get(ReadCacheDispatch, 1).turn == 0
    taken.clear()
    for _ in range(8):
        assert work('other')
    assert not set(taken) & set(focused)
    assert [groups[key] for key in taken] == [True, True, True, False] * 2
    with Session(engine()) as db, db.begin():
        for key in focused:
            db.get(ReadCache, key).focused_at = datetime.now(UTC) - read_cache.FOCUS_RECENCY - timedelta(seconds=1)
    assert not work('focused')
    taken.clear()
    for _ in range(8):
        assert work('other')
    assert set(focused) <= set(taken), 'expired foreground entries must return to normal service'


def test_focus_change_cannot_steal_an_already_running_ordinary_lease(client, monkeypatch):
    groups = dispatch_queue(client, monkeypatch)
    key = next(key for key, live in groups.items() if live)
    with Session(engine()) as db, db.begin():
        db.execute(delete(ReadCache).where(ReadCache.key != key))
    started, release = Event(), Event()
    calls = []

    def compute(*args):
        calls.append(1)
        started.set()
        assert release.wait(3)
        return empty_result()

    monkeypatch.setattr(read_cache, 'compute', compute)
    with ThreadPoolExecutor(max_workers=1) as pool:
        ordinary = pool.submit(work, 'other')
        try:
            assert started.wait(1)
            with Session(engine()) as db, db.begin():
                entry = db.get(ReadCache, key)
                token = entry.lease_token
                assert token is not None
                entry.focused_at = datetime.now(UTC)
            assert not work('focused')
            with Session(engine()) as db:
                assert db.get(ReadCache, key).lease_token == token
            release.set()
            assert ordinary.result(timeout=2)
        finally:
            release.set()
    assert len(calls) == 1
    with Session(engine()) as db:
        assert db.get(ReadCache, key).lease_token is None
        assert db.get(ReadCache, key).payload['data'] == {'complete': True}


def test_unknown_lane_never_mutates_the_queue(client, monkeypatch):
    dispatch_queue(client, monkeypatch)
    with Session(engine()) as db:
        with pytest.raises(ValueError, match='unknown_read_lane'):
            read_cache.run_one(db, lane='unbounded')
        assert db.get(ReadCacheDispatch, 1).turn == 0
        assert not any(db.scalars(select(ReadCache.lease_token)))
