"""Native auction snapshots from the already bound Fuyao connection.

The signed unmatched quantity is not matched turnover or a continuous-session
bid. Native quantities are lots (verified against price/amount and the existing
matched-auction feed); normalized shares keep the original quantity alongside.
The response timestamp is assembly time, never an invented exchange timestamp.
"""
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import json
import time as timer

from app.evidence.schemas import ReferencePayload
from app.market import fuyao
from app.market.protocol import SHANGHAI, SourceFailure, Response
from app.market.reference import ReferenceBatch

FAMILY = 'fuyao_auction'
DATASET = 'fuyao.auction_snapshot'
PATH = '/api/a-share/auction/snapshot'
PAGE_SIZE = 100
MAX_SECONDS = 50
FIELDS = ('row_type','ts_code','name','price','pre_close','pct_change','unmatched_lots',
    'unmatched_shares','volume_lots','volume_shares','amount_cny','snapshot_at','received_at',
    'auction_phase','data_status','requested','returned','usable','expected','cycle_started_at')
UNITS = {'price':'元','pre_close':'元','pct_change':'%','unmatched_lots':'手',
    'unmatched_shares':'股','volume_lots':'手','volume_shares':'股','amount_cny':'元'}


def selection(db, config, clock):
    from app.market.tdx_full_minute import universe
    from app.market.terminal_read import SavedTables
    day=clock.astimezone(SHANGHAI).date()
    limits={row['ts_code']:row for row,_,_ in SavedTables(db,clock,day).table('tushare_market_limits',day)}
    result=[key for key in universe(db,config) if fuyao.code(key) in limits
            and all(fuyao.number(limits[fuyao.code(key)].get(field),positive=True) is not None
                    for field in ('up_limit','down_limit'))]
    if not result: raise SourceFailure('auction_limits_unavailable')
    return result


def signed(value):
    if value in (None,'','-','--'): return None
    if isinstance(value,bool): raise SourceFailure('invalid_number')
    try:
        result=Decimal(str(value))
        if not result.is_finite() or abs(result)>Decimal('1e16'): raise ValueError()
        return result
    except (InvalidOperation,ValueError,TypeError):
        raise SourceFailure('invalid_number') from None


def parse(body, selection, received_at, day):
    data=fuyao.unpack(body)
    stamp=fuyao.epoch_clock(data.get('timestamp'),received_at,required=True)
    if stamp.astimezone(SHANGHAI).date()!=day or not -60<=(received_at-stamp).total_seconds()<=90:
        raise SourceFailure('auction_snapshot_stale')
    phase,status=data.get('auction_phase'),data.get('data_status')
    live=phase in {'live','auction'} and status in {'live','ready'}
    final=phase in {'final','closed'} and status in {'ready','final'}
    local=received_at.astimezone(SHANGHAI).time()
    if not (live and time(9,15)<=local<time(9,25) or final and local>=time(9,25)):
        raise SourceFailure('auction_data_not_ready')
    expected={fuyao.code(key) for key in selection}
    if not 1<=len(expected)<=PAGE_SIZE or len(expected)!=len(selection):
        raise SourceFailure('invalid_instrument_batch')
    if type(data.get('total')) is not int or data['total']!=len(data['item']):
        raise SourceFailure('incomplete_auction_page')
    seen,rows=set(),[]
    for raw in data['item']:
        code=raw.get('thscode')
        if code not in expected or code in seen or raw.get('ticker')!=code[:6]:
            raise SourceFailure('unexpected_instrument')
        seen.add(code)
        price=fuyao.number(raw.get('auction_price'),positive=True)
        previous=fuyao.number(raw.get('pre_close_price'),positive=True)
        volume=fuyao.number(raw.get('auction_volume'))
        amount=fuyao.number(raw.get('auction_amount'))
        unmatched=signed(raw.get('auction_unmatched'))
        if price is None or previous is None or unmatched is None: continue
        shares=unmatched*100
        if shares!=shares.to_integral_value(): raise SourceFailure('auction_quantity_unit_changed')
        # Fail closed if the vendor switches native volume units.
        if volume is not None and amount is not None and volume>0 and abs(price*volume*100-amount)>max(Decimal('1'),amount*Decimal('.00001')):
            raise SourceFailure('auction_quantity_unit_changed')
        pct=signed(raw.get('auction_pct'))
        if pct is None or abs((price/previous-1)*100-pct)>Decimal('.02'):
            raise SourceFailure('inconsistent_auction_price')
        name=raw.get('name')
        if not isinstance(name,str) or not name.strip() or len(name)>200: raise SourceFailure('invalid_name')
        values=dict(row_type='stock',ts_code=code,name=name,price=price,pre_close=previous,pct_change=pct,
            unmatched_lots=unmatched,unmatched_shares=shares,volume_lots=volume,
            volume_shares=volume*100 if volume is not None else None,amount_cny=amount,
            snapshot_at=stamp.isoformat(),received_at=received_at.isoformat(),auction_phase=phase,data_status=status)
        rows.append({k:format(v,'f') if isinstance(v,Decimal) else str(v) if v is not None else None
                     for k in FIELDS for v in [values.get(k)]})
    return rows,len(seen)


