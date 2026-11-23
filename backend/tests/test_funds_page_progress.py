"""Long real page traversal advances progress; permits and leases never do."""
from datetime import timedelta
from types import SimpleNamespace
import threading
import uuid

import pytest
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db import engine
from app.models import Job, WorkerHeartbeat
from app.worker import record_page_progress
from app.worker_watchdog import ProgressWatchdog, progress_reporting, report_progress
from app.market import eastmoney_trading as m
from app.market.protocol import SourceFailure
from test_fund_universe import upstream, collect
from test_data_resilience import scenario, CLOCK
from test_funds_live_isolation import configured, task
from test_realtime_fairness import one_worker_round


def test_full_scan_exceeding_five_minutes_has_real_page_progress(upstream, monkeypatch):
    clock=[0.0]; events=[]; watch=ProgressWatchdog('realtime', clock=lambda:clock[0])
    original=m.fetch_https
    def slow(url):
        clock[0]+=6
        assert not watch.expired()
        return original(url)
    monkeypatch.setattr(m,'fetch_https',slow)
    def done(detail):
        events.append(detail);watch.advance()
    with progress_reporting(done):
        response,result,permits=collect()
    assert clock[0]==408 and len(events)==68 and len(permits)==68
    assert result.rejected==0 and [len(p.rows) for _,p,_ in result.records]==[5927,128,531]
    assert events[0]==dict(table='stock_funds',page=1,page_count=60,status='accepted',request_count=1)
    assert events[-1]==dict(table='concept_funds',page=6,page_count=6,status='accepted',request_count=68)
    clock[0]+=301
    assert watch.expired()


def test_inflight_http_and_permits_do_not_reset_watchdog(upstream,monkeypatch):
    clock=[0.0];events=[];watch=ProgressWatchdog('realtime',clock=lambda:clock[0])
    def stuck(url):
        assert not events
        clock[0]=301
        assert watch.expired()
        raise RuntimeError('synthetic blocked operation terminated')
    monkeypatch.setattr(m,'fetch_https',stuck)
    with progress_reporting(lambda d:(events.append(d),watch.advance())):
        with pytest.raises(RuntimeError,match='synthetic blocked'):
            collect()
    assert not events and watch.expired()


@pytest.mark.parametrize('reason',['rate_limited','permission_missing','lease_expired','configuration_changed'])
def test_denied_next_permit_never_announces_next_page(upstream,reason):
    events=[];calls=[]
    def permit():
        calls.append(True)
        if len(calls)==2:raise SourceFailure(reason,retry_after=900)
    with progress_reporting(events.append):
        with pytest.raises(SourceFailure,match=reason):
            m.fetch_rankings('eastmoney_funds',permit,full_boards=True)
    assert len(events)==1 and events[0]['page']==1
    assert upstream['calls']==[('stock',1)]


def test_completed_failed_page_is_progress_but_never_complete_coverage(upstream):
    upstream['fail']=('stock',2);events=[]
    with progress_reporting(events.append):response,result,permits=collect()
    assert len(events)==68 and events[1]['status']=='failed'
    assert result.rejected==1 and result.records[0][1].rows[0]['capture_complete']=='0'
    assert all(r['capture_complete']=='1' for _,p,_ in result.records[1:] for r in p.rows)


def test_progress_scope_does_not_leak_to_renewal_threads_or_next_job():
    events=[]
    with progress_reporting(events.append):
        t=threading.Thread(target=lambda:report_progress({'renewal':True}));t.start();t.join()
        report_progress({'page':1})
    report_progress({'next_job':True})
    assert events==[{'page':1}]


