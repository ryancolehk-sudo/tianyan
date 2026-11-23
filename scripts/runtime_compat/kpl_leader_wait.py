"""Passive rollback support: preserve leader packets an old collector cannot run."""


def defer_kpl_leader_packet(job):
    if job.payload.get('kpl_leaders') is not True:
        return False
    from datetime import timedelta
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.evidence.common import now
    from app.models import Job
    with Session(engine()) as db, db.begin():
        clock = now(db)
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running',
            Job.lease_token == job.lease_token, Job.lease_until > clock).with_for_update())
        if current is not None:
            current.state = 'queued'
            current.available_at = max(current.available_at, clock + timedelta(minutes=5))
            current.attempts = max(0, current.attempts - 1)
            current.error_code = 'collector_version_wait'
            current.completed_at = None
            current.lease_token = current.lease_until = None
    # An expired/replaced lease cannot fall through into the old archive path.
    return True
