"""Shared, persisted physical-request pacing for KPL current and dated pages."""
import time
from math import ceil
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import lock, now
from app.integrations.credentials import assert_source_binding
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SourceFailure
from app.models import AuditEvent, Job, Provider, User

ACTION = 'source.request.kpl-atlas'
FAMILIES = {'direction_kpl_strength', 'direction_kpl_emotion', 'direction_kpl_top'}


def local_wait(seconds):
    error=SourceFailure('local_request_wait',retry_after=max(1,ceil(seconds)))
    error.request_count=0
    return error


def cooldown_until(db, source_id, clock):
    ids=select(Provider.id).where(Provider.family.in_(FAMILIES))
    short=db.scalar(select(func.max(DataFetchRun.retry_not_before)).where(
        DataFetchRun.source_id.in_(ids),DataFetchRun.retry_not_before>clock))
    # Existing durable source cooldown column has no 1-day fetch-run limit.
    # For KPL only, retain exact long supplier deadlines here. Never use the
    # mutable ordinary next_run_at schedule as an upstream cooldown.
    long=db.scalar(select(func.max(SourceSettings.legacy_cooldown_until)).where(
        SourceSettings.source_id.in_(ids),SourceSettings.legacy_cooldown_until>clock))
    return max(value for value in (short,long) if value is not None) if short or long else None


def record_failure(job, error):
    """Persist a supplier cooldown before terminal job handling can fail.

    This zero-request operational receipt is not another physical request.
    Actual request counts remain on the caller's terminal fetch run.
    """
    if error.reason not in {'rate_limited','retry_after_out_of_range'}:
        return
    supplied=type(error.retry_after) is int and error.retry_after>0
    seconds = error.retry_after if supplied else 300
    with Session(engine()) as db, db.begin():
        lock(db, ACTION)
        provider = db.get(Provider, UUID(job.payload['source_id']))
        if provider is None or provider.family not in FAMILIES:
            return
        clock = now(db)
        if error.reason=='rate_limited':
            config=db.get(SourceSettings,provider.id)
            if config is not None:
                deadline=clock+timedelta(seconds=seconds)
                config.legacy_cooldown_until=max(config.legacy_cooldown_until or deadline,deadline)
        if error.reason=='retry_after_out_of_range':
            config=db.get(SourceSettings,provider.id)
            if config is not None:
                config.paused_reason=error.reason
            db.add(AuditEvent(actor_id=job.owner_id,action='source.retry_after.pause',
                resource_type='job',resource_id=job.id,changed_field_names=['paused_reason'],created_at=clock))
        db.add(DataFetchRun(source_id=provider.id,job_id=job.id,capability='sector.atlas.cooldown',
            started_at=clock,received_at=clock,status='error',reason=error.reason,
            request_count=0,retry_not_before=clock+timedelta(seconds=seconds)
                if error.reason=='rate_limited' and seconds<=86400 else None))
        if error.reason=='rate_limited':
            db.add(AuditEvent(actor_id=job.owner_id,action='source.retry_after.kpl',
                resource_type='job',resource_id=job.id,
                changed_field_names=['supplier_retry_after' if supplied else 'local_backoff_300_seconds'],created_at=clock))


def reserve(job, *, expected_configuration=None):
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.service import permission_reason
    from app.maintenance_control import admit, paused
    from app.config import settings
    if not settings().data_collection_enabled:
        raise SourceFailure('collection_disabled')
    for _ in range(5):
        with Session(engine()) as db, db.begin():
            lock(db, ACTION)
            clock = now(db)
            current = db.scalar(select(Job).where(Job.id==job.id, Job.state=='running',
                Job.lease_token==job.lease_token, Job.lease_until>clock).with_for_update())
            if current is None:
                raise SourceFailure('lease_lost')
            if paused(db):
                raise SourceFailure('maintenance_wait')
            admit(db)
            provider = db.get(Provider, UUID(current.payload['source_id']))
            config = db.get(SourceSettings, provider.id) if provider else None
            owner = db.get(User, current.owner_id)
            if not config or provider.family not in FAMILIES or permission_reason(provider):
                raise SourceFailure('permission_missing')
            from app.market.registry import SOURCES
            if provider.adapter_version!=SOURCES[provider.family].version:
                raise SourceFailure('adapter_version_mismatch')
            if not owner or not owner.enabled or owner.role!='admin' or config.owner_id!=owner.id:
                raise SourceFailure('permission_missing')
            if current.payload!=job.payload or expected_configuration and configuration_fingerprint(provider,config)!=expected_configuration:
                raise SourceFailure('configuration_changed')
            if config.paused_reason not in {None,'schema_changed'}:
                raise SourceFailure('source_paused')
            assert_source_binding(db,provider.id,provider.family,current.payload)
            cooldown = cooldown_until(db,provider.id,clock)
            if cooldown:
                raise local_wait((cooldown-clock).total_seconds())
            last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action==ACTION))
            delay = max(0, ((last+timedelta(seconds=2))-clock).total_seconds()) if last else 0
            if not delay:
                db.add(AuditEvent(actor_id=owner.id,action=ACTION,resource_type='job',resource_id=current.id,
                    changed_field_names=['physical_page_request'],created_at=clock))
                return True
        time.sleep(min(delay+0.05,2.1))
    raise local_wait(3)
