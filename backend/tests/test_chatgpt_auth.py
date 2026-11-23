"""Offline OIDC, browser binding, owner isolation, and rotating-credential checks.

All OAuth traffic is synthetic; neither a real ChatGPT login nor subscriber request
is made by this suite.
"""
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.ai import vault
from app.ai.models import Connection
from app.auth import APIError, digest, hasher
from app.chatgpt import oauth, service
from app.chatgpt.models import (
    ChatGPTAccount,
    ChatGPTAuthorization,
    ChatGPTIdentity,
    ChatGPTInstallation,
)
from app.config import settings
from app.db import engine
from app.main import _PrivateOAuthAccessLog
from app.models import LoginSession, User

ORIGIN = {"Origin": "http://127.0.0.1:8080"}
ISSUED = "oaiapp_synthetic_registration_a"


@pytest.fixture(scope="module")
def signing_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    return private, {**jwk, "kid": "synthetic-key", "alg": "RS256", "use": "sig"}


@pytest.fixture(autouse=True)
def offline_oauth(tmp_path, monkeypatch, signing_key):
    directory, master = tmp_path / "credentials", tmp_path / "independent-master"
    directory.mkdir()
    master.write_bytes(Fernet.generate_key())
    monkeypatch.setattr(settings(), "ai_secret_directory", str(directory))
    monkeypatch.setattr(settings(), "ai_master_key_file", str(master))
    monkeypatch.setattr(oauth, "signing_keys", lambda **_: [signing_key[1]])
    def deny(*args, **kwargs):
        pytest.fail("No live OAuth network is allowed")
    monkeypatch.setattr(oauth, "request_json", deny)
    return directory


def id_token(signing_key, nonce_value, *, client_id=ISSUED, subject="synthetic-subject", **changes):
    payload = {"iss": oauth.ISSUER, "aud": client_id, "sub": subject, "exp": int(time.time()) + 3600,
        "iat": int(time.time()), "nonce": nonce_value, "email": "same-email@example.invalid", **changes}
    return jwt.encode(payload, signing_key[0], algorithm="RS256", headers={"kid": "synthetic-key"})


def begin(client, *, headers=ORIGIN, mode="login", account_id=None, new_account=False):
    body = {"mode": mode}
    if account_id:
        body["account_id"] = str(account_id)
    if new_account:
        body["new_account"] = True
    result = client.post("/api/v1/chatgpt/login", json=body, headers=headers)
    assert result.status_code == 200, result.text
    return parse_qs(urlsplit(result.json()["data"]["authorization_url"]).query), result


def complete(client, params, monkeypatch, signing_key, *, issued=ISSUED, subject="synthetic-subject", scopes=oauth.SCOPES, extra=None):
    calls = []
    def exchange(url, *, form=None, **_):
        assert url == oauth.TOKEN_URL and form["client_id"] == issued and form["resource"] == oauth.RESOURCE
        assert form["redirect_uri"] == "http://127.0.0.1:8080/api/v1/chatgpt/callback"
        assert form["grant_type"] == "authorization_code" and form["code"] == "synthetic-code"
        calls.append(form.copy())
        return {"id_token": id_token(signing_key, params["nonce"][0], client_id=issued, subject=subject),
            "access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "expires_in": 3600,
            "token_type": "Bearer", "scope": scopes, **(extra or {})}
    monkeypatch.setattr(oauth, "request_json", exchange)
    result = client.get(service.CALLBACK_PATH, params={"state": params["state"][0], "code": "synthetic-code", "client_id": issued}, follow_redirects=False)
    return result, calls


def local_login(client, username="original-admin", *, role="admin"):
    with Session(engine()) as db, db.begin():
        row = User(id=uuid.uuid4(), username=username, password_hash=hasher.hash("synthetic-password-long-enough"), role=role, enabled=True)
        db.add(row)
        owner = row.id
    response = client.post("/api/v1/session", json={"username": username, "password": "synthetic-password-long-enough"}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return owner, {**ORIGIN, "X-CSRF-Token": response.json()["data"]["csrf_token"]}


def registered(client, monkeypatch, signing_key, **kwargs):
    params, _ = begin(client)
    result, calls = complete(client, params, monkeypatch, signing_key, **kwargs)
    assert result.status_code == 303 and result.headers["location"].endswith("chatgpt=signed_in"), result.headers
    account = client.get("/api/v1/chatgpt/accounts").json()["data"]["items"][0]
    return account, calls


def expire_access(account_id):
    with Session(engine()) as db, db.begin():
        account = db.get(ChatGPTAccount, account_id)
        bundle = service.read_bundle(account)
        bundle["expires_at"] = time.time() - 1
        service.save_bundle(bundle, account.secret_ref)


def test_anonymous_status_safe_and_reads_do_not_register(client):
    for _ in range(3):
        value = client.get("/api/v1/chatgpt/status").json()["data"]
        assert value == {"available": True, "local_only": True, "reason": None, "authenticated": False, "accounts": []}
    assert client.get("/api/v1/chatgpt/accounts").status_code == 401
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ChatGPTAuthorization)) == 0
        assert db.scalar(select(func.count()).select_from(ChatGPTInstallation)) == 0


