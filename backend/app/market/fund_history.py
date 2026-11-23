"""Bounded fund-minute recovery from an already saved source universe.

The minute-series response must carry the requested date itself. Each symbol is
an independent persisted job; a partial cohort cannot replace a full ranking.
"""
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid
from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.auth import APIError
from app.db import engine
from app.evidence.common import lock, now
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import ReferencePayload
from app.integrations.credentials import assert_source_binding, job_context, resolve_source_credentials
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import (SHANGHAI, SourceFailure, SourceRedirect, fetch_https,
    eastmoney_minute_allocation_url, fetch_eastmoney_minute_allocation)
from app.market.redirect_diagnostic import redirect_diagnostic
from app.models import AuditEvent, Job, Provider, User

FAMILY = 'eastmoney_funds'
CAPABILITY = 'funds.minute_history'
HISTORY_DATASET = 'market.eastmoney.funds.minute_history'
PLAN_DATASET = 'market.eastmoney.funds.minute_scope'
SOURCE_DATASETS = {'market.eastmoney.concept_funds': 'concept',
    'market.eastmoney.industry_funds': 'industry', 'market.eastmoney.stock_funds': 'stock'}
ACTION = 'funds.minute.schedule'
INTERVAL = timedelta(minutes=15)
MAX_TARGETS = 200
MAX_ATTEMPTS = 3
TRANSPORT_FAILURES=frozenset({'transport_error','timeout','upstream_unavailable'})
TRANSPORT_BACKOFF_SECONDS = 900
SCOPE = ('东方财富交易日资金分钟回补与盘后归档；仅原已保存样本范围。'
    '金额为元，源分钟与实际补采接收时间分别保留，不补点、不插值。')


class FundHistoryTimeMismatch(SourceFailure):
    """Controlled metadata for a parsed stamp rejected by the date/receipt fence."""
    def __init__(self, requested_day: date, source_time: datetime, received_at: datetime):
        reason = ('historical_response_day_mismatch' if source_time.date() != requested_day
            else 'historical_response_time_ahead')
        super().__init__(reason)
        self.diagnostic = dict(reason=reason, requested_day=requested_day.isoformat(),
            source_time=source_time.isoformat(), received_at=received_at.isoformat())


def minute_slots(day):
    return {datetime.combine(day, start, SHANGHAI)+timedelta(minutes=n)
        for start in (time(9,31),time(13,1)) for n in range(120)}


def in_window(clock):
    return time(15,5) <= clock.astimezone(SHANGHAI).time() < time(21,30)


def in_recovery_window(clock):
    from app.market.realtime_schedule import COLLECTION_END
    local=clock.astimezone(SHANGHAI).time()
    return time(9,15)<=local<COLLECTION_END


def _trading_seconds(start,end):
    """Count a real acquisition gap without counting the scheduled lunch break."""
    day=end.astimezone(SHANGHAI).date()
    return sum(max(0,(min(end,datetime.combine(day,last,SHANGHAI))-
        max(start,datetime.combine(day,first,SHANGHAI))).total_seconds())
        for first,last in ((time(9,30),time(11,30)),(time(13),time(15))))


def _observed_runs(db,provider,day,clock):
    return select(DataFetchRun).join(Job,Job.id==DataFetchRun.job_id).where(
        DataFetchRun.source_id==provider.id,DataFetchRun.capability!=CAPABILITY,
        DataFetchRun.status.in_(['complete','partial']),DataFetchRun.request_count>0,
        func.coalesce(DataFetchRun.accepted,0)+func.coalesce(DataFetchRun.duplicates,0)>0,
        DataFetchRun.received_at>=datetime.combine(day,time(9,30),SHANGHAI),
        DataFetchRun.received_at<=clock,Job.kind=='source.collect',
        Job.payload['source_id'].astext==str(provider.id))


