"""Public metadata is initialized once; every table remains a real request."""
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

import pytest

from app.market import auction_source as source
from app.market.protocol import Response, SHANGHAI, SourceFailure


@dataclass
class Clock:
    wall: datetime
    tick: float = 1000


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    clock = Clock(datetime(2026, 10, 26, 9, 15, tzinfo=SHANGHAI))

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.wall.astimezone(tz) if tz else clock.wall

    monkeypatch.setattr(source, 'datetime', FrozenDatetime)
    monkeypatch.setattr(source.time, 'monotonic', lambda: clock.tick)
    identifiers = iter(range(1_000_000, 2_000_000))
    monkeypatch.setattr(source.time, 'time_ns', lambda: next(identifiers) * 1_000_000)
    monkeypatch.setattr(source, '_METADATA_CACHE', None)
    yield clock
    source._METADATA_CACHE = None


@pytest.fixture
def upstream(monkeypatch, clock):
    script = b'bounded synthetic decoder identity; never executed'
    monkeypatch.setattr(source, 'CRYPTO_SHA256', hashlib.sha256(script).hexdigest())
    calls, table_outcomes = [], []
    config = {'istrade': 1, 'base_url': [source.BASE], 'data_url': 'https://ds.duanxianxia.com'}
    state = {'script': script, 'config': config}

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if url == source.DATASOURCE_URL:
            body = json.dumps(state['config']).encode()
        elif url == source.CRYPTO_URL:
            body = state['script']
        else:
            outcome = table_outcomes.pop(0) if table_outcomes else b'uncached synthetic table'
            if isinstance(outcome, Exception):
                raise outcome
            body = outcome
        return Response(body, clock.wall, 7, 200)

    monkeypatch.setattr(source, 'fetch_https', fetch)
    return calls, table_outcomes, state


def test_warm_poll_fetches_only_table_and_retains_its_actual_receipt(clock, upstream):
    calls, _, _ = upstream
    cold = source.fetch_auction()
    assert cold.request_count == 3 and cold.elapsed_ms == 21
    first_url = calls[-1][0]
    calls.clear()
    clock.wall = datetime(2026, 10, 26, 9, 15, 3, tzinfo=SHANGHAI)
    clock.tick += 3
    warm = source.fetch_auction()
    assert warm.request_count == len(calls) == 1
    assert warm.received_at == clock.wall and warm.received_at != cold.received_at
    assert warm.elapsed_ms == 7
    assert calls[0][0] != first_url
    assert calls[0][0].startswith('https://ds.duanxianxia.com' + source.CURRENT_PATH + '?_=')
    assert calls[0][1] == {'timeout': 15}


def test_missing_legacy_metadata_uses_only_pinned_original_data_endpoint(monkeypatch, clock):
    calls=[]
    def fetch(url,**kwargs):
        calls.append(url)
        if url==source.DATASOURCE_URL:
            raise SourceFailure('upstream_unavailable',http_status=404)
        assert url.startswith(source.PINNED_CURRENT_ORIGIN+source.CURRENT_PATH+'?_=')
        return Response(b'synthetic encrypted contract',clock.wall,4,200)
    monkeypatch.setattr(source,'fetch_https',fetch)
    result=source.fetch_auction()
    assert result.request_count==len(calls)==2
    assert result.received_at==clock.wall
    assert source._METADATA_CACHE is None


def test_transport_preserves_missing_route_status_without_exposing_http_body(monkeypatch):
    import urllib.error
    from types import SimpleNamespace
    from app.market import protocol
    def refuse(*args,**kwargs):raise urllib.error.HTTPError(source.DATASOURCE_URL,404,'not found',{},None)
    monkeypatch.setattr(protocol.urllib.request,'build_opener',lambda *args:SimpleNamespace(open=refuse))
    with pytest.raises(SourceFailure) as failure:protocol.fetch_https(source.DATASOURCE_URL)
    assert failure.value.reason=='upstream_unavailable' and failure.value.http_status==404
    assert str(failure.value)=='upstream_unavailable'


@pytest.mark.parametrize('reason,status',[('upstream_unavailable',500),('upstream_unavailable',None),
    ('permission_missing',403),('upstream_redirect',302),('transport_error',None),('decoder_changed',None)])
def test_fixed_route_does_not_bypass_other_metadata_failures(monkeypatch,reason,status):
    calls=[]
    def fetch(url,**kwargs):
        calls.append(url)
        raise SourceFailure(reason,http_status=status)
    monkeypatch.setattr(source,'fetch_https',fetch)
    with pytest.raises(SourceFailure,match=reason):source.fetch_auction()
    assert calls==[source.DATASOURCE_URL]


