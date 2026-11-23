"""Honor real THS funds cooldowns; no invented vendor rate or daily quota."""
from datetime import timedelta
from math import ceil
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import lock,now
from app.market.models import SourceSettings
from app.market.protocol import SourceFailure
from app.market.http_retry import MAX_RETRY_AFTER,retry_after
from app.market.supplier_cooldown import THS_FAMILIES as FAMILIES,shared_deadline
from app.models import AuditEvent,Job,Provider,User

ACTION='source.cooldown.ths-funds'


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
                current.result_ref={**(current.result_ref or {}),'ths_cooldown':dict(
                    retry_not_before=deadline.isoformat(),seconds=seconds,
                    basis='supplier_retry_after' if supplied else 'local_backoff',reason=error.reason)}
        db.add(AuditEvent(actor_id=job.owner_id,action='source.retry_after.ths-funds',resource_type='job',
            resource_id=job.id,changed_field_names=['supplier_retry_after' if supplied else error.reason],created_at=clock))


def reserve(job,*,expected_configuration):
    from app.config import settings
    from app.integrations.credentials import assert_source_binding
    from app.maintenance_control import admit,paused
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.registry import SOURCES
    from app.market.service import permission_reason
    from app.market.auction_schedule import schema_recheck
    if not settings().data_collection_enabled:raise SourceFailure('collection_disabled')
    with Session(engine()) as db,db.begin():
        lock(db,ACTION);clock=now(db)
        current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',
            Job.lease_token==job.lease_token,Job.lease_until>clock).with_for_update())
        if current is None:raise SourceFailure('lease_lost')
        if paused(db):raise SourceFailure('maintenance_wait')
        admit(db)
        provider=db.get(Provider,UUID(current.payload['source_id']))
        config=db.get(SourceSettings,provider.id) if provider else None
        owner=db.get(User,current.owner_id)
        if (provider is None or provider.family not in FAMILIES or config is None or permission_reason(provider)
                or owner is None or not owner.enabled or owner.role!='admin' or config.owner_id!=owner.id):
            raise SourceFailure('permission_missing')
        if provider.adapter_version!=SOURCES[provider.family].version:raise SourceFailure('adapter_version_mismatch')
        if current.payload!=job.payload or configuration_fingerprint(provider,config)!=expected_configuration:
            raise SourceFailure('configuration_changed')
        if config.paused_reason and not (job.payload.get('schema_probe') is True and schema_recheck(provider.family,config.paused_reason)):
            raise SourceFailure('source_paused')
        assert_source_binding(db,provider.id,provider.family,current.payload)
        deadline=cooldown_until(db,clock)
        if deadline is not None:
            error=SourceFailure('local_request_wait',retry_after=max(1,ceil((deadline-clock).total_seconds())))
            error.request_count=0
            raise error
        return True
