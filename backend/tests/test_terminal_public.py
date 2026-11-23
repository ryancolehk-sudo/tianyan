"""Shared prices must keep user choices and mutable page state separate."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import hasher
from app.config import settings
from app.db import engine
from app.market import terminal_read, terminal_public
from app.market.models import Watchlist
from app.market.service import source_id
from app.models import Instrument, Provider, User
from test_terminal_workbench import AT, save, setup
from test_tushare_market import quote


def test_four_views_and_two_owners_reuse_prices_without_sharing_choices_or_mutations(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    monkeypatch.setattr(settings(),'read_cache_enabled',True)
    with Session(engine()) as db,db.begin():
        owner=db.scalar(select(User))
        other=User(username='other',password_hash=hasher.hash('synthetic-public-universe-user'),role='viewer',enabled=True)
        db.add(other);db.flush()
        instruments=[Instrument(exchange='SZSE',symbol=code,asset_type='stock',name='synthetic '+code)
            for code in ('000100','000101')]
        db.add_all(instruments);db.flush()
        db.add(Watchlist(owner_id=owner.id,instrument_ids=[str(instruments[0].id)],updated_at=AT))
        db.add(Watchlist(owner_id=other.id,instrument_ids=[str(instruments[1].id)],updated_at=AT))
        owners=[owner.id,other.id]
        save(db,'tushare_market_rt',[quote('000100.SZ',price='10'),quote('000101.SZ',price='11')])
    original=terminal_read.quoted_price;calls=[]
    def priced(value):calls.append(value);return original(value)
    monkeypatch.setattr(terminal_read,'quoted_price',priced)
    original_get=terminal_public.get;reused=[]
    def shared(key):
        result=original_get(key);reused.append(result is not None);return result
    monkeypatch.setattr(terminal_public,'get',shared)
    with Session(engine()) as db:
        first=terminal_read.workbench_view(db,owners[0],view='funds')
    computed=len(calls)
    assert computed>0 and len(terminal_public._values)==1
    assert first.stocks[0].code=='000100.SZ'
    original_name=first.stocks[0].name
    first.stocks[0].name='mutated outside shared prices'
    first.stocks[0].node_amounts['09:15']=999
    for view in ('overview','auction','ladder'):
        with Session(engine()) as db:
            second=terminal_read.workbench_view(db,owners[1],view=view)
        assert second.stocks[0].code=='000101.SZ'
        stock=next(item for item in second.stocks if item.code=='000100.SZ')
        assert stock.name==original_name and stock.node_amounts['09:15'] is None
    assert reused==[False,True,True,True]


def test_committed_quote_and_permission_changes_cannot_reuse_old_shared_prices(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    monkeypatch.setattr(settings(),'read_cache_enabled',True)
    with Session(engine()) as db,db.begin():
        owner=db.scalar(select(User.id));save(db,'tushare_market_rt',[quote('000100.SZ',price='10')])
    with Session(engine()) as db:
        assert terminal_read.workbench_view(db,owner,view='auction').stocks[0].price==10
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote('000100.SZ',price='12',at=AT+timedelta(minutes=1))],AT+timedelta(minutes=1))
    with Session(engine()) as db:
        assert terminal_read.workbench_view(db,owner,view='funds').stocks[0].price==12
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_market_rt'))
        provider.rights={**provider.rights,'display':'denied'}
    with Session(engine()) as db:
        assert not terminal_read.workbench_view(db,owner,view='funds').stocks
