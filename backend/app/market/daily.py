"""Validate unadjusted daily closes; no invented ticks, names or publication times."""
import calendar
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from app.market.protocol import InstrumentKey, Response, SHANGHAI, SourceFailure, decimal_value
from app.market.registry import INDEXES

MAX_DAILY_BODY = 8_000_000


def number(value, *, positive=False):
    if value in {'', '-', '--', 'null'}:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,16}(?:\.\d{1,10})?', value):
        raise SourceFailure('invalid_number')
    canonical = format(Decimal(value), 'f')
    if '.' in canonical:
        canonical = canonical.rstrip('0').rstrip('.')
    return decimal_value(canonical, positive=positive)


@dataclass(frozen=True)
class DailyQuote:
    instrument: InstrumentKey
    name: str
    price: Decimal | None
    previous_close: Decimal | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    received_at: datetime
    trade_day: date
    volume_shares: str | None
    amount_cny: str | None
    price_unit: str
    quote_time: None = None


@dataclass(frozen=True)
class DailyBatch:
    records: list[DailyQuote]
    rejected: list[str]
    expected: int
    days: list[tuple[date, bool]]


def fetch_daily():
    start = time.monotonic()
    try:
        result = subprocess.run([sys.executable, '-m', 'app.market.baostock_reader'],
            input=datetime.now(UTC).astimezone(SHANGHAI).isoformat(), text=True, capture_output=True,
            timeout=90, env={**os.environ, 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'})
    except subprocess.TimeoutExpired:
        raise SourceFailure('timeout') from None
    if result.returncode or len(result.stdout) > MAX_DAILY_BODY:
        raise SourceFailure('upstream_unavailable')
    return Response(result.stdout.encode('utf-8'), datetime.now(UTC), round((time.monotonic()-start)*1000), 200)


def parse_daily(body, received_at):
    try:
        if len(body) > MAX_DAILY_BODY or received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ValueError('invalid response envelope')
        data = json.loads(body)
        if data.get('error'):
            raise SourceFailure(data['error'] if data['error'] in {
                'schema_changed', 'transport_error', 'upstream_unavailable', 'daily_data_not_published'} else 'upstream_unavailable')
        day = date.fromisoformat(data['trade_day'])
        if day > received_at.astimezone(SHANGHAI).date():
            raise ValueError('future date')
        raw = data['rows']
        if not isinstance(raw, list) or not 1 <= len(raw) <= 15004 or any(not isinstance(r, dict) for r in raw):
            raise ValueError('row limit')
        known_indexes = {(e, s) for e, s, _ in INDEXES}
        names = data['names']
        if not isinstance(names, dict):
            raise ValueError('name map')
        records, rejected, seen = [], [], set()
        for row in raw:
            try:
                vendor = row['code']
                exchange = {'sh': 'SSE', 'sz': 'SZSE'}[vendor[:2]]
                symbol = vendor[3:]
                index = (exchange, symbol) in known_indexes
                key = InstrumentKey(exchange, symbol, 'index' if index else 'stock')
                if vendor != vendor[:2] + '.' + symbol or (not index and not (
                    exchange == 'SSE' and symbol.startswith('6') or exchange == 'SZSE' and symbol.startswith(('0', '3')))):
                    raise ValueError('scope')
                if key.identity in seen or row['date'] != str(day) or row.get('adjustflag', '3') != '3':
                    raise ValueError('conflicting identity/date/adjustment')
                seen.add(key.identity)
                name = names.get(vendor)
                if not isinstance(name, str) or not name.strip() or len(name) > 200:
                    raise ValueError('name unknown')
                values = {f: number(row[f], positive=True) for f in ['open', 'high', 'low', 'close', 'preclose']}
                if values['close'] is None:
                    raise ValueError('price missing')
                if all(values[k] is not None for k in ['open', 'high', 'low']):
                    if not values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']:
                        raise ValueError('price range')
                # BaoStock documents shares/yuan for stocks; index units are
                # intentionally left unknown rather than borrowing stock units.
                volume = number(row.get('volume', '')) if not index else None
                if volume is not None and volume != volume.to_integral_value():
                    raise ValueError('non-integral shares')
                amount = number(row.get('amount', '')) if not index else None
                records.append(DailyQuote(key, name.strip(), values['close'], values['preclose'], values['open'],
                    values['high'], values['low'], received_at, day,
                    format(volume, 'f') if volume is not None else None,
                    format(amount, 'f') if amount is not None else None, 'point' if index else 'CNY'))
            except (KeyError, TypeError, ValueError, SourceFailure):
                rejected.append('invalid_daily_row')
        # Duplicate rows, even identical, quarantine that identity as a whole.
        counts = {}
        for row in raw:
            counts[row.get('code')] = counts.get(row.get('code'), 0) + 1
        unique = [q for q in records if counts[{'SSE': 'sh.', 'SZSE': 'sz.'}[q.instrument.exchange] + q.instrument.symbol] == 1]
        rejected.extend(['conflicting_daily_identity'] * (len(records) - len(unique)))
        records = unique
        current = received_at.astimezone(SHANGHAI)
        days = [(date.fromisoformat(r['calendar_date']), r['is_trading_day']) for r in data['calendar']]
        expected_dates = {date(current.year, current.month, n) for n in range(1, calendar.monthrange(current.year, current.month)[1]+1)}
        if len(days) != len(expected_dates) or {d for d, _ in days} != expected_dates or any(flag not in {'0', '1'} for _, flag in days):
            raise ValueError('incomplete calendar')
        missing = sum(not any(r.get('code') == {'SSE': 'sh.', 'SZSE': 'sz.'}[e] + s for r in raw) for e, s, _ in INDEXES)
        return DailyBatch(records, rejected + ['index_not_returned'] * missing, len(raw) + missing, [(d, flag == '1') for d, flag in days])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise SourceFailure('schema_changed') from None
