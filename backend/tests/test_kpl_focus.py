"""Isolated PG: actual selected KPL identities, ordered hints and fair packets."""
from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import kpl_count_jobs as jobs
from app.market.kpl_focus import LIFETIME, focused_codes
from app.market.models import DataFetchRun, SourceSettings
from app.models import Job, Provider, User
from app.read_cache_models import ReadCache
from test_direction_archive import DAY
from test_kpl_detail_cadence import setup


def prepare(client, monkeypatch):
    ident, clock, reserved, calls = setup(client, monkeypatch)
    monkeypatch.setattr('app.market.kpl_focus.now', lambda db: clock[0])
    session = client.get('/api/v1/session').json()['data']
    headers = {'Origin':'http://127.0.0.1:8080','X-CSRF-Token':session['csrf_token']}
    with Session(engine()) as db:
        owner = db.get(SourceSettings, ident).owner_id
    return ident, owner, clock, headers


def note(client, headers, **changes):
    body = dict(context_id=str(uuid.uuid4()), sequence=1, trade_day=str(DAY), codes=['801023'])
    return client.put('/api/v1/kpl-focus', json={**body, **changes}, headers=headers)


def test_authenticated_owner_interest_never_collects_or_changes_market_evidence(client, monkeypatch):
    assert client.put('/api/v1/kpl-focus',json={}).status_code == 401
    ident, owner, clock, headers = prepare(client, monkeypatch)
    assert note(client,{}).status_code == 403
    with Session(engine()) as db:
        before = db.scalar(select(func.count()).select_from(EvidenceVersion))
    result = note(client,headers).json()['data']
    assert result == dict(active=True,accepted_count=1,expires_at=(clock[0]+LIFETIME).isoformat())
    with Session(engine()) as db:
        assert focused_codes(db,owner,clock[0]) == ['801023']
        assert focused_codes(db,uuid.uuid4(),clock[0]) == []
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 0
        assert db.scalar(select(func.count()).select_from(EvidenceVersion)) == before
        row = db.scalar(select(ReadCache).where(ReadCache.operation=='kpl-focus'))
        assert not row.dirty and row.dependency_keys == [] and row.owner_id == owner


@pytest.mark.parametrize('changes',[
    {'codes':['801023']*11},{'codes':['BK0001.DC']},{'codes':['000001.SZ']},
    {'sequence':0},{'sequence':2_147_483_648},
])
def test_interest_request_is_bounded_and_uses_native_board_identity(client,monkeypatch,changes):
    _,_,_,headers=prepare(client,monkeypatch)
    assert note(client,headers,**changes).status_code==422


def test_unknown_identity_never_creates_an_external_repair_scope(client,monkeypatch):
    _,owner,clock,headers=prepare(client,monkeypatch)
    result=note(client,headers,codes=['999999','801023','801023']).json()['data']
    assert result['accepted_count']==1
    with Session(engine()) as db:
        assert focused_codes(db,owner,clock[0])==['801023']


def test_current_day_older_rank_can_prioritize_known_details_without_claiming_fresh_strength(client,monkeypatch):
    _,owner,clock,headers=prepare(client,monkeypatch)
    clock[0]+=timedelta(minutes=11)
    assert note(client,headers).json()['data']['accepted_count']==1
    with Session(engine()) as db:assert focused_codes(db,owner,clock[0])==['801023']
    jobs.schedule()
    with Session(engine()) as db:
        task=db.scalar(select(Job))
        assert task.payload['codes'][0]['code']=='801023'


def test_future_received_board_list_cannot_supply_current_priority_identities(client,monkeypatch):
    from copy import deepcopy
    from app.evidence.schemas import ReferencePayload
    from app.market.collect import store_reference_batch
    from app.market.kpl_atlas_source import DATASET
    from app.market.reference import ReferenceBatch
    ident,owner,clock,headers=prepare(client,monkeypatch);context=str(uuid.uuid4())
    with Session(engine()) as db,db.begin():
        original=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==ident,
            EvidenceVersion.payload['dataset'].astext==DATASET))
        payload=deepcopy(original.payload);payload['rows'][-1].update(code='999999',name='合成尚未接收题材')
        store_reference_batch(db,db.get(Provider,ident),db.get(SourceSettings,ident),
            ReferenceBatch(records=[(DATASET,ReferencePayload.model_validate(payload),None)],expected=1,requests=0),
            clock[0]+timedelta(seconds=30))
    assert note(client,headers,context_id=context,codes=['999999']).json()['data']['accepted_count']==0
    with Session(engine()) as db:assert focused_codes(db,owner,clock[0])==[]
    clock[0]+=timedelta(seconds=31)
    assert note(client,headers,context_id=context,sequence=2,codes=['999999']).json()['data']['accepted_count']==1


