"""Short database transactions around immutable computations and existing AI jobs."""
import json
import re
import uuid

from sqlalchemy import func, null, select, text
from sqlalchemy.orm import Session

from app.ai.models import AiRun
from app.auth import APIError
from app.config import settings
from app.db import engine
from app.evidence.common import content_hash, lock, normalized, now
from app.evidence.models import Snapshot, SnapshotItem, SourceVersion
from app.models import Job, User
from app.tomorrow.codec import json_digest
from app.tomorrow.models import PlanReport, PlanRun, PlanSnapshot
from app.tomorrow.service import ROLES, completed_results, permitted, save_report

ROLE_NAMES = {'market_events': '市场与事件研究', 'sectors_stocks': '板块与标的研究',
              'conditional_plan': '条件预案研究', 'review_write': '全文审查与写作'}
MAX_STAGE_BYTES = 320000
STOCK_BATCH_SIZE = 15


def workflow_steps(run):
    offsets = range(0, max(1, min(30, len((run.bundle or {}).get('stocks') or []))), STOCK_BATCH_SIZE)
    return ([('market_events', 0)] + [('sectors_stocks', offset) for offset in offsets]
            + [('conditional_plan', offset) for offset in offsets] + [('review_write', 0)])


def fence(db, job):
    row = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running',
        Job.lease_token == job.lease_token, Job.lease_until > func.clock_timestamp()).with_for_update())
    if not row:
        raise APIError(409, 'lease_lost', '任务租约已变化，旧执行结果未写入。')
    return row


def snapshot(db, run, refs):
    visible = permitted(db, run.owner_id, [r['id'] for r in refs], run.as_of_time)
    strict_refs = [r for r in refs if (e := visible[r['id']][0]).historical_eligible
                   and e.available_at is not None and e.available_at <= run.as_of_time
                   and (e.event_at is None or e.event_at <= run.as_of_time)]
    legacy = strict_snapshot(db, run, strict_refs) if strict_refs else None
    manifest = dict(format='tianyan.cache-research-snapshot.v1', owner_id=str(run.owner_id),
        knowledge_mode='received_cache', as_of_time=run.as_of_time.isoformat(), source_trade_day=str(run.source_trade_day),
        target_trade_day=str(run.target_trade_day) if run.target_trade_day else None,
        historical_claim='只证明本次接收截止前已存，不证明来源交易日当时已知。',
        items=[dict(r, historical_eligible=visible[r['id']][0].historical_eligible) for r in refs])
    row = PlanSnapshot(id=uuid.uuid4(), owner_id=run.owner_id, as_of_time=run.as_of_time,
        manifest=normalized(manifest), content_hash=content_hash(manifest),
        legacy_snapshot_id=legacy.id if legacy else None, created_at=now(db))
    db.add(row)
    db.flush()
    run.snapshot_id = row.id
    return row


def strict_snapshot(db, run, refs):
    from app.evidence.schemas import Manifest
    versions = []
    for source_id in sorted({r['source_id'] for r in refs}):
        version = db.scalar(select(SourceVersion).where(SourceVersion.source_id == uuid.UUID(source_id),
            SourceVersion.recorded_at <= run.as_of_time).order_by(SourceVersion.recorded_at.desc()).limit(1))
        if version:
            versions.append(dict(version_id=version.id, recorded_at=version.recorded_at,
                                 content_hash=version.content_hash, descriptor=version.descriptor))
    manifest = normalized(Manifest(format='tianyan.snapshot.v1', app_mode=settings().app_mode,
        owner_id=run.owner_id, as_of_time=run.as_of_time, knowledge_mode='observed', required_capabilities=[],
        source_keys=None, sources=versions,
        quality={'status': 'partial', 'reasons': run.limitations},
        items=[dict(evidence_version_id=r['id'], content_hash=r['content_hash']) for r in refs]))
    row = Snapshot(id=uuid.uuid4(), owner_id=run.owner_id, as_of_time=run.as_of_time,
                   recorded_at=now(db), knowledge_mode='observed', state='pending', selection=manifest,
                   manifest=null(), manifest_hash=None)
    db.add(row)
    db.flush()
    for ref in refs:
        db.add(SnapshotItem(snapshot_id=row.id, evidence_version_id=uuid.UUID(ref['id']), content_hash=ref['content_hash']))
    db.flush()
    row.manifest, row.manifest_hash, row.state = manifest, content_hash(manifest), 'ready'
    db.flush()
    return row


