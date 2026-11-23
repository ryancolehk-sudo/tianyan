"""Synthetic OAuth wire boundaries; runnable without PostgreSQL via unittest."""
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from app.chatgpt import oauth


class Response:
    def __init__(self, status=200, body=None, raw=None):
        self.status = status
        self.raw = json.dumps(body or {}).encode() if raw is None else raw
        self.closed = False

    def read(self, limit):
        return self.raw[:limit]

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, response, *, connect_error=None, read_error=None):
        self.response, self.connect_error, self.read_error = response, connect_error, read_error
        self.requests, self.closed = [], False

    def connect(self):
        if self.connect_error:
            raise self.connect_error

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        if self.read_error:
            raise self.read_error
        return self.response

    def close(self):
        self.closed = True


class OAuthWireTests(unittest.TestCase):
    def test_token_exchange_fixed_destination_form_no_secret_no_redirects(self):
        connection = Connection(Response(body={"scope": "openid"}))
        with patch.object(oauth.http.client, "HTTPSConnection", return_value=connection) as constructor:
            value = oauth.request_json(oauth.TOKEN_URL, form={"grant_type": "authorization_code", "code": "synthetic-code", "client_id": "oaiapp_synthetic"})
        self.assertEqual(value, {"scope": "openid"})
        self.assertEqual(constructor.call_args.args, ("auth.openai.com",))
        method, path, body, headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/api/accounts/oauth/token"))
        self.assertEqual(parse_qs(body.decode()), {"grant_type": ["authorization_code"], "code": ["synthetic-code"], "client_id": ["oaiapp_synthetic"]})
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertNotIn("Authorization", headers)
        self.assertTrue(connection.closed)

    def test_destination_rejections_happen_before_connection(self):
        for url in ("http://auth.openai.com/x", "https://evil.invalid/x", "https://auth.openai.com:444/x",
                    "https://user:password@auth.openai.com/x", "https://auth.openai.com/x?redirect=secret", "https://auth.openai.com.evil.invalid/x"):
            with self.subTest(url=url), patch.object(oauth.http.client, "HTTPSConnection") as constructor:
                with self.assertRaises(oauth.OAuthFailure):
                    oauth.request_json(url, form={"token": "synthetic-token"})
                constructor.assert_not_called()

    def test_redirect_is_never_followed_and_tokens_are_absent_from_diagnostics(self):
        connection = Connection(Response(status=302, body={"error": {"code": "redirected", "message": "synthetic-secret-do-not-print"}}))
        with patch.object(oauth.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaises(oauth.OAuthFailure) as caught:
                oauth.request_json(oauth.TOKEN_URL, form={"refresh_token": "synthetic-secret-do-not-print"})
        self.assertEqual(len(connection.requests), 1)
        self.assertEqual(caught.exception.status, 302)
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_connect_failure_is_unsent_and_read_failure_is_unknown(self):
        for connection, unknown in ((Connection(Response(), connect_error=TimeoutError()), False),
                                    (Connection(Response(), read_error=TimeoutError()), True)):
            with patch.object(oauth.http.client, "HTTPSConnection", return_value=connection):
                with self.assertRaises(oauth.OAuthFailure) as caught:
                    oauth.request_json(oauth.TOKEN_URL, form={"refresh_token": "synthetic-refresh"})
            self.assertEqual(caught.exception.uncertain, unknown)

    def test_revocation_accepts_empty_200_and_no_other_success_status(self):
        connection = Connection(Response(raw=b""))
        with patch.object(oauth.http.client, "HTTPSConnection", return_value=connection):
            self.assertEqual(oauth.request_json(oauth.ISSUER + "/oauth/revoke", form={"token": "synthetic-refresh"}, empty_success=True), {})
        connection = Connection(Response(status=204, raw=b""))
        with patch.object(oauth.http.client, "HTTPSConnection", return_value=connection), self.assertRaises(oauth.OAuthFailure):
            oauth.request_json(oauth.ISSUER + "/oauth/revoke", form={"token": "synthetic-refresh"}, empty_success=True)

    def test_discovery_rejects_foreign_issuer_and_foreign_jwks(self):
        for document in ({"issuer": "https://evil.invalid", "jwks_uri": oauth.ISSUER + "/jwks"},
                         {"issuer": oauth.ISSUER, "jwks_uri": "https://evil.invalid/jwks"},
                         {"issuer": oauth.ISSUER, "jwks_uri": oauth.ISSUER + "/jwks", "token_endpoint": "https://evil.invalid/token"}):
            with patch.object(oauth, "request_json", return_value=document), self.assertRaises(oauth.OAuthFailure):
                oauth.discovery(force=True)

    def test_malformed_or_oversized_token_response_is_not_returned(self):
        for raw in (b"not-json", b"[]", b"x" * 262145):
            connection = Connection(Response(raw=raw))
            with patch.object(oauth.http.client, "HTTPSConnection", return_value=connection), self.assertRaises(oauth.OAuthFailure):
                oauth.request_json(oauth.TOKEN_URL, form={"code": "synthetic-code"})

    def test_identity_only_bundle_without_access_token_is_supported(self):
        bundle = oauth.token_bundle({"id_token": "synthetic-id-token"})
        self.assertEqual(bundle["scopes"], [])
        self.assertIsNone(bundle["access_token"])
        self.assertIsNone(bundle["refresh_token"])

    def test_absolute_deadline_aborts_retained_tls_socket_after_connection_close(self):
        timers = []
        class Timer:
            def __init__(self, seconds, callback):
                self.seconds, self.callback, self.cancelled = seconds, callback, False
                timers.append(self)
            def start(self):
                pass
            def cancel(self):
                self.cancelled = True
        class Socket:
            def __init__(self):
                self.shut, self.closed = False, False
            def shutdown(self, how):
                self.shut = True
            def close(self):
                self.closed = True
        original_socket = Socket()
        response = Response(body={"access_token": "synthetic-access"})
        connection = Connection(response)
        connection.sock = original_socket
        def dripping_read(limit):
            # Mirrors an HTTPResponse whose keepalive owner has detached sock.
            connection.sock = None
            timers[0].callback()
            return response.raw[:limit]
        response.read = dripping_read
        with patch.object(oauth.threading, "Timer", Timer), patch.object(oauth.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaises(oauth.OAuthFailure) as caught:
                oauth.request_json(oauth.TOKEN_URL, form={"refresh_token": "synthetic-refresh"})
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(timers[0].seconds, 30)
        self.assertTrue(timers[0].cancelled and response.closed and original_socket.shut and original_socket.closed)
        self.assertEqual(len(connection.requests), 1)

    def test_deadline_during_connect_never_sends_credentials_after_connect_returns(self):
        timers = []
        class Timer:
            def __init__(self, seconds, callback):
                self.callback, self.cancelled = callback, False
                timers.append(self)
            def start(self):
                pass
            def cancel(self):
                self.cancelled = True
        connection = Connection(Response())
        connection.connect = lambda: timers[0].callback()
        with patch.object(oauth.threading, "Timer", Timer), patch.object(oauth.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaises(oauth.OAuthFailure) as caught:
                oauth.request_json(oauth.TOKEN_URL, form={"code": "synthetic-code"})
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(connection.requests, [])
        self.assertTrue(timers[0].cancelled)


if __name__ == "__main__":
    unittest.main()
