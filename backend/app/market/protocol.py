"""Bounded, explicit source protocol. Missing values never become market zeroes."""
import calendar
import gzip
import io
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo
from app.market.news_feeds import NEWS_FEEDS

SHANGHAI = ZoneInfo('Asia/Shanghai')
MAX_BODY = 2_000_000
MAX_TENCENT_QUOTE_BATCH = 200
ALLOWED_HOSTS = frozenset({'qt.gtimg.cn', 'smartbox.gtimg.cn', 'web.ifzq.gtimg.cn', 'www.cninfo.com.cn', 'www.szse.cn', 'www.sse.com.cn',
    'www.ecb.europa.eu', 'api.worldbank.org', 'www.federalreserve.gov', 'www.stats.gov.cn', 'api.ashareapi.com', 'www.news.gov.hk',
    'quotes.sina.cn', 'quote.eastmoney.com', 'push2.eastmoney.com', 'duanxianxia.com', 'ds.duanxianxia.com',
    *(urllib.parse.urlsplit(feed.url).hostname for feed in NEWS_FEEDS.values())})
PREFIXES = {'SSE': 'sh', 'SZSE': 'sz', 'BSE': 'bj'}


class SourceFailure(Exception):
    """Only a controlled reason reaches health/logs; never an upstream response."""
    def __init__(self, reason: str, *, retry_after: int | None = None, http_status: int | None = None):
        self.reason = reason
        self.retry_after = retry_after
        self.http_status = http_status
        super().__init__(reason)


class SourceRedirect(SourceFailure):
    """Private transport metadata; the public/loggable reason stays controlled."""
    def __init__(self, status, target):
        super().__init__('upstream_redirect')
        self.status = status
        self.target = target if isinstance(target, str) and len(target) <= 4096 else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceRedirect(code, newurl)


@dataclass(frozen=True)
class Response:
    body: bytes
    received_at: datetime
    elapsed_ms: int
    status: int
    request_count: int = 1


def fetch_https(url: str, *, timeout: float = 10, data: bytes | None = None,
                max_body: int = MAX_BODY) -> Response:
    return _fetch_https(url, timeout=timeout, data=data, max_body=max_body, allowed_hosts=ALLOWED_HOSTS)


def eastmoney_allocation_url(original: str, redirect: SourceRedirect) -> str:
    """Only the observed same-query clist allocation, never a general redirect."""
    return _eastmoney_allocation_url(original, redirect, '/api/qt/clist/get')


def _eastmoney_allocation_url(original, redirect, expected_path):
    try:
        if any('#' in url or any(ord(char) < 33 or ord(char) == 127 for char in url)
                for url in (original, redirect.target or '')):
            raise ValueError()
        source = urllib.parse.urlsplit(original)
        target = urllib.parse.urlsplit(redirect.target or '')
        if (redirect.status != 302 or source.scheme != 'https' or source.hostname != 'push2.eastmoney.com'
                or target.scheme != 'https' or target.hostname != 'push2delay.eastmoney.com'
                or source.path != expected_path or target.path != source.path
                or not source.query or target.query != source.query
                or any(part.username is not None or part.password is not None or part.fragment or part.port not in {None, 443}
                    for part in (source, target))):
            raise ValueError()
        return redirect.target
    except (ValueError, TypeError, AttributeError):
        raise SourceFailure('upstream_redirect') from None


def fetch_eastmoney_allocation(url: str, *, original_url: str) -> Response:
    # The normal allowlist remains unchanged. A second hop is still rejected,
    # and this GET never carries cookies, authorization or caller headers.
    eastmoney_allocation_url(original_url, SourceRedirect(302, url))
    return _fetch_https(url, allowed_hosts=frozenset({'push2delay.eastmoney.com'}))


def eastmoney_minute_allocation_url(original, redirect, *, observed_rejection):
    """A prior ordinary rejection and this request must agree before one hop."""
    required = {'version': 1, 'http_status': 302, 'request_path': 'funds_minute',
        'target_valid': True, 'target_host': 'push2delay.eastmoney.com',
        'target_path': 'funds_minute', 'same_path': True, 'same_query': True, 'secure_target': True}
    if not isinstance(observed_rejection, dict) or any(
            type(observed_rejection.get(key)) is not type(value) or observed_rejection[key] != value
            for key, value in required.items()):
        raise SourceFailure('upstream_redirect')
    return _eastmoney_allocation_url(original, redirect, '/api/qt/stock/fflow/kline/get')


