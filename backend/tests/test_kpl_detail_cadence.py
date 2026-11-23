"""Both live and resumable detail packets progress under unchanged KPL permits."""
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import kpl_count_jobs as details
from app.market.kpl_leaders import DATASET as LEADERS
from app.market.kpl_limit_counts import DATASET as COUNTS
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response, SourceFailure
from app.market.resilience import latest_run
from app.models import Job, Provider
from app.worker import source_claim_allowed
from test_kpl_count_jobs import prepare, response as count_response
from test_direction_archive import DAY


def setup(client,monkeypatch,size=24):
    ident,clock,reserved=prepare(client,monkeypatch,size,
        clock_at=datetime.fromisoformat(str(DAY)+'T10:00:00+08:00'))
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,ident)
        config.paused_reason=None;config.interval_seconds=60;config.next_run_at=clock[0]+timedelta(seconds=60)
    calls=[]
    def fetch(family,*,plate_code,plate_summary=False):
        calls.append(('count' if plate_summary else 'leader',plate_code));clock[0]+=timedelta(seconds=3)
        if plate_summary:return count_response(clock[0],1)
        return leader_response(clock[0])
    monkeypatch.setattr('app.market.direction_sources.fetch',fetch)
    return ident,clock,reserved,calls


def leader_response(stamp,partial=False):
    row=[0]*70;row[0]='600001';row[1]='合成来源龙头';row[24]='龙一'
    return Response(json.dumps(dict(errcode='0',Day=[str(DAY)],Count=99 if partial else 1,list=[row])).encode(),stamp,1,200)


def take(flag):
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=db.scalar(select(Job).where(Job.payload[flag].astext=='true',Job.state=='queued').order_by(Job.created_at))
        assert job is not None
        job.state='running';job.attempts+=1;job.lease_token=uuid.uuid4()
        job.lease_until=datetime.now(UTC)+timedelta(hours=1)
        return job


def test_leaders_are_top_ten_bounded_independent_and_keep_main_clock(client,monkeypatch):
    ident,clock,reserved,calls=setup(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,ident);due=config.next_run_at
        main=Job(owner_id=config.owner_id,kind='source.collect',state='succeeded',payload={'source_id':str(ident)})
        db.add(main);db.flush()
        run=DataFetchRun(source_id=ident,job_id=main.id,capability='market.direction',status='complete',
            started_at=clock[0]-timedelta(seconds=20),received_at=clock[0],request_count=9)
        db.add(run);db.flush();main_run=run.id
    details.schedule();details.schedule(leaders=True);details.schedule(leaders=True)
    for _ in range(4):
        task=take('kpl_leaders')
        assert 1<=len(task.payload['codes'])<=3
        assert all(int(row['code'])<801010 for row in task.payload['codes'])
        details.process_job(task)
        details.schedule(leaders=True)
    with Session(engine()) as db:
        leaders=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.payload['dataset'].astext==LEADERS)))
        assert len(leaders)==10 and len(calls)==10
        assert all(row.payload['rows'][0]['archive_kind']=='intraday' for row in leaders)
        assert all(row.received_at==datetime.fromisoformat(row.payload['rows'][0]['page_received_at']) for row in leaders)
        assert db.get(SourceSettings,ident).next_run_at==due
        assert latest_run(db,ident,clock[0]).id==main_run
        assert db.scalar(select(Job.id).where(Job.payload['kpl_counts'].astext=='true',Job.state=='queued'))
    assert len(reserved)==10


@pytest.mark.parametrize('leaders',[False,True])
def test_packet_yields_before_next_request_keeps_success_and_resumes(client,monkeypatch,leaders):
    ident,clock,reserved,calls=setup(client,monkeypatch,8)
    flag='kpl_leaders' if leaders else 'kpl_counts';prefix='leader' if leaders else 'count'
    details.schedule(leaders=leaders)
    task=take(flag)
    with Session(engine()) as db,db.begin():db.get(SourceSettings,ident).next_run_at=clock[0]+timedelta(seconds=20)
    details.process_job(task)
    with Session(engine()) as db:
        saved=db.get(Job,task.id)
        assert saved.state=='queued' and saved.attempts==0 and saved.lease_token is None
        assert saved.error_code=='live_collection_wait' and len(saved.result_ref[prefix+'_completed'])==1
        assert len(calls)==len(reserved)==1
        assert db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==task.id)).request_count==1
    with Session(engine()) as db,db.begin():db.get(SourceSettings,ident).next_run_at=clock[0]+timedelta(seconds=60)
    details.process_job(take(flag))
    with Session(engine()) as db:
        assert db.get(Job,task.id).state=='succeeded'
        assert len(db.get(Job,task.id).result_ref[prefix+'_completed'])==(3 if leaders else 8)
    assert calls.count((prefix if leaders else 'count', '801000'))==1


