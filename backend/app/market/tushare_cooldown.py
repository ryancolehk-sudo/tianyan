"""Persist compatible-service instructions across its existing shared permit."""
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func,select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import lock,now
from app.market.http_retry import MAX_RETRY_AFTER
from app.market.models import DataFetchRun,SourceSettings
from app.market.protocol import SourceFailure
from app.market.tushare_specs import FAMILIES
from app.models import AuditEvent,Job,Provider

ACTION='source.request.tushare'


def cooldown_until(db,clock):
    ids=select(Provider.id).where(Provider.family.in_(FAMILIES))
    short=db.scalar(select(func.max(DataFetchRun.retry_not_before)).where(
        DataFetchRun.source_id.in_(ids),DataFetchRun.received_at<=clock,DataFetchRun.retry_not_before>clock))
    long=db.scalar(select(func.max(SourceSettings.legacy_cooldown_until)).where(
        SourceSettings.source_id.in_(ids),SourceSettings.legacy_cooldown_until>clock))
    return max(value for value in (short,long) if value is not None) if short or long else None


def run_deadline(stamp,reason,seconds,*,fallback_seconds=60):
    if reason=='rate_limited' and seconds is None:seconds=fallback_seconds
    if reason not in {'rate_limited','upstream_unavailable'} or type(seconds) is not int or not 0<=seconds<=86400:return None
    return stamp+timedelta(seconds=seconds)


def assert_request_not_paused(db,job):
    try:source=UUID(job.payload['source_id'])
    except (KeyError,TypeError,ValueError):raise SourceFailure('source_not_configured') from None
    config=db.get(SourceSettings,source)
    if config is not None and config.paused_reason=='retry_after_out_of_range':raise SourceFailure('source_paused')


def record_failure(job,error,*,fallback_seconds=60):
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
            seconds=error.retry_after if supplied else fallback_seconds
            deadline=clock+timedelta(seconds=seconds)
            config=db.get(SourceSettings,provider.id)
            if config is not None:config.legacy_cooldown_until=max(config.legacy_cooldown_until or deadline,deadline)
            if current is not None:
                current.result_ref={**(current.result_ref or {}),'tushare_cooldown':dict(
                    retry_not_before=deadline.isoformat(),seconds=seconds,
                    basis='supplier_retry_after' if supplied else 'local_backoff',reason=error.reason)}
        db.add(AuditEvent(actor_id=job.owner_id,action='source.retry_after.tushare',resource_type='job',
            resource_id=job.id,changed_field_names=['supplier_retry_after' if supplied else error.reason],created_at=clock))
