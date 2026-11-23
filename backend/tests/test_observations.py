"""Real PostgreSQL adversarial checks; all data are guarded synthetic fixtures."""
import copy
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.auth import hasher
from app.cli import bootstrap
from app.db import engine
from app.main import app
from app.models import AuditEvent, User
from app.observations.models import AuctionObservation
from app.research.models import PublicationEvent
from auction_fixtures import seed

PASSWORD='synthetic-observation-fixture-password'
ORIGIN={'Origin':'http://127.0.0.1:8080'}
PATH='/api/v1/auction-observations'


@pytest.fixture
def observation_workspace(client, monkeypatch):
    monkeypatch.setenv('JL_TEST_PROJECT','jlpm-check-0000000000-dbtest')
    bootstrap('observation-owner',PASSWORD)
    login=client.post('/api/v1/session',json={'username':'observation-owner','password':PASSWORD},headers=ORIGIN).json()['data']
    with Session(engine()) as db, db.begin():
        owner=db.get(User,uuid.UUID(login['user']['id'])); fixture=seed(db,owner)
    return {'client':client,'owner':login['user']['id'],'headers':{**ORIGIN,'X-CSRF-Token':login['csrf_token']},**fixture}


def body(w, *, unavailable=False):
    return {'plan_revision_id':w['revision_id'],'scenario_id':w['scenario_id'],'snapshot_id':w['snapshot_id'],
        'observed_at':w['observed_at'],'status':'unavailable' if unavailable else 'observed',
        'facts':[] if unavailable else [{'field':'合成事实','value':'隔离测试 <script>不执行</script>','unit':'文字','evidence_ref':w['material_id']}],
        'commentary':'合成材料缺失，无法判断' if unavailable else '这是人工解释，不是原始行情',
        'evidence_refs':[] if unavailable else [w['material_id']]}


def post(w,value=None,key=None,client=None):
    return (client or w['client']).post(PATH,json=value or body(w),headers={**w['headers'],'Idempotency-Key':key or str(uuid.uuid4())})


def test_observation_fixed_old_plan_read_only_and_late_recorded_cutoff(observation_workspace):
    w=observation_workspace; value=body(w); value['observed_at']='2002-01-02T09:25:00+08:00'; result=post(w,value)
    assert result.status_code==201,result.text
    row=result.json()['data']; assert row['phase']=='09:25' and row['timeliness']=='late'
    assert row['plan_revision_id']==w['revision_id'] and row['scenario_id']==w['scenario_id']
    assert result.json()['meta']['quality']['status']=='partial'
    # Publish P2 after P1 observation. Its scenarios cannot replace P1's identity.
    publish=w['client'].post('/api/v1/revisions/'+w['new_head_id']+'/publish',json={},headers={**w['headers'],'If-Match':'"2"','Idempotency-Key':str(uuid.uuid4())})
    assert publish.status_code==200,publish.text
    assert w['client'].get(PATH+'/'+row['id']).json()['data']==row
    assert w['client'].get(PATH,params={'plan_revision_id':w['new_head_id']}).json()['data']['items']==[]
    old=w['client'].get(PATH,params={'plan_revision_id':w['revision_id'],'as_of_time':w['observed_at']}).json()
    assert old['data']['items']==[] and old['meta']['quality']['status']=='empty'
    assert w['client'].get(PATH,params={'plan_revision_id':w['revision_id']}).json()['data']['items']==[row]
    assert w['client'].patch(PATH+'/'+row['id'],json={}).status_code==405
    assert w['client'].delete(PATH+'/'+row['id']).status_code==405


def test_concurrent_idempotent_save_and_payload_conflict(observation_workspace):
    w=observation_workspace
    def save(_):
        with TestClient(app,base_url=ORIGIN['Origin']) as client:
            client.cookies.update(w['client'].cookies);return post(w,key='synthetic-same-observation',client=client)
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(save,range(2)))
    assert [r.status_code for r in results]==[201,201]
    assert results[0].json()==results[1].json()
    changed=body(w);changed['commentary']='different'
    assert post(w,changed,key='synthetic-same-observation').status_code==409
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuctionObservation))==1
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action=='auction.observation.create'))==1


@pytest.mark.parametrize('case,expected',[('wrong_day',422),('seconds',422),('outside_phase',422),('before_publication',409),('future',422),('plan_snapshot',422),('foreign_scenario',409),('unknown_snapshot',409),('late_evidence',422),('outside_manifest',409),('fact_ref',422),('unavailable_facts',422),('unavailable_no_reason',422),('empty_observed',422),('spoof',422),('naive',422)])
def test_time_reference_and_payload_rejections(observation_workspace,case,expected):
    w=observation_workspace;b=copy.deepcopy(body(w))
    if case=='wrong_day': b['observed_at']='2002-01-03T09:25:00+08:00'
    elif case=='seconds': b['observed_at']='2002-01-02T09:25:01+08:00'
    elif case=='outside_phase': b['observed_at']='2002-01-02T09:30:00+08:00'
    elif case=='before_publication': b['observed_at']='2001-12-31T09:25:00+08:00'
    elif case=='future':
        # Keep valid target/time but set test-only service clock before it.
        from app.observations import service
        original=service.now; service.now=lambda db:datetime(2002,1,2,1,20,tzinfo=UTC)
    elif case=='plan_snapshot': b['snapshot_id']=w['prior_snapshot_id']
    elif case=='foreign_scenario': b['scenario_id']=str(uuid.uuid4())
    elif case=='unknown_snapshot': b['snapshot_id']=str(uuid.uuid4())
    elif case=='late_evidence': b['snapshot_id']=w['late_snapshot_id'];b['evidence_refs']=[w['late_material_id']];b['facts'][0]['evidence_ref']=w['late_material_id']
    elif case=='outside_manifest': b['evidence_refs']=[w['late_material_id']];b['facts'][0]['evidence_ref']=w['late_material_id']
    elif case=='fact_ref': b['facts'][0]['evidence_ref']=str(uuid.uuid4())
    elif case=='unavailable_facts': b['status']='unavailable'
    elif case=='unavailable_no_reason': b=body(w,unavailable=True);b['commentary']='   '
    elif case=='empty_observed': b['facts']=[]
    elif case=='spoof': b['recorded_at']=w['observed_at']
    elif case=='naive': b['observed_at']='2002-01-02T09:25:00'
    try: assert post(w,b).status_code==expected
    finally:
        if case=='future': service.now=original
    with Session(engine()) as db: assert db.scalar(select(func.count()).select_from(AuctionObservation))==0


