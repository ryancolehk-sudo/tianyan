"""Units, identities, field names and time rules for the Eastmoney v2 adapter.

Every conversion here is either:

* written in the public API documentation,
* observed in a bounded request whose raw response is kept outside the repo, or
* an explicitly named quote-unit convention (not a minimum order size).

Anything else stays ``None``. In particular, the adapter never turns a missing
value into a market zero, never uses ``received_at`` as ``source_time`` and never
maps an Eastmoney sector onto a Tushare DC sector by name.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
PROVIDER = "eastmoney"

AUCTION_STAGES = ("09:15", "09:20", "09:25")
STAGE_WINDOWS = {
    "09:15": (time(9, 15), time(9, 20)),
    "09:20": (time(9, 20), time(9, 25)),
    "09:25": (time(9, 25), time(9, 26)),
}
STAGE_RULES = {
    "09:15": "09:15-09:20 可撤单阶段；快照只代表该时点的委托簿，不代表最终开盘。",
    "09:20": "09:20-09:25 不可撤单阶段；快照只代表该时点的委托簿。",
    "09:25": "09:25 集中撮合后的委托簿；封单额按买一/卖一未成交量计算。",
}

# Quote units must not be derived from minimum order quantities. In particular,
# STAR's minimum order of 200 shares is not a 200-share quote lot: orders can
# increase by a single share. AKShare's stock_ask_bid_em maps f20/f40 x100 for
# every A share, and the CSISC quote SDK report lists 688/689 lots as 100 shares.
# Sources and the distinction are recorded in the handoff; no code is copied.
QUOTE_LOT = Decimal(100)

# Field names used from push2. All prices are already formatted because every
# request pins fltt=2; volumes stay in lots and are converted explicitly.
QUOTE_FIELDS = (
    "f43",  # 最新价 / 竞价参考价
    "f47",  # 成交量（手，累计）
    "f48",  # 成交额（元，累计）
    "f51",  # 涨停价
    "f52",  # 跌停价
    "f57",  # 代码
    "f58",  # 名称
    "f60",  # 昨收
    "f124",  # 来源时点（秒；休市后可能为 0）
    "f168",  # 换手率（%）
    "f170",  # 涨跌幅（%）
    "f19",  # 买一价
    "f20",  # 买一量（手）
    "f39",  # 卖一价
    "f40",  # 卖一量（手）
)
FUND_FIELDS = (
    "f3",  # 涨跌幅（%）
    "f6",  # 成交额（元）
    "f12",  # 代码
    "f13",  # 市场号
    "f14",  # 名称
    "f62",  # 主力净流入（元）
    "f66",  # 超大单净流入（元）
    "f72",  # 大单净流入（元）
    "f78",  # 中单净流入（元）
    "f84",  # 小单净流入（元）
    "f124",  # 来源时点（秒）
    "f184",  # 主力净占比（%）
)
MEMBER_FIELDS = ("f3", "f6", "f12", "f13", "f14", "f62", "f124")

AUCTION_CHECKPOINT_NOTE = (
    "stage 仅表示 f124 与目标节点精确到秒相同；五分钟阶段另记 observed_phase。"
    "stage_time_matched 仅为时间匹配；真实竞价字段尚待实测，stage_verified 保持 false。"
    "公开接口只提供调用时的当前快照，历史节点从启用采集后才积累，不能事后补造。"
)
SEAL_DEFINITION = (
    "封单额=涨停价×买一未成交委托量（或跌停价×卖一未成交委托量），"
    "仅当对应一档价格等于涨/跌停价且数量存在；竞价成交额 f48 不参与该计算。"
)
F124_NOTE = (
    "f124 是供应商时间戳；2026-10-23 收盘后实测为 15:39–16:11，晚于 15:00，"
    "不能当作最后成交时刻。盘中语义仍待真实交易时段复核。"
)
MAIN_SCOPE_NOTE = (
    "主力净额沿用东方财富 f62 定义；实测 f62=f66+f72（超大单+大单）。"
    "本适配器不重新定义主力，只检查该等式并披露差异。"
)


class MappingError(ValueError):
    """A controlled input/field error; the adapter turns it into a status."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def parse_trade_date(value: date | str) -> date:
    if isinstance(value, datetime):
        raise MappingError("invalid_trade_date")
    if isinstance(value, date):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise MappingError("invalid_trade_date")


