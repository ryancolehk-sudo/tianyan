"""Reproducible end-of-day review arithmetic, with no I/O or inferred prices.

Inputs are raw, unadjusted Tushare tables. ``None`` means a table was not
loaded; ``[]`` means it was successfully loaded and contains no rows. Every
nonempty table must contain one explicit trade date. The caller must select
consecutive exchange trading days: this function rejects mixed/future dates
but deliberately does not invent a trading calendar. It does not establish
historical availability; callers retain the immutable evidence/as-of metadata.

The daily endpoint supplies A shares; only its SH/SZ rows are used, without a
current listing-directory join. Supplier pools and price-verified market counts
have different declared scopes (the supplier pool can exclude ST shares).
Missing observations are never imputed. Monetary/percentage results are decimal
strings; counts are integers. Percentages use 0..100, not 0..1.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

_BANDS = (
    "r<=-7", "-7<r<=-5", "-5<r<=-3", "-3<r<0", "r=0",
    "0<r<3", "3<=r<5", "5<=r<7", "r>=7",
)
_DISTRIBUTION_BANDS = (
    "limit_down", "down_ge7", "down_5_7", "down_3_5", "down_0_3", "flat",
    "up_0_3", "up_3_5", "up_5_7", "up_ge7", "limit_up",
)
_MISSING = (None, "", "--", "—")


def _text(value):
    if value is None:
        return None
    if not value:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{8}", value):
        raise ValueError("review table requires an explicit trade_date")
    return date.fromisoformat(value).isoformat()


class _Quality:
    def __init__(self):
        self.flags = {}

    def add(self, code, table, field=None, symbol=None):
        key = (code, table, field or "")
        self.flags.setdefault(key, set()).add(symbol or "")

    def export(self):
        return [
            {"code": code, "table": table, "field": field or None,
             "count": len(symbols), "sample_ts_codes": sorted(s for s in symbols if s)[:20]}
            for (code, table, field), symbols in sorted(self.flags.items())
        ]


def _number(row, key, table, quality, *, positive=False, nonnegative=False):
    raw = row.get(key)
    symbol = row["ts_code"]
    if raw in _MISSING:
        quality.add("missing_value", table, key, symbol)
        return None
    try:
        if isinstance(raw, bool):
            raise InvalidOperation
        value = Decimal(str(raw))
        if not value.is_finite() or (positive and value <= 0) or (nonnegative and value < 0):
            raise InvalidOperation
        return value
    except (InvalidOperation, ValueError, TypeError):
        quality.add("invalid_value", table, key, symbol)
        return None


def _rows(rows, table, quality, expected_day=None, *, pool=False):
    if rows is None:
        quality.add("table_not_loaded", table)
        return None, None, 0
    if not isinstance(rows, list):
        raise ValueError(f"{table}: expected a table or None")
    indexed, days, seen, excluded = {}, set(), set(), 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{table}: invalid row")
        row_day = _day(row.get("trade_date"))
        days.add(row_day)
        if expected_day is not None and row_day != expected_day:
            raise ValueError(f"{table}: wrong trade_date")
        symbol = row.get("ts_code")
        if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
            raise ValueError(f"{table}: invalid A-share code")
        # Even identical duplicates or different pool types cannot silently win.
        if symbol in seen:
            raise ValueError(f"{table}: duplicate or conflicting instrument")
        seen.add(symbol)
        if pool and row.get("limit") not in {"U", "D", "Z"}:
            raise ValueError(f"{table}: invalid limit pool type")
        if symbol.endswith(".BJ"):
            excluded += 1
            continue
        indexed[symbol] = row
    if len(days) > 1:
        raise ValueError(f"{table}: mixed trade dates")
    return indexed, next(iter(days), None), excluded


def _pct(numerator, denominator):
    return _text(Decimal(numerator) * 100 / Decimal(denominator)) if denominator else None


def _median(values):
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return None
    n = len(ordered)
    return _text(ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2)


def _mean(values):
    observed = [value for value in values if value is not None]
    return _text(sum(observed, Decimal(0)) / len(observed)) if observed else None


def _band(value):
    if value <= -7:
        return 0
    if value <= -5:
        return 1
    if value <= -3:
        return 2
    if value < 0:
        return 3
    if value == 0:
        return 4
    if value < 3:
        return 5
    if value < 5:
        return 6
    if value < 7:
        return 7
    return 8


def _turnover(rows, table, quality):
    values = []
    for row in (rows or {}).values():
        value = _number(row, "amount", table, quality, nonnegative=True)
        if value is not None:
            values.append(value * 1000)  # daily.amount is in thousand yuan.
    n = len(rows or {})
    complete = rows is not None and n > 0 and len(values) == n
    total = sum(values, Decimal(0)) if values else None
    return {
        "state": "not_loaded" if rows is None else "empty" if not n else "complete" if complete else "partial",
        "N": n if rows is not None else None, "valid_n": len(values),
        "missing_n": n - len(values) if rows is not None else None,
        "amount_yuan": _text(total) if complete else None,
        "known_amount_yuan": _text(total),
        "top_n": min(20, len(values)),
        "top20_share_pct": _text(sum(sorted(values, reverse=True)[:20], Decimal(0)) * 100 / total)
        if complete and total else None,
    }


def _heights(rows, table, quality):
    result = {}
    for symbol, row in (rows or {}).items():
        if row["limit"] != "U":
            continue
        height = _number(row, "limit_times", table, quality, positive=True)
        if height is not None and height == height.to_integral_value():
            result[symbol] = int(height)
        elif height is not None:
            quality.add("invalid_value", table, "limit_times", symbol)
    return result


def _verify_limits(daily, caps, quality):
    statuses = {}
    no_limit_n = 0
    for symbol, row in (daily or {}).items():
        status = {"up": None, "down": None, "broken": None}
        statuses[symbol] = status
        cap = (caps or {}).get(symbol)
        if cap is None:
            quality.add("missing_price_limits", "price_limits", symbol=symbol)
            continue
        up = _number(cap, "up_limit", "price_limits", quality, nonnegative=True)
        down = _number(cap, "down_limit", "price_limits", quality, nonnegative=True)
        if up is None or down is None:
            continue
        if up == 0 and down == 0:
            no_limit_n += 1
            status["not_applicable"] = True
            continue
        if down <= 0 or up <= down:
            quality.add("invalid_price_limit_range", "price_limits", symbol=symbol)
            continue
        close = _number(row, "close", "daily", quality, positive=True)
        if close is None:
            continue
        if close > up or close < down:
            quality.add("close_outside_price_limits", "daily", "close", symbol)
            continue
        status["up"], status["down"] = close == up, close == down
        high = _number(row, "high", "daily", quality, positive=True)
        if high is not None:
            if high < close or high > up:
                quality.add("high_outside_price_limits", "daily", "high", symbol)
            else:
                status["broken"] = high == up and close < up
        if status["up"] is True:
            # A closing seal is not a broken seal, even if high was not supplied.
            status["broken"] = False
    comparable_n = sum(s["up"] is not None for s in statuses.values())
    unknown_n = len(statuses) - comparable_n - no_limit_n
    high_unknown_n = sum(s["up"] is not None and s["broken"] is None for s in statuses.values())
    up_count = sum(s["up"] is True for s in statuses.values())
    down_count = sum(s["down"] is True for s in statuses.values())
    broken_valid_n = sum(s["broken"] is not None for s in statuses.values())
    broken_count = sum(s["broken"] is True for s in statuses.values())
    return statuses, {
        "scope": "沪深日线中具有有效当日限制价的股票，可能包含ST",
        "state": "not_loaded" if daily is None else "empty" if not daily else
        "partial" if unknown_n or high_unknown_n else "complete",
        "N": len(statuses) if daily is not None else None,
        "valid_n": comparable_n, "unverified_n": unknown_n,
        "no_price_limit_n": no_limit_n, "broken_unverified_n": high_unknown_n,
        "up": up_count if comparable_n or daily == {} else None,
        "down": down_count if comparable_n or daily == {} else None,
        "broken": broken_count if broken_valid_n or daily == {} else None,
    }


def _zero_trade_placeholder(row, close):
    """Recognize a zero-trade carried-close row without inferring suspension."""
    try:
        raw = [row[key] for key in ("open", "high", "low", "vol", "amount", "pct_chg")]
        raw.append(row["pre_close"])
        if any(isinstance(value, bool) for value in raw):
            return False
        *zeros, previous = [Decimal(str(value)) for value in raw]
        return (all(value.is_finite() and value == 0 for value in zeros)
                and previous.is_finite() and previous > 0 and close == previous)
    except (KeyError, InvalidOperation, ValueError, TypeError):
        return False


def _intraday_limits(daily, caps):
    """Keep intraday touches and opened seals separate from closing seals.

    Each side has its own completeness denominator. ``known_*`` fields are
    observed lower bounds; totals are withheld whenever that side has unknown
    members. A closing seal establishes a touch even without its high/low,
    whereas a supplied contradictory high/low makes that side unverifiable.
    This projection has independent quality flags and cannot change the older
    closing-price, supplier-pool or relay calculations.
    """
    quality = _Quality()
    states = []
    no_limit_n = no_trade_n = 0
    if daily is None:
        quality.add("table_not_loaded", "daily")
    for symbol, row in (daily or {}).items():
        sides = {side: None for side in ("up", "down")}
        states.append(sides)
        cap = (caps or {}).get(symbol)
        if cap is None:
            quality.add("missing_price_limits", "price_limits", symbol=symbol)
            continue
        upper = _number(cap, "up_limit", "price_limits", quality, nonnegative=True)
        lower = _number(cap, "down_limit", "price_limits", quality, nonnegative=True)
        if upper is None or lower is None:
            continue
        if upper == 0 and lower == 0:
            no_limit_n += 1
            for side in sides:
                sides[side] = {"touched": False, "close": False, "opened": False}
            continue
        if lower <= 0 or upper <= lower:
            quality.add("invalid_price_limit_range", "price_limits", symbol=symbol)
            continue
        close = _number(row, "close", "daily", quality, positive=True)
        if close is None:
            continue
        if not lower <= close <= upper:
            quality.add("close_outside_price_limits", "daily", "close", symbol)
            continue
        if _zero_trade_placeholder(row, close):
            no_trade_n += 1
            for side in sides:
                sides[side] = {"touched": False, "close": False, "opened": False}
            continue
        high = _number(row, "high", "daily", quality, positive=True)
        low = _number(row, "low", "daily", quality, positive=True)
        high_invalid = high is None and row.get("high") not in _MISSING
        low_invalid = low is None and row.get("low") not in _MISSING
        if high is not None and not close <= high <= upper:
            quality.add("high_outside_price_limits", "daily", "high", symbol)
            high_invalid = True
        if low is not None and not lower <= low <= close:
            quality.add("low_outside_price_limits", "daily", "low", symbol)
            low_invalid = True
        if high is not None and low is not None and high < low:
            quality.add("invalid_ohlc_range", "daily", "high,low", symbol)
            high_invalid = low_invalid = True
        for side, extreme, endpoint, invalid in (
                ("up", high, upper, high_invalid), ("down", low, lower, low_invalid)):
            sealed = close == endpoint
            if invalid or (extreme is None and not sealed):
                continue
            touched = sealed or extreme == endpoint
            sides[side] = {"touched": touched, "close": sealed,
                           "opened": touched and not sealed}
    n = len(states) if daily is not None else None
    result = {
        "version": "ohlc-limits-v1",
        "scope": "同日沪深日线与个股真实限制价；曾涨停/曾跌停仅指盘中触板且收盘已打开，不含收盘封板",
        "state": "not_loaded" if daily is None else "empty" if not daily else "complete",
        "N": n, "no_price_limit_n": no_limit_n if daily is not None else None,
        "no_trade_n": no_trade_n if daily is not None else None,
    }
    for side in ("up", "down"):
        observed = [state[side] for state in states if state[side] is not None]
        valid_n = len(observed)
        unknown_n = n - valid_n if n is not None else None
        result[f"{side}_valid_n"] = valid_n
        result[f"{side}_unknown_n"] = unknown_n
        if unknown_n:
            result["state"] = "partial"
        for field in ("touched", "close", "opened"):
            known_n = sum(state[field] for state in observed)
            result[f"known_{field}_{side}"] = known_n if daily is not None else None
            result[f"{field}_{side}"] = known_n if unknown_n == 0 else None
    result["quality_flags"] = quality.export()
    return result


def _breadth_distribution(changes, statuses, sample_n, quality):
    """Use the breadth denominator, with price-verified seals as end buckets.

    Unknown seals still contribute their observed return to an ordinary band.
    A seal whose direction contradicts pct_chg cannot change breadth's sign
    totals; expose that conflict and keep the supplied return's ordinary band.
    """
    bins = dict.fromkeys(_DISTRIBUTION_BANDS, 0)
    unverified_n = no_limit_n = 0
    for symbol, value in changes.items():
        status = statuses.get(symbol, {})
        if status.get("not_applicable"):
            no_limit_n += 1
        elif status.get("up") is None or status.get("down") is None:
            unverified_n += 1
        elif ((status["up"] and value <= 0) or
              (status["down"] and value >= 0)):
            quality.add("limit_pct_direction_mismatch", "daily", "pct_chg", symbol)
            unverified_n += 1
        elif status["up"]:
            bins["limit_up"] += 1
            continue
        elif status["down"]:
            bins["limit_down"] += 1
            continue
        if value == 0:
            bins["flat"] += 1
        else:
            magnitude = abs(value)
            band = "0_3" if magnitude < 3 else "3_5" if magnitude < 5 else "5_7" if magnitude < 7 else "ge7"
            bins[f"{'up' if value > 0 else 'down'}_{band}"] += 1
    valid_n = len(changes)
    missing_n = sample_n - valid_n if sample_n is not None else None
    return {
        "version": "absolute-limits-v1",
        "state": "not_loaded" if sample_n is None else "empty" if not sample_n else
        "partial" if missing_n or unverified_n else "complete",
        "bins": bins if sample_n is not None else dict.fromkeys(_DISTRIBUTION_BANDS),
        "valid_n": valid_n, "missing_n": missing_n,
        "limit_unverified_n": unverified_n if sample_n is not None else None,
        "no_limit_n": no_limit_n if sample_n is not None else None,
    }


def _supplier_limits(pools, heights, statuses, quality, seal_unit_verified):
    if pools is None:
        return {"state": "not_loaded", "scope": "供应商沪深涨跌停池披露范围", "N": None,
                **dict.fromkeys(("up", "down", "broken", "first_board", "second_board", "third_plus",
                                 "max_height", "second_height", "broken_rate_pct", "up_down_ratio", "ladder")),
                "height_valid_n": 0, "height_missing_n": None,
                "max_disclosed_seal_amount_yuan": None,
                "known_max_disclosed_seal_amount_yuan": None, "seal_amount_state": "not_loaded",
                "seal_amount_scope": "当日供应商披露封单金额最大值；不是盘中峰值"}
    counts = {kind: sum(row["limit"] == kind for row in pools.values()) for kind in ("U", "D", "Z")}
    ladder = {str(h): sum(value == h for value in heights.values()) for h in sorted(set(heights.values()))}
    for symbol, row in pools.items():
        if symbol not in statuses:
            quality.add("pool_without_daily", "pools", symbol=symbol)
            continue
        status = statuses[symbol][{"U": "up", "D": "down", "Z": "broken"}[row["limit"]]]
        if status is False:
            quality.add("supplier_price_disagreement", "pools", "limit", symbol)
        elif status is None:
            quality.add("pool_price_unverified", "pools", "limit", symbol)
    seals = []
    seal_rows = [row for row in pools.values() if row["limit"] == "U"]
    if seal_rows and not seal_unit_verified:
        quality.add("seal_amount_unit_unverified", "pools", "fd_amount")
    elif seal_unit_verified:
        for row in seal_rows:
            value = _number(row, "fd_amount", "pools", quality, nonnegative=True)
            if value is not None:
                seals.append(value)
    return {
        "state": "empty" if not pools else "available", "scope": "供应商沪深涨跌停池披露范围，可能排除ST",
        "N": len(pools), "up": counts["U"], "down": counts["D"], "broken": counts["Z"],
        "first_board": sum(h == 1 for h in heights.values()),
        "second_board": sum(h == 2 for h in heights.values()),
        "third_plus": sum(h >= 3 for h in heights.values()),
        "height_valid_n": len(heights), "height_missing_n": counts["U"] - len(heights),
        "max_height": max(heights.values(), default=None),
        "second_height": sorted(set(heights.values()), reverse=True)[1] if len(set(heights.values())) > 1 else None,
        "ladder": ladder,
        "broken_rate_pct": _pct(counts["Z"], counts["U"] + counts["Z"]),
        "up_down_ratio": _text(Decimal(counts["U"]) / counts["D"]) if counts["D"] else None,
        "max_disclosed_seal_amount_yuan": _text(max(seals)) if seals and len(seals) == len(seal_rows) else None,
        "known_max_disclosed_seal_amount_yuan": _text(max(seals)) if seals else None,
        "seal_amount_state": "unit_unverified" if seal_rows and not seal_unit_verified else
        "partial" if len(seals) != len(seal_rows) else "empty" if not seal_rows else "verified_unit",
        "seal_amount_scope": "当日供应商披露封单金额最大值；不是盘中峰值",
    }


def _cohort(symbols, daily, statuses, previous_heights, quality, *, promotion=False):
    if symbols is None:
        return {"state": "not_loaded", "N": None, "valid_n": 0, "missing_n": None,
                "samples": [], "open_median_pct": None, "close_median_pct": None,
                "open_mean_pct": None, "close_mean_pct": None,
                "known_open_mean_pct": None, "known_close_mean_pct": None,
                "open_valid_n": 0, "close_valid_n": 0,
                "promotion_n": None, "promotion_pct": None, "down_limit_n": None,
                "promotion_valid_n": None, "up_limit_n": None, "up_limit_valid_n": 0,
                "down_limit_valid_n": 0, "down_limit_pct": None}
    samples, opens, closes = [], [], []
    for symbol in sorted(symbols):
        row = (daily or {}).get(symbol)
        open_pct = close_pct = None
        if row is not None:
            pre_close = _number(row, "pre_close", "daily", quality, positive=True)
            opening = _number(row, "open", "daily", quality, positive=True)
            closing = _number(row, "close", "daily", quality, positive=True)
            if pre_close is not None:
                if opening is not None:
                    open_pct = (opening / pre_close - 1) * 100
                if closing is not None:
                    close_pct = (closing / pre_close - 1) * 100
        if open_pct is None or close_pct is None:
            quality.add("cohort_missing_quote", "relay", symbol=symbol)
        opens.append(open_pct)
        closes.append(close_pct)
        status = statuses.get(symbol, {})
        if status.get("up") is None or status.get("down") is None:
            quality.add("cohort_price_unverified", "relay", symbol=symbol)
        samples.append({
            "ts_code": symbol, "previous_height": previous_heights.get(symbol),
            "open_pct": _text(open_pct), "close_pct": _text(close_pct),
            "promotion": status.get("up") if promotion else None,
            "up_limit": status.get("up"), "down_limit": status.get("down"),
        })
    n = len(samples)
    valid_n = sum(o is not None and c is not None for o, c in zip(opens, closes, strict=True))
    up_valid = sum(s["up_limit"] is not None for s in samples)
    down_valid = sum(s["down_limit"] is not None for s in samples)
    up_n = sum(s["up_limit"] is True for s in samples)
    down_n = sum(s["down_limit"] is True for s in samples)
    open_valid_n = sum(o is not None for o in opens)
    close_valid_n = sum(c is not None for c in closes)
    known_open_mean = _mean(opens)
    known_close_mean = _mean(closes)
    return {
        "state": "empty" if not n else "complete" if valid_n == n and up_valid == n and down_valid == n else "partial",
        "N": n, "valid_n": valid_n, "missing_n": n - valid_n,
        "open_valid_n": open_valid_n, "close_valid_n": close_valid_n,
        "open_median_pct": _median(opens), "close_median_pct": _median(closes),
        "open_mean_pct": known_open_mean if n and open_valid_n == n else None,
        "close_mean_pct": known_close_mean if n and close_valid_n == n else None,
        "known_open_mean_pct": known_open_mean, "known_close_mean_pct": known_close_mean,
        "promotion_n": up_n if promotion else None, "promotion_valid_n": up_valid if promotion else None,
        "promotion_pct": _pct(up_n, n) if promotion and valid_n == n and up_valid == n else None,
        "up_limit_n": up_n, "up_limit_valid_n": up_valid,
        "down_limit_n": down_n, "down_limit_valid_n": down_valid,
        "down_limit_pct": _pct(down_n, n) if valid_n == n and down_valid == n else None,
        "samples": samples,
    }


def _previous_failures(prior_pool, previous, previous_caps, previous_pool, quality):
    """Classify the fixed T-2 supplier-U set using actual T-1 trade evidence.

    Absence from a vendor pool is not evidence that a stock failed to seal.
    Missing/zero trading evidence is not automatically diagnosed as suspension.
    """
    scope = "T-2供应商沪深涨停样本，按T-1有成交的收盘与当日上限核验；含首板，非近期滚动全量"
    if prior_pool is None:
        return {"state": "not_loaded", "scope": scope, "N": None, "valid_n": 0,
                "confirmed_failed_n": None, "continued_n": None, "unknown_n": None,
                "source_conflict_n": None, "pool_membership_difference_n": None,
                "samples": []}, None
    samples, confirmed = [], set()
    candidates = {s for s, row in prior_pool.items() if row["limit"] == "U"}
    for symbol in sorted(candidates):
        row = (previous or {}).get(symbol)
        cap = (previous_caps or {}).get(symbol)
        reasons, close, upper = [], None, None
        trade_evidence = None
        if row is None:
            reasons.append("previous_quote_missing")
        else:
            close = _number(row, "close", "previous_daily", quality, positive=True)
            if close is None:
                reasons.append("previous_close_unusable")
            observed_turnover = [
                _number(row, key, "previous_daily", quality, nonnegative=True)
                for key in ("amount", "vol") if row.get(key) not in _MISSING
            ]
            valid_turnover = [value for value in observed_turnover if value is not None]
            trade_evidence = any(value > 0 for value in valid_turnover) if valid_turnover else None
            if trade_evidence is not True:
                reasons.append("previous_trading_unverified")
        if cap is None:
            reasons.append("previous_price_limit_missing")
        else:
            upper = _number(cap, "up_limit", "previous_price_limits", quality, nonnegative=True)
            if upper is None:
                reasons.append("previous_upper_limit_unusable")
            elif upper == 0:
                reasons.append("previous_price_limit_not_applicable")
            elif close is not None and close > upper:
                reasons.append("previous_close_above_limit")
            if cap.get("down_limit") not in _MISSING:
                lower = _number(cap, "down_limit", "previous_price_limits", quality, nonnegative=True)
                if lower is None or (upper is not None and upper > 0 and (lower <= 0 or lower >= upper)):
                    reasons.append("previous_price_limit_range_unusable")
                elif close is not None and lower > 0 and close < lower:
                    reasons.append("previous_close_below_limit")
        classification = "unknown" if reasons else "continued" if close == upper else "confirmed_failed"
        if classification == "confirmed_failed":
            confirmed.add(symbol)
        reported = (previous_pool or {}).get(symbol)
        reported_limit = reported["limit"] if reported is not None else None
        known_membership = previous_pool is not None and classification != "unknown"
        membership_difference = (reported_limit == "U") != (classification == "continued") if known_membership else None
        # A missing row can reflect a scope change (for example ST exclusion).
        # Only an explicit contrary U/D/Z record is a source-fact conflict.
        source_conflict = membership_difference if reported is not None and known_membership else None
        if source_conflict:
            quality.add("previous_pool_price_conflict", "previous_pools", "limit", symbol)
        elif membership_difference:
            quality.add("previous_pool_membership_difference", "previous_pools", "limit", symbol)
        for reason in reasons:
            quality.add(reason, "relay.failed_classification", symbol=symbol)
        samples.append({"ts_code": symbol, "classification": classification, "reasons": reasons,
                        "previous_close": _text(close), "previous_up_limit": _text(upper),
                        "previous_trading_verified": trade_evidence,
                        "previous_pool_limit": reported_limit,
                        "previous_pool_loaded": previous_pool is not None,
                        "source_conflict": source_conflict,
                        "pool_membership_difference": membership_difference})
    unknown_n = sum(row["classification"] == "unknown" for row in samples)
    return {"state": "empty" if not candidates else "partial" if unknown_n else "complete",
            "scope": scope, "N": len(candidates), "valid_n": len(candidates) - unknown_n,
            "confirmed_failed_n": len(confirmed),
            "continued_n": sum(row["classification"] == "continued" for row in samples),
            "unknown_n": unknown_n, "source_conflict_n": sum(row["source_conflict"] is True for row in samples),
            "pool_membership_difference_n": sum(row["pool_membership_difference"] is True for row in samples),
            "samples": samples}, confirmed


def _sealed_turnover(symbols, daily, market_amount, quality, *, membership_unknown=0):
    if symbols is None or daily is None:
        return {"state": "not_loaded", "N": None, "valid_n": 0, "missing_n": None,
                "membership_unknown_n": membership_unknown, "amount_yuan": None,
                "known_amount_yuan": None, "share_pct": None}
    amounts = []
    for symbol in symbols:
        row = daily.get(symbol)
        if row is not None:
            value = _number(row, "amount", "daily", quality, nonnegative=True)
            if value is not None:
                amounts.append(value * 1000)
    complete = len(amounts) == len(symbols) and not membership_unknown
    total = sum(amounts, Decimal(0))
    denominator = Decimal(market_amount) if market_amount is not None else None
    return {"state": "complete" if complete else "partial", "N": len(symbols),
            "valid_n": len(amounts), "missing_n": len(symbols) - len(amounts),
            "membership_unknown_n": membership_unknown,
            "amount_yuan": _text(total) if complete else None,
            "known_amount_yuan": _text(total),
            "share_pct": _text(total / denominator * 100) if complete and denominator else None}


def build_daily_metrics(*, trade_day: str, daily: list[dict] | None,
                        price_limits: list[dict] | None, pools: list[dict] | None,
                        previous_daily: list[dict] | None = None,
                        previous_price_limits: list[dict] | None = None,
                        previous_pools: list[dict] | None = None,
                        prior_pools: list[dict] | None = None,
                        seal_amount_unit_verified: bool = False) -> dict:
    """Calculate one review day; invalid identity/date tables raise ValueError.

    ``previous_*`` are T-1 and ``prior_pools`` is T-2. Missing cohort quotes keep
    their original member in N. Medians/known means report observed values with
    counts; full means require every corresponding opening/closing observation.
    Promotion/down-limit percentages are withheld for incomplete denominators.
    Without previous_price_limits, prior U-pool exits remain unverified rather
    than being labelled confirmed failures. Candidate N includes all T-2 U rows;
    failed.N includes confirmed failures and never drops missing T-day quotes.
    This function must not be used to claim that a reconstructed historical
    table was already known on that date.
    """
    trade_day = _day(trade_day)
    quality = _Quality()
    current, _, excluded = _rows(daily, "daily", quality, trade_day)
    caps, _, _ = _rows(price_limits, "price_limits", quality, trade_day)
    pool_rows, _, _ = _rows(pools, "pools", quality, trade_day, pool=True)
    previous, previous_day, _ = _rows(previous_daily, "previous_daily", quality)
    previous_caps, previous_caps_day, _ = _rows(previous_price_limits, "previous_price_limits", quality)
    previous_pool, previous_pool_day, _ = _rows(previous_pools, "previous_pools", quality, pool=True)
    prior_pool, prior_day, _ = _rows(prior_pools, "prior_pools", quality, pool=True)
    previous_days = {d for d in (previous_day, previous_caps_day, previous_pool_day) if d is not None}
    if len(previous_days) > 1:
        raise ValueError("previous tables have different trade dates")
    previous_day = next(iter(previous_days), None)
    if previous_day is not None and previous_day >= trade_day:
        raise ValueError("previous tables are not before trade_day")
    if prior_day is not None and (prior_day >= trade_day or
                                  (previous_day is not None and prior_day >= previous_day)):
        raise ValueError("prior_pools is not before the previous trade day")

    bins, changes = dict.fromkeys(_BANDS, 0), {}
    for symbol, row in (current or {}).items():
        value = _number(row, "pct_chg", "daily", quality)
        if value is not None:
            bins[_BANDS[_band(value)]] += 1
            changes[symbol] = value
    valid_n = sum(bins.values())
    breadth = {
        "state": "not_loaded" if current is None else "empty" if not current else
        "complete" if valid_n == len(current) else "partial",
        "up": sum(bins[key] for key in _BANDS[5:]) if current is not None else None,
        "down": sum(bins[key] for key in _BANDS[:4]) if current is not None else None,
        "flat": bins["r=0"] if current is not None else None, "valid_n": valid_n,
        "bins": bins if current is not None else dict.fromkeys(_BANDS),
        "missing_n": len(current) - valid_n if current is not None else None,
    }
    turnover = _turnover(current, "daily", quality)
    previous_turnover = _turnover(previous, "previous_daily", quality)
    amount = Decimal(turnover["amount_yuan"]) if turnover["amount_yuan"] is not None else None
    previous_amount = Decimal(previous_turnover["amount_yuan"]) if previous_turnover["amount_yuan"] is not None else None
    turnover.update({
        "previous": previous_turnover,
        "change_pct": _text((amount / previous_amount - 1) * 100)
        if amount is not None and previous_amount else None,
    })
    heights = _heights(pool_rows, "pools", quality)
    previous_heights = _heights(previous_pool, "previous_pools", quality)
    statuses, verified = _verify_limits(current, caps, quality)
    breadth["distribution"] = _breadth_distribution(
        changes, statuses, len(current) if current is not None else None, quality)
    supplier = _supplier_limits(pool_rows, heights, statuses, quality, seal_amount_unit_verified)
    supplier["up_turnover"] = _sealed_turnover(
        {s for s, row in pool_rows.items() if row["limit"] == "U"} if pool_rows is not None else None,
        current, turnover["amount_yuan"], quality)
    verified["up_turnover"] = _sealed_turnover(
        {s for s, state in statuses.items() if state.get("up") is True} if current is not None else None,
        current, turnover["amount_yuan"], quality,
        membership_unknown=verified["unverified_n"])
    first = {s for s, h in previous_heights.items() if h == 1} if previous_pool is not None else None
    consecutive = {s for s, h in previous_heights.items() if h >= 2} if previous_pool is not None else None
    broken = {s for s, row in previous_pool.items() if row["limit"] == "Z"} if previous_pool is not None else None
    pool_exits = ({s for s, row in prior_pool.items() if row["limit"] == "U" and
                   (s not in previous_pool or previous_pool[s]["limit"] != "U")}
                  if prior_pool is not None and previous_pool is not None else None)
    failed_classification, failed = _previous_failures(prior_pool, previous, previous_caps, previous_pool, quality)
    all_up = {s for s, row in previous_pool.items() if row["limit"] == "U"} if previous_pool is not None else None
    relay = {
        "previous_trade_day": previous_day, "prior_trade_day": prior_day,
        "scope": "首板/连板/炸板固定T-1供应商池；昨日断板另以T-2 U样本和T-1收盘限制价核验，今日缺行情保留分母",
        "unclassified_previous_limit_up_n": sum(row["limit"] == "U" for row in (previous_pool or {}).values())
        - len(previous_heights) if previous_pool is not None else None,
        "first_board": _cohort(first, current, statuses, previous_heights, quality, promotion=True),
        "consecutive": _cohort(consecutive, current, statuses, previous_heights, quality, promotion=True),
        "broken": _cohort(broken, current, statuses, previous_heights, quality),
        "failed": _cohort(failed, current, statuses, previous_heights, quality),
        "failed_classification": failed_classification,
        "previous_pool_exits": _cohort(pool_exits, current, statuses, previous_heights, quality),
    }
    relay["failed"].update({"scope": "昨日确认未续板样本的今日反馈；候选分类与未知数另列，含首板",
                            "candidate_N": failed_classification["N"],
                            "classification_unknown_n": failed_classification["unknown_n"],
                            "membership_complete": failed_classification["state"] in {"complete", "empty"}})
    relay["previous_pool_exits"]["scope"] = "T-2在U池、T-1未在U池；仅为披露池成员变化，未经断板确认"
    if failed_classification["unknown_n"]:
        relay["failed"].update({"state": "partial", "open_mean_pct": None, "close_mean_pct": None,
                                "promotion_pct": None, "down_limit_pct": None})
    if relay["unclassified_previous_limit_up_n"]:
        # An unknown T-1 height could belong to either group, so the observed
        # classified subset cannot establish either group's final rate.
        for key in ("first_board", "consecutive"):
            relay[key].update({"state": "partial", "promotion_pct": None, "down_limit_pct": None})
    yesterday_up = _cohort(all_up, current, statuses, previous_heights, quality)
    yesterday_up.update({"mean_close_pct": yesterday_up["close_mean_pct"],
                         "known_mean_close_pct": yesterday_up["known_close_mean_pct"]})
    relay["yesterday_limit_up"] = yesterday_up
    relay["by_height"] = {}
    for key, lower, upper in (("one_to_two", 1, 1), ("two_to_three", 2, 2),
                              ("three_to_four", 3, 3), ("four_plus", 4, None)):
        symbols = {s for s, h in previous_heights.items() if h >= lower and (upper is None or h <= upper)} if previous_pool is not None else None
        group = _cohort(symbols, current, statuses, previous_heights, quality, promotion=True)
        group["membership_complete"] = previous_pool is not None and not relay["unclassified_previous_limit_up_n"]
        if relay["unclassified_previous_limit_up_n"]:
            group.update(state="partial", promotion_pct=None)
        relay["by_height"][key] = group
    flags = quality.export()
    return {
        "state": "unavailable" if not current else "partial" if flags else "complete",
        "universe": {"scope": "daily接口实际返回的沪深A股；不按当前上市名单筛历史",
                     "exchanges": ["SH", "SZ"], "excluded_bse_n": excluded},
        "sample": {"trade_day": trade_day, "daily_loaded": current is not None,
                   "N": len(current) if current is not None else None,
                   "valid_n": valid_n, "missing_n": len(current) - valid_n if current is not None else None,
                   "full_exchange_coverage_verified": False},
        "breadth": breadth, "turnover": turnover,
        "limits": {"supplier_reported": supplier, "price_verified": verified,
                   "intraday": _intraday_limits(current, caps)},
        "relay": relay, "quality_flags": flags,
    }
