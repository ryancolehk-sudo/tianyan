"""Owner-scoped immutable reports and a durable staged research workflow."""
import uuid
from copy import deepcopy
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import load_only

from app.ai.models import AiBinding, AiRun, AiSettings, Connection
from app.auth import APIError, require_role
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


def codex_setup_step(db, owner_id, options):
    from app.chatgpt.models import ChatGPTAccount
    from app.chatgpt.service import account_view, availability
    if any(option['source'] == 'chatgpt_plan' for option in options):
        return 'ready'
    if not availability()[0]:
        return 'unavailable'
    account = db.scalar(select(ChatGPTAccount).where(ChatGPTAccount.owner_id == owner_id,
        ChatGPTAccount.state != 'disconnected').order_by(ChatGPTAccount.use_for_ai.desc(),
        ChatGPTAccount.updated_at.desc(), ChatGPTAccount.id).limit(1))
    if account is None:
        return 'connect'
    view = account_view(db, account)
    if view.state != 'connected' or not view.sharing_granted:
        return 'authorize'
    if not view.model:
        return 'choose_model'
    if not view.tested:
        return 'test_model'
    return 'enable_model' if not view.enabled else 'check_model'


def review_accounts(db, owner_id):
    from app.chatgpt.models import ChatGPTAccount
    from app.chatgpt.oauth import SHARING_SCOPES
    from app.chatgpt.service import availability
    if not availability()[0]:
        return []
    rows = db.execute(select(ChatGPTAccount, Connection).join(Connection, Connection.id == ChatGPTAccount.connection_id)
        .where(ChatGPTAccount.owner_id == owner_id, ChatGPTAccount.state == 'connected',
               ChatGPTAccount.secret_ref.is_not(None), Connection.secret_ref == ChatGPTAccount.secret_ref,
               Connection.owner_id == owner_id, Connection.template == 'chatgpt_plan', Connection.archived.is_(False))
        .order_by(ChatGPTAccount.use_for_ai.desc(), ChatGPTAccount.updated_at.desc(), ChatGPTAccount.id))
    return [dict(id=str(account.id), connection_id=str(connection.id), name=account.email or 'Codex 账号',
                 model=connection.model, revision=account.revision)
            for account, connection in rows if SHARING_SCOPES.issubset(set(account.scopes or []))]


def api_review_state(connection):
    from app.ai.service import model_credential_health
    health = model_credential_health(connection)
    if health.get('error_code') == 'credential_changed_requires_test':
        return 'needs_test'
    if not health['configured']:
        return 'needs_key'
    if not connection.model:
        return 'needs_model'
    test = connection.last_test or {}
    if test.get('revision') == connection.revision:
        if test.get('status') in {'error', 'failed'}:
            return 'test_failed'
        if test.get('status') == 'unknown':
            return 'test_unknown'
    if test.get('status') != 'success' or connection.tested_revision != connection.revision or test.get('revision') != connection.revision:
        return 'needs_test'
    return 'ready' if connection.enabled else 'needs_enable'


def review_providers(db):
    from sqlalchemy.orm import undefer
    return db.scalars(select(Provider).options(undefer(Provider.family)).where(
        Provider.enabled.is_(True), Provider.configured.is_(True),
        Provider.entitlement_status == 'verified').order_by(Provider.name)).all()


