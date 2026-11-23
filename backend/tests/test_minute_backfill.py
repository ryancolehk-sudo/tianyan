"""Synthetic bounded backfill on disposable PostgreSQL; zero vendor requests.

Real DB clocks, lease fencing and persisted permits remain active. These tests
verify the contract, not supplier coverage, final candles or a real outage.
"""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import EvidenceInput
from app.market.collect import store_calendar_batch, store_minute_batch
from app.market.datasets import bars
from app.market.minute import parse_sina_minutes
from app.market.minute_backfill import LOOKBACK_DAYS, calendar_open, fetch_run_dto, live_run_condition
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import InstrumentKey, Response, SHANGHAI, SourceFailure
from app.market.service import enqueue_collection, schedule_sources, source_id, source_states
from app.models import AuditEvent, Job, Provider
from app.worker import claim, process
from test_data_sources import configure, login, configuration as source_configuration
from test_minute_collection import configuration
from test_evidence import finish_snapshot, snapshot

FAMILY = 'sina_minute_1m'
KEY = InstrumentKey('SSE', '603278', 'stock')


def setup(client, monkeypatch, *, opened=True):
    headers = login(client)
    configure(client, headers, configuration(), key='backfill-minute')
    calendar=source_configuration('baostock_daily');calendar['interval_seconds']=21600
    configure(client, headers, calendar, key='backfill-calendar')
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    day = datetime.now(UTC).astimezone(SHANGHAI).date() - timedelta(days=1)
    if opened is not None:
        seed_calendar(day, opened)
    return headers, day


def seed_calendar(day, opened, *, family='baostock_daily', exchange='SSE', received=None):
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(family))
        config = db.get(SourceSettings, provider.id)
        store_calendar_batch(db, provider, config, [(day, opened)], received or datetime.now(UTC), exchanges=(exchange,))


def configure_calendar(client, headers, family):
    calendar=source_configuration(family);calendar['interval_seconds']=21600
    configure(client,headers,calendar,key='backfill-calendar-'+family)


def body(day, slots=(('09:31', '11'), ('09:32', '12'))):
    return ('=(' + json.dumps([dict(day=str(day)+' '+stamp+':00', open='10', high='13', low='9',
        close=close, volume='123', amount=None) for stamp, close in slots]) + ');').encode()


def enqueue(day):
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        return enqueue_collection(db, source_id(FAMILY), day=day).id


def refresh(client, headers, day):
    return client.post('/api/v1/data-sources/'+str(source_id(FAMILY))+'/refresh',
        json={} if day is None else {'day':str(day)}, headers={**headers,'Idempotency-Key':uuid.uuid4().hex})


def fake_fetch(monkeypatch, raw, after=None):
    calls = []
    def fetch(url):
        calls.append(url)
        if after:
            after()
        return Response(raw, datetime.now(UTC), 1, 200)
    monkeypatch.setattr('app.market.collect.fetch_https', fetch)
    return calls


def store_window(day, slots, *, backfill=False):
    received = datetime.now(UTC)
    batch = parse_sina_minutes(body(day, slots), KEY, '1m', received, day)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(FAMILY))
        result = store_minute_batch(db, provider, db.get(SourceSettings, provider.id), batch, received,
            backfill=backfill)
    return result, batch


def saved():
    with Session(engine()) as db:
        return [(row.id, row.content_hash, row.version_number, row.source_key, row.payload)
            for row in db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id(FAMILY),
                EvidenceVersion.kind=='bar').order_by(EvidenceVersion.source_key, EvidenceVersion.version_number))]


@pytest.mark.parametrize('offset,reason', [(1,'future_day'), (-8,'backfill_day_out_of_range')])
def test_day_bound_rejects_before_job_or_http(client, monkeypatch, offset, reason):
    headers, _ = setup(client, monkeypatch)
    calls = fake_fetch(monkeypatch, b'=([]);')
    today = datetime.now(UTC).astimezone(SHANGHAI).date()
    response = refresh(client, headers, today+timedelta(days=offset))
    assert response.status_code == 422 and response.json()['error']['code'] == reason
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='source.collect')) == 0
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 0
    assert calls == [] and LOOKBACK_DAYS == 7


