"""Bounded post-close direction archives, separate from live schema probes."""
import hashlib
import uuid
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.common import lock, now
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import ReferencePayload
from app.integrations.credentials import assert_source_binding, job_context, resolve_source_credentials
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.realtime_schedule import RealtimeWait
from app.market.review_store import available_trade_days, family_id, permitted_provider
from app.models import AuditEvent, Job, Provider, User

FAMILIES=('direction_em_hot','direction_kpl_strength')
INTERVAL=timedelta(minutes=15)
ACTION='direction.archive.schedule'
REQUEST_ACTION='source.request.direction-history'
MAX_ATTEMPTS=3


def in_window(clock):
    local=clock.astimezone(SHANGHAI).time()
    return time(9)<=local<time(9,15) or time(15,5)<=local<time(21,30)


def _saved(db,family,day):
    dataset='market.direction.em_hot_daily' if family=='direction_em_hot' else 'market.direction.kpl_atlas_v1'
    stmt=select(EvidenceVersion.id).where(EvidenceVersion.source_id==family_id(family),
        EvidenceVersion.payload['dataset'].astext==dataset,
        EvidenceVersion.payload['observed_day'].astext==day.isoformat())
    if family=='direction_kpl_strength':
        from app.market.direction_sources import KPL_HISTORY_URL
        stmt=stmt.where(EvidenceVersion.payload['source_url'].astext==KPL_HISTORY_URL)
    return db.scalar(stmt.limit(1)) is not None


def schedule_direction_archives():
    from app.market.kpl_count_jobs import schedule as schedule_counts
    schedule_counts()
    schedule_counts(leaders=True)
    if not settings().data_collection_enabled:
        return
    from app.maintenance_control import admit,paused
    from app.market.minute_backfill import configuration_fingerprint
    with Session(engine()) as db,db.begin():
        clock=now(db)
        if not in_window(clock) or paused(db):return
        admit(db);lock(db,ACTION)
        last=db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action==ACTION))
        if last and last+INTERVAL>clock:return
        days=available_trade_days(db,limit=2,as_of_time=clock)
        if not days:return
        day=days[-1]
        db.add(AuditEvent(action=ACTION,resource_type='direction',changed_field_names=['closed_day_archives'],created_at=clock))
        for family in FAMILIES:
            provider=permitted_provider(db,family)
            config=db.get(SourceSettings,provider.id) if provider else None
            owner=db.get(User,config.owner_id) if config else None
            if not config or config.paused_reason not in {None,'schema_changed'} or not owner or not owner.enabled or owner.role!='admin':continue
            if _saved(db,family,day):continue
            lock(db,'direction:archive:'+str(provider.id)+':'+str(day))
            jobs=list(db.scalars(select(Job).where(Job.kind=='direction.archive',Job.payload['source_id'].astext==str(provider.id),
                Job.payload['kpl_counts'].astext.is_distinct_from('true'),
                Job.payload['kpl_leaders'].astext.is_distinct_from('true'),
                Job.payload['day'].astext==day.isoformat()).order_by(Job.created_at.desc())))
            limit=MAX_ATTEMPTS+1 if clock.astimezone(SHANGHAI).time()<time(9,15) else MAX_ATTEMPTS
            if len(jobs)>=limit:continue
            prior=jobs[0] if jobs else None
            if prior and (prior.state in {'queued','running'} or (prior.completed_at or prior.created_at)+INTERVAL>clock):continue
            bound=resolve_source_credentials(db,provider.id,family)
            if bound.reason:continue
            payload=dict(source_id=str(provider.id),day=day.isoformat(),
                previous_day=days[-2].isoformat() if len(days)>1 else None,
                configuration_sha256=configuration_fingerprint(provider,config))
            binding=job_context(bound,provider.id)
            if binding is not None:payload['integration_context']=binding
            db.add(Job(owner_id=owner.id,kind='direction.archive',payload=payload))


