"""Review regressions: synthetic responses only, no database or live provider."""
from __future__ import annotations

import json
from argparse import Namespace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.market.providers.eastmoney_v2 import probe
from app.market.providers.eastmoney_v2.client import EastmoneyClient, JsonResponse, TransportFailure


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("review tests must not send live requests")

    monkeypatch.setattr(EastmoneyClient, "_pace", forbidden)
    yield


def args(tmp_path, **overrides):
    values = dict(
        mode="full-flow", date="2026-10-23", codes="600000.SH", sector=None,
        sector_type=None, stage=None, include_change=False, limit=3,
        out=str(tmp_path / "evidence"), min_interval=5.0, max_attempts=1,
    )
    return Namespace(**(values | overrides))


def flow(labels):
    return {"rc": 0, "data": {"klines": [f"2026-10-23 {label},100,0,0,60,40" for label in labels]}}


def test_tail_sample_is_not_a_verified_full_day():
    result = probe.analyze_full_flow(flow(["14:59", "15:00"]), date(2026, 10, 23), None)
    assert result["status"] == "partial"
    assert result["reason"] == "partial_day_coverage"
    assert result["sample_valid"] is True
    assert result["market_time_semantics_verified"] is False


def test_complete_returned_labels_preserve_the_expected_lunch_break():
    morning = datetime(2026, 10, 23, 9, 31)
    afternoon = datetime(2026, 10, 23, 13, 1)
    labels = [(start + timedelta(minutes=i)).strftime("%H:%M") for start in (morning, afternoon) for i in range(120)]
    result = probe.analyze_full_flow(flow(labels), date(2026, 10, 23), None)
    assert result["status"] == "ok"
    assert result["line_count"] == 240
    assert result["lunch_gap_present"] is True
    assert result["unexpected_gap_count"] == 0
    assert result["observed_label_coverage"] == "full_session_bounds"
    assert result["market_time_semantics_verified"] is False


@pytest.mark.parametrize(("labels", "reason"), [
    (["10:01", "10:00"], "timestamps_not_increasing"),
    (["10:00", "10:00"], "timestamps_not_increasing"),
    (["12:00", "12:01"], "outside_trading_session"),
    (["10:00", "10:06"], "intraday_sampling_gap"),
    (["09:30", "10:30"], "intraday_sampling_gap"),
])
def test_bad_series_cannot_report_ok(labels, reason):
    result = probe.analyze_full_flow(flow(labels), date(2026, 10, 23), None)
    assert result["status"] == "partial" and result["reason"] == reason
    assert result["lunch_gap_present"] is False


def test_amount_inconsistency_is_not_a_pass():
    result = probe.analyze_full_flow(
        {"data": {"klines": ["2026-10-23 10:00,100,0,0,10,20"]}}, date(2026, 10, 23), None,
    )
    assert result["status"] == "partial" and result["reason"] == "scope_consistency_mismatch"
    assert result["sample_valid"] is False


def test_malformed_amount_is_a_controlled_error():
    result = probe.analyze_full_flow(
        {"data": {"klines": ["2026-10-23 10:00,not-money,0,0,60,40"]}}, date(2026, 10, 23), None,
    )
    assert result == {"status": "error", "reason": "schema_changed"}


def test_naive_snapshot_cannot_be_tied_to_an_aware_minute():
    result = probe.analyze_full_flow(flow(["10:00"]), date(2026, 10, 23), {
        "source_time": "2026-10-23T10:00:00", "main_net_inflow_cny": "100",
    })
    assert result["tail_same_minute_as_snapshot"] is False
    assert result["tail_snapshot_comparable"] is False


