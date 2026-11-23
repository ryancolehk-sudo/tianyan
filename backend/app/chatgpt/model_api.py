"""Owner-scoped model selection and explicit probes for official ChatGPT plan connections."""
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field, StrictBool, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.attempts import attempt_record, perform
from app.ai.models import Connection
from app.ai.schemas import ProbeResult
from app.auth import APIError, csrf, require_instance, require_role
from app.chatgpt.models import ChatGPTAccount
from app.chatgpt.schemas import ChatGPTAccountView
from app.chatgpt.service import access_token, account_view, get_owned_account, require_available
from app.db import engine
from app.evidence.api import business_key
from app.evidence.common import content_hash, lock, write_once
from app.evidence.models import IdempotencyKey
from app.http import envelope
from app.models import AuditEvent, User
from app.schemas import DTO, Envelope, Error

router = APIRouter()
ERRORS = {status: {"model": Error} for status in (401, 403, 404, 409, 422, 429, 503)}


class ChatGPTAccountPatch(DTO):
    expected_revision: int = Field(ge=1)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    use_for_ai: StrictBool | None = None

    @field_validator("model")
    @classmethod
    def safe_model(cls, value):
        if value is not None:
            value = value.strip()
            if not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise ValueError("invalid model identifier")
        return value


def authorized(account):
    require_available()
    if (account.state != "connected" or not account.secret_ref
            or not {"resource.invoke", "chatgpt.tokens.use.direct"} <= set(account.scopes or [])):
        raise APIError(409, "chatgpt_plan_not_authorized", "尚未授权 ChatGPT 套餐使用，或连接需要重新登录。")


def revise(connection, *, preserve_test=False):
    old = connection.revision
    connection.revision += 1
    connection.updated_at = datetime.now(UTC)
    if preserve_test and connection.tested_revision == old and connection.last_test:
        connection.tested_revision = connection.revision
        connection.last_test = {**connection.last_test, "revision": connection.revision}
    else:
        connection.last_test, connection.tested_revision = None, None


def configure(db, user, account_id, body, request_id):
    lock(db, f"chatgpt:selection:{user.id}")
    account = get_owned_account(db, user.id, account_id)
    db.refresh(account, with_for_update=True)
    if body.expected_revision != account.revision:
        raise APIError(409, "configuration_changed", "本人 ChatGPT 配置已变化，请刷新后再修改。")
    connection = db.get(Connection, account.connection_id)
    if connection is None or connection.owner_id != user.id or connection.archived:
        raise APIError(409, "chatgpt_connection_unavailable", "当前 ChatGPT 连接不可用，请重新授权。")
    changed = False
    if "model" in body.model_fields_set and body.model != connection.model:
        if not body.model:
            raise APIError(422, "model_required", "请选择或填写模型 ID。")
        connection.model, connection.enabled = body.model, False
        revise(connection)
        changed = True
    if body.use_for_ai is not None:
        if body.use_for_ai:
            authorized(account)
            if (not connection.model or connection.tested_revision != connection.revision
                    or not connection.last_test or connection.last_test.get("status") != "success"):
                raise APIError(409, "test_required", "请先完成所选模型的真实生成测试，再启用本人 ChatGPT 套餐。")
            others = db.scalars(select(ChatGPTAccount).where(ChatGPTAccount.owner_id == user.id,
                 ChatGPTAccount.id != account.id, ChatGPTAccount.use_for_ai.is_(True)).with_for_update()).all()
            for previous in others:
                previous.use_for_ai, previous.revision = False, previous.revision + 1
                previous.updated_at = datetime.now(UTC)
                old_connection = db.get(Connection, previous.connection_id)
                if old_connection:
                    old_connection.enabled = False
                    revise(old_connection, preserve_test=True)
            # Flush deselection first if a partial unique index enforces one selection per owner.
            db.flush()
        if account.use_for_ai != body.use_for_ai or connection.enabled != body.use_for_ai:
            account.use_for_ai, connection.enabled = body.use_for_ai, body.use_for_ai
            revise(connection, preserve_test=True)
            changed = True
    if changed:
        account.revision += 1
        account.updated_at = datetime.now(UTC)
        db.add(AuditEvent(actor_id=user.id, action="chatgpt.model.configure", request_id=request_id,
                         resource_type="chatgpt_account", resource_id=account.id,
                         changed_field_names=sorted(body.model_fields_set - {"expected_revision"})))
    db.flush()
    return account_view(db, account), "complete", []


@router.patch("/api/v1/chatgpt/accounts/{account_id}", response_model=Envelope[ChatGPTAccountView],
              responses=ERRORS, operation_id="updateChatGPTAccount")
