"""Stage observations must be bounded, private and transparent to collection."""
import hashlib,json,uuid
from datetime import UTC,datetime,timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.market.collect import collect_job
from app.market.models import DataFetchRun,SourceSettings
from app.market.protocol import Response
from app.market.service import enqueue_collection,source_id
from app.models import Job
from test_direction_recovery import setup

PREFIX='source_collection_stages '


def observations(caplog):
    return [json.loads(record.getMessage()[len(PREFIX):]) for record in caplog.records
        if record.getMessage().startswith(PREFIX)]


@pytest.mark.parametrize('family',['direction_kpl_strength','tushare_market_rt','tencent_web'])
@pytest.mark.parametrize('health_raises',[False,True])
def test_collector_reports_after_committed_evidence_and_preserves_health_error(client,monkeypatch,caplog,family,health_raises):
    from app.config import settings
    from pydantic import SecretStr
    monkeypatch.setattr(settings(),'tushare_token',SecretStr('synthetic-private-never-log'))
    clock=setup(client,monkeypatch,family)
    monkeypatch.setattr('app.market.kpl_atlas_budget.reserve',lambda *a,**k:True)
    monkeypatch.setattr('app.market.collect.reserve_tushare_request',lambda *a,**k:True)
    monkeypatch.setattr('app.market.qt_budget.reserve',lambda *a,**k:True)

    def ranking(source,**kw):
        clock[0]+=timedelta(milliseconds=20)
        return Response(json.dumps(dict(errcode='0',Day=[str(clock[0].date())],Count=1,
            list=[['801000','synthetic-private-board',1000,*([0]*16)]])).encode(),clock[0],20,200)

    def market(*a,**k):
        from app.market.tushare_market import encoded
        from test_tushare_market import quote
        clock[0]+=timedelta(milliseconds=20)
        row=quote(at=clock[0])
        return Response(encoded(list(row),[row]),clock[0],20,200)

    monkeypatch.setattr('app.market.direction_sources.fetch',ranking)
    monkeypatch.setattr('app.market.collect.request_tushare',market)
    def quotes(*a,**k):
        from test_data_sources import quote_body
        clock[0]+=timedelta(milliseconds=20)
        return Response(quote_body(clock=clock[0].strftime('%Y%m%d%H%M%S')),clock[0],20,200)
    monkeypatch.setattr('app.market.collect.fetch_https',quotes)
    failure=RuntimeError('synthetic-private-credential-and-payload')
    checked=[]

    def health(source,run_id,job_id):
        with Session(engine()) as db:
            run=db.get(DataFetchRun,run_id);stored=db.get(Job,job_id)
            assert run is not None and stored.state=='succeeded' and stored.lease_token is None
            checked.append(run_id)
        if health_raises:raise failure

    monkeypatch.setattr('app.market.resilience.record_committed_fetch',health)
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        config=db.get(SourceSettings,source_id(family));config.paused_reason=None;config.interval_seconds=60
        job=enqueue_collection(db,source_id(family),scheduled_realtime=True)
        job.state='running';job.attempts=1;job.lease_token=uuid.uuid4()
        job.lease_until=datetime.now(UTC)+timedelta(hours=1)
    with caplog.at_level('INFO'):
        if health_raises:
            with pytest.raises(RuntimeError) as raised:collect_job(job)
            assert raised.value is failure
        else:collect_job(job)
    assert len(checked)==1
    with Session(engine()) as db:
        run=db.get(DataFetchRun,checked[0])
        assert run.status=='complete' and run.request_count==1
        assert db.get(SourceSettings,source_id(family)).interval_seconds==60
    rows=observations(caplog)
    assert len(rows)==1
    value=rows[0]
    assert value['v']==1 and value['family']==family and value['requests']==1
    assert value['run_tag']==hashlib.sha256(checked[0].bytes).hexdigest()[:16]
    assert value['health_callback_returned'] is not health_raises
    assert value['permit_calls']>=1 and value['permit_ms']>=0
    if family=='tencent_web':assert value['qt_gate_ms']>=0
    assert value['storage_commit_ms']>=0 and value['health_callback_ms']>=0
    assert value['collector_ms']>=value['health_callback_ms']
    assert not any(word in json.dumps(value) for word in ['private','"801000"',str(job.id),str(job.lease_token),clock[0].isoformat()])


def job(payload=None,**changes):
    at=datetime(2026,11,13,6,30,tzinfo=UTC)
    value=dict(kind='source.collect',payload={'scheduled_realtime':True} if payload is None else payload,
        attempts=1,created_at=at-timedelta(seconds=2),available_at=at-timedelta(seconds=1))
    value.update(changes)
    return SimpleNamespace(**value),at