def project_input(db, run, role, stock_offset=0):
    """Only objects whose entire evidence set is approved may cross the model boundary."""
    bundle = run.bundle
    selected = set(run.config_snapshot.get('profile', {}).get('ai_source_ids') or [])
    from app.tomorrow.models import PlanSettings
    live = db.get(PlanSettings, run.owner_id)
    if live is None or set(live.profile.get('ai_source_ids') or []) != selected:
        raise APIError(409, 'ai_material_permission_changed', '研究期间允许处理的来源变化，后续模型请求已停止。')
    refs = bundle.get('evidence_refs') or []
    allowed = {r['id']: r for r in refs if r['source_id'] in selected}
    visible = permitted(db, run.owner_id, list(allowed), run.as_of_time)
    allowed = {key: r for key, r in allowed.items() if key in visible and visible[key][0].content_hash == r['content_hash']}
    identifiers = set(allowed)
    catalog, evidence_groups, groups_by_hash = {}, {}, {}
    object_count = 0

    def group_for(references):
        ordered = sorted(set(references))
        digest = content_hash(ordered)
        if digest not in groups_by_hash:
            key = 'g' + str(len(evidence_groups))
            groups_by_hash[digest] = key
            evidence_groups[key] = ordered
        return groups_by_hash[digest]

    def facts(value, path, references, object_name, budget):
        if budget[0] <= 0:
            return
        if isinstance(value, dict):
            own_refs = value.get('evidence')
            if isinstance(own_refs, list) and own_refs:
                references = [str(v['id']) for v in own_refs if isinstance(v, dict) and v.get('id')]
            elif isinstance(value.get('row_reference'), dict) and value['row_reference'].get('id'):
                references = [str(value['row_reference']['id'])]
            elif value.get('evidence_refs'):
                references = [str(ref) for ref in value['evidence_refs'] if str(ref) in identifiers]
            priority = ('name', 'code', 'close', 'pct_chg', 'facts', 'calculations', 'indices', 'breadth', 'limits', 'relay')
            for key in sorted(value, key=lambda k: (priority.index(k) if k in priority else len(priority), k)):
                if key not in {'evidence', 'evidence_refs', 'row_reference', 'cached_references', 'member_codes',
                               'source_row_ranges', 'formula', 'rule_version', 'input_windows', 'source_trade_day',
                               'history_trade_days', 'content', 'text', 'summary', 'limitations', 'usage', 'id'}:
                    facts(value[key], path + '.' + key, references, object_name, budget)
                if budget[0] <= 0:
                    break
        elif isinstance(value, list):
            for i, child in enumerate(value[:8]):
                facts(child, path + '.' + str(i), references, object_name, budget)
                if budget[0] <= 0:
                    break
        elif value is not None and re.search(r'\d', str(value)) and len(str(value)) < 160 and references:
            if len(catalog) < 1200:
                catalog[path] = dict(value=value, object=object_name, evidence_group=group_for(references))
                budget[0] -= 1

    def compact(value):
        if isinstance(value, list):
            return [compact(v) for v in value[:30]]
        if isinstance(value, dict):
            return {k: compact(v) for k, v in value.items()
                    if k not in {'evidence', 'evidence_refs', 'source_row_ranges', 'sample', 'samples',
                                 'cohort', 'constituents', 'member_codes', 'cached_references', 'net_amount_series',
                                 'relative_to_sectors', 'lhb_seats'}}
        if isinstance(value, str) and len(value) > 3000:
            return value[:3000] + '（研究输入节选，完整正文仍保存在原始证据）'
        return value

    def research_view(value):
        if 'candidate_reasons' in value:
            calculation = value.get('calculations') or {}
            fundamental = value.get('fundamentals') or {}
            chosen_facts = {key: row for key, row in (value.get('facts') or {}).items()
                            if key in {'close', 'pct_chg', 'amount', 'high', 'low', 'up_limit', 'down_limit'}}
            pool = (value.get('facts') or {}).get('pool') or {}
            if pool:
                chosen_facts['pool'] = dict(value={key: val for key, val in (pool.get('value') or {}).items()
                    if key in {'limit', 'limit_times', 'first_time', 'last_time', 'open_times'}}, evidence=pool.get('evidence') or [])
            return dict(code=value.get('code'), name=value.get('name'), candidate_reasons=value.get('candidate_reasons'),
                facts=chosen_facts,
                calculations=dict(returns_pct={key: {k: v for k, v in row.items() if k in
                    {'value', 'unit', 'state', 'adjustment', 'evidence'}} for key, row in (calculation.get('returns_pct') or {}).items()},
                    **{key: calculation.get(key) for key in ('support_resistance', 'volume_to_prior5_mean',
                        'price_verified_limit_up', 'price_verified_limit_down')}),
                sectors=(value.get('sectors') or [])[:10],
                fundamentals=dict(valuation=(fundamental.get('valuation') or [])[:1],
                    financials={key: rows[:1] for key, rows in (fundamental.get('financials') or {}).items()},
                    business=(fundamental.get('business') or [])[:3], state=fundamental.get('state')),
                fund_flows={key: rows[:5] for key, rows in (value.get('fund_flows') or {}).items()},
                limitations=value.get('limitations') or [])
        return value

    def project(value, fact_budget=24):
        nonlocal object_count
        if not isinstance(value, dict):
            return None
        required = {str(v) for v in value.get('evidence_refs') or []}
        if not required or not required <= identifiers:
            return None
        if value.get('kind') in {'news', 'announcement'} and not value.get('content_sufficient'):
            return None
        # Raw tables have a separate authorized retrieval path. AI receives derived objects, not every cached row.
        object_key = 'o' + str(object_count)
        object_count += 1
        view = research_view(value)
        primary = (value.get('facts') or {}).get('close') or (value.get('facts') or {}).get('pct_change') or {}
        primary_refs = [ref['id'] for ref in primary.get('evidence') or []]
        facts(view, object_key, primary_refs or sorted(required), value.get('name') or value.get('code') or '市场', [fact_budget])
        return dict(compact(view), evidence_group=group_for(primary_refs or required), object_key=object_key)

    def batch(rows, limit, budget):
        result = []
        if limit <= 0:
            return result
        for row in rows or []:
            if (item := project(row, budget)) is not None:
                result.append(item)
                if len(result) >= limit:
                    break
        return result

    material = dict(research_role=role, source_trade_day=bundle['source_trade_day'],
                    target_trade_day=bundle.get('target_trade_day'), as_of_time=bundle['as_of_time'],
                    rule_version=bundle.get('rule_version'), limitations=bundle.get('limitations', []),
                    approved_source_ids=sorted(selected))
    if role in {'market_events', 'review_write'}:
        material['market'] = project(bundle.get('market'), 80)
    if role in {'market_events', 'sectors_stocks', 'conditional_plan', 'review_write'}:
        material['sectors'] = batch(bundle.get('sectors'), 5 if role == 'review_write' else 20 if role == 'market_events' else 10, 12)
        material['events'] = batch(bundle.get('events'), 10 if role == 'review_write' else 40 if role == 'market_events' else 20 if role == 'sectors_stocks' else 0, 4)
    if role in {'sectors_stocks', 'conditional_plan', 'review_write'}:
        stock_limit = 5 if role == 'review_write' else STOCK_BATCH_SIZE
        material['stocks'] = batch((bundle.get('stocks') or [])[stock_offset:stock_offset + stock_limit], stock_limit, 24)
        material['candidate_batch'] = dict(offset=stock_offset, limit=stock_limit,
            total_recalled=len(bundle.get('stocks') or []), research_cap=30)
    material['prior_stages'] = [dict(role=stage['role'], items=[item for item in stage['result'].get('items') or []
        if set(item.get('evidence_refs') or []) <= identifiers]) for stage in completed_results(db, run)]
    # Shared evidence groups keep multi-session numerical lineage out of repeated context copies.
    material['fact_catalog'] = catalog
    material['evidence_groups'] = evidence_groups
    if role == 'review_write':
        from app.tomorrow.render import build_report, visible_content
        head = db.scalar(select(PlanReport).where(PlanReport.run_id == run.id)
                         .order_by(PlanReport.revision_number.desc()).limit(1))
        draft = build_report(run.bundle, run.config_snapshot.get('profile') or {},
            judgments=head.payload.get('judgments', []) if head else [], ai_results=completed_results(db, run))
        if head and head.author_type == 'manual':
            draft['summary'] = head.payload['summary']
        draft['limitations'] = list(dict.fromkeys(draft['limitations'] + run.limitations))
        visible = visible_content(draft)
        material['visible_draft_hash'] = content_hash(visible)
        sections = [section for section in visible['sections'] if set(section['evidence_refs'] or []) <= identifiers]
        scenarios = [row for row in visible['scenarios'] if row.get('next_checks')]
        permitted_scenarios = [row for row in scenarios if set(row.get('evidence_refs') or []) <= identifiers]
        def audit_view(value):
            if isinstance(value, list):
                return [audit_view(item) for item in value]
            if isinstance(value, dict):
                return {key: group_for(child) if key == 'evidence_refs' and child else audit_view(child)
                        for key, child in value.items()}
            return value
        material['visible_draft'] = audit_view(dict(summary=visible['summary'], sections=sections,
            scenarios=permitted_scenarios, core_directions=visible['core_directions'], risks=visible['risks'],
            limitations=visible['limitations']))
        material['program_checks'] = dict(numeric_tables='来自同一冻结底稿，由程序渲染；模型不能改写',
            unresearched_scenario_count=len(visible['scenarios']) - len(scenarios),
            unresearched_scenario_state='统一标记为数据不足，不作为可执行条件',
            audit_scope='AI核对全部实际正文与已研究条件；数值表格及未研究模板由程序检查，完整投影散列同时绑定')
        material['audit_scope_complete'] = len(sections) == len(visible['sections']) and len(scenarios) == len(permitted_scenarios)
    material['input_limits'] = '模型深研最多30只候选，每批15只独立研究与提出条件；完整规则候选及指标另行保留。终审检查实际正文与条件，数字表格由程序检查。'
    if not any(material.get(key) for key in ('market', 'sectors', 'stocks', 'events')) or not allowed:
        raise APIError(409, 'ai_material_unavailable', '没有完整且获准的本阶段研究材料，请在预案设置选择可处理来源。')
    # Prevent an unbounded source page from exhausting model context; never silently truncate individual claims.
    used = {ref for group in evidence_groups.values() for ref in group}
    used |= {ref for stage in material['prior_stages'] for item in stage['items'] for ref in item.get('evidence_refs') or []}
    references = [{key: ref[key] for key in ('id', 'content_hash', 'source_id', 'source_name', 'received_at', 'source_time') if key in ref}
                  for identity, ref in allowed.items() if identity in used]
    budget = 480000 if role == 'review_write' else MAX_STAGE_BYTES
    if len(json.dumps(dict(input=material, evidence_refs=references), ensure_ascii=False).encode()) > budget:
        raise APIError(409, 'ai_material_too_large', '本阶段材料超出输入预算，已保留草稿及实际范围，未发送过大的模型请求。')
    return normalized(material), references