def test_dynamic_registration_pkce_cookie_and_safe_storage(client, monkeypatch, signing_key, offline_oauth):
    params, start = begin(client)
    assert params["client_id"] == ["dynamic_agent_client"] and params["agent_name_hint"] == ["Tianyan"]
    assert params["resource"] == [oauth.RESOURCE] and params["code_challenge_method"] == ["S256"]
    assert params["scope"] == [oauth.SCOPES]
    cookie = start.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/api/v1/chatgpt/callback" in cookie
    with Session(engine()) as db:
        pending = db.scalar(select(ChatGPTAuthorization))
        raw = vault.read_secret(pending.secret_ref)
        verifier = json.loads(raw)["verifier"]
        import base64
        import hashlib
        assert params["code_challenge"] == [base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")]
        assert verifier not in pending.state_hash and pending.nonce_hash == digest(params["nonce"][0])
    result, calls = complete(client, params, monkeypatch, signing_key)
    assert result.headers["location"].endswith("chatgpt=signed_in") and len(calls) == 1
    assert "no-referrer" == result.headers["referrer-policy"]
    owner = client.get("/api/v1/session").json()["data"]["user"]
    assert owner["role"] == "researcher" and owner["username"].startswith("gpt_")
    with Session(engine()) as db:
        account = db.scalar(select(ChatGPTAccount))
        connection = db.get(Connection, account.connection_id)
        assert account.state == "connected" and not account.use_for_ai and account.client_id == ISSUED
        assert connection.owner_id == account.owner_id and connection.template == "chatgpt_plan"
        assert connection.endpoint == oauth.RESOURCE and connection.model is None and not connection.enabled
        assert connection.secret_ref == account.secret_ref and "synthetic-access" not in account.secret_ref
        assert db.scalar(select(ChatGPTAuthorization)).secret_ref is None
    assert len(list(offline_oauth.glob("*.key"))) == 1
    assert all(b"synthetic-access" not in path.read_bytes() and b"synthetic-refresh" not in path.read_bytes() for path in offline_oauth.iterdir())
    text = client.get("/api/v1/chatgpt/status").text + client.get("/api/v1/chatgpt/accounts").text
    assert "synthetic-access" not in text and "synthetic-refresh" not in text and "secret_ref" not in text and "id_token" not in text


@pytest.mark.parametrize("case", ["missing_cookie", "wrong_cookie", "wrong_state", "expired", "duplicate_state", "missing_client", "dynamic_client", "denied"])
def test_callback_rejects_unbound_expired_or_incomplete_without_exchange(client, monkeypatch, case):
    params, _ = begin(client)
    query = {"state": params["state"][0], "code": "synthetic-code", "client_id": ISSUED}
    if case == "missing_cookie":
        client.cookies.clear()
    elif case == "wrong_cookie":
        client.cookies.clear()
        client.cookies.set(service.browser_cookie_name(), "wrong-browser-cookie-at-least-20", path=service.CALLBACK_PATH)
    elif case == "wrong_state":
        query["state"] = "wrong-state-at-least-20-characters"
    elif case == "expired":
        with Session(engine()) as db, db.begin():
            db.execute(update(ChatGPTAuthorization).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    elif case == "duplicate_state":
        query["state"] = [params["state"][0], params["state"][0]]
    elif case == "missing_client":
        query.pop("client_id")
    elif case == "dynamic_client":
        query["client_id"] = "dynamic_agent_client"
    else:
        query["error"] = "access_denied"
    response = client.get(service.CALLBACK_PATH, params=query, follow_redirects=False)
    assert response.status_code == 303 and "chatgpt=chatgpt_" in response.headers["location"]
    assert "synthetic-code" not in response.text and "synthetic-code" not in response.headers["location"]
    assert client.get("/api/v1/session").status_code == 401


def test_callback_is_consumed_once_and_reauthorization_reuses_registration(client, monkeypatch, signing_key):
    first, _ = begin(client)
    result, calls = complete(client, first, monkeypatch, signing_key)
    replay = client.get(service.CALLBACK_PATH, params={"state": first["state"][0], "code": "synthetic-code", "client_id": ISSUED}, follow_redirects=False)
    assert replay.headers["location"].endswith("chatgpt=chatgpt_state_invalid") and len(calls) == 1
    current = client.get("/api/v1/session").json()["data"]
    account = client.get("/api/v1/chatgpt/accounts").json()["data"]["items"][0]
    second, _ = begin(client, mode="connect", headers={**ORIGIN, "X-CSRF-Token": current["csrf_token"]}, account_id=account["id"])
    assert second["client_id"] == [ISSUED] and "agent_name_hint" not in second
    assert second["ext_agent_host_id"] == first["ext_agent_host_id"] and "id_token_hint" not in second
    assert second["login_hint"] == [account["email"]]
    assert first["nonce"] != second["nonce"] and first["state"] != second["state"]
    result, _ = complete(client, second, monkeypatch, signing_key)
    assert result.headers["location"].endswith("chatgpt=connected")
    refreshed = client.get("/api/v1/chatgpt/accounts").json()["data"]["items"]
    assert len(refreshed) == 1 and refreshed[0]["id"] == account["id"] and refreshed[0]["revision"] > account["revision"]


def test_logout_then_return_login_reuses_issued_client_without_disclosing_profile(client, monkeypatch, signing_key):
    first, _ = begin(client)
    completed, _ = complete(client, first, monkeypatch, signing_key)
    cookies = completed.headers.get_list("set-cookie")
    registration_cookie = next(value for value in cookies if value.startswith(service.registration_cookie_name() + "="))
    assert "HttpOnly" in registration_cookie and "SameSite=lax" in registration_cookie
    assert "Max-Age=2592000" in registration_cookie and "Path=/api/v1/chatgpt/login" in registration_cookie
    assert ISSUED in registration_cookie and "synthetic-access" not in registration_cookie and "synthetic-refresh" not in registration_cookie
    current = client.get("/api/v1/session").json()["data"]
    account = client.get("/api/v1/chatgpt/accounts").json()["data"]["items"][0]
    assert client.delete("/api/v1/session", headers={**ORIGIN, "X-CSRF-Token": current["csrf_token"]}).status_code == 204
    assert client.get("/api/v1/session").status_code == 401
    assert client.get("/api/v1/chatgpt/status").json()["data"]["accounts"] == []
    returning, result = begin(client)
    assert returning["client_id"] == [ISSUED] and "agent_name_hint" not in returning
    assert returning["ext_agent_host_id"] == first["ext_agent_host_id"]
    assert "login_hint" not in returning and "id_token_hint" not in returning and account["email"] not in result.text
    with Session(engine()) as db:
        pending = db.scalar(select(ChatGPTAuthorization).where(ChatGPTAuthorization.state_hash == digest(returning["state"][0])))
        assert pending.owner_id is None and pending.session_id is None and str(pending.account_id) == account["id"]
    completed, calls = complete(client, returning, monkeypatch, signing_key)
    assert completed.headers["location"].endswith("chatgpt=signed_in") and len(calls) == 1
    assert client.get("/api/v1/session").json()["data"]["user"]["id"] == current["user"]["id"]
    assert client.get("/api/v1/chatgpt/accounts").json()["data"]["items"][0]["id"] == account["id"]
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ChatGPTAccount)) == 1
        assert db.scalar(select(func.count()).select_from(User)) == 1


