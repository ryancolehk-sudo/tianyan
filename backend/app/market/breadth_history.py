"""An independent, explicitly dated history curve; never a live polling source."""
from datetime import UTC, date, datetime, time, timedelta
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import APIError, require_instance
from app.config import settings
from app.db import engine
from app.evidence.common import lock, now
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import ReferencePayload
from app.integrations.credentials import assert_source_binding, job_context, resolve_source_credentials
from app.market.breadth_history_specs import FAMILY, NAME, VERSION, CAPABILITY, DATASET, ENDPOINT, REFERENCE, SCOPE, MINIMUM_INTERVAL
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import NoRedirect, Response, SHANGHAI, SourceFailure
from app.models import AuditEvent, Job, Provider, User

JOB_KIND='breadth.archive'
ACTION='breadth.history.schedule'
BUDGET_ACTION='breadth.history.request'
DAILY_LIMIT=20
MAX_ATTEMPTS=3
MAX_ROWS=1000
LOOKBACK_DAYS=365


def in_window(clock):
    local=clock.astimezone(SHANGHAI).time()
    return time(15,5)<=local<time(21,30) or time(9)<=local<time(9,15)


def next_window(clock):
    local=clock.astimezone(SHANGHAI)
    if local.time()<time(9): return datetime.combine(local.date(),time(9),SHANGHAI)
    if local.time()<time(9,15): return clock
    if local.time()<time(15,5): return datetime.combine(local.date(),time(15,5),SHANGHAI)
    if local.time()<time(21,30): return clock
    return datetime.combine(local.date()+timedelta(days=1),time(9),SHANGHAI)


def history_url(day):
    if type(day) is not date:
        raise SourceFailure('invalid_request_day')
    return ENDPOINT+'?'+urllib.parse.urlencode({'fields':'limit_up_count,limit_down_count','date':day.isoformat()})


def fetch_history(day):
    """Fixed HTTPS destination, one request, bounded bytes and no redirects."""
    import time as timer
    started=timer.monotonic()
    request=urllib.request.Request(history_url(day),headers={
        'User-Agent':'Tianyan/1.0 (private historical research)',
        'Accept':'application/json','Accept-Encoding':'identity'})
    try:
        opener=urllib.request.build_opener(NoRedirect(),urllib.request.ProxyHandler({}))
        with opener.open(request,timeout=12) as response:
            raw=response.read(2_000_001)
            if len(raw)>2_000_000: raise SourceFailure('response_too_large')
            if response.headers.get('Content-Encoding','').lower() not in {'','identity'}:
                raise SourceFailure('unsafe_upstream_response')
            return Response(raw,datetime.now(UTC),round((timer.monotonic()-started)*1000),response.status)
    except urllib.error.HTTPError as error:
        reason='permission_missing' if error.code in {401,403} else 'rate_limited' if error.code==429 else 'upstream_unavailable'
        raw=error.headers.get('Retry-After','')
        retry=min(86400,max(1,int(raw))) if reason=='rate_limited' and re.fullmatch(r'\d{1,8}',raw) else 900 if reason=='rate_limited' else None
        raise SourceFailure(reason,retry_after=retry) from None
    except TimeoutError:
        raise SourceFailure('timeout') from None
    except (OSError,urllib.error.URLError):
        raise SourceFailure('transport_error') from None


