import base64
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.ai import vault
from app.ai.models import Connection
from app.auth import COOKIE, APIError, digest, hasher
from app.chatgpt import oauth
from app.chatgpt.models import (
    ChatGPTAccount,
    ChatGPTAuthorization,
    ChatGPTIdentity,
    ChatGPTInstallation,
)
from app.chatgpt.schemas import ChatGPTAccountView
from app.config import settings
from app.evidence.common import lock
from app.models import AuditEvent, LoginSession, User

CALLBACK_PATH = "/api/v1/chatgpt/callback"
ATTEMPT_SECONDS = 600
REGISTRATION_COOKIE_PATH = "/api/v1/chatgpt/login"
REGISTRATION_COOKIE_SECONDS = 30 * 24 * 3600


def availability():
    config = settings()
    origin = urlsplit(config.public_origin)
    if origin.scheme != "http" or origin.hostname != "127.0.0.1":
        return False, "ChatGPT 订阅登录仅支持本机 http://127.0.0.1 入口，请在本机打开。"
    if not vault.ready():
        return False, "本机加密凭据存储尚未就绪，请检查独立主密钥挂载。"
    return True, None


def require_available():
    available, reason = availability()
    if not available:
        raise APIError(409, "chatgpt_unavailable", reason)


def browser_cookie_name():
    return settings().session_cookie + "_chatgpt"


def registration_cookie_name():
    # A public OAuth client identifier is only a picker hint, never a session.
    return settings().session_cookie + "_chatgpt_registration"


def host_id(db):
    db.execute(insert(ChatGPTInstallation).values(id=1, host_id="urn:uuid:" + str(uuid.uuid4()))
        .on_conflict_do_nothing(index_elements=[ChatGPTInstallation.id]))
    return db.scalar(select(ChatGPTInstallation.host_id).where(ChatGPTInstallation.id == 1))


def get_owned_account(db, owner_id, account_id):
    row = db.scalar(select(ChatGPTAccount).where(ChatGPTAccount.id == account_id, ChatGPTAccount.owner_id == owner_id))
    if not row:
        raise APIError(404, "chatgpt_account_not_found", "ChatGPT 账号不存在或不可访问。")
    return row


def account_for_connection(db, connection_id, owner_id=None):
    statement = select(ChatGPTAccount).where(ChatGPTAccount.connection_id == connection_id)
    if owner_id is not None:
        statement = statement.where(ChatGPTAccount.owner_id == owner_id)
    return db.scalar(statement)


def account_view(db, account):
    connection = db.get(Connection, account.connection_id)
    test = connection.last_test if connection else None
    if test:
        test = {key: value for key, value in test.items() if not key.startswith("_")}
    visible_state = account.state
    if visible_state == "refreshing" and (not account.refresh_started_at or account.refresh_started_at < datetime.now(UTC) - timedelta(seconds=60)):
        visible_state = "refresh_unknown"
    return ChatGPTAccountView(id=account.id, connection_id=account.connection_id, email=account.email,
        label=account.label, state=visible_state, use_for_ai=account.use_for_ai,
        sharing_granted=oauth.SHARING_SCOPES.issubset(set(account.scopes)),
        model=connection.model if connection else None, enabled=bool(connection and connection.enabled),
        revision=account.revision, tested=bool(connection and connection.tested_revision == connection.revision and test and test.get("status") == "success"),
        last_test=test, welcome_pending=account.welcome_seen_at is None,
        reauthorization_required=visible_state in {"disconnected", "refresh_unknown", "reauthorization_required", "client_error"},
        remote_revocation_unconfirmed=account.remote_revocation_unconfirmed,
        created_at=account.created_at, updated_at=account.updated_at)


def _secret_path(reference):
    if not reference or not reference.startswith("vault:"):
        raise APIError(503, "credential_reference_invalid", "ChatGPT 凭据引用无效。")
    try:
        identity = uuid.UUID(reference[6:])
    except (ValueError, TypeError):
        raise APIError(503, "credential_reference_invalid", "ChatGPT 凭据引用无效。") from None
    directory, _ = vault.paths()
    path = directory / f"{identity}.key"
    if path.is_symlink():
        raise APIError(503, "credential_reference_invalid", "ChatGPT 凭据文件不可用。")
    return path


