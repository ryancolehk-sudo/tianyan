"""Weekend/holiday visibility uses real saved dates, never invented market days."""
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.models import DataFetchRun
from app.market.service import source_id
from app.models import Provider
from test_minute_read import DAY, payload, setup, store

SATURDAY=datetime(2026,10,24,2,0,tzinfo=UTC)


def test_weekend_auto_date_preserves_saved_friday_without_supplier_or_writes(client,monkeypatch):
    _,ident,_=setup(client)
    monkeypatch.setattr('app.market.minute_read.now',lambda db:SATURDAY)
    with Session(engine()) as db,db.begin():
        store(db,ident,payload())
        before=db.scalar(select(func.count()).select_from(EvidenceVersion))
    result=client.get('/api/v1/market-bars',params={'instrument_id':str(ident),'interval':'1m'})
    assert result.status_code==200
    view=result.json()['data']
    assert view['observation_day']==str(DAY) and view['state']=='historical'
    assert len(view['items'])==1 and view['items'][0]['payload']['close']=='11'
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(EvidenceVersion))==before
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==0


def test_explicit_empty_day_is_not_replaced_and_latest_button_can_restore_saved_day(client,monkeypatch):
    _,ident,_=setup(client)
    monkeypatch.setattr('app.market.minute_read.now',lambda db:SATURDAY)
    with Session(engine()) as db,db.begin():store(db,ident,payload())
    query={'instrument_id':str(ident),'interval':'1m'}
    empty=client.get('/api/v1/market-bars',params={**query,'day':'2026-10-24'}).json()['data']
    assert empty['observation_day']=='2026-10-24' and empty['items']==[] and empty['state']=='empty'
    latest=client.get('/api/v1/market-bars',params=query).json()['data']
    assert latest['observation_day']=='2026-10-23' and len(latest['items'])==1


@pytest.mark.parametrize('revocation',['cache','display','enabled'])
def test_no_saved_date_or_prices_disclosed_after_source_revocation(client,monkeypatch,revocation):
    _,ident,_=setup(client)
    monkeypatch.setattr('app.market.minute_read.now',lambda db:SATURDAY)
    with Session(engine()) as db,db.begin():
        store(db,ident,payload())
        provider=db.get(Provider,source_id('sina_minute_1m'))
        if revocation=='enabled':provider.enabled=False
        else:provider.rights={**provider.rights,revocation:'denied'}
    view=client.get('/api/v1/market-bars',params={'instrument_id':str(ident),'interval':'1m'}).json()['data']
    assert view['observation_day'] is None and view['items']==[] and view['latest_source_time'] is None


def test_latest_day_is_bound_to_stock_period_and_actual_receipt_and_source_clocks(client,monkeypatch):
    _,ident,other=setup(client)
    monkeypatch.setattr('app.market.minute_read.now',lambda db:SATURDAY)
    with Session(engine()) as db,db.begin():
        store(db,ident,payload())
        # Another stock and period cannot switch this 1m series to Saturday.
        saturday={**payload('2026-10-24T09:31:00+08:00'),'trade_day':'2026-10-24'}
        store(db,other,saturday,received=SATURDAY)
        store(db,ident,{**saturday,'interval':'5m'},received=SATURDAY)
        # Future receipt and future source time independently remain excluded.
        store(db,ident,saturday,received=SATURDAY+timedelta(seconds=1))
        future={**saturday,'source_clock_at':'2026-10-24T13:01:00+08:00'}
        store(db,ident,future,received=SATURDAY)
    value=client.get('/api/v1/market-bars',params={'instrument_id':str(ident),'interval':'1m'}).json()['data']
    assert value['observation_day']=='2026-10-23' and len(value['items'])==1


def test_auto_series_can_advance_to_new_actual_saved_trading_day(client,monkeypatch):
    _,ident,_=setup(client)
    monkeypatch.setattr('app.market.minute_read.now',lambda db:SATURDAY)
    with Session(engine()) as db,db.begin():store(db,ident,payload())
    query={'instrument_id':str(ident),'interval':'1m'}
    assert client.get('/api/v1/market-bars',params=query).json()['data']['observation_day']=='2026-10-23'
    monday=datetime(2026,10,26,2,0,tzinfo=UTC)
    monkeypatch.setattr('app.market.minute_read.now',lambda db:monday)
    with Session(engine()) as db,db.begin():
        row={**payload('2026-10-26T09:31:00+08:00'),'trade_day':'2026-10-26'}
        store(db,ident,row,received=monday)
    value=client.get('/api/v1/market-bars',params=query).json()['data']
    assert value['observation_day']=='2026-10-26' and len(value['items'])==1
