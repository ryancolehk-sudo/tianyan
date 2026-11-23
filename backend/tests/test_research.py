"""P2-002 adversarial tests; every account/material/calendar is disposable and synthetic."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.auth import hasher
from app.cli import bootstrap
from app.db import engine
from app.main import app
from app.models import AuditEvent, User
from app.research.models import DocumentRevision, PlanScenario, PublicationEvent, ResearchDocument, RevisionLink
from app.worker import claim, process

PASSWORD = "synthetic-research-fixture-password"
ORIGIN = {"Origin": "http://127.0.0.1:8080"}
DAY = "2001-12-31"


def post(client, path, body, headers, *, key=None, etag=None):
    values = {**headers, "Idempotency-Key": key or str(uuid.uuid4())}
    if etag is not None:
        values["If-Match"] = etag
    return client.post(path, json=body, headers=values)


@pytest.fixture
def workspace(client):
    bootstrap("research-owner", PASSWORD)
    login = client.post("/api/v1/session", json={"username": "research-owner", "password": PASSWORD}, headers=ORIGIN).json()["data"]
    headers = {**ORIGIN, "X-CSRF-Token": login["csrf_token"]}
    source = next(row["id"] for row in client.get("/api/v1/providers").json()["data"]["items"] if row["kind"] == "manual")
    material = post(client, "/api/v1/evidence", {"source_id": source, "source_key": "synthetic-research-note", "source_revision": "1",
        "kind": "manual", "authorship": "original", "payload": {"text": "Synthetic research evidence, no actual market facts."},
        "rights_basis": "self-created disposable fixture"}, headers).json()["data"]
    # The server's accepted evidence clock is authoritative. A client wall clock
    # can trail PostgreSQL under virtualized load and accidentally exclude it.
    receipt = post(client, "/api/v1/snapshots", {"as_of_time": material['received_at'], "required_capabilities": []}, headers).json()["data"]
    process(claim())
    saved = client.get('/api/v1/snapshots/' + receipt['result_ref']['id']).json()['data']
    assert material['id'] in [item['evidence_version_id'] for item in saved['items']]
    segments = [{"start": "09:30", "end": "11:30", "kind": "continuous"}, {"start": "13:00", "end": "15:00", "kind": "continuous"}]
    calendar = post(client, "/api/v1/calendar-versions", {"exchange": "SSE", "version": "synthetic-research-calendar",
        "source_ref": "self-created synthetic calendar, not actual exchange dates", "rights_basis": "self-created",
        "coverage_start": DAY, "coverage_end": "2002-01-02", "sessions": [
            {"trade_day": DAY, "is_open": True, "segments": segments},
            {"trade_day": "2002-01-01", "is_open": False, "segments": []},
            {"trade_day": "2002-01-02", "is_open": True, "segments": segments}]}, headers).json()["data"]
    return {"client": client, "headers": headers, "owner": login["user"]["id"], "snapshot": receipt["result_ref"]["id"],
            "evidence": material["id"], "calendar": calendar["id"]}


def revision(w, kind="review", parents=None):
    if kind == "review":
        content = {"type": kind, "summary": "合成人工复盘原文，不能当作真实市场结论", "evidence_refs": [w["evidence"]], "limitations": []}
    elif kind == "direction":
        content = {"type": kind, "items": [{"label": "合成方向", "core_instrument_ids": [], "judgment": "人工假设，不是自动信号", "evidence_refs": [w["evidence"]]}], "limitations": []}
    else:
        content = {"type": kind, "scenarios": [{"label": "合成条件", "instrument_ids": [], "trigger_text": "人工核验条件，不运行代码", "invalidation_text": "缺失数据时不下结论", "evidence_refs": [w["evidence"]]}], "limitations": []}
    return {"title": "合成研究 " + kind, "snapshot_id": w["snapshot"], "calendar_version_id": w["calendar"],
            "upstream_revision_ids": parents or [], "content": content}


def create(w, kind="review", parents=None, *, body=None):
    value = body or {"kind": kind, "source_trade_day": DAY, "target_trade_day": "2002-01-02" if kind == "plan" else None,
                     "revision": revision(w, kind, parents)}
    result = post(w["client"], "/api/v1/research-documents", value, w["headers"])
    assert result.status_code == 201, result.text
    assert result.headers["etag"] == '"1"'
    return result.json()["data"]


def publish(w, rev, number=1, *, key=None):
    return post(w["client"], "/api/v1/revisions/" + rev + "/publish", {}, w["headers"], key=key, etag='"' + str(number) + '"')


def published_chain(w):
    review = create(w); assert publish(w, review["head_revision_id"]).status_code == 200
    direction = create(w, "direction", [review["head_revision_id"]]); assert publish(w, direction["head_revision_id"]).status_code == 200
    plan = create(w, "plan", [direction["head_revision_id"]]); assert publish(w, plan["head_revision_id"]).status_code == 200
    return review, direction, plan


def test_exact_old_chain_and_scenarios_survive_new_head_and_publication(workspace):
    w = workspace; client = w["client"]
    review, direction, plan = published_chain(w)
    old = client.get("/api/v1/revisions/" + review["head_revision_id"]).json()["data"]
    plan_old = client.get("/api/v1/revisions/" + plan["head_revision_id"]).json()["data"]
    body = revision(w); body["content"]["summary"] = "新修订不会替换旧引用"
    added = post(client, "/api/v1/research-documents/" + review["id"] + "/revisions", body, w["headers"], etag='"1"')
    assert added.status_code == 201 and added.headers["etag"] == '"2"'
    assert publish(w, added.json()["data"]["id"], 2).status_code == 200
    reread = client.get("/api/v1/revisions/" + review["head_revision_id"])
    assert reread.headers["etag"] == '"2"'
    for key in ["content", "content_hash", "recorded_at", "snapshot_id", "publication_status", "published_at"]:
        assert reread.json()["data"][key] == old[key]
    assert client.get("/api/v1/revisions/" + direction["head_revision_id"]).json()["data"]["upstream_revision_ids"] == [old["id"]]
    plan_added = post(client, "/api/v1/research-documents/" + plan["id"] + "/revisions",
        revision(w, "plan", [direction["head_revision_id"]]), w["headers"], etag='"1"').json()["data"]
    assert plan_added["scenarios"][0]["id"] != plan_old["scenarios"][0]["id"]
    assert client.get("/api/v1/revisions/" + plan["head_revision_id"]).json()["data"]["scenarios"] == plan_old["scenarios"]


def test_two_windows_compare_and_swap_has_one_winner_without_partial_write(workspace):
    w = workspace; doc = create(w); body = revision(w); path = "/api/v1/research-documents/" + doc["id"] + "/revisions"
    def save(_):
        with TestClient(app, base_url="http://127.0.0.1:8080") as other:
            other.cookies.update(w["client"].cookies)
            return post(other, path, body, w["headers"], etag='"1"')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, range(2)))
    assert sorted(row.status_code for row in results) == [201, 412]
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(DocumentRevision)) == 2
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "research.revision.create")) == 2


def test_idempotency_replays_original_etag_even_after_head_moves_and_binds_precondition(workspace):
    w = workspace; doc = create(w); path = "/api/v1/research-documents/" + doc["id"] + "/revisions"; body = revision(w)
    saved = post(w["client"], path, body, w["headers"], key="synthetic-retry-operation", etag='"1"')
    assert saved.status_code == 201
    assert post(w["client"], path, body, w["headers"], etag='"2"').status_code == 201
    replay = post(w["client"], path, body, w["headers"], key="synthetic-retry-operation", etag='"1"')
    assert replay.json() == saved.json() and replay.headers["etag"] == '"2"'
    assert replay.headers["idempotency-replayed"] == "true"
    assert post(w["client"], path, body, w["headers"], key="synthetic-retry-operation", etag='"2"').status_code == 409


@pytest.mark.parametrize("tag,status", [(None, 428), ("*", 422), ('W/"1"', 422), ("1", 422), ('"01"', 422), ('"2"', 412)])
def test_strict_if_match_rejects_missing_weak_malformed_and_stale(workspace, tag, status):
    w = workspace; doc = create(w)
    result = post(w["client"], "/api/v1/research-documents/" + doc["id"] + "/revisions", revision(w), w["headers"], etag=tag)
    assert result.status_code == status
    assert w["client"].get("/api/v1/research-documents/" + doc["id"]).headers["etag"] == '"1"'


def test_draft_can_record_unknown_calendar_then_explicit_new_revision_binds_it(workspace):
    w = workspace; body = {"kind": "review", "source_trade_day": DAY, "revision": revision(w)}
    body["revision"]["calendar_version_id"] = None
    doc = create(w, body=body)
    assert publish(w, doc["head_revision_id"]).status_code == 422
    added = post(w["client"], "/api/v1/research-documents/" + doc["id"] + "/revisions", revision(w), w["headers"], etag='"1"').json()["data"]
    assert publish(w, added["id"], 2).status_code == 200
    assert w["client"].get("/api/v1/revisions/" + doc["head_revision_id"]).json()["data"]["calendar_version_id"] is None


def test_draft_wrong_type_unauthorized_snapshot_and_outside_manifest_do_not_commit(workspace):
    w = workspace
    for change, expected in [("kind", 422), ("snapshot", 409), ("evidence", 409)]:
        body = {"kind": "review", "source_trade_day": DAY, "revision": revision(w)}
        if change == "kind": body["kind"] = "direction"
        if change == "snapshot": body["revision"]["snapshot_id"] = str(uuid.uuid4())
        if change == "evidence": body["revision"]["content"]["evidence_refs"] = [str(uuid.uuid4())]
        assert post(w["client"], "/api/v1/research-documents", body, w["headers"]).status_code == expected
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ResearchDocument)) == 0
        assert db.scalar(select(func.count()).select_from(DocumentRevision)) == 0


def test_unpublished_wrong_kind_self_and_withdrawn_parent_references_fail(workspace):
    w = workspace; review = create(w)
    bad = {"kind": "direction", "source_trade_day": DAY, "revision": revision(w, "direction", [review["head_revision_id"]])}
    assert post(w["client"], "/api/v1/research-documents", bad, w["headers"]).status_code == 409
    assert publish(w, review["head_revision_id"]).status_code == 200
    direction = create(w, "direction", [review["head_revision_id"]])
    assert post(w["client"], "/api/v1/research-documents/" + direction["id"] + "/revisions",
        revision(w, "direction", [direction["head_revision_id"]]), w["headers"], etag='"1"').status_code == 409
    with Session(engine()) as db, db.begin():
        db.add(PublicationEvent(revision_id=uuid.UUID(review["head_revision_id"]), owner_id=uuid.UUID(w["owner"]),
            actor_id=uuid.UUID(w["owner"]), kind="withdrawn", recorded_at=datetime.now(UTC), reason="synthetic withdrawal fixture"))
    assert publish(w, direction["head_revision_id"]).status_code == 409
    assert post(w["client"], "/api/v1/research-documents", bad, w["headers"]).status_code == 409


def test_publication_is_current_head_once_and_retries_are_exact(workspace):
    w = workspace; doc = create(w)
    first = publish(w, doc["head_revision_id"], key="synthetic-publish-operation")
    assert first.status_code == 200 and first.json()["data"]["publication_status"] == "published"
    assert publish(w, doc["head_revision_id"], key="synthetic-publish-operation").json() == first.json()
    assert publish(w, doc["head_revision_id"]).status_code == 409
    added = post(w["client"], "/api/v1/research-documents/" + doc["id"] + "/revisions", revision(w), w["headers"], etag='"1"')
    assert added.status_code == 201
    assert publish(w, doc["head_revision_id"], 2).status_code == 409


def test_cross_owner_viewer_csrf_and_owner_spoof_are_rejected(workspace):
    w = workspace; doc = create(w)
    with Session(engine()) as db, db.begin():
        db.add(User(username="different-researcher", password_hash=hasher.hash(PASSWORD), role="viewer"))
    login = w["client"].post("/api/v1/session", json={"username": "different-researcher", "password": PASSWORD}, headers=ORIGIN).json()["data"]
    headers = {**ORIGIN, "X-CSRF-Token": login["csrf_token"]}
    assert w["client"].get("/api/v1/research-documents/" + doc["id"]).status_code == 404
    assert w["client"].get("/api/v1/revisions/" + doc["head_revision_id"]).status_code == 404
    assert w["client"].get("/api/v1/research-documents").json()["data"]["items"] == []
    body = {"kind": "review", "source_trade_day": DAY, "revision": revision(w)}
    assert post(w["client"], "/api/v1/research-documents", body, headers).status_code == 403
    body["owner_id"] = w["owner"]
    assert post(w["client"], "/api/v1/research-documents", body, headers).status_code == 422
    body.pop("owner_id")
    assert post(w["client"], "/api/v1/research-documents", body, ORIGIN).status_code == 403


@pytest.mark.parametrize("quality", ["empty", "unavailable"])
def test_missing_material_quality_does_not_become_definite_publication(workspace, quality):
    w = workspace
    receipt = post(w["client"], "/api/v1/snapshots", {"as_of_time": datetime.now(UTC).isoformat(),
        "required_capabilities": ["missing.synthetic.capability"] if quality == "unavailable" else [],
        "source_keys": ["no-such-synthetic-note"]}, w["headers"]).json()["data"]
    process(claim())
    body = {"kind": "review", "source_trade_day": DAY, "revision": revision(w)}
    body["revision"]["snapshot_id"] = receipt["result_ref"]["id"]; body["revision"]["content"]["evidence_refs"] = []
    doc = create(w, body=body)
    assert publish(w, doc["head_revision_id"]).status_code == (422 if quality == "empty" else 409)
    body["revision"]["content"]["limitations"] = ["合成材料缺失，只记录人工待判意见"]
    added = post(w["client"], "/api/v1/research-documents/" + doc["id"] + "/revisions", body["revision"], w["headers"], etag='"1"').json()["data"]
    assert publish(w, added["id"], 2).status_code == (200 if quality == "empty" else 409)


def test_calendar_closed_unknown_and_wrong_next_session_are_not_guessed(workspace):
    w = workspace; review, direction, _ = published_chain(w)
    for source in ["2002-01-01", "2002-01-03"]:
        assert post(w["client"], "/api/v1/research-documents", {"kind": "review", "source_trade_day": source, "revision": revision(w)}, w["headers"]).status_code == 422
    assert post(w["client"], "/api/v1/research-documents", {"kind": "plan", "source_trade_day": DAY,
        "target_trade_day": "2002-01-01", "revision": revision(w, "plan", [direction["head_revision_id"]])}, w["headers"]).status_code == 422


def test_database_rejects_mutation_and_orphan_head(workspace):
    w = workspace; review, direction, plan = published_chain(w)
    mutations = [update(DocumentRevision).values(title="overwrite"), update(PublicationEvent).values(kind="withdrawn"),
                 update(RevisionLink).values(relation="direction_plan"), update(PlanScenario).values(payload={}),
                 update(ResearchDocument).values(kind="plan")]
    for statement in mutations:
        with pytest.raises(DBAPIError):
            with engine().begin() as db: db.execute(statement)
    with pytest.raises(DBAPIError):
        with Session(engine()) as db, db.begin():
            db.add(ResearchDocument(owner_id=uuid.UUID(w["owner"]), kind="review", source_trade_day=date(2001, 12, 31),
                head_revision_number=1, created_at=datetime.now(UTC)))
    with pytest.raises(DBAPIError):
        with Session(engine()) as db, db.begin():
            db.add(DocumentRevision(document_id=uuid.UUID(review["id"]), owner_id=uuid.UUID(w["owner"]),
                revision_number=2, based_on_revision_id=uuid.UUID(review["head_revision_id"]), title="malformed synthetic row",
                snapshot_id=uuid.UUID(w["snapshot"]), calendar_version_id=uuid.UUID(w["calendar"]), content={},
                content_hash="a" * 64, author_id=uuid.UUID(w["owner"]), recorded_at=datetime.now(UTC)))
            db.flush()


def test_other_researcher_cannot_append_or_borrow_private_snapshot(workspace):
    w = workspace; doc = create(w)
    with Session(engine()) as db, db.begin():
        db.add(User(username="other-writer", password_hash=hasher.hash(PASSWORD), role="researcher"))
    login = w["client"].post("/api/v1/session", json={"username": "other-writer", "password": PASSWORD}, headers=ORIGIN).json()["data"]
    headers = {**ORIGIN, "X-CSRF-Token": login["csrf_token"]}
    assert post(w["client"], "/api/v1/research-documents/" + doc["id"] + "/revisions", revision(w), headers, etag='"1"').status_code == 404
    assert post(w["client"], "/api/v1/research-documents", {"kind": "review", "source_trade_day": DAY, "revision": revision(w)}, headers).status_code == 409
    assert post(w["client"], "/api/v1/revisions/" + doc["head_revision_id"] + "/publish", {}, headers, etag='"1"').status_code == 404


def test_future_source_day_can_be_draft_but_not_published(workspace):
    w = workspace; future = (datetime.now(UTC).date() + timedelta(days=10)).isoformat()
    calendar = post(w["client"], "/api/v1/calendar-versions", {"exchange": "SSE", "version": "synthetic-future-declaration",
        "source_ref": "synthetic future day; not a verified market schedule", "rights_basis": "self-created",
        "coverage_start": future, "coverage_end": future, "sessions": [{"trade_day": future, "is_open": True,
            "segments": [{"start": "09:30", "end": "11:30", "kind": "continuous"}]}]}, w["headers"]).json()["data"]
    body = {"kind": "review", "source_trade_day": future, "revision": revision(w)}
    body["revision"]["calendar_version_id"] = calendar["id"]
    doc = create(w, body=body)
    result = publish(w, doc["head_revision_id"])
    assert result.status_code == 422 and result.json()["error"]["code"] == "future_source_day"


def test_generated_research_contracts_include_etag_and_round_trip(workspace):
    w = workspace; doc = create(w)
    spec = json.loads((Path(__file__).resolve().parents[2] / "contracts/openapi.p2.json").read_text(encoding='utf-8'))
    for path, method, response in [("/api/v1/research-documents/{document_id}", "get", w["client"].get("/api/v1/research-documents/" + doc["id"])),
                                    ("/api/v1/revisions/{revision_id}", "get", w["client"].get("/api/v1/revisions/" + doc["head_revision_id"]))]:
        operation = spec["paths"][path][method]["responses"]["200"]
        assert "ETag" in operation["headers"] and response.headers["etag"] == '"1"'
        Draft202012Validator({**spec, **operation["content"]["application/json"]["schema"]}, format_checker=FormatChecker()).validate(response.json())


def test_revision_history_discovers_published_parent_behind_new_drafts_and_scopes_cursor(workspace):
    w = workspace; doc = create(w)
    assert publish(w, doc["head_revision_id"]).status_code == 200
    path = "/api/v1/research-documents/" + doc["id"] + "/revisions"
    for number in [1, 2]:
        assert post(w["client"], path, revision(w), w["headers"], etag='"' + str(number) + '"').status_code == 201
    first = w["client"].get(path + "?limit=1")
    assert first.headers["etag"] == '"3"'
    seen = first.json()["data"]["items"]
    cursor = first.json()["data"]["next_cursor"]
    assert seen[0]["revision_number"] == 3 and seen[0]["publication_status"] == "draft"
    while cursor:
        page = w["client"].get(path, params={"limit": 1, "cursor": cursor}).json()["data"]
        seen.extend(page["items"]); cursor = page["next_cursor"]
    assert [item["revision_number"] for item in seen] == [3, 2, 1]
    assert seen[-1]["id"] == doc["head_revision_id"] and seen[-1]["publication_status"] == "published"
    other = create(w)
    wrong = w["client"].get("/api/v1/research-documents/" + other["id"] + "/revisions", params={"cursor": first.json()["data"]["next_cursor"]})
    assert wrong.status_code == 422
    assert w["client"].get("/api/v1/evidence", params={"cursor": first.json()["data"]["next_cursor"]}).status_code == 422
