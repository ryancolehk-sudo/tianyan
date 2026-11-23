"""Explicit collection must not inherit an automatic next-session wake."""
from datetime import UTC,datetime,timedelta
import uuid

import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market.models import SourceSettings,DataFetchRun
from app.market.service import source_id
from app.models import Job
from test_data_sources import configure,configuration,login


def prepared(client,monkeypatch,families):
    headers=login(client)
    clock=datetime.now(UTC).replace(hour=12,minute=0,second=0,microsecond=0)
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr('app.market.service.now',lambda db:clock)
    for family in families:
        interval={'ths_industry_funds':30,'ths_concept_funds':60,'tencent_web':5,'direction_kpl_top':5}.get(family,300)
        configure(client,headers,{**configuration(family),'interval_seconds':interval},key=family)
    return headers,clock


def refresh(client,headers,family):
    return client.post(f'/api/v1/data-sources/{source_id(family)}/refresh',json={},
        headers={**headers,'Idempotency-Key':uuid.uuid4().hex})


def previous_capture(db,family,clock,*,ago=300):
    received=clock-timedelta(seconds=ago)
    db.add(DataFetchRun(source_id=source_id(family),capability='synthetic',started_at=received,
        received_at=received,status='complete',request_count=1))


@pytest.mark.parametrize('family',['ths_industry_funds','tencent_web','direction_kpl_top'])
def test_explicit_refresh_uses_elapsed_real_spacing_instead_of_tomorrow_wake(client,monkeypatch,family):
    headers,clock=prepared(client,monkeypatch,[family])
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(family));config.next_run_at=clock+timedelta(days=1)
        previous_capture(db,family,clock)
    response=refresh(client,headers,family)
    assert response.status_code==202,response.text
    with Session(engine()) as db:
        job=db.scalar(select(Job).where(Job.kind=='source.collect'))
        assert job is not None and not job.payload.get('scheduled_realtime')


def test_explicit_refresh_leaves_future_automatic_job_immutable_and_reuses_its_own_job(client,monkeypatch):
    family='ths_concept_funds';headers,clock=prepared(client,monkeypatch,[family])
    wake=clock+timedelta(days=1)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(family));config.next_run_at=wake
        previous_capture(db,family,clock)
        old=Job(owner_id=config.owner_id,kind='source.collect',payload={'source_id':str(config.source_id),'scheduled_realtime':True},
            available_at=wake,error_code='intraday_window_wait')
        db.add(old);db.flush();old_id=old.id;old_payload=dict(old.payload)
    assert refresh(client,headers,family).status_code==202
    assert refresh(client,headers,family).status_code==202
    with Session(engine()) as db:
        jobs=list(db.scalars(select(Job).where(Job.kind=='source.collect')))
        assert len(jobs)==2
        saved=db.get(Job,old_id)
        assert saved.payload==old_payload and saved.available_at==wake and saved.error_code=='intraday_window_wait'
        assert sum(not job.payload.get('scheduled_realtime') for job in jobs)==1


def test_source_states_show_the_persisted_shared_two_day_ths_deadline(client,monkeypatch):
    families=['ths_industry_funds','ths_concept_funds'];headers,clock=prepared(client,monkeypatch,families)
    deadline=clock+timedelta(days=2)
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(families[0])).legacy_cooldown_until=deadline
    rows=client.get('/api/v1/data-sources').json()['data']['items']
    shared=[row for row in rows if row['family'] in families]
    assert len(shared)==2
    assert all(row['cooldown_until'] is not None and datetime.fromisoformat(row['cooldown_until'])==deadline for row in shared)
    assert all(row['cooldown_scope']=='supplier' for row in shared)


@pytest.mark.parametrize('family',['ths_industry_funds','tencent_web','direction_kpl_top'])
def test_explicit_refresh_still_obeys_the_actual_capture_interval(client,monkeypatch,family):
    headers,clock=prepared(client,monkeypatch,[family])
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(family)).next_run_at=clock+timedelta(days=1)
        previous_capture(db,family,clock,ago=1)
    assert refresh(client,headers,family).status_code==429
    with Session(engine()) as db:assert db.scalar(select(func.count()).select_from(Job))==0


