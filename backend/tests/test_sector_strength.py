"""Exact-code breadth and saved curve selection; disposable database only."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.market.sector_strength import count_strength
from app.market.terminal_schemas import TerminalStock
from test_intraday_funds import AT, DAY, read, save, setup


def board(code='BK0001.DC'):
    return SimpleNamespace(code=code,name='同名题材',category='concept',industry_level=None)


def quote(code='600001.SH', **changes):
    return TerminalStock(code=code,name=code,**(dict(price=11,limit_up=11,price_basis='intraday',
        source_time=AT.isoformat(),source_received_at=AT)|changes))


def test_exact_members_known_zero_unknown_and_duplicate_members():
    boards=[board('BK0001.DC'),board('BK0002.DC'),board('BK0003.DC'),board('BK0004.DC')]
    membership={'BK0001.DC':(DAY,set(['600001.SH']*2)), 'BK0002.DC':(DAY,{'600002.SH'}),
        'BK0003.DC':(DAY,{'600003.SH'})}
    rows=count_strength(boards,membership,{'600001.SH':quote(),'600002.SH':quote('600002.SH',price=10.99)},DAY,AT)
    assert [r.limit_up_count for r in rows]==[1,0,None,None]
    assert rows[0].member_count==1 and not rows[0].partial
    assert rows[-1].member_count is None


@pytest.mark.parametrize('changes',[
    {'limit_up':None}, {'price':None}, {'price':float('nan')}, {'price_basis':'snapshot'},
    {'source_time':(AT-timedelta(minutes=4)).isoformat()},
    {'source_time':(AT+timedelta(seconds=1)).isoformat()},
    {'source_time':(AT-timedelta(days=1)).isoformat()},
    {'source_received_at':AT+timedelta(seconds=1)}, {'source_time':'invalid'},
    {'source_time':AT.replace(tzinfo=None).isoformat()}, {'price_basis':'daily_close'},
])
def test_invalid_or_future_quotes_are_unknown(changes):
    row=count_strength([board()],{'BK0001.DC':(DAY,{'600001.SH'})},{'600001.SH':quote(**changes)},DAY,AT)[0]
    assert row.limit_up_count is None and row.quoted_count==0 and row.partial


def test_older_members_partial_quotes_and_lunch_clock():
    stamp=AT.replace(hour=3,minute=30)
    row=count_strength([board()],{'BK0001.DC':(DAY-timedelta(days=1),{'600001.SH','600002.SH'})},
        {'600001.SH':quote(source_time=stamp.isoformat(),source_received_at=stamp)},DAY,stamp+timedelta(minutes=60))[0]
    assert row.limit_up_count==1 and row.quoted_count==1 and row.member_count==2 and row.partial
    assert row.members_day!=row.trade_day


def test_daily_close_is_only_counted_when_explicitly_allowed():
    row=count_strength([board()],{'BK0001.DC':(DAY,{'600001.SH'})},
        {'600001.SH':quote(price_basis='daily_close',source_time=None)},DAY,AT,daily_close=True)[0]
    assert row.limit_up_count==1 and row.basis=='daily_close'


@pytest.mark.parametrize('seconds,expected',[(0,1),(2,1),(59,1),(60,None),(273,None)])
def test_actual_closing_minute_keeps_seconds_but_later_packets_are_not_close(seconds,expected):
    stamp=AT.replace(hour=7,minute=0)+timedelta(seconds=seconds)
    row=count_strength([board()],{'BK0001.DC':(DAY,{'600001.SH'})},
        {'600001.SH':quote(source_time=stamp.isoformat(),source_received_at=stamp)},DAY,
        AT.replace(hour=7,minute=16))[0]
    assert row.limit_up_count==expected
    assert row.source_time==(stamp if expected else None)


def test_close_quote_received_after_historical_cutoff_stays_unknown():
    stamp=AT.replace(hour=7,minute=0,second=2)
    row=count_strength([board()],{'BK0001.DC':(DAY,{'600001.SH'})},
        {'600001.SH':quote(source_time=stamp.isoformat(),source_received_at=stamp+timedelta(seconds=5))},
        DAY,stamp+timedelta(seconds=1))[0]
    assert row.limit_up_count is None


def test_checked_board_outside_top_ten_uses_saved_points_without_collection(client,monkeypatch):
    setup(client)
    with Session(engine()) as db,db.begin():
        save(db,[(f'BK{i:04d}',100-i) for i in range(1,13)])
    monkeypatch.setattr('urllib.request.build_opener',lambda *args:pytest.fail('read must not collect'))
    ordinary=read()
    assert 'BK0012.DC' not in {p.sector_code for p in ordinary.points}
    compared=read(compare_codes=('BK0012.DC','BK9999.DC'))
    assert 'BK0012.DC' in {p.sector_code for p in compared.points}
    assert 'BK9999.DC' not in {p.sector_code for p in compared.points}
    assert ordinary.sectors==compared.sectors
    assert not read(AT-timedelta(seconds=1),compare_codes=('BK0012.DC',)).points


def test_compare_query_rejects_other_namespaces_and_more_than_ten(client):
    setup(client)
    for codes in ['123456','BK0001.DC,123456',','.join(f'BK{i:04d}.DC' for i in range(11))]:
        assert client.get('/api/v1/workbench-view',params={'view':'funds','fund_compare':codes}).status_code==422


def test_workbench_strength_counts_all_members_not_displayed_200(client,monkeypatch):
    from test_terminal_workbench import setup as prepare,save as store,view
    from test_tushare_market import quote as raw_quote,limits
    prepare(client,monkeypatch,['tushare_market_rt','tushare_market_limits','tushare_dc_member','tushare_moneyflow_ind_dc'])
    codes=[f'{100+i:06}.SZ' for i in range(205)]
    receipt=AT.replace(hour=9)
    with Session(engine()) as db,db.begin():
        store(db,'tushare_market_limits',[limits(code) for code in codes],AT-timedelta(minutes=1))
        store(db,'tushare_market_rt',[raw_quote(code,price='11',at=AT) for code in codes])
        store(db,'tushare_moneyflow_ind_dc',[{'trade_date':'20261023','ts_code':'BK1184.DC','name':'同名题材','net_amount':'10'}],receipt)
        store(db,'tushare_dc_member',[{'trade_date':'20261023','ts_code':'BK1184.DC','con_code':code,'name':'合成股票'} for code in codes],receipt)
    data=view(client,'funds')
    assert len(data['stocks'])==200
    row=next(r for r in data['sector_strength'] if r['code']=='BK1184.DC')
    assert row['limit_up_count']==205 and row['quoted_count']==205 and row['partial']
    # A receipt after the historical cutoff cannot enter the earlier view.
    earlier=view(client,'funds','&as_of_time=2026-10-23T10:30:00%2B08:00')
    assert not earlier['sector_strength']


def test_complete_archive_previous_day_and_permission_boundary(monkeypatch):
    from app.market.sector_strength import sector_strength
    old=DAY-timedelta(days=1)
    reader=SimpleNamespace(db=None,cutoff=AT,permitted={'tushare_dc_member':True},tables={},input_versions={},
        calendar_days=[old,DAY,*[DAY+timedelta(days=i) for i in range(1,10)]],dates=lambda *args:[],
        table=lambda *args:[],day=lambda value:__import__('datetime').date.fromisoformat(value))
    calls=[]
    def archive(db,dataset,day,**kwargs):
        calls.append(day)
        return {'state':'complete' if day==old else 'missing','rows':[{'trade_date':old.isoformat(),'ts_code':'BK0001.DC','con_code':'600001.SH'}]}
    monkeypatch.setattr('app.market.review_store.load_dataset',archive)
    result=sector_strength(reader,[board()],{'600001.SH':quote()},DAY,current=True)
    assert result[0].limit_up_count==1 and result[0].members_day==old and result[0].partial
    assert calls==[DAY,old]
    calls.clear();reader.permitted={}
    assert sector_strength(reader,[board()],{'600001.SH':quote()},DAY,current=True)[0].limit_up_count is None
    assert calls==[]
