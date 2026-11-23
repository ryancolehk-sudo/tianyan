import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.auth import COOKIE, APIError, digest, hasher, require_role
from app.cli import bootstrap
from app.db import engine
from app.main import app
from app.models import InstanceState, Instrument, Job, LoginSession, User, WorkerHeartbeat
from app.worker import claim, finish, process, renew

PASSWORD = "isolated-test-fixture-password"
ORIGIN = {"Origin": "http://127.0.0.1:8080"}


def login(client):
    bootstrap("owner", PASSWORD)
    r = client.post("/api/v1/session", json={"username": "owner", "password": PASSWORD}, headers=ORIGIN)
    assert r.status_code == 201
    return r


def test_auth_cookie_csrf_revocation_and_schema(client):
    assert client.get("/api/v1/providers").status_code == 401
    r = login(client)
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "password" not in r.text
    token = client.cookies[COOKIE]
    with Session(engine()) as db:
        row = db.scalar(select(LoginSession))
        assert row.token_hash == digest(token) and token not in row.token_hash
    assert client.get("/api/v1/session").status_code == 200
    assert client.delete("/api/v1/session", headers=ORIGIN).status_code == 403
    csrf = r.json()["data"]["csrf_token"]
    assert client.delete("/api/v1/session", headers={"Origin":"https://wrong.invalid", "X-CSRF-Token":csrf}).status_code == 403
    assert client.delete("/api/v1/session", headers={**ORIGIN, "X-CSRF-Token":csrf}).status_code == 204
    client.cookies.set(COOKIE, token)
    assert client.get("/api/v1/providers").status_code == 401


def test_origin_expiry_rotation_and_single_bootstrap(client):
    r = login(client)
    with pytest.raises(ValueError):
        bootstrap("another", PASSWORD)
    old = client.cookies[COOKIE]
    assert client.post("/api/v1/session", json={"username":"owner","password":PASSWORD}).status_code == 403
    assert client.post("/api/v1/session", json={"username":"owner","password":PASSWORD}, headers=ORIGIN).status_code == 201
    with Session(engine()) as db, db.begin():
        assert db.scalar(select(LoginSession).where(LoginSession.token_hash == digest(old))).revoked_at is not None
        db.execute(update(LoginSession).values(expires_at=datetime.now(UTC)-timedelta(seconds=1)))
    assert client.get("/api/v1/session").status_code == 401
    assert r.json()["meta"]["app_mode"] == "demo"


def test_login_rate_limit_and_no_validation_secret_echo(client):
    bootstrap("owner", PASSWORD)
    body = {"username":"owner", "password":"not-the-password"}
    for _ in range(5):
        assert client.post("/api/v1/session", json=body, headers=ORIGIN).status_code == 401
    assert client.post("/api/v1/session", json=body, headers=ORIGIN).status_code == 429
    secret = "SHOULD_NOT_APPEAR_IN_RESPONSE"
    r = client.post("/api/v1/session", json={"username":"bad space", "password": secret, "extra": secret}, headers=ORIGIN)
    assert r.status_code == 422 and secret not in r.text


