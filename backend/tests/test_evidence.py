"""P2-001 adversarial tests. All content, times and calendars are synthetic fixtures."""
import copy
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.auth import COOKIE, hasher
from app.cli import bootstrap
from app.db import engine
from app.evidence import schemas as s
from app.evidence.common import content_hash, normalized
from app.evidence.models import CalendarVersion, EvidenceVersion, IdempotencyKey, Snapshot, SnapshotItem, SourceHealth, SourceVersion, TradingSession
from app.evidence.service import freeze_snapshot, snapshot_selection
from app.main import app
from app.models import AuditEvent, Job, Provider, User
from app.schemas import Provider as ProviderDTO
from app.worker import claim, lease_renewal, process
from app.config import settings

PASSWORD = "synthetic-evidence-fixture-password"
ORIGIN = {"Origin": "http://127.0.0.1:8080"}


def authenticated(client, role="admin"):
    bootstrap("owner", PASSWORD)
    with Session(engine()) as db, db.begin():
        user = db.scalar(select(User).where(User.username == "owner"))
        user.role = role
    data = client.post("/api/v1/session", json={"username": "owner", "password": PASSWORD}, headers=ORIGIN).json()["data"]
    return {**ORIGIN, "X-CSRF-Token": data["csrf_token"]}, uuid.UUID(data["user"]["id"])


def manual_id(client):
    return next(item["id"] for item in client.get("/api/v1/providers").json()["data"]["items"] if item["kind"] == "manual")


def body_for(source, key="synthetic-note", revision="1", text_value="合成研究材料，不是行情或真实观点"):
    return {"source_id": str(source), "source_key": key, "source_revision": revision, "kind": "manual", "authorship": "original",
            "payload": {"text": text_value, "source_url": None}, "rights_basis": "自制隔离测试材料"}


def post(client, path, body, headers, key=None):
    return client.post(path, json=body, headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())})


def snapshot(client, headers, *, cutoff=None, required=None, keys=None):
    body = {"as_of_time": (cutoff or datetime.now(UTC)).isoformat(), "required_capabilities": required or []}
    if keys:
        body["source_keys"] = keys
    response = post(client, "/api/v1/snapshots", body, headers)
    assert response.status_code == 202, response.text
    return response.json()["data"]


def finish_snapshot(client, receipt):
    job = claim(); assert job and str(job.id) == receipt["job_id"]
    process(job)
    result = client.get("/api/v1/snapshots/" + receipt["result_ref"]["id"])
    assert result.status_code == 200
    return result.json()["data"]


def fixture_evidence(owner, source, key, revision, received, *, published=None, previous=None, number=1,
                     eligible=True, event_at=None):
    # Direct fixture insertion deliberately controls time; the public API forbids these fields.
    content = body_for(source, key, revision)
    row = EvidenceVersion(source_id=source, owner_id=owner, visibility="private", source_key=key,
        source_revision=revision, version_number=number, kind="manual", instrument_id=None, event_at=event_at,
        published_at=published or received, received_at=received, available_at=max(received, published or received),
        availability="known", provenance="manual" if eligible else "unverified", historical_eligible=eligible,
        rights_basis=content["rights_basis"], payload=content["payload"], content_hash=content_hash(content), supersedes_id=previous)
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        db.add(row); db.flush()
    return row


def calendar_body():
    # These dates and open/closed flags are a synthetic calendar, not an exchange assertion.
    segments = [{"start": "09:15", "end": "09:25", "kind": "auction"},
                {"start": "09:30", "end": "11:30", "kind": "continuous"},
                {"start": "13:00", "end": "15:00", "kind": "continuous"}]
    return {"exchange": "SSE", "version": "synthetic-calendar-v1", "source_ref": "自制合成日历，不能用于真实交易日期",
        "rights_basis": "自制测试材料", "coverage_start": "2001-12-31", "coverage_end": "2002-01-02",
        "sessions": [{"trade_day": "2001-12-31", "is_open": True, "segments": segments},
                     {"trade_day": "2002-01-01", "is_open": False, "segments": []},
                     {"trade_day": "2002-01-02", "is_open": True, "segments": segments}]}


