"""Disposable PostgreSQL continuity, never supplier traffic or live acceptance."""
import json
import uuid
import urllib.error
from datetime import datetime, timedelta, UTC
from email.message import Message
from types import SimpleNamespace

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.db import engine
from app.config import settings
from app.models import Job, Provider, User
from app.market.models import SourceSettings, DataFetchRun
from app.market import direction_archive as archive, sector_atlas_backfill as backfill
from app.market import kpl_atlas_source as source, kpl_atlas_budget as budget
from app.market.collect import store_reference_batch
from app.market.protocol import SourceFailure, Response
from app.evidence.models import EvidenceVersion
from app.read_cache_models import ReadCache
from app.read_cache import cache_scope
from app.market.sector_atlas_repair import required_codes, missing_codes, daily_ranking
from test_direction_archive import setup, DAY, CLOCK


def ranking(clock, count=3):
    rows=[[str(801000+i),'题材'+str(i),100-i,1,0,1000000,*([0]*13)] for i in range(count)]
    return Response(json.dumps(dict(errcode='0',Day=[str(DAY)],Count=count,list=rows)).encode(),clock,1,200)


def leader(clock, partial=False):
    row=['']*70;row[0]='600001';row[1]='合成领涨';row[24]='龙一'
    return Response(json.dumps(dict(errcode='0',Day=[str(DAY)],Count=2000 if partial else 1,list=[row])).encode(),clock,1,200)


def scenario(client,monkeypatch, count=3):
    ident,clock=setup(client,monkeypatch,'direction_kpl_strength')
    for module in (backfill,budget):
        monkeypatch.setattr(module,'now',lambda db:clock[0])
    monkeypatch.setattr(backfill,'available_trade_days',lambda *a,**k:[DAY])
    monkeypatch.setattr('time.sleep',lambda _:None)
    pages=source.Pagination(DAY);pages.add(ranking(clock[0],count))
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,ident);config.paused_reason=None
        store_reference_batch(db,db.get(Provider,ident),config,pages.batch(),clock[0])
    return ident,clock


def claim(ident, clock):
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=db.scalar(select(Job).where(Job.kind=='direction.archive',Job.state=='queued',
            Job.payload['source_id'].astext==str(ident)).order_by(Job.created_at.desc()))
        assert job is not None
        job.created_at=clock
        job.state='running';job.attempts=1;job.lease_token=uuid.uuid4()
        job.lease_until=datetime.now(UTC)+timedelta(hours=1)
        return job


def test_partial_repairs_only_missing_then_stops(client,monkeypatch):
    ident,clock=scenario(client,monkeypatch)
    calls=[]
    def fetch(family,*,day,plate_code=None,**kw):
        assert plate_code is not None,'complete ranking must not be fetched again'
        calls.append(plate_code);clock[0]+=timedelta(seconds=3)
        return leader(clock[0],partial=plate_code=='801000')
    monkeypatch.setattr(source.source,'fetch',fetch)
    backfill.schedule_atlas_backfill();job=claim(ident,clock[0]);archive.process_job(job)
    with Session(engine()) as db:
        assert db.get(Job,job.id).state=='failed'
        assert db.get(Job,job.id).error_code=='leader_details_incomplete'
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert (run.status,run.request_count,run.rejected)==('partial',3,1)
    clock[0]+=timedelta(seconds=61);backfill.schedule_atlas_backfill()
    with Session(engine()) as db:
        assert not db.scalar(select(Job.id).where(Job.state=='queued'))
    clock[0]+=timedelta(minutes=16);backfill.schedule_atlas_backfill()
    repair=claim(ident,clock[0]);assert repair.payload['leader_repair_codes']==['801000']
    def recovered(family,*,day,plate_code=None,**kw):
        calls.append(plate_code);clock[0]+=timedelta(seconds=3)
        return leader(clock[0])
    monkeypatch.setattr(source.source,'fetch',recovered)
    archive.process_job(repair)
    with Session(engine()) as db:
        assert db.get(Job,repair.id).state=='succeeded'
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext==source.DATASET))==1
        assert db.scalar(select(func.sum(DataFetchRun.request_count)))==4
    clock[0]+=timedelta(minutes=16);backfill.schedule_atlas_backfill()
    with Session(engine()) as db:
        assert not db.scalar(select(Job.id).where(Job.state=='queued'))
    assert calls==['801000','801001','801002','801000']


