"""Synthetic wire responses only; no supplier calls or database writes."""
from datetime import datetime, timedelta
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from app.market import eastmoney_trading as market, protocol
from app.market.protocol import Response, SourceFailure

NOW = datetime.fromisoformat('2026-10-30T14:00:00+08:00')
ORIGIN = 'push2.eastmoney.com'
ALLOCATED = 'push2delay.eastmoney.com'


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network forbidden'))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            at = NOW + timedelta(seconds=2)
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)
    monkeypatch.setattr(market, 'datetime', Clock)
    monkeypatch.setattr(protocol, 'datetime', Clock)


def allocation(url):
    return url.replace('https://' + ORIGIN + '/', 'https://' + ALLOCATED + '/', 1)


def wire(url, *, age=0, missing_clock=False):
    filters = parse_qs(urlsplit(url).query)['fs'][0]
    code = 'BK0001' if filters in {market.FILTERS['industry_l2'], market.FILTERS['industry']} else (
        'BK0002' if filters == market.FILTERS['concept'] else '600000')
    row = {'f12': code, 'f13': 1 if code == '600000' else 90, 'f14': 'synthetic',
        'f3': '1.5', 'f6': '1000', 'f62': '10',
        'f124': None if missing_clock else int((NOW - timedelta(seconds=age)).timestamp())}
    return json.dumps({'rc': 0, 'data': {'total': 1, 'diff': [row]}}).encode()


class WireResponse:
    status = 200
    def __init__(self, body, headers=None):
        self.body, self.headers = body, headers or {}
    def read(self, limit):
        return self.body[:limit]
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


def install_wire(monkeypatch, *, target=allocation, status=302, outcome=None):
    calls = []
    def opener(handler):
        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                url = request.full_url
                if urlsplit(url).hostname == ORIGIN:
                    return handler.redirect_request(request, None, status, '', {}, target(url))
                return outcome(request, handler) if outcome else WireResponse(wire(url))
        return Opener()
    monkeypatch.setattr(protocol.urllib.request, 'build_opener', opener)
    return calls


def test_a_same_query_allocation_rechecks_every_permit_and_keeps_native_times(monkeypatch):
    calls = install_wire(monkeypatch)
    permits = []
    def reserve():
        assert len(permits) == len(calls)
        permits.append(True)
    response = market.fetch_rankings('eastmoney_funds', reserve)
    result = market.parse_rankings(response.body, response.received_at, 'eastmoney_funds')
    assert len(calls) == len(permits) == response.request_count == result.requests == 6
    assert len(result.records) == 3 and result.rejected == 0
    assert json.loads(response.body)['allocated_tables'] == ['stock_funds', 'industry_funds', 'concept_funds']
    for original, allocated in zip(calls[::2], calls[1::2]):
        assert allocated.full_url == allocation(original.full_url)
        assert allocated.get_method() == 'GET' and allocated.data is None
        assert not any(allocated.has_header(key) for key in ['Cookie', 'Authorization', 'Proxy-Authorization'])
    for _, payload, _ in result.records:
        assert datetime.fromisoformat(payload.rows[0]['source_time']) == NOW
        assert payload.rows[0]['net_inflow_cny'] == '10'
        assert '供应商同接口分配节点' in payload.scope


@pytest.mark.parametrize('target', [
    lambda u: allocation(u).replace('https:', 'http:'),
    lambda u: allocation(u).replace(ALLOCATED, ALLOCATED + '.invalid'),
    lambda u: allocation(u).replace(ALLOCATED, '127.0.0.1'),
    lambda u: allocation(u).replace(ALLOCATED, 'user:secret@' + ALLOCATED),
    lambda u: allocation(u).replace(ALLOCATED, '@' + ALLOCATED),
    lambda u: allocation(u).replace(ALLOCATED, ALLOCATED + ':444'),
    lambda u: allocation(u).replace(ALLOCATED, ALLOCATED + ':bad'),
    lambda u: allocation(u).replace('/clist/get', '/fflow/kline/get'),
    lambda u: allocation(u) + '&token=secret',
    lambda u: allocation(u).replace('pn=1', 'pn=2'),
    lambda u: allocation(u) + '#secret',
    lambda u: allocation(u) + '#',
    lambda u: allocation(u).replace(ALLOCATED, ALLOCATED + '\t'),
    lambda u: 'https://' + ALLOCATED + '/api/qt/clist/get',
    lambda u: 'https://' + ALLOCATED + '/api/qt/clist/get?' + 'x' * 4100,
    lambda u: u,
])
def test_other_redirects_never_leave_the_original_address(monkeypatch, target):
    calls = install_wire(monkeypatch, target=target)
    with pytest.raises(market.EastmoneyFailure) as error:
        market.fetch_rankings('eastmoney_funds', lambda: None)
    assert error.value.reason == 'upstream_redirect' and error.value.request_count == 3
    assert len(calls) == 3 and all(urlsplit(r.full_url).hostname == ORIGIN for r in calls)
    assert 'secret' not in str(error.value) and 'secret' not in repr(error.value)


@pytest.mark.parametrize('status', [301, 303, 307, 308])
def test_unverified_redirect_status_is_not_followed(monkeypatch, status):
    calls = install_wire(monkeypatch, status=status)
    with pytest.raises(market.EastmoneyFailure, match='upstream_redirect') as error:
        market.fetch_rankings('eastmoney_funds', lambda: None)
    assert error.value.request_count == len(calls) == 3


@pytest.mark.parametrize('reason', ['permission_missing', 'lease_expired', 'intraday_window_wait',
    'rate_limited', 'configuration_changed'])