def test_claim_and_post_permit_wait_give_due_main_ranking_the_source(client,monkeypatch):
    ident,clock,_,calls=setup(client,monkeypatch,3)
    details.schedule(leaders=True)
    with Session(engine()) as db,db.begin():
        task=db.scalar(select(Job).where(Job.payload['kpl_leaders'].astext=='true'))
        db.get(SourceSettings,ident).next_run_at=clock[0]+timedelta(seconds=16)
        assert not source_claim_allowed(db,task,clock[0])
        db.get(SourceSettings,ident).next_run_at=clock[0]+timedelta(seconds=25)
        assert source_claim_allowed(db,task,clock[0])
    task=take('kpl_leaders')
    def slow_permit(*a,**k):clock[0]+=timedelta(seconds=10);return True
    monkeypatch.setattr('app.market.kpl_atlas_budget.reserve',slow_permit)
    details.process_job(task)
    assert calls==[]
    with Session(engine()) as db:
        assert db.get(Job,task.id).state=='queued'
        assert db.scalar(select(DataFetchRun.id)) is None


@pytest.mark.parametrize('reason,delay',[('rate_limited',937),('rate_limited',172800),('permission_missing',None),('retry_after_out_of_range',99999999)])
def test_detail_control_refusals_stop_next_code_and_preserve_actual_request(client,monkeypatch,reason,delay):
    ident,clock,_,_=setup(client,monkeypatch,3)
    monkeypatch.setattr('app.market.kpl_atlas_budget.now',lambda db:clock[0])
    details.schedule(leaders=True);task=take('kpl_leaders');calls=[]
    def refused(*a,**k):calls.append(1);raise SourceFailure(reason,retry_after=delay)
    monkeypatch.setattr('app.market.direction_sources.fetch',refused)
    with pytest.raises(SourceFailure,match=reason):details.process_job(task)
    with Session(engine()) as db:
        runs=list(db.scalars(select(DataFetchRun).where(DataFetchRun.job_id==task.id)))
        assert sum(row.request_count for row in runs)==1 and calls==[1]
        assert db.get(Job,task.id).result_ref['leader_failed']==['801000']
        config=db.get(SourceSettings,ident)
        if reason=='rate_limited':assert config.legacy_cooldown_until==clock[0]+timedelta(seconds=delay)
        else:assert config.paused_reason==reason


@pytest.mark.parametrize('denial',['permission','configuration','next_day','calendar','disabled'])
def test_leader_packet_fences_context_before_any_request(client,monkeypatch,denial):
    from app.config import settings
    ident,clock,reserved,calls=setup(client,monkeypatch,3)
    details.schedule(leaders=True);task=take('kpl_leaders')
    if denial in {'permission','configuration'}:
        with Session(engine()) as db,db.begin():
            if denial=='permission':db.get(Provider,ident).enabled=False
            else:db.get(SourceSettings,ident).interval_seconds=180
    elif denial=='next_day':clock[0]+=timedelta(days=1)
    elif denial=='calendar':monkeypatch.setattr('app.market.session.market_session',lambda *a:SimpleNamespace(is_trading_day=False))
    else:monkeypatch.setattr(settings(),'data_collection_enabled',False)
    with pytest.raises(SourceFailure):details.process_job(task)
    assert not calls and not reserved


