"""Real PostgreSQL checks for immutable per-scenario correction chains."""
import copy
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func, select, update, delete
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.auth import hasher
from app.cli import bootstrap
from app.db import engine
from app.main import app
from app.models import AuditEvent, User
from app.research.models import PublicationEvent
from app.validations.models import ScenarioValidation
from auction_fixtures import seed

PASSWORD='synthetic-validation-fixture-password'
ORIGIN={'Origin':'http://127.0.0.1:8080'}
PATH='/api/v1/validations'


@pytest.fixture
def workspace(client,monkeypatch):
    monkeypatch.setenv('JL_TEST_PROJECT','jlpm-check-0000000000-dbtest')
    bootstrap('validation-owner',PASSWORD)
    login=client.post('/api/v1/session',json={'username':'validation-owner','password':PASSWORD},headers=ORIGIN).json()['data']
    with Session(engine()) as db,db.begin(): fixture=seed(db,db.get(User,uuid.UUID(login['user']['id'])))
    return {'client':client,'owner':login['user']['id'],'headers':{**ORIGIN,'X-CSRF-Token':login['csrf_token']},**fixture}


def body(w, **changes):
    return {'plan_revision_id':w['revision_id'],'scenario_id':w['scenario_id'],'observation_id':None,
        'snapshot_id':w['validation_snapshot_id'],'outcome':'supported','reason':'合成材料支持人工条件，不是成交或收益',
        'evidence_refs':[w['validation_material_id']],'supersedes_id':None,**changes}


def post(w,value=None,*,key=None,client=None):
    return (client or w['client']).post(PATH,json=value or body(w),headers={**w['headers'],'Idempotency-Key':key or str(uuid.uuid4())})


def test_correction_preserves_original_content_and_cutoff_metadata(workspace):
    w=workspace;first=post(w);assert first.status_code==201,first.text; original=first.json()['data']
    second=post(w,body(w,outcome='refuted',reason='合成更正反证原条件',supersedes_id=original['id']))
    assert second.status_code==201,second.text; corrected=second.json()['data']
    assert corrected['content_hash']!=original['content_hash'] and corrected['supersedes_id']==original['id']
    read=w['client'].get(PATH+'/'+original['id']).json()['data']
    assert read=={**original,'superseded_by_id':corrected['id']}
    assert w['client'].get(PATH,params={'plan_revision_id':w['revision_id'],'as_of_time':original['recorded_at']}).json()['data']['items']==[original]
    assert w['client'].get(PATH,params={'plan_revision_id':w['revision_id'],'as_of_time':w['observed_at']}).json()['data']['items']==[]
    assert post(w,body(w,supersedes_id=original['id'])).status_code==409
    assert post(w).status_code==409
    assert w['client'].patch(PATH+'/'+original['id'],json={}).status_code==405
    assert w['client'].delete(PATH+'/'+original['id']).status_code==405


def test_observation_exact_reference_and_new_plan_does_not_move_old_chain(workspace):
    w=workspace
    obs=w['client'].post('/api/v1/auction-observations',json={'plan_revision_id':w['revision_id'],'scenario_id':w['scenario_id'],
        'snapshot_id':w['snapshot_id'],'observed_at':w['observed_at'],'status':'unavailable','facts':[],'evidence_refs':[],
        'commentary':'合成竞价缺失'},headers={**w['headers'],'Idempotency-Key':str(uuid.uuid4())}).json()['data']
    saved=post(w,body(w,observation_id=obs['id']));assert saved.status_code==201,saved.text
    original=saved.json()['data']
    result=w['client'].post('/api/v1/revisions/'+w['new_head_id']+'/publish',json={},headers={**w['headers'],'If-Match':'"2"','Idempotency-Key':str(uuid.uuid4())})
    assert result.status_code==200
    assert w['client'].get(PATH+'/'+original['id']).json()['data']==original
    assert w['client'].get(PATH,params={'plan_revision_id':w['new_head_id']}).json()['data']['items']==[]
    assert original['observation_id']==obs['id'] and original['scenario_id']==w['scenario_id']


