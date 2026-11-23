"""Read-only vendor calls and strict tables for daily-review jobs.

No SDK, proxy, redirect, generated quotes, implicit host switching or price fill.
Source publication clocks remain distinct from this process's receipt clock.
"""
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from datetime import time as day_time
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

from app.market.protocol import SHANGHAI, NoRedirect, Response, SourceFailure
from app.market.review_protocol import INDEX_CODES, MINUTE_CODES, NEWS_PAGE_SIZE, ReviewPage
from app.market.tushare import table

MAX_BODY = 8 * 1024 * 1024
PUBLIC_HOSTS = frozenset({'ifzq.gtimg.cn', 'quotes.sina.cn', 'www.cls.cn', 'apphis.longhuvip.com'})
EMOTION_HISTORY_URL = 'https://apphis.longhuvip.com/w1/api/index.php?a=ChangeStatistics&st=120&c=HisHomeDingPan&apiv=w44&PhoneOSNew=1&Index=0'
API_BY_DATASET = {
    'stock_funds': 'moneyflow_dc', 'market_funds': 'moneyflow_mkt_dc',
    'pool_details':'limit_list_ths', 'group_basics':'daily_basic',
    'daily': 'daily', 'limits': 'stk_limit', 'pools': 'limit_list_d',
    'themes': 'dc_index', 'members': 'dc_member',
    'theme_funds': 'moneyflow_ind_dc', 'lhb': 'top_list', 'seats': 'top_inst',
    'indices': 'index_daily', 'calendar_sse': 'trade_cal', 'calendar_szse': 'trade_cal',
    'industry_themes': 'dc_index', 'industry_funds': 'moneyflow_ind_dc',
}
FIELDS = {
    'stock_funds': 'trade_date,ts_code,name,pct_change,close,net_amount',
    'market_funds': 'trade_date,net_amount',
    'industry_themes': 'trade_date,ts_code,name,pct_change,idx_type',
    'industry_funds': 'trade_date,ts_code,name,net_amount,content_type,pct_change',
    'daily': 'ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount',
    'limits': 'trade_date,ts_code,pre_close,up_limit,down_limit',
    'members': 'trade_date,ts_code,con_code,name',
    'seats': 'trade_date,ts_code,exalter,side,buy,buy_rate,sell,sell_rate,net_buy,reason',
    'indices': 'ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount',
    'calendar_sse': 'exchange,cal_date,is_open,pretrade_date',
    'calendar_szse': 'exchange,cal_date,is_open,pretrade_date',
}
REQUIRED = {
    'stock_funds': {'trade_date', 'ts_code', 'name', 'net_amount'},
    'market_funds': {'trade_date', 'net_amount'},
    'pool_details': {'trade_date','ts_code','price','status','tag','lu_desc','limit_amount','limit_order'},
    'group_basics': {'trade_date','ts_code','close','free_share','circ_mv','total_mv'},
    'industry_themes': {'trade_date', 'ts_code', 'name', 'pct_change', 'idx_type'},
    'industry_funds': {'trade_date', 'ts_code', 'name', 'net_amount', 'content_type'},
    'daily': {'ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'amount'},
    'limits': {'trade_date', 'ts_code', 'up_limit', 'down_limit'},
    'pools': {'trade_date', 'ts_code', 'limit', 'limit_times', 'fd_amount', 'amount'},
    'themes': {'trade_date', 'ts_code', 'name', 'pct_change'},
    'members': {'trade_date', 'ts_code', 'con_code', 'name'},
    'theme_funds': {'trade_date', 'ts_code', 'net_amount'},
    'lhb': {'trade_date', 'ts_code', 'reason', 'l_buy', 'l_sell', 'net_amount'},
    'seats': {'trade_date', 'ts_code', 'reason', 'exalter', 'side', 'buy', 'sell'},
    'indices': {'trade_date', 'ts_code', 'close', 'pre_close', 'pct_chg'},
    'calendar_sse': {'exchange', 'cal_date', 'is_open'},
    'calendar_szse': {'exchange', 'cal_date', 'is_open'},
}
DOCS = {
    'emotion_history': EMOTION_HISTORY_URL,
    'stock_funds': 'https://tushare.pro/document/2?doc_id=349',
    'market_funds': 'https://tushare.pro/document/2?doc_id=345',
    'pool_details':'https://tushare.pro/document/2?doc_id=355',
    'group_basics':'https://tushare.pro/document/2?doc_id=32',
    'industry_themes': 'https://tushare.pro/document/2?doc_id=362',
    'industry_funds': 'https://tushare.pro/document/2?doc_id=344',
    'daily': 'https://tushare.pro/document/2?doc_id=27',
    'limits': 'https://tushare.pro/document/2?doc_id=183',
    'pools': 'https://tushare.pro/document/2?doc_id=298',
    'themes': 'https://tushare.pro/document/2?doc_id=362',
    'members': 'https://tushare.pro/document/2?doc_id=363',
    'theme_funds': 'https://tushare.pro/document/2?doc_id=344',
    'lhb': 'https://tushare.pro/document/2?doc_id=106',
    'seats': 'https://tushare.pro/document/2?doc_id=107',
    'indices': 'https://tushare.pro/document/1?doc_id=95',
    'calendar_sse': 'https://tushare.pro/document/2?doc_id=26',
    'calendar_szse': 'https://tushare.pro/document/2?doc_id=26',
}


def parameters(context, *, offset=0):
    dataset = context['dataset']
    day = date.fromisoformat(context['day']).strftime('%Y%m%d')
    params = {'trade_date': day, 'limit': 2500 if dataset == 'pools' else 4000 if dataset == 'pool_details' else 5000, 'offset': offset}
    if dataset == 'pool_details':
        params['limit_type'] = '涨停池'
    if dataset.startswith('calendar_'):
        params = {
            'exchange': 'SSE' if dataset == 'calendar_sse' else 'SZSE',
            'start_date': context['start_day'].replace('-', ''),
            'end_date': context['day'].replace('-', ''), 'limit': 8000,
        }
    elif dataset == 'indices':
        params = {'ts_code': context['scope'], 'start_date': context['start_day'].replace('-', ''),
                  'end_date': day, 'limit': 8000}
    elif dataset in {'themes', 'industry_themes'}:
        params.update(idx_type='行业板块' if dataset == 'industry_themes' else '概念板块', limit=5000)
    elif dataset in {'theme_funds', 'industry_funds'}:
        params.update(content_type='行业' if dataset == 'industry_funds' else '概念', limit=5000)
    elif dataset == 'members':
        params.update(limit=5000, offset=offset)
        if context['scope'] != 'all':
            params['ts_code'] = context['scope']
    return API_BY_DATASET[dataset], params, FIELDS.get(dataset, '')


def validate_table(body, context, *, allow_empty=False):
    dataset = context['dataset']
    fields, rows = table(body, allow_empty_columns=True)
    required = REQUIRED[dataset]
    if not rows and not allow_empty and (dataset in {'daily', 'indices', 'themes', 'limits'} or dataset.startswith('calendar_')):
        raise SourceFailure('review_required_table_empty')
    if not rows and not fields:
        if not allow_empty and (dataset in {'daily', 'indices', 'themes', 'limits'} or dataset.startswith('calendar_')):
            raise SourceFailure('review_required_table_empty')
        fields = sorted(required)
    if not required.issubset(fields):
        raise SourceFailure('schema_changed')
    target = context['day'].replace('-', '')
    earliest = context.get('start_day', context['day']).replace('-', '')
    seen = set()
    for row in rows:
        if dataset.startswith('calendar_'):
            expected_exchange = 'SSE' if dataset == 'calendar_sse' else 'SZSE'
            if row['exchange'] != expected_exchange or row['is_open'] not in {'0', '1'}:
                raise SourceFailure('review_calendar_invalid')
            source_day = row['cal_date']
            identity = source_day
        else:
            source_day = row['trade_date']
            code = row.get('ts_code')
            pattern = r'BK\d{4,8}\.DC' if dataset in {'themes', 'members', 'theme_funds', 'industry_themes', 'industry_funds'} else r'\d{6}\.(?:SH|SZ|BJ)'
            if dataset == 'indices':
                if code != context['scope'] or code not in INDEX_CODES:
                    raise SourceFailure('instrument_mismatch')
            elif dataset != 'market_funds' and (not isinstance(code, str) or not re.fullmatch(pattern, code)):
                raise SourceFailure('instrument_mismatch')
            identity = (source_day, code)
            if dataset in {'industry_themes', 'industry_funds'}:
                field = 'idx_type' if dataset == 'industry_themes' else 'content_type'
                if row.get(field) not in {'行业', '行业板块'}:
                    raise SourceFailure('unexpected_sector_type')
            if dataset == 'members':
                if not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)', row['con_code'] or ''):
                    raise SourceFailure('instrument_mismatch')
                if context['scope'] != 'all' and code != context['scope']:
                    raise SourceFailure('review_scope_mismatch')
                identity += (row['con_code'],)
            elif dataset == 'lhb':
                # The feed does not expose a unique disclosure/period ID.
                # Repeated code/reason rows may represent distinct disclosures;
                # preserve them and let the read model mark ambiguity.
                identity = None
            elif dataset == 'seats':
                if row['side'] not in {'0', '1'}:
                    raise SourceFailure('schema_changed')
                # Same-labelled institutional seats are distinct source rows.
                identity = None
            elif dataset == 'pools' and row['limit'] not in {'U', 'D', 'Z'}:
                raise SourceFailure('schema_changed')
        if not isinstance(source_day, str) or not re.fullmatch(r'\d{8}', source_day):
            raise SourceFailure('invalid_source_time')
        try:
            datetime.strptime(source_day, '%Y%m%d')
        except ValueError:
            raise SourceFailure('invalid_source_time') from None
        if source_day < earliest or source_day > target or (
            dataset not in {'indices', 'calendar_sse', 'calendar_szse'} and source_day != target
        ):
            raise SourceFailure('review_response_out_of_range')
        if identity is not None:
            if identity in seen:
                raise SourceFailure('review_duplicate_row')
            seen.add(identity)
    if len(rows) >= 8000:
        raise SourceFailure('review_response_truncated')
    return ReviewPage(fields, rows, DOCS[dataset], hashlib.sha256(body).hexdigest(),
                      context['scope'])


