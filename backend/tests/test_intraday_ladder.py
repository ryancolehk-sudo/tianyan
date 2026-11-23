"""Synthetic current-day ladder; disposable database only, no supplier calls."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.market.intraday_ladder import live_ladder
from app.market.terminal_schemas import TerminalStock
from app.market.models import DataFetchRun
from app.models import Job
from test_terminal_workbench import AT, DAY, calendar, save, setup, view
from test_tushare_market import quote, limits


def row(code='600001.SH', **values):
    return TerminalStock(code=code,name='合成',previous_height=2,price=11,limit_up=11,
        high=11,source_time=AT.isoformat(),price_basis='intraday',**values)


def project(rows, cutoff=AT, previous=DAY-timedelta(days=1), **kwargs):
    session=SimpleNamespace(is_trading_day=True,trade_day=DAY,phase='trading')
    return live_ladder({r.code:r for r in rows},DAY,previous,cutoff,session,**kwargs)


def test_seal_touch_open_and_wait_are_distinct_and_refresh_removes_seal():
    r=row(seal_amount_cny=1000)
    result=project([r]); assert result.sealed_count==1 and result.rows[0].target_height==3
    r.seal_amount_cny=None
    assert project([r]).rows[0].status=='touched'
    r.price=10.5
    assert project([r]).rows[0].status=='opened' and project([r]).sealed_count==0
    r.high=10.5
    assert project([r]).rows[0].status=='waiting'


def test_missing_height_is_unknown_not_first_board_and_missing_calendar_is_not_guessed():
    r=row();r.previous_height=None
    assert project([r]).rows[0].target_height is None
    assert project([row()],previous=None).rows[0].target_height is None
    assert project([row()],historical=True) is None
    assert live_ladder({},DAY,None,AT,SimpleNamespace(is_trading_day=None,trade_day=DAY)) is None


def test_future_old_undated_and_auction_prices_never_become_live_seals():
    for stamp in [None,(AT+timedelta(seconds=1)).isoformat(),(AT-timedelta(days=1)).isoformat(),
                  '2026-10-23T09:25:00+08:00','2026-10-23T10:30:00']:
        r=row(seal_amount_cny=1000);r.source_time=stamp
        result=project([r]);assert result.sealed_count==0 and result.rows[0].status=='unknown'
    r=row(seal_amount_cny=1000);r.price_basis='snapshot'
    assert project([r]).rows[0].freshness=='unknown'
    r.price_basis='daily_close'
    assert project([r]).sealed_count==0


def test_stale_values_keep_timestamp_but_do_not_count_and_lunch_preserves_last_sample():
    r=row(seal_amount_cny=1000)
    result=project([r],AT+timedelta(minutes=4))
    assert result.rows[0].freshness=='stale' and result.sealed_count==0
    r.source_time='2026-10-23T11:30:00+08:00'
    assert project([r],datetime.fromisoformat('2026-10-23T12:30:00+08:00')).sealed_count==1


@pytest.mark.parametrize(('source', 'cutoff', 'eligible'), [
    ('11:30:02', '12:30:00', True),
    ('11:30:59.999999', '12:30:00', True),
    ('15:00:02', '15:01:00', True),
    ('15:00:59.999999', '15:01:00', True),
    ('11:31:00', '12:30:00', False),
    ('12:00:00', '12:30:00', False),
    ('15:01:00', '15:02:00', False),
    ('15:04:00', '15:05:00', False),
    ('15:00:02', '15:00:01', False),
])
def test_regular_session_closing_minute_retains_source_seconds_without_borrowing_later_quotes(source, cutoff, eligible):
    r = row(seal_amount_cny=1000)
    stamp = datetime.fromisoformat(f'{DAY}T{source}+08:00')
    moment = datetime.fromisoformat(f'{DAY}T{cutoff}+08:00')
    r.source_time = stamp.isoformat()
    result = project([r], moment)
    assert result.sealed_count == int(eligible)
    assert result.rows[0].source_time == (stamp if eligible else None)


def test_ladder_rejects_a_quote_received_after_the_frozen_cutoff():
    r = row(seal_amount_cny=1000, source_received_at=AT + timedelta(seconds=1))
    result = project([r])
    assert result.sealed_count == 0 and result.rows[0].source_time is None


@pytest.mark.parametrize(('source', 'expected'), [
    ('11:30:02', 'am'), ('11:30:59.999999', 'am'),
    ('15:00:02', 'pm'), ('15:00:59.999999', 'pm'),
    ('11:31:00', None), ('12:00:00', None),
    ('15:01:00', None), ('15:04:33', None),
])
def test_price_projection_and_live_ladder_share_the_same_full_closing_minute(source, expected):
    from app.market.terminal_read import segment
    stamp = datetime.fromisoformat(f'{DAY}T{source}+08:00')
    assert segment(stamp) == ((DAY, expected) if expected else None)


def test_non_mainboard_st_and_retired_names_not_mixed_into_scope():
    rows=[row('300001.SZ'),row('688001.SH'),row('920001.BJ')]
    r=row();r.name='ST合成';rows.append(r)
    assert project(rows).rows==[]


def test_workbench_live_column_uses_full_batch_before_200_row_cap_and_no_writes(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits','tushare_limit_step','tushare_calendar_sse','tushare_calendar_szse'])
    monkeypatch.setattr('app.market.terminal_read.now',lambda db:AT)
    codes=[f'{100+i:06}.SZ' for i in range(205)]
    with Session(engine()) as db,db.begin():
        calendar(db)
        save(db,'tushare_limit_step',[dict(trade_date='20261022',ts_code=code,name='合成昨日梯队',nums='2') for code in codes],AT)
        save(db,'tushare_market_limits',[limits(code) for code in codes],AT-timedelta(seconds=1))
        save(db,'tushare_market_rt',[quote(code,price='11') for code in codes],AT)
    data=view(client,'ladder')
    assert len(data['stocks'])==200
    live=data['intraday_ladder'];assert len(live['rows'])==205 and live['sample_count']==205
    assert all(r['target_height']==3 and r['status']=='touched' for r in live['rows'])
    assert live['sealed_count']==0 and live['previous_trade_day']=='2026-10-22'
    assert view(client,'ladder','&as_of_time=2026-10-23T10:30:00%2B08:00')['intraday_ladder'] is None
    assert len(view(client,'overview')['intraday_ladder']['rows']) == 205
    assert view(client,'overview','&as_of_time=2026-10-23T10:30:00%2B08:00')['intraday_ladder'] is None
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job))==0
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==0
