"""Submit normal, fenced repairs and verify the precise job or coverage record later."""
from datetime import timedelta
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import defer

from app.auth import APIError
from app.config import settings
from app.evidence.common import content_hash, lock, now
from app.evidence.models import IdempotencyKey
from app.integrations.inspection import WAIT_REASONS, guidance, inspect, source_hold
from app.integrations.inspection_schemas import RepairBatch, RepairReceipt
from app.maintenance_control import admit
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SourceFailure
from app.market.recovery_models import RecoveryCoverage
from app.models import Job, Provider, User
from app.read_cache_models import ReadCache

ROUTE = '/api/v1/data-center/inspection/repair'


def legacy_batch(db, owner, clock):
    saved = db.scalar(select(IdempotencyKey.response_json).where(IdempotencyKey.owner_id == owner.id,
        IdempotencyKey.method == 'POST', IdempotencyKey.route == ROUTE, IdempotencyKey.expires_at > clock)
        .order_by(IdempotencyKey.expires_at.desc()).limit(1))
    return RepairBatch.model_validate(saved['data']) if saved else None


def last_batch(db, owner, clock):
    legacy = legacy_batch(db, owner, clock)
    saved = db.scalar(select(Job.result_ref).where(Job.owner_id == owner.id, Job.kind == 'inspection.repair',
        Job.state == 'succeeded').order_by(Job.completed_at.desc(), Job.id.desc()).limit(1))
    batch = RepairBatch.model_validate(saved['batch']) if saved and saved.get('batch') else None
    return legacy if legacy and (not batch or legacy.started_at > batch.started_at) else batch


def signature(db, provider, config):
    from app.integrations.credentials import resolve_source_credentials
    bound = resolve_source_credentials(db, provider.id, provider.family)
    return content_hash([config.owner_id, config.configured_at, provider.adapter_version,
                         provider.entitlement_status, provider.rights, bound.connection_id, bound.revision])


def eligible(db, source_id):
    from app.integrations.credentials import resolve_source_credentials
    from app.market.service import TDX_FAMILIES, permission_reason
    provider = db.get(Provider, source_id)
    config = db.get(SourceSettings, source_id)
    if not config or not provider:
        raise APIError(409, 'configuration_changed', '来源配置已变化，请重新核对。')
    owner = db.get(User, config.owner_id)
    if not owner or not owner.enabled or owner.role != 'admin' or permission_reason(provider):
        raise APIError(403, 'permission_missing', '核对来源和配置账号的授权，然后重新检修。')
    if provider.family in TDX_FAMILIES:
        raise APIError(422, 'points_not_enabled', '此能力使用积分，请在接入管理手动检测。')
    bound = resolve_source_credentials(db, source_id, provider.family, refresh=True)
    if bound.reason:
        raise APIError(422, bound.reason, guidance(bound.reason))
    return provider, config


def repair_source(db, item, clock, source):
    from app.market.auction_schedule import schema_recheck
    from app.market.resilience import pending_job
    from app.market.service import BREADTH_HISTORY, dispatch_scheduled
    provider, config = eligible(db, item.source_id)
    lock(db, 'source:queue:' + str(provider.id))
    if config.paused_reason and not schema_recheck(provider.family, config.paused_reason):
        raise APIError(422, 'source_paused', guidance(config.paused_reason))
    last = source.last_attempt
    item = item.model_copy(update=dict(configuration=signature(db, provider, config),
        capability=last.capability if last else None))
    job = pending_job(db, provider.id)
    if job and (job.payload.get('integration_context') or {}).get('probe'):
        return item.model_copy(update=dict(state='waiting', job_id=None, advice='连接检测正在执行，等待正常采集的有效回执。'))
    if item.state == 'waiting' and not job:
        return item.model_copy(update=dict(advice='已跟踪该来源，按现有排期采集后自动核验，你无需操作。'))
    if job is None and provider.family != BREADTH_HISTORY:
        retry = source_hold(db, provider, config, last, clock)
        if retry:
            return item.model_copy(update=dict(state='waiting', advice='已跟踪下次允许的采集，数据返回后自动核验。', retry_at=retry))
        # All current source gates are open. Wake an overdue schedule even if its
        # persisted future timestamp was calculated before the last successful run.
        config.next_run_at = min(clock, config.next_run_at)
        job = dispatch_scheduled(db, provider, config, clock)
        db.flush()
        job = job or pending_job(db, provider.id)
    if job is None:
        return item.model_copy(update=dict(state='waiting', advice='已核对采集条件，等待该来源的正常调度窗口。',
            retry_at=config.next_run_at if config.next_run_at > clock else None))
    if job.state == 'queued' and job.available_at > clock:
        return item.model_copy(update=dict(state='waiting', job_id=job.id, retry_at=job.available_at,
            advice='已跟踪排期任务，到时采集并自动核验。'))
    return item.model_copy(update=dict(state='working', job_id=job.id,
        advice='已交给采集器，正在等待这个任务的有效数据回执。'))


