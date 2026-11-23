"""Synthetic workbench scenarios on disposable PostgreSQL; no supplier network."""
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collect import store_calendar_batch, store_reference_batch
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SourceFailure
from app.market.service import source_id
from app.market.terminal_read import number, speed_5m, state
from app.market.tushare import DCFundPagination, parameters, parse_reference
from app.market.tushare_market import encoded, parse_market
from app.models import Job, Provider
from test_data_sources import configure, login
from test_tushare import body, source
from test_tushare_market import quote, limits

DAY=date(2026,10,23)
AT=datetime(2026,10,23,2,30,tzinfo=UTC)
REVIEW=datetime(2026,10,24,2,tzinfo=UTC)


def setup(client,monkeypatch,families):
    headers=login(client)
    for family in families:
        config=source(family)
        if family=='tushare_dc_member':config['sector_codes']=['BK1184.DC']
        configure(client,headers,config,key=family)
    monkeypatch.setattr('app.market.terminal_read.now',lambda db:REVIEW)
    return headers


def save(db,family,rows,received=AT):
    provider=db.get(Provider,source_id(family));config=db.get(SourceSettings,provider.id)
    batch=(parse_market(encoded(list(rows[0]),rows),received,limits=family=='tushare_market_limits')
        if family in {'tushare_market_rt','tushare_market_limits'} else
        parse_reference(encoded(list(rows[0]),rows),family,received))
    return store_reference_batch(db,provider,config,batch,received)


def calendar(db):
    days=[(date(2026,10,day),day<24) for day in range(19,26)]
    for family,exchange in [('tushare_calendar_sse','SSE'),('tushare_calendar_szse','SZSE')]:
        provider=db.get(Provider,source_id(family))
        store_calendar_batch(db,provider,db.get(SourceSettings,provider.id),days,AT-timedelta(days=7),exchanges=(exchange,))


def view(client,kind='overview',params=''):
    response=client.get('/api/v1/workbench-view?view='+kind+'&day=2026-10-23'+params)
    assert response.status_code==200,response.text
    return response.json()['data']


def test_get_is_authenticated_read_only_and_empty_is_not_demo(client,monkeypatch):
    assert client.get('/api/v1/workbench-view').status_code==401
    setup(client,monkeypatch,[])
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *a,**k:pytest.fail('GET must not collect'))
    data=view(client)
    assert data['stocks']==[] and data['sectors']==[] and data['breadth']['limit_up'] is None
    assert data['mode']=='review' and all(item['status']!='available' for item in data['capabilities'])
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job))==0
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==0