def fetch(selection, started_at, credential, *, endpoint=fuyao.FUYAO_ENDPOINT,
          before_request=None, offset=0, read=None, monotonic=timer.monotonic):
    read=read or fuyao.request
    if not 1<=len(selection)<=7900 or len({key.identity for key in selection})!=len(selection):
        raise SourceFailure('invalid_instrument_batch')
    if type(offset) is not int or not 0<=offset<len(selection) or offset%PAGE_SIZE: offset=0
    pages=[selection[i:i+PAGE_SIZE] for i in range(0,len(selection),PAGE_SIZE)]
    indexes=list(range(offset//PAGE_SIZE,len(pages)))+list(range(offset//PAGE_SIZE))
    begun,requests,returned,requested,elapsed=monotonic(),0,0,0,0
    received,rows,next_offset=started_at,[],offset
    for index in indexes:
        if requests and monotonic()-begun>=MAX_SECONDS: break
        page=pages[index]
        try:
            if before_request and before_request() is False: raise SourceFailure('request_not_permitted')
            requests+=1
            stage='live' if started_at.astimezone(SHANGHAI).time()<time(9,25) else 'final'
            response=read(PATH,{'thscodes':','.join(fuyao.code(key) for key in page),'stage':stage},credential,endpoint=endpoint)
            received=max(received,response.received_at);elapsed+=response.elapsed_ms
            items,count=parse(response.body,page,response.received_at,started_at.astimezone(SHANGHAI).date())
        except SourceFailure as exc:
            exc.request_count=requests
            raise
        rows.extend(items);returned+=count;requested+=len(page)
        next_offset=((index+1)%len(pages))*PAGE_SIZE
        if stage=='live' and received.astimezone(SHANGHAI).time()>=time(9,25): break
    if not rows:
        error=SourceFailure('auction_data_not_ready');error.request_count=requests;raise error
    coverage=dict.fromkeys(FIELDS)
    coverage.update(row_type='coverage',requested=str(requested),returned=str(returned),usable=str(len(rows)),
        expected=str(len(selection)),cycle_started_at=started_at.isoformat())
    payload=ReferencePayload(dataset=DATASET,title='同花顺竞价未匹配量快照',
        source_url=fuyao.FUYAO_DOCS+'api-reference/auction/',attribution='已接同花顺API；本机个人研究',
        scope='原生竞价价格、带方向未匹配量和真实响应接收时刻。手×100为股；价格×未匹配股数为挂单金额。'
              '响应timestamp仅为组装时间，不是交易所行情时刻；final只声明最终竞价结果，不补造早盘过程。',
        columns=[{'key':key,'label':key,'unit':UNITS.get(key)} for key in FIELDS],rows=[coverage,*rows],
        observed_day=started_at.astimezone(SHANGHAI).date())
    batch=ReferenceBatch(records=[('fuyao-auction:'+str(payload.observed_day),payload,None)],
        expected=1,rejected=int(len(rows)!=len(selection)),requests=requests)
    batch.book_progress={'next_offset':next_offset,'requested':requested,'returned':returned,'usable':len(rows),'expected':len(selection)}
    return Response(b'',received,elapsed,200,requests),batch
