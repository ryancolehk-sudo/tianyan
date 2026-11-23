"""Synthetic Sina minute fixtures only; no network or real market payloads.

These tests exercise the parser boundary, not upstream availability or live SLA.
The normal suite retains its disposable PostgreSQL guard in conftest.py.
"""
import copy
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from app.market.minute import minute_url, parse_sina_minutes
from app.market.protocol import InstrumentKey, SHANGHAI, SourceFailure


DAY = date(2026, 10, 23)
RECEIVED = datetime(2026, 10, 23, 7, 1, tzinfo=UTC)
STOCK = InstrumentKey('SSE', '603278', 'stock')


def synthetic_row(at='2026-10-23 09:31:00', **changes):
    row = dict(day=at, open='10.01', high='12.02', low='9.03', close='11.04',
               volume='100', amount='1104.00')
    row.update(changes)
    return row


def synthetic_jsonp(rows):
    return ('=(' + json.dumps(rows, ensure_ascii=True) + ');').encode()


def parsed(rows, *, instrument=STOCK, interval='1m', received=RECEIVED, day=DAY):
    return parse_sina_minutes(synthetic_jsonp(rows), instrument, interval, received, day)


def timestamps(batch):
    return [record.source_time for record in batch.records]


def test_prices_and_native_units_retain_exact_decimals_without_claiming_finality():
    row = synthetic_row(open='10.01000001', close='11.04000001',
                        volume='0', amount='0')
    batch = parsed([row])
    assert len(batch.records) == 1 and not batch.rejected
    observation = batch.records[0]
    assert observation.instrument == STOCK and observation.interval == '1m'
    assert observation.source_time == datetime(2026, 10, 23, 9, 31, tzinfo=SHANGHAI)
    assert observation.open == Decimal('10.01000001')
    assert observation.close == Decimal('11.04000001')
    assert observation.volume_native == Decimal('0')
    assert observation.amount_native == Decimal('0')
    # A source stamp must not be promoted to an invented bar-end or finality.
    assert getattr(observation, 'bar_end_at', None) is None
    assert getattr(observation, 'closed', None) is not True
    assert getattr(observation, 'finality', None) not in {'confirmed', 'final'}


@pytest.mark.parametrize('missing', [None, '', '-', '--'])
def test_missing_native_volume_and_amount_allow_prices_but_do_not_become_zero(missing):
    result = parsed([synthetic_row(volume=missing, amount=missing)])
    assert len(result.records) == 1
    assert result.records[0].volume_native is None
    assert result.records[0].amount_native is None


def test_absent_native_fields_are_unknown_and_field_order_is_irrelevant():
    row = synthetic_row()
    del row['volume']
    del row['amount']
    reverse_keys = dict(reversed(list(row.items())))
    before = copy.deepcopy(reverse_keys)
    result = parsed([reverse_keys])
    assert len(result.records) == 1
    assert result.records[0].close == Decimal('11.04')
    assert result.records[0].volume_native is None
    assert result.records[0].amount_native is None
    assert reverse_keys == before


def test_sorting_duplicates_and_midday_gap_preserve_only_actual_points():
    rows = [synthetic_row('2026-10-23 13:01:00'),
            synthetic_row('2026-10-23 09:31:00'),
            synthetic_row('2026-10-23 11:30:00'),
            synthetic_row('2026-10-23 09:31:00')]
    result = parsed(rows)
    assert timestamps(result) == [datetime(2026, 10, 23, 9, 31, tzinfo=SHANGHAI),
                                  datetime(2026, 10, 23, 11, 30, tzinfo=SHANGHAI),
                                  datetime(2026, 10, 23, 13, 1, tzinfo=SHANGHAI)]
    assert not result.rejected
    assert result.expected == 3


def test_previous_days_are_not_relabelled_as_requested_day():
    result = parsed([synthetic_row('2026-10-22 09:31:00'),
                     synthetic_row('2026-10-23 09:31:00')])
    assert len(result.records) == 1
    assert result.records[0].source_time.date() == DAY
    assert result.expected == 1 and not result.rejected
    old_only = parsed([synthetic_row('2026-10-22 09:31:00')])
    assert old_only.records == []


@pytest.mark.parametrize('changes', [dict(close='10.50'), dict(volume='101'), dict(amount='1105')])
def test_conflicting_same_stamp_rejects_that_slot_without_discarding_other_slots(changes):
    conflict = synthetic_row(**changes)
    good = synthetic_row('2026-10-23 09:32:00')
    forward = parsed([synthetic_row(), conflict, good])
    reverse = parsed([good, conflict, synthetic_row()])
    assert timestamps(forward) == timestamps(reverse) == [
        datetime(2026, 10, 23, 9, 32, tzinfo=SHANGHAI)]
    assert forward.rejected and reverse.rejected


@pytest.mark.parametrize('field,value', [
    ('open', '0'), ('close', '-1'), ('high', 'NaN'), ('low', 'Infinity'),
    ('close', 'abc'), ('open', None), ('close', True),
    ('high', '10'), ('low', '11'), ('open', '13'),
    ('volume', '-1'), ('amount', '-0.01'), ('volume', 'NaN'),
    ('amount', 'Infinity'), ('volume', True),
])
def test_bad_row_never_becomes_a_zero_or_repaired_candle(field, value):
    result = parsed([synthetic_row(**{field: value}),
                     synthetic_row('2026-10-23 09:32:00')])
    assert timestamps(result) == [datetime(2026, 10, 23, 9, 32, tzinfo=SHANGHAI)]
    assert result.rejected


