"""TdxAiData SDK boundary: private credentials, bounded subprocess, real units.

Each job starts a separate process because the native SDK binds its first token
for the process lifetime. Its temporary token ini is removed on process exit.
An SDK snapshot is L1 five-level data; it is never labelled tick-by-tick or L2.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
import time
from datetime import UTC, datetime, time as wall_time, timedelta
from decimal import Decimal

from app.evidence.schemas import ReferenceColumn, ReferencePayload
from app.market.connected_specs import TDX_DOCS, TDX_ENDPOINT, TDX_FAMILIES
from app.market.fuyao import code, number, text_number
from app.market.minute import MinuteBatch, MinuteObservation
from app.market.protocol import Quote, QuoteBatch, Response, SHANGHAI, SourceFailure
from app.market.reference import ReferenceBatch

MAX_BODY = 8_000_000
MAX_SELECTION = 20
SNAPSHOT_FIELDS = ('Now','Open','Max','Min','LastClose','Volume','Amount','RefreshTime','Buyp','Buyv','Sellp','Sellv')


def attempted_requests(directory):
    try:
        value=int((Path(directory)/'request-count').read_text(encoding='ascii'))
        return value if 0<=value<=MAX_SELECTION else 0
    except (OSError,ValueError):return 0


def fetch(family, selection, credential, *, endpoint=TDX_ENDPOINT, day=None, before_request=None):
    if family not in TDX_FAMILIES or endpoint != TDX_ENDPOINT:
        raise SourceFailure('endpoint_not_allowed')
    if not 1 <= len(selection) <= MAX_SELECTION or any(key.asset_type!='stock' for key in selection):
        raise SourceFailure('invalid_instrument_batch')
    secret=credential.get_secret_value() if hasattr(credential,'get_secret_value') else credential
    if not isinstance(secret,str) or not re.fullmatch(r'TDX-[A-Za-z0-9_-]{8,240}',secret):
        raise SourceFailure('tdx_data_key_required')
    if before_request:before_request()
    request={'family':family,'codes':[code(key) for key in selection], 'api_key':secret,
        'day':(day or datetime.now(UTC).astimezone(SHANGHAI).date()).isoformat()}
    started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='tianyan-tdx-job-') as private_dir:
        # Parent owns cleanup even if the native child crashes or is killed.
        request['private_lib_dir']=private_dir
        try:
            # stdin keeps the secret out of process listings, arguments and logs.
            proc=subprocess.run([sys.executable,'-m','app.market.tdx_reader'],input=json.dumps(request),
                capture_output=True,text=True,timeout=110,env={**os.environ,'PYTHONIOENCODING':'utf-8'})
        except subprocess.TimeoutExpired:
            error=SourceFailure('tdx_sdk_timeout')
            error.request_count=attempted_requests(private_dir)
            raise error from None
        attempted=attempted_requests(private_dir)
    if proc.returncode or len(proc.stdout.encode('utf8'))>MAX_BODY:
        error=SourceFailure('tdx_sdk_unavailable');error.request_count=attempted;raise error
    try:
        payload=json.loads(proc.stdout)
        if not isinstance(payload,dict):raise ValueError()
    except (ValueError,TypeError):
        error=SourceFailure('tdx_sdk_unavailable');error.request_count=attempted;raise error from None
    if payload.get('error'):
        # Only our worker's enumerated codes are eligible for public state.
        allowed={'tdx_dependency_missing','tdx_data_permission_missing','tdx_sdk_unavailable','tdx_no_data','schema_changed'}
        error=SourceFailure(payload['error'] if payload['error'] in allowed else 'tdx_sdk_unavailable')
        count=payload.get('request_count',attempted)
        error.request_count=count if type(count) is int and 0<=count<=MAX_SELECTION else 0
        raise error
    body=json.dumps(payload,separators=(',',':'),ensure_ascii=True).encode()
    return Response(body,datetime.now(UTC),round((time.monotonic()-started)*1000),200,
        int(payload.get('request_count',1)))


def snapshot_time(raw, received_at):
    """A time of day cannot identify the trade date, especially on weekends."""
    value=raw.get('RefreshTime')
    if value is None:return None
    text=str(value).strip()
    try:
        if re.fullmatch(r'\d{14}',text):
            stamp=datetime.strptime(text,'%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI)
        elif re.fullmatch(r'\d{1,6}',text):
            day=raw.get('Date') or raw.get('TradeDate')
            if day is None:return None
            stamp=datetime.strptime(str(day).replace('-','')[:8]+text.zfill(6),'%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI)
        else:
            stamp=datetime.fromisoformat(text)
            if stamp.utcoffset() is None:return None
    except (ValueError,TypeError):
        raise SourceFailure('invalid_source_time') from None
    if stamp>received_at+timedelta(seconds=60):raise SourceFailure('future_source_time')
    return stamp


def unpack(body):
    try:
        value=json.loads(body,parse_float=Decimal)
        if not isinstance(value,dict) or not isinstance(value.get('rows'),dict) or len(value['rows'])>MAX_SELECTION:
            raise ValueError()
        return value
    except (ValueError,TypeError,UnicodeError):
        raise SourceFailure('schema_changed') from None


def parse_quotes(body, selection, received_at, names=None):
    rows=unpack(body)['rows'];expected={code(key):key for key in selection}
    if set(rows)-expected.keys():raise SourceFailure('unexpected_instrument')
    batch=QuoteBatch(expected=len(selection))
    for vendor,key in expected.items():
        raw=rows.get(vendor)
        if not isinstance(raw,dict):batch.rejected[vendor]='instrument_missing';continue
        try:
            prices=[number(raw.get(field),positive=True) for field in ('Now','LastClose','Open','Max','Min')]
            price,previous,opening,high,low=prices
            if price is None:raise SourceFailure('instrument_unavailable')
            if high is not None and low is not None and (high<low or any(v is not None and not low<=v<=high for v in (price,opening))):
                raise SourceFailure('inconsistent_price_range')
            volume=number(raw.get('Volume'))
            # Official snapshot Volume is lots (100 shares), Amount is 10k CNY.
            shares=number(volume*100,integer=True) if volume is not None else None
            amount=number(raw.get('Amount'))
            batch.records.append(Quote(key,(names or {}).get(key.identity) or key.symbol,*prices,
                snapshot_time(raw,received_at),received_at,'CNY',volume_native=volume,
                volume_shares=shares,amount_cny=amount*10000 if amount is not None else None))
        except SourceFailure as exc:batch.rejected[vendor]=exc.reason
    return batch


def parse_depth(body,selection,received_at):
    rows=unpack(body)['rows'];expected={code(key):key for key in selection}
    if set(rows)-expected.keys():raise SourceFailure('unexpected_instrument')
    batch=ReferenceBatch(expected=len(selection))
    fields=[('side','方向'),('level','档位'),('price','价格'),('volume_shares','挂单量'),('quote_time','上游时刻')]
    for vendor,key in expected.items():
        raw=rows.get(vendor)
        if not isinstance(raw,dict):batch.rejected+=1;continue
        try:
            stamp=snapshot_time(raw,received_at);out=[]
            for side,prices,volumes in (('bid','Buyp','Buyv'),('ask','Sellp','Sellv')):
                pp,vv=raw.get(prices),raw.get(volumes)
                if not isinstance(pp,list) or not isinstance(vv,list) or len(pp)!=5 or len(vv)!=5:
                    raise SourceFailure('depth5_incomplete')
                for index,(price,volume) in enumerate(zip(pp,vv)):
                    p=number(price);v=number(volume)
                    if p is None or v is None:raise SourceFailure('depth5_incomplete')
                    out.append({'side':side,'level':str(index+1),'price':text_number(p),
                        'volume_shares':text_number(v*100,integer=True),'quote_time':stamp.isoformat() if stamp else None})
            payload=ReferencePayload(dataset='tdx.depth5',title='五档盘口快照',source_url=TDX_DOCS,
                attribution='通达信 TdxAiData',scope='明确选择的股票；五档L1挂单快照，不是逐笔、历史订单队列或L2。'+
                    ('上游有完整时刻。' if stamp else '上游仅提供时分秒、缺交易日期，时点待核验。'),
                columns=[ReferenceColumn(key=f,label=label,unit='元' if f=='price' else '股' if f=='volume_shares' else None) for f,label in fields],
                rows=out,observed_day=received_at.astimezone(SHANGHAI).date())
            batch.records.append(('tdx.depth5:'+key.identity,payload,key))
        except SourceFailure:batch.rejected+=1
    return batch


def parse_minutes(body,selection,received_at,day):
    rows=unpack(body)['rows'];expected={code(key):key for key in selection}
    if set(rows)-expected.keys():raise SourceFailure('unexpected_instrument')
    result=MinuteBatch(expected=0)
    for vendor,key in expected.items():
        raw=rows.get(vendor)
        if not isinstance(raw,dict):result.rejected[vendor]='instrument_missing';continue
        dates=raw.get('Date');times=raw.get('Time')
        if not isinstance(dates,list) or not isinstance(times,list) or len(dates)!=len(times) or len(dates)>480:
            result.rejected[vendor]='schema_changed';continue
        seen=set();count=0
        for index,(d,t) in enumerate(zip(dates,times)):
            try:
                stamp=datetime.strptime(str(d).replace('-','')[:8]+str(t).zfill(6),'%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI)
                if stamp.date()!=day:continue
                result.expected+=1
                if stamp>received_at+timedelta(seconds=60):raise SourceFailure('future_source_time')
                if stamp.second or not (wall_time(9,30)<=stamp.time()<=wall_time(11,30) or wall_time(13)<=stamp.time()<=wall_time(15)):
                    raise SourceFailure('invalid_source_time')
                if stamp in seen:
                    # Do not choose whichever conflicting copy arrived first.
                    result.records=[item for item in result.records if not (item.instrument==key and item.source_time==stamp)]
                    raise SourceFailure('duplicate_bar')
                seen.add(stamp)
                def col(name):
                    values=raw.get(name)
                    if not isinstance(values,list) or len(values)!=len(dates):raise SourceFailure('schema_changed')
                    return number(values[index],positive=name in {'Open','High','Low','Close'})
                opening,high,low,close=[col(f) for f in ('Open','High','Low','Close')]
                if any(v is None for v in (opening,high,low,close)) or not low<=min(opening,close)<=max(opening,close)<=high:
                    raise SourceFailure('inconsistent_price_range')
                shares=col('Volume');amount=col('Amount')
                if shares is not None:shares=number(shares,integer=True)
                observation=MinuteObservation(instrument=key,interval='1m',source_time=stamp,open=opening,high=high,low=low,close=close,
                    volume_native=shares,amount_native=amount*10000 if amount is not None else None)
                result.records.append(observation);count+=1
            except (SourceFailure,ValueError,TypeError):result.rejected[vendor+':'+str(index)]='invalid_minute_row'
        if not count and not any(k.startswith(vendor+':') for k in result.rejected):result.rejected[vendor]='minute_day_not_returned'
    return result
