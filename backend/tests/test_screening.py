"""Research screens on disposable PostgreSQL and synthetic saved quotes only."""

import uuid
from datetime import timedelta

import pytest
from app.db import engine
from app.market.models import DataFetchRun
from app.market.service import source_id
from app.models import Job, Provider, User
from app.screening import jobs, service
from app.screening.models import ScreenEvent, ScreenRule, ScreenRuleVersion, ScreenRun
from app.screening.schemas import ContextInput
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from test_data_sources import ORIGIN, PASSWORD
from test_terminal_workbench import AT, DAY, REVIEW, save, setup
from test_tushare_market import quote

BASE = "/api/v1/screening"


def seed(client, monkeypatch, rows=None):
    headers = setup(client, monkeypatch, ["tushare_market_rt"])
    monkeypatch.setattr(service, "now", lambda db: REVIEW)
    if rows is not None:
        with Session(engine()) as db, db.begin():
            save(db, "tushare_market_rt", rows)
    return headers


def body(**updates):
    return {
        "name": "合成条件观察",
        "scope": "market",
        "enabled": False,
        "cooldown_seconds": 300,
        "definition": {
            "conditions": [{"field": "pct_change", "operator": "gte", "value": "3"}],
            "sort_field": "amount_cny",
            "sort_direction": "desc",
        },
        **updates,
    }


def post(client, path, headers, payload, key=None):
    return client.post(
        BASE + path, json=payload, headers={**headers, "Idempotency-Key": key or uuid.uuid4().hex}
    )


def rule(client, headers, **updates):
    response = post(client, "/rules", headers, body(**updates))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def run(client, headers, ident, **ctx):
    response = post(client, f"/rules/{ident}/runs", headers, {"day": DAY.isoformat(), **ctx})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def clock(monkeypatch, at):
    monkeypatch.setattr(service, "now", lambda db: at)
    monkeypatch.setattr(jobs, "now", lambda db: at)
    monkeypatch.setattr("app.market.terminal_read.now", lambda db: at)


def monitor(ident, at, monkeypatch):
    clock(monkeypatch, at)
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(ScreenRule.owner_id).where(ScreenRule.id == uuid.UUID(ident)))
        row = service.run_rule(db, owner, uuid.UUID(ident), ContextInput(), monitor=True)
        return row.id


def test_auth_roles_and_get_do_not_generate_work(client, monkeypatch):
    assert client.get(BASE + "/rules").status_code == 401
    headers = seed(client, monkeypatch)
    assert client.get(BASE + "/catalogue").status_code == 200
    assert len(client.get(BASE + "/catalogue").json()["data"]["metrics"]) == 9
    for path in ("/rules", "/runs", "/events"):
        assert client.get(BASE + path).json()["data"]["items"] == []
    assert (
        client.post(
            BASE + "/preview", json={"definition": body()["definition"]}, headers=ORIGIN
        ).status_code
        == 403
    )
    with Session(engine()) as db, db.begin():
        db.scalar(select(User)).role = "viewer"
    assert post(client, "/rules", headers, body()).status_code == 403
    assert (
        post(client, "/preview", headers, {"definition": body()["definition"]}).status_code == 403
    )
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 0
        assert db.scalar(select(func.count()).select_from(ScreenRun)) == 0


def test_preview_all_actual_rows_before_ui_cap_and_paginate(client, monkeypatch):
    rows = [quote(f"{100 + i:06}.SZ", price="11") for i in range(235)]
    headers = seed(client, monkeypatch, rows)
    payload = {
        "day": DAY.isoformat(),
        "definition": body()["definition"],
        "state": "matched",
        "offset": 200,
        "limit": 30,
    }
    response = post(client, "/preview", headers, payload)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["counts"] == {"total": 235, "matched": 235, "not_matched": 0, "insufficient": 0}
    assert len(data["items"]) == 30 and data["has_more"] and data["offset"] == 200
    assert data["id"] is None and data["source_refs"]
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ScreenRun)) == 0


