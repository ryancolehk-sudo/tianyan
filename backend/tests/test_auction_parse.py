"""Call-auction parser contract: fixed names, units, batch pairing and time honesty."""
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.evidence.common import content_hash
from app.market.auction import (CHECKPOINTS, DETAIL_COLUMNS, STOCKS_DATASET, SUMMARY_COLUMNS, SUMMARY_DATASET,
    AUCTION_FAMILIES, parse_auction, source_keys)
from app.market.protocol import SHANGHAI, InstrumentKey, SourceFailure

FIXTURES = Path(__file__).parent / 'fixtures/auction'
RECEIVED = datetime(2026, 10, 21, 1, 20, 40, tzinfo=UTC)  # 09:20:40 上海
SELECTION = [InstrumentKey('SSE', '600000', 'stock'), InstrumentKey('SZSE', '000001', 'stock')]


def body(name):
    return (FIXTURES / name).read_bytes()


def test_frozen_names_and_columns():
    assert AUCTION_FAMILIES == {'eastmoney_auction', 'tencent_auction'}
    assert CHECKPOINTS == ('09:15', '09:20', '09:25')
    assert (SUMMARY_DATASET, STOCKS_DATASET) == ('market.auction.summary', 'market.auction.stocks')
    assert [column[0] for column in SUMMARY_COLUMNS] == [
        'trade_day', 'checkpoint', 'instrument_count', 'quoted_count', 'rejected_count', 'total_volume_shares',
        'total_amount_cny', 'up_count', 'down_count', 'flat_count', 'batch_hash']
    assert [column[0] for column in DETAIL_COLUMNS] == [
        'trade_day', 'checkpoint', 'code', 'name', 'price_cny', 'prev_close_cny', 'change_pct',
        'volume_shares', 'amount_cny', 'quote_time', 'batch_hash']


def test_eastmoney_summary_and_detail_share_one_batch_hash():
    batch = parse_auction(body('eastmoney_0920.json'), RECEIVED, SELECTION, 'eastmoney_auction')
    assert batch.batch_hash and len(batch.batch_hash) == 64
    assert (batch.expected, batch.rejected, batch.requests) == (3, 0, 1)
    assert [payload.dataset for _, payload, _ in batch.records] == [SUMMARY_DATASET, STOCKS_DATASET]
    summary, detail = batch.records[0][1].rows[0], batch.records[1][1].rows
    assert len(detail) == 2
    assert {row['batch_hash'] for row in detail} == {summary['batch_hash']} == {batch.batch_hash}
    assert summary['checkpoint'] == '09:20' and summary['quoted_count'] == '2'
    assert summary['up_count'] == '1' and summary['down_count'] == '1' and summary['flat_count'] == '0'
    # 手 -> 股, 元 unchanged; totals only when every row carries the value.
    assert [row['code'] for row in detail] == ['000001', '600000']
    assert detail[1]['volume_shares'] == '3812000'
    assert detail[1]['amount_cny'] == '37065840'
    assert summary['total_volume_shares'] == '5952000'
    assert summary['total_amount_cny'] == '61675840'
    assert detail[1]['quote_time'] == '2026-10-21T09:20:05+08:00'
    assert summary['trade_day'] == '2026-10-21'


def test_tencent_uses_exact_yuan_amount_and_lot_conversion():
    batch = parse_auction(body('tencent_0925.txt'), datetime(2026, 10, 21, 1, 25, 20, tzinfo=UTC),
                          [InstrumentKey('SSE', '600000', 'stock')], 'tencent_auction')
    summary, detail = batch.records[0][1].rows[0], batch.records[1][1].rows
    assert summary['checkpoint'] == '09:25'
    assert detail[0]['amount_cny'] == '37065840'  # exact 元 field, not rounded 万元×10000
    assert detail[0]['volume_shares'] == '3812000'
    assert detail[0]['change_pct'] == '-0.41'


