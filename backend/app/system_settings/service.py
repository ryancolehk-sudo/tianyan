"""Pure local control-plane operations. No source, model, or Docker calls."""
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.auth import APIError, digest
from app.config import settings
from app.evidence.common import lock
from app.models import AuditEvent, LoginThrottle, WorkerHeartbeat
from app.read_cache import cache_scope
from app.read_cache_models import ReadCache
from app.schemas import Preferences
from app.system_settings import schemas as s
from app.system_settings.models import BackupRecord, MaintenanceJob, SystemState, UpdateState, UserPreference
from app.version import APP_VERSION

ACTIVE = ('queued', 'running')
AUDIT_DETAILS = {'count', 'kind', 'stage', 'start_page', 'review_lookback', 'news_category',
                 'revision', 'target', 'release_id', 'version', 'error_code', 'backup_id',
                 'integrity_state', 'recovery_state', 'changed_fields'}
RESULT_FIELDS = {'backup_id', 'release_id', 'version', 'integrity_state', 'recovery_state',
                 'size_bytes', 'schema_revision', 'build_revision', 'restart_required', 'rollback',
                 'manual_recovery', 'count'}
ACTION_LABELS = {
    'session.login': '登录', 'session.login_failed': '登录失败', 'session.logout': '退出登录',
    'settings.preferences': '保存使用偏好', 'settings.password': '修改密码',
    'settings.password_failed': '密码验证失败', 'settings.sessions_revoke': '撤销登录会话',
    'settings.cache_clear': '清理页面缓存', 'maintenance.backup_queued': '提交本机备份',
    'maintenance.backup_completed': '完成本机备份', 'maintenance.backup_failed': '本机备份失败',
    'update.check': '检查版本更新', 'update.download': '下载更新', 'update.install': '安装更新',
    'update.completed': '升级完成', 'update.failed': '升级失败',
}


def safe_details(value, allowed=AUDIT_DETAILS):
    result = {}
    for key, item in (value.items() if isinstance(value, dict) else []):
        if key not in allowed:
            continue
        if item is None or isinstance(item, (bool, int, float)):
            result[key] = item
        elif isinstance(item, str):
            result[key] = item[:200]
        elif key == 'changed_fields' and isinstance(item, list):
            result[key] = [v for v in item[:20] if v in {'start_page', 'review_lookback', 'news_category'}]
    return result


def audit(db, owner, action, *, request_id=None, outcome='succeeded', context=None,
          resource_id=None, resource_type=None):
    """Old audit columns remain usable by historical application fixtures."""
    actor_id = getattr(owner, 'id', owner)
    row = AuditEvent(id=uuid.uuid4(), actor_id=actor_id, action=action,
                     request_id=request_id, resource_id=resource_id, resource_type=resource_type)
    db.add(row)
    db.flush()
    if db.scalar(text("SELECT to_regclass('user_preferences')")):
        db.execute(text('UPDATE audit_events SET outcome=:outcome,context=CAST(:context AS jsonb) WHERE id=:id'),
                   {'id': row.id, 'outcome': outcome, 'context': json.dumps(safe_details(context), ensure_ascii=False)})
    return row


def preferences(db, user):
    # Legacy upgrade fixtures deliberately serve sessions against pre-0017 DBs.
    if not db.scalar(text("SELECT to_regclass('user_preferences')")):
        return Preferences()
    row = db.get(UserPreference, getattr(user, 'id', user))
    return Preferences.model_validate(row) if row else Preferences()


def save_preferences(db, owner, body, request_id):
    lock(db, f'preferences:{owner.id}')
    row = db.get(UserPreference, owner.id)
    current = row.revision if row else 1
    if current != body.expected_revision:
        raise APIError(409, 'preferences_changed', '使用偏好已在其他页面修改，请重新读取后再保存。')
    if row is None:
        row = UserPreference(user_id=owner.id)
        db.add(row)
    changed = []
    for field in ('start_page', 'review_lookback', 'news_category'):
        if getattr(row, field) != getattr(body, field):
            changed.append(field)
        setattr(row, field, getattr(body, field))
    row.revision = current + 1
    row.updated_at = datetime.now(UTC)
    db.flush()
    audit(db, owner, 'settings.preferences', request_id=request_id,
          context={'revision': row.revision, 'changed_fields': changed})
    return Preferences.model_validate(row)


def password_allowed(db, user_id):
    moment = datetime.now(UTC)
    stmt = insert(LoginThrottle).values(bucket=digest(f'password-change:{user_id}'), attempts=1,
                                       resets_at=moment + timedelta(minutes=5))
    stmt = stmt.on_conflict_do_update(index_elements=[LoginThrottle.bucket], set_={
        'attempts': case((LoginThrottle.resets_at <= moment, 1), else_=LoginThrottle.attempts + 1),
        'resets_at': case((LoginThrottle.resets_at <= moment, moment + timedelta(minutes=5)), else_=LoginThrottle.resets_at),
    }).returning(LoginThrottle.attempts)
    attempts = db.execute(stmt).scalar_one()
    db.commit()
    if attempts > 5:
        raise APIError(429, 'password_limited', '密码验证次数过多，请五分钟后再试。')


def get_system_state(db):
    db.execute(insert(SystemState).values(id=1, active=False, stage='idle').on_conflict_do_nothing(index_elements=['id']))
    return db.get(SystemState, 1)