@pytest.mark.parametrize('opened,reason', [(False,'backfill_day_closed'), (None,'calendar_not_ready')])
def test_closed_or_unknown_calendar_never_uses_weekday_guess(client, monkeypatch, opened, reason):
    headers, day = setup(client, monkeypatch, opened=opened)
    calls = fake_fetch(monkeypatch, body(day))
    response = refresh(client, headers, day)
    assert response.status_code == 422 and response.json()['error']['code'] == reason
    assert calls == []


def test_calendar_rights_revocation_prevents_backfill(client, monkeypatch):
    headers, day = setup(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('baostock_daily'))
        provider.rights = {**provider.rights, 'cache':'denied'}
    response = refresh(client, headers, day)
    assert response.status_code == 422 and response.json()['error']['code']=='calendar_not_ready'


def test_calendar_correction_closed_wins_over_old_open_version(client,monkeypatch):
    headers,day=setup(client,monkeypatch)
    seed_calendar(day,False)
    response=refresh(client,headers,day)
    assert response.status_code==422 and response.json()['error']['code']=='backfill_day_closed'


@pytest.mark.parametrize('paid_open,free_open',[(False,True),(True,False)])
@pytest.mark.parametrize('exchange',['SSE','SZSE'])
def test_paid_calendar_precedes_newer_free_receipt_in_both_directions(client,monkeypatch,paid_open,free_open,exchange):
    headers,day=setup(client,monkeypatch,opened=None)
    family='tushare_calendar_sse' if exchange=='SSE' else 'tushare_calendar_szse'
    configure_calendar(client,headers,family)
    clock=datetime.now(UTC)-timedelta(minutes=1)
    seed_calendar(day,paid_open,family=family,exchange=exchange,received=clock-timedelta(seconds=20))
    seed_calendar(day,free_open,exchange=exchange,received=clock-timedelta(seconds=10))
    with Session(engine()) as db:
        if paid_open:
            assert calendar_open(db,day,exchange,clock) is None
        else:
            with pytest.raises(SourceFailure,match='backfill_day_closed'):
                calendar_open(db,day,exchange,clock)


@pytest.mark.parametrize('revoke',['cache','display','entitlement','enabled'])
def test_revoked_paid_calendar_can_fall_back_to_permitted_free_calendar(client,monkeypatch,revoke):
    headers,day=setup(client,monkeypatch,opened=True)
    configure_calendar(client,headers,'tushare_calendar_sse')
    seed_calendar(day,False,family='tushare_calendar_sse')
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_calendar_sse'))
        if revoke in {'cache','display'}:provider.rights={**provider.rights,revoke:'denied'}
        elif revoke=='enabled':provider.enabled=False
        else:provider.entitlement_status='denied'
    assert refresh(client,headers,day).status_code==202
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('baostock_daily'))
        provider.rights={**provider.rights,'cache':'denied'}
    with Session(engine()) as db:
        with pytest.raises(SourceFailure,match='calendar_not_ready'):
            calendar_open(db,day,'SSE',datetime.now(UTC))


def test_paid_missing_target_day_uses_free_but_future_receipt_cannot_override_known_day(client,monkeypatch):
    headers,day=setup(client,monkeypatch,opened=None)
    configure_calendar(client,headers,'tushare_calendar_sse')
    clock=datetime.now(UTC)-timedelta(minutes=1)
    seed_calendar(day-timedelta(days=1),False,family='tushare_calendar_sse',received=clock-timedelta(seconds=20))
    seed_calendar(day,True,received=clock-timedelta(seconds=10))
    seed_calendar(day,False,family='tushare_calendar_sse',received=clock+timedelta(seconds=10))
    with Session(engine()) as db:
        assert calendar_open(db,day,'SSE',clock) is None
        with pytest.raises(SourceFailure,match='backfill_day_closed'):
            calendar_open(db,day,'SSE',clock+timedelta(seconds=10))


