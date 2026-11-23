"""Synthetic projection/adapter cases; no database or network is used."""
import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from app.market.protocol import SHANGHAI, SourceFailure
from app.market.tushare import parameters, parse_reference
from app.market.workbench_v2 import (SavedTable, build_projection, five_minute_change,
    main_board, money)


@pytest.fixture(autouse=True)
def isolated_database():
    # Override the global disposable-database fixture for this pure test module.
    yield


DAY = date(2026, 10, 23)
AT = datetime(2026, 10, 23, 14, 0, tzinfo=SHANGHAI)


def table(family, rows, units=None, at=AT):
    columns = sorted(set().union(*(row.keys() for row in rows)))
    return SavedTable(family, family, family, at,
        {'columns': [{'key': key, 'unit': (units or {}).get(key)} for key in columns], 'rows': rows})


def quote(code='600001.SH', name='合成甲', price='11', stamp=AT):
    return dict(ts_code=code, name=name, close=price, pre_close='10', high='11', low='9', open='10',
        amount='12345', vol='10000', trade_time=stamp.strftime('%Y-%m-%d %H:%M:%S'))


def project(tables=None, snapshots=None, **values):
    return build_projection(view=values.pop('view', 'overview'), target=DAY, cutoff=AT,
        available_days=[DAY], tables=tables or {}, snapshots=snapshots or [], **values)


def raw(rows):
    fields = list(rows[0])
    return json.dumps({'code': 0, 'data': {'fields': fields,
        'items': [[row.get(field) for field in fields] for row in rows]}}).encode()


@pytest.mark.parametrize('unit,value,expected', [('元','0',0),('万元','1.2',12000),('亿元','-0.5',-50000000),
    (None,'123',None),('元',None,None),('元','NaN',None)])
def test_money_is_null_without_verified_unit_and_zero_remains_zero(unit, value, expected):
    row = {'net_amount': value}
    saved = table('synthetic', [row], {'net_amount': unit})
    assert money(row, saved, 'net_amount') == expected


@pytest.mark.parametrize('code,name,expected', [('600001.SH','合成甲',True),('001001.SZ','合成乙',True),
    ('688001.SH','合成科创',False),('300001.SZ','合成创业',False),('920001.BJ','合成北交',False),
    ('600001.SH','*ST合成',False),('600001.SH',None,False)])
def test_consistent_mainboard_non_st_scope(code, name, expected):
    assert main_board(code, name) is expected


def test_prices_at_exact_limit_do_not_claim_a_sealed_order_book_or_events():
    row = quote()
    saved = table('tushare_market_rt', [row])
    limit = table('tushare_market_limits', [{'ts_code':'600001.SH','trade_date':'20261023','up_limit':'11','down_limit':'9'}])
    result = project({'tushare_market_limits':{DAY:[limit]}}, [saved])
    stock = result.stocks[0]
    assert stock.current_state == '触及涨停价'
    assert result.stats.limit_up == 1 and result.stats.limit_down == 0
    assert stock.seal_amount_cny is None and stock.first_observed_at is None and stock.last_change_at is None
    row = {**row, 'close':'10.9'}
    result = project({'tushare_market_limits':{DAY:[limit]}}, [table('tushare_market_rt',[row])])
    assert result.stocks[0].current_state == '触板后回落' and result.stats.broken_count == 1


def test_missing_limits_and_missing_current_rows_are_not_zero_counts():
    result = project(snapshots=[table('tushare_market_rt', [quote()])])
    assert result.stats.limit_up is None and result.stats.limit_down is None
    result = project(snapshots=[table('tushare_market_rt', [quote('688001.SH')])], view='ladder')
    assert result.stats.advancing is None and result.stats.turnover_cny is None


def test_premarket_ten_thousand_shares_and_rt_volume_shares_are_not_hands():
    premarket = table('tushare_stk_premarket', [{'ts_code':'600001.SH','trade_date':'20261023',
        'float_share':'50'}], {'float_share':'万股'})
    result = project({'tushare_stk_premarket':{DAY:[premarket]}}, [table('tushare_market_rt',[quote()])])
    assert result.stocks[0].turnover_rate == 2


def test_general_views_keep_st_growth_science_and_beijing_but_ladder_has_explicit_scope():
    rows = [quote('600001.SH'), quote('000001.SZ','ST合成'), quote('300001.SZ'), quote('688001.SH'), quote('920001.BJ'),
            quote('900901.SH'), quote('200002.SZ'), quote('159001.SZ')]
    saved = table('tushare_market_rt', rows)
    assert len(project(snapshots=[saved]).stocks) == 5
    assert project(snapshots=[saved],view='ladder').stats.advancing == 1


def test_five_minute_change_uses_supplier_clocks_and_rejects_lunch_stale_and_gaps():
    current = quote(price='11')
    old_at = AT - timedelta(minutes=5)
    old = quote(price='10', stamp=old_at)
    assert five_minute_change(current, old, AT, old_at) == 10
    assert five_minute_change(current, old, AT + timedelta(minutes=3), old_at) is None
    assert five_minute_change(current, quote(stamp=AT-timedelta(minutes=4)), AT, AT-timedelta(minutes=4)) is None
    morning = AT.replace(hour=11, minute=30)
    afternoon = AT.replace(hour=13, minute=0)
    assert five_minute_change(quote(stamp=afternoon),quote(stamp=morning),afternoon,morning) is None


