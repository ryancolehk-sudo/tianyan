"""Read-only projections over immutable daily-review datasets.

No collection or source configuration occurs here. Pending today's daily archive
may expose explicitly marked, already saved intraday observations. A complete
stored manifest is distinct from complete market scope.
"""

import hashlib
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.market.review_metrics import build_daily_metrics
from app.market.review_store import available_trade_days, load_dataset
from app.market.review_snapshot import mark_metrics, observation, snapshot_table

SHANGHAI = ZoneInfo("Asia/Shanghai")
INDICES = {
    "000001.SH": "上证指数", "399001.SZ": "深证成指", "399006.SZ": "创业板指",
    "000688.SH": "科创50", "932000.CSI": "中证2000",
    "000300.SH": "沪深300",
}
MINUTE_INDICES = tuple(INDICES)[:3]
DAILY_DATASETS = ("daily", "limits", "pools", "themes", "members", "theme_funds",
                  "lhb", "seats", "news")
MINUTE_DATASETS = ("tencent_minutes", "sina_minutes")
STANDARD_MINUTES = tuple(
    f"{hour:02d}:{minute:02d}" for hour in range(9, 16) for minute in range(60)
    if "09:31" <= f"{hour:02d}:{minute:02d}" <= "11:30"
    or "13:01" <= f"{hour:02d}:{minute:02d}" <= "15:00"
)
_STANDARD_SET = frozenset(STANDARD_MINUTES)


def _day(value):
    if isinstance(value, datetime):
        raise ValueError("trade_day must be a date")
    return value if isinstance(value, date) else date.fromisoformat(value)


def _json(value):
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _number_text(value)
    return value


def _number(value, *, positive=False):
    try:
        if value is None or isinstance(value, bool):
            return None
        number = Decimal(str(value))
        return number if number.is_finite() and (not positive or number > 0) else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _number_text(value):
    if value is None:
        return None
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _aggregate(states):
    states = list(states)
    if states and all(state == "complete" for state in states):
        return "complete"
    return "partial" if any(state in {"complete", "partial"} for state in states) else "unavailable"


def _current_trade_day(db, day, clock):
    """Reserve today's local read slot at 09:00 without changing archive dates."""
    from app.market.mairui import pool_day
    from app.market.protocol import SourceFailure
    local = clock.astimezone(SHANGHAI)
    if day != local.date() or local.time() < time(9):
        return None
    try:
        return day if pool_day(db, clock, preopen=True) == day else None
    except SourceFailure:
        return None


def _current_snapshot(db, day, clock):
    """A reserved date slot is not evidence of a post-open quote."""
    if clock.astimezone(SHANGHAI).time() < time(9, 30) or _current_trade_day(db, day, clock) is None:
        return None
    saved = snapshot_table(db, 'daily', day, clock=clock)
    if (saved is None or saved.get('state') != 'complete' or saved.get('basis') != 'intraday_snapshot'
            or saved.get('row_count', 0) <= 0):
        return None
    # snapshot_table already excludes future receipts and source times, checks
    # the source date and permissions. Also require a post-open source sample.
    stamp = datetime.fromisoformat(saved['source_time'])
    return saved if stamp.astimezone(SHANGHAI).time() >= time(9, 30) else None


