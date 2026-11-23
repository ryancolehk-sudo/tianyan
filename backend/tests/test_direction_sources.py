"""Self-authored public-ranking fixtures; no live requests or personal database."""
import io
import json
from datetime import UTC, date, datetime
from urllib.error import HTTPError
from urllib.parse import parse_qs

import pytest

from app.market import direction_sources as sources
from app.market.protocol import NoRedirect, SourceFailure

RECEIVED = datetime(2026, 10, 26, 4, tzinfo=UTC)
SOURCE_DAY = date(2026, 10, 23)


@pytest.fixture(autouse=True)
def isolated_database():
    # This module tests only pure parsers and a mocked transport. Override the
    # shared integration fixture so it cannot truncate any database.
    yield


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def refused(*args, **kwargs):
        pytest.fail('Synthetic direction-source tests must not contact an upstream')
    monkeypatch.setattr(sources.urllib.request, 'build_opener', refused)


def hot_fixture(family='direction_ths_hot', count=12):
    if family == 'direction_ths_hot':
        return {'status_code': 0, 'data': {'stock_list': [
            {'order': index, 'code': f'000{index:03}', 'name': f'合成股票{index}',
                'rate': '120.2500', 'hot_rank_chg': -index, 'rise_and_fall': '-1.2500'}
            for index in range(1, count + 1)]}}
    if family == 'direction_em_hot':
        return {'code': 0, 'data': [{'sc': f'SZ000{index:03}', 'rk': index, 'rc': 99, 'hisRc': -99}
            for index in range(1, count + 1)]}
    columns = ['market', 'sec_code', 'stock_name', 'chg', 'now_price', 'occur_time', 'gnbk',
        'reason', 'riseDay', 'lhb', 'ranking', 'totalCount', 'jgid']
    return [[0, '', count, '', ''], columns, [], *[
        ['0', f'000{index:03}', f'合成股票{index}', '-1.25', '5.00', '2026-10-23',
            '[]', '', '', '', str(index), '12345', 'unused']
        for index in range(1, count + 1)]]


def strength_fixture(count=30):
    return {'errcode': '0', 'Day': ['2026-10-23'], 'Count': count,
        # It is intentionally a different day: this server metadata is not
        # evidence that any historical theme was ranked on the receipt day.
        'Time': 1792998000,
        'list': [[f'801{index:03}', f'合成题材{index}', 10000 - index, *([0] * 16)]
            for index in range(1, count + 1)]}


def normalized(family, value=None, **kwargs):
    value = value if value is not None else (
        {'errcode': '0', 'info': [{'Day': SOURCE_DAY.isoformat(), 'strong': '45'}]}
        if family == 'direction_kpl_emotion' else
        strength_fixture() if family == 'direction_kpl_strength' else hot_fixture(family))
    return sources.parse(family, json.dumps(value, ensure_ascii=False).encode(), RECEIVED, **kwargs)


@pytest.mark.parametrize('family', sorted(sources.FAMILIES))
def test_each_source_has_one_bounded_top_ten_reference_with_no_security_or_event_claim(family):
    batch = normalized(family)
    assert batch.expected == batch.requests == len(batch.records) == 1
    assert batch.rejected == 0 and batch.bars == []
    identity, payload, instrument = batch.records[0]
    assert instrument is None and identity == payload.dataset
    assert payload.dataset == 'market.direction.' + family.removeprefix('direction_')
    if family == 'direction_kpl_emotion':
        # This source is a scalar observation, never a ranked security table.
        assert payload.rows == [dict(value='45', source_day=SOURCE_DAY.isoformat(),
                                    sample_received_at=RECEIVED.isoformat())]
        assert payload.observed_day == SOURCE_DAY
        assert {column.key for column in payload.columns} == {'value', 'source_day', 'sample_received_at'}
        return
    assert len(payload.rows) == 10
    assert [row['rank'] for row in payload.rows] == [str(i) for i in range(1, 11)]
    assert all(row['source_time'] is None for row in payload.rows)
    assert all('published_at' not in row and 'event_at' not in row for row in payload.rows)
    assert all(set(row) == {column.key for column in payload.columns} for row in payload.rows)
    assert '买入信号' in payload.scope