def _object(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate JSON key')
        result[key]=value
    return result


def _complete(stamps,day):
    standard={datetime.combine(day,start,SHANGHAI)+timedelta(minutes=n)
        for start in (time(9,31),time(13,1)) for n in range(120)}
    return standard.issubset(stamps)


def parse_history(body,received_at,day):
    """All counts and timestamps are original provider fields, never inferred."""
    if type(day) is not date or received_at.utcoffset() is None or len(body)>2_000_000:
        raise SourceFailure('invalid_request_day')
    if day>received_at.astimezone(SHANGHAI).date(): raise SourceFailure('future_day')
    try:
        value=json.loads(body,object_pairs_hook=_object,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if not isinstance(value,dict) or type(value.get('code')) is not int or value['code']!=20000:
            raise SourceFailure('upstream_unavailable')
        raw=value.get('data')
        if not isinstance(raw,list) or len(raw)>MAX_ROWS: raise SourceFailure('schema_changed')
        if not raw: raise SourceFailure('historical_series_empty')
        rows=[];stamps=set();previous=None
        for row in raw:
            if not isinstance(row,dict) or not {'timestamp','limit_up_count','limit_down_count'}.issubset(row):
                raise SourceFailure('schema_changed')
            epoch,up,down=(row[k] for k in ('timestamp','limit_up_count','limit_down_count'))
            if type(epoch) is not int or any(type(count) is not int or not 0<=count<=100000 for count in (up,down)):
                raise SourceFailure('schema_changed')
            stamp=datetime.fromtimestamp(epoch,SHANGHAI)
            if stamp.date()!=day: raise SourceFailure('historical_response_day_mismatch')
            if stamp>received_at or not (time(9,15)<=stamp.time()<=time(11,30) or time(13)<=stamp.time()<=time(15)):
                raise SourceFailure('invalid_source_time')
            if stamp in stamps or previous is not None and stamp<=previous:
                raise SourceFailure('historical_series_order')
            stamps.add(stamp);previous=stamp
            rows.append(dict(timestamp=str(epoch),source_time=stamp.isoformat(),trade_day=day.isoformat(),
                limit_up_count=str(up),limit_down_count=str(down)))
        complete=_complete(stamps,day)
        payload=ReferencePayload(dataset=DATASET,title=NAME,source_url=REFERENCE,attribution='选股宝历史市场统计；本人本机研究',
            observed_day=day,scope=SCOPE,columns=[
                {'key':'timestamp','label':'原始Unix秒'},{'key':'source_time','label':'来源时点'},
                {'key':'trade_day','label':'来源交易日'},{'key':'limit_up_count','label':'来源涨停家数','unit':'家'},
                {'key':'limit_down_count','label':'来源跌停家数','unit':'家'}],rows=rows)
        return payload,complete
    except (ValueError,TypeError,KeyError,UnicodeError,OverflowError,OSError):
        raise SourceFailure('schema_changed') from None


def _saved(db,provider,day,cutoff):
    return db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id,
        EvidenceVersion.source_key==f'reference:breadth.history:{day}',EvidenceVersion.kind=='reference',
        EvidenceVersion.visibility=='shared',EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.provenance=='source_reported',EvidenceVersion.received_at<=cutoff,
        EvidenceVersion.payload['dataset'].astext==DATASET,
        EvidenceVersion.payload['observed_day'].astext==day.isoformat()).order_by(
        EvidenceVersion.received_at.desc(),EvidenceVersion.version_number.desc(),EvidenceVersion.id.desc()).limit(1))


def saved_series(db,day,cutoff):
    from app.market.review_store import permitted_provider
    if type(day) is not date or day>cutoff.astimezone(SHANGHAI).date(): return None
    provider=permitted_provider(db,FAMILY,cutoff)
    if provider is None or any(provider.rights.get(k)!='denied' for k in ('export','redistribute')): return None
    evidence=_saved(db,provider,day,cutoff)
    if evidence is None: return None
    raw=evidence.payload.get('rows',[])
    columns={c.get('key'):c.get('unit') for c in evidence.payload.get('columns',[]) if isinstance(c,dict)}
    if columns.get('limit_up_count')!='家' or columns.get('limit_down_count')!='家': return None
    if not isinstance(raw,list) or not 1<=len(raw)<=MAX_ROWS: return None
    points=[];previous=None;stamps=set()
    try:
        for row in raw:
            if not isinstance(row,dict) or row.get('trade_day')!=day.isoformat(): return None
            if any(not re.fullmatch(r'\d{1,10}',row.get(k) or '') for k in ('timestamp','limit_up_count','limit_down_count')): return None
            stamp=datetime.fromtimestamp(int(row['timestamp']),SHANGHAI)
            if (stamp.date()!=day or stamp>cutoff or stamp>evidence.received_at or
                stamp.isoformat()!=row.get('source_time') or previous is not None and stamp<=previous): return None
            up,down=int(row['limit_up_count']),int(row['limit_down_count'])
            if max(up,down)>100000: return None
            previous=stamp;stamps.add(stamp)
            points.append(dict(time=stamp.isoformat(),up=up,down=down))
    except (ValueError,TypeError,OverflowError,OSError): return None
    return dict(source_name=provider.name,source_family=FAMILY,scope=evidence.payload.get('scope') or SCOPE,
        trade_day=day.isoformat(),received_at=evidence.received_at.isoformat(),points=points,
        state='complete' if _complete(stamps,day) else 'partial',
        evidence_refs=[dict(evidence_id=str(evidence.id),source_id=str(provider.id),
            content_hash=evidence.content_hash,received_at=evidence.received_at.isoformat())])