def test_same_paid_source_latest_version_obeys_inclusive_received_knowledge_boundary(client,monkeypatch):
    headers,day=setup(client,monkeypatch,opened=None)
    configure_calendar(client,headers,'tushare_calendar_sse')
    clock=datetime.now(UTC)-timedelta(minutes=1)
    seed_calendar(day,True,family='tushare_calendar_sse',received=clock-timedelta(seconds=10))
    seed_calendar(day,False,family='tushare_calendar_sse',received=clock)
    # Even a later free observation must not resurrect the earlier paid open day.
    seed_calendar(day,True,received=clock+timedelta(seconds=1))
    with Session(engine()) as db:
        assert calendar_open(db,day,'SSE',clock-timedelta(microseconds=1)) is None
        for cutoff in (clock,clock+timedelta(seconds=2)):
            with pytest.raises(SourceFailure,match='backfill_day_closed'):
                calendar_open(db,day,'SSE',cutoff)


def test_invalid_paid_boolean_does_not_silently_fall_back_or_resurrect_old_version(client,monkeypatch):
    headers,day=setup(client,monkeypatch,opened=None)
    configure_calendar(client,headers,'tushare_calendar_sse')
    clock=datetime.now(UTC)-timedelta(minutes=1)
    seed_calendar(day,True,family='tushare_calendar_sse',received=clock-timedelta(seconds=10))
    seed_calendar(day,True,received=clock+timedelta(seconds=1))
    # Defensive read of an immutable malformed legacy/external row. Construct a
    # fresh version, never UPDATE old evidence or bypass an immutable trigger.
    with Session(engine()) as db,db.begin():
        old=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id('tushare_calendar_sse')))
        invalid={**old.payload,'is_open':'yes'}
        valid=EvidenceInput(source_id=old.source_id,source_key=old.source_key,source_revision=content_hash(invalid),
            kind='calendar',payload=old.payload,rights_basis=old.rights_basis,supersedes_id=old.id)
        raw=valid.model_dump(exclude={'payload','published_at','authorship'})
        digest_body=valid.model_dump();digest_body['payload']=invalid
        db.add(EvidenceVersion(**raw,payload=invalid,published_at=None,owner_id=None,visibility='shared',
            version_number=old.version_number+1,received_at=clock,available_at=clock,availability='known',
            provenance='source_reported',historical_eligible=True,content_hash=content_hash(digest_body)))
    with Session(engine()) as db:
        with pytest.raises(SourceFailure,match='calendar_not_ready'):
            calendar_open(db,day,'SSE',clock+timedelta(seconds=2))


def test_calendar_family_and_payload_exchange_must_both_match(client,monkeypatch):
    headers,day=setup(client,monkeypatch,opened=None)
    configure_calendar(client,headers,'tushare_calendar_szse')
    seed_calendar(day,True,family='tushare_calendar_szse',exchange='SSE')
    seed_calendar(day,True,exchange='SZSE')
    with Session(engine()) as db:
        with pytest.raises(SourceFailure,match='calendar_not_ready'):
            calendar_open(db,day,'SSE',datetime.now(UTC))
        assert calendar_open(db,day,'SZSE',datetime.now(UTC)) is None
        with pytest.raises(SourceFailure,match='calendar_not_ready'):
            calendar_open(db,day,'SSE',datetime.now())


