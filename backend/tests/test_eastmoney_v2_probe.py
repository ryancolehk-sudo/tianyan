"""Offline tests for the bounded evidence probe. SYNTHETIC VALUES ONLY."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from app.market.providers.eastmoney_v2 import probe
from app.market.providers.eastmoney_v2.client import JsonResponse


@pytest.fixture(autouse=True)
def isolated_database():
    yield


class FakeInner:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def request_json(self, path, params):
        self.calls += 1
        return self.response


def test_probe_refuses_to_write_inside_repository():
    with pytest.raises(SystemExit):
        probe.private_dir(str(probe.REPO_ROOT / "tmp-evidence"))


def test_recording_client_keeps_exact_bytes_and_hash(tmp_path):
    raw = b'{"rc":0,"data":{"total":0}}'
    response = JsonResponse(
        {"rc": 0, "data": {"total": 0}},
        datetime(2026, 10, 23, 1, 20, 10, tzinfo=UTC),
        1,
        "/api/qt/clist/get",
        raw_body=raw,
    )
    client = probe.RecordingClient(FakeInner(response), tmp_path / "raw")
    client.request_json("/api/qt/clist/get", {"pn": 1})
    saved = list((tmp_path / "raw").iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == raw
    assert client.calls[0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert client.calls[0]["endpoint"] == "/api/qt/clist/get"


def test_auction_summary_redacts_prices_and_marks_field_presence():
    envelope = {
        "status": "partial",
        "reason": "auction_semantics_unverified",
        "quality_flags": ["auction_semantics_unverified"],
        "records": [
            {
                "ts_code": "600000.SH",
                "source_time": "2026-10-23T09:20:05+08:00",
                "received_at": "2026-10-23T09:20:10+08:00",
                "stage": None,
                "stage_time_matched": False,
                "stage_verified": False,
                "observed_phase": "09:20",
                "checkpoint_offset_seconds": 5,
                "candidate_seal_amount_cny": "5290000",
                "candidate_seal_side": "buy",
                "seal_amount_cny": None,
                "match_state": "auction_book_unverified",
                "raw_fields": {"f43": "10.58", "f19": "10.58", "f20": "5000", "f124": "1"},
            }
        ],
    }
    summary = probe.auction_summary(envelope, "2026-10-23", "09:20", ["600000.SH"])
    text = json.dumps(summary, ensure_ascii=False)
    assert "10.58" not in text and "5290000" not in text
    record = summary["records"][0]
    assert record["candidate_seal_present"] is True
    assert record["field_presence"]["f19"] is True
    assert record["field_presence"]["f39"] is False
    assert record["stage_verified"] is False


def test_full_flow_summary_checks_spacing_scope_and_tail_alignment():
    payload = {
        "rc": 0,
        "data": {
            "klines": [
                "2026-10-23 09:30,-100,0,0,-60,-40",
                "2026-10-23 09:31,-120,0,0,-70,-50",
                "2026-10-23 09:35,-50,0,0,-30,-20",
            ]
        },
    }
    snapshot = {
        "source_time": "2026-10-23T09:35:00+08:00",
        "main_net_inflow_cny": "-50",
    }
    summary = probe.analyze_full_flow(payload, datetime(2026, 10, 23).date(), snapshot)
    assert summary["line_count"] == 3
    assert summary["gaps_60s"] == 1 and summary["gaps_non_60s"] == 1
    assert summary["timestamps_strictly_increasing"] is True
    assert summary["scope_consistency_failures"] == 0
    assert summary["tail_same_minute_as_snapshot"] is True
    assert summary["tail_within_one_yuan_of_snapshot"] is True
    assert summary["five_minute_pair_present"] is True
    assert "main_net_inflow" not in json.dumps(summary)


def test_full_flow_summary_does_not_attach_a_1539_snapshot_to_1500_tail():
    payload = {"rc": 0, "data": {"klines": ["2026-10-23 15:00,-50,0,0,-30,-20"]}}
    snapshot = {"source_time": "2026-10-23T15:39:30+08:00", "main_net_inflow_cny": "-50"}
    summary = probe.analyze_full_flow(payload, datetime(2026, 10, 23).date(), snapshot)
    assert summary["tail_same_minute_as_snapshot"] is False
    assert summary["tail_within_one_yuan_of_snapshot"] is True
