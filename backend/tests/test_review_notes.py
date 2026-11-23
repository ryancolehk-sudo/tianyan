"""Private journal schema checks and guarded disposable-PostgreSQL tests.

All users, note text and reference rows below are isolated synthetic fixtures.
No vendor requests or source creation/configuration occur in application code.
Pure schema tests can run through unittest without a database.
"""

import json
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import APIError
from app.db import engine
from app.evidence.common import content_hash, now
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import EvidenceInput, ManualPayload
from app.evidence.service import import_evidence
from app.market.review_notes import (
    MANUAL_SOURCE_ID,
    ReviewNotesContent,
    ReviewNotesInput,
    encode_content,
    notes_days,
    notes_history,
    notes_key,
    notes_view,
    save_notes,
)
from app.models import Provider, User

DAY = date(2001, 1, 3)


class ReviewNotesSchemaTests(unittest.TestCase):
    def test_defaults_and_typed_manual_content(self):
        content = ReviewNotesContent()
        self.assertEqual(content.stage, "未标记")
        self.assertEqual(json.loads(encode_content(content))["format"], "tianyan.review-notes.v1")
        parsed = ReviewNotesContent.model_validate({
            "directions": [{"slot": "主线候选", "sector_code": "BK1184.DC", "name": "合成方向"}],
            "core_stocks": [{"code": "600001.SH", "name": "合成股票", "tag": "持续跟踪"}],
            "observations": [{"text": "人工观察", "tag": "新增关注"}],
            "confirmed_sections": ["summary", "directions"],
        })
        self.assertIsNone(parsed.core_stocks[0].sector_code)
        self.assertEqual(parsed.directions[0].slot, "主线候选")

    def test_auto_market_fields_client_timestamps_and_non_stock_codes_are_rejected(self):
        invalid = [
            {"turnover": "100"}, {"stage": "自动买入"},
            {"core_stocks": [{"code": "830001.BJ", "name": "不在沪深范围"}]},
            {"core_stocks": [{"code": "000001.SH", "name": "指数不是股票"}]},
            {"catalyst_links": [{"evidence_id": str(uuid.uuid4()), "source_row": -1, "sector_code": "BK1184.DC"}]},
            {"lhb_notes": [{"record_key": "example", "stock_code": "600001.SH", "confirmed": "false"}]},
            {"confirmed_sections": ["summary", "summary"]}, {"summary": "invalid\0text"},
        ]
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(ValidationError):
                ReviewNotesContent.model_validate(content)
        with self.assertRaises(ValidationError):
            ReviewNotesInput.model_validate({"trade_day": str(DAY), "expected_head": None,
                                            "content": {}, "published_at": "2001-01-03T00:00:00Z"})
        with self.assertRaises(ValidationError):
            ReviewNotesInput.model_validate({"trade_day": str(DAY), "content": {}})

    def test_aggregate_wrapper_size_and_ambiguous_slots_are_rejected(self):
        with self.assertRaises(ValidationError):
            ReviewNotesContent.model_validate({"summary": "测" * 4000, "judgment": "测" * 4000,
                                               "observations": [{"text": "测" * 1000} for _ in range(20)]})
        direction = {"slot": "主线候选", "sector_code": "BK1184.DC", "name": "合成方向"}
        with self.assertRaises(ValidationError):
            ReviewNotesContent.model_validate({"directions": [direction, direction]})

    def test_batch_summary_bounds_and_empty_request_need_no_database(self):
        db, owner_id = object(), uuid.uuid4()
        self.assertEqual(notes_days(db, owner_id, []), {"state": "empty", "items": []})
        with self.assertRaises(APIError) as error:
            notes_days(db, owner_id, [DAY + timedelta(days=i) for i in range(64)])
        self.assertEqual(error.exception.code, "review_notes_days_invalid")


@pytest.fixture
def owners():
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        users = [User(username="notes-" + uuid.uuid4().hex, password_hash="synthetic-disabled-password",
                      role="researcher") for _ in range(2)]
        db.add_all(users)
        db.flush()
        assert db.get(Provider, MANUAL_SOURCE_ID) is not None
        return users