def review_material_scope(sources, saved):
    from app.market.registry import SOURCES
    from app.market.review_protocol import DATASET_FAMILIES
    from app.market.tushare_specs import SPECS
    from app.tomorrow.data import SUPPLEMENTARY

    requirements = [
        ('沪市交易日校验', ['calendar_sse']), ('深市交易日校验', ['calendar_szse']),
        ('全市场涨跌分布', ['daily']), ('涨跌停价格核验', ['limits']), ('涨跌停池与连板梯队', ['pools']),
        ('主要指数收盘表现', ['indices']), ('板块行情与强弱排序', ['themes']),
        ('板块成分股归属', ['members']), ('板块资金流向', ['theme_funds']),
        ('龙虎榜上榜个股', ['lhb']), ('龙虎榜席位明细', ['seats']),
    ]
    needed = [(label, [DATASET_FAMILIES[key] for key in keys]) for label, keys in requirements]
    needed.extend([
        ('公告原文核验', ['cninfo_announcements']),
        ('个股财务指标', ['tushare_fina_indicator', 'baostock_fundamental']),
        ('个股资金流向', ['tushare_moneyflow', 'tushare_moneyflow_dc']),
        ('复权价格核验', ['tushare_adj_factor']),
    ])
    relevant = {family for _, families in needed for family in families}
    relevant.update(family for family, spec in SPECS.items() if 'tushare.' + spec.api in SUPPLEMENTARY)
    grants = set(saved)
    selected = [p for p in sources if all(p.rights.get(k) == 'allowed' for k in ('display', 'cache'))
                and (str(p.id) in grants if grants else
                     p.family in relevant or p.kind in ('news', 'manual') or p.family not in SOURCES)]
    families = {p.family for p in selected}
    gaps = [dict(interfaces=' / '.join(SOURCES[f].name for f in alternatives), impact=impact)
            for impact, alternatives in needed if families.isdisjoint(alternatives)]
    if not any(p.kind == 'news' and p.family != 'cninfo_announcements' for p in selected):
        gaps.append(dict(interfaces='财经新闻正文接口', impact='核心新闻及财经热词分析'))
    ids = sorted(str(p.id) for p in selected)
    required_core = {DATASET_FAMILIES[key] for key in ('calendar_sse', 'calendar_szse', 'daily', 'limits', 'pools')}
    available = {p.family for p in sources if all(p.rights.get(k) == 'allowed' for k in ('display', 'cache'))}
    return ids, dict(count=len(ids), digest=content_hash(ids), gaps=gaps, core_missing=not required_core <= available)


def settings_view(db, owner_id):
    from app.ai.service import resolve_feature, usable_connection
    row = db.get(PlanSettings, owner_id)
    profile = deepcopy(row.profile) if row else {}
    connections = db.scalars(select(Connection).where(Connection.kind == 'model', Connection.archived.is_(False),
        or_(Connection.owner_id.is_(None), Connection.owner_id == owner_id)).order_by(Connection.name, Connection.id)).all()
    options, api_connections = [], []
    for connection in connections:
        if connection.template == 'chatgpt_plan':
            try:
                usable_connection(db, connection.id, owner_id)
            except APIError:
                continue
        else:
            state = api_review_state(connection)
            api_connections.append(dict(id=str(connection.id), name=connection.name, model=connection.model,
                                        revision=connection.revision, state=state))
            if state != 'ready':
                continue
        options.append(dict(id=str(connection.id), name=connection.name, model=connection.model,
            source='chatgpt_plan' if connection.template == 'chatgpt_plan' else 'api'))
    config, binding = db.get(AiSettings, 1), db.get(AiBinding, 'plan_generation')
    gate = None
    if not config or not config.enabled:
        gate = '全局 AI 已关闭'
    elif not binding or not binding.enabled:
        gate = '明日预案的 AI 生成功能已关闭'
    default_model = default_connection_id = None
    if not gate:
        try:
            connection = resolve_feature(db, 'plan_generation', owner_id)[0]
            if any(option['id'] == str(connection.id) for option in options):
                default_model, default_connection_id = connection.model, str(connection.id)
        except APIError:
            pass
    sources = review_providers(db)
    _, materials = review_material_scope(sources, profile.get('ai_source_ids', []))
    return dict(codex_accounts=review_accounts(db, owner_id), codex_setup=codex_setup_step(db, owner_id, options),
                api_connections=api_connections, api_bound=any(c.template != 'chatgpt_plan' and c.secret_ref for c in connections),
                revision=row.revision if row else 0, enabled=row.enabled if row else False,
                use_ai=row.use_ai if row else False, cash=profile.get('cash'), holdings=profile.get('holdings', []),
                risk_policy=profile.get('risk_policy', s.RiskPolicy().model_dump()),
                ai_source_ids=profile.get('ai_source_ids', []), review_materials=materials, model_options=options,
                model_connection_id=profile.get('model_connection_id'), model_gate=gate, default_model=default_model, default_connection_id=default_connection_id,
                sources=[dict(id=str(p.id), name=p.name, ai_allowed=all(p.rights.get(k) == 'allowed' for k in ('display', 'cache')))
                         for p in sources], evening_time='22:30', overnight_time='08:30', timezone='Asia/Shanghai')