@pytest.mark.parametrize('reason',['rate_limited','permission_missing','configuration_changed','lease_lost','maintenance_wait'])
def test_leader_control_failure_stops_all_further_requests(client,monkeypatch,reason):
    ident,clock=scenario(client,monkeypatch)
    calls=[];permits=[]
    def fetch(family,**kw):
        calls.append(kw)
        if len(calls)==1:return ranking(clock[0])
        raise SourceFailure(reason,retry_after=172800 if reason=='rate_limited' else None)
    monkeypatch.setattr(source.source,'fetch',fetch)
    with pytest.raises(SourceFailure) as caught:
        source.fetch(day=DAY,include_leaders=True,before_request=lambda:permits.append(1) or True)
    assert caught.value.reason==reason and caught.value.request_count==2
    assert len(calls)==len(permits)==2
    if reason=='rate_limited':assert caught.value.retry_after==172800


def test_local_wait_keeps_already_received_dated_leaders_without_new_supplier_cooldown(client,monkeypatch):
    ident,clock=scenario(client,monkeypatch)
    calls=[];permits=[]
    def permit(*args,**kwargs):
        permits.append(1)
        if len(permits)>1:raise budget.local_wait(3)
        return True
    def fetch(*args,**kwargs):
        calls.append(kwargs['plate_code']);clock[0]+=timedelta(seconds=3)
        return leader(clock[0])
    monkeypatch.setattr(budget,'reserve',permit)
    monkeypatch.setattr(source.source,'fetch',fetch)
    backfill.schedule_atlas_backfill();job=claim(ident,clock[0]);archive.process_job(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert calls==['801000'] and run.request_count==run.accepted==1
        assert run.reason=='local_request_wait' and run.retry_not_before is None
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext=='market.direction.kpl_leaders_v1',
            EvidenceVersion.payload['rows'][0]['plate_code'].astext=='801000'))==1
        assert budget.cooldown_until(db,ident,clock[0]) is None


def test_rate_limit_survives_new_jobs_and_shared_live_gate(client,monkeypatch):
    ident,clock=scenario(client,monkeypatch)
    calls=[]
    def limited(*a,**kw):
        calls.append(kw);raise SourceFailure('rate_limited',retry_after=937)
    monkeypatch.setattr(source.source,'fetch',limited)
    backfill.schedule_atlas_backfill();job=claim(ident,clock[0]);archive.process_job(job)
    with Session(engine()) as db:
        assert db.get(Job,job.id).state=='failed'
        assert db.get(Job,job.id).error_code=='rate_limited'
        runs=list(db.scalars(select(DataFetchRun).where(DataFetchRun.source_id==ident)))
        assert sum(r.request_count for r in runs)==1
        assert any(r.capability=='sector.atlas.cooldown' and r.retry_not_before==clock[0]+timedelta(seconds=937) for r in runs)
    # A different ordinary (current) job uses the same persisted gate.
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        current=Job(owner_id=job.owner_id,kind='source.collect',state='running',payload={'source_id':str(ident)},
            lease_token=uuid.uuid4(),lease_until=clock[0]+timedelta(hours=1))
        db.add(current);db.flush()
    with pytest.raises(SourceFailure,match='local_request_wait') as caught:budget.reserve(current)
    assert caught.value.request_count==0
    assert caught.value.retry_after>=937
    with Session(engine()) as db,db.begin():
        db.get(Job,current.id).state='failed'
    clock[0]+=timedelta(minutes=15,seconds=1);backfill.schedule_atlas_backfill()
    with Session(engine()) as db:
        assert not db.scalar(select(Job.id).where(Job.state=='queued'))
    assert len(calls)==1
    clock[0]+=timedelta(seconds=62);backfill.schedule_atlas_backfill()
    with Session(engine()) as db:
        assert db.scalar(select(Job.id).where(Job.state=='queued')) is not None


