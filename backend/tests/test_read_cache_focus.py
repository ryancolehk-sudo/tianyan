"""Visible live reads get bounded priority; background work still progresses."""
from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import read_cache
from app.db import engine
from app.models import Job
from app.read_cache_models import ReadCache
from test_read_cache import dispatch_queue, enabled, seed, worker


def test_active_hint_is_owner_scoped_coalesced_and_never_collects(client, monkeypatch):
    seed(client, monkeypatch)
    path = '/api/v1/workbench-view?view=funds'
    assert client.get(path).status_code == 503
    with Session(engine()) as db:
        assert db.scalar(select(ReadCache)).focused_at is None
    assert client.get(path, headers={'X-Workbench-Active': '1'}).status_code == 503
    with Session(engine()) as db:
        first = db.scalar(select(ReadCache)).focused_at
        assert first is not None
        assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert client.get(path, headers={'X-Workbench-Active': '1'}).status_code == 503
    with Session(engine()) as db:
        assert db.scalar(select(ReadCache)).focused_at == first


@pytest.mark.parametrize('params', [
    'view=overview&day=2026-10-23',
    'view=overview&as_of_time=2026-10-23T01:30:00Z',
])
def test_historical_parent_does_not_gain_live_priority(client, monkeypatch, params):
    seed(client, monkeypatch)
    assert client.get('/api/v1/workbench-view?' + params, headers={'X-Workbench-Active': '1'}).status_code == 503
    with Session(engine()) as db:
        assert db.scalar(select(ReadCache)).focused_at is None


def test_batch_prewarm_cannot_promote_itself_but_selected_stock_can(client, monkeypatch):
    seed(client, monkeypatch)
    today = datetime.now(UTC).astimezone(read_cache.SHANGHAI).date().isoformat()
    path = '/api/v1/workbench-details?day=' + today + '&codes='
    for codes in ['000100.SZ,000101.SZ', '000100.SZ']:
        assert client.get(path + codes, headers={'X-Workbench-Active': '1'}).status_code == 503
    with Session(engine()) as db:
        entries = list(db.scalars(select(ReadCache)))
        assert all((entry.focused_at is not None) == (len(entry.params['detail_codes']) == 1) for entry in entries)


def test_active_hint_does_not_wait_behind_ingestion_lock(client, monkeypatch):
    seed(client, monkeypatch)
    path = '/api/v1/workbench-view?view=overview'
    assert client.get(path).status_code == 503
    with Session(engine()) as locked:
        locked.scalar(select(ReadCache.key).with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(client.get, path, headers={'X-Workbench-Active': '1'})
            try:
                response = waiting.result(timeout=2)
            finally:
                locked.rollback()
            assert response.status_code == 503
    with Session(engine()) as db:
        assert db.scalar(select(ReadCache)).focused_at is None


def test_focus_gets_half_the_claims_and_cannot_starve_prepared_reads(client, monkeypatch):
    groups = dispatch_queue(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        focused = next(key for key, interactive in groups.items() if interactive)
        db.get(ReadCache, focused).focused_at = datetime.now(UTC)
    taken = []

    def finish(db, key, token):
        taken.append(key)
        entry = db.get(ReadCache, key)
        entry.lease_token = entry.lease_until = None
        entry.refresh_after = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    monkeypatch.setattr(read_cache, 'build', finish)
    for _ in range(16):
        assert worker()
    kinds = ['focus' if key == focused else 'live' if groups[key] else 'background' for key in taken]
    assert kinds == ['focus', 'live', 'focus', 'background'] * 4
    assert {key for key in taken if groups[key]} == {key for key, interactive in groups.items() if interactive}
    assert len({key for key in taken if not groups[key]}) == 4


def test_expired_focus_uses_existing_three_to_one_dispatch(client, monkeypatch):
    groups = dispatch_queue(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        entry = db.get(ReadCache, next(key for key, live in groups.items() if live))
        entry.focused_at = datetime.now(UTC) - read_cache.FOCUS_RECENCY - timedelta(seconds=1)
    taken = []
    monkeypatch.setattr(read_cache, 'build', lambda db, key, token: taken.append(key))
    for _ in range(4):
        assert worker()
    assert [groups[key] for key in taken] == [True, True, True, False]