def test_ths_uses_only_verified_heat_rank_change_and_pct_and_keeps_missing_values_null():
    value = hot_fixture()
    value['data']['stock_list'][0].update({'rate': '0', 'hot_rank_chg': 0, 'rise_and_fall': None})
    value['data']['stock_list'][1].pop('hot_rank_chg')
    value['data']['stock_list'].reverse()
    payload = normalized('direction_ths_hot', value).records[0][1]
    assert payload.rows[0]['heat'] == '0' and payload.rows[0]['rank_change'] == '0'
    assert payload.rows[0]['pct_change'] is None
    assert payload.rows[1]['heat'] == '120.25' and payload.rows[1]['rank_change'] is None
    assert payload.rows[1]['pct_change'] == '-1.25'
    assert payload.observed_day == date(2026, 10, 26)
    assert all(row['source_day'] is None for row in payload.rows)
    assert 'observed_day_basis=received_at_snapshot' in payload.scope
    assert 'received_at仅为采集时间' in payload.scope and '不是行情时点或当日收盘榜' in payload.scope


def test_em_does_not_invent_names_heat_or_changes_from_unverified_rc_fields():
    payload = normalized('direction_em_hot').records[0][1]
    assert payload.rows[0]['code'] == '000001'
    for row in payload.rows:
        assert all(row[key] is None for key in ('name', 'heat', 'rank_change', 'pct_change', 'source_day', 'source_time'))
    assert payload.observed_day == date(2026, 10, 26)
    assert 'rc/hisRc语义未核实' in payload.scope


@pytest.mark.parametrize('encoding', ['utf-8', 'gb18030'])
def test_tdx_accepts_its_actual_unicode_encodings_and_source_day_without_faking_clock(encoding):
    raw = json.dumps(hot_fixture('direction_tdx_hot'), ensure_ascii=False).encode(encoding)
    payload = sources.parse('direction_tdx_hot', raw, RECEIVED).records[0][1]
    assert payload.rows[0]['name'] == '合成股票1'
    assert payload.observed_day == SOURCE_DAY
    assert payload.rows[0]['pct_change'] == '-1.25'
    assert payload.rows[0]['source_day'] == '2026-10-23'
    assert payload.rows[0]['source_time'] is None
    assert payload.rows[0]['heat'] is None and payload.rows[0]['rank_change'] is None
    assert 'observed_day_basis=source_day' in payload.scope


def test_tdx_uses_column_names_even_if_the_vendor_reorders_columns():
    value = hot_fixture('direction_tdx_hot')
    value[1].reverse()
    for row in value[3:]:
        row.reverse()
    assert normalized('direction_tdx_hot', value).records[0][1].rows[0]['code'] == '000001'


def test_kpl_ignores_server_clock_and_preserves_actual_theme_day_and_strength():
    payload = normalized('direction_kpl_strength', strength_fixture(240)).records[0][1]
    assert payload.observed_day == SOURCE_DAY
    assert payload.rows[0]['strength'] == '9999'
    assert all(row['source_day'] == '2026-10-23' and row['source_time'] is None for row in payload.rows)
    assert 'Time为服务器元信息' in payload.scope
    assert 'RealRankingInfo' in payload.scope and '强度降序' in payload.scope
    assert payload.source_url == sources.KPL_URL


def test_kpl_requested_history_must_match_returned_date_and_gets_historical_provenance():
    payload = normalized('direction_kpl_strength', requested_day=SOURCE_DAY).records[0][1]
    assert payload.observed_day == SOURCE_DAY and payload.source_url == sources.KPL_HISTORY_URL
    with pytest.raises(SourceFailure, match='source_date_mismatch'):
        normalized('direction_kpl_strength', requested_day=date(2026, 10, 22))


