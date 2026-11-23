"""Original parser for the vendor's dated hundred-day-high tables.

Native membership, primary group and new flags are retained. Counts close
against the independent native history endpoint before a batch is published.
No account, copied client code or client credentials are involved.
"""
import hashlib
import json
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from app.market.protocol import SourceFailure
from app.market.review_protocol import ReviewPage

FAMILY = 'review_kpl_new_highs'
BASE = 'https://apphis.longhuvip.com/w1/api/index.php'
PAGE_SIZE = 1000
MAX_PAGES = 8
HISTORY_SESSIONS = 60
FIELDS = ['kind', 'trade_date', 'ts_code', 'name', 'price', 'pct_chg', 'themes',
          'amount', 'main_net', 'main_buy', 'main_sell', 'actual_float', 'total_cap',
          'is_new', 'native_group_id', 'group_name', 'actual_turnover', 'total', 'new_count']


def source_url(action, day=None, index=0):
    params = dict(a=action, c='StockNewHigh', PhoneOSNew=1,
                  DeviceID=str(uuid.uuid4()), apiv='w42', VerSion='5.21.0.2',
                  Type='0_0_0_0_0', Index=index, st=360 if action == 'GetDayNewHigh_W28' else PAGE_SIZE)
    if day is not None:
        params['Date'] = day
    if action == 'GetDayNewHigh_W28':
        params['GroupID'] = 'ALL'
    return BASE + '?' + urlencode(params)


def unpack(body):
    try:
        obj = json.loads(body)
        if not isinstance(obj, dict) or str(obj.get('errcode')) != '0':
            raise SourceFailure('upstream_unavailable')
        return obj
    except (ValueError, TypeError):
        raise SourceFailure('schema_changed') from None


def integer(value):
    if isinstance(value, bool) or not re.fullmatch(r'\d{1,6}', str(value)):
        raise SourceFailure('schema_changed')
    return int(value)


def numeric(value, *, nonnegative=False, positive=False):
    try:
        if value is None or isinstance(value, bool):
            raise ValueError()
        number = Decimal(str(value))
        if not number.is_finite() or nonnegative and number < 0 or positive and number <= 0:
            raise ValueError()
        return str(number)
    except (ValueError, InvalidOperation):
        raise SourceFailure('schema_changed') from None


def history_rows(body):
    items = unpack(body).get('x')
    if not isinstance(items, list) or len(items) > 400:
        raise SourceFailure('schema_changed')
    rows, seen = [], set()
    for item in items:
        if not isinstance(item, str) or not re.fullmatch(r'\d{8}_\d+_\d+_\d+', item):
            raise SourceFailure('schema_changed')
        day, total, new, _ = item.split('_')
        try:
            date.fromisoformat(day)
        except ValueError:
            raise SourceFailure('invalid_source_time') from None
        total, new = integer(total), integer(new)
        if day in seen or new > total:
            raise SourceFailure('review_duplicate_row' if day in seen else 'schema_changed')
        seen.add(day)
        rows.append(dict(kind='summary', trade_date=day, total=str(total), new_count=str(new)))
    return sorted(rows, key=lambda r: r['trade_date'])


def history_page(body, context, url):
    first, last = context['start_day'].replace('-', ''), context['day'].replace('-', '')
    rows = [r for r in history_rows(body) if first <= r['trade_date'] <= last]
    if not rows or rows[0]['trade_date'] > first or rows[-1]['trade_date'] != last:
        raise SourceFailure('review_highs_day_unavailable')
    return ReviewPage(['kind', 'trade_date', 'total', 'new_count'], rows, url,
                      hashlib.sha256(body).hexdigest(), '开盘啦原生百日新高历史；数量与每日新增为来源原值。')


def group_rows(body, day):
    obj = unpack(body)
    if obj.get('Date') != day:
        raise SourceFailure('review_highs_day_mismatch')
    groups = obj.get('GroupList')
    if not isinstance(groups, list) or integer(obj.get('GroupCount')) != len(groups):
        raise SourceFailure('schema_changed')
    rows, codes, group_ids = [], set(), set()
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('List'), list):
            raise SourceFailure('schema_changed')
        gid = str(integer(group.get('GroupID')))
        if gid in group_ids or len(group['List']) > PAGE_SIZE:
            raise SourceFailure('review_duplicate_row' if gid in group_ids else 'schema_changed')
        group_ids.add(gid)
        for item in group['List']:
            if not isinstance(item, list) or len(item) != 15:
                raise SourceFailure('schema_changed')
            code, name = item[:2]
            if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code) or code in codes:
                raise SourceFailure('review_duplicate_row' if code in codes else 'schema_changed')
            if not isinstance(name, str) or not name.strip() or not isinstance(item[4], str) or not isinstance(item[13], str) or not item[13].strip():
                raise SourceFailure('schema_changed')
            flag = integer(item[11])
            if flag not in {0, 1}:
                raise SourceFailure('schema_changed')
            exchange = 'SH' if code.startswith(('6','5')) else 'BJ' if code.startswith(('4','8','9')) else 'SZ'
            values = [numeric(item[2], positive=True), numeric(item[3]), item[4].strip(),
                      *[numeric(item[i], nonnegative=i in {5, 7, 9, 10}) for i in range(5, 11)],
                      str(flag), str(item[12]), item[13].strip(), numeric(item[14], nonnegative=True)]
            codes.add(code)
            rows.append(dict(zip(FIELDS[:17], ['stock', day.replace('-', ''), code+'.'+exchange, name.strip(), *values], strict=True)))
    return rows


def stocks_page(bodies, history_body, day, url):
    summaries = [r for r in history_rows(history_body) if r['trade_date'] == day.replace('-', '')]
    if len(summaries) != 1:
        raise SourceFailure('review_highs_day_unavailable')
    summary = summaries[0]
    rows = [r for body in bodies for r in group_rows(body, day)]
    codes = {r['ts_code'] for r in rows}
    if len(codes) != len(rows):
        raise SourceFailure('review_duplicate_row')
    if len(rows) != int(summary['total']) or sum(int(r['is_new']) for r in rows) != int(summary['new_count']):
        raise SourceFailure('review_highs_incomplete')
    digest = hashlib.sha256(b''.join(bodies)+history_body).hexdigest()
    normalized = [{key:row.get(key) for key in FIELDS} for row in [summary, *rows]]
    return ReviewPage(FIELDS, normalized, url, digest,
                      '逐日原生完整名单；独立历史接口核对总数和新增，金额元、实际流通市值元、实际换手%。')
