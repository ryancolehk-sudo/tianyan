"""Owner-scoped immutable reports and a durable staged research workflow."""
import uuid
from copy import deepcopy
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import load_only

from app.ai.models import AiRun
from app.auth import APIError
from app.evidence.common import content_hash, lock, normalized, now
from app.evidence.models import EvidenceVersion
from app.models import AuditEvent, Job, Provider
from app.tomorrow import schemas as s
from app.tomorrow.models import PlanConfirmation, PlanReport, PlanRun, PlanSettings, PlanValidation

SHANGHAI = ZoneInfo('Asia/Shanghai')
ROLES = ('market_events', 'sectors_stocks', 'conditional_plan', 'review_write')


def audit(db, owner, action, resource_id=None, request_id=None):
    db.add(AuditEvent(actor_id=owner.id, action='tomorrow.' + action, resource_type='tomorrow',
                      resource_id=resource_id, request_id=request_id, changed_field_names=[]))


def permitted(db, owner_id, identities, cutoff=None):
    if not identities:
        return {}
    current = cutoff or now(db)
    result = {}
    rows = db.execute(select(EvidenceVersion, Provider).join(Provider, Provider.id == EvidenceVersion.source_id)
        .options(load_only(EvidenceVersion.id, EvidenceVersion.source_id, EvidenceVersion.owner_id,
                           EvidenceVersion.visibility, EvidenceVersion.kind, EvidenceVersion.provenance,
                           EvidenceVersion.received_at, EvidenceVersion.available_at, EvidenceVersion.event_at,
                           EvidenceVersion.published_at, EvidenceVersion.content_hash, EvidenceVersion.historical_eligible,
                           raiseload=True))
        .where(EvidenceVersion.id.in_([uuid.UUID(str(v)) for v in identities]),
               EvidenceVersion.received_at <= current,
               or_(EvidenceVersion.owner_id == owner_id,
                   and_(EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared')))).all()
    for row, source in rows:
        if (row.published_at is not None and row.published_at > current) or (row.available_at is not None and row.available_at > current):
            continue
        original_manual = row.owner_id == owner_id and source.kind == 'manual' and row.provenance == 'manual'
        if original_manual or (source.enabled and source.configured and source.entitlement_status == 'verified'
                and all(source.rights.get(k) == 'allowed' for k in ('display', 'cache'))):
            result[str(row.id)] = (row, source)
    return result


def settings_view(db, owner_id):
    row = db.get(PlanSettings, owner_id)
    profile = deepcopy(row.profile) if row else {}
    sources = db.scalars(select(Provider).where(Provider.enabled.is_(True), Provider.configured.is_(True),
        Provider.entitlement_status == 'verified').order_by(Provider.name)).all()
    return dict(revision=row.revision if row else 0, enabled=row.enabled if row else False,
                use_ai=row.use_ai if row else True, cash=profile.get('cash'), holdings=profile.get('holdings', []),
                risk_policy=profile.get('risk_policy', s.RiskPolicy().model_dump()),
                ai_source_ids=profile.get('ai_source_ids', []),
                sources=[dict(id=str(p.id), name=p.name, ai_allowed=all(p.rights.get(k) == 'allowed' for k in ('display', 'cache')))
                         for p in sources], evening_time='22:30', overnight_time='08:30', timezone='Asia/Shanghai')


def update_settings(db, owner, body, request_id=None):
    lock(db, f'tomorrow:settings:{owner.id}')
    row = db.get(PlanSettings, owner.id)
    if body.expected_revision != (row.revision if row else 0):
        raise APIError(412, 'revision_conflict', '设置已更新，请先读取最新设置；你的输入仍可保留。')
    if row is None:
        row = PlanSettings(owner_id=owner.id, revision=0, enabled=False, use_ai=True, profile={})
        db.add(row)
    profile = deepcopy(row.profile)
    for field in ('cash', 'holdings', 'risk_policy', 'ai_source_ids'):
        if field in body.model_fields_set:
            profile[field] = normalized(getattr(body, field))
            if profile[field] is None and field != 'cash':
                profile[field] = s.RiskPolicy().model_dump(mode='json') if field == 'risk_policy' else []
    if body.enabled is not None:
        if body.enabled and not row.enabled:
            profile['schedule_enabled_at'] = now(db).isoformat()
        row.enabled = body.enabled
    if body.use_ai is not None:
        row.use_ai = body.use_ai
    row.profile, row.revision, row.updated_at = profile, row.revision + 1, now(db)
    audit(db, owner, 'settings.update', request_id=request_id)
    db.flush()
    return settings_view(db, owner.id), 'complete', []


def calendar_days(db, cutoff):
    from app.market.review_store import permitted_provider
    sets = []
    for family in ('tushare_calendar_sse', 'tushare_calendar_szse'):
        provider = permitted_provider(db, family, cutoff)
        days = {}
        if provider:
            rows = db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == provider.id,
                EvidenceVersion.kind == 'calendar', EvidenceVersion.received_at <= cutoff)
                .order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc())).all()
            for row in rows:
                p = row.payload
                raw = p.get('trade_day') or p.get('cal_date')
                try:
                    day = date.fromisoformat(raw) if '-' in raw else datetime.strptime(raw, '%Y%m%d').date()
                except (TypeError, ValueError):
                    continue
                if day not in days:
                    days[day] = p.get('is_open') in (True, 1, '1')
        sets.append({d for d, opened in days.items() if opened})
    return sorted(sets[0] & sets[1])


def queue_run(db, owner, body, request_id=None, *, schedule_key=None):
    current = now(db)
    cutoff = body.as_of_time or current
    if cutoff > current:
        raise APIError(422, 'future_cutoff', '接收截止不能晚于服务器时间。')
    from app.market.review_store import available_trade_days
    available = available_trade_days(db, as_of_time=cutoff)
    source_day = body.source_trade_day or (max(available) if available else None)
    if source_day is None:
        raise APIError(422, 'trading_day_unavailable', '还没有可核验的已收盘交易日，请先准备资料。')
    if source_day > cutoff.astimezone(SHANGHAI).date():
        raise APIError(422, 'future_trade_day', '来源交易日不能晚于本次接收截止日期。')
    target = body.target_trade_day
    known_days = calendar_days(db, cutoff)
    if target is None:
        target = next((d for d in known_days if d > source_day), None)
    elif target <= source_day or target not in known_days:
        raise APIError(422, 'target_day_unverified', '目标日期需为来源日期之后的已核验共同交易日。')
    settings = db.get(PlanSettings, owner.id)
    profile = deepcopy(settings.profile) if settings else {}
    from app.config import settings as runtime_settings
    config = dict(profile=profile, settings_revision=settings.revision if settings else 0,
                  app_mode=runtime_settings().app_mode)
    limitations = []
    if target is None:
        limitations.append('未来交易日历不足，目标交易日待核验，草稿不能确认。')
    if body.use_ai:
        from app.ai.service import resolve_feature
        try:
            _, _, ai_config = resolve_feature(db, 'plan_generation', owner.id)
            config['ai'] = ai_config
        except APIError as error:
            config['ai_unavailable'] = error.code
            limitations.append('AI尚未就绪：' + error.message + ' 已保留规则资料草稿。')
    # A scheduled slot is unique even if a model/profile changes while it is queued.
    material = dict(owner=str(owner.id), day=source_day, target=target, slot=body.slot,
                    cutoff=body.as_of_time, config=config, use_ai=body.use_ai)
    if body.as_of_time is None and not schedule_key:
        material['cache_watermark'] = db.scalar(select(func.max(EvidenceVersion.received_at)).where(
            EvidenceVersion.received_at <= cutoff,
            or_(EvidenceVersion.owner_id == owner.id,
                and_(EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared'))))
    dedup = content_hash({'schedule': schedule_key} if schedule_key else material)
    if body.regenerate and not schedule_key:
        dedup = content_hash(dict(material=material, explicit_regeneration=str(uuid.uuid4())))
    lock(db, f'tomorrow:run:{owner.id}:{dedup}')
    previous = db.scalar(select(PlanRun).where(PlanRun.owner_id == owner.id, PlanRun.dedup_key == dedup))
    if previous:
        return run_view(db, previous), 'partial', ['相同研究请求已有记录，未重复排队。']
    run_id, job_id = uuid.uuid4(), uuid.uuid4()
    job = Job(id=job_id, owner_id=owner.id, kind='tomorrow.build', state='queued', attempts=0,
              payload={'run_id': str(run_id)}, available_at=current, created_at=current)
    db.add(job)
    db.flush()
    base_report = None
    if body.slot == 'overnight':
        base_report = db.scalar(select(PlanReport).join(PlanRun, PlanRun.id == PlanReport.run_id)
            .where(PlanRun.owner_id == owner.id, PlanRun.target_trade_day == target, PlanRun.slot != 'overnight',
                   PlanReport.created_at <= cutoff).order_by(PlanReport.created_at.desc()).limit(1))
    row = PlanRun(id=run_id, owner_id=owner.id, job_id=job_id, source_trade_day=source_day,
        target_trade_day=target, as_of_time=cutoff, slot=body.slot, state='queued', stage='等待冻结研究资料',
        use_ai=body.use_ai, dedup_key=dedup, bundle=None, stages=[], config_snapshot=normalized(config),
        limitations=limitations, preparation={}, base_report_id=base_report.id if base_report else None)
    db.add(row)
    db.flush()
    if body.prepare:
        from app.tomorrow.prepare import queue_preparation
        row.preparation = normalized(queue_preparation(db, owner.id, source_day, current))
        row.limitations = row.limitations + ['本次快照截止已固定；之后补采的资料请用新截止重新生成。']
    audit(db, owner, 'run.create', row.id, request_id)
    return run_view(db, row), 'partial', limitations


def run_view(db, row):
    report = db.scalar(select(PlanReport).where(PlanReport.run_id == row.id)
                       .order_by(PlanReport.revision_number.desc()).limit(1))
    result = s.RunView.model_validate(row).model_dump(mode='json')
    result['report_id'] = str(report.id) if report else None
    return result


def owned_run(db, owner_id, identity):
    row = db.scalar(select(PlanRun).where(PlanRun.id == identity, PlanRun.owner_id == owner_id))
    if row is None:
        raise APIError(404, 'not_found', '预案任务不存在或不可访问。')
    return row


def owned_report(db, owner_id, identity):
    row = db.scalar(select(PlanReport).where(PlanReport.id == identity, PlanReport.owner_id == owner_id))
    if row is None:
        raise APIError(404, 'not_found', '预案不存在或不可访问。')
    return row


def report_view(db, owner_id, identity):
    row = owned_report(db, owner_id, identity)
    refs = row.payload.get('evidence_refs') or []
    accessible = permitted(db, owner_id, [r['id'] for r in refs])
    if any(str(r['id']) not in accessible or accessible[str(r['id'])][0].content_hash != r['content_hash'] for r in refs):
        raise APIError(409, 'input_permission_changed', '报告引用的来源权限或版本已变化，原内容暂停展示。')
    confirmed = db.scalar(select(PlanConfirmation).where(PlanConfirmation.report_id == row.id))
    run = db.get(PlanRun, row.run_id)
    return dict(deepcopy(row.payload), id=str(row.id), run_id=str(row.run_id), revision_number=row.revision_number,
                based_on_id=str(row.based_on_id) if row.based_on_id else None, slot=run.slot,
                status='confirmed' if confirmed else 'draft', author_type=row.author_type,
                content_hash=row.content_hash, created_at=row.created_at.isoformat(),
                confirmed_at=confirmed.created_at.isoformat() if confirmed else None)


def save_report(db, run, payload, author_type='rule', based_on=None):
    from app.tomorrow.codec import json_digest
    lock(db, f'tomorrow:report:{run.id}')
    previous = db.scalar(select(PlanReport).where(PlanReport.run_id == run.id)
                         .order_by(PlanReport.revision_number.desc()).limit(1))
    digest = json_digest(payload)
    if previous and previous.content_hash == digest:
        return previous
    row = PlanReport(id=uuid.uuid4(), owner_id=run.owner_id, run_id=run.id,
                     revision_number=previous.revision_number + 1 if previous else 1,
                     based_on_id=based_on.id if based_on else previous.id if previous else None,
                     author_type=author_type, payload=payload, content_hash=digest, created_at=now(db))
    db.add(row)
    db.flush()
    return row


def revise(db, owner, identity, body, request_id=None):
    original = owned_report(db, owner.id, identity)
    lock(db, f'tomorrow:report:{original.run_id}')
    head = db.scalar(select(PlanReport).where(PlanReport.run_id == original.run_id)
                     .order_by(PlanReport.revision_number.desc()).limit(1))
    if head.id != original.id or body.expected_revision != head.revision_number:
        raise APIError(412, 'revision_conflict', '报告已有新修订，请读取最新内容；未覆盖你的输入。')
    report_view(db, owner.id, identity)
    run = db.get(PlanRun, original.run_id)
    allowed = {str(r['id']) for r in original.payload.get('evidence_refs') or []}
    for item in body.judgments:
        if not set(item.support_refs + item.refuting_refs) <= allowed:
            raise APIError(422, 'unknown_reference', '人工判断只能引用该冻结报告中的真实证据。')
        objects = {'stock': 'stocks', 'sector': 'sectors', 'event': 'events'}
        if item.object_type == 'market':
            known = item.object_id in {'market', 'global'}
        else:
            known = any(item.object_id in {str(obj.get(key)) for key in ('code', 'ts_code', 'id')}
                        for obj in run.bundle.get(objects[item.object_type]) or [])
        if not known:
            raise APIError(422, 'unknown_research_object', '人工判断的对象不属于这份冻结底稿。')
    # The editor submits the complete manual list, including an explicit empty
    # list after removal. A summary-only API revision preserves that list.
    submitted = [item.model_dump(mode='json') for item in body.judgments] if 'judgments' in body.model_fields_set else original.payload.get('judgments') or []
    judgments = {(j['object_type'], j['object_id']): j for j in submitted}
    from app.tomorrow.render import build_report
    payload = build_report(run.bundle, run.config_snapshot.get('profile') or {}, judgments=list(judgments.values()),
                           ai_results=completed_results(db, run))
    payload['summary'] = body.summary or original.payload['summary']
    payload['revision_reason'] = body.reason
    row = save_report(db, run, payload, 'manual', original)
    audit(db, owner, 'report.revise', row.id, request_id)
    return report_view(db, owner.id, row.id), 'partial' if payload['limitations'] else 'complete', []


def confirm(db, owner, identity, body, request_id=None):
    row = owned_report(db, owner.id, identity)
    lock(db, f'tomorrow:report:{row.run_id}')
    head = db.scalar(select(PlanReport).where(PlanReport.run_id == row.run_id)
                     .order_by(PlanReport.revision_number.desc()).limit(1))
    if head.id != row.id or body.expected_revision != row.revision_number:
        raise APIError(412, 'revision_conflict', '报告已更新，请确认当前修订。')
    view = report_view(db, owner.id, identity)
    if not view['dossier'].get('ready') or not view.get('target_trade_day') or not view['dossier'].get('target_trade_day_verified'):
        raise APIError(409, 'core_data_unavailable', '核心行情或目标交易日不足，暂不能确认。')
    if not db.scalar(select(PlanConfirmation).where(PlanConfirmation.report_id == row.id)):
        db.add(PlanConfirmation(owner_id=owner.id, report_id=row.id, created_at=now(db)))
    audit(db, owner, 'report.confirm', row.id, request_id)
    db.flush()
    return report_view(db, owner.id, identity), 'partial' if view['limitations'] else 'complete', []


def validate(db, owner, identity, body, request_id=None):
    view = report_view(db, owner.id, identity)
    if view['status'] != 'confirmed':
        raise APIError(409, 'plan_unconfirmed', '先确认原预案，再追加次日验证。')
    scenarios = {str(i.get('id') or i.get('scenario_id')): i for i in view['dossier'].get('scenarios') or []}
    if body.scenario_id not in scenarios:
        raise APIError(422, 'scenario_not_found', '验证对象不属于这份原预案。')
    current = now(db)
    if current.astimezone(SHANGHAI).date() < date.fromisoformat(view['target_trade_day']):
        raise APIError(422, 'target_day_not_reached', '目标交易日尚未到达。')
    refs = permitted(db, owner.id, body.evidence_refs, current)
    if len(refs) != len(set(str(v) for v in body.evidence_refs)):
        raise APIError(422, 'evidence_unavailable', '验证证据不存在、未收到或当前不可用。')
    if body.status in {'triggered', 'confirmed', 'invalidated', 'risk_veto'} and not refs:
        raise APIError(422, 'verification_evidence_required', '该验证状态需要实际观察证据。')
    valid_ids = {item['id'] for item in validation_evidence(db, owner.id, identity)['items']}
    if not set(refs) <= valid_ids:
        raise APIError(422, 'target_evidence_required', '验证必须引用目标日新观察资料，原稿依据不能替代次日确认。')
    row = PlanValidation(owner_id=owner.id, report_id=identity, scenario_id=body.scenario_id,
        status=body.status, comment=body.comment, evidence_refs=normalized(body.evidence_refs),
        observation={'checked_at': current.isoformat(), 'method': 'manual', 'original_content_hash': view['content_hash'],
                     'scenario': scenarios[body.scenario_id]}, created_at=current)
    db.add(row)
    db.flush()
    audit(db, owner, 'validation.append', row.id, request_id)
    return validation_view(row), 'partial' if body.status == 'insufficient_data' else 'complete', []


def validation_view(row):
    return dict(id=str(row.id), report_id=str(row.report_id), scenario_id=row.scenario_id, status=row.status,
                comment=row.comment, evidence_refs=row.evidence_refs, observation=row.observation,
                created_at=row.created_at.isoformat())


def completed_results(db, run):
    result = []
    for step in run.stages or []:
        ai = db.get(AiRun, uuid.UUID(step['ai_run_id'])) if step.get('ai_run_id') else None
        if ai and ai.state == 'succeeded':
            result.append(dict(role=step['role'], result=ai.result))
    return result


def validation_evidence(db, owner_id, identity):
    view = report_view(db, owner_id, identity)
    target = date.fromisoformat(view['target_trade_day']) if view.get('target_trade_day') else None
    current = now(db)
    if target is None or current.astimezone(SHANGHAI).date() < target:
        return {'items': []}
    from app.market.review_store import load_dataset
    refs = {}
    for dataset in ('daily', 'limits', 'pools'):
        result = load_dataset(db, dataset, target, as_of_time=current)
        for ref in result.get('evidence_refs') or []:
            refs[str(ref['id'])] = ref
    rows = db.scalars(select(EvidenceVersion).where(EvidenceVersion.owner_id == owner_id,
        EvidenceVersion.kind == 'manual', EvidenceVersion.provenance == 'manual',
        EvidenceVersion.received_at > datetime.fromisoformat(view['as_of_time']),
        EvidenceVersion.received_at <= current,
        EvidenceVersion.payload['trade_day'].astext == target.isoformat()).limit(100)).all()
    for row in rows:
        refs[str(row.id)] = dict(id=str(row.id), content_hash=row.content_hash)
    visible = permitted(db, owner_id, list(refs), current)
    return {'items': [dict(id=key, content_hash=row.content_hash, source_id=str(source.id), source_name=source.name,
        received_at=row.received_at.isoformat(), source_time=(row.published_at or row.event_at).isoformat() if row.published_at or row.event_at else None,
        title=f'{target.isoformat()} 目标日观察资料 · {source.name}', url=None)
        for key, (row, source) in visible.items()
        if row.received_at > datetime.fromisoformat(view['as_of_time'])]}
