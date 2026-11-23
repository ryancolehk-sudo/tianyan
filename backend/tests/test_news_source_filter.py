"""Connected source filtering before the bounded window, using synthetic DB rows."""
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.schemas import NewsPayload
from app.market.collect import store_news_batch
from app.market.models import SourceSettings
from app.models import Provider
from test_connected_data import source_config
from test_data_sources import configure, login


def configured(client, headers, family):
    return uuid.UUID(configure(client, headers, source_config(family), key='news-config-' + family).json()['data']['provider']['id'])


def saved(db, source, key, title, received, published=None):
    provider = db.get(Provider, source)
    payload = NewsPayload(title=title, summary=None, source_url='https://example.com/' + key,
                          category='公告' if provider.family == 'cninfo_announcements' else '海外')
    return store_news_batch(db, provider, db.get(SourceSettings, source), SimpleNamespace(records=[(key, payload, published)]), received)


def test_older_source_can_be_read_before_newer_announcements_fill_the_window(client):
    headers = login(client)
    sources = {family: configured(client, headers, family) for family in ['ecb_news', 'fed_public', 'cninfo_announcements']}
    now = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        saved(db, sources['ecb_news'], 'synthetic-ecb', '合成较早ECB条目', now - timedelta(days=2), datetime(2020, 1, 2, tzinfo=UTC))
        saved(db, sources['fed_public'], 'synthetic-fed', '合成无发布时间Fed条目', now - timedelta(days=1))
        for index in range(101):
            saved(db, sources['cninfo_announcements'], 'synthetic-notice-' + str(index), '合成较新公告' + str(index), now - timedelta(seconds=101 - index))
    all_items = client.get('/api/v1/market-news').json()['data']['items']
    assert len(all_items) == 100 and {item['family'] for item in all_items} == {'cninfo_announcements'}
    ecb = client.get('/api/v1/market-news', params={'family': 'ecb_news'}).json()['data']['items']
    assert len(ecb) == 1 and ecb[0]['payload']['title'] == '合成较早ECB条目'
    assert ecb[0]['published_at'].startswith('2020-01-02T00:00')
    assert ecb[0]['received_at'] != ecb[0]['published_at']
    fed = client.get('/api/v1/market-news', params={'family': 'fed_public'}).json()['data']['items']
    assert len(fed) == 1 and fed[0]['published_at'] is None
    notices = client.get('/api/v1/market-news', params={'family': 'cninfo_announcements'}).json()['data']['items']
    assert len(notices) == 100


def test_filtered_source_keeps_latest_revision_and_rechecks_cache_permission(client):
    headers = login(client)
    source = configured(client, headers, 'ecb_news')
    now = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        saved(db, source, 'synthetic-same-item', '合成旧标题', now - timedelta(seconds=1))
        saved(db, source, 'synthetic-same-item', '合成更正标题', now)
    items = client.get('/api/v1/market-news?family=ecb_news').json()['data']['items']
    assert len(items) == 1 and items[0]['payload']['title'] == '合成更正标题'
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source)
        provider.rights = {**provider.rights, 'cache': 'denied'}
    assert client.get('/api/v1/market-news?family=ecb_news').json()['data']['items'] == []


def test_source_filter_requires_login_and_rejects_unsupported_family(client):
    assert client.get('/api/v1/market-news?family=ecb_news').status_code == 401
    login(client)
    assert client.get('/api/v1/market-news?family=not_a_news_source').status_code == 422
