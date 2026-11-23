"""Deterministic research arithmetic over a frozen, referenced cache bundle.

No I/O, model inference, trade recommendations, synthetic prices or composite
scores live here. A missing observation is never replaced with zero. Decimal
results are strings, and every window retains its actual source rows.
"""

import re
from decimal import Decimal, InvalidOperation, localcontext

from app.market.review_metrics import build_daily_metrics

RULE_VERSION = "tomorrow-research.v1"
WINDOWS = (1, 5, 20, 60)
STOCK_PATTERN = re.compile(r"^(?:6\d{5}\.SH|[03]\d{5}\.SZ)$")
FORMULAS = {
    "market.up_share_pct": "count(valid pct_chg>0)/count(all valid pct_chg)*100; missing constituents withhold final rate",
    "market.turnover_yuan": "sum(daily.amount*1000); any missing amount withholds complete total",
    "market.seal_rate_pct": "U/(U+Z)*100; vendor non-ST SH/SZ scope",
    "market.yesterday_limit_up": "freeze prior-session vendor U symbols; retain missing today quotes in N",
    "sector.returns_pct": "(product(1+pct_change/100)-1)*100 over consecutive verified sessions",
    "sector.member_up_share_pct": "count(member pct_chg>0)/stored same-day member count*100; missing quotes withhold rate",
    "stock.returns_pct": "(current_close/base_close-1)*100; factor-adjusted only with complete factors; 1d supplier pct_chg",
    "stock.relative_strength": "stock window return minus same-window sector return, in percentage points",
    "stock.volume_ratio": "today.vol/mean(prior5.vol); all five prior verified sessions required",
    "stock.support_resistance": "min(prior20.low), max(prior20.high); current session excluded, unadjusted observation reference only",
}


def number(value):
    if value is None or isinstance(value, bool) or value in ("", "--", "—"):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def text(value):
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        value = value.quantize(Decimal("0.00000001")) if value else Decimal(0)
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def percentage(numerator, denominator):
    return text(Decimal(numerator) * 100 / Decimal(denominator)) if denominator else None


def references(rows, fields=()):
    result, seen = [], set()
    for row in rows:
        ref = row.get("row_reference")
        if not ref:
            continue
        item = {**ref, "fields": list(fields)}
        key = (item.get("id"), item.get("source_row"), tuple(fields))
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def fact(row, field, unit=None):
    return {"value": row.get(field), "unit": unit,
            "evidence": references([row], [field])}


def index_rows(rows):
    """Reject duplicate identities instead of silently selecting a winner."""
    result, conflicts = {}, set()
    for row in rows or []:
        code = row.get("ts_code")
        if not isinstance(code, str):
            continue
        if code in result:
            conflicts.add(code)
        else:
            result[code] = row
    return {key: row for key, row in result.items() if key not in conflicts}, sorted(conflicts)


