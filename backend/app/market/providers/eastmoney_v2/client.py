"""Bounded HTTP transport for the Eastmoney v2 adapter.

The project's shared ``app.market.protocol.fetch_https`` is the reference for
size limits, redirect refusal and reason vocabulary, but it fixes one
User-Agent/Referer pair and its host allow-list does not contain
``push2delay.eastmoney.com``. This module keeps the same rules locally instead of
editing a shared file that every other provider depends on.

No cookie, token, account, proxy rotation or hidden retry is used. The two hosts
below are the public quote hosts named in the supplied API notes. A short burst
of requests is enough to make either host close the connection without a
response, so pacing and a bounded attempt count are part of the contract.
"""
from __future__ import annotations

import gzip
import http.client
import io
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.market.protocol import MAX_BODY, SHANGHAI

ALLOWED_HOSTS = ("push2delay.eastmoney.com", "push2.eastmoney.com")
ALLOWED_PATHS = frozenset(
    {
        "/api/qt/stock/get",
        "/api/qt/ulist.np/get",
        "/api/qt/clist/get",
        "/api/qt/stock/fflow/kline/get",
    }
)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Tianyan/1.0 (private research)"
REFERER = "https://quote.eastmoney.com/"


class TransportFailure(Exception):
    """A controlled failure carrying the exchange status the caller must return."""

    def __init__(
        self, status: str, reason: str, *, requests: int = 0,
        retry_after: int | None = None, raw_body: bytes | None = None,
        received_at: datetime | None = None,
    ):
        self.status = status
        self.reason = reason
        self.requests = requests
        self.retry_after = retry_after
        # Optional bounded, decoded HTTP entity for private probe evidence only.
        # Exception text remains the controlled reason; bodies never enter logs.
        self.raw_body = raw_body
        self.received_at = received_at
        super().__init__(reason)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TransportFailure("error", "upstream_redirect")


@dataclass(frozen=True)
class JsonResponse:
    payload: dict
    received_at: datetime
    requests: int
    endpoint: str
    raw_body: bytes | None = None