def test_fetch_run_context_keeps_daily_history_and_minute_backfill_metadata_and_live_scope(client,monkeypatch):
    _,day=setup(client,monkeypatch)
    clock=datetime.now(UTC)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(FAMILY))
        payloads=[{'source_id':str(config.source_id)},
            {'source_id':str(config.source_id),'minute_context':{'acquisition_mode':'backfill','requested_day':str(day)}},
            {'source_id':str(config.source_id),'daily_history':{'instrument':dict(exchange='SSE',symbol='603278',asset_type='stock'),
                'start_day':str(day-timedelta(days=365)),'end_day':str(day)}}]
        jobs=[Job(owner_id=config.owner_id,kind='source.collect',payload=payload) for payload in payloads]
        db.add_all(jobs);db.flush()
        runs=[DataFetchRun(source_id=config.source_id,capability='bar.minute',job_id=job.id,
            started_at=clock,received_at=clock,status='complete',expected=1,accepted=1,rejected=0,duplicates=0,
            elapsed_ms=1,request_count=1,cost_cny=None,body_hash=None) for job in jobs]
        db.add_all(runs);db.flush()
        ordinary,minute,daily=[fetch_run_dto(db,run) for run in runs]
        assert ordinary.acquisition_mode=='observed' and ordinary.requested_day is None
        assert minute.acquisition_mode=='backfill' and minute.requested_day==day
        assert daily.acquisition_mode=='daily_history' and daily.requested_instrument_code=='603278.SH'
        assert daily.requested_start_day==day-timedelta(days=365) and daily.requested_end_day==day
        assert daily.job_id==jobs[2].id and daily.id==runs[2].id
        live=list(db.scalars(select(DataFetchRun.id).where(live_run_condition())))
        assert live==[runs[0].id]


def test_seven_day_boundary_is_allowed_without_promising_actual_coverage(client,monkeypatch):
    headers,_=setup(client,monkeypatch)
    day=datetime.now(UTC).astimezone(SHANGHAI).date()-timedelta(days=7)
    seed_calendar(day,True)
    assert refresh(client,headers,day).status_code==202


def test_same_intent_is_idempotent_but_other_day_and_observed_conflict(client, monkeypatch):
    headers, day = setup(client, monkeypatch)
    other = day-timedelta(days=1)
    seed_calendar(other, True)
    first = refresh(client, headers, day)
    second = refresh(client, headers, day)
    assert first.status_code == second.status_code == 202
    assert first.json()['data']['job_id'] == second.json()['data']['job_id']
    assert first.json()['data']['requested_day'] == str(day)
    assert first.json()['data']['acquisition_mode'] == 'backfill'
    for request_day in (other, None):
        result = refresh(client, headers, request_day)
        assert result.status_code == 409 and result.json()['error']['code']=='collection_intent_conflict'
    with Session(engine()) as db:
        job = db.scalar(select(Job).where(Job.kind=='source.collect'))
        ctx = job.payload['minute_context']
        assert ctx['missing_only'] is True and ctx['interval']=='1m' and ctx['adjustment']=='unadjusted'
        assert ctx['instrument']==dict(exchange='SSE',symbol='603278',asset_type='stock')
        assert len(ctx['configuration_sha256'])==64


def test_concurrent_same_intent_creates_only_one_pending_job(client, monkeypatch):
    _, day = setup(client, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(enqueue, [day, day]))
    assert ids[0] == ids[1]
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='source.collect'))==1


def test_pending_backfill_does_not_abort_scheduler_or_move_its_live_plan(client,monkeypatch):
    _,day=setup(client,monkeypatch)
    ident=enqueue(day)
    due=datetime.now(UTC)-timedelta(minutes=1)
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(FAMILY)).next_run_at=due
        db.get(SourceSettings,source_id('baostock_daily')).next_run_at=due
    # This test isolates queue intent handling. Trading-day gates are separately
    # tested with real synthetic calendar evidence above and existing scheduler tests.
    monkeypatch.setattr('app.market.service.deferred_minute',lambda *args,**kwargs:None)
    monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime',lambda *args,**kwargs:None)
    schedule_sources()
    with Session(engine()) as db:
        assert db.get(Job,ident).state=='queued'
        assert db.get(SourceSettings,source_id(FAMILY)).next_run_at==due
        assert db.scalar(select(Job.id).where(Job.kind=='source.collect',
            Job.payload['source_id'].astext==str(source_id('baostock_daily')))) is not None