def fetch_public(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in PUBLIC_HOSTS or parts.port not in {None, 443} or parts.username or parts.password or parts.fragment:
        raise SourceFailure('endpoint_not_allowed')
    referer = ('https://gu.qq.com/' if parts.hostname == 'ifzq.gtimg.cn' else
               'https://finance.sina.com.cn/' if parts.hostname == 'quotes.sina.cn' else 'https://www.cls.cn/')
    req = urllib.request.Request(url, headers={'User-Agent': 'Dalvik/2.1.0 (Linux; U; Android 12)' if parts.hostname == 'apphis.longhuvip.com' else 'Mozilla/5.0 (Tianyan private research)',
                                             'Referer': referer, 'Accept-Encoding': 'identity'})
    started = time.monotonic()
    try:
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=15) as response:
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise SourceFailure('response_too_large')
            return Response(raw, datetime.now(UTC), int((time.monotonic() - started) * 1000), response.status)
    except urllib.error.HTTPError as exc:
        reason = 'permission_missing' if exc.code in {401, 403} else 'rate_limited' if exc.code == 429 else 'upstream_unavailable'
        retry_after = None
        if exc.code == 429:
            value = exc.headers.get('Retry-After', '')
            try:
                retry_after = max(1, int(value)) if value.isdecimal() else max(1, int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()) + 1)
            except (TypeError, ValueError, OverflowError):
                retry_after = 60
        raise SourceFailure(reason, retry_after=retry_after) from None
    except TimeoutError:
        raise SourceFailure('timeout') from None
    except (OSError, urllib.error.URLError):
        raise SourceFailure('transport_error') from None


