"""Inspect registered services and durable receipts. Reads never dispatch work."""
from datetime import date, timedelta

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import defer

from app.ai.models import AiSettings
from app.config import settings
from app.evidence.common import now
from app.integrations.inspection_schemas import Finding, Inspection, InspectionService
from app.market.coverage import coverage
from app.market.models import SourceSettings
from app.market.recovery_models import RecoveryCoverage
from app.market.protocol import SHANGHAI
from app.market.resilience_models import DataHealth
from app.market.resilience import AUTH_REASONS, SCHEMA_REASONS
from app.market.run_heads import latest_streams, receipt_jobs
from app.market.service import source_states
from app.models import InstanceState, Job, Provider, WorkerHeartbeat
from app.read_cache_models import ReadCache

ROLE_NAMES = {'general': '后台任务', 'realtime': '实时采集', 'scheduler': '采集调度',
              'recovery': '历史补漏', 'read-cache': '页面数据更新', 'ai': 'AI 任务'}
WAIT_REASONS = {'intraday_window_wait', 'publication_window_wait', 'daily_data_not_published', 'source_budget_wait',
                'local_request_wait', 'session_not_started', 'calendar_not_ready', 'rate_limited',
                'source_rate_limit', 'collection_intent_conflict', 'auction_data_not_ready', 'auction_limits_unavailable'}
LIMIT_REASONS = {'quote_time_unknown', 'snapshot_trade_date_unverified', 'news_coverage_incomplete',
                 'historical_top_ten_incomplete', 'leader_details_incomplete'}
DATASETS = {'direction.daily_history': '历史前十榜单', 'minute.bars': '分钟行情', 'realtime.snapshots': '盘中快照',
    'breadth.minute_history': '历史市场广度', 'review.stock_funds': '个股资金', 'review.market_funds': '大盘资金',
    'review.indices': '指数收盘数据', 'review.daily': '日线', 'review.limits': '涨跌幅限制',
    'review.members': '板块成分', 'review.pools': '涨跌停榜单', 'review.pool_details': '涨跌停明细',
    'review.themes': '板块行情', 'review.news': '复盘资讯', 'review.lhb': '龙虎榜', 'review.seats': '席位明细'}
HISTORY_STATE = {'unsupported': 'limited', 'blocked': 'manual', 'waiting': 'waiting',
                 'queued': 'working', 'running': 'working', 'missing': 'available', 'partial': 'available'}


def guidance(reason):
    if reason == 'replaced_by_fuyao':
        return '该来源已按现有设置由同花顺 API 替代，旧记录保留，无需操作。'
    if reason in AUTH_REASONS or reason in {'source_disabled', 'source_route_not_selected', 'source_paused'}:
        return '打开该来源的接入配置，核对启用状态、凭据和所选能力的授权，再检测连接。'
    if reason in SCHEMA_REASONS or reason == 'incomplete_page':
        return '来源返回的字段或分页未通过校验。核对供应商接口版本，更新该适配器后重新检修。'
    if reason == 'historical_top_ten_incomplete':
        return '该交易日的历史榜单不足十条。补漏器按原日期复查，供应商补齐原榜后才能验收。'
    if reason in {'quote_time_unknown', 'snapshot_trade_date_unverified'}:
        return '来源缺少可核验的行情时点。需要该来源提供带时间的数据，或接入有对应时标的来源。'
    if reason == 'news_coverage_incomplete':
        return '已保留有来源的资讯。要扩大覆盖，需要补齐该来源的频道或历史授权。'
    if reason == 'calendar_not_ready':
        return '等待已授权交易日历入库；若持续停留，检查交易日历来源的接入配置。'
    if reason in {'auction_data_not_ready', 'auction_limits_unavailable'}:
        return '竞价来源尚未提供完整数据。采集服务会在允许的竞价窗口自动复查，你无需修改接入配置。'
    if reason in WAIT_REASONS:
        return '调度器会在采集窗口、发布或冷却条件满足后继续。'
    return '打开该来源的最近调用核对返回原因，再按供应商提供的可用范围检查配置。'