def test_peer_supplier_cooldown_blocks_new_manual_work_but_not_other_vendors(client,monkeypatch):
    families=['ths_industry_funds','ths_concept_funds','tencent_web']
    headers,clock=prepared(client,monkeypatch,families)
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id('ths_industry_funds')).legacy_cooldown_until=clock+timedelta(days=2)
        for family in families:previous_capture(db,family,clock)
    assert refresh(client,headers,'ths_concept_funds').status_code==429
    assert refresh(client,headers,'tencent_web').status_code==202
    with Session(engine()) as db:
        tasks=list(db.scalars(select(Job)))
        assert len(tasks)==1 and tasks[0].payload['source_id']==str(source_id('tencent_web'))


@pytest.mark.parametrize('case',['queued_manual','queued_supplier_wait','running_automatic'])
def test_existing_explicit_supplier_wait_or_running_work_is_reused_without_new_jobs(client,monkeypatch,case):
    family='ths_concept_funds';headers,clock=prepared(client,monkeypatch,[family]);wake=clock+timedelta(days=1)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(family));config.next_run_at=wake
        previous_capture(db,family,clock)
        payload={'source_id':str(config.source_id)}
        if case!='queued_manual':payload['scheduled_realtime']=True
        old=Job(owner_id=config.owner_id,kind='source.collect',payload=payload,available_at=wake,
            error_code='source_budget_wait' if case=='queued_supplier_wait' else 'intraday_window_wait')
        if case=='running_automatic':old.state='running';old.lease_token=uuid.uuid4();old.lease_until=wake
        db.add(old);db.flush();old_id=old.id
    assert refresh(client,headers,family).status_code==202
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job))==1 and db.get(Job,old_id).payload==payload


def test_future_automatic_job_with_different_binding_remains_an_intent_conflict(client,monkeypatch):
    family='ths_concept_funds';headers,clock=prepared(client,monkeypatch,[family])
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(family))
        db.add(Job(owner_id=config.owner_id,kind='source.collect',payload={
            'source_id':str(config.source_id),'scheduled_realtime':True,
            'integration_context':{'connection_id':str(uuid.uuid4()),'source_id':str(config.source_id),'revision':1}},
            available_at=clock+timedelta(days=1),error_code='intraday_window_wait'))
    response=refresh(client,headers,family)
    assert response.status_code==409 and response.json()['error']['code']=='collection_intent_conflict'
    with Session(engine()) as db:assert db.scalar(select(func.count()).select_from(Job))==1


def test_automatic_admission_keeps_its_future_schedule(client,monkeypatch):
    from app.auth import APIError
    from app.market.service import enqueue_collection
    family='ths_industry_funds';headers,clock=prepared(client,monkeypatch,[family])
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(family)).next_run_at=clock+timedelta(days=1)
        previous_capture(db,family,clock)
    with Session(engine()) as db,db.begin(),pytest.raises(APIError) as caught:
        enqueue_collection(db,source_id(family),scheduled_realtime=True)
    assert caught.value.code=='source_rate_limit'
    with Session(engine()) as db:assert db.scalar(select(func.count()).select_from(Job))==0


def test_future_receipt_is_neither_current_spacing_nor_current_source_health(client,monkeypatch):
    family='ths_industry_funds';headers,clock=prepared(client,monkeypatch,[family])
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(family)).next_run_at=clock+timedelta(days=1)
        future=clock+timedelta(hours=1)
        db.add(DataFetchRun(source_id=source_id(family),capability='synthetic',started_at=future,received_at=future,
            status='error',reason='rate_limited',request_count=1,retry_not_before=future+timedelta(hours=1)))
    assert refresh(client,headers,family).status_code==202
    row=next(item for item in client.get('/api/v1/data-sources').json()['data']['items'] if item['family']==family)
    assert row['cooldown_until'] is None and row['last_attempt'] is None and row['last_success_at'] is None


def test_fuyao_long_wait_is_visible_across_quote_and_slow_families(client,monkeypatch):
    from test_fuyao_data import config
    from app.market.budget import family_cooldown_until
    headers=login(client);clock=datetime.now(UTC);deadline=clock+timedelta(days=2)
    monkeypatch.setattr('app.market.service.now',lambda db:clock)
    families=('fuyao_quotes','fuyao_indices','fuyao_financial')
    for family in families:configure(client,headers,config(family),key=family)
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id('fuyao_quotes')).legacy_cooldown_until=deadline
    rows=client.get('/api/v1/data-sources').json()['data']['items']
    shared=[row for row in rows if row['family'] in families]
    assert len(shared)==3 and all(row['cooldown_scope']=='supplier' for row in shared)
    assert all(datetime.fromisoformat(row['cooldown_until'])==deadline for row in shared)
    with Session(engine()) as db:assert family_cooldown_until(db,'fuyao_financial',clock)==deadline