def queue_stage(db, run, role, stock_offset=0):
    from app.ai.service import resolve_feature
    connection, _, config = resolve_feature(db, 'plan_generation', run.owner_id)
    if config != run.config_snapshot.get('ai'):
        raise APIError(409, 'configuration_changed', '排队后模型配置变化，已保留草稿，请用新配置重新生成。')
    material, refs = project_input(db, run, role, stock_offset)
    digest = content_hash(dict(input=material, evidence=refs))
    dedup = content_hash(dict(parent=str(run.id), role=role, input_hash=digest, config=config))
    lock(db, f'tomorrow:stage:{dedup}')
    ai = db.scalar(select(AiRun).where(AiRun.dedup_key == dedup))
    if ai is None:
        job_id, ai_id = uuid.uuid4(), uuid.uuid4()
        current = now(db)
        db.add(Job(id=job_id, owner_id=run.owner_id, kind='ai.generate', state='queued', attempts=0,
                   payload={'run_id': str(ai_id)}, available_at=current, created_at=current))
        db.flush()
        ai = AiRun(id=ai_id, job_id=job_id, owner_id=run.owner_id, feature='plan_generation',
            state='queued', stage=ROLE_NAMES[role], connection_id=connection.id, model=connection.model,
            trade_day=run.source_trade_day, as_of_time=run.as_of_time, config_snapshot=config,
            input_hash=digest, dedup_key=dedup, input_data=material, evidence_refs=refs,
            limitations=run.limitations, created_at=current)
        db.add(ai)
        db.flush()
    run.stages = (run.stages or []) + [dict(role=role, stock_offset=stock_offset, state=ai.state, ai_run_id=str(ai.id), input_hash=digest)]
    run.state, run.stage = 'waiting_ai', ROLE_NAMES[role]


