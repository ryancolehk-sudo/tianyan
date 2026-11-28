"""The documented ChatGPT plan-usage route, separate from API-key protocols.

Only public OpenAI HTTPS endpoints are reachable. The caller owns retry policy and
OAuth refresh; this transport performs exactly one physical request. In particular,
partial streamed text is never a successful response or a reason to resend.
"""
import codecs
import http.client
import json
import math
import re
import socket
import threading
import time

from app.ai.protocols import (
    MAX_RESPONSE,
    ModelFailure,
    PinnedHTTPSConnection,
    Reply,
    parse_reply,
    resolved_addresses,
    retry_after,
)

HOST = "api.openai.com"
PORT = 443
MAX_TIMEOUT = 180
MAX_EVENTS = 20000
MAX_MODELS = 1000
MAX_ERROR = 32768

# Only documented/static codes enter the ledger. Upstream diagnostic prose can
# contain credentials or input text and must never become an exception message.
_ERRORS = {
    "subscription_sharing_user_not_eligible": (
        "此账号、工作区或政策暂不支持使用 ChatGPT 订阅额度。", False),
    "subscription_sharing_usage_limit_exceeded": (
        "此应用的 ChatGPT 订阅用量已达上限，请在 ChatGPT 设置的“用量”中检查后再试；不会自动切换 API 计费。", False),
    "subscription_sharing_usage_unavailable": (
        "暂时无法确认 ChatGPT 订阅用量，请稍后再试；已保留登录。", True),
    "subscription_sharing_unsupported_capability": (
        "ChatGPT 订阅接口不支持本次输入、模型或参数，请检查配置。", False),
    "subscription_sharing_route_not_supported": (
        "ChatGPT 订阅接口不支持本次操作，请检查集成配置。", False),
    "subscription_sharing_invalid_user": (
        "无法验证 ChatGPT 订阅账号，请检查授权；确认授权失效后重新登录。", False),
    "chatpass_v2_scope_not_authorized": (
        "当前授权未允许使用 ChatGPT 订阅额度，请检查并重新授权。", False),
    "chatpass_v2_invalid_authorization_context": (
        "ChatGPT 订阅授权上下文无效，请检查集成授权配置。", False),
    "subscription_sharing_user_unavailable": (
        "暂时无法取得 ChatGPT 账号或工作区信息，请稍后再试；已保留登录。", True),
}
_PARAMS = frozenset({
    "model", "input", "instructions", "tools", "tool_choice", "service_tier",
    "store", "stream", "max_output_tokens", "temperature", "previous_response_id",
    "background", "conversation", "max_tool_calls", "metadata", "moderation",
    "multi_agent", "prompt", "prompt_cache_retention", "safety_identifier",
    "top_logprobs", "top_p", "truncation", "user",
})


def _failure(code, message, *, status=None, retryable=False):
    return ModelFailure(code, message, sent=True, status=status, retryable=retryable)


def _invalid():
    return _failure("invalid_response", "ChatGPT 返回的格式不符合订阅接口协议。")


def _unknown():
    return _failure("outcome_unknown", "ChatGPT 请求结果未知，为避免重复消耗订阅额度未自动重发。")


def _ensure_time(deadline, expired):
    if expired.is_set() or time.monotonic() >= deadline:
        raise _unknown()


def _safe_request_id(response, secret):
    value = response.getheader("x-request-id", "")
    if (isinstance(value, str) and re.fullmatch(r"req_[A-Za-z0-9_-]{4,96}", value)
            and secret not in value):
        return value
    return None


