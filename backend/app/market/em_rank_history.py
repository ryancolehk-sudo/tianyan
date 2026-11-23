"""Fixed historical daily rank endpoint; never requests a current-list feed."""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime

from app.market.protocol import NoRedirect, Response, SHANGHAI, SourceFailure

URL = 'https://emappdata.eastmoney.com/stockrank/getHisList'
MAX_BODY = 2_000_000


def fetch(code):
    if not isinstance(code, str) or not re.fullmatch(r'(?:SH|SZ)\d{6}', code):
        raise SourceFailure('instrument_mismatch')
    body=json.dumps({'appId':'appId01','globalId':'786e4c21-70dc-435a-93bb-38',
        'marketType':'','srcSecurityCode':code,'yearType':'5'}).encode()
    request=urllib.request.Request(URL,data=body,headers={'Content-Type':'application/json',
        'Accept-Encoding':'identity','User-Agent':'Tianyan/1.0 (private research)'})
    started=time.monotonic()
    try:
        with urllib.request.build_opener(NoRedirect(),urllib.request.ProxyHandler({})).open(request,timeout=15) as response:
            raw=response.read(MAX_BODY+1)
            if len(raw)>MAX_BODY:
                raise SourceFailure('response_too_large')
            return Response(raw,datetime.now(UTC),int((time.monotonic()-started)*1000),response.status)
    except urllib.error.HTTPError as error:
        raise SourceFailure('rate_limited' if error.code==429 else 'upstream_unavailable') from None
    except (TimeoutError,OSError,urllib.error.URLError):
        raise SourceFailure('transport_error') from None


def parse(body, code, received_at):
    if (not isinstance(code,str) or not re.fullmatch(r'(?:SH|SZ)\d{6}',code)
            or received_at.utcoffset() is None or len(body)>MAX_BODY):
        raise SourceFailure('instrument_mismatch')
    try:
        value=json.loads(body)
        if type(value.get('code')) is not int or value['code']!=0:
            raise SourceFailure('upstream_unavailable')
        rows=value.get('data')
        if not isinstance(rows,list) or len(rows)>400:
            raise ValueError()
        seen=set();result=[]
        for index,row in enumerate(rows):
            if not isinstance(row,dict) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(row.get('calcTime',''))):
                raise ValueError()
            day=date.fromisoformat(row['calcTime'])
            rank=row.get('rank')
            if type(rank) is not int or not 1<=rank<=1000000 or day in seen or day>received_at.astimezone(SHANGHAI).date():
                raise ValueError()
            seen.add(day)
            result.append(dict(code=code[2:],trade_date=day.isoformat(),rank=rank,source_row=index))
        return result
    except SourceFailure:
        raise
    except (ValueError,TypeError,KeyError,UnicodeError,AttributeError):
        raise SourceFailure('schema_changed') from None


def top_ten(histories, day, previous_day):
    """Global source ranks 1..10 prove coverage; a candidate subset cannot."""
    ranked=[]
    for code,entry in histories.items():
        matches=[row for row in entry['rows'] if row['trade_date']==day.isoformat()]
        if len(matches)!=1 or matches[0]['rank']>10:
            continue
        current=matches[0]
        previous=[row for row in entry['rows'] if previous_day and row['trade_date']==previous_day.isoformat()]
        ranked.append(dict(rank=str(current['rank']),code=code,name=entry['name'],source_day=day.isoformat(),
            source_time=None,rank_change=None,rank_change_previous_trading_day=
                str(previous[0]['rank']-current['rank']) if len(previous)==1 else None,
            rank_comparison_day=previous_day.isoformat() if previous_day else None,
            source_history_sha256=entry['body_sha256'],source_history_row=str(current['source_row'])))
    ranked.sort(key=lambda row:int(row['rank']))
    if len(ranked)!=10 or [int(row['rank']) for row in ranked]!=list(range(1,11)) or len({row['code'] for row in ranked})!=10:
        return None
    return ranked
