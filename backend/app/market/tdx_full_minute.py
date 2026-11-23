"""Explicit, resumable 20-stock chunks; no background whole-market scan."""
import base64
import json
from datetime import date, timedelta

from sqlalchemy import select

from app.auth import APIError
from app.evidence.common import content_hash, lock, normalized, now
from app.integrations.credentials import job_context, resolve_source_credentials
from app.market.daily_history import fingerprint
from app.market.models import InstrumentOrigin, SourceSettings
from app.market.protocol import InstrumentKey, SHANGHAI, SourceFailure
from app.models import AuditEvent, Instrument, Job, Provider

CHUNK_SIZE=20


def universe(db,config):
    if config.config.get('collection_mode','selected')=='selected':
        return sorted([InstrumentKey(**raw) for raw in config.config.get('symbols',[])],key=lambda k:k.identity)
    # A source-derived identity needs a currently permitted catalog claim; a
    # random local quote or an unverified historical name is not a stock pool.
    rows=db.scalars(select(Instrument).join(InstrumentOrigin,InstrumentOrigin.instrument_id==Instrument.id)
        .join(Provider,Provider.id==InstrumentOrigin.source_id).where(Instrument.asset_type=='stock',
            Instrument.valid_to.is_(None),InstrumentOrigin.origin_kind=='catalog',Provider.configured.is_(True),
            Provider.enabled.is_(True),Provider.entitlement_status=='verified',
            Provider.rights['display'].astext=='allowed',Provider.rights['cache'].astext=='allowed')
        .distinct().order_by(Instrument.exchange,Instrument.symbol,Instrument.id))
    return sorted({InstrumentKey(row.exchange,row.symbol,row.asset_type) for row in rows},key=lambda k:k.identity)


def encode_cursor(scope,offset,day):
    return base64.urlsafe_b64encode(json.dumps({'scope':scope,'offset':offset,'day':day.isoformat()},separators=(',',':')).encode()).decode()


def context(db,provider,config,day,cursor=None):
    today=now(db).astimezone(SHANGHAI).date()
    if day is None:
        from app.market.minute_backfill import calendar_open
        for offset in range(8):
            candidate=today-timedelta(days=offset)
            try:
                if all(calendar_open(db,candidate,exchange,now(db)) for exchange in ('SSE','SZSE')):
                    day=candidate;break
            except SourceFailure:continue
        if day is None:raise SourceFailure('calendar_not_ready')
    if not today-timedelta(days=7)<=day<=today:
        raise SourceFailure('minute_day_out_of_range')
    items=universe(db,config)
    if not items:raise SourceFailure('instrument_required')
    signature=content_hash([fingerprint(provider,config),[key.identity for key in items],day.isoformat()])
    offset=0
    if cursor:
        try:
            if len(cursor)>512:raise ValueError()
            value=json.loads(base64.urlsafe_b64decode(cursor.encode()))
            offset=value['offset']
            if value['scope']!=signature or value['day']!=day.isoformat():raise SourceFailure('configuration_changed')
            if type(offset) is not int or not 0<=offset<len(items) or offset%CHUNK_SIZE:raise ValueError()
        except (ValueError,TypeError,KeyError):raise SourceFailure('invalid_minute_cursor') from None
    return {'version':1,'configuration_sha256':fingerprint(provider,config),'scope_sha256':signature,
        'day':day.isoformat(),'mode':config.config.get('collection_mode','selected'),'offset':offset,'total':len(items),
        'symbols':normalized([dict(exchange=k.exchange,symbol=k.symbol,asset_type=k.asset_type) for k in items[offset:offset+CHUNK_SIZE]]),
        'request_limit':min(CHUNK_SIZE,len(items)-offset)}


def validate_context(db,provider,config,payload):
    value=payload.get('full_minute_context')
    try:
        if not isinstance(value,dict):raise ValueError()
        day=date.fromisoformat(value['day'])
        cursor=encode_cursor(value['scope_sha256'],value['offset'],day) if value['offset'] else None
        expected=context(db,provider,config,day,cursor)
        if value.get('probe') is True:
            expected={**expected,'symbols':expected['symbols'][:1],'request_limit':1,'probe':True}
        if value!=expected:raise SourceFailure('configuration_changed')
        return value
    except (ValueError,TypeError,KeyError):raise SourceFailure('invalid_full_minute_context') from None