@pytest.mark.parametrize('family', ['direction_ths_hot', 'direction_em_hot', 'direction_tdx_hot'])
def test_current_hot_lists_cannot_be_backdated_by_a_requested_history_day(family):
    with pytest.raises(SourceFailure, match='historical_not_supported'):
        normalized(family, requested_day=SOURCE_DAY)
    with pytest.raises(SourceFailure, match='historical_not_supported') as error:
        sources.fetch(family, day=SOURCE_DAY)
    assert error.value.request_count == 0


@pytest.mark.parametrize('kind', ['duplicate_code', 'duplicate_rank', 'gap_rank', 'boolean_rank',
    'negative_heat', 'nonintegral_change', 'unsafe_name', 'missing_name', 'bad_code', 'too_many', 'fraction_rank'])
def test_bad_ths_rows_fail_atomically_even_when_bad_row_is_outside_visible_top_ten(kind):
    value = hot_fixture()
    row = value['data']['stock_list'][-1]
    if kind == 'duplicate_code':
        row['code'] = '000001'
    elif kind == 'duplicate_rank':
        row['order'] = 1
    elif kind == 'gap_rank':
        row['order'] = 99
    elif kind == 'boolean_rank':
        row['order'] = True
    elif kind == 'negative_heat':
        row['rate'] = '-1'
    elif kind == 'nonintegral_change':
        row['hot_rank_chg'] = '0.5'
    elif kind == 'unsafe_name':
        row['name'] = '<script>unsafe</script>'
    elif kind == 'missing_name':
        row.pop('name')
    elif kind == 'bad_code':
        row['code'] = 'not-a-stock'
    elif kind == 'too_many':
        value = hot_fixture(count=101)
    else:
        row['order'] = '1.25'
    with pytest.raises(SourceFailure, match='schema_changed'):
        normalized('direction_ths_hot', value)


@pytest.mark.parametrize('kind', ['exchange_mismatch', 'missing_rank', 'invalid_prefix', 'duplicate_code'])
def test_bad_em_identity_or_rank_is_not_a_successful_empty_table(kind):
    value = hot_fixture('direction_em_hot')
    row = value['data'][-1]
    if kind == 'exchange_mismatch':
        row['sc'] = 'SH000012'
    elif kind == 'missing_rank':
        row.pop('rk')
    elif kind == 'invalid_prefix':
        row['sc'] = 'XX000012'
    else:
        row['sc'] = value['data'][0]['sc']
    with pytest.raises(SourceFailure, match='schema_changed'):
        normalized('direction_em_hot', value)


@pytest.mark.parametrize('kind', ['mixed_day', 'future_day', 'duplicate_column', 'missing_column',
    'wrong_width', 'bad_count', 'exchange_mismatch', 'bad_encoding', 'corrupted_name'])
def test_tdx_date_identity_encoding_and_table_shape_are_validated(kind):
    value = hot_fixture('direction_tdx_hot')
    if kind == 'mixed_day':
        value[-1][5] = '2026-10-22'
    elif kind == 'future_day':
        value[-1][5] = '2026-10-27'
    elif kind == 'duplicate_column':
        value[1][-1] = 'ranking'
    elif kind == 'missing_column':
        value[1][1] = 'security_code_changed'
    elif kind == 'wrong_width':
        value[-1].pop()
    elif kind == 'bad_count':
        value[0][2] += 1
    elif kind == 'exchange_mismatch':
        value[-1][0] = '1'
    elif kind == 'corrupted_name':
        value[-1][2] = '损坏\ufffd名称'
    else:
        with pytest.raises(SourceFailure, match='schema_changed'):
            sources.parse('direction_tdx_hot', b'\xff{not-json}', RECEIVED)
        return
    with pytest.raises(SourceFailure, match='schema_changed'):
        normalized('direction_tdx_hot', value)


