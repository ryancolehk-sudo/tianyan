"""Three explicit text protocols. No SDK retries, redirect following, tool calls, or remote URL fetching."""
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

MAX_RESPONSE = 2 * 1024 * 1024


class ModelFailure(Exception):
    def __init__(self, code, message, *, sent=False, retryable=False, status=None, retry_after=None):
        self.code, self.message, self.sent = code, message, sent
        self.retryable, self.status = retryable, status
        self.retry_after = retry_after
        super().__init__(message)


@dataclass
class Reply:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    models: list | None = None


def endpoint_parts(endpoint):
    try:
        parts = urlsplit(endpoint)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or "\\" in endpoint or any(ord(c) < 33 for c in endpoint)):
            raise ValueError()
        _ = parts.port
        return parts
    except ValueError:
        raise ModelFailure("endpoint_rejected", "模型地址须为不含凭据、查询参数或片段的公网 HTTPS 地址。") from None


def resolved_addresses(hostname, port, timeout=None):
    try:
        if timeout is None:
            rows = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        else:
            completed, outcome = threading.Event(), {}
            def resolve():
                try:
                    outcome["rows"] = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
                except OSError:
                    outcome["failed"] = True
                finally:
                    completed.set()
            thread = threading.Thread(target=resolve, daemon=True)
            thread.start()
            if not completed.wait(timeout):
                raise ModelFailure("dns_timeout", "接口地址解析超过本次时间预算，尚未发出请求。")
            if outcome.get("failed") or "rows" not in outcome:
                raise OSError()
            rows = outcome["rows"]
        addresses = list(dict.fromkeys(item[4][0] for item in rows))
    except OSError:
        raise ModelFailure("dns_failed", "模型地址解析失败，尚未发出请求。") from None
    if not addresses or any(not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast for address in addresses):
        raise ModelFailure("endpoint_rejected", "模型接口不能访问本机、内网、保留地址或云元数据地址。")
    return addresses


def validate_endpoint(endpoint, *, resolve=False):
    parts = endpoint_parts(endpoint)
    if resolve:
        resolved_addresses(parts.hostname, parts.port or 443)
    else:
        try:
            if not ipaddress.ip_address(parts.hostname).is_global or ipaddress.ip_address(parts.hostname).is_multicast:
                raise ModelFailure("endpoint_rejected", "模型接口不能访问本机、内网或保留地址。")
        except ValueError:
            if parts.hostname.lower() == "localhost" or parts.hostname.lower().endswith((".localhost", ".local", ".internal")):
                raise ModelFailure("endpoint_rejected", "模型接口不能访问本机或内网。") from None
    return parts


def route(endpoint, protocol, discovery=False):
    parts = endpoint_parts(endpoint)
    path = parts.path.rstrip("/")
    suffixes = ("/chat/completions", "/responses", "/messages", "/models")
    for suffix in suffixes:
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    suffix = "/models" if discovery else {"openai_chat": "/chat/completions", "openai_responses": "/responses", "anthropic_messages": "/messages"}[protocol]
    # Native Anthropic convention includes v1; arbitrary compatible service paths are retained.
    if protocol == "anthropic_messages" and not path:
        path = "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path + suffix, "", ""))


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, port, timeout):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self.pinned_address = address

    def connect(self):
        # Resolve once, validate every result, and pin the socket while retaining TLS hostname verification.
        remaining = getattr(self, "deadline", float("inf")) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        sock = socket.create_connection((self.pinned_address, self.port), min(self.timeout, remaining))
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
            remaining = getattr(self, "deadline", float("inf")) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            self.sock.settimeout(min(self.timeout, remaining))
            self.sock.do_handshake()
        except Exception:
            self.close()
            sock.close()
            raise


def retry_after(value, clock=None):
    if not isinstance(value, str) or not value.strip():
        return None
    clock = clock or datetime.now(UTC)
    value = value.strip()
    if value.isascii() and value.isdigit():
        # A long supplier cooldown stops this task; it must never wrap or turn into an immediate retry.
        seconds = min(int(value[:12]), 365 * 86400) if len(value) <= 12 else 365 * 86400
        return clock + timedelta(seconds=seconds)
    try:
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return None
        return max(clock, stamp.astimezone(UTC))
    except (ValueError, TypeError, OverflowError):
        return None


