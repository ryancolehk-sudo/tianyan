"""Bounded, read-only review history; no acquisition or cross-request cache.

The calendar and every dataset go through the existing rights/as-of readers.
Only compact results and immutable references survive the rolling raw window.
"""

from app.market.review_metrics import build_daily_metrics
from app.market.review_snapshot import mark_metrics, observation
from app.market.review_read import (
    INDICES,
    _aggregate,
    _context,
    _dated_rows,
    _metadata,
    _number,
    _Reader,
    _referenced,
    _references,
)

_BASE_DATASETS = ("daily", "limits", "pools")
_WARMUP_DAYS = 3


def _compact_metrics(metrics):
    # Do not copy or serialize the large sample arrays before discarding them.
    compact = dict(metrics)
    compact["relay"] = {
        key: {field: value for field, value in group.items() if field != "samples"}
        if isinstance(group, dict) else group
        for key, group in metrics.get("relay", {}).items()
    }
    compact["relay"]["by_height"] = {
        key: {field: value for field, value in group.items() if field != "samples"}
        for key, group in metrics.get("relay", {}).get("by_height", {}).items()
    }
    return compact


def _indices(reader, day):
    items, tables = [], []
    for code, name in INDICES.items():
        table = reader.get("indices", day, code)
        rows = _dated_rows(table, day)
        matches = [(n, row) for n, row in enumerate(rows or []) if row.get("ts_code") == code]
        items.append({"ts_code": code, "name": name,
                      "state": "complete" if len(matches) == 1 else "unavailable",
                      "record": _referenced(matches[0][1], table, matches[0][0]) if len(matches) == 1 else None,
                      "reason": None if len(matches) == 1 else "missing_or_ambiguous_index_day",
                      "price_unit": "index_points", "coverage": _metadata(table)})
        tables.append(table)
    return items, tables


def _themes(reader, day):
    table = reader.get("themes", day)
    rows = _dated_rows(table, day)
    ranked = [(value, index, row) for index, row in enumerate(rows or [])
              if (value := _number(row.get("pct_change"))) is not None]
    # Original order breaks equal-return ties and preserves original row refs.
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return {"state": "unavailable" if rows is None else
            "complete" if len(ranked) == len(rows) else "partial" if ranked else "unavailable",
            "scope": "同日供应商题材目录范围；原涨幅排序，非沪深成员重算或全市场排名",
            "ranking_field": "pct_change", "limit": 3,
            "disclosed_total": len(rows) if rows is not None else None,
            "valid_change_n": len(ranked) if rows is not None else None,
            "unknown_change_n": len(rows) - len(ranked) if rows is not None else None,
            "records": [_referenced(row, table, index) for _, index, row in ranked[:3]],
            "coverage": _metadata(table)}, table


def series_view(db, trade_day=None, lookback=20, as_of_time=None):
    """Return up to 20/60 proved sessions, reserving today's slot at 09:00.

Three additional calendar sessions are requested for warmup. Arithmetic only
loads its actual T-1/T-2 dependencies, never a fabricated previous weekday.
Default review mode may use later-acquired historical evidence; observed mode
retains the caller's receipt cutoff for every read. A current-day read overlay
does not change the closed-session calendar used by archive jobs.
    """
    if type(lookback) is not int or lookback not in {20, 60}:
        raise ValueError("lookback must be 20 or 60")
    context, days, selected = _context(db, trade_day, lookback + _WARMUP_DAYS, as_of_time)
    window = days[-lookback:] if selected else []
    warmup = days[:-len(window)] if window else []
    result = {**context, "state": "unavailable", "lookback": lookback,
              "calendar_window_complete": len(window) == lookback,
              "returned_day_count": len(window), "trade_days": [day.isoformat() for day in window],
              "warmup_trade_days": [day.isoformat() for day in warmup],
              "warmup_calendar_complete": len(warmup) == _WARMUP_DAYS,
              "requested_warmup_days": _WARMUP_DAYS, "records": [],
              "scope_note": "已核沪深共同交易日；开市日09:00预留今日行，资料未到时留空；仅已保存且有权的数据，历史补采不代表当时已知",
              "relay_samples_included": False}
    if not selected:
        return result
    reader = _Reader(db, as_of_time)
    for index in range(len(days) - len(window), len(days)):
        day = days[index]
        previous = days[index - 1] if index >= 1 else None
        prior = days[index - 2] if index >= 2 else None
        current = {key: reader.get(key, day) for key in _BASE_DATASETS}
        yesterday = {key: reader.get(key, previous) for key in _BASE_DATASETS}
        prior_pool = reader.get("pools", prior)
        current_rows = {key: _dated_rows(table, day) for key, table in current.items()}
        previous_rows = {key: _dated_rows(table, previous) for key, table in yesterday.items()}
        prior_rows = _dated_rows(prior_pool, prior)
        try:
            metrics = build_daily_metrics(
                trade_day=day.isoformat(), daily=current_rows["daily"], price_limits=current_rows["limits"],
                pools=current_rows["pools"], previous_daily=previous_rows["daily"],
                previous_price_limits=previous_rows["limits"], previous_pools=previous_rows["pools"],
                prior_pools=prior_rows,
                seal_amount_unit_verified=current['pools'].get('seal_amount_unit_verified') is True,
            )
        except ValueError:
            metrics = {"state": "unavailable", "quality_flags": [{"code": "invalid_metric_input_table"}]}
        mark_metrics(metrics, current["daily"])
        metrics = _compact_metrics(metrics)
        metrics["calendar"] = {"trade_day": day.isoformat(),
                               "previous_trade_day": previous.isoformat() if previous else None,
                               "prior_trade_day": prior.isoformat() if prior else None}
        indices, index_tables = _indices(reader, day)
        themes, theme_table = _themes(reader, day)
        tables = [*current.values(), *yesterday.values(), prior_pool, *index_tables, theme_table]
        result["records"].append({
            "trade_day": day.isoformat(),
            "state": _aggregate([metrics["state"], themes["state"], *(item["state"] for item in indices)]),
            "metrics": metrics, "observation": observation(current["daily"]), "indices": indices, "themes": themes,
            "coverage": [_metadata(table) for table in tables], "evidence_refs": _references(tables),
        })
        # The next date needs today's three inputs and yesterday's pool only.
        # Never retain full 60-day raw tables or reuse results across requests.
        reader.cache = {key: table for key, table in reader.cache.items()
                        if key[0] in _BASE_DATASETS and key[1] == day
                        or key[0] == "pools" and key[1] == previous}
    result["state"] = _aggregate(record["state"] for record in result["records"])
    if any(record["observation"] is not None for record in result["records"]):
        result["scope_note"] = (
            "已核沪深共同交易日；今日行是已保存的盘中样本，不代表收盘；"
            "历史补采不代表当时已知，缺失不补零"
        )
    if result["state"] == "complete" and not (
        result["calendar_window_complete"] and result["warmup_calendar_complete"]
    ):
        result["state"] = "partial"
    return result