def _upstream_failure(body, response, secret, *, streamed=False):
    status = response.status
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict) and isinstance(body, dict):
        error = body if body.get("type") == "error" else None
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, str):
        code = None
    safe_code = code if (code and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code) and secret not in code) else None
    if isinstance(code, str) and code in _ERRORS and secret not in code:
        message, retryable = _ERRORS[code]
    elif code == "model_not_found":
        code, message, retryable = "model_or_endpoint_not_found", "当前 ChatGPT 账号无法使用所选模型，请刷新模型列表。", False
    elif status == 401:
        code, message, retryable = "chatgpt_sign_in_required", "ChatGPT 身份或订阅授权未被接受，请检查所选账号和授权。", False
    elif status == 403:
        code, message, retryable = "chatgpt_permission_denied", "账号、工作区政策或服务地区不允许本次 ChatGPT 请求。", False
    elif status == 404:
        code, message, retryable = "model_or_endpoint_not_found", "当前 ChatGPT 账号无法使用所选模型，请刷新模型列表。", False
    elif status == 429 or code in {"insufficient_quota", "rate_limit_exceeded"}:
        # Do not infer a quota reset time or another billing path from a generic 429.
        code, message, retryable = "chatgpt_usage_restricted", "ChatGPT 请求或用量受限，请检查订阅用量后再试；不会自动切换 API 计费。", False
    elif status >= 500 and not streamed:
        code, message, retryable = "chatgpt_direct_unavailable", "ChatGPT 订阅服务暂时不可用，请稍后再试；已保留登录。", True
    else:
        code, message, retryable = "chatgpt_request_failed", "ChatGPT 明确拒绝或终止了本次请求，请检查模型与订阅权限。", False
    failure = _failure(code, message, status=status, retryable=retryable)
    # Keep useful structured diagnostics, never error.message/detail or the raw body.
    failure.request_id = _safe_request_id(response, secret)
    failure.upstream_code = safe_code
    param = error.get("param") if isinstance(error, dict) else None
    failure.param = param if isinstance(param, str) and param in _PARAMS and secret not in param else None
    failure.body_shape = (
        "error" if isinstance(error, dict) else "detail" if isinstance(body, dict) and "detail" in body else "other"
    )
    if retryable:
        failure.retry_after = retry_after(response.getheader("Retry-After"))
    return failure


def _chunks(response, deadline, expired, *, limit=MAX_RESPONSE):
    total = 0
    while True:
        _ensure_time(deadline, expired)
        # read1 does not wait to fill a large buffer after a terminal SSE event.
        chunk = response.read1(min(8192, limit + 1 - total))
        _ensure_time(deadline, expired)
        if not chunk:
            return
        total += len(chunk)
        if total > limit:
            raise _failure("response_too_large", "ChatGPT 响应超过本机允许的大小。")
        yield chunk


def _read_json(response, deadline, expired, *, limit=MAX_RESPONSE):
    raw = b"".join(_chunks(response, deadline, expired, limit=limit))
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None


def _catalog(body, secret):
    if (not isinstance(body, dict) or body.get("error") or not isinstance(body.get("models"), list)
            or len(body["models"]) > MAX_MODELS):
        raise _invalid()
    models = []
    for item in body["models"]:
        if not isinstance(item, dict):
            raise _invalid()
        if item.get("visibility") != "list":
            continue
        slug, name = item.get("slug"), item.get("display_name")
        if (not isinstance(slug, str) or not 0 < len(slug) <= 200 or not slug.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in slug)
                or not isinstance(name, str) or not name.strip() or len(name) > 1000):
            raise _invalid()
        models.append({"id": slug.replace(secret, "[已隐藏]"), "name": name.replace(secret, "[已隐藏]")[:200]})
    return Reply(text="", models=models)


def _completed(body, secret):
    if not isinstance(body, dict) or body.get("status") != "completed":
        raise _invalid()
    # Responses may return tools even without an execution request; never accept
    # or execute those outputs as this text-only integration's successful result.
    output = body.get("output")
    if not isinstance(output, list):
        raise _invalid()
    for item in output:
        if not isinstance(item, dict) or item.get("type") not in {"message", "reasoning"}:
            raise _invalid()
        if item.get("type") == "message":
            content = item.get("content")
            if (not isinstance(content, list) or any(not isinstance(part, dict)
                    or part.get("type") != "output_text" or not isinstance(part.get("text"), str) for part in content)):
                raise _invalid()
    try:
        return parse_reply(body, "openai_responses", secret=secret)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise _invalid() from None