def _recovery_intent(db,provider,config,day,clock):
    """Persist a detected gap via job contexts, even after live polling resumes."""
    runs=list(db.scalars(_observed_runs(db,provider,day,clock).order_by(
        DataFetchRun.received_at.desc(),DataFetchRun.id.desc()).limit(2)))
    threshold=max(180,2*config.interval_seconds)
    detected=None
    if runs and _trading_seconds(runs[0].received_at,clock)>=threshold:
        detected=(runs[0].id,clock.replace(second=0,microsecond=0)-timedelta(minutes=1))
    elif len(runs)==2 and _trading_seconds(runs[1].received_at,runs[0].received_at)>=threshold:
        detected=(runs[1].id,runs[0].received_at.astimezone(SHANGHAI).replace(second=0,microsecond=0)-timedelta(minutes=1))
    previous=db.scalar(select(Job).where(Job.kind=='funds.archive',Job.owner_id==config.owner_id,
        Job.payload['source_id'].astext==str(provider.id),
        Job.payload['funds_history_context']['day'].astext==day.isoformat(),
        Job.payload['funds_history_context']['mode'].astext=='intraday_recovery').order_by(
        Job.created_at.desc(),Job.id.desc()).limit(1))
    if previous:
        context=previous.payload['funds_history_context']
        try:
            until=datetime.fromisoformat(context['recovery_until'])
            anchor=uuid.UUID(context['recovery_anchor'])
            if until.utcoffset() is not None and until.date()==day and until<=clock and (
                    detected is None or until>=detected[1]):
                return anchor,until
        except (KeyError,ValueError,TypeError):
            pass
    return detected


def _needed_recovery_slots(context):
    until=datetime.fromisoformat(context['recovery_until'])
    return {stamp for stamp in minute_slots(date.fromisoformat(context['day'])) if stamp<=until}


def _next_recovery_window(clock):
    local=clock.astimezone(SHANGHAI)
    for opening in (time(9,30),time(13),time(15,5)):
        if local.time()<opening:
            return datetime.combine(local.date(),opening,SHANGHAI)
    return clock+timedelta(seconds=60)


def _number(value):
    if not isinstance(value,str) or not re.fullmatch(r'-?\d{1,16}(?:\.\d{1,12})?',value):
        raise SourceFailure('schema_changed')
    try:
        number=Decimal(value)
        if not number.is_finite(): raise InvalidOperation()
    except InvalidOperation:
        raise SourceFailure('schema_changed') from None
    return value


def _object(pairs):
    value={}
    for key,item in pairs:
        if key in value: raise ValueError('duplicate JSON key')
        value[key]=item
    return value


def _identity(category, code, exchange):
    if category in {'concept','industry'} and isinstance(code,str) and re.fullmatch(r'BK\d{4}',code):
        return 90
    if category == 'stock' and isinstance(code,str) and re.fullmatch(r'\d{6}',code):
        if exchange == 'SSE' and code.startswith(('6','900')): return 1
        if exchange == 'SZSE' and code.startswith(('0','2','3')): return 0
        if exchange == 'BSE' and code.startswith(('4','8','92')): return 0
    # Preserve the market association from the saved provider record.
    raise SourceFailure('historical_identity_unsupported')


def history_url(category, code, exchange=None):
    market=_identity(category,code,exchange)
    return 'https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?'+urlencode({
        'secid':f'{market}.{code}','klt':1,'lmt':240,'fields1':'f1,f2,f3,f7',
        'fields2':'f51,f52,f53,f54,f55,f56,f57'})


def parse_history(body, received_at, context):
    """Validate one dated identity. Return a payload and its actual completeness."""
    payload, complete, _ = _parse_history(body, received_at, context, allow_future_tail=False)
    return payload, complete


def parse_elapsed_history(body, received_at, context):
    """For an active selection, withhold future rows after validating the whole body."""
    return _parse_history(body, received_at, context, allow_future_tail=True)


