"""Compatible-service supplier deadlines survive transport and all consumers."""
from datetime import UTC,datetime,timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from urllib.error import HTTPError
import json
import uuid

from pydantic import SecretStr
import pytest

from app.market import tushare
from app.market.protocol import SourceFailure
from test_tushare import TOKEN

STAMP=datetime(2026,11,12,5,tzinfo=UTC)


@pytest.mark.parametrize('status',[429,503])
@pytest.mark.parametrize('hint',['172800',format_datetime(STAMP+timedelta(days=2),usegmt=True)])
def test_paid_transport_preserves_real_two_day_header_without_echoing_private_errors(monkeypatch,status,hint):
    calls=[]
    def request(value,timeout):
        calls.append(value)
        assert TOKEN not in value.full_url
        raise HTTPError(value.full_url,status,TOKEN,{'Retry-After':hint},None)
    monkeypatch.setattr(tushare.urllib.request,'build_opener',lambda *args:SimpleNamespace(open=request))
    monkeypatch.setattr(tushare,'datetime',SimpleNamespace(now=lambda *args:STAMP))
    with pytest.raises(SourceFailure) as caught:
        tushare.request('daily',{},'',SecretStr(TOKEN),'https://api.tushare.pro/')
    assert len(calls)==1 and TOKEN not in str(caught.value)
    assert caught.value.retry_after==172800


@pytest.mark.parametrize('hint,expected',[('0',0),('',None),('not-a-delay',None)])
def test_zero_or_missing_header_never_becomes_an_invented_supplier_deadline(monkeypatch,hint,expected):
    def request(*args,**kwargs):raise HTTPError('https://api.tushare.pro/',429,TOKEN,{'Retry-After':hint},None)
    monkeypatch.setattr(tushare.urllib.request,'build_opener',lambda *args:SimpleNamespace(open=request))
    with pytest.raises(SourceFailure) as caught:
        tushare.request('daily',{},'',SecretStr(TOKEN),'https://api.tushare.pro/')
    assert caught.value.retry_after==expected and TOKEN not in str(caught.value)


def test_unrepresentable_supplier_header_is_rejected_without_truncation_or_credential_echo(monkeypatch):
    def request(*args,**kwargs):raise HTTPError('https://api.tushare.pro/',429,TOKEN,{'Retry-After':'99999999999'},None)
    monkeypatch.setattr(tushare.urllib.request,'build_opener',lambda *args:SimpleNamespace(open=request))
    with pytest.raises(SourceFailure,match='retry_after_out_of_range') as caught:
        tushare.request('daily',{},'',SecretStr(TOKEN),'https://api.tushare.pro/')
    assert caught.value.http_status==429 and TOKEN not in str(caught.value)


def configured(client,monkeypatch,families=('tushare_cn_cpi','tushare_shibor')):
    from app.config import settings
    from test_data_sources import login,configure
    from test_tushare import source
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr(settings(),'tushare_token',SecretStr(TOKEN))
    headers=login(client)
    for family in families:configure(client,headers,source(family),key=family)


def running(db,family,clock):
    from app.market.models import SourceSettings
    from app.market.service import source_id
    from app.models import Job
    config=db.get(SourceSettings,source_id(family))
    task=Job(owner_id=config.owner_id,kind='source.collect',state='running',attempts=1,
        lease_token=uuid.uuid4(),lease_until=clock+timedelta(days=1),payload={'source_id':str(config.source_id)})
    db.add(task);db.flush();db.expunge(task)
    return task


def test_long_cooldown_survives_expired_lease_is_shared_and_cannot_be_shortened(client,monkeypatch):
    from sqlalchemy import func,select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market import budget,tushare_cooldown as cooldown
    from app.market.models import DataFetchRun
    from app.models import AuditEvent,Job
    configured(client,monkeypatch);clock=datetime.now(UTC)
    monkeypatch.setattr(cooldown,'now',lambda db:clock)
    monkeypatch.setattr(budget,'now',lambda db:clock)
    with Session(engine()) as db,db.begin():
        first=running(db,'tushare_cn_cpi',clock);other=running(db,'tushare_shibor',clock)
        db.get(Job,first.id).lease_until=clock-timedelta(seconds=1)
    cooldown.record_failure(first,SourceFailure('rate_limited',retry_after=172800))
    cooldown.record_failure(first,SourceFailure('rate_limited',retry_after=600))
    assert not budget.reserve_tushare_request(other)
    with Session(engine()) as db:
        assert budget.tushare_cooldown_until(db,clock)==clock+timedelta(days=2)
        queued=db.get(Job,other.id)
        assert queued.state=='queued' and queued.available_at==clock+timedelta(days=2) and queued.attempts==0
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==0
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==budget.TUSHARE_ACTION))==0


def test_unrepresentable_wait_stops_the_other_source_final_permit(client,monkeypatch):
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market import budget,tushare_cooldown as cooldown
    from app.market.models import SourceSettings
    from app.market.service import source_id
    configured(client,monkeypatch);clock=datetime.now(UTC)
    with Session(engine()) as db,db.begin():
        first=running(db,'tushare_cn_cpi',clock);other=running(db,'tushare_shibor',clock)
    cooldown.record_failure(first,SourceFailure('retry_after_out_of_range'))
    with pytest.raises(SourceFailure,match='source_paused'):budget.reserve_tushare_request(other)
    with Session(engine()) as db:
        assert db.get(SourceSettings,source_id('tushare_cn_cpi')).paused_reason=='retry_after_out_of_range'
        assert db.get(SourceSettings,source_id('tushare_shibor')).paused_reason=='retry_after_out_of_range'


