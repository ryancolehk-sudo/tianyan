import hmac
import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select

from app.ai import protocols, vault
from app.ai import schemas as s
from app.ai.models import AiBinding, AiProbeProof, AiSettings, Connection
from app.auth import APIError
from app.evidence.common import content_hash, lock
from app.models import AuditEvent


def data_module():
    from app.integrations import data
    return data


def audit(db, user, action, resource, fields, request_id=None):
    db.add(AuditEvent(actor_id=user.id, action=action, request_id=request_id, resource_type="service_connection",
                      resource_id=resource, changed_field_names=fields))


def credential(body, existing=None):
    value = body.api_key.get_secret_value() if body.api_key else None
    return value or (vault.read_secret(existing.secret_ref) if existing and existing.secret_ref else None)


def safe_body(body):
    values = body.model_dump(mode="json", exclude={"api_key"})
    raw = body.api_key.get_secret_value() if body.api_key else None
    if raw:
        values["credential_digest"] = vault.secret_fingerprint(raw)
    return values


def fingerprint(body, raw_key):
    values = body.model_dump(mode="json", exclude={"id", "source_id", "api_key", "enabled", "expected_revision", "test_proof", "name"})
    values["credential_digest"] = vault.secret_fingerprint(raw_key) if raw_key else None
    return content_hash(values)


def check_connection_owner(row, owner_id=None):
    if row.owner_id is not None and row.owner_id != owner_id:
        raise APIError(404, "not_found", "接入服务不存在或不可访问。")


def get_connection(db, identity, *, materialize=False, include_archived=False, owner_id=None):
    row = db.scalar(select(Connection).where(Connection.id == identity).with_for_update() if materialize else select(Connection).where(Connection.id == identity))
    if row:
        check_connection_owner(row, owner_id)
        if row.archived and not include_archived:
            raise APIError(404, "not_found", "接入服务已移除。")
        return row
    from app.model_connections import virtual_connections as legacy_connections
    for virtual in data_module().virtual_connections(db) + legacy_connections(db):
        if str(virtual["id"]) == str(identity):
            row = Connection(id=identity, name=virtual["name"], kind=virtual["kind"], template=virtual["template"],
                             endpoint=virtual["endpoint"], protocol=virtual.get("protocol"), model=virtual.get("model"),
                             enabled=virtual.get("enabled", False), revision=virtual.get("revision", 1),
                             secret_ref=virtual.get("known_env_ref"), source_ids=[str(x) for x in virtual.get("source_ids", [])],
                             options=virtual.get("options", {}), archived=False, last_test=virtual.get("last_test"), tested_revision=virtual.get("tested_revision"))
            if materialize:
                db.add(row)
                db.flush()
            return row
    raise APIError(404, "not_found", "接入服务不存在。")


def connection_test(db, row):
    if row.kind != "model":
        return data_module().connection_test(db, row)
    return row.last_test


def model_credential_health(row):
    try:
        raw = vault.read_secret(row.secret_ref)
        configured = bool(raw)
        tested_key = (row.last_test or {}).get("_credential_fingerprint")
        if configured and (row.secret_ref or "").startswith("env:model:") and tested_key:
            if not hmac.compare_digest(tested_key, vault.secret_fingerprint(raw)):
                return {"configured": False, "error_code": "credential_changed_requires_test"}
        return {"configured": configured, "error_code": None if configured else "credentials_missing"}
    except APIError as error:
        return {"configured": False, "error_code": error.code}


def connection_view(db, row):
    test = connection_test(db, row)
    if test:
        test = {key: value for key, value in test.items() if not key.startswith("_")}
    capability_rows = data_module().capabilities(db, row) if row.kind != "model" else []
    configured = bool(row.secret_ref)
    credential_error = None
    if row.kind == "model" or row.template == "ifind_mcp":
        health = model_credential_health(row)
        configured, credential_error = health["configured"], health["error_code"]
    elif capability_rows:
        configured = all(item.get("credential_configured") is not False for item in capability_rows)
    elif (row.secret_ref or "").startswith("env:"):
        configured = bool(vault.read_secret(row.secret_ref))
    data = {key: getattr(row, key) for key in ("id", "name", "kind", "template", "endpoint", "protocol", "model", "enabled", "revision", "source_ids", "owner_id")}
    data.update(options=s.ConnectionOptions.model_validate(row.options or {}).model_dump(), credential_configured=configured,
                credential_origin="vault" if (row.secret_ref or "").startswith("vault:") else "environment" if row.secret_ref else "none",
                credential_error_code=credential_error,
                last_test=test, tested_revision=row.tested_revision,
                capabilities=capability_rows)
    return data


