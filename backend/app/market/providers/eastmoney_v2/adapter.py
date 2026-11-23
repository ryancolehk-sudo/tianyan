"""Eastmoney v2 supplemental adapter.

Five keyword-only entry points return the minimal exchange envelope agreed with
the front-end/Tushare workstream::

    {"dataset", "provider", "status", "reason", "records"}

``provider`` is always ``eastmoney``. ``status`` is one of ``ok``, ``partial``,
``unavailable``, ``permission_denied``, ``rate_limited``, ``error``. An upstream
error is never flattened into a successful empty table.

The adapter is intentionally standalone: it does not import the running
collector, does not write evidence rows and does not register a source. See
``docs/integrations/EASTMONEY_V2_HANDOFF.md`` for the integration steps.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.market.protocol import SHANGHAI
from app.market.providers.eastmoney_v2.client import EastmoneyClient, TransportFailure
from app.market.providers.eastmoney_v2.mapping import (
    AUCTION_CHECKPOINT_NOTE,
    F124_NOTE,
    FUND_FIELDS,
    MAIN_SCOPE_NOTE,
    MEMBER_FIELDS,
    PROVIDER,
    QUOTE_FIELDS,
    SEAL_DEFINITION,
    STAGE_RULES,
    MappingError,
    amount_string,
    auction_phase_for,
    decimal_string,
    epoch_seconds,
    iso,
    lot_basis,
    lot_size,
    lots,
    money,
    parse_sector_code,
    parse_sector_codes,
    parse_stage,
    parse_trade_date,
    parse_ts_codes,
    percent,
    price,
    raw_fields,
    shares_from_lots,
    stage_for,
    ts_code_from_market,
)

DATASET_AUCTION = "market.eastmoney.auction.order_snapshot"
DATASET_ORDER_BOOK = "market.eastmoney.order_book.snapshot"
DATASET_SECTOR_FUND = "market.eastmoney.sector_fund.snapshot"
DATASET_STOCK_FUND = "market.eastmoney.stock_fund.snapshot"
DATASET_MEMBERS = "market.eastmoney.sector_members.snapshot"

STOCK_UNIVERSE = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
INDUSTRY_BOARDS = "m:90+t:2"
FUND_FIELDS_TEXT = ",".join(FUND_FIELDS)
QUOTE_FIELDS_TEXT = ",".join(QUOTE_FIELDS)
MEMBER_FIELDS_TEXT = ",".join(MEMBER_FIELDS)

_MAX_SYMBOLS = 5
_MAX_MEMBERS = 300
_MAX_RANKING = 100
_MAX_CHANGE_SYMBOLS = 5
_FFLOW_FIELDS1 = "f1,f2,f3,f7"
_FFLOW_FIELDS2 = "f51,f52,f53,f54,f55,f56"

_PRIORITY = (
    "upstream_permission_denied",
    "upstream_rate_limited",
    "upstream_unavailable",
    "auction_history_unavailable",
    "outside_auction_window",
    "historical_membership_unavailable",
    "invalid_request",
    "no_records",
    "missing_required_fields",
    "stale_quote",
    "stage_time_mismatch",
    "stage_unverified",
    "auction_semantics_unverified",
    "source_time_stale",
    "source_time_missing",
    "order_book_unavailable",
    "order_book_partial",
    "trade_totals_missing",
    "main_net_scope_mismatch",
    "sector_not_found",
    "membership_date_unavailable",
    "membership_truncated",
    "intraday_change_unavailable",
    "partial_coverage",
)

_DEFAULT_CLIENT: EastmoneyClient | None = None


def _client(client: EastmoneyClient | None) -> EastmoneyClient:
    global _DEFAULT_CLIENT
    if client is not None:
        return client
    if _DEFAULT_CLIENT is None:
        # 3s pacing is a local guard, not a supplier quota. The handoff asks the
        # worker to schedule this adapter far less often than the quote pollers.
        _DEFAULT_CLIENT = EastmoneyClient(min_interval=3.0)
    return _DEFAULT_CLIENT


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _envelope(dataset: str, records: list[dict], status: str = "ok", reason: str | None = None) -> dict:
    return {"dataset": dataset, "provider": PROVIDER, "status": status, "reason": reason, "records": records,
            "quality_flags": [reason] if reason else []}


def _priority(flags) -> str | None:
    if not flags:
        return None
    for reason in _PRIORITY:
        if reason in flags:
            return reason
    return sorted(flags)[0]


def _finish(dataset: str, records: list[dict], requested: int, flags: set[str]) -> dict:
    if not records:
        flags.discard("partial_coverage")
        result = _envelope(dataset, records, "unavailable", _priority(flags) or "no_records")
        result["quality_flags"] = sorted(flags or {"no_records"})
        return result
    if requested and len(records) < requested:
        flags.add("partial_coverage")
    if flags:
        result = _envelope(dataset, records, "partial", _priority(flags))
        result["quality_flags"] = sorted(flags)
        return result
    return _envelope(dataset, records, "ok", None)


def _transport_failure(dataset: str, error: TransportFailure) -> dict:
    status = error.status if error.status in {"permission_denied", "rate_limited", "error"} else "error"
    result = _envelope(dataset, [], status, error.reason)
    if error.retry_after is not None:
        result["retry_after_seconds"] = error.retry_after
    return result


def _base_record(
    *,
    trade_date: date,
    received_at: datetime,
    source_time: datetime | None,
    frequency: str,
    metric_scope: str,
    endpoint: str,
    source_time_kind: str | None,
    ts_code: str | None = None,
    sector_code: str | None = None,
) -> dict:
    return {
        "trade_date": trade_date.isoformat(),
        "ts_code": ts_code,
        "sector_code": sector_code,
        "source_time": iso(source_time),
        "received_at": iso(received_at),
        "frequency": frequency,
        "metric_scope": metric_scope,
        "source_time_kind": source_time_kind,
        "source_endpoint": endpoint,
        "quality_notes": [],
        "quality_flags": [],
    }


def _check_code(item: dict, expected_code: str, expected_market) -> None:
    code = item.get("f12")
    if code is not None and code != expected_code:
        raise MappingError("instrument_mismatch")
    market = item.get("f13")
    if market is not None and expected_market is not None:
        if isinstance(market, bool):
            raise MappingError("instrument_mismatch")
        if isinstance(market, Decimal):
            if market != market.to_integral_value():
                raise MappingError("instrument_mismatch")
            market = int(market)
        if market != expected_market:
            raise MappingError("instrument_mismatch")


def _rows(payload: dict) -> tuple[list[dict], int | None]:
    data = payload.get("data")
    if data is None:
        return [], None
    if not isinstance(data, dict):
        raise MappingError("schema_changed")
    rows = data.get("diff")
    if isinstance(rows, dict):
        rows = list(rows.values())
    if rows is None:
        rows = []
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise MappingError("schema_changed")
    total = data.get("total")
    if total is not None and type(total) is not int:
        raise MappingError("schema_changed")
    return rows, total


def _positive_price(value, field: str) -> Decimal | None:
    parsed = price(value, field=field)
    return parsed if parsed is not None and parsed > 0 else None


def _quote_values(item: dict, info: dict, trade_date: date, received_at: datetime) -> dict:
    """Parse one stock/get row. Missing fields stay null; nothing is zero-filled."""
    if item.get("f57") not in (None, info["symbol"]):
        raise MappingError("instrument_mismatch")
    last = _positive_price(item.get("f43"), "last_price")
    bid1 = _positive_price(item.get("f19"), "bid1_price")
    bid1_lots = lots(item.get("f20"), field="bid1_volume")
    ask1 = _positive_price(item.get("f39"), "ask1_price")
    ask1_lots = lots(item.get("f40"), field="ask1_volume")
    limit_up = _positive_price(item.get("f51"), "limit_up")
    limit_down = _positive_price(item.get("f52"), "limit_down")
    volume_lots = lots(item.get("f47"), field="volume")
    amount = money(item.get("f48"), field="turnover")
    pct = percent(item.get("f170"), field="change_pct")
    prev_close = _positive_price(item.get("f60"), "prev_close")
    source_time = epoch_seconds(item.get("f124"), received_at=received_at)
    return {
        "last": last,
        "bid1": bid1,
        "bid1_lots": bid1_lots,
        "bid1_shares": shares_from_lots(bid1_lots, info["ts_code"]) if bid1_lots is not None else None,
        "ask1": ask1,
        "ask1_lots": ask1_lots,
        "ask1_shares": shares_from_lots(ask1_lots, info["ts_code"]) if ask1_lots is not None else None,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "volume_lots": volume_lots,
        "volume_shares": shares_from_lots(volume_lots, info["ts_code"]) if volume_lots is not None else None,
        "amount": amount,
        "pct": pct,
        "prev_close": prev_close,
        "source_time": source_time,
        "received_at": received_at,
    }


def _quote_flags(values: dict, trade_date: date) -> set[str]:
    flags: set[str] = set()
    if values["source_time"] is None:
        flags.add("source_time_missing")
    elif values["source_time"].date() != trade_date:
        flags.add("stale_quote")
    elif (values["received_at"] - values["source_time"]).total_seconds() > 90:
        flags.add("source_time_stale")
    if values["last"] is None and values["bid1"] is None and values["ask1"] is None:
        flags.add("order_book_unavailable")
    elif values["bid1"] is None and values["ask1"] is None:
        flags.add("order_book_unavailable")
    elif values["bid1"] is not None and values["bid1_shares"] is None:
        flags.add("order_book_partial")
    elif values["ask1"] is not None and values["ask1_shares"] is None:
        flags.add("order_book_partial")
    if values["volume_shares"] is None or values["amount"] is None:
        flags.add("trade_totals_missing")
    return flags


def _seal(values: dict) -> tuple[str | None, Decimal | None, Decimal | None]:
    """Return ``(side, shares, amount)`` for a limit queue, else ``(None, None, None)``."""
    if (
        values["bid1"] is not None
        and values["limit_up"] is not None
        and values["bid1"] == values["limit_up"]
        and values["bid1_shares"] is not None
        and values["bid1_shares"] > 0
    ):
        return "buy", values["bid1_shares"], values["bid1"] * values["bid1_shares"]
    if (
        values["ask1"] is not None
        and values["limit_down"] is not None
        and values["ask1"] == values["limit_down"]
        and values["ask1_shares"] is not None
        and values["ask1_shares"] > 0
    ):
        return "sell", values["ask1_shares"], values["ask1"] * values["ask1_shares"]
    return None, None, None


def _match_state(values: dict, seal_side: str | None) -> str:
    # A bid at the upper limit alone does not prove that the opposite book is
    # empty. Null/omitted fields are not a disclosed zero-size opposing queue.
    if (seal_side == "buy" and values["last"] == values["limit_up"]
            and values["ask1"] is None and values["ask1_shares"] == 0):
        return "limit_up_sealed"
    if (seal_side == "sell" and values["last"] == values["limit_down"]
            and values["bid1"] is None and values["bid1_shares"] == 0):
        return "limit_down_sealed"
    if values["last"] is None:
        return "unknown"
    if values["limit_up"] is not None and values["last"] == values["limit_up"]:
        return "at_limit_up_book_unknown"
    if values["limit_down"] is not None and values["last"] == values["limit_down"]:
        return "at_limit_down_book_unknown"
    if values["bid1"] is not None and values["ask1"] is not None:
        return "two_sided"
    if values["bid1"] is not None:
        return "buy_side_unmatched"
    if values["ask1"] is not None:
        return "sell_side_unmatched"
    return "unknown"


def _unmatched_side(state: str) -> str | None:
    if state in {"limit_up_sealed", "buy_side_unmatched"}:
        return "buy"
    if state in {"limit_down_sealed", "sell_side_unmatched"}:
        return "sell"
    if state == "two_sided":
        return "both"
    return None


def _quality(values: dict, flags: set[str], extra: list[str] | None = None) -> list[str]:
    notes = list(extra or [])
    if "source_time_missing" in flags:
        notes.append("f124 缺失或为 0，source_time 保持 null，未用接收时间替代。")
    if "stale_quote" in flags:
        notes.append("f124 的日期与请求交易日不一致，记录未返回。")
    if "source_time_stale" in flags:
        notes.append("来源时间距接收时间超过90秒，仅保留旧快照，不能推断当前封板状态。")
    if "order_book_partial" in flags:
        notes.append("买一/卖一价格存在但数量缺失；对应数量或金额为 null。")
    if (values.get("bid1") is None) != (values.get("ask1") is None):
        notes.append("仅一侧盘口存在；可能是真实单边委托，也可能是上游未揭示，本适配器不推断缺失侧。")
    if "order_book_unavailable" in flags:
        notes.append("当前响应没有买一/卖一，盘口不可用。")
    if "trade_totals_missing" in flags:
        notes.append("累计成交量或成交额缺失；未补零。")
    if values.get("source_time") is not None:
        notes.append(F124_NOTE)
    return notes


def _unit_basis(info: dict) -> dict:
    return {
        "price": "元（请求固定 fltt=2，价格字段已格式化）",
        "volume": f"手 × {lot_size(info['ts_code'])} 股/手 = 股；{lot_basis(info['ts_code'])}",
        "amount": "元（f48/f62/f66/f72/f78/f84 原单位，无缩放）",
        "seal_amount": "元；Decimal 乘法后保留两位小数",
    }


def _auction_record(
    info: dict,
    values: dict,
    *,
    requested_stage: str,
    trade_date: date,
    received_at: datetime,
) -> dict:
    observed = stage_for(values["source_time"])
    phase = auction_phase_for(values["source_time"])
    target = datetime.combine(trade_date, time.fromisoformat(requested_stage), tzinfo=SHANGHAI)
    offset = ((values["source_time"] - target).total_seconds()
              if values["source_time"] is not None else None)
    seal_side, seal_shares, seal_amount = _seal(values)
    state = _match_state(values, seal_side)
    record = _base_record(
        trade_date=trade_date,
        received_at=received_at,
        source_time=values["source_time"],
        frequency="snapshot",
        metric_scope="auction_unmatched_order_book",
        endpoint="/api/qt/stock/get",
        source_time_kind="provider_update_time",
        ts_code=info["ts_code"],
    )
    record.update(
        {
            "stage": observed,
            "requested_stage": requested_stage,
            "stage_verified": False,
            "stage_time_matched": observed == requested_stage,
            "observed_phase": phase,
            "checkpoint_offset_seconds": offset,
            "stage_rule": STAGE_RULES[phase] if phase else None,
            "stage_cancelable": (phase == "09:15") if phase else None,
            "last_price_cny": decimal_string(values["last"]),
            "prev_close_cny": decimal_string(values["prev_close"]),
            "change_pct": decimal_string(values["pct"]),
            "bid1_price_cny": decimal_string(values["bid1"]),
            "bid1_volume_shares": decimal_string(values["bid1_shares"]),
            "ask1_price_cny": decimal_string(values["ask1"]),
            "ask1_volume_shares": decimal_string(values["ask1_shares"]),
            "limit_up_price_cny": decimal_string(values["limit_up"]),
            "limit_down_price_cny": decimal_string(values["limit_down"]),
            "seal_side": None,
            "seal_volume_shares": None,
            "seal_amount_cny": None,
            "candidate_seal_side": seal_side,
            "candidate_seal_volume_shares": decimal_string(seal_shares),
            "candidate_seal_amount_cny": amount_string(seal_amount),
            "seal_definition": SEAL_DEFINITION,
            "matched_volume_shares": None,
            "matched_amount_cny": None,
            "reported_cumulative_volume_shares": decimal_string(values["volume_shares"]),
            "reported_cumulative_turnover_cny": decimal_string(values["amount"]),
            "matched_totals_scope": "f47/f48 竞价时段语义尚待实测，仅保留来源累计原值",
            "match_state": "auction_book_unverified",
            "candidate_match_state": state,
            "unmatched_side": None,
            "auction_history_scope": "live_snapshot_only",
            "observation_basis": "polling_snapshot",
            "auction_verification": "field_semantics_pending_live_auction",
            "raw_fields": raw_fields(
                values["_item"], ("f43", "f47", "f48", "f51", "f52", "f57", "f58", "f60", "f124", "f170", "f19", "f20", "f39", "f40")
            ),
            "unit_basis": _unit_basis(info),
        }
    )
    record["quality_notes"] = _quality(
        values,
        _quote_flags(values, trade_date),
        [
            AUCTION_CHECKPOINT_NOTE,
            "封单额由买一/卖一未成交队列推导；公开接口未单独标注“未匹配量”，需在真实竞价时段复核。",
        ],
    )
    record["quality_flags"] = sorted(_quote_flags(values, trade_date) | {"auction_semantics_unverified"})
    return record


def _order_book_record(info: dict, values: dict, *, trade_date: date, received_at: datetime) -> dict:
    seal_side, seal_shares, seal_amount = _seal(values)
    state = _match_state(values, seal_side)
    record = _base_record(
        trade_date=trade_date,
        received_at=received_at,
        source_time=values["source_time"],
        frequency="snapshot",
        metric_scope="level1_best_bid_ask",
        endpoint="/api/qt/stock/get",
        source_time_kind="provider_update_time",
        ts_code=info["ts_code"],
    )
    record.update(
        {
            "last_price_cny": decimal_string(values["last"]),
            "prev_close_cny": decimal_string(values["prev_close"]),
            "change_pct": decimal_string(values["pct"]),
            "bid1_price_cny": decimal_string(values["bid1"]),
            "bid1_volume_shares": decimal_string(values["bid1_shares"]),
            "ask1_price_cny": decimal_string(values["ask1"]),
            "ask1_volume_shares": decimal_string(values["ask1_shares"]),
            "limit_up_price_cny": decimal_string(values["limit_up"]),
            "limit_down_price_cny": decimal_string(values["limit_down"]),
            "seal_side": seal_side,
            "seal_volume_shares": decimal_string(seal_shares),
            "seal_amount_cny": amount_string(seal_amount),
            "seal_definition": SEAL_DEFINITION,
            "match_state": state,
            "unmatched_side": _unmatched_side(state),
            "matched_volume_shares": decimal_string(values["volume_shares"]),
            "matched_amount_cny": decimal_string(values["amount"]),
            "observation_basis": "polling_snapshot",
            "event_completeness": "not_tick_by_tick_no_total_count",
            "raw_fields": raw_fields(
                values["_item"], ("f43", "f47", "f48", "f51", "f52", "f57", "f58", "f60", "f124", "f170", "f19", "f20", "f39", "f40")
            ),
            "unit_basis": _unit_basis(info),
        }
    )
    record["quality_notes"] = _quality(values, _quote_flags(values, trade_date))
    record["quality_flags"] = sorted(_quote_flags(values, trade_date))
    if values["source_time"] is None or "source_time_stale" in record["quality_flags"]:
        record["candidate_match_state"] = record["match_state"]
        record["match_state"] = "unknown"
    return record


def _fund_record(
    info: dict,
    item: dict,
    *,
    trade_date: date,
    received_at: datetime,
    kind: str,
    rank: int | None = None,
    rank_scope: str | None = None,
    rank_total: int | None = None,
    sector_type: str | None = None,
) -> tuple[dict | None, set[str]]:
    flags: set[str] = set()
    expected_market = 90 if kind == "sector" else None
    _check_code(item, info["symbol"] if kind == "stock" else info["sector_code"], expected_market)
    if kind == "stock":
        derived = ts_code_from_market(item.get("f12"), item.get("f13"))
        if derived != info["ts_code"]:
            raise MappingError("instrument_mismatch")
    main = money(item.get("f62"), field="main_net_inflow")
    if main is None:
        flags.add("missing_required_fields")
        return None, flags
    super_large = money(item.get("f66"), field="super_large_net_inflow")
    large = money(item.get("f72"), field="large_net_inflow")
    medium = money(item.get("f78"), field="medium_net_inflow")
    small = money(item.get("f84"), field="small_net_inflow")
    source_time = epoch_seconds(item.get("f124"), received_at=received_at)
    if source_time is None:
        flags.add("source_time_missing")
        if trade_date != received_at.astimezone(SHANGHAI).date():
            return None, flags | {"source_date_unverified"}
    elif source_time.date() != trade_date:
        flags.add("stale_quote")
        return None, flags
    consistency_status = "incomplete"
    delta = None
    if super_large is not None and large is not None:
        delta = main - (super_large + large)
        if abs(delta) <= Decimal("1"):
            consistency_status = "consistent"
        else:
            consistency_status = "mismatch"
            flags.add("main_net_scope_mismatch")
    record = _base_record(
        trade_date=trade_date,
        received_at=received_at,
        source_time=source_time,
        frequency="snapshot",
        metric_scope="cumulative_main_net_inflow_sector" if kind == "sector" else "cumulative_main_net_inflow_stock",
        endpoint="/api/qt/clist/get" if rank is not None else "/api/qt/ulist.np/get",
        source_time_kind="provider_update_time",
        ts_code=info["ts_code"] if kind == "stock" else None,
        sector_code=info["sector_code"] if kind == "sector" else None,
    )
    record.update(
        {
            "name": item.get("f14") if isinstance(item.get("f14"), str) else None,
            "sector_type": sector_type,
            "main_net_inflow_cny": decimal_string(main),
            "pct_change": decimal_string(percent(item.get("f3"), field="change_pct")),
            "turnover_cny": decimal_string(money(item.get("f6"), field="turnover")),
            "super_large_net_inflow_cny": decimal_string(super_large),
            "large_net_inflow_cny": decimal_string(large),
            "medium_net_inflow_cny": decimal_string(medium),
            "small_net_inflow_cny": decimal_string(small),
            "main_net_inflow_ratio_pct": decimal_string(percent(item.get("f184"), field="main_ratio")),
            "main_net_scope": "eastmoney_f62_main_order_net",
            "main_net_scope_note": MAIN_SCOPE_NOTE,
            "market_time": None,
            "market_time_basis": "f124_is_provider_update_not_verified_trade_time",
            "main_net_inflow_5m_change_cny": None,
            "change_window_start": None,
            "change_window_end": None,
            "change_status": "not_requested",
            "main_net_consistency": {
                "status": consistency_status,
                "delta_cny": decimal_string(delta) if delta is not None else None,
                "components": "f62 - (f66 + f72)",
            },
            "rank": rank,
            "rank_scope": rank_scope,
            "rank_total": rank_total,
            "rank_5m_change": None,
            "rank_change_status": "not_computed_requires_two_snapshots",
            "raw_fields": raw_fields(item, FUND_FIELDS),
            "unit_basis": {
                "main_net_inflow": "元（f62）",
                "change_pct": "%（f3，2.31 表示 2.31%）",
                "turnover": "元（f6）",
                "ratio": "%（f184）",
                "main_scope": "主力=超大单+大单（f62=f66+f72）",
            },
        }
    )
    record["quality_notes"] = []
    if consistency_status == "incomplete":
        record["quality_notes"].append("超大单/大单至少一项缺失，无法复核 f62=f66+f72。")
    if consistency_status == "mismatch":
        record["quality_notes"].append("f62 与 f66+f72 不一致；保留原 f62，未重算主力净额。")
    if "source_time_missing" in flags:
        record["quality_notes"].append("f124 缺失或为 0，source_time 保持 null。")
    elif source_time is not None:
        record["quality_notes"].append(F124_NOTE)
        if source_time.time() > time(15):
            record["quality_notes"].append("f124 晚于15:00，仅表示供应商更新时间，不视为盘中行情节点。")
    record["quality_flags"] = sorted(flags)
    return record, flags


def _intraday_change(
    client: EastmoneyClient,
    info: dict,
    *,
    trade_date: date,
    flags: set[str],
    snapshot: dict,
) -> dict:
    """An exact five-minute pair aligned with this cumulative snapshot.

    The two endpoints are not assumed to have the same clock. A 15:00 kline
    cannot be silently attached as a 15:39/16:11 'current five-minute' change.
    """
    if snapshot.get("source_time") is None:
        flags.add("intraday_change_unaligned")
        return {}
    snapshot_time = datetime.fromisoformat(snapshot["source_time"])
    if snapshot["main_net_consistency"]["status"] == "mismatch":
        flags.add("intraday_change_scope_mismatch")
        return {}
    try:
        response = client.request_json(
            "/api/qt/stock/fflow/kline/get",
            {
                "lmt": 6,
                "klt": 1,
                "secid": info["secid"],
                "fields1": _FFLOW_FIELDS1,
                "fields2": _FFLOW_FIELDS2,
            },
        )
    except TransportFailure as error:
        if error.status in {"permission_denied", "rate_limited"}:
            raise
        flags.add("intraday_change_unavailable")
        return {}
    data = response.payload.get("data")
    if not isinstance(data, dict):
        flags.add("intraday_change_unavailable")
        return {}
    klines = data.get("klines")
    if not isinstance(klines, list) or not klines:
        flags.add("intraday_change_unavailable")
        return {}
    expected_symbol = info["secid"].split(".", 1)[1]
    if data.get("code") not in (None, expected_symbol):
        raise MappingError("instrument_mismatch")
    points: dict[datetime, Decimal] = {}
    for line in klines:
        if not isinstance(line, str):
            raise MappingError("schema_changed")
        parts = line.split(",")
        if len(parts) != 6:
            raise MappingError("schema_changed")
        try:
            moment = datetime.strptime(parts[0], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
        except ValueError as error:
            raise MappingError("schema_changed") from error
        value = money(parts[1], field="main_net_inflow")
        large = money(parts[4], field="large_net_inflow")
        super_large = money(parts[5], field="super_large_net_inflow")
        if moment.date() != trade_date:
            flags.add("stale_quote")
            return {}
        if moment > response.received_at.astimezone(SHANGHAI):
            flags.add("future_source_time")
            return {}
        if large is not None and super_large is not None and value is not None:
            if abs(value - (large + super_large)) > Decimal("1"):
                flags.add("intraday_change_scope_mismatch")
                return {}
        if value is not None:
            if moment in points and points[moment] != value:
                flags.add("conflicting_intraday_point")
                return {}
            points[moment] = value
    if len(points) < 2:
        flags.add("intraday_change_unavailable")
        return {}
    last_time = max(points)
    last_value = points[last_time]
    if last_time != snapshot_time.replace(second=0, microsecond=0):
        flags.add("intraday_change_unaligned")
        return {}
    if abs(last_value - money(snapshot["main_net_inflow_cny"], field="main_net_inflow")) > Decimal("1"):
        flags.add("intraday_change_unaligned")
        return {}
    target = last_time - timedelta(minutes=5)
    if target not in points or not _same_continuous_session(target, last_time):
        flags.add("intraday_change_unavailable")
        return {}
    start_time, start_value = target, points[target]
    return {
        "main_net_inflow_5m_change_cny": decimal_string(last_value - start_value),
        "change_window_start": iso(start_time),
        "change_window_end": iso(last_time),
        "change_basis": "同源同口径累计主力净额差（fflow/kline 1m，两个真实时点）",
        "change_source_time_kind": "kline_label",
        "change_received_at": iso(response.received_at),
        "change_start_cumulative_cny": decimal_string(start_value),
        "change_end_cumulative_cny": decimal_string(last_value),
        "change_alignment": "exact_five_minutes_and_snapshot_minute_value_consistent",
        "change_label_semantics": "minute_start_or_end_pending_live_verification",
    }


def _same_continuous_session(earlier: datetime, later: datetime) -> bool:
    if earlier.date() != later.date():
        return False
    return any(start <= earlier.time() <= later.time() <= end for start, end in (
        (time(9, 30), time(11, 30)), (time(13), time(15)),
    ))


def _auction_guard(trade_date: date, now: datetime) -> str | None:
    if trade_date != now.date():
        # Public endpoints expose only the current snapshot; there is no
        # historical auction book to replay.
        return "auction_history_unavailable"
    start = now.replace(hour=9, minute=14, second=0, microsecond=0)
    end = now.replace(hour=9, minute=27, second=0, microsecond=0)
    if not start <= now <= end:
        return "outside_auction_window"
    return None


def _read_quote(
    client: EastmoneyClient, info: dict, trade_date: date
) -> tuple[dict | None, datetime, set[str]]:
    response = client.request_json(
        "/api/qt/stock/get",
        {"secid": info["secid"], "fltt": 2, "invt": 2, "fields": QUOTE_FIELDS_TEXT},
    )
    data = response.payload.get("data")
    if not isinstance(data, dict):
        return None, response.received_at, {"no_records"}
    values = _quote_values(data, info, trade_date, response.received_at)
    values["_item"] = data
    if values["source_time"] is not None and values["source_time"].date() != trade_date:
        return None, response.received_at, {"stale_quote"}
    if values["source_time"] is None and trade_date != response.received_at.astimezone(SHANGHAI).date():
        return None, response.received_at, {"source_date_unverified"}
    if values["last"] is None and values["bid1"] is None and values["ask1"] is None:
        return None, response.received_at, {"order_book_unavailable"}
    return values, response.received_at, _quote_flags(values, trade_date)


def observed_seal_transition(previous: dict | None, current: dict, *, max_gap_seconds: int = 90) -> dict:
    """Compare two snapshots and label only what was actually observed.

    This is not a tick stream and cannot produce a complete open/reseal count.
    The caller must store the pair (or its source times) as evidence.
    """
    if not isinstance(current, dict):
        raise MappingError("invalid_snapshot")
    if type(max_gap_seconds) is not int or not 1 <= max_gap_seconds <= 300:
        raise MappingError("invalid_max_gap")
    previous_state = previous.get("match_state") if isinstance(previous, dict) else None
    current_state = current.get("match_state")
    sealed = {"limit_up_sealed", "limit_down_sealed"}
    event = "first_observation" if previous is None else "incomparable"
    reason = None if previous is None else "source_time_missing"
    elapsed = None
    known = sealed | {"two_sided", "buy_side_unmatched", "sell_side_unmatched"}
    try:
        if previous and previous.get("source_time") and current.get("source_time"):
            earlier = datetime.fromisoformat(previous["source_time"])
            later = datetime.fromisoformat(current["source_time"])
            # fromisoformat accepts naive values; reject before timezone conversion.
            if earlier.utcoffset() is None or later.utcoffset() is None:
                raise ValueError
            earlier, later = earlier.astimezone(SHANGHAI), later.astimezone(SHANGHAI)
            elapsed = int((later - earlier).total_seconds())
            if not current.get("ts_code") or previous.get("ts_code") != current.get("ts_code"):
                reason = "instrument_mismatch"
            elif elapsed <= 0:
                reason = "source_time_not_increasing"
            elif not _same_continuous_session(earlier, later):
                reason = "different_trading_session"
            elif elapsed > max_gap_seconds:
                reason = "sampling_gap"
            elif previous_state not in known or current_state not in known:
                reason = "book_state_unknown"
            elif ("source_time_stale" in previous.get("quality_flags", [])
                    or "source_time_stale" in current.get("quality_flags", [])):
                reason = "source_time_stale"
            else:
                reason = None
                if previous_state in sealed and current_state not in sealed:
                    event = "seal_ended"
                elif previous_state not in sealed and current_state in sealed:
                    event = "seal_started"
                elif previous_state == current_state:
                    event = "unchanged"
                else:
                    event = "state_changed"
    except (TypeError, ValueError):
        elapsed = None
    return {
        "previous_source_time": previous.get("source_time") if isinstance(previous, dict) else None,
        "current_source_time": current.get("source_time"),
        "elapsed_seconds": elapsed,
        "previous_state": previous_state,
        "current_state": current_state,
        "observed_event": event,
        "comparison_reason": reason,
        "max_gap_seconds": max_gap_seconds,
        "observation_scope": "adjacent_polling_snapshots_not_tick_stream",
        "count_claim": "none",
        "note": "仅比较同股、同交易时段、有序且间隔有界的已知盘口；断采、未知盘口不推断开板/回封。",
    }


def _rank_key(record: dict) -> tuple[str, str] | None:
    sector = record.get("sector_code")
    stock = record.get("ts_code")
    if sector and stock:
        return None
    if isinstance(sector, str) and sector:
        return "sector", sector
    if isinstance(stock, str) and stock:
        return "stock", stock
    return None


def _rank_batch(records) -> tuple[dict, dict]:
    """Validate one ranking batch; return its metadata or an incomparable reason."""
    if not isinstance(records, (list, tuple)) or not records:
        return {}, {"reason": "no_records"}
    if len(records) > 200:
        return {}, {"reason": "too_many_records"}
    kinds: set[str] = set()
    codes: set[str] = set()
    scopes: set[str] = set()
    totals: set[int] = set()
    ranks: set[int] = set()
    sector_types: set[str | None] = set()
    metric_scopes: set[str | None] = set()
    times: list[datetime] = []
    trade_days: set[str] = set()
    times_by_code: dict[str, datetime] = {}
    for record in records:
        if not isinstance(record, dict):
            return {}, {"reason": "invalid_record"}
        key = _rank_key(record)
        if key is None:
            return {}, {"reason": "mixed_instrument_kind"}
        kinds.add(key[0])
        if key[1] in codes:
            return {}, {"reason": "duplicate_code"}
        codes.add(key[1])
        rank = record.get("rank")
        if type(rank) is not int or rank < 1:
            return {}, {"reason": "rank_missing"}
        if rank in ranks:
            return {}, {"reason": "duplicate_rank"}
        ranks.add(rank)
        scope = record.get("rank_scope")
        if not isinstance(scope, str) or not scope.strip():
            return {}, {"reason": "rank_scope_missing"}
        scopes.add(scope)
        total = record.get("rank_total")
        if type(total) is not int or total < 1:
            return {}, {"reason": "rank_total_missing"}
        if rank > total:
            return {}, {"reason": "rank_out_of_range"}
        totals.add(total)
        sector_type = record.get("sector_type")
        if sector_type is not None and sector_type not in ("industry", "concept", "region"):
            return {}, {"reason": "invalid_sector_type"}
        sector_types.add(sector_type)
        metric_scope = record.get("metric_scope")
        if metric_scope is not None and (not isinstance(metric_scope, str) or not metric_scope):
            return {}, {"reason": "invalid_metric_scope"}
        metric_scopes.add(metric_scope)
        trade_day = record.get("trade_date")
        if not isinstance(trade_day, str):
            return {}, {"reason": "trade_date_missing"}
        try:
            parsed_day = date.fromisoformat(trade_day)
        except ValueError:
            return {}, {"reason": "trade_date_missing"}
        if parsed_day.isoformat() != trade_day:
            return {}, {"reason": "trade_date_missing"}
        trade_days.add(trade_day)
        moment = record.get("source_time")
        if not isinstance(moment, str) or not moment:
            return {}, {"reason": "source_time_missing"}
        try:
            parsed = datetime.fromisoformat(moment)
            if parsed.utcoffset() is None:
                return {}, {"reason": "source_time_missing"}
            parsed = parsed.astimezone(SHANGHAI)
        except (ValueError, OverflowError):
            return {}, {"reason": "source_time_missing"}
        if parsed.date() != parsed_day:
            return {}, {"reason": "source_trade_date_mismatch"}
        if not _same_continuous_session(parsed, parsed):
            return {}, {"reason": "different_trading_session"}
        received = record.get("received_at")
        if not isinstance(received, str) or not received:
            return {}, {"reason": "received_at_missing"}
        try:
            received_time = datetime.fromisoformat(received)
            if received_time.utcoffset() is None:
                return {}, {"reason": "received_at_missing"}
            received_time = received_time.astimezone(SHANGHAI)
        except (ValueError, OverflowError):
            return {}, {"reason": "received_at_missing"}
        lag = received_time - parsed
        if lag < timedelta(0):
            return {}, {"reason": "future_source_time"}
        if lag > timedelta(seconds=90):
            return {}, {"reason": "stale_snapshot"}
        flags = record.get("quality_flags", [])
        if not isinstance(flags, (list, tuple)) or any(not isinstance(flag, str) for flag in flags):
            return {}, {"reason": "invalid_quality_flags"}
        if "source_time_stale" in flags:
            return {}, {"reason": "stale_snapshot"}
        times.append(parsed)
        times_by_code[key[1]] = parsed
    if len(kinds) != 1:
        return {}, {"reason": "mixed_instrument_kind"}
    if len(scopes) != 1:
        return {}, {"reason": "rank_scope_mismatch"}
    if len(totals) != 1:
        return {}, {"reason": "rank_total_mismatch"}
    if len(sector_types) != 1:
        return {}, {"reason": "sector_type_mismatch"}
    if len(metric_scopes) != 1:
        return {}, {"reason": "metric_scope_mismatch"}
    if len(trade_days) != 1:
        return {}, {"reason": "trade_date_missing"}
    spread = max(times) - min(times)
    if spread > timedelta(seconds=90):
        return {}, {"reason": "source_time_spread"}
    return {
        "kind": next(iter(kinds)),
        "scope": next(iter(scopes)),
        "total": next(iter(totals)),
        "trade_date": next(iter(trade_days)),
        "sector_type": next(iter(sector_types)),
        "metric_scope": next(iter(metric_scopes)),
        "batch_time": max(times),
        "source_time_spread_seconds": spread.total_seconds(),
        "times_by_code": times_by_code,
        "codes": codes,
    }, {}


def observed_rank_change(
    previous_records,
    current_records,
    *,
    window_seconds: int = 300,
    tolerance_seconds: int = 0,
) -> dict:
    """Ordinal rank change between two same-scope ranking snapshots.

    Only compares records returned by the same ranking scope and total, with a
    bounded source-time spread in each batch, fresh source times, and an exact
    window for every common instrument in the same continuous session. It never
    subtracts cumulative money values: the output is
    an ordinal change, not a new flow measurement.
    """
    if type(window_seconds) is not int or not 60 <= window_seconds <= 900:
        raise MappingError("invalid_window")
    if type(tolerance_seconds) is not int or not 0 <= tolerance_seconds <= 30:
        raise MappingError("invalid_tolerance")
    previous, previous_error = _rank_batch(previous_records)
    current, current_error = _rank_batch(current_records)
    result = {
        "comparable": False,
        "rank_change_status": "not_comparable",
        "reason": None,
        "window_start": None,
        "window_end": None,
        "elapsed_seconds": None,
        "rank_scope": None,
        "rank_total": None,
        "changes": [],
        "coverage": {
            "previous_count": len(previous_records) if isinstance(previous_records, (list, tuple)) else 0,
            "current_count": len(current_records) if isinstance(current_records, (list, tuple)) else 0,
            "common_count": 0,
            "missing_in_current": [],
            "missing_in_previous": [],
        },
        "observation_scope": "same_scope_rank_change_between_two_polling_snapshots",
        "count_claim": "none",
        "note": "只比较同一 rank_scope/rank_total 的序位变化；不把不同口径或不同时点的累计金额相减。",
    }
    if previous_error:
        result["reason"] = previous_error["reason"]
        return result
    if current_error:
        result["reason"] = current_error["reason"]
        return result
    if previous["kind"] != current["kind"]:
        result["reason"] = "mixed_instrument_kind"
        return result
    if previous["scope"] != current["scope"]:
        result["reason"] = "rank_scope_mismatch"
        return result
    if previous["total"] != current["total"]:
        result["reason"] = "rank_total_mismatch"
        return result
    for field in ("sector_type", "metric_scope"):
        if previous[field] != current[field]:
            result["reason"] = f"{field}_mismatch"
            return result
    if previous["trade_date"] != current["trade_date"]:
        result["reason"] = "different_trade_date"
        return result
    interval = current["batch_time"] - previous["batch_time"]
    elapsed = interval.total_seconds()
    result["window_start"] = iso(previous["batch_time"])
    result["window_end"] = iso(current["batch_time"])
    result["elapsed_seconds"] = int(elapsed) if elapsed.is_integer() else elapsed
    result["rank_scope"] = previous["scope"]
    result["rank_total"] = previous["total"]
    if elapsed <= 0:
        result["reason"] = "source_time_not_increasing"
        return result
    if not _same_continuous_session(previous["batch_time"], current["batch_time"]):
        result["reason"] = "different_trading_session"
        return result
    if abs(interval - timedelta(seconds=window_seconds)) > timedelta(seconds=tolerance_seconds):
        result["reason"] = "window_not_exact"
        return result
    previous_by_code = {_rank_key(record)[1]: record for record in previous_records}
    current_by_code = {_rank_key(record)[1]: record for record in current_records}
    common = sorted(set(previous_by_code) & set(current_by_code))
    if not common:
        result["reason"] = "no_common_codes"
        return result
    changes = []
    for code in common:
        before = previous_by_code[code]
        after = current_by_code[code]
        before_time = previous["times_by_code"][code]
        after_time = current["times_by_code"][code]
        if after_time <= before_time:
            result["reason"] = "source_time_not_increasing"
            return result
        if not _same_continuous_session(before_time, after_time):
            result["reason"] = "different_trading_session"
            return result
        if abs((after_time - before_time) - timedelta(seconds=window_seconds)) > timedelta(seconds=tolerance_seconds):
            result["reason"] = "window_not_exact"
            return result
        changes.append(
            {
                "code": code,
                "previous_rank": before["rank"],
                "current_rank": after["rank"],
                "rank_change": before["rank"] - after["rank"],
                "previous_main_net_inflow_cny": before.get("main_net_inflow_cny"),
                "current_main_net_inflow_cny": after.get("main_net_inflow_cny"),
            }
        )
    result["changes"] = changes
    result["coverage"].update(
        {
            "common_count": len(common),
            "missing_in_current": sorted(set(previous_by_code) - set(current_by_code)),
            "missing_in_previous": sorted(set(current_by_code) - set(previous_by_code)),
        }
    )
    result["comparable"] = True
    result["rank_change_status"] = "computed"
    if result["coverage"]["missing_in_current"] or result["coverage"]["missing_in_previous"]:
        result["reason"] = "partial_rank_coverage"
    return result


def fetch_auction_order_snapshots(*, trade_date, ts_codes, stage, client=None):
    """Snapshot the auction order book at 09:15/09:20/09:25.

    Only the live snapshot exists. A historical ``trade_date`` returns
    ``unavailable`` with ``auction_history_unavailable``; the adapter never
    fabricates node history after the fact.
    """
    try:
        day = parse_trade_date(trade_date)
        label = parse_stage(stage)
        codes = parse_ts_codes(ts_codes)
        if len(codes) > _MAX_SYMBOLS:
            raise MappingError("too_many_symbols")
    except MappingError as error:
        return _envelope(DATASET_AUCTION, [], "error", error.reason)
    now = _now()
    guard = _auction_guard(day, now)
    if guard:
        return _envelope(DATASET_AUCTION, [], "unavailable", guard)
    reader = _client(client)
    records: list[dict] = []
    flags: set[str] = set()
    for info in codes:
        try:
            values, received_at, row_flags = _read_quote(reader, info, day)
        except TransportFailure as error:
            flags.add(error.reason)
            if error.status in {"permission_denied", "rate_limited"}:
                if not records:
                    return _transport_failure(DATASET_AUCTION, error)
                break
            continue
        except MappingError as error:
            flags.add(error.reason)
            continue
        if values is None:
            flags |= row_flags
            continue
        flags |= row_flags
        observed = stage_for(values["source_time"])
        if observed != label:
            flags.add("stage_time_mismatch" if observed else "stage_unverified")
        flags.add("auction_semantics_unverified")
        records.append(
            _auction_record(info, values, requested_stage=label, trade_date=day, received_at=received_at)
        )
    return _finish(DATASET_AUCTION, records, len(codes), flags)


def fetch_order_book_snapshots(*, trade_date, ts_codes, client=None):
    """Current best bid/ask (level-1) for the requested stocks.

    The endpoint has no historical book. Outside the session Eastmoney may omit
    f19/f20/f39/f40 entirely; those cells stay null and the envelope is
    ``partial``/``unavailable`` instead of a zero-filled quote.
    """
    try:
        day = parse_trade_date(trade_date)
        codes = parse_ts_codes(ts_codes)
        if len(codes) > _MAX_SYMBOLS:
            raise MappingError("too_many_symbols")
    except MappingError as error:
        return _envelope(DATASET_ORDER_BOOK, [], "error", error.reason)
    reader = _client(client)
    records: list[dict] = []
    flags: set[str] = set()
    for info in codes:
        try:
            values, received_at, row_flags = _read_quote(reader, info, day)
        except TransportFailure as error:
            flags.add(error.reason)
            if error.status in {"permission_denied", "rate_limited"}:
                if not records:
                    return _transport_failure(DATASET_ORDER_BOOK, error)
                break
            continue
        except MappingError as error:
            flags.add(error.reason)
            continue
        if values is None:
            flags |= row_flags
            continue
        flags |= row_flags
        records.append(_order_book_record(info, values, trade_date=day, received_at=received_at))
    return _finish(DATASET_ORDER_BOOK, records, len(codes), flags)


def _fund_request_params(codes: list[dict], kind: str, limit: int, sector_type: str | None) -> tuple[str, dict]:
    if codes:
        secids = ",".join(info["secid"] for info in codes)
        return "/api/qt/ulist.np/get", {
            "fltt": 2,
            "invt": 2,
            "secids": secids,
            "fields": FUND_FIELDS_TEXT,
        }
    if kind == "stock":
        universe = STOCK_UNIVERSE
    else:
        universe = {
            "industry": INDUSTRY_BOARDS,
            "concept": "m:90+t:3",
            "region": "m:90+t:1",
        }.get(sector_type or "industry", INDUSTRY_BOARDS)
    return "/api/qt/clist/get", {
        "pn": 1,
        "pz": min(limit, _MAX_RANKING),
        "po": 1,
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": "f62",
        "fs": universe,
        "fields": FUND_FIELDS_TEXT,
    }


def _fund_collect(
    dataset: str,
    *,
    trade_date,
    ts_codes,
    sector_codes,
    kind: str,
    limit: int,
    sector_type: str | None,
    include_intraday_change: bool | None,
    client,
) -> dict:
    try:
        day = parse_trade_date(trade_date)
        if kind == "stock":
            codes = parse_ts_codes(ts_codes) if ts_codes is not None else []
            sectors: list[dict] = []
            if len(codes) > 50:
                raise MappingError("too_many_symbols")
        else:
            sectors = parse_sector_codes(sector_codes, sector_type)
            if sector_codes is not None and not sectors:
                raise MappingError("invalid_sector_codes")
            codes = []
            if len(sectors) > 50:
                raise MappingError("too_many_sectors")
        if type(limit) is not int or not 1 <= limit <= _MAX_RANKING:
            raise MappingError("invalid_limit")
        if type(include_intraday_change) is not bool:
            raise MappingError("invalid_intraday_change_option")
        change_count = len(codes or sectors) or limit
        if include_intraday_change and change_count > _MAX_CHANGE_SYMBOLS:
            raise MappingError("too_many_intraday_change_symbols")
    except MappingError as error:
        return _envelope(dataset, [], "error", error.reason)
    reader = _client(client)
    requested = len(codes) if kind == "stock" else len(sectors)
    path, params = _fund_request_params(codes or sectors, kind, limit, sector_type)
    try:
        response = reader.request_json(path, params)
    except TransportFailure as error:
        return _transport_failure(dataset, error)
    try:
        rows, total = _rows(response.payload)
    except MappingError as error:
        return _envelope(dataset, [], "error", error.reason)
    wanted = {info["symbol"] if kind == "stock" else info["sector_code"]: info for info in (codes or sectors)}
    records: list[dict] = []
    flags: set[str] = set()
    seen: set[str] = set()
    for index, item in enumerate(rows[:50 if wanted else limit], start=1):
        raw_code = item.get("f12")
        if not isinstance(raw_code, str):
            flags.add("no_records")
            continue
        if wanted:
            info = wanted.get(raw_code)
            if info is None:
                continue
        else:
            if kind == "stock":
                try:
                    info = {
                        "symbol": raw_code,
                        "ts_code": ts_code_from_market(raw_code, item.get("f13")),
                        "secid": "",
                    }
                except MappingError as error:
                    flags.add(error.reason)
                    continue
            else:
                try:
                    info = parse_sector_code(raw_code)
                    info["sector_type"] = sector_type or "industry"
                except MappingError as error:
                    flags.add(error.reason)
                    continue
        if raw_code in seen:
            flags.add("duplicate_instrument")
            continue
        seen.add(raw_code)
        try:
            record, row_flags = _fund_record(
                info,
                item,
                trade_date=day,
                received_at=response.received_at,
                kind=kind,
                rank=index if not wanted else None,
                rank_scope=(
                    f"eastmoney:{kind}:fs={params['fs']}:fid={params['fid']}:po={params['po']}"
                    if not wanted else None
                ),
                rank_total=total if not wanted else None,
                sector_type=(sector_type or info.get("sector_type")) if kind == "sector" else None,
            )
        except MappingError as error:
            flags.add(error.reason)
            continue
        flags |= row_flags
        if record is None:
            continue
        records.append(record)
    if wanted:
        missing = set(wanted) - seen
        if missing:
            flags.add("partial_coverage")
    if not records:
        return _finish(dataset, records, requested, flags)

    if include_intraday_change:
        for record in records:
            record["change_status"] = "not_attempted"
        for record in records:
            info = {
                "secid": (
                    f"90.{record['sector_code']}"
                    if record.get("sector_code")
                    else f"{'1' if record['ts_code'].endswith('.SH') else '0'}.{record['ts_code'].split('.')[0]}"
                )
            }
            change_flags: set[str] = set()
            try:
                change = _intraday_change(
                    reader,
                    info,
                    trade_date=day,
                    flags=change_flags,
                    snapshot=record,
                )
            except TransportFailure as error:
                flags.add(error.reason)
                record["quality_flags"].append(error.reason)
                record["change_status"] = error.status
                if error.status in {"permission_denied", "rate_limited"}:
                    break
                continue
            except MappingError as error:
                flags.add(error.reason)
                record["quality_flags"].append(error.reason)
                record["change_status"] = "error"
                continue
            flags |= change_flags
            record["quality_flags"] = sorted(set(record["quality_flags"]) | change_flags)
            record["change_status"] = "ok" if change else "unavailable"
            record.update(change)
    return _finish(dataset, records, requested, flags)


def fetch_sector_fund_snapshots(
    *,
    trade_date,
    sector_codes,
    sector_type=None,
    include_intraday_change=False,
    limit=50,
    client=None,
):
    """Cumulative sector main net with a provider timestamp.

    ``sector_codes=None`` returns the industry ranking by f62 (one page, up to
    100); explicit codes are fetched by ``ulist.np`` and carry no rank. The
    five-minute change is computed from two real cumulative points of the same
    source, never from a second provider.
    """
    return _fund_collect(
        DATASET_SECTOR_FUND,
        trade_date=trade_date,
        ts_codes=None,
        sector_codes=sector_codes,
        kind="sector",
        limit=limit,
        sector_type=sector_type,
        include_intraday_change=include_intraday_change,
        client=client,
    )


def fetch_stock_fund_snapshots(
    *,
    trade_date,
    ts_codes,
    include_intraday_change=False,
    limit=50,
    client=None,
):
    """Cumulative stock main net, optional ranking and same-source 5m change."""
    return _fund_collect(
        DATASET_STOCK_FUND,
        trade_date=trade_date,
        ts_codes=ts_codes,
        sector_codes=None,
        kind="stock",
        limit=limit,
        sector_type=None,
        include_intraday_change=include_intraday_change,
        client=client,
    )


def fetch_sector_members(*, trade_date, sector_codes, sector_type=None, client=None):
    """Current sector members.

    Eastmoney does not publish a membership effective date. ``membership_date``
    is therefore always null and the envelope is ``partial`` with
    ``membership_date_unavailable``; the query is a current snapshot, not a
    historical membership replay.
    """
    try:
        day = parse_trade_date(trade_date)
        sectors = parse_sector_codes(sector_codes, sector_type)
        if not sectors:
            raise MappingError("invalid_sector_codes")
        if len(sectors) > 5:
            raise MappingError("too_many_sectors")
    except MappingError as error:
        return _envelope(DATASET_MEMBERS, [], "error", error.reason)
    now = _now()
    if day != now.date():
        return _envelope(DATASET_MEMBERS, [], "unavailable", "historical_membership_unavailable")
    reader = _client(client)
    records: list[dict] = []
    flags: set[str] = {"membership_date_unavailable"}
    hard_stop = False
    requests_remaining = 3
    for sector in sectors:
        page = 1
        total = None
        seen: set[str] = set()
        while page <= 3:
            if requests_remaining == 0:
                flags.add("request_budget_exhausted")
                hard_stop = True
                break
            requests_remaining -= 1
            try:
                response = reader.request_json(
                    "/api/qt/clist/get",
                    {
                        "pn": page,
                        "pz": 100,
                        "po": 1,
                        "np": 1,
                        "fltt": 2,
                        "invt": 2,
                        "fid": "f3",
                        "fs": f"b:{sector['sector_code']}",
                        "fields": MEMBER_FIELDS_TEXT,
                    },
                )
            except TransportFailure as error:
                flags.add(error.reason)
                hard_stop = error.status in {"permission_denied", "rate_limited"}
                if hard_stop and not records:
                    return _transport_failure(DATASET_MEMBERS, error)
                break
            try:
                rows, total = _rows(response.payload)
            except MappingError as error:
                flags.add(error.reason)
                break
            if not rows:
                break
            for item in rows:
                code = item.get("f12")
                market = item.get("f13")
                if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
                    flags.add("instrument_mismatch")
                    continue
                try:
                    ts_code = ts_code_from_market(code, market)
                    change_pct = percent(item.get("f3"), field="change_pct")
                except MappingError as error:
                    flags.add(error.reason)
                    continue
                if ts_code in seen:
                    continue
                seen.add(ts_code)
                record = _base_record(
                    trade_date=day,
                    received_at=response.received_at,
                    source_time=None,
                    frequency="daily",
                    metric_scope="sector_membership_snapshot",
                    endpoint="/api/qt/clist/get",
                    source_time_kind=None,
                    ts_code=ts_code,
                    sector_code=sector["sector_code"],
                )
                record.update(
                    {
                        "sector_identity": sector["sector_identity"],
                        "sector_type": sector["sector_type"],
                        "sector_name": None,
                        "member_name": item.get("f14") if isinstance(item.get("f14"), str) else None,
                        "membership_date": None,
                        "membership_date_basis": "not_provided_by_source",
                        "membership_as_of": "current_snapshot_not_historical",
                        "quote_change_pct": decimal_string(change_pct),
                        "raw_fields": raw_fields(item, MEMBER_FIELDS),
                        "unit_basis": {"change_pct": "%（f3）", "membership_date": "上游未提供"},
                    }
                )
                record["quality_notes"] = ["东财未提供成分生效日期；membership_date 保持 null。"]
                records.append(record)
            if total is None or page * 100 >= total:
                break
            page += 1
        if total is not None and total > _MAX_MEMBERS:
            flags.add("membership_truncated")
        if total is not None and len(seen) < total:
            flags.add("partial_coverage")
        if not seen and not hard_stop:
            flags.add("sector_not_found")
        if hard_stop:
            break
    return _finish(DATASET_MEMBERS, records, 0, flags)