@pytest.mark.parametrize('kind', ['mixed_days', 'future_day', 'invalid_date', 'wrong_width',
    'unsorted', 'missing_strength', 'duplicate_code', 'bad_count', 'too_many'])
def test_kpl_does_not_fabricate_top_ten_for_wrong_schema_order_or_date(kind):
    value = strength_fixture()
    if kind == 'mixed_days':
        value['Day'].append('2026-10-22')
    elif kind == 'future_day':
        value['Day'] = ['2026-10-27']
    elif kind == 'invalid_date':
        value['Day'] = ['2026-02-30']
    elif kind == 'wrong_width':
        value['list'][-1].pop()
    elif kind == 'unsorted':
        value['list'][12][2] = 999999
    elif kind == 'missing_strength':
        value['list'][0][2] = '--'
    elif kind == 'duplicate_code':
        value['list'][-1][0] = value['list'][0][0]
    elif kind == 'bad_count':
        value['Count'] = 1
    else:
        value = strength_fixture(401)
    with pytest.raises(SourceFailure, match='schema_changed'):
        normalized('direction_kpl_strength', value)


@pytest.mark.parametrize('raw', [b'', b'{}', b'[]', b'{"code":0,"code":1}', b'{"code":NaN}',
    b'{"code":Infinity}', b'x' * (sources.MAX_BODY + 1)],
    ids=['empty', 'object', 'array', 'duplicate-key', 'nan', 'infinity', 'oversized'])
def test_invalid_oversized_or_ambiguous_json_is_controlled(raw):
    with pytest.raises(SourceFailure, match='schema_changed'):
        sources.parse('direction_em_hot', raw, RECEIVED)


@pytest.mark.parametrize('family', sorted(sources.FAMILIES))
def test_vendor_error_messages_and_empty_lists_never_become_valid_data_or_error_text(family):
    value = ({'errcode': '0', 'info': [{'Day': SOURCE_DAY.isoformat(), 'strong': '45'}]}
             if family == 'direction_kpl_emotion' else
             strength_fixture() if family == 'direction_kpl_strength' else hot_fixture(family))
    if family == 'direction_tdx_hot':
        value[0][0] = 99
        value[0][1] = 'PRIVATE-UPSTREAM-MESSAGE'
    else:
        value['errcode' if family in {'direction_kpl_strength', 'direction_kpl_emotion'} else 'status_code' if family == 'direction_ths_hot' else 'code'] = 99
        value['message'] = 'PRIVATE-UPSTREAM-MESSAGE'
    with pytest.raises(SourceFailure) as error:
        normalized(family, value)
    assert error.value.reason == 'upstream_unavailable'
    assert 'PRIVATE' not in str(error.value)
    if family == 'direction_kpl_emotion':
        with pytest.raises(SourceFailure, match='schema_changed'):
            normalized(family, {'errcode': '0', 'info': []})
        return
    empty = strength_fixture() if family == 'direction_kpl_strength' else hot_fixture(family)
    if family == 'direction_tdx_hot':
        empty = empty[:3]
    elif family == 'direction_ths_hot':
        empty['data']['stock_list'] = []
    else:
        empty['list' if family == 'direction_kpl_strength' else 'data'] = []
    with pytest.raises(SourceFailure, match='upstream_unavailable'):
        normalized(family, empty)


def test_observed_kpl_parameter_error_is_rejected_without_becoming_a_schema_pause():
    with pytest.raises(SourceFailure, match='upstream_unavailable') as failure:
        normalized('direction_kpl_strength', {'errcode': 1020, 'errmsg': '参数出错'}, requested_day=SOURCE_DAY)
    diagnostic = failure.value.direction_diagnostic
    assert diagnostic['source_error_code'] == '1020'
    assert diagnostic['message_category'] == 'parameter_error'
    assert diagnostic['fields'] == {'errcode': 'number', 'errmsg': 'string'}
    assert diagnostic['checkpoint'] == 'source_rejected_or_empty'
    assert 'row_count' not in diagnostic


