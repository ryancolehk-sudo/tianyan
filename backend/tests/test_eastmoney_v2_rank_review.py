"""Offline rank-window regression tests. All values are synthetic."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.market.providers.eastmoney_v2 import adapter
from app.market.providers.eastmoney_v2.client import JsonResponse


@pytest.fixture(autouse=True)
def isolated_database():
    """No network or database is needed for these synthetic records."""
    yield


def rank_record(code="BK0001", rank=2, at="10:00:00", **overrides):
    source = f"2026-10-23T{at}+08:00"
    record = {
        "sector_code": code,
        "ts_code": None,
        "rank": rank,
        "rank_total": 496,
        "rank_scope": "eastmoney_f62_desc",
        "trade_date": "2026-10-23",
        "source_time": source,
        "received_at": (datetime.fromisoformat(source) + timedelta(seconds=5)).isoformat(),
        "quality_flags": [],
    }
    record.update(overrides)
    return record


def rejected(previous, current, reason):
    result = adapter.observed_rank_change(previous, current)
    assert result["comparable"] is False
    assert result["rank_change_status"] == "not_comparable"
    assert result["reason"] == reason
    assert result["changes"] == []
    return result


@pytest.mark.parametrize("fraction", ["000001", "900000"])
def test_fractional_seconds_do_not_get_truncated_into_an_exact_window(fraction):
    result = rejected(
        [rank_record()], [rank_record(rank=1, at=f"10:05:00.{fraction}")],
        "window_not_exact",
    )
    assert result["elapsed_seconds"] > 300


def test_explicit_tolerance_preserves_the_actual_fractional_window():
    result = adapter.observed_rank_change(
        [rank_record()], [rank_record(rank=1, at="10:05:00.900000")],
        tolerance_seconds=1,
    )
    assert result["comparable"] is True
    assert result["elapsed_seconds"] == 300.9


def test_each_instrument_needs_the_exact_window_even_when_batch_maxima_match():
    previous = [
        rank_record(at="10:00:30"),
        rank_record("BK0002", rank=3),
    ]
    current = [
        rank_record(rank=1, at="10:05:00"),
        rank_record("BK0002", rank=2, at="10:05:30"),
    ]
    rejected(previous, current, "window_not_exact")


def test_every_record_must_belong_to_a_continuous_trading_session():
    previous = [
        rank_record(at="12:59:30"),
        rank_record("BK0002", rank=3, at="13:00:00"),
    ]
    current = [
        rank_record(rank=1, at="13:05:00"),
        rank_record("BK0002", rank=2, at="13:05:00"),
    ]
    rejected(previous, current, "different_trading_session")


@pytest.mark.parametrize("trade_day", [None, [], {}, True, "invalid", "20261023"])
def test_malformed_trade_date_is_controlled(trade_day):
    rejected(
        [rank_record(trade_date=trade_day)], [rank_record(rank=1, at="10:05:00")],
        "trade_date_missing",
    )


def test_source_clock_date_must_equal_the_declared_trade_date():
    rejected(
        [rank_record(trade_date="2026-10-22")],
        [rank_record(rank=1, at="10:05:00", trade_date="2026-10-22")],
        "source_trade_date_mismatch",
    )


@pytest.mark.parametrize("flags", [None, 1, "source_time_stale", {}, ["ok", 1]])
def test_malformed_quality_flags_are_controlled(flags):
    rejected(
        [rank_record(quality_flags=flags)], [rank_record(rank=1, at="10:05:00")],
        "invalid_quality_flags",
    )


@pytest.mark.parametrize("received", [None, "bad", "2026-10-23T10:00:01", "9999-12-31T23:59:59-12:00"])
def test_received_at_must_be_an_aware_representable_clock(received):
    rejected(
        [rank_record(received_at=received)], [rank_record(rank=1, at="10:05:00")],
        "received_at_missing",
    )


@pytest.mark.parametrize("source", [None, "bad", "2026-10-23T10:00:00", "9999-12-31T23:59:59-12:00"])
def test_source_time_must_be_an_aware_representable_clock(source):
    rejected(
        [rank_record(source_time=source)], [rank_record(rank=1, at="10:05:00")],
        "source_time_missing",
    )


def test_stale_rank_is_rejected_even_if_the_caller_omits_the_stale_flag():
    rejected(
        [rank_record(received_at="2026-10-23T14:00:00+08:00")],
        [rank_record(rank=1, at="10:05:00", received_at="2026-10-23T14:05:00+08:00")],
        "stale_snapshot",
    )


def test_freshness_threshold_does_not_truncate_subseconds():
    rejected(
        [rank_record(received_at="2026-10-23T10:01:30.000001+08:00")],
        [rank_record(rank=1, at="10:05:00")],
        "stale_snapshot",
    )


def test_source_cannot_follow_the_receipt_clock():
    rejected(
        [rank_record(received_at="2026-10-23T09:59:59.999999+08:00")],
        [rank_record(rank=1, at="10:05:00")],
        "future_source_time",
    )


def test_valid_alternative_timezone_is_compared_in_shanghai_time():
    result = adapter.observed_rank_change(
        [rank_record(source_time="2026-10-23T02:00:00+00:00", received_at="2026-10-23T02:00:05+00:00")],
        [rank_record(rank=1, at="10:05:00")],
    )
    assert result["comparable"] is True
    assert result["window_start"] == "2026-10-23T10:00:00+08:00"


@pytest.mark.parametrize("field,first,second", [
    ("sector_type", "industry", "concept"),
    ("metric_scope", "cumulative_main_net_inflow_sector", "another_net_scope"),
])
def test_available_scope_metadata_must_match_across_batches(field, first, second):
    rejected(
        [rank_record(**{field: first})],
        [rank_record(rank=1, at="10:05:00", **{field: second})],
        f"{field}_mismatch",
    )


@pytest.mark.parametrize("field,first,second", [
    ("sector_type", "industry", "concept"),
    ("metric_scope", "cumulative_main_net_inflow_sector", "another_net_scope"),
])
def test_one_batch_cannot_mix_available_scope_metadata(field, first, second):
    rejected(
        [rank_record(**{field: first}), rank_record("BK0002", rank=3, **{field: second})],
        [rank_record(rank=1, at="10:05:00")],
        f"{field}_mismatch",
    )


@pytest.mark.parametrize("field,bad,reason", [
    ("sector_type", [], "invalid_sector_type"),
    ("metric_scope", {}, "invalid_metric_scope"),
    ("rank_scope", " ", "rank_scope_missing"),
    ("rank", True, "rank_missing"),
    ("rank", 0, "rank_missing"),
    ("rank", 497, "rank_out_of_range"),
])
def test_invalid_scope_or_rank_is_controlled(field, bad, reason):
    rejected(
        [rank_record(**{field: bad})], [rank_record(rank=1, at="10:05:00")], reason,
    )


def test_duplicate_ordinal_rank_is_not_a_valid_provider_ranking():
    rejected(
        [rank_record(), rank_record("BK0002")], [rank_record(rank=1, at="10:05:00")],
        "duplicate_rank",
    )


class RankingClient:
    def __init__(self, kind):
        self.kind = kind
        self.params = None

    def request_json(self, path, params):
        self.params = params
        moment = datetime.fromisoformat("2026-10-23T10:00:00+08:00")
        row = {
            "f12": "600000" if self.kind == "stock" else "BK0001",
            "f13": 1 if self.kind == "stock" else 90,
            "f14": "合成对象",
            "f62": 1000,
            "f66": 600,
            "f72": 400,
            "f124": int(moment.timestamp()),
        }
        return JsonResponse(
            {"rc": 0, "data": {"total": 3, "diff": [row]}},
            moment + timedelta(seconds=5), 1, path,
        )


@pytest.mark.parametrize("kind,sector_type,fs", [
    ("sector", "industry", "m:90+t:2"),
    ("sector", "concept", "m:90+t:3"),
    ("sector", "region", "m:90+t:1"),
    ("stock", None, "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"),
])
def test_real_request_universe_and_sort_are_encoded_in_rank_scope(kind, sector_type, fs):
    client = RankingClient(kind)
    if kind == "sector":
        result = adapter.fetch_sector_fund_snapshots(
            trade_date="2026-10-23", sector_codes=None, sector_type=sector_type,
            limit=1, client=client,
        )
    else:
        result = adapter.fetch_stock_fund_snapshots(
            trade_date="2026-10-23", ts_codes=None, limit=1, client=client,
        )
    assert client.params["fs"] == fs
    record = result["records"][0]
    assert record["rank_scope"] == f"eastmoney:{kind}:fs={fs}:fid=f62:po=1"
    assert record["rank_total"] == 3
