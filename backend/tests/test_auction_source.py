"""短线侠竞价封单表：协议校验、解码、字段解析与批次标识的离线契约测试。"""
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.evidence.common import content_hash
from app.market import protocol
from app.market.auction_source import (
    CHECKPOINTS,
    FAMILIES,
    FAMILY,
    HISTORY_FAMILY,
    STOCKS_COLUMNS,
    STOCKS_DATASET,
    SUMMARY_COLUMNS,
    SUMMARY_DATASET,
    _decrypt_block,
    _round_keys,
    auction_url,
    fetch_auction,
    parse_auction,
    parse_history,
    source_keys,
)
from app.market.protocol import SourceFailure

FIXTURES = Path(__file__).parent / 'fixtures/auction'
RECEIVED = datetime(2026, 10, 21, 1, 26, 0, tzinfo=UTC)  # 上海 09:26，与当前表日期同日


def body(name):
    return (FIXTURES / name).read_bytes()


def head(day, one_word='3', total='7.6亿', live=False):
    extra = " | <i style='font-weight:normal;color:#555;'>涨幅</i>" if live else ''
    return (f'{day}<br><span>一字:{one_word}个  |  封单:{total}</span><br>'
            f"<div class='time'><i>9:15</i> | <i>9:20</i> | <i>9:25</i>{extra}</div>")


def row(code, name='样本', tags=('机器人',), cells=('1亿', '1亿', '1亿'), pct=None):
    tag_html = ''.join(f'<p>{tag}</p>' for tag in tags)
    inner = f"<b>{name}<i>{tag_html}</i></b><br>" + ' | '.join(f'<span>{cell}</span>' for cell in cells)
    if pct is not None:
        inner += f" | <span class='zf'><span style='color:red'>{pct}</span></span>"
    return f"<td code='{code}' class='fd st{code}' >{inner}</td>"


def single_day(rows, day='2026-10-21', totals=('4.2亿', '7.1亿', '7.6亿'), header=None):
    return json.dumps({'th': header or head(day), 't15': totals[0], 't20': totals[1], 't25': totals[2],
                       'table': '<table class="table stock">' + ''.join(rows) + '</table>'},
                      ensure_ascii=False).encode()


def test_frozen_names_columns_and_datasets():
    assert FAMILY == 'duanxianxia_auction'
    assert CHECKPOINTS == ('09:15', '09:20', '09:25')
    assert (SUMMARY_DATASET, STOCKS_DATASET) == ('market.duanxianxia.auction.summary', 'market.duanxianxia.auction.stocks')
    assert [column[0] for column in SUMMARY_COLUMNS] == [
        'trade_day', 'source_date_raw', 'source_header_text', 'table_kind', 'row_count', 'one_word_count', 'header_total_raw',
        'header_total_cny', 'total_fd_0915_raw', 'total_fd_0920_raw', 'total_fd_0925_raw',
        'total_fd_0915_cny', 'total_fd_0920_cny', 'total_fd_0925_cny', 'batch_hash']
    assert [column[0] for column in STOCKS_COLUMNS] == [
        'trade_day', 'source_order', 'code', 'name', 'tags_raw', 'board_raw', 'fd_0915_raw', 'fd_0920_raw',
        'fd_0925_raw', 'fd_0915_cny', 'fd_0920_cny', 'fd_0925_cny', 'change_pct_raw', 'change_pct', 'batch_hash']


def test_encrypted_current_table_is_decoded_and_split_into_two_payloads():
    batch = parse_auction(body('duanxianxia_current_table.b64'), RECEIVED, 'current')
    assert (batch.expected, batch.rejected, batch.requests) == (5, 0, 1)
    assert batch.batch_hash and len(batch.batch_hash) == 64
    assert [payload.dataset for _, payload, _ in batch.records] == [SUMMARY_DATASET, STOCKS_DATASET]
    summary = batch.records[0][1].rows[0]
    assert summary['trade_day'] == '2026-10-21' and summary['source_date_raw'] == '2026-10-21'
    assert summary['table_kind'] == 'current' and summary['row_count'] == '4'
    assert summary['one_word_count'] == '3'
    assert (summary['header_total_raw'], summary['header_total_cny']) == ('7.6亿', '760000000')
    assert (summary['total_fd_0915_raw'], summary['total_fd_0925_cny']) == ('4.2亿', '760000000')
    assert batch.records[0][1].observed_day.isoformat() == '2026-10-21'


