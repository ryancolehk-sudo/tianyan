"""Closed-day minute archives: pure parsing and disposable DB lineage tests."""
from datetime import UTC, date, datetime, timedelta
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import fund_history as history
from app.market.collect import store_reference_batch
from app.market.intraday_funds import intraday_funds
from app.market.models import SourceSettings,DataFetchRun
from app.market.protocol import SourceFailure,Response
from app.market.reference import ReferenceBatch
from app.market.service import source_id
from app.models import AuditEvent,Job,Provider,User
from test_intraday_funds import setup, save

DAY=date(2026,10,23)
CLOSE=datetime.fromisoformat('2026-10-23T17:00:00+08:00')
LIVE=CLOSE.replace(hour=13,minute=45)


def context(code='BK0001',category='concept',count=1,exchange=None):
    return dict(day=DAY.isoformat(),code=code,category=category,exchange=exchange,
        cohort_sha256='a'*64,cohort_count=count)


def body(code='BK0001',*,market=90,value='20',slots=None):
    slots=sorted(history.minute_slots(DAY)) if slots is None else slots
    return {'rc':0,'data':{'code':code,'market':market,'name':'合成历史',
        'klines':[f'{stamp:%Y-%m-%d %H:%M},{value},-1,-1,10,10' for stamp in slots]}}


def parse(value,ctx=None):
    return history.parse_history(json.dumps(value).encode(),CLOSE,ctx or context())


def test_parser_complete240_source_minutes_and_units_are_explicit():
    payload,complete=parse(body())
    assert complete and len(payload.rows)==240
    assert payload.rows[0]['source_time']=='2026-10-23T09:31:00+08:00'
    assert payload.rows[-1]['source_time']=='2026-10-23T15:00:00+08:00'
    assert next(c for c in payload.columns if c.key=='net_inflow_cny').unit=='CNY'
    assert all(r['archive_complete']=='true' for r in payload.rows)
    assert '插值' in payload.scope and payload.observed_day==DAY


def test_parser_partial_history_is_kept_without_claiming_complete():
    payload,complete=parse(body(slots=sorted(history.minute_slots(DAY))[:-1]))
    assert not complete and len(payload.rows)==239
    assert payload.rows[-1]['archive_complete']=='false'


@pytest.mark.parametrize('kind,reason',[
    ('other_code','instrument_mismatch'),('other_market','instrument_mismatch'),
    ('wrong_day','historical_response_day_mismatch'),('duplicate','historical_series_order'),
    ('out_of_order','historical_series_order'),('lunch','invalid_source_time'),
    ('nan','schema_changed'),('extra_field','schema_changed'),('missing_timestamp_date','schema_changed'),
    ('empty','historical_series_empty'),('api_error','upstream_unavailable')])
def test_parser_rejects_identity_date_shape_and_empty_errors(kind,reason):
    value=body()
    lines=value['data']['klines']
    if kind=='other_code':value['data']['code']='BK9999'
    elif kind=='other_market':value['data']['market']=0
    elif kind=='wrong_day':lines[0]=lines[0].replace('2026-10-23','2026-10-22')
    elif kind=='duplicate':lines[1]=lines[0]
    elif kind=='out_of_order':lines[0],lines[1]=lines[1],lines[0]
    elif kind=='lunch':lines[0]=lines[0].replace('09:31','12:00')
    elif kind=='nan':lines[0]=lines[0].replace(',20,',',NaN,')
    elif kind=='extra_field':lines[0]+=',0'
    elif kind=='missing_timestamp_date':lines[0]=lines[0].replace('2026-10-23 ','')
    elif kind=='empty':value['data']['klines']=[]
    elif kind=='api_error':value['rc']=1
    with pytest.raises(SourceFailure,match=reason):parse(value)


@pytest.mark.parametrize('clock,allowed', [('15:04:59',False),('15:05:00',True),
    ('21:29:59',True),('21:30:00',False),('10:00:00',False)])
def test_parser_window_never_permits_live_hours(clock,allowed):
    assert history.in_window(datetime.fromisoformat(f'2026-10-23T{clock}+08:00')) is allowed


@pytest.mark.parametrize('category,code,exchange,market',[
    ('concept','BK1184',None,90),('industry','BK0001',None,90),
    ('stock','600000','SSE',1),('stock','000001','SZSE',0),('stock','920001','BSE',0)])
