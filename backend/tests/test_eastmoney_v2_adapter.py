"""Entry-point behaviour for the Eastmoney v2 adapter.

SYNTHETIC SAMPLES ONLY. No vendor response is embedded here; every number is
invented to exercise units, time windows and status aggregation. These tests are
pure unit tests, so the module overrides the package autouse fixture that would
otherwise require the disposable PostgreSQL used by integration suites.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.market.providers.eastmoney_v2 import adapter
from app.market.providers.eastmoney_v2.client import JsonResponse, TransportFailure

SHANGHAI = timezone(timedelta(hours=8))
RECEIVED = datetime(2026, 10, 23, 1, 20, 10, tzinfo=UTC)
RECEIVED_LATE = datetime(2026, 10, 23, 1, 25, 10, tzinfo=UTC)
STAGE_TIME = datetime(2026, 10, 23, 9, 20, 5, tzinfo=SHANGHAI)


@pytest.fixture(autouse=True)
def isolated_database():
    """These tests do not touch PostgreSQL; override the integration fixture."""
    yield


class FakeClient:
    def __init__(self, *responses, received_at=RECEIVED):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.received_at = received_at

    def request_json(self, path, params):
        self.calls.append((path, dict(params)))
        if not self.responses:
            raise AssertionError("unexpected request")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(path, params)
        return JsonResponse(value, self.received_at, 1, path)


def quote(**overrides):
    data = {
        "f43": 10.58,
        "f47": 12345,
        "f48": 130000000,
        "f51": 10.58,
        "f52": 8.66,
        "f57": "600000",
        "f58": "合成银行",
        "f60": 9.62,
        "f124": int(STAGE_TIME.timestamp()),
        "f170": 9.98,
        "f19": 10.58,
        "f20": 5000,
        "f39": 0,
        "f40": 0,
    }
    data.update(overrides)
    return {"rc": 0, "data": data}


def fund_row(**overrides):
    row = {
        "f3": 2.31,
        "f6": 100000000,
        "f12": "BK0475",
        "f13": 90,
        "f14": "合成板块",
        "f62": 1234567,
        "f66": 1000000,
        "f72": 234567,
        "f78": -1234567,
        "f84": 0,
        "f124": int(STAGE_TIME.timestamp()),
        "f184": 1.23,
    }
    row.update(overrides)
    return {"rc": 0, "data": {"total": 1, "diff": [row]}}


def test_auction_seal_uses_limit_bid_lots_and_not_turnover(monkeypatch):
    monkeypatch.setattr(adapter, "_now", lambda: datetime(2026, 10, 23, 9, 20, 30, tzinfo=SHANGHAI))
    client = FakeClient(quote())
    result = adapter.fetch_auction_order_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"], stage="09:20", client=client
    )
    assert result["provider"] == "eastmoney"
    assert result["status"] == "partial"
    record = result["records"][0]
    # 5,000 lots x 100 shares x 10.58; f48 (130,000,000) is deliberately unrelated.
    assert record["seal_amount_cny"] is None
    assert record["candidate_seal_amount_cny"] == "5290000"
    assert record["candidate_seal_volume_shares"] == "500000"
    assert record["bid1_volume_shares"] == "500000"
    assert record["matched_amount_cny"] is None
    assert record["reported_cumulative_turnover_cny"] == "130000000"
    assert record["candidate_match_state"] == "limit_up_sealed"
    assert record["candidate_seal_side"] == "buy"
    assert record["stage"] is None and record["stage_verified"] is False
    assert record["observed_phase"] == "09:20"
    assert record["checkpoint_offset_seconds"] == 5
    assert record["source_time"] == "2026-10-23T09:20:05+08:00"
    assert record["raw_fields"]["f20"] == "5000"


def test_star_board_quote_lot_is_one_hundred_shares(monkeypatch):
    monkeypatch.setattr(adapter, "_now", lambda: datetime(2026, 10, 23, 9, 25, 10, tzinfo=SHANGHAI))
    payload = quote(
        f57="688001",
        f58="合成科创",
        f43=20.0,
        f51=20.0,
        f19=20.0,
        f20=3000,
        f124=int(STAGE_TIME.replace(minute=25).timestamp()),
    )
    result = adapter.fetch_auction_order_snapshots(
        trade_date="2026-10-23", ts_codes=["688001.SH"], stage="09:25", client=FakeClient(payload, received_at=RECEIVED_LATE)
    )
    record = result["records"][0]
    assert record["bid1_volume_shares"] == "300000"
    assert record["candidate_seal_amount_cny"] == "6000000"
    assert "手 × 100" in record["unit_basis"]["volume"]


def test_stage_mismatch_is_partial_and_never_relabelled(monkeypatch):
    monkeypatch.setattr(adapter, "_now", lambda: datetime(2026, 10, 23, 9, 20, 30, tzinfo=SHANGHAI))
    payload = quote(f124=int(STAGE_TIME.replace(minute=16).timestamp()))
    result = adapter.fetch_auction_order_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"], stage="09:20", client=FakeClient(payload)
    )
    assert result["status"] == "partial"
    assert result["reason"] == "stage_unverified"
    record = result["records"][0]
    assert record["stage"] is None
    assert record["observed_phase"] == "09:15"
    assert record["requested_stage"] == "09:20"
    assert record["stage_verified"] is False


def test_historical_auction_is_unavailable_without_any_request(monkeypatch):
    monkeypatch.setattr(adapter, "_now", lambda: datetime(2026, 10, 24, 10, 0, 0, tzinfo=SHANGHAI))
    client = FakeClient()
    result = adapter.fetch_auction_order_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"], stage="09:20", client=client
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "auction_history_unavailable"
    assert result["records"] == [] and client.calls == []


def test_outside_window_is_unavailable(monkeypatch):
    monkeypatch.setattr(adapter, "_now", lambda: datetime(2026, 10, 23, 10, 0, 0, tzinfo=SHANGHAI))
    result = adapter.fetch_auction_order_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"], stage="09:20", client=FakeClient()
    )
    assert (result["status"], result["reason"]) == ("unavailable", "outside_auction_window")


def test_order_book_after_close_keeps_nulls_and_null_source_time():
    payload = {
        "rc": 0,
        "data": {"f43": 9.53, "f47": 755926, "f48": 726344396, "f57": "600000", "f58": "合成银行", "f124": 0.0},
    }
    result = adapter.fetch_order_book_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"], client=FakeClient(payload)
    )
    assert result["status"] == "partial"
    assert result["reason"] == "source_time_missing"
    record = result["records"][0]
    assert record["source_time"] is None
    assert record["bid1_price_cny"] is None and record["ask1_volume_shares"] is None
    # The received time is retained separately and never copied into source_time.
    assert record["received_at"] == "2026-10-23T09:20:10+08:00"
    assert record["matched_volume_shares"] == "75592600"


def test_cross_day_quote_is_rejected():
    payload = quote(f124=int(STAGE_TIME.replace(day=22).timestamp()))
    result = adapter.fetch_order_book_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"], client=FakeClient(payload)
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "stale_quote"
    assert result["records"] == []


def test_fund_scope_mismatch_keeps_provider_main_and_flags_it():
    payload = fund_row(f62=1000, f66=600, f72=300)
    result = adapter.fetch_sector_fund_snapshots(
        trade_date="2026-10-23", sector_codes=["BK0475"], include_intraday_change=False, client=FakeClient(payload)
    )
    assert result["status"] == "partial"
    assert result["reason"] == "main_net_scope_mismatch"
    record = result["records"][0]
    assert record["main_net_inflow_cny"] == "1000"
    assert record["main_net_consistency"]["status"] == "mismatch"
    assert record["main_net_consistency"]["delta_cny"] == "100"


def test_fund_five_minute_change_uses_two_real_cumulative_points():
    moment = STAGE_TIME.replace(minute=35)
    fund = fund_row(f62=-50, f66=-30, f72=-20, f124=int(moment.timestamp()))
    fflow = {
        "rc": 0,
        "data": {
            "klines": [
                "2026-10-23 09:30,-100.00,10,20,-60,-40",
                "2026-10-23 09:31,-120.00,10,20,-70,-50",
                "2026-10-23 09:35,-50.00,10,20,-30,-20",
            ]
        },
    }
    result = adapter.fetch_sector_fund_snapshots(
        trade_date="2026-10-23",
        sector_codes=["BK0475"],
        include_intraday_change=True,
        client=FakeClient(fund, fflow, received_at=moment + timedelta(seconds=10)),
    )
    assert result["status"] == "ok"
    record = result["records"][0]
    assert record["main_net_inflow_5m_change_cny"] == "50"
    assert record["change_window_start"] == "2026-10-23T09:30:00+08:00"
    assert record["change_window_end"] == "2026-10-23T09:35:00+08:00"
    assert "同源同口径" in record["change_basis"]


def test_missing_fflow_leaves_change_null_without_failing_fund_record():
    result = adapter.fetch_sector_fund_snapshots(
        trade_date="2026-10-23",
        sector_codes=["BK0475"],
        include_intraday_change=True,
        client=FakeClient(fund_row(), {"rc": 0, "data": {"klines": []}}),
    )
    assert result["status"] == "partial"
    assert result["reason"] == "intraday_change_unavailable"
    record = result["records"][0]
    assert record["main_net_inflow_5m_change_cny"] is None
    assert record["main_net_inflow_cny"] == "1234567"


def test_sector_ranking_has_rank_and_universe_total():
    rows = [
        {"f3": 1.0, "f6": 10, "f12": "BK0001", "f13": 90, "f14": "合成一", "f62": 200, "f124": int(STAGE_TIME.timestamp())},
        {"f3": 2.0, "f6": 20, "f12": "BK0002", "f13": 90, "f14": "合成二", "f62": 100, "f124": int(STAGE_TIME.timestamp())},
    ]
    payload = {"rc": 0, "data": {"total": 496, "diff": rows}}
    result = adapter.fetch_sector_fund_snapshots(
        trade_date="2026-10-23", sector_codes=None, include_intraday_change=False, client=FakeClient(payload)
    )
    assert result["status"] == "ok"
    assert [record["rank"] for record in result["records"]] == [1, 2]
    assert result["records"][0]["rank_total"] == 496
    assert result["records"][0]["rank_scope"] == "eastmoney:sector:fs=m:90+t:2:fid=f62:po=1"
    assert result["records"][0]["sector_type"] == "industry"
    assert result["records"][0]["rank_5m_change"] is None
    assert result["records"][0]["rank_change_status"] == "not_computed_requires_two_snapshots"


def test_stock_ranking_maps_sh_sz_bj_from_market_number():
    rows = [
        {"f3": 1.0, "f6": 10, "f12": "600000", "f13": 1, "f14": "合成沪市", "f62": 2, "f124": int(STAGE_TIME.timestamp())},
        {"f3": 2.0, "f6": 20, "f12": "000001", "f13": 0, "f14": "合成深市", "f62": 1, "f124": int(STAGE_TIME.timestamp())},
        {"f3": 3.0, "f6": 30, "f12": "920002", "f13": 0, "f14": "合成北交所", "f62": 0, "f124": int(STAGE_TIME.timestamp())},
    ]
    payload = {"rc": 0, "data": {"total": 3, "diff": rows}}
    result = adapter.fetch_stock_fund_snapshots(
        trade_date="2026-10-23", ts_codes=None, include_intraday_change=False, client=FakeClient(payload)
    )
    assert [record["ts_code"] for record in result["records"]] == ["600000.SH", "000001.SZ", "920002.BJ"]


def test_sector_members_keep_null_membership_date_and_map_codes(monkeypatch):
    monkeypatch.setattr(adapter, "_now", lambda: datetime(2026, 10, 23, 18, 0, 0, tzinfo=SHANGHAI))
    rows = [
        {"f3": 1.0, "f6": 10, "f12": "000001", "f13": 0, "f14": "合成深市", "f62": 1, "f124": 0},
        {"f3": 2.0, "f6": 20, "f12": "600000", "f13": 1, "f14": "合成沪市", "f62": 2, "f124": 0},
    ]
    payload = {"rc": 0, "data": {"total": 2, "diff": rows}}
    result = adapter.fetch_sector_members(
        trade_date="2026-10-23",
        sector_codes=["BK0475"],
        sector_type="industry",
        client=FakeClient(payload),
    )
    assert result["status"] == "partial"
    assert result["reason"] == "membership_date_unavailable"
    assert [record["ts_code"] for record in result["records"]] == ["000001.SZ", "600000.SH"]
    assert all(record["membership_date"] is None for record in result["records"])
    assert result["records"][0]["sector_identity"] == "eastmoney:BK0475"
    assert result["records"][0]["membership_date_basis"] == "not_provided_by_source"


def test_historical_membership_is_not_replayed(monkeypatch):
    monkeypatch.setattr(adapter, "_now", lambda: datetime(2026, 10, 24, 10, 0, 0, tzinfo=SHANGHAI))
    result = adapter.fetch_sector_members(
        trade_date="2026-10-23", sector_codes=["BK0475"], client=FakeClient()
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "historical_membership_unavailable"


def test_invalid_input_returns_error_not_empty_success():
    result = adapter.fetch_stock_fund_snapshots(
        trade_date="2026-10-23", ts_codes=["600000"], client=FakeClient()
    )
    assert result["status"] == "error"
    assert result["reason"] == "invalid_ts_code"
    assert result["records"] == []


def test_transport_failure_is_not_wrapped_as_ok():
    result = adapter.fetch_stock_fund_snapshots(
        trade_date="2026-10-23",
        ts_codes=["600000.SH"],
        client=FakeClient(TransportFailure("rate_limited", "upstream_rate_limited")),
    )
    assert result["status"] == "rate_limited"
    assert result["reason"] == "upstream_rate_limited"
    assert result["records"] == []


def test_repeated_sampling_keeps_the_source_clock_and_a_new_received_at():
    payload = quote()
    first = adapter.fetch_order_book_snapshots(
        trade_date="2026-10-23",
        ts_codes=["600000.SH"],
        client=FakeClient(payload, received_at=datetime(2026, 10, 23, 1, 20, 10, tzinfo=UTC)),
    )
    second = adapter.fetch_order_book_snapshots(
        trade_date="2026-10-23",
        ts_codes=["600000.SH"],
        client=FakeClient(payload, received_at=datetime(2026, 10, 23, 1, 20, 40, tzinfo=UTC)),
    )
    assert first["records"][0]["source_time"] == second["records"][0]["source_time"]
    assert first["records"][0]["received_at"] != second["records"][0]["received_at"]
    assert first["records"][0]["bid1_volume_shares"] == second["records"][0]["bid1_volume_shares"]


def test_missing_symbol_is_partial_not_a_silent_full_table():
    payload = quote()
    result = adapter.fetch_order_book_snapshots(
        trade_date="2026-10-23",
        ts_codes=["600000.SH", "000001.SZ"],
        client=FakeClient(payload, payload),
    )
    assert result["status"] == "partial"
    assert len(result["records"]) == 1
    assert result["records"][0]["ts_code"] == "600000.SH"


def test_order_book_also_exposes_a_limit_seal_snapshot():
    result = adapter.fetch_order_book_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"], client=FakeClient(quote())
    )
    record = result["records"][0]
    assert record["seal_side"] == "buy"
    assert record["seal_volume_shares"] == "500000"
    assert record["seal_amount_cny"] == "5290000"
    assert record["match_state"] == "limit_up_sealed"
    assert record["observation_basis"] == "polling_snapshot"


def test_price_without_volume_is_null_and_partial():
    payload = quote(f19=10.0)
    payload["data"].pop("f20")
    result = adapter.fetch_order_book_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"], client=FakeClient(payload)
    )
    assert result["status"] == "partial"
    assert result["reason"] == "order_book_partial"
    record = result["records"][0]
    assert record["bid1_price_cny"] == "10"
    assert record["bid1_volume_shares"] is None


def test_observed_seal_transition_is_not_a_tick_count():
    previous = {"ts_code": "600000.SH", "match_state": "limit_up_sealed", "source_time": "2026-10-23T10:00:00+08:00"}
    current = {"ts_code": "600000.SH", "match_state": "two_sided", "source_time": "2026-10-23T10:00:30+08:00"}
    transition = adapter.observed_seal_transition(previous, current)
    assert transition["observed_event"] == "seal_ended"
    assert transition["elapsed_seconds"] == 30
    assert transition["observation_scope"] == "adjacent_polling_snapshots_not_tick_stream"
    assert transition["count_claim"] == "none"
    assert adapter.observed_seal_transition(None, current)["observed_event"] == "first_observation"
    resealed = {"ts_code": "600000.SH", "match_state": "limit_up_sealed", "source_time": "2026-10-23T10:01:00+08:00"}
    assert adapter.observed_seal_transition(current, resealed)["observed_event"] == "seal_started"
    missing_time = adapter.observed_seal_transition({"match_state": "limit_up_sealed"}, current)
    assert missing_time["elapsed_seconds"] is None


def test_null_data_is_unavailable_not_ok_empty():
    result = adapter.fetch_stock_fund_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"], client=FakeClient({"rc": 0, "data": None})
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "no_records"
    assert result["records"] == []


def test_exact_auction_time_still_requires_live_field_semantics(monkeypatch):
    monkeypatch.setattr(adapter, "_now", lambda: STAGE_TIME)
    payload = quote(f124=int(STAGE_TIME.replace(second=0).timestamp()))
    result = adapter.fetch_auction_order_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"], stage="09:20", client=FakeClient(payload)
    )
    record = result["records"][0]
    assert record["stage"] == "09:20"
    assert record["stage_time_matched"] is True
    assert record["stage_verified"] is False
    assert result["status"] == "partial"
    assert result["reason"] == "auction_semantics_unverified"


@pytest.mark.parametrize(("source_time", "received"), [
    (None, STAGE_TIME),
    (int(STAGE_TIME.replace(second=11).timestamp()), STAGE_TIME.replace(second=10)),
])
def test_unknown_or_future_time_cannot_be_a_verified_quote(source_time, received):
    result = adapter.fetch_order_book_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"],
        client=FakeClient(quote(f124=source_time), received_at=received),
    )
    assert result["status"] != "ok"
    assert all(row["match_state"] != "limit_up_sealed" for row in result["records"])


def test_missing_opposite_book_is_not_a_disclosed_empty_queue():
    result = adapter.fetch_order_book_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"],
        client=FakeClient(quote(f39=None, f40=None)),
    )
    assert result["records"][0]["match_state"] == "at_limit_up_book_unknown"


@pytest.mark.parametrize("kind", ["quote", "fund"])
def test_missing_provider_date_cannot_be_used_to_replay_an_old_day(kind):
    reader = FakeClient(
        quote(f124=0) if kind == "quote" else fund_row(f124=0),
        received_at=STAGE_TIME.replace(day=24),
    )
    if kind == "quote":
        result = adapter.fetch_order_book_snapshots(trade_date="2026-10-23", ts_codes=["600000.SH"], client=reader)
    else:
        result = adapter.fetch_sector_fund_snapshots(trade_date="2026-10-23", sector_codes=["BK0475"], client=reader)
    assert result["status"] == "unavailable"
    assert result["records"] == []
    assert "source_date_unverified" in result["quality_flags"]


def test_per_symbol_transient_failure_does_not_discard_other_good_quotes():
    result = adapter.fetch_order_book_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH", "000001.SZ"],
        client=FakeClient(TransportFailure("error", "timeout"), quote(f57="000001")),
    )
    assert result["status"] == "partial"
    assert result["records"][0]["ts_code"] == "000001.SZ"
    assert "timeout" in result["quality_flags"]


def test_fund_duplicate_row_retains_other_valid_records():
    first = fund_row()["data"]["diff"][0]
    second = fund_row(f12="BK0002")["data"]["diff"][0]
    result = adapter.fetch_sector_fund_snapshots(
        trade_date="2026-10-23", sector_codes=["BK0475", "BK0002"],
        client=FakeClient({"rc": 0, "data": {"total": 3, "diff": [first, first, second]}}),
    )
    assert result["status"] == "partial"
    assert [row["sector_code"] for row in result["records"]] == ["BK0475", "BK0002"]
    assert "duplicate_instrument" in result["quality_flags"]


def test_large_change_request_is_rejected_before_network_fanout():
    reader = FakeClient()
    result = adapter.fetch_sector_fund_snapshots(
        trade_date="2026-10-23", sector_codes=None, limit=100, include_intraday_change=True, client=reader,
    )
    assert (result["status"], result["reason"]) == ("error", "too_many_intraday_change_symbols")
    assert reader.calls == []


@pytest.mark.parametrize("sector_type", ["theme", 123])
def test_invalid_ranking_type_is_rejected_without_network(sector_type):
    reader = FakeClient()
    result = adapter.fetch_sector_fund_snapshots(
        trade_date="2026-10-23", sector_codes=None, sector_type=sector_type, client=reader,
    )
    assert (result["status"], result["reason"]) == ("error", "invalid_sector_type")
    assert reader.calls == []


@pytest.mark.parametrize(("last_label", "start_label", "snapshot_label", "expected_flag"), [
    ("15:00", "14:55", "15:39", "intraday_change_unaligned"),
    ("13:42", "13:36", "13:42", "intraday_change_unavailable"),
    ("13:42", "13:37", "13:41", "future_source_time"),
])
def test_five_minute_delta_refuses_misaligned_future_or_six_minute_windows(
    last_label, start_label, snapshot_label, expected_flag,
):
    snapshot_time = datetime.fromisoformat(f"2026-10-23T{snapshot_label}:05+08:00")
    fund = fund_row(f62=150, f66=90, f72=60, f124=int(snapshot_time.timestamp()))
    fflow = {"rc": 0, "data": {"klines": [
        f"2026-10-23 {start_label},100,0,0,60,40",
        f"2026-10-23 {last_label},150,0,0,90,60",
    ]}}
    result = adapter.fetch_sector_fund_snapshots(
        trade_date="2026-10-23", sector_codes=["BK0475"], include_intraday_change=True,
        client=FakeClient(fund, fflow, received_at=snapshot_time + timedelta(seconds=5)),
    )
    assert result["status"] == "partial"
    assert result["records"][0]["main_net_inflow_5m_change_cny"] is None
    assert expected_flag in result["quality_flags"]
    if snapshot_label == "15:39":
        assert result["records"][0]["source_time_kind"] == "provider_update_time"
        assert result["records"][0]["market_time"] is None


@pytest.mark.parametrize(("current_time", "current_state", "expected_reason"), [
    ("10:00:00", "two_sided", "source_time_not_increasing"),
    ("09:59:59", "two_sided", "source_time_not_increasing"),
    ("10:03:00", "two_sided", "sampling_gap"),
    ("13:00:30", "two_sided", "different_trading_session"),
    ("10:00:30", "at_limit_up_book_unknown", "book_state_unknown"),
])
def test_transition_does_not_infer_open_events_across_gaps_or_unknown_books(
    current_time, current_state, expected_reason,
):
    previous = {"ts_code": "600000.SH", "source_time": "2026-10-23T10:00:00+08:00", "match_state": "limit_up_sealed"}
    current = {"ts_code": "600000.SH", "source_time": f"2026-10-23T{current_time}+08:00", "match_state": current_state}
    result = adapter.observed_seal_transition(previous, current)
    assert result["observed_event"] == "incomparable"
    assert result["comparison_reason"] == expected_reason


def test_membership_request_budget_is_shared_across_sectors(monkeypatch):
    monkeypatch.setattr(adapter, "_now", lambda: STAGE_TIME)
    response = {"rc": 0, "data": {"total": 1, "diff": [{"f12": "600000", "f13": 1, "f3": 1}]}}
    reader = FakeClient(response, response, response)
    result = adapter.fetch_sector_members(
        trade_date="2026-10-23", sector_codes=["BK0001", "BK0002", "BK0003", "BK0004"], client=reader,
    )
    assert result["status"] == "partial"
    assert len(result["records"]) == 3
    assert len(reader.calls) == 3
    assert "request_budget_exhausted" in result["quality_flags"]


def test_fund_enrichment_rate_limit_preserves_the_cumulative_record():
    result = adapter.fetch_sector_fund_snapshots(
        trade_date="2026-10-23", sector_codes=["BK0475"], include_intraday_change=True,
        client=FakeClient(fund_row(), TransportFailure("rate_limited", "upstream_rate_limited")),
    )
    assert result["status"] == "partial"
    assert result["reason"] == "upstream_rate_limited"
    assert result["records"][0]["main_net_inflow_cny"] == "1234567"
    assert result["records"][0]["main_net_inflow_5m_change_cny"] is None
    assert result["records"][0]["change_status"] == "rate_limited"


def test_hard_rate_limit_retains_retry_after_hint():
    result = adapter.fetch_order_book_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"],
        client=FakeClient(TransportFailure("rate_limited", "upstream_rate_limited", retry_after=120)),
    )
    assert result["status"] == "rate_limited"
    assert result["retry_after_seconds"] == 120


def test_same_day_stale_book_is_not_the_current_sealed_state():
    result = adapter.fetch_order_book_snapshots(
        trade_date="2026-10-23", ts_codes=["600000.SH"],
        client=FakeClient(quote(), received_at=STAGE_TIME + timedelta(minutes=5)),
    )
    assert result["status"] == "partial"
    assert result["records"][0]["match_state"] == "unknown"
    assert "source_time_stale" in result["quality_flags"]

def rank_record(code, rank, moment, *, scope="eastmoney_f62_desc", total=496, kind="sector", stale=False):
    record = {
        "trade_date": "2026-10-23",
        "rank": rank,
        "rank_scope": scope,
        "rank_total": total,
        "source_time": moment,
        "received_at": moment,
        "quality_flags": ["source_time_stale"] if stale else [],
        "main_net_inflow_cny": str(1000 - rank),
    }
    if kind == "sector":
        record["sector_code"] = code
        record["ts_code"] = None
    else:
        record["ts_code"] = code
        record["sector_code"] = None
    return record


def test_rank_change_uses_same_scope_and_exact_five_minutes():
    previous = [
        rank_record("BK0001", 5, "2026-10-23T10:00:00+08:00"),
        rank_record("BK0002", 2, "2026-10-23T10:00:00+08:00"),
        rank_record("BK0003", 8, "2026-10-23T10:00:00+08:00"),
    ]
    current = [
        rank_record("BK0001", 2, "2026-10-23T10:05:00+08:00"),
        rank_record("BK0002", 5, "2026-10-23T10:05:00+08:00"),
        rank_record("BK0003", 1, "2026-10-23T10:05:00+08:00"),
    ]
    result = adapter.observed_rank_change(previous, current)
    assert result["comparable"] is True and result["reason"] is None
    assert result["rank_change_status"] == "computed"
    assert result["elapsed_seconds"] == 300
    assert result["observation_scope"] == "same_scope_rank_change_between_two_polling_snapshots"
    changes = {item["code"]: item for item in result["changes"]}
    assert changes["BK0001"]["rank_change"] == 3
    assert changes["BK0002"]["rank_change"] == -3
    assert changes["BK0003"]["rank_change"] == 7
    assert result["coverage"]["common_count"] == 3


def test_rank_change_refuses_scope_or_total_changes():
    previous = [rank_record("BK0001", 5, "2026-10-23T10:00:00+08:00")]
    current = [rank_record("BK0001", 2, "2026-10-23T10:05:00+08:00", scope="other_scope")]
    assert adapter.observed_rank_change(previous, current)["reason"] == "rank_scope_mismatch"
    current = [rank_record("BK0001", 2, "2026-10-23T10:05:00+08:00", total=497)]
    assert adapter.observed_rank_change(previous, current)["reason"] == "rank_total_mismatch"


def test_rank_change_refuses_non_five_minute_or_cross_session_windows():
    previous = [rank_record("BK0001", 5, "2026-10-23T10:00:00+08:00")]
    six_minutes = [rank_record("BK0001", 2, "2026-10-23T10:06:00+08:00")]
    assert adapter.observed_rank_change(previous, six_minutes)["reason"] == "window_not_exact"
    lunch_prev = [rank_record("BK0001", 5, "2026-10-23T11:28:00+08:00")]
    lunch_curr = [rank_record("BK0001", 2, "2026-10-23T11:33:00+08:00")]
    assert adapter.observed_rank_change(lunch_prev, lunch_curr)["reason"] == "different_trading_session"


def test_rank_change_refuses_stale_spread_duplicate_or_missing_time():
    previous = [rank_record("BK0001", 5, "2026-10-23T10:00:00+08:00")]
    assert adapter.observed_rank_change(
        previous, [rank_record("BK0001", 2, "2026-10-23T10:05:00+08:00", stale=True)]
    )["reason"] == "stale_snapshot"
    spread = [
        rank_record("BK0001", 5, "2026-10-23T10:00:00+08:00"),
        rank_record("BK0002", 6, "2026-10-23T10:01:31+08:00"),
    ]
    assert adapter.observed_rank_change(spread, spread)["reason"] == "source_time_spread"
    duplicate = [rank_record("BK0001", 5, "2026-10-23T10:00:00+08:00")] * 2
    assert adapter.observed_rank_change(duplicate, previous)["reason"] == "duplicate_code"
    no_time = [rank_record("BK0001", 5, None)]
    assert adapter.observed_rank_change(no_time, previous)["reason"] == "source_time_missing"


def test_rank_change_reports_partial_coverage_without_inventing_ranks():
    previous = [
        rank_record("BK0001", 5, "2026-10-23T10:00:00+08:00"),
        rank_record("BK0002", 2, "2026-10-23T10:00:00+08:00"),
    ]
    current = [
        rank_record("BK0001", 2, "2026-10-23T10:05:00+08:00"),
        rank_record("BK0003", 1, "2026-10-23T10:05:00+08:00"),
    ]
    result = adapter.observed_rank_change(previous, current)
    assert result["comparable"] is True
    assert result["reason"] == "partial_rank_coverage"
    assert [item["code"] for item in result["changes"]] == ["BK0001"]
    assert result["coverage"]["missing_in_current"] == ["BK0002"]
    assert result["coverage"]["missing_in_previous"] == ["BK0003"]


def test_rank_change_requires_common_codes_and_one_instrument_kind():
    previous = [rank_record("BK0001", 5, "2026-10-23T10:00:00+08:00")]
    current = [rank_record("BK0002", 2, "2026-10-23T10:05:00+08:00")]
    assert adapter.observed_rank_change(previous, current)["reason"] == "no_common_codes"
    stock = [rank_record("600000.SH", 2, "2026-10-23T10:05:00+08:00", kind="stock")]
    assert adapter.observed_rank_change(previous, stock)["reason"] == "mixed_instrument_kind"
    assert adapter.observed_rank_change(previous, stock)["rank_change_status"] == "not_comparable"