def test_returning_client_cookie_is_not_authentication_and_cannot_select_other_subject(client, monkeypatch, signing_key):
    account, _ = registered(client, monkeypatch, signing_key)
    client.cookies.clear()
    client.cookies.set(service.registration_cookie_name(), ISSUED, path=service.REGISTRATION_COOKIE_PATH)
    assert client.get("/api/v1/session").status_code == 401
    assert client.get("/api/v1/chatgpt/status").json()["data"]["accounts"] == []
    returning, _ = begin(client)
    result, calls = complete(client, returning, monkeypatch, signing_key, subject="other-signed-subject")
    assert result.headers["location"].endswith("chatgpt=chatgpt_identity_mismatch") and len(calls) == 1
    assert client.get("/api/v1/session").status_code == 401
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ChatGPTAccount)) == 1
        assert db.get(ChatGPTAccount, uuid.UUID(account["id"])).subject == "synthetic-subject"


def test_new_account_explicitly_ignores_returning_registration_cookie(client, monkeypatch, signing_key):
    account, _ = registered(client, monkeypatch, signing_key)
    current = client.get("/api/v1/session").json()["data"]
    assert client.delete("/api/v1/session", headers={**ORIGIN, "X-CSRF-Token": current["csrf_token"]}).status_code == 204
    fresh, _ = begin(client, new_account=True)
    assert fresh["client_id"] == ["dynamic_agent_client"] and fresh["agent_name_hint"] == ["Tianyan"]
    assert "login_hint" not in fresh and "id_token_hint" not in fresh
    result, _ = complete(client, fresh, monkeypatch, signing_key, issued="oaiapp_explicit_new_registration")
    assert result.headers["location"].endswith("chatgpt=signed_in")
    assert len(client.get("/api/v1/chatgpt/accounts").json()["data"]["items"]) == 2
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.parametrize("hint", ["invalid!cookie", "oaiapp_unknown_registration", "dynamic_agent_client"])
def test_stale_or_invalid_returning_cookie_falls_through_to_dynamic_registration(client, hint):
    client.cookies.set(service.registration_cookie_name(), hint, path=service.REGISTRATION_COOKIE_PATH)
    params, response = begin(client)
    assert params["client_id"] == ["dynamic_agent_client"] and params["agent_name_hint"] == ["Tianyan"]
    assert "login_hint" not in params and "id_token_hint" not in params
    assert client.get("/api/v1/session").status_code == 401