def parse_stage(value: str) -> str:
    if value not in AUCTION_STAGES:
        raise MappingError("invalid_stage")
    return value


def _six_digit(code: str) -> str:
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        raise MappingError("invalid_ts_code")
    return code


def security_identity(ts_code: str) -> dict:
    """Return ``ts_code``/``secid``/exchange for an explicit A-share code.

    The suffix is mandatory. Guessing an exchange from a bare six-digit code
    would make index and stock identities collide.
    """
    if not isinstance(ts_code, str):
        raise MappingError("invalid_ts_code")
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", ts_code.upper())
    if not match:
        raise MappingError("invalid_ts_code")
    code, suffix = match.groups()
    _six_digit(code)
    if suffix == "SH":
        if not code.startswith("6"):
            raise MappingError("exchange_symbol_mismatch")
        exchange, market = "SSE", "1"
    elif suffix == "SZ":
        if not code.startswith(("0", "3")):
            raise MappingError("exchange_symbol_mismatch")
        exchange, market = "SZSE", "0"
    else:
        if not code.startswith(("4", "8", "92")):
            raise MappingError("exchange_symbol_mismatch")
        exchange, market = "BSE", "0"
    return {
        "ts_code": f"{code}.{suffix}",
        "symbol": code,
        "exchange": exchange,
        "secid": f"{market}.{code}",
    }


def ts_code_from_market(code, market) -> str:
    """Map a provider code+market pair to an explicit exchange suffix.

    Used for list endpoints where the caller did not supply a ts_code. A
    missing or inconsistent market number is an instrument mismatch, never a
    guessed Shenzhen identity.
    """
    _six_digit(code)
    if isinstance(market, bool):
        raise MappingError("instrument_mismatch")
    if isinstance(market, Decimal):
        if market != market.to_integral_value():
            raise MappingError("instrument_mismatch")
        market = int(market)
    if market == 1 and code.startswith("6"):
        return f"{code}.SH"
    if market == 0 and code.startswith(("0", "3")):
        return f"{code}.SZ"
    if market == 0 and code.startswith(("4", "8", "92")):
        return f"{code}.BJ"
    raise MappingError("instrument_mismatch")


def parse_ts_codes(values) -> list[dict]:
    if isinstance(values, str):
        values = [values]
    if values is None:
        raise MappingError("invalid_ts_codes")
    try:
        items = list(values)
    except TypeError as error:
        raise MappingError("invalid_ts_codes") from error
    if not items or len(items) > 50:
        raise MappingError("invalid_ts_codes")
    parsed = [security_identity(item) for item in items]
    if len({item["ts_code"] for item in parsed}) != len(parsed):
        raise MappingError("duplicate_ts_code")
    return parsed


def parse_sector_code(value: str) -> dict:
    if not isinstance(value, str):
        raise MappingError("invalid_sector_code")
    raw = value.strip()
    if raw.startswith("eastmoney:"):
        raw = raw.split(":", 1)[1]
    if raw.startswith("90."):
        raw = raw.split(".", 1)[1]
    if not re.fullmatch(r"BK\d{4}", raw.upper()):
        raise MappingError("invalid_sector_code")
    code = raw.upper()
    return {
        "sector_code": code,
        "sector_identity": f"eastmoney:{code}",
        "secid": f"90.{code}",
        # The member endpoint does not state whether BKxxxx is an industry,
        # concept or region board. Keep it null unless the caller supplies it.
        "sector_type": None,
    }


def parse_sector_codes(values, sector_type: str | None = None) -> list[dict]:
    if sector_type not in (None, "industry", "concept", "region"):
        raise MappingError("invalid_sector_type")
    if isinstance(values, str):
        values = [values]
    if values is None:
        return []
    try:
        items = list(values)
    except TypeError as error:
        raise MappingError("invalid_sector_codes") from error
    if len(items) > 50:
        raise MappingError("invalid_sector_codes")
    parsed = [parse_sector_code(item) for item in items]
    for item in parsed:
        item["sector_type"] = sector_type
    if len({item["sector_code"] for item in parsed}) != len(parsed):
        raise MappingError("duplicate_sector_code")
    return parsed


