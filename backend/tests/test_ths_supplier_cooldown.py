"""THS supplier Retry-After must never be silently shortened by local policy."""
from datetime import UTC,datetime,timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from urllib.error import HTTPError
import uuid

import pytest

from app.market import ths_funds
from app.market.protocol import SourceFailure


def test_explicit_manual_execution_does_not_turn_into_an_automatic_session_wait(client,monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market.models import SourceSettings,DataFetchRun
    from app.market.service import source_id
    from app.models import Job
    from app.worker import claim,process
    from test_explicit_realtime_refresh import prepared,previous_capture,refresh
    from test_ths_funds_unit import page
    family='ths_industry_funds';headers,clock=prepared(client,monkeypatch,[family]);calls=[]
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(family));config.next_run_at=clock+timedelta(days=1)
        previous_capture(db,family,clock)
        old=Job(owner_id=config.owner_id,kind='source.collect',payload={'source_id':str(config.source_id),'scheduled_realtime':True},
            available_at=config.next_run_at,error_code='intraday_window_wait')
        db.add(old);db.flush();old_id=old.id;wake=old.available_at;payload=dict(old.payload)
    def request(*args):calls.append(args);return page(),datetime.now(UTC)
    monkeypatch.setattr('app.market.ths_funds.request',request)
    assert refresh(client,headers,family).status_code==202
    task=claim()
    assert task is not None and task.id!=old_id and not task.payload.get('scheduled_realtime')
    process(task)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==task.id))
        assert len(calls)==1 and run.request_count==1 and run.status=='complete'
        saved=db.get(Job,old_id)
        assert saved.payload==payload and saved.available_at==wake and saved.state=='queued'

STAMP=datetime(2026,11,12,5,tzinfo=UTC)


@pytest.mark.parametrize('status',[429,503])
@pytest.mark.parametrize('header',['172800',format_datetime(STAMP+timedelta(days=2),usegmt=True)])
def test_transport_preserves_supplier_two_day_wait_in_seconds_and_http_date(monkeypatch,header,status):
    failure=HTTPError('https://data.10jqka.com.cn/',status,'Synthetic limit',{'Retry-After':header},None)
    def request(*args,**kwargs):raise failure
    monkeypatch.setattr(ths_funds.urllib.request,'build_opener',lambda *args:SimpleNamespace(open=request))
    monkeypatch.setattr(ths_funds,'datetime',SimpleNamespace(now=lambda *args:STAMP))
    with pytest.raises(SourceFailure) as caught:ths_funds.request('hyzjl',1,'synthetic')
    assert caught.value.reason==('rate_limited' if status==429 else 'upstream_unavailable')
    assert caught.value.retry_after==172800


def test_http_date_compatibility_and_malformed_values_do_not_invent_supplier_limits():
    from app.market.ths_funds_cooldown import MAX_RETRY_AFTER,retry_after
    for value in ('Sat, 14 Nov 2026 05:00:00 GMT','Saturday, 14-Nov-26 05:00:00 GMT','Sat Nov 14 05:00:00 2026'):
        assert retry_after(value,STAMP)==172800
    assert retry_after('0',STAMP)==0
    assert retry_after(str(MAX_RETRY_AFTER),STAMP)==MAX_RETRY_AFTER
    for value in (None,'','-1','garbage','١٢','14 Nov 2026 05:00:00'):
        assert retry_after(value,STAMP) is None
    for value in ('99999999999',format_datetime(STAMP+timedelta(days=4000),usegmt=True)):
        with pytest.raises(SourceFailure,match='retry_after_out_of_range'):retry_after(value,STAMP)


@pytest.mark.parametrize('reason,delay',[('rate_limited',172800),('upstream_unavailable',172800),('retry_after_out_of_range',None)])
def test_supplier_wait_or_unrepresentable_hint_stops_the_page_set_without_extra_requests(monkeypatch,reason,delay):
    calls=[]
    def request(*args):calls.append(args);raise SourceFailure(reason,retry_after=delay)
    monkeypatch.setattr(ths_funds,'request',request)
    with pytest.raises(SourceFailure) as caught:ths_funds.fetch('ths_industry_funds',lambda:True)
    assert len(calls)==caught.value.request_count==1


def prepared(client,monkeypatch):
    from app.config import settings
    from test_data_sources import login,configure,configuration
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    headers=login(client)
    for family in ('ths_industry_funds','ths_concept_funds'):
        configure(client,headers,configuration(family),key=family)