def test_identity_route_is_fixed_and_preserves_saved_market(category,code,exchange,market):
    url=history.history_url(category,code,exchange)
    assert 'fflow/kline/get?' in url and f'secid={market}.{code}' in url
    assert '/clist/' not in url


@pytest.mark.parametrize('category,code,exchange',[
    ('concept','600000',None),('stock','000001','SSE'),('stock','600000','SZSE'),
    ('stock','920001','SSE'),('other','BK1184',None)])
def test_identity_never_guesses_a_conflicting_market(category,code,exchange):
    with pytest.raises(SourceFailure,match='historical_identity_unsupported'):
        history.history_url(category,code,exchange)


def create_plan(db,count=2):
    provider=db.get(Provider,source_id(history.FAMILY));config=db.get(SourceSettings,provider.id)
    save(db,[(f'BK{i:04d}',str(i*10)) for i in range(1,count+1)],LIVE)
    evidence=history._plan(db,provider,config,'concept',DAY,CLOSE)
    return provider,config,evidence


def store_history(db,provider,config,plan,code,value='50',*,received=CLOSE,complete=True):
    target=next(row for row in plan.payload['rows'] if row['code']==code)
    ctx={**context(code,count=len(plan.payload['rows'])),'cohort_sha256':target['cohort_sha256']}
    slots=sorted(history.minute_slots(DAY))
    payload,_=parse(body(code,value=value,slots=slots if complete else slots[:-1]),ctx)
    key=f'funds.history:{DAY}:concept:{code}'
    return store_reference_batch(db,provider,config,
        ReferenceBatch(records=[(key,payload,None)],expected=1),received)


def test_partial_cohort_only_supplements_curve_and_full_cohort_replaces_ranking(client):
    setup(client)
    with Session(engine()) as db,db.begin():
        provider,config,plan=create_plan(db)
        store_history(db,provider,config,plan,'BK0001')
    with Session(engine()) as db:
        partial=intraday_funds(db,DAY,CLOSE,session={'phase':'closed'})
        provider=db.get(Provider,source_id(history.FAMILY))
        groups=history.saved_archives(db,provider,DAY,CLOSE)
    assert not groups[0]['complete']
    assert len(partial.sectors)==2 and max(r.source_time for r in partial.sectors)==LIVE
    assert len([p for p in partial.points if p.sector_code=='BK0001.DC'])>=240
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(history.FAMILY));config=db.get(SourceSettings,provider.id)
        plan=history._plan(db,provider,config,'concept',DAY,CLOSE)
        store_history(db,provider,config,plan,'BK0002',value='100')
    manifest=[]
    with Session(engine()) as db:
        full=intraday_funds(db,DAY,CLOSE,session={'phase':'closed'},input_manifest=manifest)
        expected={str(row.id):row.content_hash for row in db.scalars(select(EvidenceVersion).where(
            EvidenceVersion.source_id==source_id(history.FAMILY),EvidenceVersion.kind=='reference'))}
    assert len(full.sectors)==2 and [r.code for r in full.sectors]==['BK0002.DC','BK0001.DC']
    assert full.latest_source_time.hour==15 and full.latest_source_time.minute==0
    assert all(row.change_5m_cny==0 for row in full.sectors)
    assert all(row.rank_change_5m==0 for row in full.sectors)
    assert len(full.points)==480
    # The original live table proves the frozen cohort. The scope manifest and
    # both actual responses remain dependencies; temporary minute anchors never
    # become made-up evidence identifiers.
    assert {item['evidence_id'] for item in manifest}==set(expected)
    assert all(str(uuid.UUID(item['evidence_id']))==item['evidence_id']
        and item['content_hash']==expected[item['evidence_id']] for item in manifest)


def test_late_archive_cannot_enter_an_earlier_receipt_cutoff(client):
    setup(client)
    with Session(engine()) as db,db.begin():
        provider,config,plan=create_plan(db,count=1)
        store_history(db,provider,config,plan,'BK0001',received=CLOSE+timedelta(seconds=1))
    with Session(engine()) as db:
        earlier=intraday_funds(db,DAY,CLOSE,session={'phase':'closed'})
        later=intraday_funds(db,DAY,CLOSE+timedelta(seconds=2),session={'phase':'closed'})
    assert earlier.latest_source_time==LIVE and len(earlier.points)==1
    assert later.latest_source_time.hour==15 and len(later.points)==240