def erase_secret(reference, *, db=None):
    if not reference:
        return
    try:
        with vault.credential_write(db):
            _secret_path(reference).unlink(missing_ok=True)
    except OSError:
        raise APIError(503, "credential_cleanup_failed", "本机凭据清理失败，请检查凭据卷权限。") from None


def save_bundle(bundle, reference=None, *, db=None):
    """Replace the entire encrypted token group atomically without retaining old refresh tokens."""
    with vault.credential_write(db):
        return _save_bundle(bundle, reference)


def _save_bundle(bundle, reference=None):
    raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if not reference:
        return vault._write_secret(raw)
    path = _secret_path(reference)
    temporary = path.with_name("." + str(uuid.uuid4()) + ".tmp")
    try:
        encrypted = vault.cipher().encrypt(raw.encode("utf-8"))
        with temporary.open("xb") as output:
            os.chmod(temporary, 0o600)
            output.write(encrypted)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        return reference
    except OSError:
        raise APIError(503, "credential_store_unavailable", "ChatGPT 凭据密文保存失败，请重新授权。") from None
    finally:
        temporary.unlink(missing_ok=True)


def read_bundle(account):
    try:
        value = json.loads(vault.read_secret(account.secret_ref) or "null")
        if not isinstance(value, dict):
            raise ValueError()
        if (value.get("issuer") != account.issuer or value.get("subject") != account.subject
                or value.get("client_id") != account.client_id):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise APIError(503, "chatgpt_credentials_invalid", "ChatGPT 凭据无法使用，请重新授权。") from None


def _invalidate(db, account, state, *, clear_tokens=False):
    connection = db.get(Connection, account.connection_id)
    account.state = state
    account.revision += 1
    account.updated_at = datetime.now(UTC)
    account.refresh_started_at = None
    if clear_tokens:
        erase_secret(account.secret_ref, db=db)
        account.secret_ref = None
    if connection:
        connection.enabled = False
        connection.revision += 1
        connection.last_test = None
        connection.tested_revision = None
        connection.updated_at = datetime.now(UTC)
        if clear_tokens:
            connection.secret_ref = None