def task(db,family,clock):
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.models import SourceSettings
    from app.market.service import source_id
    from app.models import Job,Provider
    provider=db.get(Provider,source_id(family));config=db.get(SourceSettings,provider.id)
    row=Job(owner_id=config.owner_id,kind='source.collect',state='running',attempts=1,
        lease_token=uuid.uuid4(),lease_until=clock+timedelta(days=1),payload={'source_id':str(provider.id)})
    db.add(row);db.flush();fingerprint=configuration_fingerprint(provider,config);db.expunge(row)
    return row,fingerprint


@pytest.mark.parametrize('reason',['rate_limited','upstream_unavailable'])
def test_long_wait_is_durable_shared_and_survives_expired_request_lease(client,monkeypatch,reason):
    from sqlalchemy import func,select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market import ths_funds_cooldown as budget
    from app.market.models import DataFetchRun
    from app.models import AuditEvent,Job
    prepared(client,monkeypatch);clock=datetime.now(UTC)
    monkeypatch.setattr(budget,'now',lambda db:clock)
    with Session(engine()) as db,db.begin():
        origin,_=task(db,'ths_industry_funds',clock)
        other,fingerprint=task(db,'ths_concept_funds',clock)
        db.get(Job,origin.id).lease_until=clock-timedelta(seconds=1)
    budget.record_failure(origin,SourceFailure(reason,retry_after=172800))
    budget.record_failure(origin,SourceFailure(reason,retry_after=600))
    with pytest.raises(SourceFailure,match='local_request_wait') as caught:
        budget.reserve(other,expected_configuration=fingerprint)
    assert caught.value.request_count==0 and caught.value.retry_after==172800
    with Session(engine()) as db:
        assert budget.cooldown_until(db,clock)==clock+timedelta(days=2)
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==0
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action=='source.retry_after.ths-funds'))==2


@pytest.mark.parametrize('delay,basis,seconds',[(None,'local_backoff',300),(0,'supplier_retry_after',0)])
def test_missing_header_is_explicit_local_fallback_and_zero_is_preserved(client,monkeypatch,delay,basis,seconds):
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market import ths_funds_cooldown as budget
    from app.models import Job
    prepared(client,monkeypatch);clock=datetime.now(UTC)
    monkeypatch.setattr(budget,'now',lambda db:clock)
    with Session(engine()) as db,db.begin():job,_=task(db,'ths_industry_funds',clock)
    budget.record_failure(job,SourceFailure('rate_limited',retry_after=delay))
    with Session(engine()) as db:
        receipt=db.get(Job,job.id).result_ref['ths_cooldown']
        assert receipt['basis']==basis and receipt['seconds']==seconds


def test_future_run_is_not_a_current_vendor_instruction_and_plain_schedule_is_not_cooldown(client,monkeypatch):
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market import ths_funds_cooldown as budget
    from app.market.models import DataFetchRun,SourceSettings
    from app.market.service import source_id
    prepared(client,monkeypatch);clock=datetime.now(UTC)
    monkeypatch.setattr(budget,'now',lambda db:clock)
    with Session(engine()) as db,db.begin():
        job,fingerprint=task(db,'ths_industry_funds',clock)
        db.get(SourceSettings,source_id('ths_industry_funds')).next_run_at=clock+timedelta(days=1)
        db.add(DataFetchRun(source_id=source_id('ths_concept_funds'),capability='synthetic.future',
            started_at=clock+timedelta(seconds=5),received_at=clock+timedelta(seconds=10),
            status='error',reason='rate_limited',request_count=1,retry_not_before=clock+timedelta(hours=1)))
    with Session(engine()) as db:assert budget.cooldown_until(db,clock) is None
    assert budget.reserve(job,expected_configuration=fingerprint)


@pytest.mark.parametrize('boundary',['expired_lease','changed_configuration','disabled_source','revoked_rights','disabled_owner'])
def test_final_guard_rechecks_current_lease_configuration_and_permission(client,monkeypatch,boundary):
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market import ths_funds_cooldown as budget
    from app.market.models import SourceSettings
    from app.market.service import source_id
    from app.models import Job,Provider,User
    prepared(client,monkeypatch);clock=datetime.now(UTC)
    monkeypatch.setattr(budget,'now',lambda db:clock)
    with Session(engine()) as db,db.begin():job,fingerprint=task(db,'ths_industry_funds',clock)
    with Session(engine()) as db,db.begin():
        if boundary=='expired_lease':db.get(Job,job.id).lease_until=clock
        elif boundary=='changed_configuration':db.get(SourceSettings,source_id('ths_industry_funds')).interval_seconds+=60
        elif boundary=='disabled_owner':db.get(User,job.owner_id).enabled=False
        else:
            provider=db.get(Provider,source_id('ths_industry_funds'))
            if boundary=='disabled_source':provider.enabled=False
            else:provider.rights={**provider.rights,'cache':'denied'}
    with pytest.raises(SourceFailure):budget.reserve(job,expected_configuration=fingerprint)