@pytest.mark.parametrize('field', ['interval_seconds','adapter_version','capabilities','symbols','rights'])
def test_changed_configuration_is_fenced_before_http(client, monkeypatch, field):
    _, day = setup(client, monkeypatch)
    ident = enqueue(day)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(FAMILY))
        config = db.get(SourceSettings, provider.id)
        if field=='interval_seconds':config.interval_seconds+=60
        elif field=='adapter_version':provider.adapter_version='synthetic-version-changed'
        elif field=='capabilities':provider.capabilities=[*provider.capabilities,'synthetic.changed']
        elif field=='symbols':config.config={'symbols':[dict(exchange='SSE',symbol='600000',asset_type='stock')]}
        else:provider.rights={**provider.rights,'display':'denied'}
    calls = fake_fetch(monkeypatch, body(day))
    job = claim(); assert job and job.id==ident
    process(job)
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==ident))
        assert run and run.request_count==0 and run.status in {'unavailable','error'}
        assert run.reason in {'configuration_changed','permission_missing'}
        assert db.get(Job, ident).state=='failed'
    assert calls == [] and saved() == []


def test_old_claim_cannot_spend_permit_after_job_target_changes_to_another_valid_day(client,monkeypatch):
    _,day=setup(client,monkeypatch)
    other=day-timedelta(days=1)
    seed_calendar(other,True)
    ident=enqueue(day)
    job=claim();assert job and job.id==ident
    original=dict(job.payload['minute_context'])
    with Session(engine()) as db,db.begin():
        current=db.get(Job,ident)
        # Both dates have real synthetic open-calendar evidence and fit the
        # seven-day bound. This changes intent, not permissions or validity.
        current.payload={**current.payload,'minute_context':{**current.payload['minute_context'],
            'requested_day':str(other)}}
    calls=fake_fetch(monkeypatch,body(day))
    process(job)
    assert job.payload['minute_context']==original
    assert calls==[] and saved()==[]
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==ident))
        assert run and run.reason=='configuration_changed' and run.request_count==0
        assert db.get(Job,ident).state=='failed'
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action=='source.request.sina-minute'))==0


def test_post_response_rights_check_keeps_fetched_body_out_of_evidence(client, monkeypatch):
    _, day = setup(client, monkeypatch)
    ident = enqueue(day)
    def revoke():
        with Session(engine()) as db, db.begin():
            provider = db.get(Provider, source_id(FAMILY))
            provider.rights={**provider.rights,'display':'denied'}
    calls = fake_fetch(monkeypatch, body(day), after=revoke)
    job=claim(); assert job and job.id==ident
    process(job)
    assert len(calls)==1 and saved()==[]
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==ident))
        assert run.reason=='configuration_changed' and run.request_count==1


def test_backfill_only_fills_empty_keys_conflicts_keep_immutable_originals(client, monkeypatch):
    _, day=setup(client,monkeypatch)
    assert store_window(day,[('09:31','11'),('09:32','12')])[0]==(2,0)
    before=saved()
    result,batch=store_window(day,[('09:31','10'),('09:32','12'),('09:33','11')],backfill=True)
    assert result==(1,1) and list(batch.rejected.values())==['backfill_conflict_existing_observation']
    after=saved()
    assert after[:2]==before and len(after)==3
    assert after[-1][4]['arrival_kind']=='backfill'
    assert all(row[2]==1 for row in after)


def test_arrival_tag_is_not_a_revision_normal_a_b_a_still_is(client,monkeypatch):
    _,day=setup(client,monkeypatch)
    slots=[('09:31','11')]
    assert store_window(day,slots,backfill=True)[0]==(1,0)
    original=saved()
    assert store_window(day,slots)[0]==(0,1)
    assert store_window(day,slots,backfill=True)[0]==(0,1)
    assert saved()==original
    assert store_window(day,[('09:31','12')])[0]==(1,0)
    assert store_window(day,slots)[0]==(1,0)
    rows=saved()
    assert [row[2] for row in rows]==[1,2,3]
    assert [row[4]['close'] for row in rows]==['11','12','11']
    assert 'arrival_kind' not in rows[1][4] and 'arrival_kind' not in rows[2][4]


