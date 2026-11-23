"""Real Fuyao shared cooldowns, separate from ordinary sampling schedules."""
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import lock,now
from app.market.connected_specs import FUYAO_FAMILIES as FAMILIES
from app.market.http_retry import MAX_RETRY_AFTER
from app.market.models import SourceSettings
from app.market.supplier_cooldown import shared_deadline
from app.models import AuditEvent,Job,Provider

ACTION='source.cooldown.fuyao'


def cooldown_until(db,clock):
    return shared_deadline(db,clock,FAMILIES)


def record_failure(job,error):
    supplied=type(error.retry_after) is int and 0<=error.retry_after<=MAX_RETRY_AFTER
    if error.reason not in {'rate_limited','retry_after_out_of_range'} and not (
            error.reason=='upstream_unavailable' and supplied):return
    with Session(engine()) as db,db.begin():
        lock(db,ACTION);clock=now(db)
        provider=db.get(Provider,UUID(job.payload['source_id']))
        if provider is None or provider.family not in FAMILIES:return
        current=db.get(Job,job.id)
        if error.reason=='retry_after_out_of_range':
            for config in db.scalars(select(SourceSettings).join(Provider,Provider.id==SourceSettings.source_id)
                    .where(Provider.family.in_(FAMILIES))):config.paused_reason=error.reason
        else:
            seconds=error.retry_after if supplied else 300
            deadline=clock+timedelta(seconds=seconds)
            config=db.get(SourceSettings,provider.id)
            if config is not None:config.legacy_cooldown_until=max(config.legacy_cooldown_until or deadline,deadline)
            if current is not None:
                current.result_ref={**(current.result_ref or {}),'fuyao_cooldown':dict(
                    retry_not_before=deadline.isoformat(),seconds=seconds,
                    basis='supplier_retry_after' if supplied else 'local_backoff',reason=error.reason)}
        db.add(AuditEvent(actor_id=job.owner_id,action='source.retry_after.fuyao',resource_type='job',
            resource_id=job.id,changed_field_names=['supplier_retry_after' if supplied else error.reason],created_at=clock))