def _context(db, trade_day, limit, as_of_time):
    if as_of_time is not None and (
        not isinstance(as_of_time, datetime) or as_of_time.utcoffset() is None
    ):
        raise ValueError("as_of_time must include a timezone")
    requested = _day(trade_day) if trade_day is not None else None
    clock = as_of_time or datetime.now(SHANGHAI)
    cutoff_day = clock.astimezone(SHANGHAI).date()
    end = min(requested, cutoff_day) if requested else cutoff_day
    # The calendar service supplies verified SH/SZ common trading days. Never
    # manufacture a missing date from weekdays, quotes or the current directory.
    days = sorted({_day(d) for d in available_trade_days(
        db, limit=limit, end_day=end, as_of_time=clock
    ) if _day(d) <= end})
    # Explicit historical dates and all observed cutoffs keep the original
    # closed-calendar path. From 09:00 a proved current open day has its own
    # initially empty slot; its availability never depends on a feed arriving.
    if (as_of_time is None and requested in {None, cutoff_day} and cutoff_day not in days
            and _current_trade_day(db, cutoff_day, clock) is not None):
        days = [*days, cutoff_day][-limit:]
    selected = requested or (days[-1] if days else None)
    known = selected is not None and selected in days
    return {
        "shared_date": selected.isoformat() if selected else None,
        "calendar_state": "complete" if known else "unavailable",
        "calendar_reason": None if known else "verified_trading_day_unavailable",
        "knowledge_mode": "observed" if as_of_time is not None else "review",
        "as_of_time": as_of_time.isoformat() if as_of_time else None,
    }, days, selected if known else None


class _Reader:
    def __init__(self, db, as_of_time):
        self.db, self.as_of_time, self.cache = db, as_of_time, {}

    def get(self, dataset, day, scope="all", *, include_rows=True):
        key = (dataset, day, scope, include_rows)
        if key not in self.cache:
            if day is None:
                value = {"state": "missing", "reason": "verified_trading_day_unavailable"}
            else:
                value = dict(load_dataset(self.db, dataset, day, scope=scope,
                                          as_of_time=self.as_of_time, include_rows=include_rows))
                if dataset == 'pools' and value.get('state') in {'missing', 'complete'} and not (
                    value.get('rows') if include_rows else value.get('row_count')):
                    from app.market.review_fallback import pool_table
                    saved = pool_table(self.db, day, self.as_of_time)
                    if saved is not None:
                        value = saved if include_rows else {**saved, 'rows': []}
                if value.get("state") == "missing" and include_rows:
                    saved = (_current_snapshot(self.db, day, datetime.now(SHANGHAI))
                             if dataset == 'daily' and self.as_of_time is None else
                             snapshot_table(self.db, dataset, day, scope, as_of_time=self.as_of_time))
                    if saved is not None:
                        value = saved
            value.update(dataset=dataset, trade_day=day.isoformat() if day else None, scope=scope)
            self.cache[key] = value
        return self.cache[key]


def _metadata(table):
    permitted = table.get("state") == "complete"
    return _json({
        "dataset": table["dataset"], "trade_day": table["trade_day"], "scope": table["scope"],
        "state": "partial" if table.get("basis") == "intraday_snapshot" else table.get("state", "missing"),
        "reason": table.get("reason"), "basis": table.get("basis"), "source_time": table.get("source_time"),
        "source_id": table.get("source_id"), "received_at": table.get("received_at"),
        "requested_day": table.get("requested_day"),
        "coverage_scope": table.get("coverage_scope"),
        "row_count": table.get("matched_row_count", table.get("row_count")) if permitted else None,
        "evidence_refs": table.get("evidence_refs", []) if permitted else [],
    })


def _rows(table):
    return table.get("rows", []) if table.get("state") == "complete" else None


def _dated_rows(table, day):
    rows = _rows(table)
    if rows is None or day is None:
        return None
    try:
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or _day(row.get("trade_date")) != day for row in rows
        ):
            raise ValueError
    except (ValueError, TypeError):
        table.update(state="broken_lineage", reason="wrong_or_missing_trade_date")
        return None
    return rows


def _references(tables):
    refs, seen = [], set()
    for table in tables:
        if table.get("state") != "complete":
            continue
        for ref in table.get("evidence_refs", []):
            normalized = _json(ref)
            key = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
            if key not in seen:
                refs.append(normalized)
                seen.add(key)
    return refs