def test_calendar_missing_auction_and_withdrawn_plan_rejected(observation_workspace):
    w=observation_workspace
    with Session(engine()) as db,db.begin():
        other=seed(db,db.get(User,uuid.UUID(w['owner'])),auction=False)
    assert post({**w,**other}).status_code==422
    with Session(engine()) as db,db.begin():
        db.add(PublicationEvent(revision_id=uuid.UUID(w['revision_id']),owner_id=uuid.UUID(w['owner']),actor_id=uuid.UUID(w['owner']),kind='withdrawn',recorded_at=datetime.now(UTC),reason='synthetic fixture'))
    assert post(w).status_code==409


def test_unavailable_is_explicit_missing_not_supported(observation_workspace):
    w=observation_workspace; result=post(w,body(w,unavailable=True));assert result.status_code==201,result.text
    row=result.json()['data'];assert row['facts']==[] and row['evidence_refs']==[]
    assert result.json()['meta']['quality']['status']=='unavailable'
    assert w['client'].get(PATH+'/'+row['id']).json()['meta']['quality']['status']=='unavailable'


def test_read_write_owner_role_csrf_and_pagination_scope(observation_workspace):
    w=observation_workspace; rows=[post(w).json()['data'] for _ in range(3)]
    assert w['client'].get(PATH).status_code==422
    assert w['client'].get(PATH,params={'plan_revision_id':w['revision_id'],'scenario_id':str(uuid.uuid4())}).status_code==409
    page=w['client'].get(PATH,params={'plan_revision_id':w['revision_id'],'limit':1}).json()['data']
    assert len(page['items'])==1 and page['next_cursor']
    assert w['client'].get(PATH,params={'plan_revision_id':w['revision_id'],'scenario_id':w['scenario_id'],'cursor':page['next_cursor']}).status_code==422
    assert w['client'].get(PATH,params={'plan_revision_id':w['new_head_id'],'cursor':page['next_cursor']}).status_code==422
    assert w['client'].get('/api/v1/evidence',params={'cursor':page['next_cursor']}).status_code==422
    seen=list(page['items']);cursor=page['next_cursor']
    while cursor:
        page=w['client'].get(PATH,params={'plan_revision_id':w['revision_id'],'cursor':cursor,'limit':1}).json()['data'];seen+=page['items'];cursor=page['next_cursor']
    assert {r['id'] for r in seen}=={r['id'] for r in rows}
    assert w['client'].post(PATH,json=body(w),headers={**ORIGIN,'Idempotency-Key':'no-csrf-test'}).status_code==403
    for role in ['viewer','researcher']:
        with Session(engine()) as db,db.begin():db.add(User(username='other-'+role,password_hash=hasher.hash(PASSWORD),role=role))
        login=w['client'].post('/api/v1/session',json={'username':'other-'+role,'password':PASSWORD},headers=ORIGIN).json()['data']
        assert w['client'].get(PATH+'/'+rows[0]['id']).status_code==404
        assert w['client'].get(PATH,params={'plan_revision_id':w['revision_id']}).status_code==404
        result=w['client'].post(PATH,json=body(w),headers={**ORIGIN,'X-CSRF-Token':login['csrf_token'],'Idempotency-Key':str(uuid.uuid4())})
        assert result.status_code==(403 if role=='viewer' else 404)


def test_database_mutation_and_forged_time_rejected_and_contract_roundtrip(observation_workspace):
    w=observation_workspace;result=post(w);assert result.status_code==201,result.text
    row=result.json()['data']
    with pytest.raises(DBAPIError):
        with engine().begin() as db:db.execute(update(AuctionObservation).values(commentary='overwrite'))
    with pytest.raises(DBAPIError):
        with Session(engine()) as db,db.begin():
            forged=db.get(AuctionObservation,uuid.UUID(row['id']))
            values={column.name:getattr(forged,column.name) for column in AuctionObservation.__table__.columns}
            values.update(id=uuid.uuid4(),phase='09:20')
            db.add(AuctionObservation(**values));db.flush()
    spec=json.loads((Path(__file__).resolve().parents[2]/'contracts/openapi.p2.json').read_text(encoding='utf-8'))
    schema=spec['paths'][PATH]['post']['responses']['201']['content']['application/json']['schema']
    Draft202012Validator({**spec,**schema},format_checker=FormatChecker()).validate(result.json())


def test_target_date_discovery_includes_old_source_across_closure(observation_workspace):
    w=observation_workspace
    response=w['client'].get('/api/v1/research-documents',params={'kind':'plan','target_trade_day':w['target_day']})
    assert [r['id'] for r in response.json()['data']['items']]==[w['plan_id']]
    assert w['client'].get('/api/v1/research-documents',params={'kind':'plan','target_trade_day':'2002-01-01'}).json()['data']['items']==[]