def test_auction_preserves_actual_four_dates_and_never_fills_order_nodes():
    days = [DAY-timedelta(days=offset) for offset in (0, 1, 2, 3, 6)]
    tables = {'tushare_stk_auction':{day:[table('tushare_stk_auction',
        [{'ts_code':'600001.SH','trade_date':day.strftime('%Y%m%d'),'name':'合成竞价',
          'price':'10.2','pre_close':'10','amount':'12000'}], {'amount':'元'})] for day in days}}
    result = project(tables, view='auction')
    assert [row.day for row in result.auction_days] == days[:4]
    assert all(row.total_count == 1 for row in result.auction_days)
    item = result.auction_days[0].rows[0]
    assert item.auction_amount_cny == 12000 and item.auction_pct_change == 2
    assert item.price is None and item.pct_change is None
    assert item.node_0915_cny is None and item.node_0920_cny is None and item.node_0925_cny is None


@pytest.mark.parametrize('view', ['overview', 'auction', 'ladder', 'funds'])
def test_sections_are_projected_only_for_the_page_that_uses_them(view):
    auction = table('tushare_stk_auction', [{'ts_code':'600001.SH', 'trade_date':'20261023',
        'name':'合成竞价', 'price':'10.2', 'pre_close':'10', 'amount':'12000'}], {'amount':'元'})
    funds = table('tushare_moneyflow_ind_dc', [{'ts_code':'BK1184.DC', 'trade_date':'20261023',
        'name':'合成板块', 'net_amount':'123'}], {'net_amount':'元'})
    result = project({'tushare_stk_auction':{DAY:[auction]},
        'tushare_moneyflow_ind_dc':{DAY:[funds]}}, view=view)
    assert bool(result.auction_days) is (view == 'auction')
    assert bool(result.fund_history) is (view == 'funds')


def test_auction_bounds_top_rows_but_keeps_low_rank_selected_and_watchlist_on_each_day():
    codes = [f'600{number:03d}.SH' for number in range(1, 36)]
    identities = {code:uuid4() for code in codes}
    tables = {'tushare_stk_auction':{}}
    for day in (DAY, DAY-timedelta(days=1)):
        rows = [{'ts_code':code, 'trade_date':day.strftime('%Y%m%d'), 'name':'合成竞价',
            'price':'10.2', 'pre_close':'10', 'amount':str(1000-rank)} for rank, code in enumerate(codes)]
        tables['tushare_stk_auction'][day] = [table('tushare_stk_auction', rows, {'amount':'元'})]
    result = project(tables, view='auction', identities=identities, selected_code=codes[-2],
        watchlist_ids=[identities[codes[-1]]])
    expected = codes[:20] + codes[-2:]
    assert result.selected_stock.ts_code == codes[-2]
    assert [stock.ts_code for stock in result.stocks] == expected
    for day in result.auction_days:
        assert day.total_count == 35 and [stock.ts_code for stock in day.rows] == expected
    # Bounded response is a projection; the complete cache supplied to it is untouched.
    assert len(tables['tushare_stk_auction'][DAY][0].rows()) == 35


def test_auction_selected_object_only_present_on_an_earlier_day_is_retained():
    prior = DAY-timedelta(days=1)
    rows = [{'ts_code':f'600{number:03d}.SH', 'name':'合成竞价', 'price':'10.2',
        'pre_close':'10', 'amount':str(1000-number)} for number in range(1, 32)]
    tables = {'tushare_stk_auction':{
        DAY:[table('tushare_stk_auction', [{**row, 'trade_date':'20261023'} for row in rows[:-1]], {'amount':'元'})],
        prior:[table('tushare_stk_auction', [{**row, 'trade_date':'20261022'} for row in rows], {'amount':'元'})]}}
    result = project(tables, view='auction', selected_code='600031.SH')
    assert result.selected_stock.ts_code == '600031.SH'
    assert result.selected_stock.auction_amount_cny == 969
    assert '600031.SH' not in {stock.ts_code for stock in result.stocks}
    assert '600031.SH' in {stock.ts_code for stock in result.auction_days[1].rows}
    assert [day.total_count for day in result.auction_days] == [30, 31]


def test_auction_amount_of_old_unknown_unit_table_is_not_relabelled():
    saved = table('tushare_stk_auction', [{'ts_code':'600001.SH','trade_date':'20261023',
        'name':'合成竞价','price':'10.2','pre_close':'10','amount':'0'}])
    result = project({'tushare_stk_auction':{DAY:[saved]}}, view='auction')
    assert result.stocks[0].auction_amount_cny is None