@pytest.mark.parametrize('family,change,checkpoint', [
    ('direction_em_hot', 'identity', 'em_exchange_mismatch'),
    ('direction_em_hot', 'rank', 'noncontiguous_ranks'),
    ('direction_kpl_strength', 'order', 'kpl_strength_order'),
    ('direction_kpl_strength', 'width', 'kpl_row_shape'),
])
def test_strict_parser_failures_expose_only_fixed_schema_diagnostics(family, change, checkpoint):
    value = strength_fixture() if family == 'direction_kpl_strength' else hot_fixture(family)
    if change == 'identity':
        value['data'][-1]['sc'] = 'SH000012'
    elif change == 'rank':
        value['data'][-1]['rk'] = 99
    elif change == 'order':
        value['list'][-1][2] = 999999
    else:
        value['list'][-1].pop()
    value['PRIVATE-TOKEN-KEY'] = 'https://private.invalid/?token=secret'
    value['errmsg'] = 'PRIVATE-TOKEN https://private.invalid/?token=secret'
    with pytest.raises(SourceFailure, match='schema_changed') as failure:
        normalized(family, value)
    diagnostic = failure.value.direction_diagnostic
    assert diagnostic['checkpoint'] == checkpoint
    assert diagnostic['row_count'] == (30 if family == 'direction_kpl_strength' else 12)
    assert len(diagnostic['body_sha256']) == 64
    serialized = json.dumps(diagnostic)
    assert len(serialized) < 2000
    assert all(secret not in serialized for secret in ('PRIVATE', 'http', 'secret', '999999', 'SH000012'))
    if family == 'direction_kpl_strength':
        assert diagnostic['row_widths'] == ([18, 19] if change == 'width' else [19])


@pytest.mark.parametrize('raw,checkpoint', [
    (b'{"code":0,"code":1}', 'duplicate_json_key'),
    (b'{"code":NaN}', 'nonfinite_json_number'),
    (b'<html>PRIVATE gateway URL</html>', 'invalid_json'),
])
def test_invalid_json_diagnostics_never_copy_untrusted_response_text(raw, checkpoint):
    with pytest.raises(SourceFailure, match='schema_changed') as failure:
        sources.parse('direction_em_hot', raw, RECEIVED)
    assert failure.value.direction_diagnostic['checkpoint'] == checkpoint
    assert 'PRIVATE' not in json.dumps(failure.value.direction_diagnostic)


def test_unaware_receipt_and_invalid_family_and_requested_date_are_controlled():
    with pytest.raises(SourceFailure, match='schema_changed'):
        sources.parse('direction_em_hot', b'{}', datetime(2026, 10, 26))
    with pytest.raises(SourceFailure, match='capability_not_implemented'):
        sources.parse('unsupported', b'{}', RECEIVED)
    for day in ('2026-10-23', datetime(2026, 10, 23, tzinfo=UTC), date(2026, 10, 27)):
        with pytest.raises(SourceFailure, match='schema_changed'):
            normalized('direction_kpl_strength', requested_day=day)


class Reply:
    def __init__(self, body=b'{}', *, encoding='', status=200):
        self.body = body
        self.headers = {'Content-Encoding': encoding}
        self.status = status
        self.read_budget = None

    def read(self, budget):
        self.read_budget = budget
        return self.body[:budget]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Opener:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def install_opener(monkeypatch, reply):
    opener = Opener(reply)
    def build(*handlers):
        assert len(handlers) == 1 and isinstance(handlers[0], NoRedirect)
        return opener
    monkeypatch.setattr(sources.urllib.request, 'build_opener', build)
    return opener


