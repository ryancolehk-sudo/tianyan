"""Real queue/lease semantics over disposable PostgreSQL; no upstream I/O."""
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.market import qt_budget as budget
from app.market.bulk_quote_queue import deferred_background, permit
from app.market.minute_backfill import configuration_fingerprint
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.service import source_id
from app.models import AuditEvent, Job, Provider
from app.worker import claim
from test_data_sources import configuration, configure
from test_tencent_market import configure_all


@pytest.fixture(autouse=True)
def collection_switch(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings(),'data_collection_enabled',True)


def task(db,family,clock,*,scheduled=False):
    provider=db.get(Provider,source_id(family));config=db.get(SourceSettings,provider.id)
    row=Job(owner_id=config.owner_id,kind='source.collect',state='running',attempts=1,
        lease_token=uuid.uuid4(),lease_until=clock+timedelta(days=1),
        payload=dict(source_id=str(provider.id),**({'scheduled_reference':True} if scheduled else {})))
    db.add(row);db.flush()
    fingerprint=configuration_fingerprint(provider,config);db.expunge(row)
    return row,fingerprint


def test_long_retry_after_is_not_clipped_to_a_day_and_invalid_hints_are_safe():
    clock=datetime(2026,11,12,tzinfo=UTC)
    assert budget.retry_after('172800',clock)==172800
    assert budget.retry_after(format_datetime(clock+timedelta(days=2)),clock)==172800
    assert budget.retry_after('nonsense',clock) is None
    assert budget.retry_after('-1',clock) is None
    with pytest.raises(SourceFailure,match='retry_after_out_of_range'):budget.retry_after('9999999999',clock)


def test_shared_physical_permits_wait_outside_transaction_and_cooldown_is_persistent(client,monkeypatch):
    configure_all(client);clock=[datetime.now(UTC)]
    monkeypatch.setattr(budget,'now',lambda db:clock[0])
    sleeps=[]
    def advance(seconds):sleeps.append(seconds);clock[0]+=timedelta(seconds=seconds)
    monkeypatch.setattr(budget.time,'sleep',advance)
    with Session(engine()) as db,db.begin():job,fingerprint=task(db,'tencent_market_rt',clock[0])
    assert budget.reserve(job,expected_configuration=fingerprint)
    assert budget.reserve(job,expected_configuration=fingerprint)
    assert sum(sleeps)>=1
    deadline=clock[0]+timedelta(days=2)
    budget.record_failure(job,SourceFailure('rate_limited',retry_after=172800))
    assert not budget.reserve(job,expected_configuration=fingerprint)
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.state=='queued' and saved.available_at==deadline and saved.attempts==0
        assert saved.lease_token is None
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==budget.ACTION))==2
        assert budget.cooldown_until(db,clock[0])==deadline


def test_continuation_waits_for_regressed_permit_clock_without_losing_completed_pages(client,monkeypatch):
    configure_all(client);clock=[datetime.now(UTC)]
    monkeypatch.setattr(budget,'now',lambda db:clock[0])
    sleeps=[]
    def advance(seconds):sleeps.append(seconds);clock[0]+=timedelta(seconds=seconds)
    monkeypatch.setattr(budget.time,'sleep',advance)
    with Session(engine()) as db,db.begin():job,fingerprint=task(db,'tencent_market_rt',clock[0])
    assert budget.reserve(job,expected_configuration=fingerprint)
    clock[0]-=timedelta(seconds=9)
    assert budget.reserve(job,expected_configuration=fingerprint,in_progress=True)
    assert 10<=sum(sleeps)<11
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.state=='running' and saved.lease_token==job.lease_token


