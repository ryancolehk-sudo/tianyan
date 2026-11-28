"""One admin action repairs only the sources and historical scopes used by the feature catalog."""
from collections import defaultdict
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.auth import APIError
from app.config import settings
from app.evidence.common import lock, now
from app.integrations import routing
from app.integrations.coverage_routes import follow_route, permitted, ready, route_receipt, select_verified
from app.integrations.coverage_schemas import CoverageRepairReport
from app.integrations.inspection import WAIT_REASONS, guidance, history_groups, source_finding
from app.integrations.inspection_repair import eligible, repair_history, repair_source, signature, verify_batch
from app.integrations.inspection_schemas import RepairBatch, RepairReceipt
from app.maintenance_control import admit
from app.market.protocol import SourceFailure
from app.market.recovery_models import RecoveryCoverage
from app.market.resilience import pending_job
from app.models import Job

URL = '/api/v1/data-center/coverage-repair'
RECEIPT = 'coverage.repair'


def historical_receipts(db, item, source, config, clock, allowance):
    if item.state in {'limited', 'manual'}:
        return [item]
    if not settings().data_collection_enabled or not settings().recovery_enabled:
        return [item.model_copy(update=dict(state='manual', advice='在本机启动器启用采集与历史补漏后，再核对这些日期。'))]
    _, _, dataset, state, reason = item.id.split(':', 4)
    # A run can span many symbols and dates. Keep the exact rows accepted in this bounded batch.
    # The existing recovery ledger remains responsible for the complete outstanding inventory.
    limit = 48
    rows = list(db.scalars(select(RecoveryCoverage).where(RecoveryCoverage.source_id == item.source_id,
        RecoveryCoverage.owner_id == config.owner_id, RecoveryCoverage.dataset == dataset,
        RecoveryCoverage.state == state, RecoveryCoverage.reason.is_not_distinct_from(reason or None))
        .order_by(RecoveryCoverage.checked_at, RecoveryCoverage.trade_day.desc(), RecoveryCoverage.id).limit(limit)))
    fixed = isolated(db, item, lambda: repair_history(db, item, clock, allowance)) if item.state == 'available' and allowance[0] else []
    if any(not r.coverage_id for r in fixed):
        return fixed
    seen = {r.coverage_id for r in fixed}
    stamp = signature(db, source, config)
    for row in rows:
        if row.id in seen:
            continue
        seen.add(row.id)
        fixed.append(item.model_copy(update=dict(id='history:' + str(row.id), title=item.title + ' · ' + str(row.trade_day),
            count=1, state='working' if row.state in {'queued', 'running'} else 'waiting', source_id=row.source_id,
            trade_day=row.trade_day, job_id=row.job_id, retry_at=row.next_retry_at, coverage_id=row.id,
            coverage_signature=row.configuration_sha256, configuration=stamp,
            advice='已跟踪该日和原采集范围，收到完整回执后自动核验。')))
    if item.count > len(seen):
        fixed.append(item.model_copy(update=dict(state='limited', count=item.count-len(seen),
            advice='其余日期保留在历史补漏队列，本次结果仅逐项跟踪本批范围。可打开历史记录查看进度。')))
    return fixed


def isolated(db, item, action):
    """A blocked source may not discard successful work for another feature."""
    try:
        with db.begin_nested():
            return action()
    except (APIError, SourceFailure) as error:
        reason = error.code if isinstance(error, APIError) else error.reason
        return [item.model_copy(update=dict(state='waiting' if reason in WAIT_REASONS else 'manual', reason=reason,
            advice=error.message if isinstance(error, APIError) else guidance(reason)))]
    except OperationalError as error:
        if getattr(error.orig, 'sqlstate', None) not in {'55P03', '40P01', '57014'}:
            raise
        return [item.model_copy(update=dict(state='waiting', advice='同一范围正在处理，稍后可重新核对。'))]


