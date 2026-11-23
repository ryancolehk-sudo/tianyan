"""Persistent per-request permit; restarts never reset the public trial budget."""
import time
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import lock, now
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SHANGHAI, SourceFailure
from app.models import AuditEvent, Job, Provider

ACTION = 'source.request.tencent_auction_book'


def reserve_book_request(job, *, expected_configuration):
    from app.market.service import permission_reason
    from app.market.minute_backfill import configuration_fingerprint
    for _ in range(4):
        with Session(engine()) as db, db.begin():
            lock(db, ACTION)
            clock = now(db)
            current = db.scalar(select(Job).where(Job.id==job.id, Job.state=='running',
                Job.lease_token==job.lease_token, Job.lease_until>clock).with_for_update())
            if current is None: raise SourceFailure('lease_expired')
            provider = db.get(Provider, UUID(current.payload['source_id']))
            config = db.get(SourceSettings, provider.id) if provider else None
            if provider is None or config is None or provider.family!='tencent_auction' or permission_reason(provider):
                raise SourceFailure('permission_missing')
            if config.paused_reason and not (config.paused_reason=='schema_changed' and current.payload.get('schema_probe') is True):
                raise SourceFailure('source_paused')
            if config.config.get('collection_mode')!='full_market' or configuration_fingerprint(provider,config)!=expected_configuration:
                raise SourceFailure('configuration_changed')
            cooldown = db.scalar(select(func.max(DataFetchRun.retry_not_before)).where(
                DataFetchRun.source_id==provider.id, DataFetchRun.retry_not_before>clock))
            if cooldown: raise SourceFailure('rate_limited',retry_after=max(1,int((cooldown-clock).total_seconds())+1))
            midnight = clock.astimezone(SHANGHAI).replace(hour=0,minute=0,second=0,microsecond=0)
            count = db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==ACTION,AuditEvent.created_at>=midnight))
            if count>=600: raise SourceFailure('local_request_wait',retry_after=3600)
            last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action==ACTION))
            delay=max(0,(last+timedelta(seconds=2)-clock).total_seconds()) if last else 0
            if not delay:
                db.add(AuditEvent(actor_id=current.owner_id,action=ACTION,resource_type='job',resource_id=current.id,
                    changed_field_names=['public_auction_book_request'],created_at=clock))
                return True
        time.sleep(min(delay+.02,2.02))
    raise SourceFailure('local_request_wait',retry_after=2)