def test_two_minute_cycles_allow_main_count_and_leader_progress(client,monkeypatch):
    from app.market.collect import collect_job
    from app.market.service import enqueue_collection, source_id
    from test_direction_recovery import setup as live_setup
    clock=live_setup(client,monkeypatch,'direction_kpl_strength')
    ident=source_id('direction_kpl_strength')
    monkeypatch.setattr(details,'now',lambda db:clock[0])
    monkeypatch.setattr('app.market.kpl_atlas_budget.reserve',lambda *a,**k:True)
    monkeypatch.setattr('time.sleep',lambda seconds:clock.__setitem__(0,clock[0]+timedelta(seconds=seconds)))
    calls=[]
    def fetch(family,*,index=0,day=None,plate_code=None,plate_summary=False):
        if plate_code is not None:
            calls.append(('count' if plate_summary else 'leader',plate_code))
            clock[0]+=timedelta(seconds=3)
            return count_response(clock[0],1) if plate_summary else leader_response(clock[0])
        calls.append(('main',index));clock[0]+=timedelta(milliseconds=50)
        rows=[[str(801000+i),'合成题材'+str(i),1000-i,*([0]*16)] for i in range(index,min(index+30,241))]
        return Response(json.dumps(dict(errcode='0',Day=[str(DAY)],Count=241,list=rows)).encode(),clock[0],50,200)
    monkeypatch.setattr('app.market.direction_sources.fetch',fetch)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,ident);config.paused_reason=None;config.interval_seconds=60
    beginning=clock[0]
    main_ids=[]
    for cycle in range(2):
        start=beginning+timedelta(minutes=cycle)
        clock[0]=start
        with Session(engine(),expire_on_commit=False) as db,db.begin():
            main=enqueue_collection(db,ident,scheduled_realtime=True)
            main.state='running';main.attempts=1;main.lease_token=uuid.uuid4()
            main.lease_until=datetime.now(UTC)+timedelta(hours=1)
            main_ids.append(main.id)
        collect_job(main)
        with Session(engine()) as db:
            run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==main.id))
            assert run.status=='complete' and run.request_count==9 and run.started_at==start
            assert run.received_at<start+timedelta(seconds=20)
            assert db.get(SourceSettings,ident).next_run_at==start+timedelta(seconds=60)
        details.schedule();details.schedule(leaders=True)
        with Session(engine()) as db:
            queued=list(db.scalars(select(Job).where(Job.kind=='direction.archive',Job.state=='queued')
                .order_by(Job.created_at,Job.id)))
            flags=['kpl_leaders' if task.payload.get('kpl_leaders') else 'kpl_counts' for task in queued]
        for flag in flags:
            details.process_job(take(flag))
        assert clock[0]<start+timedelta(seconds=60)
    with Session(engine()) as db:
        assert len(list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.payload['dataset'].astext==COUNTS))))>=8
        assert len(list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.payload['dataset'].astext==LEADERS))))>=3
        runs=list(db.scalars(select(DataFetchRun).where(DataFetchRun.job_id.in_(main_ids)).order_by(DataFetchRun.started_at)))
        assert len(runs)==2 and runs[1].started_at-runs[0].started_at==timedelta(seconds=60)
        assert latest_run(db,ident,clock[0]).id==runs[-1].id
    assert [offset for kind,offset in calls if kind=='main']==list(range(0,241,30))*2
    assert {kind for kind,code in calls}=={'main','count','leader'}


@pytest.mark.parametrize('backfill',[False,True])
def test_old_long_archives_stay_queued_during_live_window_with_progress_intact(client,monkeypatch,backfill):
    ident,clock,_,_=setup(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,ident)
        payload=dict(source_id=str(ident),day=str(DAY))
        if backfill:payload['sector_atlas_backfill']=True
        task=Job(owner_id=config.owner_id,kind='direction.archive',state='queued',payload=payload,
            result_ref={'known_source_ranks':[1,2]},attempts=1)
        db.add(task);db.flush()
        assert not source_claim_allowed(db,task,clock[0])
        assert task.state=='queued' and task.attempts==1 and task.result_ref=={'known_source_ranks':[1,2]}
        assert source_claim_allowed(db,task,clock[0].replace(hour=15,minute=5))


def test_live_detail_failures_do_not_exhaust_closed_day_archive_budget(client,monkeypatch):
    from app.market import sector_atlas_backfill as archive
    ident,clock,_,_=setup(client,monkeypatch)
    clock[0]=clock[0].replace(hour=16)
    monkeypatch.setattr(archive,'now',lambda db:clock[0])
    monkeypatch.setattr(archive,'available_trade_days',lambda *a,**k:[DAY])
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,ident)
        for mode in ('kpl_counts','kpl_leaders'):
            for _ in range(3):
                db.add(Job(owner_id=config.owner_id,kind='direction.archive',state='failed',
                    payload={'source_id':str(ident),'day':str(DAY),mode:True},created_at=clock[0],
                    completed_at=clock[0],error_code='timeout'))
    archive.schedule_atlas_backfill()
    with Session(engine()) as db:
        tasks=list(db.scalars(select(Job).where(Job.state=='queued')))
        assert len(tasks)==1 and tasks[0].payload['sector_atlas_backfill'] is True


def test_recovery_does_not_treat_live_leader_packet_as_daily_archive(client,monkeypatch):
    from app.market import recovery
    ident,clock,_,_=setup(client,monkeypatch)
    details.schedule(leaders=True)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,ident);config=db.get(SourceSettings,ident)
        monkeypatch.setattr(recovery,'budget_available',lambda db:True)
        monkeypatch.setattr(recovery,'pending',lambda *a:False)
        monkeypatch.setattr('app.market.direction_archive._saved',lambda *a:False)
        monkeypatch.setattr('app.market.review_store.available_trade_days',lambda *a,**k:[DAY])
        recovery.direction_coverage(db,provider,config,[DAY],clock[0],[1])
        tasks=list(db.scalars(select(Job).where(Job.kind=='direction.archive')))
        assert len(tasks)==2 and sum(task.payload.get('kpl_leaders') is True for task in tasks)==1