def lot_size(ts_code: str) -> Decimal:
    security_identity(ts_code)
    return QUOTE_LOT


def lot_basis(ts_code: str) -> str:
    size = lot_size(ts_code)
    return f"行情数量单位每手 {size} 股（AKShare 同字段换算；并非最小申报股数）"


def decimal_value(value, *, field: str, nonnegative: bool = False, maximum: Decimal | None = None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple, bytes)):
        raise MappingError(f"invalid_{field}")
    if isinstance(value, float):
        # JSON is parsed with parse_float=Decimal, so floats only appear in
        # synthetic callers. repr keeps the shortest exact decimal spelling.
        value = repr(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw in ("", "-", "--", "null", "None"):
            return None
        if len(raw) > 32 or not re.fullmatch(r"-?\d{1,18}(?:\.\d{1,10})?", raw):
            raise MappingError(f"invalid_{field}")
        try:
            number = Decimal(raw)
        except InvalidOperation as error:
            raise MappingError(f"invalid_{field}") from error
    elif isinstance(value, (int, Decimal)):
        number = Decimal(value)
    else:
        raise MappingError(f"invalid_{field}")
    if not number.is_finite():
        raise MappingError(f"invalid_{field}")
    if nonnegative and number < 0:
        raise MappingError(f"invalid_{field}")
    if maximum is not None and abs(number) > maximum:
        raise MappingError(f"invalid_{field}")
    return number


def price(value, *, field: str = "price") -> Decimal | None:
    return decimal_value(value, field=field, nonnegative=True, maximum=Decimal("1000000"))


def percent(value, *, field: str = "percent") -> Decimal | None:
    return decimal_value(value, field=field, maximum=Decimal("100000"))


def money(value, *, field: str = "money") -> Decimal | None:
    return decimal_value(value, field=field, maximum=Decimal("10000000000000000"))


def lots(value, *, field: str = "lots") -> Decimal | None:
    number = decimal_value(value, field=field, nonnegative=True, maximum=Decimal("10000000000"))
    # The quote may reveal odd shares in fractional lots. Only quantities that
    # convert into whole shares are accepted, rather than rejecting 1.5 lots.
    if number is not None and number * QUOTE_LOT != (number * QUOTE_LOT).to_integral_value():
        raise MappingError(f"invalid_{field}")
    return number


def shares_from_lots(value, ts_code: str) -> Decimal | None:
    number = lots(value)
    return None if number is None else number * lot_size(ts_code)


def decimal_string(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def amount_string(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return decimal_string(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def epoch_seconds(value, *, received_at: datetime) -> datetime | None:
    """Turn f124 into a Shanghai-aware time, or null when the field is unusable."""
    number = decimal_value(value, field="source_time", nonnegative=True, maximum=Decimal("100000000000"))
    if number is None or number <= 0:
        return None
    if number != number.to_integral_value():
        raise MappingError("invalid_source_time")
    if received_at.utcoffset() is None:
        raise MappingError("invalid_received_at")
    stamp = int(number)
    try:
        moment = datetime.fromtimestamp(stamp, SHANGHAI)
    except (OverflowError, OSError, ValueError) as error:
        raise MappingError("invalid_source_time") from error
    if moment > received_at.astimezone(SHANGHAI):
        raise MappingError("future_source_time")
    return moment


def stage_for(moment: datetime | None) -> str | None:
    """An exact checkpoint, never a five-minute auction phase."""
    if moment is None:
        return None
    local = moment.astimezone(SHANGHAI)
    for label in AUCTION_STAGES:
        if local.time() == time.fromisoformat(label):
            return label
    return None


def auction_phase_for(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    local = moment.astimezone(SHANGHAI)
    for label, (start, end) in STAGE_WINDOWS.items():
        if start <= local.time() < end:
            return label
    return None


def iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(SHANGHAI).isoformat(timespec="seconds")


def raw_fields(data: dict, field_names) -> dict:
    """Keep the provider's original key/value spelling for traceability."""
    kept = {}
    for name in field_names:
        if name in data:
            value = data[name]
            if isinstance(value, float):
                value = repr(value)
            elif isinstance(value, Decimal):
                value = format(value, "f")
            elif isinstance(value, bool):
                value = str(value).lower()
            else:
                value = str(value)
            kept[name] = value
    return kept
