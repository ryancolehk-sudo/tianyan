"""Two public call-auction snapshot adapters, normalized into summary + detail tables.

Contract: docs/AUCTION_INTEGRATION_CONTRACT.md (frozen v1). A snapshot is only
labelled 09:15/09:20/09:25 when the *source* clock lands within 60 seconds of
that checkpoint. Received time never becomes a quote time.
"""
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from app.evidence.common import content_hash
from app.evidence.schemas import ReferencePayload
from app.market.protocol import SHANGHAI, InstrumentKey, Response, SourceFailure, fetch_https
from app.market.reference import ReferenceBatch

AUCTION_FAMILIES = frozenset({'eastmoney_auction', 'tencent_auction'})
CHECKPOINTS = ('09:15', '09:20', '09:25')
CHECKPOINT_TOLERANCE = timedelta(seconds=60)
SUMMARY_DATASET = 'market.auction.summary'
STOCKS_DATASET = 'market.auction.stocks'
LEGACY_DETAIL_DATASET = 'market.auction.detail'  # v1 只读兼容，v2 起不再写入
MAX_AUCTION_SYMBOLS = 20
MAX_AUCTION_BODY = 1_000_000

SUMMARY_COLUMNS = (
    ('trade_day', '交易日', ''),
    ('checkpoint', '竞价检查点', ''),
    ('instrument_count', '请求标的数', '只'),
    ('quoted_count', '有效报价数', '只'),
    ('rejected_count', '未纳入数', '只'),
    ('total_volume_shares', '竞价量合计', '股'),
    ('total_amount_cny', '竞价额合计', '元'),
    ('up_count', '上涨数', '只'),
    ('down_count', '下跌数', '只'),
    ('flat_count', '平盘数', '只'),
    ('batch_hash', '批次哈希', ''),
)
DETAIL_COLUMNS = (
    ('trade_day', '交易日', ''),
    ('checkpoint', '竞价检查点', ''),
    ('code', '证券代码', ''),
    ('name', '名称', ''),
    ('price_cny', '竞价参考价', '元'),
    ('prev_close_cny', '昨收', '元'),
    ('change_pct', '竞价涨跌幅', '%'),
    ('volume_shares', '竞价量', '股'),
    ('amount_cny', '竞价额', '元'),
    ('quote_time', '来源时点', ''),
    ('batch_hash', '批次哈希', ''),
)

# Public pages only; no key, no account, no paid entitlement is claimed here.
FAMILY_META = {
    'eastmoney_auction': {
        'title': '东方财富公开盘前快照',
        'url': 'https://quote.eastmoney.com/',
        'attribution': '东方财富公开网页行情（push2 ulist，无需登录）',
    },
    'tencent_auction': {
        'title': '腾讯网页公开盘前快照',
        'url': 'https://gu.qq.com/',
        'attribution': '腾讯网页行情（qt.gtimg.cn，无需登录）',
    },
}
SCOPE = ('供应商公开快照的当前指示性价格与累计竞价量；仅当来源时点落在检查点±60秒内才标记该检查点，'
         '未命中保持未知。价格为元、量为股（来源手×100）、金额为元（来源万元×10000，优先取原币元字段）。'
         '不推断未匹配量、撤单或委托队列，不代表撮合成交完成，也不构成交易建议。')


@dataclass(frozen=True)
class _Row:
    instrument: InstrumentKey
    name: str
    price: Decimal | None
    prev_close: Decimal | None
    change_pct: Decimal | None
    volume_shares: str | None
    amount_cny: str | None
    quote_time: datetime | None


def _text(value, *, signed=False):
    """Normalize a vendor number to a decimal string; missing stays None."""
    if value is None or value == '' or value in {'-', '--', 'null'}:
        return None
    if isinstance(value, bool):
        raise SourceFailure('schema_changed')
    if isinstance(value, (int, float)):
        text = format(Decimal(str(value)), 'f')
    elif isinstance(value, str) and re.fullmatch(r'-?\d{1,18}(?:\.\d{1,10})?', value.strip()):
        text = value.strip()
    else:
        raise SourceFailure('schema_changed')
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise SourceFailure('schema_changed') from None
    if number < 0 and not signed:
        raise SourceFailure('schema_changed')
    if '.' in text:
        return format(number.normalize(), 'f')
    return str(int(number))


def _shares(lots):
    text = _text(lots)
    if text is None:
        return None
    number = Decimal(text) * 100
    if number != number.to_integral_value():
        raise SourceFailure('schema_changed')
    return str(int(number))


def _name(value):
    if not isinstance(value, str):
        raise SourceFailure('schema_changed')
    name = value.strip()
    if not name or len(name) > 200 or any(ord(char) < 32 for char in name):
        raise SourceFailure('invalid_name')
    return name