def body(text="人工复盘草稿", *, expected=None, target=DAY, **content):
    return ReviewNotesInput(trade_day=target, expected_head=expected,
                            content=ReviewNotesContent(summary=text, **content))


def save(owner, value):
    with Session(engine()) as db, db.begin():
        result, quality, reasons = save_notes(db, owner, value, uuid.uuid4())
        assert quality == "complete" and not reasons
        return result


def manual_reference(db, owner):
    value = EvidenceInput(source_id=MANUAL_SOURCE_ID, source_key="synthetic-reference:" + uuid.uuid4().hex,
                          source_revision=str(uuid.uuid4()), kind="manual", authorship="original",
                          payload=ManualPayload(text="独立人工证据"), rights_basis="Synthetic test original")
    return import_evidence(db, owner, value, None)[0].id


def shared_reference(db):
    source = db.scalar(select(Provider).where(Provider.kind != "manual").order_by(Provider.id).limit(1))
    assert source is not None
    # Test-only permission fixture; save_notes never changes a provider.
    source.configured = source.enabled = True
    source.entitlement_status = "verified"
    source.rights = {"display": "allowed", "cache": "allowed", "export": "unknown", "redistribute": "unknown"}
    payload = {"dataset": "review.news", "rows": [{"title": "SECRET_SHARED_ROW_NOT_IN_NOTES_413"}]}
    row = EvidenceVersion(source_id=source.id, owner_id=None, visibility="shared", kind="reference",
                          source_key="synthetic-news:" + uuid.uuid4().hex, source_revision=str(uuid.uuid4()),
                          version_number=1, payload=payload, rights_basis="Synthetic test fixture",
                          received_at=now(db), availability="unknown", provenance="source_reported",
                          historical_eligible=False, content_hash=content_hash(payload))
    db.add(row)
    db.flush()
    return row.id, source.id


def test_original_notes_have_server_times_append_versions_and_preserve_provider(owners):
    owner = owners[0]
    with Session(engine()) as db:
        source = db.get(Provider, MANUAL_SOURCE_ID)
        before_source = (source.id, source.configured, source.enabled, source.entitlement_status, dict(source.rights))
        provider_count = db.scalar(select(func.count()).select_from(Provider))
        before = now(db)
    first = save(owner, body())
    first_id = uuid.UUID(first["revision"]["id"])
    second = save(owner, body("确认后的人工判断", expected=first_id, confirmed_sections=["summary"]))
    third = save(owner, body("确认后的人工判断", expected=uuid.UUID(second["revision"]["id"]),
                            confirmed_sections=["summary"]))
    assert first["content"]["summary"] == "人工复盘草稿"
    assert [first["revision"]["version_number"], second["revision"]["version_number"], third["revision"]["version_number"]] == [1, 2, 3]
    assert first["revision"]["id"] != second["revision"]["id"] != third["revision"]["id"]
    assert second["revision"]["supersedes_id"] == str(first_id)
    with Session(engine()) as db:
        row = db.get(EvidenceVersion, first_id)
        assert row.owner_id == owner.id and row.visibility == "private" and row.provenance == "manual"
        assert row.source_key == notes_key(DAY)
        assert row.received_at == row.published_at == row.available_at and row.received_at >= before
        assert row.received_at.date() != DAY and row.event_at is None
        source = db.get(Provider, MANUAL_SOURCE_ID)
        assert (source.id, source.configured, source.enabled, source.entitlement_status, source.rights) == before_source
        assert db.scalar(select(func.count()).select_from(Provider)) == provider_count
        old = notes_view(db, owner.id, DAY, revision_id=first_id)
        assert old["content"] == first["content"] and not old["is_current"]
        history = notes_history(db, owner.id, DAY)
        assert [r["revision"]["version_number"] for r in history["items"]] == [3, 2, 1]
        assert all(item["head_id"] == third["revision"]["id"] for item in history["items"])
        assert history["items"][-1]["revision"]["id"] == str(first_id)


