"""Preopen calendar, bounded empty retries and restart guards; synthetic only."""
from datetime import UTC, date, datetime, timedelta
import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import APIError
from app.config import settings
from app.db import engine
from app.market.budget import reserve_tushare_request
from app.market.collect import store_calendar_batch
from app.market.mairui import choose_pool_day, pool_day
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response, SourceFailure
from app.market.publication_schedule import LIMITS, align_publication, deferred_publication, next_publication
from app.market.service import enqueue_collection, schedule_sources, source_id
from app.market.tushare_market import parse_market
from app.models import AuditEvent, Job, Provider
from test_data_sources import configuration, configure, login
from test_publication_schedule import at
from test_tushare import TOKEN, body, queue, source


def setup(client, monkeypatch):
    monkeypatch.setattr(settings(),'tushare_token',SecretStr(TOKEN))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    headers=login(client);configure(client,headers,source(LIMITS))
    return headers


@pytest.mark.parametrize('stamp,expected',[
    ('2026-10-23T08:59:59','2026-10-23T09:00:00'),
    ('2026-10-23T09:00:00','2026-10-23T09:00:00'),
    ('2026-10-23T15:00:59','2026-10-23T15:00:59'),
    ('2026-10-23T15:01:00','2026-10-24T09:00:00'),
    ('2026-10-23T23:59:59','2026-10-24T09:00:00')])
def test_window_alignment_and_timezone(stamp,expected):
    assert align_publication(LIMITS,at(stamp))==at(expected)
    assert align_publication(LIMITS,at(stamp).astimezone(UTC))==at(expected)


def test_retry_and_overnight_phase_do_not_miss_next_morning():
    assert next_publication(LIMITS,at('2026-10-23T09:00:00'),900)==at('2026-10-23T09:15:00')
    assert next_publication(LIMITS,at('2026-10-23T14:50:00'),900)==at('2026-10-24T09:00:00')
    assert next_publication(LIMITS,at('2026-10-23T23:30:00'),21600)==at('2026-10-24T09:00:00')


@pytest.mark.parametrize('entries,expected',[
    ({'SSE':True,'SZSE':True},date(2026,10,23)),
    ({'SSE':False,'SZSE':False},date(2026,10,22)),
    ({'SSE':True,'SZSE':False},None),({'SSE':True},None),
    ({'SSE':True,'SZSE':'true'},None)])
def test_preopen_requires_both_observed_calendars_and_keeps_old_cutoff(entries,expected):
    today=date(2026,10,23);previous=date(2026,10,22)
    calendars={ex:{previous:True,**({today:entries[ex]} if ex in entries else {})} for ex in ('SSE','SZSE')}
    if expected is None:
        with pytest.raises(SourceFailure,match='calendar_not_ready'):
            choose_pool_day(calendars,at('2026-10-23T09:00:00'),preopen=True)
    else:
        assert choose_pool_day(calendars,at('2026-10-23T09:00:00'),preopen=True)==expected
        assert choose_pool_day(calendars,at('2026-10-23T09:00:00'))==previous


def test_preopen_uses_received_calendar_and_immediately_honors_revocation(client):
    headers=login(client);configure(client,headers,{**configuration('baostock_daily'),'interval_seconds':21600})
    clock=datetime.now(UTC);today=clock.astimezone(at('2026-10-23T00:00:00').tzinfo).date()
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('baostock_daily'));config=db.get(SourceSettings,provider.id)
        store_calendar_batch(db,provider,config,[(today-timedelta(days=1),True),(today,True)],clock-timedelta(seconds=1),exchanges=('SSE','SZSE'))
    with Session(engine()) as db:
        assert pool_day(db,clock,preopen=True)==today
        with pytest.raises(SourceFailure,match='calendar_not_ready'):pool_day(db,clock-timedelta(seconds=2),preopen=True)
    with Session(engine()) as db,db.begin():
        db.get(Provider,source_id('baostock_daily')).rights={'display':'denied','cache':'allowed'}
    with Session(engine()) as db:
        with pytest.raises(SourceFailure,match='calendar_not_ready'):pool_day(db,clock,preopen=True)


@pytest.mark.parametrize('fields',[[],['trade_date','ts_code','up_limit','down_limit']])
def test_limits_unpublished_and_legal_empty_realtime_remain_distinct(fields):
    with pytest.raises(SourceFailure,match='daily_data_not_published'):
        parse_market(body(fields,[]),at('2026-10-23T09:00:00'),limits=True)
    with pytest.raises(SourceFailure,match='instrument_unavailable'):
        parse_market(body([],[]),at('2026-10-23T09:00:00'))


@pytest.mark.parametrize('raw,reason',[
    (b'{"code":403,"data":{"fields":[],"items":[]}}','permission_missing'),
    (b'{"code":0,"data":{"fields":null,"items":[]}}','schema_changed'),
    (b'{"code":0,"data":{"fields":["token"],"items":[]}}','schema_changed'),
    (body(['name'],[]),'schema_changed')])
def test_malformed_and_permission_failures_remain_distinct(raw,reason):
    with pytest.raises(SourceFailure,match=reason):parse_market(raw,at('2026-10-23T09:00:00'),limits=True)


@pytest.mark.parametrize('state,day_offset,accepted',[
    ('complete',0,1),('complete',-1,1),('empty',0,0),('partial',0,1),('error',0,0)])