def auction_url(family, selection):
    if family not in AUCTION_FAMILIES:
        raise SourceFailure('capability_not_implemented')
    if not 1 <= len(selection) <= MAX_AUCTION_SYMBOLS or len({item.identity for item in selection}) != len(selection):
        raise SourceFailure('invalid_instrument_batch')
    if any(item.asset_type != 'stock' for item in selection):
        raise SourceFailure('unsupported_asset_scope')
    if family == 'eastmoney_auction':
        # 1 = 上海, 0 = 深圳/北交所前缀；本批只接受沪深股票。
        secids = ','.join(('1.' if item.exchange == 'SSE' else '0.') + item.symbol for item in selection)
        return ('https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&pn=1&pz='
                f'{MAX_AUCTION_SYMBOLS}&fields=f2,f3,f5,f6,f12,f14,f15,f16,f17,f18,f124&secids=' + secids)
    return 'https://qt.gtimg.cn/q=' + ','.join(item.vendor_key for item in selection)


def fetch_auction(family, selection):
    """One bounded HTTPS read. Persistence and permission checks stay in collect."""
    return fetch_https(auction_url(family, selection))


def source_keys(family, trade_day, batch_hash):
    base = f'auction:{family}:{trade_day or "unknown"}:{batch_hash[:16]}'
    return base + ':summary', base + ':stocks'


def _checkpoint(rows, received_at):
    """Return (checkpoint, basis, quote_time). Received time never invents a checkpoint."""
    times = [row.quote_time for row in rows if row.quote_time is not None]
    if not times:
        return None, 'unknown', None
    latest = max(times)
    local = latest.astimezone(SHANGHAI)
    for label in CHECKPOINTS:
        hour, minute = (int(part) for part in label.split(':'))
        centre = datetime.combine(local.date(), time(hour, minute), SHANGHAI)
        if abs(local - centre) <= CHECKPOINT_TOLERANCE:
            return label, 'source_time', latest
    return None, 'unknown', latest


def _trade_day(rows, received_at):
    times = [row.quote_time for row in rows if row.quote_time is not None]
    if times:
        return max(times).astimezone(SHANGHAI).date(), 'source_time'
    return received_at.astimezone(SHANGHAI).date(), 'received_at'


def _parse_eastmoney(body, received_at, requested):
    try:
        payload = json.loads(body.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError):
        raise SourceFailure('schema_changed') from None
    if not isinstance(payload, dict) or payload.get('rc') != 0:
        raise SourceFailure('upstream_unavailable')
    data = payload.get('data')
    rows = data.get('diff') if isinstance(data, dict) else None
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list) or len(rows) > MAX_AUCTION_SYMBOLS * 2:
        raise SourceFailure('schema_changed')
    by_symbol = {item.symbol: item for item in requested}
    parsed, rejected, seen = [], {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise SourceFailure('schema_changed')
        code = row.get('f12')
        if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code):
            raise SourceFailure('schema_changed')
        key = by_symbol.get(code)
        if key is None or code in seen:
            raise SourceFailure('instrument_mismatch')
        seen.add(code)
        try:
            price = _text(row.get('f2'))
            if price is None:
                rejected[code] = 'instrument_unavailable'
                continue
            stamp = row.get('f124')
            quote_time = None
            if isinstance(stamp, int) and stamp > 0:
                quote_time = datetime.fromtimestamp(stamp, SHANGHAI)
                if quote_time > received_at + timedelta(seconds=60):
                    raise SourceFailure('future_source_time')
            parsed.append(_Row(instrument=key, name=_name(row.get('f14')), price=Decimal(price),
                prev_close=_decimal(row.get('f18')), change_pct=_decimal(row.get('f3'), signed=True),
                volume_shares=_shares(row.get('f5')), amount_cny=_text(row.get('f6')), quote_time=quote_time))
        except SourceFailure as error:
            if error.reason in {'schema_changed', 'future_source_time', 'invalid_name'}:
                raise
            rejected[code] = error.reason
    for item in requested:
        if item.symbol not in seen:
            rejected[item.symbol] = 'instrument_missing'
    return parsed, rejected