def begin_authorization(db, *, mode, identity=None, account_id=None, returning_client_id=None):
    require_available()
    owner, session = identity if identity else (None, None)
    if mode == "connect" and owner is None:
        raise APIError(401, "unauthenticated", "请登录原有账号后绑定 ChatGPT。")
    if account_id is not None and (owner is None or mode != "connect"):
        raise APIError(400, "chatgpt_account_selection_invalid", "请在已登录账号内选择要重新授权的 ChatGPT 账号。")
    account = get_owned_account(db, owner.id, account_id) if account_id else None
    if (account is None and mode == "login" and isinstance(returning_client_id, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{5,199}", returning_client_id)
            and returning_client_id != "dynamic_agent_client"):
        candidates = list(db.scalars(select(ChatGPTAccount).where(ChatGPTAccount.client_id == returning_client_id).limit(2)))
        # Ignore stale, tampered or ambiguous public hints. No identity is
        # disclosed here, and this selection never authenticates its owner.
        if len(candidates) == 1:
            account = candidates[0]
    if account and account.state == "refreshing" and account.refresh_started_at and account.refresh_started_at > datetime.now(UTC) - timedelta(seconds=60):
        raise APIError(409, "chatgpt_refresh_in_progress", "ChatGPT 会话正在更新，请稍后重新授权。")
    # Delete expired verifier files before accepting another attempt; never copy them into audit/idempotency bodies.
    current = datetime.now(UTC)
    for old in db.scalars(select(ChatGPTAuthorization).where(ChatGPTAuthorization.expires_at < current,
            ChatGPTAuthorization.secret_ref.is_not(None)).limit(100)):
        erase_secret(old.secret_ref, db=db)
        old.secret_ref = None
        old.consumed_at = old.consumed_at or current
    state, nonce, verifier, browser = (secrets.token_urlsafe(32) for _ in range(4))
    redirect_uri = settings().public_origin + CALLBACK_PATH
    params = {"client_id": account.client_id if account else "dynamic_agent_client", "ext_agent_host_id": host_id(db),
        "response_type": "code", "redirect_uri": redirect_uri, "scope": oauth.SCOPES, "resource": oauth.RESOURCE,
        "state": state, "nonce": nonce, "code_challenge_method": "S256",
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
    if account:
        # OpenAI supports id_token_hint, but the local product deliberately keeps
        # every retained token out of browser JavaScript, responses and URLs.
        if mode == "connect" and account.email:
            params["login_hint"] = account.email
        if not oauth.SHARING_SCOPES.issubset(set(account.scopes)):
            params["prompt"] = "consent"
    else:
        params["agent_name_hint"] = "Tianyan"
    secret_ref = vault.write_secret(json.dumps({"verifier": verifier}), db=db)
    pending = ChatGPTAuthorization(state_hash=digest(state), browser_hash=digest(browser), nonce_hash=digest(nonce),
        secret_ref=secret_ref, mode=mode, owner_id=owner.id if owner and mode == "connect" else None,
        session_id=session.id if session and mode == "connect" else None, account_id=account.id if account else None,
        client_id=account.client_id if account else None, redirect_uri=redirect_uri,
        expires_at=current + timedelta(seconds=ATTEMPT_SECONDS))
    db.add(pending)
    try:
        db.commit()
    except Exception:
        db.rollback()
        erase_secret(secret_ref)
        raise
    return oauth.AUTHORIZE_URL + "?" + urlencode(params), browser


def consume_authorization(db, *, state, browser, client_id, code, error):
    require_available()
    if not isinstance(state, str) or not 20 <= len(state) <= 200 or not isinstance(browser, str) or not 20 <= len(browser) <= 200:
        raise APIError(400, "chatgpt_state_invalid", "登录验证已失效，请重新开始。")
    pending = db.scalar(select(ChatGPTAuthorization).where(ChatGPTAuthorization.state_hash == digest(state)).with_for_update())
    current = datetime.now(UTC)
    if (not pending or pending.consumed_at or pending.expires_at <= current
            or not secrets.compare_digest(pending.browser_hash, digest(browser))
            or pending.redirect_uri != settings().public_origin + CALLBACK_PATH):
        raise APIError(400, "chatgpt_state_invalid", "登录验证已失效，请重新开始。")
    try:
        saved = json.loads(vault.read_secret(pending.secret_ref) or "null")
        verifier = saved["verifier"]
    except (ValueError, TypeError, KeyError):
        raise APIError(400, "chatgpt_state_invalid", "登录验证已失效，请重新开始。") from None
    # Persist consumption before exchanging a single-use authorization code.
    pending.consumed_at = current
    erase_secret(pending.secret_ref, db=db)
    pending.secret_ref = None
    if error is not None:
        db.commit()
        raise APIError(400, "chatgpt_authorization_declined", "ChatGPT 授权未完成，可重新登录或继续使用原账号。")
    if pending.client_id:
        if client_id is not None and client_id != pending.client_id:
            db.commit()
            raise APIError(400, "chatgpt_registration_mismatch", "返回的账号登记与本次选择不一致，请重新授权。")
        issued = pending.client_id
    else:
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{5,199}", client_id) or client_id == "dynamic_agent_client":
            db.commit()
            raise APIError(400, "chatgpt_registration_incomplete", "ChatGPT 未返回有效应用登记，请重新开始。")
        pending.client_id = client_id
        issued = client_id
    db.commit()
    if not isinstance(code, str) or not 1 <= len(code) <= 4096 or any(ord(char) < 33 or ord(char) > 126 for char in code):
        raise APIError(400, "chatgpt_code_missing", "ChatGPT 未返回有效授权码，请重新开始。")
    owner = None
    if pending.owner_id:
        owner = db.scalar(select(User).join(LoginSession, LoginSession.user_id == User.id).where(
            User.id == pending.owner_id, User.enabled.is_(True), LoginSession.id == pending.session_id,
            LoginSession.revoked_at.is_(None), LoginSession.expires_at > func.now()))
        if owner is None:
            raise APIError(401, "chatgpt_original_session_expired", "原账号登录已失效，请先重新登录再绑定。")
    selected_account = None
    if pending.account_id:
        selected_account = db.get(ChatGPTAccount, pending.account_id)
        if (selected_account is None or selected_account.client_id != issued
                or (pending.owner_id is not None and selected_account.owner_id != pending.owner_id)):
            raise APIError(400, "chatgpt_registration_mismatch", "返回的账号登记与本次选择不一致，请重新授权。")
    try:
        tokens = oauth.request_json(oauth.TOKEN_URL, form={"grant_type": "authorization_code", "client_id": issued,
            "code": code, "code_verifier": verifier, "redirect_uri": pending.redirect_uri, "resource": oauth.RESOURCE})
        claims = oauth.verify_identity(tokens.get("id_token"), issued, pending.nonce_hash)
        bundle = oauth.token_bundle(tokens)
    except oauth.OAuthFailure as failure:
        raise APIError(502, "chatgpt_exchange_unknown" if failure.uncertain else "chatgpt_authorization_failed",
            "授权结果尚未确认，请重新开始登录；不会自动重发授权码。" if failure.uncertain else "ChatGPT 授权无法验证，请重新登录。") from None
    account = selected_account
    if pending.owner_id:
        # Account/session changes while the browser was at OpenAI must not finish a binding.
        valid_session = db.scalar(select(LoginSession.id).join(User, User.id == LoginSession.user_id).where(
            LoginSession.id == pending.session_id, LoginSession.user_id == pending.owner_id,
            LoginSession.revoked_at.is_(None), LoginSession.expires_at > func.now(), User.enabled.is_(True)))
        if valid_session is None:
            raise APIError(401, "chatgpt_original_session_expired", "原账号登录已失效，请先重新登录再绑定。")
    if account and (account.issuer != claims["iss"] or account.subject != claims["sub"] or account.client_id != issued):
        raise APIError(409, "chatgpt_identity_mismatch", "返回的 ChatGPT 身份与所选账号不一致，请重新选择。")
    lock(db, "chatgpt:identity:" + digest(claims["iss"] + "\0" + claims["sub"]))
    mapping = db.scalar(select(ChatGPTIdentity).where(ChatGPTIdentity.issuer == claims["iss"], ChatGPTIdentity.subject == claims["sub"]))
    if account is not None and mapping is None:
        raise APIError(409, "chatgpt_identity_mismatch", "返回的 ChatGPT 身份与所选账号不一致，请重新选择。")
    if mapping:
        if owner is not None and mapping.owner_id != owner.id:
            raise APIError(409, "chatgpt_identity_already_linked", "这个 ChatGPT 身份已绑定其他本地账号，不能更换归属。")
        owner = db.get(User, mapping.owner_id)
        if not owner or not owner.enabled:
            raise APIError(403, "chatgpt_account_disabled", "绑定的本地账号已停用，请联系本机管理员。")
    elif owner is None:
        owner = User(id=uuid.uuid4(), username="gpt_" + uuid.uuid4().hex[:20], password_hash=hasher.hash(secrets.token_urlsafe(64)),
            role="researcher", enabled=True)
        db.add(owner)
        db.flush()
    if mapping is None:
        db.add(ChatGPTIdentity(owner_id=owner.id, issuer=claims["iss"], subject=claims["sub"]))
    if account is None:
        account = db.scalar(select(ChatGPTAccount).where(ChatGPTAccount.issuer == claims["iss"], ChatGPTAccount.subject == claims["sub"], ChatGPTAccount.client_id == issued))
    if account and account.owner_id != owner.id:
        raise APIError(409, "chatgpt_identity_already_linked", "这个 ChatGPT 登记已绑定其他本地账号。")
    email = claims.get("email")
    email = email if isinstance(email, str) and len(email) <= 320 and not any(ord(c) < 32 for c in email) else None
    registration_id = account.id if account else uuid.uuid4()
    label = ((email or "ChatGPT")[:140] + " · " + str(registration_id))
    if account is None:
        connection = Connection(id=uuid.uuid4(), owner_id=owner.id, name="ChatGPT · " + label, kind="model", template="chatgpt_plan",
            endpoint=oauth.RESOURCE, protocol="openai_responses", model=None, enabled=False, revision=1,
            options={}, source_ids=[], archived=False)
        db.add(connection)
        db.flush()
        account = ChatGPTAccount(id=registration_id, owner_id=owner.id, connection_id=connection.id, issuer=claims["iss"],
            subject=claims["sub"], client_id=issued, label=label, state="disconnected", use_for_ai=False, revision=1)
        db.add(account)
    else:
        db.refresh(account, with_for_update=True)
        connection = db.get(Connection, account.connection_id)
        _invalidate(db, account, "disconnected")
    # Fence any previous runnable configuration before replacing a file. A crash
    # between the vault write and the final DB commit must leave the account
    # disconnected, rather than reuse an old success proof with new credentials.
    db.flush()
    token_revision = account.revision
    db.commit()
    db.refresh(account, with_for_update=True)
    if account.revision != token_revision or account.state != "disconnected":
        raise APIError(409, "chatgpt_authorization_superseded", "本次授权已被较新的账号操作替代，请刷新后重试。")
    owner = db.scalar(select(User).where(User.id == account.owner_id, User.enabled.is_(True))
        .with_for_update().execution_options(populate_existing=True))
    if owner is None:
        raise APIError(403, "chatgpt_account_disabled", "绑定的本地账号已停用，请联系本机管理员。")
    if pending.owner_id:
        # The durable fence released its original transaction. Lock the original
        # user/session again through the credential write and final API commit.
        # A concurrent logout/disable then orders before or after this binding.
        original_session = db.scalar(select(LoginSession).where(LoginSession.id == pending.session_id,
            LoginSession.user_id == pending.owner_id, LoginSession.revoked_at.is_(None),
            LoginSession.expires_at > func.now()).with_for_update().execution_options(populate_existing=True))
        if owner.id != pending.owner_id or original_session is None:
            raise APIError(401, "chatgpt_original_session_expired", "原账号登录已失效，请先重新登录再绑定。")
    connection = db.get(Connection, account.connection_id)
    bundle.update(issuer=claims["iss"], subject=claims["sub"], client_id=issued, ext_agent_host_id=host_id(db), saved_at=datetime.now(UTC).isoformat())
    new_secret = account.secret_ref is None
    account.secret_ref = save_bundle(bundle, account.secret_ref, db=db)
    if new_secret:
        db.info.setdefault("chatgpt_uncommitted_secrets", []).append(account.secret_ref)
    account.email, account.label, account.scopes = email, label, bundle["scopes"]
    account.state = "connected" if oauth.SHARING_SCOPES.issubset(set(account.scopes)) else "identity_only"
    account.last_authorized_at = account.updated_at = datetime.now(UTC)
    account.remote_revocation_unconfirmed = False
    account.refresh_started_at = None
    connection.secret_ref = account.secret_ref
    connection.enabled = False
    connection.archived = False
    connection.last_test = None
    connection.tested_revision = None
    connection.updated_at = datetime.now(UTC)
    db.add(AuditEvent(actor_id=owner.id, action="chatgpt.connected", resource_type="chatgpt_account", resource_id=account.id))
    db.flush()
    return owner, account, pending.mode


def issue_session(db, owner, request, response):
    old = request.cookies.get(COOKIE)
    if old:
        db.execute(update(LoginSession).where(LoginSession.token_hash == digest(old)).values(revoked_at=func.now()))
    token, csrf_token = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    db.add(LoginSession(user_id=owner.id, token_hash=digest(token), csrf_token=csrf_token,
        expires_at=datetime.now(UTC) + timedelta(hours=settings().session_hours)))
    db.add(AuditEvent(actor_id=owner.id, action="session.chatgpt_login", request_id=request.state.request_id))
    response.set_cookie(COOKIE, token, max_age=settings().session_hours * 3600, httponly=True,
        secure=settings().cookie_secure, samesite="strict", path="/")


def access_token(db, account):
    """Refresh once under a durable per-registration latch; never retry uncertain rotation."""
    account = db.scalar(select(ChatGPTAccount).where(ChatGPTAccount.id == account.id).with_for_update().execution_options(populate_existing=True))
    if not account or account.state != "connected" or not account.secret_ref or not oauth.SHARING_SCOPES.issubset(set(account.scopes)):
        db.rollback()
        raise APIError(409, "chatgpt_reauthorization_required", "所选 ChatGPT 账号需要重新授权；不会改用其他计费方式。")
    bundle = read_bundle(account)
    expires = bundle.get("expires_at")
    if isinstance(expires, (int, float)) and expires > time.time() + 60 and bundle.get("access_token"):
        # Release the row lock without committing the caller's pending model operation.
        return bundle["access_token"]
    if not bundle.get("refresh_token"):
        _invalidate(db, account, "reauthorization_required", clear_tokens=True)
        db.commit()
        raise APIError(409, "chatgpt_reauthorization_required", "ChatGPT 会话已到期，请重新授权。")
    account.state = "refreshing"
    account.refresh_started_at = datetime.now(UTC)
    db.commit()
    try:
        response = oauth.request_json(oauth.TOKEN_URL, form={"grant_type": "refresh_token", "client_id": account.client_id,
            "refresh_token": bundle["refresh_token"], "resource": oauth.RESOURCE})
        replacement = oauth.token_bundle(response, previous=bundle)
        if response.get("id_token"):
            claims = oauth.verify_identity(response["id_token"], account.client_id)
            if claims["iss"] != account.issuer or claims["sub"] != account.subject:
                raise oauth.OAuthFailure("chatgpt_identity_invalid")
    except oauth.OAuthFailure as failure:
        db.refresh(account, with_for_update=True)
        if failure.code in oauth.TERMINAL_REFRESH_ERRORS:
            _invalidate(db, account, "reauthorization_required", clear_tokens=True)
        elif failure.code == "invalid_client":
            _invalidate(db, account, "client_error")
        elif failure.uncertain or failure.code in {"chatgpt_token_invalid", "chatgpt_identity_invalid", "chatgpt_response_invalid"}:
            _invalidate(db, account, "refresh_unknown")
        else:
            # A confirmed rejection/temporary error has not supplied a rotated token.
            account.state = "connected"
            account.refresh_started_at = None
        db.commit()
        raise APIError(409 if account.state != "connected" else 503,
            "chatgpt_refresh_unknown" if account.state == "refresh_unknown" else "chatgpt_reauthorization_required" if account.state in {"reauthorization_required", "client_error"} else "chatgpt_refresh_unavailable",
            "ChatGPT 会话更新未完成，请重新授权；不会重复使用结果不明的刷新凭据。" if account.state != "connected" else "ChatGPT 会话暂时无法更新，请稍后再试。") from None
    db.refresh(account, with_for_update=True)
    if account.state != "refreshing":
        raise APIError(409, "chatgpt_refresh_conflict", "ChatGPT 账号状态已变化，请重新授权。")
    replacement.update(issuer=account.issuer, subject=account.subject, client_id=account.client_id,
        ext_agent_host_id=bundle.get("ext_agent_host_id"), saved_at=datetime.now(UTC).isoformat())
    account.secret_ref = save_bundle(replacement, account.secret_ref, db=db)
    connection = db.get(Connection, account.connection_id)
    if set(replacement["scopes"]) != set(account.scopes):
        _invalidate(db, account, "connected" if oauth.SHARING_SCOPES.issubset(set(replacement["scopes"])) else "identity_only")
    account.scopes = replacement["scopes"]
    account.state = "connected" if oauth.SHARING_SCOPES.issubset(set(account.scopes)) else "identity_only"
    account.refresh_started_at = None
    account.updated_at = datetime.now(UTC)
    connection.secret_ref = account.secret_ref
    db.commit()
    if account.state != "connected":
        raise APIError(409, "chatgpt_plan_not_authorized", "此 ChatGPT 账号尚未授权订阅额度，已暂停调用。")
    return replacement["access_token"]


def disconnect(db, account):
    db.refresh(account, with_for_update=True)
    if account.state == "disconnected" and not account.secret_ref:
        return account.remote_revocation_unconfirmed
    if account.state == "refreshing" and account.refresh_started_at and account.refresh_started_at > datetime.now(UTC) - timedelta(seconds=60):
        raise APIError(409, "chatgpt_refresh_in_progress", "ChatGPT 会话正在更新，请稍后再断开。")
    confirmed = not account.secret_ref
    if account.secret_ref:
        try:
            bundle = read_bundle(account)
        except APIError:
            # Corrupt/lost encryption material cannot confirm remote revocation,
            # but must not prevent the user from clearing this local credential.
            bundle = {}
        if bundle.get("refresh_token"):
            try:
                endpoint = oauth.discovery().get("revocation_endpoint")
                if not oauth.auth_url_allowed(endpoint):
                    raise oauth.OAuthFailure("chatgpt_revocation_unavailable")
                oauth.request_json(endpoint, form={"token": bundle["refresh_token"], "token_type_hint": "refresh_token", "client_id": account.client_id}, empty_success=True)
                confirmed = True
            except oauth.OAuthFailure:
                confirmed = False
        else:
            confirmed = False
    _invalidate(db, account, "disconnected", clear_tokens=True)
    account.scopes = []
    account.remote_revocation_unconfirmed = not confirmed
    db.add(AuditEvent(actor_id=account.owner_id, action="chatgpt.disconnected", resource_type="chatgpt_account", resource_id=account.id))
    db.commit()
    return not confirmed
