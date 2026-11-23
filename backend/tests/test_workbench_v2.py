"""Synthetic rows in the disposable PostgreSQL; GET must never acquire data."""
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collect import store_reference_batch
from app.market.models import SourceSettings
from app.market.service import source_id
from app.market.tushare import parse_reference
from app.market.tushare_market import encoded, parse_market
from app.models import Provider
from test_data_sources import configure, configuration, login


DAY = date(2026, 10, 23)
AT = datetime(2026, 10, 23, 2, 30, tzinfo=UTC)


def setup(client, families):
    headers = login(client)
    for family in families:
        body = configuration(family)
        body['interval_seconds'] = 21600
        if family == 'tushare_dc_member':
            body['board_codes'] = ['BK1184']
        configure(client, headers, body, key='workbench-'+family)
    return headers


def save(family, rows, at=AT):
    raw = encoded(list(rows[0]), rows)
    batch = (parse_market(raw, at, limits=family == 'tushare_market_limits')
        if family in {'tushare_market_rt','tushare_market_limits'} else parse_reference(raw, family, at))
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(family))
        config = db.get(SourceSettings, provider.id)
        store_reference_batch(db, provider, config, batch, at)


def quote(price='11', at=AT, name='合成主板'):
    return dict(ts_code='600001.SH', name=name, close=price, pre_close='10', open='10',
        high='11', low='9', vol='1000', amount='10000',
        trade_time=at.strftime('%Y-%m-%d %H:%M:%S'))


def request(client, **values):
    response = client.get('/api/v1/workbench-v2', params={'view':'overview','trade_day':str(DAY), **values})
    assert response.status_code == 200, response.text
    return response.json()['data']


def test_auth_and_empty_get_are_read_only_and_do_not_request_any_supplier(client, monkeypatch):
    assert client.get('/api/v1/workbench-v2').status_code == 401
    login(client)
    monkeypatch.setattr('app.market.collect.request_tushare', lambda *a, **k: pytest.fail('GET must never collect'))
    with Session(engine()) as db:
        before = db.scalar(select(func.count()).select_from(EvidenceVersion))
    data = request(client)
    assert data['stocks'] == [] and data['stats']['limit_up'] is None
    assert {'auction_order','seal_book','fund_intraday'} <= {cap['key'] for cap in data['capabilities']}
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(EvidenceVersion)) == before


def test_tushare_single_source_scope_rights_and_exact_limits(client):
    setup(client, ['tushare_market_rt','tushare_market_limits','tushare_limit_step'])
    save('tushare_limit_step',[dict(ts_code='600001.SH',trade_date='20261022',name='合成主板',nums='3')])
    save('tushare_market_limits', [dict(ts_code='600001.SH', trade_date='20261023', up_limit='11', down_limit='9')], AT-timedelta(seconds=1))
    # Supplier clocks are Shanghai local, not UTC receipt strings.
    value = {**quote(), 'trade_time':'2026-10-23 10:30:00'}
    save('tushare_market_rt', [value, {**value, 'ts_code':'688001.SH','name':'合成科创'},
        {**value,'ts_code':'000001.SZ','name':'*ST合成'}])
    assert len(request(client)['stocks']) == 3
    data = request(client, view='ladder')
    assert len(data['stocks']) == 1 and data['stocks'][0]['ts_code'] == '600001.SH'
    assert data['stats']['limit_up'] == 1 and data['stocks'][0]['seal_amount_cny'] is None
    assert data['breadth'][-1]['up'] == 1
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('tushare_market_rt'))
        provider.rights = {**provider.rights, 'display':'denied'}
    data = request(client)
    assert all(stock['price'] is None and stock['source_time'] is None for stock in data['stocks'])
    assert data['breadth'] == []
    assert next(cap for cap in data['capabilities'] if cap['key'] == 'market_quotes')['status'] == 'permission_missing'


def test_explicit_asof_selects_versions_after_receipt_filter_and_review_can_see_late_data(client):
    setup(client, ['tushare_moneyflow_ind_dc'])
    row = dict(ts_code='BK1184.DC', trade_date='20261023', name='合成板块', net_amount='100', pct_change='1')
    save('tushare_moneyflow_ind_dc', [row], AT)
    save('tushare_moneyflow_ind_dc', [{**row,'net_amount':'200'}], AT+timedelta(minutes=1))
    result = request(client, view='funds', as_of_time=(AT+timedelta(seconds=30)).isoformat())
    assert result['mode'] == 'as_of' and result['sectors'][0]['net_cny'] == 100
    assert request(client, view='funds')['sectors'][0]['net_cny'] == 200
    assert request(client, view='funds', as_of_time=(AT-timedelta(seconds=1)).isoformat())['sectors'] == []


