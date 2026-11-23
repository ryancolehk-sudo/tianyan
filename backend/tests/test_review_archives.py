import copy
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.auth import hasher
from app.cli import bootstrap, import_review_archive
from app.db import engine
from app.main import app
from app.models import AuditEvent, User
from app.review_archives.models import ReviewArchive
from archive_fixtures import archive_body, artifact

PASSWORD = "synthetic-private-archive-password"
ORIGIN = {"Origin": "http://127.0.0.1:8080"}


@pytest.fixture
def archive_workspace(client):
    bootstrap("archive-owner", PASSWORD)
    login = client.post("/api/v1/session", headers=ORIGIN, json={"username": "archive-owner", "password": PASSWORD}).json()["data"]
    return client, {**ORIGIN, "X-CSRF-Token": login["csrf_token"]}


def post(client, headers, body, key=None):
    return client.post("/api/v1/review-archives", headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())}, json=body)


def test_exact_report_and_same_version_summary_preserve_provenance_and_received_time(archive_workspace):
    client, headers = archive_workspace
    body = archive_body()
    response = post(client, headers, body)
    assert response.status_code == 201
    row = response.json()["data"]
    assert row["full_text"] == body["full"]["text"]
    assert row["summary_text"] == "**市场判断**\n合成总结保留原句，不自动改写。\n\n**风险与边界**\n仅用于隔离验证，不是当前可执行预案。"
    assert row["based_on_trade_day"] == "2001-12-31" and row["target_trade_day"] == "2002-01-02"
    assert row["source_version"] == 1 and row["has_summary"] is True
    assert row["imported_at"] > row["source_generated_at"]
    assert row["full_source"]["sha256"] == body["full"]["sha256"]
    assert body["source_commit"] in row["full_source"]["url"]
    assert client.get("/api/v1/review-archives/" + row["id"]).json()["data"] == row
    assert response.json()["meta"]["quality"]["status"] == "partial"
    spec = json.loads((Path(__file__).resolve().parents[2] / 'contracts/openapi.p2.json').read_text(encoding='utf-8'))
    for path, status, actual in [('/api/v1/review-archives', '201', response),
            ('/api/v1/review-archives/{archive_id}', '200', client.get('/api/v1/review-archives/' + row['id']))]:
        method = 'post' if status == '201' else 'get'
        schema = spec['paths'][path][method]['responses'][status]['content']['application/json']['schema']
        Draft202012Validator({**spec, **schema}, format_checker=FormatChecker()).validate(actual.json())
    with engine().connect() as db:
        assert db.execute(text("SELECT count(*) FROM research_documents")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM evidence_versions")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM publication_events")).scalar_one() == 0


def test_repeat_and_concurrent_import_are_idempotent_but_changed_same_source_is_rejected(archive_workspace):
    client, headers = archive_workspace
    body = archive_body()
    cookies = client.cookies
    def save(_):
        with TestClient(app, base_url="http://127.0.0.1:8080") as other:
            other.cookies.update(cookies)
            return post(other, headers, body).json()["data"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(save, range(2))
    assert first == second
    original = post(client, headers, body, "archive-repeat-operation")
    replay = post(client, headers, body, "archive-repeat-operation")
    assert replay.headers["Idempotency-Replayed"] == "true" and original.json() == replay.json()
    changed = copy.deepcopy(body)
    changed["full"] = artifact(body["full"]["path"], body["full"]["text"] + "新增内容")
    assert post(client, headers, changed).json()["error"]["code"] == "archive_source_conflict"
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ReviewArchive)) == 1
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "review_archive.import")) == 1


@pytest.mark.parametrize("failure", ["hash", "blob", "header", "future", "summary_version", "summary_unknown_tag", "invalid_date", "null"])
def test_bad_source_bytes_headers_times_or_mixed_summary_never_create_a_partial_import(archive_workspace, failure):
    client, headers = archive_workspace
    body = archive_body()
    if failure == "hash":
        body["full"]["sha256"] = "0" * 64
    elif failure == "blob":
        body["full"]["blob"] = "0" * 40
    elif failure == "summary_version":
        body["summary"] = archive_body(2)["summary"]
    elif failure == "summary_unknown_tag":
        body["summary"] = artifact("docs/bad.json", json.dumps({"schema": "2.0", "body": {"elements": [{"tag": "img", "src": "http://127.0.0.1/private"}]}}))
    else:
        value = body["full"]["text"]
        value = {"header": "unrecognized report", "future": value.replace("2001-12-31 21:00:00", "2099-12-31 21:00:00"),
            "invalid_date": value.replace("2002-01-02", "2002-02-30"), "null": value + "\0"}.get(failure, value)
        body["full"] = artifact(body["full"]["path"], value)
        body["summary"] = None
    assert post(client, headers, body).status_code == 422
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ReviewArchive)) == 0


