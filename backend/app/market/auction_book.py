"""Bounded public L1 auction trial. Never infer a book from a trade/bar.

Both sides use the same dated Tencent response. The existing seal-table feed
is untouched. Each immutable batch includes its actual catalog/clock coverage.
"""
import re
import time as timer
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from app.evidence.common import content_hash
from app.evidence.schemas import ReferencePayload
from app.market.protocol import SHANGHAI, SourceFailure, fetch_https
from app.market.reference import ReferenceBatch

FAMILY = 'tencent_auction'
DATASET = 'market.auction.book'
PAGE_SIZE = 500
MAX_UNIVERSE = 8000
MAX_SECONDS = 50
BOOK_FIELDS = ('bid_price1', 'bid_volume1', 'ask_price1', 'ask_volume1')
FIELDS = ('row_type', 'ts_code', 'name', 'close', 'pre_close', 'up_limit', 'down_limit',
          *BOOK_FIELDS, 'trade_time', 'requested_count', 'returned_count', 'usable_count',
          'universe_count', 'universe_hash', 'cycle_started_at')
UNITS = {key: '元' for key in ('close', 'pre_close', 'up_limit', 'down_limit', 'bid_price1', 'ask_price1')}
UNITS.update(bid_volume1='股', ask_volume1='股')


def decimal(value):
    if value in (None, '', '-', '--'):
        return None
    try:
        if isinstance(value, bool): raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > Decimal('1e16'): raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise SourceFailure('invalid_number') from None


def seal(row, side):
    own, other, limit = ('bid', 'ask', 'up_limit') if side == 'up' else ('ask', 'bid', 'down_limit')
    price, bound = decimal(row.get(own+'_price1')), decimal(row.get(limit))
    volume, opposing = decimal(row.get(own+'_volume1')), decimal(row.get(other+'_volume1'))
    if price is None or bound is None or bound <= 0 or price != bound or volume is None or opposing != 0:
        return None
    return float(price * volume)


