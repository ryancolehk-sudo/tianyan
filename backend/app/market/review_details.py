"""Small read-only projections for stock selection and historical themes.

Only original same-day datasets or explicitly marked saved current-day quotes
are used. No network acquisition, inferred prices or automatic research stages
are involved.
"""

import re
from app.market.review_snapshot import observation

from app.market.review_read import (
    _aggregate,
    _context,
    _dated_rows,
    _metadata,
    _Reader,
    _referenced,
    _references,
)


def _codes(values, pattern, maximum):
    if not isinstance(values, (list, tuple)):
        raise ValueError("codes must be a list")
    unique = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError("invalid review code")
        if value not in seen:
            seen.add(value)
            unique.append(value)
            if len(unique) > maximum:
                raise ValueError(f"at most {maximum} distinct codes are allowed")
    return unique


def _select(table, day, codes):
    """Scan once; retain at most one source offset and count per requested code.

Duplicate identities remain ambiguous even if their payloads are identical.
Neither arbitrary last-row selection nor unbounded duplicate detail is allowed.
    """
    rows = _dated_rows(table, day)
    counts = dict.fromkeys(codes, 0)
    first = {}
    for index, row in enumerate(rows or []):
        code = row.get("ts_code")
        if code in counts:
            counts[code] += 1
            first.setdefault(code, (index, row))
    selected = {}
    for code in codes:
        count = counts[code] if rows is not None else None
        selected[code] = {
            "state": table.get("state", "missing") if rows is None else
            "complete" if count == 1 else "not_disclosed" if count == 0 else "ambiguous_duplicate",
            "match_count": count,
            "record": _referenced(first[code][1], table, first[code][0]) if count == 1 else None,
        }
    return selected


def _record_state(*parts):
    if all(part["state"] == "complete" for part in parts):
        return "complete"
    return "partial" if any(part["record"] is not None for part in parts) else "unavailable"


def stocks_view(db, trade_day, codes, as_of_time=None):
    """Original daily quotes and supplier-pool rows for at most 30 SH/SZ codes."""
    codes = _codes(codes, r"\d{6}\.(?:SH|SZ)", 30)
    if trade_day is None:
        raise ValueError("trade_day is required")
    context, _, selected = _context(db, trade_day, 1, as_of_time)
    result = {**context, "state": "unavailable", "codes": codes, "items": [],
              "coverage": [], "evidence_refs": [],
              "scope_note": "仅所选原日沪深日线与供应商池披露；无行情不补价，名称仅取原日池记录"}
    if not codes:
        result["state"] = "empty" if selected else "unavailable"
        return result
    reader = _Reader(db, as_of_time)
    daily, pools = reader.get("daily", selected), reader.get("pools", selected)
    quotes, pool_rows = _select(daily, selected, codes), _select(pools, selected, codes)
    for code in codes:
        quote, pool = quotes[code], pool_rows[code]
        state = _record_state(quote, pool)
        # A quoted stock need not appear in a successfully loaded limit pool.
        if quote["state"] == "complete" and pool["state"] == "not_disclosed":
            state = "complete"
        result["items"].append({
            "ts_code": code, "name": (pool["record"].get("name") or None) if pool["record"] else None,
            "quote": quote["record"], "pool_record": pool["record"], "state": state,
            "quote_state": quote["state"], "pool_state": pool["state"],
            "quote_match_count": quote["match_count"], "pool_match_count": pool["match_count"],
        })
    result.update(state=_aggregate(item["state"] for item in result["items"]),
                  observation=observation(daily),
                  coverage=[_metadata(daily), _metadata(pools)], evidence_refs=_references([daily, pools]))
    return result


def theme_history_view(db, trade_day, sector_codes, lookback=5, as_of_time=None):
    """Original catalog/fund rows for up to 10 themes over five verified days."""
    sector_codes = _codes(sector_codes, r"BK\d{4,8}\.DC", 10)
    if type(lookback) is not int or lookback != 5:
        raise ValueError("lookback must be 5")
    if trade_day is None:
        raise ValueError("trade_day is required")
    context, days, selected = _context(db, trade_day, lookback, as_of_time)
    window = days[-lookback:] if selected else []
    items = [{"ts_code": code, "state": "unavailable", "records": []} for code in sector_codes]
    result = {**context, "state": "unavailable", "sector_codes": sector_codes, "lookback": lookback,
              "trade_days": [day.isoformat() for day in window], "returned_day_count": len(window),
              "calendar_window_complete": len(window) == lookback, "items": items,
              "coverage": [], "evidence_refs": [],
              "scope_note": "原日供应商题材目录与盘后资金；不以当前成员回填历史；研究阶段由人工笔记提供"}
    if not selected or not sector_codes:
        result["state"] = "empty" if selected and not sector_codes else "unavailable"
        return result
    reader = _Reader(db, as_of_time)
    tables = []
    for day in window:
        catalog, funds = reader.get("themes", day), reader.get("theme_funds", day)
        catalog_rows, fund_rows = _select(catalog, day, sector_codes), _select(funds, day, sector_codes)
        coverage, references = [_metadata(catalog), _metadata(funds)], _references([catalog, funds])
        for item in items:
            code = item["ts_code"]
            original, flow = catalog_rows[code], fund_rows[code]
            item["records"].append({
                "trade_day": day.isoformat(), "state": _record_state(original, flow),
                "catalog_record": original["record"], "fund_record": flow["record"],
                "catalog_state": original["state"], "fund_state": flow["state"],
                "catalog_match_count": original["match_count"], "fund_match_count": flow["match_count"],
                "coverage": coverage, "evidence_refs": references,
            })
        tables.extend((catalog, funds))
    for item in items:
        item["state"] = _aggregate(record["state"] for record in item["records"])
    result.update(state=_aggregate(item["state"] for item in items),
                  coverage=[_metadata(table) for table in tables], evidence_refs=_references(tables))
    if result["state"] == "complete" and not result["calendar_window_complete"]:
        result["state"] = "partial"
    return result