def repair_history(db, item, clock, allowance):
    from app.market import recovery
    from app.market.budget import family_cooldown_until
    from app.market.review_protocol import DATASET_FAMILIES
    from app.market.minute import FAMILY_INTERVALS
    from app.market.direction_archive import FAMILIES as DIRECTION
    provider, config = eligible(db, item.source_id)
    _, _, dataset, state, reason = item.id.split(':', 4)
    if config.paused_reason or not settings().recovery_enabled:
        return [item.model_copy(update=dict(state='manual', advice='核对来源暂停原因与本机补漏开关。'))]
    lock(db, 'recovery.source:' + str(provider.id))
    cooldown = family_cooldown_until(db, provider.family, clock)
    if cooldown:
        return [item.model_copy(update=dict(state='waiting', advice='等待供应商冷却，后台将继续核对原日期。', retry_at=cooldown))]
    q = select(RecoveryCoverage).where(RecoveryCoverage.owner_id == config.owner_id, RecoveryCoverage.source_id == provider.id,
        RecoveryCoverage.dataset == dataset, RecoveryCoverage.state == state, RecoveryCoverage.reason.is_not_distinct_from(reason or None),
        or_(RecoveryCoverage.next_retry_at.is_(None), RecoveryCoverage.next_retry_at <= clock))
    rows = list(db.scalars(q.order_by(RecoveryCoverage.checked_at, RecoveryCoverage.trade_day.desc(),
        RecoveryCoverage.id).limit(allowance[0]).with_for_update(skip_locked=True)))
    results = []
    for row in rows:
        scope = row.scope.get('scope')
        if dataset == 'direction.daily_history' and provider.family in DIRECTION and scope == 'top10':
            recovery.direction_coverage(db, provider, config, [row.trade_day], clock, allowance)
        elif dataset.startswith('review.') and DATASET_FAMILIES.get(dataset[7:]) == provider.family and scope:
            recovery.daily_coverage(db, provider, config, dataset[7:], scope, [row.trade_day], clock, allowance)
        elif dataset == 'minute.bars' and provider.family in FAMILY_INTERVALS and row.scope.get('instrument') in config.config.get('symbols', []):
            recovery.minute_coverage(db, provider, config, [row.trade_day], clock, allowance)
        else:
            continue
        job = db.get(Job, row.job_id) if row.job_id else None
        status = ('verified' if row.state == 'complete' and (row.actual_count or 0) > 0 else
                  'working' if job and job.state in {'queued', 'running'} else 'manual' if row.state == 'blocked' else 'waiting')
        advice = {'verified':'按原日期和范围复核，入库数据已完整。','working':'补采任务已登记，等待原日期的完整性核验。',
                  'waiting':'该范围仍缺数据，补漏器将在条件满足后继续。','manual':guidance(row.reason)}[status]
        results.append(item.model_copy(update=dict(id='history:' + str(row.id), title=provider.name + ' · ' + str(row.trade_day),
            count=1, state=status, advice=advice, reason=row.reason, retry_at=row.next_retry_at,
            coverage_id=row.id, job_id=row.job_id, trade_day=row.trade_day, configuration=signature(db, provider, config),
            coverage_signature=row.configuration_sha256)))
    return results