def validated(body, db):
    if body.template == "chatgpt_plan":
        raise APIError(422, "chatgpt_official_authorization_required", "ChatGPT 套餐连接请使用官方授权入口，不能填写或替换为普通 Key。")
    if body.kind == "model":
        try:
            protocols.validate_endpoint(body.endpoint)
        except protocols.ModelFailure as error:
            raise APIError(422, error.code, error.message) from None
    else:
        data_module().validate_data_input(db, body)


def store_connection(db, user, body, identity=None, request_id=None, *, owner_id=None):
    if identity:
        body = body.model_copy(update={"id": identity})
    validated(body, db)
    row = get_connection(db, identity, materialize=True, owner_id=owner_id) if identity else None
    if row and body.expected_revision is not None and body.expected_revision != row.revision:
        raise APIError(409, "configuration_changed", "接入配置已变化，请重新加载后修改。")
    if row and row.kind != body.kind:
        raise APIError(422, "kind_immutable", "已有接入不能更换类型，请新建连接。")
    if row is None:
        row = Connection(id=uuid.uuid4(), revision=1, archived=False, tested_revision=None, last_test=None)
        changed = True
    else:
        old_test = connection_test(db, row)
        changed = any(getattr(row, name) != value for name, value in {
            "endpoint": body.endpoint, "protocol": body.protocol, "model": body.model, "template": body.template,
            "source_ids": [str(x) for x in body.source_ids]}.items())
        changed = changed or s.ConnectionOptions.model_validate(row.options or {}).model_dump() != body.options.model_dump()
        changed = changed or bool(body.api_key and body.api_key.get_secret_value())
        if changed:
            row.revision += 1
            row.last_test, row.tested_revision = None, None
        elif row.enabled != body.enabled:
            old_revision = row.revision
            row.revision += 1
            if old_test and old_test.get("revision") == old_revision:
                row.last_test = {**old_test, "revision": row.revision}
                row.tested_revision = row.revision if old_test.get("status") == "success" else None
    raw_key = credential(body, row) if body.enabled or body.test_proof or (body.api_key and body.api_key.get_secret_value()) else None
    proof = None
    if body.test_proof and body.kind == "model":
        proof = db.get(AiProbeProof, body.test_proof)
        if (not proof or proof.owner_id != user.id or proof.expires_at <= datetime.now(UTC)
                or proof.fingerprint != fingerprint(body, raw_key)):
            raise APIError(409, "test_proof_expired", "配置或凭据已变化，请重新测试本次配置。")
    if proof:
        row.last_test = {**proof.result, "revision": row.revision}
        row.tested_revision = row.revision
    if body.enabled:
        test = connection_test(db, row) if body.kind != "model" else row.last_test
        valid = test and test.get("status") == "success" and test.get("revision") == row.revision
        # Existing virtual sources keep their original on/off semantics until edited; changes require a fresh test.
        compatible = body.kind != "model" and not changed and row.enabled
        if not valid and not compatible:
            raise APIError(409, "test_required", "请先保存为停用状态，并完成当前配置的真实检测再启用。")
        if body.kind == "model" and (not body.model or not raw_key):
            raise APIError(422, "model_credentials_required", "启用模型前请填写模型 ID 和有效密钥。")
    if body.api_key and body.api_key.get_secret_value():
        row.secret_ref = vault.write_secret(body.api_key.get_secret_value(), db=db)
    for field in ("name", "kind", "template", "endpoint", "protocol", "model", "enabled"):
        setattr(row, field, getattr(body, field))
    row.options, row.source_ids, row.updated_at = body.options.model_dump(), [str(x) for x in body.source_ids], datetime.now(UTC)
    db.add(row)
    db.flush()
    if row.kind != "model":
        data_module().sync_connection(db, row)
    audit(db, user, "integration.update" if identity else "integration.create", row.id,
          ["configuration", "credential_reference"] if body.api_key else ["configuration"], request_id)
    return connection_view(db, row), "complete", []


def usable_connection(db, identity, owner_id=None):
    row = db.get(Connection, identity) if identity else None
    if row:
        check_connection_owner(row, owner_id)
    if (not row or row.archived or row.kind != "model" or not row.enabled or not row.model
            or row.tested_revision != row.revision or not row.last_test or row.last_test.get("status") != "success"):
        raise APIError(409, "model_not_ready", "请选择已启用且当前配置测试通过的模型服务。")
    health = model_credential_health(row)
    if not health["configured"]:
        raise APIError(409, health["error_code"], "当前模型凭据不可读取，请检查独立密钥挂载或重新保存密钥。")
    if row.template == "chatgpt_plan":
        from app.chatgpt.service import account_for_connection, require_available
        require_available()
        account = account_for_connection(db, row.id, owner_id=owner_id)
        if (account is None or account.state != "connected" or not account.secret_ref
                or not {"resource.invoke", "chatgpt.tokens.use.direct"} <= set(account.scopes or [])):
            raise APIError(409, "chatgpt_plan_not_authorized", "当前 ChatGPT 连接尚未授权套餐使用或需要重新登录。")
    return row


