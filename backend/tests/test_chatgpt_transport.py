"""Offline wire-contract checks; no OAuth login, stored token, database, or network.

Also runnable with the standard library:
    PYTHONPATH=. python -m unittest discover -s tests -p test_chatgpt_transport.py
"""
import json
import socket
import unittest
from unittest.mock import patch

from app.ai import chatgpt_transport as transport
from app.ai.protocols import MAX_RESPONSE, ModelFailure


def completed(text="摘要正文", **changes):
    return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 17, "output_tokens": 6}, **changes}


def event(kind, *, response=None, **values):
    body = {"type": kind, **values}
    if response is not None:
        body["response"] = response
    return ("event: " + kind + "\ndata: " + json.dumps(body, ensure_ascii=False) + "\n\n").encode("utf-8")


class Response:
    def __init__(self, raw=b"", *, status=200, content_type="text/event-stream", headers=None, chunks=None):
        self.status = status
        self.headers = {"content-type": content_type, **{key.lower(): value for key, value in (headers or {}).items()}}
        self.chunks = list(chunks if chunks is not None else [raw])
        self.read_sizes = []
        self.before_read = None

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read1(self, count):
        self.read_sizes.append(count)
        if self.before_read:
            self.before_read()
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if isinstance(chunk, Exception):
            raise chunk
        if len(chunk) > count:
            self.chunks.insert(0, chunk[count:])
            chunk = chunk[:count]
        return chunk


class Timer:
    def __init__(self, interval, callback):
        self.interval, self.callback = interval, callback
        self.daemon, self.cancelled = False, False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


class Connection:
    def __init__(self, response, events):
        self.response, self.events = response, events
        self.requests = []
        self.closed = False
        self.connect_error = self.request_error = self.response_error = None
        self.after_connect = None
        self.after_response = None
        self.sock = None

    def connect(self):
        self.events.append("connect")
        if self.connect_error:
            raise self.connect_error
        if self.after_connect:
            self.after_connect()

    def request(self, method, path, *, body, headers):
        self.events.append("request")
        self.requests.append((method, path, body, headers))
        if self.request_error:
            raise self.request_error

    def getresponse(self):
        self.events.append("response")
        if self.response_error:
            raise self.response_error
        if self.after_response:
            self.after_response()
        return self.response

    def close(self):
        self.closed = True


