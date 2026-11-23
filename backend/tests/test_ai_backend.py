"""Synthetic local integrations: mock every model transport; never contact a paid API."""
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import protocols, worker
from app.ai.models import AiAttempt, AiRun, AiSettings, Connection
from app.auth import hasher
from app.cli import bootstrap
from app.config import settings
from app.db import engine
from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion, IdempotencyKey
from app.models import Job, Provider, User

SECRET = "synthetic-never-valid-model-credential"
ORIGIN = {"Origin": "http://127.0.0.1:8080"}


@pytest.fixture(autouse=True)
def private_vault(tmp_path, monkeypatch):
    root, master = tmp_path / "credentials", tmp_path / "independent-master.key"
    root.mkdir()
    master.write_bytes(Fernet.generate_key())
    monkeypatch.setattr(settings(), "ai_secret_directory", str(root))
    monkeypatch.setattr(settings(), "ai_master_key_file", str(master))
    # Unmocked network is always a test failure.
    original = protocols.socket.getaddrinfo
    def guarded(host, *args, **kwargs):
        if host == "db":
            return original(host, *args, **kwargs)
        pytest.fail("No model network allowed in synthetic tests")
    monkeypatch.setattr(protocols.socket, "getaddrinfo", guarded)
    return root, master


def login(client, username="owner"):
    bootstrap(username, "synthetic-ai-password-long-enough")
    response = client.post("/api/v1/session", json={"username": username, "password": "synthetic-ai-password-long-enough"}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    return {**ORIGIN, "X-CSRF-Token": data["csrf_token"]}, uuid.UUID(data["user"]["id"])


def mutate(client, method, path, body, headers, key=None):
    return client.request(method, path, json=body, headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())})


def body(**changes):
    return {"name": "离线测试模型", "kind": "model", "template": "custom_model", "endpoint": "https://models.example.com/v1",
            "protocol": "openai_chat", "model": "synthetic-model", "api_key": SECRET, "enabled": False,
            "source_ids": [], "options": {"timeout_seconds": 30, "max_output_tokens": 1200}, **changes}


def patch_input(connection, **changes):
    keys = ("name", "kind", "template", "endpoint", "protocol", "model", "enabled", "source_ids", "options")
    return {**{key: connection[key] for key in keys}, "expected_revision": connection["revision"], **changes}


def mock_model(monkeypatch, calls, *, failure=None):
    def fake(endpoint, protocol, key, model, prompt=None, *, discovery=False, on_send, **kwargs):
        assert key == SECRET
        on_send()
        calls.append({"model": model, "discovery": discovery})
        if failure:
            raise failure
        if discovery:
            return protocols.Reply("", models=[{"id": "synthetic-model"}])
        return protocols.Reply("连接测试通过。", 8, 6)
    monkeypatch.setattr(protocols, "exchange", fake)


def ready_model(client, headers, monkeypatch, *, name="离线测试模型", model="synthetic-model"):
    calls = []
    mock_model(monkeypatch, calls)
    created = mutate(client, "POST", "/api/v1/integrations", body(name=name, model=model), headers)
    assert created.status_code == 201, created.text
    connection = created.json()["data"]
    tested = mutate(client, "POST", "/api/v1/integrations/probe", {**patch_input(connection), "id": connection["id"]}, headers)
    assert tested.status_code == 200 and tested.json()["data"]["status"] == "success", tested.text
    enabled = mutate(client, "PATCH", "/api/v1/integrations/" + connection["id"], patch_input(connection, enabled=True), headers)
    assert enabled.status_code == 200, enabled.text
    return enabled.json()["data"]


def enable_feature(client, headers, connection, feature="news_summary"):
    settings_response = mutate(client, "PATCH", "/api/v1/ai/settings", {"enabled": True, "default_connection_id": connection["id"]}, headers)
    assert settings_response.status_code == 200, settings_response.text
    binding = mutate(client, "PATCH", "/api/v1/ai/bindings/" + feature, {"enabled": True, "connection_id": None, "fallback_connection_id": None}, headers)
    assert binding.status_code == 200, binding.text


