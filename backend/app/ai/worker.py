"""Independent worker. Never reclaims an expired model request or collects market jobs."""
import logging
import signal
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai import service
from app.ai.attempts import attempt_record, perform
from app.ai.models import AiAttempt, AiRun
from app.ai.runs import instructions, parse_result, prompt, verify_inputs
from app.auth import APIError, require_instance
from app.config import settings
from app.db import engine
from app.models import Job, User
from app.worker import heartbeat, renew
from app.maintenance_control import admit, paused

log = logging.getLogger("tianyan.ai.worker")


def recover(db, clock):
    retired = db.scalars(select(Job).where(Job.kind == "news.ai", Job.state == "running", Job.lease_until < clock)
                         .with_for_update(skip_locked=True)).all()
    for job in retired:
        # The new executor never reissues a legacy request. Closing its job lets the news journal mark prior I/O unknown.
        job.state, job.error_code, job.completed_at = "cancelled", "feature_not_implemented", clock
        job.lease_token = job.lease_until = None
    if retired:
        from app.news_center.jobs import _close_terminated_attempts
        _close_terminated_attempts(db)
    expired = db.scalars(select(Job).where(Job.kind == "ai.generate", Job.state == "running", Job.lease_until < clock)
                         .with_for_update(skip_locked=True)).all()
    for job in expired:
        run = db.scalar(select(AiRun).where(AiRun.job_id == job.id).with_for_update())
        attempts = db.scalars(select(AiAttempt).where(AiAttempt.run_id == run.id)).all() if run else []
        uncertain = any(item.sent_at and item.state in {"running", "preparing"} for item in attempts)
        for item in attempts:
            if item.state in {"running", "preparing"}:
                item.state = "unknown" if item.sent_at else "failed"
                item.error_code = "outcome_unknown" if item.sent_at else "worker_interrupted"
                item.completed_at = clock
        job.state, job.error_code, job.completed_at = "failed", "outcome_unknown" if uncertain else "worker_interrupted", clock
        job.lease_token = job.lease_until = None
        if run:
            run.state, run.error_code, run.completed_at = "unknown" if uncertain else "failed", job.error_code, clock
            run.stage = "请求结果未知，未自动重发" if uncertain else "执行器中断，请检查后手动重新生成"
    # Probes may outlive an API process. They also must never be retried by recovery.
    stale = db.scalars(select(AiAttempt).where(AiAttempt.run_id.is_(None), AiAttempt.state.in_(["running", "preparing"]),
                                              AiAttempt.started_at < clock - timedelta(seconds=240)).with_for_update(skip_locked=True)).all()
    for item in stale:
        item.state, item.error_code = ("unknown", "outcome_unknown") if item.sent_at else ("failed", "worker_interrupted")
        item.completed_at = clock


def claim():
    clock = datetime.now(UTC)
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        require_instance(db)
        if paused(db):
            return None
        admit(db)
        clock = db.scalar(select(func.clock_timestamp()))
        recover(db, clock)
        job = db.scalar(select(Job).where(or_(and_(Job.kind == "ai.generate", Job.attempts == 0), Job.kind == "news.ai"),
                                         Job.state == "queued", Job.available_at <= clock)
                        .order_by(Job.created_at, Job.id).with_for_update(skip_locked=True).limit(1))
        if not job:
            return None
        job.state, job.attempts, job.lease_token = "running", min(3, job.attempts + 1), uuid.uuid4()
        job.lease_until = clock + timedelta(seconds=settings().lease_seconds)
        return job


def fenced(db, job):
    current = db.scalar(select(Job).where(Job.id == job.id, Job.state == "running", Job.lease_token == job.lease_token,
                                          Job.lease_until > func.now()).with_for_update())
    if not current:
        raise APIError(409, "lease_lost", "AI 执行租约已失效，请检查现有任务结果。")
    return current


def validate_run(db, job, *, selected_id=None):
    fenced(db, job)
    run = db.scalar(select(AiRun).where(AiRun.job_id == job.id).with_for_update())
    owner = db.get(User, run.owner_id) if run else None
    if not run or not owner or not owner.enabled or owner.role not in {"admin", "researcher"}:
        raise APIError(409, "owner_permission_changed", "账号权限已变化，任务已停止。")
    connection, fallback, snapshot = service.resolve_feature(db, run.feature, run.owner_id,
        plan_connection_id=run.config_snapshot.get("plan_connection_id"), plan_model=run.config_snapshot.get("plan_model"))
    if snapshot != run.config_snapshot:
        raise APIError(409, "configuration_changed", "排队后模型配置已变化，旧任务已停止，请重新生成。")
    if run.feature == 'plan_generation':
        from app.tomorrow.jobs import validate_ai_inputs
        validate_ai_inputs(db, run)
    else:
        verify_inputs(db, run)
    selected = fallback if selected_id is not None and fallback and fallback.id == selected_id else connection
    if selected_id is not None and selected.id != selected_id:
        raise APIError(409, "configuration_changed", "备用模型配置已变化。")
    return run, selected, fallback


def finish(job, *, state, code=None, result=None, usage=None, stage=None):
    with Session(engine()) as db, db.begin():
        try:
            current = fenced(db, job)
        except APIError:
            return False
        run = db.scalar(select(AiRun).where(AiRun.job_id == job.id).with_for_update())
        if not run:
            current.state, current.error_code = "failed", "run_missing"
        else:
            if state == 'succeeded' and run.feature == 'plan_generation':
                try:
                    validate_run(db, job, selected_id=run.connection_id)
                except APIError as error:
                    state, code, result = 'cancelled', error.code, None
            run.state, run.error_code, run.completed_at = state, code, datetime.now(UTC)
            run.result, run.usage = result, usage
            run.stage = stage or {"succeeded": "生成完成", "unknown": "请求结果未知，未自动重发", "cancelled": "配置或权限变化，任务已停止"}.get(state, "生成失败")
            current.state = "succeeded" if state == "succeeded" else "cancelled" if state == "cancelled" else "failed"
            current.error_code = code
            current.result_ref = {"ai_run_id": str(run.id)}
        current.completed_at = datetime.now(UTC)
        current.lease_token = current.lease_until = None
        return True


