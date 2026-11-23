"""The actual four-page workbench consumes paired seal snapshots, not fake trades."""
from datetime import datetime
import hashlib

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.market.auction_source import _batch, decode_table, FAMILY, HISTORY_FAMILY
from app.market.collect import store_reference_batch
from app.market.models import SourceSettings
from app.market.models import DataFetchRun
from app.market.service import source_id
from app.models import Provider
from test_auction_source import head, row, single_day
from test_auction_tables import configure_family
from test_terminal_workbench import setup, save


def at(value):
    return datetime.fromisoformat(value + '+08:00')


def test_missing_decorative_header_uses_only_supplier_total_of_elapsed_stage():
    from types import SimpleNamespace
    from app.market.terminal_auction import header_total
    from app.market.terminal_read import number
    batch=SimpleNamespace(header={'header_total_cny':None,'total_fd_0915_cny':'100',
        'total_fd_0920_cny':'200','total_fd_0925_cny':'300'},stages={'09:15'})
    assert header_total(batch,number)==100
    batch.stages.add('09:20');assert header_total(batch,number)==200
    batch.stages.add('09:25');assert header_total(batch,number)==300
    batch.header['header_total_cny']='400';assert header_total(batch,number)==400
    batch.header['header_total_cny']=None;batch.stages.clear();assert header_total(batch,number) is None


def prepare(client, monkeypatch, clock='2026-10-26T09:20:46', families=()):
    headers = setup(client, monkeypatch, list(families))
    configure_family(client, headers, FAMILY)
    monkeypatch.setattr('app.market.terminal_read.now', lambda db: at(clock))
    return headers


def seal_batch(received, *, day='2026-10-26', count=2, amount='13亿', kind='current', broken=None, cells=None):
    raw = single_day([row(f'{600100+i:06}', f'合成封单{i}', ('机器人', '3板'),
                          cells or ('10亿', '-', amount), '9.95%' if kind == 'current' else None)
                      for i in range(count)], day=day, header=head(day, live=kind == 'current'))
    batch = _batch(decode_table(raw, 'history'), kind, received, count, hashlib.sha256(raw).hexdigest())
    if broken == 'missing_summary':batch.records = batch.records[1:]
    if broken == 'mismatched_hash':batch.records[0][1].rows[0]['batch_hash'] = 'a'*64
    family = FAMILY if kind == 'current' else HISTORY_FAMILY
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(family))
        store_reference_batch(db, provider, db.get(SourceSettings, provider.id), batch, received)


def read(client, suffix=''):
    result = client.get('/api/v1/workbench-view?view=auction' + suffix)
    assert result.status_code == 200, result.text
    return result.json()['data']


def test_today_defaults_to_received_seal_day_before_matched_results_exist(client, monkeypatch):
    prepare(client, monkeypatch, families=('tushare_stk_auction',))
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_stk_auction', [{'trade_date':'20261023','ts_code':'600100.SH',
            'price':'11','pre_close':'10','amount':'1250000','vol':'1000','volume_ratio':'3.2'}], at('2026-10-23T09:26:00'))
    seal_batch(at('2026-10-26T09:20:30'))
    data = read(client)
    assert data['trade_day'] == '2026-10-26' and data['mode'] == 'live'
    today = data['auction_days'][0]
    item = today['items'][0]
    assert today['node_status'] == 'available' and today['node_source'] == '短线侠当前表'
    assert item['node_amounts'] == {'09:15':1000000000, '09:20':None, '09:25':None}
    assert item['auction_current_seal_cny'] == 1300000000
    assert item['auction_pct'] is None and item['auction_amount_cny'] is None
    assert item['auction_indicative_pct'] == 9.95 and item['auction_tags'] == ['机器人', '3板']
    assert item['auction_board'] == '3板' and data['selected_stock_code'] == item['code']
    assert next(x for x in data['capabilities'] if x['key'] == 'auction_orders')['status'] == 'partial'


@pytest.mark.parametrize('has_matched', [True, False])
def test_previous_first_board_visible_without_auction_seal_and_never_fills_nodes(client, monkeypatch, has_matched):
    prepare(client, monkeypatch, clock='2026-10-26T09:28:00',
        families=('tushare_stk_auction', 'tushare_limit_list_d'))
    seal_batch(at('2026-10-26T09:25:10'))
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_limit_list_d', [dict(trade_date='20261023', ts_code='600100.SH',
            name='合成首板', limit='U', limit_times='1', fd_amount='26760112',
            first_time='093033', last_time='093130', open_times='1')], at('2026-10-23T16:00:00'))
        # A different stock establishes the prior auction day even when the
        # selected stock has only a saved closing pool, not matched trades.
        save(db, 'tushare_stk_auction', [dict(trade_date='20261023',
            ts_code='600100.SH' if has_matched else '600999.SH', price='13.22',
            pre_close='12.38', amount='3187342', vol='241100', volume_ratio='12.7')], at('2026-10-23T09:26:00'))
    response=client.get('/api/v1/workbench-details?day=2026-10-26&codes=600100.SH')
    assert response.status_code == 200, response.text
    days=response.json()['data']['auction_days']
    old=next(day for day in days if day['day']=='2026-10-23')['items'][0]
    assert old['daily_pool']['board_height']==1
    assert old['daily_pool']['state']=='U'
    assert old['daily_pool']['first_limit_time']=='09:30:33'
    assert old['auction_amount_cny']==(3187342 if has_matched else None)
    assert all(value is None for value in old['node_amounts'].values())
    assert old['seal_amount_cny'] is None