def enqueue_full_minute(db,source,*,day=None,cursor=None,request_id=None,integration_probe=None):
    from app.config import settings
    from app.market.service import permission_reason
    provider=db.get(Provider,source);config=db.get(SourceSettings,source)
    if not settings().data_collection_enabled:raise APIError(422,'collection_disabled','本机采集未启用。')
    if provider is None or config is None or provider.family!='tdx_full_minute':raise APIError(404,'not_found','按需分钟来源尚未配置。')
    if config.config.get('points_enabled') is not True:raise APIError(422,'points_not_enabled','请先明确启用该能力的通达信积分使用。')
    if integration_probe is None:
        from app.integrations.routing import route_settings
        routes=route_settings(db)
        choice=(routes.choices if routes else {}).get('full_minute',{})
        if choice.get('provider_id')!='tdx_data' or choice.get('allow_points') is not True:
            raise APIError(422,'full_minute_not_selected','请先检测并选择全量分钟能力，明确启用积分。')
    if permission_reason(provider) or config.paused_reason:raise APIError(422,permission_reason(provider) or 'source_paused','请先核对来源权限或暂停原因。')
    bound=resolve_source_credentials(db,source,provider.family,allow_disabled=integration_probe is not None,refresh=True)
    if bound.reason:raise APIError(422,bound.reason,'数据服务连接未就绪，请核对数据服务Key。')
    binding=job_context(bound,source,probe=integration_probe is not None)
    if integration_probe is not None and integration_probe!=binding:raise APIError(409,'configuration_changed','检测配置已变化。')
    try:chunk=context(db,provider,config,day,cursor)
    except SourceFailure as exc:raise APIError(422,exc.reason,'分钟范围、游标或配置已变化；未发起请求。') from None
    # A probe is one selected stock, even when the configured collection mode
    # is full_market. It must never secretly start a 20-request paid chunk.
    if integration_probe is not None:
        chunk={**chunk,'symbols':chunk['symbols'][:1],'request_limit':1,'probe':True}
    lock(db,f'source:queue:{source}')
    existing=db.scalar(select(Job).where(Job.kind=='source.collect',Job.payload['source_id'].astext==str(source),
        Job.state.in_(['queued','running'])).limit(1))
    if existing:
        if existing.payload.get('full_minute_context')!=chunk or existing.payload.get('integration_context')!=binding:
            raise APIError(409,'collection_intent_conflict','该来源已有其他分钟范围或配置的任务。')
        return existing
    payload={'source_id':str(source),'full_minute_context':chunk}
    if binding is not None:payload['integration_context']=binding
    job=Job(owner_id=config.owner_id,kind='source.collect',payload=payload)
    db.add(job);db.flush()
    db.add(AuditEvent(actor_id=config.owner_id,action='source.refresh',request_id=request_id,resource_type='job',resource_id=job.id,
        changed_field_names=['queued','full_minute_context']))
    return job


def progress(value,minutes,*,db=None,current_job_id=None):
    from app.market.fuyao import code
    keys=[InstrumentKey(**item) for item in value['symbols']]
    codes=[key.identity for key in keys]
    returned={row.instrument.identity for row in minutes.records} if minutes else set()
    if minutes:
        # Some rows can be stored for an instrument while its other rows are
        # rejected. That instrument has not completed this requested chunk.
        rejected=minutes.rejected
        returned-={key.identity for key in keys if any(item==code(key) or item.startswith(code(key)+':') for item in rejected)}
    next_offset=value['offset']+len(codes)
    failed=sorted(set(codes)-returned)
    chunks={value['offset']:{'completed':returned,'failed':set(failed)}}
    if db is not None and not value.get('probe'):
        jobs=db.scalars(select(Job).where(Job.kind=='source.collect',Job.id!=current_job_id,
            Job.payload['full_minute_context']['scope_sha256'].astext==value['scope_sha256'],
            Job.payload['full_minute_context']['day'].astext==value['day'],Job.state.in_(['succeeded','failed']))
            .order_by(Job.created_at.desc(),Job.id.desc()))
        for job in jobs:
            previous=job.result_ref or {};old_context=job.payload.get('full_minute_context',{})
            offset=old_context.get('offset')
            if offset in chunks or previous.get('kind')!='tdx_full_minute' or old_context.get('probe'):continue
            chunks[offset]={'completed':set(previous.get('completed_instruments',[])),
                'failed':set(previous.get('chunk_failed_instruments',previous.get('failed_instruments',[])))}
    completed=set().union(*(chunk['completed'] for chunk in chunks.values()))
    all_failed=set().union(*(chunk['failed'] for chunk in chunks.values()))-completed
    traversed=all(offset in chunks for offset in range(0,value['total'],CHUNK_SIZE)) and not value.get('probe')
    complete=bool(traversed and len(completed)==value['total'] and not all_failed)
    return {'kind':'tdx_full_minute','mode':value['mode'],'day':value['day'],'scope_count':value['total'],
        'chunk_offset':value['offset'],'chunk_requested':len(codes),'chunk_completed':len(returned),
        'chunk_failed_count':len(failed),'failed_count':len(all_failed),'completed_count':len(completed),
        'completed_through':next_offset,'remaining':max(0,value['total']-next_offset),
        'request_limit':value['request_limit'],
        'next_cursor':encode_cursor(value['scope_sha256'],next_offset,date.fromisoformat(value['day']))
            if next_offset<value['total'] and not value.get('probe') else None,
        'completed_instruments':sorted(returned),'chunk_failed_instruments':failed,
        'returned_bars':len(minutes.records) if minutes else 0,
        'coverage_semantics':'每个目录标的至少返回一根所选日1分钟K；不证明全天240根或历史完整。',
        'failed_instruments':sorted(all_failed),'scope_traversed':bool(traversed),'scope_complete':complete,'is_complete':complete}