def _parse_history(body, received_at, context, *, allow_future_tail):
    try:
        day=date.fromisoformat(context['day'])
        market=_identity(context['category'],context['code'],context.get('exchange'))
        if (type(context.get('cohort_count')) is not int or not 1<=context['cohort_count']<=MAX_TARGETS or
            not isinstance(context.get('cohort_sha256'),str) or
            not re.fullmatch(r'[a-f0-9]{64}',context['cohort_sha256'])):
            raise SourceFailure('funds_history_context_invalid')
    except (ValueError,TypeError,KeyError):
        raise SourceFailure('funds_history_context_invalid') from None
    if len(body)>2_000_000 or received_at.utcoffset() is None or day>received_at.astimezone(SHANGHAI).date():
        raise SourceFailure('invalid_source_time')
    try:
        value=json.loads(body,parse_float=Decimal,object_pairs_hook=_object,
            parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        data=value.get('data')
        if type(value.get('rc')) is not int or value['rc']!=0 or not isinstance(data,dict):
            raise SourceFailure('upstream_unavailable')
        if data.get('code')!=context['code'] or type(data.get('market')) is not int or data['market']!=market:
            raise SourceFailure('instrument_mismatch')
        name=data.get('name')
        if not isinstance(name,str) or not name.strip() or len(name)>200 or any(ord(c)<32 or c in '<>' for c in name):
            raise SourceFailure('schema_changed')
        lines=data.get('klines')
        if not isinstance(lines,list) or len(lines)>242:
            raise SourceFailure('schema_changed')
        if not lines: raise SourceFailure('historical_series_empty')
        rows=[];seen=set();elapsed=set();previous=None;future=None;future_count=0
        for line in lines:
            if not isinstance(line,str) or len(line)>500:
                raise SourceFailure('schema_changed')
            fields=line.split(',')
            if len(fields)!=6 or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}',fields[0]):
                raise SourceFailure('schema_changed')
            stamp=datetime.strptime(fields[0],'%Y-%m-%d %H:%M').replace(tzinfo=SHANGHAI)
            if stamp.date()!=day or (stamp>received_at and not allow_future_tail):
                raise FundHistoryTimeMismatch(day, stamp, received_at)
            if not (time(9,30)<=stamp.time()<=time(11,30) or time(13)<=stamp.time()<=time(15)):
                raise SourceFailure('invalid_source_time')
            if stamp in seen or previous is not None and stamp<=previous:
                raise SourceFailure('historical_series_order')
            seen.add(stamp);previous=stamp
            main,small,medium,large,super_large=map(_number,fields[1:])
            if stamp>received_at:
                # Validate identity, date, order, session and amounts even in
                # withheld rows. A malformed tail cannot launder a good prefix.
                future=future or FundHistoryTimeMismatch(day, stamp, received_at)
                future_count+=1
                continue
            elapsed.add(stamp)
            rows.append(dict(code=context['code'],name=name,exchange=context.get('exchange'),
                category=context['category'],trade_day=day.isoformat(),source_time=stamp.isoformat(),
                net_inflow_cny=main,small_net_cny=small,medium_net_cny=medium,
                large_net_cny=large,super_large_net_cny=super_large,
                cohort_sha256=context['cohort_sha256'],cohort_count=str(context['cohort_count'])))
        if future is not None and not rows:
            raise future
        complete=minute_slots(day).issubset(elapsed)
        for row in rows: row['archive_complete']='true' if complete else 'false'
        payload=ReferencePayload(dataset=HISTORY_DATASET,title='资金分钟历史 · '+name,
            source_url='https://data.eastmoney.com/bkzj/',attribution='东方财富资金分钟历史；个人本机研究',
            observed_day=day,scope=SCOPE,columns=[
                {'key':'code','label':'原代码'},{'key':'name','label':'来源名称'},
                {'key':'exchange','label':'交易所'},{'key':'category','label':'样本类别'},
                {'key':'trade_day','label':'来源交易日'},{'key':'source_time','label':'来源分钟'},
                *[{'key':key,'label':label,'unit':'CNY'} for key,label in (
                    ('net_inflow_cny','主力净流入'),('small_net_cny','小单净流入'),
                    ('medium_net_cny','中单净流入'),('large_net_cny','大单净流入'),('super_large_net_cny','超大单净流入'))],
                {'key':'cohort_sha256','label':'原样本范围指纹'},{'key':'cohort_count','label':'原样本数'},
                {'key':'archive_complete','label':'240个标准源分钟完整'}],rows=rows)
        rejected=(dict(future.diagnostic,retained_minutes=len(rows),excluded_future_minutes=future_count)
                  if future is not None else None)
        return payload,complete,rejected
    except (ValueError,TypeError,KeyError,UnicodeError,OverflowError):
        raise SourceFailure('schema_changed') from None


def _evidence_query(provider,day,cutoff,dataset):
    from app.market import read_facets
    return read_facets.with_facets(select(EvidenceVersion)).where(EvidenceVersion.source_id==provider.id,
        EvidenceVersion.kind=='reference',EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.visibility=='shared',EvidenceVersion.provenance=='source_reported',
        EvidenceVersion.received_at<=cutoff,read_facets.observed_day()==day.isoformat(),
        read_facets.value('dataset')==dataset)