def emotion_history_page(body, context):
    """Native daily history, never a date parameter on the live endpoint."""
    from app.market.direction_sources import _unpack
    try:
        value = _unpack('direction_kpl_emotion', body)
        info = value['info']
        if str(value.get('errcode')) != '0' or not isinstance(info, list) or not 1 <= len(info) <= 120:
            raise ValueError()
        rows, seen = [], set()
        first, last = context['start_day'], context['day']
        for record in info:
            day = date.fromisoformat(record['Day']).isoformat()
            score = Decimal(str(record['strong']))
            if day in seen or not score.is_finite() or not 0 <= score <= 100:
                raise ValueError()
            seen.add(day)
            if first <= day <= last:
                rows.append({'trade_date': day.replace('-', ''), 'emotion': str(score)})
        if last not in seen:
            raise SourceFailure('daily_data_not_published')
        return ReviewPage(['trade_date', 'emotion'], rows, EMOTION_HISTORY_URL,
                          hashlib.sha256(body).hexdigest(), '原生开盘啦日情绪；补采时间另存，不回填盘中曲线。')
    except SourceFailure:
        raise
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise SourceFailure('schema_changed') from None


def minute_url(dataset, code):
    if code not in MINUTE_CODES:
        raise SourceFailure('instrument_mismatch')
    symbol = INDEX_CODES[code][0]
    if dataset == 'tencent_minutes':
        return f'https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={symbol},m1,,320'
    if dataset == 'sina_minutes':
        return 'https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData?' + urllib.parse.urlencode({
            'symbol': symbol, 'scale': 1, 'ma': 'no', 'datalen': 1970,
        })
    raise SourceFailure('endpoint_not_allowed')