def source_hold(db, provider, config, run, clock):
    from app.market.auction_schedule import next_collection
    from app.market.budget import family_cooldown_until
    from app.market.cadence_policy import effective_interval
    from app.market.collection_cadence import run_anchor
    from app.market.service import background_window, collection_window

    deadlines = [family_cooldown_until(db, provider.family, clock),
        background_window(db, provider.family, clock) or collection_window(db, provider, config, clock)]
    if run:
        deadlines.append(run.retry_not_before)
    if config.next_run_at > clock:
        if config.paused_reason or not run:
            deadlines.append(config.next_run_at)
        else:
            anchor = run_anchor(db, provider.family, run)
            interval = effective_interval(provider.family, config.interval_seconds) if anchor else config.interval_seconds
            deadlines.append(next_collection(provider.family, anchor or run.received_at, interval))
    return max((at for at in deadlines if at and at > clock), default=None)


def source_finding(db, source, clock, pending, connection_id):
    from app.market.auction_schedule import schema_recheck
    from app.market.service import TDX_FAMILIES
    if not source.provider.enabled or source.collection_state == 'disabled':
        return None
    run = source.last_attempt
    state = source.collection_state
    reason = source.paused_reason or (run.reason if run else None)
    retry = None
    if source.paused_reason == 'replaced_by_fuyao':
        status, advice = 'limited', guidance(source.paused_reason)
    elif source.paused_reason and not schema_recheck(source.family, source.paused_reason):
        status, advice = 'manual', guidance(source.paused_reason)
    elif state in {'permission_missing', 'credentials_missing'} or reason in AUTH_REASONS:
        status, advice = 'manual', guidance(reason or state)
    elif (state == 'ready' and run and source.collection_enabled and not pending and
            source.next_run_at < clock - timedelta(minutes=2) and
            run.received_at < clock - timedelta(seconds=max(120, 2 * source.interval_seconds))):
        status, advice, reason = 'available', '来源已超过两个采集周期。检修将核对采集窗口并恢复调度。', 'collection_overdue'
    elif state in {'ready', 'waiting_focus'} and not source.paused_reason:
        return None
    elif not source.collection_enabled:
        status, advice = 'waiting', '本机采集已关闭。需要采集时，由管理员开启本机数据采集。'
    elif state == 'partial' and reason in LIMIT_REASONS:
        status, advice = 'limited', guidance(reason)
    elif state == 'partial' and run and (run.accepted or run.duplicates) and not run.rejected and not reason and (
            run.expected is None or run.expected <= (run.accepted or 0) + (run.duplicates or 0)):
        status, advice = 'limited', '已保存该来源实际提供的数据。完整范围需供应商提供覆盖证明。'
    elif source.family in TDX_FAMILIES:
        status, advice = 'manual', '此能力使用积分。请在接入管理核对积分范围并手动检测该能力。'
    elif reason in SCHEMA_REASONS and not schema_recheck(source.family, reason):
        status, advice = 'manual', guidance(reason)
    elif pending:
        future = pending.state == 'queued' and pending.available_at > clock
        status = 'waiting' if future else 'working'
        advice = '已按原采集窗口排期。' if future else '采集任务已存在，正在等候实际回执。'
        retry = pending.available_at if future else None
    elif state == 'waiting' or source.cooldown_until and source.cooldown_until > clock:
        status, advice, retry = 'waiting', guidance(reason or 'source_rate_limit'), source.next_run_at
    elif source.next_run_at > clock and (state == 'not_checked' or reason in WAIT_REASONS):
        status, advice, retry = 'waiting', '已按该来源的采集周期排期，到时自动取数。', source.next_run_at
    else:
        status, advice = 'available', '按现有授权和采集窗口重新取数，收到有效数据后复核。'
    if status == 'available':
        config = db.get(SourceSettings, source.provider.id)
        retry = source_hold(db, db.get(Provider, source.provider.id), config, run, clock)
        if retry:
            status, advice = 'waiting', '已排期，采集窗口和请求间隔满足后自动取数。'
    return Finding(id='source:' + str(source.provider.id), area='source', title=source.provider.name,
        state=status, reason=reason or state, advice=advice, retry_at=retry, source_id=source.provider.id,
        connection_id=connection_id, job_id=pending.id if pending else None)