def test_exact_threshold_real_zero_missing_and_frozen_explanations(client, monkeypatch):
    headers = seed(
        client,
        monkeypatch,
        [
            quote("000100.SZ", price="11"),
            quote("000101.SZ", price="10"),
            quote("000102.SZ", price="9"),
        ],
    )
    definition = {
        "conditions": [{"field": "pct_change", "operator": "eq", "value": "10"}],
        "sort_field": "pct_change",
        "sort_direction": "desc",
    }
    saved = rule(client, headers, definition=definition)
    result = run(client, headers, saved["id"])
    assert result["counts"] == {"total": 3, "matched": 1, "not_matched": 2, "insufficient": 0}
    assert result["items"][0]["checks"][0]["actual"] == "10.0"
    assert (
        result["rule_version"] == 1
        and result["input_hash"]
        and result["engine_version"] == "screen-v1"
    )
    missing = post(
        client,
        "/preview",
        headers,
        {
            "day": DAY.isoformat(),
            "definition": {
                "conditions": [{"field": "speed_5m", "operator": "gte", "value": "0"}],
                "sort_field": "speed_5m",
                "sort_direction": "desc",
            },
        },
    ).json()["data"]
    assert missing["counts"]["insufficient"] == 3 and all(
        row["metrics"]["speed_5m"] is None for row in missing["items"]
    )
    with Session(engine()) as db, db.begin():
        save(
            db,
            "tushare_market_rt",
            [quote("000100.SZ", price="9", at=AT + timedelta(minutes=1))],
            AT + timedelta(minutes=1),
        )
    frozen = client.get(BASE + "/runs/" + result["id"]).json()["data"]
    assert frozen["counts"] == result["counts"] and frozen["input_hash"] == result["input_hash"]
    assert frozen["items"] == result["items"]


def test_versions_conflict_idempotency_and_unchanged_run_dedup(client, monkeypatch):
    headers = seed(client, monkeypatch, [quote(price="11")])
    payload = body()
    first = post(client, "/rules", headers, payload, key="same-rule")
    replay = post(client, "/rules", headers, payload, key="same-rule")
    assert replay.status_code == 200 and replay.headers["Idempotency-Replayed"] == "true"
    saved = first.json()["data"]
    updated = post(
        client, "/rules", headers, body(id=saved["id"], expected_version=1, name="修订后的观察")
    ).json()["data"]
    assert updated["version"] == 2
    assert (
        post(client, "/rules", headers, body(id=saved["id"], expected_version=1)).status_code == 409
    )
    a = run(client, headers, saved["id"])
    b = run(client, headers, saved["id"])
    assert a["id"] == b["id"] and a["rule_version"] == 2
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ScreenRuleVersion)) == 2
        assert db.scalar(select(func.count()).select_from(ScreenRun)) == 1


@pytest.mark.parametrize(
    "edit",
    [
        {"conditions": [], "sort_field": "price", "sort_direction": "desc"},
        {
            "conditions": [{"field": "price", "operator": "gte", "value": "NaN"}],
            "sort_field": "price",
            "sort_direction": "desc",
        },
        {
            "conditions": [{"field": "__import__", "operator": "gte", "value": "1"}],
            "sort_field": "price",
            "sort_direction": "desc",
        },
        {
            "conditions": [{"field": "price", "operator": "python", "value": "1"}],
            "sort_field": "price",
            "sort_direction": "desc",
        },
    ],
)
def test_invalid_definitions_cannot_enter_storage(client, monkeypatch, edit):
    headers = seed(client, monkeypatch)
    assert post(client, "/rules", headers, body(definition=edit)).status_code == 422
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ScreenRule)) == 0


