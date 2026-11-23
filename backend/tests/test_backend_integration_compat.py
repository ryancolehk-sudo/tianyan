"""Offline checks for the merged contracts and preserved source identities."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json

from fastapi.routing import APIRoute
import pytest

from app.main import app
from app.market.api import router
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.reference_backfill import validate_context as validate_reference
from app.market.registry import SOURCES
from app.market.sector_scope import member_sector_codes
from app.market.terminal_dates import validate_dated_context
from app.market.tushare import DCFundPagination
from app.market.tushare_specs import DATED_FAMILIES, SPECS


@pytest.fixture(autouse=True)
def isolated_database():
    # All values are synthetic. This module never connects to a database.
    yield


def test_primary_and_legacy_get_contracts_have_distinct_routes_and_operations():
    routes = [route for route in router.routes if isinstance(route, APIRoute)]
    published = app.openapi()['paths']
    for path, expected_parameter, operation in (
        ('/api/v1/workbench-view', 'day', 'getWorkbenchView'),
        ('/api/v1/workbench-v2', 'trade_day', 'getWorkbenchV2'),
    ):
        matches = [route for route in routes if route.path == path and 'GET' in route.methods]
        assert len(matches) == 1
        assert matches[0].operation_id == operation
        assert expected_parameter in {parameter.name for parameter in matches[0].dependant.query_params}
        assert published[path]['get']['operationId'] == operation
    operations = [route.operation_id for route in routes if route.operation_id]
    assert len(operations) == len(set(operations))


@pytest.mark.parametrize('family', [
    'tushare_dc_index', 'tushare_dc_member', 'tushare_moneyflow_ind_dc',
    'tushare_moneyflow_dc', 'tushare_limit_step', 'tushare_stk_premarket',
])
def test_each_new_source_keeps_its_dated_mode_without_a_second_registry_entry(family):
    assert family in DATED_FAMILIES
    assert SPECS[family].mode in {'dated_table', 'dated_members'}
    assert SOURCES[family].version == 'tushare-http-v1'
    assert not any('eastmoney_v2' in family for family in SOURCES)


@pytest.mark.parametrize('config,expected', [
    ({'symbols': [], 'board_codes': ['BK1184']}, ['BK1184.DC']),
    ({'symbols': [], 'sector_codes': ['BK1184.DC', 'BK1185.DC']}, ['BK1184.DC', 'BK1185.DC']),
    ({'board_codes': ['BK1184'], 'sector_codes': ['BK1184.DC']}, ['BK1184.DC']),
])
def test_legacy_and_modern_member_scope_reads_do_not_rewrite_persisted_configuration(config, expected):
    original = deepcopy(config)
    assert member_sector_codes(config) == expected
    assert config == original


@pytest.mark.parametrize('config,reason', [
    ({}, 'sector_codes_required'),
    ({'board_codes': ['BK1184', 'BK1185']}, 'board_required'),
    ({'board_codes': ['BK1184'], 'sector_codes': ['BK1185.DC']}, 'conflicting_sector_scope'),
    ({'sector_codes': ['BK1184.DC'] * 2}, 'duplicate_sector_code'),
])
def test_ambiguous_or_unbounded_member_scope_fails_closed(config, reason):
    with pytest.raises(SourceFailure, match=reason):
        member_sector_codes(config)


def test_reference_and_dated_intents_cannot_be_mixed_or_silently_drop_a_null_context():
    context = {'requested_day': '2026-10-23', 'acquisition_mode': 'backfill'}
    payload = {'reference_context': context, 'dated_context': context}
    for validate in (validate_reference, validate_dated_context):
        with pytest.raises(SourceFailure, match='invalid_backfill_context'):
            validate(None, payload, None, None, None)
    with pytest.raises(SourceFailure, match='invalid_backfill_context'):
        validate_dated_context(None, {'dated_context': None}, None, None, None)


def test_primary_pager_keeps_precise_page_lineage_without_a_second_collect_path():
    stamp = datetime(2026, 10, 23, 16, tzinfo=SHANGHAI)
    def page(start, count):
        fields = ['trade_date', 'ts_code', 'name', 'net_amount']
        rows = [['20261023', f'{number:06d}.SZ', '合成分页', '1'] for number in range(start, start + count)]
        return json.dumps({'code': 0, 'data': {'fields': fields, 'items': rows}}).encode()
    first, second = page(1, 3000), page(3001, 1)
    pager = DCFundPagination('20261023')
    pager.add(first, stamp)
    pager.add(second, stamp + timedelta(seconds=3))
    payload = pager.batch().records[0][1]
    assert payload.rows[0]['source_page_offset'] == '0'
    assert payload.rows[-1]['source_page_offset'] == '3000'
    assert payload.rows[0]['source_page_sha256'] == hashlib.sha256(first).hexdigest()
    assert payload.rows[-1]['source_page_sha256'] == hashlib.sha256(second).hexdigest()
    assert payload.rows[-1]['source_page_received_at'] == (stamp + timedelta(seconds=3)).isoformat()
    assert next(column.unit for column in payload.columns if column.key == 'net_amount') == '万元'