def get_update_state(db):
    db.execute(insert(UpdateState).values(id=1, status='unchecked', message='尚未检查更新。').on_conflict_do_nothing(index_elements=['id']))
    return db.get(UpdateState, 1)


def connected(value, *, moment=None):
    return bool(value and value >= (moment or datetime.now(UTC)) - timedelta(seconds=90))


def job_dto(row):
    values = {key: getattr(row, key) for key in s.MaintenanceJob.model_fields}
    values['result'] = safe_details(row.result, RESULT_FIELDS) if row.result is not None else None
    return s.MaintenanceJob.model_validate(values)


def backup_dto(row):
    return s.Backup.model_validate(row)


def create_job(db, owner, kind, params=None, *, key=None, request_id=None):
    if kind not in {'backup', 'update.download', 'update.install'}:
        raise ValueError('unsupported maintenance job')
    lock(db, 'maintenance:queue')
    if kind in {'backup', 'update.install'}:
        existing = db.scalar(select(MaintenanceJob.id).where(MaintenanceJob.kind.in_(['backup', 'update.install']),
                                                           MaintenanceJob.status.in_(ACTIVE)).limit(1))
        if existing:
            raise APIError(409, 'maintenance_busy', '已有备份或升级任务，请等待完成后再试。')
    elif db.scalar(select(MaintenanceJob.id).where(MaintenanceJob.kind == kind,
                                                  MaintenanceJob.status.in_(ACTIVE)).limit(1)):
        raise APIError(409, 'download_busy', '已有更新下载任务，请等待完成后再试。')
    job = MaintenanceJob(owner_id=getattr(owner, 'id', owner), kind=kind, status='queued', stage='queued',
                         message='等待备份进程处理。' if kind == 'backup' else '等待更新进程处理。',
                         params=params or {}, idempotency_key=key)
    db.add(job)
    db.flush()
    audit(db, owner, 'maintenance.backup_queued' if kind == 'backup' else kind,
          request_id=request_id, resource_id=job.id, resource_type='maintenance_job', context={'kind': kind})
    return job


def cache_stats(db, owner):
    scope = cache_scope(db, owner)
    if scope is None:
        return s.CacheStats(count=0, bytes=0, enabled=False)
    count, size = db.execute(select(func.count(ReadCache.key), func.coalesce(func.sum(func.pg_column_size(ReadCache.payload)), 0))
                            .where(ReadCache.owner_id == owner.id, ReadCache.scope == scope)).one()
    return s.CacheStats(count=count, bytes=size, enabled=True)


def clear_cache(db, owner, request_id):
    scope = cache_scope(db, owner)
    removed = db.execute(delete(ReadCache).where(ReadCache.owner_id == owner.id, ReadCache.scope == scope)) if scope else None
    audit(db, owner, 'settings.cache_clear', request_id=request_id,
          context={'count': removed.rowcount if removed else 0})
    # Deleting lease-bearing rows fences in-flight publication. A later fresh
    # request gets a different lease token; dependency versions are untouched.
    return s.CacheStats(count=0, bytes=0, enabled=settings().read_cache_enabled)


def overview(db, owner):
    moment = datetime.now(UTC)
    state = get_system_state(db)
    current_update = get_update_state(db)
    latest = db.scalar(select(BackupRecord).order_by(BackupRecord.created_at.desc(), BackupRecord.id.desc()).limit(1))
    heartbeat = db.scalar(select(func.max(WorkerHeartbeat.seen_at)).where(WorkerHeartbeat.worker_id.not_like('ai:%')))
    health = [s.HealthItem(id='database', label='资料库', state='ok', checked_at=moment, message='本机数据库可读取。'),
              s.HealthItem(id='collector', label='采集进程', state='ok' if connected(heartbeat, moment=moment) else 'warning', checked_at=moment,
                           message='采集进程最近在线。' if connected(heartbeat, moment=moment) else '尚未收到近期采集心跳。'),
              s.HealthItem(id='backup', label='备份进程', state='ok' if connected(state.backup_heartbeat_at, moment=moment) else 'unavailable', checked_at=moment,
                           message='备份进程已连接。' if connected(state.backup_heartbeat_at, moment=moment) else '备份进程未连接，请先启动本机维护服务。'),
              s.HealthItem(id='updater', label='本机升级器', state='ok' if connected(state.updater_heartbeat_at, moment=moment) else 'unavailable', checked_at=moment,
                           message='升级器已连接。' if connected(state.updater_heartbeat_at, moment=moment) else '升级器未连接，可查看手动升级指引。')]
    configured = bool(settings().update_feed_url and settings().update_public_key)
    return s.Overview(version=APP_VERSION, app_mode=settings().app_mode, health=health,
        cache=cache_stats(db, owner), last_backup=backup_dto(latest) if latest and owner.role == 'admin' else None,
        maintenance=s.MaintenanceSummary(active=state.active, stage=state.stage,
            updater_connected=connected(state.updater_heartbeat_at), backup_connected=connected(state.backup_heartbeat_at)),
        update=s.UpdateSummary(configured=configured, state=current_update.status if configured else 'unconfigured'))


def action_label(action):
    return ACTION_LABELS.get(action, '系统操作' if action.startswith(('settings.', 'maintenance.', 'update.')) else '已记录的操作')