def test_connect_callback_updates_returning_cookie_and_disabled_owner_cannot_return(client, monkeypatch, signing_key):
    owner, headers = local_login(client)
    params, _ = begin(client, headers=headers, mode="connect")
    result, _ = complete(client, params, monkeypatch, signing_key)
    assert result.headers["location"].endswith("chatgpt=connected")
    assert any(value.startswith(service.registration_cookie_name() + "=" + ISSUED) for value in result.headers.get_list("set-cookie"))
    assert client.delete("/api/v1/session", headers=headers).status_code == 204
    with Session(engine()) as db, db.begin():
        db.get(User, owner).enabled = False
    params, start = begin(client)
    assert params["client_id"] == [ISSUED] and "same-email@example.invalid" not in start.text
    result, _ = complete(client, params, monkeypatch, signing_key)
    assert result.headers["location"].endswith("chatgpt=chatgpt_account_disabled")
    assert client.get("/api/v1/session").status_code == 401


def test_login_origin_and_link_csrf_are_required(client):
    assert client.post("/api/v1/chatgpt/login", json={"mode": "login"}).status_code == 403
    assert client.post("/api/v1/chatgpt/login", json={"mode": "login"}, headers={"Origin": "https://wrong.invalid"}).status_code == 403
    assert client.post("/api/v1/chatgpt/login", json={"mode": "connect"}, headers=ORIGIN).status_code == 401
    local_login(client)
    assert client.post("/api/v1/chatgpt/login", json={"mode": "connect"}, headers=ORIGIN).status_code == 403


def test_existing_local_account_binding_preserves_password_role_and_owner(client, monkeypatch, signing_key):
    owner, headers = local_login(client)
    with Session(engine()) as db:
        previous = db.get(User, owner).password_hash
    params, _ = begin(client, headers=headers, mode="connect")
    result, _ = complete(client, params, monkeypatch, signing_key)
    assert result.headers["location"].endswith("chatgpt=connected")
    with Session(engine()) as db:
        assert db.scalar(select(ChatGPTIdentity)).owner_id == owner
        assert db.get(User, owner).password_hash == previous and db.get(User, owner).role == "admin"
        assert db.scalar(select(func.count()).select_from(User)) == 1
    assert client.get("/api/v1/session").json()["data"]["user"]["id"] == str(owner)


def test_binding_does_not_follow_switched_session_and_revoked_original_fails(client, monkeypatch, signing_key):
    owner, headers = local_login(client)
    params, _ = begin(client, headers=headers, mode="connect")
    # Revoking the original session while OAuth is in progress prevents linking.
    assert client.delete("/api/v1/session", headers=headers).status_code == 204
    def deny(*a, **k):
        pytest.fail("Revoked original session must be rejected before token exchange")
    monkeypatch.setattr(oauth, "request_json", deny)
    result = client.get(service.CALLBACK_PATH, params={"state": params["state"][0], "code": "synthetic-code", "client_id": ISSUED}, follow_redirects=False)
    assert result.headers["location"].endswith("chatgpt=chatgpt_original_session_expired")
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ChatGPTIdentity)) == 0