def test_strict_cutoff_and_no_future_dates(client, monkeypatch):
    headers = seed(client, monkeypatch, [quote(price="11")])
    saved = rule(client, headers)
    earlier = run(client, headers, saved["id"], as_of_time=(AT - timedelta(seconds=1)).isoformat())
    assert earlier["counts"]["total"] == 0 and earlier["mode"] == "observed"
    visible = run(client, headers, saved["id"], as_of_time=AT.isoformat())
    assert visible["counts"]["matched"] == 1
    later = run(client, headers, saved["id"], as_of_time=(AT + timedelta(seconds=1)).isoformat())
    assert later["id"] != visible["id"] and later["as_of_time"] != visible["as_of_time"]
    response = post(client, f"/rules/{saved['id']}/runs", headers, {"day": "2099-01-01"})
    assert response.status_code == 422
    response = post(
        client,
        "/preview",
        headers,
        {
            "definition": body()["definition"],
            "as_of_time": (REVIEW + timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 422


def test_watchlist_empty_stays_empty_instead_of_falling_back_to_market(client, monkeypatch):
    headers = seed(client, monkeypatch, [quote(price="11")])
    saved = rule(client, headers, scope="watchlist")
    data = run(client, headers, saved["id"])
    assert data["counts"]["total"] == 0 and data["items"] == []


def test_owner_and_revoked_source_block_historical_values(client, monkeypatch):
    headers = seed(client, monkeypatch, [quote(price="11")])
    saved = rule(client, headers)
    path = f"/rules/{saved['id']}/runs"
    result = post(client, path, headers, {"day": DAY.isoformat()}, key="protected-run").json()[
        "data"
    ]
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id("tushare_market_rt"))
        provider.rights = {**provider.rights, "display": "denied"}
    assert client.get(BASE + "/runs/" + result["id"]).status_code == 403
    assert (
        post(client, path, headers, {"day": DAY.isoformat()}, key="protected-run").status_code
        == 403
    )
    assert client.get(BASE + "/runs").json()["data"]["items"] == []
    from app.auth import hasher

    with Session(engine()) as db, db.begin():
        db.add(
            User(
                username="other",
                password_hash=hasher.hash(PASSWORD),
                role="researcher",
                enabled=True,
            )
        )
    client.post("/api/v1/session", json={"username": "other", "password": PASSWORD}, headers=ORIGIN)
    assert client.get(BASE + "/runs/" + result["id"]).status_code == 404
    assert client.get(BASE + "/runs?rule_id=" + saved["id"]).status_code == 404
    assert client.get(BASE + "/rules").json()["data"]["items"] == []


def test_monitor_baseline_enter_exit_restart_and_duplicates(client, monkeypatch):
    headers = seed(client, monkeypatch, [quote(price="10")])
    saved = rule(client, headers, enabled=True)
    first = monitor(saved["id"], AT, monkeypatch)
    assert client.get(BASE + "/events").json()["data"]["items"] == []
    changed = AT + timedelta(minutes=1)
    with Session(engine()) as db, db.begin():
        save(db, "tushare_market_rt", [quote(price="11", at=changed)], changed)
    entered = monitor(saved["id"], changed, monkeypatch)
    assert monitor(saved["id"], changed, monkeypatch) == entered
    events = client.get(BASE + "/events").json()["data"]["items"]
    assert (
        len(events) == 1
        and events[0]["direction"] == "entered"
        and events[0]["run_id"] == str(entered)
    )
    later = changed + timedelta(minutes=1)
    with Session(engine()) as db, db.begin():
        save(db, "tushare_market_rt", [quote(price="10", at=later)], later)
    monitor(saved["id"], later, monkeypatch)
    events = client.get(BASE + "/events").json()["data"]["items"]
    assert {item["direction"] for item in events} == {"entered", "exited"}
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ScreenRun)) == 3
        assert db.get(ScreenRun, first).event_count == 0


def test_stale_or_non_session_monitor_does_not_create_events(client, monkeypatch):
    headers = seed(client, monkeypatch, [quote(price="11")])
    saved = rule(client, headers, enabled=True)
    from app.auth import APIError

    with pytest.raises(APIError) as exc:
        monitor(saved["id"], AT + timedelta(minutes=4), monkeypatch)
    assert exc.value.code == "screen_market_not_fresh"
    with pytest.raises(APIError):
        monitor(saved["id"], REVIEW, monkeypatch)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ScreenRun)) == 0
        assert db.scalar(select(func.count()).select_from(ScreenEvent)) == 0


def test_repeated_and_regressing_source_times_do_not_fabricate_exit(client, monkeypatch):
    headers = seed(client, monkeypatch, [quote(price="10")])
    saved = rule(client, headers, enabled=True)
    monitor(saved["id"], AT, monkeypatch)
    newer = AT + timedelta(minutes=1)
    with Session(engine()) as db, db.begin():
        save(db, "tushare_market_rt", [quote(price="11", at=newer)], newer)
    entered = monitor(saved["id"], newer, monkeypatch)
    received = newer + timedelta(seconds=30)
    with Session(engine()) as db, db.begin():
        save(db, "tushare_market_rt", [quote(price="10", at=AT + timedelta(seconds=30))], received)
    assert monitor(saved["id"], received, monkeypatch) == entered
    assert [item["direction"] for item in client.get(BASE + "/events").json()["data"]["items"]] == [
        "entered"
    ]


def test_expired_lease_rolls_back_baseline_and_result(client, monkeypatch):
    headers = seed(client, monkeypatch, [quote(price="10")])
    clock(monkeypatch, AT)
    rule(client, headers, enabled=True)
    clock(monkeypatch, AT)
    jobs.schedule_screens()
    from app.auth import APIError
    from app.worker import claim, process

    job = claim()
    original = service.run_rule

    def expire(db, *args, **kwargs):
        result = original(db, *args, **kwargs)
        db.get(Job, job.id).lease_until = db.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
        return result

    monkeypatch.setattr(service, "run_rule", expire)
    with pytest.raises(APIError) as exc:
        process(job)
    assert exc.value.code == "screen_lease_lost"
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ScreenRun)) == 0
        assert db.scalar(select(ScreenRule.transition_state)) == {}