def enrich_scenarios(bundle):
    """Explicit human checkpoints are not silently converted to automatic trading signals."""
    if bundle.get('scenarios'):
        return
    scenarios = []
    for stock in bundle.get('stocks') or []:
        code = stock.get('code') or stock.get('ts_code')
        if not code:
            continue
        scenarios.append(dict(id=f'{code}:manual', object_type='stock', object_id=code,
            label='条件待研究', target_trade_day=bundle.get('target_trade_day'),
            observation_window='09:25–09:59', status='insufficient_data',
            trigger='尚未形成可验证的对象条件，不能据此判断触发。',
            confirmation='补齐具体观察对象、支持及否定条件后，由实际开盘资料或人工记录确认。',
            invalidation='尚无已确认算法或人工条件，不能自动判断失效。',
            risk_veto=['交易状态不适用', '重大风险未排除', '必要数据不足'],
            required_data=['具体条件', '目标日开盘观察记录'], next_checks=[],
            verification_method='manual', evidence_refs=stock.get('evidence_refs') or []))
    bundle['scenarios'] = scenarios


def process_build(job):
    try:
        with Session(engine(), expire_on_commit=False) as db, db.begin():
            # Stable read, all inputs additionally bounded by the original receive cutoff.
            db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
            run = db.scalar(select(PlanRun).where(PlanRun.job_id == job.id))
            if run is None:
                raise APIError(409, 'run_missing', '预案任务不存在。')
            owner = db.get(User, run.owner_id)
            if not owner or not owner.enabled or owner.role not in {'admin', 'researcher'}:
                raise APIError(409, 'owner_permission_changed', '账号权限变化，任务停止。')
            from app.tomorrow.data import build_bundle
            bundle = run.bundle or build_bundle(db, run.owner_id, run.source_trade_day, run.target_trade_day, run.as_of_time,
                additional_codes=[h['code'] for h in run.config_snapshot.get('profile', {}).get('holdings') or []])
            bundle['source_trade_day'] = run.source_trade_day.isoformat()
            bundle['target_trade_day'] = run.target_trade_day.isoformat() if run.target_trade_day else None
            bundle['as_of_time'] = run.as_of_time.isoformat()
            enrich_scenarios(bundle)
        with Session(engine()) as db, db.begin():
            current = fence(db, job)
            run = db.scalar(select(PlanRun).where(PlanRun.job_id == job.id).with_for_update(key_share=True))
            if run.bundle is None:
                refs = bundle.get('evidence_refs') or []
                visible = permitted(db, run.owner_id, [r['id'] for r in refs], run.as_of_time)
                if len(visible) != len({r['id'] for r in refs}):
                    raise APIError(409, 'input_permission_changed', '输入来源变化，未保存研究结果。')
                run.limitations = list(dict.fromkeys(run.limitations + bundle.get('limitations', [])))
                frozen = snapshot(db, run, refs)
                bundle['snapshot_id'] = str(frozen.id)
                run.bundle, run.input_hash = bundle, json_digest(bundle)
            from app.tomorrow.render import build_report
            previous = db.get(PlanReport, run.base_report_id) if run.base_report_id else None
            previous_payload = dict(previous.payload, id=str(previous.id)) if previous else None
            payload = build_report(run.bundle, run.config_snapshot.get('profile') or {}, previous=previous_payload)
            payload['limitations'] = list(dict.fromkeys(payload['limitations'] + run.limitations))
            save_report(db, run, payload)
            if run.use_ai and 'ai' in run.config_snapshot and bundle.get('ready'):
                try:
                    queue_stage(db, run, ROLES[0])
                except APIError as error:
                    run.state, run.stage, run.error_code = 'partial', '资料草稿可用，AI待处理', error.code
                    run.limitations = run.limitations + [error.message]
            else:
                run.state, run.stage = ('succeeded', '规则资料草稿已完成') if bundle.get('ready') else ('blocked', '核心行情资料不足')
            if run.state != 'waiting_ai':
                run.completed_at = now(db)
            current.state, current.completed_at = 'succeeded', now(db)
            current.result_ref = {'tomorrow_run_id': str(run.id)}
            current.lease_token = current.lease_until = None
    except APIError as error:
        from app.worker import finish
        with Session(engine()) as db, db.begin():
            try:
                fence(db, job)
            except APIError:
                return
            run = db.scalar(select(PlanRun).where(PlanRun.job_id == job.id))
            if run:
                run.state, run.stage, run.error_code = 'failed', error.message[:120], error.code
                run.completed_at = now(db)
        finish(job.id, job.lease_token, error_code=error.code)