def test_same_email_new_subject_never_merges_and_registrations_stay_separate(client, monkeypatch, signing_key):
    first, _ = registered(client, monkeypatch, signing_key)
    session = client.get("/api/v1/session").json()["data"]
    headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}
    params, _ = begin(client, mode="connect", headers=headers)
    result, _ = complete(client, params, monkeypatch, signing_key, issued="oaiapp_second_registration")
    assert result.headers["location"].endswith("chatgpt=connected")
    rows = client.get("/api/v1/chatgpt/accounts").json()["data"]["items"]
    assert len(rows) == 2 and len({row["label"] for row in rows}) == 2
    assert len({row["email"] for row in rows}) == 1
    client.cookies.clear()
    params, _ = begin(client)
    result, _ = complete(client, params, monkeypatch, signing_key, issued="oaiapp_other_subject", subject="different-validated-subject")
    assert result.headers["location"].endswith("chatgpt=signed_in")
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 2
        assert db.scalar(select(func.count()).select_from(ChatGPTAccount)) == 3


def test_identity_cannot_be_rebound_to_other_local_owner(client, monkeypatch, signing_key):
    account, _ = registered(client, monkeypatch, signing_key)
    client.cookies.clear()
    other, headers = local_login(client, "other-owner", role="researcher")
    assert client.get("/api/v1/chatgpt/accounts").json()["data"]["items"] == []
    assert client.delete("/api/v1/chatgpt/accounts/" + account["id"], headers=headers).status_code == 404
    params, _ = begin(client, mode="connect", headers=headers)
    result, _ = complete(client, params, monkeypatch, signing_key, issued="oaiapp_attempted_rebind")
    assert result.headers["location"].endswith("chatgpt=chatgpt_identity_already_linked")
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ChatGPTAccount)) == 1
        assert db.scalar(select(ChatGPTIdentity)).owner_id != other


def test_identity_only_grant_logs_in_but_never_allows_subscription(client, monkeypatch, signing_key):
    account, _ = registered(client, monkeypatch, signing_key, scopes="openid profile email offline_access")
    assert account["state"] == "identity_only" and not account["sharing_granted"]
    with Session(engine()) as db:
        row = db.get(ChatGPTAccount, uuid.UUID(account["id"]))
        with pytest.raises(APIError, match="不会改用其他计费方式"):
            service.access_token(db, row)


def test_identity_only_response_does_not_require_inference_access_or_refresh_tokens(client, monkeypatch, signing_key):
    account, _ = registered(client, monkeypatch, signing_key, scopes="openid profile email", extra={"access_token": None, "refresh_token": None, "token_type": None, "expires_in": None})
    assert account["state"] == "identity_only" and not account["sharing_granted"]
    assert client.get("/api/v1/session").status_code == 200


@pytest.mark.parametrize("change", ["signature", "issuer", "audience", "expired", "nonce", "missing_iat", "future_iat", "algorithm"])
def test_signed_identity_validation_rejects_each_invalid_claim(signing_key, change):
    changes = {}
    if change == "issuer":
        changes["iss"] = "https://attacker.invalid"
    elif change == "audience":
        changes["aud"] = "wrong-client"
    elif change == "expired":
        changes["exp"] = int(time.time()) - 60
    elif change == "nonce":
        changes["nonce"] = "wrong-nonce"
    elif change == "future_iat":
        changes["iat"] = int(time.time()) + 60
    token = id_token(signing_key, "expected-nonce", **changes)
    if change == "signature":
        wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = id_token((wrong, {}), "expected-nonce")
    elif change == "missing_iat":
        payload = jwt.decode(token, options={"verify_signature": False})
        payload.pop("iat")
        token = jwt.encode(payload, signing_key[0], algorithm="RS256", headers={"kid": "synthetic-key"})
    elif change == "algorithm":
        token = jwt.encode({"sub": "fake"}, "synthetic-offline-hmac-key-at-least-32-bytes", algorithm="HS256", headers={"kid": "synthetic-key"})
    with pytest.raises(oauth.OAuthFailure) as error:
        oauth.verify_identity(token, ISSUED, digest("expected-nonce"))
    assert error.value.code == "chatgpt_identity_invalid"


