"""Public funds reuse must retain source rights, real age and exact cutoffs."""
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market import intraday_funds as module
from app.market.models import DataFetchRun
from app.market.service import source_id
from app.models import Provider
from test_intraday_funds import setup,save,AT,DAY


def current(at=AT,phase='trading'):
    with Session(engine()) as db:
        return module.intraday_funds(db,DAY,at,current=True,session={'phase':phase})


def test_unchanged_public_inputs_are_shared_but_callers_cannot_mutate_the_next_view(client,monkeypatch):
    setup(client);monkeypatch.setattr(settings(),'read_cache_enabled',True)
    with Session(engine()) as db,db.begin():save(db,[('BK0001','100')])
    first=current();first.sectors[0].main_net_cny=999
    monkeypatch.setattr(module,'_native_funds',lambda *a,**k:pytest.fail('unchanged evidence should reuse its public projection'))
    second=current(AT+timedelta(seconds=1))
    assert second.sectors[0].main_net_cny==100 and second.state=='available'
    assert current(AT+timedelta(minutes=6)).state=='stale'


def test_new_evidence_or_failed_fetch_and_revoked_source_cannot_reuse_an_old_normal_result(client,monkeypatch):
    setup(client);monkeypatch.setattr(settings(),'read_cache_enabled',True)
    with Session(engine()) as db,db.begin():save(db,[('BK0001','100')])
    assert current().state=='available'
    later=AT+timedelta(seconds=1)
    with Session(engine()) as db,db.begin():save(db,[('BK0001','200')],later)
    assert current(later).sectors[0].main_net_cny==200
    with Session(engine()) as db,db.begin():
        db.add(DataFetchRun(source_id=source_id('eastmoney_funds'),capability='market.eastmoney.funds',
            started_at=later,received_at=later,status='error',reason='timeout',expected=3,
            accepted=0,rejected=0,duplicates=0,request_count=1))
    assert current(later).state=='stale'
    with Session(engine()) as db,db.begin():db.get(Provider,source_id('eastmoney_funds')).enabled=False
    denied=current(later)
    assert denied.state=='permission_denied' and not denied.sectors and not denied.points


def test_future_native_clock_is_neither_cached_as_missing_nor_leaked_when_clock_moves_back(client,monkeypatch):
    setup(client);monkeypatch.setattr(settings(),'read_cache_enabled',True)
    with Session(engine()) as db,db.begin():
        save(db,[('BK0001','100')],AT+timedelta(seconds=30),received=AT)
    assert current().state=='missing'
    later=current(AT+timedelta(seconds=31))
    assert later.state=='available' and later.sectors[0].source_time==AT+timedelta(seconds=30)
    assert current().state=='missing'


def test_cached_age_uses_lunch_boundary_and_resumes_actual_age_after_reopening(client,monkeypatch):
    setup(client);monkeypatch.setattr(settings(),'read_cache_enabled',True)
    at=AT.replace(hour=3,minute=29,second=30)
    with Session(engine()) as db,db.begin():save(db,[('BK0001','100')],at)
    assert current(at).state=='available'
    monkeypatch.setattr(module,'_native_funds',lambda *a,**k:pytest.fail('a phase change does not rewrite unchanged evidence'))
    assert current(at.replace(hour=4,minute=30),phase='break').state=='available'
    assert current(at.replace(hour=5,minute=0),phase='trading').state=='stale'