def collect(db, source, clock, *, gated=False):
    item = RepairReceipt(id='source:' + str(source.provider.id), title=source.provider.name, area='source',
                         source_id=source.provider.id, state='available', advice='核对当前配置的有效回执。')
    def run():
        eligible(db, item.source_id)
        if not source.collection_enabled:
            return [item.model_copy(update=dict(state='manual', advice='本机总采集已关闭，请在本机启动器启用采集后再修复。'))]
        if source.collection_state == 'waiting_focus':
            return [item.model_copy(update=dict(state='manual', advice='请先选择需要跟踪的股票，或在采集参数中登记标的范围。'))]
        pending = pending_job(db, item.source_id)
        finding = source_finding(db, source, clock, pending, None)
        result = RepairReceipt(**finding.model_dump()) if finding else item
        if result.state in {'manual', 'limited'}:
            return [result]
        if gated:
            return [result.model_copy(update=dict(state='waiting', job_id=None,
                advice='已登记单项检测，后台会在允许的采集窗口核验并选用。'))]
        return [repair_source(db, result, clock, source)]
    return isolated(db, item, run)[0]


def submit(db, owner, body, request_id=None):
    from app.system_settings.service import audit
    admit(db)
    lock(db, 'system:inspection:repair')
    db.execute(text("SET LOCAL lock_timeout = '2s'"))
    existing = routing._locked_routes(db, body.expected_revision)
    snapshot = routing.matrix(db)
    suppliers = {p['id']: p for p in snapshot['providers']}
    ports = {cap['id']: cap for cap in snapshot['capabilities'] if cap['routable']}
    states, configs, providers, _, _ = routing._source_snapshot(db)
    sources = {source.provider.id: source for source in states.values()}
    options = defaultdict(list)
    for cap in snapshot['capabilities']:
        for option in cap['options']:
            for ident in option['source_ids']:
                options[UUID(ident)].append(option)
    report = CoverageRepairReport(started_at=now(db), by_need={})
    actions, history_needs = {}, defaultdict(set)
    if len({t.need_id for t in body.targets}) != len(body.targets):
        raise APIError(422, 'duplicate_need', '同一数据需求只能提交一次。')

    def attach(need, item):
        actions[item.id] = item
        ids = report.by_need.setdefault(need, [])
        if item.id not in ids:
            ids.append(item.id)

    for target in body.targets:
        if target.gap:
            report.by_need[target.need_id] = []
        if not target.gap:
            continue
        for port in target.port_ids:
            if port not in ports:
                raise APIError(409, 'routing_changed', '功能的数据项已变化，请刷新后重试。')
            cap = ports[port]
            base = route_receipt(cap, None, 'manual', '请配置该端口的授权、采集范围或可用来源。')
            if base.id in actions:
                attach(target.need_id, actions[base.id])
                continue
            choices = [o for o in cap['options'] if permitted(o, suppliers[o['provider_id']])]
            ordered = [cap['effective_provider_id'], cap['current_provider_id'], *cap['fallback_provider_ids'], 'tushare']
            choices.sort(key=lambda o: ordered.index(o['provider_id']) if o['provider_id'] in ordered else len(ordered))
            proven = next((o for o in choices if ready(db, o)), None)
            if proven:
                name = suppliers[proven['provider_id']]['name']
                def apply():
                    nonlocal existing, snapshot
                    snapshot = select_verified(db, owner, cap, proven, existing, snapshot, request_id)
                    existing = routing.route_settings(db)
                    return [route_receipt(cap, proven['provider_id'], 'verified', '已核验并选用' + name + '。')]
                attach(target.need_id, isolated(db, base, apply)[0])
                continue
            candidates = [(o, UUID(s)) for o in choices for s in o['source_ids'] if UUID(s) in target.source_ids]
            for option, sid in candidates:
                source = sources[sid]
                gated = not routing.collection_allowed(db, source.family)
                item = actions.get('source:' + str(sid)) or collect(db, source, report.started_at, gated=gated)
                if not gated or item.state in {'manual', 'limited'}:
                    attach(target.need_id, item)
                if item.state not in {'working', 'waiting'} or not (gated or item.configuration):
                    continue
                base = follow_route(db, owner, cap, option, providers[source.family], configs[sid], existing,
                                    report.started_at, item, probe=gated)
                break
            attach(target.need_id, base)
        if target.port_ids:
            continue
        for sid in target.source_ids:
            if not any(permitted(o, suppliers[o['provider_id']]) for o in options[sid]):
                continue
            source = sources[sid]
            if not routing.collection_allowed(db, source.family):
                continue
            item = actions.get('source:' + str(sid)) or collect(db, source, report.started_at)
            attach(target.need_id, item)
            if item.state in {'waiting', 'working'}:
                break

    for target in body.targets:
        chosen = set()
        if target.gap:
            for action_id in report.by_need[target.need_id]:
                action = actions[action_id]
                if action.state not in {'verified', 'waiting', 'working'}:
                    continue
                if action.source_id:
                    chosen.add(action.source_id)
                elif action.area == 'capability':
                    option = next((o for o in ports[action.capability]['options'] if o['provider_id'] == action.reason), None)
                    if option:
                        chosen.update(UUID(s) for s in option['source_ids'])
        for sid in chosen or (target.source_ids[:1] if target.gap else target.source_ids):
            if sid in sources:
                history_needs[sid].add(target.need_id)
    allowance = [settings().recovery_jobs_per_round]
    findings, _ = history_groups(db, {sid: sources[sid] for sid in history_needs}, report.started_at)
    for finding in findings:
        item = RepairReceipt(**finding.model_dump())
        source = sources[item.source_id]
        results = historical_receipts(db, item, providers[source.family], configs[item.source_id], report.started_at, allowance)
        for result in results:
            for need in sorted(history_needs[item.source_id]):
                attach(need, result)
    report.actions = list(actions.values())
    # Queue workers handle only open jobs. This completed command is the durable user receipt,
    # including when its exact source jobs wait for a trading window beyond the idempotency TTL.
    db.add(Job(owner_id=owner.id, kind=RECEIPT, state='succeeded', payload={}, result_ref=report.model_dump(mode='json'),
               created_at=report.started_at, completed_at=now(db)))
    audit(db, owner, 'data.coverage.repair', resource_type='inspection')
    return report