def local_services(db, clock):
    from app.system_settings.models import SystemState
    from app.system_settings.service import connected
    from app.version import SCHEMA_REVISION
    cfg = settings()
    version = db.scalar(text('SELECT version_num FROM alembic_version'))
    matched = version == SCHEMA_REVISION and db.scalar(select(InstanceState.app_mode).where(InstanceState.id == 1)) == cfg.app_mode
    services = [InspectionService(id='api', name='网页接口', online=True, checked_at=clock),
                InspectionService(id='database', name='数据库与版本', online=matched, checked_at=clock)]
    beats = list(db.execute(select(WorkerHeartbeat.worker_id, WorkerHeartbeat.seen_at)))
    roles = list(cfg.required_worker_roles) or ['collector']
    if cfg.read_cache_enabled:
        roles.append('read-cache')
    ai = db.get(AiSettings, 1)
    if ai and ai.enabled:
        roles.append('ai')
    for role in roles:
        if role == 'collector':
            times = [at for name, at in beats if name.startswith('market:') or ':' not in name]
        else:
            prefix = (role if role in {'read-cache', 'ai'} else 'market:' + role) + ':'
            times = [at for name, at in beats if name.startswith(prefix)]
        last = max(times, default=None)
        services.append(InspectionService(id=role, name=ROLE_NAMES.get(role, '后台采集'),
            online=bool(last and last >= clock - timedelta(seconds=90)), checked_at=last))
    system = db.get(SystemState, 1)
    for enabled, role, name, last in ((cfg.maintenance_enabled, 'maintenance', '备份服务', system.backup_heartbeat_at if system else None),
            (cfg.update_feed_url and cfg.update_public_key, 'updater', '升级服务', system.updater_heartbeat_at if system else None)):
        if enabled:
            services.append(InspectionService(id=role, name=name, online=bool(last and connected(last, moment=clock)), checked_at=last))
    return services


def infrastructure(db, owner, clock):
    services = local_services(db, clock)
    checks = {'数据库与版本': 1, '后台进程': len(services) - 2}
    findings = [Finding(id='runtime:' + s.id, area='runtime', state='manual',
        title='数据库版本或实例模式不匹配' if s.id == 'database' else s.name + '未就绪',
        advice='数据库版本或模式需要与当前天衍版本一致。' if s.id == 'database' else '打开本机启动入口检查此服务，恢复后会自动更新状态。')
        for s in services if not s.online]
    checks['待执行与执行中任务'] = db.scalar(select(func.count(Job.id)).where(Job.state.in_(['queued', 'running'])))
    expired = db.scalar(select(func.count(Job.id)).where(Job.state == 'running', Job.lease_until < clock))
    if expired:
        findings.append(Finding(id='runtime:lease', area='runtime', state='waiting', title='有任务的执行租约已过期',
            count=expired, advice='后台执行器会按原任务接管。若对应进程离线，先恢复该进程。'))
    return checks, findings


def cache_items(db, owner, clock):
    from app.read_cache import cache_scope
    if not settings().read_cache_enabled:
        return []
    return list(db.scalars(select(ReadCache).options(defer(ReadCache.payload, raiseload=True)).where(
        ReadCache.owner_id == owner.id, ReadCache.scope == cache_scope(db, owner),
        ReadCache.accessed_at >= clock - timedelta(hours=1), ReadCache.dirty.is_(True),
        or_(ReadCache.last_error.is_not(None), func.coalesce(ReadCache.built_at, ReadCache.accessed_at) <
            clock - timedelta(minutes=2)))))


