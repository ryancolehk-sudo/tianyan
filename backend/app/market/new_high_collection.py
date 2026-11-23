"""Explicit bounded recovery through the existing persistent review worker."""
import uuid
from datetime import date

from app.auth import APIError, require_instance
from app.config import settings
from app.evidence.common import now
from app.market.new_high_protocol import HISTORY_SESSIONS
from app.market.postclose import default_day
from app.market.review_jobs import queue_dataset
from app.market.review_store import available_trade_days
from app.models import AuditEvent
from app.schemas import DTO


class NewHighCollectionInput(DTO):
    trade_day: date | None = None
    force: bool = False


def enqueue_new_highs(db, owner, body, request_id=None):
    require_instance(db)
    if not settings().data_collection_enabled:
        raise APIError(422,'collection_disabled','本机采集未启用。')
    clock = now(db)
    end = body.trade_day or default_day(db, clock)
    days = available_trade_days(db, limit=HISTORY_SESSIONS, end_day=end, as_of_time=clock) if end else []
    if not days or days[-1] != end:
        raise APIError(422,'calendar_not_ready','所选日期尚无已核实的收盘交易日历。')
    batch = uuid.uuid4()
    latest = available_trade_days(db,limit=1,as_of_time=clock)
    current = bool(latest and end==latest[-1])
    jobs = [queue_dataset(db, owner,'new_high_history',end,start_day=days[0],batch_id=batch,force=body.force,review_latest=current)]
    jobs.extend(queue_dataset(db,owner,'new_highs',day,batch_id=batch,force=body.force,review_latest=current and day==end) for day in reversed(days))
    # Reused completed jobs keep their old batch. Return those through a small
    # dependency-only prepare record so the standard status route remains exact.
    from app.models import Job
    marker = Job(owner_id=owner.id,kind='review.prepare',state='succeeded',
                 payload={'review_batch_id':str(batch),'calendar_jobs':[]},
                 result_ref={'collection_job_ids':[str(j.id) for j in jobs]},completed_at=clock)
    db.add(marker)
    db.add(AuditEvent(actor_id=owner.id,action='review.new_highs.request',resource_type='review',
                      request_id=request_id,changed_field_names=['dated_native_highs_recovery']))
    db.flush()
    return dict(batch_id=str(batch),state='queued',requested_sessions=len(days))