def news(owner, *, title_only=False):
    clock = datetime.now(UTC) - timedelta(seconds=5)
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        provider = db.scalar(select(Provider).where(Provider.kind == "manual"))
        payload = {"title": "隔离测试新闻标题", "source_url": "https://news.example.com/synthetic"}
        if not title_only:
            payload["summary"] = "这是一条专供隔离环境使用的合成资讯。测试主体公布测试事件和日期，所有字段均无真实金融含义。"
        row = EvidenceVersion(id=uuid.uuid4(), source_id=provider.id, owner_id=owner, visibility="private", source_key=str(uuid.uuid4()),
                              source_revision="1", version_number=1, kind="news", event_at=clock, published_at=clock,
                              received_at=clock, available_at=clock, availability="known", provenance="unverified",
                              historical_eligible=False, rights_basis="自制测试资料", payload=payload, content_hash=content_hash(payload))
        db.add(row)
    return row.id


def queue_news(client, headers, identity, **options):
    response = mutate(client, "POST", "/api/v1/ai/runs", {"feature": "news_summary", "news_id": str(identity), **options}, headers)
    assert response.status_code == 202, response.text
    return response.json()["data"]


def result_reply(reference):
    return protocols.Reply(json.dumps({"summary": "本机合成资料的测试摘要。", "items": [
        {"title": "测试事件", "text": "合成主体公布合成事件。", "evidence_refs": [str(reference)]}]}), 100, 30)


def test_read_only_settings_default_off_and_auth(client):
    assert client.get("/api/v1/ai/settings").status_code == 401
    headers, owner = login(client)
    result = client.get("/api/v1/ai/settings").json()["data"]
    assert result["enabled"] is False and result["default_connection_id"] is None
    assert {item['id'] for item in result['features'] if item['implemented']} == {'theme_summary', 'news_summary', 'plan_generation'}
    assert all(not item["enabled"] for item in result["features"])
    index = client.get("/api/v1/integrations")
    assert index.status_code == 200, index.text
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AiSettings)) == 0
        assert db.scalar(select(func.count()).select_from(AiAttempt)) == 0


def test_vault_key_never_enters_database_or_reply(client, private_vault):
    headers, _ = login(client)
    response = mutate(client, "POST", "/api/v1/integrations", body(), headers)
    assert response.status_code == 201, response.text
    assert SECRET not in response.text and "api_key" not in response.json()["data"]
    root, master = private_vault
    assert len(list(root.iterdir())) == 1 and SECRET.encode() not in next(root.iterdir()).read_bytes()
    with Session(engine()) as db:
        connection = db.scalar(select(Connection))
        assert connection.secret_ref.startswith("vault:")
        assert SECRET not in str([(row.payload_hash, row.response_json) for row in db.scalars(select(IdempotencyKey))])


def test_probe_discovery_is_not_generation_and_keyed_replay(client, monkeypatch):
    headers, _ = login(client)
    calls = []
    mock_model(monkeypatch, calls)
    created = mutate(client, "POST", "/api/v1/integrations", body(), headers).json()["data"]
    attempted = mutate(client, "PATCH", "/api/v1/integrations/" + created["id"], patch_input(created, enabled=True), headers)
    assert attempted.status_code == 409
    discovery = mutate(client, "POST", "/api/v1/integrations/discover-models", {**patch_input(created), "id": created["id"]}, headers)
    assert discovery.status_code == 200 and discovery.json()["data"]["models"][0]["id"] == "synthetic-model"
    still = mutate(client, "PATCH", "/api/v1/integrations/" + created["id"], patch_input(created, enabled=True), headers)
    assert still.status_code == 409
    probe_body, key = {**patch_input(created), "id": created["id"]}, str(uuid.uuid4())
    first = mutate(client, "POST", "/api/v1/integrations/probe", probe_body, headers, key)
    repeated = mutate(client, "POST", "/api/v1/integrations/probe", probe_body, headers, key)
    assert first.json()["data"]["status"] == "success" and repeated.headers.get("Idempotency-Replayed") == "true"
    assert len(calls) == 2
    with Session(engine()) as db:
        assert sum(db.scalars(select(AiAttempt.request_count))) == 2


