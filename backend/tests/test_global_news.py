"""Offline publisher fixtures plus real PostgreSQL collection/permission checks."""
import hashlib
import gzip
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.market.collect import store_news_batch
from app.market.models import SourceSettings
from app.market.news_feeds import NEWS_FEEDS, NEWS_FAMILIES
from app.market.protocol import ALLOWED_HOSTS, MAX_BODY, SourceFailure, fetch_https
from app.market.public import URLS, parse_public
from app.market.registry import SOURCES
from app.models import Provider
from test_connected_data import source_config
from test_data_sources import configure, login

NOW = datetime(2026, 10, 21, 10, tzinfo=UTC)


def mock_http(monkeypatch, body, encoding):
    class Response:
        status = 200
        headers = {'Content-Encoding': encoding}

        def read(self, maximum):
            assert maximum == MAX_BODY + 1
            return body[:maximum]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def open_request(request, **kwargs):
        assert request.get_header('Accept-encoding') == 'identity'
        return Response()
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: SimpleNamespace(open=open_request))


@pytest.mark.parametrize('encoding', ['', 'identity', 'gzip', ' GZip '])
def test_transport_decodes_declared_gzip_without_changing_native_feed_bytes(monkeypatch, encoding):
    original = feed('un_economy', title='合成联合国经济条目')
    body = gzip.compress(original) if encoding.strip().lower() == 'gzip' else original
    mock_http(monkeypatch, body, encoding)
    response = fetch_https(URLS['un_economy'])
    assert response.body == original and response.status == 200
    assert parse_public('un_economy', response.body, NOW).records[0][1].title == '合成联合国经济条目'


@pytest.mark.parametrize('encoding,body,reason', [
    ('gzip', gzip.compress(b'x' * (MAX_BODY + 1)), 'response_too_large'),
    ('identity', b'x' * (MAX_BODY + 1), 'response_too_large'),
    ('gzip', b'not a gzip stream', 'unsafe_upstream_response'),
    ('gzip', gzip.compress(b'synthetic feed')[:-3], 'unsafe_upstream_response'),
    ('br', b'synthetic unsupported coding', 'unsafe_upstream_response'),
    ('gzip, gzip', gzip.compress(b'synthetic double coding'), 'unsafe_upstream_response'),
])
def test_compressed_transport_is_bounded_and_invalid_coding_is_explicit(monkeypatch, encoding, body, reason):
    mock_http(monkeypatch, body, encoding)
    with pytest.raises(SourceFailure, match=reason):
        fetch_https(URLS['un_economy'])


@pytest.mark.parametrize('body', [b'<!DOCTYPE rss><rss/>', '<rss/>'.encode('utf-16')])
def test_transport_decompression_keeps_xml_envelope_and_encoding_guards(monkeypatch, body):
    mock_http(monkeypatch, gzip.compress(body), 'gzip')
    response = fetch_https(URLS['un_economy'])
    with pytest.raises(SourceFailure, match='schema_changed'):
        parse_public('un_economy', response.body, NOW)


def feed(family, *, title='Synthetic original', link=None, date='Wed, 21 Oct 2026 09:00:00 +0800', extra='', summary='Synthetic feed summary'):
    host = NEWS_FEEDS[family].article_hosts[0]
    link = link if link is not None else 'https://' + host + '/synthetic/article'
    return (f'<rss version="2.0"><channel><item><title>{escape(title)}</title><link>{escape(link)}</link>'
            + (f'<pubDate>{escape(date)}</pubDate>' if date else '')
            + f'<description>{escape(summary)}</description>{extra}</item></channel></rss>').encode()