def test_nonempty_subset_or_old_batch_does_not_stop_later_refreshes(client,monkeypatch,state,day_offset,accepted):
    setup(client,monkeypatch);clock=at('2026-10-23T09:30:00')
    monkeypatch.setattr('app.market.mairui.pool_day',lambda db,stamp,**kw:stamp.date())
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(LIMITS))
        stamp=at('2026-10-23T09:01:00')+timedelta(days=day_offset)
        db.add(DataFetchRun(source_id=config.source_id,capability='market.limits',started_at=stamp,received_at=stamp,
            status=state,request_count=1,accepted=accepted))
    with Session(engine()) as db:
        assert deferred_publication(db,LIMITS,clock,900) is None
        assert next_publication(LIMITS,clock,900)==at('2026-10-23T09:45:00')


@pytest.mark.parametrize('final_stamp,calendar_open,expected',[
    ('2026-10-23T15:01:00',True,'2026-10-24T09:00:00'),
    ('2026-10-24T00:00:01',True,'2026-10-24T09:00:00'),
    ('2026-10-23T09:01:00',False,'2026-10-23T09:16:00')])
def test_automatic_queue_and_final_guard_recheck_without_spending_permit(client,monkeypatch,final_stamp,calendar_open,expected):
    setup(client,monkeypatch);clock=at('2026-10-23T09:00:00')
    monkeypatch.setattr('app.market.service.now',lambda db:clock)
    monkeypatch.setattr('app.market.mairui.pool_day',lambda db,stamp,**kw:stamp.date())
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(LIMITS)).next_run_at=clock-timedelta(seconds=1)
    schedule_sources();schedule_sources()
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        jobs=list(db.scalars(select(Job).where(Job.kind=='source.collect')))
        assert len(jobs)==1 and jobs[0].payload['scheduled_publication'] is True
        job=jobs[0];job.state='running';job.attempts=1;job.lease_token=uuid.uuid4();job.lease_until=at('2026-10-25T23:00:00')
    monkeypatch.setattr('app.market.budget.now',lambda db:at(final_stamp))
    if not calendar_open:monkeypatch.setattr('app.market.mairui.pool_day',lambda *args,**kw:date(2026,10,22))
    assert reserve_tushare_request(job) is False
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.state=='queued' and saved.attempts==0 and saved.lease_token is None
        assert saved.available_at==at(expected) and saved.error_code=='publication_window_wait'
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action=='source.request.tushare'))
        assert not db.scalar(select(DataFetchRun.id))


def test_explicit_pending_task_is_not_relabelled_and_manual_refresh_keeps_spacing(client,monkeypatch):
    setup(client,monkeypatch);clock=at('2026-10-23T09:00:00')
    monkeypatch.setattr('app.market.service.now',lambda db:clock)
    monkeypatch.setattr('app.market.mairui.pool_day',lambda db,stamp,**kw:stamp.date())
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=enqueue_collection(db,source_id(LIMITS))
        db.get(SourceSettings,source_id(LIMITS)).next_run_at=clock-timedelta(seconds=1)
    schedule_sources()
    with Session(engine()) as db:
        assert db.get(Job,job.id).payload=={'source_id':str(source_id(LIMITS))}
        assert len(list(db.scalars(select(Job).where(Job.kind=='source.collect'))))==1


def test_empty_worker_outcome_does_not_pause_or_store_zero_counts(client,monkeypatch):
    headers=setup(client,monkeypatch)
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *args:Response(body([],[]),datetime.now(UTC),1,200))
    queue(client,headers,LIMITS)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.source_id==source_id(LIMITS)))
        assert run.status=='empty' and run.reason=='daily_data_not_published' and run.request_count==1
        assert db.get(SourceSettings,source_id(LIMITS)).paused_reason is None


@pytest.mark.parametrize('paused',[False,True])
def test_final_permit_rechecks_revocation_and_pause(client,monkeypatch,paused):
    setup(client,monkeypatch);clock=at('2026-10-23T09:00:00')
    monkeypatch.setattr('app.market.service.now',lambda db:clock)
    monkeypatch.setattr('app.market.budget.now',lambda db:clock)
    monkeypatch.setattr('app.market.mairui.pool_day',lambda db,stamp,**kw:stamp.date())
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=enqueue_collection(db,source_id(LIMITS),scheduled_publication=True)
        job.state='running';job.attempts=1;job.lease_token=uuid.uuid4();job.lease_until=clock+timedelta(minutes=5)
        if paused:db.get(SourceSettings,source_id(LIMITS)).paused_reason='schema_changed'
        else:db.get(Provider,source_id(LIMITS)).rights={'display':'denied','cache':'allowed'}
    with pytest.raises(SourceFailure):reserve_tushare_request(job)
    with Session(engine()) as db:
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action=='source.request.tushare'))


@pytest.mark.parametrize('age,allowed',[(901,True),(899,False)])
def test_manual_refresh_does_not_wait_for_automatic_next_day_but_keeps_interval(client,monkeypatch,age,allowed):
    setup(client,monkeypatch);clock=at('2026-10-23T18:00:00')
    monkeypatch.setattr('app.market.service.now',lambda db:clock)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(LIMITS));config.next_run_at=at('2026-10-24T09:00:00')
        stamp=clock-timedelta(seconds=age)
        db.add(DataFetchRun(source_id=config.source_id,capability='market.limits',started_at=stamp,received_at=stamp,status='complete',accepted=1,request_count=1))
    with Session(engine()) as db,db.begin():
        if allowed:assert enqueue_collection(db,source_id(LIMITS)).payload=={'source_id':str(source_id(LIMITS))}
        else:
            with pytest.raises(APIError,match='source_rate_limit'):enqueue_collection(db,source_id(LIMITS))
