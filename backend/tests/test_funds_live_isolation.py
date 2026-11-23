"""Archive failures cannot impersonate or freeze current ranking collection."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.schemas import ReferencePayload
from app.market import budget, resilience
from app.market import fund_history as history
from app.market.collect import store_reference_batch
from app.market.fund_history import CAPABILITY, HISTORY_DATASET, PLAN_DATASET
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SourceFailure
from app.market.reference import ReferenceBatch
from app.market.resilience_models import DataHealth
from app.market.service import source_id
from app.market.minute_backfill import configuration_fingerprint
from app.models import AuditEvent, Job, Provider
from test_data_resilience import CLOCK, scenario, source
from test_intraday_funds import save


def configured(db, owner, family='eastmoney_funds'):
    provider, config = source(db, owner, family, symbols=[])
    config.interval_seconds = 150
    return provider, config


@pytest.mark.parametrize('kind', ['source.collect', 'funds.focus', 'funds.archive'])
def test_1200_local_permits_do_not_disable_next_request(scenario, monkeypatch, kind):
    check = CLOCK + timedelta(minutes=10)
    monkeypatch.setattr(budget, 'now', lambda db: check)
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        provider, _ = configured(db, scenario)
        for _ in range(1200):
            db.add(AuditEvent(actor_id=scenario, action=budget.EASTMONEY_ACTION,
                resource_type='provider', resource_id=provider.id,
                created_at=check-timedelta(minutes=1), changed_field_names=['synthetic_permit']))
        job = task(db, scenario, provider, kind=kind, state='running', at=check)
    budget.reserve_eastmoney_request(job)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == budget.EASTMONEY_ACTION)) == 1201


def test_local_release_preserves_original_receipt_and_other_supplier_wait(scenario):
    with Session(engine()) as db, db.begin():
        provider, _ = configured(db, scenario)
        job = task(db, scenario, provider)
        old = fetch_run(db, provider, job, reason='rate_limited', wait=CLOCK+timedelta(hours=8))
        real = fetch_run(db, provider, job, reason='rate_limited', wait=CLOCK+timedelta(minutes=5))
        db.add(AuditEvent(actor_id=scenario, action='source.local_budget_released',
            resource_type='data_fetch_run', resource_id=old.id,
            changed_field_names=['verified_legacy_local_daily_cap'], created_at=CLOCK))
        db.flush()
        assert budget.eastmoney_cooldown_until(db, CLOCK) == real.retry_not_before
        assert old.reason == 'rate_limited' and old.retry_not_before == CLOCK+timedelta(hours=8)


def task(db, owner, provider, *, kind='source.collect', at=CLOCK,
         state='succeeded', extra=None):
    job = Job(owner_id=owner, kind=kind, state=state,
        payload={'source_id':str(provider.id), **(extra or {})},
        available_at=at-timedelta(seconds=1), created_at=at,
        attempts=1 if state != 'queued' else 0,
        lease_token=uuid.uuid4() if state == 'running' else None,
        lease_until=datetime.now(UTC)+timedelta(hours=1) if state == 'running' else None,
        completed_at=at if state in {'succeeded','failed'} else None)
    db.add(job)
    db.flush()
    return job


def fetch_run(db, provider, job, *, at=CLOCK, reason=None, wait=None, history=False):
    row = DataFetchRun(source_id=provider.id, job_id=job.id,
        capability=CAPABILITY if history else 'market.eastmoney.funds',
        started_at=at-timedelta(milliseconds=20), received_at=at,
        status='error' if reason else 'complete', reason=reason,
        expected=1, accepted=0 if reason else 1, duplicates=0, rejected=0,
        request_count=1, retry_not_before=wait)
    db.add(row)
    db.flush()
    return row


def failed_archives(db, owner, provider, *, reason='transport_error'):
    rows=[]
    for offset in [-20,-10,0]:
        at=CLOCK+timedelta(seconds=offset)
        job=task(db,owner,provider,kind='funds.archive',at=at,state='failed',
            extra={'funds_history_context':{'day':CLOCK.date().isoformat()}})
        row=fetch_run(db,provider,job,at=at,reason=reason,history=True,
            wait=CLOCK+timedelta(minutes=15) if offset==0 else None)
        rows.append(row)
        db.add(AuditEvent(actor_id=owner,action=budget.EASTMONEY_ACTION,
            resource_type='job',resource_id=job.id,created_at=at,
            changed_field_names=['synthetic_request_permit']))
    db.flush()
    return rows


def test_three_history_transport_failures_preserve_backoff_and_live_permits(scenario,monkeypatch):
    check=CLOCK+timedelta(seconds=5)
    monkeypatch.setattr(budget,'now',lambda db:check)
    with Session(engine()) as db,db.begin():
        provider,config=configured(db,scenario)
        failures=failed_archives(db,scenario,provider)
        deadline=failures[-1].retry_not_before
        failed_id=failures[-1].id
        live=task(db,scenario,provider,state='running',at=check)
        archive=task(db,scenario,provider,kind='funds.archive',state='running',at=check,
            extra={'funds_history_context':{'day':CLOCK.date().isoformat()}})
        live_id,archive_id=live.id,archive.id
        assert budget.eastmoney_cooldown_until(db,check) is None
        assert budget.eastmoney_cooldown_until(db,check,job=archive)==deadline
    with Session(engine(),expire_on_commit=False) as db:
        live,archive=db.get(Job,live_id),db.get(Job,archive_id)
    budget.reserve_eastmoney_request(live)
    with pytest.raises(SourceFailure,match='rate_limited'):
        budget.reserve_eastmoney_request(archive)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action==budget.EASTMONEY_ACTION))==4
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==3
        assert db.get(DataFetchRun,failed_id).retry_not_before==deadline


@pytest.mark.parametrize('kind,family',[
    ('source.collect','eastmoney_funds'),('source.collect','eastmoney_boards'),
    ('funds.archive','eastmoney_funds')])
def test_actual_429_keeps_live_history_and_sibling_waiting(scenario,monkeypatch,kind,family):
    check=CLOCK+timedelta(seconds=5)
    monkeypatch.setattr(budget,'now',lambda db:check)
    with Session(engine()) as db,db.begin():
        provider,_=configured(db,scenario)
        failed_archives(db,scenario,provider,reason='rate_limited')
        selected=provider if family=='eastmoney_funds' else configured(db,scenario,family)[0]
        job=task(db,scenario,selected,kind=kind,state='running',at=check)
        ident=job.id
        assert budget.eastmoney_cooldown_until(db,check)==CLOCK+timedelta(minutes=15)
    with Session(engine(),expire_on_commit=False) as db:job=db.get(Job,ident)
    with pytest.raises(SourceFailure,match='rate_limited'):
        budget.reserve_eastmoney_request(job)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action==budget.EASTMONEY_ACTION))==3


@pytest.mark.parametrize('cause',['archive_transport','due_live_job'])
def test_repeated_local_archive_deferral_never_becomes_shared_throttling(scenario,monkeypatch,cause):
    check=CLOCK+timedelta(minutes=10)
    monkeypatch.setattr(budget,'now',lambda db:check)
    monkeypatch.setattr(history,'now',lambda db:check)
    monkeypatch.setattr('app.market.minute_backfill.calendar_open',lambda *a:None)
    monkeypatch.setattr(history,'fetch_https',lambda *a:pytest.fail('local deferral must not send HTTP'))
    with Session(engine()) as db,db.begin():
        provider,config=configured(db,scenario)
        save(db,[('BK0001','10')],CLOCK)
        live=task(db,scenario,provider)
        anchor=fetch_run(db,provider,live)
        plan=history._plan(db,provider,config,'concept',CLOCK.date(),check)
        target=plan.payload['rows'][0]
        context=dict(version=1,day=CLOCK.date().isoformat(),category='concept',code='BK0001',
            plan_id=str(plan.id),cohort_count=1,cohort_sha256=target['cohort_sha256'],exchange=None,
            configuration_sha256=configuration_fingerprint(provider,config),mode='intraday_recovery',
            recovery_anchor=str(anchor.id),recovery_until=(check-timedelta(minutes=1)).isoformat())
        if cause=='archive_transport':
            failed_archives(db,scenario,provider)
        else:
            live.state='queued'
            live.available_at=check
            for offset in [-30,-20]:
                db.add(AuditEvent(actor_id=scenario,action=budget.EASTMONEY_ACTION,
                    resource_type='provider',resource_id=provider.id,
                    created_at=check+timedelta(seconds=offset),changed_field_names=['synthetic_permit']))
        ident=provider.id
    for _ in range(2):
        with Session(engine(),expire_on_commit=False) as db,db.begin():
            provider=db.get(Provider,ident)
            archive=task(db,scenario,provider,kind='funds.archive',state='running',at=check,
                extra={'funds_history_context':context})
        history.process_job(archive)
        with Session(engine()) as db:
            queued=db.get(Job,archive.id)
            assert queued.state=='queued' and queued.attempts==0 and queued.lease_token is None
            assert budget.eastmoney_cooldown_until(db,check) is None
            assert db.scalar(select(func.count()).select_from(DataFetchRun).where(
                DataFetchRun.job_id==archive.id))==0
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        provider=db.get(Provider,ident)
        current=task(db,scenario,provider,state='running',at=check)
    budget.reserve_eastmoney_request(current)


@pytest.mark.parametrize('kind,extra',[
    ('funds.archive',{'funds_history_context':{}}),
    ('direction.archive',{}),('breadth.archive',{}),
    *[('source.collect',{key:{}}) for key in ('minute_context','reference_context',
        'daily_history','dated_context','full_minute_context','backfill_context',
        'funds_history_context','review_context')]])
def test_archive_receipts_never_become_latest_live_runs(scenario,kind,extra):
    with Session(engine()) as db,db.begin():
        provider,_=configured(db,scenario)
        live=task(db,scenario,provider,at=CLOCK-timedelta(seconds=30))
        actual=fetch_run(db,provider,live,at=CLOCK-timedelta(seconds=30))
        history=task(db,scenario,provider,kind=kind,at=CLOCK,extra=extra)
        fetch_run(db,provider,history,reason='transport_error',history=True,
            wait=CLOCK+timedelta(minutes=15))
        assert resilience.latest_run(db,provider.id,CLOCK).id==actual.id


@pytest.mark.parametrize('dataset',[HISTORY_DATASET,PLAN_DATASET])
def test_late_archive_does_not_replace_fresh_live_health_or_its_clocks(scenario,dataset):
    check=CLOCK+timedelta(seconds=10)
    with Session(engine()) as db,db.begin():
        provider,config=configured(db,scenario)
        for category,code in [('concept','BK0001'),('industry','BK0002'),('stock','600000')]:
            save(db,[(code,'10')],CLOCK,category=category)
        live=task(db,scenario,provider)
        actual=fetch_run(db,provider,live)
        archive=task(db,scenario,provider,kind='funds.archive',at=check,
            extra={'funds_history_context':{}})
        payload=ReferencePayload(dataset=dataset,title='合成历史参考',
            source_url='https://example.invalid/synthetic-history',
            attribution='synthetic only',scope='synthetic archive only',
            columns=[{'key':'code','label':'代码'},
                {'key':'source_time','label':'来源时点'}],
            rows=[{'code':'BK0001','source_time':(CLOCK-timedelta(minutes=30)).isoformat()}],
            observed_day=CLOCK.date())
        store_reference_batch(db,provider,config,ReferenceBatch(records=[
            ('synthetic-archive',payload,None)],expected=1),check)
        fetch_run(db,provider,archive,at=check,history=True)
        latest=resilience.latest_run(db,provider.id,check)
        assert latest.id==actual.id
        resilience.evaluate(db,provider,config,latest,check)
        health=db.scalar(select(DataHealth).where(DataHealth.primary_source_id==provider.id))
        assert health.state=='normal'
        assert health.last_run_id==actual.id
        assert health.last_valid_at==CLOCK and health.last_source_time==CLOCK


@pytest.mark.parametrize('missing',['concept','industry','stock'])
def test_one_fresh_fund_table_cannot_claim_all_categories_complete(scenario,missing):
    with Session(engine()) as db,db.begin():
        provider,config=configured(db,scenario)
        for category,code in [('concept','BK0001'),('industry','BK0002'),('stock','600000')]:
            if category != missing:
                save(db,[(code,'10')],CLOCK,category=category)
        live=task(db,scenario,provider)
        resilience.evaluate(db,provider,config,fetch_run(db,provider,live),CLOCK)
        health=db.scalar(select(DataHealth).where(DataHealth.primary_source_id==provider.id))
        assert health.state=='recovering' and health.reason=='coverage_partial'
        assert health.missing_count==1 and health.last_valid_at is None


def test_checker_repairs_old_history_wait_then_scheduler_queues_live(scenario,monkeypatch):
    from app.market import service
    check=CLOCK+timedelta(minutes=6)
    monkeypatch.setattr(resilience,'now',lambda db:check)
    monkeypatch.setattr(service,'now',lambda db:check)
    monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime',lambda *a:None)
    monkeypatch.setattr('app.market.intraday_schedule.deferred_intraday',lambda *a,**k:None)
    with Session(engine()) as db,db.begin():
        provider,config=configured(db,scenario)
        live=task(db,scenario,provider,at=CLOCK-timedelta(seconds=30))
        actual=fetch_run(db,provider,live,at=CLOCK-timedelta(seconds=30))
        actual_id=actual.id
        failed_archives(db,scenario,provider)
        config.next_run_at=check+timedelta(minutes=15)
        ident=provider.id
    resilience.reconcile_source(ident)
    with Session(engine()) as db:
        assert db.get(SourceSettings,ident).next_run_at==check
        assert resilience.latest_run(db,ident,check).id==actual_id
    service.schedule_sources()
    with Session(engine()) as db:
        pending=db.scalar(select(Job).where(Job.kind=='source.collect',Job.state=='queued'))
        assert pending and pending.payload['source_id']==str(ident)


def test_old_lease_cannot_acquire_a_permit_after_replacement(scenario,monkeypatch):
    monkeypatch.setattr(budget,'now',lambda db:CLOCK)
    with Session(engine()) as db,db.begin():
        provider,_=configured(db,scenario)
        live=task(db,scenario,provider,state='running')
        retired=SimpleNamespace(id=live.id,lease_token=uuid.uuid4())
    with pytest.raises(SourceFailure,match='lease_expired'):
        budget.reserve_eastmoney_request(retired)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action==budget.EASTMONEY_ACTION))==0


def test_current_source_lease_still_excludes_a_concurrent_slot(scenario):
    from app.worker import claim
    with Session(engine()) as db,db.begin():
        provider,_=configured(db,scenario)
        task(db,scenario,provider,kind='funds.archive',state='running')
        waiting=task(db,scenario,provider,state='queued')
        ident=waiting.id
    assert claim('realtime') is None
    with Session(engine()) as db:
        assert db.get(Job,ident).state=='queued'


@pytest.mark.parametrize('state',['queued','expired_running'])
def test_due_live_lease_wins_over_a_queued_archive(scenario,state):
    from app.worker import claim
    with Session(engine()) as db,db.begin():
        provider,_=configured(db,scenario)
        archive=task(db,scenario,provider,kind='funds.archive',state='queued')
        live=task(db,scenario,provider,state='running' if state=='expired_running' else 'queued')
        if state=='expired_running':live.lease_until=datetime.now(UTC)-timedelta(seconds=1)
        archive_id,live_id=archive.id,live.id
    assert claim('recovery') is None
    claimed=claim('realtime')
    assert claimed is not None and claimed.id==live_id
    with Session(engine()) as db:
        assert db.get(Job,archive_id).state=='queued'


def test_future_live_retry_does_not_block_archive_capacity(scenario):
    from app.worker import claim
    with Session(engine()) as db,db.begin():
        provider,_=configured(db,scenario)
        archive=task(db,scenario,provider,kind='funds.archive',state='queued')
        live=task(db,scenario,provider,state='queued')
        live.available_at=datetime.now(UTC)+timedelta(hours=1)
        archive_id,live_id=archive.id,live.id
    claimed=claim('recovery')
    assert claimed is not None and claimed.id==archive_id
    with Session(engine()) as db:
        assert db.get(Job,live_id).state=='queued'


def test_due_native_quote_precedes_another_source_in_its_execution_group(scenario):
    from app.worker import claim
    with Session(engine()) as db,db.begin():
        historical,_=source(db,scenario,'tdx_full_minute',symbols=[])
        live_source,_=source(db,scenario,'tdx_quotes')
        archive=task(db,scenario,historical,state='queued')
        live=task(db,scenario,live_source,state='queued')
        archive_id,live_id=archive.id,live.id
    assert claim('recovery') is None
    claimed=claim('realtime')
    assert claimed is not None and claimed.id==live_id
    with Session(engine()) as db:
        assert db.get(Job,archive_id).state=='queued'