def price_returns(rows_by_day, days, *, factors=None, percent_field="pct_chg"):
    """Use consecutive verified trading-day slots, never the last N quotes.

    One-day return retains the supplier's disclosed percentage. Longer stock
    windows use close*factor when every required factor exists, otherwise expose
    an explicitly unadjusted price change, not an investment total return.
    """
    current = rows_by_day.get(days[-1], {}) if days else {}
    results = {}
    for window in WINDOWS:
        chosen = days[-window:] if window == 1 else days[-window-1:]
        required = window if window == 1 else window + 1
        rows = [rows_by_day.get(day) for day in chosen]
        valid = len(chosen) == required and all(row is not None for row in rows)
        value, adjustment = None, "supplier_disclosed" if window == 1 else "unadjusted"
        evidence = references([row for row in rows if row], [percent_field] if window == 1 else ["close"])
        if valid and window == 1:
            value = number(current.get(percent_field))
        elif valid:
            closes = [number(row.get("close")) for row in rows]
            if all(value is not None and value > 0 for value in closes):
                first, last = closes[0], closes[-1]
                factor_rows = [factors.get(day) or factors.get(str(day)) for day in chosen] if factors is not None else []
                factor_values = [number(row.get("adj_factor")) if row else None for row in factor_rows]
                if len(factor_values) == required and all(value is not None and value > 0 for value in factor_values):
                    first, last = first * factor_values[0], last * factor_values[-1]
                    adjustment = "factor_adjusted_price"
                    evidence += references(factor_rows, ["adj_factor"])
                value = (last / first - 1) * 100
        results[str(window)] = {
            "value": text(value), "unit": "%", "window_sessions": window,
            "observed_sessions": sum(row is not None for row in rows),
            "required_sessions": required, "state": "complete" if value is not None else "insufficient",
            "adjustment": adjustment, "formula": "supplier_pct" if window == 1 else "(last_close*last_factor/(base_close*base_factor)-1)*100" if adjustment == "factor_adjusted_price" else "(last_close/base_close-1)*100",
            "start_day": str(chosen[0]) if chosen else None,
            "end_day": str(chosen[-1]) if chosen else None,
            "evidence": evidence,
            "limitation": None if window == 1 or adjustment == "factor_adjusted_price" else "未复权价格变化，除权除息可能影响比较；不是含分红收益率。",
        }
    return results


def compounded_theme_returns(rows_by_day, days):
    """DC theme tables may have no close; compound disclosed daily changes."""
    results = {}
    for window in WINDOWS:
        chosen = days[-window:]
        rows = [rows_by_day.get(day) for day in chosen]
        values = [number(row.get("pct_change")) if row else None for row in rows]
        valid = len(chosen) == window and all(value is not None and value >= -100 for value in values)
        total = Decimal(1)
        if valid:
            for value in values:
                total *= 1 + value / 100
        results[str(window)] = {"value": text((total - 1) * 100) if valid else None,
                               "unit": "%", "window_sessions": window,
                               "observed_sessions": sum(value is not None for value in values),
                               "required_sessions": window, "state": "complete" if valid else "insufficient",
                               "formula": "(product(1+pct_change/100)-1)*100",
                               "evidence": references([row for row in rows if row], ["pct_change"])}
    return results


def support_resistance(rows_by_day, days):
    """Prior 20-session extrema are references, never approved order prices."""
    chosen = days[-21:-1]
    rows = [rows_by_day.get(day) for day in chosen]
    lows = [number(row.get("low")) if row else None for row in rows]
    highs = [number(row.get("high")) if row else None for row in rows]
    valid = len(chosen) == 20 and all(value is not None and value > 0 for value in lows + highs)
    return {"state": "complete" if valid else "insufficient", "window_sessions": 20,
            "observed_sessions": sum(row is not None for row in rows),
            "support_candidate": text(min(lows)) if valid else None,
            "resistance_candidate": text(max(highs)) if valid else None,
            "unit": "元", "adjustment": "unadjusted",
            "formula": "min(prior20.low), max(prior20.high); current session excluded",
            "usage": "历史价格观察参考，未批准为入场、止损或止盈价；须检查除权除息。",
            "evidence": references([row for row in rows if row], ["low", "high"])}


def relative_strength(stock_returns, sector_returns):
    results = {}
    for window in map(str, WINDOWS):
        stock = stock_returns.get(window, {})
        sector = sector_returns.get(window, {})
        left, right = number(stock.get("value")), number(sector.get("value"))
        value = left - right if left is not None and right is not None else None
        results[window] = {"value": text(value), "unit": "百分点", "state": "complete" if value is not None else "insufficient",
                           "formula": "stock_return_pct-sector_return_pct", "window_sessions": int(window),
                           # The canonical stock/sector windows already retain
                           # exact row/field evidence. Repeating those complete
                           # dictionaries per membership makes a dense concept
                           # graph quadratic after JSON normalization.
                           "input_windows": {"stock": f"calculations.returns_pct.{window}", "sector": f"calculations.returns_pct.{window}"},
                           "limitation_source": "stock.input_window.limitation"}
    return results


