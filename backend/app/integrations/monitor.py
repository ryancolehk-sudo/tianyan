"""Unify existing collection records with model attempts without rewriting either."""
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select

from app.ai.models import AiAttempt, AiRun, AiSettings, Connection
from app.auth import APIError
from app.evidence.common import cursor_scope, decode_cursor, encode_cursor
from app.integrations.monitor_schemas import (
    CallRecord,
    Calls,
    Issue,
    Overview,
    ServiceStatus,
    TaskStatus,
)
from app.market.models import DataFetchRun
from app.models import AuditEvent, Job, Provider, WorkerHeartbeat

SHANGHAI = ZoneInfo('Asia/Shanghai')
SUCCESS = ('succeeded', 'success', 'complete', 'partial', 'empty')
FAILURE = ('failed', 'error', 'unavailable', 'unknown', 'result_unknown')


def _views(db, owner=None):
    from app.ai.service import model_credential_health
    from app.integrations.data import capabilities, virtual_connections
    from app.model_connections import virtual_connections as legacy_models
    saved = list(db.scalars(select(Connection)))
    occupied = {str(row.id) for row in saved}
    views = [row for row in virtual_connections(db) + legacy_models(db) if str(row['id']) not in occupied]
    for connection in saved:
        if connection.owner_id is not None and (owner is None or connection.owner_id != owner.id):
            continue
        if not connection.archived:
            views.append({'id': connection.id, 'name': connection.name, 'kind': connection.kind, 'template': connection.template,
                          'enabled': connection.enabled, 'source_ids': connection.source_ids,
                          'revision': connection.revision, 'tested_revision': connection.tested_revision,
                          'last_test': connection.last_test,
                          'credential_error_code': model_credential_health(connection)['error_code'] if connection.kind == 'model' else None,
                          'capabilities': capabilities(db, connection) if connection.kind != 'model' else []})
    return views


def _timestamp(value):
    if not value:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def _source_map(views):
    return {str(source): view for view in views for source in view.get('source_ids', [])}


def _max_time(values):
    actual = [_timestamp(value) for value in values if value]
    return max(actual) if actual else None


def _latest_data(values):
    # Compare recorded source dates without turning date precision into a fake clock time.
    candidates = []
    for value in values:
        if not value:
            continue
        text = str(value)
        try:
            if len(text) in {8, 10} and 'T' not in text and ':' not in text:
                day = date.fromisoformat(text)
                candidates.append((datetime.combine(day, time.min, SHANGHAI), day.isoformat()))
            else:
                stamp = _timestamp(text)
                if stamp.tzinfo is None:
                    # Source-native local times are displayed as provided, never replaced by received_at.
                    stamp = stamp.replace(tzinfo=SHANGHAI)
                candidates.append((stamp, text))
        except (ValueError, TypeError):
            continue
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _data_state(caps):
    active = [entry for entry in caps if entry.get('enabled')]
    if not active:
        return 'disabled' if caps else 'not_checked'
    if all(entry.get('collection_enabled') is False for entry in active):
        return 'collection_disabled'
    states = {entry['state'] for entry in active}
    if states == {'ready'}:
        return 'ready'
    if len(states) == 1:
        return next(iter(states))
    # A successful child does not establish coverage for a partial, empty, failed or untested sibling.
    return 'partial'