@pytest.mark.parametrize("git_marker", ["file", "directory"])
def test_private_output_cannot_enter_a_different_git_checkout(tmp_path, git_marker):
    checkout = tmp_path / "other-checkout"
    checkout.mkdir()
    if git_marker == "file":
        (checkout / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    else:
        (checkout / ".git").mkdir()
    with pytest.raises(SystemExit):
        probe.private_dir(str(checkout / "evidence"))
    assert not (checkout / "evidence").exists()


def test_default_output_is_also_checked_against_repositories(monkeypatch):
    monkeypatch.setattr(probe, "DEFAULT_OUT", probe.REPO_ROOT / "unsafe-default")
    with pytest.raises(SystemExit):
        probe.private_dir(None)


class FailingInner:
    def __init__(self, failure):
        self.failure = failure
        self.count = 0

    def request_json(self, path, params):
        self.count += 1
        raise self.failure


def test_recording_keeps_failure_metadata_and_stops_followup_requests(tmp_path):
    inner = FailingInner(TransportFailure("error", "upstream_connection_reset", requests=1))
    client = probe.RecordingClient(inner, tmp_path / "raw")
    for _ in range(2):
        with pytest.raises(TransportFailure):
            client.request_json("/api/qt/stock/get", {"secid": "1.600000"})
    assert inner.count == 1 and len(client.calls) == 1
    assert client.calls[0]["reason"] == "upstream_connection_reset"
    assert client.calls[0]["body_basis"] == "not_received"
    assert client.calls[0]["raw_file"] is None


def test_recording_preserves_malformed_response_bytes_privately(tmp_path):
    raw = b"bad JSON, synthetic sample"
    failure = TransportFailure(
        "error", "schema_changed", raw_body=raw, received_at=datetime(2026, 10, 23, tzinfo=UTC),
    )
    client = probe.RecordingClient(FailingInner(failure), tmp_path / "raw")
    with pytest.raises(TransportFailure):
        client.request_json("/api/qt/stock/get", {})
    call = client.calls[0]
    assert (tmp_path / "raw" / call["raw_file"]).read_bytes() == raw
    assert call["body_basis"] == "decoded_http_entity" and call["reason"] == "schema_changed"
    assert raw.decode() not in json.dumps(call)


def test_reencoded_payload_is_not_labeled_raw_response(tmp_path):
    class InjectedInner:
        def request_json(self, path, params):
            return JsonResponse({"rc": 0, "data": None}, datetime.now(UTC), 1, path)

    client = probe.RecordingClient(InjectedInner(), tmp_path / "raw")
    client.request_json("/api/qt/stock/get", {})
    assert client.calls[0]["body_basis"] == "reencoded_payload_not_raw_response"


def test_full_flow_does_not_request_another_endpoint_after_snapshot_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.adapter, "fetch_stock_fund_snapshots", lambda **kw: {
        "status": "error", "reason": "upstream_connection_reset", "records": [],
    })
    result = probe.run(args(tmp_path))
    assert result["status"] == "error" and result["reason"] == "upstream_connection_reset"
    assert result["flow"]["reason"] == "snapshot_unavailable_no_followup_request"
    saved = json.loads(Path(result["summary_path"]).read_text(encoding="utf-8"))
    assert saved["calls"] == [] and saved["status"] == "error"


def test_full_flow_second_endpoint_failure_still_writes_a_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.adapter, "fetch_stock_fund_snapshots", lambda **kw: {
        "status": "ok", "reason": None,
        "records": [{"source_time": "2026-10-23T10:00:00+08:00", "main_net_inflow_cny": "100"}],
    })
    inner = FailingInner(TransportFailure("rate_limited", "upstream_rate_limited", requests=1))
    monkeypatch.setattr(probe, "EastmoneyClient", lambda **kw: inner)
    result = probe.run(args(tmp_path))
    saved = json.loads(Path(result["summary_path"]).read_text(encoding="utf-8"))
    assert result["status"] == saved["status"] == "rate_limited"
    assert saved["calls"][0]["reason"] == "upstream_rate_limited"
    assert inner.count == 1


@pytest.mark.parametrize("overrides", [
    {"limit": 20}, {"max_attempts": 2}, {"min_interval": 0},
    {"min_interval": float("nan")}, {"codes": "600000.SH,000001.SZ,000002.SZ,600001.SH"},
    {"sector": "BK0475"},
])
def test_probe_bounds_are_checked_before_creating_output(tmp_path, overrides):
    with pytest.raises(ValueError):
        probe.run(args(tmp_path, **overrides))
    assert not (tmp_path / "evidence").exists()


def test_cli_reports_a_failed_probe_as_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(probe, "run", lambda args: {"status": "error", "reason": "upstream_connection_reset"})
    assert probe.main(["--mode", "funds"]) == 1
    assert "upstream_connection_reset" in capsys.readouterr().out


def test_sector_ranking_probe_uses_a_ranking_query_not_an_explicit_sector(tmp_path, monkeypatch):
    captured = {}

    def sector(**kwargs):
        captured.update(kwargs)
        return {"status": "unavailable", "reason": "no_records", "records": []}

    monkeypatch.setattr(probe.adapter, "fetch_sector_fund_snapshots", sector)
    result = probe.run(args(tmp_path, mode="funds", codes="", sector_type="industry"))
    assert captured["sector_codes"] is None
    assert captured["sector_type"] == "industry" and captured["limit"] == 3
    assert result["request"]["ranking"] is True


def test_fund_summary_keeps_only_the_nonmonetary_rank_comparison_fields():
    record = {
        "sector_code": "BK0001", "sector_type": "industry", "ts_code": None,
        "trade_date": "2026-10-23", "source_time": "2026-10-23T10:00:00+08:00",
        "received_at": "2026-10-23T10:00:01+08:00", "metric_scope": "synthetic_scope",
        "rank": 1, "rank_scope": "synthetic_ranking_scope", "rank_total": 3,
        "main_net_inflow_cny": "1234567", "raw_fields": {"f62": "1234567"},
    }
    result = probe.fund_summary({"status": "ok", "reason": None, "records": [record]}, "2026-10-23", "sector", {})
    returned = result["records"][0]
    for key in ("sector_code", "ts_code", "sector_type", "trade_date", "source_time", "received_at", "metric_scope", "rank", "rank_scope", "rank_total"):
        assert returned[key] == record[key]
    assert "1234567" not in json.dumps(result)