def minute_page(response, dataset, code):
    fields = ['ts_code', 'trade_time', 'open', 'high', 'low', 'close', 'volume_raw', 'amount_raw']
    rows = []
    try:
        body = response.body.decode('utf-8')
        if dataset == 'tencent_minutes':
            value = json.loads(body)
            if value.get('code') != 0:
                raise SourceFailure('upstream_unavailable')
            values = value['data'][INDEX_CODES[code][0]]['m1']
            if len(values) > 320:
                raise SourceFailure('response_too_large')
            for v in values:
                stamp = datetime.strptime(str(v[0]), '%Y%m%d%H%M').replace(tzinfo=SHANGHAI)
                # The eighth field is not verified index turnover.
                rows.append(dict(zip(fields, [code, stamp.isoformat(), str(v[1]), str(v[3]),
                                              str(v[4]), str(v[2]), str(v[5]), None], strict=True)))
        else:
            # The response is JSONP. Only decode the JSON array; never execute it.
            start, end = body.index('['), body.rindex(']') + 1
            values = json.loads(body[start:end])
            if len(values) > 1970:
                raise SourceFailure('response_too_large')
            for v in values:
                stamp = datetime.fromisoformat(v['day']).replace(tzinfo=SHANGHAI)
                rows.append(dict(zip(fields, [code, stamp.isoformat(), str(v['open']), str(v['high']),
                                              str(v['low']), str(v['close']),
                                              str(v['volume']) if v.get('volume') is not None else None,
                                              str(v['amount']) if v.get('amount') is not None else None], strict=True)))
        seen = set()
        for row in rows:
            stamp = datetime.fromisoformat(row['trade_time'])
            clock = stamp.timetz().replace(tzinfo=None)
            if stamp > response.received_at or stamp.weekday() >= 5 or not (
                day_time(9, 30) <= clock <= day_time(11, 30) or day_time(13, 1) <= clock <= day_time(15)
            ):
                raise SourceFailure('invalid_source_time')
            prices = [Decimal(row[k]) for k in ['open', 'high', 'low', 'close']]
            op, hi, lo, cl = prices
            if not all(p.is_finite() and p > 0 for p in prices) or not lo <= min(op, cl) <= max(op, cl) <= hi:
                raise SourceFailure('inconsistent_price_range')
            if row['trade_time'] in seen:
                raise SourceFailure('review_duplicate_row')
            seen.add(row['trade_time'])
        if not rows:
            raise SourceFailure('instrument_unavailable')
        return ReviewPage(fields, sorted(rows, key=lambda r: r['trade_time']),
                          minute_url(dataset, code), hashlib.sha256(response.body).hexdigest(), code)
    except SourceFailure:
        raise
    except (ValueError, KeyError, TypeError, IndexError, InvalidOperation, AttributeError):
        raise SourceFailure('schema_changed') from None