def fetch_eastmoney_minute_allocation(url, *, original_url, observed_rejection):
    eastmoney_minute_allocation_url(original_url, SourceRedirect(302, url),
        observed_rejection=observed_rejection)
    return _fetch_https(url, allowed_hosts=frozenset({'push2delay.eastmoney.com'}))


def _fetch_https(url: str, *, timeout: float = 10, data: bytes | None = None,
                 max_body: int = MAX_BODY, allowed_hosts: frozenset[str]) -> Response:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in allowed_hosts or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.fragment:
        raise SourceFailure('endpoint_not_allowed')
    if not 0 < timeout <= (30 if parsed.hostname=='api.worldbank.org' else 15):
        raise SourceFailure('invalid_timeout')
    if data is not None and len(data)>4000:
        raise SourceFailure('request_too_large')
    if type(max_body) is not int or not 0 < max_body <= MAX_BODY:
        raise SourceFailure('invalid_response_budget')
    import time
    start = time.monotonic()
    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(url, data=data, headers={'User-Agent': 'Tianyan/1.0 (private research)',
        'Accept': 'application/json,application/xml,text/plain,text/html', 'Accept-Encoding': 'identity',
        **({'Content-Type':'application/x-www-form-urlencoded'} if data is not None else {})})
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(max_body + 1)
            if len(body) > max_body:
                raise SourceFailure('response_too_large')
            encoding = response.headers.get('Content-Encoding', '').strip().lower()
            if encoding == 'gzip':
                try:
                    # Some publishers still send gzip for an identity request.
                    # Bound decoded bytes too; a tiny wire body is not a size guarantee.
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                        body = compressed.read(max_body + 1)
                except (gzip.BadGzipFile, EOFError, zlib.error):
                    raise SourceFailure('unsafe_upstream_response') from None
                if len(body) > max_body:
                    raise SourceFailure('response_too_large')
            elif encoding not in {'', 'identity'}:
                raise SourceFailure('unsafe_upstream_response')
            return Response(body=body, received_at=datetime.now(UTC), elapsed_ms=round((time.monotonic()-start)*1000), status=response.status)
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            # Missing/unsupported Location may be rejected by urllib before
            # redirect_request runs; preserve the real status without following.
            raise SourceRedirect(error.code, error.headers.get('Location') if error.headers else None) from None
        if error.code in {401, 403}:
            raise SourceFailure('permission_missing') from None
        if error.code == 429:
            raw = error.headers.get('Retry-After', '')
            if parsed.hostname=='qt.gtimg.cn':
                from app.market.qt_budget import retry_after
                seconds=retry_after(raw)
            else:
                seconds = min(int(raw), 86400) if re.fullmatch(r'\d{1,8}', raw) else None
            raise SourceFailure('rate_limited', retry_after=seconds) from None
        raise SourceFailure('upstream_unavailable', http_status=error.code) from None
    except (TimeoutError, socket.timeout):
        raise SourceFailure('timeout') from None
    except (urllib.error.URLError, OSError):
        raise SourceFailure('transport_error') from None


@dataclass(frozen=True)
class InstrumentKey:
    exchange: Literal['SSE', 'SZSE', 'BSE']
    symbol: str
    asset_type: Literal['stock', 'index', 'etf', 'convertible']

    def __post_init__(self):
        if self.exchange not in PREFIXES or not re.fullmatch(r'\d{6}', self.symbol) or self.asset_type not in {'stock', 'index', 'etf', 'convertible'}:
            raise SourceFailure('invalid_instrument')

    @property
    def vendor_key(self):
        return PREFIXES[self.exchange] + self.symbol

    @property
    def identity(self):
        return f'{self.exchange}:{self.asset_type}:{self.symbol}'


def decimal_value(value: str, *, positive=False) -> Decimal | None:
    if value in {'', '-', '--', 'null'}:
        return None
    if len(value) > 25 or not re.fullmatch(r'-?\d{1,16}(?:\.\d{1,8})?', value):
        raise SourceFailure('invalid_number')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise SourceFailure('invalid_number') from None
    if result < 0:
        raise SourceFailure('invalid_number')
    if positive and result == 0:
        return None
    return result


