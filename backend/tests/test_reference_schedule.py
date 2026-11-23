"""Offline PG: slow automatic references yield without changing explicit intent."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import APIError
from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.auction_schedule import next_collection
from app.market.budget import reserve_tushare_request, TUSHARE_ACTION
from app.market.collect import collect_job
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response, SHANGHAI, SourceFailure
from app.market.reference_schedule import align_reference
from app.market.service import enqueue_collection, schedule_sources, source_id
from app.models import AuditEvent, Job, Provider, User
from test_data_sources import configure, login
from test_tushare import body, source, TOKEN


def setup(client, monkeypatch, family='tushare_ths_member', *, hour=8, minute=59, second=58):
    monkeypatch.setattr(settings(), 'tushare_token', SecretStr(TOKEN))
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    configure(client, login(client), source(family))
    clock=[datetime.now(UTC).astimezone(SHANGHAI).replace(hour=hour, minute=minute,
        second=second, microsecond=0)]
    monkeypatch.setattr('app.market.service.now', lambda db:clock[0])
    monkeypatch.setattr('app.market.budget.now', lambda db:clock[0])
    with Session(engine()) as db, db.begin():
        db.get(SourceSettings, source_id(family)).next_run_at=clock[0]-timedelta(seconds=1)
    return clock


def running(db, job, clock):
    job.state='running';job.attempts+=1;job.error_code=None
    job.lease_token=uuid.uuid4()
    job.lease_until=max(clock, datetime.now(UTC))+timedelta(days=1)
    return job


@pytest.mark.parametrize('value,expected', [
    ('08:59:59','08:59:59'), ('09:00:00','15:10:00'),
    ('11:30:00','15:10:00'), ('13:00:00','15:10:00'),
    ('15:05:00','15:10:00'), ('15:09:59','15:10:00'), ('15:10:00','15:10:00'),
])
def test_local_window_retains_close_receipt_buffer_and_timezone(value, expected):
    stamp=datetime.fromisoformat('2026-11-11T'+value+'+08:00')
    target=datetime.fromisoformat('2026-11-11T'+expected+'+08:00')
    assert align_reference('tushare_ths_member', stamp)==target
    assert align_reference('tushare_ths_member', stamp.astimezone(UTC))==target
    # Configured six-hour spacing is kept before aligning an automatic wake.
    assert next_collection('tushare_ths_member', stamp-timedelta(hours=6), 21600)==target


@pytest.mark.parametrize('family', ['tushare_calendar_sse','tushare_calendar_szse',
    'tushare_stock_basic','tushare_market_rt','tushare_market_limits',
    'tushare_stk_premarket','tushare_shibor'])
def test_live_dependencies_and_publication_families_are_not_static_gated(family):
    stamp=datetime.fromisoformat('2026-11-11T10:00:00+08:00')
    assert align_reference(family, stamp)==stamp


@pytest.mark.parametrize('family', ['tushare_ths_member','tushare_income','tushare_index_basic_sse'])
def test_quiet_scheduler_moves_only_due_time_without_jobs_or_http(client, monkeypatch, family):
    clock=setup(client, monkeypatch, family, hour=10)
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *a:pytest.fail('scheduler cannot fetch'))
    schedule_sources();schedule_sources()
    with Session(engine()) as db:
        config=db.get(SourceSettings,source_id(family))
        assert config.next_run_at==clock[0].replace(hour=15,minute=10,second=0)
        assert config.interval_seconds==source(family)['interval_seconds']
        assert not db.scalar(select(Job.id).where(Job.kind=='source.collect'))
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action==TUSHARE_ACTION))
        assert not db.scalar(select(DataFetchRun.id))


def test_offhours_scheduler_labels_new_job_and_does_not_relabel_existing_manual(client, monkeypatch):
    clock=setup(client,monkeypatch)
    ident=source_id('tushare_ths_member')
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        explicit=enqueue_collection(db,ident)
    schedule_sources()
    with Session(engine()) as db,db.begin():
        saved=db.get(Job,explicit.id)
        assert 'scheduled_reference' not in saved.payload
        saved.state='succeeded';saved.completed_at=clock[0]
        db.get(SourceSettings,ident).next_run_at=clock[0]
    schedule_sources()
    with Session(engine()) as db:
        jobs=list(db.scalars(select(Job).where(Job.kind=='source.collect')))
        assert len(jobs)==2
        automatic=next(task for task in jobs if task.id!=explicit.id)
        assert automatic.payload['scheduled_reference'] is True
        assert 'scheduled_reference' not in db.get(Job,explicit.id).payload


@pytest.mark.parametrize('cooldown', [False,True])
def test_restart_final_permit_defers_without_attempt_or_fake_request_and_keeps_cooldown(client,monkeypatch,cooldown):
    clock=setup(client,monkeypatch)
    ident=source_id('tushare_ths_member')
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=running(db,enqueue_collection(db,ident,scheduled_reference=True),clock[0])
        if cooldown:
            db.add(DataFetchRun(source_id=ident,capability='reference.tushare',started_at=clock[0],
                received_at=clock[0],status='error',reason='rate_limited',request_count=1,
                retry_not_before=clock[0].replace(hour=16)))
    clock[0]=clock[0].replace(hour=9)
    assert reserve_tushare_request(job) is False
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        expected=clock[0].replace(hour=16) if cooldown else clock[0].replace(hour=15,minute=10,second=0)
        assert saved.state=='queued' and saved.available_at==expected
        assert saved.attempts==0 and saved.lease_token is None and saved.lease_until is None
        assert saved.error_code=='publication_window_wait'
        assert db.get(SourceSettings,ident).next_run_at>=expected
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action==TUSHARE_ACTION))
        assert not db.scalar(select(EvidenceVersion.id))
        assert len(list(db.scalars(select(DataFetchRun))))==int(cooldown)


def test_crossing_window_stops_before_second_page_and_resumes_atomic_capture_after_close(client,monkeypatch):
    clock=setup(client,monkeypatch)
    ident=source_id('tushare_ths_member')
    monkeypatch.setattr('app.market.ths_members.PAGE_SIZE',1)
    monkeypatch.setattr('time.sleep',lambda seconds:clock.__setitem__(0,clock[0]+timedelta(seconds=seconds)))
    calls=[]
    def fetch(api,params,*args):
        calls.append((clock[0],params['offset']))
        rows=[['885800.TI','000001.SZ','合成股票']] if params['offset']==0 else []
        return Response(body(['ts_code','con_code','con_name'],rows),clock[0],1,200)
    monkeypatch.setattr('app.market.collect.request_tushare',fetch)
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=running(db,enqueue_collection(db,ident,scheduled_reference=True),clock[0])
    collect_job(job)
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.state=='queued' and saved.attempts==0 and saved.lease_token is None
        assert saved.available_at==clock[0].replace(hour=15,minute=10,second=0,microsecond=0)
        assert len(calls)==1 and calls[0][1]==0
        assert not db.scalar(select(EvidenceVersion.id))
        assert not db.scalar(select(DataFetchRun.id))
        assert len(list(db.scalars(select(AuditEvent).where(AuditEvent.action==TUSHARE_ACTION))))==1
    clock[0]=clock[0].replace(hour=15,minute=10,second=0,microsecond=0)
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=running(db,db.get(Job,job.id),clock[0])
    collect_job(job)
    with Session(engine()) as db:
        assert db.get(Job,job.id).state=='succeeded'
        runs=list(db.scalars(select(DataFetchRun)))
        assert len(runs)==1 and runs[0].status=='complete' and runs[0].request_count==2
        evidence=list(db.scalars(select(EvidenceVersion)))
        assert len(evidence)==2 and all(item.received_at>=clock[0]-timedelta(seconds=3) for item in evidence)
        assert [offset for _,offset in calls]==[0,0,1]
        assert len(list(db.scalars(select(AuditEvent).where(AuditEvent.action==TUSHARE_ACTION))))==3


@pytest.mark.parametrize('age,allowed', [(7,True),(5,False)])
def test_manual_request_ignores_automatic_window_wake_but_keeps_six_hour_spacing(client,monkeypatch,age,allowed):
    clock=setup(client,monkeypatch,hour=10)
    ident=source_id('tushare_ths_member')
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        config=db.get(SourceSettings,ident)
        config.next_run_at=clock[0].replace(hour=15,minute=10,second=0)
        db.add(DataFetchRun(source_id=ident,capability='reference.tushare',started_at=clock[0]-timedelta(hours=age),
            received_at=clock[0]-timedelta(hours=age),status='complete',request_count=1))
        db.flush()
        if allowed:
            job=running(db,enqueue_collection(db,ident),clock[0])
            assert 'scheduled_reference' not in job.payload
        else:
            with pytest.raises(APIError,match='尚在该来源的刷新间隔内'):
                enqueue_collection(db,ident)
    if allowed:
        assert reserve_tushare_request(job) is True
        with Session(engine()) as db:
            assert db.get(Job,job.id).state=='running'
            assert len(list(db.scalars(select(AuditEvent).where(AuditEvent.action==TUSHARE_ACTION))))==1


@pytest.mark.parametrize('change', ['permission','owner','payload','paused','lease'])
def test_reference_window_never_hides_revoked_rights_changed_intent_or_expired_lease(client,monkeypatch,change):
    clock=setup(client,monkeypatch)
    ident=source_id('tushare_ths_member')
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=running(db,enqueue_collection(db,ident,scheduled_reference=True),clock[0])
    with Session(engine()) as db,db.begin():
        saved=db.get(Job,job.id)
        if change=='permission':
            provider=db.get(Provider,ident);provider.rights={**provider.rights,'cache':'denied'}
        elif change=='owner':
            other=User(username='synthetic-reference-owner',password_hash='disabled-synthetic-user',role='researcher')
            db.add(other);db.flush();saved.owner_id=other.id
        elif change=='payload':saved.payload={**saved.payload,'synthetic_intent':True}
        elif change=='paused':db.get(SourceSettings,ident).paused_reason='schema_changed'
        else:saved.lease_until=clock[0]-timedelta(seconds=1)
    clock[0]=clock[0].replace(hour=9)
    with pytest.raises(SourceFailure):reserve_tushare_request(job)
    with Session(engine()) as db:
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action==TUSHARE_ACTION))


@pytest.mark.parametrize('family',['tushare_calendar_sse','tushare_calendar_szse','tushare_stock_basic'])
def test_calendar_and_directory_still_enqueue_at_opening(client,monkeypatch,family):
    setup(client,monkeypatch,family,hour=9,minute=15,second=0)
    schedule_sources()
    with Session(engine()) as db:
        job=db.scalar(select(Job).where(Job.kind=='source.collect'))
        assert job and job.payload['source_id']==str(source_id(family))
        assert 'scheduled_reference' not in job.payload