def test_delayed_older_request_cannot_restore_deselected_or_historical_interest(client,monkeypatch):
    _,owner,clock,headers=prepare(client,monkeypatch);context=str(uuid.uuid4())
    assert note(client,headers,context_id=context,sequence=5).json()['data']['accepted_count']==1
    assert note(client,headers,context_id=context,sequence=7,codes=[]).json()['data']['accepted_count']==0
    clock[0]+=timedelta(seconds=2)
    assert note(client,headers,context_id=context,sequence=6).json()['data']['accepted_count']==0
    with Session(engine()) as db:
        assert focused_codes(db,owner,clock[0])==[]
        cleared=db.scalar(select(ReadCache).where(ReadCache.operation=='kpl-focus'))
        assert cleared.payload['sequence']==7 and cleared.focused_at==clock[0]-timedelta(seconds=2)
    assert note(client,headers,context_id=context,sequence=8).json()['data']['accepted_count']==1
    assert not note(client,headers,context_id=context,sequence=9,as_of_time=clock[0].isoformat()).json()['data']['active']
    with Session(engine()) as db:assert focused_codes(db,owner,clock[0])==[]
    assert note(client,headers,context_id=context,sequence=10).json()['data']['accepted_count']==1
    note(client,headers,context_id=context,sequence=9,trade_day=str(DAY-timedelta(days=1)))
    with Session(engine()) as db:
        assert focused_codes(db,owner,clock[0])==['801023']
        assert focused_codes(db,owner,clock[0]+LIFETIME+timedelta(seconds=1))==[]
        assert focused_codes(db,owner,clock[0]-timedelta(seconds=1))==[]


def test_context_limits_allow_clear_renewal_and_only_ten_aggregate_priorities(client,monkeypatch):
    _,owner,clock,headers=prepare(client,monkeypatch);context=str(uuid.uuid4())
    assert note(client,headers,context_id=context,codes=['801000']).status_code==200
    for index in range(1,16):
        clock[0]+=timedelta(seconds=1)
        assert note(client,headers,codes=[str(801000+index)]).status_code==200
    assert note(client,headers).status_code==429
    assert note(client,headers,context_id=context,sequence=2).status_code==200
    with Session(engine()) as db:assert len(focused_codes(db,owner,clock[0]))==10
    assert note(client,headers,context_id=context,sequence=3,codes=[]).status_code==200
    assert note(client,headers).status_code==200


@pytest.mark.parametrize('change',['rights','owner','paused'])
def test_ineligible_source_clears_own_hint_and_cannot_collect(client,monkeypatch,change):
    ident,owner,clock,headers=prepare(client,monkeypatch);context=str(uuid.uuid4())
    note(client,headers,context_id=context)
    with Session(engine()) as db,db.begin():
        if change=='rights':db.get(Provider,ident).rights={'display':'denied','cache':'denied'}
        elif change=='owner':
            other=User(username='other-synthetic-owner',password_hash=db.get(User,owner).password_hash,role='admin',enabled=True)
            db.add(other);db.flush();db.get(SourceSettings,ident).owner_id=other.id
        else:db.get(SourceSettings,ident).paused_reason='credentials_invalid'
    result=note(client,headers,context_id=context,sequence=2).json()['data']
    assert not result['active'] and result['accepted_count']==0
    with Session(engine()) as db:assert focused_codes(db,owner,clock[0])==[]
    jobs.schedule();jobs.schedule(leaders=True)
    with Session(engine()) as db:
        tasks=list(db.scalars(select(Job)))
        if change=='owner':
            assert len(tasks)==2 and all(task.owner_id!=owner for task in tasks)
            assert all('801023' not in [row['code'] for row in task.payload['codes']] for task in tasks)
        else:assert not tasks