class ChatGPTTransportTests(unittest.TestCase):
    secret = "offline-test-oauth-access-token-never-real"

    def setUp(self):
        self.events, self.sent = [], 0
        self.now = 100.0
        self.response = Response(event("response.completed", response=completed()))
        self.conn = Connection(self.response, self.events)
        self.timers = []
        self.patchers = [
            patch.object(transport, "resolved_addresses", return_value=["93.184.216.34", "93.184.216.35"]),
            patch.object(transport, "PinnedHTTPSConnection", return_value=self.conn),
            patch.object(transport.time, "monotonic", side_effect=lambda: self.now),
            patch.object(transport.threading, "Timer", side_effect=self.timer),
        ]
        self.dns, self.factory, _, _ = [p.start() for p in self.patchers]
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patchers)])

    def timer(self, interval, callback):
        timer = Timer(interval, callback)
        self.timers.append(timer)
        return timer

    def on_send(self):
        self.events.append("send")
        self.sent += 1

    def exchange(self, **changes):
        values = {"api_key": self.secret, "model": "gpt-account-specific", "prompt": "概括给定材料。", "on_send": self.on_send, **changes}
        return transport.exchange(**values)

    def use(self, response):
        self.response = self.conn.response = response

    def failure(self, code, *, sent=True, **changes):
        with self.assertRaises(ModelFailure) as caught:
            self.exchange(**changes)
        failure = caught.exception
        self.assertEqual(failure.code, code)
        self.assertEqual(failure.sent, sent)
        self.assertNotIn(self.secret, str(failure))
        self.assertNotIn(self.secret, repr(vars(failure)))
        return failure

    def test_generation_uses_only_fixed_authority_and_documented_fields(self):
        result = self.exchange(system="只使用提供的事实。", max_tokens=20)
        self.assertEqual(result.text, "摘要正文")
        self.assertEqual((result.input_tokens, result.output_tokens), (17, 6))
        self.dns.assert_called_once_with("api.openai.com", 443, 60)
        self.factory.assert_called_once_with("api.openai.com", "93.184.216.34", 443, 60)
        method, path, raw, headers = self.conn.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/responses"))
        self.assertEqual(json.loads(raw), {"model": "gpt-account-specific", "input": [{"role": "user", "content": "概括给定材料。"}],
                                          "instructions": "只使用提供的事实。", "store": False, "stream": True})
        self.assertEqual(headers["Authorization"], "Bearer " + self.secret)
        self.assertEqual(headers["Accept"], "text/event-stream")
        self.assertNotIn(self.secret.encode(), raw)
        self.assertEqual(self.events, ["connect", "send", "request", "response"])
        self.assertEqual(self.sent, 1)
        self.assertTrue(self.timers[0].cancelled and self.conn.closed)

    def test_no_unrequested_parameters_tools_or_conversation_storage(self):
        self.exchange(max_tokens=99999)
        body = json.loads(self.conn.requests[0][2])
        self.assertEqual(set(body), {"model", "input", "store", "stream"})
        self.assertEqual([message["role"] for message in body["input"]], ["user"])

    def test_endpoint_and_protocol_arguments_are_not_accepted(self):
        with self.assertRaises(TypeError):
            self.exchange(endpoint="https://attacker.invalid")
        with self.assertRaises(TypeError):
            self.exchange(protocol="openai_chat")
        self.assertEqual(self.sent, 0)
        self.dns.assert_not_called()

    def test_discovery_uses_models_slug_visibility_order_not_api_data_id(self):
        rows = [{"slug": "second", "display_name": "第二模型", "visibility": "list"},
                {"slug": "hidden", "display_name": "隐藏", "visibility": "hide"},
                {"slug": "first", "display_name": "第一模型", "visibility": "list"}]
        self.use(Response(json.dumps({"models": rows}).encode(), content_type="application/json"))
        result = self.exchange(discovery=True, model=None, prompt=None)
        self.assertEqual(result.models, [{"id": "second", "name": "第二模型"}, {"id": "first", "name": "第一模型"}])
        self.assertEqual(result.text, "")
        self.assertIsNone(result.input_tokens)
        method, path, raw, headers = self.conn.requests[0]
        self.assertEqual((method, path, raw), ("GET", "/v1/models", None))
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(self.sent, 1)

    def test_discovery_empty_visible_catalog_is_not_generation(self):
        self.use(Response(b'{"models":[]}', content_type="application/json"))
        result = self.exchange(discovery=True)
        self.assertEqual(result.models, [])
        self.assertEqual(result.text, "")
        self.assertIsNone(result.output_tokens)

    def test_api_key_catalog_shape_rejected(self):
        self.use(Response(b'{"data":[{"id":"not-an-oauth-catalog"}]}', content_type="application/json"))
        self.failure("invalid_response", discovery=True)

    def test_malformed_visible_catalog_entry_rejected(self):
        for value in [{"models": None}, {"models": [None]}, {"models": [{"slug": "m", "visibility": "list"}]},
                      {"models": [{"slug": "m\nx", "display_name": "M", "visibility": "list"}]},
                      {"models": [{"slug": "", "display_name": "M", "visibility": "list"}]}]:
            with self.subTest(value=value):
                self.use(Response(json.dumps(value).encode(), content_type="application/json"))
                self.failure("invalid_response", discovery=True)

    def test_catalog_limit_is_bounded(self):
        self.use(Response(json.dumps({"models": [{}] * 1001}).encode(), content_type="application/json"))
        self.failure("invalid_response", discovery=True)

    def test_secret_is_redacted_in_model_catalog(self):
        self.use(Response(json.dumps({"models": [{"slug": "model-" + self.secret,
                          "display_name": "模型 " + self.secret, "visibility": "list"}]}).encode(), content_type="application/json"))
        result = self.exchange(discovery=True)
        self.assertNotIn(self.secret, repr(result))
        self.assertIn("[已隐藏]", result.models[0]["id"])

    def test_sse_multiple_data_lines_and_comments(self):
        raw = (': keepalive\nid: ignored\nretry: 2000\nevent: response.completed\n'
               'data: {"type":"response.completed",\n'
               'data: "response": ' + json.dumps(completed()) + '}\n\n').encode()
        self.use(Response(raw))
        self.assertEqual(self.exchange().text, "摘要正文")

    def test_utf8_and_crlf_can_split_at_every_byte(self):
        raw = b"\xef\xbb\xbf" + event("response.completed", response=completed("跨块中文"))
        raw = raw.replace(b"\n", b"\r\n")
        self.use(Response(chunks=[bytes([byte]) for byte in raw]))
        self.assertEqual(self.exchange().text, "跨块中文")

    def test_cr_only_sse_delimiters(self):
        self.use(Response(event("response.completed", response=completed()).replace(b"\n", b"\r")))
        self.assertEqual(self.exchange().text, "摘要正文")

    def test_sse_without_event_header_uses_json_type(self):
        raw = b"data:" + json.dumps({"type": "response.completed", "response": completed()}).encode() + b"\n\n"
        self.use(Response(raw))
        self.assertEqual(self.exchange().text, "摘要正文")

    def test_completed_returns_at_terminal_event_without_waiting_for_eof(self):
        self.use(Response(chunks=[event("response.completed", response=completed()), OSError("must not read beyond terminal")]))
        self.assertEqual(self.exchange().text, "摘要正文")
        self.assertEqual(len(self.response.read_sizes), 1)

    def test_mismatched_sse_event_and_json_type_rejected(self):
        self.use(Response(event("response.completed", response=completed()).replace(b"event: response.completed", b"event: response.failed")))
        self.failure("invalid_response")

    def test_text_deltas_before_quota_failure_never_become_success(self):
        raw = event("response.output_text.delta", delta="不是最终结果") + event("response.failed", response={"status": "failed", "error": {
            "code": "subscription_sharing_usage_limit_exceeded", "message": self.secret}})
        self.use(Response(raw))
        failure = self.failure("subscription_sharing_usage_limit_exceeded")
        self.assertFalse(failure.retryable)
        self.assertEqual(self.sent, 1)
        self.assertEqual(len(self.conn.requests), 1)

    def test_all_documented_subscription_errors_have_safe_specific_recovery(self):
        cases = [
            ("subscription_sharing_user_not_eligible", False),
            ("subscription_sharing_usage_limit_exceeded", False),
            ("subscription_sharing_usage_unavailable", True),
            ("subscription_sharing_unsupported_capability", False),
            ("subscription_sharing_route_not_supported", False),
            ("subscription_sharing_invalid_user", False),
            ("chatpass_v2_scope_not_authorized", False),
            ("chatpass_v2_invalid_authorization_context", False),
            ("subscription_sharing_user_unavailable", True),
        ]
        for code, retryable in cases:
            with self.subTest(code=code):
                raw = event("response.failed", response={"status": "failed", "error": {"code": code, "message": self.secret}})
                self.use(Response(raw))
                failure = self.failure(code)
                self.assertEqual(failure.retryable, retryable)
                self.assertEqual(failure.status, 200)

    def test_top_level_stream_error_is_not_ignored(self):
        self.use(Response(event("error", code="subscription_sharing_user_not_eligible", message=self.secret)))
        self.failure("subscription_sharing_user_not_eligible")

    def test_response_incomplete_is_distinct_and_not_retryable(self):
        self.use(Response(event("response.output_text.delta", delta="半句") + event("response.incomplete", response={"status": "incomplete"})))
        failure = self.failure("response_incomplete")
        self.assertFalse(failure.retryable)

    def test_eof_after_delta_is_unknown_not_success_or_retryable(self):
        self.use(Response(event("response.output_text.delta", delta="半句")))
        failure = self.failure("outcome_unknown")
        self.assertFalse(failure.retryable)
        self.assertEqual(self.sent, 1)

    def test_disconnect_after_delta_is_unknown_and_never_resent(self):
        self.use(Response(chunks=[event("response.output_text.delta", delta="半句"), ConnectionResetError(self.secret)]))
        self.failure("outcome_unknown")
        self.assertEqual(self.sent, 1)
        self.assertEqual(len(self.conn.requests), 1)

    def test_empty_stream_and_done_marker_do_not_prove_completion(self):
        for raw in [b"", b"data: [DONE]\n\n", event("response.output_text.done", text="不是完成凭证")]:
            with self.subTest(raw=raw[:60]):
                self.use(Response(raw))
                self.failure("outcome_unknown")

    def test_unterminated_completed_event_is_not_a_success(self):
        self.use(Response(event("response.completed", response=completed()).rstrip(b"\n")))
        self.failure("outcome_unknown")

    def test_completed_empty_or_wrong_status_rejected(self):
        for value in [completed(""), completed("  "), completed(status="failed"), completed(status="incomplete"),
                      completed(status=None), completed(error={"code": "bad"}), {"status": "completed"}, None]:
            with self.subTest(value=value):
                self.use(Response(event("response.completed", response=value)))
                self.failure("invalid_response")

    def test_completed_tools_and_non_text_parts_are_rejected(self):
        for output in [[{"type": "function_call", "name": "shell", "arguments": "{}"}],
                       completed()["output"] + [{"type": "function_call_output", "output": "x"}],
                       [{"type": "message", "content": [{"type": "image", "text": "x"}]}],
                       [{"type": "message", "content": [{"type": "output_text", "text": 123}]}],
                       [None]]:
            with self.subTest(output=output):
                self.use(Response(event("response.completed", response=completed(output=output))))
                self.failure("invalid_response")

    def test_reasoning_and_multiple_text_messages_are_parsed(self):
        output = [{"type": "reasoning", "summary": []}, *completed("第一段")["output"], *completed("第二段")["output"]]
        self.use(Response(event("response.completed", response=completed(output=output))))
        self.assertEqual(self.exchange().text, "第一段\n第二段")

    def test_secret_in_final_text_is_redacted(self):
        self.use(Response(event("response.completed", response=completed("意外回显 " + self.secret))))
        result = self.exchange()
        self.assertNotIn(self.secret, repr(result))
        self.assertEqual(result.text, "意外回显 [已隐藏]")

    def test_unknown_usage_stays_unknown(self):
        for usage in [None, {}, {"input_tokens": -1, "output_tokens": "12"}, {"input_tokens": True, "output_tokens": 1000000001}]:
            with self.subTest(usage=usage):
                self.use(Response(event("response.completed", response=completed(usage=usage))))
                result = self.exchange()
                self.assertIsNone(result.input_tokens)
                self.assertIsNone(result.output_tokens)

    def test_error_detail_shape_keeps_status_request_id_not_diagnostic_text(self):
        self.use(Response(json.dumps({"detail": "private " + self.secret}).encode(), status=403, content_type="application/json",
                          headers={"x-request-id": "req_safe12345"}))
        failure = self.failure("chatgpt_permission_denied")
        self.assertEqual((failure.status, failure.body_shape, failure.request_id), (403, "detail", "req_safe12345"))
        self.assertFalse(failure.retryable)

    def test_structured_http_error_keeps_safe_code_param_and_retry_after(self):
        self.use(Response(json.dumps({"error": {"code": "subscription_sharing_usage_unavailable", "param": "model",
                                               "message": self.secret}}).encode(), status=503, content_type="application/json",
                          headers={"Retry-After": "20", "x-request-id": "req_safe12345"}))
        failure = self.failure("subscription_sharing_usage_unavailable")
        self.assertEqual((failure.status, failure.param, failure.body_shape), (503, "model", "error"))
        self.assertTrue(failure.retryable)
        self.assertIsNotNone(failure.retry_after)

    def test_unsafe_error_metadata_is_not_copied(self):
        self.use(Response(json.dumps({"error": {"code": self.secret, "param": self.secret, "message": self.secret}}).encode(),
                          status=400, content_type="application/json", headers={"x-request-id": "req_" + self.secret}))
        failure = self.failure("chatgpt_request_failed")
        self.assertIsNone(failure.param)
        self.assertIsNone(failure.request_id)
        self.assertIsNone(failure.upstream_code)

    def test_unknown_safe_machine_error_code_is_preserved_without_raw_prose(self):
        self.use(Response(json.dumps({"error": {"code": "context_length_exceeded", "param": "input",
                                               "message": self.secret}}).encode(), status=400, content_type="application/json"))
        failure = self.failure("chatgpt_request_failed")
        self.assertEqual(failure.upstream_code, "context_length_exceeded")
        self.assertEqual(failure.param, "input")

    def test_malformed_error_code_never_raises_type_error(self):
        self.use(Response(b'{"error":{"code":["untrusted"],"param":{},"message":"ignored"}}', status=400, content_type="application/json"))
        self.failure("chatgpt_request_failed")

    def test_common_admission_http_statuses_preserve_meaning(self):
        for status, code, retryable in [(401, "chatgpt_sign_in_required", False), (403, "chatgpt_permission_denied", False),
                                        (404, "model_or_endpoint_not_found", False), (429, "chatgpt_usage_restricted", False),
                                        (503, "chatgpt_direct_unavailable", True)]:
            with self.subTest(status=status):
                self.use(Response(b"not a standard error object", status=status, content_type="text/plain"))
                failure = self.failure(code)
                self.assertEqual(failure.status, status)
                self.assertEqual(failure.retryable, retryable)

    def test_generic_insufficient_quota_does_not_enable_api_fallback(self):
        self.use(Response(b'{"error":{"code":"insufficient_quota"}}', status=400, content_type="application/json"))
        self.assertFalse(self.failure("chatgpt_usage_restricted").retryable)

    def test_error_body_reads_are_bounded_and_do_not_expose_oversize_text(self):
        self.use(Response(b"x" * (transport.MAX_ERROR + 100000), status=503, content_type="application/json"))
        failure = self.failure("chatgpt_direct_unavailable")
        self.assertEqual(failure.status, 503)
        self.assertLessEqual(sum(self.response.read_sizes), transport.MAX_ERROR + 1)

    def test_malformed_json_and_utf8_events_fail_safely(self):
        for raw in [b"data: {bad-json}\n\n", b"data: []\n\n", b"data: {}\n\n", b"data: \xff\n\n"]:
            with self.subTest(raw=raw):
                self.use(Response(raw))
                self.failure("invalid_response")

    def test_deeply_nested_json_does_not_escape_as_recursion_error(self):
        self.use(Response(b"data: " + b"[" * 2000 + b"0" + b"]" * 2000 + b"\n\n"))
        self.failure("invalid_response")

    def test_response_size_is_bounded_even_without_event_delimiters(self):
        self.use(Response(b"data: " + b"x" * MAX_RESPONSE))
        self.failure("response_too_large")
        self.assertLessEqual(sum(self.response.read_sizes), MAX_RESPONSE + 1)

    def test_response_event_count_is_bounded(self):
        self.use(Response(event("response.created") * 3))
        with patch.object(transport, "MAX_EVENTS", 2):
            self.failure("response_too_large")

    def test_cr_only_terminal_event_cannot_bypass_event_count_limit(self):
        self.use(Response(event("response.created") * 2 + event("response.completed", response=completed()).replace(b"\n", b"\r")))
        with patch.object(transport, "MAX_EVENTS", 2):
            self.failure("response_too_large")

    def test_http_redirects_never_forward_bearer_credentials(self):
        for status in [301, 302, 303, 307, 308]:
            with self.subTest(status=status):
                before = len(self.conn.requests)
                self.use(Response(status=status, headers={"Location": "https://attacker.invalid/steal"}))
                failure = self.failure("redirect_rejected")
                self.assertEqual(failure.status, status)
                self.assertEqual(len(self.conn.requests), before + 1)
                self.assertEqual(self.conn.requests[-1][1], "/v1/responses")

    def test_compressed_or_non_stream_success_is_rejected(self):
        self.use(Response(event("response.completed", response=completed()), headers={"Content-Encoding": "gzip"}))
        self.failure("response_encoding_unsupported")
        self.use(Response(json.dumps(completed()).encode(), content_type="application/json"))
        self.failure("invalid_response")

    def test_dns_rejection_is_unsent(self):
        self.dns.side_effect = ModelFailure("endpoint_rejected", "拒绝内网地址。")
        self.failure("endpoint_rejected", sent=False)
        self.factory.assert_not_called()
        self.assertEqual(self.sent, 0)

    def test_dns_deadline_expiration_is_unsent(self):
        def resolve(*args):
            self.now += 61
            return ["93.184.216.34"]
        self.dns.side_effect = resolve
        self.failure("dns_timeout", sent=False)
        self.factory.assert_not_called()
        self.assertEqual(self.sent, 0)

    def test_tls_connection_failure_is_unsent(self):
        self.conn.connect_error = TimeoutError(self.secret)
        self.failure("connection_failed", sent=False)
        self.assertEqual(self.sent, 0)
        self.assertEqual(self.conn.requests, [])
        self.assertTrue(self.conn.closed and self.timers[0].cancelled)

    def test_connection_finished_after_deadline_does_not_send(self):
        self.conn.after_connect = lambda: setattr(self, "now", self.now + 61)
        self.failure("connection_timeout", sent=False)
        self.assertEqual(self.sent, 0)
        self.assertEqual(self.conn.requests, [])

    def test_before_send_guard_can_cancel_without_any_request(self):
        guard_error = RuntimeError("configuration changed")
        def guard():
            self.on_send()
            raise guard_error
        with self.assertRaises(RuntimeError) as caught:
            self.exchange(on_send=guard)
        self.assertIs(caught.exception, guard_error)
        self.assertEqual(self.sent, 1)
        self.assertEqual(self.conn.requests, [])
        self.assertTrue(self.conn.closed and self.timers[0].cancelled)

    def test_timeout_at_send_boundary_is_unknown_without_retransmission(self):
        def guard():
            self.on_send()
            self.now += 61
        self.failure("outcome_unknown", on_send=guard)
        self.assertEqual(self.sent, 1)
        self.assertEqual(self.conn.requests, [])

    def test_request_write_and_header_read_failures_are_unknown_once_sent(self):
        for field in ["request_error", "response_error"]:
            with self.subTest(field=field):
                self.conn.request_error = self.conn.response_error = None
                setattr(self.conn, field, OSError(self.secret))
                before = len(self.conn.requests)
                self.failure("outcome_unknown")
                self.assertEqual(len(self.conn.requests), before + 1)

    def test_slow_stream_deadline_is_unknown(self):
        self.response.before_read = lambda: setattr(self, "now", self.now + 61)
        self.failure("outcome_unknown")
        self.assertEqual(self.sent, 1)
        self.assertEqual(len(self.conn.requests), 1)

    def test_timer_abort_can_interrupt_a_socket_and_always_cancels(self):
        class Socket:
            shutdown_calls = []
            def shutdown(self, how):
                self.shutdown_calls.append(how)
        sock = Socket()
        self.conn.sock = sock
        self.response.before_read = lambda: self.timers[0].callback()
        self.failure("outcome_unknown")
        self.assertEqual(sock.shutdown_calls, [socket.SHUT_RDWR])
        self.assertTrue(self.timers[0].cancelled and self.conn.closed)

    def test_deadline_interrupts_response_after_connection_clears_socket_reference(self):
        class Socket:
            shutdown_calls = []
            def shutdown(self, how):
                self.shutdown_calls.append(how)
        sock = Socket()
        self.conn.sock = sock
        self.conn.after_response = lambda: setattr(self.conn, "sock", None)
        self.response.before_read = lambda: self.timers[0].callback()
        self.failure("outcome_unknown")
        self.assertIsNone(self.conn.sock)
        self.assertEqual(sock.shutdown_calls, [socket.SHUT_RDWR])

    def test_timeout_is_capped_and_does_not_add_a_retry_layer(self):
        self.exchange(timeout=9999)
        self.factory.assert_called_once_with("api.openai.com", "93.184.216.34", 443, transport.MAX_TIMEOUT)
        self.assertEqual(self.timers[0].interval, transport.MAX_TIMEOUT)
        self.assertEqual(len(self.conn.requests), 1)

    def test_invalid_time_budget_is_rejected_before_dns(self):
        for timeout in [0, -1, True, "60", float("nan"), float("inf")]:
            with self.subTest(timeout=timeout):
                self.failure("request_invalid", sent=False, timeout=timeout)
        self.dns.assert_not_called()
        self.assertEqual(self.sent, 0)

    def test_invalid_credentials_are_not_sent_or_echoed(self):
        for token in [None, "", "with space", "x\r\nInjected: x", "非ASCII", "x" * 65537]:
            with self.subTest(kind=type(token).__name__):
                self.failure("chatgpt_sign_in_required", sent=False, api_key=token)
        self.dns.assert_not_called()
        self.assertEqual(self.sent, 0)

    def test_invalid_model_and_inputs_fail_before_dns(self):
        for model in [None, "", " ", "line\nbreak", "x" * 201]:
            with self.subTest(model=model):
                self.failure("model_required", sent=False, model=model)
        for prompt in [None, "", {}, 42]:
            with self.subTest(prompt=prompt):
                self.failure("request_invalid", sent=False, prompt=prompt)
        self.failure("request_invalid", sent=False, system={"role": "system"})
        self.dns.assert_not_called()
        self.assertEqual(self.sent, 0)

    def test_request_input_size_limit_is_unsent(self):
        self.failure("request_too_large", sent=False, prompt="x" * MAX_RESPONSE)
        self.assertEqual(self.sent, 0)
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