def repair_cache(db, owner, item, clock):
    from app.read_cache import cache_scope
    row = db.scalar(select(ReadCache).options(defer(ReadCache.payload, raiseload=True)).where(
        ReadCache.key == item.id[6:], ReadCache.owner_id == owner.id, ReadCache.scope == cache_scope(db, owner)).with_for_update())
    if row is None or not row.dirty:
        return None
    if not row.lease_until or row.lease_until <= clock:
        row.refresh_after = min(row.refresh_after, clock)
    return item.model_copy(update=dict(state='working', advice='已交给页面更新进程，等待新页面数据通过校验。'))


def repair_route(db, item, clock, *, verify=False):
    from app.integrations.routing import collection_allowed
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.protocol import SHANGHAI
    from app.market.resilience import backup, binding_reason, evaluate, latest_run, saved_quality
    from app.market.resilience_models import DataHealth
    row = db.get(DataHealth, UUID(item.id[6:]))
    source, config = db.get(Provider, item.source_id), db.get(SourceSettings, item.source_id)
    if (not row or not source or not config or not source.enabled or config.paused_reason or
            row.primary_source_id != source.id or row.owner_id != config.owner_id or
            row.configuration_sha256 != configuration_fingerprint(source, config) or
            not collection_allowed(db, source.family) or row.trade_day != clock.astimezone(SHANGHAI).date()):
        return item.model_copy(update=dict(state='waiting', advice='供数日期或配置已变化，请查看当前来源状态。'))
    if not verify:
        lock(db, 'data-health:' + str(source.id))
        evaluate(db, source, config, latest_run(db, source.id, clock), clock, allow_actions=False)
        db.flush()
    if row.state == 'normal' and row.active_source_id == source.id and row.last_valid_at:
        run = latest_run(db, source.id, clock)
        quality = saved_quality(db, source, config, row.capability, row.trade_day, clock, run)
        if run and quality.valid and not binding_reason(db, source, run):
            return item.model_copy(update=dict(state='verified', advice='已核对该范围的主来源有效数据，当前可供数。'))
    if row.state == 'fallback':
        alternate = backup(db, source, config, row, clock)
        if alternate and alternate[0].id == row.active_source_id:
            return item.model_copy(update=dict(state='verified', backup_source_id=alternate[0].id,
                advice='已核实「' + alternate[0].name + '」有同范围有效数据并已接手供数，主来源继续复查。'))
    return item.model_copy(update=dict(state='waiting', advice='当前没有同范围、可验证的备用数据，等待主来源或可用备源。'))


def repair(db, owner, *, progress=None):
    from app.system_settings.service import audit
    admit(db)
    lock(db, 'system:inspection:repair')
    db.execute(text("SET LOCAL lock_timeout = '2s'"))
    clock = now(db)
    previous = last_batch(db, owner, clock)
    if previous and previous.started_at > clock - timedelta(seconds=45):
        return verify_batch(db, owner, previous), 'complete', []
    scan = inspect(db, owner, include_receipt=False)
    batch = RepairBatch(started_at=clock)
    allowance = [settings().recovery_jobs_per_round]
    from app.market.service import source_states
    sources = {source.provider.id: source for source in source_states(db)}
    planned = []
    for finding in scan.findings:
        source = sources.get(finding.source_id)
        follow_up = (finding.area == 'source' and finding.state == 'waiting' and finding.retry_at
            and source and source.last_attempt and source.last_attempt.status in {'partial','error','unavailable','empty'})
        if finding.state != 'available' and not follow_up:
            continue
        planned.append((finding, source))
    for index, (finding, source) in enumerate(planned):
        if progress:
            progress(index, len(planned), finding.title)
        item = RepairReceipt(**finding.model_dump())
        try:
            with db.begin_nested():
                if item.area == 'source':
                    results = [repair_source(db, item, clock, source)]
                elif item.area == 'history':
                    results = repair_history(db, item, clock, allowance) if allowance[0] > 0 else []
                elif item.area == 'cache':
                    result = repair_cache(db, owner, item, clock)
                    results = [result] if result else []
                elif item.area == 'route':
                    results = [repair_route(db, item, clock)]
                else:
                    results = []
        except (APIError, SourceFailure) as err:
            code = err.code if isinstance(err, APIError) else err.reason
            status = 'waiting' if code in WAIT_REASONS else 'manual'
            results = [item.model_copy(update=dict(state=status, reason=code,
                advice=err.message if isinstance(err, APIError) else guidance(code)))]
        except OperationalError as err:
            if getattr(err.orig, 'sqlstate', None) not in {'55P03','40P01','57014'}:
                raise
            results = [item.model_copy(update=dict(state='waiting', advice='同一范围正在被后台核对，等待下一轮对账。'))]
        batch.actions.extend(results)
    if progress:
        progress(len(planned), len(planned), None)
    audit(db, owner, 'system.inspection.repair', resource_type='inspection')
    return batch, 'complete', []