def history_groups(db, sources, clock):
    row = RecoveryCoverage
    # Group the entire ledger, including records that a paginated monitor would never display.
    groups = list(db.execute(select(row.source_id, row.dataset, row.state, row.reason,
        func.count(), func.min(row.trade_day), func.max(row.trade_day), func.min(func.coalesce(row.next_retry_at, clock)))
        .join(SourceSettings, (SourceSettings.source_id == row.source_id) & (SourceSettings.owner_id == row.owner_id))
        .group_by(row.source_id, row.dataset, row.state, row.reason)))
    findings = []
    for source_id, dataset, state, reason, count, first, last, retry in groups:
        if state == 'complete' or source_id not in sources:
            continue
        source = sources[source_id]
        status = HISTORY_STATE[state]
        effective_reason = reason
        if source.paused_reason == 'replaced_by_fuyao':
            status, effective_reason = 'limited', source.paused_reason
            advice = guidance(effective_reason)
        elif state == 'unsupported':
            advice = '该历史范围缺少可回溯的原始数据。需要该日归档或有相应历史权益的来源。'
        elif not source.provider.enabled or source.collection_state == 'disabled':
            status, advice = 'limited', '该来源已停用，历史记录保留。需要恢复采集时可在接入管理重新启用。'
        elif source.paused_reason or source.collection_state in {'credentials_missing','permission_missing'}:
            effective_reason = source.paused_reason or source.collection_state
            status, advice = 'manual', guidance(effective_reason)
        elif state == 'blocked' and reason in WAIT_REASONS:
            status, advice = 'waiting', guidance(reason)
        elif state == 'blocked':
            advice = guidance(reason)
        elif state in {'queued', 'running'}:
            advice = '已有对应日期和范围的补采任务，完整性由补漏器对照入库数据复核。'
        elif state == 'waiting' or retry and retry > clock:
            status, advice = 'waiting', '补漏器将在条件满足后重新核对该日期和范围。'
        elif dataset in {'direction.daily_history', 'minute.bars'} or dataset.startswith('review.'):
            advice = '按原交易日和范围重新对账，缺少的可回溯数据将进入补采队列。'
        else:
            status, advice = 'limited', '检查已有历史归档。缺少当时采样的范围需由支持历史回放的来源提供。'
        if not settings().recovery_enabled or not settings().data_collection_enabled:
            if status in {'working', 'available', 'waiting'}:
                status, advice = 'waiting', '本机补漏或采集已关闭。开启后由补漏器继续核对。'
        days = str(first) if first == last else f'{first} 至 {last}'
        title = source.provider.name + ' · ' + DATASETS.get(dataset, dataset)
        findings.append(Finding(id=f'history:{source_id}:{dataset}:{state}:{reason or ""}', title=title, area='history',
            state=status, source_id=source_id, advice=f'{days}。{advice}', reason=effective_reason, count=count,
            retry_at=retry if retry and retry > clock else None, trade_day=first if first == last else None))
    return findings, sum(group[4] for group in groups)