def test_original_note_has_server_time_private_owner_and_no_content_logs(client, caplog):
    headers, owner = authenticated(client, "researcher")
    body = body_for(manual_id(client), text_value="ONLY_IN_PRIVATE_RECORD_NOT_IN_LOG_731")
    before = datetime.now(UTC)
    response = post(client, "/api/v1/evidence", body, headers)
    assert response.status_code == 201
    row = response.json()["data"]
    assert row["owner_id"] == str(owner) and row["visibility"] == "private"
    received = datetime.fromisoformat(row["received_at"].replace("Z", "+00:00"))
    assert before <= received <= datetime.now(UTC)
    assert row["published_at"] == row["received_at"] == row["available_at"]
    assert row["provenance"] == "manual" and row["historical_eligible"] is True
    assert row["content_hash"] == content_hash(s.EvidenceInput.model_validate(body))
    assert client.get("/api/v1/evidence/" + row["id"]).json()["data"] == row
    health = client.get("/api/v1/source-health").json()["data"]["items"]
    assert len(health) == 1 and health[0]["coverage"] == {"expected": 1, "accepted": 1, "duplicates": 0}
    assert body["payload"]["text"] not in caplog.text
    with Session(engine()) as db:
        audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "evidence.import"))
        assert audit.resource_id == uuid.UUID(row["id"]) and audit.changed_field_names == ["created"]


@pytest.mark.parametrize("forbidden,value", [("owner_id", str(uuid.uuid4())), ("visibility", "shared"),
    ("received_at", "2000-01-01T00:00:00Z"), ("available_at", "2000-01-01T00:00:00Z"),
    ("historical_eligible", True), ("app_mode", "real"), ("version_number", 99)])
def test_client_cannot_override_trust_owner_mode_or_receive_time(client, forbidden, value):
    headers, _ = authenticated(client)
    body = {**body_for(manual_id(client)), forbidden: value}
    assert post(client, "/api/v1/evidence", body, headers).status_code == 422
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(EvidenceVersion)) == 0


def test_write_requires_session_csrf_role_and_idempotency_key(client):
    assert client.post("/api/v1/evidence", json={}).status_code == 401
    headers, _ = authenticated(client, "viewer")
    body = body_for(manual_id(client))
    assert post(client, "/api/v1/evidence", body, ORIGIN).status_code == 403
    assert post(client, "/api/v1/evidence", body, headers).status_code == 403
    with Session(engine()) as db, db.begin():
        db.execute(update(User).values(role="researcher"))
    assert client.post("/api/v1/evidence", json=body, headers=headers).status_code == 422
    assert post(client, "/api/v1/evidence", body, {**headers, "Origin": "https://wrong.invalid"}).status_code == 403
    assert post(client, "/api/v1/calendar-versions", calendar_body(), headers).status_code == 403
    assert post(client, "/api/v1/evidence", body, headers).status_code == 201


def test_idempotency_replays_exact_response_and_conflicts_do_not_mutate(client):
    headers, _ = authenticated(client)
    body = body_for(manual_id(client))
    first = post(client, "/api/v1/evidence", body, headers, "test-same-request")
    again = post(client, "/api/v1/evidence", body, headers, "test-same-request")
    assert first.status_code == again.status_code == 201 and again.json() == first.json()
    assert again.headers["idempotency-replayed"] == "true"
    assert again.headers["x-request-id"] == again.json()["meta"]["request_id"]
    changed = copy.deepcopy(body); changed["payload"]["text"] = "冲突内容仍为合成材料"
    assert post(client, "/api/v1/evidence", changed, headers, "test-same-request").json()["error"]["code"] == "idempotency_conflict"
    assert post(client, "/api/v1/evidence", changed, headers).json()["error"]["code"] == "source_revision_conflict"
    duplicate = post(client, "/api/v1/evidence", body, headers)
    assert duplicate.status_code == 201 and duplicate.json()["data"]["id"] == first.json()["data"]["id"]
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(EvidenceVersion)) == 1
        assert db.scalar(select(func.count()).select_from(SourceHealth)) == 1