@pytest.mark.parametrize('index_state', ['missing', 'stale'])
def test_fund_tables_and_archive_queries_fall_back_to_original_evidence(client, index_state):
    from sqlalchemy import delete, update
    from app.market.read_facets import EvidenceReadFacet

    setup(client)
    with Session(engine()) as db, db.begin():
        provider, config, plan = create_plan(db, count=1)
        store_history(db, provider, config, plan, 'BK0001')
    expected_inputs = []
    with Session(engine()) as db:
        expected = intraday_funds(db, DAY, CLOSE, session={'phase': 'closed'},
                                 input_manifest=expected_inputs).model_dump(mode='json')
    with Session(engine()) as db, db.begin():
        if index_state == 'missing':
            db.execute(delete(EvidenceReadFacet))
        else:
            db.execute(update(EvidenceReadFacet).values(content_hash='0' * 64,
                                                       dataset='synthetic-wrong-dataset', observed_day='1900-01-01'))
    actual_inputs = []
    with Session(engine()) as db:
        actual = intraday_funds(db, DAY, CLOSE, session={'phase': 'closed'},
                               input_manifest=actual_inputs).model_dump(mode='json')
    assert actual == expected and actual_inputs == expected_inputs
    assert len(actual['points']) == 240 and actual['sectors'][0]['change_5m_cny'] == 0


def test_scheduling_is_persistent_bounded_and_does_not_change_live_plan(client,monkeypatch):
    setup(client)
    monkeypatch.setattr(history,'settings',lambda:SimpleNamespace(data_collection_enabled=True))
    monkeypatch.setattr(history,'now',lambda db:CLOSE)
    monkeypatch.setattr('app.market.review_store.available_trade_days',lambda *a,**k:[DAY])
    with Session(engine()) as db,db.begin():
        provider,config,_=create_plan(db)
        original=config.next_run_at
    history.schedule_fund_archives();history.schedule_fund_archives()
    with Session(engine()) as db:
        jobs=list(db.scalars(select(Job).where(Job.kind=='funds.archive')))
        assert len(jobs)==2 and all(j.payload['funds_history_context']['cohort_count']==2 for j in jobs)
        assert db.get(SourceSettings,source_id(history.FAMILY)).next_run_at==original
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext==history.PLAN_DATASET))==1


def test_scheduler_requires_closed_verified_day_before_queuing(client,monkeypatch):
    setup(client)
    monkeypatch.setattr(history,'settings',lambda:SimpleNamespace(data_collection_enabled=True))
    monkeypatch.setattr(history,'now',lambda db:LIVE)
    monkeypatch.setattr('app.market.review_store.available_trade_days',lambda *a,**k:[DAY])
    history.schedule_fund_archives()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='funds.archive'))==0


def test_claimed_archive_crosses_real_session_boundaries_and_persists_response(client,monkeypatch):
    setup(client)
    monkeypatch.setattr(history,'settings',lambda:SimpleNamespace(data_collection_enabled=True))
    monkeypatch.setattr(history,'now',lambda db:CLOSE)
    monkeypatch.setattr('app.market.review_store.available_trade_days',lambda *a,**k:[DAY])
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls,tz=None):
            return CLOSE.astimezone(tz) if tz else CLOSE.replace(tzinfo=None)
    monkeypatch.setattr(history,'datetime',FrozenDateTime)
    with Session(engine()) as db,db.begin():
        provider,config,_=create_plan(db,count=1)
        original_next=config.next_run_at
    history.schedule_fund_archives()
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=db.scalar(select(Job).where(Job.kind=='funds.archive'))
        job.state='running';job.attempts=1;job.lease_token=uuid.uuid4()
        job.lease_until=datetime.now(UTC)+timedelta(hours=1)
    calls=[]
    def fetch(url):
        calls.append(url)
        return Response(json.dumps(body()).encode(),CLOSE,1,200)
    monkeypatch.setattr(history,'fetch_https',fetch)
    history.process_job(job)
    with Session(engine()) as db:
        assert db.get(Job,job.id).state=='succeeded'
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert run.status=='complete' and run.request_count==1 and run.accepted==1
        saved=db.scalar(select(EvidenceVersion).where(EvidenceVersion.payload['dataset'].astext==history.HISTORY_DATASET))
        assert len(saved.payload['rows'])==240
        assert db.get(SourceSettings,source_id(history.FAMILY)).next_run_at==original_next
    assert len(calls)==1 and 'fflow/kline/get' in calls[0]


