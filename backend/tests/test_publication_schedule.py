"""Publication windows, empty runs and restart/budget invariants."""
from datetime import UTC,date,datetime,timedelta
import json
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from pydantic import SecretStr

from app.config import settings
from app.db import engine
from app.market.protocol import SourceFailure,Response
from app.market.publication_schedule import (align_publication,next_publication,deferred_publication)
from app.market.tushare import parse_reference
from app.market.service import source_id,schedule_sources
from app.market.budget import reserve_tushare_request
from app.market.models import SourceSettings,DataFetchRun
from app.models import AuditEvent,Job
from app.worker import claim
from test_tushare import body,source,queue,TOKEN
from test_data_sources import configure,login


def at(value):return datetime.fromisoformat(value+'+08:00')


@pytest.mark.parametrize('family,stamp,expected',[
 ('tushare_limit_list_d','2026-10-23T09:00:00','2026-10-23T16:00:00'),
 ('tushare_top_list','2026-10-23T16:00:00','2026-10-23T16:30:00'),
 ('tushare_top_list','2026-10-23T16:30:00','2026-10-23T16:30:00'),
 ('tushare_top_list','2026-10-23T21:29:59','2026-10-23T21:29:59'),
 ('tushare_top_list','2026-10-23T21:30:00','2026-10-24T16:30:00'),
])
def test_alignment_is_local_policy_and_never_moves_backwards(family,stamp,expected):
    assert align_publication(family,at(stamp))==at(expected)
    assert align_publication(family,at(stamp).astimezone(UTC))==at(expected)


def test_interval_is_preserved_and_end_window_does_not_schedule_overnight_http():
    stamp=at('2026-10-23T16:30:00')
    assert next_publication('tushare_top_list',stamp,900)==at('2026-10-23T16:45:00')
    assert next_publication('tushare_top_list',stamp,21600)==at('2026-10-24T16:30:00')
    assert next_publication('tushare_top_list',at('2026-10-23T21:20:00'),900)==at('2026-10-24T16:30:00')


@pytest.mark.parametrize('outcome',[None,date(2026,10,22),date(2026,10,23)])
def test_current_observed_calendar_is_required_without_weekday_guess(monkeypatch,outcome):
    def calendar(db,clock):
        if outcome is None:raise SourceFailure('calendar_not_ready')
        return outcome
    monkeypatch.setattr('app.market.mairui.pool_day',calendar)
    clock=at('2026-10-23T17:00:00')
    deadline=deferred_publication(None,'tushare_top_list',clock,900)
    assert deadline==(None if outcome==date(2026,10,23) else at('2026-10-23T18:00:00'))


@pytest.mark.parametrize('fields',[[],['trade_date','ts_code','reason']])
def test_top_list_empty_is_not_zero_or_permanent_schema_pause(fields):
    with pytest.raises(SourceFailure,match='daily_data_not_published'):
        parse_reference(body(fields,[]),'tushare_top_list',datetime.now(UTC))


@pytest.mark.parametrize('data',[None,{}, {'fields':None,'items':[]},{'fields':[],'items':None},
    {'fields':[],'items':[['bad']]},{'fields':['token'],'items':[]},{'fields':['name','name'],'items':[]}])
def test_top_list_malformed_success_stays_a_schema_failure(data):
    with pytest.raises(SourceFailure,match='schema_changed'):
        parse_reference(json.dumps({'code':0,'data':data}).encode(),'tushare_top_list',datetime.now(UTC))


def test_top_list_permission_error_is_not_hidden_by_empty_handling():
    with pytest.raises(SourceFailure,match='permission_missing'):
        parse_reference(json.dumps({'code':403,'data':{'fields':[],'items':[]}}).encode(),'tushare_top_list',datetime.now(UTC))


def test_top_list_nonempty_requires_dates_identity_and_keeps_multiple_reasons():
    fields=['trade_date','ts_code','reason'];rows=[['20011231','000001.SZ','合成原因一'],['20011231','000001.SZ','合成原因二']]
    parsed=parse_reference(body(fields,rows),'tushare_top_list',datetime.now(UTC))
    assert len(parsed.records[0][1].rows)==2
    for changed,reason in [([['20991231','000001.SZ','合成']],'future_source_time'),
                           ([['20011231','bad','合成']],'instrument_mismatch')]:
        with pytest.raises(SourceFailure,match=reason):
            parse_reference(body(fields,changed),'tushare_top_list',datetime.now(UTC))
    with pytest.raises(SourceFailure,match='schema_changed'):
        parse_reference(body(['date','text'],[['20011231','合成']]),'tushare_top_list',datetime.now(UTC))


def configure_top(client,monkeypatch):
    monkeypatch.setattr(settings(),'tushare_token',SecretStr(TOKEN))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    headers=login(client);configure(client,headers,source('tushare_top_list'))
    return headers


def test_scheduler_defers_before_window_without_jobs_or_supplier_calls(client,monkeypatch):
    configure_top(client,monkeypatch)
    clock=at('2026-10-23T09:00:00')
    monkeypatch.setattr('app.market.service.now',lambda db:clock)
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *args:pytest.fail('No request outside window'))
    with Session(engine()) as db,db.begin():db.get(SourceSettings,source_id('tushare_top_list')).next_run_at=clock-timedelta(seconds=1)
    schedule_sources();schedule_sources()
    with Session(engine()) as db:
        assert db.get(SourceSettings,source_id('tushare_top_list')).next_run_at==at('2026-10-23T16:30:00')
        assert not db.scalar(select(Job.id).where(Job.kind=='source.collect'))
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action=='source.request.tushare'))


def test_automatic_queue_and_final_permit_recheck_after_restart(client,monkeypatch):
    configure_top(client,monkeypatch)
    clock=at('2026-10-23T17:00:00')
    monkeypatch.setattr('app.market.service.now',lambda db:clock)
    monkeypatch.setattr('app.market.mairui.pool_day',lambda db,stamp:stamp.date())
    with Session(engine()) as db,db.begin():db.get(SourceSettings,source_id('tushare_top_list')).next_run_at=clock-timedelta(seconds=1)
    schedule_sources();schedule_sources()
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        jobs=list(db.scalars(select(Job).where(Job.kind=='source.collect')))
        assert len(jobs)==1 and jobs[0].payload['scheduled_publication'] is True
        # Simulate a queued task surviving until after the publication window.
        job=jobs[0];job.state='running';job.attempts=1
        import uuid
        job.lease_token=uuid.uuid4();job.lease_until=at('2026-10-23T23:00:00')
    monkeypatch.setattr('app.market.budget.now',lambda db:at('2026-10-23T22:00:00'))
    assert reserve_tushare_request(job) is False
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.state=='queued' and saved.attempts==0 and saved.lease_token is None
        assert saved.available_at==at('2026-10-24T16:30:00')
        assert saved.error_code=='publication_window_wait'
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action=='source.request.tushare'))
        assert not db.scalar(select(DataFetchRun.id))


def test_manual_empty_refresh_is_recorded_once_without_automatic_pause(client,monkeypatch):
    headers=configure_top(client,monkeypatch)
    monkeypatch.setattr('app.market.collect.pool_day',lambda db,stamp:date(2001,12,31))
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *args:Response(body([],[]),datetime.now(UTC),1,200))
    queue(client,headers,'tushare_top_list')
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.source_id==source_id('tushare_top_list')))
        assert run.status=='empty' and run.reason=='daily_data_not_published' and run.request_count==1
        assert db.get(SourceSettings,source_id('tushare_top_list')).paused_reason is None