def market_metrics(day, tables, days):
    previous = days[-2] if len(days) >= 2 else None
    prior = days[-3] if len(days) >= 3 else None
    def rows(dataset, selected):
        table = tables.get((dataset, selected), {})
        return table.get("rows") if table.get("state") == "complete" else None
    try:
        result = build_daily_metrics(
            trade_day=str(day), daily=rows("daily", day), price_limits=rows("limits", day),
            pools=rows("pools", day), previous_daily=rows("daily", previous),
            previous_price_limits=rows("limits", previous), previous_pools=rows("pools", previous),
            prior_pools=rows("pools", prior))
    except (ValueError, TypeError, KeyError) as exc:
        return {"state": "invalid", "reason": "core_tables_invalid", "detail": str(exc),
                "breadth": {}, "limits": {}, "relay": {}, "quality_flags": []}
    breadth = result["breadth"]
    breadth["up_share_pct"] = percentage(breadth.get("up"), breadth.get("valid_n")) if breadth.get("up") is not None and not breadth.get("missing_n") else None
    result["rule_version"] = RULE_VERSION
    result["evidence"] = references([
        row for (dataset, selected), table in tables.items()
        if dataset in {"daily", "limits", "pools"} and selected in {day, previous, prior}
        for row in table.get("rows", [])
    ], ["ts_code", "trade_date", "close", "open", "pre_close", "pct_chg", "amount", "limit", "limit_times", "up_limit", "down_limit"])
    return result


def stock_record(code, name, history, days, *, cap=None, pool=None, sectors=(), reasons=(), references_by_dataset=None):
    current = history.get(days[-1], {}) if days else {}
    references_by_dataset = references_by_dataset or {}
    factors = {row["business_day"]: row for row in references_by_dataset.get("tushare.adj_factor", []) if row.get("business_day")}
    facts = {field: fact(current, field, unit) for field, unit in {
        "open": "元", "close": "元", "high": "元", "low": "元", "pre_close": "元",
        "pct_chg": "%", "amount": "千元", "vol": "手"}.items()}
    if cap:
        facts.update({field: fact(cap, field, "元") for field in ("up_limit", "down_limit")})
    if pool:
        facts["pool"] = {"value": {k: v for k, v in pool.items() if k != "row_reference"},
                         "evidence": references([pool], [k for k in pool if k != "row_reference"])}
    close, limit_up, limit_down = number(current.get("close")), number((cap or {}).get("up_limit")), number((cap or {}).get("down_limit"))
    comparable = close is not None and limit_down is not None and limit_up is not None and 0 < limit_down < limit_up and limit_down <= close <= limit_up
    prior_rows = [history.get(day) for day in days[-6:-1]]
    previous_volumes = [number(row.get("vol")) if row else None for row in prior_rows]
    current_volume = number(current.get("vol"))
    volume_ratio = current_volume / (sum(previous_volumes) / 5) if len(prior_rows) == 5 and all(v is not None and v > 0 for v in previous_volumes) and current_volume is not None and current_volume >= 0 else None
    all_rows = [current, *( [cap] if cap else []), *( [pool] if pool else [])]
    limitations = []
    if not current:
        limitations.append("当日行情缺失，不能判断价格及强弱。")
    if not comparable:
        limitations.append("有效当日限制价或收盘价不足，涨跌停状态不可判断。")
    if not references_by_dataset.get("tushare.adj_factor"):
        limitations.append("缺少完整复权因子，多日价格变化只作未复权观察。")
    if not any(key in references_by_dataset for key in ("tushare.income", "tushare.fina_indicator")):
        limitations.append("缺少截止前财务资料，不能生成业绩或估值结论。")
    def source_values(rows, limit):
        ordered = sorted(rows, key=lambda row: str(row.get("end_date") or row.get("statDate") or row.get("business_day") or ""), reverse=True)
        return [{"values": {key: value for key, value in row.items() if key not in {"row_reference", "units", "business_day", "announced_day"}},
                 "units": row.get("units", {}), "evidence_refs": [row["row_reference"]["id"]] if row.get("row_reference") else []}
                for row in ordered[:limit]]
    financials = {key: source_values(rows, 4) for key, rows in references_by_dataset.items()
                  if key in {"tushare.income", "tushare.balancesheet", "tushare.cashflow", "tushare.fina_indicator", "tushare.forecast", "tushare.express"} or key.startswith("financial.")}
    return {"code": code, "ts_code": code, "name": name or code, "facts": facts,
            "calculations": {"returns_pct": price_returns(history, days, factors=factors),
                             "support_resistance": support_resistance(history, days),
                             "volume_to_prior5_mean": {"value": text(volume_ratio), "formula": "today.vol/mean(prior5.vol)",
                                                        "evidence": references([current, *[r for r in prior_rows if r]], ["vol"])},
                             "price_verified_limit_up": close == limit_up if comparable else None,
                             "price_verified_limit_down": close == limit_down if comparable else None},
            "sectors": list(sectors), "candidate_reasons": list(dict.fromkeys(reasons)),
            "fundamentals": {"valuation": source_values(references_by_dataset.get("tushare.daily_basic", []), 1),
                             "financials": financials, "business": source_values(references_by_dataset.get("tushare.fina_mainbz", []), 20),
                             "state": "available_partial_scope" if financials else "insufficient"},
            "fund_flows": {key: source_values(references_by_dataset[key], 20) for key in ("tushare.moneyflow", "tushare.moneyflow_dc") if key in references_by_dataset},
            "cached_references": references_by_dataset,
            "evidence": references(all_rows), "limitations": limitations,
            "judgments": [], "plans": [], "rule_version": RULE_VERSION}


