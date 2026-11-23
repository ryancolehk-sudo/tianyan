import uuid
from datetime import UTC, datetime, timedelta

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.ai import service, vault
from app.ai.attempts import attempt_record, perform
from app.ai.models import AiProbeProof, Connection
from app.ai.schemas import ConnectionInput
from app.auth import APIError, require_instance
from app.db import engine
from app.evidence.common import content_hash, lock, normalized
from app.evidence.models import IdempotencyKey
from app.http import envelope


def existing_input(row):
    return ConnectionInput(id=row.id, name=row.name, kind=row.kind, template=row.template, endpoint=row.endpoint,
                           protocol=row.protocol, model=row.model, enabled=row.enabled, source_ids=row.source_ids,
                           options=row.options or {}, expected_revision=row.revision)


def model_probe(request, user, body, key, *, discovery=False):
    route = request.url.path
    identity = (user.id, "POST", route, key)
    fingerprint = content_hash(service.safe_body(body))
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        require_instance(db)
        lock(db, f"idempotency:{user.id}:POST:{route}:{key}")
        prior = db.get(IdempotencyKey, identity)
        clock = datetime.now(UTC)
        if prior and prior.expires_at > clock:
            if prior.payload_hash != fingerprint:
                raise APIError(409, "idempotency_conflict", "本次操作编号已用于其他配置。")
            return JSONResponse(status_code=prior.status_code, content=prior.response_json, headers={"Idempotency-Replayed": "true"})
        if prior:
            db.delete(prior)
            db.flush()
        service.validated(body, db)
        current = service.get_connection(db, body.id, materialize=True) if body.id else None
        if current and body.expected_revision is not None and current.revision != body.expected_revision:
            raise APIError(409, "configuration_changed", "配置已变化，请重新加载后测试。")
        raw_key = service.credential(body, current)
        if not raw_key:
            raise APIError(422, "api_key_required", "请先填写或保存模型密钥。")
        if not discovery and not body.model:
            raise APIError(422, "model_required", "请先填写或选择模型 ID。")
        connection = Connection(id=current.id if current else None, name=body.name, kind="model", template=body.template,
                                endpoint=body.endpoint, protocol=body.protocol, model=body.model, options=body.options.model_dump())
        attempt = attempt_record(user.id, connection, "discover" if discovery else "probe")
        db.add(attempt)
        pending = jsonable_encoder(envelope(request, {"status": "running", "attempt_id": attempt.id, "elapsed_ms": 0,
                                                     "error_code": None, "message": "模型请求处理中；相同操作编号不会重复发出。", "request_count": 0}))
        db.add(IdempotencyKey(owner_id=user.id, method="POST", route=route, key=key, payload_hash=fingerprint,
                              response_json=pending, status_code=202, expires_at=clock + timedelta(hours=24)))
        attempt_id = attempt.id
        proof_fingerprint = service.fingerprint(body, raw_key)
    reply, failure, elapsed = perform(attempt_id, connection, raw_key, prompt="请仅回复：连接测试通过。",
                                     system="你正在完成一次连接测试，仅输出指定的简短文本，不执行工具或访问网址。", discovery=discovery, probe=True)
    status = "unknown" if failure and failure.code == "outcome_unknown" else "error" if failure else "success"
    result = {"status": status, "attempt_id": str(attempt_id), "elapsed_ms": elapsed,
              "error_code": failure.code if failure else None,
              "message": failure.message if failure else "模型列表已读取，仍需实际生成测试。" if discovery else "当前配置真实生成测试通过。",
              "request_count": 1 if not failure or failure.sent else 0}
    if reply:
        if discovery:
            result["models"] = reply.models
        else:
            result["preview"] = reply.text[:400]
    with Session(engine()) as db, db.begin():
        if not discovery:
            test = {"status": status, "at": datetime.now(UTC).isoformat(), "error_code": result["error_code"],
                    "elapsed_ms": elapsed, "preview": result.get("preview"), "attempt_id": str(attempt_id),
                    "_credential_fingerprint": vault.secret_fingerprint(raw_key)}
            current = db.get(Connection, body.id) if body.id else None
            if current and not current.archived and proof_fingerprint == service.fingerprint(existing_input(current), vault.read_secret(current.secret_ref)):
                test["revision"] = current.revision
                current.last_test = test
                current.tested_revision = current.revision if status == "success" else None
                result["revision"] = current.revision
            if status == "success":
                proof = AiProbeProof(id=uuid.uuid4(), owner_id=user.id, fingerprint=proof_fingerprint,
                                     tested_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(minutes=20), result=test)
                db.add(proof)
                result["test_proof"] = str(proof.id)
        stored = db.get(IdempotencyKey, identity)
        final = jsonable_encoder(envelope(request, result, "complete" if status == "success" else "unavailable", [] if status == "success" else [result["message"]]))
        stored.response_json, stored.status_code = final, 200
    return JSONResponse(status_code=200, content=final)


def data_probe(db, user, body):
    if body.api_key and body.api_key.get_secret_value():
        raise APIError(409, "save_before_probe", "数据凭据请先保存，再检测指定能力。")
    if body.id:
        service.get_connection(db, body.id, materialize=True)
    result = service.data_module().probe_data(db, body, None)
    if body.id:
        row = service.get_connection(db, body.id, materialize=True)
        row.last_test = {**normalized(result), "at": datetime.now(UTC).isoformat(), "revision": row.revision}
        # queued is not success; only the actual DataFetchRun may validate this revision.
    return result, "partial" if result.get("status") == "queued" else "complete", []