def business_jobs(db, owner, clock):
    from app.system_settings.models import MaintenanceJob
    types = {'screen.evaluate': '规则筛选', 'snapshot.create': '研究快照', 'tomorrow.build': '明日预案',
             'tomorrow.validate': '预案验证', 'review.prepare': '每日复盘准备'}
    jobs = db.scalars(select(Job).where(Job.owner_id == owner.id, Job.kind.in_(types),
        Job.created_at >= clock - timedelta(days=1)).order_by(Job.created_at.desc(), Job.id.desc()))
    seen = set()
    findings = []
    for job in jobs:
        # Separate independent business intents so another document or date cannot mask a failure.
        target = next((str(job.payload[key]) for key in ('snapshot_id', 'rule_id', 'run_id', 'trade_day', 'day') if job.payload.get(key)), '')
        key = (job.kind, target)
        if key in seen:
            continue
        seen.add(key)
        if job.state == 'failed':
            findings.append(Finding(id='job:' + str(job.id), area='runtime', state='manual',
                title=types[job.kind] + '任务失败', job_id=job.id, reason=job.error_code,
                advice='打开「' + types[job.kind] + '」查看原任务的日期和输入。确认依赖数据已就绪后重新生成。'))
    maintenance = db.scalars(select(MaintenanceJob).where(MaintenanceJob.owner_id == owner.id)
        .distinct(MaintenanceJob.kind).order_by(MaintenanceJob.kind, MaintenanceJob.created_at.desc(), MaintenanceJob.id.desc()))
    count = len(seen)
    for job in maintenance:
        count += 1
        if job.status in {'failed', 'manual_recovery'}:
            name = '备份' if job.kind == 'backup' else '升级'
            findings.append(Finding(id='maintenance:' + str(job.id), area='runtime', state='manual', title=name + '任务需要处理',
                advice='打开系统设置的' + name + '记录，按原任务的失败阶段检查本机服务和存储位置。', reason=job.error_code))
    return count, findings


def routes(db, sources, clock):
    from app.market.minute_backfill import configuration_fingerprint
    rows = list(db.scalars(select(DataHealth).where(DataHealth.trade_day == clock.astimezone(SHANGHAI).date(),
        or_(DataHealth.capability.in_(['stock_quote','index_quote']), DataHealth.capability.startswith('stock_minute_')))))
    result = []
    for row in rows:
        source = sources.get(row.primary_source_id)
        if not source or not source.provider.enabled or row.state == 'normal':
            continue
        config = db.get(SourceSettings, row.primary_source_id)
        provider = db.get(Provider, row.primary_source_id)
        if config.owner_id != row.owner_id or configuration_fingerprint(provider, config) != row.configuration_sha256:
            continue
        if not settings().data_collection_enabled or config.paused_reason:
            continue
        backup = sources.get(row.active_source_id) if row.state == 'fallback' else None
        name = {'stock_quote':'个股报价','index_quote':'指数报价'}.get(row.capability, '分钟行情')
        advice = (f'当前由「{backup.provider.name}」供数，后台持续复核主来源。' if backup else
            '核对同一数据范围的已授权备用来源；有有效数据时自动切换。')
        waiting = not backup and row.next_action == 'wait_session' and row.next_action_at and row.next_action_at > clock
        result.append(Finding(id='route:'+str(row.id), area='route', source_id=row.primary_source_id,
            title=source.provider.name+' · '+name, state='working' if backup else 'waiting' if waiting else 'available',
            advice='交易窗口开启后自动核对主备来源。' if waiting else advice, retry_at=row.next_action_at if waiting else None))
    return len(rows), result