def test_refresh_rotation_is_atomic_and_does_not_invalidate_tested_configuration(client, monkeypatch, signing_key, offline_oauth):
    account, _ = registered(client, monkeypatch, signing_key)
    identity = uuid.UUID(account["id"])
    expire_access(identity)
    with Session(engine()) as db, db.begin():
        row = db.get(ChatGPTAccount, identity)
        connection = db.get(Connection, row.connection_id)
        connection.enabled = True
        connection.last_test = {"status": "success"}
        connection.tested_revision = connection.revision
        old_ref = row.secret_ref
        old_ciphertext = service._secret_path(old_ref).read_bytes()
        account_revision, connection_revision = row.revision, connection.revision
    calls = []
    def refresh(url, *, form, **_):
        assert form == {"grant_type": "refresh_token", "client_id": ISSUED, "refresh_token": "synthetic-refresh", "resource": oauth.RESOURCE}
        calls.append(form)
        return {"access_token": "synthetic-access-replaced", "refresh_token": "synthetic-refresh-rotated", "token_type": "Bearer", "expires_in": 3600}
    monkeypatch.setattr(oauth, "request_json", refresh)
    with Session(engine(), expire_on_commit=False) as db:
        row = db.get(ChatGPTAccount, identity)
        assert service.access_token(db, row) == "synthetic-access-replaced"
        assert service.access_token(db, row) == "synthetic-access-replaced"
        assert row.state == "connected" and row.revision == account_revision and row.secret_ref == old_ref
        connection = db.get(Connection, row.connection_id)
        assert connection.revision == connection_revision and connection.enabled and connection.tested_revision == connection.revision
        assert service.read_bundle(row)["refresh_token"] == "synthetic-refresh-rotated"
    assert len(calls) == 1 and len(list(offline_oauth.glob("*.key"))) == 1
    assert old_ciphertext != service._secret_path(old_ref).read_bytes()


@pytest.mark.parametrize("failure", ["invalid_grant", "refresh_token_reused", "invalid_client", "unknown"])
def test_terminal_or_uncertain_refresh_never_replays_rotating_token(client, monkeypatch, signing_key, failure):
    account, _ = registered(client, monkeypatch, signing_key)
    identity = uuid.UUID(account["id"])
    expire_access(identity)
    calls = []
    def refresh(*_, **kwargs):
        calls.append(kwargs)
        raise oauth.OAuthFailure("chatgpt_network_unknown" if failure == "unknown" else failure, uncertain=failure == "unknown")
    monkeypatch.setattr(oauth, "request_json", refresh)
    with Session(engine(), expire_on_commit=False) as db:
        row = db.get(ChatGPTAccount, identity)
        reference = row.secret_ref
        for _ in range(2):
            with pytest.raises(APIError):
                service.access_token(db, row)
        db.refresh(row)
        assert row.state == ("refresh_unknown" if failure == "unknown" else "client_error" if failure == "invalid_client" else "reauthorization_required")
        if failure in {"invalid_grant", "refresh_token_reused"}:
            assert row.secret_ref is None and not service._secret_path(reference).exists()
    assert len(calls) == 1