def test_draft_probe_proof_and_revision_invalidation(client, monkeypatch):
    headers, _ = login(client)
    calls = []
    mock_model(monkeypatch, calls)
    proof = mutate(client, "POST", "/api/v1/integrations/probe", body(), headers).json()["data"]["test_proof"]
    invalid = mutate(client, "POST", "/api/v1/integrations", body(model="changed", enabled=True, test_proof=proof), headers)
    assert invalid.status_code == 409
    created = mutate(client, "POST", "/api/v1/integrations", body(enabled=True, test_proof=proof), headers)
    assert created.status_code == 201, created.text
    value = created.json()["data"]
    edited = mutate(client, "PATCH", "/api/v1/integrations/" + value["id"], patch_input(value, endpoint="https://different.example.com/v1", enabled=False), headers)
    assert edited.status_code == 200, edited.text
    assert edited.json()["data"]["last_test"] is None and edited.json()["data"]["revision"] > value["revision"]


def test_unimplemented_features_and_roles_are_enforced(client):
    headers, owner = login(client)
    response = mutate(client, "PATCH", "/api/v1/ai/bindings/emotion_analysis", {"enabled": True, "connection_id": None, "fallback_connection_id": None}, headers)
    assert response.status_code == 409
    with Session(engine()) as db, db.begin():
        db.get(User, owner).role = "viewer"
    forbidden = mutate(client, "POST", "/api/v1/integrations", body(), headers)
    assert forbidden.status_code == 403
    assert client.get("/api/v1/integrations").status_code == 200


def test_news_generation_is_manual_idempotent_and_versioned(client, monkeypatch):
    headers, owner = login(client)
    model = ready_model(client, headers, monkeypatch)
    enable_feature(client, headers, model)
    identity = news(owner)
    first = queue_news(client, headers, identity)
    duplicate = queue_news(client, headers, identity)
    assert duplicate["id"] == first["id"]
    calls = []
    def fake(*args, on_send, **kwargs):
        on_send()
        calls.append(1)
        return result_reply(identity)
    monkeypatch.setattr(protocols, "exchange", fake)
    assert client.get("/api/v1/ai/runs").status_code == 200 and calls == []
    job = worker.claim()
    assert job is not None
    worker.process(job)
    result = client.get("/api/v1/ai/runs/" + first["id"])
    assert result.status_code == 200, result.text
    value = result.json()["data"]
    assert value["state"] == "succeeded" and value["result"]["items"][0]["evidence_refs"] == [str(identity)]
    assert value["evidence_refs"][0]["url"] == "https://news.example.com/synthetic" and calls == [1]
    assert queue_news(client, headers, identity)["id"] == first["id"]
    assert queue_news(client, headers, identity, regenerate=True)["id"] != first["id"]


def test_title_only_and_other_users_news_never_sent(client, monkeypatch):
    headers, owner = login(client)
    model = ready_model(client, headers, monkeypatch)
    enable_feature(client, headers, model)
    identity = news(owner, title_only=True)
    response = mutate(client, "POST", "/api/v1/ai/runs", {"feature": "news_summary", "news_id": str(identity)}, headers)
    assert response.status_code == 422 and response.json()["error"]["code"] == "insufficient_news_content"
    with Session(engine()) as db, db.begin():
        other = User(id=uuid.uuid4(), username="other", role="researcher", enabled=True, password_hash=hasher.hash("synthetic-other-password"))
        db.add(other)
        other_id = other.id
    other_news = news(other_id)
    response = mutate(client, "POST", "/api/v1/ai/runs", {"feature": "news_summary", "news_id": str(other_news)}, headers)
    assert response.status_code == 404