def _target(db,clock,day=None):
    from app.market.review_store import available_trade_days
    if not in_window(clock): raise SourceFailure('historical_window_closed')
    local=clock.astimezone(SHANGHAI)
    available=available_trade_days(db,limit=1,end_day=day,as_of_time=clock)
    selected=day or (available[0] if available else None)
    if (type(selected) is not date or not local.date()-timedelta(days=LOOKBACK_DAYS)<=selected<=local.date() or
        available!=[selected] or selected==local.date() and local.time()<time(15,5)):
        raise SourceFailure('calendar_not_ready')
    return selected


def enqueue_breadth_archive(db,provider,config,day=None,*,request_id=None,integration_probe=None,recovery=False):
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.service import permission_reason
    clock=now(db)
    if not settings().data_collection_enabled: raise APIError(422,'collection_disabled','本机采集未启用。')
    if permission_reason(provider) or config.paused_reason: raise APIError(422,'permission_missing','请先核对历史来源的开关、权限与暂停状态。')
    owner=db.get(User,config.owner_id)
    if owner is None or not owner.enabled or owner.role!='admin': raise APIError(403,'admin_required','历史归档须由有效管理员配置。')
    try: target=_target(db,clock,day)
    except SourceFailure as error: raise APIError(422,error.reason,'历史归档仅在收盘后或次日开盘准备窗口读取已核交易日。') from None
    bound=resolve_source_credentials(db,provider.id,FAMILY,allow_disabled=integration_probe is not None,refresh=True)
    if bound.reason: raise APIError(422,bound.reason,'历史来源接入尚不可用。')
    binding=job_context(bound,provider.id,probe=integration_probe is not None)
    if integration_probe is not None and integration_probe!=binding: raise APIError(409,'configuration_changed','连接配置已变化。')
    context=dict(version=1,day=target.isoformat(),configuration_sha256=configuration_fingerprint(provider,config))
    payload={'source_id':str(provider.id),'breadth_history_context':context}
    if binding is not None: payload['integration_context']=binding
    lock(db,f'breadth.history.queue:{provider.id}:{target}')
    jobs=list(db.scalars(select(Job).where(Job.kind==JOB_KIND,Job.payload['source_id'].astext==str(provider.id),
        Job.payload['breadth_history_context']['day'].astext==target.isoformat(),
        Job.payload['breadth_history_context']['configuration_sha256'].astext==context['configuration_sha256']).order_by(Job.created_at.desc())))
    for pending in jobs:
        if pending.state in {'queued','running'}:
            if pending.payload!=payload: raise APIError(409,'configuration_changed','同日已有其他配置的历史归档。')
            return pending
    series=saved_series(db,target,clock)
    if series and series['state']=='complete':
        if jobs: return jobs[0]
        job=Job(owner_id=owner.id,kind=JOB_KIND,state='succeeded',payload=payload,
            completed_at=clock,result_ref={'already_saved':True,'evidence_refs':series['evidence_refs']})
        db.add(job);db.flush();return job
    if len(jobs)>=MAX_ATTEMPTS and not recovery: raise APIError(429,'historical_retry_exhausted','该日历史读取已达到三次有界尝试。')
    if jobs and (jobs[0].completed_at or jobs[0].created_at)+timedelta(seconds=MINIMUM_INTERVAL)>clock:
        raise APIError(429,'source_rate_limit','该日历史读取等待下一次有限重试窗口。')
    job=Job(owner_id=owner.id,kind=JOB_KIND,payload=payload)
    db.add(job);db.flush()
    config.next_run_at=clock+timedelta(seconds=MINIMUM_INTERVAL)
    db.add(AuditEvent(actor_id=owner.id,action='breadth.history.queue',request_id=request_id,
        resource_type='job',resource_id=job.id,changed_field_names=['explicit_historical_date'],created_at=clock))
    return job