def test_concurrent_retries_and_natural_duplicate_have_one_material(client):
    headers, _ = authenticated(client)
    body = body_for(manual_id(client)); token = client.cookies[COOKIE]
    def send(key):
        with TestClient(app, base_url="http://127.0.0.1:8080") as concurrent:
            concurrent.cookies.set(COOKIE, token)
            return post(concurrent, "/api/v1/evidence", body, headers, key)
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(send, ["parallel-same-key", "parallel-same-key", "parallel-other-key"]))
    assert [r.status_code for r in responses] == [201, 201, 201]
    assert len({r.json()["data"]["id"] for r in responses}) == 1
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(EvidenceVersion)) == 1


def test_corrections_append_order_without_lexical_revision_guess_or_branch(client):
    headers, _ = authenticated(client); source = manual_id(client)
    first = post(client, "/api/v1/evidence", body_for(source, revision="10"), headers).json()["data"]
    correction = {**body_for(source, revision="2", text_value="新的合成更正"), "supersedes_id": first["id"]}
    second = post(client, "/api/v1/evidence", correction, headers).json()["data"]
    assert second["version_number"] == 2 and second["supersedes_id"] == first["id"]
    assert post(client, "/api/v1/evidence", {**correction, "source_revision": "branch"}, headers).status_code == 409
    assert post(client, "/api/v1/evidence", body_for(source, revision="3"), headers).status_code == 409
    assert client.get("/api/v1/evidence/" + first["id"]).json()["data"]["payload"]["text"] != second["payload"]["text"]
    frozen = finish_snapshot(client, snapshot(client, headers))
    assert [item["evidence_version_id"] for item in frozen["items"]] == [second["id"]]
    for statement in ["UPDATE evidence_versions SET source_revision='overwritten'", "DELETE FROM evidence_versions"]:
        with pytest.raises(DBAPIError), engine().begin() as db:
            db.execute(text(statement))


@pytest.mark.parametrize("change", [
    {"payload": {"text": "bad\0text", "source_url": None}},
    {"payload": {"text": "外部链接测试", "source_url": "javascript:alert(1)"}},
    {"payload": {"text": "外部链接测试", "source_url": "https://user:password@example.invalid/"}},
    {"kind": "news"}, {"published_at": "2026-10-01T12:00:00"},
    {"payload": {"text": "x" * 20001, "source_url": None}},
    {"kind": "quote", "payload": {"price": 12.3, "volume_shares": "100", "amount_cny": "1230", "quote_time": "2001-01-01T09:20:00Z"}},
])
def test_invalid_material_rejected_without_partial_write(client, change):
    headers, _ = authenticated(client)
    assert post(client, "/api/v1/evidence", {**body_for(manual_id(client)), **change}, headers).status_code == 422
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(EvidenceVersion)) == 0


def test_imported_unknown_or_backdated_external_material_is_not_trusted_history(client):
    headers, _ = authenticated(client); source = manual_id(client)
    external = {**body_for(source), "authorship": "external", "kind": "news", "payload": {"title": "合成资讯标题", "summary": "合成摘要", "source_url": "https://example.invalid/synthetic"}}
    unknown = post(client, "/api/v1/evidence", external, headers).json()["data"]
    assert unknown["available_at"] is None and unknown["historical_eligible"] is False
    reported = post(client, "/api/v1/evidence", {**external, "source_key": "reported", "published_at": "2001-01-01T09:20:00Z"}, headers).json()["data"]
    assert reported["available_at"] == reported["received_at"] and reported["provenance"] == "unverified"
    frozen = finish_snapshot(client, snapshot(client, headers))
    assert frozen["items"] == [] and frozen["quality"]["status"] == "unavailable"
    assert client.get("/api/v1/evidence", params={"as_of_time": "2001-01-01T09:25:00Z"}).json()["data"]["items"] == []


def test_missing_authorship_is_external_and_original_cannot_claim_old_publication(client):
    headers, _ = authenticated(client); body = body_for(manual_id(client))
    body.pop("authorship")
    response = post(client, "/api/v1/evidence", body, headers)
    assert response.status_code == 201 and response.json()["data"]["historical_eligible"] is False
    invalid = {**body, "authorship": "original", "published_at": "2001-01-01T00:00:00Z"}
    assert post(client, "/api/v1/evidence", invalid, headers).status_code == 422