def saved_archives(db, provider, day, cutoff):
    """Return three receipt-bounded cohorts; validate normalized saved rows again."""
    plans={};candidates=[];roots={}
    query=_evidence_query(provider,day,cutoff,PLAN_DATASET).order_by(
        EvidenceVersion.received_at.desc(),EvidenceVersion.version_number.desc(),EvidenceVersion.id.desc()).execution_options(yield_per=64)
    for evidence in db.scalars(query):
        rows=evidence.payload.get('rows',[])
        if not rows or len(rows)>MAX_TARGETS: continue
        category=rows[0].get('category')
        if category not in {'concept','industry','stock'}: continue
        targets={r.get('code'):r for r in rows if isinstance(r,dict)}
        if len(targets)!=len(rows) or any(r.get('category')!=category for r in rows): continue
        digest=rows[0].get('cohort_sha256')
        if not isinstance(digest,str) or not re.fullmatch(r'[a-f0-9]{64}',digest): continue
        candidate=dict(evidence=evidence,category=category,targets=targets,cohort_sha256=digest,series={})
        candidates.append(candidate)
        if not rows[0].get('scope_root_id') and category not in roots:
            roots[category]=candidate
    known={category:set(plan['targets']) for category,plan in roots.items()}
    for category,plan in roots.items():plans[category,plan['cohort_sha256']]=plan
    for plan in candidates:
        root=roots.get(plan['category']);rows=list(plan['targets'].values())
        if not root or any(row.get('scope_root_id')!=str(root['evidence'].id) for row in rows):continue
        if known[plan['category']] & plan['targets'].keys():continue
        known[plan['category']].update(plan['targets'])
        plans[plan['category'],plan['cohort_sha256']]=plan
    if not plans: return []
    required_slots = minute_slots(day)
    trade_day = day.isoformat()
    from functools import lru_cache
    parse_clock = lru_cache(maxsize=1024)(datetime.fromisoformat)
    query=_evidence_query(provider,day,cutoff,HISTORY_DATASET).distinct(EvidenceVersion.source_key).order_by(
        EvidenceVersion.source_key,EvidenceVersion.received_at.desc(),EvidenceVersion.version_number.desc(),
        EvidenceVersion.id.desc()).execution_options(yield_per=64)
    for evidence in db.scalars(query):
        rows=evidence.payload.get('rows',[])
        if not isinstance(rows,list) or not 1<=len(rows)<=242: continue
        head=rows[0];category=head.get('category');plan=plans.get((category,head.get('cohort_sha256')))
        code=head.get('code')
        if not plan or code not in plan['targets'] or head.get('cohort_sha256')!=plan['cohort_sha256']: continue
        if code in plan['series']: continue
        columns={c.get('key'):c.get('unit') for c in evidence.payload.get('columns',[]) if isinstance(c,dict)}
        if columns.get('net_inflow_cny')!='CNY': continue
        valid=True;seen=set()
        for row in rows:
            if (not isinstance(row,dict) or row.get('code')!=code or row.get('category')!=category
                or row.get('trade_day')!=trade_day or row.get('cohort_sha256')!=plan['cohort_sha256']
                or row.get('cohort_count')!=str(len(plan['targets']))): valid=False;break
            try:
                stamp=parse_clock(row['source_time'])
                _number(row['net_inflow_cny'])
                if stamp.utcoffset() is None or stamp.date()!=day or stamp>cutoff or stamp>evidence.received_at or stamp in seen:
                    valid=False;break
                seen.add(stamp)
            except (ValueError,KeyError,TypeError,SourceFailure): valid=False;break
        if valid:
            plan['series'][code]=(evidence,rows,required_slots.issubset(seen))
    for plan in plans.values():
        plan['complete']=(len(plan['series'])==len(plan['targets']) and
            all(complete for _,_,complete in plan['series'].values()))
    return list(plans.values())