def test_loss_of_a_second_permit_prevents_the_allocated_request(monkeypatch, reason):
    calls = install_wire(monkeypatch)
    permits = []
    def reserve():
        permits.append(True)
        if len(permits) == 2:
            raise SourceFailure(reason, retry_after=75 if reason == 'rate_limited' else None)
    with pytest.raises(market.EastmoneyFailure, match=reason) as error:
        market.fetch_rankings('eastmoney_funds', reserve)
    assert error.value.request_count == len(calls) == 1 and len(permits) == 2
    if reason == 'rate_limited':
        assert error.value.retry_after == 75


@pytest.mark.parametrize('code,reason', [(403, 'permission_missing'), (429, 'rate_limited')])
def test_allocated_permission_or_rate_limit_stops_the_entire_batch(monkeypatch, code, reason):
    def failure(request, handler):
        raise HTTPError(request.full_url, code, '', {'Retry-After': '75'}, None)
    calls = install_wire(monkeypatch, outcome=failure)
    with pytest.raises(market.EastmoneyFailure, match=reason) as error:
        market.fetch_rankings('eastmoney_funds', lambda: None)
    assert error.value.request_count == len(calls) == 2
    if code == 429:
        assert error.value.retry_after == 75


def test_a_second_redirect_never_causes_a_third_request(monkeypatch):
    def again(request, handler):
        return handler.redirect_request(request, None, 302, '', {}, request.full_url)
    calls = install_wire(monkeypatch, outcome=again)
    with pytest.raises(market.EastmoneyFailure, match='upstream_redirect') as error:
        market.fetch_rankings('eastmoney_funds', lambda: None)
    assert len(calls) == error.value.request_count == 6


def test_one_allocated_timeout_preserves_the_other_two_tables(monkeypatch):
    def outcome(request, handler):
        if len(calls) == 2:
            raise TimeoutError()
        return WireResponse(wire(request.full_url))
    calls = install_wire(monkeypatch, outcome=outcome)
    response = market.fetch_rankings('eastmoney_funds', lambda: None)
    result = market.parse_rankings(response.body, response.received_at, 'eastmoney_funds')
    assert len(calls) == result.requests == response.request_count == 6
    assert (len(result.records), result.expected, result.rejected) == (2, 3, 1)
    assert json.loads(response.body)['failed_tables'] == [{'table': 'stock_funds', 'reason': 'timeout'}]


@pytest.mark.parametrize('missing_clock', [False, True])
def test_an_allocated_response_cannot_replace_old_or_missing_source_time(monkeypatch, missing_clock):
    install_wire(monkeypatch, outcome=lambda req, _: WireResponse(wire(req.full_url, age=600, missing_clock=missing_clock)))
    response = market.fetch_rankings('eastmoney_funds', lambda: None)
    result = market.parse_rankings(response.body, response.received_at, 'eastmoney_funds')
    for _, payload, _ in result.records:
        raw = payload.rows[0]['source_time']
        assert raw is None if missing_clock else datetime.fromisoformat(raw) == NOW - timedelta(seconds=600)
        assert response.received_at == NOW + timedelta(seconds=2)


@pytest.mark.parametrize('corruption', ['count', 'duplicates', 'unknown', 'wrong_type'])
def test_persisted_allocation_metadata_cannot_hide_request_counts_or_scope(monkeypatch, corruption):
    install_wire(monkeypatch)
    response = market.fetch_rankings('eastmoney_funds', lambda: None)
    value = json.loads(response.body)
    if corruption == 'count': value['request_count'] = 3
    elif corruption == 'duplicates': value['allocated_tables'] = ['stock_funds'] * 3
    elif corruption == 'unknown': value['allocated_tables'][0] = 'other'
    else: value['allocated_tables'] = True
    with pytest.raises(SourceFailure, match='schema_changed'):
        market.parse_rankings(json.dumps(value).encode(), response.received_at, 'eastmoney_funds')


def test_generic_transport_still_rejects_the_allocated_host(monkeypatch):
    monkeypatch.setattr(protocol.urllib.request, 'build_opener', lambda *a: pytest.fail('no connection allowed'))
    assert ALLOCATED not in protocol.ALLOWED_HOSTS
    with pytest.raises(SourceFailure, match='endpoint_not_allowed'):
        protocol.fetch_https(allocation(market.ranking_url('stocks', funds=True)))


def test_the_same_address_cannot_be_used_for_a_different_original_api(monkeypatch):
    monkeypatch.setattr(protocol.urllib.request, 'build_opener', lambda *a: pytest.fail('no connection allowed'))
    original = market.ranking_url('stocks', funds=True).replace('/clist/get', '/fflow/kline/get')
    with pytest.raises(SourceFailure, match='upstream_redirect'):
        protocol.fetch_eastmoney_allocation(allocation(original), original_url=original)


def test_existing_board_and_configured_constituent_pages_use_the_same_bounded_rule(monkeypatch):
    calls = install_wire(monkeypatch)
    permits = []
    response = market.fetch_rankings('eastmoney_boards', lambda: permits.append(True), ['BK0010'])
    result = market.parse_rankings(response.body, response.received_at, 'eastmoney_boards')
    assert len(calls) == len(permits) == response.request_count == result.requests == 6
    assert len(result.records) == 3 and not result.rejected
    assert json.loads(response.body)['allocated_tables'] == ['industry', 'concept', 'constituents:BK0010']
    assert result.records[-1][1].rows[0]['board_code'] == 'BK0010'