def _event(event_name, data, response, secret):
    if data == "[DONE]":
        # [DONE] is not the Responses completion proof.
        raise _unknown()
    try:
        body = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None
    if not isinstance(body, dict) or not isinstance(body.get("type"), str):
        raise _invalid()
    kind = body["type"]
    if event_name not in {"", "message", kind}:
        raise _invalid()
    if kind == "response.completed":
        return _completed(body.get("response"), secret)
    if kind == "response.failed":
        value = body.get("response")
        raise _upstream_failure(value, response, secret, streamed=True)
    if kind == "error":
        raise _upstream_failure(body, response, secret, streamed=True)
    if kind == "response.incomplete":
        raise _failure("response_incomplete", "ChatGPT 未完成本次生成，部分文本未作为结果保存。", status=response.status)
    return None


def _stream(response, secret, deadline, expired):
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    pending, event_name, data_lines, events = "", "", [], 0
    try:
        for chunk in _chunks(response, deadline, expired):
            pending += decoder.decode(chunk)
            while True:
                ends = [idx for idx in (pending.find("\n"), pending.find("\r")) if idx >= 0]
                if not ends:
                    break
                end = min(ends)
                # A CR/LF separator may straddle network chunks.
                if pending[end] == "\r" and end == len(pending) - 1:
                    break
                line = pending[:end]
                width = 2 if pending[end:end + 2] == "\r\n" else 1
                pending = pending[end + width:]
                if line == "":
                    if data_lines:
                        events += 1
                        if events > MAX_EVENTS:
                            raise _failure("response_too_large", "ChatGPT 返回事件数量超过本机限制。")
                        reply = _event(event_name, "\n".join(data_lines), response, secret)
                        if reply is not None:
                            return reply
                    event_name, data_lines = "", []
                elif not line.startswith(":"):
                    field, separator, value = line.partition(":")
                    value = value[1:] if value.startswith(" ") else value
                    if field == "data":
                        data_lines.append(value if separator else "")
                    elif field == "event":
                        event_name = value if separator else ""
        decoder.decode(b"", final=True)
        # SSE also permits a CR-only blank delimiter at EOF. A lone final CR
        # after a data line terminates that line, not an event, so it stays unknown.
        if pending == "\r" and data_lines:
            events += 1
            if events > MAX_EVENTS:
                raise _failure("response_too_large", "ChatGPT 返回事件数量超过本机限制。")
            reply = _event(event_name, "\n".join(data_lines), response, secret)
            if reply is not None:
                return reply
    except UnicodeError:
        raise _invalid() from None
    # An unterminated final event, a [DONE] marker, or deltas without completed
    # cannot prove what the provider charged or completed.
    raise _unknown()