def test_concurrent_cold_polls_share_one_verified_initialization(monkeypatch, upstream):
    calls, _, _ = upstream
    fetch = source.fetch_https
    entered, release = threading.Event(), threading.Event()

    def blocking_fetch(url, **kwargs):
        if url == source.DATASOURCE_URL:
            entered.set()
            assert release.wait(timeout=2)
        return fetch(url, **kwargs)

    monkeypatch.setattr(source, 'fetch_https', blocking_fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(source.fetch_auction)
        assert entered.wait(timeout=2)
        second = pool.submit(source.fetch_auction)
        release.set()
        responses = [first.result(timeout=2), second.result(timeout=2)]
    assert sorted(response.request_count for response in responses) == [1, 3]
    assert len(calls) == 4
    assert sum(url == source.DATASOURCE_URL for url, _ in calls) == 1
    assert sum(url == source.CRYPTO_URL for url, _ in calls) == 1


@pytest.mark.parametrize('start,finish', [
    ('2026-10-26T09:14:59', '2026-10-26T09:15:01'),
    ('2026-10-26T09:34:59', '2026-10-26T09:35:01'),
    ('2026-10-26T23:59:59', '2026-10-27T00:00:01'),
])
def test_entering_leaving_window_or_crossing_day_revalidates(clock, upstream, start, finish):
    calls, _, _ = upstream
    clock.wall = datetime.fromisoformat(start).replace(tzinfo=SHANGHAI)
    source.fetch_auction()
    calls.clear()
    clock.wall = datetime.fromisoformat(finish).replace(tzinfo=SHANGHAI)
    clock.tick += 2
    assert source.fetch_auction().request_count == 3
    assert [url for url, _ in calls[:2]] == [source.DATASOURCE_URL, source.CRYPTO_URL]


@pytest.mark.parametrize('hour,minute,lifetime', [(9, 15, 1200), (10, 0, 300)])
def test_ttl_is_bounded_and_expiry_revalidates(clock, upstream, hour, minute, lifetime):
    calls, _, _ = upstream
    clock.wall = datetime(2026, 10, 26, hour, minute, tzinfo=SHANGHAI)
    source.fetch_auction()
    clock.tick += lifetime - 0.001
    assert source.fetch_auction().request_count == 1
    calls.clear()
    clock.tick += 0.001
    assert source.fetch_auction().request_count == 3
    assert len(calls) == 3


def test_changed_pin_invalidates_expired_metadata_and_is_never_cached(clock, upstream):
    calls, _, state = upstream
    source.fetch_auction()
    clock.tick += 1200
    state['script'] = b'changed public decoder'
    for _ in range(2):
        calls.clear()
        with pytest.raises(SourceFailure) as raised:
            source.fetch_auction()
        assert raised.value.reason == 'decoder_changed'
        assert raised.value.request_count == len(calls) == 2
        assert source._METADATA_CACHE is None


@pytest.mark.parametrize('bad_origin', ['https://unverified.example', 'http://duanxianxia.com',
    'https://ds.duanxianxia.com/path', 'https://duanxianxia.com@unverified.example'])
def test_invalid_routing_is_not_cached_or_followed(upstream, bad_origin):
    calls, _, state = upstream
    state['config']['data_url'] = bad_origin
    for _ in range(2):
        calls.clear()
        with pytest.raises(SourceFailure) as raised:
            source.fetch_auction()
        assert raised.value.reason == 'endpoint_not_allowed'
        assert raised.value.request_count == 1 and len(calls) == 1
        assert source._METADATA_CACHE is None


@pytest.mark.parametrize('reason', ['permission_missing', 'upstream_redirect', 'schema_changed',
    'decoder_changed', 'endpoint_not_allowed', 'response_too_large', 'rate_limited'])
def test_current_table_nontransport_failure_is_not_retried(upstream, reason):
    calls, outcomes, _ = upstream
    outcomes.append(SourceFailure(reason))
    with pytest.raises(SourceFailure) as raised:
        source.fetch_auction()
    assert raised.value.reason == reason and raised.value.request_count == len(calls) == 3


@pytest.mark.parametrize('reason', ['transport_error', 'timeout'])
@pytest.mark.parametrize('warm', [False, True])
def test_table_transient_failure_retries_identical_url_once(upstream, reason, warm):
    calls, outcomes, _ = upstream
    if warm:
        source.fetch_auction()
        calls.clear()
    outcomes.extend([SourceFailure(reason), b'retry succeeded'])
    response = source.fetch_auction()
    assert response.body == b'retry succeeded'
    assert response.request_count == len(calls) == (2 if warm else 4)
    assert calls[-1] == calls[-2]


def test_success_after_retry_includes_failed_request_elapsed_time(monkeypatch, clock, upstream):
    fetch = source.fetch_https
    failed = False

    def timed_fetch(url, **kwargs):
        nonlocal failed
        if source.CURRENT_PATH in url and not failed:
            failed = True
            clock.tick += 0.8
            raise SourceFailure('timeout')
        return fetch(url, **kwargs)

    monkeypatch.setattr(source, 'fetch_https', timed_fetch)
    response = source.fetch_auction()
    assert response.request_count == 4
    assert response.elapsed_ms == 821


@pytest.mark.parametrize('warm', [False, True])
def test_second_table_failure_is_terminal_and_counts_both_attempts(upstream, warm):
    calls, outcomes, _ = upstream
    if warm:
        source.fetch_auction()
        calls.clear()
    outcomes.extend([SourceFailure('transport_error'), SourceFailure('timeout')])
    with pytest.raises(SourceFailure) as raised:
        source.fetch_auction()
    assert raised.value.reason == 'timeout'
    assert raised.value.request_count == len(calls) == (2 if warm else 4)
    assert calls[-1] == calls[-2]


def test_initialization_transport_failure_is_not_retried_or_cached(monkeypatch):
    calls = []

    def fail(url, **kwargs):
        calls.append(url)
        raise SourceFailure('transport_error')

    monkeypatch.setattr(source, 'fetch_https', fail)
    with pytest.raises(SourceFailure) as raised:
        source.fetch_auction()
    assert raised.value.request_count == len(calls) == 1
    assert source._METADATA_CACHE is None


def test_history_never_reuses_initialization_or_retries(upstream):
    calls, outcomes, _ = upstream
    source.fetch_auction()
    calls.clear()
    outcomes.append(SourceFailure('timeout'))
    with pytest.raises(SourceFailure) as raised:
        source.fetch_auction('history')
    assert raised.value.request_count == 1
    assert calls == [(source.BASE + source.HISTORY_PATH, {'timeout': 15, 'data': b''})]