def cls_url(before):
    params = {'appName': 'CailianpressWeb', 'os': 'web', 'sv': '7.7.5',
              'last_time': str(before), 'refresh_type': '1', 'rn': str(NEWS_PAGE_SIZE)}
    query = '&'.join(f'{key}={params[key]}' for key in sorted(params))
    signature = hashlib.md5(hashlib.sha1(query.encode()).hexdigest().encode()).hexdigest()
    return 'https://www.cls.cn/v1/roll/get_roll_list?' + query + '&sign=' + signature


def news_page(response, target_day):
    fields = ['id', 'published_at', 'modified_at', 'title', 'summary', 'source_url',
              'stock_codes', 'text_truncated']
    try:
        obj = json.loads(response.body)
        if obj.get('errno') != 0:
            raise SourceFailure('upstream_unavailable')
        items = (obj.get('data') or {}).get('roll_data')
        if not isinstance(items, list) or len(items) > NEWS_PAGE_SIZE:
            raise SourceFailure('schema_changed')
        rows, timestamps, seen = [], [], set()
        for value in items:
            if not isinstance(value.get('ctime'), int) or not value.get('id'):
                raise SourceFailure('invalid_source_time')
            stamp = datetime.fromtimestamp(value['ctime'], SHANGHAI)
            if stamp > response.received_at:
                raise SourceFailure('future_source_time')
            timestamps.append(value['ctime'])
            if stamp.date() != target_day:
                continue
            def clean(v):
                return html.unescape(re.sub(r'<[^>]+>', '', str(v or ''))).replace('\0', '')
            title = clean(value.get('title') or value.get('brief') or value.get('content'))
            summary = clean(value.get('brief') or value.get('content'))
            modified = value.get('modified_time')
            modified_at = datetime.fromtimestamp(modified, SHANGHAI).isoformat() if isinstance(modified, int) and modified > 0 else None
            ident = str(value['id'])
            if not re.fullmatch(r'\d{1,30}', ident) or not title:
                raise SourceFailure('schema_changed')
            if ident in seen:
                raise SourceFailure('review_duplicate_row')
            seen.add(ident)
            stocks = value.get('stock_list') or []
            stock_codes = [str(r.get('stock_id') or r.get('symbol') or r.get('code') or '')
                           for r in stocks if isinstance(r, dict)]
            rows.append(dict(zip(fields, [ident, stamp.isoformat(), modified_at, title[:200],
                                          summary[:500], 'https://www.cls.cn/detail/' + ident,
                                          ','.join(stock_codes)[:500],
                                          str(len(title) > 200 or len(summary) > 500).lower()], strict=True)))
        page = ReviewPage(fields, rows, 'https://www.cls.cn/telegraph',
                          hashlib.sha256(response.body).hexdigest(), target_day.isoformat())
        return page, min(timestamps) if timestamps else None, len(items)
    except SourceFailure:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise SourceFailure('schema_changed') from None
