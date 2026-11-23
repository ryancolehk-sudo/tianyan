"""Synthetic start/receipt clocks and offline PG; never a supplier timing promise."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import APIError
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collection_cadence import successful_next, run_anchor
from app.market.collect import collect_job
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response, SHANGHAI
from app.market.service import enqueue_collection, source_id
from app.models import Job, Provider
from test_direction_recovery import setup, AT


@pytest.mark.parametrize('family,interval,seconds', [
    ('direction_kpl_strength',60,60), ('direction_kpl_strength',300,300),
    ('direction_kpl_strength',30,60), ('tushare_market_rt',60,60),
    ('tushare_market_rt',300,300), ('tushare_market_rt',30,30),
    ('tushare_market_rt',3,15), ('fuyao_quotes',15,15),
    ('ths_industry_funds',30,30), ('ths_concept_funds',30,60)])
def test_success_uses_first_request_and_never_reduces_configured_or_physical_floor(family,interval,seconds):
    received=AT+timedelta(seconds=20)
    assert successful_next(family,dict(scheduled_realtime=True),AT,received,'complete',9,interval)==max(received,AT+timedelta(seconds=seconds))
    late=AT+timedelta(seconds=75)
    assert successful_next(family,dict(scheduled_realtime=True),AT,late,'partial',9,60)==late


@pytest.mark.parametrize('payload,status,requests', [
    ({},'complete',1), ({'scheduled_realtime':True,'schema_probe':True},'complete',1),
    ({'scheduled_realtime':True,'integration_context':{'probe':True}},'complete',1),
    ({'scheduled_realtime':True,'minute_context':{}},'complete',1),
    ({'scheduled_realtime':True},'error',1), ({'scheduled_realtime':True},'unavailable',1),
    ({'scheduled_realtime':True},'complete',0)])
def test_other_intents_failures_and_zero_request_runs_keep_original_schedule(payload,status,requests):
    assert successful_next('direction_kpl_strength',payload,AT,AT,'complete' if status=='complete' else status,requests,60) is None
    assert successful_next('direction_kpl_emotion',dict(scheduled_realtime=True),AT,AT,'complete',1,60) is None


@pytest.mark.parametrize('family,interval', [
    ('direction_kpl_strength',60), ('direction_kpl_strength',180),
    ('tushare_market_rt',60), ('tushare_market_rt',180), ('tushare_market_rt',15)])
def test_collector_enqueue_wake_and_final_permit_share_actual_start(client,monkeypatch,family,interval):
    from app.config import settings
    from pydantic import SecretStr
    monkeypatch.setattr(settings(),'tushare_token',SecretStr('synthetic-offline-cadence-test'))
    clock=setup(client,monkeypatch,family)
    ident=source_id(family)
    with Session(engine()) as db,db.begin():
        cfg=db.get(SourceSettings,ident);cfg.paused_reason=None;cfg.interval_seconds=interval
    monkeypatch.setattr('time.sleep',lambda seconds:clock.__setitem__(0,clock[0]+timedelta(seconds=seconds)))
    starts=[];calls=[]
    def permit(*a,**k):
        if not starts:
            clock[0]+=timedelta(seconds=3)  # Waiting for a real shared permit is not request time.
            starts.append(clock[0])
        return True
    monkeypatch.setattr('app.market.kpl_atlas_budget.reserve',permit)
    monkeypatch.setattr('app.market.collect.reserve_tushare_request',permit)
    def ranking(source,**kw):
        assert 'plate_code' not in kw
        offset=kw['index'];calls.append(offset);clock[0]+=timedelta(seconds=2)
        rows=[[str(801000+i),'合成题材'+str(i),1000-i,*([0]*16)] for i in range(offset,min(offset+30,241))]
        return Response(json.dumps(dict(errcode='0',Day=[str(clock[0].date())],Count=241,list=rows)).encode(),clock[0],1,200)
    def market(*a,**k):
        from app.market.tushare_market import encoded
        from test_tushare_market import quote
        calls.append('market');clock[0]+=timedelta(seconds=9)
        row=quote(at=clock[0]);return Response(encoded(list(row),[row]),clock[0],9000,200)
    monkeypatch.setattr('app.market.direction_sources.fetch',ranking)
    monkeypatch.setattr('app.market.collect.request_tushare',market)
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=enqueue_collection(db,ident,scheduled_realtime=True)
        job.state='running';job.attempts=1;job.lease_token=uuid.uuid4();job.lease_until=datetime.now(UTC)+timedelta(hours=1)
    collect_job(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert run.status=='complete' and run.started_at==starts[0]
        assert run.received_at>run.started_at
        due=run.started_at+timedelta(seconds=interval)
        assert db.get(SourceSettings,ident).next_run_at==due
        assert db.get(SourceSettings,ident).interval_seconds==interval
        assert run_anchor(db,family,run)==starts[0]
        evidence=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==ident)))
        assert len(evidence)==(2 if family=='direction_kpl_strength' else 1)
        assert all(row.received_at==run.received_at for row in evidence)
        if family=='direction_kpl_strength':
            assert calls==list(range(0,241,30)) and run.request_count==9 and run.expected==2
            full=next(row for row in evidence if row.payload['dataset'].endswith('kpl_atlas_v1'))
            assert len(full.payload['rows'])==241
            assert all(datetime.fromisoformat(row['page_received_at'])<=run.received_at for row in full.payload['rows'])
    clock[0]=due-timedelta(microseconds=1)
    with Session(engine()) as db,db.begin():
        with pytest.raises(APIError):enqueue_collection(db,ident,scheduled_realtime=True)
    clock[0]=due
    from app.market.resilience import wake
    with Session(engine()) as db,db.begin():
        cfg=db.get(SourceSettings,ident);cfg.next_run_at=due+timedelta(hours=1)
        wake(db,db.get(Provider,ident),cfg,due)
        assert cfg.next_run_at==due
    # Manual refresh retains receipt + interval even when an auto sample used start + interval.
    with Session(engine()) as db,db.begin():
        with pytest.raises(APIError):enqueue_collection(db,ident)
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        next_job=enqueue_collection(db,ident,scheduled_realtime=True)
        assert next_job.available_at==due
        next_job.state='running';next_job.lease_token=uuid.uuid4();next_job.lease_until=datetime.now(UTC)+timedelta(hours=1)
    from app.market.realtime_schedule import permit_realtime
    assert permit_realtime(next_job)
    clock[0]=due.replace(hour=15,minute=5)
    assert not permit_realtime(next_job)


def test_old_unmarked_success_cannot_claim_a_physical_start(client,monkeypatch):
    setup(client,monkeypatch,'direction_kpl_strength')
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id('direction_kpl_strength'))
        job=Job(owner_id=config.owner_id,kind='source.collect',
            payload={'source_id':str(config.source_id),'scheduled_realtime':True},state='succeeded')
        db.add(job);db.flush()
        run=DataFetchRun(source_id=config.source_id,job_id=job.id,capability='market.direction',
            started_at=AT-timedelta(seconds=20),received_at=AT,status='complete',request_count=9)
        assert run_anchor(db,'direction_kpl_strength',run) is None
        for invalid in ('not-a-clock', AT.isoformat()):
            job.result_ref={'sample_request_started_at':invalid}
            assert run_anchor(db,'direction_kpl_strength',run) is None
        job.result_ref={'sample_request_started_at':run.started_at.isoformat()}
        assert run_anchor(db,'direction_kpl_strength',run)==run.started_at
        job.payload={**job.payload,'source_id':str(uuid.uuid4())}
        assert run_anchor(db,'direction_kpl_strength',run) is None


def test_faster_live_feed_reserves_its_real_remaining_daily_capacity(client,monkeypatch):
    from types import SimpleNamespace
    from app.market.budget import protect_live_budget
    setup(client,monkeypatch,'tushare_market_rt')
    monkeypatch.setattr('app.market.session.market_session',lambda *a:SimpleNamespace(is_trading_day=True))
    clock=AT.replace(hour=14,minute=55,second=0,microsecond=0)
    history=SimpleNamespace(kind='source.collect',payload={'daily_history':{}})
    live=SimpleNamespace(kind='source.collect',payload={'scheduled_realtime':True})
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id('tushare_market_rt'))
        config.paused_reason=None
        config.interval_seconds=15
        # Twenty 15-second trading polls and five slower close receipts remain.
        assert protect_live_budget(db,history,clock,25,0,{'tushare_market_rt'})==clock.replace(hour=15,minute=5)
        assert protect_live_budget(db,live,clock,35,0,{'tushare_market_rt'}) is None
        config.interval_seconds=60
        assert protect_live_budget(db,history,clock,35,0,{'tushare_market_rt'}) is None