def test_units_raw_strings_and_display_order_are_preserved():
    rows = parse_auction(body('duanxianxia_current_table.b64'), RECEIVED, 'current').records[1][1].rows
    assert [row['code'] for row in rows] == ['600001', '000002', '300003', '600004']
    assert [row['source_order'] for row in rows] == ['1', '2', '3', '4']
    first, second = rows[0], rows[1]
    assert (first['fd_0915_raw'], first['fd_0915_cny']) == ('3.2亿', '320000000')
    assert (first['fd_0925_raw'], first['fd_0925_cny']) == ('5亿', '500000000')
    assert (second['fd_0915_raw'], second['fd_0915_cny']) == ('8845万', '88450000')
    assert first['tags_raw'] == '机器人|商业航天|2板' and first['board_raw'] == '2板'
    assert (first['change_pct_raw'], first['change_pct']) == ('10.01%', '10.01')
    assert (rows[2]['change_pct_raw'], rows[2]['change_pct']) == ('-1.20%', '-1.20')


def test_missing_is_null_and_zero_stays_zero():
    rows = {row['code']: row for row in
            parse_auction(body('duanxianxia_current_table.b64'), RECEIVED, 'current').records[1][1].rows}
    missing = rows['000002']
    assert (missing['fd_0925_raw'], missing['fd_0925_cny']) == ('-', None)
    zero = rows['300003']
    assert (zero['fd_0915_raw'], zero['fd_0915_cny']) == ('0万', '0')
    assert (zero['fd_0920_raw'], zero['fd_0920_cny']) == ('0', '0')
    blank = rows['600004']
    assert [blank[key] for key in ('fd_0915_cny', 'fd_0920_cny', 'fd_0925_cny')] == [None, None, None]
    assert blank['tags_raw'] == '昨首板' and blank['board_raw'] is None


def test_empty_tags_are_dropped_not_turned_into_values():
    rows = {row['code']: row for row in
            parse_auction(body('duanxianxia_current_table.b64'), RECEIVED, 'current').records[1][1].rows}
    assert rows['300003']['tags_raw'] == '并购重组|首板'


def test_history_returns_one_batch_per_day_with_its_own_hash():
    batches = parse_history(body('duanxianxia_history_days.json'), RECEIVED)
    assert [batch.records[0][1].rows[0]['trade_day'] for batch in batches] == ['2026-10-19', '2026-10-20']
    assert {batch.batch_hash for batch in batches} == {hashlib.sha256(body('duanxianxia_history_days.json')).hexdigest()}
    for batch in batches:
        summary, stocks = batch.records[0][1].rows[0], batch.records[1][1].rows
        assert summary['table_kind'] == 'history'
        assert {row['batch_hash'] for row in stocks} == {summary['batch_hash']} == {batch.batch_hash}
        assert all(row['change_pct'] is None and row['change_pct_raw'] is None for row in stocks)
    empty_day = batches[0].records[0][1].rows[0]
    assert (empty_day['total_fd_0915_raw'], empty_day['total_fd_0915_cny']) == ('-', None)
    assert (empty_day['total_fd_0920_raw'], empty_day['total_fd_0920_cny']) == ('0', '0')


def test_empty_table_writes_no_batch():
    with pytest.raises(SourceFailure) as error:
        parse_auction(single_day([]), RECEIVED, 'history')
    assert error.value.reason == 'instrument_unavailable'
    with pytest.raises(SourceFailure) as error:
        parse_history(b'{}', RECEIVED)
    assert error.value.reason == 'instrument_unavailable'


def test_duplicate_code_is_counted_as_rejected_and_written_once():
    batch = parse_auction(single_day([row('600001'), row('600001')]), RECEIVED, 'history')
    assert (batch.expected, batch.rejected) == (3, 1)
    assert [item['code'] for item in batch.records[1][1].rows] == ['600001']