def parse_page(body, selection, received_at, day):
    if len(body) > 1_000_000 or not 1 <= len(selection) <= PAGE_SIZE:
        raise SourceFailure('invalid_instrument_batch')
    expected = {key.vendor_key: key for key in selection}
    if len(expected) != len(selection) or any(key.asset_type != 'stock' for key in selection):
        raise SourceFailure('invalid_instrument_batch')
    try:
        lines = body.decode('gb18030').strip().splitlines()
    except UnicodeError:
        raise SourceFailure('schema_changed') from None
    seen, candidates, usable, latest = set(), [], 0, None
    for line in lines:
        match = re.fullmatch(r'\s*v_((?:sh|sz|bj)\d{6})="([^"\r\n]*)";?\s*', line)
        if not match: raise SourceFailure('schema_changed')
        vendor, raw = match.groups()
        if vendor not in expected or vendor in seen: raise SourceFailure('unexpected_instrument')
        seen.add(vendor)
        cells = raw.split('~')
        if len(cells) < 49: continue
        key = expected[vendor]
        if cells[2] != key.symbol: raise SourceFailure('instrument_mismatch')
        if not re.fullmatch(r'\d{14}', cells[30]): continue
        try:
            stamp = datetime.strptime(cells[30], '%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI)
        except ValueError: raise SourceFailure('invalid_source_time') from None
        if stamp > received_at + timedelta(seconds=60): raise SourceFailure('future_source_time')
        # Friday's last quote on a weekend, 09:30 data, and old cached books
        # cannot be labelled as this day's auction, even if HTTP returned 200.
        if (stamp.date() != day or not time(9, 15) <= stamp.time() <= time(9, 25)
                or not timedelta(0) <= received_at-stamp <= timedelta(seconds=90)):
            continue
        name = cells[1].strip()
        if not name or len(name) > 200 or any(ord(c) < 32 for c in name):
            raise SourceFailure('invalid_name')
        values = {field: decimal(cells[index]) for field, index in
                  (('close', 3), ('pre_close', 4), ('bid_price1', 9), ('bid_volume1', 10),
                   ('ask_price1', 19), ('ask_volume1', 20))}
        if any(values[field] is None for field in BOOK_FIELDS): continue
        for field in ('bid_volume1', 'ask_volume1'):
            values[field] *= 100  # Native qt lots -> integral shares.
            if values[field] != values[field].to_integral_value(): raise SourceFailure('invalid_number')
        if values['bid_volume1']==values['ask_volume1']==0: continue
        if any(values[side+'_volume1']>0 and values[side+'_price1']<=0 for side in ('bid','ask')): continue
        for field, index in (('up_limit', 47), ('down_limit', 48)):
            values[field] = None if cells[index] in ('-1', '', '-', '--') else decimal(cells[index])
        if all(values[k] is not None and values[k] > 0 for k in ('up_limit', 'down_limit')) and values['up_limit'] < values['down_limit']:
            raise SourceFailure('inconsistent_price_range')
        usable += 1
        latest = max(latest, stamp) if latest else stamp
        row = {key: format(value, 'f') if value is not None else None for key, value in values.items()}
        if not any((value := seal(row, side)) is not None and value > 0 for side in ('up', 'down')):
            continue
        row.update(row_type='stock', ts_code=key.symbol+'.'+{'SSE':'SH','SZSE':'SZ','BSE':'BJ'}[key.exchange],
                   name=name, trade_time=stamp.isoformat())
        candidates.append({field: row.get(field) for field in FIELDS})
    return candidates, len(seen), usable, latest


def fetch_books(selection, started_at, before_request, *, offset=0, read=None, monotonic=timer.monotonic):
    read = read or fetch_https
    if not 1 <= len(selection) <= MAX_UNIVERSE or len({key.identity for key in selection}) != len(selection):
        raise SourceFailure('invalid_instrument_batch')
    if offset < 0 or offset >= len(selection) or offset % PAGE_SIZE:
        offset = 0
    scope = content_hash([key.identity for key in selection])
    pages = [selection[i:i+PAGE_SIZE] for i in range(0, len(selection), PAGE_SIZE)]
    first = offset // PAGE_SIZE
    indexes = list(range(first, len(pages))) + list(range(first))
    begun, requests, returned, usable, requested, rows, elapsed = monotonic(), 0, 0, 0, 0, [], 0
    received, latest = started_at, None
    next_offset = offset
    for index in indexes:
        if requests and monotonic()-begun >= MAX_SECONDS: break
        if before_request() is False: raise SourceFailure('request_not_permitted')
        page = pages[index]
        try:
            requests += 1
            response = read('https://qt.gtimg.cn/q='+','.join(key.vendor_key for key in page))
            received = max(received, response.received_at)
            elapsed += response.elapsed_ms
            candidates, count, valid, stamp = parse_page(response.body, page, response.received_at, started_at.astimezone(SHANGHAI).date())
            if stamp: latest = max(latest, stamp) if latest else stamp
        except SourceFailure as exc:
            exc.request_count = requests
            raise
        rows.extend(candidates); returned += count; usable += valid; requested += len(page)
        next_offset = ((index+1) % len(pages))*PAGE_SIZE
        if received.astimezone(SHANGHAI).time() > time(9, 25, 30): break
    if not usable:
        error = SourceFailure('auction_book_unavailable'); error.request_count = requests; raise error
    coverage = {field: None for field in FIELDS}
    coverage.update(row_type='coverage', requested_count=str(requested), returned_count=str(returned),
        usable_count=str(usable), universe_count=str(len(selection)), universe_hash=scope, cycle_started_at=started_at.isoformat(),
        trade_time=latest.isoformat() if latest else None)
    payload = ReferencePayload(dataset=DATASET, title='腾讯公开竞价盘口试采',
        source_url='https://gu.qq.com/', attribution='腾讯公开网页L1买卖盘；本机个人试采',
        scope='有权股票目录分包实际返回；覆盖行记录请求/返回/有效同日盘口数。仅留存原生限价处单边挂单，'
              '双向均有量、缺单位、旧日期、过期及竞价结束后的行情均不生成封单；不代表逐笔、L2或完整委托队列。'
              '涨停与跌停同批独立保存；不替换短线侠原表。',
        columns=[{'key':field,'label':field,'unit':UNITS.get(field)} for field in FIELDS], rows=[coverage, *rows],
        observed_day=started_at.astimezone(SHANGHAI).date())
    batch = ReferenceBatch(records=[('auction-book:'+str(payload.observed_day), payload, None)],
        expected=1, rejected=int(usable != len(selection)), requests=requests)
    batch.book_progress = {'type':'auction_book', 'universe_hash':scope, 'next_offset':next_offset,
        'requested':requested, 'returned':returned, 'usable':usable, 'universe':len(selection), 'requests':requests,
        'up_count':sum((seal(row,'up') or 0)>0 for row in rows),
        'down_count':sum((seal(row,'down') or 0)>0 for row in rows),'source_time':latest.isoformat() if latest else None}
    from app.market.protocol import Response
    return Response(b'', received, elapsed, 200, requests), batch
