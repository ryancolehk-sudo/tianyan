"""Synthetic publisher RSS and disposable real PostgreSQL; no live feed in CI."""
import uuid
from datetime import UTC, datetime
from html import escape

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collect import store_news_batch
from app.market.models import SourceSettings
from app.market.protocol import Response, SourceFailure
from app.market.public import parse_public
from app.models import Provider
from app.worker import claim, process
from test_connected_data import source_config
from test_data_sources import configure, login

FAMILY = 'hk_gov_finance'
NOW = datetime(2020, 1, 4, tzinfo=UTC)
LINK = 'https://www.news.gov.hk/tc/synthetic-article.shtml'


def item(link=LINK, publication='Thu, 02 Jan 2020 00:00:00 +0800', title='合成香港官方財經條目'):
    date = '' if publication is None else '<pubDate>' + escape(publication) + '</pubDate>'
    return '<item><title>' + escape(title) + '</title><link>' + escape(link) + '</link>' + date + '<description>合成正文和图片描述不得复制</description></item>'


def feed(items):
    return ('<rss version="2.0"><channel><title>合成隔离RSS</title>' + items + '</channel></rss>').encode()


def test_midnight_encoded_day_never_becomes_a_release_clock_or_copied_body():
    batch = parse_public(FAMILY, feed(item()), NOW)
    assert batch.expected == 1 and batch.rejected == 0 and len(batch.records) == 1
    key, payload, published = batch.records[0]
    assert len(key) == 64 and published is None and str(payload.published_day) == '2020-01-02'
    assert payload.title == '合成香港官方財經條目' and payload.source_url == LINK
    assert payload.source_name == '香港政府新聞網' and payload.category == '财经'
    assert payload.summary is None


def test_absent_publication_stays_unknown_instead_of_using_receipt():
    _, payload, published = parse_public(FAMILY, feed(item(publication=None)), NOW).records[0]
    assert published is None and payload.published_day is None


def test_clock_syntax_cannot_promote_this_source_to_precise_publication_time():
    _, payload, published = parse_public(FAMILY, feed(item(publication='Thu, 02 Jan 2020 13:05:00 +0800')), NOW).records[0]
    assert published is None and str(payload.published_day) == '2020-01-02'


def test_explicit_date_field_stays_date_only():
    _, payload, published = parse_public(FAMILY, feed(item(publication='2020-01-02')), NOW).records[0]
    assert published is None and str(payload.published_day) == '2020-01-02'


@pytest.mark.parametrize('link', [
    'http://www.news.gov.hk/tc/item.shtml', 'https://example.com/item',
    'https://www.news.gov.hk.evil.example/item', 'https://viewer@www.news.gov.hk/item',
    'https://www.news.gov.hk:444/item', LINK + '#fragment', LINK + '\\hidden',
])
def test_links_stay_on_the_original_https_publisher_without_rewriting(link):
    batch = parse_public(FAMILY, feed(item(link=link)), NOW)
    assert batch.expected == 1 and batch.rejected == 1 and not batch.records


@pytest.mark.parametrize('value', ['not-a-date', 'Thu, 02 Jan 2020 13:05:00', '2021-01-02'])
def test_bad_or_future_publication_does_not_fall_back_to_receipt(value):
    batch = parse_public(FAMILY, feed(item(publication=value)), NOW)
    assert batch.expected == 1 and batch.rejected == 1 and not batch.records


@pytest.mark.parametrize('body', [
    b'<!DOCTYPE rss [<!ENTITY leak "private">]><rss version="2.0"><channel/></rss>',
    '<rss version="2.0"><channel/></rss>'.encode('utf-16'),
    feed('<item><title>合成</title><link>' + LINK + '</link><item/></item>'),
    feed(''.join(item(link=LINK + '?n=' + str(index)) for index in range(101))),
])
def test_ambiguous_xml_and_over_budget_envelopes_pause_the_source(body):
    with pytest.raises(SourceFailure, match='schema_changed'):
        parse_public(FAMILY, body, NOW)


def test_duplicate_urls_and_duplicate_date_fields_are_reported_as_rejected():
    bad_date = item(link=LINK + '?bad').replace('</pubDate>', '</pubDate><pubDate>2020-01-03</pubDate>')
    batch = parse_public(FAMILY, feed(item() + item(title='合成重复标题') + bad_date), NOW)
    assert batch.expected == 3 and batch.rejected == 2 and len(batch.records) == 1


