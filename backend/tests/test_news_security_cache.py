"""News identity projection versions do not invalidate unrelated market caches."""
import copy
from datetime import UTC, date, datetime, timedelta
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import read_cache
from app.config import settings
from app.db import engine
from app.models import User
from app.news_center.editions import at, create_daily
from app.read_cache_models import ReadCache
from test_data_sources import login
from test_news_center import source, started
from test_news_securities import archive, store_news


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings(), 'read_cache_enabled', True)
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: pytest.fail('News cache tests cannot use network'))


def seed(client, *, securities=None):
    origin = source(client, login(client), 'cninfo_announcements')
    day = date(2026, 10, 24)
    with Session(engine()) as db, db.begin():
        store_news(db, origin, 'cache-identity', at(day - timedelta(days=1), 17), securities=securities)
        edition = create_daily(db, started(db, day), day, at(day, 10))
        return origin, day, str(edition.id)


def rebuild(key):
    with Session(engine(), expire_on_commit=False) as db:
        token = read_cache.claim(db, key)
        assert token and read_cache.build(db, key, token)


@pytest.mark.parametrize('operation', ['news-items', 'news-edition'])
def test_legacy_without_security_fields_is_not_reused_as_a_fresh_new_projection(client, monkeypatch, operation):
    _, _, edition_id = seed(client, securities=[{'code': '000001', 'name': '合成证券甲'}])
    params = {'saved': False, 'unread': False, 'following': False, 'order': 'latest', 'limit': 50} if operation == 'news-items' else {'edition_id': edition_id}
    url = '/api/v1/news/items' if operation == 'news-items' else '/api/v1/news/editions/' + edition_id
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(User))
        scope = read_cache.cache_scope(db, owner)
        payload = read_cache.compute(db, owner.id, operation, params)
        for row in payload['data']['items']:
            row.pop('securities', None); row.pop('security_status', None)
        if payload['data'].get('focus'):
            payload['data']['focus'].pop('securities', None); payload['data']['focus'].pop('security_status', None)
        # Reproduce the pre-upgrade key, dependency set and timeless/fresh
        # manifest, rather than weakening the new reader under test.
        key = read_cache.fingerprint([scope, operation, params])
        moment = datetime.now(UTC)
        entry = ReadCache(key=key, owner_id=owner.id, scope=scope, operation=operation, params=params,
            dependency_keys=[value for value in read_cache.dependency_keys(operation, owner.id, params)
                if value != 'family:cninfo_announcements'], payload=payload, dirty=False,
            built_at=moment, validated_at=moment, refresh_after=moment + timedelta(days=1), accessed_at=moment)
        old_manifest = read_cache.revision_manifest(db, entry, moment)
        old_manifest['format'] = read_cache.FORMAT
        entry.revision = read_cache.fingerprint(old_manifest)
        db.add(entry)
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.headers['x-cache-scope'] == scope
    assert response.json()['meta']['read_cache']['state'] == 'fresh'
    assert response.json()['data']['items'][0]['securities'][0]['code'] == '000001'
    with Session(engine()) as db:
        entries = db.scalars(select(ReadCache).where(ReadCache.operation == operation)).all()
        assert len(entries) == 2
        upgraded = next(row for row in entries if row.key != key)
        assert 'family:cninfo_announcements' in upgraded.dependency_keys
        assert read_cache.cache_scope(db, db.scalar(select(User))) == scope
        assert read_cache.revision_manifest(db, upgraded)['format'] == read_cache.FORMAT + '-news-securities-1'
    monkeypatch.setattr(read_cache, 'compute', lambda *a, **k: pytest.fail('Fresh issuer projection was recomputed'))
    assert client.get(url).json()['data'] == response.json()['data']
    assert client.get(url, headers={'If-None-Match': response.headers['etag']}).status_code == 304


def test_announcement_reference_arrival_refreshes_feed_but_cannot_enrich_an_earlier_edition(client):
    origin, day, edition_id = seed(client)
    feed_url, edition_url = '/api/v1/news/items', '/api/v1/news/editions/' + edition_id
    feed, edition = client.get(feed_url), client.get(edition_url)
    assert feed.status_code == edition.status_code == 200
    assert feed.json()['data']['items'][0]['securities'] == edition.json()['data']['items'][0]['securities'] == []
    with Session(engine()) as db:
        entries = {entry.operation: entry.key for entry in db.scalars(select(ReadCache))}
        owner = db.scalar(select(User))
        scope = read_cache.cache_scope(db, owner)
    # This is a real reference insertion with the normal invalidation trigger.
    # Its receipt is after the immutable edition's 08:00 cutoff.
    with Session(engine()) as db, db.begin():
        archive(db, origin, 'cache-identity', at(day, 9), available=at(day, 9))
    with Session(engine()) as db:
        for key in entries.values():
            assert db.get(ReadCache, key).dirty
    for key in entries.values():
        rebuild(key)
    current, historical = client.get(feed_url), client.get(edition_url)
    assert current.json()['data']['items'][0]['securities'][0]['code'] == '000001'
    assert historical.json()['data']['items'][0]['securities'] == []
    assert current.headers['x-cache-scope'] == historical.headers['x-cache-scope'] == scope
    assert client.get(feed_url, headers={'If-None-Match': current.headers['etag']}).status_code == 304


@pytest.mark.parametrize('operation,params,format_suffix', [
    ('review-overview', {'lookback': 20, 'trade_day': '2026-10-23'}, '-postclose-2'),
    ('review-overview', {'lookback': 60, 'trade_day': '2026-10-23'}, '-postclose-2'),
    ('news-editions', {'period': 'day'}, ''),
    ('details', {'day': '2026-10-23', 'detail_codes': ['000001.SZ']}, '-quote-speed-1'),
])
def test_other_operations_keep_their_existing_key_format_and_scope(client, operation, params, format_suffix):
    seed(client)
    with Session(engine(), expire_on_commit=False) as db:
        owner = db.scalar(select(User))
        before = read_cache.cache_scope(db, owner)
        entry = read_cache.ensure_entry(db, owner, before, operation, copy.deepcopy(params))
        identity = [before, operation, params]
        calendar_format = ''
        if operation.startswith('review-') or operation == 'details':
            from app.market.calendar_selection import READ_VERSION
            calendar_format = '-' + READ_VERSION
            identity.append(READ_VERSION)
            assert entry.key != read_cache.fingerprint([before, operation, params])
        assert entry.key == read_cache.fingerprint(identity)
        assert read_cache.revision_manifest(db, entry)['format'] == read_cache.FORMAT + calendar_format + format_suffix
        assert read_cache.cache_scope(db, owner) == before
