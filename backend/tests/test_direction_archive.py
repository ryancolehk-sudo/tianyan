"""Historical direction jobs never reopen live polling or invent a full ranking."""
import json
import uuid
from datetime import datetime,date,timedelta

import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session

from app.db import engine
from app.config import settings
from app.market import direction_archive as archive
from app.market.collect import store_reference_batch
from app.market.direction_sources import parse
from app.market.direction_read import snapshot_panel
from app.market.models import SourceSettings,DataFetchRun
from app.market.protocol import Response,SourceFailure
from app.market.service import source_id
from app.models import Provider,Job,AuditEvent
from app.evidence.models import EvidenceVersion
from test_data_sources import configure,login

DAY=date(2026,10,26);PREVIOUS=date(2026,10,23)
CLOCK=datetime.fromisoformat('2026-10-26T16:00:00+08:00')


def setup(client,monkeypatch,family='direction_em_hot'):
    headers=login(client)
    configure(client,headers,dict(family=family,symbols=[],interval_seconds=300,enabled=True,
        entitlement_status='verified',rights_basis='Synthetic personal ranking test.',
        rights={'display':'allowed','cache':'allowed','export':'denied','redistribute':'denied'}))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    clock=[CLOCK]
    monkeypatch.setattr(archive,'now',lambda db:clock[0])
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls,tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(archive,'datetime',FrozenDateTime)
    monkeypatch.setattr(archive,'available_trade_days',lambda *a,**k:[PREVIOUS,DAY])
    ident=source_id(family)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,ident);config.paused_reason='schema_changed'
        if family=='direction_em_hot':
            rows=[{'sc':f'SH{600001+i}','rk':i+1} for i in range(10)]
            batch=parse(family,json.dumps({'code':0,'data':rows}).encode(),CLOCK)
            store_reference_batch(db,db.get(Provider,ident),config,batch,CLOCK)
    return ident,clock


def running(ident,clock=CLOCK):
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=db.scalar(select(Job).where(Job.kind=='direction.archive',Job.payload['source_id'].astext==str(ident)))
        job.state='running';job.attempts=1;job.lease_token=uuid.uuid4()
        job.lease_until=max(clock+timedelta(hours=8),datetime.now(CLOCK.tzinfo)+timedelta(minutes=10))
        return job


def test_full_historical_ranks_publish_daily_panel_without_unpausing_live(client,monkeypatch):
    ident,clock=setup(client,monkeypatch)
    archive.schedule_direction_archives();archive.schedule_direction_archives()
    job=running(ident)
    monkeypatch.setattr('time.sleep',lambda _:None)
    def fetch(code):
        rank=int(code[2:])-600000;clock[0]+=timedelta(seconds=3)
        body=json.dumps({'code':0,'data':[{'calcTime':str(PREVIOUS),'rank':rank+2},
            {'calcTime':str(DAY),'rank':rank}]}).encode()
        return Response(body,clock[0],1,200)
    monkeypatch.setattr('app.market.em_rank_history.fetch',fetch)
    archive.process_job(job)
    with Session(engine()) as db:
        assert db.get(Job,job.id).state=='succeeded'
        assert db.get(SourceSettings,ident).paused_reason=='schema_changed'
        data=snapshot_panel(db,'direction_em_hot','em_hot','Synthetic history',DAY,None)
        assert data['status']=='ready' and data['rank_change_basis']=='previous_trading_day'
        assert data['rank_comparison_day']==str(PREVIOUS) and all(r['rank_change']==2 for r in data['rows'])
        assert data['source_time'] is None
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext=='market.direction.em_rank_history'))==10
    clock[0]+=timedelta(minutes=16);archive.schedule_direction_archives()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='direction.archive'))==1


def test_historical_source_error_keeps_safe_diagnostic_and_old_settings(client,monkeypatch):
    ident,clock=setup(client,monkeypatch,'direction_kpl_strength')
    archive.schedule_direction_archives();job=running(ident)
    def unavailable(*, day, before_request, include_leaders=False, leader_codes=None, on_failure=None):
        assert before_request()
        error=SourceFailure('upstream_unavailable')
        error.request_count=1
        error.direction_diagnostic={'version':1,'source_error_code':'1020','message_category':'parameter_error'}
        raise error
    monkeypatch.setattr('app.market.kpl_atlas_source.fetch',unavailable)
    archive.process_job(job)
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.state=='failed' and saved.error_code=='upstream_unavailable'
        assert saved.result_ref['direction_diagnostic']['source_error_code']=='1020'
        assert db.get(SourceSettings,ident).paused_reason=='schema_changed'
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert run.request_count==1 and run.status=='error'


@pytest.mark.parametrize('clock', ['13:30:00','21:30:00'])
def test_outside_archive_window_never_issues_supplier_request(client,monkeypatch,clock):
    ident,_=setup(client,monkeypatch)
    archive.schedule_direction_archives();job=running(ident)
    monkeypatch.setattr(archive,'now',lambda db:datetime.fromisoformat('2026-10-26T'+clock+'+08:00'))
    monkeypatch.setattr('app.market.em_rank_history.fetch',lambda *a:pytest.fail('outside archive window'))
    archive.process_job(job)
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.state=='queued' and saved.attempts==0
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action==archive.REQUEST_ACTION))