def _parse_tencent(body, received_at, requested):
    try:
        text = body.decode('gb18030')
    except UnicodeError:
        raise SourceFailure('schema_changed') from None
    by_vendor = {item.vendor_key: item for item in requested}
    parsed, rejected, seen = [], {}, set()
    for line in text.splitlines():
        match = re.fullmatch(r'\s*v_((?:sh|sz|bj)\d{6})="([^"\r\n]*)";?\s*', line)
        if not match:
            raise SourceFailure('schema_changed')
        vendor, raw = match.groups()
        key = by_vendor.get(vendor)
        if key is None or vendor in seen:
            raise SourceFailure('instrument_mismatch')
        seen.add(vendor)
        fields = raw.split('~')
        if len(fields) < 38:
            rejected[key.symbol] = 'instrument_unavailable'
            continue
        try:
            if fields[2] != key.symbol:
                raise SourceFailure('instrument_mismatch')
            price = _text(fields[3])
            if price is None:
                rejected[key.symbol] = 'instrument_unavailable'
                continue
            quote_time = None
            stamp = fields[30]
            if re.fullmatch(r'\d{14}', stamp):
                quote_time = datetime.strptime(stamp, '%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI)
                if quote_time > received_at + timedelta(seconds=60):
                    raise SourceFailure('future_source_time')
            # fields[35] is price/volume(lots)/amount(CNY): exact yuan beats the
            # rounded 万元 field, so the rounded field is never used for storage.
            amount = None
            parts = fields[35].split('/')
            if len(parts) == 3 and re.fullmatch(r'\d{1,18}(?:\.\d{1,10})?', parts[2]):
                amount = _text(parts[2])
            parsed.append(_Row(instrument=key, name=_name(fields[1]), price=Decimal(price),
                prev_close=_decimal(fields[4]), change_pct=_decimal(fields[32], signed=True),
                volume_shares=_shares(fields[6]), amount_cny=amount, quote_time=quote_time))
        except SourceFailure as error:
            if error.reason in {'schema_changed', 'future_source_time', 'invalid_name', 'instrument_mismatch'}:
                raise
            rejected[key.symbol] = error.reason
    for item in requested:
        if item.vendor_key not in seen:
            rejected[item.symbol] = 'instrument_missing'
    return parsed, rejected


def _decimal(value, *, signed=False):
    text = _text(value, signed=signed)
    return Decimal(text) if text is not None else None


def _payload(family, dataset, title, columns, rows, trade_day):
    meta = FAMILY_META[family]
    return ReferencePayload(dataset=dataset, title=title, source_url=meta['url'], attribution=meta['attribution'],
        scope=SCOPE, columns=[{'key': key, 'label': label, 'unit': unit or None} for key, label, unit in columns],
        rows=rows, observed_day=trade_day)


def parse_auction(body, received_at, selection, family):
    """Parse one vendor snapshot into a paired summary + detail ReferenceBatch."""
    if family not in AUCTION_FAMILIES:
        raise SourceFailure('capability_not_implemented')
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise SourceFailure('invalid_received_time')
    if not 1 <= len(selection) <= MAX_AUCTION_SYMBOLS or len({item.identity for item in selection}) != len(selection):
        raise SourceFailure('invalid_instrument_batch')
    if any(item.asset_type != 'stock' for item in selection):
        raise SourceFailure('unsupported_asset_scope')
    if len(body) > MAX_AUCTION_BODY:
        raise SourceFailure('response_too_large')
    parsed, rejected = (_parse_eastmoney if family == 'eastmoney_auction' else _parse_tencent)(body, received_at, selection)
    if not parsed:
        raise SourceFailure('instrument_unavailable')
    trade_day, _ = _trade_day(parsed, received_at)
    checkpoint, _, quote_time = _checkpoint(parsed, received_at)
    day_text = trade_day.isoformat() if trade_day else None
    detail = []
    for row in sorted(parsed, key=lambda item: item.instrument.symbol):
        detail.append({
            'trade_day': day_text,
            'checkpoint': checkpoint,
            'code': row.instrument.symbol,
            'name': row.name,
            'price_cny': format(row.price, 'f'),
            'prev_close_cny': format(row.prev_close, 'f') if row.prev_close is not None else None,
            'change_pct': format(row.change_pct, 'f') if row.change_pct is not None else None,
            'volume_shares': row.volume_shares,
            'amount_cny': row.amount_cny,
            'quote_time': row.quote_time.astimezone(SHANGHAI).isoformat() if row.quote_time else None,
        })
    batch_hash = content_hash({'family': family, 'trade_day': day_text, 'checkpoint': checkpoint, 'detail': detail})
    for row in detail:
        row['batch_hash'] = batch_hash
    volumes = [row['volume_shares'] for row in detail]
    amounts = [row['amount_cny'] for row in detail]
    pct = [row['change_pct'] for row in detail]
    summary = {
        'trade_day': day_text,
        'checkpoint': checkpoint,
        'instrument_count': str(len(selection)),
        'quoted_count': str(len(detail)),
        'rejected_count': str(len(rejected)),
        'total_volume_shares': str(sum(int(value) for value in volumes)) if all(value is not None for value in volumes) else None,
        'total_amount_cny': format(sum(Decimal(value) for value in amounts), 'f') if all(value is not None for value in amounts) else None,
        'up_count': str(sum(1 for value in pct if value is not None and Decimal(value) > 0)),
        'down_count': str(sum(1 for value in pct if value is not None and Decimal(value) < 0)),
        'flat_count': str(sum(1 for value in pct if value is not None and Decimal(value) == 0)),
        'batch_hash': batch_hash,
    }
    meta = FAMILY_META[family]
    summary_key, detail_key = source_keys(family, day_text, batch_hash)
    summary_payload = _payload(family, SUMMARY_DATASET, meta['title'] + ' · 竞价批次汇总', SUMMARY_COLUMNS, [summary], trade_day)
    detail_payload = _payload(family, STOCKS_DATASET, meta['title'] + ' · 竞价批次明细', DETAIL_COLUMNS, detail, trade_day)
    result = ReferenceBatch(records=[(summary_key, summary_payload, None), (detail_key, detail_payload, None)],
        expected=1 + len(selection), rejected=len(rejected), requests=1, batch_hash=batch_hash)
    return result