def update_settings(db, owner, body, request_id=None):
    lock(db, f'tomorrow:settings:{owner.id}')
    row = db.get(PlanSettings, owner.id)
    if body.expected_revision != (row.revision if row else 0):
        raise APIError(412, 'revision_conflict', '设置已更新，请先读取最新设置；你的输入仍可保留。')
    if row is None:
        row = PlanSettings(owner_id=owner.id, revision=0, enabled=False, use_ai=False, profile={})
        db.add(row)
    profile = deepcopy(row.profile)
    if 'model_connection_id' in body.model_fields_set:
        if body.model_connection_id:
            from app.ai.service import usable_connection
            connection = usable_connection(db, body.model_connection_id, owner.id)
            if connection.template != 'chatgpt_plan' and not connection.secret_ref:
                raise APIError(422, 'model_not_connected', '请先接入模型 API 并测试连接。')
            profile['model_connection_id'] = str(connection.id)
        else:
            profile.pop('model_connection_id', None)
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


def enable_plan_ai(db, owner, connection_id, request_id=None):
    from app.ai.service import usable_connection
    usable_connection(db, connection_id, owner.id)
    open_plan_ai(db, owner, connection_id, request_id)
    return settings_view(db, owner.id), 'complete', []


def open_plan_ai(db, owner, connection_id, request_id):
    require_role(owner, 'admin')
    lock(db, 'ai:settings')
    config = db.get(AiSettings, 1)
    binding = db.get(AiBinding, 'plan_generation')
    if config is None:
        db.add(AiSettings(id=1, enabled=True, revision=1))
    elif not config.enabled:
        config.enabled = True
        config.revision += 1
    if binding is None:
        db.add(AiBinding(feature='plan_generation', enabled=True, revision=1))
    elif not binding.enabled:
        binding.enabled = True
        binding.revision += 1
    audit(db, owner, 'ai.enable', connection_id, request_id)
    db.flush()


def prepare_review_api(db, owner, review, request_id):
    from app.ai.probes import existing_input
    from app.ai.service import store_connection
    connection = db.scalar(select(Connection).where(Connection.id == review.connection_id,
        Connection.kind == 'model', Connection.template != 'chatgpt_plan', Connection.archived.is_(False),
        or_(Connection.owner_id.is_(None), Connection.owner_id == owner.id)).with_for_update())
    if connection is None:
        raise APIError(404, 'not_found', '这项 API 接入不存在或不可用。')
    if connection.revision != review.connection_revision:
        raise APIError(412, 'configuration_changed', 'API 配置已变化，请核对最新模型后再复盘。')
    state = api_review_state(connection)
    if state == 'ready':
        return connection
    if state != 'needs_enable':
        raise APIError(409, 'model_not_ready', '请检查已保存的 API Key 和模型，并测试当前配置。')
    if not review.enable_connection:
        raise APIError(409, 'api_not_enabled', '这项 API 已通过测试，需启用后复盘。')
    require_role(owner, 'admin')
    body = existing_input(connection).model_copy(update={'enabled': True})
    store_connection(db, owner, body, connection.id, request_id, owner_id=owner.id)
    return connection


