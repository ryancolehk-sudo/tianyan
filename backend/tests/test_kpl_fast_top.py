"""Offline PostgreSQL: fast KPL observations retain their own coverage and guards."""
import json
import uuid
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import kpl_top
from app.market.collect import collect_job, store_reference_batch
from app.market.kpl_atlas_budget import cooldown_until, reserve, ACTION
from app.market.models import SourceSettings, DataFetchRun
from app.market.protocol import Response, SourceFailure, SHANGHAI
from app.market.service import source_id, schedule_sources, configure_source
from app.market.schemas import SourceConfiguration
from app.models import AuditEvent, Job, Provider, User
from app.worker import claim
from test_data_sources import configure, configuration
from test_direction_recovery import setup, AT


def body(day=AT.date(), *, count=241, size=30):
    return json.dumps(dict(errcode='0',Day=[str(day)],Count=count,
        list=[[str(801000+i),'合成题材'+str(i),1000-i,*([0]*16)] for i in range(size)])).encode()


def prepare(client,monkeypatch):
    clock=setup(client,monkeypatch,kpl_top.FAMILY)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(kpl_top.FAMILY))
        config.paused_reason=None;config.interval_seconds=10
    monkeypatch.setattr('app.market.kpl_atlas_budget.now',lambda db:clock[0])
    return clock


def test_single_page_never_claims_full_universe_or_native_market_clock():
    batch=kpl_top.parse(body(),AT,expected_day=AT.date())
    assert batch.requests==batch.expected==1 and len(batch.records)==1
    payload=batch.records[0][1]
    assert payload.dataset==kpl_top.DATASET and len(payload.rows)==10
    assert all(row['coverage']=='first_page' and row['source_time'] is None for row in payload.rows)
    assert payload.rows[0]['total_count']=='241'
    assert batch.records[0][0]!='market.direction.kpl_atlas_v1'


@pytest.mark.parametrize('change,accepted',[('tail_only',True),('head_swap',False),('tail_exceeds_tenth',False)])
def test_native_first_ten_stay_strict_without_requiring_unused_tail_order(change,accepted):
    value=json.loads(body())
    if change=='tail_only':value['list'][20],value['list'][21]=value['list'][21],value['list'][20]
    elif change=='head_swap':value['list'][2],value['list'][3]=value['list'][3],value['list'][2]
    else:value['list'][20][2]=value['list'][9][2]+1
    if accepted:
        payload=kpl_top.parse(json.dumps(value).encode(),AT,expected_day=AT.date()).records[0][1]
        assert [row['code'] for row in payload.rows]==[str(801000+i) for i in range(10)]
        assert [row['strength'] for row in payload.rows]==[str(1000-i) for i in range(10)]
    else:
        with pytest.raises(SourceFailure,match='schema_changed') as failed:
            kpl_top.parse(json.dumps(value).encode(),AT,expected_day=AT.date())
        diagnostic=failed.value.direction_diagnostic['top_strength_order']
        assert diagnostic['head_descending']==(change!='head_swap')
        assert diagnostic['head_dominates_tail']==(change!='tail_exceeds_tenth')


@pytest.mark.parametrize('change',['old_day','duplicate','reversed','short_page','bad_count'])
def test_bad_or_stale_first_pages_are_rejected(change):
    value=json.loads(body())
    if change=='old_day':value['Day']=['2026-10-23']
    elif change=='duplicate':value['list'][1][0]=value['list'][0][0]
    elif change=='reversed':value['list'].reverse()
    elif change=='short_page':value['list']=value['list'][:10]
    else:value['Count']=401
    with pytest.raises(SourceFailure):kpl_top.parse(json.dumps(value).encode(),AT,expected_day=AT.date())


def test_cutoff_source_day_and_receipt_scope_cannot_be_relabelled():
    payload=kpl_top.parse(body(),AT,expected_day=AT.date()).records[0][1].model_dump(mode='json')
    record=SimpleNamespace(received_at=AT,payload=payload)
    assert kpl_top.project([record],AT.date(),AT)['rows']
    assert not kpl_top.project([record],AT.date(),AT-timedelta(microseconds=1))['rows']
    later=SimpleNamespace(received_at=AT+timedelta(days=1),payload=payload)
    assert not kpl_top.project([later],AT.date(),later.received_at)['rows']
    record.received_at=AT+timedelta(seconds=1)
    assert not kpl_top.project([record],AT.date(),record.received_at)['rows']


