"""Read-only shadow-run quality and operating-cost evaluation, never P&L.

Only scalar metadata and the small JSON fragments required for aggregation are
read. Full run bundles, rendered tables, credentials and model prompts are not
loaded. Unknown usage and historical knowledge remain unknown.
"""

import math
import uuid
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONPATH

from app.auth import APIError
from app.evidence.common import normalized, now
from app.evidence.models import EvidenceVersion
from app.market.protocol import SHANGHAI
from app.market.review_store import available_trade_days
from app.models import Provider
from app.tomorrow.models import PlanConfirmation, PlanReport, PlanRun, PlanValidation

MAX_RUNS = 5000
MANUAL_METHODS = frozenset({"manual", "human", "通达信人工确认"})
MACHINE_METHODS = frozenset({"machine", "automatic", "rule", "deterministic"})


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _text(value):
    if value is None:
        return None
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _percent(n, denominator):
    return _text((Decimal(n) * 100 / denominator).quantize(Decimal("0.01"))) if denominator else None


def _timestamp(value):
    if isinstance(value, datetime):
        return value if value.utcoffset() is not None else None
    if isinstance(value, str):
        try:
            return _timestamp(datetime.fromisoformat(value))
        except ValueError:
            pass
    return None


def _day(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return None


def _uuids(values):
    result = set()
    for value in values:
        try:
            result.add(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            continue
    return list(result)


def _load_records(db, owner_id, cutoff, days):
    empty = dict(runs=[], reports=[], confirmations=[], validations=[], attempts=[], evidence={}, revisions=[], truncated=False)
    if not days:
        return empty
    runs = list(db.execute(select(
        PlanRun.id, PlanRun.source_trade_day, PlanRun.target_trade_day, PlanRun.as_of_time,
        PlanRun.created_at, PlanRun.completed_at, PlanRun.state, PlanRun.error_code,
        PlanRun.use_ai, PlanRun.stages,
        PlanRun.config_snapshot["app_mode"].astext.label("app_mode"),
        PlanRun.config_snapshot["synthetic"].label("synthetic"),
        PlanRun.bundle["knowledge_mode"].astext.label("knowledge_mode"),
    ).where(PlanRun.owner_id == owner_id, PlanRun.created_at <= cutoff, PlanRun.as_of_time <= cutoff,
            or_(PlanRun.target_trade_day.in_(days), and_(PlanRun.target_trade_day.is_(None), PlanRun.source_trade_day.in_(days))))
        .order_by(PlanRun.created_at.desc(), PlanRun.id.desc()).limit(MAX_RUNS + 1)).mappings())
    truncated = len(runs) > MAX_RUNS
    runs = [dict(row) for row in runs[:MAX_RUNS]]
    if not runs:
        return empty
    run_ids = [row["id"] for row in runs]
    # Revision metadata are small; latest report content is selected afterwards.
    revisions = [dict(row) for row in db.execute(select(
        PlanReport.id, PlanReport.run_id, PlanReport.revision_number, PlanReport.author_type,
        PlanReport.created_at, PlanReport.payload["revision_reason"].astext.label("reason"),
    ).where(PlanReport.owner_id == owner_id, PlanReport.run_id.in_(run_ids), PlanReport.created_at <= cutoff)
        .order_by(PlanReport.run_id, PlanReport.revision_number.desc(), PlanReport.id.desc())).mappings()]
    revision_index = {row["id"]: row for row in revisions}
    confirmations = [dict(row) for row in db.execute(select(
        PlanConfirmation.report_id, PlanConfirmation.created_at,
    ).join(PlanReport, PlanReport.id == PlanConfirmation.report_id).where(
        PlanConfirmation.owner_id == owner_id, PlanConfirmation.created_at <= cutoff,
        PlanReport.owner_id == owner_id, PlanReport.run_id.in_(run_ids), PlanReport.created_at <= cutoff,
    )).mappings()]
    heads, confirmed, prospective = {}, {}, {}
    run_index = {row["id"]: row for row in runs}
    for row in revisions:
        heads.setdefault(row["run_id"], row["id"])
        target = run_index[row["run_id"]]["target_trade_day"]
        if target and row["created_at"] < datetime.combine(target, time(9, 15), SHANGHAI):
            prospective.setdefault(row["run_id"], row["id"])
    for row in sorted(confirmations, key=lambda item: item["created_at"], reverse=True):
        revision = revision_index.get(row["report_id"])
        if revision:
            confirmed.setdefault(revision["run_id"], row["report_id"])
    report_ids = list(set(heads.values()) | set(confirmed.values()) | set(prospective.values()))
    reports = [dict(row) for row in db.execute(select(
        PlanReport.id, PlanReport.run_id, PlanReport.revision_number, PlanReport.created_at,
        PlanReport.author_type,
        PlanReport.payload["dossier"]["ready"].label("core_ready"),
        PlanReport.payload["dossier"]["target_trade_day_verified"].label("target_verified"),
        PlanReport.payload["dossier"]["scenarios"].label("scenarios"),
        PlanReport.payload["evidence_refs"].label("evidence_refs"),
        func.jsonb_path_query_array(PlanReport.payload, cast('$.sections[*].analysis[*].evidence_refs', JSONPATH)).label("analysis_refs"),
        func.jsonb_array_length(func.jsonb_path_query_array(PlanReport.payload, cast('$.sections[*].analysis[*]', JSONPATH))).label("analysis_count"),
        PlanReport.payload["audit_binding"]["passed"].label("audit_passed"),
    ).where(PlanReport.owner_id == owner_id, PlanReport.id.in_(report_ids))).mappings()] if report_ids else []
    for row in reports:
        row["is_head"] = heads.get(row["run_id"]) == row["id"]
        row["is_selected_confirmation"] = confirmed.get(row["run_id"]) == row["id"]
        row["is_prospective_head"] = prospective.get(row["run_id"]) == row["id"]
    validations = [dict(row) for row in db.execute(select(
        PlanValidation.id, PlanValidation.report_id, PlanValidation.scenario_id, PlanValidation.status,
        PlanValidation.created_at, PlanValidation.evidence_refs,
    ).join(PlanReport, PlanReport.id == PlanValidation.report_id).where(
        PlanValidation.owner_id == owner_id, PlanValidation.created_at <= cutoff,
        PlanReport.owner_id == owner_id, PlanReport.run_id.in_(run_ids), PlanReport.created_at <= cutoff,
    ).order_by(PlanValidation.created_at.desc(), PlanValidation.id.desc())).mappings()]
    from app.ai.models import AiAttempt, AiRun
    stage_ids = _uuids(step.get("ai_run_id") for run in runs for step in run.get("stages") or [] if isinstance(step, dict))
    attempts = [dict(row) for row in db.execute(select(
        AiAttempt.id, AiAttempt.run_id, AiAttempt.state, AiAttempt.started_at, AiAttempt.sent_at,
        AiAttempt.completed_at, AiAttempt.elapsed_ms, AiAttempt.input_tokens, AiAttempt.output_tokens,
        AiAttempt.cost_cny, AiAttempt.request_count, AiAttempt.error_code,
        AiRun.config_snapshot["billing_source"].astext.label("billing_source"),
    ).join(AiRun, AiRun.id == AiAttempt.run_id).where(
        AiAttempt.owner_id == owner_id, AiRun.owner_id == owner_id,
        AiRun.feature == "plan_generation", AiRun.id.in_(stage_ids), AiRun.created_at <= cutoff,
        AiAttempt.started_at <= cutoff,
    )).mappings()] if stage_ids else []
    ids = _uuids(ref.get("id") for report in reports for ref in report.get("evidence_refs") or [] if isinstance(ref, dict))
    evidence = {}
    if ids:
        # Avoid service.permitted's full payload load for hundreds of table parts.
        for row in db.execute(select(
            EvidenceVersion.id, EvidenceVersion.content_hash, EvidenceVersion.received_at,
            EvidenceVersion.available_at, EvidenceVersion.published_at, EvidenceVersion.historical_eligible,
            EvidenceVersion.owner_id, EvidenceVersion.provenance, Provider.kind,
            Provider.configured, Provider.enabled, Provider.entitlement_status, Provider.rights,
        ).join(Provider, Provider.id == EvidenceVersion.source_id).where(
            EvidenceVersion.id.in_(ids), EvidenceVersion.received_at <= cutoff,
            or_(and_(EvidenceVersion.visibility == "private", EvidenceVersion.owner_id == owner_id),
                and_(EvidenceVersion.visibility == "shared", EvidenceVersion.owner_id.is_(None))),
        )).mappings():
            manual = row["owner_id"] == owner_id and row["kind"] == "manual" and row["provenance"] == "manual"
            allowed = manual or (row["configured"] and row["enabled"] and row["entitlement_status"] == "verified"
                                 and all((row["rights"] or {}).get(key) == "allowed" for key in ("display", "cache")))
            if allowed:
                evidence[str(row["id"])] = {key: row[key] for key in ("content_hash", "received_at", "published_at", "available_at", "historical_eligible")}
    return dict(runs=runs, reports=reports, confirmations=confirmations, validations=validations,
                attempts=attempts, evidence=evidence, revisions=revisions, truncated=truncated)


def _usage(attempts, cutoff):
    sent, missing_sent, states, billing = [], 0, Counter(), Counter()
    for attempt in attempts:
        if not attempt.get("request_count"):
            continue
        stamp = _timestamp(attempt.get("sent_at"))
        if stamp is None:
            missing_sent += 1
            continue
        if stamp > cutoff:
            continue
        sent.append(attempt)
        complete = _timestamp(attempt.get("completed_at"))
        states[attempt.get("state", "unknown") if complete and complete <= cutoff else "in_progress_at_cutoff"] += 1
        billing[attempt.get("billing_source") or "api_or_unspecified"] += 1
    result = {"attempt_count": len(attempts), "sent_attempt_count": len(sent),
              "actual_request_count": sum(int(attempt["request_count"]) for attempt in sent),
              "request_records_without_send_time": missing_sent, "states": dict(states), "billing_sources": dict(billing)}
    for field in ("input_tokens", "output_tokens", "cost_cny", "elapsed_ms"):
        values = []
        for attempt in sent:
            complete = _timestamp(attempt.get("completed_at"))
            values.append(_decimal(attempt.get(field)) if complete and complete <= cutoff else None)
        known = [value for value in values if value is not None]
        all_known = len(known) == len(values)
        total = sum(known, Decimal(0))
        result[field] = {"total": _text(total) if all_known else None,
                         "known_total": _text(total) if known or not values else None,
                         "known_attempts": len(known), "unknown_attempts": len(values) - len(known)}
    result["cost_policy"] = "仅累计实际请求记录的cost_cny；不按模型名称推测单价，不把未知费用当零。"
    return result


def _reference_status(report, evidence, cutoff):
    refs = [ref for ref in report.get("evidence_refs") or [] if isinstance(ref, dict)]
    valid, strict = set(), 0
    for ref in refs:
        meta = evidence.get(str(ref.get("id")))
        usable = meta and meta.get("content_hash") == ref.get("content_hash") and cutoff is not None
        if usable:
            for field in ("received_at", "published_at", "available_at"):
                stamp = _timestamp(meta.get(field))
                if stamp and stamp > cutoff:
                    usable = False
        if usable:
            valid.add(str(ref["id"]))
            strict += int(meta.get("historical_eligible") is True and _timestamp(meta.get("available_at")) is not None)
    return refs, valid, strict


def _summarize(records, cutoff, days, requested):
    runs, reports = records["runs"], records["reports"]
    heads = {row["run_id"]: row for row in reports if row.get("is_head")}
    prospective = {row["run_id"]: row for row in reports if row.get("is_prospective_head")}
    confirmed = {row["report_id"]: row for row in records["confirmations"]}
    confirmed_by_run = {row["run_id"]: row for row in reports if row.get("is_selected_confirmation")}
    revision_index = {row["id"]: row for row in records["revisions"]}
    quality, modes, states = Counter(), Counter(), Counter()
    calendar = sorted({_day(day) for day in records.get("known_closed_trade_days", days) if _day(day)})
    predecessors = {day: calendar[index - 1] for index, day in enumerate(calendar) if index > 0}
    covered_days, eligible_days, confirmed_days = set(), set(), set()
    ref_total = ref_valid = strict_refs = claims = claimed = all_claimed = 0
    conditions, validation_states = Counter(), Counter()
    durations, observations = [], []
    for run in runs:
        target = _day(run.get("target_trade_day"))
        completed = _timestamp(run.get("completed_at"))
        created, cutoff_run = _timestamp(run.get("created_at")), _timestamp(run.get("as_of_time"))
        states[run.get("state", "unknown") if completed is None or completed <= cutoff else "in_progress_at_cutoff"] += 1
        if completed and created and created <= completed <= cutoff:
            durations.append(int((completed - created).total_seconds() * 1000))
        head = heads.get(run["id"])
        if not head:
            quality["without_report"] += 1
            continue
        if target in days:
            covered_days.add(target)
        if head.get("core_ready") is False:
            quality["core_data_missing"] += 1
        elif head.get("core_ready") is not True:
            quality["core_data_unknown"] += 1
        if head.get("target_verified") is not True:
            quality["target_day_unverified"] += 1
        adjacent = target in predecessors and _day(run.get("source_trade_day")) == predecessors[target]
        if not adjacent:
            quality["source_target_alignment_unverified"] += 1
        deadline = datetime.combine(target, time(9, 15), SHANGHAI) if target else None
        prior_report = prospective.get(run["id"])
        if run.get("synthetic") is True or run.get("app_mode") == "demo":
            mode = "synthetic_or_demo"
        elif not prior_report or not deadline or not created or not cutoff_run or created >= deadline or cutoff_run >= deadline or cutoff_run > created or run.get("knowledge_mode") in {"historical_reference", "historical_reconstruction", "reconstructed", "review"}:
            mode = "historical_or_retrospective"
        elif run.get("app_mode") != "real":
            mode = "environment_unverified"
        elif not adjacent:
            mode = "source_target_alignment_unverified"
        else:
            mode = "prospective_received_cache"
        modes[mode] += 1
        references, valid_ids, strict = _reference_status(head, records["evidence"], cutoff_run)
        strict_refs += strict
        ref_total += len(references)
        ref_valid += len(valid_ids)
        narrative_count = max(0, int(head.get("analysis_count") or 0))
        claims += narrative_count
        for refs in head.get("analysis_refs") or []:
            declared = {str(ref) for ref in refs} if isinstance(refs, list) else set()
            claimed += int(bool(declared & valid_ids))
            all_claimed += int(bool(declared) and declared <= valid_ids)
        prior_refs, prior_valid, _ = _reference_status(prior_report or {}, records["evidence"], cutoff_run)
        eligible = (mode == "prospective_received_cache" and target in days and prior_report.get("core_ready") is True
                    and prior_report.get("target_verified") is True and prior_refs and len(prior_valid) == len(prior_refs))
        if eligible:
            eligible_days.add(target)
            if any((rev := revision_index.get(conf["report_id"])) and rev["run_id"] == run["id"]
                   and _timestamp(conf.get("created_at")) and _timestamp(conf["created_at"]) < deadline
                   for conf in records["confirmations"]):
                confirmed_days.add(target)
        chosen = confirmed_by_run.get(run["id"], head)
        for scenario in chosen.get("scenarios") or []:
            if not isinstance(scenario, dict):
                continue
            conditions["total"] += 1
            method = scenario.get("verification_method")
            bucket = "manual_observation" if method in MANUAL_METHODS else "declared_machine_method" if method in MACHINE_METHODS else "method_unclassified"
            conditions[bucket] += 1
            conditions["confirmed_plan"] += int(chosen["id"] in confirmed)
            checks = [item for item in scenario.get("next_checks") or [] if isinstance(item, dict)]
            conditions["structured_next_checks"] += int(bool(checks))
            sufficient = bool(checks) and all(all(isinstance(item.get(key), str) and item[key].strip()
                                                  for key in ("subject", "window", "support", "invalidation"))
                                             and not item.get("missing_data") for item in checks)
            conditions["actionable_manual_observation"] += int(method in MANUAL_METHODS and sufficient)
            conditions["necessary_data_missing"] += int(any(item.get("missing_data") for item in checks) or scenario.get("status") == "insufficient_data")
        observations.append({"run_id": str(run["id"]), "target_trade_day": str(target) if target else None,
                             "mode": mode, "eligible_shadow_sample": bool(eligible),
                             "core_ready": head.get("core_ready"), "report_id": str(head["id"])})
    chosen_reports = {row["id"] for row in confirmed_by_run.values()} | {row["id"] for run_id, row in heads.items() if run_id not in confirmed_by_run}
    latest_validations = {}
    for validation in sorted(records["validations"], key=lambda item: (_timestamp(item.get("created_at")) or cutoff, str(item.get("id"))), reverse=True):
        key = (validation["report_id"], validation["scenario_id"])
        if validation["report_id"] in chosen_reports:
            latest_validations.setdefault(key, validation)
    for validation in latest_validations.values():
        validation_states[validation.get("status", "unknown")] += 1
    reasons = Counter(str(row.get("reason") or "未记录原因") for row in records["revisions"] if row.get("author_type") == "manual")
    ordered = sorted(durations)
    middle = (Decimal(ordered[len(ordered) // 2]) if len(ordered) % 2 else Decimal(ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2) if ordered else None
    limitations = ["这是研究流程影子评估，不计算回测收益、实盘收益、胜率或交易有效性。",
                   "引用覆盖只证明绑定了可解析且当前可用的证据，不证明论断语义或预测正确。",
                   "历史补采或目标日之后生成的报告不计入提前保存的影子样本；received_cache不等于历史observed证明。",
                   "未标记运行环境的旧记录保持未知；演示及合成资料不能证明实盘表现。",
                   "合格样本要求来源日为目标日之前的最近共同交易日，避免把过期行情当成明日预案。",
                   "状态变迁未完整保存历史；晚于评估截止的完成结果、费用和用量不计入当时统计。",
                   "具体价位规则仍需确认，人工观察条件数量不代表自动可执行交易。"]
    if not days:
        limitations.append("缺少可核验的已收盘共同交易日历，不以工作日替代交易日。")
    if records.get("truncated"):
        limitations.append("窗口内运行记录超过只读扫描上限，结果为部分统计，不能标记20日验收完成。")
    complete = len(eligible_days) >= requested and not records.get("truncated")
    return normalized({"format": "tianyan.tomorrow.shadow-evaluation.v1", "as_of_time": cutoff,
        "window": {"requested_trading_days": requested, "verified_closed_trading_days": [str(day) for day in days],
                   "actual_calendar_days": len(days), "basis": "目标交易日，最近已收盘的沪深共同交易日"},
        "progress": {"covered_target_days": sorted(map(str, covered_days)), "covered_target_day_count": len(covered_days),
                     "eligible_shadow_target_days": sorted(map(str, eligible_days)), "eligible_shadow_target_day_count": len(eligible_days),
                     "confirmed_before_target_day_count": len(confirmed_days), "goal": requested,
                     "observation_window_complete": complete,
                     "acceptance_status": "requires_human_quality_review" if complete else "insufficient_observation_days",
                     "timing_policy": "报告及输入截止均在目标日09:15之前；仅为评估政策，不替代实际市场时段规则"},
        "runs": {"count": len(runs), "states": dict(states), "knowledge_modes": dict(modes), "scan_truncated": bool(records.get("truncated"))},
        "reports": {"latest_count": len(heads), "confirmed_latest_count": sum(head["id"] in confirmed for head in heads.values()),
                    "draft_latest_count": sum(head["id"] not in confirmed for head in heads.values()),
                    "runs_with_confirmed_version": len(confirmed_by_run),
                    "audit_passed_latest_count": sum(head.get("audit_passed") is True for head in heads.values())},
        "data_quality": {key: quality[key] for key in ("without_report", "core_data_missing", "core_data_unknown", "target_day_unverified", "source_target_alignment_unverified")},
        "evidence": {"declared_reference_occurrences": ref_total, "available_reference_occurrences": ref_valid,
                     "unavailable_reference_occurrences": ref_total - ref_valid,
                     "strict_historical_eligible_reference_occurrences": strict_refs,
                     "reference_availability_pct": _percent(ref_valid, ref_total),
                     "narrative_paragraphs": claims, "paragraphs_with_available_reference": claimed,
                     "paragraphs_with_all_declared_references_available": all_claimed,
                     "narrative_reference_coverage_pct": _percent(all_claimed, claims)},
        "conditions": {**{key: conditions[key] for key in ("total", "manual_observation", "declared_machine_method", "method_unclassified",
                                                           "confirmed_plan", "structured_next_checks", "actionable_manual_observation", "necessary_data_missing")},
                       "machine_checkability_verified": None,
                       "validation_record_count": len(records["validations"]),
                       "latest_validation_count": len(latest_validations), "latest_statuses": dict(validation_states),
                       "scope": "每运行取最新已确认版本的条件；尚未确认时取最新草稿，历史验证保留独立计数",
                       "checkability_policy": "人工可检查要求对象、时段、支持/否定及无已知缺数据；machine方法标签未经实际规则执行验证，不当作自动可执行"},
        "manual_revisions": {"count": sum(reasons.values()), "reason_counts": [{"reason": reason, "count": count} for reason, count in reasons.most_common(20)]},
        "model_usage": _usage(records["attempts"], cutoff),
        "timing": {"completed_runs": len(ordered), "end_to_end_ms_total": sum(ordered),
                   "end_to_end_ms_median": _text(middle), "end_to_end_ms_p95": ordered[math.ceil(len(ordered) * .95) - 1] if ordered else None,
                   "scope": "实际created_at到completed_at，含排队及阶段等待；未完成不估算"},
        "observations": observations, "limitations": limitations})


def shadow_evaluation(db, owner_id, as_of_time=None, limit=20):
    """Aggregate saved owner-scoped records without collection or model calls."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 60:
        raise APIError(422, "evaluation_limit_invalid", "评估窗口需为1至60个交易日。")
    current = now(db)
    cutoff = as_of_time or current
    if not isinstance(cutoff, datetime) or cutoff.utcoffset() is None:
        raise APIError(422, "evaluation_cutoff_invalid", "评估截止需包含时区。")
    if cutoff > current:
        raise APIError(422, "future_cutoff", "评估截止不能晚于服务器时间。")
    calendar = sorted(set(available_trade_days(db, limit=limit + 1, end_day=cutoff.astimezone(SHANGHAI).date(), as_of_time=cutoff)))
    days = calendar[-limit:]
    records = _load_records(db, owner_id, cutoff, days)
    records["known_closed_trade_days"] = calendar
    return _summarize(records, cutoff, days, limit)