def test_exact_breadth_all_rows_before_200_stock_display_cap(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits'])
    quotes=[quote(code=f'{100+i:06}.SZ',price='11',at=AT) for i in range(205)]
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_limits',[limits(row['ts_code']) for row in quotes],AT-timedelta(minutes=1))
        save(db,'tushare_market_rt',quotes)
    data=view(client)
    assert len(data['stocks'])==205 and data['coverage']['quote_count']==205
    assert data['breadth']['up']==205 and data['breadth']['limit_up']==205
    assert data['breadth']['points'][0]['up']==205 and data['breadth']['points'][0]['down']==0
    assert all(item['seal_amount_cny'] is None for item in data['stocks'])
    assert all('盘口待核' in item['status'] for item in data['stocks'])


def test_overview_keeps_low_gain_speed_leader_and_selected_stock_from_large_sample(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    earlier=AT-timedelta(minutes=5)
    codes=[f'{100+i:06}.SZ' for i in range(240)]
    old=[quote(code=code,price='11',at=earlier) for code in codes]
    current=[quote(code=code,price='11',at=AT) for code in codes]
    # The speed leader has only a 1% day gain, behind 240 stocks at 10%, but
    # advances from 9.90 to 10.10 over two genuinely saved five-minute snapshots.
    old.extend([quote('000400.SZ',price='9.90',at=earlier),quote('000401.SZ',price='9',at=earlier)])
    current.extend([quote('000400.SZ',price='10.10',at=AT),quote('000401.SZ',price='9',at=AT)])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',old,earlier)
        save(db,'tushare_market_rt',current,AT)
    data=view(client,'overview','&stock=000401.SZ')
    rows={item['code']:item for item in data['stocks']}
    assert len(rows)==242 and data['coverage']['quote_count']==242
    assert rows['000400.SZ']['pct_change']==pytest.approx(1)
    assert max(data['stocks'],key=lambda item:item['speed_5m'])['code']=='000400.SZ'
    assert rows['000400.SZ']['speed_5m']==pytest.approx((10.1/9.9-1)*100)
    assert data['selected_stock_code']=='000401.SZ' and rows['000401.SZ']['price']==9


def test_speed_matches_each_stock_by_quote_time_across_saved_batches(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    earlier = AT - timedelta(minutes=5)
    with Session(engine()) as db, db.begin():
        save(db,'tushare_market_rt',[
            quote('000100.SZ',price='8',at=earlier-timedelta(seconds=45)),
            quote('000101.SZ',price='10',at=earlier),
        ],earlier)
        save(db,'tushare_market_rt',[
            quote('000100.SZ',price='10',at=earlier+timedelta(seconds=10)),
            quote('000101.SZ',price='9',at=earlier+timedelta(seconds=20)),
        ],earlier+timedelta(seconds=35))
        save(db,'tushare_market_rt',[
            quote('000100.SZ',price='11'), quote('000101.SZ',price='11'), quote('000102.SZ',price='11'),
        ],AT)
    rows = {row['code']: row for row in view(client)['stocks']}
    assert rows['000100.SZ']['speed_5m'] == pytest.approx(10)
    assert rows['000100.SZ']['speed_window_seconds'] == 290
    assert rows['000101.SZ']['speed_5m'] == pytest.approx(10)
    assert rows['000101.SZ']['speed_window_seconds'] == 300
    assert rows['000102.SZ']['speed_5m'] is None


def test_overview_keeps_gain_leaders_when_yesterday_ladder_exceeds_display_limit(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_limit_step','tushare_calendar_sse','tushare_calendar_szse'])
    ladder_codes=[f'{100+i:06}.SZ' for i in range(205)]
    winners=[f'{500+i:06}.SZ' for i in range(8)]
    quotes=[quote(code,price='10.10') for code in ladder_codes]+[quote(code,price='11') for code in winners]
    with Session(engine()) as db,db.begin():
        calendar(db)
        save(db,'tushare_limit_step',[{'trade_date':'20261022','ts_code':code,'name':'合成昨日梯队','nums':'2'} for code in ladder_codes],AT)
        save(db,'tushare_market_rt',quotes,AT)
    data=view(client)
    returned={item['code'] for item in data['stocks']}
    assert len(returned)==213 and data['coverage']['quote_count']==213
    assert set(winners).issubset(returned)
    assert data['stocks'][0]['previous_height']==2
    assert all(item['previous_height']==2 for item in data['stocks'] if item['code'] not in winners)


def test_current_permission_revocation_never_falls_back_to_old_source(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote()])
        provider=db.get(Provider,source_id('tushare_market_rt'))
        provider.rights={**provider.rights,'display':'denied'}
    data=view(client)
    assert data['stocks']==[] and data['breadth']['up'] is None
    assert next(item for item in data['capabilities'] if item['key']=='quotes')['status']=='permission_denied'


def test_limit_price_evidence_is_not_borrowed_from_a_different_trade_day(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_limits',[{**limits(),'trade_date':'20261022'}],AT-timedelta(days=1))
        save(db,'tushare_market_rt',[quote(price='11')])
    data=view(client)
    assert data['breadth']['limit_up'] is None and data['breadth']['comparable']==0
    assert data['stocks'][0]['limit_up'] is None


def test_late_limits_help_review_but_never_rewrite_observed_earlier_curve(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote(price='11')])
        save(db,'tushare_market_limits',[limits()],AT+timedelta(minutes=1))
    data=view(client)
    assert data['breadth']['limit_up']==1 and data['breadth']['points']==[]
    observed=view(client,params='&as_of_time=2026-10-23T10:30:10%2B08:00')
    assert observed['breadth']['limit_up'] is None


def test_auction_matched_amount_never_populates_three_seal_nodes(client,monkeypatch):
    setup(client,monkeypatch,['tushare_stk_auction','tushare_calendar_sse','tushare_calendar_szse'])
    with Session(engine()) as db,db.begin():
        calendar(db)
        save(db,'tushare_stk_auction',[{'trade_date':'20261023','ts_code':'000001.SZ','price':'11','pre_close':'10',
            'amount':'1250000','vol':'1000','volume_ratio':'3.2'}],AT)
    data=view(client,'auction','&stock=000001.SZ')
    assert [item['day'] for item in data['auction_days']]==['2026-10-23','2026-10-22','2026-10-21','2026-10-20']
    row=data['auction_days'][0]['items'][0]
    assert row['auction_amount_cny']==1250000 and row['auction_pct']==pytest.approx(10)
    assert row['node_amounts']=={'09:15':None,'09:20':None,'09:25':None} and row['seal_amount_cny'] is None
    assert data['auction_days'][1]['items']==[]


def test_dc_sector_yuan_stock_wanyuan_and_premarket_wangu_are_separate(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits','tushare_stk_premarket',
        'tushare_dc_member','tushare_moneyflow_ind_dc','tushare_moneyflow_dc'])
    received=datetime(2026,10,23,9,tzinfo=UTC)
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote('000001.SZ',price='11')])
        save(db,'tushare_stk_premarket',[{'trade_date':'20261023','ts_code':'000001.SZ','float_share':'100',
            'pre_close':'10','up_limit':'11','down_limit':'9'}],AT)
        save(db,'tushare_moneyflow_ind_dc',[{'trade_date':'20261023','ts_code':'BK1184.DC','name':'合成机器人','net_amount':'1250000','pct_change':'2.31'}],received)
        save(db,'tushare_moneyflow_dc',[{'trade_date':'20261023','ts_code':'000001.SZ','name':'合成股票','net_amount':'125','pct_change':'10','close':'11'}],received)
        save(db,'tushare_dc_member',[{'trade_date':'20261023','ts_code':'BK1184.DC','con_code':'000001.SZ','name':'合成股票'}],received)
    data=view(client,'funds','&sector=BK1184.DC')
    assert data['sectors'][0]['main_net_cny']==1250000
    row=data['selected_members'][0]
    assert row['main_net_cny']==1250000 and row['turnover_rate']==pytest.approx(.01)
    assert row['float_mv_cny']==11000000
    assert data['funds_history']==[{'day':'2026-10-23','sector_code':'BK1184.DC','value':1250000}]