@pytest.mark.parametrize('reason',['transport_error','timeout','upstream_unavailable'])
def test_three_upstream_disconnects_cool_only_archives_before_the_next_symbol(client,monkeypatch,reason):
    setup(client)
    clock=[CLOSE]
    monkeypatch.setattr(history,'settings',lambda:SimpleNamespace(data_collection_enabled=True))
    monkeypatch.setattr(history,'now',lambda db:clock[0])
    monkeypatch.setattr('app.market.budget.now',lambda db:clock[0])
    monkeypatch.setattr('app.market.review_store.available_trade_days',lambda *a,**k:[DAY])
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls,tz=None):return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(history,'datetime',FrozenDateTime)
    with Session(engine()) as db,db.begin():create_plan(db,count=4)
    history.schedule_fund_archives()
    def take():
        with Session(engine(),expire_on_commit=False) as db,db.begin():
            job=db.scalar(select(Job).where(Job.kind=='funds.archive',Job.state=='queued').order_by(Job.id).limit(1))
            job.state='running';job.attempts=1;job.lease_token=uuid.uuid4()
            job.lease_until=datetime.now(UTC)+timedelta(hours=1)
            return job
    calls=[]
    def unavailable(url):
        calls.append(url);raise SourceFailure(reason)
    monkeypatch.setattr(history,'fetch_https',unavailable)
    attempts=[]
    for _ in range(3):
        job=take();history.process_job(job);attempts.append(job)
        clock[0]+=timedelta(seconds=11)
    deferred_job=take();history.process_job(deferred_job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==attempts[-1].id))
        assert run.reason==reason and run.request_count==1
        assert run.retry_not_before==CLOSE+timedelta(seconds=22,minutes=15)
        assert all(db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id)).retry_not_before is None for job in attempts[:2])
        deferred=db.get(Job,deferred_job.id)
        assert deferred.state=='queued' and deferred.attempts==0
        assert deferred.available_at>=run.retry_not_before and deferred.lease_token is None
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==3
        from app.market.budget import eastmoney_cooldown_until
        assert eastmoney_cooldown_until(db,clock[0]) is None
        assert eastmoney_cooldown_until(db,clock[0],job=deferred_job)==run.retry_not_before
    assert len(calls)==3


def test_success_between_disconnections_breaks_the_shared_failure_streak(client,monkeypatch):
    setup(client)
    clock=[CLOSE]
    monkeypatch.setattr(history,'settings',lambda:SimpleNamespace(data_collection_enabled=True))
    monkeypatch.setattr(history,'now',lambda db:clock[0])
    monkeypatch.setattr('app.market.budget.now',lambda db:clock[0])
    monkeypatch.setattr('app.market.review_store.available_trade_days',lambda *a,**k:[DAY])
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls,tz=None):return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(history,'datetime',FrozenDateTime)
    with Session(engine()) as db,db.begin():create_plan(db,count=3)
    history.schedule_fund_archives()
    jobs=[]
    for index in range(3):
        with Session(engine(),expire_on_commit=False) as db,db.begin():
            job=db.scalar(select(Job).where(Job.kind=='funds.archive',Job.state=='queued').order_by(Job.id).limit(1))
            job.state='running';job.attempts=1;job.lease_token=uuid.uuid4()
            job.lease_until=datetime.now(UTC)+timedelta(hours=1)
        def fetch(url):
            if index!=1:raise SourceFailure('transport_error')
            return Response(json.dumps(body(job.payload['funds_history_context']['code'])).encode(),clock[0],1,200)
        monkeypatch.setattr(history,'fetch_https',fetch)
        history.process_job(job);jobs.append(job);clock[0]+=timedelta(seconds=11)
    with Session(engine()) as db:
        runs=[db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id)) for job in jobs]
        assert [run.status for run in runs]==['error','complete','error']
        assert all(run.retry_not_before is None for run in runs)