def test_backfill_run_metadata_day_health_and_future_live_schedule_are_separate(client,monkeypatch):
    _,day=setup(client,monkeypatch)
    future=datetime.now(UTC)+timedelta(days=3)
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(FAMILY)).next_run_at=future
    ident=enqueue(day)
    with Session(engine()) as db:
        assert db.get(SourceSettings,source_id(FAMILY)).next_run_at==future
    calls=fake_fetch(monkeypatch,body(day))
    job=claim(); assert job and job.id==ident
    process(job)
    response=client.get('/api/v1/data-fetch-runs',params={'source_id':str(source_id(FAMILY)),'job_id':str(ident)})
    assert response.status_code==200
    runs=response.json()['data']['items']; assert len(runs)==1
    assert runs[0]['requested_day']==str(day) and runs[0]['acquisition_mode']=='backfill'
    assert runs[0]['job_id']==str(ident) and runs[0]['status']=='complete'
    with Session(engine()) as db:
        record=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id(FAMILY),EvidenceVersion.kind=='bar'))
        target=bars(db,record.instrument_id,'unadjusted',interval='1m',day=day)
        today=bars(db,record.instrument_id,'unadjusted',interval='1m',day=datetime.now(UTC).astimezone(SHANGHAI).date())
        live=next(x for x in source_states(db) if x.family==FAMILY)
        assert target.last_attempt.job_id==ident and target.last_success_at is not None
        assert target.backfill_supported and target.backfill_lookback_days==7
        assert all(item.arrival_kind=='backfill' for item in target.items)
        assert today.last_attempt is None and today.last_success_at is None
        assert live.last_attempt is None and live.last_success_at is None
        assert db.get(SourceSettings,source_id(FAMILY)).next_run_at>=future
    assert len(calls)==1 and 'datalen=1970' in calls[0]


def test_missing_target_window_has_reason_and_does_not_claim_suspension(client,monkeypatch):
    _,day=setup(client,monkeypatch)
    ident=enqueue(day)
    calls=fake_fetch(monkeypatch,body(day-timedelta(days=1)))
    job=claim(); assert job and job.id==ident
    process(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==ident))
        assert (run.status,run.reason,run.accepted)==('empty','backfill_day_not_returned',0)
        assert db.get(Job,ident).state=='succeeded'
    assert len(calls)==1 and saved()==[]


def test_backfill_reuses_persisted_shared_budget_and_keeps_future_plan(client,monkeypatch):
    _,day=setup(client,monkeypatch)
    ident=enqueue(day)
    calls=fake_fetch(monkeypatch,body(day))
    job=claim(); assert job and job.id==ident
    process(job)
    future=datetime.now(UTC)+timedelta(days=3)
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(FAMILY)).next_run_at=future
    second=enqueue(day)
    job=claim();assert job and job.id==second
    process(job)
    with Session(engine()) as db:
        queued=db.get(Job,second)
        assert queued.state=='queued' and queued.error_code=='source_budget_wait'
        assert queued.attempts==0
        assert db.scalar(select(func.count()).select_from(DataFetchRun).where(DataFetchRun.job_id==second))==0
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action=='source.request.sina-minute'))==1
        assert db.get(SourceSettings,source_id(FAMILY)).next_run_at>=future
    assert len(calls)==1


def test_backfill_never_becomes_strict_history_or_changes_daily_selection(client,monkeypatch):
    headers,day=setup(client,monkeypatch)
    store_window(day,[('09:31','11')],backfill=True)
    with Session(engine()) as db:
        record=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id(FAMILY),EvidenceVersion.kind=='bar'))
        assert record.available_at is None and record.historical_eligible is False and record.availability=='unknown'
        assert bars(db,record.instrument_id,'unadjusted',interval='1d').items==[]
    cutoff=datetime.now(UTC)
    visible=client.get('/api/v1/evidence',params={'kind':'bar'}).json()['data']['items']
    strict=client.get('/api/v1/evidence',params={'kind':'bar','as_of_time':cutoff.isoformat()}).json()['data']['items']
    assert len(visible)==1 and strict==[]
    result=finish_snapshot(client,snapshot(client,headers,cutoff=cutoff,keys=[saved()[0][3]]))
    assert result['items']==[] and result['quality']['status']=='unavailable'
    # The original source observation is immutable even through a later retry.
    before=saved()
    store_window(day,[('09:31','12')],backfill=True)
    assert saved()==before
