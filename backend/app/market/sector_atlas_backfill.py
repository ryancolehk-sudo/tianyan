"""Resume up to fifty observed sessions outside the live collection window."""
import uuid
from datetime import time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import APIError
from app.config import settings
from app.db import engine
from app.evidence.common import lock, now
from app.evidence.models import EvidenceVersion
from app.integrations.credentials import job_context, resolve_source_credentials
from app.market.models import SourceSettings
from app.market.protocol import SHANGHAI
from app.market.review_store import available_trade_days, permitted_provider
from app.models import AuditEvent, Job, User
from app.market.models import DataFetchRun
from app.market.sector_atlas_repair import daily_ranking, required_codes, missing_codes, repair_slice

ACTION='sector.atlas.backfill.schedule'


def in_window(clock):
    local=clock.astimezone(SHANGHAI).time()
    return local<time(9,15) or local>=time(15,5)


def schedule_atlas_backfill():
    from app.maintenance_control import admit, paused
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.service import enqueue_collection
    if not settings().data_collection_enabled:
        return
    with Session(engine()) as db,db.begin():
        clock=now(db)
        if not in_window(clock) or paused(db):
            return
        admit(db);lock(db,ACTION)
        last=db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action==ACTION))
        if last and last+timedelta(seconds=60)>clock:
            return
        days=list(reversed(available_trade_days(db,limit=50,as_of_time=clock)))
        if not days:
            return
        db.add(AuditEvent(action=ACTION,resource_type='direction',changed_field_names=['fifty_session_backfill'],created_at=clock))
        for family,dataset in [('direction_kpl_strength','market.direction.kpl_atlas_v1'),('tushare_ths_daily','tushare.ths_daily')]:
            provider=permitted_provider(db,family)
            config=db.get(SourceSettings,provider.id) if provider else None
            owner=db.get(User,config.owner_id) if config else None
            if not config or config.paused_reason or not owner or not owner.enabled or owner.role!='admin':
                continue
            if family=='direction_kpl_strength':
                from app.market.kpl_atlas_budget import cooldown_until
                if cooldown_until(db,provider.id,clock):
                    continue
            if db.scalar(select(DataFetchRun.id).where(DataFetchRun.source_id==provider.id,
                    DataFetchRun.retry_not_before>clock).limit(1)):
                continue
            if db.scalar(select(Job.id).where(Job.payload['source_id'].astext==str(provider.id),
                    Job.state.in_(['queued','running']),Job.kind.in_(['source.collect','direction.archive'])).limit(1)):
                continue
            # Always prefer the most recent missing day; bounded failures do
            # not permanently starve older dates. Jobs/attempts survive restart.
            for day in days:
                repair_codes=[]
                key=(EvidenceVersion.payload['observed_day'].astext==day.isoformat() if family=='direction_kpl_strength'
                     else EvidenceVersion.payload['rows'][0]['trade_date'].astext==day.strftime('%Y%m%d'))
                conditions=[EvidenceVersion.source_id==provider.id,EvidenceVersion.payload['dataset'].astext==dataset,key]
                if family=='direction_kpl_strength':
                    conditions.append(EvidenceVersion.payload['rows'][0]['archive_kind'].astext=='daily')
                if family=='direction_kpl_strength':
                    saved=daily_ranking(db,provider.id,day,clock)
                    if saved:
                        required=required_codes(db,provider,owner.id,day,days,clock,saved)
                        repair_codes=repair_slice(db,provider.id,owner.id,day,
                            missing_codes(db,provider.id,day,required,clock))
                        if not repair_codes:
                            continue
                elif db.scalar(select(EvidenceVersion.id).where(*conditions).limit(1)):
                    continue
                target=(Job.payload['day'].astext if family=='direction_kpl_strength'
                        else Job.payload['dated_context']['requested_day'].astext)
                failures=list(db.scalars(select(Job).where(Job.payload['source_id'].astext==str(provider.id),
                    Job.payload['kpl_counts'].astext.is_distinct_from('true'),
                    Job.payload['kpl_leaders'].astext.is_distinct_from('true'),
                    target==day.isoformat(),Job.state=='failed',Job.created_at>=clock-timedelta(days=1))))
                if len(failures)>=3 or failures and max(j.completed_at or j.created_at for j in failures)+timedelta(minutes=15)>clock:
                    continue
                if family=='tushare_ths_daily':
                    try:
                        enqueue_collection(db,provider.id,day=day)
                    except APIError:
                        pass  # Source state reports the original refusal.
                else:
                    bound=resolve_source_credentials(db,provider.id,family)
                    if bound.reason:
                        break
                    payload=dict(source_id=str(provider.id),day=day.isoformat(),sector_atlas_backfill=True,
                        configuration_sha256=configuration_fingerprint(provider,config))
                    if repair_codes:
                        payload['leader_repair_codes']=repair_codes
                        payload['leader_required_codes']=required
                        from app.read_cache import cache_scope
                        payload['leader_interest_scope']=cache_scope(db,owner)
                    binding=job_context(bound,provider.id)
                    if binding is not None:
                        payload['integration_context']=binding
                    db.add(Job(owner_id=owner.id,kind='direction.archive',payload=payload))
                break