def overview(db, owner):
    views = _views(db, owner)
    source_map = _source_map(views)
    clock = datetime.now(UTC)
    start = datetime.combine(clock.astimezone(SHANGHAI).date(), time.min, SHANGHAI)
    data_requests = db.scalar(select(func.coalesce(func.sum(DataFetchRun.request_count), 0)).where(
        DataFetchRun.started_at >= start, DataFetchRun.started_at < start + timedelta(days=1)))
    private_connections = select(Connection.id).where(Connection.owner_id.is_not(None), Connection.owner_id != owner.id)
    ai_scope = or_(AiAttempt.owner_id == owner.id, AiAttempt.connection_id.is_(None), AiAttempt.connection_id.not_in(private_connections)) if owner.role == 'admin' else AiAttempt.owner_id == owner.id
    ai_requests = db.scalar(select(func.coalesce(func.sum(AiAttempt.request_count), 0)).where(
        AiAttempt.sent_at >= start, AiAttempt.sent_at < start + timedelta(days=1), ai_scope))
    services, issues = [], []
    for view in views:
        ident = UUID(str(view['id']))
        if view['kind'] == 'model':
            latest = db.scalar(select(AiAttempt).where(AiAttempt.connection_id == ident, AiAttempt.purpose != 'discover').order_by(
                AiAttempt.started_at.desc(), AiAttempt.id.desc()).limit(1))
            successful = db.scalar(select(func.max(AiAttempt.completed_at)).where(
                AiAttempt.connection_id == ident, AiAttempt.state.in_(SUCCESS), AiAttempt.purpose != 'discover'))
            test = view.get('last_test') or {}
            current_test = test.get('revision') == view.get('revision')
            verified = current_test and test.get('status') == 'success'
            status = ('disabled' if not view['enabled'] else latest.state if verified and latest
                      else 'error' if current_test and test.get('status') == 'error' else 'not_checked')
            attempt_at = latest.started_at if latest else None
            error = latest.error_code if latest and latest.state in FAILURE else None
            if view.get('credential_error_code'):
                error = view['credential_error_code']
                status = 'unavailable' if view['enabled'] else 'disabled'
            latest_data = None
            modules = ['AI 功能']
        elif view.get('template') == 'ifind_mcp':
            latest = db.scalar(select(AuditEvent).where(AuditEvent.resource_id == ident,
                AuditEvent.action.in_(['ifind.success', 'ifind.error', 'ifind.unknown'])).order_by(AuditEvent.created_at.desc()).limit(1))
            successful = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.resource_id == ident, AuditEvent.action == 'ifind.success'))
            status = 'disabled' if not view['enabled'] else 'standby' if not latest or latest.action == 'ifind.success' else latest.action.split('.')[-1]
            attempt_at = latest.created_at if latest else None
            error = (latest.changed_field_names or [None, None])[-1] if latest and latest.action != 'ifind.success' else None
            latest_data, modules = None, ['iFinD 手动研究']
        else:
            caps = view.get('capabilities', [])
            status = 'disabled' if not view['enabled'] else _data_state(caps)
            successful = _max_time(entry.get('last_success_at') for entry in caps)
            latest_data = _latest_data(entry.get('data_time') for entry in caps)
            ids = [UUID(str(value)) for value in view.get('source_ids', [])]
            latest = db.scalar(select(DataFetchRun).where(DataFetchRun.source_id.in_(ids)).order_by(
                DataFetchRun.started_at.desc(), DataFetchRun.id.desc()).limit(1)) if ids else None
            attempt_at = latest.started_at if latest else None
            error = latest.reason if latest and latest.status not in SUCCESS else None
            modules = ['资讯中心'] if view['kind'] == 'news' else ['工作台', '每日复盘']
        services.append(ServiceStatus(id=ident, name=view['name'], kind=view['kind'], status=status,
                                      latest_data_at=latest_data, last_success_at=successful,
                                      last_attempt_at=attempt_at, error_code=error, impact_modules=modules))
        if view['enabled'] and (error or status in {'error', 'partial', 'unavailable', 'credentials_missing', 'permission_missing'}):
            issues.append(Issue(id='service:' + str(ident), title=view['name'] + '需要处理',
                                message=error or status, connection_id=ident,
                                retry_at=getattr(latest, 'retry_at', None) or getattr(latest, 'retry_not_before', None)))
    active_stmt = select(Job).where(Job.state.in_(['queued', 'running']))
    if owner.role != 'admin':
        active_stmt = active_stmt.where(Job.owner_id == owner.id)
    else:
        hidden_jobs = select(AiRun.job_id).join(Connection, Connection.id == AiRun.connection_id).where(
            Connection.owner_id.is_not(None), Connection.owner_id != owner.id)
        active_stmt = active_stmt.where(Job.id.not_in(hidden_jobs))
    active = list(db.scalars(active_stmt.order_by(Job.created_at, Job.id).limit(50)))
    run_by_job = {row.job_id: row for row in db.scalars(select(AiRun).where(AiRun.job_id.in_([j.id for j in active])))}
    providers = {str(row.id): row.name for row in db.scalars(select(Provider))}
    tasks = []
    for job in active:
        run = run_by_job.get(job.id)
        source = str(job.payload.get('source_id', ''))
        context = job.payload.get('review_context', {})
        view = source_map.get(source)
        tasks.append(TaskStatus(id=job.id, kind='ai' if run else 'data' if source or context else job.kind,
                                feature=run.feature if run else context.get('dataset', job.kind),
                                service_name=next((v['name'] for v in views if run and str(v['id']) == str(run.connection_id)), '')
                                if run else (view or {}).get('name', providers.get(source, '后台任务')),
                                state=run.state if run else job.state,
                                stage=run.stage if run else '等待执行' if job.state == 'queued' else '正在采集与校验',
                                created_at=job.created_at, completed_at=job.completed_at, error_code=job.error_code))
    ai_settings = db.get(AiSettings, 1)
    if ai_settings and ai_settings.enabled:
        seen = db.scalar(select(func.max(WorkerHeartbeat.seen_at)).where(WorkerHeartbeat.worker_id.like('ai:%')))
        if not seen or seen < clock - timedelta(seconds=90):
            issues.append(Issue(id='ai-worker', title='AI 执行器未就绪',
                                message='尚未收到近期心跳，生成任务会保留在队列中。'))
    running_stmt = select(func.count()).select_from(Job).where(Job.state == 'running')
    if owner.role != 'admin':
        running_stmt = running_stmt.where(Job.owner_id == owner.id)
    else:
        running_stmt = running_stmt.where(Job.id.not_in(hidden_jobs))
    return Overview(services_count=len(services), running_tasks=db.scalar(running_stmt) or 0,
                    today_requests=int(data_requests or 0) + int(ai_requests or 0), pending_issues=len(issues),
                    services=services, tasks=tasks, issues=issues, updated_at=clock)


