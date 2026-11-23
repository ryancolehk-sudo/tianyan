"""Personal research minute observations from a pinned AKShare Sina route.

This module performs no network requests and executes no SDK or JavaScript.
An observation is bound to the requested instrument: the source rows do not
independently prove that identity. Source timestamps are not claimed bar ends,
closed bars or publication times. Native quantity units remain unverified.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlencode

from app.market.protocol import InstrumentKey, SHANGHAI, SourceFailure, decimal_value

SOURCE_VERSION = 'akshare-1.19.1-sina-minute-v1'
SOURCE_COMMIT = 'fe10d1decb705e5f43cc8ca3339ca39e580fb8f4'
SOURCE_REFERENCE = ('https://github.com/akfamily/akshare/blob/' + SOURCE_COMMIT
                    + '/akshare/stock/stock_zh_a_sina.py#L350-L453')
SOURCE_DOCUMENTATION = 'https://akshare.akfamily.xyz/data/stock/stock.html#id31'
SOURCE_URL = 'https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData'
SOURCE_FILE_SHA256 = 'aa60721af3f7bdec29e1e5f59ff6770b54fc919fc6867706d16d36b3a7ee07a2'
MAX_BODY = 2_000_000
MAX_ROWS = 1970
INTERVALS = {'1m': '1', '5m': '5', '15m': '15', '30m': '30', '60m': '60'}
FAMILY_INTERVALS = {'sina_minute_' + interval: interval for interval in INTERVALS}
FAMILIES = frozenset(FAMILY_INTERVALS)
MINIMUM_INTERVALS = {family: int(INTERVALS[interval]) * 60 for family, interval in FAMILY_INTERVALS.items()}


@dataclass(frozen=True)
class MinuteObservation:
    instrument: InstrumentKey
    interval: str
    source_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_native: Decimal | None
    amount_native: Decimal | None


@dataclass
class MinuteBatch:
    """Expected counts today's distinct valid rows plus observable rejections.

    Normal other-day history and identical duplicates do not inflate expected
    or turn a valid current-day batch partial. Conflicting slots reject every
    candidate in that slot; rejection keys are original zero-based row indices.
    """
    records: list[MinuteObservation] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    expected: int = 0
    duplicates: int = 0
    out_of_day: int = 0


def _selection(instrument, interval):
    if not isinstance(instrument, InstrumentKey):
        raise SourceFailure('invalid_instrument')
    if not isinstance(interval, str) or interval not in INTERVALS:
        raise SourceFailure('unsupported_interval')
    # This narrow adapter covers the currently established mainland stock
    # directory. Neither a syntactically valid index nor another asset qualifies.
    allowed = (instrument.exchange == 'SSE' and instrument.symbol.startswith('6')
               or instrument.exchange == 'SZSE' and instrument.symbol.startswith(('0', '3'))
               and not instrument.symbol.startswith('399'))
    if instrument.asset_type != 'stock' or not allowed:
        raise SourceFailure('unsupported_asset_scope')


def minute_url(instrument: InstrumentKey, interval: str) -> str:
    """One fixed HTTPS route; no fallback, hidden daily request or adjustment."""
    _selection(instrument, interval)
    return SOURCE_URL + '?' + urlencode({'symbol': instrument.vendor_key,
        'scale': INTERVALS[interval], 'ma': 'no', 'datalen': str(MAX_ROWS)})


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SourceFailure('schema_changed')
        result[key] = value
    return result


def _nonfinite(_value):
    raise SourceFailure('invalid_number')


def _rows(body):
    if not isinstance(body, bytes):
        raise SourceFailure('schema_changed')
    if len(body) > MAX_BODY:
        raise SourceFailure('response_too_large')
    try:
        text = body.decode('utf-8-sig').strip()
        # Some JSONP publishers prepend a short comment. Strip at most one
        # bounded comment, then require the entire original assignment wrapper.
        if text.startswith('/*'):
            end = text.find('*/', 2)
            if end < 0 or end > 256:
                raise SourceFailure('schema_changed')
            text = text[end + 2:].lstrip()
        match = re.fullmatch(r'=\s*\((.*)\)\s*;?', text, flags=re.S)
        if match is None:
            raise SourceFailure('schema_changed')
        rows = json.loads(match.group(1), parse_float=Decimal,
                          parse_constant=_nonfinite, object_pairs_hook=_object)
        if not isinstance(rows, list):
            raise SourceFailure('schema_changed')
        if len(rows) > MAX_ROWS:
            raise SourceFailure('response_too_large')
        return rows
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise SourceFailure('schema_changed') from None


def _number(value, *, price=False):
    if value is None:
        text = ''
    elif isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise SourceFailure('invalid_number')
    elif isinstance(value, Decimal):
        # Bound the exponent before formatting to prevent huge numeric strings.
        if not value.is_finite() or value.adjusted() > 15 or value.as_tuple().exponent < -8:
            raise SourceFailure('invalid_number')
        text = format(value, 'f')
    else:
        text = str(value)
    number = decimal_value(text, positive=price)
    if price and number is None:
        raise SourceFailure('invalid_number')
    return number


def _source_time(value, received_at):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:00', value):
        raise SourceFailure('invalid_source_time')
    try:
        stamp = datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=SHANGHAI)
    except ValueError:
        raise SourceFailure('invalid_source_time') from None
    if stamp > received_at:
        raise SourceFailure('future_source_time')
    return stamp


def parse_sina_minutes(body: bytes, instrument: InstrumentKey, interval: str,
                       received_at: datetime, trade_day: date) -> MinuteBatch:
    """Parse request-bound observations; never synthesize missing or closed bars.

    Structural failures reject the response. Bad individual rows or conflicting
    same-time candidates remain observable in rejected; valid unrelated slots
    survive. Historical other-day rows are normal finite source-window context.
    """
    _selection(instrument, interval)
    if (not isinstance(received_at, datetime) or received_at.utcoffset() is None
            or type(trade_day) is not date
            or trade_day > received_at.astimezone(SHANGHAI).date()):
        raise SourceFailure('invalid_source_time')
    result = MinuteBatch()
    slots = {}
    for index, row in enumerate(_rows(body)):
        try:
            if not isinstance(row, dict):
                raise SourceFailure('schema_changed')
            stamp = _source_time(row.get('day'), received_at)
            if stamp.date() != trade_day:
                result.out_of_day += 1
                continue
            slots.setdefault(stamp, []).append((str(index), row))
        except SourceFailure as error:
            result.rejected[str(index)] = error.reason
    for stamp, candidates in slots.items():
        parsed = []
        failures = {}
        for index, row in candidates:
            try:
                # The normal source lacks a row identity. If a publisher later
                # adds one, an explicit contradictory value must not be ignored.
                if 'symbol' in row and row['symbol'] != instrument.vendor_key:
                    raise SourceFailure('instrument_mismatch')
                opening, high, low, close = [_number(row.get(name), price=True)
                                              for name in ('open', 'high', 'low', 'close')]
                if not low <= opening <= high or not low <= close <= high:
                    raise SourceFailure('inconsistent_price_range')
                parsed.append((index, MinuteObservation(instrument, interval, stamp,
                    opening, high, low, close, _number(row.get('volume')), _number(row.get('amount')))))
            except SourceFailure as error:
                failures[index] = error.reason
        if len(candidates) > 1 and (failures or any(row != parsed[0][1] for _, row in parsed[1:])):
            # No input-order winner: a malformed second claim cannot preserve
            # the first claim at a timestamp with contradictory content.
            for index, _ in candidates:
                result.rejected[index] = failures.get(index, 'conflicting_observation')
            continue
        result.rejected.update(failures)
        if parsed:
            result.records.append(parsed[0][1])
            result.duplicates += len(parsed) - 1
    result.records.sort(key=lambda row: row.source_time)
    result.expected = len(result.records) + len(result.rejected)
    return result