def inspect(db, owner, *, include_receipt=True):
    from app.integrations.monitor import overview
    from app.integrations.inspection_repair import last_batch, verify_batch
    from app.market.collection_cadence import HISTORY_KEYS
    from app.market.resilience import HISTORY_KEYS as OTHER_HISTORY
    clock = now(db)
    sources = {s.provider.id: s for s in source_states(db)}
    checks, findings = infrastructure(db, owner, clock)
    checks['业务与维护任务'], business = business_jobs(db, owner, clock)
    findings.extend(business)
    monitor = overview(db, owner)
    for service in monitor.services:
        if (service.kind == 'model' or service.impact_modules == ['iFinD 手动研究']) and (
                service.error_code or service.status in {'error','failed','unavailable','unknown','result_unknown','rate_limited'}):
            findings.append(Finding(id='model:' + str(service.id), title=service.name, area='call', state='manual',
                reason=service.error_code, connection_id=service.id,
                advice='在接入配置核对凭据、已授权能力和最近调用，再手动检测连接。'))
    checks['接入服务'] = monitor.services_count
    checks['采集能力'] = len(sources)
    checks['行情供数路径'], paths = routes(db, sources, clock)
    findings.extend(paths)
    jobs = db.scalars(select(Job).where(Job.state.in_(['queued', 'running']), Job.kind == 'source.collect')
                      .order_by(Job.created_at.desc(), Job.id.desc()))
    pending = {job.payload.get('source_id'):job for job in jobs if not (HISTORY_KEYS | OTHER_HISTORY).intersection(job.payload)
               and not (job.payload.get('integration_context') or {}).get('probe')}
    for source in sources.values():
        item = source_finding(db, source, clock, pending.get(str(source.provider.id)), None)
        if item:
            findings.append(item)
    streams = latest_streams(db, list(sources), clock)
    checks['调用类型'] = len(streams)
    jobs_by_id = receipt_jobs(db, streams)
    for run in streams:
        source = sources[run.source_id]
        if not source.provider.enabled or run.status not in {'partial', 'unavailable', 'error'}:
            continue
        if source.last_attempt and source.last_attempt.id == run.id:
            continue
        job = jobs_by_id.get(run.job_id)
        context = (job.payload.get('review_context') or {}) if job else {}
        raw_day = (job.payload.get('day') or context.get('day')) if job else None
        try:
            day = date.fromisoformat(str(raw_day)) if raw_day else None
        except ValueError:
            day = None
        advice = guidance(run.reason) if run.reason in LIMIT_REASONS | WAIT_REASONS | AUTH_REASONS | SCHEMA_REASONS else (
            '该调用类型的最近回执未完整。请按来源和资料日查看采集记录，补漏器会核对已登记的缺口。')
        findings.append(Finding(id=f'call:{run.source_id}:{run.capability}', area='call',
            title=source.provider.name + ' · ' + DATASETS.get(run.capability, run.capability), state='manual' if run.reason in AUTH_REASONS | SCHEMA_REASONS else 'limited',
            reason=run.reason, source_id=run.source_id, job_id=run.job_id, trade_day=day, advice=(f'{day}。' if day else '') + advice))
    history, checks['历史核验记录'] = history_groups(db, sources, clock)
    findings.extend(history)
    capabilities = coverage(db).items
    checks['数据类别'] = len(capabilities)
    for cap in capabilities:
        if cap.state == 'partial':
            continue
        configured = [s for s in sources.values() if s.family in cap.families]
        enabled = [s for s in configured if s.provider.enabled and s.collection_state != 'disabled']
        selected = next(iter(enabled or configured), None)
        advice = ('已有来源，等待该类有效数据入库。请查看对应来源的检修结果。' if enabled else
                  '在接入管理选择具备该能力的来源，核实授权并检测连接。' if cap.families else
                  '当前未登记可直接接入的适配器，需要支持该能力的数据源和对应适配器。')
        findings.append(Finding(id='capability:' + cap.id, title=cap.id + ' ' + cap.name, area='capability',
            state='limited', advice=advice, source_id=selected.provider.id if selected else None))
    caches = cache_items(db, owner, clock)
    if settings().read_cache_enabled:
        checks['页面缓存异常'] = len(caches)
    for cache in caches:
        leased = cache.lease_until and cache.lease_until > clock
        findings.append(Finding(id='cache:' + cache.key, title='页面数据 · ' + cache.operation, area='cache',
            state='working' if leased else 'available', advice='后台正在重建页面数据。' if leased else '重新调度当前页面数据，并核对新生成的结果。'))
    batch = last_batch(db, owner, clock) if include_receipt else None
    return Inspection(checked_at=clock, checks=checks, findings=findings, last_repair=verify_batch(db, owner, batch) if batch else None)