def calls(db, owner, *, kind=None, connection_id=None, errors_only=False, from_date=None,
          to_date=None, cursor=None, limit=50):
    if from_date and to_date and from_date > to_date:
        raise APIError(422, 'invalid_date_range', '开始日期不能晚于结束日期。')
    filters = {'resource': 'data_center_calls', 'kind': kind, 'connection_id': str(connection_id) if connection_id else None,
               'errors_only': errors_only, 'from_date': str(from_date) if from_date else None,
               'to_date': str(to_date) if to_date else None}
    scope = cursor_scope(owner.id, filters)
    position = decode_cursor(cursor, scope)
    views = _views(db, owner)
    source_map = _source_map(views)
    records = []

    def bounds(stmt, model):
        if from_date:
            stmt = stmt.where(model.started_at >= datetime.combine(from_date, time.min, SHANGHAI))
        if to_date:
            stmt = stmt.where(model.started_at < datetime.combine(to_date + timedelta(days=1), time.min, SHANGHAI))
        if position:
            stamp, ident = position
            stmt = stmt.where(or_(model.started_at < stamp, and_(model.started_at == stamp, model.id < ident)))
        return stmt.order_by(model.started_at.desc(), model.id.desc()).limit(limit + 1)

    if kind != 'ai':
        recorded_connection = Job.payload['integration_context']['connection_id'].astext
        stmt = select(DataFetchRun).outerjoin(Job, Job.id == DataFetchRun.job_id)
        if connection_id:
            view = next((v for v in views if str(v['id']) == str(connection_id)), None)
            ids = [UUID(str(value)) for value in view.get('source_ids', [])] if view else []
            # Archived connections remain queryable through their stored binding.
            archived = db.get(Connection, connection_id) if not view else None
            if archived:
                ids = [UUID(str(value)) for value in archived.source_ids]
            stmt = stmt.where(or_(recorded_connection == str(connection_id),
                                 and_(recorded_connection.is_(None), DataFetchRun.source_id.in_(ids))))
        if errors_only:
            stmt = stmt.where(or_(DataFetchRun.status.in_(('error', 'unavailable')),
                                 and_(DataFetchRun.reason.is_not(None), DataFetchRun.reason != 'review_page_pending')))
        rows = list(db.scalars(bounds(stmt, DataFetchRun)))
        providers = {row.id: row for row in db.scalars(select(Provider).where(Provider.id.in_([r.source_id for r in rows])))}
        jobs = {row.id: row for row in db.scalars(select(Job).where(Job.id.in_([r.job_id for r in rows if r.job_id])))}
        historic = {str(row.id): row for row in db.scalars(select(Connection))}
        for row in rows:
            job = jobs.get(row.job_id)
            context = (job.payload or {}).get('integration_context', {}) if job else {}
            recorded_id = context.get('connection_id')
            stored = historic.get(recorded_id)
            service = ({'id': recorded_id, 'name': stored.name if stored else '历史接入服务'} if recorded_id
                       else source_map.get(str(row.source_id)))
            provider = providers.get(row.source_id)
            records.append(CallRecord(id=row.id, kind='data', task=row.capability,
                service_name=service['name'] if service else provider.name if provider else '历史数据服务',
                connection_id=UUID(str(service['id'])) if service else None,
                job_id=row.job_id, status=row.status, started_at=row.started_at, completed_at=row.received_at,
                elapsed_ms=row.elapsed_ms, request_count=row.request_count, error_code=row.reason,
                retry_at=row.retry_not_before, cost_cny=format(row.cost_cny, 'f') if row.cost_cny is not None else None,
                details={'source_id': str(row.source_id), 'capability': row.capability,
                         'accepted': row.accepted, 'rejected': row.rejected, 'duplicates': row.duplicates,
                         'connection_attribution': '执行时绑定' if recorded_id else '旧记录按来源查询',
                         'record_granularity': '采集批次；请求数按原始记录保留'}))
    if kind != 'data':
        stmt = select(AiAttempt)
        if owner.role != 'admin':
            stmt = stmt.where(AiAttempt.owner_id == owner.id)
        else:
            others_personal = select(Connection.id).where(Connection.owner_id.is_not(None), Connection.owner_id != owner.id)
            stmt = stmt.where(or_(AiAttempt.owner_id == owner.id, AiAttempt.connection_id.is_(None), AiAttempt.connection_id.not_in(others_personal)))
        if connection_id:
            stmt = stmt.where(AiAttempt.connection_id == connection_id)
        if errors_only:
            stmt = stmt.where(or_(AiAttempt.state.in_(FAILURE), AiAttempt.error_code.is_not(None)))
        rows = list(db.scalars(bounds(stmt, AiAttempt)))
        runs = {row.id: row for row in db.scalars(select(AiRun).where(AiRun.id.in_([r.run_id for r in rows if r.run_id])))}
        for row in rows:
            run = runs.get(row.run_id)
            records.append(CallRecord(id=row.id, kind='ai', task=run.feature if run else {'probe': '模型测试', 'discover': '获取模型'}.get(row.purpose, row.purpose),
                service_name=row.service_name, connection_id=row.connection_id, job_id=run.job_id if run else None,
                status=row.state, started_at=row.started_at, completed_at=row.completed_at,
                elapsed_ms=row.elapsed_ms, request_count=row.request_count, error_code=row.error_code,
                retry_at=row.retry_at, input_tokens=row.input_tokens, output_tokens=row.output_tokens,
                cost_cny=format(row.cost_cny, 'f') if row.cost_cny is not None else None,
                details={'purpose': row.purpose, 'model': row.model, 'run_id': str(row.run_id) if row.run_id else None,
                         **(row.safe_details or {})}))
    records.sort(key=lambda row: (row.started_at, str(row.id)), reverse=True)
    more = len(records) > limit
    visible = records[:limit]
    next_cursor = encode_cursor(SimpleNamespace(id=visible[-1].id, started_at=visible[-1].started_at), scope, 'started_at') if more else None
    return Calls(items=visible, next_cursor=next_cursor)