@pytest.mark.parametrize('case,expected',[('no_evidence',422),('empty_reason',422),('wrong_scenario',409),('missing_plan',404),('unknown_snapshot',409),('old_snapshot',422),('auction_snapshot',422),('unknown_observation',409),('wrong_observation',409),('unknown_supersedes',409),('outside_manifest',409),('duplicate_ref',422),('spoof_recorded',422),('spoof_owner',422),('before_close',422),('future_snapshot',409),('unknown_calendar',422),('withdrawn',409),('draft_plan',409)])
def test_invalid_references_and_times_are_atomic(workspace,monkeypatch,case,expected):
    w=workspace;b=copy.deepcopy(body(w))
    if case=='no_evidence':b['evidence_refs']=[]
    elif case=='empty_reason':b['reason']='  '
    elif case=='wrong_scenario':b['scenario_id']=str(uuid.uuid4())
    elif case=='missing_plan':b['plan_revision_id']=str(uuid.uuid4())
    elif case=='unknown_snapshot':b['snapshot_id']=str(uuid.uuid4())
    elif case=='old_snapshot':b['snapshot_id']=w['prior_snapshot_id']
    elif case=='auction_snapshot':b['snapshot_id']=w['snapshot_id']
    elif case=='unknown_observation':b['observation_id']=str(uuid.uuid4())
    elif case=='unknown_supersedes':b['supersedes_id']=str(uuid.uuid4())
    elif case=='outside_manifest':b['evidence_refs']=[str(uuid.uuid4())]
    elif case=='duplicate_ref':b['evidence_refs']*=2
    elif case=='spoof_recorded':b['recorded_at']=w['observed_at']
    elif case=='spoof_owner':b['owner_id']=w['owner']
    elif case in {'before_close','future_snapshot'}:
        from app.validations import service
        monkeypatch.setattr(service,'now',lambda db: datetime(2002,1,2,3 if case=='before_close' else 6,tzinfo=UTC))
    elif case=='unknown_calendar':
        with Session(engine()) as db,db.begin(): other=seed(db,db.get(User,uuid.UUID(w['owner'])),continuous=False)
        b=body(other)
    elif case=='withdrawn':
        with Session(engine()) as db,db.begin():db.add(PublicationEvent(revision_id=uuid.UUID(w['revision_id']),owner_id=uuid.UUID(w['owner']),actor_id=uuid.UUID(w['owner']),kind='withdrawn',recorded_at=datetime.now(UTC),reason='synthetic'))
    elif case=='draft_plan':
        r=w['client'].get('/api/v1/revisions/'+w['new_head_id']).json()['data'];b.update(plan_revision_id=r['id'],scenario_id=r['scenarios'][0]['id'])
    elif case=='wrong_observation':
        with Session(engine()) as db,db.begin(): other=seed(db,db.get(User,uuid.UUID(w['owner'])))
        obs=w['client'].post('/api/v1/auction-observations',json={'plan_revision_id':other['revision_id'],'scenario_id':other['scenario_id'],
            'snapshot_id':other['snapshot_id'],'observed_at':other['observed_at'],'status':'unavailable','facts':[],'evidence_refs':[],'commentary':'合成'},
            headers={**w['headers'],'Idempotency-Key':str(uuid.uuid4())}).json()['data'];b['observation_id']=obs['id']
    result=post(w,b);assert result.status_code==expected,result.text
    if case=='future_snapshot':assert result.json()['error']['code']=='invalid_state'
    with Session(engine()) as db:assert db.scalar(select(func.count()).select_from(ScenarioValidation))==0


def test_inconclusive_retains_reason_and_no_evidence_does_not_mean_supported(workspace):
    w=workspace;result=post(w,body(w,outcome='inconclusive',reason='缺少有权收盘材料，不可判断',evidence_refs=[]))
    assert result.status_code==201,result.text
    assert result.json()['meta']['quality']['status']=='unavailable' and result.json()['data']['evidence_refs']==[]


@pytest.mark.parametrize('case',['future_event','unavailable'])
def test_material_time_and_quality_cannot_be_promoted_to_supported(workspace,case):
    w=workspace
    if case=='future_event':
        # Snapshot membership itself already rejects a future event. Do not
        # disable that trigger just to manufacture an impossible API fixture.
        with pytest.raises(DBAPIError):
            with Session(engine()) as db,db.begin():seed(db,db.get(User,uuid.UUID(w['owner'])),validation_future_event=True)
        with Session(engine()) as db:assert db.scalar(select(func.count()).select_from(ScenarioValidation))==0
        return
    with Session(engine()) as db,db.begin():
        other=seed(db,db.get(User,uuid.UUID(w['owner'])),validation_quality='unavailable')
    result=post(w,body(other))
    assert result.status_code==(422 if case=='future_event' else 409),result.text
    assert result.json()['error']['code']==('late_evidence' if case=='future_event' else 'insufficient_evidence')
    if case=='unavailable':
        missing=post(w,body(other,outcome='inconclusive',reason='合成快照缺失，保留原因',evidence_refs=[]))
        assert missing.status_code==201 and missing.json()['meta']['quality']['status']=='unavailable'