def test_hints_are_not_projection_work_even_when_dirty_and_due(client,monkeypatch):
    from app.read_cache import run_one
    _,_,_,headers=prepare(client,monkeypatch);note(client,headers)
    with Session(engine()) as db,db.begin():
        row=db.scalar(select(ReadCache).where(ReadCache.operation=='kpl-focus'))
        row.dirty=True;row.accessed_at=datetime.now(UTC);row.refresh_after=datetime.now(UTC)-timedelta(seconds=1)
    with Session(engine()) as db:assert run_one(db) is False
    with Session(engine()) as db:
        row=db.scalar(select(ReadCache).where(ReadCache.operation=='kpl-focus'))
        assert row is not None and row.dirty and row.last_error is None


@pytest.mark.parametrize('leaders',[False,True])
def test_selected_board_outside_top_ten_is_prioritized_without_stopping_rotation(client,monkeypatch,leaders):
    ident,owner,clock,headers=prepare(client,monkeypatch)
    assert note(client,headers,codes=['801023','801022']).json()['data']['accepted_count']==2
    with Session(engine()) as db:before=db.get(SourceSettings,ident).next_run_at
    jobs.schedule(leaders=leaders)
    with Session(engine()) as db:
        task=db.scalar(select(Job))
        codes=[row['code'] for row in task.payload['codes']]
        assert codes[:2]==['801023','801022']
        assert len(codes)==(3 if leaders else 8) and len(set(codes))==len(codes)
        assert codes[2]=='801000'
        assert db.get(SourceSettings,ident).next_run_at==before
        assert task.payload['source_id']==str(ident)


def test_selected_error_cooldown_and_expired_hints_keep_original_fair_scope(client,monkeypatch):
    ident,owner,clock,headers=prepare(client,monkeypatch);note(client,headers)
    with Session(engine()) as db,db.begin():
        db.add(DataFetchRun(source_id=ident,capability='sector.leaders:801023',started_at=clock[0],
            received_at=clock[0],status='error',reason='timeout',request_count=1))
    jobs.schedule(leaders=True)
    with Session(engine()) as db,db.begin():
        task=db.scalar(select(Job));assert '801023' not in [row['code'] for row in task.payload['codes']]
        task.state='succeeded'
    clock[0]+=LIFETIME+timedelta(seconds=1);jobs.schedule()
    with Session(engine()) as db:
        task=db.scalar(select(Job).where(Job.state=='queued'))
        assert [row['code'] for row in task.payload['codes']]==[str(801000+i) for i in range(8)]


@pytest.mark.parametrize('leaders',[False,True])
def test_slow_packets_advance_all_priority_codes_and_the_other_class(client,monkeypatch,leaders):
    from app.market.collect import store_reference_batch
    from app.market.reference import ReferenceBatch
    from app.market.kpl_leaders import parse as leader_payload,DATASET as LEADERS
    from app.market.kpl_limit_counts import batch as count_batch
    from test_kpl_detail_cadence import leader_response
    from test_kpl_count_jobs import response as count_response
    ident,owner,clock,headers=prepare(client,monkeypatch);context=str(uuid.uuid4())
    focused=[str(801000+i) for i in range(14,24)]
    seen=set()
    for turn in range(10 if leaders else 5):
        assert note(client,headers,context_id=context,sequence=turn+1,codes=focused).status_code==200
        jobs.schedule(leaders=leaders)
        with Session(engine()) as db,db.begin():
            task=db.scalar(select(Job).where(Job.state=='queued'))
            assert task is not None
            codes=[row['code'] for row in task.payload['codes']];seen.update(codes)
            assert len([code for code in codes if code in focused])==2
            assert len([code for code in codes if code not in focused])==(1 if leaders else 6)
            provider=db.get(Provider,ident);config=db.get(SourceSettings,ident)
            for row in task.payload['codes']:
                if leaders:
                    payload=leader_payload(leader_response(clock[0]),row['code'],DAY)
                    batch=ReferenceBatch(records=[(LEADERS+':'+row['code'],payload,None)],expected=1,requests=0)
                else:
                    batch=count_batch(count_response(clock[0],0),DAY,row['code'],row['name'])
                store_reference_batch(db,provider,config,batch,clock[0])
            task.state='succeeded';task.completed_at=clock[0]
        # Even if every next packet starts after the freshness threshold,
        # unobserved selected boards and the other class must still advance.
        clock[0]+=timedelta(seconds=61)
    expected=set(focused+[str(801000+i) for i in range(10 if leaders else 24)])
    assert seen==expected