def source_time(value: str, received_at: datetime) -> datetime | None:
    if not value:
        return None
    if not re.fullmatch(r'\d{14}', value):
        raise SourceFailure('invalid_source_time')
    try:
        result = datetime.strptime(value, '%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI)
    except ValueError:
        raise SourceFailure('invalid_source_time') from None
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise SourceFailure('invalid_received_time')
    if result > received_at + timedelta(seconds=60):
        raise SourceFailure('future_source_time')
    return result


@dataclass(frozen=True)
class Quote:
    instrument: InstrumentKey
    name: str
    price: Decimal | None
    previous_close: Decimal | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    quote_time: datetime | None
    received_at: datetime
    price_unit: Literal['CNY', 'point']
    # Native fields are not normalized until the instrument-specific unit is verified.
    volume_native: Decimal | None = None
    volume_shares: Decimal | None = None
    amount_cny: Decimal | None = None
    source_snapshot_time: datetime | None = None

    @property
    def change_percent(self):
        return (self.price / self.previous_close - 1) * 100 if self.price is not None and self.previous_close else None


@dataclass
class QuoteBatch:
    records: list[Quote] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    expected: int = 0


def parse_tencent_quotes(body: bytes, requested: list[InstrumentKey], received_at: datetime, *, maximum=MAX_TENCENT_QUOTE_BATCH) -> QuoteBatch:
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise SourceFailure('invalid_received_time')
    if maximum not in {MAX_TENCENT_QUOTE_BATCH,750} or not 1 <= len(requested) <= maximum or len({item.vendor_key for item in requested}) != len(requested):
        raise SourceFailure('invalid_instrument_batch')
    try:
        text = body.decode('gb18030')
    except UnicodeError:
        raise SourceFailure('schema_changed') from None
    expected = {item.vendor_key: item for item in requested}
    result = QuoteBatch(expected=len(requested))
    seen = set()
    for line in text.splitlines():
        match = re.fullmatch(r'\s*v_((?:sh|sz|bj)\d{6})="([^"\r\n]*)";?\s*', line)
        if not match:
            raise SourceFailure('schema_changed')
        key, raw = match.groups()
        if key not in expected or key in seen:
            raise SourceFailure('unexpected_instrument')
        seen.add(key)
        fields = raw.split('~')
        if len(fields) < 35:
            result.rejected[key] = 'instrument_unavailable'
            continue
        try:
            item = expected[key]
            if fields[2] != item.symbol:
                raise SourceFailure('instrument_mismatch')
            name = fields[1].strip()
            if not name or len(name) > 200 or any(ord(char) < 32 for char in name):
                raise SourceFailure('invalid_name')
            price, previous, opening, high, low = (decimal_value(fields[index], positive=True) for index in [3, 4, 5, 33, 34])
            if high is not None and low is not None and (high < low or any(value is not None and not low <= value <= high for value in [price, opening])):
                raise SourceFailure('inconsistent_price_range')
            volume=decimal_value(fields[6])
            trade_fields=fields[35].split('/') if len(fields)>35 else []
            # Same native contract as the existing Tencent auction adapter:
            # field 35 contains exact yuan, field 37 is rounded ten-thousands.
            amount=decimal_value(trade_fields[2]) if len(trade_fields)==3 else None
            result.records.append(Quote(instrument=item, name=name, price=price, previous_close=previous, open=opening, high=high, low=low,
                quote_time=source_time(fields[30], received_at), received_at=received_at, price_unit='point' if item.asset_type == 'index' else 'CNY',
                volume_native=volume,
                volume_shares=volume*100 if item.asset_type=='stock' and volume is not None else None,
                amount_cny=amount if item.asset_type=='stock' else None))
        except SourceFailure as error:
            result.rejected[key] = error.reason
    for key in expected.keys() - seen:
        result.rejected[key] = 'instrument_missing'
    return result


def quote_url(instruments: list[InstrumentKey], *, maximum=MAX_TENCENT_QUOTE_BATCH) -> str:
    if maximum not in {MAX_TENCENT_QUOTE_BATCH,750} or not 1 <= len(instruments) <= maximum or len({item.vendor_key for item in instruments}) != len(instruments):
        raise SourceFailure('invalid_instrument_batch')
    return 'https://qt.gtimg.cn/q=' + ','.join(item.vendor_key for item in instruments)


def parse_szse_calendar(body: bytes, year: int, month: int) -> list[tuple[date, bool]]:
    try:
        value = json.loads(body)
        rows = value['data']
        if not isinstance(rows, list) or len(rows) > 31:
            raise ValueError()
        expected = {date(year, month, day) for day in range(1, calendar.monthrange(year, month)[1]+1)}
        result = {}
        for row in rows:
            day = date.fromisoformat(row['jyrq'])
            if day not in expected or day in result or row['jybz'] not in {'0','1'}:
                raise ValueError()
            result[day] = row['jybz'] == '1'
        if set(result) != expected:
            raise SourceFailure('incomplete_calendar')
        return sorted(result.items())
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise SourceFailure('schema_changed') from None


CNINFO_CATALOG_URL = 'https://www.cninfo.com.cn/new/data/szse_stock.json'


def exchange_for_listed_share(symbol: str) -> Literal['SSE', 'SZSE', 'BSE'] | None:
    """Map a six-digit listed share code to exchange. B-shares and unknown prefixes stay None."""
    if not re.fullmatch(r'\d{6}', symbol):
        return None
    if symbol[0] == '6':
        return 'SSE'
    if symbol.startswith(('000', '001', '002', '003', '300', '301')):
        return 'SZSE'
    if symbol[0] == '8' or symbol.startswith(('43', '92')):
        return 'BSE'
    return None


@dataclass(frozen=True)
class CatalogRecord:
    instrument: InstrumentKey
    name: str
    org_id: str | None
    category: str


@dataclass
class CatalogBatch:
    records: list[CatalogRecord] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    expected: int = 0


def parse_cninfo_catalog(body: bytes) -> CatalogBatch:
    """Parse the cninfo listed-share directory. Filename says szse; body covers SSE/SZSE/BSE A-shares."""
    try:
        payload = json.loads(body.decode('utf-8'))
        rows = payload['stockList']
        if not isinstance(rows, list) or not rows or len(rows) > 20_000:
            raise ValueError()
    except (UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise SourceFailure('schema_changed') from None
    result = CatalogBatch(expected=len(rows))
    # Identity is decided per row; conflicts are decided per identity so the
    # outcome never depends on the order rows happen to arrive in.
    grouped: dict[str, list[tuple[str, CatalogRecord]]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            result.rejected[f'row:{index}'] = 'schema_changed'
            continue
        code = row.get('code')
        name = row.get('zwjc')
        category = row.get('category')
        org_id = row.get('orgId')
        key = f'row:{index}:{code}' if isinstance(code, str) else f'row:{index}'
        if not isinstance(code, str) or not isinstance(name, str) or not isinstance(category, str):
            result.rejected[key] = 'schema_changed'
            continue
        if category not in {'A股', 'CDR'}:
            result.rejected[key] = 'unsupported_share_class'
            continue
        if not name.strip() or len(name) > 200 or any(ord(char) < 32 for char in name):
            result.rejected[key] = 'invalid_name'
            continue
        if org_id is not None and (not isinstance(org_id, str) or len(org_id) > 64):
            result.rejected[key] = 'schema_changed'
            continue
        exchange = exchange_for_listed_share(code)
        if exchange is None:
            result.rejected[key] = 'exchange_unmapped'
            continue
        try:
            instrument = InstrumentKey(exchange, code, 'stock')
        except SourceFailure as error:
            result.rejected[key] = error.reason
            continue
        grouped.setdefault(instrument.identity, []).append((key, CatalogRecord(
            instrument=instrument, name=name.strip(),
            org_id=org_id if isinstance(org_id, str) else None, category=category)))
    for group in grouped.values():
        if len({(record.name, record.org_id, record.category) for _, record in group}) > 1:
            # Same identity, different content: quarantine every claim instead of
            # letting input order pick a winner that a later upsert would write.
            for key, _ in group:
                result.rejected[key] = 'conflicting_instrument'
            continue
        result.records.append(group[0][1])
        for key, _ in group[1:]:
            result.rejected[key] = 'duplicate_instrument'
    result.records.sort(key=lambda record: record.instrument.identity)
    # A fully conflicting batch is a valid, auditable refusal to update. Only a
    # batch with no usable rows and no conflict is treated as an unavailable source.
    if not result.records and not any(reason == 'conflicting_instrument' for reason in result.rejected.values()):
        raise SourceFailure('instrument_unavailable')
    return result