def verify_source(db, item, batch, provider, config):
    from app.market.minute_backfill import live_run_condition
    from app.market.resilience import binding_reason
    if signature(db, provider, config) != item.configuration:
        return item.model_copy(update=dict(state='manual', advice='来源配置已变化，请重新检修。'))
    query = select(DataFetchRun).where(DataFetchRun.source_id == item.source_id,
        DataFetchRun.received_at >= batch.started_at)
    if item.capability:
        query = query.where(DataFetchRun.capability == item.capability)
    job = db.get(Job, item.job_id) if item.job_id else None
    if item.job_id:
        if not job or job.owner_id != config.owner_id:
            return item.model_copy(update=dict(state='manual', advice='任务归属已变化，请重新检修。'))
        query = query.where(DataFetchRun.job_id == item.job_id)
    elif item.state == 'waiting':
        # A scheduled follow-up may not have a job yet. Only a new ordinary
        # collection of the exact owner, source and call type may satisfy it.
        query = query.join(Job, Job.id == DataFetchRun.job_id).where(Job.owner_id == config.owner_id,
            Job.kind.in_(['source.collect','review.collect']), Job.payload['source_id'].astext == str(item.source_id),
            Job.payload['integration_context']['probe'].astext.is_distinct_from('true'),
            DataFetchRun.started_at >= batch.started_at, live_run_condition(by_job=True))
    else:
        return item
    run = db.scalar(query.order_by(DataFetchRun.received_at.desc(), DataFetchRun.id.desc()).limit(1))
    if not job and run:
        job = db.get(Job, run.job_id)
    if not job:
        return item
    if job.state == 'running' or job.state == 'queued' and job.available_at <= now(db):
        return item.model_copy(update=dict(state='working', job_id=job.id, advice='采集任务已就绪，正在等待有效数据回执。'))
    if (job.state == 'succeeded' and run and run.status == 'complete' and run.request_count > 0
            and not binding_reason(db, provider, run) and not run.clock_regressed
            and (run.accepted or 0) + (run.duplicates or 0) > 0 and not run.rejected
            and (run.expected is None or (run.accepted or 0) + (run.duplicates or 0) >= run.expected)):
        return item.model_copy(update=dict(state='verified', job_id=job.id, retry_at=None,
            advice='对应采集任务已完成，已核验本次有效数据回执。'))
    if job.state in {'succeeded','failed','cancelled'}:
        reason = (run.reason if run else None) or job.error_code
        if item.job_id and reason in WAIT_REASONS and job.completed_at:
            # A rate-limited job can end before the regular scheduler obtains the next receipt.
            waiting = item.model_copy(update=dict(state='waiting', reason=reason, job_id=None, advice=guidance(reason)))
            after = max(batch.started_at, job.completed_at + timedelta(microseconds=1))
            return verify_source(db, waiting, batch.model_copy(update=dict(started_at=after)), provider, config)
        return item.model_copy(update=dict(state='waiting' if reason in WAIT_REASONS else 'manual',
            reason=reason, advice=guidance(reason) if reason else '本次任务没有完整的有效数据回执，请核对该来源的采集记录。'))
    return item