@pytest.mark.parametrize('lost',['token','expired','finished'])
def test_persisted_page_progress_is_fenced_by_current_lease(scenario,lost):
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        provider,_=configured(db,scenario)
        job=task(db,scenario,provider,state='running')
        job.result_ref={'existing':'preserved'}
    detail=dict(table='stock_funds',page=1,page_count=60,status='accepted',request_count=2)
    record_page_progress(job,'synthetic-page-worker',detail)
    with Session(engine()) as db,db.begin():
        row=db.get(Job,job.id);saved=dict(row.result_ref)
        beat=db.get(WorkerHeartbeat,'synthetic-page-worker');stamp=beat.progress_at
        assert saved['existing']=='preserved' and saved['funds_page_progress']['page']==1
        assert beat.job_id==job.id and saved['funds_page_progress']['completed_at']
        if lost=='token':row.lease_token=uuid.uuid4()
        elif lost=='expired':row.lease_until=CLOCK-timedelta(days=1)
        else:row.state='succeeded'
    with pytest.raises(SourceFailure,match='lease_expired'):
        record_page_progress(job,'synthetic-page-worker',dict(detail,page=2))
    with Session(engine()) as db:
        assert db.get(Job,job.id).result_ref==saved
        assert db.get(WorkerHeartbeat,'synthetic-page-worker').progress_at==stamp


def test_standard_realtime_worker_wires_actual_job_progress(monkeypatch):
    worker=one_worker_round(monkeypatch);events=[]
    job=SimpleNamespace(id=uuid.uuid4(),kind='source.collect',payload={'source_id':str(uuid.uuid4())})
    monkeypatch.setattr(worker,'claim',lambda role:job)
    monkeypatch.setattr(worker,'heartbeat',lambda *a,**k:None)
    monkeypatch.setattr(worker,'record_page_progress',lambda j,w,d:events.append((j.id,d)))
    monkeypatch.setattr(worker,'process',lambda j:report_progress({'page':1}))
    worker.main(['--role','realtime'])
    report_progress({'page':2})
    assert events==[(job.id,{'page':1})]


def test_standard_worker_process_completes_long_scan_and_preserves_terminal_state(scenario, upstream, monkeypatch):
    from app.market import collect as collector
    from app.market.models import DataFetchRun
    real_event=threading.Event
    from app import worker
    monkeypatch.setattr(worker.signal,'signal',lambda *args:None)
    monkeypatch.setattr(worker,'HEARTBEAT_ID',None)
    created=[]
    def events():
        event=real_event()
        created.append(event)
        return event
    clock=[0.0];advances=[]
    watch=ProgressWatchdog('realtime',clock=lambda:clock[0])
    monkeypatch.setattr('app.worker_watchdog.ProgressWatchdog',lambda role:SimpleNamespace(
        start=lambda:None,close=lambda:None,
        advance=lambda:(advances.append(clock[0]),watch.advance())))
    upstream['at']=CLOCK
    monkeypatch.setattr(collector,'datetime',m.datetime)
    monkeypatch.setattr(collector,'reserve_eastmoney_request',lambda job:None)
    original=m.fetch_https
    def slow(url):
        clock[0]+=6
        assert not watch.expired()
        return original(url)
    monkeypatch.setattr(m,'fetch_https',slow)
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        provider,_=configured(db,scenario)
        job=task(db,scenario,provider,state='running')
        job.result_ref={'existing':'preserved'}
    monkeypatch.setattr(worker,'claim',lambda role:job)
    actual_process=worker.process
    def process_once(current):
        try:actual_process(current)
        finally:created[0].set()
    monkeypatch.setattr(worker,'process',process_once)
    monkeypatch.setattr(worker.threading,'Event',events)
    worker.main(['--role','realtime'])
    assert clock[0]==408 and len(advances)>=68
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert saved.state=='succeeded' and saved.completed_at is not None
        assert saved.lease_token is None and saved.lease_until is None
        assert saved.result_ref['existing']=='preserved'
        assert saved.result_ref['funds_page_progress']['page']==6
        assert (run.status,run.accepted,run.rejected,run.request_count)==('complete',3,0,68)
        beat=db.get(WorkerHeartbeat,worker.HEARTBEAT_ID)
        assert beat.job_id==job.id and beat.progress_at is not None
