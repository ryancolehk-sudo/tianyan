"""Isolated PG schema recovery and physical accounting; no supplier requests."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func,select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import kpl_top, kpl_atlas_budget as budget
from app.market.auction_schedule import schema_recheck,next_collection
from app.market.collect import collect_job
from app.market.models import DataFetchRun,SourceSettings
from app.market.protocol import Response,SourceFailure
from app.market.service import schedule_sources,source_id
from app.models import AuditEvent,Job
from app.worker import claim
from test_direction_recovery import setup,AT,at
from test_kpl_fast_top import body


def order_failure():
    value=json.loads(body(day=AT.date(),size=30))
    value['list'][0],value['list'][1]=value['list'][1],value['list'][0]
    return json.dumps(value).encode()


def order_diagnostic():
    with pytest.raises(SourceFailure) as failed:
        kpl_top.parse(order_failure(),AT,expected_day=AT.date())
    return failed.value.direction_diagnostic


@pytest.mark.parametrize('change',[{}, {'checkpoint':'kpl_first_page_size'}, {'row_widths':[18]},
    {'source_error_code':'1'}, {'source_total_count':'401'}, {'family':'direction_kpl_strength'},
    {'top_strength_order':{'head_descending':True,'head_dominates_tail':True}}, {'version':True}])
def test_only_validated_native_order_failure_gets_short_local_retry(change):
    from app.market.kpl_order_recovery import interval_seconds
    diagnostic={**order_diagnostic(),**change}
    assert interval_seconds(kpl_top.FAMILY,10,diagnostic)==(30 if not change else 900)
    assert interval_seconds('direction_kpl_strength',10,diagnostic)==900
    assert interval_seconds(kpl_top.FAMILY,120,order_diagnostic())==120


@pytest.mark.parametrize('clock,expected',[
    ('2026-10-26T10:00:00','2026-10-26T10:00:30'),
    ('2026-10-26T11:29:50','2026-10-26T13:00:00'),
    ('2026-10-26T15:04:50','2026-10-27T09:15:00')])
def test_short_order_retry_respects_lunch_and_receipt_end(clock,expected):
    assert next_collection(kpl_top.FAMILY,at(clock),10,schema_probe=True,
        probe_diagnostic=order_diagnostic())==at(expected)


def test_native_order_failure_stays_unpublished_then_strictly_recovers_after_thirty_seconds(client,monkeypatch):
    clock=setup(client,monkeypatch,kpl_top.FAMILY)
    monkeypatch.setattr(budget,'now',lambda db:clock[0])
    with Session(engine()) as db,db.begin():db.get(SourceSettings,source_id(kpl_top.FAMILY)).interval_seconds=10
    replies=[order_failure(),body(day=AT.date(),size=30)]
    calls=[]
    def fetch(*args,**kwargs):
        calls.append(clock[0]);return Response(replies.pop(0),clock[0],1,200)
    monkeypatch.setattr('app.market.direction_sources.fetch',fetch)
    schedule_sources();first=claim('realtime',fast_only=True);assert first
    collect_job(first)
    with Session(engine()) as db:
        config=db.get(SourceSettings,source_id(kpl_top.FAMILY))
        assert config.paused_reason=='schema_changed' and config.next_run_at==AT+timedelta(seconds=30)
        assert not list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==config.source_id)))
    clock[0]=AT+timedelta(seconds=29)
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(kpl_top.FAMILY)).next_run_at=AT-timedelta(seconds=1)
    schedule_sources();assert claim('realtime',fast_only=True) is None
    with Session(engine()) as db:
        assert db.get(SourceSettings,source_id(kpl_top.FAMILY)).next_run_at==AT+timedelta(seconds=30)
    clock[0]=AT+timedelta(seconds=30)
    schedule_sources();second=claim('realtime',fast_only=True);assert second
    assert second.payload['schema_probe'] is True
    collect_job(second)
    with Session(engine()) as db:
        config=db.get(SourceSettings,source_id(kpl_top.FAMILY))
        assert calls==[AT,AT+timedelta(seconds=30)]
        assert config.paused_reason is None and config.next_run_at==clock[0]+timedelta(seconds=10)
        assert len(list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==config.source_id))))==1


def test_newer_physical_outcome_and_shared_supplier_deadline_override_old_order_hint(client,monkeypatch):
    from app.market.kpl_order_recovery import latest_diagnostic,interval_seconds
    from app.models import Provider
    clock=setup(client,monkeypatch,kpl_top.FAMILY)
    monkeypatch.setattr(budget,'now',lambda db:clock[0])
    monkeypatch.setattr('app.market.direction_sources.fetch',lambda *args,**kwargs:Response(order_failure(),clock[0],1,200))
    with Session(engine()) as db,db.begin():db.get(SourceSettings,source_id(kpl_top.FAMILY)).interval_seconds=10
    schedule_sources();job=claim('realtime',fast_only=True);assert job;collect_job(job)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(kpl_top.FAMILY))
        assert interval_seconds(provider.family,10,latest_diagnostic(db,provider,AT))==30
        db.add(DataFetchRun(source_id=provider.id,capability='market.direction',started_at=AT+timedelta(seconds=1),
            received_at=AT+timedelta(seconds=1),status='error',reason='rate_limited',request_count=1))
        db.flush()
        assert interval_seconds(provider.family,10,latest_diagnostic(db,provider,AT))==30
        assert latest_diagnostic(db,provider,AT+timedelta(seconds=1)) is None
        db.get(SourceSettings,provider.id).legacy_cooldown_until=AT+timedelta(days=2)
    clock[0]=AT+timedelta(seconds=31)
    schedule_sources();assert claim('realtime',fast_only=True) is None
    with Session(engine()) as db:
        assert db.get(SourceSettings,source_id(kpl_top.FAMILY)).legacy_cooldown_until==AT+timedelta(days=2)
        assert budget.cooldown_until(db,source_id(kpl_top.FAMILY),clock[0])==AT+timedelta(days=2)
    # Ordinary schema pacing expires first; the final physical guard must still
    # refuse HTTP until the separately persisted supplier deadline.
    clock[0]=AT+timedelta(seconds=901)
    monkeypatch.setattr('app.market.direction_sources.fetch',lambda *args,**kwargs:pytest.fail('Supplier cooldown spent HTTP'))
    schedule_sources();waiting=claim('realtime',fast_only=True);assert waiting
    collect_job(waiting)
    with Session(engine()) as db:
        assert db.get(SourceSettings,source_id(kpl_top.FAMILY)).next_run_at>=AT+timedelta(days=2)


def test_fast_probe_keeps_lunch_and_fifteen_minute_boundary():
    assert schema_recheck(kpl_top.FAMILY,'schema_changed')
    for reason in ('permission_missing','decoder_changed','endpoint_not_allowed','response_too_large','upstream_redirect'):
        assert not schema_recheck(kpl_top.FAMILY,reason)
    assert next_collection(kpl_top.FAMILY,at('2026-10-26T10:00:00'),10,schema_probe=True)==at('2026-10-26T10:15:00')
    assert next_collection(kpl_top.FAMILY,at('2026-10-26T11:20:00'),10,schema_probe=True)==at('2026-10-26T13:00:00')
    assert next_collection(kpl_top.FAMILY,at('2026-10-26T14:55:00'),10,schema_probe=True)==at('2026-10-27T09:15:00')


def test_diagnostic_keeps_shape_and_count_without_body_or_arbitrary_message():
    value=json.loads(body(count=241,size=10));value['message']='private-upstream-message-42'
    with pytest.raises(SourceFailure,match='schema_changed') as failed:
        kpl_top.parse(json.dumps(value).encode(),AT,expected_day=AT.date())
    diag=failed.value.direction_diagnostic
    assert diag['family']==kpl_top.FAMILY and diag['checkpoint']=='kpl_first_page_size'
    assert diag['source_total_count']=='241' and diag['row_count']==10 and diag['row_widths']==[19]
    assert 'private-upstream-message-42' not in json.dumps(diag) and '801000' not in json.dumps(diag)


@pytest.mark.parametrize('valid',[True,False])
def test_only_strict_single_page_probe_recovers_and_restart_does_not_duplicate(client,monkeypatch,valid):
    clock=setup(client,monkeypatch,kpl_top.FAMILY)
    monkeypatch.setattr(budget,'now',lambda db:clock[0])
    with Session(engine()) as db,db.begin():db.get(SourceSettings,source_id(kpl_top.FAMILY)).interval_seconds=10
    schedule_sources();job=claim('realtime',fast_only=True)
    assert job and job.payload.get('schema_probe') is True
    with Session(engine()) as db,db.begin():db.get(SourceSettings,source_id(kpl_top.FAMILY)).next_run_at=AT-timedelta(seconds=1)
    schedule_sources();assert claim('realtime',fast_only=True) is None
    calls=[]
    def request(*args,**kwargs):
        calls.append(1);return Response(body(day=AT.date(),size=30 if valid else 10),AT,1,200)
    monkeypatch.setattr('app.market.direction_sources.fetch',request)
    collect_job(job)
    with Session(engine()) as db:
        cfg=db.get(SourceSettings,source_id(kpl_top.FAMILY));saved=db.get(Job,job.id)
        runs=list(db.scalars(select(DataFetchRun).where(DataFetchRun.source_id==cfg.source_id)))
        assert len(calls)==len(runs)==1 and runs[0].request_count==1
        assert cfg.paused_reason==(None if valid else 'schema_changed')
        assert cfg.next_run_at==AT+timedelta(seconds=10 if valid else 900)
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.source_id==cfg.source_id))==(1 if valid else 0)
        if not valid:assert saved.result_ref['direction_diagnostic']['checkpoint']=='kpl_first_page_size'


@pytest.mark.parametrize('boundary',['cooldown','spacing'])
def test_local_wait_is_zero_http_and_does_not_create_supplier_cooldown(client,monkeypatch,boundary):
    clock=setup(client,monkeypatch,kpl_top.FAMILY)
    monkeypatch.setattr(budget,'now',lambda db:clock[0])
    monkeypatch.setattr(budget.time,'sleep',lambda seconds:None)
    schedule_sources();job=claim('realtime',fast_only=True)
    assert job and budget.reserve(job)
    deadline=AT+timedelta(days=2)
    if boundary=='cooldown':
        with Session(engine()) as db,db.begin():db.get(SourceSettings,source_id(kpl_top.FAMILY)).legacy_cooldown_until=deadline
    with pytest.raises(SourceFailure,match='local_request_wait') as failed:budget.reserve(job)
    assert failed.value.request_count==0
    budget.record_failure(job,failed.value)
    with Session(engine()) as db:
        assert not list(db.scalars(select(DataFetchRun)))
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action=='source.retry_after.kpl'))==0
        assert budget.cooldown_until(db,source_id(kpl_top.FAMILY),AT)==(deadline if boundary=='cooldown' else None)