def exchange(api_key, model, prompt=None, *, system=None, discovery=False, timeout=60,
             max_tokens=1200, on_send=lambda: None):
    """Use an OAuth access token, never an arbitrary endpoint or existing login file.

    max_tokens is accepted for caller compatibility only. The subscription route
    forbids max_output_tokens and equivalent hidden request-field substitutions.
    """
    if (not isinstance(api_key, str) or not api_key or len(api_key) > 65536
            or any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
        raise ModelFailure("chatgpt_sign_in_required", "请先登录并授权使用 ChatGPT 订阅额度。")
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise ModelFailure("request_invalid", "ChatGPT 请求的时间预算无效，尚未发出请求。")
    timeout = min(timeout, MAX_TIMEOUT)
    if not discovery:
        if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in model):
            raise ModelFailure("model_required", "请先选择当前 ChatGPT 账号可用的模型。")
        if not isinstance(prompt, str) or not prompt.strip() or (system is not None and not isinstance(system, str)):
            raise ModelFailure("request_invalid", "ChatGPT 文本输入无效，尚未发出请求。")
    deadline = time.monotonic() + timeout
    addresses = resolved_addresses(HOST, PORT, timeout)
    if time.monotonic() >= deadline:
        raise ModelFailure("dns_timeout", "ChatGPT 地址解析超过本次时间预算，尚未发出请求。")
    payload = None
    if not discovery:
        body = {"model": model, "input": [{"role": "user", "content": prompt}], "store": False, "stream": True}
        if system:
            body["instructions"] = system
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_RESPONSE:
            raise ModelFailure("request_too_large", "ChatGPT 文本输入超过本机允许的大小，尚未发出请求。")
    conn = PinnedHTTPSConnection(HOST, addresses[0], PORT, timeout)
    conn.deadline = deadline
    expired = threading.Event()
    sent = False
    active_socket = None

    def abort():
        expired.set()
        try:
            # HTTPConnection can clear conn.sock for Connection: close while
            # HTTPResponse still owns its file descriptor. Keep the TLS socket
            # reference so the deadline can interrupt that response read too.
            active = active_socket or getattr(conn, "sock", None)
            if active:
                active.shutdown(socket.SHUT_RDWR)
            conn.close()
        except OSError:
            pass

    timer = threading.Timer(max(.001, deadline - time.monotonic()), abort)
    timer.daemon = True
    timer.start()
    try:
        conn.connect()
        active_socket = getattr(conn, "sock", None)
        if expired.is_set() or time.monotonic() >= deadline:
            raise ModelFailure("connection_timeout", "连接 ChatGPT 超时，尚未发出请求。")
        on_send()
        sent = True
        _ensure_time(deadline, expired)
        conn.request("GET" if discovery else "POST", "/v1/models" if discovery else "/v1/responses", body=payload,
                     headers={"Authorization": "Bearer " + api_key, "Accept": "application/json" if discovery else "text/event-stream",
                              "Content-Type": "application/json", "User-Agent": "Tianyan/0.3"})
        response = conn.getresponse()
        if 300 <= response.status < 400:
            raise _failure("redirect_rejected", "ChatGPT 接口返回重定向，已停止请求并保护凭据。", status=response.status)
        if not 200 <= response.status < 300:
            try:
                body = _read_json(response, deadline, expired, limit=MAX_ERROR)
            except (ModelFailure, OSError, http.client.HTTPException):
                # The status already proves refusal; an unreadable diagnostic body
                # must not hide the status or expose its potentially sensitive text.
                body = None
            raise _upstream_failure(body, response, api_key)
        if response.getheader("Content-Encoding", "identity").lower().strip() not in {"", "identity"}:
            raise _failure("response_encoding_unsupported", "ChatGPT 返回了不支持的压缩格式。")
        media_type = response.getheader("Content-Type", "").split(";", 1)[0].lower().strip()
        if discovery:
            if media_type != "application/json":
                raise _invalid()
            return _catalog(_read_json(response, deadline, expired), api_key)
        if media_type != "text/event-stream":
            raise _invalid()
        return _stream(response, api_key, deadline, expired)
    except ModelFailure as failure:
        if discovery and failure.code == "outcome_unknown":
            raise _failure("model_catalog_unavailable", "未收到完整的 Codex 模型目录。可刷新目录或填写模型 ID。") from None
        raise
    except (OSError, http.client.HTTPException):
        if discovery and sent:
            raise _failure("model_catalog_unavailable", "未收到完整的 Codex 模型目录。可刷新目录或填写模型 ID。") from None
        raise _unknown() if sent else ModelFailure("connection_failed", "无法连接 ChatGPT，尚未发出请求。") from None
    finally:
        timer.cancel()
        try:
            conn.close()
        except OSError:
            pass
