"""Single-thread scheduler checkpoints and pre-limit archive eligibility on offline PG."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.market import resilience
from app.market.models import SourceSettings
from app.market.protocol import SourceFailure
from app.market.realtime_schedule import FAMILIES
from app.market.service import source_id
from app.models import Job, Provider
import app.worker as worker
from test_data_resilience import source
from test_direction_recovery import setup as live_setup
from test_kpl_detail_cadence import setup as detail_setup


def test_long_health_sweep_dispatches_every_two_seconds_and_advances_progress(monkeypatch):
    clock=[0.0];dispatches=[];progress=[]
    monkeypatch.setattr(worker,'monotonic',lambda:clock[0])
    monkeypatch.setattr('app.market.service.schedule_sources',lambda:dispatches.append(clock[0]))
    def health(*,checkpoint):
        for _ in range(40):
            clock[0]+=0.6
            checkpoint()
    monkeypatch.setattr(resilience,'reconcile',health)
    monkeypatch.setattr('app.market.fund_focus.schedule',lambda:None)
    worker.schedule_all(progress=lambda:progress.append(clock[0]))
    assert dispatches[0]==0 and dispatches[-1]>20
    assert all(2<=after-before<=2.5 for before,after in zip(dispatches,dispatches[1:]))
    assert len(progress)>=40 and clock[0]-dispatches[-1]<2.5


def test_checkpoint_dispatch_failure_and_heartbeat_failure_do_not_abort_health(monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError
    clock=[0.0];dispatches=[];checks=[]
    monkeypatch.setattr(worker,'monotonic',lambda:clock[0])
    def dispatch():
        dispatches.append(clock[0])
        if len(dispatches)==2:raise SQLAlchemyError('synthetic database busy')
    def progress():raise SQLAlchemyError('synthetic heartbeat busy')
    def health(*,checkpoint):
        for index in range(8):
            clock[0]+=1
            checkpoint();checks.append(index)
    monkeypatch.setattr('app.market.service.schedule_sources',dispatch)
    monkeypatch.setattr(resilience,'reconcile',health)
    monkeypatch.setattr('app.market.fund_focus.schedule',lambda:None)
    worker.schedule_all(progress=progress)
    assert checks==list(range(8)) and dispatches==[0,2,4,6,8]


@pytest.mark.parametrize('denial',[None,'maintenance','calendar','cooldown'])
def test_due_feed_enqueues_during_real_health_loop_and_keeps_existing_gates(client,monkeypatch,denial):
    import app.market.service as service
    clock=live_setup(client,monkeypatch,'direction_kpl_strength')
    start=clock[0];due=start+timedelta(seconds=6);ident=source_id('direction_kpl_strength')
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,ident);config.paused_reason=None;config.interval_seconds=60;config.next_run_at=due
        for family in sorted(FAMILIES-{'direction_kpl_strength'})[:20]:
            _,other=source(db,config.owner_id,family,symbols=[])
            other.next_run_at=start+timedelta(days=2)
    monkeypatch.setattr(worker,'monotonic',lambda:(clock[0]-start).total_seconds())
    monkeypatch.setattr(resilience,'now',lambda db:clock[0])
    monkeypatch.setattr(resilience,'projection_health',lambda *a:None)
    monkeypatch.setattr('app.market.fund_focus.schedule',lambda:None)
    monkeypatch.setattr('app.market.direction_sources.fetch',lambda *a,**k:pytest.fail('scheduler cannot fetch'))
    if denial=='maintenance':
        monkeypatch.setattr('app.maintenance_control.paused',lambda db:clock[0]>=due)
    if denial=='calendar':
        def calendar(*a,**k):
            if clock[0]>=due:raise SourceFailure('calendar_not_ready')
            return start.date()
        monkeypatch.setattr('app.market.mairui.pool_day',calendar)
    if denial=='cooldown':
        monkeypatch.setattr('app.market.budget.family_cooldown_until',
            lambda *a,**k:clock[0]+timedelta(minutes=5) if clock[0]>=due else None)
    checked=[];progress=[];dispatches=[]
    original_enqueue=service.enqueue_collection
    def enqueue(*a,**k):
        dispatches.append(clock[0]);return original_enqueue(*a,**k)
    monkeypatch.setattr(service,'enqueue_collection',enqueue)
    def check(identity):
        # Each source releases its own transaction before the callback. A local
        # malformed check still invokes it and cannot strand the due source.
        with Session(engine()) as db,db.begin():
            assert db.get(Provider,identity)
            clock[0]+=timedelta(seconds=0.75)
            checked.append(identity)
            if len(checked)==9:raise KeyError('synthetic malformed local check')
    monkeypatch.setattr(resilience,'reconcile_source',check)
    worker.schedule_all(progress=lambda:progress.append(clock[0]))
    assert len(checked)==21 and len(progress)>=21
    with Session(engine()) as db:
        queued=list(db.scalars(select(Job).where(Job.kind=='source.collect',Job.payload['source_id'].astext==str(ident))))
        assert db.get(SourceSettings,ident).interval_seconds==60
        if denial is None:
            assert len(queued)==1 and queued[0].payload['scheduled_realtime'] is True
            assert queued[0].available_at==due
            assert due<=dispatches[0]<=due+timedelta(seconds=2.25)
        else:
            assert not queued


@pytest.mark.parametrize('mode',['kpl_counts','kpl_leaders'])
def test_more_than_64_closed_window_archives_cannot_hide_live_packets_and_resume_at_night(client,monkeypatch,mode):
    from app.market import kpl_count_jobs
    ident,clock,_,_=detail_setup(client,monkeypatch)
    kpl_count_jobs.schedule(leaders=mode=='kpl_leaders')
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,ident)
        detail=db.scalar(select(Job).where(Job.payload[mode].astext=='true'))
        detail.available_at=detail.created_at=clock[0];detail_id=detail.id
        for index in range(80):
            payload=dict(source_id=str(ident),day=str(clock[0].date()-timedelta(days=1)))
            if index%2:payload['sector_atlas_backfill']=True
            db.add(Job(owner_id=config.owner_id,kind='direction.archive',payload=payload,
                created_at=clock[0]-timedelta(days=1),available_at=clock[0]-timedelta(days=1)))
    class FrozenSession(Session):
        def scalar(self,statement,*a,**k):
            columns=list(getattr(statement,'selected_columns',()))
            if len(columns)==1 and getattr(columns[0],'name',None)=='clock_timestamp':return clock[0]
            return super().scalar(statement,*a,**k)
    monkeypatch.setattr(worker,'Session',FrozenSession)
    selected=worker.claim('recovery')
    assert selected and selected.id==detail_id
    with Session(engine()) as db:
        old=list(db.scalars(select(Job).where(Job.id!=detail_id)))
        assert len(old)==80 and all(task.state=='queued' and task.attempts==0 for task in old)
    clock[0]=clock[0].replace(hour=20)
    historical=worker.claim('recovery')
    assert historical and historical.id!=detail_id
    assert not historical.payload.get('kpl_counts') and not historical.payload.get('kpl_leaders')
