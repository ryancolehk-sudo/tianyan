import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.ai import protocols
from app.ai.models import AiAttempt
from app.auth import APIError
from app.db import engine


def attempt_record(owner_id, connection, purpose, run_id=None, *, model=None):
    return AiAttempt(id=uuid.uuid4(), owner_id=owner_id, run_id=run_id, connection_id=connection.id,
                     service_name=connection.name, model=model or connection.model, purpose=purpose, state="preparing",
                     started_at=datetime.now(UTC), request_count=0, safe_details={})


def price(reply, options):
    incoming, outgoing = options.get("input_price_per_million_cny"), options.get("output_price_per_million_cny")
    if reply.input_tokens is None or reply.output_tokens is None or incoming is None or outgoing is None:
        return None
    return (Decimal(str(incoming)) * reply.input_tokens + Decimal(str(outgoing)) * reply.output_tokens) / 1000000


def perform(attempt_id, connection, credential, *, prompt=None, system=None, discovery=False, probe=False, before_send=None, time_budget=None, model=None):
    started = time.monotonic()

    def sent():
        # This commit precedes network I/O. A killed process leaves an auditable unknown attempt, never a retry lease.
        with Session(engine()) as db, db.begin():
            if before_send:
                before_send(db)
            row = db.get(AiAttempt, attempt_id)
            if row is None or row.state != "preparing":
                raise APIError(409, "attempt_already_sent", "本次请求已处理，不能重复发出。")
            row.sent_at, row.state, row.request_count = datetime.now(UTC), "running", 1

    reply, failure = None, None
    try:
        timeout = min(30, connection.options.get("timeout_seconds", 60)) if probe else connection.options.get("timeout_seconds", 60)
        if time_budget is not None:
            timeout = min(timeout, time_budget)
        kwargs = {"system": system, "discovery": discovery, "timeout": timeout,
                  "max_tokens": connection.options.get("max_output_tokens", 1200), "on_send": sent}
        if connection.template == "chatgpt_plan":
            from app.ai.chatgpt_transport import exchange
            reply = exchange(credential, model or connection.model, prompt, **kwargs)
        else:
            reply = protocols.exchange(connection.endpoint, connection.protocol, credential, connection.model, prompt, **kwargs)
    except protocols.ModelFailure as error:
        failure = error
    except APIError as error:
        failure = protocols.ModelFailure(error.code, error.message)
    except Exception:
        # Never leak exception bodies: third-party text can contain secrets. The ledger is authoritative for whether I/O started.
        with Session(engine()) as db:
            recorded = db.get(AiAttempt, attempt_id)
            sent_value = bool(recorded and recorded.sent_at)
        failure = protocols.ModelFailure("outcome_unknown" if sent_value else "request_invalid", "请求结果未知，未自动重发。" if sent_value else "请求准备失败，尚未发出。", sent=sent_value)
    elapsed = int((time.monotonic() - started) * 1000)
    with Session(engine()) as db, db.begin():
        row = db.get(AiAttempt, attempt_id)
        row.completed_at, row.elapsed_ms = datetime.now(UTC), elapsed
        if failure:
            row.state = "unknown" if failure.code == "outcome_unknown" else "failed"
            row.error_code = failure.code
            row.retry_at = failure.retry_after
            row.safe_details = {"message": failure.message, "http_status": failure.status, "retryable": failure.retryable}
        else:
            row.state = "succeeded"
            row.input_tokens, row.output_tokens = reply.input_tokens, reply.output_tokens
            row.cost_cny = None if connection.template == "chatgpt_plan" else price(reply, connection.options)
            row.safe_details = {"http_status": 200, "model_list_count": len(reply.models or [])} if discovery else {"http_status": 200}
        if connection.template == "chatgpt_plan":
            row.safe_details = {**row.safe_details, "billing_source": "chatgpt_plan"}
            if failure:
                for field in ("request_id", "upstream_code", "param", "body_shape"):
                    value = getattr(failure, field, None)
                    if value is not None:
                        row.safe_details[field] = value
    return reply, failure, elapsed