def _decode_body(body: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                decoded = compressed.read(MAX_BODY + 1)
        except (gzip.BadGzipFile, EOFError, zlib.error) as error:
            raise TransportFailure("error", "unsafe_upstream_response") from error
        if len(decoded) > MAX_BODY:
            raise TransportFailure("error", "response_too_large")
        return decoded
    raise TransportFailure("error", "unsafe_upstream_response")


def _json_payload(body: bytes) -> dict:
    if not body:
        raise TransportFailure("error", "schema_changed")
    try:
        payload = json.loads(
            body,
            parse_float=Decimal,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError) as error:
        raise TransportFailure("error", "schema_changed") from error
    if not isinstance(payload, dict):
        raise TransportFailure("error", "schema_changed")
    rc = payload.get("rc")
    if type(rc) is not int:
        raise TransportFailure("error", "schema_changed")
    if rc != 0:
        raise TransportFailure("error", f"upstream_rc_{rc}")
    data = payload.get("data")
    if data is not None and not isinstance(data, dict):
        raise TransportFailure("error", "schema_changed")
    return payload


class EastmoneyClient:
    """One adapter-local client. Tests inject ``opener`` or ``sleep``/``monotonic``."""

    def __init__(
        self,
        *,
        min_interval: float = 3.0,
        timeout: float = 12.0,
        max_attempts: int = 1,
        opener=None,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ):
        if (not math.isfinite(min_interval) or min_interval < 0 or not 0 < timeout <= 15
                or type(max_attempts) is not int or not 1 <= max_attempts <= 3):
            raise ValueError("invalid transport bounds")
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request: float | None = None
        self._requests = 0
        self._pace_lock = threading.Lock()

    def _pace(self):
        with self._pace_lock:
            now = self._monotonic()
            if self._last_request is not None:
                wait = self.min_interval - (now - self._last_request)
                if wait > 0:
                    self._sleep(wait)
            self._last_request = self._monotonic()

    def _attempt(self, host: str, path: str, query: str) -> JsonResponse:
        url = f"https://{host}{path}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json,text/plain,*/*",
                "Accept-Encoding": "identity",
                "Referer": REFERER,
                "Connection": "close",
            },
        )
        self._pace()
        self._requests += 1
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                if status in (401, 403):
                    raise TransportFailure("permission_denied", "upstream_permission_denied", requests=self._requests)
                if status == 429:
                    raw = response.headers.get("Retry-After", "") if response.headers else ""
                    seconds = min(int(raw), 86400) if str(raw).isdigit() else None
                    raise TransportFailure(
                        "rate_limited", "upstream_rate_limited", requests=self._requests, retry_after=seconds
                    )
                if status >= 500:
                    raise TransportFailure("error", "upstream_unavailable", requests=self._requests)
                body = response.read(MAX_BODY + 1)
                if len(body) > MAX_BODY:
                    raise TransportFailure("error", "response_too_large", requests=self._requests)
                body = _decode_body(body, response.headers.get("Content-Encoding", "") if response.headers else "")
                received_at = datetime.now(UTC).astimezone(SHANGHAI)
                try:
                    payload = _json_payload(body)
                except TransportFailure as error:
                    raise TransportFailure(
                        error.status, error.reason, requests=self._requests,
                        raw_body=body, received_at=received_at,
                    ) from None
                return JsonResponse(
                    payload=payload,
                    received_at=received_at,
                    requests=self._requests,
                    endpoint=path,
                    raw_body=body,
                )
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise TransportFailure("permission_denied", "upstream_permission_denied", requests=self._requests) from None
            if error.code == 429:
                raw = error.headers.get("Retry-After", "") if error.headers else ""
                seconds = min(int(raw), 86400) if str(raw).isdigit() else None
                raise TransportFailure(
                    "rate_limited", "upstream_rate_limited", requests=self._requests, retry_after=seconds
                ) from None
            if error.code >= 500:
                raise TransportFailure("error", "upstream_unavailable", requests=self._requests) from None
            raise TransportFailure("error", f"upstream_rejected_{error.code}", requests=self._requests) from None
        except TransportFailure:
            raise
        except TimeoutError:
            raise TransportFailure("error", "timeout", requests=self._requests) from None
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, http.client.IncompleteRead):
            # A disconnect can be a network failure or throttling. It is not a
            # confirmed 429 and must not trigger a host-switch retry burst.
            raise TransportFailure("error", "upstream_connection_reset", requests=self._requests) from None
        except urllib.error.URLError as error:
            reason = error.reason
            if isinstance(reason, TimeoutError):
                raise TransportFailure("error", "timeout", requests=self._requests) from None
            raise TransportFailure("error", "transport_error", requests=self._requests) from None
        except OSError:
            raise TransportFailure("error", "transport_error", requests=self._requests) from None

    def request_json(self, path: str, params: dict) -> JsonResponse:
        if path not in ALLOWED_PATHS:
            raise TransportFailure("error", "endpoint_not_allowed")
        query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
        last: TransportFailure | None = None
        retryable = {"upstream_unavailable", "timeout", "transport_error"}
        # Primary is the delayed host named first in the notes; the realtime host
        # is a fallback. A hard permission/rate-limit answer is never retried.
        sequence = [ALLOWED_HOSTS[0], ALLOWED_HOSTS[1], ALLOWED_HOSTS[0]][: self.max_attempts]
        for host in sequence:
            try:
                return self._attempt(host, path, query)
            except TransportFailure as error:
                if error.status in ("permission_denied", "rate_limited"):
                    raise
                last = error
                if error.reason not in retryable:
                    raise
                continue
        raise last or TransportFailure("error", "transport_error", requests=self._requests)