def advance_runs():
    with Session(engine()) as db, db.begin():
        stopped = db.execute(select(PlanRun, Job).join(Job, Job.id == PlanRun.job_id).where(
            PlanRun.state == 'queued', Job.state.in_(['failed', 'cancelled'])).with_for_update(of=PlanRun, skip_locked=True).limit(10)).all()
        for run, job in stopped:
            run.state, run.error_code, run.completed_at = job.state, job.error_code or 'build_stopped', now(db)
            run.stage = '准备或计算任务已停止，已有资料仍保留'
        runs = db.scalars(select(PlanRun).where(PlanRun.state == 'waiting_ai')
                          .order_by(PlanRun.created_at).with_for_update(key_share=True, skip_locked=True).limit(1)).all()
        for run in runs:
            owner = db.get(User, run.owner_id)
            if not owner or not owner.enabled or owner.role not in {'admin', 'researcher'}:
                run.state, run.error_code, run.completed_at = 'cancelled', 'owner_permission_changed', now(db)
                continue
            if not run.stages:
                continue
            step = run.stages[-1]
            ai = db.get(AiRun, uuid.UUID(step['ai_run_id']))
            if ai is None or ai.state in {'queued', 'running', 'retry_wait'}:
                continue
            run.stages = [dict(s, state=ai.state) if s['ai_run_id'] == str(ai.id) else s for s in run.stages]
            steps = workflow_steps(run)
            if ai.state == 'succeeded' and len(run.stages) < len(steps):
                try:
                    queue_stage(db, run, *steps[len(run.stages)])
                    continue
                except APIError as error:
                    run.error_code = error.code
                    run.limitations = run.limitations + [error.message]
            elif ai.state != 'succeeded':
                run.error_code = ai.error_code or 'ai_stage_failed'
                run.limitations = run.limitations + [f"{ROLE_NAMES[step['role']]}未完成；保留已有结果，不自动重发未知请求。"]
            from app.tomorrow.render import build_report
            # Lock before reading the latest manual revision. NO KEY UPDATE on
            # the parent above still permits revision INSERT's FK KEY SHARE.
            lock(db, f'tomorrow:report:{run.id}')
            head = db.scalar(select(PlanReport).where(PlanReport.run_id == run.id)
                             .order_by(PlanReport.revision_number.desc()).limit(1))
            previous = db.get(PlanReport, run.base_report_id) if run.base_report_id else None
            payload = build_report(run.bundle, run.config_snapshot.get('profile') or {},
                judgments=head.payload.get('judgments', []) if head else [], ai_results=completed_results(db, run),
                previous=dict(previous.payload, id=str(previous.id)) if previous else None)
            if head and head.author_type == 'manual':
                payload['summary'] = head.payload['summary']
            payload['limitations'] = list(dict.fromkeys(payload['limitations'] + run.limitations))
            if ai.state == 'succeeded' and step['role'] == 'review_write':
                from app.tomorrow.render import visible_content
                actual = build_report(run.bundle, run.config_snapshot.get('profile') or {},
                    judgments=head.payload.get('judgments', []) if head else [],
                    ai_results=[r for r in completed_results(db, run) if r['role'] != 'review_write'],
                    previous=dict(previous.payload, id=str(previous.id)) if previous else None)
                if head and head.author_type == 'manual':
                    actual['summary'] = head.payload['summary']
                actual['limitations'] = list(dict.fromkeys(actual['limitations'] + run.limitations))
                same = content_hash(visible_content(actual)) == ai.input_data.get('visible_draft_hash')
                payload['audit_binding'] = dict(visible_content_hash=content_hash(visible_content(actual)),
                    audited_content_hash=ai.input_data.get('visible_draft_hash'), ai_run_id=str(ai.id),
                    input_hash=ai.input_hash, audit_scope_complete=ai.input_data.get('audit_scope_complete', False),
                    passed=same and ai.result.get('audit_status') == 'passed' and ai.input_data.get('audit_scope_complete', False))
                if not payload['audit_binding']['passed']:
                    run.error_code = 'research_audit_needs_review'
                    run.limitations = run.limitations + ['研究终审未覆盖或未通过当前可见内容，保留草稿供人工修订。']
                    payload['limitations'] = list(dict.fromkeys(payload['limitations'] + run.limitations))
            save_report(db, run, payload, 'ai')
            run.state = 'partial' if run.error_code else 'succeeded'
            run.stage, run.completed_at = '研究草稿已保存，请人工确认', now(db)