def _row_reference(table, index):
    """Locate the immutable part row, including filtered historical windows.

    start_row/row_count address the returned, concatenated table. selected_rows
    maps those offsets back to zero-based positions in the original part.
    """
    if table.get("state") != "complete":
        return None
    for ref in table.get("evidence_refs", []):
        start, count = ref.get("start_row"), ref.get("row_count")
        if type(start) is not int or type(count) is not int or not start <= index < start + count:
            continue
        if ref.get("object_payload"):
            return _json({key: ref.get(key) for key in ("id", "content_hash", "source_id", "received_at")})
        offset = index - start
        selected = ref.get("selected_rows")
        if selected is not None:
            if not isinstance(selected, list) or offset >= len(selected):
                return None
            offset = selected[offset]
        if type(offset) is not int or offset < 0:
            return None
        return _json({key: ref.get(key) for key in ("id", "content_hash", "source_id", "received_at")} | {
            "source_row": offset, "row_index_base": 0,
        })
    return None


def _referenced(row, table, index):
    return {**_json(row), "row_reference": _row_reference(table, index)}


def _minute_series(table, day, code):
    points, slots, invalid, duplicates, other_dates = [], set(), 0, [], 0
    for index, row in enumerate(_rows(table) or []):
        if row.get("ts_code") != code:
            continue
        try:
            stamp = datetime.fromisoformat(row["trade_time"])
            # Public adapters normalize to +08:00; a naive source label is
            # interpreted only as its declared China-market local time.
            stamp = stamp.replace(tzinfo=SHANGHAI) if stamp.utcoffset() is None else stamp.astimezone(SHANGHAI)
            if stamp.date() != day:
                other_dates += 1
                continue
            slot = stamp.strftime("%H:%M")
            if stamp.second or stamp.microsecond or (slot not in _STANDARD_SET and slot != "09:30"):
                invalid += 1
                continue
            values = [_number(row.get(field), positive=True) for field in ("open", "high", "low", "close")]
            op, high, low, close = values
            if any(value is None for value in values) or not low <= min(op, close) <= max(op, close) <= high:
                invalid += 1
                continue
            if slot in slots:
                duplicates.append(slot)
            slots.add(slot)
            points.append({**_referenced(row, table, index), "trade_time": stamp.isoformat(), "minute": slot})
        except (ValueError, KeyError, TypeError):
            invalid += 1
    duplicate_slots = set(duplicates)
    # Conflicting or identical repeated slots are not silently collapsed.
    points = sorted((p for p in points if p["minute"] not in duplicate_slots), key=lambda p: p["trade_time"])
    unique = {p["minute"] for p in points}
    missing = sorted(_STANDARD_SET - unique)
    standard = [p for p in points if p["minute"] in _STANDARD_SET]
    segments, current, previous = [], [], None
    for point in standard:
        stamp = datetime.fromisoformat(point["trade_time"])
        if previous is not None and stamp - previous != timedelta(minutes=1):
            segments.append(current)
            current = []
        current.append(point)
        previous = stamp
    if current:
        segments.append(current)
    state = "complete" if standard and not missing and not invalid and not duplicates else "partial" if standard else "unavailable"
    return {
        "dataset": table["dataset"], "state": state, "storage_state": table.get("state"),
        "point_count": len(points), "standard_point_count": len(standard),
        "missing_standard_minutes": missing, "duplicate_minutes": sorted(duplicate_slots),
        "invalid_row_count": invalid, "ignored_other_date_count": other_dates,
        "points": standard, "extra_points": [p for p in points if p["minute"] == "09:30"],
        "segments": segments, "evidence_refs": _metadata(table)["evidence_refs"],
        "source_id": _json(table.get("source_id")),
    }