def verify_batch(db, owner, batch):
    from app.integrations.credentials import resolve_source_credentials
    from app.market.service import permission_reason
    from app.read_cache import cache_scope
    clock = now(db)
    cache_keys = [item.id[6:] for item in batch.actions if item.area == 'cache']
    caches = {}
    if cache_keys:
        query = select(ReadCache).where(ReadCache.key.in_(cache_keys), ReadCache.owner_id == owner.id,
            ReadCache.scope == cache_scope(db, owner)).options(defer(ReadCache.payload, raiseload=True))
        caches = {row.key: row for row in db.scalars(query)}
    results = []
    for item in batch.actions:
        status, advice = item.state, item.advice
        if item.area == 'route':
            results.append(repair_route(db, item, clock, verify=True))
            continue
        if item.source_id:
            config = db.get(SourceSettings, item.source_id)
            provider = db.get(Provider, item.source_id)
            if not config or not provider or permission_reason(provider) or resolve_source_credentials(db, provider.id, provider.family).reason:
                results.append(item.model_copy(update=dict(state='manual', advice='来源已停用或授权已变化，请重新检查配置。')))
                continue
        if item.area == 'cache' and item.state in {'working','waiting','verified'}:
            cache = caches.get(item.id[6:])
            validated = bool(cache and cache.built_at and cache.revision and cache.validated_at and
                             cache.validated_at >= batch.started_at and not cache.last_error)
            if validated and not cache.dirty:
                status, advice = 'verified', '页面数据已通过本次校验。'
            elif not cache:
                status, advice = 'manual', '页面缓存范围已变化，打开对应页面后重新检修。'
            elif cache.lease_until and cache.lease_until > clock:
                status, advice = 'working', '页面更新服务正在处理这一项，完成后会自动核验。'
            elif cache.last_attempt_at and cache.last_attempt_at >= batch.started_at and cache.last_error:
                status, advice = 'waiting', '上次页面更新失败，已由页面更新服务排队重试。'
            elif validated:
                status, advice = 'waiting', '页面已校验过一次，随后有新数据进入，正在排队更新。'
            else:
                status, advice = 'waiting', '已排队等待页面更新，轮到此项后会自动核验。'
            retry = cache.refresh_after if cache and status == 'waiting' and cache.refresh_after > clock else None
            results.append(item.model_copy(update=dict(state=status, advice=advice, retry_at=retry)))
            continue
        elif item.coverage_id:
            row = db.get(RecoveryCoverage, item.coverage_id)
            if (not row or row.source_id != item.source_id or row.owner_id != config.owner_id
                    or row.configuration_sha256 != item.coverage_signature or signature(db, provider, config) != item.configuration):
                status, advice = 'manual', '原采集范围或授权已变化，请重新检修。'
            elif row.checked_at >= batch.started_at and row.state == 'complete' and (row.actual_count or 0) > 0:
                status, advice = 'verified', '已按原日期、来源和范围验收完整入库结果。'
            elif row.state in {'blocked','unsupported'}:
                status, advice = 'manual', guidance(row.reason)
            elif row.state in {'partial','missing','waiting'}:
                status, advice = 'waiting', '原日期仍有缺口，等待可回溯的原始数据。'
            elif item.job_id:
                job = db.get(Job, item.job_id)
                if job and job.state in {'failed','cancelled'}:
                    status, advice = 'waiting', guidance(job.error_code)
        elif item.area == 'source' and item.configuration:
            results.append(verify_source(db, item, batch, provider, config))
            continue
        results.append(item.model_copy(update=dict(state=status, advice=advice)))
    return RepairBatch(started_at=batch.started_at, actions=results)