def _plan(db,provider,config,category,day,clock):
    """Keep the original cohort; append bounded, linked real board shards."""
    existing=db.scalar(_evidence_query(provider,day,clock,PLAN_DATASET).where(
        EvidenceVersion.source_key==f'reference:funds.history.scope:{day}:{category}').order_by(
        EvidenceVersion.received_at.desc(),EvidenceVersion.version_number.desc()).limit(1))
    if existing is not None and category=='stock': return existing
    dataset=next(key for key,value in SOURCE_DATASETS.items() if value==category)
    evidence=db.scalar(_evidence_query(provider,day,clock,dataset).order_by(
        EvidenceVersion.received_at.desc(),EvidenceVersion.version_number.desc(),EvidenceVersion.id.desc()).limit(1))
    if evidence is None: return existing
    raw=evidence.payload.get('rows',[])
    from app.market.fund_universe import saved_capture
    capture=saved_capture(evidence.payload,evidence.received_at)
    if existing is not None and capture is None:return existing
    if not isinstance(raw,list) or not 1<=len(raw)<= (5000 if capture else MAX_TARGETS): return existing
    known=set()
    if existing is not None:
        known.update(row['code'] for row in existing.payload['rows'])
        for prior in db.scalars(_evidence_query(provider,day,clock,PLAN_DATASET)):
            rows=prior.payload.get('rows',[])
            if rows and all(row.get('scope_root_id')==str(existing.id) and row.get('category')==category for row in rows):
                known.update(row['code'] for row in rows)
    rows=[];seen=set()
    for row in raw:
        if not isinstance(row,dict) or row.get('trade_day')!=day.isoformat(): return None
        code=row.get('code');exchange=row.get('exchange')
        try: _identity(category,code,exchange)
        except SourceFailure: continue
        if code in seen: return None
        seen.add(code)
        if code in known:continue
        rows.append(dict(category=category,code=code,exchange=exchange,name=row.get('name') or code,
            base_evidence_id=str(evidence.id),base_content_hash=evidence.content_hash))
    if not rows: return existing
    rows.sort(key=lambda r:r['code'])
    from app.market.collect import store_reference_batch
    from app.market.reference import ReferenceBatch
    root=existing
    for offset in range(0,len(rows),MAX_TARGETS):
        chunk=rows[offset:offset+MAX_TARGETS]
        digest=hashlib.sha256(json.dumps([day.isoformat(),category,
            [(r['code'],r['exchange']) for r in chunk]],separators=(',',':')).encode()).hexdigest()
        for row in chunk:
            row['cohort_sha256']=digest
            if root is not None:row['scope_root_id']=str(root.id)
        payload=ReferencePayload(dataset=PLAN_DATASET,title='资金分钟原样本范围分片',
            source_url='https://data.eastmoney.com/bkzj/',attribution='东方财富实际保存批次的归档范围',
            observed_day=day,scope='仅冻结本日已实际收到的代码；每片最多200，不改原范围、旧任务或原归档。未收到的目录代码不能补造。',
            columns=[{'key':key,'label':key} for key in chunk[0]],rows=chunk)
        key=f'funds.history.scope:{day}:{category}'+(f':part:{digest}' if root is not None else '')
        store_reference_batch(db,provider,config,ReferenceBatch(records=[(key,payload,None)],expected=1),clock)
        if root is None:
            root=db.scalar(_evidence_query(provider,day,clock,PLAN_DATASET).where(
                EvidenceVersion.source_key=='reference:'+key).order_by(EvidenceVersion.version_number.desc()).limit(1))
    return root