def _intraday(tables, day):
    result = []
    for code in MINUTE_INDICES:
        candidates = [_minute_series(table, day, code) for table in tables]
        # Prefer a complete Tencent series; otherwise select the single source
        # with most valid standard slots. Never combine providers point by point.
        chosen = max(candidates, key=lambda c: (c["state"] == "complete", c["standard_point_count"],
                                                c["dataset"] == "tencent_minutes"))
        result.append({
            "ts_code": code, "name": INDICES[code], **chosen,
            "selected_source": chosen["dataset"] if chosen["standard_point_count"] else None,
            "selection_basis": "whole_source_complete_then_most_standard_minutes",
            "source_mixed": False, "price_unit": "index_points",
            "quantity_scope": "源原始量额，指数口径未核实；不换算或合计为市场成交额",
            "lunch_break": {"start": "11:30", "end": "13:01", "connect": False},
            "alternatives": [{k: v for k, v in candidate.items()
                              if k not in {"points", "extra_points", "segments"}} for candidate in candidates],
        })
    return result


def coverage_view(db, trade_day=None, lookback=20, as_of_time=None):
    """Dataset/date coverage; complete manifests are not market-completeness claims."""
    if type(lookback) is not int or not 1 <= lookback <= 63:
        raise ValueError("lookback must be between 1 and 63")
    context, days, selected = _context(db, trade_day, lookback, as_of_time)
    reader, entries = _Reader(db, as_of_time), []
    for day in days[-lookback:] if selected else []:
        for dataset, scope in [(d, "all") for d in DAILY_DATASETS + MINUTE_DATASETS] + [
            ("indices", code) for code in INDICES
        ]:
            table = reader.get(dataset, day, scope, include_rows=False)
            meta = _metadata(table)
            if dataset in MINUTE_DATASETS and table.get("state") == "complete":
                detail = reader.get(dataset, day)
                series = [_minute_series(detail, day, code) for code in MINUTE_INDICES]
                meta["observations_state"] = _aggregate(item["state"] for item in series)
                meta["indices"] = [{"ts_code": code, "state": item["state"],
                                    "point_count": item["point_count"],
                                    "missing_standard_minutes": item["missing_standard_minutes"]}
                                   for code, item in zip(MINUTE_INDICES, series, strict=True)]
            entries.append(meta)
    states = [entry.get("observations_state", entry["state"]) for entry in entries]
    summary_state = _aggregate(states)
    if summary_state == "complete" and len(days) < lookback:
        summary_state = "partial"
    return {**context, "state": summary_state, "lookback": lookback,
            "calendar_window_complete": len(days) >= lookback,
            "trade_days": [d.isoformat() for d in days[-lookback:]] if selected else [],
            "returned_day_count": len(days[-lookback:]) if selected else 0,
            "entries": entries, "gaps": [e for e in entries if e.get("observations_state", e["state"]) != "complete"],
            "scope_note": "已保存且有权的范围；来源缺失或分页链不完整不补零"}