@contextmanager
def renewal(job):
    stop = threading.Event()

    def keep_alive():
        while not stop.wait(max(.25, settings().lease_seconds / 3)):
            try:
                if not renew(job.id, job.lease_token):
                    return
                heartbeat("ai:" + socket.gethostname())
            except SQLAlchemyError:
                log.error("AI lease renewal unavailable; final commit remains fenced")

    thread = threading.Thread(target=keep_alive, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)


def process(job):
    if job.kind == "news.ai":
        from app.news_center.jobs import process_ai
        process_ai(job)
        return
    with renewal(job):
        _process(job)


def _process(job):
    deadline = time.monotonic() + 180
    selected_id = None
    usage = {"input_tokens": None, "output_tokens": None, "cost_cny": None}
    try:
        for sequence in range(3):
            if time.monotonic() >= deadline:
                finish(job, state="failed", code="task_time_budget_exhausted", stage="本次生成已达到 180 秒总等待上限")
                return
            with Session(engine(), expire_on_commit=False) as db:
                with db.begin():
                    run, connection, fallback = validate_run(db, job, selected_id=selected_id)
                raw_key = service.connection_credential(db, connection, run.owner_id)
                if not raw_key:
                    raise APIError(409, "api_key_required", "当前模型凭据不可用，任务已停止。")
                db.commit()
                with db.begin():
                    run, connection, fallback = validate_run(db, job, selected_id=connection.id)
                    run.state, run.stage = "running", "备用模型生成中" if selected_id else "正在分析输入证据"
                    run.connection_id, run.model = connection.id, run.config_snapshot.get("plan_model") or connection.model
                    text = prompt(run)
                    attempt = attempt_record(run.owner_id, connection, "generate", run.id, model=run.model)
                    db.add(attempt)
                    attempt_id, allowed = attempt.id, {item["id"] for item in run.evidence_refs}

            def before_send(db, connection_id=connection.id):
                validate_run(db, job, selected_id=connection_id)

            reply, failure, _ = perform(attempt_id, connection, raw_key, prompt=text, system=instructions(run.feature),
                                        before_send=before_send, time_budget=max(.01, deadline - time.monotonic()), model=run.model)
            if reply:
                try:
                    result = parse_result(reply.text, allowed, feature=run.feature) if run.feature == 'plan_generation' else parse_result(reply.text, allowed)
                    if run.feature == 'plan_generation':
                        from app.tomorrow.jobs import validate_stage_result
                        result = validate_stage_result(run, result)
                except APIError as error:
                    with Session(engine()) as db, db.begin():
                        recorded = db.get(AiAttempt, attempt_id)
                        recorded.state, recorded.error_code = "failed", error.code
                        recorded.safe_details = {"message": error.message, "response_received": True}
                    finish(job, state="failed", code=error.code)
                    return
                with Session(engine()) as db:
                    attempts = db.scalars(select(AiAttempt).where(AiAttempt.run_id == run.id, AiAttempt.request_count == 1)).all()
                    for key, attr in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"), ("cost_cny", "cost_cny")):
                        values = [getattr(item, attr) for item in attempts]
                        usage[key] = float(sum(values)) if values and all(value is not None for value in values) and key == "cost_cny" else sum(values) if values and all(value is not None for value in values) else None
                finish(job, state="succeeded", result=result, usage=usage)
                return
            if failure.code == "outcome_unknown":
                finish(job, state="unknown", code=failure.code)
                return
            if not failure.retryable or sequence == 2:
                finish(job, state="cancelled" if failure.code in {"configuration_changed", "ai_disabled", "feature_disabled", "owner_permission_changed", "input_permission_changed"} else "failed", code=failure.code)
                return
            # Only an explicit 429/5xx response permits another actual request; at most three, including fallback.
            wait = max(2 ** (sequence + 1), max(0, (failure.retry_after - datetime.now(UTC)).total_seconds()) if failure.retry_after else 0)
            retry_at = datetime.now(UTC) + timedelta(seconds=wait)
            with Session(engine()) as db, db.begin():
                fenced(db, job)
                current = db.get(AiRun, run.id)
                current.state, current.stage = "retry_wait", "供应商明确拒绝，等待有限重试"
                recorded = db.get(AiAttempt, attempt_id)
                recorded.retry_at = retry_at
            if wait >= deadline - time.monotonic():
                finish(job, state="failed", code="retry_after_exceeds_budget", stage="供应商要求的重试时间超过本次 180 秒预算，已停止继续请求")
                return
            if sequence >= 1 and fallback:
                selected_id = fallback.id
            threading.Event().wait(wait)
    except APIError as error:
        finish(job, state="cancelled", code=error.code)
    except SQLAlchemyError:
        # Do not resubmit on database loss. Expiration recovery determines unknown from the committed send ledger.
        log.error("AI database unavailable; sent requests will not be automatically repeated")
    except Exception:
        # At this boundary, a remote side effect may already have happened. Conservative terminal state avoids double charges.
        finish(job, state="unknown", code="outcome_unknown")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        try:
            heartbeat("ai:" + socket.gethostname())
            job = claim()
            if job:
                process(job)
        except (SQLAlchemyError, APIError):
            log.error("AI worker unavailable; no request replay attempted")
        stop.wait(settings().poll_seconds)


if __name__ == "__main__":
    main()
