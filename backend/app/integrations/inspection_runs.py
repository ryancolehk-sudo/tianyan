"""Register repairs immediately and follow their checks and exact collection receipts."""
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import APIError
from app.db import engine
from app.evidence.common import lock, now
from app.evidence.models import IdempotencyKey
from app.integrations.inspection import local_services
from app.integrations.inspection_repair import legacy_batch, repair, verify_batch
from app.integrations.inspection_schemas import InspectionProgress, InspectionRun, RepairBatch
from app.maintenance_control import admit
from app.models import Job, User

KIND = 'inspection.repair'
URL = '/api/v1/data-center/inspection/runs'


def view(job):
    result = job.result_ref or {}
    phase = {'queued': 'queued', 'running': result.get('phase', 'scanning'), 'succeeded': 'submitted',
             'failed': 'failed', 'cancelled': 'failed'}[job.state]
    return InspectionRun(id=job.id, started_at=job.created_at, phase=phase, checked=result.get('checked', 0),
        total=result.get('total'), current=result.get('current') if job.state == 'running' else None,
        message=result.get('message'), batch=result.get('batch') if job.state == 'succeeded' else None)


def submit(db, owner):
    admit(db)
    lock(db, KIND + ':' + str(owner.id))
    job = db.scalar(select(Job).where(Job.owner_id == owner.id, Job.kind == KIND, Job.state.in_(['running', 'queued']))
                    .order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
    if not job:
        job = Job(owner_id=owner.id, kind=KIND, state='queued', payload={})
        db.add(job)
        db.flush()
    return view(job), 'complete', []


def read_progress(db, owner, key=None):
    clock = now(db)
    result = InspectionProgress(checked_at=clock, services=local_services(db, clock))
    query = select(Job).where(Job.owner_id == owner.id, Job.kind == KIND)
    if key is not None:
        receipt = db.get(IdempotencyKey, (owner.id, 'POST', URL, str(key)))
        if not receipt or receipt.expires_at <= clock:
            return result
        query = query.where(Job.id == UUID(receipt.response_json['data']['id']))
    job = db.scalar(query.order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
    run = view(job) if job else None
    # Previously submitted synchronous repairs remain visible across the upgrade.
    legacy = legacy_batch(db, owner, clock) if key is None else None
    if legacy and (run is None or legacy.started_at > run.started_at and run.phase in {'submitted', 'failed'}):
        run = InspectionRun(started_at=legacy.started_at, phase='submitted', batch=legacy)
    if run and run.batch:
        run.batch = verify_batch(db, owner, run.batch)
    result.run = run
    return result


class LeaseLost(Exception):
    pass


def fenced(db, claimed, **values):
    written = db.execute(update(Job).where(Job.id == claimed.id, Job.kind == KIND, Job.state == 'running',
        Job.lease_token == claimed.lease_token, Job.lease_until > func.clock_timestamp()).values(**values))
    if written.rowcount != 1:
        raise LeaseLost()


def process_run(claimed):
    state = {'phase':'scanning', 'checked':0, 'total':None, 'current':None}

    def publish(checked=None, total=None, current=None):
        if checked is not None:
            state.update(phase='applying', checked=checked, total=total, current=current)
        # Only a counter is visible until the repair transaction commits. A collected
        # row, a route change and its final receipt always commit or roll back together.
        with Session(engine()) as journal, journal.begin():
            fenced(journal, claimed, result_ref=dict(state))

    try:
        publish()
        with Session(engine()) as db, db.begin():
            job = db.get(Job, claimed.id)
            owner = db.get(User, job.owner_id)
            if not owner or not owner.enabled or owner.role != 'admin':
                raise APIError(403, 'permission_missing', '修复账号的管理员权限已变化。')
            batch, _, _ = repair(db, owner, progress=publish)
            db.refresh(owner)
            if not owner.enabled or owner.role != 'admin':
                raise APIError(403, 'permission_missing', '修复账号的管理员权限已变化。')
            fenced(db, claimed, state='succeeded', completed_at=now(db), lease_token=None, lease_until=None,
                   result_ref={**state, 'batch':batch.model_dump(mode='json'), 'current':None}, error_code=None)
    except LeaseLost:
        return
    except (SQLAlchemyError, APIError) as exc:
        code = exc.code if isinstance(exc, APIError) else 'inspection_database_busy'
        with Session(engine()) as db, db.begin():
            job = db.get(Job, claimed.id)
            retry = job and job.attempts < 3 and (isinstance(exc, SQLAlchemyError) or exc.status == 503)
            message = ('数据服务忙，后台将自动重试。' if retry else
                       exc.message if isinstance(exc, APIError) else '数据服务未能完成本次检查，可点击重新检修。')
            try:
                fenced(db, claimed, state='queued' if retry else 'failed', completed_at=None if retry else now(db),
                       available_at=now(db) + timedelta(seconds=5), lease_token=None, lease_until=None, error_code=code,
                       result_ref={**state, 'current':None, 'message':message})
            except LeaseLost:
                return