def _permit(job, *, reserve=True):
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.service import permission_reason
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',Job.lease_token==job.lease_token).with_for_update())
        clock=now(db)
        if current is None or current.lease_until is None or current.lease_until<=clock:raise SourceFailure('lease_lost')
        from app.maintenance_control import admit,paused
        if paused(db):
            current.state,current.available_at='queued',clock+timedelta(seconds=60)
            current.attempts=max(0,current.attempts-1);current.error_code='maintenance_wait'
            current.lease_token=current.lease_until=None;return None
        admit(db)
        provider=db.get(Provider,uuid.UUID(current.payload['source_id']));config=db.get(SourceSettings,provider.id) if provider else None
        if (not config or provider.family not in FAMILIES or permission_reason(provider)
                or config.paused_reason not in {None,'schema_changed'}):raise SourceFailure('permission_missing')
        if current.payload!=job.payload or current.owner_id!=config.owner_id or configuration_fingerprint(provider,config)!=job.payload['configuration_sha256']:
            raise SourceFailure('configuration_changed')
        owner=db.get(User,current.owner_id)
        if owner is None or not owner.enabled or owner.role!='admin':raise SourceFailure('permission_missing')
        from datetime import date
        day=date.fromisoformat(job.payload['day'])
        if day not in available_trade_days(db,limit=200,as_of_time=clock):raise SourceFailure('calendar_not_ready')
        from app.market.sector_atlas_backfill import in_window as atlas_window
        if not (atlas_window(clock) if current.payload.get('sector_atlas_backfill') is True else in_window(clock)):
            local=clock.astimezone(SHANGHAI)
            next_day=local.date()+(timedelta(days=1) if local.time()>=time(21,30) else timedelta())
            opening=time(9) if local.time()<time(9) or local.time()>=time(21,30) else time(15,5)
            deadline=datetime.combine(next_day,opening,SHANGHAI)
            current.state,current.available_at='queued',deadline;current.attempts=max(0,current.attempts-1)
            current.error_code='publication_window_wait';current.lease_token=current.lease_until=None
            return None
        # These endpoints request an explicit closed historical date. Recovery
        # on a weekend must validate that date, not require today to be open.
        assert_source_binding(db,provider.id,provider.family,current.payload)
        if not reserve:return provider,config,day
        if provider.family=='direction_kpl_strength':
            from app.market.registry import SOURCES
            if provider.adapter_version!=SOURCES[provider.family].version:
                raise SourceFailure('adapter_version_mismatch')
        cooldown=db.scalar(select(func.max(DataFetchRun.retry_not_before)).where(
            DataFetchRun.source_id==provider.id,DataFetchRun.retry_not_before>clock))
        if provider.family=='direction_kpl_strength':
            from app.market.kpl_atlas_budget import cooldown_until
            cooldown=cooldown_until(db,provider.id,clock)
        if cooldown:
            raise SourceFailure('rate_limited',retry_after=max(1,int((cooldown-clock).total_seconds())+1))
        lock(db,REQUEST_ACTION)
        last=db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action==REQUEST_ACTION))
        start=datetime.combine(clock.astimezone(SHANGHAI).date(),time.min,SHANGHAI)
        used=db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==REQUEST_ACTION,AuditEvent.created_at>=start))
        if used>=120 or last and last+timedelta(seconds=2)>clock:
            current.state,current.available_at='queued',clock+timedelta(seconds=3) if used<120 else start+timedelta(days=1,hours=15,minutes=5)
            current.attempts=max(0,current.attempts-1);current.error_code='source_budget_wait'
            current.lease_token=current.lease_until=None;return None
        db.add(AuditEvent(actor_id=current.owner_id,action=REQUEST_ACTION,resource_type='job',resource_id=current.id,
            changed_field_names=['dated_history_request'],created_at=clock))
        return provider,config,day