def test_fast_slot_can_read_top_while_full_pages_run_but_quote_only_stays_separate(client,monkeypatch):
    clock=prepare(client,monkeypatch)
    due=datetime.now(UTC)-timedelta(seconds=60)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(kpl_top.FAMILY))
        full=Job(owner_id=config.owner_id,kind='source.collect',state='running',lease_token=uuid.uuid4(),
            lease_until=datetime.now(UTC)+timedelta(minutes=5),payload={'source_id':str(source_id('direction_kpl_strength'))})
        top=Job(owner_id=config.owner_id,kind='source.collect',state='queued',available_at=due,
            payload={'source_id':str(config.source_id)})
        slow=Job(owner_id=config.owner_id,kind='source.collect',state='queued',available_at=due,
            payload={'source_id':str(source_id('ths_concept_funds'))})
        db.add_all([full,top,slow]);db.flush();expected=top.id
    assert claim('realtime',quotes_only=True) is None
    assert claim('realtime',fast_only=True).id==expected
    assert claim('realtime',fast_only=True) is None
    with pytest.raises(ValueError):claim('general',fast_only=True)


def test_automatic_top_uses_one_http_its_own_clock_and_no_full_dataset(client,monkeypatch):
    clock=prepare(client,monkeypatch);calls=[]
    def fetch(family,**kwargs):
        assert family=='direction_kpl_strength' and not kwargs
        calls.append(family);clock[0]+=timedelta(seconds=1)
        return Response(body(),clock[0],1000,200)
    monkeypatch.setattr('app.market.direction_sources.fetch',fetch)
    schedule_sources(families={kpl_top.FAMILY})
    task=claim('realtime',fast_only=True)
    assert task is not None
    collect_job(task)
    with Session(engine()) as db:
        evidence=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id(kpl_top.FAMILY))))
        assert len(evidence)==1 and evidence[0].payload['dataset']==kpl_top.DATASET
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==task.id))
        assert run.status=='complete' and run.request_count==1 and calls==['direction_kpl_strength']
        assert db.get(SourceSettings,run.source_id).next_run_at==AT+timedelta(seconds=10)
        assert len(kpl_top.read(db,AT.date(),clock[0])['history'])==1


def test_shared_physical_spacing_and_longest_supplier_cooldown_cover_new_family(client,monkeypatch):
    clock=prepare(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(kpl_top.FAMILY))
        primary=db.get(Provider,config.source_id)
        for family,seconds in [('direction_kpl_strength',900),('direction_kpl_emotion',172800)]:
            provider=Provider(id=source_id(family),name='合成来源',kind='market',family=family,adapter_version='synthetic',
                enabled=False,entitlement_status='unknown',rights={})
            db.add(provider);db.flush()
            db.add(SourceSettings(source_id=provider.id,owner_id=config.owner_id,interval_seconds=60,config={},
                rights_basis='Synthetic only',next_run_at=clock[0],configured_at=clock[0],
                legacy_cooldown_until=clock[0]+timedelta(seconds=seconds)))
        task=Job(owner_id=config.owner_id,kind='source.collect',state='running',lease_token=uuid.uuid4(),
            lease_until=datetime.now(UTC)+timedelta(days=1),payload={'source_id':str(primary.id)})
        db.add(task);db.flush();db.expunge(task)
    with Session(engine()) as db:
        assert cooldown_until(db,source_id(kpl_top.FAMILY),clock[0])==clock[0]+timedelta(days=2)
    with pytest.raises(SourceFailure,match='local_request_wait') as waiting:reserve(task)
    assert waiting.value.request_count==0 and waiting.value.retry_after==172800
    with Session(engine()) as db,db.begin():
        for config in db.scalars(select(SourceSettings)):config.legacy_cooldown_until=None
        db.add(AuditEvent(action=ACTION,created_at=clock[0],resource_type='job',resource_id=task.id,changed_field_names=[]))
    sleeps=[]
    def advance(seconds):sleeps.append(seconds);clock[0]+=timedelta(seconds=seconds)
    monkeypatch.setattr('app.market.kpl_atlas_budget.time.sleep',advance)
    assert reserve(task)
    assert sleeps and sum(sleeps)>=2


