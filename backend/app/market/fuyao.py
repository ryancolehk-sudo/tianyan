"""Bounded official Fuyao REST requests and evidence-contract normalization.

Implemented independently from the official REST contract. The TSP MIT plugin
was consulted for the raw/unadjusted default and field aliases only. No server
message, credential, or full upstream body is included in an exception.
"""
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, time as wall_time, timedelta
from decimal import Decimal, InvalidOperation

from app.evidence.schemas import BarPayload, ReferenceColumn, ReferencePayload
from app.market.connected_specs import FUYAO_ENDPOINT, FUYAO_FAMILIES, FUYAO_DOCS
from app.market.protocol import NoRedirect, Quote, QuoteBatch, Response, SHANGHAI, SourceFailure
from app.market.reference import ReferenceBatch

MAX_BODY = 2_000_000
STATEMENTS = ('income-statements', 'balance-sheets', 'cash-flow-statements')
SUFFIX = {'SSE': 'SH', 'SZSE': 'SZ', 'BSE': 'BJ'}
# The existing index endpoint uses these Shanghai internal ticker aliases.
# thscode still has to match the requested canonical exchange and instrument.
INDEX_TICKERS = {('SSE', '000001'): '1A0001', ('SSE', '000688'): '1B0688'}


def code(key):
    return key.symbol + '.' + SUFFIX[key.exchange]


def number(value, *, integer=False, positive=False):
    if value is None or value in ('', '-', '--'):
        return None
    if isinstance(value, bool):
        raise SourceFailure('invalid_number')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SourceFailure('invalid_number') from None
    if not result.is_finite() or result < 0 or result > Decimal('9999999999999999.99999999'):
        raise SourceFailure('invalid_number')
    if integer and result != result.to_integral_value():
        raise SourceFailure('invalid_number')
    if integer:
        result=result.to_integral_value()
    elif result.normalize().as_tuple().exponent < -8:
        raise SourceFailure('invalid_number')
    if positive and result == 0:
        return None
    return result


def text_number(value, **kwargs):
    result = number(value, **kwargs)
    if result is None:
        return None
    return str(int(result)) if kwargs.get('integer') else format(result, 'f')


def epoch_clock(value, received_at, *, required=False):
    if value is None or value == 0:
        if required:
            raise SourceFailure('invalid_source_time')
        return None
    # Historical millisecond epochs before September 2001 contain 12 digits.
    # Keep seconds (10 digits) invalid instead of silently changing their unit.
    if isinstance(value, bool) or not re.fullmatch(r'\d{12,13}', str(value)):
        raise SourceFailure('invalid_source_time')
    try:
        result = datetime.fromtimestamp(int(value) / 1000, UTC)
    except (ValueError, OverflowError, OSError):
        raise SourceFailure('invalid_source_time') from None
    if result > received_at + timedelta(seconds=60):
        raise SourceFailure('future_source_time')
    return result


def request(path, params, credential, *, endpoint=FUYAO_ENDPOINT, before_request=None):
    if endpoint != FUYAO_ENDPOINT or not path.startswith('/api/a-share') or '?' in path:
        raise SourceFailure('endpoint_not_allowed')
    secret = credential.get_secret_value() if hasattr(credential, 'get_secret_value') else credential
    if not isinstance(secret, str) or not secret:
        raise SourceFailure('credentials_missing')
    if before_request:
        if before_request() is False:
            raise SourceFailure('request_not_permitted')
    from app.market import fuyao_transport
    try:
        result=fuyao_transport.request(path,params,{'X-api-key':secret,'Accept':'application/json',
            'Accept-Encoding':'identity','User-Agent':'Tianyan/1.0 (private research)'},max_body=MAX_BODY)
    except SourceFailure as exc:
        exc.request_count=1;raise
    try:unpack(result.body)
    except SourceFailure as exc:exc.request_count=1;raise
    return result


def unpack(body):
    try:
        value = json.loads(body, parse_float=Decimal)
        if not isinstance(value, dict) or 'code' not in value:
            raise ValueError()
        status = str(value['code'])
        if status != '0':
            error=SourceFailure('credentials_rejected' if status == '2001' else
                'permission_missing' if status in {'2002', '2003'} else
                'rate_limited' if status == '4001' else
                'instrument_unavailable' if status in {'3002', '5003'} else 'upstream_unavailable')
            error.transport_diagnostic={'version':1,'upstream_code':status if re.fullmatch(r'\d{1,9}',status) else 'unknown'}
            raise error
        data = value['data']
        if not isinstance(data, dict) or not isinstance(data.get('item'), list) or len(data['item']) > 8000:
            raise ValueError()
        return data
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise SourceFailure('schema_changed') from None