def personal_connection(db, owner_id):
    if owner_id is None:
        return None
    from app.chatgpt.models import ChatGPTAccount
    account = db.scalar(select(ChatGPTAccount).where(ChatGPTAccount.owner_id == owner_id,
                                                     ChatGPTAccount.use_for_ai.is_(True)))
    if account:
        connection = db.get(Connection, account.connection_id)
        if connection and connection.owner_id == owner_id:
            return connection
        raise APIError(409, "chatgpt_connection_unavailable", "选中的 ChatGPT 连接不可用，请重新连接或取消用于本人 AI。")
    return None


def connection_credential(db, row, owner_id):
    check_connection_owner(row, owner_id)
    if row.template == "chatgpt_plan":
        from app.chatgpt.service import access_token, account_for_connection, require_available
        require_available()
        account = account_for_connection(db, row.id, owner_id=owner_id)
        if account is None:
            raise APIError(409, "chatgpt_connection_unavailable", "本人 ChatGPT 连接不存在。")
        return access_token(db, account)
    return vault.read_secret(row.secret_ref)


def settings_view(db, owner_id=None):
    config = db.get(AiSettings, 1)
    bindings = {row.feature: row for row in db.scalars(select(AiBinding))}
    default = config.default_connection_id if config else None
    personal = personal_connection(db, owner_id)
    personal_ready = False
    if personal:
        try:
            usable_connection(db, personal.id, owner_id)
            personal_ready = True
        except APIError:
            pass
    return {"enabled": config.enabled if config else False, "default_connection_id": default,
            "revision": config.revision if config else 1,
            "personal_connection_id": personal.id if personal else None, "personal_model_ready": personal_ready,
            "features": [{"id": name, "module": label[0], "name": label[1], "implemented": label[2],
                          "enabled": bindings[name].enabled if name in bindings else False,
                          "connection_id": bindings[name].connection_id if name in bindings else None,
                          "effective_connection_id": personal.id if personal else (bindings[name].connection_id or default) if name in bindings else default,
                          "fallback_connection_id": bindings[name].fallback_connection_id if name in bindings else None}
                         for name, label in s.FEATURES.items()]}


def update_settings(db, user, body, request_id=None):
    lock(db, "ai:settings")
    row = db.get(AiSettings, 1)
    if not row:
        row = AiSettings(id=1, enabled=False, revision=1)
        db.add(row)
    if body.expected_revision is not None and row.revision != body.expected_revision:
        raise APIError(409, "configuration_changed", "全局模型配置已变化，请刷新。")
    if "default_connection_id" in body.model_fields_set:
        if body.default_connection_id:
            usable_connection(db, body.default_connection_id)
        row.default_connection_id = body.default_connection_id
    if body.enabled is not None:
        row.enabled = body.enabled
    if row.enabled:
        current = personal_connection(db, user.id) if not row.default_connection_id else None
        usable_connection(db, current.id if current else row.default_connection_id, user.id if current else None)
    row.revision += 1
    audit(db, user, "ai.settings.update", None, list(body.model_fields_set), request_id)
    db.flush()
    return settings_view(db, user.id), "complete", []


def update_binding(db, user, feature, body, request_id=None):
    if feature not in s.FEATURES:
        raise APIError(404, "not_found", "AI 功能不存在。")
    if body.enabled and not s.FEATURES[feature][2]:
        raise APIError(409, "feature_not_implemented", "该功能业务待接入，暂不可启用。")
    lock(db, "ai:settings")
    row = db.get(AiBinding, feature)
    for field in ("connection_id", "fallback_connection_id"):
        identity = getattr(body, field)
        if identity and (row is None or getattr(row, field) != identity):
            usable_connection(db, identity)
    config = db.get(AiSettings, 1)
    if body.enabled:
        primary = body.connection_id or (config.default_connection_id if config else None)
        personal = personal_connection(db, user.id) if not primary else None
        usable_connection(db, personal.id if personal else primary, user.id if personal else None)
    if body.fallback_connection_id and body.fallback_connection_id == (body.connection_id or (config.default_connection_id if config else None)):
        raise APIError(422, "fallback_same_model", "备用服务须与当前使用的服务不同。")
    if row is None:
        row = AiBinding(feature=feature, revision=1)
        db.add(row)
    else:
        row.revision += 1
    row.enabled, row.connection_id, row.fallback_connection_id = body.enabled, body.connection_id, body.fallback_connection_id
    audit(db, user, "ai.binding.update", None, [feature], request_id)
    db.flush()
    return settings_view(db, user.id), "complete", []