@pytest.mark.parametrize('header,expected',[('937',937),('99999999',99999999),('172800',172800),('bad',None),('0',0)])
def test_http_retry_after_preserved_without_retry(client,monkeypatch,header,expected):
    headers=Message();headers['Retry-After']=header
    calls=[]
    def open_request(*a,**k):
        calls.append(1);raise urllib.error.HTTPError('https://local.invalid',429,'controlled',headers,None)
    monkeypatch.setattr(source.source.urllib.request,'build_opener',lambda *a:SimpleNamespace(open=open_request))
    with pytest.raises(SourceFailure) as caught:source.source.fetch('direction_kpl_strength',day=DAY)
    assert caught.value.reason=='rate_limited' and caught.value.retry_after==expected
    assert caught.value.request_count==1 and len(calls)==1


@pytest.mark.parametrize('http_date',[False,True])
def test_two_day_supplier_deadline_is_never_shortened(client,monkeypatch,http_date):
    from email.utils import format_datetime
    ident,clock=scenario(client,monkeypatch)
    fixed=clock[0].astimezone(UTC)
    class FixedClock(datetime):
        @classmethod
        def now(cls,tz=None):return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(source.source,'datetime',FixedClock)
    headers=Message();headers['Retry-After']=format_datetime(fixed+timedelta(days=2),usegmt=True) if http_date else '172800'
    calls=[]
    def reject(*a,**kw):
        calls.append(1);raise urllib.error.HTTPError('https://local.invalid',429,'controlled',headers,None)
    monkeypatch.setattr(source.source.urllib.request,'build_opener',lambda *a:SimpleNamespace(open=reject))
    backfill.schedule_atlas_backfill();job=claim(ident,clock[0]);archive.process_job(job)
    with Session(engine()) as db:
        runs=list(db.scalars(select(DataFetchRun).where(DataFetchRun.source_id==ident)))
        assert sum(r.request_count for r in runs)==1
        assert all(r.retry_not_before is None for r in runs)
        assert db.get(SourceSettings,ident).legacy_cooldown_until>=clock[0]+timedelta(days=2)
        assert budget.cooldown_until(db,ident,clock[0])>=clock[0]+timedelta(days=2)
    original_deadline=clock[0]+timedelta(days=2)
    # A newly started ordinary job shares the same long deadline.
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        current=Job(owner_id=job.owner_id,kind='source.collect',state='running',
            payload={'source_id':str(ident)},lease_token=uuid.uuid4(),
            lease_until=clock[0]+timedelta(hours=1))
        db.add(current);db.flush()
    with pytest.raises(SourceFailure,match='local_request_wait') as gate:budget.reserve(current)
    assert gate.value.request_count==0
    assert gate.value.retry_after>=172800
    # A shorter late receipt from another in-flight request cannot reduce it.
    clock[0]+=timedelta(minutes=1)
    budget.record_failure(current,SourceFailure('rate_limited',retry_after=937))
    with Session(engine()) as db,db.begin():
        assert budget.cooldown_until(db,ident,clock[0])>=original_deadline
        db.get(Job,current.id).state='failed'
    clock[0]+=timedelta(days=1,hours=1);backfill.schedule_atlas_backfill()
    with Session(engine()) as db:
        assert not db.scalar(select(Job.id).where(Job.state=='queued'))
    assert len(calls)==1