def _themes(tables, day, sector, daily, pools=None):
    catalog, members, funds = (_dated_rows(tables[key], day) for key in ("themes", "members", "theme_funds"))
    quotes = {r["ts_code"]: r for r in daily or [] if str(r.get("ts_code", "")).endswith((".SH", ".SZ"))}
    members_by_sector, funds_by_sector = {}, {}
    pool_by_code = {r.get("ts_code"): r for r in pools or []}
    # Keep each original table offset so grouping cannot change evidence rows.
    # Each large table is scanned once, independent of the number of sectors.
    for index, row in enumerate(members or []):
        members_by_sector.setdefault(row.get("ts_code"), []).append((index, row))
    for index, row in enumerate(funds or []):
        funds_by_sector.setdefault(row.get("ts_code"), []).append((index, row))
    items = []
    for index, row in enumerate(catalog or []):
        code = row.get("ts_code")
        if sector is not None and code != sector:
            continue
        disclosed = members_by_sector.get(code, [])
        symbols = {r.get("con_code") for _, r in disclosed
                   if str(r.get("con_code", "")).endswith((".SH", ".SZ"))}
        matched = [quotes[s] for s in sorted(symbols) if s in quotes]
        changes = [n for r in matched if (n := _number(r.get("pct_chg"))) is not None]
        amounts = [n * 1000 for r in matched if (n := _number(r.get("amount"))) is not None and n >= 0]
        # The overview computes the full sample but does not serialize every
        # member. Detail limits only affect rows returned for an explicit board.
        detail = disclosed[:1500] if sector is not None else []
        detail_symbols = {r.get("con_code") for _, r in detail
                          if str(r.get("con_code", "")).endswith((".SH", ".SZ"))}
        items.append({
            "ts_code": code, "name": row.get("name"), "provider_record": _referenced(row, tables["themes"], index),
            "provider_scope": "供应商原板块范围；不等同下方沪深已披露成员样本",
            "fund_records": [_referenced(r, tables["theme_funds"], n) for n, r in funds_by_sector.get(code, [])],
            "detail_available": bool(disclosed), "detail_requested": sector is not None,
            "disclosed_total": len(disclosed) if members is not None else None,
            "returned_member_count": len(detail), "returned": len(detail), "detail_limit": 1500,
            "truncated": sector is not None and len(disclosed) > len(detail),
            "member_records": [_referenced(r, tables["members"], n) for n, r in detail],
            "stock_records": [_referenced(r, tables["daily"], n) for n, r in enumerate(daily or [])
                              if r.get("ts_code") in detail_symbols] if detail_symbols else [],
            "member_sample": {
                "state": "partial" if symbols else "unavailable", "membership_complete_verified": False,
                "scope": "同日已披露沪深成员与同日日线交集；不声称全题材覆盖",
                "disclosed_member_n": len(symbols) if disclosed else None,
                "matched_daily_n": len(matched) if symbols and daily is not None else None,
                "valid_change_n": len(changes), "missing_quote_n": len(symbols) - len(matched) if symbols else None,
                "known_up": sum(n > 0 for n in changes) if changes else None,
                "known_down": sum(n < 0 for n in changes) if changes else None,
                "known_flat": sum(n == 0 for n in changes) if changes else None,
                "pool_up": sum(pool_by_code.get(s, {}).get("limit") == "U" for s in symbols) if symbols and pools is not None else None,
                "pool_broken": sum(pool_by_code.get(s, {}).get("limit") == "Z" for s in symbols) if symbols and pools is not None else None,
                "turnover_yuan": None, "known_turnover_yuan": _number_text(sum(amounts, Decimal(0))) if amounts else None,
            },
        })
    return {"state": _aggregate(tables[k].get("state") for k in ("themes", "members", "theme_funds")),
            "items": items, "selected_sector": sector, "full_theme_membership_verified": False,
            "coverage": [_metadata(tables[k]) for k in ("themes", "members", "theme_funds")]}