def test_hk_source_configuration_enforces_interval_scope_and_permissions(client):
    headers = login(client)
    body = source_config(FAMILY)
    for changes in [
        {'interval_seconds': 899},
        {'symbols': [{'exchange': 'SSE', 'symbol': '000001', 'asset_type': 'index'}]},
        {'entitlement_status': 'unknown'},
    ]:
        response = client.post('/api/v1/data-sources', json={**body, **changes},
            headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
        assert response.status_code == 422
    data = configure(client, headers, body).json()['data']
    assert data['family'] == FAMILY and data['provider']['capabilities'] == ['news.public']


def test_worker_date_order_revision_and_current_permission_use_shared_evidence(client, monkeypatch):
    monkeypatch.setenv('JL_DATA_COLLECTION_ENABLED', 'true')
    settings.cache_clear()
    try:
        headers = login(client)
        ident = configure(client, headers, source_config(FAMILY)).json()['data']['provider']['id']
        calls = []
        def fetch(family):
            calls.append(family)
            # A non-midnight field still has no verified second-level semantics.
            return Response(feed(item(publication='Thu, 02 Jan 2020 13:05:00 +0800')), datetime.now(UTC), 1, 200)
        monkeypatch.setattr('app.market.collect.fetch_public', fetch)
        queued = client.post('/api/v1/data-sources/' + ident + '/refresh', json={},
            headers={**headers, 'Idempotency-Key': 'hk-synthetic-collect'})
        assert queued.status_code == 202
        job = claim()
        assert job and str(job.id) == queued.json()['data']['job_id']
        process(job)
        assert calls == [FAMILY]
        rows = client.get('/api/v1/market-news?family=' + FAMILY).json()['data']['items']
        assert len(rows) == 1 and rows[0]['published_at'] is None
        assert rows[0]['payload']['published_day'] == '2020-01-02'
        received = datetime.now(UTC)
        with Session(engine()) as db, db.begin():
            provider = db.get(Provider, uuid.UUID(ident))
            config = db.get(SourceSettings, provider.id)
            stored = db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id == provider.id))
            assert stored.published_at is None and stored.event_at is None
            assert stored.historical_eligible is False
            assert stored.payload['published_day'] == '2020-01-02'
            # An older source day arriving later must not appear newer.
            older = feed(item(link=LINK + '?older', publication='Wed, 01 Jan 2020 00:00:00 +0800'))
            store_news_batch(db, provider, config, parse_public(FAMILY, older, received), received)
        rows = client.get('/api/v1/market-news?family=' + FAMILY).json()['data']['items']
        assert [row['payload']['published_day'] for row in rows] == ['2020-01-02', '2020-01-01']
        original_hash = rows[0]['content_hash']
        with Session(engine()) as db, db.begin():
            provider = db.get(Provider, uuid.UUID(ident))
            config = db.get(SourceSettings, provider.id)
            correction = feed(item(publication='Fri, 03 Jan 2020 00:00:00 +0800'))
            added, repeated = store_news_batch(db, provider, config, parse_public(FAMILY, correction, received), received)
            assert added == 1 and repeated == 0
            saved = list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == provider.id)))
            assert len(saved) == 3 and original_hash in {row.content_hash for row in saved}
        rows = client.get('/api/v1/market-news?family=' + FAMILY).json()['data']['items']
        assert len(rows) == 2 and rows[0]['payload']['published_day'] == '2020-01-03'
        coverage = client.get('/api/v1/data-coverage').json()['data']['items']
        capability = next(row for row in coverage if row['id'] == 'D16')
        assert capability['state'] == 'partial' and FAMILY in capability['families'] and '香港' in capability['detail']
        with Session(engine()) as db, db.begin():
            provider = db.get(Provider, uuid.UUID(ident))
            provider.rights = {**provider.rights, 'cache': 'denied'}
        assert client.get('/api/v1/market-news?family=' + FAMILY).json()['data']['items'] == []
        with Session(engine()) as db:
            assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.source_id == uuid.UUID(ident))) == 3
    finally:
        settings.cache_clear()