@pytest.mark.parametrize('status',[429,503])
def test_real_collect_path_retains_two_day_wait_and_other_family_sends_zero_http(client,monkeypatch,status):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market.collect import collect_job
    from app.market.models import DataFetchRun,SourceSettings
    from app.market.service import source_id
    from app.models import Job
    prepared(client,monkeypatch);clock=datetime.now(UTC);calls=[]
    def request(*args,**kwargs):
        calls.append(args)
        raise HTTPError('https://data.10jqka.com.cn/',status,'Synthetic limit',{'Retry-After':'172800'},None)
    monkeypatch.setattr(ths_funds.urllib.request,'build_opener',lambda *args:SimpleNamespace(open=request))
    with Session(engine()) as db,db.begin():
        first,_=task(db,'ths_industry_funds',clock);other,_=task(db,'ths_concept_funds',clock)
    collect_job(first);collect_job(other)
    with Session(engine()) as db:
        runs=list(db.scalars(select(DataFetchRun).order_by(DataFetchRun.received_at)))
        assert len(calls)==sum(run.request_count for run in runs)==1
        by_job={run.job_id:run for run in runs}
        assert by_job[first.id].request_count==1 and by_job[first.id].reason==('rate_limited' if status==429 else 'upstream_unavailable')
        assert by_job[first.id].retry_not_before is None
        assert by_job[other.id].request_count==0 and by_job[other.id].reason=='local_request_wait'
        deadline=db.get(SourceSettings,source_id('ths_industry_funds')).legacy_cooldown_until
        assert deadline>=clock+timedelta(days=2)
        pending=db.get(Job,other.id)
        assert pending.state=='queued' and pending.attempts==0 and pending.available_at>=deadline


def test_unrepresentable_supplier_wait_pauses_both_families_and_keeps_failure_reason(client,monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market.collect import collect_job
    from app.market.models import DataFetchRun,SourceSettings
    from app.market.service import source_id
    prepared(client,monkeypatch);clock=datetime.now(UTC);calls=[]
    def request(*args,**kwargs):
        calls.append(args)
        raise HTTPError('https://data.10jqka.com.cn/',429,'Synthetic unrepresentable limit',{'Retry-After':'99999999999'},None)
    monkeypatch.setattr(ths_funds.urllib.request,'build_opener',lambda *args:SimpleNamespace(open=request))
    with Session(engine()) as db,db.begin():job,_=task(db,'ths_industry_funds',clock)
    collect_job(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert len(calls)==run.request_count==1 and run.reason=='retry_after_out_of_range'
        for family in ('ths_industry_funds','ths_concept_funds'):
            assert db.get(SourceSettings,source_id(family)).paused_reason=='retry_after_out_of_range'


def test_mid_page_vendor_cooldown_retains_already_sent_request_count(client,monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market.collect import collect_job
    from app.market.models import DataFetchRun,SourceSettings
    from app.market.service import source_id
    from app.models import Job
    from test_ths_funds_unit import page
    prepared(client,monkeypatch);clock=datetime.now(UTC);calls=[]
    def request(*args):
        calls.append(args)
        with Session(engine()) as db,db.begin():
            db.get(SourceSettings,source_id('ths_concept_funds')).legacy_cooldown_until=clock+timedelta(days=2)
        return page(pager='1/2'),clock
    monkeypatch.setattr(ths_funds,'request',request);monkeypatch.setattr(ths_funds.time,'sleep',lambda seconds:None)
    with Session(engine()) as db,db.begin():job,_=task(db,'ths_industry_funds',clock)
    collect_job(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert len(calls)==run.request_count==1 and run.reason=='local_request_wait'
        pending=db.get(Job,job.id)
        assert pending.state=='queued' and pending.attempts==0 and pending.available_at>=clock+timedelta(days=2)