def test_earlier_batch_does_not_unlock_future_stage_as_clock_advances(client, monkeypatch):
    prepare(client, monkeypatch, clock='2026-10-26T09:27:00')
    seal_batch(at('2026-10-26T09:19:30'))
    data = read(client)
    assert data['auction_days'][0]['items'][0]['node_amounts']['09:25'] is None
    assert data['auction_days'][0]['items'][0]['node_amounts']['09:20'] is None
    assert read(client, '&as_of_time=2026-10-26T09:18:00%2B08:00')['auction_days'] == []
    seal_batch(at('2026-10-26T09:25:10'))
    latest = read(client)['auction_days'][0]['items'][0]
    assert latest['node_amounts']['09:25'] == 1300000000
    observed = read(client, '&as_of_time=2026-10-26T09:20:00%2B08:00')
    assert observed['auction_days'][0]['items'][0]['node_amounts']['09:25'] is None


def test_current_rolls_in_the_rightmost_populated_column_before_0920(client, monkeypatch):
    prepare(client, monkeypatch)
    seal_batch(at('2026-10-26T09:18:30'), cells=('10亿', '8亿', '-'))
    item=read(client)['auction_days'][0]['items'][0]
    assert item['auction_current_seal_cny']==800000000
    assert item['node_amounts']=={'09:15':1000000000,'09:20':None,'09:25':None}


@pytest.mark.parametrize('failure', ['missing_summary', 'mismatched_hash', 'revoked'])
def test_unpaired_or_revoked_seals_never_fill_the_workbench(client, monkeypatch, failure):
    prepare(client, monkeypatch)
    seal_batch(at('2026-10-26T09:20:30'), broken=failure)
    if failure == 'revoked':
        with Session(engine()) as db, db.begin():
            p = db.get(Provider, source_id(FAMILY)); p.rights = {**p.rights, 'display':'denied'}
    data = read(client)
    assert not any(item['node_status'] == 'available' for item in data['auction_days'])


def test_full_source_list_and_selected_details_keep_trade_amounts_separate(client, monkeypatch):
    prepare(client, monkeypatch, clock='2026-10-26T09:28:00', families=('tushare_stk_auction',))
    seal_batch(at('2026-10-26T09:25:10'), count=220)
    with Session(engine()) as db, db.begin():
        # The displayed seal stock ranks below 200 unrelated matched stocks.
        # Its true matched result must still join before any fallback row cap.
        matched=[{'trade_date':'20261026','ts_code':f'{602000+i}.SH',
            'price':'11','pre_close':'10','amount':'99000000','vol':'1000','volume_ratio':'3.2'} for i in range(221)]
        matched.append({'trade_date':'20261026','ts_code':'600100.SH',
            'price':'11','pre_close':'10','amount':'1250000','vol':'1000','volume_ratio':'3.2'})
        save(db, 'tushare_stk_auction', matched, at('2026-10-26T09:26:00'))
    data = read(client)
    assert len(data['auction_days'][0]['items']) == 220
    row = next(item for item in data['auction_days'][0]['items'] if item['code'] == '600100.SH')
    assert row['auction_amount_cny'] == 1250000 and row['node_amounts']['09:25'] == 1300000000
    response = client.get('/api/v1/workbench-details?day=2026-10-26&codes=600319.SH')
    assert response.status_code == 200, response.text
    detail = response.json()['data']
    assert detail['stocks'][0]['node_amounts']['09:25'] == 1300000000
    assert detail['stocks'][0]['auction_amount_cny'] is None


def test_historical_table_fills_its_own_day_without_current_percentage(client, monkeypatch):
    headers = prepare(client, monkeypatch)
    configure_family(client, headers, HISTORY_FAMILY)
    seal_batch(at('2026-10-26T09:18:00'), day='2026-10-23', kind='history')
    old = read(client, '&day=2026-10-23')['auction_days'][0]
    assert old['day'] == '2026-10-23' and old['node_source'] == '短线侠历史表'
    assert old['items'][0]['node_amounts']['09:25'] == 1300000000
    assert old['items'][0]['auction_indicative_pct'] is None
    assert old['items'][0]['auction_current_seal_cny'] is None


def test_complete_history_supersedes_an_early_partial_current_copy(client, monkeypatch):
    headers = prepare(client, monkeypatch)
    configure_family(client, headers, HISTORY_FAMILY)
    seal_batch(at('2026-10-23T09:19:00'), day='2026-10-23', amount='8亿')
    seal_batch(at('2026-10-26T09:18:00'), day='2026-10-23', kind='history')
    old = read(client, '&day=2026-10-23')['auction_days'][0]
    assert old['node_source'] == '短线侠历史表'
    assert old['items'][0]['node_amounts']['09:25'] == 1300000000
    assert old['items'][0]['auction_indicative_pct'] is None


def test_cache_dependencies_include_both_seal_families():
    from app.read_cache import dependency_keys
    keys = dependency_keys('workbench', 'synthetic-owner')
    assert f'family:{FAMILY}' in keys and f'family:{HISTORY_FAMILY}' in keys


def test_transport_failure_keeps_the_last_snapshot_and_marks_it_stale(client, monkeypatch):
    prepare(client,monkeypatch)
    seal_batch(at('2026-10-26T09:18:30'))
    with Session(engine()) as db,db.begin():
        db.add(DataFetchRun(source_id=source_id(FAMILY),capability='market.reference',
            started_at=at('2026-10-26T09:19:30'),received_at=at('2026-10-26T09:19:31'),
            status='error',reason='transport_error',request_count=1))
    data=read(client)
    assert data['auction_days'][0]['items'][0]['node_amounts']['09:15']==1000000000
    capability=next(item for item in data['capabilities'] if item['key']=='auction_orders')
    assert capability['status']=='stale' and 'transport_error' in capability['reason']
    assert data['auction_days'][0]['node_received_at'].endswith('01:18:30Z')
