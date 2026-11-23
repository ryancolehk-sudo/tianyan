import base64
import hashlib
import hmac
import json
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.auth import APIError, require_instance
from app.config import settings
from app.db import engine
from app.evidence.models import IdempotencyKey
from app.http import envelope


def normalized(value):
    if isinstance(value, BaseModel):
        return normalized(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, (UUID, date)):
        return str(value)
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalized(item) for item in value]
    return value


def canonical(value):
    return json.dumps(normalized(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def content_hash(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def lock(db, namespace):
    key = int.from_bytes(hashlib.sha256(namespace.encode()).digest()[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def now(db):
    return db.scalar(select(func.clock_timestamp()))


def write_once(request, owner, key, body, create, *, status=201, as_of_time=None, validate_replay=None):
    route = request.url.path
    fingerprint = content_hash(body)
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        require_instance(db)
        lock(db, f"idempotency:{owner.id}:POST:{route}:{key}")
        identity = (owner.id, "POST", route, key)
        existing = db.get(IdempotencyKey, identity)
        current = now(db)
        if existing and existing.expires_at > current:
            if existing.payload_hash != fingerprint:
                raise APIError(409, "idempotency_conflict", "本次操作编号已用于其他内容，请重新提交。")
            if validate_replay is not None:
                validate_replay(db, existing.response_json)
            request.state.replayed_request_id = existing.response_json["meta"]["request_id"]
            return JSONResponse(status_code=existing.status_code, content=existing.response_json,
                                headers={"Idempotency-Replayed": "true"})
        if existing:
            db.delete(existing)
            db.flush()
        data, quality, reasons = create(db)
        response = jsonable_encoder(envelope(request, data, quality, reasons, as_of_time=as_of_time))
        db.add(IdempotencyKey(owner_id=owner.id, method="POST", route=route, key=key,
            payload_hash=fingerprint, response_json=response, status_code=status, expires_at=current + timedelta(hours=24)))
    return JSONResponse(status_code=status, content=response)


def cursor_scope(owner_id, filters):
    return content_hash({"owner": owner_id, "mode": settings().app_mode, "filters": filters})


def cursor_key():
    # Domain-separated from the instance's random database credential. Never returned/logged.
    return hashlib.sha256(("tianyan.cursor.v1\0" + settings().database_url).encode()).digest()


def encode_cursor(row, scope, field):
    raw = canonical({"t": getattr(row, field), "i": row.id, "s": scope})
    return base64.urlsafe_b64encode(raw + hmac.digest(cursor_key(), raw, "sha256")).decode().rstrip("=")


def decode_cursor(value, scope):
    if not value:
        return None
    try:
        packed = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(packed).decode().rstrip("=") != value:
            raise ValueError()
        raw, signature = packed[:-32], packed[-32:]
        if not hmac.compare_digest(signature, hmac.digest(cursor_key(), raw, "sha256")):
            raise ValueError()
        body = json.loads(raw)
        if body["s"] != scope:
            raise ValueError()
        if type(body["t"]) is int:
            if not 1 <= body["t"] <= 2147483647:
                raise ValueError()
            position = body["t"]
        elif isinstance(body["t"], str):
            position = datetime.fromisoformat(body["t"].replace("Z", "+00:00"))
            if position.tzinfo is None:
                raise ValueError()
        else:
            raise ValueError()
        return position, UUID(body["i"])
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise APIError(422, "invalid_cursor", "列表位置已失效，请从第一页重新读取。") from None