def test_monitor_new_version_and_new_day_build_fresh_baselines(client, monkeypatch):
    headers = seed(client, monkeypatch, [quote(price="10")])
    saved = rule(client, headers, enabled=True)
    monitor(saved["id"], AT, monkeypatch)
    updated = post(
        client,
        "/rules",
        headers,
        body(id=saved["id"], expected_version=1, enabled=True, name="新版规则"),
    ).json()["data"]
    changed = AT + timedelta(minutes=1)
    with Session(engine()) as db, db.begin():
        save(db, "tushare_market_rt", [quote(price="11", at=changed)], changed)
    monitor(saved["id"], changed, monkeypatch)
    assert updated["version"] == 2 and client.get(BASE + "/events").json()["data"]["items"] == []
    with Session(engine()) as db, db.begin():
        row = db.get(ScreenRule, uuid.UUID(saved["id"]))
        row.transition_state = {**row.transition_state, "trade_day": "2026-10-22"}
        row.content = {
            **row.content,
            "definition": {
                **row.content["definition"],
                "conditions": [{"field": "pct_change", "operator": "lte", "value": "0"}],
            },
        }
    # A changed source input starts a separate daily baseline; no synthetic exit.
    later = changed + timedelta(seconds=1)
    with Session(engine()) as db, db.begin():
        save(db, "tushare_market_rt", [quote(price="11", at=later)], later)
    monitor(saved["id"], later, monkeypatch)
    assert client.get(BASE + "/events").json()["data"]["items"] == []


def test_scheduler_and_worker_use_queue_and_cancel_outdated_definition(client, monkeypatch):
    headers = seed(client, monkeypatch, [quote(price="10")])
    clock(monkeypatch, AT)
    saved = rule(client, headers, enabled=True)
    clock(monkeypatch, AT)
    jobs.schedule_screens()
    jobs.schedule_screens()
    from app.worker import claim, process

    job = claim()
    assert job.kind == "screen.evaluate"
    process(job)
    with Session(engine()) as db:
        assert db.get(Job, job.id).state == "succeeded"
        assert db.scalar(select(func.count()).select_from(ScreenRun)) == 1
    later = AT + timedelta(minutes=1)
    clock(monkeypatch, later)
    jobs.schedule_screens()
    post(client, "/rules", headers, body(id=saved["id"], expected_version=1, enabled=False))
    job = claim()
    assert job.kind == "screen.evaluate"
    process(job)
    with Session(engine()) as db:
        assert db.get(Job, job.id).state == "cancelled"


def test_stored_result_integrity_mismatch_fails_closed(client, monkeypatch):
    headers = seed(client, monkeypatch, [quote(price="11")])
    saved = rule(client, headers)
    result = run(client, headers, saved["id"])
    with Session(engine()) as db, db.begin():
        row = db.get(ScreenRun, uuid.UUID(result["id"]))
        row.result = {**row.result, "result_hash": "0" * 64}
    assert client.get(BASE + "/runs/" + result["id"]).status_code == 409


def test_maintenance_fences_screen_schedule_and_http_writes(client, monkeypatch):
    headers = seed(client, monkeypatch, [quote(price="10")])
    clock(monkeypatch, AT)
    rule(client, headers, enabled=True)
    with Session(engine()) as db, db.begin():
        db.execute(
            text(
                "INSERT INTO system_settings_state(id,active,stage) VALUES(1,true,'draining') "
                "ON CONFLICT(id) DO UPDATE SET active=true"
            )
        )
    jobs.schedule_screens()
    with Session(engine()) as db:
        assert (
            db.scalar(select(func.count()).select_from(Job).where(Job.kind == "screen.evaluate"))
            == 0
        )
    assert post(client, "/rules", headers, body()).status_code == 503
    assert client.get(BASE + "/rules").status_code == 200


def test_database_role_cannot_rewrite_or_delete_frozen_screen_history(client, monkeypatch):
    seed(client, monkeypatch)
    with Session(engine()) as db:
        for table in ("screen_rule_versions", "screen_runs", "screen_events"):
            assert db.scalar(
                text("SELECT has_table_privilege(:role,:table,:permission)"),
                {"role": "jlpm_app", "table": table, "permission": "SELECT"},
            )
            for permission in ("UPDATE", "DELETE"):
                assert not db.scalar(
                    text("SELECT has_table_privilege(:role,:table,:permission)"),
                    {"role": "jlpm_app", "table": table, "permission": permission},
                )