def test_fund_and_member_dates_do_not_silently_mix_with_current_quotes(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_moneyflow_ind_dc','tushare_moneyflow_dc','tushare_dc_member'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote('000001.SZ',price='11')])
        save(db,'tushare_moneyflow_ind_dc',[{'trade_date':'20261022','ts_code':'BK1184.DC','name':'合成机器人','net_amount':'1250000'}],AT)
        save(db,'tushare_moneyflow_dc',[{'trade_date':'20261022','ts_code':'000001.SZ','name':'合成股票','net_amount':'125','close':'10'}],AT)
        save(db,'tushare_dc_member',[{'trade_date':'20261022','ts_code':'BK1184.DC','con_code':'000001.SZ','name':'合成股票'}],AT)
    overview=view(client,'overview','&sector=BK1184.DC')
    assert overview['funds_trade_day']=='2026-10-22' and overview['trade_day']=='2026-10-23'
    assert overview['selected_members']==[] and overview['stocks'][0]['main_net_cny'] is None
    funds=view(client,'funds','&sector=BK1184.DC')
    assert funds['sectors']==[] and funds['funds_trade_day'] is None
    assert funds['funds_history'][0]['day']=='2026-10-22'


def test_multiple_sector_history_has_top_ten_plus_selection_and_null_gaps(client,monkeypatch):
    setup(client,monkeypatch,['tushare_moneyflow_ind_dc'])
    received=datetime(2026,10,23,9,tzinfo=UTC)
    with Session(engine()) as db,db.begin():
        save(db,'tushare_moneyflow_ind_dc',[{'trade_date':'20261022','ts_code':'BK1000.DC','name':'合成板块0','net_amount':'20'}],received-timedelta(minutes=1))
        save(db,'tushare_moneyflow_ind_dc',[{'trade_date':'20261023','ts_code':f'BK{1000+i}.DC','name':f'合成板块{i}','net_amount':str(12-i)} for i in range(12)],received)
    data=view(client,'funds','&sector=BK1011.DC')
    points=data['funds_history']
    assert len(points)==22
    assert {point['sector_code'] for point in points}=={f'BK{1000+i}.DC' for i in range(10)}|{'BK1011.DC'}
    assert next(point['value'] for point in points if point['day']=='2026-10-22' and point['sector_code']=='BK1011.DC') is None
    assert next(point['value'] for point in points if point['day']=='2026-10-23' and point['sector_code']=='BK1011.DC')==1


def test_hard_asof_excludes_late_receipts_and_later_source_clock(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_stk_auction'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote(price='10',at=AT)],AT)
        save(db,'tushare_market_rt',[quote(price='11',at=AT+timedelta(seconds=60))],AT+timedelta(seconds=30))
        save(db,'tushare_stk_auction',[{'trade_date':'20261023','ts_code':'603278.SH','price':'11','pre_close':'10','amount':'500'}],AT+timedelta(hours=6))
    known=view(client,'overview','&as_of_time=2026-10-23T10:30:40%2B08:00')
    assert known['mode']=='observed' and known['stocks'][0]['price']==10
    assert all(not item['items'] for item in known['auction_days'])
    review=view(client)
    assert review['stocks'][0]['price']==11 and review['stocks'][0]['auction_amount_cny']==500


def test_ladder_uses_yesterday_trade_day_and_not_calendar_yesterday(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_limit_step','tushare_calendar_sse','tushare_calendar_szse'])
    monday=date(2026,10,26)
    with Session(engine()) as db,db.begin():
        for family,exchange in [('tushare_calendar_sse','SSE'),('tushare_calendar_szse','SZSE')]:
            provider=db.get(Provider,source_id(family))
            store_calendar_batch(db,provider,db.get(SourceSettings,provider.id),[(DAY,True),(DAY+timedelta(days=1),False),(DAY+timedelta(days=2),False),(monday,True)],AT,exchanges=(exchange,))
        save(db,'tushare_limit_step',[{'trade_date':'20261023','ts_code':'000001.SZ','name':'合成股票','nums':'4'}],AT+timedelta(hours=6))
    monkeypatch.setattr('app.market.terminal_read.now',lambda db:datetime(2026,10,26,2,tzinfo=UTC))
    monkeypatch.setattr('app.market.read_models.now',lambda db:datetime(2026,10,26,2,tzinfo=UTC))
    response=client.get('/api/v1/workbench-view?view=ladder&day=2026-10-26')
    assert response.status_code==200,response.text
    data=response.json()['data']
    assert data['previous_trade_day']=='2026-10-23' and data['stocks'][0]['previous_height']==4


def test_seal_snapshots_are_book_evidence_and_gap_is_explicit(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits'])
    row={**quote(price='11'),'bid_price1':'11','bid_volume1':'1000','ask_price1':None,'ask_volume1':'0'}
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_limits',[limits()],AT-timedelta(minutes=1))
        save(db,'tushare_market_rt',[row],AT)
        later=AT+timedelta(minutes=10)
        save(db,'tushare_market_rt',[{**row,'trade_time':'2026-10-23 10:40:00'}],later)
    data=view(client,'ladder','&stock=603278.SH')
    assert data['stocks'][0]['seal_amount_cny']==11000
    assert [item['value'] for item in data['stock_seals']]==[11000,None,11000]
    assert any(item['kind']=='gap' for item in data['events'])
    assert all('完整' not in item['label'] for item in data['events'])


@pytest.mark.parametrize('afternoon,missing',[('13:01:00',False),('13:10:00',True)])
def test_lunch_does_not_create_a_false_gap_but_a_missing_trading_interval_does(client,monkeypatch,afternoon,missing):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits'])
    morning=datetime.fromisoformat('2026-10-23T11:29:00+08:00')
    later=datetime.fromisoformat('2026-10-23T'+afternoon+'+08:00')
    row={**quote(price='11'),'bid_price1':'11','bid_volume1':'1000','ask_price1':None,'ask_volume1':'0'}
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_limits',[limits()],AT-timedelta(minutes=1))
        save(db,'tushare_market_rt',[{**row,'trade_time':'2026-10-23 11:29:00'}],morning+timedelta(seconds=1))
        save(db,'tushare_market_rt',[{**row,'trade_time':'2026-10-23 '+afternoon}],later+timedelta(seconds=1))
    detail=view(client,'ladder','&stock=603278.SH')
    assert any(item['kind']=='gap' for item in detail['events'])==missing
    assert any(item['value'] is None for item in detail['stock_seals'])==missing
    overview=view(client)
    assert len([point for point in overview['breadth']['points'] if point['up'] is not None])==2
    assert any(point['up'] is None for point in overview['breadth']['points'])==missing


def test_late_limit_receipt_cannot_create_an_earlier_seal_event(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits'])
    row={**quote(price='11'),'bid_price1':'11','bid_volume1':'1000','ask_price1':None,'ask_volume1':'0'}
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[row],AT)
        save(db,'tushare_market_limits',[limits()],AT+timedelta(minutes=5))
        later=AT+timedelta(minutes=10)
        save(db,'tushare_market_rt',[{**row,'trade_time':'2026-10-23 10:40:00'}],later)
    data=view(client,'ladder','&stock=603278.SH')
    assert data['stock_seals'][0]['value'] is None and data['stock_seals'][-1]['value']==11000
    assert data['stocks'][0]['first_observed']=='10:40:00'
    assert not any('封住' in event['label'] and event['time']<'2026-10-23T10:35:00+08:00' for event in data['events'])


def test_source_high_is_touch_without_proof_of_seal():
    assert state({'close':'10.5','high':'11'},11,9)==('触板后回落',None)
    assert state({'close':'11','high':'11','bid_price1':'11','bid_volume1':'100'},11,9)==('涨停价（盘口待核）',None)


def test_breadth_curve_breaks_across_missing_samples(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_limits',[limits()],AT-timedelta(minutes=1))
        save(db,'tushare_market_rt',[quote(price='11')],AT)
        later=AT+timedelta(minutes=30)
        save(db,'tushare_market_rt',[quote(price='11',at=later)],later)
    data=view(client)
    assert [point['up'] for point in data['breadth']['points']]==[1,None,1]


def test_ladder_pool_is_actually_main_board_non_st(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote('600001.SH'),quote('688001.SH'),quote('300001.SZ'),
            {**quote('000001.SZ'),'name':'ST合成股票'}])
    data=view(client,'ladder')
    assert [item['code'] for item in data['stocks']]==['600001.SH']
    assert data['coverage']['quote_count']==4 and data['breadth']['down']==4


@pytest.mark.parametrize('start,end,valid',[
    ('2026-10-23 10:00:00','2026-10-23 10:05:00',True),
    ('2026-10-23 11:30:00','2026-10-23 13:00:00',False),
    ('2026-10-23 11:30:00','2026-10-23 13:05:00',False),
    ('2026-10-23 10:00:00','2026-10-23 10:10:00',False),
    ('2026-10-22 10:00:00','2026-10-23 10:05:00',False),
])
def test_five_minute_speed_requires_actual_same_session_clocks(start,end,valid):
    current={'trade_time':end,'close':'11'};previous={'trade_time':start,'close':'10'}
    value,seconds=speed_5m(current,previous,AT+timedelta(days=1),AT+timedelta(days=1))
    assert (value is not None)==valid
    if valid:assert value==pytest.approx(10) and seconds==300


@pytest.mark.parametrize('value',[None,'','--','nan','Infinity',True,{},[]])
def test_unknown_and_nonfinite_values_are_not_zero(value):
    assert number(value) is None


def test_dated_dc_parameters_keep_types_and_never_issue_undated_whole_market_members():
    assert parameters('tushare_dc_index',[],AT)=={'limit':5000,'idx_type':'概念板块'}
    assert parameters('tushare_moneyflow_ind_dc',[],AT)=={'limit':5000,'content_type':'概念'}
    assert parameters('tushare_moneyflow_dc',[],AT)=={'limit':6000}
    assert parameters('tushare_stk_premarket',[],AT)=={'limit':8000}


def test_extended_units_duplicates_and_ambiguous_truncation_are_checked():
    fields=['trade_date','ts_code','name','net_amount']
    rows=[['20261023','000001.SZ','合成股票','-12.5']]
    parsed=parse_reference(body(fields,rows),'tushare_moneyflow_dc',AT).records[0][1]
    assert next(col.unit for col in parsed.columns if col.key=='net_amount')=='万元'
    with pytest.raises(SourceFailure,match='duplicate_instrument'):
        parse_reference(body(fields,rows*2),'tushare_moneyflow_dc',AT)
    with pytest.raises(SourceFailure,match='market_scope_truncated'):
        parse_reference(body(fields,rows*6000),'tushare_moneyflow_dc',AT)


def test_dated_backfill_request_passes_exact_day_without_modifying_old_evidence(client,monkeypatch):
    from pydantic import SecretStr
    from app.config import settings
    from app.market.service import enqueue_collection
    setup(client,monkeypatch,['tushare_moneyflow_dc'])
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(settings(),'tushare_token',SecretStr('synthetic-terminal-token'))
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_moneyflow_dc'))
        job=enqueue_collection(db,provider.id,day=DAY)
        assert job.payload['dated_context']['requested_day']=='2026-10-23'
        assert job.payload['dated_context']['acquisition_mode']=='backfill'
        assert db.scalar(select(func.count()).select_from(EvidenceVersion))==0
        from app.market.terminal_dates import validate_dated_context
        assert validate_dated_context(db,job.payload,provider,db.get(SourceSettings,provider.id),REVIEW)==DAY


def test_different_pending_dates_are_a_conflict_not_silent_reuse(client,monkeypatch):
    from pydantic import SecretStr
    from app.auth import APIError
    from app.config import settings
    from app.market.service import enqueue_collection
    setup(client,monkeypatch,['tushare_moneyflow_dc'])
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(settings(),'tushare_token',SecretStr('synthetic-terminal-token'))
    with Session(engine()) as db,db.begin():
        enqueue_collection(db,source_id('tushare_moneyflow_dc'),day=DAY)
        with pytest.raises(APIError) as error:
            enqueue_collection(db,source_id('tushare_moneyflow_dc'),day=DAY-timedelta(days=1))
        assert error.value.code=='collection_intent_conflict'


def test_dated_backfill_does_not_wait_for_daily_live_poll_interval(client,monkeypatch):
    from pydantic import SecretStr
    from app.config import settings
    from app.market.service import enqueue_collection
    setup(client,monkeypatch,['tushare_moneyflow_dc'])
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(settings(),'tushare_token',SecretStr('synthetic-terminal-token'))
    with Session(engine()) as db,db.begin():
        stamp=datetime.now(UTC)
        provider=db.get(Provider,source_id('tushare_moneyflow_dc'))
        db.add(DataFetchRun(source_id=provider.id,capability='reference.tushare',started_at=stamp,received_at=stamp,
            status='complete',request_count=1,reason=None,cost_cny=None))
        config=db.get(SourceSettings,provider.id)
        config.next_run_at=stamp+timedelta(hours=6)
        db.flush()
        job=enqueue_collection(db,provider.id,day=DAY)
        assert job.payload['dated_context']['requested_day']==DAY.isoformat()
        assert config.next_run_at==stamp+timedelta(hours=6)


def test_two_members_are_collected_once_under_real_persisted_two_second_budget(client,monkeypatch):
    from pydantic import SecretStr
    from app.config import settings
    from app.market.protocol import Response
    from app.market.service import enqueue_collection
    from app.worker import claim,process
    setup(client,monkeypatch,['tushare_dc_member'])
    # Same authenticated user/session; this deliberately updates only this
    # isolated synthetic source before enqueuing its immutable dated intent.
    with Session(engine()) as db,db.begin():
        source_config=db.get(SourceSettings,source_id('tushare_dc_member'))
        source_config.config={'symbols':[],'sector_codes':['BK1184.DC','BK1185.DC']}
    monkeypatch.setattr(settings(),'tushare_token',SecretStr('synthetic-terminal-token'))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    calls=[]
    def respond(api,params,fields,credential,endpoint):
        assert api=='dc_member' and params['trade_date']=='20261023' and params['limit']==5000
        calls.append((params['ts_code'],datetime.now(UTC)))
        return Response(body(['trade_date','ts_code','con_code','name'],[
            ['20261023',params['ts_code'],'000001.SZ','合成成分']]),datetime.now(UTC),1,200)
    monkeypatch.setattr('app.market.collect.request_tushare',respond)
    with Session(engine()) as db,db.begin():
        job_id=enqueue_collection(db,source_id('tushare_dc_member'),day=DAY).id
    running=claim();assert running.id==job_id
    process(running)
    assert [code for code,_ in calls]==['BK1184.DC','BK1185.DC']
    assert (calls[1][1]-calls[0][1]).total_seconds()>=2
    with Session(engine()) as db:
        job=db.get(Job,job_id)
        assert job.state=='succeeded',job.error_code
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job_id))
        assert run.status=='complete' and run.request_count==2 and run.accepted==2
        keys=set(db.scalars(select(EvidenceVersion.source_key).where(EvidenceVersion.source_id==source_id('tushare_dc_member'))))
        assert keys=={'reference:dc_member:BK1184.DC','reference:dc_member:BK1185.DC'}


def fund_page(start,count,trade_date='20261023',*,reverse_fields=False):
    fields=['trade_date','ts_code','name','net_amount']
    rows=[[trade_date,f'{index:06}.SZ','合成分页资金股票',str(index-3000)] for index in range(start,start+count)]
    if reverse_fields:
        fields=list(reversed(fields));rows=[list(reversed(row)) for row in rows]
    return body(fields,rows)


def test_fund_pager_accepts_6001_distinct_rows_only_after_short_final_page():
    pager=DCFundPagination('20261023')
    pager.add(fund_page(1,3000),AT)
    pager.add(fund_page(3001,3000),AT+timedelta(seconds=3))
    with pytest.raises(SourceFailure,match='market_scope_truncated'):pager.batch()
    pager.add(fund_page(6001,1),AT+timedelta(seconds=6))
    batch=pager.batch()
    payload=batch.records[0][1]
    assert len(payload.rows)==6001 and batch.requests==3
    assert payload.rows[0]['ts_code']=='000001.SZ' and payload.rows[-1]['ts_code']=='006001.SZ'
    assert next(column.unit for column in payload.columns if column.key=='net_amount')=='万元'
    # The unpaged 6,000-row ambiguity guard remains in force.
    with pytest.raises(SourceFailure,match='market_scope_truncated'):
        parse_reference(fund_page(1,6000),'tushare_moneyflow_dc',AT)


def test_fund_pager_empty_last_page_confirms_exact_page_multiple_without_guessing():
    pager=DCFundPagination('20261023')
    pager.add(fund_page(1,3000),AT)
    pager.add(body([],[]),AT+timedelta(seconds=3))
    assert len(pager.batch().records[0][1].rows)==3000 and pager.finished


@pytest.mark.parametrize('continuation,reason',[
    (lambda:fund_page(1,3000),'pagination_overlap'),
    (lambda:fund_page(2999,2),'pagination_overlap'),
    (lambda:fund_page(3001,1,reverse_fields=True),'pagination_fields_changed'),
    (lambda:fund_page(3001,1,trade_date='20261022'),'history_response_out_of_range'),
    (lambda:fund_page(3001,3001),'pagination_size_mismatch'),
])
def test_fund_pager_rejects_ignored_offset_overlap_wrong_day_and_changed_columns(continuation,reason):
    pager=DCFundPagination('20261023')
    pager.add(fund_page(1,3000),AT)
    with pytest.raises(SourceFailure,match=reason):pager.add(continuation(),AT+timedelta(seconds=3))


def test_fund_pager_keeps_existing_8000_table_bound_and_never_adds_a_fourth_request():
    full=DCFundPagination('20261023')
    full.add(fund_page(1,3000),AT);full.add(fund_page(3001,3000),AT+timedelta(seconds=3))
    full.add(fund_page(6001,2000),AT+timedelta(seconds=6))
    assert len(full.batch().records[0][1].rows)==8000
    with pytest.raises(SourceFailure,match='pagination_limit_reached'):
        full.add(body([],[]),AT+timedelta(seconds=9))
    over=DCFundPagination('20261023')
    over.add(fund_page(1,3000),AT);over.add(fund_page(3001,3000),AT+timedelta(seconds=3))
    with pytest.raises(SourceFailure,match='market_scope_truncated'):
        over.add(fund_page(6001,2001),AT+timedelta(seconds=6))


def test_fund_pager_full_unpublished_result_is_not_a_zero_table():
    with pytest.raises(SourceFailure,match='daily_data_not_published'):
        DCFundPagination('20261023').add(body([],[]),AT)


@pytest.mark.parametrize('failed_second_page',[False,True])
def test_moneyflow_pagination_collects_atomically_under_real_shared_budget(client,monkeypatch,failed_second_page):
    from pydantic import SecretStr
    from app.config import settings
    from app.market.protocol import Response
    from app.market.service import enqueue_collection
    from app.worker import claim,process
    setup(client,monkeypatch,['tushare_moneyflow_dc'])
    monkeypatch.setattr(settings(),'tushare_token',SecretStr('synthetic-terminal-paging-token'))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    with Session(engine()) as db,db.begin():
        if failed_second_page:
            save(db,'tushare_moneyflow_dc',[{'trade_date':'20261023','ts_code':'000099.SZ','name':'合成旧缓存','net_amount':'0'}],AT)
        before=set(db.scalars(select(EvidenceVersion.id)))
        job_id=enqueue_collection(db,source_id('tushare_moneyflow_dc'),day=DAY).id
    calls=[]
    def respond(api,params,fields,credential,endpoint):
        assert api=='moneyflow_dc' and params['trade_date']=='20261023' and params['limit']==3000
        calls.append((params['offset'],datetime.now(UTC)))
        start=params['offset']+1
        raw=(fund_page(1,3000) if failed_second_page and params['offset']==3000 else
            fund_page(start,1 if params['offset']==6000 else 3000))
        return Response(raw,datetime.now(UTC),1,200)
    monkeypatch.setattr('app.market.collect.request_tushare',respond)
    running=claim();assert running.id==job_id
    process(running)
    with Session(engine()) as db:
        outcome=db.get(Job,job_id)
        diagnostic=(outcome.state,outcome.error_code)
    assert [offset for offset,_ in calls]==([0,3000] if failed_second_page else [0,3000,6000]),diagnostic
    assert all((right[1]-left[1]).total_seconds()>=2 for left,right in zip(calls,calls[1:]))
    with Session(engine()) as db:
        job=db.get(Job,job_id)
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job_id))
        if failed_second_page:
            assert job.state=='failed' and job.error_code=='pagination_overlap'
            assert set(db.scalars(select(EvidenceVersion.id)))==before
            assert run.request_count==2 and run.status=='error'
        else:
            assert job.state=='succeeded',job.error_code
            assert run.status=='complete' and run.request_count==3 and run.accepted==1
            evidence=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id('tushare_moneyflow_dc')))
            assert len(evidence.payload['rows'])==6001
            assert evidence.source_key=='reference:moneyflow_dc'