def schedule_fund_archives():
    if not settings().data_collection_enabled: return
    from app.auth import require_instance
    from app.maintenance_control import admit,paused
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.review_store import available_trade_days,family_id
    from app.market.service import permission_reason
    with Session(engine()) as db,db.begin():
        require_instance(db)
        if paused(db): return
        admit(db);clock=now(db)
        intraday=in_recovery_window(clock)
        if not in_window(clock) and not intraday: return
        day=clock.astimezone(SHANGHAI).date()
        provider=db.get(Provider,family_id(FAMILY))
        config=db.get(SourceSettings,provider.id) if provider else None
        owner=db.get(User,config.owner_id) if config else None
        if not provider or not config or not owner or not owner.enabled or owner.role!='admin': return
        if permission_reason(provider) or config.paused_reason: return
        intent=None
        if intraday:
            from app.market.minute_backfill import calendar_open
            try:
                for exchange in ('SSE','SZSE'):calendar_open(db,day,exchange,clock)
            except SourceFailure:return
            intent=_recovery_intent(db,provider,config,day,clock)
            if intent is None:return
        elif available_trade_days(db,limit=1,as_of_time=clock)!=[day]:return
        lock(db,ACTION)
        last_query=select(func.max(AuditEvent.created_at)).where(AuditEvent.action==ACTION)
        if intent:
            last_query=last_query.where(AuditEvent.resource_type=='funds_gap',AuditEvent.resource_id==intent[0])
        else:
            last_query=last_query.where(AuditEvent.resource_type=='provider',AuditEvent.resource_id==provider.id)
        last=db.scalar(last_query)
        if last is not None and last+INTERVAL>clock:return
        bound=resolve_source_credentials(db,provider.id,FAMILY,refresh=True)
        if bound.reason: return
        configuration=configuration_fingerprint(provider,config)
        db.add(AuditEvent(action=ACTION,resource_type='funds_gap' if intent else 'provider',
            resource_id=intent[0] if intent else provider.id,
            changed_field_names=['intraday_fund_gap_recovery' if intent else 'closed_day_fund_minute_archives'],created_at=clock))
        for category in ('concept','industry','stock'):
            _plan(db,provider,config,category,day,clock)
        for plan in saved_archives(db,provider,day,clock):
            for code,target in plan['targets'].items():
                saved=plan['series'].get(code)
                if saved and saved[2]: continue
                if intent and saved:
                    needed={stamp for stamp in minute_slots(day) if stamp<=intent[1]}
                    observed={datetime.fromisoformat(row['source_time']) for row in saved[1]}
                    if needed.issubset(observed):continue
                jobs=list(db.scalars(select(Job).where(Job.kind=='funds.archive',
                    Job.payload['source_id'].astext==str(provider.id),
                    Job.payload['funds_history_context']['day'].astext==day.isoformat(),
                    Job.payload['funds_history_context']['category'].astext==plan['category'],
                    Job.payload['funds_history_context']['code'].astext==code).order_by(Job.created_at.desc())))
                if any(job.state in {'queued','running'} for job in jobs):continue
                attempts=[job for job in jobs if job.payload['funds_history_context'].get('configuration_sha256')==configuration
                    and (job.payload['funds_history_context'].get('recovery_anchor')==str(intent[0])
                    if intent else job.payload['funds_history_context'].get('mode')!='intraday_recovery')]
                failures=[job.id for job in attempts if job.state=='failed' and
                    (job.completed_at or job.created_at)>=clock-INTERVAL*MAX_ATTEMPTS]
                if failures and db.scalar(select(func.count()).select_from(DataFetchRun).where(
                    DataFetchRun.job_id.in_(failures),DataFetchRun.request_count>0,DataFetchRun.status=='error'))>=MAX_ATTEMPTS:continue
                prior=attempts[0] if attempts else None
                if jobs and ((jobs[0].completed_at or jobs[0].created_at)+INTERVAL>clock or
                    prior is not None and prior.error_code in {'schema_changed','permission_missing','configuration_changed',
                        'instrument_mismatch','unsafe_upstream_response'} or
                    prior is not None and prior.error_code=='upstream_redirect' and
                        not (prior.result_ref or {}).get('last_transport_rejection')): continue
                context=dict(version=1,day=day.isoformat(),category=plan['category'],code=code,
                    exchange=target.get('exchange'),cohort_sha256=plan['cohort_sha256'],cohort_count=len(plan['targets']),
                    plan_id=str(plan['evidence'].id),configuration_sha256=configuration)
                if intent:
                    context.update(mode='intraday_recovery',recovery_anchor=str(intent[0]),recovery_until=intent[1].isoformat())
                payload={'source_id':str(provider.id),'funds_history_context':context}
                if binding:=job_context(bound,provider.id): payload['integration_context']=binding
                diagnostic=(prior.result_ref or {}).get('last_transport_rejection') if prior else None
                db.add(Job(owner_id=owner.id,kind='funds.archive',payload=payload,
                    result_ref={'last_transport_rejection':diagnostic} if diagnostic else None))