@pytest.mark.parametrize('boundary',['expired_lease','changed_config','disabled'])
def test_final_request_permit_refuses_changed_control_state(client,monkeypatch,boundary):
    configure_all(client);clock=datetime.now(UTC)
    monkeypatch.setattr(budget,'now',lambda db:clock)
    with Session(engine()) as db,db.begin():job,fingerprint=task(db,'tencent_market_rt',clock)
    with Session(engine()) as db,db.begin():
        if boundary=='expired_lease':db.get(Job,job.id).lease_until=clock
        elif boundary=='changed_config':db.get(SourceSettings,source_id('tencent_market_rt')).interval_seconds=30
        else:db.get(Provider,source_id('tencent_market_rt')).enabled=False
    with pytest.raises(SourceFailure):budget.reserve(job,expected_configuration=fingerprint)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==budget.ACTION))==0


def test_slow_sdk_waits_only_for_enabled_bulk_on_verified_trading_day(client,monkeypatch):
    headers=configure_all(client)
    configure(client,headers,{**configuration('baostock_daily'),'interval_seconds':21600},key='qq-sdk-reference-config')
    clock=datetime.now(UTC).astimezone(SHANGHAI).replace(hour=10,minute=0,second=0,microsecond=0)
    monkeypatch.setattr('app.market.session.market_session',lambda *a:SimpleNamespace(is_trading_day=True))
    monkeypatch.setattr('app.market.bulk_quote_queue.now',lambda db:clock)
    with Session(engine()) as db,db.begin():job,fingerprint=task(db,'baostock_daily',clock,scheduled=True)
    with Session(engine()) as db:
        assert deferred_background(db,'baostock_daily',clock)==clock.replace(hour=15,minute=10)
        assert deferred_background(db,'baostock_daily',clock.replace(hour=16)) is None
        assert deferred_background(db,'tencent_web',clock) is None
    assert not permit(job,fingerprint)
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.state=='queued' and saved.attempts==0
    assert permit(SimpleNamespace(payload={}),fingerprint)
    with Session(engine()) as db,db.begin():db.get(Provider,source_id('tencent_market_rt')).enabled=False
    with Session(engine()) as db:assert deferred_background(db,'baostock_daily',clock) is None


def test_bulk_never_enters_fast_slot_and_yields_to_ready_background(client,monkeypatch):
    headers=configure_all(client)
    configure(client,headers,configuration('cninfo'),key='qq-queue-catalog-config')
    clock=datetime.now(UTC)-timedelta(seconds=5)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id('tencent_market_rt'))
        rows=[Job(owner_id=config.owner_id,kind='source.collect',state='queued',available_at=clock,
            payload={'source_id':str(source_id(family))}) for family in ('cninfo','tencent_market_rt')]
        db.add_all(rows);db.flush();background,bulk=[row.id for row in rows]
    assert claim('realtime',fast_only=True) is None
    assert claim('realtime',quotes_only=True) is None
    assert claim('general').id==bulk
    assert claim('general',prefer_background=True).id==background


def test_bulk_can_resume_its_fourth_and_fifth_live_attempt(client):
    configure_all(client)
    clock=datetime.now(UTC)-timedelta(seconds=5)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id('tencent_market_rt'))
        row=Job(owner_id=config.owner_id,kind='source.collect',state='queued',attempts=3,available_at=clock,
            payload={'source_id':str(config.source_id),'scheduled_realtime':True})
        db.add(row);db.flush();ident=row.id
    claimed=claim('general')
    assert claimed.id==ident and claimed.attempts==4