def schedule_breadth_archives():
    if not settings().data_collection_enabled: return
    from app.maintenance_control import admit,paused
    from app.market.minute_backfill import calendar_open
    from app.market.review_store import family_id
    with Session(engine()) as db,db.begin():
        require_instance(db)
        if paused(db): return
        admit(db);clock=now(db)
        provider=db.get(Provider,family_id(FAMILY));config=db.get(SourceSettings,provider.id) if provider else None
        if not provider or not config or config.next_run_at>clock: return
        if not in_window(clock): config.next_run_at=next_window(clock);return
        try:
            for exchange in ('SSE','SZSE'):
                calendar_open(db,clock.astimezone(SHANGHAI).date(),exchange,clock)
            target=_target(db,clock)
        except SourceFailure:
            config.next_run_at=clock+timedelta(seconds=MINIMUM_INTERVAL);return
        series=saved_series(db,target,clock)
        if series and series['state']=='complete':
            local=clock.astimezone(SHANGHAI)
            config.next_run_at=(datetime.combine(local.date(),time(15,5),SHANGHAI) if local.time()<time(9,15)
                else datetime.combine(local.date()+timedelta(days=1),time(9),SHANGHAI))
            return
        try: enqueue_breadth_archive(db,provider,config,target)
        except APIError as error:
            if error.code not in {'permission_missing','admin_required','credentials_missing','source_disabled',
                'configuration_changed','source_rate_limit','historical_retry_exhausted'}: raise
            config.next_run_at=clock+timedelta(seconds=MINIMUM_INTERVAL)


def _validate(db,job,clock):
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.review_store import family_id
    from app.market.service import permission_reason
    context=job.payload.get('breadth_history_context')
    if not isinstance(context,dict) or set(context)!={'version','day','configuration_sha256'} or context.get('version')!=1:
        raise SourceFailure('breadth_history_context_invalid')
    try: day=date.fromisoformat(context['day'])
    except (TypeError,ValueError,KeyError): raise SourceFailure('breadth_history_context_invalid') from None
    _target(db,clock,day)
    if job.kind!=JOB_KIND or job.payload.get('source_id')!=str(family_id(FAMILY)): raise SourceFailure('configuration_changed')
    provider=db.get(Provider,family_id(FAMILY));config=db.get(SourceSettings,provider.id) if provider else None
    owner=db.get(User,job.owner_id)
    if (not provider or not config or permission_reason(provider) or config.paused_reason or
        not owner or not owner.enabled or owner.role!='admin'): raise SourceFailure('permission_missing')
    if config.owner_id!=job.owner_id or configuration_fingerprint(provider,config)!=context['configuration_sha256']:
        raise SourceFailure('configuration_changed')
    bound=assert_source_binding(db,provider.id,FAMILY,job.payload)
    return provider,config,day,bound


def _reserve(job):
    from app.maintenance_control import admit,paused
    with Session(engine()) as db,db.begin():
        lock(db,BUDGET_ACTION);clock=now(db)
        current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',Job.lease_token==job.lease_token,
            Job.lease_until>clock).with_for_update())
        if not current: raise SourceFailure('lease_expired')
        if current.payload!=job.payload or current.owner_id!=job.owner_id: raise SourceFailure('configuration_changed')
        if paused(db): raise SourceFailure('maintenance_paused')
        admit(db);_validate(db,current,clock)
        midnight=datetime.combine(clock.astimezone(SHANGHAI).date(),time.min,SHANGHAI)
        count=db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==BUDGET_ACTION,AuditEvent.created_at>=midnight))
        last=db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action==BUDGET_ACTION))
        if count>=DAILY_LIMIT: raise SourceFailure('rate_limited',retry_after=3600)
        if last and last+timedelta(seconds=MINIMUM_INTERVAL)>clock:
            raise SourceFailure('rate_limited',retry_after=max(1,int((last+timedelta(seconds=MINIMUM_INTERVAL)-clock).total_seconds())+1))
        cooldown=db.scalar(select(func.max(DataFetchRun.retry_not_before)).where(
            DataFetchRun.source_id==uuid.UUID(current.payload['source_id']),DataFetchRun.retry_not_before>clock))
        if cooldown: raise SourceFailure('rate_limited',retry_after=max(1,int((cooldown-clock).total_seconds())+1))
        db.add(AuditEvent(actor_id=current.owner_id,action=BUDGET_ACTION,resource_type='job',resource_id=current.id,
            changed_field_names=['historical_request_permit'],created_at=clock))


