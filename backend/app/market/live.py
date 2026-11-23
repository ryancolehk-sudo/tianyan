"""Documented free tiers only; credentials never appear in URLs or errors."""
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.market.protocol import MAX_BODY, SHANGHAI, NoRedirect, Quote, QuoteBatch, Response, SourceFailure, decimal_value

SUFFIXES={'SSE':'SH','SZSE':'SZ','BSE':'BJ'}


@dataclass(frozen=True)
class DelayedQuote(Quote):
    trade_day: date | None = None
    quote_kind: str = 'delayed_reference'


def selected_symbols(selection, *, tickflow=False):
    if not 1<=len(selection)<=5 or len({key.vendor_key for key in selection})!=len(selection):
        raise SourceFailure('invalid_instrument_batch')
    return ','.join(key.symbol+'.'+SUFFIXES[key.exchange] if tickflow else key.vendor_key for key in selection)


def fetch_tickflow(selection, credential):
    if credential is None:
        raise SourceFailure('credentials_missing')
    url='https://api.tickflow.org/v1/quotes?'+urllib.parse.urlencode({'symbols':selected_symbols(selection,tickflow=True)})
    token=credential.get_secret_value()
    if not 8<=len(token)<=512 or any(ord(c)<33 or ord(c)>126 for c in token):
        raise SourceFailure('credentials_invalid')
    request=urllib.request.Request(url,headers={'x-api-key':token,'User-Agent':'Tianyan/1.0 (private research)','Accept':'application/json'})
    opener=urllib.request.build_opener(NoRedirect())
    start=time.monotonic()
    try:
        with opener.open(request,timeout=10) as response:
            raw=response.read(MAX_BODY+1)
            if len(raw)>MAX_BODY:raise SourceFailure('response_too_large')
            return Response(raw,datetime.now(UTC),round((time.monotonic()-start)*1000),response.status)
    except urllib.error.HTTPError as error:
        if error.code in {401,403}:raise SourceFailure('permission_missing') from None
        if error.code==429:
            after=error.headers.get('Retry-After','')
            raise SourceFailure('rate_limited',retry_after=min(int(after),86400) if after.isdecimal() and len(after)<9 else 60) from None
        raise SourceFailure('upstream_unavailable') from None
    except (TimeoutError,socket.timeout):raise SourceFailure('timeout') from None
    except (OSError,urllib.error.URLError):raise SourceFailure('transport_error') from None


def number(value, *, positive=False):
    if value is None:return None
    if type(value) not in {str,int,Decimal}:raise SourceFailure('invalid_number')
    return decimal_value(format(value,'f') if isinstance(value,Decimal) else str(value),positive=positive)


def envelope(body):
    def invalid_constant(_):raise ValueError('invalid JSON number')
    try:
        value=json.loads(body,parse_float=Decimal,parse_constant=invalid_constant)
        if not isinstance(value,dict) or not isinstance(value.get('data'),list):raise ValueError()
        return value
    except (ValueError,UnicodeError):raise SourceFailure('schema_changed') from None


def price_fields(row, names):
    price,previous,opening,high,low=(number(row.get(key),positive=True) if key else None for key in names)
    if high is not None and low is not None and (high<low or any(v is not None and not low<=v<=high for v in [price,opening])):
        raise SourceFailure('inconsistent_price_range')
    return dict(price=price,previous_close=previous,open=opening,high=high,low=low)


def parse_tickflow(body, selection, received_at):
    if received_at.tzinfo is None:raise SourceFailure('invalid_received_time')
    selected_symbols(selection,tickflow=True)
    expected={key.symbol+'.'+SUFFIXES[key.exchange]:key for key in selection}
    rows=envelope(body)['data']
    if len(rows)>5:raise SourceFailure('unexpected_instrument')
    batch=QuoteBatch(expected=len(expected));seen=set()
    for row in rows:
        if not isinstance(row,dict):raise SourceFailure('schema_changed')
        symbol=row.get('symbol')
        if symbol not in expected or symbol in seen:raise SourceFailure('unexpected_instrument')
        seen.add(symbol);key=expected[symbol]
        try:
            if row.get('region')!='CN':raise SourceFailure('instrument_mismatch')
            stamp=row.get('timestamp')
            if stamp is not None and (type(stamp) is not int or not 946684800000<=stamp<=4102444800000):raise SourceFailure('invalid_source_time')
            at=datetime.fromtimestamp(stamp/1000,UTC) if stamp is not None else None
            if at is not None and at>received_at+timedelta(seconds=60):raise SourceFailure('future_source_time')
            values=price_fields(row,['last_price','prev_close','open','high','low'])
            batch.records.append(Quote(instrument=key,name=key.symbol,quote_time=at,received_at=received_at,
                price_unit='point' if key.asset_type=='index' else 'CNY',**values))
        except SourceFailure as error:batch.rejected[symbol]=error.reason
    for symbol in expected.keys()-seen:batch.rejected[symbol]='instrument_missing'
    return batch


def ashare_url(selection):
    return 'https://api.ashareapi.com/v1/quote?'+urllib.parse.urlencode({'code':selected_symbols(selection)})


def parse_ashare(body,selection,received_at):
    if received_at.tzinfo is None:raise SourceFailure('invalid_received_time')
    selected_symbols(selection);value=envelope(body)
    if value.get('ok') is not True:raise SourceFailure('upstream_unavailable')
    if value.get('endpoint')!='quote' or value.get('tier')!='free':raise SourceFailure('schema_changed')
    rows=value['data']
    if len(rows)>150:raise SourceFailure('response_too_large')
    expected={key.vendor_key:key for key in selection};latest={};seen=set();batch=QuoteBatch(expected=len(expected))
    for row in rows:
        if not isinstance(row,dict) or row.get('symbol') not in expected:raise SourceFailure('unexpected_instrument')
        symbol=row['symbol']
        try:
            day=date.fromisoformat(row['date'])
            if day>received_at.astimezone(SHANGHAI).date():raise SourceFailure('future_source_time')
            if (symbol,day) in seen:raise SourceFailure('duplicate_source_day')
            seen.add((symbol,day))
            # Validate every row, but never use a historical row to fill a live gap.
            values=price_fields(row,['last',None,'open','high','low'])
            if symbol not in latest or latest[symbol][0]<day:latest[symbol]=(day,values)
        except (KeyError,TypeError,ValueError):batch.rejected[symbol]='schema_changed'
        except SourceFailure as error:batch.rejected[symbol]=error.reason
    for symbol,key in expected.items():
        if symbol in batch.rejected:continue
        if symbol not in latest:batch.rejected[symbol]='instrument_missing';continue
        day,values=latest[symbol]
        batch.records.append(DelayedQuote(instrument=key,name=key.symbol,quote_time=None,received_at=received_at,trade_day=day,
            price_unit='point' if key.asset_type=='index' else 'CNY',**values))
    return batch
