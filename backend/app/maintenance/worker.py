"""Single local maintenance worker. Never claims collector or model jobs."""
import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.orm import Session

from app.db import engine
from app.maintenance.backup import BackupFailure, create_backup
from app.system_settings.models import BackupRecord, MaintenanceJob, SystemState
from app.system_settings.service import audit, get_system_state
from app.version import SCHEMA_REVISION
from app.config import settings
from app.models import User

WORKER_GATE = 804368692414571039
LEASE_SECONDS = 120
log = logging.getLogger(__name__)


class LeaseLost(Exception):
    pass


def heartbeat(job_id=None, token=None, *, bind=None):
    with Session(bind or engine()) as db, db.begin():
        now = datetime.now(UTC)
        state = get_system_state(db)
        state.backup_heartbeat_at = now
        if job_id:
            changed = db.execute(update(MaintenanceJob).where(MaintenanceJob.id == job_id,
                MaintenanceJob.lease_token == token, MaintenanceJob.status == 'running')
                .values(lease_until=now + timedelta(seconds=LEASE_SECONDS)))
            return changed.rowcount == 1
    return True


def _renew(stop, lost, identity, token, bind):
    while not stop.wait(10):
        try:
            if not heartbeat(identity, token, bind=bind):
                lost.set()
                return
        except Exception:
            # A transient DB outage must not claim a different job. The owning
            # process keeps its separate advisory lock through publication.
            log.warning('maintenance_heartbeat_failed')


def _stage(db, job, token, stage, message, lost):
    if lost.is_set():
        raise LeaseLost()
    result = db.execute(update(MaintenanceJob).where(MaintenanceJob.id == job.id,
        MaintenanceJob.status == 'running', MaintenanceJob.lease_token == token)
        .values(stage=stage, message=message, lease_until=datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)))
    if result.rowcount != 1:
        db.rollback()
        raise LeaseLost()
    db.commit()


def execute_backup(db, job, *, progress=lambda stage, message: None, bind=None):
    """Shared backup action for normal jobs and the host upgrade preflight."""
    record = db.scalar(select(BackupRecord).where(BackupRecord.job_id == job.id))
    if record is None:
        record = BackupRecord(id=uuid.uuid4(), owner_id=job.owner_id, job_id=job.id,
            name='backup-' + str(job.id), location_key='backup-' + str(job.id),
            app_mode=settings().app_mode, schema_revision=SCHEMA_REVISION,
            build_revision=settings().build_revision)
        db.add(record)
        db.commit()
    metadata = create_backup(job.id, progress=progress, bind=bind)
    for field, value in metadata.items():
        setattr(record, field, value)
    record.completed_at = datetime.now(UTC)
    audit(db, job.owner_id, 'maintenance.backup_completed', resource_id=record.id,
          resource_type='backup', context={'backup_id': str(record.id),
              'integrity_state': 'passed', 'recovery_state': 'unverified'})
    db.commit()
    return {'backup_id': str(record.id), 'size_bytes': record.size_bytes,
            'integrity_state': record.integrity_state, 'recovery_state': record.recovery_state,
            'schema_revision': record.schema_revision, 'build_revision': record.build_revision}


def _failure(db, job, token, code, message):
    db.rollback()
    result = db.execute(update(MaintenanceJob).where(MaintenanceJob.id == job.id,
        MaintenanceJob.status == 'running', MaintenanceJob.lease_token == token)
        .values(status='failed', stage='failed', message=message, error_code=code,
                completed_at=datetime.now(UTC), lease_until=None))
    if result.rowcount:
        if job.kind == 'backup':
            record = db.scalar(select(BackupRecord).where(BackupRecord.job_id == job.id))
            if record:
                record.integrity_state = 'failed'
                record.completed_at = datetime.now(UTC)
        audit(db, job.owner_id, 'maintenance.backup_failed' if job.kind == 'backup' else 'update.failed',
              outcome='failed', resource_id=job.id, resource_type='maintenance_job',
              context={'kind': job.kind, 'error_code': code})
    db.commit()


def run_one(db, *, bind=None, job_id=None):
    """Claim only backup/download, with lease fencing and process crash recovery."""
    bind = bind or db.get_bind()
    with bind.connect().execution_options(isolation_level='AUTOCOMMIT') as guard:
        locked = guard.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': WORKER_GATE})
        if not locked:
            return False
        try:
            state = db.get(SystemState, 1)
            if state is not None and state.active:
                db.rollback()
                return False
            now = datetime.now(UTC)
            statement = select(MaintenanceJob).where(MaintenanceJob.kind.in_(('backup', 'update.download')),
                or_(MaintenanceJob.status == 'queued', and_(MaintenanceJob.status == 'running',
                    or_(MaintenanceJob.lease_until.is_(None), MaintenanceJob.lease_until < now))))
            if job_id:
                statement = statement.where(MaintenanceJob.id == job_id)
            job = db.scalar(statement.order_by(MaintenanceJob.created_at, MaintenanceJob.id)
                            .with_for_update(skip_locked=True).limit(1))
            if job is None:
                db.rollback()
                return False
            token = uuid.uuid4()
            job.status, job.stage, job.message = 'running', 'checking', '正在检查维护任务。'
            job.started_at = job.started_at or now
            job.lease_token = token
            job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            job.error_code = None
            db.commit()
            stop, lost = threading.Event(), threading.Event()
            renew = threading.Thread(target=_renew, args=(stop, lost, job.id, token, bind), daemon=True)
            renew.start()
            try:
                owner = db.get(User, job.owner_id)
                if owner is None or not owner.enabled or owner.role != 'admin':
                    raise BackupFailure('maintenance_permission_changed', '管理员权限已变化，维护任务已停止。')
                progress = lambda stage, message: _stage(db, job, token, stage, message, lost)
                if job.kind == 'backup':
                    result = execute_backup(db, job, progress=progress, bind=bind)
                else:
                    from app.updates.service import run_download
                    result = run_download(db, job)
                if lost.is_set():
                    raise LeaseLost()
                changed = db.execute(update(MaintenanceJob).where(MaintenanceJob.id == job.id,
                    MaintenanceJob.lease_token == token, MaintenanceJob.status == 'running')
                    .values(status='succeeded', stage='completed', message='本机备份已完成；尚未进行恢复演练。'
                            if job.kind == 'backup' else '更新包已下载并通过校验，尚未安装。',
                            result=result or {}, completed_at=datetime.now(UTC), lease_until=None))
                if changed.rowcount != 1:
                    raise LeaseLost()
                db.commit()
            except LeaseLost:
                db.rollback()
            except BackupFailure as failure:
                _failure(db, job, token, failure.code, failure.message)
            except Exception as failure:
                # Errors from the update service also use controlled code/message.
                code = getattr(failure, 'code', 'maintenance_failed')
                message = getattr(failure, 'message', '本机维护未完成，请检查服务状态后重试。')
                _failure(db, job, token, code, message)
                log.warning('maintenance_failed kind=%s error=%s', job.kind, type(failure).__name__)
            finally:
                stop.set()
                renew.join(timeout=2)
            return True
        finally:
            guard.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': WORKER_GATE})


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    while True:
        try:
            heartbeat()
            with Session(engine(), expire_on_commit=False) as db:
                if run_one(db):
                    continue
            from app.updates.service import maybe_check
            with Session(engine(), expire_on_commit=False) as db:
                maybe_check(db)
        except Exception as failure:
            log.warning('maintenance_unavailable error=%s', type(failure).__name__)
        time.sleep(5)


if __name__ == '__main__':
    main()