@pytest.mark.parametrize('reason',['timeout','transport_error'])
def test_repair_transport_retry_is_bounded_and_preserves_last_reason(client,monkeypatch,reason):
    calls=[]
    monkeypatch.setattr('time.sleep',lambda _:None)
    def fail(*a,**k):calls.append(1);raise SourceFailure(reason)
    monkeypatch.setattr(source.source,'fetch',fail)
    with pytest.raises(SourceFailure) as caught:
        source.fetch(day=DAY,before_request=lambda:True,include_leaders=True,
            leader_codes=['801000'],saved_rows=[{'code':'801000'}])
    assert caught.value.reason==reason and caught.value.request_count==2
    assert len(calls)==2


def test_unrepresentable_supplier_wait_pauses_instead_of_shortening(client,monkeypatch):
    ident,clock=scenario(client,monkeypatch)
    headers=Message();headers['Retry-After']='999999999999'
    def reject(*a,**kw):raise urllib.error.HTTPError('https://local.invalid',429,'controlled',headers,None)
    monkeypatch.setattr(source.source.urllib.request,'build_opener',lambda *a:SimpleNamespace(open=reject))
    backfill.schedule_atlas_backfill();job=claim(ident,clock[0]);archive.process_job(job)
    with Session(engine()) as db:
        assert db.get(SourceSettings,ident).paused_reason=='retry_after_out_of_range'
        assert db.get(Job,job.id).state=='failed'
    clock[0]+=timedelta(days=2);backfill.schedule_atlas_backfill()
    with Session(engine()) as db:assert not db.scalar(select(Job.id).where(Job.state=='queued'))


def test_scope_selected_unranked_and_full_target_survive_cache_eviction(client,monkeypatch):
    ident,clock=scenario(client,monkeypatch,count=25)
    monkeypatch.setattr(settings(),'read_cache_enabled',True)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,ident);owner=db.get(User,config.owner_id);scope=cache_scope(db,owner)
        for code,cache_scope_value,end,owner_id in [('801020',scope,str(DAY),owner.id),
                ('801021','revoked',str(DAY),owner.id),('801022',scope,'2027-01-01',owner.id),
                ('801023',scope,str(DAY),uuid.uuid4())]:
            db.add(ReadCache(key=code,scope=cache_scope_value,owner_id=owner_id,operation='review-sector-atlas',
                params={'source':'kpl','trade_day':end,'lookback':20,'selected_codes':code},
                dependency_keys=[],accessed_at=clock[0]))
    backfill.schedule_atlas_backfill();job=claim(ident,clock[0])
    assert '801020' in job.payload['leader_required_codes']
    assert not set(job.payload['leader_required_codes']) & {'801021','801022','801023'}
    assert len(job.payload['leader_required_codes'])==11 and len(job.payload['leader_repair_codes'])==10
    calls=[]
    def partial(*a,**kw):
        calls.append(kw['plate_code']);clock[0]+=timedelta(seconds=3);return leader(clock[0],partial=True)
    monkeypatch.setattr(source.source,'fetch',partial);archive.process_job(job)
    with Session(engine()) as db,db.begin():
        for row in db.scalars(select(ReadCache)):db.delete(row)
    clock[0]+=timedelta(minutes=16);backfill.schedule_atlas_backfill();second=claim(ident,clock[0])
    assert len(second.payload['leader_required_codes'])==11
    remaining=set(second.payload['leader_required_codes'])-set(job.payload['leader_repair_codes'])
    assert remaining and second.payload['leader_repair_codes'][0] in remaining
    assert len(calls)==10