@pytest.mark.parametrize('reason',['rate_limited','upstream_unavailable'])
def test_normal_collect_keeps_true_two_day_wait_and_other_source_sends_no_request(client,monkeypatch,reason):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market import collect
    from app.market.models import DataFetchRun,SourceSettings
    from app.market.service import source_id
    from app.models import Job
    configured(client,monkeypatch);clock=datetime.now(UTC);calls=[]
    def request(*args):calls.append(args);raise SourceFailure(reason,retry_after=172800)
    monkeypatch.setattr(collect,'request_tushare',request)
    with Session(engine()) as db,db.begin():
        first=running(db,'tushare_cn_cpi',clock);other=running(db,'tushare_shibor',clock)
    collect.collect_job(first);collect.collect_job(other)
    with Session(engine()) as db:
        runs=list(db.scalars(select(DataFetchRun)))
        assert len(calls)==sum(item.request_count for item in runs)==1
        failed=next(item for item in runs if item.job_id==first.id)
        assert failed.reason==reason and failed.retry_not_before is None
        deadline=db.get(SourceSettings,source_id('tushare_cn_cpi')).legacy_cooldown_until
        assert deadline>=clock+timedelta(days=2)
        pending=db.get(Job,other.id)
        assert pending.state=='queued' and pending.attempts==0 and pending.available_at>=deadline


def test_mid_page_shared_cooldown_keeps_already_sent_http_and_queues_without_attempt_loss(client,monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.evidence.models import EvidenceVersion
    from app.market import collect
    from app.market.models import DataFetchRun,SourceSettings
    from app.market.service import source_id
    from app.models import Job
    from app.market.protocol import Response
    configured(client,monkeypatch,('tushare_ths_member','tushare_shibor'))
    clock=datetime.now(UTC);calls=[]
    monkeypatch.setattr('app.market.ths_members.PAGE_SIZE',1)
    monkeypatch.setattr(collect.time,'sleep',lambda seconds:None)
    def request(*args):
        calls.append(args)
        with Session(engine()) as db,db.begin():
            db.get(SourceSettings,source_id('tushare_shibor')).legacy_cooldown_until=clock+timedelta(days=2)
        body=json.dumps({'code':0,'data':{'fields':['ts_code','con_code','con_name'],
            'items':[['885800.TI','000001.SZ','合成股票']]}}).encode()
        return Response(body,datetime.now(UTC),1,200)
    monkeypatch.setattr(collect,'request_tushare',request)
    with Session(engine()) as db,db.begin():job=running(db,'tushare_ths_member',clock)
    collect.collect_job(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert len(calls)==run.request_count==1 and run.reason=='local_request_wait'
        assert not list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id('tushare_ths_member'))))
        queued=db.get(Job,job.id)
        assert queued.state=='queued' and queued.attempts==0 and queued.available_at>=clock+timedelta(days=2)


@pytest.mark.parametrize('reason',['rate_limited','upstream_unavailable'])
def test_review_attempt_and_terminal_receipts_do_not_truncate_or_violate_database_bound(monkeypatch,reason):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.config import settings
    from app.db import engine
    from app.market import review_jobs,budget
    from app.market.models import DataFetchRun,SourceSettings
    from app.models import Job
    from test_review_collection import setup_source
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr(settings(),'tushare_token',SecretStr(TOKEN))
    _,provider,_,job=setup_source('daily');clock=datetime.now(UTC);calls=[]
    def request(*args):calls.append(args);raise SourceFailure(reason,retry_after=172800)
    monkeypatch.setattr(tushare,'request',request)
    review_jobs.collect_job(job)
    with Session(engine()) as db:
        runs=list(db.scalars(select(DataFetchRun).where(DataFetchRun.job_id==job.id)))
        assert len(calls)==sum(item.request_count for item in runs)==1
        assert all(item.reason==reason and item.retry_not_before is None for item in runs)
        deadline=db.get(SourceSettings,provider.id).legacy_cooldown_until
        assert deadline>=clock+timedelta(days=2) and budget.tushare_cooldown_until(db,datetime.now(UTC))==deadline
        pending=db.get(Job,job.id)
        assert pending.state=='queued' and pending.available_at>=deadline


@pytest.mark.parametrize('reason',['rate_limited','upstream_unavailable'])
def test_tomorrow_preparation_keeps_supplier_wait_and_progress_receipt(monkeypatch,reason):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.config import settings
    from app.db import engine
    from app.market.models import DataFetchRun,SourceSettings
    from app.models import Job
    from app.tomorrow import prepare
    from test_tomorrow_prepare import setup_source,running_job
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr(settings(),'tushare_token',SecretStr(TOKEN))
    owner,provider,_=setup_source('tushare_daily_basic')
    job=running_job(owner,provider.family,'selected','600001.SH')
    clock=datetime.now(UTC);calls=[]
    def request(*args):calls.append(args);raise SourceFailure(reason,retry_after=172800)
    monkeypatch.setattr(prepare,'tushare_request',request)
    prepare.process_preparation(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert len(calls)==run.request_count==1 and run.reason==reason and run.retry_not_before is None
        deadline=db.get(SourceSettings,provider.id).legacy_cooldown_until
        assert deadline>=clock+timedelta(days=2)
        pending=db.get(Job,job.id)
        assert pending.state=='queued' and pending.available_at>=deadline
        assert pending.result_ref['requests']==1 and pending.result_ref['tushare_cooldown']['basis']=='supplier_retry_after'