def test_daily_funds_are_not_minute_curves_or_intraday_rank_changes():
    prior = DAY-timedelta(days=1)
    funds = table('tushare_moneyflow_ind_dc', [{'ts_code':'BK1184.DC','name':'合成板块',
        'trade_date':'20261022','net_amount':'123','pct_change':'2'}], {'net_amount':'元'})
    stock = table('tushare_moneyflow_dc', [{'ts_code':'600001.SH','name':'合成股票',
        'trade_date':'20261022','net_amount':'1.2','pct_change':'3','close':'10'}], {'net_amount':'万元'})
    members = table('tushare_dc_member', [{'ts_code':'BK1184.DC','con_code':'600001.SH','trade_date':'20261022','name':'合成股票'}])
    result = project({'tushare_moneyflow_ind_dc':{prior:[funds]}, 'tushare_moneyflow_dc':{prior:[stock]},
        'tushare_dc_member':{prior:[members]}}, view='funds')
    assert result.sectors[0].trade_day == prior and result.sectors[0].net_cny == 123
    assert result.sectors[0].change_5m is None and result.sectors[0].rank_change is None
    assert result.stocks[0].net_cny == 12000 and result.stocks[0].fund_trade_day == prior
    assert result.stocks[0].sector_code == 'BK1184.DC'
    assert len(result.fund_history) == 1 and result.fund_history[0].day == prior


def test_new_tushare_tables_carry_per_endpoint_units_and_keep_original_strings():
    received = AT.astimezone(UTC)
    for api, code, expected in [('moneyflow_dc','600001.SH','万元'),('moneyflow_ind_dc','BK1184.DC','元')]:
        original = {'trade_date':'20261023','ts_code':code,'name':'合成样本','net_amount':'-1.25'}
        payload = parse_reference(raw([original]), 'tushare_'+api, received).records[0][1]
        assert next(column.unit for column in payload.columns if column.key == 'net_amount') == expected
        assert payload.rows[0]['net_amount'] == '-1.25'


def test_new_tushare_bounded_parameters_and_single_board_validation():
    assert parameters('tushare_dc_member', [], AT, board_codes=['BK1184']) == {'ts_code':'BK1184.DC','limit':5000}
    assert parameters('tushare_moneyflow_dc', [], AT) == {'limit':6000}
    assert parameters('tushare_dc_index', [], AT) == {'idx_type':'概念板块','limit':5000}
    # The collector fills each configured member scope separately; the base
    # parameter helper alone never authorizes an unscoped supplier request.
    assert parameters('tushare_dc_member', [], AT) == {'limit':5000}
    from app.market.sector_scope import member_sector_codes
    with pytest.raises(SourceFailure, match='sector_codes_required'):
        member_sector_codes({})
    with pytest.raises(SourceFailure, match='board_required'):
        parameters('tushare_dc_member', [], AT, board_codes=['BK1184','BK1000'])


@pytest.mark.parametrize('change,reason', [({'ts_code':'BK1184'},'instrument_mismatch'),
    ({'trade_date':'20990101'},'future_source_time'),({'net_amount':'NaN'},'invalid_number')])
def test_new_tables_reject_bad_identifiers_dates_numbers(change, reason):
    row = {'ts_code':'BK1184.DC','trade_date':'20261023','name':'合成板块','net_amount':'1'}
    with pytest.raises(SourceFailure, match=reason):
        parse_reference(raw([{**row, **change}]), 'tushare_moneyflow_ind_dc', AT)


def test_empty_success_is_publication_gap_and_does_not_overwrite_last_table():
    response = json.dumps({'code':0,'data':{'fields':[], 'items':[]}}).encode()
    with pytest.raises(SourceFailure, match='daily_data_not_published'):
        parse_reference(response, 'tushare_moneyflow_ind_dc', AT)


@pytest.mark.parametrize('case,reason',[('duplicate','duplicate_instrument'),('date','backfill_day_not_returned'),
    ('full_tail','market_scope_truncated')])
def test_moneyflow_page_pair_must_be_same_day_disjoint_and_have_terminal_page(case,reason):
    from app.market.protocol import Response
    from app.market.tushare_pages import combine
    rows=[{'ts_code':f'{index:06d}.SZ','trade_date':'20261023','net_amount':'1'} for index in range(1,6001)]
    tail=[{'ts_code':'006001.SZ','trade_date':'20261023','net_amount':'1'}]
    if case=='duplicate':tail[0]['ts_code']='000001.SZ'
    if case=='date':tail[0]['trade_date']='20261022'
    if case=='full_tail':tail=rows
    first=Response(raw(rows),AT,1,200);second=Response(raw(tail),AT,1,200)
    with pytest.raises(SourceFailure,match=reason):combine(first,second,DAY)


def test_exact_full_first_page_accepts_explicit_empty_terminal_page():
    from app.market.tushare import DCFundPagination
    rows=[{'ts_code':f'{index:06d}.SZ','name':'合成股票','trade_date':'20261023','net_amount':'1'} for index in range(1,3001)]
    pager=DCFundPagination('20261023')
    pager.add(raw(rows),AT)
    with pytest.raises(SourceFailure,match='market_scope_truncated'):pager.batch()
    empty=json.dumps({'code':0,'data':{'fields':[],'items':[]}}).encode()
    pager.add(empty,AT)
    batch=pager.batch()
    assert len(batch.records[0][1].rows)==3000 and batch.requests==2
