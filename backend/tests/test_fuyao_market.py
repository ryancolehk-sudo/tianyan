"""Existing-endpoint bulk snapshots and honest live fallback; synthetic data only."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import engine
from app.market import fuyao_market as fm
from app.market.collect import store_calendar_batch, store_reference_batch
from app.market.models import DataFetchRun, SourceSettings
from app.market.read_facets import EvidenceReadFacet
from app.evidence.models import EvidenceVersion
from app.market.protocol import Response, SourceFailure
from app.market.terminal_read import workbench_view
from app.models import User, Instrument
from test_data_resilience import CLOCK, SYMBOL, receipt, scenario, source


def raw(code='600000.SH', **changes):
    return dict(thscode=code,ticker=code[:6],last_price='12',prev_price='10',
        open_price='10',high_price='12',low_price='9',volume=100,turnover='1200',**changes)


def batch(rows=None, clock=CLOCK):
    return fm.parse([(row,int(clock.timestamp()*1000),clock) for row in (rows or [raw()])],clock)


def test_bulk_preserves_snapshot_and_never_invents_market_clock():
    payload=batch().records[0][1]
    row=payload.rows[0]
    assert row['trade_time'] is None and datetime.fromisoformat(row['snapshot_time'])==CLOCK
    assert row['close']=='12' and row['vol']=='100' and row['amount']=='1200'
    assert payload.observed_day==CLOCK.date()
    assert not any(key in row for key in ('trade_date','trade_day'))
    with pytest.raises(SourceFailure,match='duplicate_instrument'):
        batch([raw(),raw()])


@pytest.mark.parametrize('failure',['truncated','changed','empty'])
def test_full_fetch_rejects_missing_or_changed_pages(monkeypatch,failure):
    calls=[]
    def request(path,params,credential,**kw):
        calls.append(params)
        size=6000 if len(calls)==1 else 0
        total=6001 if len(calls)==1 or failure!='changed' else 6002
        if failure=='truncated':size=20
        if failure=='empty':size=0
        data={'code':0,'data':{'total':total,'timestamp':int(CLOCK.timestamp()*1000),'item':[raw()]*size}}
        return Response(json.dumps(data).encode(),CLOCK,5,200)
    monkeypatch.setattr(fm.fuyao,'request',request)
    with pytest.raises(SourceFailure,match='market_scope_'):
        fm.fetch('synthetic-private-key')
    assert len(calls)<=2 and all(item['limit']==6000 for item in calls)


def test_single_response_total_is_checked_before_use(monkeypatch):
    calls=[]
    def request(path,params,credential,**kw):
        calls.append((path,params))
        return Response(json.dumps({'code':0,'data':{'total':1,
            'timestamp':int(CLOCK.timestamp()*1000),'item':[raw()]}}).encode(),CLOCK,5,200)
    monkeypatch.setattr(fm.fuyao,'request',request)
    response,rows=fm.fetch('synthetic-private-key')
    assert response.request_count==1 and len(rows)==1
    assert calls==[('/api/a-share/prices/snapshot',{'limit':6000,'offset':0})]
    assert batch().records[0][1].rows==fm.parse(rows,CLOCK).records[0][1].rows


def scene(db,owner,monkeypatch,*,primary_age=300,snapshot_age=0,backup=True):
    from app.market.tushare_market import parse_market,encoded
    from test_tushare_market import quote
    from app.market import resilience as r
    monkeypatch.setattr('app.market.terminal_read.now',lambda db:CLOCK)
    monkeypatch.setattr('app.market.read_models.now',lambda db:CLOCK)
    for family,exchange in [('tushare_calendar_sse','SSE'),('tushare_calendar_szse','SZSE')]:
        provider,cfg=source(db,owner,family,symbols=[])
        store_calendar_batch(db,provider,cfg,[(CLOCK.date(),True)],CLOCK-timedelta(hours=1),exchanges=(exchange,))
    primary,pc=source(db,owner,'tushare_market_rt',symbols=[])
    at=CLOCK-timedelta(seconds=primary_age)
    rows=[quote('600000.SH',price='11',at=at)]
    store_reference_batch(db,primary,pc,parse_market(encoded(list(rows[0]),rows),at),at)
    receipt(db,primary,state='error',reason='timeout')
    provider,cfg=source(db,owner,'fuyao_quotes',symbols=[])
    cfg.config={'symbols':[],'collection_mode':'full_market','backup_enabled':backup}
    at=CLOCK-timedelta(seconds=snapshot_age)
    store_reference_batch(db,provider,cfg,batch(clock=at),at)
    run=DataFetchRun(source_id=provider.id,capability='quote.live',started_at=at-timedelta(milliseconds=50),received_at=at,
        status='partial',reason='snapshot_trade_date_unverified',expected=1,accepted=1,duplicates=0,rejected=0,request_count=1)
    db.add(run);db.flush()
    return primary,pc,provider,cfg,run


def test_automatic_backup_keeps_paid_priority_and_requires_explicit_config(scenario,monkeypatch):
    from app.integrations.routing import preferred_families,collection_allowed,invalidate
    with Session(engine()) as db,db.begin():
        _,_,provider,cfg,_=scene(db,scenario,monkeypatch,backup=False)
        assert not collection_allowed(db,provider.family)
        assert preferred_families(db,'stock_quote',['fuyao_quotes','tushare_market_rt'])==['tushare_market_rt']
        cfg.config={**cfg.config,'backup_enabled':True};db.flush();invalidate(db)
        assert collection_allowed(db,provider.family)
        assert preferred_families(db,'stock_quote',['fuyao_quotes','tushare_market_rt'])==['tushare_market_rt','fuyao_quotes']
        assert not collection_allowed(db,'tdx_quotes')


@pytest.mark.parametrize('primary_age,snapshot_age,expected',[(0,0,'tushare_market_rt'),(300,0,'fuyao_quotes'),(300,300,'tushare_market_rt')])
def test_live_fallback_is_fresh_whole_record_and_has_no_fake_history(scenario,monkeypatch,primary_age,snapshot_age,expected):
    with Session(engine()) as db,db.begin():
        scene(db,scenario,monkeypatch,primary_age=primary_age,snapshot_age=snapshot_age)
        result=workbench_view(db,scenario,stock='600000.SH')
        stock=next(item for item in result.stocks if item.code=='600000.SH')
        assert stock.source_family==expected
        if expected=='fuyao_quotes':
            assert stock.price==12 and stock.price_basis=='snapshot'
            assert stock.source_time is None and stock.source_snapshot_time==CLOCK
            assert stock.speed_5m is None and stock.seal_amount_cny is None
            assert not result.stock_seals and not result.events
            assert result.capabilities[0].status=='partial'
            assert '成交时刻未知' in result.capabilities[0].reason
            assert result.breadth.up==1  # primary series remains independently timestamped


@pytest.mark.parametrize('facet_state',['present','stale','missing'])
def test_bulk_fallback_keeps_receipt_date_and_original_payload_guards(scenario,monkeypatch,facet_state):
    from app.market import resilience as r
    with Session(engine()) as db,db.begin():
        _,_,provider,cfg,run=scene(db,scenario,monkeypatch)
        current=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id,
            EvidenceVersion.kind=='reference',EvidenceVersion.received_at==CLOCK))
        facet=db.get(EvidenceReadFacet,current.id)
        if facet_state=='stale':
            facet.content_hash='0'*64
            facet.observed_day=(CLOCK.date()-timedelta(days=1)).isoformat()
        elif facet_state=='missing':
            db.execute(delete(EvidenceReadFacet).where(EvidenceReadFacet.evidence_id==current.id))
        # A later receipt is outside the immutable observation, and a larger
        # historical response must not displace the exact current-day batch.
        past=CLOCK-timedelta(days=1)
        store_reference_batch(db,provider,cfg,batch([raw(),raw('600001.SH')],past),past)
        future=CLOCK+timedelta(seconds=1)
        store_reference_batch(db,provider,cfg,batch([{**raw(),'last_price':'99','high_price':'99'}],future),future)
        db.flush()
        health=r.saved_quality(db,provider,cfg,'stock_quote',CLOCK.date(),CLOCK,run)
        assert health.valid and health.received_at==CLOCK and health.missing_count==0
        result=workbench_view(db,scenario,stock='600000.SH')
        stock=next(item for item in result.stocks if item.code=='600000.SH')
        assert stock.source_family=='fuyao_quotes' and stock.price==12
        assert stock.source_received_at==CLOCK and stock.source_time is None
        assert stock.source_snapshot_time==CLOCK


def test_bulk_quote_read_and_historical_cutoff_do_not_gain_a_trade_clock(scenario,monkeypatch):
    from app.market.read_models import quote_batch
    from app.market import resilience as r
    with Session(engine()) as db,db.begin():
        primary,pc,provider,cfg,run=scene(db,scenario,monkeypatch)
        r.evaluate(db,primary,pc,r.latest_run(db,primary.id,CLOCK),CLOCK,allow_actions=False)
        identity=Instrument(**SYMBOL,name='synthetic',first_received_at=CLOCK-timedelta(days=1))
        db.add(identity);db.flush()
        live=quote_batch(db,instrument_id=identity.id,observation_cutoff=CLOCK).items[0]
        assert live.provenance.family=='fuyao_quotes' and live.state=='partial'
        assert live.quote.price=='12' and live.quote.quote_time is None
        assert live.quote.source_snapshot_time==CLOCK and not live.provenance.historical_eligible
        strict=quote_batch(db,instrument_id=identity.id,as_of_time=CLOCK).items[0]
        assert strict.quote is None


def test_parent_observation_keeps_snapshot_uncertainty_and_strict_history_rules(scenario, monkeypatch):
    with Session(engine()) as db, db.begin():
        scene(db, scenario, monkeypatch)
        parent = workbench_view(db, scenario, stock='600000.SH')
        original = next(item for item in parent.stocks if item.code == '600000.SH')
        assert original.source_family == 'fuyao_quotes' and original.price == 12
        monkeypatch.setattr('app.market.terminal_read.now', lambda db: CLOCK + timedelta(minutes=5))
        pinned = workbench_view(db, scenario, day=parent.trade_day, detail_codes=['600000.SH'],
            observation_cutoff=parent.detail_observation_cutoff)
        assert pinned.revision == parent.detail_revision
        assert pinned.stocks[0].price == 12 and pinned.stocks[0].source_time is None
        assert pinned.stocks[0].source_snapshot_time == CLOCK
        assert pinned.stocks[0].speed_5m is None and pinned.stocks[0].seal_amount_cny is None
        strict = workbench_view(db, scenario, day=parent.trade_day, detail_codes=['600000.SH'], as_of_time=CLOCK)
        assert strict.stocks[0].source_family != 'fuyao_quotes'


def test_enabling_bulk_mode_does_not_hide_older_eligible_native_quote(scenario,monkeypatch):
    from app.market.read_models import quote_batch
    from app.market.collect import store_quote_batch
    from app.market.fuyao import parse_quotes
    from app.market.protocol import InstrumentKey
    with Session(engine()) as db,db.begin():
        _,_,provider,cfg,_=scene(db,scenario,monkeypatch)
        clock=CLOCK-timedelta(minutes=5)
        row=raw();row['quote_time_ms']=int(clock.timestamp()*1000)
        body=json.dumps({'code':0,'data':{'item':[row]}}).encode()
        store_quote_batch(db,provider,cfg,parse_quotes(body,[InstrumentKey(**SYMBOL)],clock))
        identity=db.scalar(select(Instrument).where(Instrument.symbol==SYMBOL['symbol']))
        result=quote_batch(db,instrument_id=identity.id,as_of_time=CLOCK).items[0]
        assert result.provenance.family=='fuyao_quotes' and result.provenance.historical_eligible
        assert result.quote.quote_time==clock and result.quote.price=='12'


def test_configuration_accepts_explicit_free_bulk_backup_without_enabling_other_services(client):
    from test_data_sources import login,configuration,configure
    from app.market.models import SourceSettings
    from app.market.service import source_id
    headers=login(client)
    body=configuration('fuyao_quotes')
    body.update(collection_mode='full_market',backup_enabled=True,symbols=[])
    configure(client,headers,body,key='synthetic-explicit-fuyao-backup')
    with Session(engine()) as db:
        cfg=db.get(SourceSettings,source_id('fuyao_quotes'))
        assert cfg.config=={'symbols':[],'collection_mode':'full_market','backup_enabled':True}
    forbidden={**body,'family':'tdx_quotes','collection_mode':'selected',
        'symbols':[SYMBOL]}
    response=client.post('/api/v1/data-sources',json=forbidden,headers=headers)
    assert response.status_code==422


def test_supplier_retry_after_is_shared_between_existing_stock_and_index(scenario,monkeypatch):
    from app.market.budget import family_cooldown_until
    from app.market.auction_schedule import schema_recheck
    with Session(engine()) as db,db.begin():
        _,_,provider,_,_=scene(db,scenario,monkeypatch)
        limited=DataFetchRun(source_id=provider.id,capability='quote.live',started_at=CLOCK,
            received_at=CLOCK,status='error',reason='rate_limited',expected=1,accepted=0,rejected=0,
            duplicates=0,request_count=1,retry_not_before=CLOCK+timedelta(seconds=90))
        db.add(limited);db.flush()
        for family in ('fuyao_quotes','fuyao_indices'):
            assert family_cooldown_until(db,family,CLOCK)==limited.retry_not_before
            assert schema_recheck(family,'schema_changed')
            assert schema_recheck(family,'upstream_redirect')
            assert not schema_recheck(family,'permission_missing')
        assert family_cooldown_until(db,'tushare_market_rt',CLOCK) is None


@pytest.mark.parametrize('hour,minute,market_hour,market_minute,age',[(12,30,11,29,60),(15,20,15,0,0)])
def test_break_and_close_keep_healthy_paid_quote_ahead_of_undated_snapshot(scenario,monkeypatch,hour,minute,market_hour,market_minute,age):
    from app.market.read_models import quote_batch
    from app.market.tushare_market import parse_market,encoded
    from test_tushare_market import quote
    cutoff=CLOCK.replace(hour=hour,minute=minute)
    native=CLOCK.replace(hour=market_hour,minute=market_minute)
    with Session(engine()) as db,db.begin():
        paid,pc=source(db,scenario,'tushare_market_rt',symbols=[])
        rows=[quote('600000.SH',price='11',at=native)]
        store_reference_batch(db,paid,pc,parse_market(encoded(list(rows[0]),rows),native),native)
        standby,sc=source(db,scenario,'fuyao_quotes',symbols=[])
        sc.config={'symbols':[],'collection_mode':'full_market','backup_enabled':True}
        store_reference_batch(db,standby,sc,batch(clock=cutoff),cutoff)
        identity=Instrument(**SYMBOL,name='synthetic',first_received_at=CLOCK-timedelta(days=1))
        db.add(identity);db.flush()
        result=quote_batch(db,instrument_id=identity.id,observation_cutoff=cutoff).items[0]
        assert result.provenance.family=='tushare_market_rt' and result.quote.price=='11'
        assert result.state=='available' and result.age_seconds==age


@pytest.mark.parametrize('clock_backstep',[False,True])
def test_collector_writes_one_bulk_evidence_and_keeps_time_uncertainty(client,monkeypatch,clock_backstep):
    from test_fuyao_data import setup,config
    from test_data_sources import configure
    from app.market.service import enqueue_collection,source_id
    from app.worker import claim,process
    from app.evidence.models import EvidenceVersion
    from app.integrations.credentials import resolve_source_credentials,job_context
    headers=setup(client,monkeypatch)
    body=config();body.update(collection_mode='full_market',backup_enabled=True,symbols=[])
    configure(client,headers,body,key='synthetic-bulk-mode')
    received=datetime.now(CLOCK.tzinfo)+timedelta(seconds=1)
    started=received+timedelta(seconds=1) if clock_backstep else received-timedelta(milliseconds=4)
    class CollectorClock(datetime):
        @classmethod
        def now(cls,tz=None):return started.astimezone(tz) if tz else started.replace(tzinfo=None)
    monkeypatch.setattr('app.market.collect.datetime',CollectorClock)
    def request(path,params,credential,**kwargs):
        assert params=={'limit':6000,'offset':0}
        now=received
        value={'code':0,'data':{'total':1,'timestamp':int(now.timestamp()*1000),'item':[raw()]}}
        return Response(json.dumps(value).encode(),now,4,200)
    monkeypatch.setattr('app.market.fuyao_transport.request',request)
    with Session(engine()) as db,db.begin():
        bound=resolve_source_credentials(db,source_id('fuyao_quotes'),'fuyao_quotes')
        queued=enqueue_collection(db,source_id('fuyao_quotes'),integration_probe=job_context(bound,source_id('fuyao_quotes'),probe=True))
        job_id=queued.id
    job=claim();assert job.id==job_id;process(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job_id))
        assert run.status=='partial' and run.reason=='snapshot_trade_date_unverified'
        assert run.started_at==started and run.received_at==received
        assert run.clock_regressed is clock_backstep and run.elapsed_ms==4
        assert run.accepted==1 and run.request_count==1
        rows=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id('fuyao_quotes'))))
        assert len(rows)==1 and rows[0].kind=='reference'
        assert rows[0].event_at is None and rows[0].historical_eligible is False
        assert rows[0].payload['rows'][0]['trade_time'] is None
        from app.integrations.data import connection_test
        proof=connection_test(db,{'revision':bound.revision,'source_ids':[str(run.source_id)],'last_test':{
            'revision':bound.revision,'job_id':str(job_id),'source_id':str(run.source_id),'status':'queued'}})
        assert proof['status']=='success' and '成交时刻未知' in proof['message']


def test_manual_probe_cannot_bypass_supplier_retry_after(client,monkeypatch):
    from test_fuyao_data import setup
    from app.market.service import enqueue_collection,source_id
    from app.integrations.credentials import resolve_source_credentials,job_context
    from app.evidence.common import now
    from app.worker import claim,process
    from sqlalchemy import func
    from app.models import Job
    setup(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        at=now(db);until=at+timedelta(seconds=90)
        bound=resolve_source_credentials(db,source_id('fuyao_quotes'),'fuyao_quotes')
        queued=enqueue_collection(db,source_id('fuyao_quotes'),integration_probe=job_context(bound,source_id('fuyao_quotes'),probe=True))
        job_id=queued.id
        # The supplier limit arrives after this probe was queued. Checking
        # only when creating the job cannot protect the physical request.
        db.add(DataFetchRun(source_id=source_id('fuyao_quotes'),capability='quote.live',started_at=at,
            received_at=at,status='error',reason='rate_limited',expected=1,accepted=0,rejected=0,
            duplicates=0,request_count=1,retry_not_before=until))
    monkeypatch.setattr('app.market.fuyao_transport.request',lambda *a,**k:pytest.fail('a manual probe must respect shared Retry-After'))
    job=claim();assert job.id==job_id;process(job)
    with Session(engine()) as db:
        saved=db.get(Job,job_id)
        assert saved.state=='queued' and saved.available_at==until and saved.attempts==0
        assert saved.error_code=='source_budget_wait' and saved.lease_token is None
        assert db.scalar(select(func.count()).select_from(DataFetchRun).where(DataFetchRun.job_id==job_id))==0


@pytest.mark.parametrize('age,allowed',[(120,True),(1,False)])
def test_manual_probe_uses_actual_spacing_instead_of_next_automatic_open(client,monkeypatch,age,allowed):
    from test_fuyao_data import setup
    from app.market.service import enqueue_collection,source_id
    from app.integrations.credentials import resolve_source_credentials,job_context
    from app.evidence.common import now
    from app.auth import APIError
    setup(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        clock=now(db);provider=source_id('fuyao_quotes')
        cfg=db.get(SourceSettings,provider)
        cfg.next_run_at=clock+timedelta(days=1)
        earlier=clock-timedelta(seconds=age)
        db.add(DataFetchRun(source_id=provider,capability='quote.live',started_at=earlier,
            received_at=earlier,status='partial',reason='quote_time_unknown',expected=1,
            accepted=1,rejected=0,duplicates=0,request_count=1))
        db.flush()
        bound=resolve_source_credentials(db,provider,'fuyao_quotes')
        if allowed:
            queued=enqueue_collection(db,provider,integration_probe=job_context(bound,provider,probe=True))
            assert queued.state=='queued' and queued.payload['integration_context']['probe'] is True
        else:
            with pytest.raises(APIError) as caught:
                enqueue_collection(db,provider,integration_probe=job_context(bound,provider,probe=True))
            assert caught.value.code=='source_rate_limit'


def test_default_routing_keeps_current_paid_quote_ahead_of_legacy_free_source(scenario):
    from app.market.read_models import quote_batch
    from app.market.collect import store_quote_batch
    from app.market.protocol import Quote,QuoteBatch,InstrumentKey
    from app.market.tushare_market import parse_market,encoded
    from test_tushare_market import quote
    from decimal import Decimal
    with Session(engine()) as db,db.begin():
        paid,pc=source(db,scenario,'tushare_market_rt',symbols=[])
        rows=[quote('600000.SH',price='11',at=CLOCK)]
        store_reference_batch(db,paid,pc,parse_market(encoded(list(rows[0]),rows),CLOCK),CLOCK)
        free,fc=source(db,scenario,'tencent_web')
        record=Quote(InstrumentKey(**SYMBOL),'synthetic',Decimal('12'),Decimal('10'),
            Decimal('10'),Decimal('12'),Decimal('9'),CLOCK,CLOCK,'CNY')
        store_quote_batch(db,free,fc,QuoteBatch(records=[record],expected=1))
        identity=db.scalar(select(Instrument).where(Instrument.symbol==SYMBOL['symbol']))
        result=quote_batch(db,instrument_id=identity.id,observation_cutoff=CLOCK).items[0]
        assert result.provenance.family=='tushare_market_rt' and result.quote.price=='11'
