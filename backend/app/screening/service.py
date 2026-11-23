"""Frozen evaluations; GET only replays stored inputs and never collects."""

import uuid
from datetime import datetime

from sqlalchemy import func, select

from app.auth import APIError
from app.evidence.common import content_hash, lock, normalized, now
from app.market.protocol import SHANGHAI
from app.market.service import permission_reason
from app.market.terminal_read import pct, segment, workbench_view
from app.market.watchlist import watchlist
from app.models import AuditEvent, Provider
from app.screening import schemas as s
from app.screening.engine import AUCTION_METRICS, ENGINE_VERSION, METRICS, evaluate, transitions
from app.screening.models import ScreenEvent, ScreenRule, ScreenRuleVersion, ScreenRun

MAX_RULES = 20
INTERVAL_SECONDS = 60
REASONS = [
    "研究条件命中不代表可买入；这里只评估本机已保存的实际样本，不宣称全市场完整覆盖。",
    "行情、盘后资料与历史高度各自保留时点；缺值不填零，不补造五档或分钟资金。",
    "自动观察每60秒检查本机缓存，仅连续交易时段的180秒内新行情参与提醒；首次运行只建基线。",
]


def catalogue():
    descriptions = {
        "price": "同源当前成交价，元。",
        "pct_change": "当前价相对昨收，百分数，例如3表示3%。",
        "speed_5m": "同源、同一连续交易段两次报价相隔270至330秒计算。",
        "amount_cny": "同源当日累计成交额，元。",
        "turnover_rate": "同日股本或同日盘后基本指标，百分数。",
        "float_mv_cny": "同日可用流通股本与当前价，或同日基本指标，元。",
        "open_pct": "当日开盘价相对昨收，百分数。",
        "previous_height": "前一共同交易日的已确认日榜高度，板。",
        "volume_ratio": "同日已保存竞价成交参考的量比；不是全时段实时量比。",
    }
    return s.Catalogue(
        engine_version=ENGINE_VERSION,
        metrics=[
            s.MetricInfo(
                field=key, label=value["label"], unit=value["unit"], description=descriptions[key]
            )
            for key, value in METRICS.items()
        ],
        scope="当前已存沪深京批量快照的实际证券，或本人关注列表；不受看板200行展示上限截断。",
        boundaries=REASONS + ["尚未接入完整历史复权、持续全市场分钟、盘口队列与通用策略回测。"],
    )


def rule_dto(row):
    return s.Rule(
        id=row.id,
        version=row.version,
        enabled=row.enabled,
        last_run_at=row.last_run_at,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        **row.content,
    )


def get_rule(db, owner_id, ident, *, for_update=False):
    stmt = select(ScreenRule).where(ScreenRule.id == ident, ScreenRule.owner_id == owner_id)
    row = db.scalar(stmt.with_for_update() if for_update else stmt)
    if row is None:
        raise APIError(404, "not_found", "观察规则不存在或不可访问。")
    return row


def save_rule(db, owner, body, request_id):
    lock(db, f"screening-rules:{owner.id}")
    clock = now(db)
    content = normalized(body.model_dump(exclude={"id", "expected_version", "enabled"}))
    if body.id:
        row = get_rule(db, owner.id, body.id, for_update=True)
        if row.version != body.expected_version:
            raise APIError(
                409, "screen_rule_conflict", "观察规则已在其他窗口更新，请重新读取后保存。"
            )
        if row.content == content and row.enabled == body.enabled:
            return rule_dto(row)
        row.version += 1
        row.content = content
        row.enabled = body.enabled
        row.transition_state = {}
        row.last_error = None
        row.next_run_at = clock
        row.updated_at = clock
    else:
        count = db.scalar(
            select(func.count()).select_from(ScreenRule).where(ScreenRule.owner_id == owner.id)
        )
        if count >= MAX_RULES:
            raise APIError(
                422, "screen_rule_limit", "每个账号最多保存20条观察规则，可修改已有规则。"
            )
        row = ScreenRule(
            id=uuid.uuid4(),
            owner_id=owner.id,
            version=1,
            content=content,
            enabled=body.enabled,
            transition_state={},
            next_run_at=clock,
            created_at=clock,
            updated_at=clock,
        )
        db.add(row)
        db.flush()
    db.add(
        ScreenRuleVersion(
            rule_id=row.id,
            owner_id=owner.id,
            version=row.version,
            content={**content, "enabled": body.enabled},
            content_hash=content_hash({**content, "enabled": body.enabled}),
            created_at=clock,
        )
    )
    db.add(
        AuditEvent(
            actor_id=owner.id,
            action="screening.rule_saved",
            request_id=request_id,
            resource_type="screen_rule",
            resource_id=row.id,
            changed_field_names=["version", "content", "enabled"],
        )
    )
    db.flush()
    return rule_dto(row)