def daily_view(db, trade_day=None, sector=None, stock=None, as_of_time=None):
    context, days, selected = _context(db, trade_day, 3, as_of_time)
    reader = _Reader(db, as_of_time)
    previous = days[-2] if selected and len(days) >= 2 else None
    prior = days[-3] if selected and len(days) >= 3 else None
    tables = {key: reader.get(key, selected) for key in DAILY_DATASETS}
    if selected and sector and tables["members"].get("state") != "complete":
        # An individual historical board can be complete without an all-board batch.
        tables["members"] = reader.get("members", selected, sector)
    current = {key: _dated_rows(tables[key], selected) for key in ("daily", "limits", "pools")}
    previous_daily = reader.get("daily", previous)
    previous_limits = reader.get("limits", previous)
    previous_pools = reader.get("pools", previous)
    prior_pools = reader.get("pools", prior)
    metric_day = selected or (_day(context["shared_date"]) if context["shared_date"] else None)
    try:
        metrics = build_daily_metrics(
            trade_day=metric_day.isoformat(), daily=current["daily"], price_limits=current["limits"],
            pools=current["pools"], previous_daily=_dated_rows(previous_daily, previous),
            previous_price_limits=_dated_rows(previous_limits, previous),
            previous_pools=_dated_rows(previous_pools, previous), prior_pools=_dated_rows(prior_pools, prior),
            seal_amount_unit_verified=tables['pools'].get('seal_amount_unit_verified') is True,
        ) if metric_day else {"state": "unavailable", "quality_flags": [{"code": "verified_trading_day_unavailable"}]}
    except ValueError:
        metrics = {"state": "unavailable", "quality_flags": [{"code": "invalid_metric_input_table"}]}
    mark_metrics(metrics, tables["daily"])
    metrics["calendar"] = {"trade_day": context["shared_date"],
                           "previous_trade_day": previous.isoformat() if previous else None,
                           "prior_trade_day": prior.isoformat() if prior else None}
    indices = []
    for code, name in INDICES.items():
        table = reader.get("indices", selected, code)
        rows = _dated_rows(table, selected)
        matching = [_referenced(row, table, index) for index, row in enumerate(rows or []) if row.get("ts_code") == code]
        indices.append({"ts_code": code, "name": name, "state": "complete" if len(matching) == 1 else "unavailable",
                        "record": _json(matching[0]) if len(matching) == 1 else None,
                        "reason": None if len(matching) == 1 else "missing_or_ambiguous_index_day",
                        "coverage": _metadata(table), "price_unit": "index_points"})
    minute_tables = [reader.get(dataset, selected) for dataset in MINUTE_DATASETS]
    intraday = _intraday(minute_tables, selected)
    themes = _themes(tables, selected, sector, current["daily"], current["pools"])
    pool_rows = [(n, r) for n, r in enumerate(current["pools"] or [])
                 if str(r.get("ts_code", "")).endswith((".SH", ".SZ"))]
    pools = {"coverage": _metadata(tables["pools"]),
             "scope": "供应商同日披露的沪深涨跌停池原始记录",
             "source_total": len(current["pools"]) if current["pools"] is not None else None,
             "disclosed_total": len(pool_rows) if current["pools"] is not None else None,
             "returned": min(len(pool_rows), 500), "limit": 500, "truncated": len(pool_rows) > 500,
             "records": [_referenced(r, tables["pools"], n) for n, r in pool_rows[:500]]}
    all_tables = list(reader.cache.values())
    return {**context, "state": _aggregate([metrics["state"], themes["state"],
                                           *(item["state"] for item in indices + intraday)]),
            "metrics": metrics, "observation": observation(tables["daily"]),
            "indices": indices, "intraday": intraday, "themes": themes, "pools": pools,
            "selection": {"sector": sector, "stock": stock,
                          "stock_records": [_referenced(r, tables["daily"], n) for n, r in enumerate(current["daily"] or []) if r.get("ts_code") == stock],
                          "market_metrics_filtered": False},
            "news": {"coverage": _metadata(tables["news"]),
                     "scope_note": "当日最新最多20条已披露电报窗口，非当日全量。",
                     "records": [_referenced(r, tables["news"], n) for n, r in enumerate(_rows(tables["news"]) or [])]},
            "coverage": [_metadata(t) for t in all_tables], "evidence_refs": _references(all_tables)}