def observed_run(db,stamp,*,rows=None):
    provider=db.get(Provider,source_id(history.FAMILY))
    owner=db.get(SourceSettings,provider.id).owner_id
    if rows is not None:save(db,rows,stamp)
    job=Job(owner_id=owner,kind='source.collect',state='succeeded',attempts=1,
        payload={'source_id':str(provider.id)},created_at=stamp,completed_at=stamp)
    db.add(job);db.flush()
    run=DataFetchRun(source_id=provider.id,job_id=job.id,capability='market.eastmoney.funds',
        started_at=stamp,received_at=stamp,status='complete',accepted=1,duplicates=0,
        rejected=0,expected=1,request_count=1,elapsed_ms=1)
    db.add(run);db.flush()
    return run


def intraday_setup(client,monkeypatch,*,stamp='10:10:00',observed='10:00:00',calendar=True):
    """Only synthetic original observations/calendar; never a supplier call."""
    setup(client)
    clock=[datetime.fromisoformat(f'{DAY}T{stamp}+08:00')]
    monkeypatch.setattr(history,'settings',lambda:SimpleNamespace(data_collection_enabled=True))
    monkeypatch.setattr(history,'now',lambda db:clock[0])
    monkeypatch.setattr('app.market.budget.now',lambda db:clock[0])
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls,tz=None):return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(history,'datetime',FrozenDateTime)
    with Session(engine()) as db,db.begin():
        if calendar:
            from app.market.collect import store_calendar_batch
            from app.market.service import configure_source
            from app.market.schemas import SourceConfiguration
            from test_tushare import source
            owner=db.scalar(select(User).where(User.role=='admin'))
            for family,exchange in [('tushare_calendar_sse','SSE'),('tushare_calendar_szse','SZSE')]:
                provider=configure_source(db,owner,SourceConfiguration(**source(family)))
                store_calendar_batch(db,provider,db.get(SourceSettings,provider.id),
                    [(DAY,True)],clock[0]-timedelta(days=1),exchanges=(exchange,))
        previous=datetime.fromisoformat(f'{DAY}T{observed}+08:00')
        run=observed_run(db,previous,rows=[('BK0001','10')])
        anchor=run.id
    return clock,anchor


def take_recovery(clock):
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=db.scalar(select(Job).where(Job.kind=='funds.archive',Job.state=='queued')
            .order_by(Job.created_at,Job.id).limit(1))
        assert job is not None
        job.created_at=clock[0]
        job.state='running';job.attempts=1;job.lease_token=uuid.uuid4()
        # Lease fencing deliberately follows the real DB clock, independent of
        # the synthetic acquisition clock used for market-session tests.
        job.lease_until=datetime.now(UTC)+timedelta(hours=1)
        return job


def fetch_intraday(clock,calls):
    def fetch(url):
        calls.append(url)
        slots=[stamp for stamp in sorted(history.minute_slots(DAY)) if stamp<=clock[0]-timedelta(minutes=1)]
        return Response(json.dumps(body(slots=slots)).encode(),clock[0],1,200)
    return fetch


def test_intraday_restart_gap_queues_one_persisted_frozen_cohort(client,monkeypatch):
    clock,anchor=intraday_setup(client,monkeypatch)
    history.schedule_fund_archives();history.schedule_fund_archives()
    with Session(engine()) as db:
        jobs=list(db.scalars(select(Job).where(Job.kind=='funds.archive')))
        assert len(jobs)==1
        context=jobs[0].payload['funds_history_context']
        assert context['mode']=='intraday_recovery' and context['recovery_anchor']==str(anchor)
        assert context['recovery_until']=='2026-10-23T10:09:00+08:00'
        assert context['cohort_count']==1 and context['code']=='BK0001'
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action==history.ACTION,AuditEvent.resource_type=='funds_gap'))==1


@pytest.mark.parametrize('stamp,observed', [('10:03:00','10:00:00'),
    ('12:30:00','11:29:00'),('13:01:00','11:29:00'),('15:05:00','14:40:00')])
def test_normal_updates_and_scheduled_breaks_do_not_queue_recovery(client,monkeypatch,stamp,observed):
    intraday_setup(client,monkeypatch,stamp=stamp,observed=observed)
    history.schedule_fund_archives()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='funds.archive'))==0


def test_intraday_recovery_requires_actual_calendar_evidence(client,monkeypatch):
    intraday_setup(client,monkeypatch,calendar=False)
    history.schedule_fund_archives()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='funds.archive'))==0