def test_maintenance_pause_returns_attempt_without_request(client,monkeypatch):
    ident,_=setup(client,monkeypatch);archive.schedule_direction_archives();job=running(ident)
    monkeypatch.setattr('app.maintenance_control.paused',lambda db:True)
    monkeypatch.setattr('app.market.em_rank_history.fetch',lambda *a:pytest.fail('maintenance pause'))
    archive.process_job(job)
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.state=='queued' and saved.attempts==0 and saved.error_code=='maintenance_wait'


def test_next_morning_can_repair_the_previous_closed_day_without_current_endpoint(client,monkeypatch):
    ident,clock=setup(client,monkeypatch)
    clock[0]+=timedelta(days=1);clock[0]=clock[0].replace(hour=9,minute=0)
    archive.schedule_direction_archives()
    with Session(engine()) as db:
        job=db.scalar(select(Job).where(Job.kind=='direction.archive'))
        assert job is not None and job.payload['day']=='2026-10-26'
        assert not db.scalar(select(DataFetchRun.id))


def test_unpublished_historical_day_stops_after_three_attempts(client,monkeypatch):
    ident,clock=setup(client,monkeypatch,'direction_kpl_strength')
    with Session(engine()) as db,db.begin():
        owner=db.get(SourceSettings,ident).owner_id
        for offset in range(3):
            db.add(Job(owner_id=owner,kind='direction.archive',state='failed',
                payload={'source_id':str(ident),'day':str(DAY)},
                completed_at=CLOCK-timedelta(hours=1,minutes=offset),error_code='upstream_unavailable'))
    archive.schedule_direction_archives()
    clock[0]+=timedelta(minutes=16);archive.schedule_direction_archives()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='direction.archive'))==3
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==0
    # Reserve exactly one recovery on the next proved open preparation window.
    clock[0]=CLOCK.replace(day=27,hour=9,minute=0)
    archive.schedule_direction_archives()
    clock[0]+=timedelta(minutes=14);archive.schedule_direction_archives()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='direction.archive'))==4


def test_historical_recovery_on_a_closed_current_date_keeps_the_original_target(client,monkeypatch):
    ident,clock=setup(client,monkeypatch)
    # The target is still the fixture's verified closed trading day. The host
    # may restart on a later Saturday without changing that evidence date.
    clock[0]=CLOCK.replace(day=31,hour=16)
    archive.schedule_direction_archives()
    job=running(ident,clock[0])
    assert job.payload['day']==str(DAY)
    permitted=archive._permit(job,reserve=False)
    assert permitted is not None and permitted[2]==DAY
    with Session(engine()) as db:assert not db.scalar(select(DataFetchRun.id))


def test_historical_recovery_still_rejects_an_unverified_target_date(client,monkeypatch):
    ident,_=setup(client,monkeypatch)
    archive.schedule_direction_archives();job=running(ident)
    monkeypatch.setattr(archive,'available_trade_days',lambda *a,**k:[])
    with pytest.raises(SourceFailure) as failure:
        archive._permit(job,reserve=False)
    assert failure.value.reason=='calendar_not_ready'
    with Session(engine()) as db:assert not db.scalar(select(DataFetchRun.id))


def test_incomplete_global_ranks_preserve_original_history_but_do_not_publish_top_ten(client,monkeypatch):
    ident,clock=setup(client,monkeypatch)
    archive.schedule_direction_archives();job=running(ident)
    monkeypatch.setattr('time.sleep',lambda _:None)
    def fetch(code):
        clock[0]+=timedelta(seconds=3)
        return Response(json.dumps({'code':0,'data':[{'calcTime':str(DAY),
            'rank':int(code[2:])-599999}]}).encode(),clock[0],1,200)
    monkeypatch.setattr('app.market.em_rank_history.fetch',fetch)
    archive.process_job(job)
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.error_code=='historical_top_ten_incomplete'
        assert saved.result_ref['known_source_ranks']==list(range(2,12))
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext=='market.direction.em_rank_history'))==10
        assert not db.scalar(select(EvidenceVersion.id).where(
            EvidenceVersion.payload['dataset'].astext=='market.direction.em_hot_daily'))


def test_live_failure_diagnostic_is_persisted_without_raw_values(client,monkeypatch):
    from app.market.collect import collect_job
    from datetime import UTC
    ident,_=setup(client,monkeypatch)
    secret='SYNTHETIC-DO-NOT-STORE-RAW'
    raw=json.dumps({'code':0,'data':[{'sc':secret,'rk':1}],'untrusted_extra':secret}).encode()
    monkeypatch.setattr('app.market.direction_sources.fetch',lambda *a,**k:Response(raw,datetime.now(UTC),1,200))
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        config=db.get(SourceSettings,ident);config.paused_reason=None
        job=Job(owner_id=config.owner_id,kind='source.collect',state='running',attempts=1,
            lease_token=uuid.uuid4(),lease_until=datetime.now(CLOCK.tzinfo)+timedelta(minutes=10),payload={'source_id':str(ident)})
        db.add(job);db.flush()
    collect_job(job)
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        diagnostic=saved.result_ref['direction_diagnostic']
        assert diagnostic['version']==1 and diagnostic['checkpoint']=='em_identity'
        assert secret not in json.dumps(saved.result_ref)
        assert db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id)).body_hash is not None
