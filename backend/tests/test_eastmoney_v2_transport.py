"""Transport/status mapping tests. SYNTHETIC BODIES ONLY; no network access."""
from __future__ import annotations

import gzip
import http.client
import json
import urllib.error
from datetime import UTC, datetime

import pytest

from app.market.providers.eastmoney_v2.client import EastmoneyClient, TransportFailure


@pytest.fixture(autouse=True)
def isolated_database():
    yield


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict | None = None):
        self._body = body
        self.status = status
        self.headers = headers or {}

    def read(self, _limit=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request.full_url, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client(outcomes, **kwargs):
    return EastmoneyClient(
        opener=FakeOpener(outcomes),
        min_interval=0,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0,
        **kwargs,
    )


def body(payload):
    return json.dumps(payload).encode()


def test_permission_denied_is_not_retried():
    opener = FakeOpener([urllib.error.HTTPError("https://x", 403, "forbidden", {}, None)])
    reader = EastmoneyClient(opener=opener, min_interval=0, sleep=lambda _s: None, monotonic=lambda: 0, max_attempts=2)
    with pytest.raises(TransportFailure) as error:
        reader.request_json("/api/qt/stock/get", {"secid": "1.600000"})
    assert (error.value.status, error.value.reason) == ("permission_denied", "upstream_permission_denied")
    assert len(opener.requests) == 1


def test_rate_limit_keeps_retry_after():
    opener = FakeOpener([urllib.error.HTTPError("https://x", 429, "slow down", {"Retry-After": "12"}, None)])
    reader = EastmoneyClient(opener=opener, min_interval=0, sleep=lambda _s: None, monotonic=lambda: 0)
    with pytest.raises(TransportFailure) as error:
        reader.request_json("/api/qt/clist/get", {})
    assert error.value.status == "rate_limited"
    assert error.value.retry_after == 12
    assert len(opener.requests) == 1


def test_server_error_is_retried_once_then_succeeds():
    opener = FakeOpener(
        [
            urllib.error.HTTPError("https://x", 500, "boom", {}, None),
            FakeResponse(body({"rc": 0, "data": {"total": 0, "diff": []}})),
        ]
    )
    reader = EastmoneyClient(opener=opener, min_interval=0, sleep=lambda _s: None, monotonic=lambda: 0, max_attempts=2)
    response = reader.request_json("/api/qt/clist/get", {})
    assert response.payload["rc"] == 0
    assert len(opener.requests) == 2


def test_connection_reset_is_unconfirmed_and_stops_without_host_retry():
    opener = FakeOpener(
        [
            http.client.RemoteDisconnected("closed"),
            http.client.RemoteDisconnected("closed"),
            http.client.RemoteDisconnected("closed"),
        ]
    )
    reader = EastmoneyClient(opener=opener, min_interval=0, sleep=lambda _s: None, monotonic=lambda: 0)
    with pytest.raises(TransportFailure) as error:
        reader.request_json("/api/qt/stock/get", {})
    assert error.value.status == "error"
    assert error.value.reason == "upstream_connection_reset"
    assert len(opener.requests) == 1


def test_malformed_json_is_an_error_not_an_empty_success():
    reader = client([FakeResponse(b"not-json")])
    with pytest.raises(TransportFailure) as error:
        reader.request_json("/api/qt/stock/get", {})
    assert (error.value.status, error.value.reason) == ("error", "schema_changed")
    assert error.value.raw_body == b"not-json"
    assert error.value.received_at is not None
    assert error.value.requests == 1


def test_nonzero_rc_is_preserved_as_a_reason():
    reader = client([FakeResponse(body({"rc": -1, "data": None}))])
    with pytest.raises(TransportFailure) as error:
        reader.request_json("/api/qt/stock/get", {})
    assert error.value.reason == "upstream_rc_-1"


def test_gzip_body_is_bounded_and_decoded():
    raw = gzip.compress(body({"rc": 0, "data": {"total": 1, "diff": []}}))
    reader = client([FakeResponse(raw, headers={"Content-Encoding": "gzip"})])
    response = reader.request_json("/api/qt/stock/get", {})
    assert response.payload["data"]["total"] == 1


def test_unknown_endpoint_is_refused_before_any_request():
    opener = FakeOpener([])
    reader = EastmoneyClient(opener=opener, min_interval=0, sleep=lambda _s: None, monotonic=lambda: 0)
    with pytest.raises(TransportFailure) as error:
        reader.request_json("/api/qt/secret/get", {})
    assert error.value.reason == "endpoint_not_allowed"
    assert opener.requests == []


def test_pacing_sleeps_between_attempts():
    sleeps = []
    times = iter([0, 0, 1, 1])
    reader = EastmoneyClient(
        opener=FakeOpener(
            [
                FakeResponse(body({"rc": 0, "data": {"total": 0, "diff": []}})),
                FakeResponse(body({"rc": 0, "data": {"total": 0, "diff": []}})),
            ]
        ),
        min_interval=2,
        sleep=sleeps.append,
        monotonic=lambda: next(times),
    )
    reader.request_json("/api/qt/stock/get", {})
    reader.request_json("/api/qt/stock/get", {})
    assert sleeps == [1]


def test_received_at_is_shanghai_aware():
    reader = client([FakeResponse(body({"rc": 0, "data": {}}))])
    response = reader.request_json("/api/qt/stock/get", {})
    assert response.received_at.utcoffset().total_seconds() == 8 * 3600
    assert response.received_at.tzinfo is not None
    assert response.received_at <= datetime.now(UTC).astimezone(response.received_at.tzinfo)


def test_default_does_not_retry_a_server_error():
    opener = FakeOpener([urllib.error.HTTPError("https://x", 500, "boom", {}, None)])
    reader = EastmoneyClient(opener=opener, min_interval=0)
    with pytest.raises(TransportFailure) as error:
        reader.request_json("/api/qt/stock/get", {})
    assert error.value.reason == "upstream_unavailable"
    assert len(opener.requests) == 1


def test_even_opt_in_retries_do_not_switch_hosts_after_a_disconnect():
    opener = FakeOpener([http.client.RemoteDisconnected("closed")])
    reader = EastmoneyClient(opener=opener, min_interval=0, max_attempts=3)
    with pytest.raises(TransportFailure) as error:
        reader.request_json("/api/qt/stock/get", {})
    assert (error.value.status, error.value.reason) == ("error", "upstream_connection_reset")
    assert len(opener.requests) == 1