def test_configuration_change_cancels_old_queue_before_send(client, monkeypatch):
    headers, owner = login(client)
    model = ready_model(client, headers, monkeypatch)
    enable_feature(client, headers, model)
    identity = news(owner)
    queued = queue_news(client, headers, identity)
    disabled = mutate(client, "PATCH", "/api/v1/integrations/" + model["id"], patch_input(model, enabled=False), headers)
    assert disabled.status_code == 200, disabled.text
    newer = disabled.json()["data"]
    enabled = mutate(client, "PATCH", "/api/v1/integrations/" + model["id"], patch_input(newer, enabled=True), headers)
    assert enabled.status_code == 200, enabled.text
    monkeypatch.setattr(protocols, "exchange", lambda *a, **k: pytest.fail("Old config must never send"))
    worker.process(worker.claim())
    result = client.get("/api/v1/ai/runs/" + queued["id"]).json()["data"]
    assert result["state"] == "cancelled" and result["error_code"] == "configuration_changed"


def test_sent_unknown_and_expired_lease_never_replay(client, monkeypatch):
    headers, owner = login(client)
    model = ready_model(client, headers, monkeypatch)
    enable_feature(client, headers, model)
    queued = queue_news(client, headers, news(owner))
    calls = []
    def fake(*args, on_send, **kwargs):
        on_send()
        calls.append(1)
        raise protocols.ModelFailure("outcome_unknown", "测试断线", sent=True)
    monkeypatch.setattr(protocols, "exchange", fake)
    worker.process(worker.claim())
    value = client.get("/api/v1/ai/runs/" + queued["id"]).json()["data"]
    assert value["state"] == "unknown" and calls == [1] and worker.claim() is None
    crashed = queue_news(client, headers, news(owner))
    job = worker.claim()
    with Session(engine()) as db, db.begin():
        stored = db.get(Job, job.id)
        stored.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        db.add(AiAttempt(owner_id=owner, run_id=uuid.UUID(crashed["id"]), connection_id=uuid.UUID(model["id"]),
                         purpose="generate", service_name="合成测试", model="synthetic-model", state="running", request_count=1,
                         sent_at=datetime.now(UTC), safe_details={}))
    assert worker.claim() is None
    assert client.get("/api/v1/ai/runs/" + crashed["id"]).json()["data"]["state"] == "unknown"


def test_invalid_citation_is_failed_without_retry(client, monkeypatch):
    headers, owner = login(client)
    model = ready_model(client, headers, monkeypatch)
    enable_feature(client, headers, model)
    queued = queue_news(client, headers, news(owner))
    calls = []
    def fake(*args, on_send, **kwargs):
        on_send()
        calls.append(1)
        return result_reply(uuid.uuid4())
    monkeypatch.setattr(protocols, "exchange", fake)
    worker.process(worker.claim())
    value = client.get("/api/v1/ai/runs/" + queued["id"]).json()["data"]
    assert value["state"] == "failed" and value["result"] is None and value["error_code"] == "invalid_generated_result" and calls == [1]


