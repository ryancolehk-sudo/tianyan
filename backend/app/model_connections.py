"""Read-only compatibility for environment model declarations.

Only an environment variable *name* is stored in this contract. Credentials are
resolved immediately before a request and never belong to a public DTO.
"""

import ipaddress
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_FORBIDDEN_EXTRA = {
    "model", "messages", "stream", "stream_options", "response_format", "n",
    "authorization", "api_key", "apikey", "access_token", "credential", "credentials",
    "password", "secret", "headers", "base_url", "api_base", "endpoint", "url",
    "tools", "tool_choice", "functions", "function_call",
}


class ModelConnectionError(ValueError):
    """A stable, non-sensitive configuration error suitable for status output."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _boolean(value: str, name: str) -> bool:
    value = value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ModelConnectionError(f"invalid_{name}")


def _validate_extras(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise ModelConnectionError("model_extra_too_deep")
    if isinstance(value, dict):
        if len(value) > 64:
            raise ModelConnectionError("model_extra_too_large")
        for key, child in value.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 100:
                raise ModelConnectionError("invalid_model_extra")
            normalized = key.lower().replace("-", "_")
            if normalized in _FORBIDDEN_EXTRA or normalized.endswith(("_api_key", "_password", "_secret")):
                raise ModelConnectionError("reserved_model_extra")
            _validate_extras(child, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 100:
            raise ModelConnectionError("model_extra_too_large")
        for child in value:
            _validate_extras(child, depth=depth + 1)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise ModelConnectionError("invalid_model_extra")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ModelConnectionError("invalid_model_extra")


@dataclass(frozen=True)
class ModelConnection:
    connection_id: str
    base_url: str
    model: str
    credential_ref: str = "JL_MODEL_API_KEY"
    enabled: bool = False
    timeout_seconds: float = 30.0
    json_mode: bool = True
    extra_body: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if not isinstance(self.connection_id, str) or not _NAME.fullmatch(self.connection_id):
            raise ModelConnectionError("invalid_model_connection_id")
        if not isinstance(self.model, str) or not 1 <= len(self.model) <= 200 or any(ord(c) < 33 for c in self.model):
            raise ModelConnectionError("invalid_model_name")
        if not isinstance(self.credential_ref, str) or not _ENV_NAME.fullmatch(self.credential_ref):
            raise ModelConnectionError("invalid_model_credential_ref")
        if type(self.enabled) is not bool or type(self.json_mode) is not bool:
            raise ModelConnectionError("invalid_model_boolean")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or not 1 <= self.timeout_seconds <= 120:
            raise ModelConnectionError("invalid_model_timeout")
        if not isinstance(self.base_url, str) or len(self.base_url) > 2048:
            raise ModelConnectionError("invalid_model_base_url")
        try:
            parsed = urlsplit(self.base_url)
            _ = parsed.port
        except ValueError:
            raise ModelConnectionError("invalid_model_base_url") from None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelConnectionError("invalid_model_base_url")
        if any(c.isspace() or ord(c) < 32 for c in self.base_url):
            raise ModelConnectionError("invalid_model_base_url")
        if parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname == "localhost"
            if not loopback:
                raise ModelConnectionError("model_requires_https")
        if not isinstance(self.extra_body, dict):
            raise ModelConnectionError("invalid_model_extra")
        _validate_extras(self.extra_body)
        if len(json.dumps(self.extra_body, ensure_ascii=False, allow_nan=False)) > 16_384:
            raise ModelConnectionError("model_extra_too_large")

    @property
    def completion_url(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


def get_model_connection() -> ModelConnection | None:
    """Read safe configuration. No key resolution and no network side effects."""
    base_url = os.getenv("JL_MODEL_BASE_URL", "").strip()
    model = os.getenv("JL_MODEL_NAME", "").strip()
    enabled = _boolean(os.getenv("JL_MODEL_ENABLED", "false"), "model_enabled")
    if not base_url and not model and not enabled:
        return None
    if not base_url or not model:
        raise ModelConnectionError("incomplete_model_connection")
    try:
        timeout = float(os.getenv("JL_MODEL_TIMEOUT_SECONDS", "30"))
        extras = json.loads(os.getenv("JL_MODEL_EXTRA_JSON", "{}"))
    except (ValueError, TypeError):
        raise ModelConnectionError("invalid_model_configuration") from None
    return ModelConnection(
        connection_id=os.getenv("JL_MODEL_CONNECTION_ID", "news-default").strip(),
        base_url=base_url,
        model=model,
        credential_ref=os.getenv("JL_MODEL_CREDENTIAL_REF", "JL_MODEL_API_KEY").strip(),
        enabled=enabled,
        timeout_seconds=timeout,
        json_mode=_boolean(os.getenv("JL_MODEL_JSON_MODE", "true"), "model_json_mode"),
        extra_body=extras,
    )


def resolve_model_credential(connection: ModelConnection) -> str:
    """Resolve server environment only. Never include the value in an exception."""
    value = os.getenv(connection.credential_ref, "")
    if not value:
        raise ModelConnectionError("model_credential_missing")
    if len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ModelConnectionError("invalid_model_credential")
    return value


def virtual_connections(db) -> list[dict]:
    """Expose a valid legacy declaration once, disabled and untested, without writing or returning credentials."""
    from uuid import NAMESPACE_URL, uuid5

    from app.ai.models import Connection

    try:
        connection = get_model_connection()
    except ModelConnectionError:
        return []
    if connection is None:
        return []
    identity = uuid5(NAMESPACE_URL, "tianyan/model-environment/" + connection.connection_id)
    if db.get(Connection, identity) is not None:
        # An archived declaration stays archived; environment discovery must not resurrect it.
        return []
    configured, error = True, None
    try:
        resolve_model_credential(connection)
    except ModelConnectionError as failure:
        configured, error = False, failure.code
    return [{"id": identity, "name": "旧环境模型 · " + connection.connection_id, "kind": "model",
             "template": "custom_model", "endpoint": connection.base_url.rstrip("/"), "protocol": "openai_chat",
             "model": connection.model, "enabled": False, "revision": 1, "credential_configured": configured,
             "credential_origin": "environment", "credential_error_code": error,
             "known_env_ref": "env:model:" + connection.credential_ref,
             "source_ids": [], "capabilities": [], "options": {"timeout_seconds": max(5, min(120, int(math.ceil(connection.timeout_seconds)))),
                 "max_output_tokens": 1200}, "last_test": None, "tested_revision": None}]


def connection_status(db=None) -> dict:
    """News period enhancements remain pending, irrespective of legacy environment switches."""
    if db is None:
        from sqlalchemy.orm import Session

        from app.db import engine
        with Session(engine()) as local:
            return connection_status(local)
    from app.ai.models import AiSettings, Connection

    config = db.get(AiSettings, 1)
    connection = db.get(Connection, config.default_connection_id) if config and config.default_connection_id else None
    return {"state": "disabled", "connection_id": str(connection.id) if connection and not connection.archived else None,
            "model": connection.model if connection and not connection.archived else None,
            "reason": "period_digest_not_implemented"}