@pytest.mark.parametrize('stamp', [
    '2026-10-23 09:31:01', '2026-10-23 15:02:00',
    '2026-10-23 99:31:00', 'not-a-date', '', None,
])
def test_invalid_or_future_source_time_is_rejected_without_using_receipt_as_stamp(stamp):
    result = parsed([synthetic_row(stamp)])
    assert not result.records and result.rejected


def test_source_stamp_is_not_shifted_and_utc_receipt_is_compared_as_an_instant():
    received = datetime(2026, 10, 23, 1, 31, tzinfo=UTC)
    result = parsed([synthetic_row()], received=received)
    assert timestamps(result) == [datetime(2026, 10, 23, 9, 31, tzinfo=SHANGHAI)]
    too_early = datetime(2026, 10, 23, 1, 30, 59, tzinfo=UTC)
    result = parsed([synthetic_row()], received=too_early)
    assert not result.records and result.rejected


@pytest.mark.parametrize('interval', ['1m', '5m', '15m', '30m', '60m'])
def test_supported_intervals_keep_the_source_stamp_and_never_fill_missing_rows(interval):
    result = parsed([synthetic_row('2026-10-23 10:00:00')], interval=interval)
    assert len(result.records) == 1
    assert result.records[0].interval == interval
    assert result.records[0].source_time == datetime(2026, 10, 23, 10, 0, tzinfo=SHANGHAI)


@pytest.mark.parametrize('instrument', [InstrumentKey('BSE', '920001', 'stock'),
    InstrumentKey('SSE', '000001', 'index'), InstrumentKey('SSE', '510300', 'etf'),
    InstrumentKey('SSE', '000001', 'stock'), InstrumentKey('SZSE', '399001', 'stock')])
def test_unsupported_or_ambiguous_identity_cannot_be_silently_mapped(instrument):
    with pytest.raises(SourceFailure):
        minute_url(instrument, '1m')
    with pytest.raises(SourceFailure):
        parsed([synthetic_row()], instrument=instrument)


@pytest.mark.parametrize('interval', ['1d', '1MIN', '0m', '2m', '', '120m'])
def test_unsupported_period_never_falls_back_to_daily_or_one_minute(interval):
    with pytest.raises(SourceFailure):
        minute_url(STOCK, interval)
    with pytest.raises(SourceFailure):
        parsed([synthetic_row()], interval=interval)


@pytest.mark.parametrize('stock,prefix', [(STOCK, 'sh603278'),
    (InstrumentKey('SZSE', '000001', 'stock'), 'sz000001')])
def test_fixed_minute_url_preserves_market_identity_and_bounded_rows(stock, prefix):
    url = urlsplit(minute_url(stock, '5m'))
    assert url.scheme == 'https' and url.hostname == 'quotes.sina.cn'
    assert not url.username and not url.password and not url.fragment
    query = parse_qs(url.query)
    assert query['symbol'] == [prefix]
    assert query['scale'] == ['5']
    assert 1 <= int(query['datalen'][0]) <= 1970


@pytest.mark.parametrize('body', [b'', b'<html>synthetic error</html>',
    b'=(not_json);', b'=({"error":"synthetic"});', b'=(null);',
    b'alert(1);=([]);', b'=([]);alert(1)', b'=([]);/* extra JavaScript */',
    b'=([{"day":"x",}]);', b'=([]);\x00', b'x' * (2_000_001)])
def test_unsafe_or_malformed_envelope_is_never_executed_or_treated_as_no_trading(body):
    with pytest.raises(SourceFailure):
        parse_sina_minutes(body, STOCK, '1m', RECEIVED, DAY)


def test_empty_array_is_an_honest_empty_batch_not_a_flat_candle():
    result = parsed([])
    assert not result.records and not result.rejected and result.expected == 0


def test_explicit_contradictory_symbol_is_not_accepted_as_requested_stock():
    result = parsed([synthetic_row(symbol='sz000001')])
    assert not result.records and result.rejected


def test_non_object_rows_remain_rejected_without_hiding_a_valid_row():
    result = parsed([None, 'synthetic bad row', 10, synthetic_row()])
    assert len(result.records) == 1
    assert len(result.rejected) == 3


@pytest.mark.parametrize('body', [
    b'=([{ "day":"2026-10-23 09:31:00", "open":"10", "open":"11" }]);',
    b'=([{ "day":"2026-10-23 09:31:00", "open":NaN }]);',
    b'=([{ "day":"2026-10-23 09:31:00", "volume":Infinity }]);',
    b'=([\xff]);',
    b'=([]);=([]);',
])
def test_ambiguous_keys_non_json_numbers_and_non_utf8_cannot_change_observations(body):
    with pytest.raises(SourceFailure):
        parse_sina_minutes(body, STOCK, '1m', RECEIVED, DAY)


def test_row_bound_is_checked_before_duplicate_collapse():
    with pytest.raises(SourceFailure):
        parsed([synthetic_row()] * 1971)


def test_naive_receipt_is_not_assumed_to_be_shanghai_or_utc():
    with pytest.raises(SourceFailure):
        parsed([synthetic_row()], received=datetime(2026, 10, 23, 15, 1))