def _defer(job,reason,deadline):
    with Session(engine()) as db,db.begin():
        current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',Job.lease_token==job.lease_token,
            Job.lease_until>func.now()).with_for_update())
        if not current: return
        current.state='queued';current.available_at=deadline;current.attempts=max(0,current.attempts-1)
        current.error_code=reason;current.lease_token=current.lease_until=None


def process_job(job):
    from app.maintenance_control import admit,paused
    from app.market.collect import store_reference_batch
    from app.market.reference import ReferenceBatch
    from app.worker import finish
    from app.market.review_store import family_id
    if not settings().data_collection_enabled:
        finish(job.id,job.lease_token,error_code='collection_disabled');return
    started=datetime.now(UTC);response=None;reason=None;payload=None;complete=False;requests=0;retry_after=None
    try:
        with Session(engine()) as db,db.begin():
            if paused(db): raise SourceFailure('maintenance_paused')
            admit(db);provider,config,day,bound=_validate(db,job,now(db))
        _reserve(job)
        with Session(engine()) as db,db.begin():
            if paused(db): raise SourceFailure('maintenance_paused')
            admit(db);provider,config,day,again=_validate(db,job,now(db))
            assert_source_binding(db,provider.id,FAMILY,job.payload,frozen=bound)
        requests=1;response=fetch_history(day)
        payload,complete=parse_history(response.body,response.received_at,day)
    except (SourceFailure,APIError) as error:
        reason=error.reason if isinstance(error,SourceFailure) else 'maintenance_paused' if error.code=='maintenance_active' else error.code
        retry_after=min(86400,max(900,getattr(error,'retry_after',None) or 900)) if reason=='rate_limited' else None
        if not requests and reason in {'rate_limited','maintenance_paused','historical_window_closed'}:
            with Session(engine()) as db: clock=now(db)
            deadline=next_window(clock) if reason=='historical_window_closed' else clock+timedelta(seconds=retry_after or 60)
            _defer(job,reason,deadline);return
    received=response.received_at if response else datetime.now(UTC)
    with Session(engine()) as db,db.begin():
        current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',Job.lease_token==job.lease_token,
            Job.lease_until>func.now()).with_for_update())
        if current is None:return
        accepted=duplicates=0
        if current.payload!=job.payload or current.owner_id!=job.owner_id:reason='configuration_changed'
        if reason is None:
            try:
                if paused(db):raise SourceFailure('maintenance_paused')
                admit(db);provider,config,day,again=_validate(db,current,now(db))
                assert_source_binding(db,provider.id,FAMILY,current.payload,frozen=bound)
            except (SourceFailure,APIError) as error:
                reason=error.reason if isinstance(error,SourceFailure) else 'maintenance_paused' if error.code=='maintenance_active' else error.code
        if reason is None:
            accepted,duplicates=store_reference_batch(db,provider,config,
                ReferenceBatch(records=[(f'breadth.history:{day}',payload,None)],expected=1,requests=1),received)
        db.add(DataFetchRun(source_id=family_id(FAMILY),job_id=current.id,capability=CAPABILITY,
            started_at=started,received_at=received,status='error' if reason else 'complete' if complete else 'partial',
            reason=reason or (None if complete else 'historical_minutes_incomplete'),expected=1,accepted=accepted,
            duplicates=duplicates,rejected=0,elapsed_ms=response.elapsed_ms if response else None,request_count=requests,cost_cny=None,
            retry_not_before=received+timedelta(seconds=retry_after) if retry_after else None,
            body_hash=hashlib.sha256(response.body).hexdigest() if response else None))
        current.state='failed' if reason else 'succeeded';current.error_code=reason;current.completed_at=now(db)
        current.lease_token=current.lease_until=None
        current.result_ref=dict(historical_archive=True,points=len(payload.rows) if payload else 0,
            complete=complete,source_time=payload.rows[-1]['source_time'] if payload else None,
            received_at=received.isoformat())