@pytest.mark.parametrize("protocol,upstream,expected_field", [
    ("openai_chat", {"choices": [{"message": {"content": "测试回复"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}, "messages"),
    ("openai_responses", {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "测试回复"}]}], "usage": {"input_tokens": 4, "output_tokens": 2}}, "input"),
    ("anthropic_messages", {"content": [{"type": "text", "text": "测试回复"}], "stop_reason": "end_turn", "usage": {"input_tokens": 4, "output_tokens": 2}}, "messages"),
])
def test_protocols_pin_validated_host_and_use_distinct_wire(monkeypatch, protocol, upstream, expected_field):
    observed = {}
    class Response:
        status = 200
        def getheader(self, *args):
            return "identity"
        def read(self, size):
            return json.dumps(upstream).encode()
    class Client:
        def __init__(self, hostname, address, port, timeout):
            observed.update(host=hostname, address=address)
        def connect(self):
            pass
        def request(self, method, path, body, headers):
            observed.update(body=json.loads(body), headers=headers, path=path)
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(protocols, "resolved_addresses", lambda *args: ["93.184.216.34"])
    monkeypatch.setattr(protocols, "PinnedHTTPSConnection", Client)
    result = protocols.exchange("https://models.example.com/v1", protocol, SECRET, "gpt-6-synthetic", "合成输入",
                                system="可信的独立系统约束", on_send=lambda: observed.update(sent=True))
    assert result.text == "测试回复" and result.input_tokens == 4 and result.output_tokens == 2
    assert observed["address"] == "93.184.216.34" and observed["host"] == "models.example.com" and expected_field in observed["body"]
    assert observed["sent"] and observed["body"]["stream"] is False
    if protocol == "openai_responses":
        assert observed["body"]["store"] is False and "max_output_tokens" in observed["body"] and observed["body"]["instructions"] == "可信的独立系统约束"
    if protocol == "anthropic_messages":
        assert observed["headers"]["x-api-key"] == SECRET and "Authorization" not in observed["headers"] and observed["body"]["system"] == "可信的独立系统约束"
    if protocol == "openai_chat":
        assert observed["body"]["messages"][0]["role"] == "system" and "max_completion_tokens" in observed["body"]


@pytest.mark.parametrize("address", ["http://models.example.com/v1", "https://127.0.0.1/v1", "https://[::1]/v1", "https://169.254.169.254/latest", "https://224.0.0.1/v1", "https://user:pass@models.example.com/v1", "https://models.example.com/v1?key=private", "https://localhost/v1"])
def test_private_endpoints_rejected(address):
    with pytest.raises(protocols.ModelFailure) as error:
        protocols.validate_endpoint(address)
    assert error.value.code == "endpoint_rejected"