def test_resumed_live_sample_does_not_hide_the_gap_that_preceded_it(client,monkeypatch):
    clock,anchor=intraday_setup(client,monkeypatch,stamp='10:11:00')
    with Session(engine()) as db,db.begin():
        observed_run(db,clock[0]-timedelta(minutes=1),rows=[('BK0001','20')])
    history.schedule_fund_archives()
    with Session(engine()) as db:
        job=db.scalar(select(Job).where(Job.kind=='funds.archive'))
        assert job is not None and job.payload['funds_history_context']['recovery_anchor']==str(anchor)
        assert job.payload['funds_history_context']['recovery_until']=='2026-10-23T10:09:00+08:00'


def test_queued_intraday_recovery_can_continue_during_lunch(client,monkeypatch):
    clock,_=intraday_setup(client,monkeypatch,stamp='11:29:00',observed='11:00:00')
    history.schedule_fund_archives()
    job=take_recovery(clock)
    clock[0]=clock[0].replace(hour=11,minute=30)
    calls=[]
    monkeypatch.setattr(history,'fetch_https',fetch_intraday(clock,calls))
    history.process_job(job)
    with Session(engine()) as db:
        queued=db.get(Job,job.id)
        assert queued.state=='succeeded' and queued.lease_token is None
        assert db.scalar(select(func.count()).select_from(DataFetchRun).where(DataFetchRun.capability==history.CAPABILITY))==1
        assert len(calls)==1


def test_four_successful_partial_recoveries_on_separate_gaps_do_not_exhaust_failure_budget(client,monkeypatch):
    clock,_=intraday_setup(client,monkeypatch)
    calls=[]
    monkeypatch.setattr(history,'fetch_https',fetch_intraday(clock,calls))
    completed=[];hashes={}
    for index in range(4):
        if index:
            clock[0]+=timedelta(minutes=20)
        history.schedule_fund_archives()
        job=take_recovery(clock)
        history.process_job(job)
        completed.append(job.id)
        with Session(engine()) as db,db.begin():
            assert db.get(Job,job.id).state=='succeeded'
            run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
            assert run.status=='partial' and run.request_count==1
            for evidence in db.scalars(select(EvidenceVersion).where(
                EvidenceVersion.payload['dataset'].astext==history.HISTORY_DATASET)):
                if evidence.id in hashes:assert hashes[evidence.id]==evidence.content_hash
                hashes[evidence.id]=evidence.content_hash
            observed_run(db,clock[0]+timedelta(minutes=1),rows=[('BK0001',str(30+index))])
    assert len(calls)==4 and len(hashes)==4
    with Session(engine()) as db:
        assert all(db.get(Job,identity).result_ref['complete'] is False for identity in completed)


def test_filled_gap_does_not_refetch_during_normal_updates(client,monkeypatch):
    clock,_=intraday_setup(client,monkeypatch)
    calls=[]
    monkeypatch.setattr(history,'fetch_https',fetch_intraday(clock,calls))
    history.schedule_fund_archives()
    job=take_recovery(clock);history.process_job(job)
    with Session(engine()) as db,db.begin():
        for offset in range(1,16,2):
            observed_run(db,clock[0]+timedelta(minutes=offset),rows=[('BK0001',str(20+offset))])
    clock[0]+=timedelta(minutes=16)
    # Persisted gap intent is still checked, but its required source minutes
    # already exist. A stale outer poll cannot replay the whole cohort again.
    history.schedule_fund_archives()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='funds.archive'))==1
    assert len(calls)==1


def test_intraday_response_with_other_day_is_rejected_without_altering_old_evidence(client,monkeypatch):
    clock,_=intraday_setup(client,monkeypatch)
    history.schedule_fund_archives();job=take_recovery(clock)
    with Session(engine()) as db:
        original={row.id:row.content_hash for row in db.scalars(select(EvidenceVersion))}
    def wrong_day(url):
        value=body(slots=[datetime.fromisoformat('2026-10-22T10:00:00+08:00')])
        return Response(json.dumps(value).encode(),clock[0],1,200)
    monkeypatch.setattr(history,'fetch_https',wrong_day)
    history.process_job(job)
    with Session(engine()) as db:
        failed=db.get(Job,job.id)
        assert failed.state=='failed' and failed.error_code=='historical_response_day_mismatch'
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext==history.HISTORY_DATASET))==0
        assert all(db.get(EvidenceVersion,identity).content_hash==digest for identity,digest in original.items())