def test_daily_member_config_is_bounded_to_one_board_and_persisted(client):
    headers = setup(client, [])
    body = configuration('tushare_dc_member')
    body['interval_seconds'] = 21600
    response = client.post('/api/v1/data-sources', json=body, headers={**headers,'Idempotency-Key':'missing-board-workbench'})
    assert response.status_code == 422
    body['board_codes'] = ['BK1184']
    configure(client, headers, body, key='single-board-workbench')
    with Session(engine()) as db:
        assert db.get(SourceSettings, source_id('tushare_dc_member')).config['board_codes'] == ['BK1184']
    body['board_codes'] = ['BK1184','BK1000']
    response = client.post('/api/v1/data-sources', json=body, headers={**headers,'Idempotency-Key':'two-board-workbench'})
    assert response.status_code == 422


def test_invalid_future_queries_and_sector_patterns_are_rejected(client):
    login(client)
    assert client.get('/api/v1/workbench-v2?view=bad').status_code == 422
    assert client.get('/api/v1/workbench-v2?sector_code=invalid').status_code == 422
    assert client.get('/api/v1/workbench-v2?trade_day=2099-01-01').status_code == 422
    assert client.get('/api/v1/workbench-v2?as_of_time=2099-01-01T00:00:00Z').status_code == 422


def test_auction_and_funds_skip_historical_breadth_and_decode_only_used_sections(client, monkeypatch):
    from app.market import workbench_v2
    setup(client, ['tushare_market_rt', 'tushare_stk_auction', 'tushare_moneyflow_ind_dc', 'tushare_moneyflow_dc'])
    save('tushare_market_rt', [{**quote(), 'trade_time':'2026-10-23 10:30:00'}])
    save('tushare_stk_auction', [dict(ts_code='600001.SH', trade_date='20261023',
        price='10.2', pre_close='10', amount='12000')])
    save('tushare_moneyflow_ind_dc', [dict(ts_code='BK1184.DC', trade_date='20261023',
        name='合成板块', net_amount='123')])
    save('tushare_moneyflow_dc', [dict(ts_code='600001.SH', trade_date='20261023',
        name='合成股票', net_amount='1.2')])
    monkeypatch.setattr(workbench_v2, 'read_breadth', lambda *args: pytest.fail('page has no breadth chart'))
    project = workbench_v2.build_projection

    def checked_project(**values):
        assert ('tushare_stk_auction' in values['tables']) is (values['view'] == 'auction')
        assert ('tushare_moneyflow_dc' in values['tables']) is (values['view'] == 'funds')
        return project(**values)

    monkeypatch.setattr(workbench_v2, 'build_projection', checked_project)
    auction = request(client, view='auction')
    assert auction['breadth'] == [] and auction['fund_history'] == []
    assert auction['stats']['advancing'] == 1 and auction['auction_days'][0]['total_count'] == 1
    funds = request(client, view='funds')
    assert funds['breadth'] == [] and funds['auction_days'] == []
    assert funds['stats']['advancing'] == 1 and len(funds['fund_history']) == 1
    assert next(cap for cap in funds['capabilities'] if cap['key'] == 'auction_trade')['status'] == 'partial'


def test_full_breadth_is_chunked_single_flight_and_permissions_are_rechecked(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import event
    from app.market.workbench_v2 import workbench_view, _BREADTH_CACHE, _BREADTH_LOCK
    from app.models import User
    setup(client, ['tushare_market_rt','tushare_market_limits'])
    save('tushare_market_limits', [dict(ts_code='600001.SH', trade_date='20261023', up_limit='11', down_limit='9')],AT-timedelta(seconds=1))
    for minute in range(31):
        at=AT+timedelta(minutes=minute)
        value={**quote(at=at),'trade_time':at.astimezone(__import__('app.market.protocol',fromlist=['SHANGHAI']).SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')}
        save('tushare_market_rt',[value],at)
    with Session(engine()) as db:owner=db.scalar(select(User.id).limit(1))
    with _BREADTH_LOCK:_BREADTH_CACHE.clear()
    calls=[]
    def query_count(conn,cursor,statement,parameters,context,many):
        if 'WITH pairs AS MATERIALIZED' in statement:calls.append(1)
    event.listen(engine(),'before_cursor_execute',query_count)
    barrier=Barrier(2)
    def read(view):
        with Session(engine()) as db:
            barrier.wait(timeout=10)
            return workbench_view(db,owner,view=view,trade_day=DAY)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(read,'overview');second=pool.submit(read,'ladder')
            a,b=first.result(timeout=20),second.result(timeout=20)
        assert len(a.breadth)==31 and len(b.breadth)==31
        # Two bounded statements for 31 real points, shared across both scopes.
        assert len(calls)==2
        with Session(engine()) as db,db.begin():
            provider=db.get(Provider,source_id('tushare_market_rt'))
            provider.rights={**provider.rights,'display':'denied'}
        assert request(client)['breadth']==[]
        assert len(calls)==2
    finally:
        event.remove(engine(),'before_cursor_execute',query_count)