def parse_quotes(body, selection, received_at, names=None):
    data = unpack(body)
    expected = {code(key): key for key in selection}
    if not 1 <= len(expected) <= 50 or len(expected) != len(selection):
        raise SourceFailure('invalid_instrument_batch')
    batch = QuoteBatch(expected=len(expected))
    seen = set()
    for raw in data['item']:
        if not isinstance(raw, dict) or raw.get('thscode') not in expected or raw.get('thscode') in seen:
            raise SourceFailure('unexpected_instrument')
        vendor = raw['thscode']; seen.add(vendor); key = expected[vendor]
        try:
            tickers = {key.symbol}
            if key.asset_type == 'index':
                alias = INDEX_TICKERS.get((key.exchange, key.symbol))
                if alias:
                    tickers.add(alias)
            ticker = raw.get('ticker', key.symbol)
            if not isinstance(ticker, str) or ticker not in tickers or raw.get('currency', 'CNY') != 'CNY':
                raise SourceFailure('instrument_mismatch')
            prices = [number(raw.get(name, raw.get(alias)), positive=True) for name, alias in (
                ('last_price','last_price'),('prev_price','prev_close_price'),('open_price','open_price'),
                ('high_price','highest_price'),('low_price','lowest_price'))]
            price, previous, opening, high, low = prices
            if price is None:
                raise SourceFailure('instrument_unavailable')
            if high is not None and low is not None and (high < low or any(v is not None and not low <= v <= high for v in (price, opening))):
                raise SourceFailure('inconsistent_price_range')
            # Index turnover/volume conventions are not interchangeable with share units.
            volume = number(raw.get('volume'), integer=True) if key.asset_type == 'stock' else None
            amount = number(raw.get('turnover'))
            # 2026-10-25 live probe: the outer timestamp advances on Sunday
            # although rows have no trade clock. Treat it as server metadata;
            # only an explicit row-level market clock can date this quote.
            stamp = epoch_clock(raw.get('quote_time_ms') or raw.get('trade_time_ms'), received_at)
            name = (names or {}).get(key.identity) or key.symbol
            batch.records.append(Quote(key, name, price, previous, opening, high, low, stamp, received_at,
                'point' if key.asset_type == 'index' else 'CNY', volume_shares=volume, amount_cny=amount,
                source_snapshot_time=epoch_clock(data.get('timestamp'), received_at)))
        except SourceFailure as exc:
            batch.rejected[vendor] = exc.reason
    for missing in expected.keys() - seen:
        batch.rejected[missing] = 'instrument_missing'
    return batch


def table(dataset, title, rows, keys, received_at, *, scope, units=None):
    return ReferencePayload(dataset=dataset, title=title, source_url=FUYAO_DOCS, attribution='同花顺 API官方 REST',
        scope=scope, observed_day=received_at.astimezone(SHANGHAI).date(),
        columns=[ReferenceColumn(key=key, label=label, unit=(units or {}).get(key)) for key, label in keys],
        rows=rows)