def test_missing_same_version_summary_is_explicit_and_does_not_borrow_from_another_archive(archive_workspace):
    client, headers = archive_workspace
    first = post(client, headers, archive_body(1)).json()["data"]
    second = post(client, headers, archive_body(2, summary=False)).json()["data"]
    assert first["summary_text"] and second["summary_text"] is None
    assert second["has_summary"] is False and second["summary_source"] is None
    listed = client.get("/api/v1/review-archives").json()["data"]["items"]
    assert [row["source_version"] for row in listed] == [2, 1]


def test_private_ownership_roles_csrf_and_anonymous_reads(archive_workspace):
    client, headers = archive_workspace
    row = post(client, headers, archive_body()).json()["data"]
    assert post(client, ORIGIN, archive_body(2)).status_code == 403
    assert post(client, {**headers, "Origin": "https://wrong.example"}, archive_body(2)).status_code == 403
    with Session(engine()) as db, db.begin():
        db.add_all([User(username="archive-other", password_hash=hasher.hash(PASSWORD), role="researcher"),
                    User(username="archive-viewer", password_hash=hasher.hash(PASSWORD), role="viewer")])
    for username in ["archive-other", "archive-viewer"]:
        with TestClient(app, base_url="http://127.0.0.1:8080") as other:
            auth = other.post("/api/v1/session", headers=ORIGIN, json={"username": username, "password": PASSWORD}).json()["data"]
            assert other.get("/api/v1/review-archives/" + row["id"]).status_code == 404
            assert other.get("/api/v1/review-archives").json()["data"]["items"] == []
            if username.endswith("viewer"):
                assert post(other, {**ORIGIN, "X-CSRF-Token": auth["csrf_token"]}, archive_body()).status_code == 403
    client.cookies.clear()
    assert client.get("/api/v1/review-archives").status_code == 401
    assert client.get("/api/v1/review-archives/" + row["id"]).status_code == 401


def test_filter_version_paging_and_cursor_scope_do_not_skip_or_mix_days(archive_workspace):
    client, headers = archive_workspace
    for version, target in [(3, "2002-01-02"), (1, "2002-01-03"), (2, "2002-01-02"), (1, "2002-01-02")]:
        assert post(client, headers, archive_body(version, target=target)).status_code == 201
    first = client.get("/api/v1/review-archives?limit=2").json()["data"]
    second = client.get("/api/v1/review-archives", params={"limit": 2, "cursor": first["next_cursor"]}).json()["data"]
    assert [(r["target_trade_day"], r["source_version"]) for r in first["items"] + second["items"]] == [
        ("2002-01-03", 1), ("2002-01-02", 3), ("2002-01-02", 2), ("2002-01-02", 1)]
    assert second["next_cursor"] is None
    assert client.get("/api/v1/review-archives", params={"target_trade_day": "2002-01-02", "cursor": first["next_cursor"]}).status_code == 422
    assert client.get("/api/v1/review-archives?target_trade_day=2003-01-01").json()["meta"]["quality"]["status"] == "empty"


def test_archive_content_and_provenance_are_immutable_in_the_database(archive_workspace):
    client, headers = archive_workspace
    row = post(client, headers, archive_body()).json()["data"]
    for statement in ["UPDATE review_archives SET source_version=2 WHERE id=:id", "DELETE FROM review_archives WHERE id=:id"]:
        with pytest.raises(DBAPIError), engine().begin() as db:
            db.execute(text(statement), {"id": row["id"]})
    assert client.get("/api/v1/review-archives/" + row["id"]).json()["data"] == row


def test_operator_import_uses_the_same_checks_and_never_needs_a_user_password(archive_workspace):
    client, _ = archive_workspace
    result = import_review_archive("archive-owner", archive_body())
    assert result["has_summary"] is True
    assert client.get("/api/v1/review-archives/" + result["id"]).status_code == 200
    with pytest.raises(ValueError):
        import_review_archive("missing-admin", archive_body(2))
