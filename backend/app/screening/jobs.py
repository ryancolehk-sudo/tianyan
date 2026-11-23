"""Local-only monitor jobs share the durable queue and its lease fence."""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import APIError, require_instance
from app.db import engine
from app.evidence.common import lock, now
from app.maintenance_control import admit, paused
from app.market.protocol import SHANGHAI
from app.market.terminal_read import segment
from app.models import Job, User
from app.screening import service
from app.screening.models import ScreenRule
from app.screening.schemas import ContextInput


def schedule_screens():
    with Session(engine()) as db, db.begin():
        require_instance(db)
        if paused(db):
            return
        admit(db)
        clock = now(db)
        local = clock.astimezone(SHANGHAI)
        if local.weekday() >= 5 or segment(clock) is None:
            return
        rules = db.scalars(
            select(ScreenRule)
            .join(User, User.id == ScreenRule.owner_id)
            .where(
                ScreenRule.enabled.is_(True),
                ScreenRule.next_run_at <= clock,
                User.enabled.is_(True),
                User.role.in_(["admin", "researcher"]),
            )
            .order_by(ScreenRule.next_run_at, ScreenRule.id)
            .with_for_update(of=ScreenRule, skip_locked=True)
            .limit(20)
        ).all()
        for rule in rules:
            pending = db.scalar(
                select(Job.id)
                .where(
                    Job.kind == "screen.evaluate",
                    Job.owner_id == rule.owner_id,
                    Job.payload["rule_id"].astext == str(rule.id),
                    Job.state.in_(["queued", "running"]),
                )
                .limit(1)
            )
            if pending is None:
                db.add(
                    Job(
                        owner_id=rule.owner_id,
                        kind="screen.evaluate",
                        state="queued",
                        payload={"rule_id": str(rule.id), "rule_version": rule.version},
                        available_at=clock,
                    )
                )
            rule.next_run_at = clock + timedelta(seconds=service.INTERVAL_SECONDS)


def process_screen(detached):
    import uuid

    with Session(engine(), expire_on_commit=False) as db, db.begin():
        require_instance(db)
        job = db.scalar(
            select(Job)
            .where(
                Job.id == detached.id,
                Job.state == "running",
                Job.lease_token == detached.lease_token,
                Job.lease_until > func.now(),
            )
            .with_for_update()
        )
        if job is None:
            return
        lock(db, f"screening-run:{job.payload['rule_id']}")
        rule = db.scalar(
            select(ScreenRule)
            .where(
                ScreenRule.id == uuid.UUID(job.payload["rule_id"]),
                ScreenRule.owner_id == job.owner_id,
            )
            .with_for_update()
        )
        user = db.get(User, job.owner_id)
        if (
            not rule
            or not rule.enabled
            or rule.version != job.payload["rule_version"]
            or not user
            or not user.enabled
            or user.role not in {"admin", "researcher"}
        ):
            job.state = "cancelled"
            job.error_code = "screen_rule_changed"
        else:
            try:
                # A savepoint prevents a rejected observation from committing a
                # partial baseline, result or event while retaining job outcome.
                with db.begin_nested():
                    service.run_rule(db, job.owner_id, rule.id, ContextInput(), monitor=True)
                job.state = "succeeded"
                job.error_code = None
            except APIError as error:
                rule.last_error = error.code
                job.state = "failed"
                job.error_code = error.code
        # The row lock fences concurrent claimers but also delays the renewal
        # thread. Check the real database clock again before committing ANY
        # baseline, result or event, even if observation time is synthetic.
        if job.lease_until <= db.scalar(select(func.clock_timestamp())):
            raise APIError(409, "screen_lease_lost", "观察任务租约已过期，未提交结果。")
        job.completed_at = now(db)
        job.lease_token = job.lease_until = None