def test_cas_conflicts_and_concurrent_saves_do_not_overwrite(owners):
    owner = owners[0]
    first = save(owner, body())
    head = uuid.UUID(first["revision"]["id"])
    def attempt(text):
        try:
            return save(owner, body(text, expected=head))["revision"]["id"]
        except APIError as error:
            return error.status, error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["第一个并发草稿", "第二个并发草稿"]))
    assert sum(isinstance(result, str) for result in results) == 1
    assert (409, "review_notes_conflict") in results
    with pytest.raises(APIError) as error:
        save(owner, body("过期的整日内容", expected=head))
    assert error.value.status == 409
    with Session(engine()) as db:
        assert len(notes_history(db, owner.id, DAY)["items"]) == 2
        assert notes_view(db, owner.id, DAY, revision_id=head)["content"]["summary"] == "人工复盘草稿"


def test_owner_date_revision_and_as_of_isolation(owners):
    owner, other = owners
    first = save(owner, body())
    old_id = uuid.UUID(first["revision"]["id"])
    cutoff = datetime.fromisoformat(first["revision"]["received_at"])
    latest = save(owner, body("截止之后新增内容", expected=old_id))
    with Session(engine()) as db:
        historical = notes_view(db, owner.id, DAY, as_of_time=cutoff)
        assert historical["revision"]["id"] == str(old_id) and historical["head_id"] == str(old_id)
        assert historical["content"]["summary"] == "人工复盘草稿" and not historical["writable"]
        assert notes_view(db, other.id, DAY)["state"] == "empty"
        assert not notes_history(db, other.id, DAY)["items"]
        for identity, target, revision, as_of in [
            (other.id, DAY, old_id, None), (owner.id, DAY + timedelta(days=1), old_id, None),
            (owner.id, DAY, uuid.UUID(latest["revision"]["id"]), cutoff),
        ]:
            with pytest.raises(APIError) as error:
                notes_view(db, identity, target, revision_id=revision, as_of_time=as_of)
            assert error.value.status == 404
        with pytest.raises(APIError):
            notes_view(db, owner.id, DAY, as_of_time=now(db) + timedelta(days=1))
    other_first = save(other, body("另一账号的同日笔记"))
    assert other_first["revision"]["version_number"] == 1
    assert other_first["revision"]["id"] != str(old_id)


def test_disabled_original_provider_still_allows_private_history_but_rejects_save(owners):
    owner = owners[0]
    first = save(owner, body())
    with Session(engine()) as db, db.begin():
        db.get(Provider, MANUAL_SOURCE_ID).enabled = False
    with Session(engine()) as db:
        view = notes_view(db, owner.id, DAY)
        assert view["content"]["summary"] == "人工复盘草稿" and not view["writable"]
        assert len(notes_history(db, owner.id, DAY)["items"]) == 1
    with pytest.raises(APIError) as error:
        save(owner, body("不得写入", expected=uuid.UUID(first["revision"]["id"])))
    assert error.value.status == 422


def test_evidence_binding_rights_and_revoked_shared_data_never_leak(owners):
    owner = owners[0]
    with Session(engine()) as db, db.begin():
        own_ref = manual_reference(db, owner)
        shared_ref, source_id = shared_reference(db)
    content = {"lhb_notes": [{"record_key": "synthetic-lhb-event", "stock_code": "600001.SH", "text": "本人研究摘记",
                              "confirmed": True, "evidence_ids": [own_ref, shared_ref]}],
               "catalyst_links": [{"evidence_id": shared_ref, "source_row": 0, "sector_code": "BK1184.DC", "comment": "仅为同步观察"}]}
    saved = save(owner, body(**content))
    assert {row["state"] for row in saved["evidence_status"]} == {"available"}
    assert "SECRET_SHARED_ROW_NOT_IN_NOTES_413" not in json.dumps(saved)
    with Session(engine()) as db, db.begin():
        db.get(Provider, source_id).enabled = False
    with Session(engine()) as db:
        view = notes_view(db, owner.id, DAY)
        states = {row["evidence_id"]: row["state"] for row in view["evidence_status"]}
        assert states[str(shared_ref)] == "unavailable" and states[str(own_ref)] == "available"
        assert view["content"]["lhb_notes"][0]["text"] == "本人研究摘记"
        assert "SECRET_SHARED_ROW_NOT_IN_NOTES_413" not in json.dumps(notes_history(db, owner.id, DAY))
    with pytest.raises(APIError) as error:
        save(owner, body(expected=uuid.UUID(saved["revision"]["id"]), **content))
    assert error.value.code == "review_notes_evidence_unavailable"