def process_job(job):
    if job.payload.get('kpl_counts') is True or job.payload.get('kpl_leaders') is True:
        from app.market.kpl_count_jobs import process_job as collect_counts
        return collect_counts(job)
    from app.market.collect import store_reference_batch
    from app.market.reference import ReferenceBatch
    from app.worker import finish
    from app.market import em_rank_history
    from app.market.direction_history import fetch_history
    started=datetime.now(SHANGHAI);response=None;requests=0;batch=None;reason=None;diagnostic=None;retry_after=None
    histories={};candidates={}
    permitted=_permit(job,reserve=False)
    if permitted is None:return
    provider,config,day=permitted
    try:
        if provider.family=='direction_kpl_strength':
            from app.market.kpl_atlas_source import fetch as fetch_atlas
            from app.market.kpl_atlas_budget import reserve as reserve_kpl, record_failure
            def permit_page():
                return _permit(job) is not None and reserve_kpl(job,
                    expected_configuration=job.payload['configuration_sha256'])
            # Today's leading boards also need dated leaders when they were
            # outside the old top ten. Read interests from our saved source.
            from app.market.sector_atlas_repair import daily_ranking, required_codes, missing_codes, MAX_REPAIR_CODES
            with Session(engine()) as db:
                clock=now(db)
                saved=daily_ranking(db,provider.id,day,clock)
                days=available_trade_days(db,limit=50,as_of_time=clock)
                leader_codes=required_codes(db,provider,job.owner_id,day,days,clock,saved)
                if saved:
                    wanted=job.payload.get('leader_repair_codes',leader_codes)
                    leader_codes=missing_codes(db,provider.id,day,
                        [c for c in wanted if c in leader_codes],clock)[:MAX_REPAIR_CODES]
            if saved and not leader_codes:
                batch=ReferenceBatch(records=[],expected=0,requests=0)
            else:
                kwargs=dict(day=day,before_request=permit_page,include_leaders=True,leader_codes=leader_codes,
                    on_failure=lambda error:record_failure(job,error))
                if saved:
                    kwargs['saved_rows']=saved.payload['rows']
                response,batch=fetch_atlas(**kwargs)
            if batch.rejected:
                reason='leader_details_incomplete'
            requests=response.request_count if response else 0
        else:
            # Candidates come only from saved permitted same-day ranking rows.
            with Session(engine()) as db:
                candidates={}
                for family,dataset in (('direction_em_hot','market.direction.em_hot'),
                        ('direction_ths_hot','market.direction.ths_hot'),('direction_tdx_hot','market.direction.tdx_hot')):
                    allowed=permitted_provider(db,family)
                    if not allowed:continue
                    for saved in db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==allowed.id,
                        EvidenceVersion.payload['dataset'].astext==dataset,
                        EvidenceVersion.payload['observed_day'].astext==day.isoformat())
                        .order_by(EvidenceVersion.received_at.desc()).limit(100)):
                        for row in saved.payload.get('rows',[]):
                            candidates.setdefault(row['code'],row.get('name') or row['code'])
                allowed=permitted_provider(db,'mairui_pools')
                if allowed:
                    pool=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==allowed.id,
                        EvidenceVersion.payload['dataset'].astext=='market.mairui.limit_up',
                        EvidenceVersion.payload['observed_day'].astext==day.isoformat())
                        .order_by(EvidenceVersion.received_at.desc()).limit(1))
                    if pool:
                        import re
                        for row in pool.payload.get('rows',[]):
                            symbol=str(row.get('symbol',''))
                            match=re.fullmatch(r'(?:sh|sz)?([036]\d{5})',symbol)
                            if match:candidates.setdefault(match[1],row.get('name') or match[1])
            if not candidates:raise SourceFailure('historical_candidates_missing')
            histories={};records=[]
            with Session(engine()) as db:
                for saved in db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id,
                    EvidenceVersion.payload['dataset'].astext=='market.direction.em_rank_history',
                    EvidenceVersion.payload['observed_day'].astext==day.isoformat())
                    .order_by(EvidenceVersion.received_at.desc()).limit(120)):
                    old=saved.payload.get('rows',[])
                    if not old:continue
                    code=old[0]['code']
                    if code in histories:continue
                    histories[code]=dict(name=candidates.get(code,code),
                        rows=[{**row,'rank':int(row['rank']),'source_row':int(row['source_row'])} for row in old],
                        body_sha256=old[0]['body_sha256'],received_at=old[0]['fetched_at'])
            from datetime import date
            previous=date.fromisoformat(job.payload['previous_day']) if job.payload.get('previous_day') else None
            loaded_codes=set(histories)
            ranks=em_rank_history.top_ten(histories,day,previous)
            for index,(code,name) in enumerate(list(candidates.items())[:80]):
                if ranks is not None:break
                if code in histories:continue
                if requests:
                    import time as timer
                    timer.sleep(2.05)
                if _permit(job) is None:return
                requests+=1
                symbol=('SH' if code.startswith('6') else 'SZ')+code
                response=em_rank_history.fetch(symbol)
                rows=em_rank_history.parse(response.body,symbol,response.received_at)
                histories[code]=dict(name=name,rows=rows,body_sha256=hashlib.sha256(response.body).hexdigest(),
                    received_at=response.received_at.isoformat())
                ranks=em_rank_history.top_ten(histories,day,previous)
                if ranks is not None:break
            for code,entry in histories.items():
                if code in loaded_codes:continue
                original=ReferencePayload(dataset='market.direction.em_rank_history',title='东方财富个股历史日频人气',
                    source_url=em_rank_history.URL,attribution='东方财富历史人气序列；本人本机研究',observed_day=day,
                    scope='来源返回的日频排名序列；保留来源日期、原行号与该次请求接收时刻。',
                    columns=[{'key':k,'label':k} for k in ('code','trade_date','rank','source_row','body_sha256','fetched_at')],
                    rows=[{**row,'rank':str(row['rank']),'source_row':str(row['source_row']),
                        'body_sha256':entry['body_sha256'],'fetched_at':entry['received_at']} for row in entry['rows']])
                records.append(('direction.em_rank_history:'+str(day)+':'+code,original,None))
            if ranks is None:
                reason='historical_top_ten_incomplete'
            else:
                payload=ReferencePayload(dataset='market.direction.em_hot_daily',title='东方财富历史日频人气前10',
                    source_url=em_rank_history.URL,attribution='东方财富历史人气序列；本人本机研究',observed_day=day,
                    scope='按历史序列返回日期筛选；来源原始排名1至10均已取得后发布。日期为来源日频日期，未提供独立盘中时刻；名次差仅比较已核上一交易日。',
                    columns=[{'key':k,'label':k} for k in ranks[0]],rows=ranks)
                records.append(('direction.em_hot_daily:'+str(day),payload,None))
            batch=ReferenceBatch(records=records,expected=len(records),requests=requests)
    except RealtimeWait:
        return
    except SourceFailure as error:
        requests=getattr(error,'request_count',requests)
        retry_after=error.retry_after
        if error.reason in {'rate_limited','local_request_wait'}:
            retry_after=max(1,retry_after or (300 if error.reason=='rate_limited' else 3))
            batch=getattr(error,'partial_batch',batch)
        reason=error.reason;response=getattr(error,'response',response);diagnostic=getattr(error,'direction_diagnostic',None)
    received=response.received_at if response else datetime.now(SHANGHAI)
    with Session(engine()) as db,db.begin():
        current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',Job.lease_token==job.lease_token,
            Job.lease_until>func.now()).with_for_update())
        if current is None:return
        from app.market.minute_backfill import configuration_fingerprint
        live_provider=db.get(Provider,provider.id);live_config=db.get(SourceSettings,provider.id)
        if not live_config or configuration_fingerprint(live_provider,live_config)!=job.payload['configuration_sha256']:
            reason=reason or 'configuration_changed';batch=None
        from app.market.service import permission_reason
        if permission_reason(live_provider):
            reason=reason or 'permission_missing';batch=None
        try:
            assert_source_binding(db,provider.id,provider.family,current.payload)
        except SourceFailure as error:
            reason=reason or error.reason;batch=None
        accepted=duplicates=0
        if batch is not None and reason in {None,'historical_top_ten_incomplete','leader_details_incomplete','rate_limited','local_request_wait'}:
            accepted,duplicates=store_reference_batch(db,live_provider,live_config,batch,received)
        db.add(DataFetchRun(source_id=provider.id,job_id=job.id,capability='direction.daily_history',
            started_at=started,received_at=received,status='partial' if batch and batch.rejected else 'complete' if reason is None else 'partial' if reason=='historical_top_ten_incomplete' else 'error',
            reason=reason or ('leader_details_incomplete' if batch and batch.rejected else None),accepted=accepted,duplicates=duplicates,rejected=batch.rejected if batch else 0,expected=batch.expected if batch else None,
            elapsed_ms=response.elapsed_ms if response else None,request_count=requests,cost_cny=None,
            retry_not_before=received+timedelta(seconds=retry_after) if retry_after and retry_after<=86400 and reason!='local_request_wait' else None,
            body_hash=hashlib.sha256(response.body).hexdigest() if response else None))
        current.result_ref={'requested_day':day.isoformat(),'historical_only':True,'requests':requests}
        if provider.family=='direction_em_hot':
            current.result_ref={**current.result_ref,'known_source_ranks':sorted({r['rank'] for entry in histories.values()
                for r in entry['rows'] if r['trade_date']==day.isoformat()}),'candidate_count':len(candidates)}
        if diagnostic:current.result_ref={**current.result_ref,'direction_diagnostic':diagnostic}
        current.state='succeeded' if reason is None else 'failed';current.error_code=reason;current.completed_at=received
        current.lease_token=current.lease_until=None