def test_header_must_declare_the_three_checkpoints_in_order():
    swapped = head('2026-10-21').replace('<i>9:15</i> | <i>9:20</i>', '<i>9:20</i> | <i>9:15</i>')
    with pytest.raises(SourceFailure) as error:
        parse_auction(single_day([row('600001')], header=swapped), RECEIVED, 'history')
    assert error.value.reason == 'schema_changed'


def test_plain_or_tampered_current_body_is_refused():
    with pytest.raises(SourceFailure) as error:
        parse_auction(single_day([row('600001')]), RECEIVED, 'current')
    assert error.value.reason == 'schema_changed'
    ciphertext = bytearray(base64.b64decode(body('duanxianxia_current_table.b64')))
    ciphertext[20] ^= 0xFF
    with pytest.raises(SourceFailure) as error:
        parse_auction(base64.b64encode(bytes(ciphertext)), RECEIVED, 'current')
    assert error.value.reason == 'schema_changed'


def test_aes_256_matches_the_published_known_answer_vector():
    key = bytes(range(32))
    ciphertext = bytes.fromhex('8ea2b7ca516745bfeafc49904b496089')
    assert _decrypt_block(ciphertext, _round_keys(key)).hex() == '00112233445566778899aabbccddeeff'


def test_batch_hash_is_content_bound_and_source_keys_pair_the_tables():
    first = parse_auction(body('duanxianxia_current_table.b64'), RECEIVED, 'current')
    again = parse_auction(body('duanxianxia_current_table.b64'), RECEIVED, 'current')
    assert first.batch_hash == again.batch_hash
    changed = parse_auction(single_day([row('600001', cells=('1亿', '2亿', '3亿'))]), RECEIVED, 'history')
    assert changed.batch_hash != parse_auction(single_day([row('600001')]), RECEIVED, 'history').batch_hash
    assert source_keys(FAMILY, '2026-10-21', first.batch_hash) == (
        f'auction:{FAMILY}:2026-10-21:summary',
        f'auction:{FAMILY}:2026-10-21:stocks')
    assert hashlib.sha256(body('duanxianxia_current_table.b64')).hexdigest() == first.batch_hash


def test_each_table_kind_keeps_its_own_family_in_hash_and_keys():
    assert FAMILIES == {'duanxianxia_auction', 'duanxianxia_auction_history'}
    history = parse_auction(single_day([row('600001')]), RECEIVED, 'history')
    assert hashlib.sha256(single_day([row('600001')])).hexdigest() == history.batch_hash
    assert source_keys(HISTORY_FAMILY, '2026-10-21', history.batch_hash)[1].endswith(':stocks')
    with pytest.raises(SourceFailure) as error:
        source_keys('eastmoney_auction', '2026-10-21', history.batch_hash)
    assert error.value.reason == 'capability_not_implemented'


def test_future_source_date_is_refused():
    with pytest.raises(SourceFailure) as error:
        parse_auction(single_day([row('600001')], day='2026-10-22', header=head('2026-10-22')), RECEIVED, 'history')
    assert error.value.reason == 'future_source_date'


def test_history_key_must_match_its_header_date():
    payload = json.loads(body('duanxianxia_history_days.json'))
    payload['2026-10-18'] = payload.pop('2026-10-19')
    with pytest.raises(SourceFailure) as error:
        parse_history(json.dumps(payload, ensure_ascii=False).encode(), RECEIVED)
    assert error.value.reason == 'schema_changed'


def test_unknown_kind_and_endpoints_stay_inside_the_allowlist():
    with pytest.raises(SourceFailure) as error:
        parse_auction(body('duanxianxia_current_table.b64'), RECEIVED, 'strength')
    assert error.value.reason == 'capability_not_implemented'
    assert auction_url('current', cache_bust=False) == 'https://duanxianxia.com/vendor/stockdata/jjlive.json'
    assert auction_url('history') == 'https://duanxianxia.com/api/getFengdanLast'
    assert auction_url('current').startswith('https://duanxianxia.com/vendor/stockdata/jjlive.json?_=')
    assert 'duanxianxia.com' in protocol.ALLOWED_HOSTS
    assert fetch_auction  # 只在真实验证中调用，离线测试不发起请求