def test_parallel_same_key_and_different_key_corrections(workspace):
    w=workspace
    def save(value,key):
        with TestClient(app,base_url=ORIGIN['Origin']) as c:
            c.cookies.update(w['client'].cookies);return post(w,value,key=key,client=c)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:save(body(w),'same-validation-key'),range(2)))
    assert [r.status_code for r in results]==[201,201] and results[0].json()==results[1].json()
    root=results[0].json()['data'];assert post(w,body(w,reason='changed'),key='same-validation-key').status_code==409
    correction=body(w,outcome='refuted',supersedes_id=root['id'])
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:save(correction,str(uuid.uuid4())),range(2)))
    assert sorted(r.status_code for r in results)==[201,409]
    replay=post(w,key='same-validation-key');assert replay.json()['data']==root
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(ScenarioValidation))==2
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action=='research.validation.create'))==2


def test_chain_cannot_cross_scenario_or_plan(workspace):
    w=workspace;root=post(w).json()['data']
    with Session(engine()) as db,db.begin():other=seed(db,db.get(User,uuid.UUID(w['owner'])))
    assert post(w,body(other,supersedes_id=root['id'])).status_code==409


def test_owner_roles_csrf_and_scope_bound_pagination(workspace):
    w=workspace;rows=[post(w).json()['data']]
    for _ in range(2):rows.append(post(w,body(w,supersedes_id=rows[-1]['id'])).json()['data'])
    assert w['client'].get(PATH).status_code==422
    assert w['client'].get(PATH,params={'plan_revision_id':w['revision_id'],'scenario_id':str(uuid.uuid4())}).status_code==409
    params={'plan_revision_id':w['revision_id'],'limit':1};p=w['client'].get(PATH,params=params).json()['data'];seen=p['items']
    assert p['next_cursor']
    assert w['client'].get(PATH,params={**params,'scenario_id':w['scenario_id'],'cursor':p['next_cursor']}).status_code==422
    assert w['client'].get('/api/v1/auction-observations',params={**params,'cursor':p['next_cursor']}).status_code==422
    while p['next_cursor']:
        p=w['client'].get(PATH,params={**params,'cursor':p['next_cursor']}).json()['data'];seen+=p['items']
    assert {r['id'] for r in seen}=={r['id'] for r in rows}
    assert w['client'].post(PATH,json=body(w),headers={**ORIGIN,'Idempotency-Key':'without-csrf'}).status_code==403
    for role in ['viewer','researcher']:
        with Session(engine()) as db,db.begin():db.add(User(username='other-'+role,password_hash=hasher.hash(PASSWORD),role=role))
        auth=w['client'].post('/api/v1/session',json={'username':'other-'+role,'password':PASSWORD},headers=ORIGIN).json()['data']
        assert w['client'].get(PATH+'/'+rows[0]['id']).status_code==404
        assert w['client'].get(PATH,params={'plan_revision_id':w['revision_id']}).status_code==404
        response=w['client'].post(PATH,json=body(w),headers={**ORIGIN,'X-CSRF-Token':auth['csrf_token'],'Idempotency-Key':str(uuid.uuid4())})
        assert response.status_code==(403 if role=='viewer' else 404)


def test_db_immutability_wrong_chain_and_contract(workspace):
    w=workspace;result=post(w);assert result.status_code==201,result.text;row=result.json()['data']
    for mutation in [update(ScenarioValidation).values(reason='overwrite'),delete(ScenarioValidation)]:
        with pytest.raises(DBAPIError):
            with engine().begin() as db:db.execute(mutation)
    for forged in [{'supersedes_id':None},{'supersedes_id':row['id'],'recorded_at':datetime(2002,1,2,3,tzinfo=UTC)}]:
        with pytest.raises(DBAPIError):
            with Session(engine()) as db,db.begin():
                old=db.get(ScenarioValidation,uuid.UUID(row['id']));values={c.name:getattr(old,c.name) for c in ScenarioValidation.__table__.columns}
                values.update(id=uuid.uuid4(),**forged);db.add(ScenarioValidation(**values));db.flush()
    spec=json.loads((Path(__file__).resolve().parents[2]/'contracts/openapi.p2.json').read_text(encoding='utf-8'))
    schema=spec['paths'][PATH]['post']['responses']['201']['content']['application/json']['schema']
    Draft202012Validator({**spec,**schema},format_checker=FormatChecker()).validate(result.json())
