"""Regressions from the independent acceptance; all upstream bodies are synthetic."""
import hashlib
import json
from datetime import UTC, datetime

import pytest

from app.market import auction_source as source
from app.market.budget import AUCTION_INTERVAL_SECONDS
from app.market.protocol import Response, SourceFailure
from test_auction_source import head, row, single_day, RECEIVED
from test_auction_tables import (CURRENT, HISTORY, SUMMARY, STOCKS, batches, configuration,
    configure_family, current_body, dataset, history_body, job_row, run, run_failing, runs)
from test_data_sources import login


@pytest.fixture
def no_pacing(monkeypatch):
    monkeypatch.setattr('app.market.budget.AUCTION_INTERVAL_SECONDS', 0)


@pytest.mark.parametrize('trading,origin', [(0, source.BASE), (1, 'https://ds.duanxianxia.com')])
def test_routing_and_pinned_decoder_are_checked_before_current_table(monkeypatch, trading, origin):
    calls = []
    script = b'public decoder synthetic fixture'
    monkeypatch.setattr(source, 'CRYPTO_SHA256', hashlib.sha256(script).hexdigest())
    config = {'istrade': trading, 'base_url': [source.BASE], 'data_url': 'https://ds.duanxianxia.com'}

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        body = json.dumps(config).encode() if url == source.DATASOURCE_URL else script if url == source.CRYPTO_URL else current_body()
        return Response(body, RECEIVED, 10, 200)
    monkeypatch.setattr(source, 'fetch_https', fetch)
    response = source.fetch_auction('current')
    assert len(calls) == response.request_count == 3
    assert [entry[0] for entry in calls[:2]] == [source.DATASOURCE_URL, source.CRYPTO_URL]
    assert calls[2][0].startswith(origin + source.CURRENT_PATH + '?_=')
    assert all(kwargs == {'timeout': 15} for _, kwargs in calls)
    assert response.elapsed_ms == 30 and response.body == current_body()


@pytest.mark.parametrize('bad', ['https://unverified.example', 'http://duanxianxia.com',
    'https://duanxianxia.com@unverified.example', 'https://ds.duanxianxia.com/path', ['nested']])
def test_unapproved_data_origins_never_trigger_a_second_request(monkeypatch, bad):
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        return Response(json.dumps({'istrade': 1, 'base_url': [source.BASE], 'data_url': bad}).encode(), RECEIVED, 1, 200)
    monkeypatch.setattr(source, 'fetch_https', fetch)
    with pytest.raises(SourceFailure) as error:
        source.fetch_auction()
    assert error.value.reason == 'endpoint_not_allowed' and error.value.request_count == 1
    assert calls == [source.DATASOURCE_URL]


def test_changed_decoder_stops_before_table_and_counts_two_requests(monkeypatch):
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        body = json.dumps({'istrade': 0, 'base_url': [source.BASE], 'data_url': source.BASE}).encode() if len(calls) == 1 else b'changed script'
        return Response(body, RECEIVED, 1, 200)
    monkeypatch.setattr(source, 'fetch_https', fetch)
    with pytest.raises(SourceFailure) as error:
        source.fetch_auction()
    assert error.value.reason == 'decoder_changed' and error.value.request_count == 2
    assert len(calls) == 2


@pytest.mark.parametrize('reason', ['timeout', 'permission_missing'])
def test_fetch_failure_is_not_retried_and_counts_the_attempt(monkeypatch, reason):
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        raise SourceFailure(reason)
    monkeypatch.setattr(source, 'fetch_https', fetch)
    with pytest.raises(SourceFailure) as error:
        source.fetch_auction('history')
    assert error.value.reason == reason and error.value.request_count == 1
    assert len(calls) == 1


def test_history_uses_one_empty_post_without_decoder_execution(monkeypatch):
    calls = []
    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return Response(history_body(), RECEIVED, 7, 200)
    monkeypatch.setattr(source, 'fetch_https', fetch)
    result = source.fetch_auction('history')
    assert calls == [(source.BASE + source.HISTORY_PATH, {'timeout': 15, 'data': b''})]
    assert result.request_count == 1