def exchange(endpoint, protocol, api_key, model, prompt=None, *, system=None, discovery=False, timeout=60, max_tokens=1200, on_send=lambda: None):
    deadline = time.monotonic() + timeout
    address = route(endpoint, protocol, discovery)
    parts = validate_endpoint(address)
    addresses = resolved_addresses(parts.hostname, parts.port or 443, timeout)
    if time.monotonic() >= deadline:
        raise ModelFailure("dns_timeout", "接口地址解析超过本次时间预算，尚未发出请求。")
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Tianyan/0.3"}
    if protocol == "anthropic_messages":
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = "Bearer " + api_key
    data = None
    if not discovery:
        if not model:
            raise ModelFailure("model_required", "请先填写或选择模型 ID。")
        if protocol == "openai_chat":
            # Reasoning models use the newer completion budget; compatible non-reasoning services commonly implement max_tokens.
            token_field = "max_completion_tokens" if model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")) else "max_tokens"
            messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
            data = {"model": model, "messages": messages, token_field: max_tokens, "stream": False}
        elif protocol == "openai_responses":
            data = {"model": model, "input": prompt, "max_output_tokens": max_tokens, "store": False, "stream": False}
            if system:
                data["instructions"] = system
        else:
            data = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "stream": False}
            if system:
                data["system"] = system
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    conn = PinnedHTTPSConnection(parts.hostname, addresses[0], parts.port or 443, timeout)
    conn.deadline = deadline
    sent = False
    expired = threading.Event()

    def abort():
        expired.set()
        try:
            active = getattr(conn, "sock", None)
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
        if expired.is_set() or time.monotonic() >= deadline:
            raise ModelFailure("connection_timeout", "连接准备已超时，尚未发出模型请求。")
        on_send()
        sent = True
        if expired.is_set() or time.monotonic() >= deadline:
            raise ModelFailure("outcome_unknown", "请求边界超时，为避免重复计费未重发。", sent=True)
        conn.request("GET" if discovery else "POST", parts.path or "/", body=payload, headers=headers)
        response = conn.getresponse()
        # Credentials never follow redirects, even same-site redirects (which may conceal a downgrade).
        if 300 <= response.status < 400:
            raise ModelFailure("redirect_rejected", "接口返回重定向，请填写最终 HTTPS 接口地址。", sent=True, status=response.status)
        if response.status >= 400:
            code = {401: "invalid_api_key", 403: "permission_denied", 404: "model_or_endpoint_not_found", 429: "rate_limited"}.get(response.status, "upstream_unavailable" if response.status >= 500 else "upstream_rejected")
            messages = {"invalid_api_key": "密钥无效或已过期。", "permission_denied": "账号没有所选模型权限。", "model_or_endpoint_not_found": "模型或协议路径不存在。", "rate_limited": "供应商请求受限。", "upstream_unavailable": "模型服务暂时不可用。", "upstream_rejected": "模型服务拒绝请求，请检查协议和参数。"}
            header = response.getheader("Retry-After") if hasattr(response, "getheader") else None
            raise ModelFailure(code, messages[code], sent=True, retryable=response.status == 429 or 500 <= response.status < 600,
                               status=response.status, retry_after=retry_after(header))
        if response.getheader("Content-Encoding", "identity") not in {"identity", ""}:
            raise ModelFailure("response_encoding_unsupported", "模型返回了不支持的压缩格式。", sent=True)
        raw = response.read(MAX_RESPONSE + 1)
        if expired.is_set() or time.monotonic() >= deadline:
            raise ModelFailure("outcome_unknown", "请求超过本次时间预算，为避免重复计费未自动重发。", sent=True)
        if len(raw) > MAX_RESPONSE:
            raise ModelFailure("response_too_large", "模型响应超过本机允许的大小。", sent=True)
        try:
            body = json.loads(raw)
            return parse_reply(body, protocol, discovery=discovery, secret=api_key)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise ModelFailure("invalid_response", "模型返回格式不符合所选协议。", sent=True) from None
    except ModelFailure:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException):
        raise ModelFailure("outcome_unknown" if sent else "connection_failed", "请求结果未知，为避免重复计费未自动重发。" if sent else "接口连接失败。", sent=sent) from None
    finally:
        timer.cancel()
        conn.close()


def token_count(value):
    return value if type(value) is int and 0 <= value <= 1000000000 else None


def parse_reply(body, protocol, *, discovery=False, secret=""):
    if not isinstance(body, dict) or body.get("error"):
        raise ValueError("invalid response")
    if discovery:
        rows = body.get("data")
        if not isinstance(rows, list):
            raise ValueError("invalid model list")
        models = []
        for item in rows[:1000]:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and 0 < len(item["id"]) <= 200:
                identity = item["id"].replace(secret, "[已隐藏]") if secret else item["id"]
                name = str(item.get("display_name") or identity)
                models.append({"id": identity, "name": (name.replace(secret, "[已隐藏]") if secret else name)[:200]})
        return Reply(text="", models=models)
    usage = body.get("usage") or {}
    if protocol == "openai_chat":
        message = body["choices"][0]["message"]
        text = message.get("content")
        if isinstance(text, list):
            text = "\n".join(item.get("text", "") for item in text if isinstance(item, dict) and item.get("type") == "text")
        if message.get("tool_calls") or body["choices"][0].get("finish_reason") == "length":
            raise ValueError("incomplete or tool output")
        incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
    elif protocol == "openai_responses":
        if body.get("status") not in {None, "completed"}:
            raise ValueError("incomplete response")
        if any(output.get("type") not in {"message", "reasoning"} for output in body.get("output", [])):
            raise ValueError("non-text output")
        text = "\n".join(part["text"] for output in body.get("output", []) if output.get("type") == "message"
                         for part in output.get("content", []) if part.get("type") == "output_text")
        incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
    else:
        if body.get("stop_reason") in {"max_tokens", "tool_use", "pause_turn"}:
            raise ValueError("incomplete or tool output")
        if any(item.get("type") == "tool_use" for item in body["content"]):
            raise ValueError("tool output")
        text = "\n".join(item["text"] for item in body["content"] if item.get("type") == "text")
        incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty response")
    text = text.strip()
    if secret:
        text = text.replace(secret, "[已隐藏]")
    return Reply(text=text, input_tokens=token_count(incoming), output_tokens=token_count(outgoing))