def test_cutoff_before_latest_correction_preserves_old_visible_version(client):
    headers, owner = authenticated(client); source = uuid.UUID(manual_id(client)); cutoff = datetime.now(UTC) - timedelta(hours=1)
    first = fixture_evidence(owner, source, "cutoff", "10", cutoff - timedelta(minutes=10))
    fixture_evidence(owner, source, "cutoff", "2", cutoff + timedelta(minutes=10), previous=first.id, number=2)
    fixture_evidence(owner, source, "late", "1", cutoff + timedelta(minutes=6), published=cutoff - timedelta(minutes=5))
    fixture_evidence(owner, source, "future-event", "1", cutoff - timedelta(minutes=6), event_at=cutoff + timedelta(minutes=1))
    frozen = finish_snapshot(client, snapshot(client, headers, cutoff=cutoff))
    assert [item["evidence_version_id"] for item in frozen["items"]] == [str(first.id)]
    assert content_hash(frozen["manifest"]) == frozen["manifest_hash"]


def test_consistent_read_excludes_a_transaction_committed_after_selection_began(client):
    _, owner = authenticated(client); source = uuid.UUID(manual_id(client)); cutoff = datetime.now(UTC) - timedelta(minutes=1)
    first = fixture_evidence(owner, source, "before-selection", "1", cutoff - timedelta(minutes=2))
    entered, release = threading.Event(), threading.Event()
    def pause(conn, cursor, statement, parameters, context, many):
        if "FROM providers ORDER BY" in statement and not entered.is_set():
            entered.set(); assert release.wait(8)
    event.listen(engine(), "after_cursor_execute", pause)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(snapshot_selection, owner, s.SnapshotInput(as_of_time=cutoff, required_capabilities=[]))
            assert entered.wait(5)
            fixture_evidence(owner, source, "committed-later", "1", cutoff - timedelta(minutes=1))
            release.set(); selection = task.result(timeout=8)
    finally:
        release.set(); event.remove(engine(), "after_cursor_execute", pause)
    assert [item["evidence_version_id"] for item in selection["items"]] == [str(first.id)]


def test_late_worker_uses_captured_material_and_source_versions(client):
    headers, _ = authenticated(client); source = manual_id(client)
    first = post(client, "/api/v1/evidence", body_for(source), headers).json()["data"]
    receipt = snapshot(client, headers)
    post(client, "/api/v1/evidence", {**body_for(source, revision="2"), "supersedes_id": first["id"]}, headers)
    with Session(engine()) as db, db.begin():
        descriptor = normalized(ProviderDTO.model_validate(db.get(Provider, uuid.UUID(source))))
        descriptor["adapter_version"] = "synthetic-later-version"
        db.add(SourceVersion(source_id=uuid.UUID(source), recorded_at=datetime.now(UTC), descriptor=descriptor, content_hash=content_hash(descriptor)))
    frozen = finish_snapshot(client, receipt)
    assert [item["evidence_version_id"] for item in frozen["items"]] == [first["id"]]
    assert all(item["descriptor"]["adapter_version"] != "synthetic-later-version" for item in frozen["manifest"]["sources"])
    for statement in ["UPDATE snapshots SET state='pending'", "DELETE FROM snapshots", "UPDATE snapshot_items SET content_hash=repeat('a',64)"]:
        with pytest.raises(DBAPIError), engine().begin() as db:
            db.execute(text(statement))


def test_expired_worker_cannot_publish_result_after_lease_reclaimed(client):
    headers, _ = authenticated(client)
    receipt = snapshot(client, headers)
    original = claim(); assert original
    with Session(engine()) as db, db.begin():
        db.execute(update(Job).where(Job.id == original.id).values(lease_until=datetime.now(UTC)-timedelta(seconds=1)))
    current = claim(); assert current and current.attempts == 2
    assert freeze_snapshot(original) is False
    assert client.get("/api/v1/snapshots/" + receipt["result_ref"]["id"]).json()["data"]["state"] == "pending"
    assert freeze_snapshot(current) is True
    with Session(engine()) as db:
        assert db.get(Job, current.id).state == "succeeded"


