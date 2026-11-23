"""Unit and identity rules. SYNTHETIC VALUES ONLY."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.market.providers.eastmoney_v2 import mapping


@pytest.fixture(autouse=True)
def isolated_database():
    yield


SHANGHAI = timezone(timedelta(hours=8))


def test_decimal_string_keeps_value_and_removes_insignificant_zeros():
    assert mapping.decimal_string(Decimal("2.3100")) == "2.31"
    assert mapping.decimal_string(Decimal("-0.0")) == "0"
    assert mapping.decimal_string(Decimal("100")) == "100"
    assert mapping.decimal_string(None) is None


def test_amount_string_quantizes_to_cents():
    assert mapping.amount_string(Decimal("5290000")) == "5290000"
    assert mapping.amount_string(Decimal("5290000.005")) == "5290000.01"


@pytest.mark.parametrize(
    ("code", "expected"),
    [("600000.SH", "1.600000"), ("000001.SZ", "0.000001"), ("920002.BJ", "0.920002"), ("688001.SH", "1.688001")],
)
def test_security_identity_builds_secid(code, expected):
    identity = mapping.security_identity(code)
    assert identity["secid"] == expected
    assert identity["ts_code"] == code


@pytest.mark.parametrize("code", ["600000", "600000.HK", "000001.SH", "920002.SZ", "ABCDEF.SH"])
def test_security_identity_rejects_bare_or_mismatched_codes(code):
    with pytest.raises(mapping.MappingError):
        mapping.security_identity(code)


def test_ts_code_from_market_never_guesses_a_missing_market():
    assert mapping.ts_code_from_market("600000", 1) == "600000.SH"
    assert mapping.ts_code_from_market("000001", 0) == "000001.SZ"
    assert mapping.ts_code_from_market("920002", 0) == "920002.BJ"
    with pytest.raises(mapping.MappingError):
        mapping.ts_code_from_market("600000", None)
    with pytest.raises(mapping.MappingError):
        mapping.ts_code_from_market("000001", 1)


def test_duplicate_codes_are_refused():
    with pytest.raises(mapping.MappingError) as error:
        mapping.parse_ts_codes(["600000.SH", "600000.SH"])
    assert error.value.reason == "duplicate_ts_code"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("BK0475", "BK0475"), ("90.BK0475", "BK0475"), ("eastmoney:BK0475", "BK0475"), ("bk0475", "BK0475")],
)
def test_sector_codes_are_normalised(value, expected):
    assert mapping.parse_sector_code(value)["sector_code"] == expected


def test_sector_type_is_not_invented():
    assert mapping.parse_sector_code("BK0475")["sector_type"] is None
    assert mapping.parse_sector_codes(["BK0475"], "concept")[0]["sector_type"] == "concept"
    with pytest.raises(mapping.MappingError):
        mapping.parse_sector_codes(["BK0475"], "theme")


def test_quote_lot_does_not_use_star_minimum_order_quantity():
    assert mapping.lot_size("600000.SH") == 100
    assert mapping.lot_size("000001.SZ") == 100
    assert mapping.lot_size("920002.BJ") == 100
    assert mapping.lot_size("688001.SH") == 100
    assert mapping.lot_size("689009.SH") == 100
    assert "并非最小申报股数" in mapping.lot_basis("688001.SH")


def test_quote_lots_support_odd_shares_but_not_fractional_shares():
    assert mapping.shares_from_lots(5000, "600000.SH") == Decimal("500000")
    assert mapping.shares_from_lots(3000, "688001.SH") == Decimal("300000")
    assert mapping.shares_from_lots(Decimal("2.01"), "688001.SH") == Decimal("201")
    with pytest.raises(mapping.MappingError):
        mapping.shares_from_lots(Decimal("1.005"), "600000.SH")


def test_auction_checkpoints_are_separate_from_five_minute_phases():
    def at(hour, minute, second=0):
        return datetime(2026, 10, 23, hour, minute, second, tzinfo=SHANGHAI)

    assert mapping.stage_for(at(9, 14, 59)) is None
    assert mapping.stage_for(at(9, 15, 0)) == "09:15"
    assert mapping.stage_for(at(9, 19, 59)) is None
    assert mapping.auction_phase_for(at(9, 19, 59)) == "09:15"
    assert mapping.stage_for(at(9, 20, 0)) == "09:20"
    assert mapping.stage_for(at(9, 24, 59)) is None
    assert mapping.auction_phase_for(at(9, 24, 59)) == "09:20"
    assert mapping.stage_for(at(9, 25, 0)) == "09:25"
    assert mapping.stage_for(at(9, 25, 59)) is None
    assert mapping.stage_for(at(9, 26, 0)) is None


@pytest.mark.parametrize("code", ["900901.SH", "200001.SZ"])
def test_b_shares_are_refused_by_the_cny_a_share_adapter(code):
    with pytest.raises(mapping.MappingError):
        mapping.security_identity(code)


@pytest.mark.parametrize("market", [True, Decimal("1.5")])
def test_market_number_is_not_coerced_from_boolean_or_fraction(market):
    with pytest.raises(mapping.MappingError):
        mapping.ts_code_from_market("600000", market)


def test_epoch_zero_is_null_and_future_is_rejected():
    received = datetime(2026, 10, 23, 9, 20, 10, tzinfo=SHANGHAI)
    assert mapping.epoch_seconds(0, received_at=received) is None
    assert mapping.epoch_seconds("-", received_at=received) is None
    future = int((received + timedelta(minutes=5)).timestamp())
    with pytest.raises(mapping.MappingError) as error:
        mapping.epoch_seconds(future, received_at=received)
    assert error.value.reason == "future_source_time"


def test_numeric_rejections_are_explicit():
    with pytest.raises(mapping.MappingError):
        mapping.decimal_value(True, field="price")
    with pytest.raises(mapping.MappingError):
        mapping.decimal_value("NaN", field="price")
    with pytest.raises(mapping.MappingError):
        mapping.price(-1, field="last_price")
    assert mapping.money("-") is None
    assert mapping.percent("2.31") == Decimal("2.31")


def test_raw_fields_keep_original_spelling_and_units():
    kept = mapping.raw_fields(
        {"f20": 5000, "f62": Decimal("-123.40"), "f43": 10.58, "unused": "x"},
        ("f20", "f62", "f43"),
    )
    assert kept == {"f20": "5000", "f62": "-123.40", "f43": "10.58"}


def test_trade_date_accepts_iso_only():
    assert mapping.parse_trade_date("2026-10-23") == date(2026, 10, 23)
    with pytest.raises(mapping.MappingError):
        mapping.parse_trade_date("20261023")
    with pytest.raises(mapping.MappingError):
        mapping.parse_trade_date(datetime(2026, 10, 23, 0, 0))