def validate_stage_result(run, result):
    placeholder = re.compile(r'\{\{fact:([A-Za-z0-9_.:-]+)\}\}')
    catalog = run.input_data.get('fact_catalog') or {}
    def render(part, references):
        if re.search(r'[0-9０-９]', placeholder.sub('', part)):
            raise APIError(422, 'unsupported_numeric_claim', '研究文本中的数字必须通过已绑定事实引用，不能自由补写。')
        if re.search(r'(?:百分之|千分之)[零〇一二两三四五六七八九十百千万亿]+|[零〇一二两三四五六七八九十百千万亿]+(?:元|亿元|万元|只|家|连板|天|日|成仓|倍|个百分点|年)', placeholder.sub('', part)):
            raise APIError(422, 'unsupported_numeric_claim', '中文数字也必须通过已绑定事实引用，不能自由补写。')
        if re.search(r'https?://|javascript:|<script', part, re.I):
            raise APIError(422, 'unsupported_link', '研究文本不能引入新的外部链接或可执行内容。')
        def replace(match):
            fact = catalog.get(match.group(1))
            required = run.input_data.get('evidence_groups', {}).get(fact.get('evidence_group'), []) if fact else []
            if fact is None or not required or not set(required) <= set(references):
                raise APIError(422, 'unknown_fact_reference', '模型引用的数值字段或来源不属于本次输入。')
            return f"{fact['object']}：{fact['value']}"
        return placeholder.sub(replace, part)
    result['summary'] = render(result['summary'], [ref for item in result['items'] for ref in item['evidence_refs']])
    result['audit_issues'] = [render(issue, [ref for item in result['items'] for ref in item['evidence_refs']])
                            for issue in result.get('audit_issues') or []]
    for item in result['items']:
        item['title'] = render(item['title'], item['evidence_refs'])
        item['text'] = render(item['text'], item['evidence_refs'])
        for check in item.get('next_checks') or []:
            for field in ('subject', 'support', 'invalidation', 'missing_data'):
                check[field] = render(check[field], item['evidence_refs'])
    role = run.input_data.get('research_role')
    identities = {'market': {'market', 'global'}}
    for kind, group in (('stock', 'stocks'), ('sector', 'sectors'), ('event', 'events')):
        identities[kind] = {str(value) for obj in run.input_data.get(group) or []
                            for key in ('id', 'code', 'ts_code') if (value := obj.get(key)) is not None}
    for item in result['items']:
        if item.get('object_type') is not None and item.get('object_id') not in identities.get(item['object_type'], set()):
            raise APIError(422, 'unknown_research_object', '研究对象不属于本阶段实际输入。')
        if role == 'conditional_plan':
            checks = item.get('next_checks') or []
            if not item.get('object_type') or not checks or not any(c['window'] == '09:25–09:59' for c in checks):
                raise APIError(422, 'opening_check_required', '条件预案需要实际对象及可在开盘时段验证的具体检查。')
            if any(c['support'].strip() in {'继续观察', '关注', '等待确认', '观察确认'} for c in checks):
                raise APIError(422, 'unverifiable_condition', '笼统观察不能代替可验证的支持与否定条件。')
    if role == 'review_write' and result.get('audit_status') not in {'passed', 'needs_review'}:
        raise APIError(422, 'research_audit_required', '终审阶段未返回当前可见稿的审查结论。')
    return result


def validate_ai_inputs(db, ai):
    from app.tomorrow.models import PlanSettings
    config = db.get(PlanSettings, ai.owner_id)
    current = set((config.profile if config else {}).get('ai_source_ids') or [])
    if current != set(ai.input_data.get('approved_source_ids') or []):
        raise APIError(409, 'ai_material_permission_changed', '允许AI处理的来源已变化，本阶段停止。')
    expected = {ref['id']: ref for ref in ai.evidence_refs}
    visible = permitted(db, ai.owner_id, list(expected), ai.as_of_time)
    if set(visible) != set(expected) or any(visible[key][0].content_hash != ref['content_hash'] for key, ref in expected.items()):
        raise APIError(409, 'input_permission_changed', '研究来源当前不可用，本阶段停止。')