def test_other_private_unknown_evidence_and_wrong_row_bindings_are_rejected(owners):
    owner, other = owners
    with Session(engine()) as db, db.begin():
        other_ref = manual_reference(db, other)
        shared_ref, _ = shared_reference(db)
    for evidence_id in [other_ref, uuid.uuid4()]:
        with pytest.raises(APIError) as error:
            save(owner, body(lhb_notes=[{"record_key": "synthetic", "stock_code": "600001.SH", "evidence_ids": [evidence_id]}]))
        assert error.value.code == "review_notes_evidence_unavailable"
    with pytest.raises(APIError) as error:
        save(owner, body(catalyst_links=[{"evidence_id": shared_ref, "source_row": 1, "sector_code": "BK1184.DC"}]))
    assert error.value.code == "review_notes_source_row_invalid"
    with Session(engine()) as db:
        assert notes_view(db, owner.id, DAY)["state"] == "empty"


def test_history_is_capped_at_fifty_revisions(owners):
    owner = owners[0]
    with Session(engine()) as db, db.begin():
        head = None
        for number in range(52):
            result, _, _ = save_notes(db, owner, body(str(number), expected=head))
            head = uuid.UUID(result["revision"]["id"])
        history = notes_history(db, owner.id, DAY)
        assert len(history["items"]) == 50
        assert history["items"][0]["revision"]["version_number"] == 52
        assert history["items"][-1]["revision"]["version_number"] == 3


def test_batch_stages_select_latest_per_day_and_owner_without_prose(owners):
    owner, other = owners
    first = save(owner, body("PROSE_MUST_NOT_APPEAR_IN_STAGE_SUMMARY_781", stage="退潮"))
    latest = save(owner, body("PROSE_MUST_NOT_APPEAR_IN_STAGE_SUMMARY_781", expected=uuid.UUID(first["revision"]["id"]),
                              stage="局部修复", theme_stages=[{"sector_code": "BK1184.DC", "stage": "启动"}]))
    other_day = DAY + timedelta(days=1)
    save(owner, body(target=other_day, stage="轮动"))
    save(other, body("另一个人的笔记", stage="冰点"))
    with Session(engine()) as db:
        result = notes_days(db, owner.id, [DAY + timedelta(days=i) for i in range(63)])
        items = {item["trade_day"]: item for item in result["items"]}
        assert set(items) == {str(DAY), str(other_day)}
        assert items[str(DAY)]["revision"]["id"] == latest["revision"]["id"]
        assert items[str(DAY)]["stage"] == "局部修复"
        assert items[str(DAY)]["theme_stages"] == [{"sector_code": "BK1184.DC", "stage": "启动"}]
        assert items[str(other_day)]["stage"] == "轮动"
        assert "PROSE_MUST_NOT_APPEAR_IN_STAGE_SUMMARY_781" not in json.dumps(result)
        assert all(set(item) == {"trade_day", "stage", "theme_stages", "revision"} for item in result["items"])


def test_batch_stages_apply_receipt_cutoff_before_choosing_latest_revision(owners):
    owner, other = owners
    first = save(owner, body(stage="退潮"))
    cutoff = datetime.fromisoformat(first["revision"]["received_at"])
    save(owner, body(expected=uuid.UUID(first["revision"]["id"]), stage="启动"))
    with Session(engine()) as db:
        result = notes_days(db, owner.id, [DAY], as_of_time=cutoff)
        assert result["items"][0]["stage"] == "退潮"
        assert result["items"][0]["revision"]["id"] == first["revision"]["id"]
        assert notes_days(db, owner.id, [DAY], as_of_time=cutoff - timedelta(microseconds=1))["items"] == []
        assert notes_days(db, other.id, [DAY], as_of_time=cutoff)["items"] == []


if __name__ == "__main__":
    unittest.main()
