"""Persistent projections on a disposable real PostgreSQL; no upstream calls."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.orm import Session
from test_data_sources import ORIGIN, PASSWORD, login
from test_terminal_workbench import AT, save, setup
from test_tushare_market import quote

from app import read_cache
from app.auth import hasher
from app.config import settings
from app.db import engine
from app.market.models import DataFetchRun, SourceSettings
from app.market.service import source_id
from app.models import Job, Provider, User
from app.read_cache_models import ReadCache, ReadCacheDispatch, ReadDependency

URL = '/api/v1/workbench-details?day=2026-10-23&codes=000100.SZ'


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings(), 'read_cache_enabled', True)
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: pytest.fail('Cache reads must not use network'))


def seed(client, monkeypatch):
    headers = setup(client, monkeypatch, ['tushare_market_rt'])
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='10')])
    return headers


@pytest.mark.parametrize('operation,params', [
    ('workbench', {'view': view, 'day': '2026-10-23'}) for view in ('overview', 'auction', 'ladder', 'funds')
] + [('details', {'day': '2026-10-23', 'detail_codes': ['000100.SZ']})])
def test_persisted_projection_keeps_the_full_existing_json_contract(client, monkeypatch, operation, params):
    from fastapi.encoders import jsonable_encoder
    from app.market.terminal_read import workbench_view
    seed(client, monkeypatch)
    with Session(engine()) as db:
        owner = db.scalar(select(User.id))
        expected = jsonable_encoder(workbench_view(db, owner,
            **{**params, 'day': datetime.fromisoformat(params['day']).date()}))
        result = read_cache.compute(db, owner, operation, params)
    assert result['data'] == expected
    assert result['as_of_time'] is None
    assert result['data']['stocks'][0]['price'] == 10
    assert result['data']['stocks'][0]['source_received_at']


def worker():
    with Session(engine(), expire_on_commit=False) as db:
        return read_cache.run_one(db)


def test_parent_pinned_detail_cache_ignores_later_receipts_but_rechecks_rights(client, monkeypatch):
    from urllib.parse import urlencode
    from app.market import terminal_read
    from test_terminal_workbench import view

    seed(client, monkeypatch)
    captured = AT + timedelta(seconds=30, microseconds=123456)
    monkeypatch.setattr(terminal_read, 'now', lambda db: captured)
    parent = ready_parent(client, '/api/v1/workbench-view?view=overview&day=2026-10-23').json()['data']
    path = URL + '&' + urlencode({'observation_cutoff': parent['detail_observation_cutoff']})
    first = client.get(path)
    assert first.status_code == 200
    assert first.json()['meta']['as_of_time'] is None
    assert first.json()['data']['revision'] == parent['detail_revision']
    with Session(engine()) as db:
        entry = db.scalar(select(ReadCache).where(ReadCache.operation == 'details'))
        assert not any(key.startswith('family:') for key in entry.dependency_keys)
        assert {'authorization', 'catalog', 'routing-health'} <= set(entry.dependency_keys)
        assert entry.refresh_after > datetime.now(UTC) + timedelta(days=1)
    later = AT + timedelta(minutes=1)
    monkeypatch.setattr(terminal_read, 'now', lambda db: later + timedelta(seconds=30))
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='11', at=later)], later)
    compute = read_cache.compute
    monkeypatch.setattr(read_cache, 'compute', lambda *a: pytest.fail('pinned result rebuilt for a later receipt'))
    again = client.get(path)
    assert again.status_code == 200 and again.json()['data'] == first.json()['data']
    monkeypatch.setattr(read_cache, 'compute', compute)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('tushare_market_rt'))
        provider.rights = {**provider.rights, 'display': 'denied'}
    denied = client.get(path)
    assert denied.status_code == 200 and denied.json()['data']['stocks'][0]['price'] is None
    assert denied.headers['x-cache-scope'] != first.headers['x-cache-scope']


@pytest.mark.parametrize('skew_seconds', [-30, 30])
def test_queue_and_lease_use_database_clock_and_still_respect_retry_deadline(client, monkeypatch, skew_seconds):
    seed(client, monkeypatch)
    ready_parent(client, '/api/v1/workbench-view?day=2026-10-23&view=overview')
    with Session(engine()) as db, db.begin():
        db.execute(text("SELECT bump_read_dependency('family:tushare_market_rt')"))
        key = db.scalar(select(ReadCache.key))
    class SkewedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.now(UTC) + timedelta(seconds=skew_seconds)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(read_cache, 'datetime', SkewedDateTime)
    assert worker(), 'a committed database invalidation is ready despite API-process clock skew'
    with Session(engine()) as db, db.begin():
        entry = db.get(ReadCache,key)
        entry.dirty = True
        entry.last_error = 'synthetic_retry'
        entry.refresh_after = func.clock_timestamp() + timedelta(seconds=15)
    # An ahead process clock must not make a persisted retry run early either.
    assert worker() is False


def ready_parent(client, path):
    response = client.get(path)
    assert response.status_code == 503 and response.json()['error']['code'] == 'read_cache_warming'
    assert worker()
    response = client.get(path)
    assert response.status_code == 200
    return response


def test_overview_cold_read_queues_without_waiting_for_large_projection(client, monkeypatch):
    seed(client, monkeypatch)
    day = datetime.now(UTC).astimezone(read_cache.SHANGHAI).date().isoformat()
    path = '/api/v1/review-data/overview?trade_day=' + day + '&lookback=20'
    calls = []
    def compute(*args):
        calls.append(True)
        return {'data': {'trade_day': day, 'records': [], 'rankings': {}},
                'quality': {'status': 'partial', 'reasons': []}, 'as_of_time': None}
    monkeypatch.setattr(read_cache, 'compute', compute)
    response = client.get(path)
    assert response.status_code == 503 and response.json()['error']['code'] == 'read_cache_warming'
    assert calls == []
    with Session(engine()) as db:
        entry = db.scalar(select(ReadCache).where(ReadCache.operation == 'review-overview'))
        assert entry.payload is None and entry.last_attempt_at is None
        assert db.scalar(select(read_cache.interactive_condition(datetime.now(UTC))).where(ReadCache.key == entry.key))
    assert worker()
    assert len(calls) == 1
    assert client.get(path).json()['data']['trade_day'] == day


def test_live_details_queue_quickly_without_computing_on_api_thread(client,monkeypatch):
    seed(client,monkeypatch)
    monkeypatch.setattr('app.market.terminal_read.now',lambda db:datetime.now(UTC))
    today=datetime.now(UTC).astimezone(read_cache.SHANGHAI).date().isoformat()
    path='/api/v1/workbench-details?day='+today+'&codes=000100.SZ'
    original=read_cache.compute
    monkeypatch.setattr(read_cache,'compute',lambda *a:pytest.fail('live GET must not compute'))
    waiting=client.get(path)
    assert waiting.status_code==503 and waiting.json()['error']['code']=='read_cache_warming'
    monkeypatch.setattr(read_cache,'compute',original)
    assert worker()
    ready=client.get(path)
    assert ready.status_code==200 and ready.json()['data']['stocks'][0]['code']=='000100.SZ'


def test_cached_get_relogin_restart_and_304_do_not_recompute(client, monkeypatch):
    headers = seed(client, monkeypatch)
    scope = client.get('/api/v1/session').json()['data']['cache_scope']
    first = client.get(URL)
    assert first.status_code == 200, first.text
    assert first.json()['data']['stocks'][0]['price'] == 10
    assert first.json()['meta']['read_cache']['state'] == 'fresh'
    assert first.headers['x-cache-scope'] == scope
    assert first.headers['cache-control'] == 'private, no-cache'
    monkeypatch.setattr(read_cache, 'compute', lambda *a: pytest.fail('cached GET recomputed'))
    # New SQLAlchemy sessions/engine pools have no in-memory result to reuse.
    engine().dispose()
    second = client.get(URL, headers={'If-None-Match': first.headers['etag']})
    assert second.status_code == 304 and not second.content
    assert second.headers['x-read-cache-built-at'] == first.headers['x-read-cache-built-at']
    assert client.delete('/api/v1/session', headers=headers).status_code == 204
    assert client.get(URL, headers={'If-None-Match': first.headers['etag']}).status_code == 401
    again = client.post('/api/v1/session', json={'username': 'owner', 'password': PASSWORD}, headers=ORIGIN)
    assert again.json()['data']['cache_scope'] == scope
    assert again.headers['cache-control'] == 'no-store'
    assert client.get(URL, headers={'If-None-Match': first.headers['etag']}).status_code == 304


def test_committed_ingestion_queues_worker_and_old_result_has_truthful_clocks(client, monkeypatch):
    seed(client, monkeypatch)
    first = client.get(URL)
    stamp = first.json()['meta']['read_cache']['built_at']
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='11', at=AT+timedelta(minutes=1))], AT+timedelta(minutes=1))
    waiting = client.get(URL, headers={'If-None-Match': first.headers['etag']})
    assert waiting.status_code == 200
    assert waiting.json()['meta']['read_cache']['state'] == 'refreshing'
    assert waiting.json()['data']['stocks'][0]['price'] == 10
    assert waiting.json()['meta']['read_cache']['built_at'] == stamp
    assert worker()
    after = client.get(URL)
    assert after.json()['data']['stocks'][0]['price'] == 11
    assert after.json()['meta']['read_cache']['state'] == 'fresh'
    assert after.headers['etag'] != first.headers['etag']
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 0


def test_rollback_does_not_invalidate_and_poll_metadata_does_not_reset_details(client, monkeypatch):
    seed(client, monkeypatch)
    first = client.get(URL)
    with Session(engine()) as db:
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='12', at=AT+timedelta(minutes=2))], AT+timedelta(minutes=2))
        db.rollback()
    with Session(engine()) as db, db.begin():
        config = db.get(SourceSettings, source_id('tushare_market_rt'))
        config.next_run_at += timedelta(minutes=1)
        db.add(DataFetchRun(source_id=config.source_id, capability='market', started_at=AT,
            received_at=AT, status='empty', request_count=0))
    monkeypatch.setattr(read_cache, 'compute', lambda *a: pytest.fail('no changed detail evidence'))
    assert client.get(URL, headers={'If-None-Match': first.headers['etag']}).status_code == 304


def test_current_permission_revocation_changes_scope_and_cannot_restore_values(client, monkeypatch):
    seed(client, monkeypatch)
    first = client.get(URL)
    old_scope = client.get('/api/v1/session').json()['data']['cache_scope']
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('tushare_market_rt'))
        provider.rights = {**provider.rights, 'cache': 'denied'}
    session = client.get('/api/v1/session').json()['data']
    assert session['cache_scope'] != old_scope
    denied = client.get(URL, headers={'If-None-Match': first.headers['etag']})
    assert denied.status_code == 200
    assert denied.json()['data']['stocks'][0]['price'] is None
    assert denied.headers['x-cache-scope'] == session['cache_scope']
    assert denied.headers['etag'] != first.headers['etag']
    # Old scope queued before revocation is discarded, never refreshed under a
    # new permission scope or sent to another account.
    assert worker()
    with Session(engine()) as db:
        assert db.scalar(select(ReadCache).where(ReadCache.scope == old_scope)) is None


def test_other_account_gets_distinct_scope_and_disabled_user_cannot_validate(client, monkeypatch):
    headers = seed(client, monkeypatch)
    first = client.get(URL)
    with Session(engine()) as db, db.begin():
        db.add(User(username='second', password_hash=hasher.hash(PASSWORD), role='viewer'))
    client.delete('/api/v1/session', headers=headers)
    second = client.post('/api/v1/session', json={'username': 'second', 'password': PASSWORD}, headers=ORIGIN)
    assert second.json()['data']['cache_scope'] != first.headers['x-cache-scope']
    response = client.get(URL, headers={'If-None-Match': first.headers['etag']})
    assert response.status_code == 200 and response.headers['etag'] != first.headers['etag']
    with Session(engine()) as db, db.begin():
        db.scalar(select(User).where(User.username == 'second')).enabled = False
    assert client.get(URL, headers={'If-None-Match': response.headers['etag']}).status_code == 401


def test_exact_asof_and_date_have_independent_cache_and_no_future_data(client, monkeypatch):
    seed(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='11', at=AT+timedelta(minutes=1))], AT+timedelta(minutes=1))
    observed = client.get(URL + '&as_of_time=2026-10-23T10:30:30%2B08:00')
    current = client.get(URL)
    assert observed.json()['data']['stocks'][0]['price'] == 10
    assert current.json()['data']['stocks'][0]['price'] == 11
    assert observed.headers['etag'] != current.headers['etag']
    assert client.get(URL.replace('2026-10-23', '2099-10-23')).status_code == 422
    assert client.get(URL + '&as_of_time=2099-10-23T10:30:30%2B08:00').status_code == 422
    empty = client.get(URL.replace('2026-10-23', '2026-10-22'))
    assert empty.json()['data']['trade_day'] == '2026-10-22'
    assert empty.json()['data']['stocks'][0]['price'] is None


def test_lease_deduplicates_expires_and_data_advance_keeps_refreshing(client, monkeypatch):
    seed(client, monkeypatch)
    client.get(URL)
    with Session(engine(), expire_on_commit=False) as db:
        entry = db.scalar(select(ReadCache))
        key, old_revision = entry.key, entry.revision
        first = read_cache.claim(db, key)
        attempted_at = db.get(ReadCache, key).last_attempt_at
        assert first and attempted_at is not None
        assert read_cache.claim(db, key) is None
        assert db.get(ReadCache, key).last_attempt_at == attempted_at
        assert read_cache.claim(db, key, moment=datetime.now(UTC)+timedelta(seconds=121))
        read_cache.release_obsolete(db, key, db.get(ReadCache, key).lease_token)
        assert db.get(ReadCache, key).last_attempt_at >= attempted_at
    original = read_cache.compute
    def changed_during_compute(db, *args):
        result = original(db, *args)
        with Session(engine()) as changed, changed.begin():
            changed.execute(text("SELECT bump_read_dependency('family:tushare_market_rt')"))
        return result
    monkeypatch.setattr(read_cache, 'compute', changed_during_compute)
    with Session(engine()) as db, db.begin():
        db.execute(text("SELECT bump_read_dependency('family:tushare_market_rt')"))
    assert worker()
    with Session(engine()) as db:
        entry = db.get(ReadCache, key)
        assert entry.revision != old_revision and entry.dirty and entry.lease_token is None
        assert entry.revision != read_cache.desired_revision(db, entry)


def test_repeatable_read_candidate_advances_during_continuous_ingestion(client, monkeypatch):
    seed(client, monkeypatch)
    assert client.get(URL).json()['data']['stocks'][0]['price'] == 10
    original = read_cache.compute
    for value in (11, 13, 15):
        stamp = AT + timedelta(minutes=value - 10)
        with Session(engine()) as db, db.begin():
            save(db, 'tushare_market_rt', [quote('000100.SZ', price=str(value), at=stamp)], stamp)

        def advancing(db, *args):
            assert db.scalar(text('SHOW transaction_isolation')) == 'repeatable read'
            assert db.scalar(text('SHOW transaction_read_only')) == 'on'
            candidate = original(db, *args)
            with Session(engine()) as changed, changed.begin():
                save(changed, 'tushare_market_rt', [quote('000100.SZ', price=str(value + 1),
                    at=stamp + timedelta(seconds=1))], stamp + timedelta(seconds=1))
            # A second read in this build must not mix newly committed evidence
            # with earlier panels or dependency versions from its snapshot.
            assert original(db, *args) == candidate
            return candidate

        monkeypatch.setattr(read_cache, 'compute', advancing)
        assert worker()
        response = client.get(URL)
        assert response.json()['data']['stocks'][0]['price'] == value
        assert response.json()['meta']['read_cache']['state'] == 'refreshing'
        with Session(engine()) as db:
            entry = db.scalar(select(ReadCache))
            assert entry.dirty and entry.lease_token is None
            assert entry.revision != read_cache.desired_revision(db, entry)
    monkeypatch.setattr(read_cache, 'compute', original)
    assert worker()
    response = client.get(URL)
    assert response.json()['data']['stocks'][0]['price'] == 16
    assert response.json()['meta']['read_cache']['state'] == 'fresh'


@pytest.mark.parametrize('revoke', ['provider', 'owner'])
def test_current_authorization_is_rechecked_outside_the_build_snapshot(client, monkeypatch, revoke):
    seed(client, monkeypatch)
    client.get(URL)
    with Session(engine()) as db, db.begin():
        entry = db.scalar(select(ReadCache))
        key, owner_id = entry.key, entry.owner_id
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='11', at=AT + timedelta(minutes=1))],
             AT + timedelta(minutes=1))
    original = read_cache.compute

    def revoked(db, *args):
        candidate = original(db, *args)
        with Session(engine()) as changed, changed.begin():
            if revoke == 'provider':
                provider = changed.get(Provider, source_id('tushare_market_rt'))
                provider.rights = {**provider.rights, 'display': 'denied'}
            else:
                changed.get(User, owner_id).enabled = False
        return candidate

    monkeypatch.setattr(read_cache, 'compute', revoked)
    with Session(engine(), expire_on_commit=False) as db:
        token = read_cache.claim(db, key)
        assert token and not read_cache.build(db, key, token)
        assert db.get(ReadCache, key) is None
    monkeypatch.setattr(read_cache, 'compute', original)
    response = client.get(URL)
    if revoke == 'owner':
        assert response.status_code == 401
    else:
        assert response.status_code == 200
        assert response.json()['data']['stocks'][0]['price'] is None


def test_revoked_cold_build_has_no_payload_or_expired_orm_row_failure(client, monkeypatch):
    seed(client, monkeypatch)
    original = read_cache.compute

    def revoked(db, *args):
        candidate = original(db, *args)
        with Session(engine()) as changed, changed.begin():
            provider = changed.get(Provider, source_id('tushare_market_rt'))
            provider.rights = {**provider.rights, 'display': 'denied'}
        return candidate

    monkeypatch.setattr(read_cache, 'compute', revoked)
    response = client.get(URL)
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'read_cache_warming'
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ReadCache)) == 0


def test_active_health_route_changes_parent_and_details_without_changing_access_scope(client, monkeypatch):
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.resilience_models import DataHealth

    seed(client, monkeypatch)
    parent_url = '/api/v1/workbench-view?view=overview&day=2026-10-23'
    parent, child = ready_parent(client, parent_url), client.get(URL)
    before = parent.json()['data']['detail_revision']
    assert child.json()['data']['revision'] == before
    scope = child.headers['x-cache-scope']
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('tushare_market_rt'))
        config = db.get(SourceSettings, provider.id)
        required = {'universe': 'whole_market', 'asset_type': 'stock', 'symbols': []}
        health = DataHealth(owner_id=config.owner_id, primary_source_id=provider.id,
            active_source_id=None, capability='stock_quote', scope_key=read_cache.fingerprint(required),
            scope=required, trade_day=AT.date(), state='recovering', stage='source',
            failure_count=0, recovery_successes=0,
            configuration_sha256=configuration_fingerprint(provider, config), updated_at=datetime.now(UTC))
        db.add(health)
        db.flush()
        health.active_source_id = provider.id
        health.state = 'normal'
    with Session(engine()) as db:
        assert db.get(ReadDependency, 'routing-health').revision > 0
        entries = list(db.scalars(select(ReadCache)))
        assert len(entries) == 2 and all(entry.dirty for entry in entries)
        assert read_cache.cache_scope(db, db.scalar(select(User))) == scope
    assert worker() and worker()
    parent, child = client.get(parent_url), client.get(URL)
    assert parent.json()['data']['detail_revision'] == child.json()['data']['revision'] != before
    assert parent.headers['x-cache-scope'] == child.headers['x-cache-scope'] == scope
    assert parent.json()['meta']['read_cache']['state'] == child.json()['meta']['read_cache']['state'] == 'fresh'


@pytest.mark.parametrize('replacement', [False, True])
def test_expired_or_replaced_lease_cannot_publish_a_complete_candidate(client, monkeypatch, replacement):
    seed(client, monkeypatch)
    client.get(URL)
    with Session(engine()) as db, db.begin():
        entry = db.scalar(select(ReadCache))
        key, before = entry.key, entry.revision
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='11', at=AT + timedelta(minutes=1))],
             AT + timedelta(minutes=1))
    original = read_cache.compute
    replacement_tokens = []

    def expired(db, *args):
        candidate = original(db, *args)
        with Session(engine()) as changed, changed.begin():
            changed.execute(update(ReadCache).where(ReadCache.key == key).values(
                lease_until=datetime.now(UTC) - timedelta(seconds=1)))
        if replacement:
            with Session(engine()) as changed:
                replacement_tokens.append(read_cache.claim(changed, key))
        return candidate

    monkeypatch.setattr(read_cache, 'compute', expired)
    with Session(engine(), expire_on_commit=False) as db:
        token = read_cache.claim(db, key)
        assert token and not read_cache.build(db, key, token)
        entry = db.get(ReadCache, key)
        assert entry.revision == before and entry.payload['data']['stocks'][0]['price'] == 10
        assert entry.dirty
        if replacement:
            assert entry.lease_token == replacement_tokens[0] != token


@pytest.mark.parametrize('outcome', ['success', 'failure', 'expired'])
def test_hot_dirty_page_cannot_starve_other_parents_or_details(client, monkeypatch, outcome):
    seed(client, monkeypatch)
    paths = ['/api/v1/workbench-view?day=2026-10-23&view=' + kind
             for kind in ('overview', 'ladder', 'funds', 'auction')] + [URL]
    for path in paths:
        if 'workbench-view' in path:
            ready_parent(client, path)
        else:
            assert client.get(path).status_code == 200
    moment = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        entries = list(db.scalars(select(ReadCache)))
        hot = next(entry for entry in entries if entry.operation == 'workbench' and entry.params.get('view') == 'overview')
        hot_key = hot.key
        for index, entry in enumerate(sorted(entries, key=lambda item: item.key)):
            entry.last_attempt_at = moment - timedelta(days=1, seconds=index)
            entry.accessed_at = moment - timedelta(minutes=2)
            entry.refresh_after = moment - timedelta(seconds=1)
            entry.dirty = True
        hot.last_attempt_at = moment - timedelta(days=2)
        hot.accessed_at = moment
    original, attempted = read_cache.compute, []

    def tracked(db, owner_id, operation, params):
        label = params['view'] if operation == 'workbench' else operation
        attempted.append(label)
        if label == 'overview' and outcome == 'failure':
            raise RuntimeError('synthetic build failure')
        candidate = original(db, owner_id, operation, params)
        if label == 'overview' and outcome == 'expired':
            with Session(engine()) as changed, changed.begin():
                changed.execute(update(ReadCache).where(ReadCache.key == hot_key).values(
                    lease_until=datetime.now(UTC) - timedelta(seconds=1)))
        return candidate

    monkeypatch.setattr(read_cache, 'compute', tracked)
    for turn in range(len(paths)):
        with Session(engine()) as db, db.begin():
            db.execute(text("SELECT bump_read_dependency('family:tushare_market_rt')"))
        if turn == 0 and outcome == 'failure':
            with pytest.raises(RuntimeError, match='synthetic build failure'):
                worker()
        else:
            assert worker()
        # A user keeps revisiting the hot page and new evidence keeps every
        # projection dirty. Persisted attempt order still bounds service to N.
        with Session(engine()) as db, db.begin():
            entry = db.get(ReadCache, hot_key)
            assert entry.last_attempt_at > moment - timedelta(seconds=1)
            entry.accessed_at = datetime.now(UTC)
            entry.refresh_after = moment - timedelta(seconds=1)
            entry.dirty = True
    assert attempted[0] == 'overview'
    assert len(attempted) == len(set(attempted)) == len(paths)
    assert set(attempted) == {'overview', 'ladder', 'funds', 'auction', 'details'}


def dispatch_queue(client, monkeypatch, *, sector=False):
    """Real durable queue: old prewarm work precedes five newly visited panels."""
    seed(client, monkeypatch)
    moment = datetime.now(UTC)
    today = moment.astimezone(read_cache.SHANGHAI).date().isoformat()
    specs = [('workbench', {'view': view, 'day': today}, True)
             for view in ('overview', 'ladder', 'funds', 'auction')]
    specs.append(('details', {'view': 'overview', 'day': today, 'detail_codes': ['000100.SZ']}, True))
    if sector:
        specs.append(('details', {'view': 'overview', 'day': today,
            'detail_codes': [], 'sector': 'BK0001.DC'}, True))
    for index in range(40):
        specs.append(('details', {'view': 'overview', 'day': today,
            'detail_codes': [f'{1000 + index * 24 + offset:06}.SZ' for offset in range(24)]}, False))
    groups = {}
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(User))
        scope = read_cache.cache_scope(db, owner)
        dependencies = {operation: read_cache.dependency_keys(operation, owner.id)
                        for operation in ('workbench', 'details')}
        for key in sorted(set().union(*dependencies.values())):
            db.execute(text('INSERT INTO read_cache_dependencies(key,revision,last_txid) '
                'VALUES(:key,0,0) ON CONFLICT(key) DO NOTHING'), {'key': key})
        for operation, params, interactive in specs:
            key = read_cache.fingerprint([scope, operation, params])
            groups[key] = interactive
            db.add(ReadCache(key=key, owner_id=owner.id, scope=scope, operation=operation,
                params=params, dependency_keys=dependencies[operation], dirty=True,
                refresh_after=moment - timedelta(seconds=1), accessed_at=moment,
                last_attempt_at=moment - timedelta(seconds=1) if interactive else None))
        db.execute(text('INSERT INTO read_cache_dispatch(id,turn) VALUES(1,0) '
                        'ON CONFLICT(id) DO UPDATE SET turn=0'))
    return groups


@pytest.mark.parametrize('outcome', ['success', 'failure', 'expired'])
def test_persistent_three_to_one_quota_bounds_interactive_and_background_service(client, monkeypatch, outcome):
    groups = dispatch_queue(client, monkeypatch)
    attempted, original = [], read_cache.build

    def observed(db, key, token):
        attempted.append(key)
        return original(db, key, token)

    def compute(db, *args):
        if len(attempted) == 1 and outcome == 'failure':
            raise RuntimeError('synthetic quota failure')
        if len(attempted) == 1 and outcome == 'expired':
            with Session(engine()) as expired, expired.begin():
                expired.execute(update(ReadCache).where(ReadCache.key == attempted[-1]).values(
                    lease_until=datetime.now(UTC) - timedelta(seconds=1)))
        return {'data': {'stocks': []}, 'quality': {'status': 'empty', 'reasons': []}, 'as_of_time': None}

    monkeypatch.setattr(read_cache, 'build', observed)
    monkeypatch.setattr(read_cache, 'compute', compute)
    for turn in range(8):
        if turn == 0 and outcome == 'failure':
            with pytest.raises(RuntimeError, match='synthetic quota failure'):
                worker()
        else:
            assert worker()
        with Session(engine()) as db, db.begin():
            assert db.get(ReadCacheDispatch, 1).turn == (turn + 1) % 4
            entry = db.get(ReadCache, attempted[-1])
            assert entry.last_attempt_at is not None and entry.lease_token is None
            if turn == 0 and outcome != 'success':
                assert entry.payload is None and entry.dirty
            # Keep the hot set continuously eligible, including failed/obsolete
            # work. Its retries still consume their persisted dispatch turn.
            entry.dirty = True
            entry.refresh_after = datetime.now(UTC) - timedelta(seconds=1)
            entry.accessed_at = datetime.now(UTC)
    assert [groups[key] for key in attempted] == [True, True, True, False] * 2
    assert {key for key in attempted[:6] if groups[key]} == {key for key in groups if groups[key]}
    assert len({key for key in attempted if not groups[key]}) == 2


@pytest.mark.parametrize('interactive', [True, False])
def test_dispatch_cursor_changes_only_after_a_claim_and_empty_groups_fall_back(client, monkeypatch, interactive):
    groups = dispatch_queue(client, monkeypatch)
    phase = 3 if interactive else 0
    with Session(engine()) as db, db.begin():
        db.execute(update(ReadCache).values(lease_token=uuid.uuid4(), lease_until=datetime.now(UTC) + timedelta(hours=1)))
        db.get(ReadCacheDispatch, 1).turn = phase
    assert not worker()
    with Session(engine()) as db, db.begin():
        assert db.get(ReadCacheDispatch, 1).turn == phase
        key = next(key for key in groups if groups[key] == interactive)
        db.get(ReadCache, key).lease_until = None
    taken = []
    monkeypatch.setattr(read_cache, 'build', lambda db, key, token: taken.append((key, token)))
    assert worker()
    assert len(taken) == 1 and groups[taken[0][0]] == interactive
    with Session(engine()) as db:
        assert db.get(ReadCacheDispatch, 1).turn == (phase + 1) % 4
        assert db.get(ReadCache, taken[0][0]).lease_token == taken[0][1]


def test_dispatch_current_scope_excludes_history_fixed_cutoffs_and_inactive_reads(client, monkeypatch):
    groups = dispatch_queue(client, monkeypatch, sector=True)
    with Session(engine()) as db, db.begin():
        entries = list(db.scalars(select(ReadCache).where(ReadCache.operation == 'workbench', ReadCache.key.in_(
            [key for key, interactive in groups.items() if interactive]))))
        historical, observed, inactive = entries[:3]
        historical.params = {**historical.params, 'day': '2020-01-01'}
        observed.params = {**observed.params, 'as_of_time': datetime.now(UTC).isoformat()}
        inactive.accessed_at = datetime.now(UTC) - timedelta(minutes=6)
        excluded = {historical.key, observed.key, inactive.key}
    taken = []
    monkeypatch.setattr(read_cache, 'build', lambda db, key, token: taken.append(key))
    for _ in range(3):
        assert worker()
    assert len(taken) == 3 and not set(taken) & excluded
    assert set(taken) == {key for key, interactive in groups.items() if interactive} - excluded


def test_two_workers_share_persistent_quota_and_never_claim_the_same_lease(client, monkeypatch):
    groups = dispatch_queue(client, monkeypatch, sector=True)
    barrier, taken = Barrier(2), []

    def held_build(db, key, token):
        entry = db.get(ReadCache, key)
        assert entry.lease_token == token and entry.lease_until > datetime.now(UTC)
        taken.append((entry.last_attempt_at, key, token))
        # Both claims must commit before either mocked build can finish. The
        # singleton dispatch row must not stay locked during computation.
        barrier.wait(timeout=5)
        return False

    monkeypatch.setattr(read_cache, 'build', held_build)
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert all(executor.map(lambda _: worker(), range(8)))
    ordered = sorted(taken)
    assert len({key for _, key, _ in ordered}) == len({token for _, _, token in ordered}) == 8
    assert [groups[key] for _, key, _ in ordered] == [True, True, True, False] * 2
    with Session(engine()) as db:
        assert db.get(ReadCacheDispatch, 1).turn == 0
        for _, key, token in ordered:
            assert db.get(ReadCache, key).lease_token == token


def test_background_failure_preserves_last_authorized_result_then_recovers(client, monkeypatch):
    seed(client, monkeypatch)
    first = client.get(URL)
    with Session(engine()) as db, db.begin():
        db.execute(text("SELECT bump_read_dependency('family:tushare_market_rt')"))
    original = read_cache.compute
    monkeypatch.setattr(read_cache, 'compute', lambda *args: (_ for _ in ()).throw(RuntimeError('private fixture detail')))
    with pytest.raises(RuntimeError):
        worker()
    stale = client.get(URL)
    assert stale.json()['meta']['read_cache']['state'] == 'stale'
    assert stale.json()['data'] == first.json()['data']
    with Session(engine()) as db, db.begin():
        entry = db.scalar(select(ReadCache))
        assert entry.last_error == 'RuntimeError'
        entry.refresh_after = datetime.now(UTC)-timedelta(seconds=1)
    monkeypatch.setattr(read_cache, 'compute', original)
    assert worker()
    assert client.get(URL).json()['meta']['read_cache']['state'] == 'fresh'


def test_unchanged_expiry_metadata_validates_without_rebuilding(client, monkeypatch):
    seed(client, monkeypatch)
    first = client.get(URL)
    with Session(engine()) as db, db.begin():
        db.execute(update(ReadCache).values(refresh_after=datetime.now(UTC)-timedelta(seconds=1)))
    monkeypatch.setattr(read_cache, 'compute', lambda *args: pytest.fail('unchanged expiry recomputed'))
    assert worker()
    assert client.get(URL, headers={'If-None-Match': first.headers['etag']}).status_code == 304


@pytest.mark.parametrize('path', ['/api/v1/review-data/day', '/api/v1/review-data/series',
    '/api/v1/review-data/lhb?trade_day=2026-10-23', '/api/v1/review-data/stocks?trade_day=2026-10-23&codes=000100.SZ',
    '/api/v1/review-data/theme-history?trade_day=2026-10-23&sector_codes=BK1184.DC',
    '/api/v1/news/items', '/api/v1/news/editions'])
def test_whitelist_typed_projections_support_etag(path, client, monkeypatch):
    login(client)
    first = client.get(path)
    assert first.status_code == 200, first.text
    assert first.json()['meta']['read_cache']['state'] == 'fresh'
    monkeypatch.setattr(read_cache, 'compute', lambda *args: pytest.fail('conditional read recomputed'))
    assert client.get(path, headers={'If-None-Match': first.headers['etag']}).status_code == 304


def test_cache_can_be_disabled_without_using_old_results(client, monkeypatch):
    seed(client, monkeypatch)
    first = client.get(URL)
    monkeypatch.setattr(settings(), 'read_cache_enabled', False)
    assert client.get('/api/v1/session').json()['data']['cache_scope'] is None
    uncached = client.get(URL, headers={'If-None-Match': first.headers['etag']})
    assert uncached.status_code == 200
    assert 'etag' not in uncached.headers and uncached.headers['cache-control'] == 'no-store'


def test_cache_tables_are_rebuildable_and_retention_does_not_touch_evidence(client, monkeypatch):
    seed(client, monkeypatch)
    client.get(URL)
    from app.evidence.models import EvidenceVersion
    with Session(engine()) as db, db.begin():
        count = db.scalar(select(func.count()).select_from(EvidenceVersion))
        db.execute(update(ReadCache).values(accessed_at=datetime.now(UTC)-timedelta(days=8)))
    assert not worker()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ReadCache)) == 0
        assert db.scalar(select(func.count()).select_from(EvidenceVersion)) == count
    assert client.get(URL).json()['data']['stocks'][0]['price'] == 10


def test_bulk_ingestion_bumps_each_dependency_once_per_transaction(client, monkeypatch):
    seed(client, monkeypatch)
    client.get(URL)
    with Session(engine()) as db:
        before = db.get(ReadDependency, 'catalog').revision
    with engine().begin() as db:
        db.execute(text("""INSERT INTO instruments(id,exchange,symbol,name,asset_type)
            SELECT gen_random_uuid(),'SSE',lpad(value::text,6,'0'),'Synthetic bulk','stock'
            FROM generate_series(1000,1999) value"""))
    with Session(engine()) as db:
        assert db.get(ReadDependency, 'catalog').revision == before + 1
        assert db.scalar(select(ReadCache)).dirty
    with engine().connect() as db:
        db.execute(text("""UPDATE instruments SET name='Synthetic rollback'
            WHERE symbol>='001000' AND symbol<='001999'"""))
        db.rollback()
    with Session(engine()) as db:
        assert db.get(ReadDependency, 'catalog').revision == before + 1


def test_completed_historical_read_does_not_expire_at_current_market_phase(client, monkeypatch):
    seed(client, monkeypatch)
    client.get(URL)
    with Session(engine()) as db:
        entry = db.scalar(select(ReadCache))
        early = datetime(2026, 10, 26, 1, 14, tzinfo=UTC)
        late = datetime(2026, 10, 26, 7, 6, tzinfo=UTC)
        assert read_cache.desired_revision(db, entry, early) == read_cache.desired_revision(db, entry, late)
        entry.params = {**entry.params, 'day': '2026-10-26'}
        assert read_cache.desired_revision(db, entry, early) != read_cache.desired_revision(db, entry, late)


def test_watchlist_and_news_user_changes_do_not_discard_market_details(client, monkeypatch):
    seed(client, monkeypatch)
    first = client.get(URL)
    from app.market.models import Watchlist
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(User))
        db.add(Watchlist(owner_id=owner.id, instrument_ids=[], updated_at=datetime.now(UTC)))
        db.execute(text("SELECT bump_read_dependency(:key)"), {'key': f'news-user:{owner.id}'})
    monkeypatch.setattr(read_cache, 'compute', lambda *args: pytest.fail('unrelated state rebuilt market details'))
    assert client.get(URL, headers={'If-None-Match': first.headers['etag']}).status_code == 304


def test_corrupt_derived_result_is_rebuilt_instead_of_breaking_the_page(client, monkeypatch):
    seed(client, monkeypatch)
    client.get(URL)
    with Session(engine()) as db, db.begin():
        db.scalar(select(ReadCache)).payload = {'invalid': 'disposable corruption fixture'}
    repaired = client.get(URL)
    assert repaired.status_code == 200 and repaired.json()['data']['stocks'][0]['price'] == 10


def test_application_database_role_can_read_scope_and_refresh_cached_projection(client, monkeypatch):
    seed(client, monkeypatch)
    expected = client.get(URL)
    app_url = os.getenv('JL_TEST_APP_URL')
    assert app_url, 'Disposable app-role URL required'
    application = create_engine(app_url)
    try:
        with Session(application, expire_on_commit=False) as db:
            owner = db.scalar(select(User))
            assert read_cache.cache_scope(db, owner) == expected.headers['x-cache-scope']
            entry = db.scalar(select(ReadCache))
            token = read_cache.claim(db, entry.key)
            assert token and read_cache.build(db, entry.key, token)
    finally:
        application.dispose()


def test_authorized_raw_bottom_tables_reuse_payloads_across_detail_batches(client, monkeypatch):
    seed(client, monkeypatch)
    from app import read_cache_payloads
    from app.evidence.models import EvidenceVersion
    from app.market.terminal_read import SavedTables
    read_cache_payloads.clear()
    with Session(engine()) as db:
        reader = SavedTables(db, AT + timedelta(hours=1))
        meta = reader.market(AT.date())[0]
        payload = reader.evidence(meta).payload
    original = Session.get
    def no_raw_transfer(self, entity, *args, **kwargs):
        if entity is EvidenceVersion:
            pytest.fail('second detail batch reread immutable raw payload')
        return original(self, entity, *args, **kwargs)
    monkeypatch.setattr(Session, 'get', no_raw_transfer)
    with Session(engine()) as db:
        reader = SavedTables(db, AT + timedelta(hours=1))
        assert reader.evidence(reader.market(AT.date())[0]).payload is payload
    with Session(engine()) as db, db.begin():
        provider = original(db, Provider, source_id('tushare_market_rt'))
        provider.rights = {**provider.rights, 'display': 'denied'}
    with Session(engine()) as db:
        assert SavedTables(db, AT + timedelta(hours=1)).market(AT.date()) == []


def test_hot_conditional_read_does_not_wait_for_an_ingestion_transaction(client, monkeypatch):
    seed(client, monkeypatch)
    first = client.get(URL)
    with Session(engine()) as db, db.begin():
        db.execute(update(ReadCache).values(accessed_at=datetime.now(UTC)-timedelta(minutes=2),
            refresh_after=datetime.now(UTC)-timedelta(seconds=1)))
    with engine().connect() as ingestion, ThreadPoolExecutor(max_workers=1) as executor:
        transaction = ingestion.begin()
        ingestion.execute(text("SELECT bump_read_dependency('family:tushare_market_rt')"))
        future = executor.submit(client.get, URL, headers={'If-None-Match': first.headers['etag']})
        try:
            response = future.result(timeout=1)
            assert response.status_code == 304
        finally:
            # Release even on regression so the test cannot leave a blocked
            # request or a transaction holding the shared disposable database.
            transaction.rollback()