def _validate(db,job,clock,*,require_window=True):
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.review_store import available_trade_days,family_id
    from app.market.service import permission_reason
    context=job.payload.get('funds_history_context')
    if not isinstance(context,dict) or context.get('version')!=1:
        raise SourceFailure('funds_history_context_invalid')
    try: day=date.fromisoformat(context['day'])
    except (ValueError,KeyError,TypeError): raise SourceFailure('funds_history_context_invalid') from None
    if day!=clock.astimezone(SHANGHAI).date(): raise SourceFailure('historical_window_expired')
    recovery=context.get('mode')=='intraday_recovery'
    if context.get('mode') not in {None,'intraday_recovery'}:raise SourceFailure('funds_history_context_invalid')
    if require_window and not (in_window(clock) or recovery and in_recovery_window(clock)):
        raise SourceFailure('historical_window_closed')
    if recovery:
        from app.market.minute_backfill import calendar_open
        for exchange in ('SSE','SZSE'):calendar_open(db,day,exchange,clock)
    elif available_trade_days(db,limit=1,as_of_time=clock)!=[day]:raise SourceFailure('calendar_not_ready')
    if job.payload.get('source_id')!=str(family_id(FAMILY)): raise SourceFailure('configuration_changed')
    provider=db.get(Provider,family_id(FAMILY));config=db.get(SourceSettings,provider.id) if provider else None
    if not provider or not config or permission_reason(provider) or config.paused_reason:
        raise SourceFailure('permission_missing')
    if job.owner_id!=config.owner_id or configuration_fingerprint(provider,config)!=context.get('configuration_sha256'):
        raise SourceFailure('configuration_changed')
    owner=db.get(User,job.owner_id)
    if owner is None or not owner.enabled or owner.role!='admin':
        raise SourceFailure('permission_missing')
    if recovery:
        try:
            until=datetime.fromisoformat(context['recovery_until'])
            anchor=db.get(DataFetchRun,uuid.UUID(context['recovery_anchor']))
            original=db.get(Job,anchor.job_id) if anchor else None
            if (until.utcoffset() is None or until.date()!=day or until>clock or not _needed_recovery_slots(context)
                or not anchor or anchor.source_id!=provider.id or anchor.capability==CAPABILITY
                or anchor.status not in {'complete','partial'} or anchor.request_count<=0
                or (anchor.accepted or 0)+(anchor.duplicates or 0)<=0
                or anchor.received_at.astimezone(SHANGHAI).date()!=day or anchor.received_at>until
                or not original or original.kind!='source.collect'
                or original.payload.get('source_id')!=str(provider.id)):
                raise SourceFailure('funds_history_context_invalid')
        except (ValueError,KeyError,TypeError):raise SourceFailure('funds_history_context_invalid') from None
    try: plan=db.get(EvidenceVersion,uuid.UUID(context['plan_id']))
    except (ValueError,KeyError,TypeError): raise SourceFailure('funds_history_context_invalid') from None
    if (not plan or plan.source_id!=provider.id or plan.payload.get('dataset')!=PLAN_DATASET or
        plan.payload.get('observed_day')!=day.isoformat() or plan.received_at>clock):
        raise SourceFailure('funds_history_context_invalid')
    targets=plan.payload.get('rows',[])
    matching=[row for row in targets if row.get('code')==context.get('code') and row.get('category')==context.get('category')]
    if (len(matching)!=1 or len(targets)!=context.get('cohort_count') or
        matching[0].get('cohort_sha256')!=context.get('cohort_sha256') or
        matching[0].get('exchange')!=context.get('exchange')):
        raise SourceFailure('funds_history_context_invalid')
    _identity(context['category'],context['code'],context.get('exchange'))
    bound=assert_source_binding(db,provider.id,FAMILY,job.payload)
    return provider,config,context,bound