def test_unknown_source_time_never_becomes_a_checkpoint():
    payload = json.loads(body('eastmoney_0920.json'))
    payload['data']['diff'][0].pop('f124')
    payload['data']['diff'][1].pop('f124')
    batch = parse_auction(json.dumps(payload).encode(), RECEIVED, SELECTION, 'eastmoney_auction')
    summary, detail = batch.records[0][1].rows[0], batch.records[1][1].rows
    assert summary['checkpoint'] is None
    assert all(row['quote_time'] is None for row in detail)
    assert summary['trade_day'] == '2026-10-21'  # received date only, never an intraday clock


def test_off_checkpoint_time_is_not_labelled():
    payload = json.loads(body('eastmoney_0920.json'))
    for row in payload['data']['diff']:
        row['f124'] = 1792549200  # 10:20:00 上海, outside every checkpoint window
    batch = parse_auction(json.dumps(payload).encode(), datetime(2026, 10, 21, 2, 20, 5, tzinfo=UTC),
                          SELECTION, 'eastmoney_auction')
    assert batch.records[0][1].rows[0]['checkpoint'] is None
    assert batch.records[1][1].rows[0]['quote_time'].endswith('+08:00')


def test_future_source_time_is_rejected():
    payload = json.loads(body('eastmoney_0920.json'))
    for row in payload['data']['diff']:
        row['f124'] = 1792553250
    with pytest.raises(SourceFailure) as error:
        parse_auction(json.dumps(payload).encode(), RECEIVED, SELECTION, 'eastmoney_auction')
    assert error.value.reason == 'future_source_time'


def test_all_rows_unavailable_writes_no_batch():
    payload = json.loads(body('eastmoney_0920.json'))
    for row in payload['data']['diff']:
        row['f2'] = '-'
    with pytest.raises(SourceFailure) as error:
        parse_auction(json.dumps(payload).encode(), RECEIVED, SELECTION, 'eastmoney_auction')
    assert error.value.reason == 'instrument_unavailable'


def test_unexpected_symbol_and_non_stock_are_refused():
    payload = json.loads(body('eastmoney_0920.json'))
    payload['data']['diff'][0]['f12'] = '600519'
    with pytest.raises(SourceFailure) as error:
        parse_auction(json.dumps(payload).encode(), RECEIVED, SELECTION, 'eastmoney_auction')
    assert error.value.reason == 'instrument_mismatch'
    with pytest.raises(SourceFailure) as error:
        parse_auction(body('eastmoney_0920.json'), RECEIVED, [InstrumentKey('SSE', '000001', 'index')], 'eastmoney_auction')
    assert error.value.reason == 'unsupported_asset_scope'


def test_missing_amount_keeps_total_unknown_instead_of_partial_sum():
    lines = body('tencent_0925.txt').decode('gb18030').replace('9.72/38120/37065840', '9.72/38120/-')
    batch = parse_auction(lines.encode('gb18030'), datetime(2026, 10, 21, 1, 25, 20, tzinfo=UTC),
                          [InstrumentKey('SSE', '600000', 'stock')], 'tencent_auction')
    summary, detail = batch.records[0][1].rows[0], batch.records[1][1].rows
    assert detail[0]['amount_cny'] is None
    assert summary['total_amount_cny'] is None


def test_batch_hash_is_deterministic_and_content_bound():
    first = parse_auction(body('eastmoney_0920.json'), RECEIVED, SELECTION, 'eastmoney_auction')
    again = parse_auction(body('eastmoney_0920.json'), RECEIVED, SELECTION, 'eastmoney_auction')
    assert first.batch_hash == again.batch_hash
    payload = json.loads(body('eastmoney_0920.json'))
    payload['data']['diff'][1]['f2'] = 11.51
    changed = parse_auction(json.dumps(payload).encode(), RECEIVED, SELECTION, 'eastmoney_auction')
    assert changed.batch_hash != first.batch_hash
    assert source_keys('eastmoney_auction', '2026-10-21', first.batch_hash) == (
        f'auction:eastmoney_auction:2026-10-21:{first.batch_hash[:16]}:summary',
        f'auction:eastmoney_auction:2026-10-21:{first.batch_hash[:16]}:stocks')
    stripped = [{key: value for key, value in row.items() if key != 'batch_hash'} for row in first.records[1][1].rows]
    assert content_hash({'family': 'eastmoney_auction', 'trade_day': '2026-10-21', 'checkpoint': '09:20',
                         'detail': stripped}) == first.batch_hash