def test_public_text_and_explicit_yuan_are_preserved_without_html():
    batch = source.parse_auction(single_day([row('600001', name='样本&amp;<em>公司</em><script>hidden()</script>',
        tags=('机器人&amp;算力',), cells=('12元', '0', '-'))]), RECEIVED, 'history')
    summary, stock = batch.records[0][1].rows[0], batch.records[1][1].rows[0]
    assert stock['name'] == '样本&公司' and stock['tags_raw'] == '机器人&算力'
    assert stock['fd_0915_raw'] == '12元' and stock['fd_0915_cny'] == '12'
    assert stock['fd_0920_cny'] == '0' and stock['fd_0925_cny'] is None
    assert '9:15' in summary['source_header_text'] and '<' not in summary['source_header_text']


def test_raw_hash_covers_summary_percent_and_order_changes():
    original = single_day([row('600001', pct='1.20%')])
    for changed in [original.replace(b'1.20%', b'1.21%'), original.replace(b'4.2', b'4.3')]:
        a = source.parse_auction(original, RECEIVED, 'history')
        b = source.parse_auction(changed, RECEIVED, 'history')
        assert a.batch_hash != b.batch_hash


def test_shared_window_and_defaults_match_the_user_plan():
    assert AUCTION_INTERVAL_SECONDS >= 65
    assert configuration(CURRENT)['interval_seconds'] == 65
    assert configuration(HISTORY)['interval_seconds'] == 3600


def test_returning_to_a_previous_table_is_the_latest_observation(client, monkeypatch, no_pacing):
    headers = login(client)
    configure_family(client, headers, HISTORY)
    a = history_body()
    b = a.replace(b'560', b'660')
    assert a != b
    run(monkeypatch, HISTORY, a)
    run(monkeypatch, HISTORY, b)
    run(monkeypatch, HISTORY, a)
    stored = batches(HISTORY)
    assert len(stored) == 12
    latest = client.get('/api/v1/reference-datasets', params={'dataset': STOCKS, 'family': HISTORY}, headers=headers).json()['data']['items']
    assert len(latest) == 2
    assert {item['payload']['rows'][0]['batch_hash'] for item in latest} == {hashlib.sha256(a).hexdigest()}
    assert all(item['published_at'] is None for item in latest)
    run(monkeypatch, HISTORY, a)
    assert len(batches(HISTORY)) == 12, 'only consecutive equal responses deduplicate'


def test_latest_three_dates_and_exact_day_survive_over_one_hundred_versions(client, monkeypatch, no_pacing):
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.models import Provider
    from app.market.models import SourceSettings
    from app.market.collect import store_auction_batches
    from app.market.service import source_id
    headers = login(client)
    configure_family(client, headers, HISTORY)
    with Session(engine()) as db, db.begin():
        provider, config = db.get(Provider, source_id(HISTORY)), db.get(SourceSettings, source_id(HISTORY))
        for index in range(105):
            body = single_day([row('600001', cells=(f'{index}元', '0', '-'))])
            store_auction_batches(db, provider, config, [source.parse_auction(body, RECEIVED, 'history')], RECEIVED)
        for day in ['2026-10-16', '2026-10-19', '2026-10-20']:
            body = single_day([row('600001')], day=day)
            store_auction_batches(db, provider, config, [source.parse_auction(body, RECEIVED, 'history')], RECEIVED)
    def read(**params):
        return client.get('/api/v1/reference-datasets', params={'dataset': SUMMARY, 'family': HISTORY, **params}, headers=headers).json()['data']['items']
    assert [item['payload']['observed_day'] for item in read()] == ['2026-10-21', '2026-10-20', '2026-10-19']
    assert [item['payload']['observed_day'] for item in read(day='2026-10-16')] == ['2026-10-16']
    assert read(day='2026-10-15') == []


def test_decoder_failure_pauses_collection_and_keeps_cache(client, monkeypatch, no_pacing):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    run(monkeypatch, CURRENT, current_body())
    failed = run_failing(monkeypatch, CURRENT, 'decoder_changed')
    assert job_row(failed.id).state == 'failed'
    assert len(batches(CURRENT)) == 2 and len(dataset(client, headers, SUMMARY)) == 1
    index = client.get('/api/v1/data-sources', headers=headers).json()['data']['items']
    assert next(item for item in index if item['family'] == CURRENT)['paused_reason'] == 'decoder_changed'