def test_ths_partial_is_saved_but_never_full_coverage(client,monkeypatch):
    from app.market import sector_atlas as atlas
    monkeypatch.setattr(atlas,'_context',lambda *a:({},[DAY],DAY))
    daily=SimpleNamespace(id='daily',received_at=CLOCK,payload={'rows':[
        {'ts_code':'885001.TI','trade_date':DAY.strftime('%Y%m%d'),'pct_change':'0'}]})
    directory=SimpleNamespace(id='dir',received_at=CLOCK,payload={'rows':[
        {'ts_code':'885001.TI','name':'一','type':'N','exchange':'A'},
        {'ts_code':'885002.TI','name':'二','type':'N','exchange':'A'}]})
    monkeypatch.setattr(atlas,'_tables',lambda *a,**k:{DAY.strftime('%Y%m%d'):daily})
    monkeypatch.setattr(atlas,'_directory',lambda *a:directory)
    with Session(engine()) as db:result=atlas.sector_atlas(db,DAY,20,'ths')
    coverage=result['coverage']
    assert coverage['ranking_days']==1 and coverage['full_days']==0
    assert coverage['partial_days']==1 and coverage['missing_catalog_observations']==1
    assert result['days'][0]['top_ten_complete'] is False


def test_ordinary_collector_saves_ranks_without_spending_detail_requests(client,monkeypatch):
    from test_direction_recovery import setup as live_setup, fixture
    from app.market.service import schedule_sources, source_id
    from app.worker import claim as worker_claim
    from app.market.collect import collect_job
    current=live_setup(client,monkeypatch,'direction_kpl_strength')
    monkeypatch.setattr(budget,'now',lambda db:current[0])
    monkeypatch.setattr('time.sleep',lambda _:None)
    schedule_sources();job=worker_claim();assert job
    calls=[]
    def fetch(family,**kw):
        calls.append(kw);current[0]+=timedelta(seconds=3)
        if len(calls)==1:return Response(json.dumps(fixture(family,current[0])).encode(),current[0],1,200)
        pytest.fail('Main ranking must not request counts or leaders inline')
    monkeypatch.setattr(source.source,'fetch',fetch)
    collect_job(job)
    with Session(engine()) as db:
        runs=list(db.scalars(select(DataFetchRun).where(DataFetchRun.source_id==source_id('direction_kpl_strength'))))
        assert sum(r.request_count for r in runs)==1
        assert all(r.reason is None for r in runs)
        saved=db.get(Job,job.id)
        assert saved.error_code is None and saved.state=='succeeded'
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.source_id==source_id('direction_kpl_strength')))==2
    assert len(calls)==1


def test_old_descriptor_spends_zero_then_audited_upgrade_allows_requests(client,monkeypatch):
    from app.market.registry import SOURCES
    from app.evidence.common import normalized,content_hash
    from app.evidence.models import SourceVersion
    from app.schemas import Provider as ProviderDTO
    from app.models import AuditEvent
    ident,clock=scenario(client,monkeypatch)
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        config=db.get(SourceSettings,ident);db.get(Provider,ident).adapter_version='prior-direction-parser'
        job=Job(owner_id=config.owner_id,kind='source.collect',state='running',
            payload={'source_id':str(ident)},lease_token=uuid.uuid4(),lease_until=clock[0]+timedelta(hours=1))
        db.add(job);db.flush()
    calls=[]
    def fetch(*a,**kw):calls.append(kw);return leader(clock[0])
    monkeypatch.setattr(source.source,'fetch',fetch)
    def execute():return source.fetch(day=DAY,before_request=lambda:budget.reserve(job),
        include_leaders=True,leader_codes=['801000'],saved_rows=[{'code':'801000'}])
    with pytest.raises(SourceFailure,match='adapter_version_mismatch') as caught:execute()
    assert caught.value.request_count==0 and calls==[]
    # Same audited descriptor operation used by prepare; no supplier call or
    # pause/rights change is used to bypass the required version transition.
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,ident);provider.adapter_version=SOURCES[provider.family].version
        descriptor=normalized(ProviderDTO.model_validate(provider))
        db.add(SourceVersion(source_id=ident,recorded_at=clock[0],descriptor=descriptor,content_hash=content_hash(descriptor)))
        db.add(AuditEvent(actor_id=job.owner_id,action='source.atlas_upgrade',resource_type='provider',
            resource_id=ident,changed_field_names=['adapter_version'],created_at=clock[0]))
    response,batch=execute()
    assert response.request_count==1 and batch.rejected==0 and len(calls)==1
