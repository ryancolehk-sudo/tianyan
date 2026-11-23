"""Whole-market snapshots from the already bound Fuyao stock endpoint.

A server snapshot clock is not an individual trade clock. These observations
can supply current screen prices, but never dated trades, auction checkpoints,
historical decisions or an invented intraday curve.
"""
import re
from datetime import time, timedelta

from app.evidence.schemas import ReferencePayload
from app.market import fuyao
from app.market.protocol import Quote, Response, SHANGHAI, SourceFailure
from app.market.reference import ReferenceBatch

DATASET = 'fuyao.market_snapshot'
PAGE_SIZE = 6000
MAX_ROWS = 7999
PATTERN = re.compile(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ|[489]\d{5}\.BJ)')
FIELDS = ('ts_code', 'close', 'pre_close', 'open', 'high', 'low', 'vol',
          'amount', 'trade_time', 'snapshot_time')


def fetch(credential, *, endpoint=fuyao.FUYAO_ENDPOINT, before_request=None):
    responses, rows, total = [], [], None
    # At most two bounded pages; a changed total cannot silently truncate scope.
    for offset in (0, PAGE_SIZE):
        try:
            response = fuyao.request('/api/a-share/prices/snapshot',
                {'limit': PAGE_SIZE, 'offset': offset}, credential,
                endpoint=endpoint, before_request=before_request)
            data = fuyao.unpack(response.body)
            count = data.get('total')
            if type(count) is not int or not 0 < count <= MAX_ROWS:
                raise SourceFailure('market_scope_truncated')
            if total is not None and total != count:
                raise SourceFailure('market_scope_changed')
            total = count
            page = data['item']
            if len(page) > PAGE_SIZE or not page:
                raise SourceFailure('market_scope_truncated')
            # Page clocks remain separate even if the server rolls mid-request.
            rows.extend((raw, data.get('timestamp'), response.received_at) for raw in page)
            responses.append(response)
            if len(rows) == total:
                break
            if len(rows) > total or len(page) < PAGE_SIZE:
                raise SourceFailure('market_scope_truncated')
        except SourceFailure as exc:
            exc.request_count = len(responses) + getattr(exc, 'request_count', 1)
            raise
    if len(rows) != total:
        error = SourceFailure('market_scope_truncated')
        error.request_count = len(responses)
        raise error
    body = b'[' + b','.join(item.body for item in responses) + b']'
    return Response(body, max(item.received_at for item in responses),
        sum(item.elapsed_ms for item in responses), 200, len(responses)), rows


def parse(rows, received_at):
    normalized, seen = [], set()
    for raw, snapshot_ms, page_received_at in rows:
        if not isinstance(raw, dict):
            raise SourceFailure('schema_changed')
        symbol = raw.get('thscode')
        if not isinstance(symbol, str) or not PATTERN.fullmatch(symbol):
            raise SourceFailure('unexpected_instrument')
        if symbol in seen:
            raise SourceFailure('duplicate_instrument')
        seen.add(symbol)
        if raw.get('ticker', symbol[:6]) != symbol[:6] or raw.get('currency', 'CNY') != 'CNY':
            raise SourceFailure('instrument_mismatch')
        snapshot = fuyao.epoch_clock(snapshot_ms, page_received_at, required=True)
        stamp = fuyao.epoch_clock(raw.get('quote_time_ms') or raw.get('trade_time_ms'), page_received_at)
        values = {target: fuyao.text_number(raw.get(native, raw.get(alias)), positive=True)
            for target, native, alias in (
                ('close', 'last_price', 'last_price'), ('pre_close', 'prev_price', 'prev_close_price'),
                ('open', 'open_price', 'open_price'), ('high', 'high_price', 'highest_price'),
                ('low', 'low_price', 'lowest_price'))}
        # Keep absent/zero prices absent, including suspended/unlisted rows.
        prices = {name: fuyao.number(value) for name, value in values.items()}
        high, low = prices['high'], prices['low']
        if high is not None and low is not None and (high < low or any(
                value is not None and not low <= value <= high for value in (prices['close'], prices['open']))):
            raise SourceFailure('inconsistent_price_range')
        normalized.append(dict(ts_code=symbol, **values,
            vol=fuyao.text_number(raw.get('volume'), integer=True),
            amount=fuyao.text_number(raw.get('turnover')),
            trade_time=stamp.isoformat() if stamp else None,
            snapshot_time=snapshot.isoformat()))
    if not 0 < len(normalized) <= MAX_ROWS:
        raise SourceFailure('market_scope_truncated')
    payload = ReferencePayload(dataset=DATASET, title='同花顺 · 全市场价格快照',
        source_url=fuyao.FUYAO_DOCS, attribution='同花顺 API 官方 REST；本人已有连接',
        scope='同一股票快照接口分页实际返回；保留每页服务器快照时间。'
              'trade_time缺失表示个股行情日期及成交时刻未知，不将快照或接收时间填入成交时间；'
              '仅供当前观察价格，不补竞价、盘口、历史曲线或严格历史判断。',
        columns=[dict(key=field, label=field,
            unit='CNY' if field in {'close','pre_close','open','high','low','amount'} else '股' if field=='vol' else None)
            for field in FIELDS], rows=normalized,
        observed_day=received_at.astimezone(SHANGHAI).date())
    return ReferenceBatch(records=[(DATASET, payload, None)], expected=1, requests=1)


def snapshot_clock(row, received_at):
    from app.market.resilience import stamp
    value = stamp(row.get('snapshot_time'))
    return value if value is not None and value <= received_at + timedelta(seconds=60) else None


def current_snapshot(row, received_at, day, cutoff, freshness):
    """A fresh live observation with unknown trade date stays explicitly partial."""
    from app.market.session import quote_fresh_at
    snapshot = snapshot_clock(row, received_at)
    return (snapshot is not None and snapshot <= cutoff and received_at <= cutoff
        and snapshot.astimezone(SHANGHAI).date() == day and day.weekday() < 5
        and received_at.astimezone(SHANGHAI).date() == day
        and snapshot.astimezone(SHANGHAI).time() >= time(9, 30)
        and (quote_fresh_at(cutoff) - snapshot).total_seconds() <= freshness)


def snapshot_predicate(received_at, day, cutoff, freshness):
    """Reuse identical snapshot clocks only inside one fixed-context batch."""
    checked = {}

    def current(row):
        value = row.get('snapshot_time')
        if not isinstance(value, str):
            return current_snapshot(row, received_at, day, cutoff, freshness)
        if value not in checked:
            checked[value] = current_snapshot(row, received_at, day, cutoff, freshness)
        return checked[value]

    return current


def quote_from_snapshot(payload, identity, received_at):
    suffix = {'SSE':'SH', 'SZSE':'SZ', 'BSE':'BJ'}.get(identity.exchange)
    if identity.asset_type != 'stock' or suffix is None:
        return None
    rows = [row for row in payload.get('rows', []) if row.get('ts_code') == identity.symbol + '.' + suffix]
    if len(rows) != 1:
        return None
    row = rows[0]
    from app.market.resilience import stamp
    from app.market.protocol import InstrumentKey
    return Quote(InstrumentKey(identity.exchange, identity.symbol, 'stock'), identity.name,
        *(fuyao.number(row.get(field)) for field in ('close','pre_close','open','high','low')),
        stamp(row.get('trade_time')), received_at, 'CNY',
        volume_shares=fuyao.number(row.get('vol'), integer=True), amount_cny=fuyao.number(row.get('amount')),
        source_snapshot_time=snapshot_clock(row, received_at))