def authorized_plan_codex(db, connection_id, owner_id):
    from app.chatgpt import oauth
    from app.chatgpt.service import account_for_connection, require_available
    require_available()
    connection = db.get(Connection, connection_id)
    if (not owner_id or not connection or connection.owner_id != owner_id or connection.archived
            or connection.kind != 'model' or connection.template != 'chatgpt_plan'):
        raise APIError(404, 'not_found', '当前账号没有这个 Codex 连接。')
    account = account_for_connection(db, connection.id, owner_id=owner_id)
    if (not account or account.state != 'connected' or not account.secret_ref or connection.secret_ref != account.secret_ref
            or not oauth.SHARING_SCOPES.issubset(set(account.scopes or []))):
        raise APIError(409, 'chatgpt_plan_not_authorized', '此 Codex 账号需要重新授权。')
    return connection


def resolve_feature(db, feature, owner_id=None, *, plan_connection_id=None, plan_model=None):
    if feature not in s.FEATURES or not s.FEATURES[feature][2]:
        raise APIError(409, "feature_not_implemented", "该功能业务待接入。")
    config, binding = db.get(AiSettings, 1), db.get(AiBinding, feature)
    if not config or not config.enabled:
        raise APIError(409, "ai_disabled", "全局 AI 已关闭，请先在数据中心开启。")
    if not binding or not binding.enabled:
        raise APIError(409, "feature_disabled", "此 AI 功能尚未开启，请先完成数据中心的模型分配。")
    if plan_model is not None and plan_connection_id is None:
        raise APIError(409, 'invalid_model_selection', '请选择本次复盘使用的 Codex 账号。')
    if plan_connection_id is not None:
        if feature != "plan_generation" or owner_id is None:
            raise APIError(409, "invalid_model_selection", "仅本人明日预案可指定此模型。")
        connection_id = uuid.UUID(str(plan_connection_id))
        connection = (authorized_plan_codex(db, connection_id, owner_id) if plan_model is not None
                      else usable_connection(db, connection_id, owner_id))
        fallback_id = None
    else:
        personal = personal_connection(db, owner_id)
        connection = usable_connection(db, personal.id if personal else binding.connection_id or config.default_connection_id, owner_id)
        fallback_id = None if personal else binding.fallback_connection_id
    configured_fallback = db.get(Connection, fallback_id) if fallback_id else None
    fallback = None
    if fallback_id:
        try:
            fallback = usable_connection(db, fallback_id)
        except APIError:
            # A deliberately configured but later unavailable backup must not block the healthy primary.
            pass
    snapshot = {"settings_revision": config.revision, "binding_revision": binding.revision,
                "connection_id": str(connection.id), "connection_revision": connection.revision,
                "connection_credential_fingerprint": (connection.last_test or {}).get("_credential_fingerprint"),
                "fallback_connection_id": str(fallback_id) if fallback_id else None,
                "fallback_revision": configured_fallback.revision if configured_fallback else None,
                "fallback_credential_fingerprint": (configured_fallback.last_test or {}).get("_credential_fingerprint") if configured_fallback else None,
                "fallback_available": fallback is not None}
    if plan_connection_id is not None:
        snapshot["plan_connection_id"] = str(connection.id)
    if plan_model is not None:
        snapshot["plan_model"] = plan_model
    if connection.template == "chatgpt_plan":
        snapshot["billing_source"] = "chatgpt_plan"
        snapshot["owner_id"] = str(owner_id)
    return connection, fallback, snapshot


def archive_connection(db, user, identity, request_id=None):
    lock(db, "ai:settings")
    row = get_connection(db, identity, materialize=True, include_archived=True)
    if row.kind == "model":
        config = db.get(AiSettings, 1)
        bound = db.scalar(select(AiBinding.feature).where(or_(AiBinding.connection_id == row.id,
                                                             AiBinding.fallback_connection_id == row.id)).limit(1))
        if (config and config.default_connection_id == row.id) or bound:
            raise APIError(409, "connection_in_use", "此模型仍被全局默认或功能主/备用配置引用，请先切换或解除引用再移除。")
    row.archived, row.enabled = True, False
    row.revision += 1
    if row.kind != "model":
        data_module().sync_connection(db, row)
    audit(db, user, "integration.archive", row.id, ["archived", "enabled"], request_id)