def test_snapshot_missing_capability_empty_and_future_are_distinct(client):
    headers, _ = authenticated(client)
    empty = finish_snapshot(client, snapshot(client, headers))
    assert empty["state"] == "ready" and empty["quality"]["status"] == "empty" and empty["manifest_hash"]
    missing = finish_snapshot(client, snapshot(client, headers, required=["market.realtime"]))
    assert missing["state"] == "ready" and missing["quality"]["status"] == "unavailable"
    future = {"as_of_time": (datetime.now(UTC)+timedelta(hours=1)).isoformat(), "required_capabilities": []}
    assert post(client, "/api/v1/snapshots", future, headers).status_code == 422


def test_snapshot_retries_exhausted_are_terminal_for_task_and_resource(client):
    headers, _ = authenticated(client)
    receipt = snapshot(client, headers)
    for _ in range(3):
        job = claim(); assert job
        with Session(engine()) as db, db.begin():
            db.execute(update(Job).where(Job.id == job.id).values(lease_until=datetime.now(UTC)-timedelta(seconds=1)))
    assert claim() is None
    resource = client.get("/api/v1/snapshots/" + receipt["result_ref"]["id"]).json()["data"]
    assert resource["state"] == "failed" and resource["error_code"] == "retry_exhausted"


def test_snapshot_lease_is_renewed_while_work_is_in_progress(client, monkeypatch):
    headers, _ = authenticated(client)
    receipt = snapshot(client, headers)
    monkeypatch.setattr(settings(), "lease_seconds", 5)
    job = claim(); first_until = job.lease_until
    with lease_renewal(job):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with Session(engine()) as db:
                if db.get(Job, job.id).lease_until > first_until:
                    break
            time.sleep(0.1)
        else:
            pytest.fail("The live lease was never renewed")
        assert freeze_snapshot(job)
    assert client.get("/api/v1/snapshots/" + receipt["result_ref"]["id"]).json()["data"]["state"] == "ready"


def test_private_resources_and_signed_cursors_do_not_cross_owner_or_filters(client):
    headers, _ = authenticated(client); source = manual_id(client)
    for i in range(3):
        assert post(client, "/api/v1/evidence", body_for(source, key=f"pagination-{i}"), headers).status_code == 201
    first = client.get("/api/v1/evidence?limit=1").json()["data"]
    cursor = first["next_cursor"]; assert cursor
    second = client.get("/api/v1/evidence", params={"limit": 1, "cursor": cursor}).json()["data"]
    assert second["items"][0]["id"] != first["items"][0]["id"]
    assert client.get("/api/v1/evidence", params={"cursor": cursor, "kind": "news"}).status_code == 422
    assert client.get("/api/v1/evidence", params={"cursor": cursor[:-2] + "XX"}).status_code == 422
    assert client.get("/api/v1/evidence", params={"cursor": ("A" if cursor[0] != "A" else "B") + cursor[1:]}).status_code == 422
    receipt = snapshot(client, headers)
    with Session(engine()) as db, db.begin():
        db.add(User(username="other", password_hash=hasher.hash(PASSWORD), role="admin"))
    other_session = client.post("/api/v1/session", json={"username": "other", "password": PASSWORD}, headers=ORIGIN).json()["data"]
    assert client.get("/api/v1/evidence/" + first["items"][0]["id"]).status_code == 404
    assert client.get("/api/v1/snapshots/" + receipt["result_ref"]["id"]).status_code == 404
    assert client.get("/api/v1/source-health").json()["data"]["items"] == []
    assert client.get("/api/v1/evidence", params={"cursor": cursor}).status_code == 422
    own_receipt = snapshot(client, {**ORIGIN, "X-CSRF-Token": other_session["csrf_token"]})
    with pytest.raises(DBAPIError), engine().begin() as db:
        db.execute(insert(SnapshotItem).values(snapshot_id=uuid.UUID(own_receipt["result_ref"]["id"]),
            evidence_version_id=uuid.UUID(first["items"][0]["id"]), content_hash=first["items"][0]["content_hash"]))


