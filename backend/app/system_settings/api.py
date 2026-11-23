import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select, text, tuple_, update
from sqlalchemy.orm import Session

from app.auth import APIError, COOKIE, csrf, hasher, principal, require_role, verify_password
from app.config import settings
from app.db import db_session
from app.evidence.api import business_key
from app.evidence.common import cursor_scope, decode_cursor, encode_cursor, write_once
from app.http import envelope
from app.models import AuditEvent, LoginSession, User
from app.schemas import Envelope, Error, Page, Preferences
from app.system_settings import schemas as s, service
from app.system_settings.models import BackupRecord, MaintenanceJob

router = APIRouter()
READ_ERRORS = {401: {'model': Error}, 403: {'model': Error}, 404: {'model': Error},
               422: {'model': Error}, 503: {'model': Error}}
WRITE_ERRORS = {**READ_ERRORS, 409: {'model': Error}, 429: {'model': Error}}


@router.get('/api/v1/me/preferences', response_model=Envelope[Preferences], responses=READ_ERRORS, operation_id='getPreferences')
def get_preferences(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    return envelope(request, service.preferences(db, identity[0]))


@router.put('/api/v1/me/preferences', response_model=Envelope[Preferences], responses=WRITE_ERRORS, operation_id='savePreferences')
def save_preferences(body: s.PreferencesInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    def create(db):
        return service.save_preferences(db, identity[0], body, request.state.request_id), 'complete', []
    return write_once(request, identity[0], key, body, create, status=200)


@router.post('/api/v1/me/password', status_code=204, responses={**WRITE_ERRORS, 400: {'model': Error}}, operation_id='changePassword')
def change_password(body: s.PasswordInput, request: Request, response: Response,
                    identity=Depends(csrf), db: Session = Depends(db_session)):
    owner = identity[0]
    service.password_allowed(db, owner.id)
    owner = db.scalar(select(User).where(User.id == owner.id).with_for_update().execution_options(populate_existing=True))
    if not verify_password(owner.password_hash, body.current_password):
        service.audit(db, owner, 'settings.password_failed', request_id=request.state.request_id, outcome='failed')
        db.commit()
        raise APIError(400, 'password_incorrect', '当前密码不正确。')
    if body.current_password == body.new_password:
        raise APIError(422, 'password_unchanged', '新密码不能与当前密码相同。')
    owner.password_hash = hasher.hash(body.new_password)
    db.execute(update(LoginSession).where(LoginSession.user_id == owner.id, LoginSession.revoked_at.is_(None))
               .values(revoked_at=datetime.now(UTC)))
    service.audit(db, owner, 'settings.password', request_id=request.state.request_id)
    db.commit()
    response.delete_cookie(COOKIE, path='/', secure=settings().cookie_secure, httponly=True, samesite='strict')


@router.get('/api/v1/me/sessions', response_model=Envelope[Page[s.SessionItem]], responses=READ_ERRORS, operation_id='listSessions')
def list_sessions(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(LoginSession).where(LoginSession.user_id == identity[0].id,
        LoginSession.revoked_at.is_(None), LoginSession.expires_at > func.now()).order_by(LoginSession.created_at.desc())).all()
    return envelope(request, {'items': [s.SessionItem(id=row.id, created_at=row.created_at,
        expires_at=row.expires_at, current=row.id == identity[1].id) for row in rows], 'next_cursor': None})


@router.delete('/api/v1/me/sessions/others', status_code=204, responses=WRITE_ERRORS, operation_id='revokeOtherSessions')
def revoke_other_sessions(request: Request, identity=Depends(csrf), db: Session = Depends(db_session)):
    result = db.execute(update(LoginSession).where(LoginSession.user_id == identity[0].id,
        LoginSession.id != identity[1].id, LoginSession.revoked_at.is_(None), LoginSession.expires_at > func.now())
        .values(revoked_at=datetime.now(UTC)))
    service.audit(db, identity[0], 'settings.sessions_revoke', request_id=request.state.request_id,
                  context={'count': result.rowcount, 'target': 'others'})
    db.commit()


@router.delete('/api/v1/me/sessions/{session_id}', status_code=204, responses=WRITE_ERRORS, operation_id='revokeSession')
def revoke_session(session_id: UUID, request: Request, response: Response,
                   identity=Depends(csrf), db: Session = Depends(db_session)):
    row = db.scalar(select(LoginSession).where(LoginSession.id == session_id, LoginSession.user_id == identity[0].id))
    if not row:
        raise APIError(404, 'not_found', '登录会话不存在或不可访问。')
    row.revoked_at = datetime.now(UTC)
    service.audit(db, identity[0], 'settings.sessions_revoke', request_id=request.state.request_id,
                  context={'count': 1, 'target': 'current' if session_id == identity[1].id else 'selected'})
    db.commit()
    if session_id == identity[1].id:
        response.delete_cookie(COOKIE, path='/', secure=settings().cookie_secure, httponly=True, samesite='strict')


@router.get('/api/v1/system/overview', response_model=Envelope[s.Overview], responses=READ_ERRORS, operation_id='getSystemOverview')
def get_overview(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    return envelope(request, service.overview(db, identity[0]))


@router.get('/api/v1/system/cache', response_model=Envelope[s.CacheStats], responses=READ_ERRORS, operation_id='getSystemCache')
def get_cache(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    return envelope(request, service.cache_stats(db, identity[0]))


@router.delete('/api/v1/system/cache', response_model=Envelope[s.CacheStats], responses=WRITE_ERRORS, operation_id='clearSystemCache')
def clear_cache(request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin')
    def create(db):
        return service.clear_cache(db, identity[0], request.state.request_id), 'complete', []
    return write_once(request, identity[0], key, {}, create, status=200)


@router.get('/api/v1/system/backups', response_model=Envelope[Page[s.Backup]], responses=READ_ERRORS, operation_id='listSystemBackups')
def list_backups(request: Request, cursor: str | None = Query(default=None, max_length=512),
                 limit: int = Query(default=50, ge=1, le=100), identity=Depends(principal), db: Session = Depends(db_session)):
    require_role(identity[0], 'admin')
    scope = cursor_scope(identity[0].id, {'resource': 'system-backups'})
    position = decode_cursor(cursor, scope)
    stmt = select(BackupRecord)
    if position:
        stmt = stmt.where(tuple_(BackupRecord.created_at, BackupRecord.id) < position)
    rows = db.scalars(stmt.order_by(BackupRecord.created_at.desc(), BackupRecord.id.desc()).limit(limit + 1)).all()
    return envelope(request, {'items': [service.backup_dto(row) for row in rows[:limit]],
        'next_cursor': encode_cursor(rows[limit - 1], scope, 'created_at') if len(rows) > limit else None})


@router.post('/api/v1/system/maintenance-jobs', response_model=Envelope[s.MaintenanceJob], status_code=202,
             responses=WRITE_ERRORS, operation_id='createMaintenanceJob')
def create_maintenance_job(body: s.MaintenanceInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin')
    def create(db):
        state = service.get_system_state(db)
        if not settings().maintenance_enabled or not service.connected(state.backup_heartbeat_at):
            raise APIError(503, 'backup_worker_unavailable', '备份进程未连接，请启动本机维护服务后再试。')
        job = service.create_job(db, identity[0], body.kind, key=key, request_id=request.state.request_id)
        return service.job_dto(job), 'complete', []
    return write_once(request, identity[0], key, body, create, status=202)


@router.get('/api/v1/system/maintenance-jobs/{job_id}', response_model=Envelope[s.MaintenanceJob], responses=READ_ERRORS, operation_id='getMaintenanceJob')
def get_maintenance_job(job_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    require_role(identity[0], 'admin')
    row = db.get(MaintenanceJob, job_id)
    if not row:
        raise APIError(404, 'not_found', '维护任务不存在或不可访问。')
    return envelope(request, service.job_dto(row))


@router.get('/api/v1/system/activity', response_model=Envelope[Page[s.Activity]], responses=READ_ERRORS, operation_id='listSystemActivity')
def list_activity(request: Request,
                  kind: Literal['all', 'login', 'preferences', 'password', 'sessions', 'cache', 'backup', 'update'] = 'all',
                  from_date: date | None = None, to_date: date | None = None,
                  cursor: str | None = Query(default=None, max_length=512), limit: int = Query(default=50, ge=1, le=100),
                  identity=Depends(principal), db: Session = Depends(db_session)):
    if from_date and to_date and from_date > to_date:
        raise APIError(422, 'invalid_date_range', '开始日期不能晚于结束日期。')
    # Do not deserialize arbitrary historical audit bodies. Only our known
    # context keys and nullable new columns are included in this read.
    stmt = select(AuditEvent, User.username, text('audit_events.outcome'), text('audit_events.context')).outerjoin(User, AuditEvent.actor_id == User.id)
    if identity[0].role != 'admin':
        stmt = stmt.where(AuditEvent.actor_id == identity[0].id)
    if kind != 'all':
        patterns = {'login': ('session.login', 'session.login_failed', 'session.logout'),
                    'preferences': ('settings.preferences',), 'password': ('settings.password', 'settings.password_failed'),
                    'sessions': ('settings.sessions_revoke',), 'cache': ('settings.cache_clear',)}
        if kind in patterns:
            stmt = stmt.where(AuditEvent.action.in_(patterns[kind]))
        else:
            stmt = stmt.where(AuditEvent.action.like('maintenance.backup%' if kind == 'backup' else 'update.%'))
    local = ZoneInfo('Asia/Shanghai')
    if from_date:
        stmt = stmt.where(AuditEvent.created_at >= datetime.combine(from_date, time.min, local))
    if to_date == date.max:
        stmt = stmt.where(AuditEvent.created_at <= datetime.combine(to_date, time.max, local))
    elif to_date:
        stmt = stmt.where(AuditEvent.created_at < datetime.combine(to_date + timedelta(days=1), time.min, local))
    scope = cursor_scope(identity[0].id, {'resource': 'system-activity', 'kind': kind, 'from': from_date, 'to': to_date,
                                       'role': identity[0].role})
    position = decode_cursor(cursor, scope)
    if position:
        stmt = stmt.where(tuple_(AuditEvent.created_at, AuditEvent.id) < position)
    rows = db.execute(stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit + 1)).all()
    items = [s.Activity(id=row.id, at=row.created_at, action=row.action, label=service.action_label(row.action),
              outcome=outcome if outcome in {'succeeded', 'failed'} else 'recorded', actor=actor,
              details=service.safe_details(context)) for row, actor, outcome, context in rows[:limit]]
    return envelope(request, {'items': items, 'next_cursor': encode_cursor(rows[limit - 1][0], scope, 'created_at') if len(rows) > limit else None})


@router.get('/api/v1/system/diagnostics', response_model=Envelope[s.Diagnostics], responses=READ_ERRORS, operation_id='getSystemDiagnostics')
def diagnostics(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    require_role(identity[0], 'admin')
    summary = service.overview(db, identity[0])
    rows = db.scalars(select(MaintenanceJob.error_code).where(MaintenanceJob.error_code.is_not(None))
                      .order_by(MaintenanceJob.created_at.desc()).limit(20)).all()
    errors = sorted({value for value in rows if value and re.fullmatch('[A-Za-z0-9_]{1,80}', value)})
    return envelope(request, s.Diagnostics(version=summary.version, app_mode=summary.app_mode,
        generated_at=datetime.now(UTC), health=summary.health, maintenance=summary.maintenance, errors=errors))