def seat_disclosures_view(db, trade_day, seat_name, lookback=5, min_amount_yuan=None, as_of_time=None):
    """Same disclosed name, buy-side top-five rows; never infer trader identity."""
    if type(lookback) is not int or not 1 <= lookback <= 20:
        raise ValueError("lookback must be between 1 and 20")
    minimum = _number(min_amount_yuan) if min_amount_yuan is not None else None
    if min_amount_yuan is not None and (minimum is None or minimum < 0):
        raise ValueError("minimum amount must be nonnegative")
    context, days, selected = _context(db, trade_day, lookback, as_of_time)
    reader, records = _Reader(db, as_of_time), []
    for day in reversed(days if selected else []):
        table = reader.get("seats", day)
        headers = reader.get("lhb", day)
        header_rows = _dated_rows(headers, day) or []
        for index, row in enumerate(_dated_rows(table, day) or []):
            code = row.get("ts_code", "")
            if row.get("exalter") != seat_name or row.get("side") != "0" or not code.endswith((".SH", ".SZ")):
                continue
            amount = _number(row.get("buy"))
            if amount is not None and amount < 0:
                amount = None
            if minimum is not None and (amount is None or amount < minimum):
                continue
            matches = [(n, r) for n, r in enumerate(header_rows)
                       if r.get("ts_code") == code and r.get("reason") == row.get("reason")]
            header = _referenced(matches[0][1], headers, matches[0][0]) if len(matches) == 1 else None
            records.append({"trade_day": day.isoformat(), "ts_code": code,
                            "name": header.get("name") if header else None,
                            "reason": row.get("reason"), "lhb_record_key": _event_key(day, code, row.get("reason")),
                            "amount_yuan": _number_text(amount), "amount_state": "available" if amount is not None and amount >= 0 else "unavailable",
                            "seat_record": _referenced(row, table, index), "listing_record": header})
    tables = list(reader.cache.values())
    seats = [table for table in tables if table["dataset"] == "seats"]
    state = _aggregate(table.get("state") for table in seats)
    if state == "complete" and len(days) < lookback:
        state = "partial"
    return {**context, "state": state, "seat_name": seat_name, "lookback": lookback,
            "min_amount_yuan": _number_text(minimum), "trade_days": [str(d) for d in days] if selected else [],
            "identity_basis": "exact_disclosed_name", "unique_identity_verified": False,
            "scope_note": "同名席位的买入侧前五披露；同名不代表同一交易者，也不是其全部买卖；不同原因/区间逐条保留，不累计金额。",
            "records": records[:500], "matched_record_count": len(records), "truncated": len(records) > 500,
            "coverage": [_metadata(table) for table in tables], "evidence_refs": _references(tables)}


def _event_key(day, code, reason):
    value = json.dumps([day.isoformat(), code, reason], ensure_ascii=False, separators=(",", ":"))
    return "lhb_" + hashlib.sha256(value.encode()).hexdigest()[:32]


def _top_five(rows, field, loaded, *, ambiguous=False):
    def order(item):
        try:
            return int(item.get("source_order")), item["row_order"]
        except (ValueError, TypeError):
            return item["row_order"], item["row_order"]
    ordered = sorted(rows, key=order)
    selected = ordered[:5]
    amounts = [_number(row.get(field)) for row in selected]
    valid = [n for n in amounts if n is not None and n >= 0]
    complete = len(selected) == 5 and len(valid) == 5 and len(ordered) == 5
    return {"state": "ambiguous_disclosure" if ambiguous else "complete" if complete else "partial" if selected else "unavailable",
            "dataset_loaded": loaded, "records": selected, "disclosed_row_count": len(ordered),
            "used_row_count": len(selected), "valid_amount_count": len(valid),
            "amount_yuan": _number_text(sum(valid, Decimal(0))) if complete and not ambiguous else None,
            "known_amount_yuan": _number_text(sum(valid, Decimal(0))) if valid and not ambiguous else None,
            "extra_rows": ordered[5:], "amount_field": field}


