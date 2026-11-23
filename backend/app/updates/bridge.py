"""Fixed control protocol used only by scripts/update_host.py via Docker CLI.

This container has no Docker access. Input is one bounded JSON command on stdin;
the host owns the deployment template, image switching and durable progress.
"""
import json
import shutil
import sys
import uuid
from pathlib import Path
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.maintenance_control import begin_maintenance
from app.models import Job
from app.system_settings.models import MaintenanceJob
from app.system_settings.service import audit, get_system_state
from app.updates.manifest import UpdateFailure, verified
from app.version import APP_VERSION, SCHEMA_REVISION, SCHEMA_COMPATIBLE_REVISIONS


def dispatch(db, body):
    action = body['action']
    system = get_system_state(db)
    if action == 'heartbeat':
        system.updater_heartbeat_at = datetime.now(UTC)
        if body.get('id'):
            db.execute(update(MaintenanceJob).where(MaintenanceJob.id == uuid.UUID(body['id']),
                MaintenanceJob.lease_token == uuid.UUID(body['token']), MaintenanceJob.status == 'running')
                .values(lease_until=datetime.now(UTC) + timedelta(seconds=120)))
        db.commit()
        return {'ok': True}
    if action == 'claim':
        job = db.scalar(select(MaintenanceJob).where(MaintenanceJob.kind == 'update.install',
            or_(MaintenanceJob.status == 'queued', and_(MaintenanceJob.status == 'running',
                MaintenanceJob.stage == 'preflight', MaintenanceJob.lease_until < datetime.now(UTC))))
            .order_by(MaintenanceJob.created_at).with_for_update(skip_locked=True).limit(1))
        if not job:
            db.rollback()
            return None
        from app.models import User
        owner = db.get(User, job.owner_id)
        try:
            if not owner or not owner.enabled or owner.role != 'admin':
                raise UpdateFailure('maintenance_permission_changed', '管理员权限已变化，安装已停止。')
            verified(job.params.get('manifest'), job.params.get('signature'), settings().update_public_key)
        except UpdateFailure as failure:
            job.status, job.stage, job.error_code, job.message = 'failed', 'failed', failure.code, failure.message
            job.completed_at, job.lease_until = datetime.now(UTC), None
            audit(db, job.owner_id, 'update.failed', outcome='failed', resource_id=job.id, context={'error_code': failure.code})
            db.commit()
            return None
        actual_schema = db.scalar(text('SELECT version_num FROM alembic_version'))
        if actual_schema not in SCHEMA_COMPATIBLE_REVISIONS:
            raise UpdateFailure('update_control_version_mismatch', '当前控制程序与数据库不兼容，请按手动升级指引处理。')
        if job.params.get('confirmed') is not True or job.params.get('drafts_saved') is not True:
            raise UpdateFailure('update_confirmation_required', '本次安装尚未经确认。')
        job.status, job.stage, job.started_at = 'running', 'preflight', datetime.now(UTC)
        job.lease_token, job.lease_until = uuid.uuid4(), datetime.now(UTC) + timedelta(seconds=120)
        db.commit()
        return {'id': str(job.id), 'token': str(job.lease_token), 'params': job.params,
                'current_schema': actual_schema, 'compatible_revisions': sorted(SCHEMA_COMPATIBLE_REVISIONS)}
    if action == 'describe':
        revision = db.scalar(text('SELECT version_num FROM alembic_version'))
        size = db.scalar(text('SELECT pg_database_size(current_database())'))
        # Includes stale/unknown running tasks. Never infer completion from a lease
        # expiry or replay a possibly sent model request during a maintenance drain.
        running = db.scalar(select(func.count()).select_from(Job).where(Job.state == 'running'))
        from app.ai.models import AiAttempt
        probes = db.scalar(select(func.count()).select_from(AiAttempt).where(AiAttempt.run_id.is_(None), AiAttempt.state.in_(['preparing', 'running'])))
        file_size = sum(path.stat().st_size for folder in (Path(settings().artifact_directory), Path(settings().ai_secret_directory))
            if folder.is_dir() for path in folder.rglob('*') if path.is_file() and not path.is_symlink())
        return {'schema': revision, 'version': APP_VERSION, 'database_bytes': size, 'running': running + probes,
                'backup_estimate_bytes': size * 2 + file_size + 1024**3,
                'backup_free_bytes': shutil.disk_usage(settings().backup_directory).free,
                'runtime_free_bytes': shutil.disk_usage(settings().runtime_directory).free}
    identity, token = uuid.UUID(body['id']), uuid.UUID(body['token'])
    if action == 'finish':
        prior = db.scalar(select(MaintenanceJob).where(MaintenanceJob.id == identity,
            MaintenanceJob.lease_token == token, MaintenanceJob.kind == 'update.install'))
        if prior and prior.status in {'succeeded', 'failed', 'manual_recovery'}:
            if prior.status != body['status']:
                raise UpdateFailure('update_result_conflict', '原任务已有明确结果，不能覆盖，请查看记录。')
            return {'ok': True}
    if action == 'observe':
        existing = db.scalar(select(MaintenanceJob).where(MaintenanceJob.id == identity,
            MaintenanceJob.lease_token == token, MaintenanceJob.kind == 'update.install'))
        if not existing:
            raise UpdateFailure('update_lease_lost', '无法核对原升级任务，请保留现场并按恢复指引操作。')
        return {'status': existing.status, 'message': existing.message, 'error_code': existing.error_code}
    job = db.scalar(select(MaintenanceJob).where(MaintenanceJob.id == identity,
        MaintenanceJob.kind == 'update.install', MaintenanceJob.status == 'running', MaintenanceJob.lease_token == token))
    if not job:
        raise UpdateFailure('update_lease_lost', '升级任务已由其他进程处理，请查看操作记录。')
    if action == 'freeze':
        begin_maintenance(db, identity)
        job.stage, job.message = 'draining', '正在等待已开始的任务完成，暂不接收新的写入。'
        db.commit()
        return {'ok': True}
    if action == 'stage':
        allowed = {'preflight', 'draining', 'backup', 'loading', 'stopping', 'switching', 'migrating', 'starting', 'verifying', 'rollback'}
        if body['stage'] not in allowed:
            raise ValueError('unsupported phase')
        job.stage = system.stage = body['stage']
        job.message = str(body.get('message', '正在处理升级。'))[:500]
        db.commit()
        return {'ok': True}
    if action == 'backup':
        from app.maintenance.worker import WORKER_GATE, execute_backup
        with db.get_bind().connect().execution_options(isolation_level='AUTOCOMMIT') as guard:
            if not guard.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': WORKER_GATE}):
                raise UpdateFailure('maintenance_busy', '已有备份动作正在进行，请稍后安装。')
            try:
                def progress(stage, message):
                    job.stage, job.message = stage, message
                    db.commit()
                return execute_backup(db, job, progress=progress)
            finally:
                guard.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': WORKER_GATE})
    if action == 'finish':
        outcome = body['status']
        if outcome not in {'succeeded', 'failed', 'manual_recovery'}:
            raise ValueError('unsupported result')
        job.status, job.stage, job.message = outcome, outcome, str(body['message'])[:500]
        job.error_code, job.completed_at, job.lease_until = body.get('error_code'), datetime.now(UTC), None
        from app.system_settings.service import safe_details, RESULT_FIELDS
        job.result = safe_details(body.get('result'), RESULT_FIELDS)
        system.active = outcome == 'manual_recovery'
        system.stage, system.upgrade_job_id = outcome, identity
        audit(db, job.owner_id, 'update.completed' if outcome == 'succeeded' else 'update.failed',
            outcome='succeeded' if outcome == 'succeeded' else 'failed', resource_id=job.id,
            context={'release_id': job.params.get('release_id'), 'error_code': job.error_code})
        db.commit()
        return {'ok': True}
    raise ValueError('unsupported command')


def main():
    try:
        payload = sys.stdin.buffer.read(256 * 1024 + 1)
        if len(payload) > 256 * 1024:
            raise ValueError('oversized control command')
        with Session(engine(), expire_on_commit=False) as db:
            result = dispatch(db, json.loads(payload))
        print(json.dumps({'data': result}, ensure_ascii=False))
    except Exception as failure:
        print(json.dumps({'error': {'code': getattr(failure, 'code', 'updater_control_unavailable'),
            'message': getattr(failure, 'message', '本机升级控制服务暂不可用。')}}, ensure_ascii=False))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