def latest(db, owner):
    saved = db.scalar(select(Job.result_ref).where(Job.owner_id == owner.id, Job.kind == RECEIPT,
        Job.state == 'succeeded').order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
    if not saved:
        return None
    report = CoverageRepairReport.model_validate(saved)
    batch = RepairBatch(started_at=report.started_at, actions=[a for a in report.actions if a.area != 'capability'
        and (a.configuration or a.coverage_id)])
    checked = {a.id: a for a in verify_batch(db, owner, batch).actions}
    port_actions = [a for a in report.actions if a.area == 'capability']
    caps = {c['id']: c for c in routing.matrix(db)['capabilities']} if port_actions else {}
    for item in port_actions:
        cap = caps.get(item.capability)
        effective = next((o for o in cap['options'] if o['provider_id'] == cap['effective_provider_id'] and ready(db, o)), None) if cap else None
        if effective:
            checked[item.id] = item.model_copy(update=dict(state='verified', advice='当前选用来源已有有效数据回执。'))
        elif item.job_id:
            job = db.get(Job, item.job_id)
            if job and job.owner_id == owner.id:
                state = 'waiting' if job.state == 'queued' else 'working' if job.state == 'running' else 'manual'
                checked[item.id] = item.model_copy(update=dict(state=state, retry_at=job.available_at if state == 'waiting' else None,
                    advice=(job.result_ref or {}).get('advice') if job.state == 'failed' and job.result_ref else
                    '本次验证后仍缺少有效来源，请核对该端口。' if state == 'manual' else item.advice))
        elif item.state == 'verified':
            checked[item.id] = item.model_copy(update=dict(state='manual', advice='当前来源或有效回执已变化，请重新修复。'))
    report.actions = [checked.get(a.id, a) for a in report.actions]
    return report