def test_monotonic_groups_keep_nested_permits_and_post_commit_work_distinct(caplog):
    from app.market.collection_timings import CollectionTiming
    task,at=job();clock=[0.0]
    trace=CollectionTiming(task,at,clock=lambda:clock[0]);trace.bind('tencent_web')

    @trace.permission
    def permit():
        def source():clock[0]+=1;return True
        return trace.call('source_gate',source)

    clock[0]=2;assert permit() is True
    clock[0]=4;assert permit() is True
    clock[0]=7;trace.storage_started(at+timedelta(seconds=6),at+timedelta(seconds=7))
    clock[0]=9;trace.committed()
    clock[0]=11
    with caplog.at_level('INFO'):trace.finish(uuid.uuid4(),'complete',2,health_returned=True)
    value=observations(caplog)[0]
    assert value['creation_to_entry_ms']==2000 and value['ready_to_entry_ms']==1000
    assert value['to_first_permit_ms']==3000 and value['outside_permit_before_first_ms']==2000
    assert value['permit_calls']==2 and value['permit_ms']==2000 and value['source_gate_ms']==2000
    assert value['qt_gate_ms']==0 and value['first_permit_to_storage_ms']==4000
    assert value['receipt_to_storage_wall_ms']==1000 and value['storage_commit_ms']==2000
    assert value['health_callback_ms']==2000 and value['collector_ms']==11000


@pytest.mark.parametrize('payload',[
    {},{'scheduled_realtime':'true'},{'scheduled_realtime':True,'schema_probe':True},
    {'scheduled_realtime':True,'integration_context':{'probe':True}},
    {'scheduled_realtime':True,'minute_context':{}},{'scheduled_realtime':True,'reference_context':{}},
    {'scheduled_realtime':True,'review_context':{}},{'scheduled_realtime':True,'daily_history':{}},
])
def test_other_intents_emit_no_diagnostic(payload,caplog):
    from app.market.collection_timings import CollectionTiming
    task,at=job(payload);trace=CollectionTiming(task,at);trace.bind('tencent_web')
    with caplog.at_level('INFO'):trace.finish(uuid.uuid4(),'complete',1,health_returned=True)
    assert observations(caplog)==[]


@pytest.mark.parametrize('family',['unknown-secret-family','cninfo_announcements','tushare_daily'])
def test_non_live_families_emit_no_diagnostic(family,caplog):
    from app.market.collection_timings import CollectionTiming
    task,at=job();trace=CollectionTiming(task,at);trace.bind(family)
    with caplog.at_level('INFO'):trace.finish(uuid.uuid4(),'complete',1,health_returned=True)
    assert observations(caplog)==[]


def test_callbacks_keep_exact_result_and_exception_and_logger_cannot_break_collection(monkeypatch,caplog):
    from app.market import collection_timings as module
    task,at=job();trace=module.CollectionTiming(task,at);trace.bind('tencent_web')
    sentinel=object()
    @trace.permission
    def returns():return trace.call('qt_gate',lambda:sentinel)
    assert returns() is sentinel
    error=RuntimeError('synthetic-private-error')
    @trace.permission
    def fails():
        def raise_original():raise error
        return trace.call('source_gate',raise_original)
    with pytest.raises(RuntimeError) as raised:fails()
    assert raised.value is error
    monkeypatch.setattr(module.log,'info',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('logger failed')))
    trace.finish(uuid.uuid4(),'error',0,health_returned=False)


def test_unknown_or_discontinuous_clocks_stay_missing_and_output_is_bounded(caplog):
    from app.market.collection_timings import CollectionTiming
    task,at=job(attempts=True,created_at=datetime(2026,11,13),available_at=at_placeholder())
    trace=CollectionTiming(task,at,clock=lambda:float('nan'));trace.bind('tencent_web')
    trace.storage_started(at+timedelta(seconds=1),at)
    trace.committed()
    with caplog.at_level('INFO'):trace.finish('synthetic-private-id','synthetic-private-status',True,health_returned=False)
    value=observations(caplog)[0]
    assert value['creation_to_entry_ms'] is None and value['ready_to_entry_ms'] is None
    assert value['attempt'] is None and value['requests'] is None and value['run_status'] is None
    assert value['run_tag'] is None and value['collector_ms'] is None
    assert value['receipt_to_storage_wall_ms'] is None
    assert len(json.dumps(value))<2048 and 'NaN' not in json.dumps(value) and 'synthetic-private' not in json.dumps(value)


def at_placeholder():return datetime(2026,11,13,6,30,tzinfo=UTC)+timedelta(seconds=1)


def test_unobserved_first_grant_cannot_borrow_a_later_grant_clock(caplog):
    from app.market.collection_timings import CollectionTiming
    task,at=job();ticks=iter([0,1,float('nan'),3,4,5,6,7])
    trace=CollectionTiming(task,at,clock=lambda:next(ticks));trace.bind('tencent_web')
    allowed=trace.permission(lambda:True)
    assert allowed() is True and allowed() is True
    trace.storage_started(at,at);trace.committed()
    with caplog.at_level('INFO'):trace.finish(uuid.uuid4(),'complete',2,health_returned=True)
    value=observations(caplog)[0]
    assert value['permit_calls']==2
    assert value['to_first_permit_ms'] is None and value['first_permit_to_storage_ms'] is None