def context(db, owner_id, body, *, scope, monitor=False):
    clock = now(db)
    cutoff = body.as_of_time or clock
    if cutoff > clock:
        raise APIError(422, "future_cutoff", "研究截止不能晚于服务器当前时间。")
    refs = []
    view = workbench_view(
        db, owner_id, day=body.day, as_of_time=cutoff, full_universe=True, input_manifest=refs
    )
    rows = []
    watched = watchlist(db, owner_id).items if scope == "watchlist" else []
    stocks = {stock.instrument_id: stock for stock in view.stocks if stock.instrument_id}
    selection = (
        [
            stocks.get(str(item.instrument.id))
            or {
                "code": item.instrument.symbol
                + "."
                + {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[item.instrument.exchange],
                "name": item.instrument.name,
                "instrument_id": str(item.instrument.id),
            }
            for item in watched
        ]
        if scope == "watchlist"
        else [item for item in view.stocks if item.source_time]
    )
    quote_cap = next((item for item in view.capabilities if item.key == "quotes"), None)
    received = quote_cap.last_received_at if quote_cap else None
    latest_source = None
    for stock in selection:
        original = stock if isinstance(stock, dict) else stock.model_dump(mode="json")
        row = {
            **{key: original.get(key) for key in ("code", "name", "instrument_id", "source_time")},
            **{key: original.get(key) for key in METRICS},
            "received_at": received.isoformat() if received else None,
        }
        if (
            not isinstance(stock, dict)
            and stock.open is not None
            and stock.pre_close is not None
            and stock.pre_close > 0
        ):
            row["open_pct"] = pct(stock.open, stock.pre_close)
        source = None
        if row["source_time"]:
            source = datetime.fromisoformat(row["source_time"])
            latest_source = max(latest_source, source) if latest_source else source
        if monitor and (source is None or not 0 <= (clock - source).total_seconds() <= 180):
            for field in METRICS:
                row[field] = None
        rows.append(row)
    fresh = bool(
        received
        and latest_source
        and 0 <= (clock - received).total_seconds() <= 180
        and 0 <= (clock - latest_source).total_seconds() <= 180
    )
    if monitor and (
        view.trade_day != clock.astimezone(SHANGHAI).date() or segment(clock) is None or not fresh
    ):
        raise APIError(
            422,
            "screen_market_not_fresh",
            "非连续交易时段或缺少180秒内的当前行情，未生成观察提醒。",
        )
    rows.sort(key=lambda row: row["code"])
    return {
        "rows": normalized(rows),
        "source_refs": refs,
        "trade_day": view.trade_day,
        "as_of_time": cutoff,
        "data_revision": view.detail_revision,
        "mode": "monitor" if monitor else "observed" if body.as_of_time else "review",
        "created_at": clock,
        "reasons": REASONS
        + ([] if rows else ["当前范围没有可用证券；可配置批量行情或添加有权关注标的。"]),
    }


def evaluate_context(ctx, content):
    evaluated = evaluate(ctx["rows"], content["definition"], metrics=AUCTION_METRICS if ctx.get('screen_context')=='auction' else METRICS)
    fingerprint = content_hash(
        {
            "engine": ENGINE_VERSION,
            "definition": content["definition"],
            "scope": content["scope"],
            "trade_day": ctx["trade_day"],
            "rows": ctx["rows"],
            "source_refs": ctx["source_refs"],
            "observation_cutoff": ctx["as_of_time"] if ctx["mode"] == "observed" else None,
            **({'screen_context': 'auction'} if ctx.get('screen_context') == 'auction' else {}),
        }
    )
    return evaluated, fingerprint


def preview(db, owner_id, body):
    content = {"definition": body.definition.model_dump(), "scope": body.scope}
    metrics = AUCTION_METRICS if body.screen_context == 'auction' else METRICS
    if any(c.field not in metrics for c in body.definition.conditions) or body.definition.sort_field not in metrics:
        raise APIError(422, 'screen_context_mismatch', '所选条件与当前数据范围不一致。')
    if body.screen_context == 'auction':
        from app.screening.auction import auction_context
        ctx = auction_context(db, owner_id, body)
    else:
        ctx = context(db, owner_id, body, scope=body.scope)
    evaluated, fingerprint = evaluate_context(ctx, content)
    items = [
        item for item in evaluated["items"] if body.state is None or item["state"] == body.state
    ]
    return s.RunResult(
        definition=body.definition,
        scope=body.scope,
        trade_day=ctx["trade_day"],
        as_of_time=ctx["as_of_time"],
        mode=ctx["mode"],
        counts=evaluated["counts"],
        items=items[body.offset : body.offset + body.limit],
        offset=body.offset,
        limit=body.limit,
        has_more=body.offset + body.limit < len(items),
        input_hash=fingerprint,
        data_revision=ctx["data_revision"],
        engine_version=ENGINE_VERSION,
        source_refs=ctx["source_refs"],
        created_at=ctx["created_at"],
        reasons=ctx["reasons"],
        screen_context=ctx.get('screen_context', 'market'), coverage=ctx.get('coverage', {}),
    )


def run_rule(db, owner_id, ident, body, *, monitor=False):
    lock(db, f"screening-run:{ident}")
    rule = get_rule(db, owner_id, ident, for_update=True)
    ctx = context(db, owner_id, body, scope=rule.content["scope"], monitor=monitor)
    previous = (
        rule.transition_state
        if rule.transition_state.get("trade_day") == ctx["trade_day"].isoformat()
        else {}
    )
    watermarks = dict(previous.get("source_watermarks", {}))
    if monitor:
        advancing = False
        ignored = 0
        for item in ctx["rows"]:
            source = item.get("source_time")
            if source is None:
                continue
            prior = watermarks.get(item["code"])
            if prior is not None and datetime.fromisoformat(source) <= datetime.fromisoformat(
                prior
            ):
                for field in METRICS:
                    item[field] = None
                ignored += 1
            else:
                watermarks[item["code"]] = source
                advancing = True
        if not advancing:
            last = db.scalar(
                select(ScreenRun)
                .where(
                    ScreenRun.rule_id == rule.id,
                    ScreenRun.rule_version == rule.version,
                    ScreenRun.mode == "monitor",
                )
                .order_by(ScreenRun.created_at.desc(), ScreenRun.id.desc())
                .limit(1)
            )
            if last is not None:
                return last
        if ignored:
            ctx["reasons"] = ctx["reasons"] + [
                "部分证券的来源时点未推进或倒退，本轮保留上次确定状态；同源时点修订不触发提醒。"
            ]
    evaluated, fingerprint = evaluate_context(ctx, rule.content)
    existing = db.scalar(
        select(ScreenRun).where(
            ScreenRun.rule_id == rule.id,
            ScreenRun.rule_version == rule.version,
            ScreenRun.input_hash == fingerprint,
            ScreenRun.mode == ctx["mode"],
        )
    )
    if existing:
        return existing
    events = []
    if monitor:
        if not rule.enabled:
            raise APIError(409, "screen_rule_disabled", "该观察规则已停用。")
        new_state, events = transitions(
            previous, evaluated["items"], ctx["created_at"], rule.content["cooldown_seconds"]
        )
        rule.transition_state = {
            **new_state,
            "trade_day": ctx["trade_day"].isoformat(),
            "source_watermarks": watermarks,
        }
    row = ScreenRun(
        id=uuid.uuid4(),
        owner_id=owner_id,
        rule_id=rule.id,
        rule_version=rule.version,
        rule_name=rule.content["name"],
        content=rule.content,
        trade_day=ctx["trade_day"],
        as_of_time=ctx["as_of_time"],
        mode=ctx["mode"],
        engine_version=ENGINE_VERSION,
        input_hash=fingerprint,
        data_revision=ctx["data_revision"],
        inputs=ctx["rows"],
        source_refs=normalized(ctx["source_refs"]),
        result={"counts": evaluated["counts"], "result_hash": content_hash(evaluated)},
        reasons=ctx["reasons"],
        event_count=len(events),
        created_at=ctx["created_at"],
    )
    db.add(row)
    db.flush()
    for event in events:
        db.add(
            ScreenEvent(
                owner_id=owner_id,
                rule_id=rule.id,
                run_id=row.id,
                rule_name=row.rule_name,
                rule_version=rule.version,
                code=event["code"],
                name=event.get("name") or event["code"],
                direction=event["direction"],
                source_time=event.get("source_time"),
                created_at=ctx["created_at"],
            )
        )
    rule.last_run_at = ctx["created_at"]
    rule.last_error = None
    db.flush()
    return row


def readable(db, row):
    source_ids = {uuid.UUID(ref["source_id"]) for ref in row.source_refs}
    providers = {
        provider.id: provider
        for provider in db.scalars(select(Provider).where(Provider.id.in_(source_ids)))
    }
    return all(
        ident in providers and not permission_reason(providers[ident]) for ident in source_ids
    )


def summary(row):
    return s.RunSummary(
        id=row.id,
        rule_id=row.rule_id,
        rule_version=row.rule_version,
        rule_name=row.rule_name,
        trade_day=row.trade_day,
        as_of_time=row.as_of_time,
        mode=row.mode,
        counts=row.result["counts"],
        created_at=row.created_at,
        event_count=row.event_count,
        input_hash=row.input_hash,
        engine_version=row.engine_version,
    )


def run_result(db, row, *, state=None, offset=0, limit=100):
    if not readable(db, row):
        raise APIError(
            403, "screen_source_permission", "来源当前已撤权，历史派生结果暂停展示；原记录仍保留。"
        )
    if row.engine_version != ENGINE_VERSION:
        raise APIError(409, "screen_engine_version", "此运行使用较早计算版本，需要对应版本读取器。")
    evaluated = evaluate(row.inputs, row.content["definition"])
    if content_hash(evaluated) != row.result["result_hash"]:
        raise APIError(
            409, "screen_result_mismatch", "冻结输入的复算结果与原记录不符，已停止展示。"
        )
    items = [item for item in evaluated["items"] if state is None or item["state"] == state]
    return s.RunResult(
        id=row.id,
        rule_id=row.rule_id,
        rule_version=row.rule_version,
        rule_name=row.rule_name,
        definition=row.content["definition"],
        scope=row.content["scope"],
        trade_day=row.trade_day,
        as_of_time=row.as_of_time,
        mode=row.mode,
        counts=row.result["counts"],
        items=items[offset : offset + limit],
        offset=offset,
        limit=limit,
        has_more=offset + limit < len(items),
        event_count=row.event_count,
        input_hash=row.input_hash,
        data_revision=row.data_revision,
        engine_version=row.engine_version,
        source_refs=row.source_refs,
        created_at=row.created_at,
        reasons=row.reasons,
    )
