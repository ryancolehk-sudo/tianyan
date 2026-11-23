"""Pure, deterministic screen evaluation and state-edge notifications.

Callers supply already-authorized, time-qualified observations. This module does
not fetch data, infer absent metrics, execute expressions, or send notifications.
Numeric outputs are decimal strings so persistence cannot round a threshold.
"""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from operator import eq, ge, gt, le, lt

ENGINE_VERSION = "screen-v1"
METRICS = {
    "price": {"label": "最新价", "unit": "元"},
    "pct_change": {"label": "涨跌幅", "unit": "%"},
    "speed_5m": {"label": "近5分钟涨速", "unit": "%"},
    "amount_cny": {"label": "成交额", "unit": "元"},
    "turnover_rate": {"label": "换手率", "unit": "%"},
    "float_mv_cny": {"label": "流通市值", "unit": "元"},
    "open_pct": {"label": "开盘涨幅", "unit": "%"},
    "previous_height": {"label": "前一交易日连板高度", "unit": "板"},
    "volume_ratio": {"label": "竞价成交量比", "unit": "倍"},
}
AUCTION_METRICS = {
    'auction_pct': {'label': '竞价成交涨幅', 'unit': '%'},
    'auction_amount_cny': {'label': '竞价成交额', 'unit': '元'},
    'auction_turnover_rate': {'label': '竞价换手率', 'unit': '%'},
    'auction_volume_ratio': {'label': '来源竞价量比', 'unit': '倍'},
    'previous_height': METRICS['previous_height'],
    'yesterday_up': {'label': '昨日涨停池成员', 'unit': ''},
    'yesterday_broken': {'label': '昨日炸板池成员', 'unit': ''},
    'yesterday_lhb': {'label': '昨日龙虎榜成员', 'unit': ''},
}
_OPERATORS = {"gt": gt, "gte": ge, "lt": lt, "lte": le, "eq": eq}
_STATES = ("matched", "not_matched", "insufficient")
_DIRECTIONS = {"entered", "exited"}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid_decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("invalid_decimal") from error
    if not result.is_finite():
        raise ValueError("invalid_decimal")
    return result


def _text(value):
    return str(value) if value is not None else None