def process_job(job):
    """One supplier request per claimed job, with a fenced final transaction."""
    from app.maintenance_control import admit,paused
    from app.market.budget import reserve_eastmoney_request
    from app.market.collect import store_reference_batch
    from app.market.reference import ReferenceBatch
    from app.worker import finish
    if not settings().data_collection_enabled:
        finish(job.id,job.lease_token,error_code='collection_disabled');return
    started=datetime.now(UTC);response=None;reason=None;payload=None;complete=False;requests=0;retry_after=None;checked_clock=None
    last_transport_rejection=(job.result_ref or {}).get('last_transport_rejection')
    request_url=None
    try:
        with Session(engine()) as db,db.begin():
            if paused(db): raise SourceFailure('maintenance_paused')
            admit(db);checked_clock=now(db);provider,config,context,bound=_validate(db,job,checked_clock)
        reserve_eastmoney_request(job,minimum_interval=10)
        with Session(engine()) as db,db.begin():
            current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',
                Job.lease_token==job.lease_token,Job.lease_until>func.now()))
            if current is None: raise SourceFailure('lease_expired')
            if current.payload!=job.payload or current.owner_id!=job.owner_id:
                raise SourceFailure('configuration_changed')
            if paused(db): raise SourceFailure('maintenance_paused')
            admit(db);checked_clock=now(db);provider,config,context,again=_validate(db,current,checked_clock)
            assert_source_binding(db,provider.id,FAMILY,current.payload,frozen=bound)
        requests=1
        request_url=history_url(context['category'],context['code'],context.get('exchange'))
        try:
            response=fetch_https(request_url)
        except SourceRedirect as redirect:
            previous=last_transport_rejection
            last_transport_rejection=redirect_diagnostic(request_url,redirect) or previous
            target=eastmoney_minute_allocation_url(request_url,redirect,observed_rejection=previous)
            reserve_eastmoney_request(job,minimum_interval=10)
            with Session(engine()) as db,db.begin():
                current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',
                    Job.lease_token==job.lease_token,Job.lease_until>func.now()))
                if current is None:raise SourceFailure('lease_expired')
                if current.payload!=job.payload or current.owner_id!=job.owner_id:raise SourceFailure('configuration_changed')
                if paused(db):raise SourceFailure('maintenance_paused')
                admit(db);provider,config,context,again=_validate(db,current,now(db))
                assert_source_binding(db,provider.id,FAMILY,current.payload,frozen=bound)
            requests+=1
            response=fetch_eastmoney_minute_allocation(target,original_url=request_url,observed_rejection=previous)
        payload,complete=parse_history(response.body,response.received_at,context)
        if requests==2:
            payload=payload.model_copy(update={'scope':payload.scope+' 本批在原正常重试再次实际遇到同路径同查询的HTTPS分配后，仅续接一次。'})
    except (SourceFailure,APIError) as error:
        reason=error.reason if isinstance(error,SourceFailure) else 'maintenance_paused' if error.code=='maintenance_active' else error.code
        if diagnostic:=redirect_diagnostic(request_url,error):last_transport_rejection=diagnostic
        if reason=='local_request_wait':
            retry_after=max(2,getattr(error,'retry_after',None) or 10)
        if reason=='rate_limited':
            retry_after=max(120,min(86400,getattr(error,'retry_after',None) or 300))
        recover_wait=(reason=='historical_window_closed' and job.payload.get('funds_history_context',{}).get('mode')=='intraday_recovery'
            and checked_clock is not None and checked_clock.astimezone(SHANGHAI).time()<time(21,30)
            and job.payload['funds_history_context'].get('day')==checked_clock.astimezone(SHANGHAI).date().isoformat())
        if not requests and (reason in {'rate_limited','local_request_wait','maintenance_paused'} or recover_wait):
            with Session(engine()) as db,db.begin():
                current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',
                    Job.lease_token==job.lease_token,Job.lease_until>func.now()).with_for_update())
                if current:
                    current.state='queued';current.available_at=(_next_recovery_window(now(db)) if recover_wait
                        else now(db)+timedelta(seconds=retry_after or 60))
                    current.attempts=max(0,current.attempts-1);current.error_code=reason
                    current.lease_token=current.lease_until=None
            return
    received=response.received_at if response else datetime.now(UTC)
    with Session(engine()) as db,db.begin():
        current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',
            Job.lease_token==job.lease_token,Job.lease_until>func.now()).with_for_update())
        if current is None: return
        accepted=duplicates=0
        if current.payload!=job.payload or current.owner_id!=job.owner_id:
            reason='configuration_changed'
        if reason is None:
            try:
                if paused(db): raise SourceFailure('maintenance_paused')
                admit(db);provider,config,context,again=_validate(db,current,now(db),
                    require_window=current.payload.get('funds_history_context',{}).get('mode')!='intraday_recovery')
                assert_source_binding(db,provider.id,FAMILY,current.payload,frozen=bound)
            except (SourceFailure,APIError) as error:
                reason=error.reason if isinstance(error,SourceFailure) else 'maintenance_paused' if error.code=='maintenance_active' else error.code
        if reason is None:
            key=f"funds.history:{context['day']}:{context['category']}:{context['code']}"
            accepted,duplicates=store_reference_batch(db,provider,config,
                ReferenceBatch(records=[(key,payload,None)],expected=1,requests=requests),received)
        if requests and reason in TRANSPORT_FAILURES:
            previous=list(db.scalars(select(DataFetchRun.reason).where(
                DataFetchRun.source_id==uuid.UUID(current.payload['source_id']),
                DataFetchRun.capability==CAPABILITY,DataFetchRun.request_count>0).order_by(
                DataFetchRun.received_at.desc(),DataFetchRun.id.desc()).limit(2)))
            # One bad symbol must not stop a healthy cohort. Repeated transport
            # failures cool only minute archives, not current ranking polls.
            # Actual rate_limited/Retry-After remains shared by both lanes.
            if len(previous)==2 and all(value in TRANSPORT_FAILURES for value in previous):
                retry_after=TRANSPORT_BACKOFF_SECONDS
        db.add(DataFetchRun(source_id=uuid.UUID(current.payload['source_id']),job_id=current.id,
            capability=CAPABILITY,started_at=started,received_at=received,
            status='error' if reason else 'complete' if complete else 'partial',
            reason=reason or (None if complete else 'historical_minutes_incomplete'),
            expected=1,accepted=accepted,duplicates=duplicates,rejected=0,
            elapsed_ms=response.elapsed_ms if response else None,request_count=requests,cost_cny=None,
            retry_not_before=received+timedelta(seconds=retry_after) if retry_after else None,
            body_hash=hashlib.sha256(response.body).hexdigest() if response else None))
        current.state='failed' if reason else 'succeeded';current.error_code=reason
        current.completed_at=now(db);current.lease_token=current.lease_until=None
        current.result_ref=dict(points=len(payload.rows) if payload else 0,complete=complete,
            source_time=payload.rows[-1]['source_time'] if payload else None,
            received_at=received.isoformat(),historical_archive=True)
        if last_transport_rejection is not None:
            current.result_ref['last_transport_rejection']=last_transport_rejection
