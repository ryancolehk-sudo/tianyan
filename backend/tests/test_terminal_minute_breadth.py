"""Minute samples retain actual clocks, receipt cutoffs and exact limit versions."""
from datetime import datetime, time, timedelta

from sqlalchemy.orm import Session

from app.db import engine
from app.market.protocol import SHANGHAI
from test_terminal_workbench import DAY, save, setup, view
from test_tushare_market import limits, quote

START=datetime.combine(DAY,time(9,30),SHANGHAI)


def sample(db,at,price='11'):
    save(db,'tushare_market_rt',[quote(price=price,at=at)],at+timedelta(seconds=1))


def test_each_saved_minute_survives_past_sixty_points_and_latest_same_minute_wins(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_limits',[limits()],START-timedelta(seconds=5))
        for minute in range(70):sample(db,START+timedelta(minutes=minute))
        sample(db,START+timedelta(seconds=40),price='9')
    points=view(client)['breadth']['points']
    assert len(points)==70
    assert datetime.fromisoformat(points[0]['time'])==START+timedelta(seconds=40)
    assert (points[0]['up'],points[0]['down'])==(0,1)
    assert all((p['up'],p['down'])==(1,0) for p in points[1:])
    assert datetime.fromisoformat(points[-1]['time'])==START+timedelta(minutes=69)


def test_minute_curve_does_not_apply_a_later_limit_table_or_receipt_to_earlier_samples(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_limits',[limits()],START-timedelta(seconds=5))
        for minute in range(4):sample(db,START+timedelta(minutes=minute))
        save(db,'tushare_market_limits',[limits(upper='12')],START+timedelta(minutes=2,seconds=30))
    points=view(client)['breadth']['points']
    assert [p['up'] for p in points]==[1,1,1,0]
    cutoff=(START+timedelta(minutes=1,seconds=2)).isoformat().replace('+','%2B')
    observed=view(client,params='&as_of_time='+cutoff)['breadth']['points']
    assert [p['up'] for p in observed]==[1,1]
    assert all(datetime.fromisoformat(p['time'])<=START+timedelta(minutes=1) for p in observed)


def test_missing_minutes_stay_empty_but_lunch_is_not_a_trading_gap(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_limits',[limits()],START-timedelta(seconds=5))
        for minute in (0,1,4,120,210):sample(db,START+timedelta(minutes=minute))
    points=view(client)['breadth']['points']
    actual=[p for p in points if p['up'] is not None]
    assert len(actual)==5
    assert [datetime.fromisoformat(p['time']) for p in actual]==[START+timedelta(minutes=m) for m in (0,1,4,120,210)]
    # Nulls mark the three-minute and long pre-lunch outages; the normal lunch
    # closure between 11:30 and 13:00 does not add another false outage.
    assert len([p for p in points if p['up'] is None and p['down'] is None])==2