def _timestamp(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _code(row):
    if not isinstance(row, dict) or not isinstance(row.get("code"), str) or not row["code"].strip():
        raise ValueError("invalid_instrument_code")
    return row["code"]


def _definition(definition, metrics=METRICS):
    if not isinstance(definition, dict) or not isinstance(definition.get("conditions"), list):
        raise ValueError("invalid_definition")
    conditions = []
    for condition in definition["conditions"]:
        if not isinstance(condition, dict):
            raise ValueError("invalid_condition")
        field, operator = condition.get("field"), condition.get("operator")
        if not isinstance(field, str) or field not in metrics:
            raise ValueError("unknown_metric")
        if not isinstance(operator, str) or operator not in _OPERATORS:
            raise ValueError("unknown_operator")
        conditions.append((field, operator, _number(condition.get("value"))))
    sort_field, direction = definition.get("sort_field"), definition.get("sort_direction")
    if not isinstance(sort_field, str) or sort_field not in metrics:
        raise ValueError("unknown_sort_metric")
    if direction not in ("asc", "desc"):
        raise ValueError("invalid_sort_direction")
    return conditions, sort_field, direction


def evaluate(rows: list[dict], definition: dict, *, metrics=METRICS) -> dict:
    """Return every row with a three-valued AND and stable null-last sorting.

    Invalid definitions raise ValueError. Missing or invalid observation values
    are unknown, never zero. A known false condition wins over unknowns; no
    conditions is the ordinary vacuous AND (matched).
    """
    conditions, sort_field, direction = _definition(definition, metrics)
    if not isinstance(rows, list):
        raise ValueError("invalid_rows")
    counts = {"total": len(rows), "matched": 0, "not_matched": 0, "insufficient": 0}
    items = []
    for row in rows:
        code = _code(row)
        values, reasons = {}, {}
        for field in metrics:
            if row.get(field) is None:
                values[field], reasons[field] = None, "数据缺失，无法判断。"
                continue
            try:
                values[field] = _number(row[field])
            except ValueError:
                values[field], reasons[field] = None, "数据不是有效的有限数值，无法判断。"
        checks = []
        for field, operator, target in conditions:
            actual = values[field]
            state = (
                "insufficient"
                if actual is None
                else "matched"
                if _OPERATORS[operator](actual, target)
                else "not_matched"
            )
            checks.append(
                {
                    "field": field,
                    "operator": operator,
                    "target": str(target),
                    "actual": _text(actual),
                    "state": state,
                    "reason": reasons[field]
                    if actual is None
                    else "满足条件。"
                    if state == "matched"
                    else "未满足条件。",
                }
            )
        state = (
            "not_matched"
            if any(check["state"] == "not_matched" for check in checks)
            else "insufficient"
            if any(check["state"] == "insufficient" for check in checks)
            else "matched"
        )
        item = {
            "code": code,
            "name": row.get("name"),
            "instrument_id": _text(row.get("instrument_id")),
            "source_time": _timestamp(row.get("source_time")),
            "received_at": _timestamp(row.get("received_at")),
                "metrics": {field: _text(values[field]) for field in metrics},
            "state": state,
            "checks": checks,
        }
        items.append((item, values[sort_field]))
        counts[state] += 1

    # First sort on the stable tie-breaker. Reversing only the numeric sort
    # preserves ascending codes for equal values, including descending screens.
    items.sort(key=lambda entry: entry[0]["code"])
    known = [entry for entry in items if entry[1] is not None]
    missing = [entry for entry in items if entry[1] is None]
    known.sort(key=lambda entry: entry[1], reverse=direction == "desc")
    return {"items": [item for item, _ in known + missing], "counts": counts}


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware_datetime_required")
    return value.astimezone(UTC)


def _saved_time(value):
    if not isinstance(value, str):
        raise ValueError("invalid_saved_time")
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError as error:
        raise ValueError("invalid_saved_time") from error


def _previous(previous, at):
    if not isinstance(previous, dict):
        raise ValueError("invalid_previous_state")
    if not previous:
        return {}
    if (
        previous.get("version") != ENGINE_VERSION
        or previous.get("initialized") is not True
        or not isinstance(previous.get("instruments"), dict)
    ):
        raise ValueError("invalid_previous_state")
    instruments = {}
    for code, entry in previous["instruments"].items():
        _code({"code": code})
        if (
            not isinstance(entry, dict)
            or entry.get("state") not in ("matched", "not_matched", None)
            or not isinstance(entry.get("last_event_at"), dict)
        ):
            raise ValueError("invalid_previous_state")
        seen = _saved_time(entry.get("last_seen_at"))
        if seen > at:
            raise ValueError("evaluation_time_regression")
        event_times = {}
        for direction, value in entry["last_event_at"].items():
            if direction not in _DIRECTIONS:
                raise ValueError("invalid_previous_state")
            event_time = _saved_time(value)
            if event_time > seen:
                raise ValueError("invalid_previous_state")
            event_times[direction] = event_time.isoformat()
        instruments[code] = {
            "state": entry.get("state"),
            "last_seen_at": seen.isoformat(),
            "last_event_at": event_times,
        }
    return instruments


def transitions(
    previous: dict, evaluations: list[dict], at: datetime, cooldown_seconds: int
) -> tuple[dict, list[dict]]:
    """Track definite edges; the first definite observation is only a baseline.

    Missing rows and insufficient evaluations retain the last definite state.
    Cooldown suppresses delivery, not state movement; a suppressed edge is never
    replayed later merely because time passes. State is JSON-safe and reusable
    after restart. Each call and its resulting state must be persisted atomically
    by the caller. A different screen definition must use a separate state.
    """
    now = _aware(at)
    if (
        isinstance(cooldown_seconds, bool)
        or not isinstance(cooldown_seconds, int)
        or cooldown_seconds < 0
    ):
        raise ValueError("invalid_cooldown")
    if not isinstance(evaluations, list):
        raise ValueError("invalid_evaluations")
    instruments = _previous(previous, now)
    by_code = {}
    for item in evaluations:
        code = _code(item)
        if code in by_code:
            raise ValueError("duplicate_instrument_code")
        if item.get("state") not in _STATES:
            raise ValueError("invalid_evaluation_state")
        by_code[code] = item
    events = []
    for code, item in sorted(by_code.items()):
        entry = instruments.setdefault(
            code, {"state": None, "last_seen_at": now.isoformat(), "last_event_at": {}}
        )
        entry["last_seen_at"] = now.isoformat()
        current, prior = item["state"], entry["state"]
        if current == "insufficient":
            continue
        entry["state"] = current
        if prior is None or prior == current:
            continue
        direction = "entered" if current == "matched" else "exited"
        last = entry["last_event_at"].get(direction)
        if last is not None and (now - _saved_time(last)).total_seconds() < cooldown_seconds:
            continue
        entry["last_event_at"][direction] = now.isoformat()
        events.append(
            {
                "code": code,
                "direction": direction,
                "source_time": _timestamp(item.get("source_time")),
                "name": item.get("name"),
            }
        )
    return {"version": ENGINE_VERSION, "initialized": True, "instruments": instruments}, events