@pytest.mark.parametrize('boundary',['lunch','permission'])
def test_after_permit_controls_prevent_http_and_record_no_fake_request(client,monkeypatch,boundary):
    clock=prepare(client,monkeypatch)
    schedule_sources(families={kpl_top.FAMILY});task=claim('realtime',fast_only=True)
    def permitted(*args,**kwargs):
        if boundary=='lunch':clock[0]=clock[0].replace(hour=12,minute=0)
        else:
            with Session(engine()) as db,db.begin():db.get(Provider,source_id(kpl_top.FAMILY)).enabled=False
        return True
    monkeypatch.setattr('app.market.kpl_atlas_budget.reserve',permitted)
    monkeypatch.setattr('app.market.direction_sources.fetch',lambda *a,**k:pytest.fail('Control refusal must not fetch'))
    collect_job(task)
    with Session(engine()) as db:
        assert not list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id(kpl_top.FAMILY))))
        runs=list(db.scalars(select(DataFetchRun).where(DataFetchRun.job_id==task.id)))
        assert sum(run.request_count for run in runs)==0


def test_top_api_and_detail_priority_keep_full_batch_and_time_independent(client,monkeypatch):
    from app.market import kpl_count_jobs as counts
    from test_kpl_count_jobs import prepare as prepare_counts
    from test_direction_archive import DAY
    main,clock,_=prepare_counts(client,monkeypatch,24,clock_at=datetime.fromisoformat(str(DAY)+'T10:00:00+08:00'))
    full_stamp=clock[0]
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,main);before=config.next_run_at
        owner=db.get(User,config.owner_id)
        configure_source(db,owner,SourceConfiguration.model_validate({**configuration(kpl_top.FAMILY),'interval_seconds':10}),str(uuid.uuid4()))
        clock[0]+=timedelta(seconds=1)
        value=json.loads(body(day=DAY,count=24,size=24))
        value['list']=[value['list'][-1],*value['list'][:-1]]
        for index,row in enumerate(value['list']):row[2]=1000-index
        parsed=kpl_top.parse(json.dumps(value).encode(),clock[0],expected_day=DAY)
        store_reference_batch(db,db.get(Provider,source_id(kpl_top.FAMILY)),db.get(SourceSettings,source_id(kpl_top.FAMILY)),parsed,clock[0])
    top=client.get('/api/v1/kpl-strength',params={'trade_day':str(DAY),'top_only':'true'}).json()['data']
    full=client.get('/api/v1/kpl-strength',params={'trade_day':str(DAY)}).json()['data']
    assert len(top['rows'])==10 and top['rows'][0]['code']=='801023' and top['coverage']=='first_page'
    assert len(full['rows'])==24 and full['coverage']=='complete_pages'
    assert datetime.fromisoformat(full['received_at'])==full_stamp
    assert datetime.fromisoformat(top['received_at'])==clock[0]
    counts.schedule();counts.schedule(leaders=True)
    with Session(engine()) as db:
        for flag in ('kpl_counts','kpl_leaders'):
            task=db.scalar(select(Job).where(Job.payload[flag].astext=='true'))
            assert task.payload['codes'][0]['code']=='801023'
            assert task.payload['source_id']==str(main)
        assert db.get(SourceSettings,main).next_run_at==before
    with Session(engine()) as db,db.begin():db.get(Provider,source_id(kpl_top.FAMILY)).rights={'display':'denied','cache':'denied'}
    assert client.get('/api/v1/kpl-strength?top_only=true').json()['data']['state']=='disabled'
    assert len(client.get('/api/v1/kpl-strength',params={'trade_day':str(DAY)}).json()['data']['rows'])==24


def test_old_direction_batch_never_grants_or_enqueues_the_new_fast_source(client,monkeypatch):
    from test_direction_data import setup as legacy_setup
    from app.config import settings
    from pydantic import SecretStr
    headers=legacy_setup(client,monkeypatch)
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr(settings(),'tushare_token',SecretStr('synthetic-old-batch-only'))
    response=client.post('/api/v1/direction-data/collections',json={'trade_day':'2026-10-23'},
        headers={**headers,'Idempotency-Key':'synthetic-no-fast-implicit'})
    assert response.status_code==202,response.text
    with Session(engine()) as db:
        assert db.get(SourceSettings,source_id(kpl_top.FAMILY)) is None
        assert not list(db.scalars(select(Job.id).where(Job.payload['source_id'].astext==str(source_id(kpl_top.FAMILY)))))