@pytest.mark.parametrize('family', sorted(sources.FAMILIES))
def test_one_fixed_https_request_with_no_pagination_and_a_read_timeout_and_size_budget(monkeypatch, family):
    reply = Reply()
    opener = install_opener(monkeypatch, reply)
    response = sources.fetch(family)
    assert len(opener.calls) == response.request_count == 1
    request, timeout = opener.calls[0]
    assert timeout == 15 and reply.read_budget == sources.MAX_BODY + 1
    assert request.full_url.startswith('https://') and response.body == b'{}'
    assert response.received_at.utcoffset() is not None
    assert request.get_header('Accept-encoding') == 'identity'
    if family == 'direction_kpl_strength':
        params = parse_qs(request.data.decode())
        assert request.full_url == sources.KPL_URL and 'Date' not in params
        assert params['Index'] == ['0'] and params['Order'] == ['1'] and params['Type'] == ['1']
        assert params['ZSType'] == ['7'] and 'Token' not in params and 'UserID' not in params
    elif family == 'direction_em_hot':
        params = json.loads(request.data)
        assert params['pageNo'] == 1 and params['pageSize'] == 100


def test_kpl_explicit_history_uses_only_the_allowlisted_historical_endpoint(monkeypatch):
    opener = install_opener(monkeypatch, Reply())
    sources.fetch('direction_kpl_strength', day=date(2020, 1, 2))
    request, _ = opener.calls[0]
    assert request.full_url.split('?')[0] == sources.KPL_HISTORY_URL
    assert request.data is None
    assert parse_qs(request.full_url.split('?')[1])['Date'] == ['2020-01-02']


@pytest.mark.parametrize('reply,reason', [
    (Reply(b'x' * (sources.MAX_BODY + 1)), 'response_too_large'),
    (Reply(encoding='gzip'), 'unsafe_upstream_response'),
    (Reply(status=500), 'upstream_unavailable'),
    (TimeoutError('PRIVATE URL'), 'timeout'),
    (OSError('PRIVATE URL'), 'transport_error'),
    (SourceFailure('upstream_redirect'), 'upstream_redirect'),
])
def test_transport_fails_bounded_and_never_exposes_upstream_body_or_exception(monkeypatch, reply, reason):
    install_opener(monkeypatch, reply)
    with pytest.raises(SourceFailure) as error:
        sources.fetch('direction_em_hot')
    assert error.value.reason == reason and error.value.request_count == 1
    assert 'PRIVATE' not in str(error.value)


@pytest.mark.parametrize('status,reason', [(401, 'permission_missing'), (403, 'permission_missing'),
    (429, 'rate_limited'), (302, 'upstream_unavailable'), (503, 'upstream_unavailable')])
def test_http_errors_do_not_include_vendor_url_or_messages(monkeypatch, status, reason):
    reply = HTTPError('https://PRIVATE.invalid/', status, 'PRIVATE', {}, io.BytesIO(b'PRIVATE'))
    install_opener(monkeypatch, reply)
    with pytest.raises(SourceFailure) as error:
        sources.fetch('direction_tdx_hot')
    assert error.value.reason == reason and error.value.request_count == 1
    assert 'PRIVATE' not in str(error.value)


def test_mutated_endpoint_is_refused_before_network(monkeypatch):
    monkeypatch.setitem(sources._SOURCES, 'direction_ths_hot', ('ths_hot', '合成', '合成', 'https://evil.invalid/'))
    with pytest.raises(SourceFailure, match='endpoint_not_allowed') as error:
        sources.fetch('direction_ths_hot')
    assert error.value.request_count == 0


def test_actual_redirect_handler_rejects_cross_origin_and_same_origin_redirects():
    handler = NoRedirect()
    for destination in ('https://evil.invalid/', sources.THS_URL):
        with pytest.raises(SourceFailure, match='upstream_redirect'):
            handler.redirect_request(None, None, 302, '', {}, destination)