def apply_review(db, owner, review, config, request_id):
    from app.ai.service import authorized_plan_codex, resolve_feature
    codex = isinstance(review, s.PlanCodexReview)
    if codex:
        authorized_plan_codex(db, review.connection_id, owner.id)
    else:
        connection = prepare_review_api(db, owner, review, request_id)
        config['requested_model'] = connection.model
    profile = config['profile']
    if review.materials_digest is not None:
        ids, materials = review_material_scope(review_providers(db), profile.get('ai_source_ids', []))
        if review.source_ids is not None or review.materials_digest != materials['digest']:
            raise APIError(412, 'revision_conflict', '可用接口已变化，请按更新后的资料范围复盘。')
        if not ids:
            raise APIError(409, 'ai_sources_missing', '没有可供模型复盘的资料接口。')
        if not profile.get('ai_source_ids'):
            profile['ai_source_ids'] = ids
            config['one_time_sources'] = True
    if review.source_ids is not None:
        if profile.get('ai_source_ids'):
            raise APIError(412, 'revision_conflict', '已有资料授权，请按最新选择复盘。')
        requested = set(review.source_ids)
        rows = db.scalars(select(Provider).where(Provider.id.in_(requested), Provider.enabled.is_(True),
            Provider.configured.is_(True), Provider.entitlement_status == 'verified'))
        allowed = {p.id for p in rows if all(p.rights.get(k) == 'allowed' for k in ('display', 'cache'))}
        if requested != allowed:
            raise APIError(409, 'input_permission_changed', '所选复盘资料的权限已变化，请重新选择。')
        profile['ai_source_ids'] = sorted(str(identity) for identity in allowed)
        config['one_time_sources'] = True
    if not profile.get('ai_source_ids'):
        raise APIError(409, 'ai_sources_missing', '请为这次复盘选择资料。')
    if review.enable_ai:
        open_plan_ai(db, owner, review.connection_id, request_id)
    _, _, config['ai'] = resolve_feature(db, 'plan_generation', owner.id,
        plan_connection_id=review.connection_id, plan_model=review.model if codex else None)


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
    if body.expected_settings_revision is not None and body.expected_settings_revision != (settings.revision if settings else 0):
        raise APIError(412, 'revision_conflict', '预案设置已变化，请重新核对模型后生成。')
    profile = deepcopy(settings.profile) if settings else {}
    from app.config import settings as runtime_settings
    config = dict(profile=profile, settings_revision=settings.revision if settings else 0,
                  app_mode=runtime_settings().app_mode)
    limitations = []
    if target is None:
        limitations.append('未来交易日历不足，目标交易日待核验，草稿不能确认。')
    if body.codex_review or body.api_review:
        apply_review(db, owner, body.codex_review or body.api_review, config, request_id)
    elif body.use_ai:
        from app.ai.service import resolve_feature
        explicit = body.model_connection_id is not None or body.expected_settings_revision is not None
        if explicit and not profile.get('ai_source_ids'):
            raise APIError(409, 'ai_sources_missing', '请先选择允许模型读取的预案资料。')
        try:
            _, _, ai_config = resolve_feature(db, 'plan_generation', owner.id,
                plan_connection_id=body.model_connection_id or profile.get('model_connection_id'))
            config['ai'] = ai_config
        except APIError as error:
            if explicit:
                raise
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
    snapshot = row.config_snapshot or {}
    result['requested_model'] = snapshot.get('requested_model') or (snapshot.get('ai') or {}).get('plan_model')
    if result['requested_model'] and row.error_code and row.stages and (stage_id := row.stages[-1].get('ai_run_id')):
        from app.ai.models import AiAttempt
        details = db.scalar(select(AiAttempt.safe_details).where(AiAttempt.owner_id == row.owner_id,
            AiAttempt.run_id == uuid.UUID(stage_id), AiAttempt.error_code == row.error_code)
            .order_by(AiAttempt.started_at.desc()).limit(1))
        result['model_error'] = (details or {}).get('message')
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
    roles = set(row.payload.get('ai_completed_roles') or [])
    stage_ids = [uuid.UUID(step['ai_run_id']) for step in run.stages or []
                 if step.get('role') in roles and step.get('ai_run_id')]
    models = db.scalars(select(AiRun.model).distinct().where(AiRun.id.in_(stage_ids),
        AiRun.owner_id == owner_id, AiRun.state == 'succeeded', AiRun.completed_at <= row.created_at)
        .order_by(AiRun.model)).all() if stage_ids else []
    return dict(deepcopy(row.payload), id=str(row.id), run_id=str(row.run_id), revision_number=row.revision_number,
                based_on_id=str(row.based_on_id) if row.based_on_id else None, slot=run.slot,
                status='confirmed' if confirmed else 'draft', author_type=row.author_type, generation_models=models,
                can_export=all(source.rights.get("export") == "allowed" or
                               (version.owner_id == owner_id and source.kind == "manual" and version.provenance == "manual")
                               for version, source in accessible.values()),
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