@pytest.mark.parametrize('family', NEWS_FEEDS)
def test_every_global_publisher_has_fixed_endpoint_identity_and_parses_native_rss(family):
    batch = parse_public(family, feed(family), NOW)
    assert batch.expected == 1 and batch.rejected == 0
    key, payload, published = batch.records[0]
    assert key == hashlib.sha256(payload.source_url.encode()).hexdigest()
    assert payload.title == 'Synthetic original' and payload.source_name == NEWS_FEEDS[family].publisher
    assert payload.category == NEWS_FEEDS[family].category
    assert published == datetime(2026, 10, 21, 1, tzinfo=UTC)
    assert SOURCES[family].kind == 'news' and URLS[family] == NEWS_FEEDS[family].url
    assert NEWS_FEEDS[family].url.split('/')[2] in ALLOWED_HOSTS
    assert SOURCES[family].minimum_interval >= 900


@pytest.mark.parametrize('link', ['https://example.com/not-publisher', 'https://127.0.0.1/private',
    'https://www.cnbc.com@127.0.0.1/private', 'https://www.cnbc.com.evil.test/item', 'javascript:alert(1)',
    'http://www.cnbc.com/item', 'https://www.cnbc.com:444/item', 'https://user@www.cnbc.com/item',
    'https://www.cnbc.com/item#fragment', 'https://www.cnbc.com\\@example.com/item'])
def test_global_feed_rejects_out_of_scope_urls_without_following_them(link):
    batch = parse_public('cnbc_finance', feed('cnbc_finance', link=link), NOW)
    assert batch.rejected == 1 and not batch.records


def test_dates_html_and_rdf_preserve_publisher_precision_without_inventing_times():
    missing = parse_public('bbc_business', feed('bbc_business', date=''), NOW).records[0]
    assert missing[2] is None and missing[1].published_day is None
    day = parse_public('bbc_business', feed('bbc_business', date='2026-10-20'), NOW).records[0]
    assert day[2] is None and str(day[1].published_day) == '2026-10-20'
    midnight = parse_public('bbc_business', feed('bbc_business', date='Wed, 21 Oct 2026 00:00:00 +0000'), NOW).records[0]
    assert midnight[2] == datetime(2026, 10, 21, tzinfo=UTC)  # Only date-encoded publishers lose the clock.
    rdf = b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel/><item><title>Synthetic policy</title><link>https://www.bis.org/synthetic</link><dc:title>Same optional metadata</dc:title><dc:date>2026-10-20T00:00:00Z</dc:date><description>Policy note</description></item></rdf:RDF>'''
    record = parse_public('bis_news', rdf, NOW).records[0]
    assert record[2] is None and str(record[1].published_day) == '2026-10-20'
    html = parse_public('cna_business', feed('cna_business', title='<script>synthetic</script>', summary='<p>Original feed text</p>'), NOW).records[0][1]
    assert html.title == 'synthetic' and html.summary == 'Original feed text'
    assert parse_public('cna_business', feed('cna_business', summary='x' * 2001), NOW).records[0][1].summary is None


@pytest.mark.parametrize('stamp', ['Wed, 21 Oct 2026 12:00:00 +0000', '2026-10-22', '2026-10-21T09:00:00', 'not a date'])
def test_future_invalid_or_unzoned_publication_is_rejected(stamp):
    result = parse_public('bbc_business', feed('bbc_business', date=stamp), NOW)
    assert result.rejected == 1 and not result.records


@pytest.mark.parametrize('body', [b'<!DOCTYPE rss><rss/>', b'<!ENTITY attack "x"><rss/>', b'<rss/>', b'<html>not rss</html>',
    b'<rss version="2.0"><channel/><channel/></rss>', b'\xff\xfe<\0r\0s\0s\0/>', b'x' * 2000001])
def test_unsafe_or_changed_global_feed_envelope_is_not_a_success(body):
    with pytest.raises(SourceFailure, match='schema_changed'):
        parse_public('bbc_business', body, NOW)