def lhb_view(db, trade_day, stock=None, record_key=None, as_of_time=None):
    context, days, selected = _context(db, trade_day, 63, as_of_time)
    reader = _Reader(db, as_of_time)
    listings, seats = reader.get("lhb", selected), reader.get("seats", selected)
    rows, seat_rows = _dated_rows(listings, selected), _dated_rows(seats, selected)
    grouped = {}
    for index, row in enumerate(rows or []):
        code, reason = row.get("ts_code"), row.get("reason")
        if not str(code).endswith((".SH", ".SZ")):
            continue
        if stock is not None and code != stock:
            continue
        key = _event_key(selected, code, reason)
        grouped.setdefault(key, {"record_key": key, "trade_day": selected.isoformat(), "ts_code": code,
                                 "reason": reason, "source_rows": []})["source_rows"].append(
            {**_referenced(row, listings, index), "row_order": index + 1})
    events, matched_seat_rows = [], set()
    for event in grouped.values():
        related = []
        for index, row in enumerate(seat_rows or []):
            if event["reason"] and row.get("ts_code") == event["ts_code"] and row.get("reason") == event["reason"]:
                related.append({**_referenced(row, seats, index), "row_order": index + 1})
                matched_seat_rows.add(index)
        single = event["source_rows"][0] if len(event["source_rows"]) == 1 else None
        event.update({
            "source_id": _json(listings.get("source_id")), "evidence_refs": _metadata(listings)["evidence_refs"],
            "headline": {field: _number_text(_number(single.get(field))) if single else None
                         for field in ("l_buy", "l_sell", "net_amount")},
            "headline_basis": "top_list原披露金额；不由两侧席位重新计算",
            "headline_state": "available" if single else "ambiguous_duplicate_event",
            "period": {"reason_text": event["reason"], "start_day": None, "end_day": None,
                       "basis": "仅保留来源原因，不推断单日或多日统计起止"},
            "buy_top5": _top_five([r for r in related if r.get("side") == "0"], "buy", seat_rows is not None,
                                 ambiguous=single is None),
            "sell_top5": _top_five([r for r in related if r.get("side") == "1"], "sell", seat_rows is not None,
                                  ambiguous=single is None),
            "unknown_side_records": [r for r in related if r.get("side") not in {"0", "1"}],
            "seat_evidence_refs": _metadata(seats)["evidence_refs"],
            "seat_identity_note": "按原行及买卖侧保留；机构专用等同名席位不是唯一交易者",
        })
        events.append(event)
    history_dates, history_records, history_coverage = [], [], []
    if stock:
        for day in days:
            table = reader.get("lhb", day)
            historical = _dated_rows(table, day)
            groups = {}
            for index, row in enumerate(historical or []):
                if row.get("ts_code") == stock:
                    groups.setdefault(row.get("reason"), []).append(_referenced(row, table, index))
            history_coverage.append(_metadata(table))
            if groups:
                history_dates.append(day.isoformat())
            for reason, original in groups.items():
                history_records.append({"trade_day": day.isoformat(), "ts_code": stock, "reason": reason,
                                        "record_key": _event_key(day, stock, reason),
                                        "net_amount": _number_text(_number(original[0].get("net_amount"))) if len(original) == 1 else None,
                                        "state": "available" if len(original) == 1 else "ambiguous_duplicate_event",
                                        "source_rows": original})
    selected_record = next((event for event in events if event["record_key"] == record_key), None)
    if record_key is None and len(events) == 1:
        selected_record = events[0]
    return {**context, "state": _aggregate([listings.get("state"), seats.get("state")]),
            "market_scope": "SH_SZ_A", "source_listing_count": len(rows) if rows is not None else None,
            "returned_listing_count": len(events),
            "stock": stock, "records": events, "selected_record": selected_record,
            "selection_state": "selected" if selected_record else "record_not_found" if record_key else "choose_record",
            "history_dates": history_dates, "history_scope": "本地有权已披露记录；不累计不同原因或区间金额",
            "history_records": list(reversed(history_records))[:200],
            "history_truncated": len(history_records) > 200, "history_coverage": history_coverage,
            "unmatched_seat_records": [{**_referenced(row, seats, index), "row_order": index + 1}
                                       for index, row in enumerate(seat_rows or [])
                                       if index not in matched_seat_rows and str(row.get("ts_code", "")).endswith((".SH", ".SZ"))
                                       and (stock is None or row.get("ts_code") == stock)],
            "coverage": [_metadata(listings), _metadata(seats)],
            "evidence_refs": _references([listings, seats])}