def test_two_process_sessions_cannot_refresh_same_token_concurrently(client, monkeypatch, signing_key):
    account, _ = registered(client, monkeypatch, signing_key)
    identity = uuid.UUID(account["id"])
    expire_access(identity)
    entered, proceed = threading.Event(), threading.Event()
    calls = []
    def refresh(*_, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert proceed.wait(5)
        return {"access_token": "rotated-access", "refresh_token": "rotated-refresh", "token_type": "Bearer", "expires_in": 3600}
    monkeypatch.setattr(oauth, "request_json", refresh)
    def first():
        with Session(engine(), expire_on_commit=False) as db:
            return service.access_token(db, db.get(ChatGPTAccount, identity))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        assert entered.wait(5)
        try:
            with Session(engine()) as db:
                with pytest.raises(APIError):
                    service.access_token(db, db.get(ChatGPTAccount, identity))
        finally:
            proceed.set()
        assert future.result(timeout=5) == "rotated-access"
    assert len(calls) == 1


def test_disconnect_revokes_then_deletes_tokens_preserves_identity_and_billing_preference(client, monkeypatch, signing_key):
    account, _ = registered(client, monkeypatch, signing_key)
    current = client.get("/api/v1/session").json()["data"]
    headers = {**ORIGIN, "X-CSRF-Token": current["csrf_token"]}
    identity = uuid.UUID(account["id"])
    with Session(engine()) as db, db.begin():
        row = db.get(ChatGPTAccount, identity)
        row.use_for_ai = True
        reference = row.secret_ref
    calls = []
    monkeypatch.setattr(oauth, "discovery", lambda: {"revocation_endpoint": oauth.ISSUER + "/oauth/revoke"})
    def revoke(url, *, form, empty_success):
        calls.append((url, form))
        assert form == {"token": "synthetic-refresh", "token_type_hint": "refresh_token", "client_id": ISSUED} and empty_success
        return {}
    monkeypatch.setattr(oauth, "request_json", revoke)
    response = client.delete("/api/v1/chatgpt/accounts/" + account["id"], headers=headers)
    assert response.status_code == 200, response.text
    value = response.json()["data"]
    assert not value["remote_revocation_unconfirmed"] and value["account"]["state"] == "disconnected" and value["account"]["use_for_ai"]
    assert len(calls) == 1 and not service._secret_path(reference).exists()
    with Session(engine()) as db:
        row = db.get(ChatGPTAccount, identity)
        assert row.secret_ref is None and row.client_id == ISSUED
        connection = db.get(Connection, row.connection_id)
        assert connection.secret_ref is None and not connection.enabled
        assert db.scalar(select(func.count()).select_from(ChatGPTIdentity)) == 1
    retry, _ = begin(client, mode="connect", headers=headers, account_id=account["id"])
    assert retry["client_id"] == [ISSUED] and "id_token_hint" not in retry


def test_disconnection_reports_remote_revocation_failure_and_still_clears_local_tokens(client, monkeypatch, signing_key):
    account, _ = registered(client, monkeypatch, signing_key)
    current = client.get("/api/v1/session").json()["data"]
    monkeypatch.setattr(oauth, "discovery", lambda: {"revocation_endpoint": "https://attacker.invalid/revoke"})
    response = client.delete("/api/v1/chatgpt/accounts/" + account["id"], headers={**ORIGIN, "X-CSRF-Token": current["csrf_token"]})
    assert response.status_code == 200 and response.json()["data"]["remote_revocation_unconfirmed"]
    with Session(engine()) as db:
        assert db.get(ChatGPTAccount, uuid.UUID(account["id"])).secret_ref is None
    again = client.delete("/api/v1/chatgpt/accounts/" + account["id"], headers={**ORIGIN, "X-CSRF-Token": current["csrf_token"]})
    assert again.status_code == 200 and again.json()["data"]["remote_revocation_unconfirmed"]


def test_reauthorization_file_failure_keeps_old_configuration_durably_disabled(client, monkeypatch, signing_key, offline_oauth):
    account, _ = registered(client, monkeypatch, signing_key)
    current = client.get("/api/v1/session").json()["data"]
    identity = uuid.UUID(account["id"])
    with Session(engine()) as db, db.begin():
        row = db.get(ChatGPTAccount, identity)
        connection = db.get(Connection, row.connection_id)
        connection.enabled, connection.tested_revision = True, connection.revision
        connection.last_test = {"status": "success"}
    params, _ = begin(client, mode="connect", headers={**ORIGIN, "X-CSRF-Token": current["csrf_token"]}, account_id=identity)
    def fail(*args, **kwargs):
        raise APIError(503, "credential_store_unavailable", "synthetic write failure")
    monkeypatch.setattr(service, "save_bundle", fail)
    result, _ = complete(client, params, monkeypatch, signing_key)
    assert result.headers["location"].endswith("chatgpt=chatgpt_authorization_failed")
    with Session(engine()) as db:
        row = db.get(ChatGPTAccount, identity)
        connection = db.get(Connection, row.connection_id)
        assert row.state == "disconnected" and row.revision > account["revision"]
        assert not connection.enabled and connection.last_test is None and connection.tested_revision is None
    assert len(list(offline_oauth.glob("*.key"))) == 1


def test_new_registration_final_session_failure_cleans_uncommitted_token_file(client, monkeypatch, signing_key, offline_oauth):
    params, _ = begin(client)
    def fail(*args, **kwargs):
        raise APIError(503, "session_write_failed", "synthetic session failure")
    monkeypatch.setattr(service, "issue_session", fail)
    result, _ = complete(client, params, monkeypatch, signing_key)
    assert result.headers["location"].endswith("chatgpt=chatgpt_authorization_failed")
    assert list(offline_oauth.glob("*.key")) == []
    with Session(engine()) as db:
        row = db.scalar(select(ChatGPTAccount))
        assert row.state == "disconnected" and row.secret_ref is None
        assert not db.get(Connection, row.connection_id).enabled
        assert db.scalar(select(func.count()).select_from(LoginSession)) == 0


def test_logout_between_durable_fence_and_token_write_cannot_finish_binding(client, monkeypatch, signing_key):
    account, _ = registered(client, monkeypatch, signing_key)
    current = client.get("/api/v1/session").json()["data"]
    headers = {**ORIGIN, "X-CSRF-Token": current["csrf_token"]}
    params, _ = begin(client, mode="connect", headers=headers, account_id=account["id"])
    with Session(engine()) as db:
        row = db.get(ChatGPTAccount, uuid.UUID(account["id"]))
        old_ciphertext = service._secret_path(row.secret_ref).read_bytes()
        original_session_id = db.scalar(select(ChatGPTAuthorization.session_id).where(ChatGPTAuthorization.state_hash == digest(params["state"][0])))
    original_commit = Session.commit
    fired = []
    def commit_and_logout(db):
        is_fence = any(isinstance(item, ChatGPTAccount) and item.state == "disconnected" for item in db.identity_map.values())
        result = original_commit(db)
        if is_fence and not fired:
            fired.append(True)
            with engine().begin() as connection:
                connection.execute(update(LoginSession).where(LoginSession.id == original_session_id).values(revoked_at=datetime.now(UTC)))
        return result
    monkeypatch.setattr(Session, "commit", commit_and_logout)
    result, _ = complete(client, params, monkeypatch, signing_key)
    assert fired and result.headers["location"].endswith("chatgpt=chatgpt_original_session_expired")
    with Session(engine()) as db:
        row = db.get(ChatGPTAccount, uuid.UUID(account["id"]))
        assert row.state == "disconnected"
        assert service._secret_path(row.secret_ref).read_bytes() == old_ciphertext


@pytest.mark.parametrize("recovery", ["reauthorize", "disconnect"])
def test_interrupted_refresh_can_recover_after_timeout_without_refresh_replay(client, monkeypatch, signing_key, recovery):
    account, _ = registered(client, monkeypatch, signing_key)
    current = client.get("/api/v1/session").json()["data"]
    headers = {**ORIGIN, "X-CSRF-Token": current["csrf_token"]}
    identity = uuid.UUID(account["id"])
    with Session(engine()) as db, db.begin():
        row = db.get(ChatGPTAccount, identity)
        row.state, row.refresh_started_at = "refreshing", datetime.now(UTC) - timedelta(minutes=5)
    view = client.get("/api/v1/chatgpt/accounts").json()["data"]["items"][0]
    assert view["state"] == "refresh_unknown" and view["reauthorization_required"]
    if recovery == "reauthorize":
        params, _ = begin(client, mode="connect", headers=headers, account_id=identity)
        result, calls = complete(client, params, monkeypatch, signing_key)
        assert result.headers["location"].endswith("chatgpt=connected") and len(calls) == 1
        assert calls[0]["grant_type"] == "authorization_code"
    else:
        monkeypatch.setattr(oauth, "discovery", lambda: {"revocation_endpoint": oauth.ISSUER + "/oauth/revoke"})
        calls = []
        def revoke(*args, **kwargs):
            calls.append(kwargs["form"])
            return {}
        monkeypatch.setattr(oauth, "request_json", revoke)
        result = client.delete("/api/v1/chatgpt/accounts/" + account["id"], headers=headers)
        assert result.status_code == 200 and len(calls) == 1
        assert "grant_type" not in calls[0]


def test_remote_origins_cannot_offer_loopback_subscription_login(client, monkeypatch):
    monkeypatch.setattr(settings(), "public_origin", "https://example.invalid")
    value = client.get("/api/v1/chatgpt/status").json()["data"]
    assert not value["available"] and value["local_only"]
    assert client.post("/api/v1/chatgpt/login", json={"mode": "login"}, headers={"Origin": "https://example.invalid"}).status_code == 409


def test_callback_access_logs_drop_query_material():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/api/v1/chatgpt/callback?code=DO_NOT_LOG&state=PRIVATE", "1.1", 303), None)
    assert _PrivateOAuthAccessLog().filter(record)
    assert "DO_NOT_LOG" not in record.getMessage() and "PRIVATE" not in record.getMessage()