def update_account(account_id: uuid.UUID, body: ChatGPTAccountPatch, request: Request,
                   identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin", "researcher")
    return write_once(request, identity[0], key, body,
                      lambda db: configure(db, identity[0], account_id, body, request.state.request_id), status=200)


def probe(request, user, account_id, key, *, discovery):
    identity = (user.id, "POST", request.url.path, key)
    fingerprint = content_hash({"account_id": str(account_id), "discovery": discovery})
    # OAuth rotation uses a durable latch and owns its transaction boundaries.
    # Finish it before the idempotency/probe transaction can contain writes.
    with Session(engine(), expire_on_commit=False) as auth_db:
        require_instance(auth_db)
        prior = auth_db.get(IdempotencyKey, identity)
        if prior and prior.expires_at > datetime.now(UTC):
            if prior.payload_hash != fingerprint:
                raise APIError(409, "idempotency_conflict", "本次操作编号已用于其他请求。")
            return JSONResponse(status_code=prior.status_code, content=prior.response_json,
                                headers={"Idempotency-Replayed": "true"})
        account = get_owned_account(auth_db, user.id, account_id)
        authorized(account)
        token = access_token(auth_db, account)
        auth_db.commit()
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        require_instance(db)
        lock(db, f"idempotency:{user.id}:POST:{request.url.path}:{key}")
        prior = db.get(IdempotencyKey, identity)
        clock = datetime.now(UTC)
        if prior and prior.expires_at > clock:
            if prior.payload_hash != fingerprint:
                raise APIError(409, "idempotency_conflict", "本次操作编号已用于其他请求。")
            return JSONResponse(status_code=prior.status_code, content=prior.response_json,
                                headers={"Idempotency-Replayed": "true"})
        if prior:
            db.delete(prior)
            db.flush()
        account = get_owned_account(db, user.id, account_id)
        authorized(account)
        connection = db.get(Connection, account.connection_id)
        if not connection or connection.owner_id != user.id or connection.archived:
            raise APIError(409, "chatgpt_connection_unavailable", "本人 ChatGPT 连接不可用。")
        if not discovery and not connection.model:
            raise APIError(422, "model_required", "请先选择或填写模型 ID。")
        revision = connection.revision
        attempt = attempt_record(user.id, connection, "discover" if discovery else "probe")
        db.add(attempt)
        attempt_id = attempt.id
        pending = jsonable_encoder(envelope(request, {"status": "running", "attempt_id": attempt_id,
                          "message": "请求处理中；相同操作编号不会重复发出。", "request_count": 0}))
        db.add(IdempotencyKey(owner_id=user.id, method="POST", route=request.url.path, key=key,
            payload_hash=fingerprint, response_json=pending, status_code=202, expires_at=clock + timedelta(hours=24)))

    def before_send(db):
        current_user = db.get(User, user.id)
        current = get_owned_account(db, user.id, account_id)
        current_connection = db.get(Connection, current.connection_id)
        authorized(current)
        if (not current_user or not current_user.enabled or current_user.role not in {"admin", "researcher"}
                or current_connection.archived or current_connection.revision != revision):
            raise APIError(409, "configuration_changed", "配置或账号权限已变化，尚未发出模型请求。")

    reply, failure, elapsed = perform(attempt_id, connection, token, discovery=discovery, probe=True,
         prompt="请仅回复：连接测试通过。", system="只输出指定短文本，不使用工具，不访问网址。", before_send=before_send)
    status = "unknown" if failure and failure.code == "outcome_unknown" else "error" if failure else "success"
    result = {"status": status, "attempt_id": str(attempt_id), "elapsed_ms": elapsed,
              "error_code": failure.code if failure else None,
              "message": failure.message if failure else "已获取模型目录；仍需实际生成测试。" if discovery else "当前模型已完成真实生成测试。",
              "request_count": 1 if not failure or failure.sent else 0}
    if reply:
        result["models" if discovery else "preview"] = reply.models if discovery else reply.text[:400]
    with Session(engine()) as db, db.begin():
        account = get_owned_account(db, user.id, account_id)
        db.refresh(account, with_for_update=True)
        current = db.get(Connection, account.connection_id)
        if current and not current.archived and current.revision == revision and account.state == "connected":
            if not discovery:
                current.last_test = {"status": status, "revision": revision, "at": datetime.now(UTC).isoformat(),
                    "elapsed_ms": elapsed, "error_code": result["error_code"], "attempt_id": str(attempt_id),
                    "preview": result.get("preview"), "billing_source": "chatgpt_plan"}
                current.tested_revision = revision if status == "success" else None
                result["revision"] = revision
        else:
            result.update(status="error", error_code="configuration_changed", message="请求期间配置已变化，旧结果不能验证当前配置。")
        final = jsonable_encoder(envelope(request, result, "complete" if result["status"] == "success" else "unavailable",
                                         [] if result["status"] == "success" else [result["message"]]))
        stored = db.get(IdempotencyKey, identity)
        stored.response_json, stored.status_code = final, 200
    return JSONResponse(status_code=200, content=final)


@router.post("/api/v1/chatgpt/accounts/{account_id}/models", response_model=Envelope[ProbeResult],
             responses=ERRORS, operation_id="listChatGPTModels")
def models(account_id: uuid.UUID, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin", "researcher")
    return probe(request, identity[0], account_id, key, discovery=True)


@router.post("/api/v1/chatgpt/accounts/{account_id}/probe", response_model=Envelope[ProbeResult],
             responses=ERRORS, operation_id="probeChatGPTAccount")
def test_model(account_id: uuid.UUID, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin", "researcher")
    return probe(request, identity[0], account_id, key, discovery=False)