def test_health_mode_and_missing_data(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    login(client)
    r = client.get("/api/v1/providers").json()
    assert len(r["data"]["items"]) == 5
    assert not any(p["configured"] or p["enabled"] for p in r["data"]["items"] if p["kind"] != "manual")
    assert next(p for p in r["data"]["items"] if p["kind"] == "manual")["configured"] is True
    empty = client.get("/api/v1/instruments").json()
    assert empty["data"]["items"] == [] and empty["meta"]["quality"]["status"] == "unavailable"
    with Session(engine()) as db, db.begin():
        db.execute(update(InstanceState).values(app_mode="real"))
    assert client.get("/api/v1/providers").status_code == 503
    assert client.get("/health/ready").status_code == 503
    assert client.get("/health/live").status_code == 200


def test_worker_staleness(client):
    with Session(engine()) as db, db.begin():
        db.execute(update(WorkerHeartbeat).values(seen_at=datetime.now(UTC)-timedelta(minutes=2)))
    assert client.get("/health/ready").status_code == 503


def test_instrument_catalogue_missing_empty_and_match(client):
    login(client)
    missing = client.get("/api/v1/instruments", params={"q": "000001"}).json()
    assert missing["meta"]["quality"] == {"status": "unavailable", "reasons": ["标的基础资料尚未导入"]}
    with Session(engine()) as db, db.begin():
        db.add(Instrument(exchange="SZSE", symbol="000001", name="隔离测试标的", valid_from=date(2026, 1, 1)))
    empty = client.get("/api/v1/instruments", params={"q": "999999"}).json()
    assert empty["data"]["items"] == [] and empty["meta"]["quality"] == {"status": "empty",
        "reasons": ["仅查询本机已留存的标的；完整市场目录与历史有效期不由报价推定"]}
    match = client.get("/api/v1/instruments", params={"q": "000001"}).json()
    assert [item["name"] for item in match["data"]["items"]] == ["隔离测试标的"]
    assert match["meta"]["quality"]["status"] == "partial"


def test_job_ownership_and_role_guard(client):
    session = login(client).json()
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        other = User(username="other", password_hash=hasher.hash(PASSWORD), role="viewer")
        db.add(other); db.flush()
        hidden = Job(owner_id=other.id, kind="system.check")
        visible = Job(owner_id=uuid.UUID(session["data"]["user"]["id"]), kind="system.check")
        db.add_all([hidden, visible]); db.flush()
    assert client.get(f"/api/v1/jobs/{hidden.id}").status_code == 404
    assert client.get(f"/api/v1/jobs/{visible.id}").status_code == 200
    with pytest.raises(APIError) as e:
        require_role(other, "admin")
    assert e.value.status == 403


def test_job_fencing_recovery_and_retry_limit():
    bootstrap("owner", PASSWORD)
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(User))
        db.add(Job(owner_id=owner.id, kind="system.check"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: claim(), range(2)))
    jobs = [j for j in claimed if j]
    assert len(jobs) == 1
    first = jobs[0]
    assert renew(first.id, first.lease_token)
    with Session(engine()) as db, db.begin():
        db.execute(update(Job).values(lease_until=datetime.now(UTC)-timedelta(seconds=1)))
    second = claim()
    assert second.attempts == 2 and second.lease_token != first.lease_token
    assert not finish(first.id, first.lease_token)
    assert finish(second.id, second.lease_token, error_code="transient", retry=True)
    with Session(engine()) as db, db.begin():
        db.execute(update(Job).values(available_at=datetime.now(UTC)-timedelta(seconds=1)))
    third = claim()
    assert third.attempts == 3
    assert finish(third.id, third.lease_token, error_code="transient", retry=True)
    assert claim() is None
    with Session(engine()) as db:
        assert db.get(Job, first.id).state == "failed"


def test_internal_check_handler_and_unsupported_kind():
    bootstrap("owner", PASSWORD)
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(User))
        db.add(Job(owner_id=owner.id, kind="system.check"))
    job = claim(); process(job)
    with Session(engine()) as db:
        assert db.get(Job, job.id).state == "succeeded"
    with Session(engine()) as db, db.begin():
        db.add(Job(owner_id=job.owner_id, kind="rule.run"))
    job = claim(); process(job)
    with Session(engine()) as db:
        assert db.get(Job, job.id).error_code == "handler_not_implemented"


def test_database_app_cannot_create_tables():
    restricted = create_engine(os.environ["JL_TEST_APP_URL"])
    try:
        with restricted.begin() as db:
            assert db.execute(text("SELECT count(*) FROM providers")).scalar_one() == 5
        with pytest.raises(DBAPIError):
            with restricted.begin() as db:
                db.execute(text("CREATE TABLE must_not_exist(id int)"))
    finally:
        restricted.dispose()


def test_openapi_and_actual_response_contracts(client):
    spec = json.loads((Path(__file__).resolve().parents[2] / "contracts/openapi.p2.json").read_text(encoding="utf-8"))
    runtime = app.openapi()
    validate(runtime)
    assert set(runtime["paths"]) == set(spec["paths"])
    def check(name, value):
        schema = dict(spec["components"]["schemas"][name], components=spec["components"])
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    check("Health", client.get("/health/ready").json())
    check("Error", client.get("/api/v1/providers").json())
    check("SessionResponse", login(client).json())
    check("ProviderListResponse", client.get("/api/v1/providers").json())
    check("InstrumentListResponse", client.get("/api/v1/instruments").json())