def test_mixed_public_private_dns_rejected_and_key_redacted(monkeypatch):
    monkeypatch.setattr(protocols.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(protocols.ModelFailure):
        protocols.resolved_addresses("models.example.com", 443)
    reply = protocols.parse_reply({"choices": [{"message": {"content": "echo " + SECRET}}]}, "openai_chat", secret=SECRET)
    assert SECRET not in reply.text
    names = protocols.parse_reply({"data": [{"id": SECRET, "display_name": SECRET}]}, "openai_chat", discovery=True, secret=SECRET)
    assert SECRET not in str(names.models)


def test_theme_summary_uses_bounded_real_saved_manifest(client, monkeypatch):
    from test_data_sources import configure
    from test_terminal_workbench import AT, REVIEW, save
    from test_tushare import source
    from test_tushare_market import quote
    headers, owner = login(client)
    configure(client, headers, source("tushare_market_rt"), key="synthetic-theme-market-source")
    with Session(engine()) as db, db.begin():
        save(db, "tushare_market_rt", [quote(f"{100 + i:06}.SZ", price="11", at=AT) for i in range(65)], AT)
    monkeypatch.setattr("app.ai.runs.now", lambda db: REVIEW)
    monkeypatch.setattr("app.market.terminal_read.now", lambda db: REVIEW)
    model = ready_model(client, headers, monkeypatch)
    enable_feature(client, headers, model, "theme_summary")
    response = mutate(client, "POST", "/api/v1/ai/runs", {"feature": "theme_summary", "trade_day": "2026-10-23", "as_of_time": REVIEW.isoformat()}, headers)
    assert response.status_code == 202, response.text
    queued = response.json()["data"]
    with Session(engine()) as db:
        stored = db.get(AiRun, uuid.UUID(queued["id"]))
        assert len(stored.input_data["stocks"]) == 50 and stored.input_data["coverage"]["quote_count"] == 65
        assert stored.evidence_refs and all(db.get(EvidenceVersion, uuid.UUID(item["id"])) for item in stored.evidence_refs)
        reference = stored.evidence_refs[0]["id"]
    def fake(*args, on_send, **kwargs):
        on_send()
        return result_reply(reference)
    monkeypatch.setattr(protocols, "exchange", fake)
    worker.process(worker.claim())
    result = client.get("/api/v1/ai/runs/" + queued["id"]).json()["data"]
    assert result["state"] == "succeeded" and result["trade_day"] == "2026-10-23"
    assert any("50" in item and "全市场" in item for item in result["limitations"])
    matching = client.get("/api/v1/ai/runs", params={"feature": "theme_summary", "as_of_time": REVIEW.isoformat()}).json()["data"]["items"]
    absent = client.get("/api/v1/ai/runs", params={"feature": "theme_summary", "as_of_time": (REVIEW - timedelta(seconds=1)).isoformat()}).json()["data"]["items"]
    assert len(matching) == 1 and absent == []


def test_only_explicit_rate_limit_retries_and_fallback_shares_three_attempt_budget(client, monkeypatch):
    headers, owner = login(client)
    primary = ready_model(client, headers, monkeypatch, name="主模型", model="primary")
    backup = ready_model(client, headers, monkeypatch, name="备用模型", model="backup")
    enable_feature(client, headers, primary)
    response = mutate(client, "PATCH", "/api/v1/ai/bindings/news_summary", {
        "enabled": True, "connection_id": None, "fallback_connection_id": backup["id"]}, headers)
    assert response.status_code == 200, response.text
    identity = news(owner)
    queued = queue_news(client, headers, identity)
    calls = []
    def fake(endpoint, protocol, key, model, *args, on_send, **kwargs):
        on_send()
        calls.append(model)
        if model == "primary":
            raise protocols.ModelFailure("rate_limited", "合成 429", sent=True, retryable=True, status=429)
        return result_reply(identity)
    monkeypatch.setattr(protocols, "exchange", fake)
    worker.process(worker.claim())
    value = client.get("/api/v1/ai/runs/" + queued["id"]).json()["data"]
    assert value["state"] == "succeeded" and value["connection_id"] == backup["id"] and value["model"] == "backup"
    assert calls == ["primary", "primary", "backup"]
    with Session(engine()) as db:
        attempts = db.scalars(select(AiAttempt).where(AiAttempt.run_id == uuid.UUID(queued["id"])).order_by(AiAttempt.started_at)).all()
        assert len(attempts) == 3 and sum(item.request_count for item in attempts) == 3
        assert [item.state for item in attempts] == ["failed", "failed", "succeeded"]


@pytest.mark.parametrize("status,code,retryable", [(401, "invalid_api_key", False), (403, "permission_denied", False),
    (404, "model_or_endpoint_not_found", False), (429, "rate_limited", True), (503, "upstream_unavailable", True),
    (302, "redirect_rejected", False)])
def test_http_failures_never_redirect_or_expose_vendor_body(monkeypatch, status, code, retryable):
    observed = []
    class Response:
        def __init__(self):
            self.status = status
        def read(self, *args):
            pytest.fail("Error body must not be consumed or exposed")
    class Client:
        def __init__(self, *args):
            pass
        def connect(self):
            pass
        def request(self, *args, **kwargs):
            observed.append(1)
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(protocols, "resolved_addresses", lambda *args: ["93.184.216.34"])
    monkeypatch.setattr(protocols, "PinnedHTTPSConnection", Client)
    with pytest.raises(protocols.ModelFailure) as error:
        protocols.exchange("https://models.example.com/v1", "openai_chat", SECRET, "synthetic", "test")
    assert error.value.code == code and error.value.retryable is retryable and error.value.sent
    assert SECRET not in str(error.value) and observed == [1]


@pytest.mark.parametrize("upstream,protocol", [({}, "openai_chat"),
    ({"choices": [{"message": {"content": ""}}]}, "openai_chat"),
    ({"choices": [{"message": {"content": "截断"}, "finish_reason": "length"}]}, "openai_chat"),
    ({"status": "incomplete", "output": []}, "openai_responses"),
    ({"content": [{"type": "tool_use", "name": "anything"}], "stop_reason": "tool_use"}, "anthropic_messages")])
def test_empty_truncated_and_tool_outputs_cannot_pass_probe(upstream, protocol):
    with pytest.raises((ValueError, KeyError)):
        protocols.parse_reply(upstream, protocol)


@pytest.mark.parametrize("header,seconds", [("120", 120), ("Sun, 25 Oct 2026 02:05:00 GMT", 300),
    ("Sun, 25 Oct 2026 01:00:00 GMT", 0), ("99999999999999999999999999999", 365 * 86400)])
def test_retry_after_seconds_dates_and_large_values(header, seconds):
    clock = datetime(2026, 10, 25, 2, tzinfo=UTC)
    assert protocols.retry_after(header, clock) == clock + timedelta(seconds=seconds)
    assert protocols.retry_after("invalid", clock) is None


def test_long_retry_after_stops_task_and_persists_supplier_deadline(client, monkeypatch):
    headers, owner = login(client)
    model = ready_model(client, headers, monkeypatch)
    enable_feature(client, headers, model)
    identity = news(owner)
    queued = queue_news(client, headers, identity)
    future, calls = datetime.now(UTC) + timedelta(minutes=15), []
    def fake(*args, on_send, **kwargs):
        assert kwargs["time_budget"] <= 180 if "time_budget" in kwargs else kwargs["timeout"] <= 180
        on_send()
        calls.append(1)
        raise protocols.ModelFailure("rate_limited", "合成冷却", sent=True, retryable=True, status=429, retry_after=future)
    monkeypatch.setattr(protocols, "exchange", fake)
    worker.process(worker.claim())
    value = client.get("/api/v1/ai/runs/" + queued["id"]).json()["data"]
    assert value["state"] == "failed" and value["error_code"] == "retry_after_exceeds_budget" and calls == [1]
    with Session(engine()) as db:
        attempt = db.scalar(select(AiAttempt).where(AiAttempt.run_id == uuid.UUID(queued["id"])))
        assert abs((attempt.retry_at - future).total_seconds()) < .1


def test_model_response_total_deadline_closes_socket_without_replay(monkeypatch):
    closed, sent = threading.Event(), []
    class Response:
        status = 200
        def getheader(self, *args):
            return "identity"
        def read(self, size):
            closed.wait(.5)
            raise OSError("synthetic socket closed")
    class Client:
        def __init__(self, *args):
            pass
        def connect(self):
            pass
        def request(self, *args, **kwargs):
            sent.append(1)
        def getresponse(self):
            return Response()
        def close(self):
            closed.set()
    monkeypatch.setattr(protocols, "resolved_addresses", lambda *args: ["93.184.216.34"])
    monkeypatch.setattr(protocols, "PinnedHTTPSConnection", Client)
    with pytest.raises(protocols.ModelFailure) as failure:
        protocols.exchange("https://models.example.com/v1", "openai_chat", SECRET, "synthetic", "test", timeout=.02)
    assert failure.value.code == "outcome_unknown" and closed.is_set() and sent == [1]


def test_referenced_model_removal_requires_explicit_unbinding(client, monkeypatch):
    headers, _ = login(client)
    primary = ready_model(client, headers, monkeypatch, name="主模型", model="primary")
    backup = ready_model(client, headers, monkeypatch, name="备用模型", model="backup")
    enable_feature(client, headers, primary)
    bound = mutate(client, "PATCH", "/api/v1/ai/bindings/news_summary", {
        "enabled": True, "connection_id": primary["id"], "fallback_connection_id": backup["id"]}, headers)
    assert bound.status_code == 200, bound.text
    for value in (primary, backup):
        blocked = client.delete("/api/v1/integrations/" + value["id"], headers=headers)
        assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "connection_in_use"
    cleared = mutate(client, "PATCH", "/api/v1/ai/bindings/news_summary", {
        "enabled": False, "connection_id": None, "fallback_connection_id": None}, headers)
    assert cleared.status_code == 200, cleared.text
    assert client.delete("/api/v1/integrations/" + backup["id"], headers=headers).status_code == 204
    still_default = client.delete("/api/v1/integrations/" + primary["id"], headers=headers)
    assert still_default.status_code == 409
    turned_off = mutate(client, "PATCH", "/api/v1/ai/settings", {"enabled": False, "default_connection_id": None}, headers)
    assert turned_off.status_code == 200, turned_off.text
    assert client.delete("/api/v1/integrations/" + primary["id"], headers=headers).status_code == 204


def test_enabled_global_cannot_clear_default_without_turning_off(client, monkeypatch):
    headers, _ = login(client)
    connection = ready_model(client, headers, monkeypatch)
    enable_feature(client, headers, connection)
    response = mutate(client, "PATCH", "/api/v1/ai/settings", {"default_connection_id": None}, headers)
    assert response.status_code == 409
    current = client.get("/api/v1/ai/settings").json()["data"]
    assert current["enabled"] is True and current["default_connection_id"] == connection["id"]
    response = mutate(client, "PATCH", "/api/v1/ai/settings", {"enabled": False, "default_connection_id": None}, headers)
    assert response.status_code == 200 and response.json()["data"]["enabled"] is False


def test_later_disabled_fallback_preserves_primary_and_invalidates_old_queue(client, monkeypatch):
    headers, owner = login(client)
    primary = ready_model(client, headers, monkeypatch, name="主模型", model="primary")
    backup = ready_model(client, headers, monkeypatch, name="备用模型", model="backup")
    enable_feature(client, headers, primary)
    response = mutate(client, "PATCH", "/api/v1/ai/bindings/news_summary", {
        "enabled": True, "connection_id": None, "fallback_connection_id": backup["id"]}, headers)
    assert response.status_code == 200, response.text
    identity = news(owner)
    old = queue_news(client, headers, identity)
    disabled = mutate(client, "PATCH", "/api/v1/integrations/" + backup["id"], patch_input(backup, enabled=False), headers)
    assert disabled.status_code == 200, disabled.text
    fresh = queue_news(client, headers, identity)
    assert fresh["id"] != old["id"]
    sent = []
    def fake(endpoint, protocol, key, model, *args, on_send, **kwargs):
        on_send()
        sent.append(model)
        return result_reply(identity)
    monkeypatch.setattr(protocols, "exchange", fake)
    worker.process(worker.claim())
    worker.process(worker.claim())
    assert client.get("/api/v1/ai/runs/" + old["id"]).json()["data"]["state"] == "cancelled"
    assert client.get("/api/v1/ai/runs/" + fresh["id"]).json()["data"]["state"] == "succeeded"
    assert sent == ["primary"]
    config = client.get("/api/v1/ai/settings").json()["data"]
    assert next(item for item in config["features"] if item["id"] == "news_summary")["fallback_connection_id"] == backup["id"]
    for enabled in (False, True):
        response = mutate(client, "PATCH", "/api/v1/ai/bindings/news_summary", {
            "enabled": enabled, "connection_id": None, "fallback_connection_id": backup["id"]}, headers)
        assert response.status_code == 200, response.text


def test_missing_vault_object_is_visible_and_blocks_new_model_requests(client, monkeypatch, private_vault):
    headers, owner = login(client)
    model = ready_model(client, headers, monkeypatch)
    enable_feature(client, headers, model)
    root, _ = private_vault
    with Session(engine()) as db:
        stored = db.get(Connection, uuid.UUID(model["id"]))
        reference = stored.secret_ref.removeprefix("vault:")
    (root / (reference + ".key")).unlink()
    value = next(item for item in client.get("/api/v1/integrations").json()["data"]["items"] if item["id"] == model["id"])
    assert value["credential_configured"] is False and value["credential_error_code"] == "credential_unreadable"
    response = mutate(client, "POST", "/api/v1/ai/runs", {"feature": "news_summary", "news_id": str(news(owner))}, headers)
    assert response.status_code == 409 and response.json()["error"]["code"] == "credential_unreadable"
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AiRun)) == 0