def test_ambiguous_fields_duplicates_and_oversized_scope_are_rejected():
    for extra in ['<title>ambiguous</title>', '<pubDate>Wed, 21 Oct 2026 09:01:00 +0800</pubDate>', '<link>https://www.bbc.com/other</link>']:
        result = parse_public('bbc_business', feed('bbc_business', extra=extra), NOW)
        assert result.rejected == 1 and not result.records
    raw = feed('bbc_business')
    item = raw.split(b'<item>')[1].split(b'</item>')[0]
    duplicates = b'<rss version="2.0"><channel>' + (b'<item>' + item + b'</item>') * 2 + b'</channel></rss>'
    result = parse_public('bbc_business', duplicates, NOW)
    assert len(result.records) == 1 and result.rejected == 1
    with pytest.raises(SourceFailure):
        parse_public('bbc_business', b'<rss version="2.0"><channel>' + b'<item/>' * 101 + b'</channel></rss>', NOW)


def test_global_source_metadata_and_native_collection_keep_versions_permissions_and_cached_reads(client, monkeypatch):
    headers = login(client)
    sources = {}
    for family in ['bbc_business', 'bbc_world', 'cna_business', 'cnbc_finance']:
        response = configure(client, headers, source_config(family), key=uuid.uuid4().hex)
        assert response.status_code == 201
        sources[family] = uuid.UUID(response.json()['data']['provider']['id'])
    index = client.get('/api/v1/data-sources').json()['data']
    bbc = [row for row in index['available_adapters'] if row['family'] in ['bbc_business', 'bbc_world']]
    assert len(bbc) == 2 and {row['news']['publisher'] for row in bbc} == {'BBC News'}
    assert {row['family'] for row in index['available_adapters'] if row['kind'] == 'news'} == set(NEWS_FAMILIES) | {'review_cls_news'}
    assert {row['family'] for row in index['available_adapters'] if row['family'] in NEWS_FAMILIES} == set(NEWS_FAMILIES)
    assert {'chinanews_finance', 'cninfo_announcements'} <= set(NEWS_FAMILIES)
    received = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        for family, source in sources.items():
            provider = db.get(Provider, source)
            batch = parse_public(family, feed(family, date='2020-01-02'), received)
            assert store_news_batch(db, provider, db.get(SourceSettings, source), batch, received) == (1, 0)
            assert store_news_batch(db, provider, db.get(SourceSettings, source), batch, received) == (0, 1)
        source = sources['bbc_business']
        provider = db.get(Provider, source)
        corrected = parse_public('bbc_business', feed('bbc_business', title='Synthetic correction', date='2020-01-02'), received)
        assert store_news_batch(db, provider, db.get(SourceSettings, source), corrected, received + timedelta(seconds=1)) == (1, 0)
    def no_fetch(*args, **kwargs):
        pytest.fail('Reading cached publisher news must never contact the publisher')
    monkeypatch.setattr('app.market.public.fetch_https', no_fetch)
    rows = client.get('/api/v1/market-news?family=bbc_business').json()['data']['items']
    assert len(rows) == 1 and rows[0]['payload']['title'] == 'Synthetic correction'
    assert rows[0]['published_at'] is None and rows[0]['payload']['published_day'] == '2020-01-02'
    assert rows[0]['received_at'] != rows[0]['payload']['published_day']
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, sources['bbc_business'])
        provider.rights = {**provider.rights, 'cache': 'denied'}
    assert not client.get('/api/v1/market-news?family=bbc_business').json()['data']['items']
    assert client.get('/api/v1/market-news?family=not_a_news_source').status_code == 422
    forbidden = source_config('bbc_business')
    forbidden['symbols'] = [{'exchange': 'SSE', 'symbol': '000001', 'asset_type': 'index'}]
    assert client.post('/api/v1/data-sources', json=forbidden, headers={**headers, 'Idempotency-Key': uuid.uuid4().hex}).status_code == 422
    fast = source_config('bbc_business'); fast['interval_seconds'] = 60
    assert client.post('/api/v1/data-sources', json=fast, headers={**headers, 'Idempotency-Key': uuid.uuid4().hex}).status_code == 422