def parse_reference(family, body, key, received_at, *, statement=None):
    data = unpack(body)
    if data.get('thscode', code(key)) != code(key):
        raise SourceFailure('instrument_mismatch')
    result = ReferenceBatch(expected=len(data['item']))
    if family in {'fuyao_daily', 'fuyao_index_daily'}:
        seen = set()
        for raw in data['item']:
            try:
                if raw.get('thscode', code(key)) != code(key) or raw.get('currency','CNY') != 'CNY':
                    raise SourceFailure('instrument_mismatch')
                day = epoch_clock(raw.get('date_ms'), received_at, required=True).astimezone(SHANGHAI).date()
                if day in seen:
                    raise SourceFailure('duplicate_bar')
                seen.add(day)
                values = {target: text_number(raw.get(source), positive=True) for target, source in (
                    ('open','open_price'),('high','high_price'),('low','low_price'),('close','close_price'))}
                if any(v is None for v in values.values()):
                    raise SourceFailure('invalid_number')
                bar = BarPayload(**values, volume_shares=text_number(raw.get('volume'), integer=True) if key.asset_type=='stock' else None,
                    amount_cny=text_number(raw.get('turnover')), interval='1d', bar_end_at=None, trade_day=day,
                    adjustment='unadjusted', price_unit='point' if key.asset_type=='index' else 'CNY')
                result.bars.append((key, bar))
            except (SourceFailure, ValueError, TypeError, AttributeError):
                result.rejected += 1
        return result
    if family == 'fuyao_adj_factor':
        rows = []
        fields = [('ex_date','除权除息日'),('dividend_per_share','每股税前派息'),('per_share_bonus','每股送股比例'),
            ('allotment_ratio','配股比例'),('allotment_price','配股价格')]
        for raw in data['item']:
            try:
                if raw.get('ticker',key.symbol) != key.symbol:
                    raise SourceFailure('instrument_mismatch')
                day = epoch_clock(raw.get('ex_date_ms'), received_at, required=True).astimezone(SHANGHAI).date()
                rows.append({'ex_date':day.isoformat(), **{field:text_number(raw.get(field)) for field,_ in fields[1:]}})
            except (SourceFailure, ValueError, TypeError, AttributeError):
                result.rejected += 1
        payload = table('fuyao.adjustment_events', '原始除权事件', rows, fields, received_at,
            scope='已选股票原始事件；每股口径，未推导累积复权因子；没有公告时刻，不用于严格历史判断。',
            units={'dividend_per_share':'元/股','per_share_bonus':'比例','allotment_ratio':'比例','allotment_price':'元'})
        result.records.append(('fuyao.adjustment_events:'+key.identity, payload, key))
        return result
    if family != 'fuyao_financial' or statement not in STATEMENTS:
        raise SourceFailure('capability_not_implemented')
    metadata = [('thscode','证券代码'),('period_end','报告期末'),('report_date','披露日期'),('currency','币种')]
    # Keep the official scalar table in its original currency; signed financial
    # values are valid and missing disclosures remain null.
    names = sorted({name for row in data['item'] if isinstance(row,dict) for name,value in row.items()
        if name not in {'thscode','ticker','period','fiscal_year','fiscal_period','report_date_ms','period_end_ms','currency'}
        and re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]{0,59}', name) and (value is None or isinstance(value,(int,float,Decimal)))})
    if len(names) > 56:
        raise SourceFailure('schema_changed')
    rows=[]
    for raw in data['item']:
        try:
            if raw.get('thscode') != code(key) or raw.get('currency') != 'CNY':
                raise SourceFailure('instrument_mismatch')
            period=epoch_clock(raw.get('period_end_ms'),received_at,required=True).astimezone(SHANGHAI).date()
            reported=epoch_clock(raw.get('report_date_ms'),received_at)
            values={}
            for name in names:
                value=raw.get(name)
                if value is None:values[name]=None;continue
                numeric=Decimal(str(value))
                if not numeric.is_finite() or abs(numeric)>Decimal('1e20'):
                    raise SourceFailure('invalid_number')
                values[name]=format(numeric,'f')
            rows.append({'thscode':code(key),'period_end':period.isoformat(),'report_date':reported.astimezone(SHANGHAI).date().isoformat() if reported else None,
                'currency':'CNY',**values})
        except (SourceFailure, ValueError, TypeError, AttributeError, InvalidOperation):
            result.rejected+=1
    dataset='fuyao.financial.'+statement.replace('-','_')
    fields=metadata+[(name,name) for name in names]
    payload=table(dataset,'财务报表 · '+statement,rows,fields,received_at,
        scope='已选股票整体合并报表、季度累计口径；金额为原币元，每股收益元/股；披露日无精确时刻，仅为当前已观察资料。',
        units={name:'元/股' if name=='basic_eps' else '元' for name in names})
    result.records.append((dataset+':'+key.identity,payload,key))
    return result


def fetch(family, selection, credential, *, endpoint=FUYAO_ENDPOINT, clock=None, before_request=None):
    if family not in FUYAO_FAMILIES or not 1 <= len(selection) <= 50:
        raise SourceFailure('invalid_instrument_batch')
    clock=clock or datetime.now(UTC)
    if family in {'fuyao_quotes','fuyao_indices'}:
        prefix='a-share-index' if family=='fuyao_indices' else 'a-share'
        response=request('/api/'+prefix+'/prices/snapshot',{'thscodes':','.join(code(key) for key in selection)},credential,
            endpoint=endpoint,before_request=before_request)
        return response, [(None,None,response.body)]
    output=[];responses=[]
    today=clock.astimezone(SHANGHAI).date()
    # Initial history is bounded to one year; no full-universe or ten-year dump.
    start=int(datetime.combine(today-timedelta(days=370),wall_time.min,SHANGHAI).timestamp()*1000)
    end=int(datetime.combine(today,wall_time.min,SHANGHAI).timestamp()*1000)
    for key in selection:
        if family in {'fuyao_daily','fuyao_index_daily'}:
            prefix='a-share-index' if family=='fuyao_index_daily' else 'a-share'
            params={'thscode':code(key),'interval':'1d','start':start,'end':end}
            if family=='fuyao_daily':params['adjust']='none'
            calls=[('/api/'+prefix+'/prices/historical',params,None)]
        elif family=='fuyao_adj_factor':
            calls=[('/api/a-share/corporate-actions/adjustment-factors',{'thscode':code(key),'to':today.isoformat()},None)]
        else:
            calls=[('/api/a-share/financials/'+statement,{'thscode':code(key),'period':'quarterly','limit':4},statement) for statement in STATEMENTS]
        for path,params,statement in calls:
            try:
                response=request(path,params,credential,endpoint=endpoint,before_request=before_request)
            except SourceFailure as exc:
                exc.request_count=len(responses)+getattr(exc,'request_count',0)
                raise
            responses.append(response);output.append((key,statement,response.body))
    # Body hash identifies a deterministic envelope; never store credentials.
    body=json.dumps([json.loads(row.body) for row in responses],separators=(',',':')).encode()
    return Response(body,max(row.received_at for row in responses),sum(row.elapsed_ms for row in responses),200,len(responses)),output
