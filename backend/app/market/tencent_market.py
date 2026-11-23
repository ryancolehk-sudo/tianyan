"""One bounded multi-page Tencent quote batch, with an explicit saved input scope.

The input may be a received listed-stock catalog or a recent real market batch.
Neither is relabelled as an exchange-verified current universe. No quote fields
or statistics are blended with another supplier's observation.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from collections import Counter
import hashlib
import json
import re
import uuid
import time as timer

from sqlalchemy import select

from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import ReferencePayload
from app.market.protocol import (InstrumentKey, Response, SHANGHAI, SourceFailure,
    decimal_value, fetch_https, parse_tencent_quotes, quote_url)
from app.market.reference import ReferenceBatch
from app.models import Provider

FAMILY='tencent_market_rt'
DATASET='tencent.rt_market'
MANIFEST='tencent.market_capture'
PAGE_SIZE=750
MAX_ROWS=7999
MAX_SECONDS=25
MAX_CLOCK_WAIT=15
CLOCK_POLICY='actual-observation-after-pages-v1'
SCOPE_AGE=timedelta(days=2)
PATTERN=re.compile(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ|[489]\d{5}\.BJ)')
FIELDS=('ts_code','name','close','pre_close','open','high','low','vol','amount','trade_time',
    'up_limit','down_limit','bid_price1','bid_volume1','ask_price1','ask_volume1',
    'page','capture_id')
STAT_FIELDS=('limit_up','limit_down','advancing','declining','unchanged','returned','comparable',
    'unknown','old_day','fresh','source_time_min','source_time_max','total_amount_cny')
CALCULATION='local-native-price-equality-v1'


@dataclass(frozen=True)
class Universe:
    instruments: tuple[InstrumentKey,...]
    source_id: uuid.UUID
    evidence_id: uuid.UUID
    received_at: datetime
    kind: str
    digest: str
    excluded: int=0


def universe(db, cutoff):
    from app.market.catalog_scope import read_catalog_scope
    from app.market.service import source_id, permission_reason
    from app.market.tushare_market import instrument_key
    captured=read_catalog_scope(db,'tushare_stock_basic',cutoff,max_age=SCOPE_AGE)
    if captured is not None and not captured.rejected:
        items=tuple(key for key in captured.instruments if PATTERN.fullmatch(
            key.symbol+'.'+{'SSE':'SH','SZSE':'SZ','BSE':'BJ'}[key.exchange]))
        if 0 < len(items) <= MAX_ROWS:
            return Universe(items,captured.source_id,captured.manifest_id,captured.received_at,
                'listed_catalog_capture',content_hash([key.identity for key in items]),len(captured.instruments)-len(items))
    captured=read_catalog_scope(db,'cninfo',cutoff,max_age=SCOPE_AGE)
    if captured is not None:
        items=tuple(key for key in captured.instruments if PATTERN.fullmatch(
            key.symbol+'.'+{'SSE':'SH','SZSE':'SZ','BSE':'BJ'}[key.exchange]))
        if 0 < len(items) <= MAX_ROWS:
            return Universe(items,captured.source_id,captured.manifest_id,captured.received_at,
                'public_catalog_listing_unknown',content_hash([key.identity for key in items]),
                sum(captured.rejected.values())+len(captured.instruments)-len(items))
    # Bootstrap uses an actual recent immutable quote membership, not current
    # cumulative InstrumentOrigin rows or an unverified free-directory union.
    provider=db.get(Provider,source_id('tushare_market_rt'))
    if provider is not None and not permission_reason(provider):
        evidence=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id,
            EvidenceVersion.kind=='reference',EvidenceVersion.visibility=='shared',EvidenceVersion.owner_id.is_(None),
            EvidenceVersion.payload['dataset'].astext=='tushare.rt_market',
            EvidenceVersion.received_at<=cutoff,EvidenceVersion.received_at>=cutoff-SCOPE_AGE)
            .order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(1))
        if evidence:
            codes=[row.get('ts_code') for row in evidence.payload.get('rows',[])]
            if (0 < len(codes) <= MAX_ROWS and len(set(codes))==len(codes)
                    and all(isinstance(code,str) and PATTERN.fullmatch(code) for code in codes)):
                items=tuple(sorted((instrument_key(code) for code in codes),key=lambda key:key.identity))
                return Universe(items,provider.id,evidence.id,evidence.received_at,'previous_quote_batch',
                    content_hash([key.identity for key in items]))
    raise SourceFailure('catalog_scope_unavailable')


def scope_allowed(db, scope, cutoff):
    from app.market.service import permission_reason
    provider=db.get(Provider,scope.source_id)
    wanted={'listed_catalog_capture':('tushare_stock_basic','instrument.catalog.capture'),
        'public_catalog_listing_unknown':('cninfo','instrument.catalog.capture'),
        'previous_quote_batch':('tushare_market_rt','tushare.rt_market')}.get(scope.kind)
    if not wanted or provider is None or provider.family!=wanted[0] or permission_reason(provider):return False
    evidence=db.execute(select(EvidenceVersion.source_id,EvidenceVersion.received_at).where(
        EvidenceVersion.id==scope.evidence_id,EvidenceVersion.kind=='reference',
        EvidenceVersion.visibility=='shared',EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.payload['dataset'].astext==wanted[1])).first()
    return bool(evidence and evidence.source_id==provider.id and evidence.received_at==scope.received_at
        and evidence.received_at<=cutoff and cutoff-evidence.received_at<=SCOPE_AGE)


def saved_scope_allowed(db, payload, cutoff, *, received_at=None, coverage=None):
    try:
        row=coverage if coverage is not None else manifest_for(db,payload['rows'][0]['capture_id'],cutoff,received_at=received_at)
        if row is None:return False
        scope=Universe((),uuid.UUID(row['scope_source_id']),uuid.UUID(row['scope_evidence_id']),
            datetime.fromisoformat(row['scope_received_at']),row['scope_kind'],row['universe_hash'])
        return scope_allowed(db,scope,min(cutoff,received_at or cutoff))
    except (KeyError,TypeError,ValueError,IndexError):
        return False


def parse_page(response, selection):
    batch=parse_tencent_quotes(response.body,list(selection),response.received_at,maximum=PAGE_SIZE)
    cells={match[1]:match[2].split('~') for line in response.body.decode('gb18030').splitlines()
        if (match:=re.fullmatch(r'\s*v_((?:sh|sz|bj)\d{6})="([^"\r\n]*)";?\s*',line))}
    result=[]
    text=lambda number:format(number,'f') if number is not None else None
    for quote in batch.records:
        key=quote.instrument;raw=cells[key.vendor_key]
        # The native parser bounds clock lead to 60s. A short local regression
        # may settle before publication; fetch never publishes a future tick.
        try:
            if quote.amount_cny is not None and quote.amount_cny<0:
                raise SourceFailure('invalid_number')
            def number(index):
                value=decimal_value(raw[index]) if index<len(raw) else None
                if value is not None and value<0:raise SourceFailure('invalid_number')
                return value
            # This observed pair provides no usable price bounds. Keep the
            # valid quote with unknown limits; never infer an unlimited regime.
            if len(raw)>48 and raw[47]==raw[48]=='-1':
                upper=lower=None
            else:
                upper,lower=number(47),number(48)
            if upper is not None and lower is not None and upper>0 and lower>0 and upper<lower:
                raise SourceFailure('inconsistent_price_range')
            row=dict(ts_code=key.symbol+'.'+{'SSE':'SH','SZSE':'SZ','BSE':'BJ'}[key.exchange],name=quote.name,
                **{name:text(value) for name,value in (('close',quote.price),('pre_close',quote.previous_close),
                    ('open',quote.open),('high',quote.high),('low',quote.low),('vol',quote.volume_shares),
                    ('amount',quote.amount_cny),('up_limit',upper),('down_limit',lower),
                    ('bid_price1',number(9)),('bid_volume1',number(10)*100 if number(10) is not None else None),
                    ('ask_price1',number(19)),('ask_volume1',number(20)*100 if number(20) is not None else None))},
                trade_time=quote.quote_time.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S') if quote.quote_time else None)
            for field in ('vol','bid_volume1','ask_volume1'):
                if row[field] is not None:
                    from decimal import Decimal
                    value=Decimal(row[field])
                    if value<0 or value!=value.to_integral_value():raise SourceFailure('invalid_number')
            result.append(row)
        except SourceFailure as error:
            batch.rejected[key.vendor_key]=error.reason
    return result,batch.rejected


def observation_clock():
    return datetime.now(SHANGHAI)


def settled_observation(rows, pages):
    """Read an actual clock after all retained observations, without inventing it.

    Page wall clocks remain unchanged, including regressions. Monotonic time
    bounds this local wait; it is independent of any supplier Retry-After.
    """
    required=max([datetime.fromisoformat(page[field]) for page in pages
        for field in ('request_started_at','received_at')]+[
        datetime.fromisoformat(row['trade_time']).replace(tzinfo=SHANGHAI)
        for row in rows if row.get('trade_time')])
    started=timer.monotonic()
    while True:
        observed=observation_clock()
        elapsed=timer.monotonic()-started
        if observed>=required:return observed,round(elapsed*1000)
        if elapsed>=MAX_CLOCK_WAIT:
            error=SourceFailure('local_clock_unsettled');error.request_count=len(pages);raise error
        timer.sleep(min(.1,MAX_CLOCK_WAIT-elapsed))


def fetch(scope, day, *, before_request):
    if (not 0 < len(scope.instruments) <= MAX_ROWS
            or len({key.identity for key in scope.instruments})!=len(scope.instruments)
            or any(key.asset_type!='stock' for key in scope.instruments)):
        raise SourceFailure('invalid_instrument_batch')
    rows,pages,rejected=[],[],{}
    started=timer.monotonic()
    for offset in range(0,len(scope.instruments),PAGE_SIZE):
        selection=scope.instruments[offset:offset+PAGE_SIZE]
        try:
            before_request()
            if offset and timer.monotonic()-started>=MAX_SECONDS:
                raise SourceFailure('collection_deadline')
        except SourceFailure as error:
            error.request_count=len(pages)
            raise
        try:
            began=observation_clock()
            response=fetch_https(quote_url(list(selection),maximum=PAGE_SIZE))
            part,failures=parse_page(response,selection)
            regressed=response.received_at<began or bool(pages and began<max(
                datetime.fromisoformat(page[field]) for page in pages
                for field in ('request_started_at','received_at')))
            rows.extend(dict(row,page=str(offset//PAGE_SIZE)) for row in part);rejected.update(failures)
            pages.append(dict(page=str(offset//PAGE_SIZE),requested=str(len(selection)),returned=str(len(part)),
                request_started_at=began.isoformat(),
                received_at=response.received_at.isoformat(),sha256=hashlib.sha256(response.body).hexdigest(),
                elapsed_ms=str(response.elapsed_ms),clock_regressed=str(regressed).lower()))
        except SourceFailure as error:
            error.request_count=len(pages)+getattr(error,'request_count',1)
            raise
    if not rows:
        error=SourceFailure('instrument_unavailable');error.request_count=len(pages);raise error
    received,clock_wait_ms=settled_observation(rows,pages)
    capture=content_hash(dict(scope=scope.digest,pages=pages,day=day,received_at=received))
    common=dict(capture_id=capture,scope_source_id=str(scope.source_id),scope_evidence_id=str(scope.evidence_id),
        scope_kind=scope.kind,scope_received_at=scope.received_at.isoformat(),universe_hash=scope.digest,
        clock_policy=CLOCK_POLICY,clock_wait_ms=str(clock_wait_ms))
    rows=[dict(row,capture_id=capture) for row in rows]
    if len({row['ts_code'] for row in rows})!=len(rows):
        error=SourceFailure('duplicate_instrument');error.request_count=len(pages);raise error
    scope_text=('有权已存名录或最近报价实际范围分批获取；不是交易所当日在市全集。每股原生时刻、逐页接收与整批接收分别保存。'
        '涨跌停只用同一原生响应的实际限制价，不按比例猜测；旧日/缺价/缺时刻与排除项不补零。')
    payload=ReferencePayload(dataset=DATASET,title='腾讯 · 名录范围批量行情',source_url='https://qt.gtimg.cn/',
        attribution='腾讯公开网页报价；本人本机研究',scope=scope_text,observed_day=day,
        columns=[dict(key=field,label=field,unit='股' if field in {'vol','bid_volume1','ask_volume1'} else
            '元' if field in {'close','pre_close','open','high','low','amount','up_limit','down_limit','bid_price1','ask_price1'} else None)
            for field in FIELDS],rows=rows)
    from app.market.tushare_market import compare_limits
    comparison_clock=received if received.astimezone(SHANGHAI).date()==day else datetime.combine(day,datetime.max.time(),SHANGHAI)
    calculated=compare_limits(rows,native_limits(rows,day),comparison_clock)
    statistics={f'stat_{key}':(value.isoformat() if isinstance(value,datetime) else str(value) if value is not None else None)
        for key,value in calculated.items()}
    revision=content_hash(payload)
    empty_stats={f'stat_{key}':None for key in STAT_FIELDS}
    metadata=[dict(row_type='page',**common,**page,expected=None,accepted=None,rejected=None,
        exclusion_counts=None,input_excluded=None,snapshot_revision=revision,calculation=CALCULATION,
        **empty_stats) for page in pages]
    metadata.append(dict(row_type='coverage',**common,page=None,requested=None,returned=None,
        request_started_at=None,
        clock_regressed=str(any(page['clock_regressed']=='true' for page in pages)).lower(),
        received_at=received.isoformat(),sha256=None,elapsed_ms=None,expected=str(len(scope.instruments)),
        accepted=str(len(rows)),rejected=str(len(rejected)),input_excluded=str(scope.excluded),
        exclusion_counts=json.dumps(dict(Counter(rejected.values())),separators=(',',':')),
        snapshot_revision=revision,calculation=CALCULATION,**statistics))
    manifest=ReferencePayload(dataset=MANIFEST,title='腾讯 · 批量行情覆盖',source_url='https://qt.gtimg.cn/',
        attribution=payload.attribution,scope=scope_text,observed_day=day,
        columns=[dict(key=key,label=key) for key in metadata[0]],rows=metadata)
    invalid=sum(row.get('trade_time') is None or not row['trade_time'].startswith(day.isoformat())
        or row['close'] is None for row in rows)
    result=ReferenceBatch(records=[(DATASET,payload,None),(MANIFEST,manifest,None)],expected=2,
        rejected=len(rejected)+invalid,requests=len(pages))
    response=Response(json.dumps([page['sha256'] for page in pages]).encode(),received,
        sum(int(page['elapsed_ms']) for page in pages),200,len(pages))
    return response,result


def native_limits(rows, day):
    return [dict(ts_code=row['ts_code'],trade_date=day.strftime('%Y%m%d'),
        up_limit=row.get('up_limit'),down_limit=row.get('down_limit')) for row in rows]


def manifest_for(db, capture, cutoff, *, received_at=None):
    from app.market.service import source_id
    if not isinstance(capture,str) or not re.fullmatch(r'[a-f0-9]{64}',capture):return None
    query=select(EvidenceVersion.payload).where(EvidenceVersion.source_id==source_id(FAMILY),
        EvidenceVersion.kind=='reference',EvidenceVersion.owner_id.is_(None),EvidenceVersion.visibility=='shared',
        EvidenceVersion.received_at<=cutoff,EvidenceVersion.payload['dataset'].astext==MANIFEST,
        EvidenceVersion.payload['rows'][0]['capture_id'].astext==capture)
    if received_at is not None:query=query.where(EvidenceVersion.received_at==received_at)
    payload=db.scalar(query.order_by(EvidenceVersion.received_at.desc()).limit(1))
    if payload is None:return None
    return closed_coverage(payload,capture,received_at)


def closed_coverage(payload, capture, received_at=None):
    """A missing/partial page manifest cannot authorize a full batch read."""
    try:
        rows=payload['rows']
        coverage=[row for row in rows if row['row_type']=='coverage']
        pages=[row for row in rows if row['row_type']=='page']
        if len(coverage)!=1 or len(pages)+1!=len(rows) or not 1<=len(pages)<=11:return None
        result=coverage[0]
        policy=result.get('clock_policy')
        if policy is not None and policy!=CLOCK_POLICY:return None
        observed=datetime.fromisoformat(result['received_at'])
        if observed.tzinfo is None:return None
        if policy==CLOCK_POLICY and (not 0<=int(result['clock_wait_ms'])<=MAX_CLOCK_WAIT*1000+1000
                or any(row.get('clock_policy')!=policy or row.get('clock_wait_ms')!=result['clock_wait_ms'] for row in rows)):
            return None
        if any(row['capture_id']!=capture or row['snapshot_revision']!=result['snapshot_revision']
               or row['calculation']!=CALCULATION for row in rows):return None
        pages.sort(key=lambda row:int(row['page']))
        prior=[];regressions=[]
        for index,page in enumerate(pages):
            start,end=map(datetime.fromisoformat,(page['request_started_at'],page['received_at']))
            regressed=end<start or bool(prior and start<max(prior))
            if (int(page['page'])!=index or not 0<int(page['requested'])<=PAGE_SIZE
                    or not 0<=int(page['returned'])<=int(page['requested'])
                    or start.tzinfo is None or end.tzinfo is None
                    or policy is None and start>end
                    or max(start,end)>observed
                    or policy==CLOCK_POLICY and (page.get('clock_regressed')!=str(regressed).lower()
                        or int(page['elapsed_ms'])<0)
                    or not re.fullmatch(r'[a-f0-9]{64}',page['sha256'])):return None
            prior.extend((start,end));regressions.append(regressed)
            if index+1<len(pages) and int(page['requested'])!=PAGE_SIZE:return None
        if policy==CLOCK_POLICY and result.get('clock_regressed')!=str(any(regressions)).lower():return None
        expected,accepted,rejected=[int(result[key]) for key in ('expected','accepted','rejected')]
        if (not 0<accepted<=expected<=MAX_ROWS or expected!=accepted+rejected
                or expected!=sum(int(page['requested']) for page in pages)
                or accepted!=sum(int(page['returned']) for page in pages)
                or received_at is not None and datetime.fromisoformat(result['received_at'])!=received_at):return None
        return result
    except (KeyError,ValueError,TypeError,AttributeError):return None


def snapshot_metadata(db, provider, day, cutoff, *, limit=2000):
    """Two bounded queries transfer small manifests instead of raw quote history."""
    base=select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id,
        EvidenceVersion.kind=='reference',EvidenceVersion.owner_id.is_(None),EvidenceVersion.visibility=='shared',
        EvidenceVersion.received_at<=cutoff,EvidenceVersion.payload['observed_day'].astext==day.isoformat())
    snapshots=list(db.execute(base.where(EvidenceVersion.payload['dataset'].astext==DATASET).with_only_columns(
        EvidenceVersion.id,EvidenceVersion.source_id,EvidenceVersion.source_key,EvidenceVersion.received_at,
        EvidenceVersion.content_hash,EvidenceVersion.source_revision,EvidenceVersion.payload['rows'][0].label('first'))
        .order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(limit)))
    if not snapshots:return []
    oldest=min(row.received_at for row in snapshots)
    manifests={}
    for stamp,payload in db.execute(base.where(EvidenceVersion.payload['dataset'].astext==MANIFEST,
            EvidenceVersion.received_at>=oldest).with_only_columns(EvidenceVersion.received_at,EvidenceVersion.payload)):
        capture=(payload.get('rows') or [{}])[0].get('capture_id')
        key=(stamp,capture)
        manifests[key]=None if key in manifests else closed_coverage(payload,capture,stamp)
    allowed={};result=[]
    for row in snapshots:
        first=row._mapping['first'];capture=first.get('capture_id') if isinstance(first,dict) else None
        meta=manifests.get((row.received_at,capture))
        if meta is None or meta.get('snapshot_revision')!=row.source_revision:continue
        key=(meta.get('scope_source_id'),meta.get('scope_evidence_id'))
        if key not in allowed:
            allowed[key]=saved_scope_allowed(db,{'rows':[first]},cutoff,received_at=row.received_at,coverage=meta)
        # Age belongs to each actual capture, not the newest batch in the read.
        if not allowed[key] or row.received_at-datetime.fromisoformat(meta['scope_received_at'])>SCOPE_AGE:continue
        result.append(SimpleNamespace(**{key:value for key,value in row._mapping.items() if key!='first'},
            family=FAMILY,dates=frozenset({day}),coverage=meta))
    return result


def comparison(db, snapshot, cutoff):
    """Read locally calculated scalars bound to the exact immutable raw batch.

    This avoids decoding every 6,000-stock history batch on every page update.
    The calculation is explicitly local; it is not a supplier aggregate.
    """
    if getattr(snapshot,'coverage',None) is not None:
        raw=(snapshot.source_revision,snapshot.coverage['capture_id']);meta=snapshot.coverage
    else:
        raw=db.execute(select(EvidenceVersion.source_revision,EvidenceVersion.payload['rows'][0]['capture_id'].astext)
            .where(EvidenceVersion.id==snapshot.id,EvidenceVersion.received_at<=cutoff)).first()
        if raw is None:return {}
        meta=manifest_for(db,raw[1],cutoff,received_at=snapshot.received_at)
    if snapshot.received_at>cutoff:return {}
    if meta is None or meta.get('snapshot_revision')!=raw[0] or meta.get('calculation')!=CALCULATION:return {}
    result={}
    try:
        for key in STAT_FIELDS:
            value=meta.get('stat_'+key)
            if key in {'source_time_min','source_time_max'}:
                value=datetime.fromisoformat(value) if value is not None else None
                if value is not None and (value.tzinfo is None or value>snapshot.received_at or value>cutoff):return {}
            elif key=='total_amount_cny':
                if value is not None:
                    if not isinstance(value,str) or len(value)>64:return {}
                    amount=Decimal(value)
                    if not amount.is_finite() or amount<0:return {}
            else:
                value=int(value)
                if not 0<=value<=MAX_ROWS:return {}
            result[key]=value
        if result['returned']!=int(meta['accepted']) or result['comparable']+result['unknown']!=result['returned']:return {}
        return result
    except (ValueError,TypeError,SourceFailure,InvalidOperation):return {}


def breadth_series(db, target_day, cutoff, *, latest_only=False):
    from app.market.schemas import BreadthPoint, BreadthSeries
    from app.market.service import permission_reason, source_id
    provider=db.get(Provider,source_id(FAMILY))
    if provider is None or permission_reason(provider):return None
    snapshots=snapshot_metadata(db,provider,target_day,cutoff,limit=1 if latest_only else 2001)
    points=[]
    for snapshot in reversed(snapshots[:2000]):
        counts=comparison(db,snapshot,cutoff)
        if not counts:continue
        if not counts['comparable']:counts.update(limit_up=None,limit_down=None)
        points.append(BreadthPoint(id=snapshot.id,received_at=snapshot.received_at,**counts))
    if not points:return None
    clock=lambda point:point.received_at.astimezone(SHANGHAI).strftime('%H:%M:%S')
    items=[p for p in points if '09:30:00'<=clock(p)<='11:30:59' or '13:00:00'<=clock(p)<='15:00:59']
    return BreadthSeries(observation_day=target_day,source_name=provider.name,source_family=FAMILY,
        scope='目录实际范围逐页接收；目录不保证当日在市全集。用本批各股原生上下限价与最新价精确比较；旧日、缺价、缺时刻不补零。与其他来源历史分开。',
        items=items,latest=points[-1],truncated=len(snapshots)>2000)