@pytest.mark.parametrize('boundary',['cooldown','window','spacing'])
def test_mid_batch_wait_preserves_lease_for_terminal_accounting(client,monkeypatch,boundary):
    configure_all(client);clock=datetime.now(UTC)
    monkeypatch.setattr(budget,'now',lambda db:clock)
    monkeypatch.setattr(budget.time,'sleep',lambda seconds:None)
    with Session(engine()) as db,db.begin():
        job,fingerprint=task(db,'tencent_market_rt',clock)
        if boundary=='window':
            saved=db.get(Job,job.id);saved.payload={**saved.payload,'scheduled_realtime':True}
            job.payload=saved.payload
    assert budget.reserve(job,expected_configuration=fingerprint) if boundary!='window' else True
    deadline=clock+timedelta(days=2)
    if boundary=='cooldown':
        with Session(engine()) as db,db.begin():db.get(SourceSettings,source_id('tencent_market_rt')).legacy_cooldown_until=deadline
    if boundary=='window':
        monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime',lambda *args:deadline)
    with pytest.raises(SourceFailure,match='intraday_window_wait' if boundary=='window' else 'source_budget_wait') as failed:
        budget.reserve(job,expected_configuration=fingerprint,in_progress=True)
    assert failed.value.retry_after==(1 if boundary=='spacing' else 172800)
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.state=='running' and saved.lease_token==job.lease_token and saved.attempts==1
        assert not list(db.scalars(select(DataFetchRun)))  # no artificial HTTP/rate-limit run


@pytest.mark.parametrize('boundary',['cooldown','window','config','scope_rights','owner'])
def test_collector_keeps_completed_request_count_when_later_page_is_stopped(client,monkeypatch,boundary):
    from app.config import settings
    from app.evidence.models import EvidenceVersion
    from app.market import tencent_market as qq
    from app.market.collect import collect_job
    from app.market.protocol import Response
    from app.market.service import enqueue_collection
    from app.models import User
    from test_catalog_scope import native,save
    from test_tencent_market import body
    configure_all(client);clock=datetime.now(UTC)
    keys=tuple(str(600001+i) for i in range(751))
    with Session(engine()) as db,db.begin():save(db,native(keys),clock-timedelta(seconds=1))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr('app.market.collect.pool_day',lambda *a,**k:clock.astimezone(SHANGHAI).date())
    monkeypatch.setattr(budget,'now',lambda db:clock)
    monkeypatch.setattr(qq,'observation_clock',lambda:clock)
    monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime',lambda *args:None)
    with Session(engine()) as db,db.begin():
        enqueue_collection(db,source_id(qq.FAMILY));db.flush()
        db.scalar(select(Job).where(Job.kind=='source.collect')).available_at=clock-timedelta(minutes=1)
    job=claim('general');assert job is not None
    calls=[]
    if boundary=='window':
        with Session(engine()) as db,db.begin():
            saved=db.get(Job,job.id);saved.payload={**saved.payload,'scheduled_realtime':True}
            job.payload=saved.payload
    deadline=clock+timedelta(days=2)
    def request(url):
        calls.append(url)
        assert len(calls)==1
        with Session(engine()) as db,db.begin():
            if boundary=='cooldown':db.get(SourceSettings,source_id(qq.FAMILY)).legacy_cooldown_until=deadline
            elif boundary=='config':db.get(SourceSettings,source_id(qq.FAMILY)).interval_seconds=30
            elif boundary=='scope_rights':
                source=db.get(Provider,source_id('cninfo'));source.rights={**source.rights,'display':'denied'}
            elif boundary=='owner':db.get(User,job.owner_id).enabled=False
        if boundary=='window':monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime',lambda *args:deadline)
        return Response(body(tuple(qq.InstrumentKey('SSE',key,'stock') for key in keys[:750]),stamp=clock-timedelta(seconds=1)),clock,50,200)
    monkeypatch.setattr(qq,'fetch_https',request)
    collect_job(job)
    with Session(engine()) as db:
        runs=list(db.scalars(select(DataFetchRun).where(DataFetchRun.source_id==source_id(qq.FAMILY))))
        assert len(calls)==len(runs)==1 and runs[0].request_count==1
        assert runs[0].reason=={'cooldown':'source_budget_wait','window':'intraday_window_wait',
            'config':'configuration_changed','scope_rights':'permission_missing','owner':'permission_missing'}[boundary]
        assert not list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.payload['dataset'].astext==qq.DATASET)))
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==budget.ACTION))==1
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action=='source.retry_after.tencent'))==0
        if boundary=='cooldown':
            assert budget.cooldown_until(db,clock)==deadline and db.get(Job,job.id).available_at>=deadline