def test_calendar_missing_weekend_holiday_lunch_cross_year_and_coverage(client):
    headers, _ = authenticated(client)
    params = {"exchange": "SSE", "trade_day": "2001-12-31", "calendar_version": "synthetic-calendar-v1"}
    assert client.get("/api/v1/trading-sessions", params=params).json()["meta"]["quality"]["status"] == "unavailable"
    response = post(client, "/api/v1/calendar-versions", calendar_body(), headers)
    assert response.status_code == 201
    calendar = response.json()["data"]
    assert client.get("/api/v1/trading-sessions", params={**params, "trade_day": "2002-01-01"}).json()["data"]["session"]["is_open"] is False
    assert client.get("/api/v1/trading-sessions", params={**params, "trade_day": "2002-01-03"}).json()["data"]["session"] is None
    next_url = f"/api/v1/calendar-versions/{calendar['id']}/next-session"
    assert client.get(next_url, params={"after": "2001-12-31"}).json()["data"]["session"]["trade_day"] == "2002-01-02"
    assert client.get(next_url, params={"after": "2002-01-02"}).json()["data"]["session"] is None
    elapsed = f"/api/v1/calendar-versions/{calendar['id']}/elapsed"
    assert client.get(elapsed, params={"start_at": "2001-12-31T11:25:00+08:00", "end_at": "2001-12-31T13:05:00+08:00"}).json()["data"]["seconds"] == 600
    assert client.get(elapsed, params={"start_at": "2001-12-31T09:15:00+08:00", "end_at": "2001-12-31T09:25:00+08:00"}).json()["data"]["seconds"] == 0
    assert client.get(elapsed, params={"start_at": "2001-12-30T09:30:00+08:00", "end_at": "2001-12-31T10:00:00+08:00"}).json()["data"]["seconds"] is None


def test_calendar_conflicts_gaps_overlap_and_immutability(client):
    headers, _ = authenticated(client); body = calendar_body()
    first = post(client, "/api/v1/calendar-versions", body, headers).json()["data"]
    assert post(client, "/api/v1/calendar-versions", {**body, "sessions": list(reversed(body["sessions"]))}, headers).json()["data"]["id"] == first["id"]
    changed = copy.deepcopy(body); changed["source_ref"] = "不同来源"
    assert post(client, "/api/v1/calendar-versions", changed, headers).status_code == 409
    invalid = [copy.deepcopy(body) for _ in range(4)]
    invalid[0]["sessions"].pop()
    invalid[1]["sessions"][0]["segments"][1]["start"] = "09:20"
    invalid[2]["sessions"][1]["segments"] = body["sessions"][0]["segments"]
    invalid[3]["sessions"][0]["is_open"] = "true"
    for item in invalid:
        assert post(client, "/api/v1/calendar-versions", {**item, "version": "invalid-version"}, headers).status_code == 422
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(CalendarVersion)) == 1
        assert db.scalar(select(func.count()).select_from(TradingSession)) == 3
    with pytest.raises(DBAPIError), engine().begin() as db:
        db.execute(text("UPDATE calendar_versions SET version='overwritten'"))


def test_implemented_p2_request_and_response_contracts(client):
    headers, _ = authenticated(client)
    spec = json.loads((Path(__file__).resolve().parents[2] / "contracts/openapi.p2.json").read_text(encoding="utf-8"))
    def check(name, value):
        Draft202012Validator({**spec["components"]["schemas"][name], "components": spec["components"]}, format_checker=FormatChecker()).validate(value)
    check("EvidenceVersionResponse", post(client, "/api/v1/evidence", body_for(manual_id(client)), headers).json())
    check("EvidenceVersionListResponse", client.get("/api/v1/evidence").json())
    receipt = snapshot(client, headers)
    finish_snapshot(client, receipt)
    check("SnapshotResponse", client.get("/api/v1/snapshots/" + receipt["result_ref"]["id"]).json())
    check("SourceHealthListResponse", client.get("/api/v1/source-health").json())
    check("CalendarVersionResponse", post(client, "/api/v1/calendar-versions", calendar_body(), headers).json())