def sector_record(code, history, days, members, stock_rows, pool_rows, funds=None, *, members_loaded=True):
    current = history.get(days[-1], {}) if days else {}
    unique = {row.get("con_code"): row for row in members if STOCK_PATTERN.fullmatch(str(row.get("con_code", "")))}
    observations = [stock_rows[symbol] for symbol in unique if symbol in stock_rows]
    changes = [number(row.get("pct_chg")) for row in observations]
    valid_changes = [value for value in changes if value is not None]
    up_n = sum(value > 0 for value in valid_changes)
    sealed = {row["ts_code"] for row in pool_rows or [] if row.get("limit") == "U"} if pool_rows is not None else None
    count = len(unique)
    return {"code": code, "ts_code": code, "name": current.get("name") or code,
            "facts": {key: fact(current, key, unit) for key, unit in {
                "pct_change": "%", "turnover_rate": "%", "up_num": "只", "down_num": "只"}.items()},
            "calculations": {"returns_pct": compounded_theme_returns(history, days),
                             "members": count if members_loaded else None, "observed_member_quotes": len(observations),
                             "valid_member_changes": len(valid_changes),
                             "member_up_share_pct": percentage(up_n, count) if count and len(valid_changes) == count else None,
                             "member_limit_up_n": sum(symbol in sealed for symbol in unique) if sealed is not None and members_loaded else None,
                             "member_limit_up_share_pct": percentage(sum(symbol in sealed for symbol in unique), count) if sealed is not None and members_loaded else None,
                             "membership_state": "available_partial_scope" if members_loaded else "not_loaded",
                             "membership_completeness_verified": False},
            "member_codes": sorted(unique), "funds": funds or [],
            "evidence": references([current, *members, *observations]),
            "limitations": ["只覆盖当日已保存成分；不以当前成分回填历史，成分表完整性未经独立核验。"],
            "judgments": [], "rule_version": RULE_VERSION}
