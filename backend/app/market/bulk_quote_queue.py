"""Keep long automatic SDK reads out of an enabled bulk quote's live window.

Unknown calendars can still bootstrap through the original SDK path. Manual
jobs keep their explicit intent; no additional worker or service is introduced.
"""
from datetime import datetime, time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import now
from app.market.models import SourceSettings
from app.market.protocol import SHANGHAI, SourceFailure
from app.models import Job, Provider

SDK=frozenset({'baostock_daily','baostock_history','baostock_fundamental','baostock_classification'})


def deferred_background(db, family, clock):
    if family not in SDK:return None
    from app.market.service import source_id,permission_reason
    from app.market.session import market_session
    provider=db.get(Provider,source_id('tencent_market_rt'))
    config=db.get(SourceSettings,provider.id) if provider else None
    local=clock.astimezone(SHANGHAI)
    if (provider and config and not config.paused_reason and not permission_reason(provider)
            and time(9)<=local.time()<time(15,10) and market_session(db,clock).is_trading_day is True):
        return datetime.combine(local.date(),time(15,10),SHANGHAI)
    return None


def permit(job, expected_configuration):
    if job.payload.get('scheduled_reference') is not True:return True
    from app.market.minute_backfill import configuration_fingerprint
    with Session(engine()) as db,db.begin():
        clock=now(db)
        current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',
            Job.lease_token==job.lease_token,Job.lease_until>clock).with_for_update())
        if current is None:raise SourceFailure('lease_lost')
        provider=db.get(Provider,UUID(current.payload['source_id']))
        if provider.family not in SDK:return True
        config=db.get(SourceSettings,provider.id)
        if (current.payload!=job.payload or config.owner_id!=current.owner_id
                or configuration_fingerprint(provider,config)!=expected_configuration):
            raise SourceFailure('configuration_changed')
        deadline=deferred_background(db,provider.family,clock)
        if deadline is not None:
            current.state,current.available_at='queued',deadline
            current.attempts=max(0,current.attempts-1)
            current.error_code='publication_window_wait'
            current.lease_token=current.lease_until=None
            config.next_run_at=max(config.next_run_at,deadline)
            return False
        return True
