"""Failure-to-next-action contracts on a disposable database, without HTTP."""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.common import content_hash, normalized
from app.evidence.models import EvidenceVersion, SourceVersion
from app.integrations.routing_models import DataRoutes
from app.market import resilience as r
from app.market.models import DataFetchRun, SourceSettings
from app.market.resilience_models import DataHealth
from app.market.service import source_id
from app.models import Instrument, Job, Provider, User
from app.schemas import Provider as ProviderDTO
from test_data_sources import login

CLOCK = datetime.fromisoformat('2026-10-27T09:31:00+08:00')
SYMBOL = {'exchange':'SSE', 'symbol':'600000', 'asset_type':'stock'}


@pytest.fixture
def scenario(client, monkeypatch):
    login(client)
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr(r, 'now', lambda db: CLOCK)
    monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime', lambda *a: None)
    from app.integrations.credentials import SourceCredentials
    monkeypatch.setattr('app.integrations.credentials.resolve_source_credentials',
        lambda *a,**k:SourceCredentials(None,'https://example.invalid'))
    monkeypatch.setattr('urllib.request.build_opener', lambda *a, **k: pytest.fail('recovery checks cannot contact a vendor'))
    with Session(engine()) as db:
        owner = db.scalar(select(User).where(User.role == 'admin')).id
    return owner


def source(db, owner, family='tencent_web', symbols=None, configured=True, enabled=True):
    provider = Provider(id=source_id(family), name=family, family=family, kind='market',
        configured=configured, enabled=enabled, entitlement_status='verified',
        adapter_version='synthetic-test', capabilities=['quote.l1'],
        rights={'display':'allowed', 'cache':'allowed', 'export':'unknown', 'redistribute':'unknown'})
    db.add(provider)
    db.flush()
    config = SourceSettings(source_id=provider.id, owner_id=owner, rights_basis='synthetic test only',
        config={'symbols':symbols if symbols is not None else [SYMBOL]}, interval_seconds=30,
        configured_at=CLOCK-timedelta(hours=1), next_run_at=CLOCK+timedelta(hours=1))
    db.add(config)
    db.flush()
    descriptor = normalized(ProviderDTO.model_validate(provider))
    db.add(SourceVersion(source_id=provider.id, recorded_at=config.configured_at,
        descriptor=descriptor, content_hash=content_hash(descriptor)))
    return provider, config


def receipt(db, provider, at=CLOCK, *, state='complete', reason=None, native='current',
            symbols=None, duplicates=0, price='11'):
    run = DataFetchRun(source_id=provider.id, capability='quote.l1',
        started_at=at-timedelta(milliseconds=50), received_at=at, status=state,
        reason=reason, expected=1, accepted=0 if duplicates or state=='error' else 1,
        duplicates=duplicates, rejected=0, request_count=1)
    db.add(run)
    if state != 'error' and not duplicates:
        for symbol in symbols or [SYMBOL]:
            identity = db.scalar(select(Instrument).where(Instrument.exchange==symbol['exchange'],
                Instrument.symbol==symbol['symbol'], Instrument.asset_type==symbol['asset_type']))
            if identity is None:
                identity = Instrument(**symbol, name='synthetic', valid_from=CLOCK.date())
                db.add(identity)
                db.flush()
            source_time = at if native == 'current' else native
            payload = dict(price=price, previous_close='10', open='10', high='11', low='10',
                quote_time=source_time.isoformat() if source_time else None, price_unit='CNY')
            key = uuid.uuid4().hex
            db.add(EvidenceVersion(source_id=provider.id, visibility='shared', source_key=key,
                source_revision=key, version_number=1, kind='quote', instrument_id=identity.id,
                event_at=source_time, received_at=at, available_at=at, availability='known',
                provenance='source_reported', historical_eligible=False, rights_basis='synthetic test',
                payload=payload, content_hash=content_hash(normalized(payload))))
    db.flush()
    return run


def row(db, provider):
    return db.scalar(select(DataHealth).where(DataHealth.primary_source_id==provider.id))


def test_complete_http_without_saved_evidence_is_recovering(scenario):
    with Session(engine()) as db, db.begin():
        p,c = source(db,scenario)
        run=receipt(db,p,duplicates=1)
        r.evaluate(db,p,c,run,CLOCK)
        value=row(db,p)
        assert value.state=='recovering' and value.reason=='quote_missing_or_stale'
        assert value.next_action=='retry_source' and value.last_valid_at is None


def test_duplicate_valid_batch_keeps_original_data_clock(scenario):
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        first=receipt(db,p)
        r.evaluate(db,p,c,first,CLOCK)
        second=receipt(db,p,CLOCK+timedelta(seconds=5),duplicates=1)
        r.evaluate(db,p,c,second,CLOCK+timedelta(seconds=5))
        value=row(db,p)
        assert value.state=='normal'
        assert value.last_source_time==CLOCK and value.last_valid_at==CLOCK


@pytest.mark.parametrize('minute,expected',[(59,'normal'),(55,'recovering')])
def test_close_does_not_age_a_valid_last_snapshot_or_hide_a_preclose_gap(scenario,monkeypatch,minute,expected):
    close=CLOCK.replace(hour=15,minute=0)
    check=close+timedelta(hours=2)
    next_open=(close+timedelta(days=1)).replace(hour=9,minute=30)
    monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime',lambda *a:next_open)
    with Session(engine()) as db,db.begin():
        p,c=source(db,scenario)
        at=close.replace(hour=14,minute=minute)
        run=receipt(db,p,at)
        r.evaluate(db,p,c,run,check)
        value=row(db,p)
        assert value.state==expected
        if expected=='normal':
            assert value.last_source_time==at and value.next_action is None
        else:
            assert value.reason=='source_stale' and value.next_action=='wait_session'
        assert db.scalar(select(func.count()).select_from(Job))==0


@pytest.mark.parametrize('native,price',[(None,'11'),(CLOCK-timedelta(days=1),'11'),
    (CLOCK+timedelta(minutes=1),'11'),('current',None)])
def test_unknown_old_future_or_missing_price_never_becomes_live(scenario,native,price):
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        run=receipt(db,p,native=native,price=price)
        r.evaluate(db,p,c,run,CLOCK)
        assert row(db,p).state=='recovering'


def test_verified_compatible_backup_is_used_without_changing_preferences(scenario):
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        alternative,backup_config=source(db,scenario,'tushare_rt_k')
        receipt(db,alternative)
        failed=receipt(db,p,state='error',reason='timeout')
        r.evaluate(db,p,c,failed,CLOCK)
        value=row(db,p)
        assert (value.state,value.active_source_id,value.next_action)==('fallback',alternative.id,'probe_primary')
        assert db.scalar(select(func.count()).select_from(DataRoutes))==0
        assert db.scalar(select(func.count()).select_from(Job))==0


@pytest.mark.parametrize('issue',['never_succeeded','disabled','permission','wrong_scope','stale','unknown_time','partial'])
def test_ineligible_backup_never_claims_recovery(scenario,issue):
    with Session(engine()) as db, db.begin():
        symbols=[SYMBOL,{**SYMBOL,'symbol':'600001'}] if issue=='partial' else None
        p,c=source(db,scenario,symbols=symbols)
        q,qc=source(db,scenario,'tushare_rt_k',symbols=[{**SYMBOL,'symbol':'600002'}] if issue=='wrong_scope' else None)
        if issue!='never_succeeded':
            receipt(db,q,CLOCK-timedelta(minutes=10) if issue=='stale' else CLOCK,
                native=None if issue=='unknown_time' else 'current',symbols=qc.config['symbols'])
        if issue=='disabled':q.enabled=False
        if issue=='permission':q.rights={**q.rights,'display':'denied'}
        failed=receipt(db,p,state='error',reason='timeout')
        r.evaluate(db,p,c,failed,CLOCK)
        assert row(db,p).state=='recovering' and row(db,p).active_source_id is None


def test_small_selection_cannot_replace_whole_market(scenario):
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario,'tushare_market_rt',symbols=[])
        q,qc=source(db,scenario,'tencent_web')
        receipt(db,q)
        failed=receipt(db,p,state='error',reason='timeout')
        r.evaluate(db,p,c,failed,CLOCK)
        assert row(db,p).scope['universe']=='whole_market'
        assert row(db,p).active_source_id is None


@pytest.mark.parametrize('family',['tushare_rt_indices','fuyao_indices'])
def test_live_index_health_uses_actual_four_index_collection_scope(scenario,family):
    from app.market.registry import INDEXES
    from app.market.review_protocol import INDEX_CODES
    assert len(INDEX_CODES) > len(INDEXES)
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario,family,symbols=[])
        symbols=[dict(exchange=e,symbol=s,asset_type='index') for e,s,_ in INDEXES]
        r.evaluate(db,p,c,receipt(db,p,symbols=symbols),CLOCK)
        health=row(db,p)
        assert health.state=='normal' and health.missing_count==0
        identity=lambda item:(item['exchange'],item['symbol'],item['asset_type'])
        assert {identity(item) for item in health.scope['symbols']} == {identity(item) for item in symbols}


def test_one_local_check_failure_persists_action_and_does_not_stop_other_sources(scenario,monkeypatch):
    with Session(engine()) as db, db.begin():
        bad,_=source(db,scenario,'tushare_rt_indices',symbols=[])
        good,_=source(db,scenario,'tencent_web')
        bad_id,good_id=bad.id,good.id
    called=[]
    original=r.reconcile_source
    def fail_one(identity):
        called.append(identity)
        if identity==bad_id:
            raise KeyError('synthetic local check failure')
        return original(identity)
    monkeypatch.setattr(r,'reconcile_source',fail_one)
    r.reconcile(force=True)
    assert bad_id in called and good_id in called
    with Session(engine()) as db:
        fault=db.scalar(select(DataHealth).where(DataHealth.primary_source_id==bad_id))
        assert (fault.state,fault.stage,fault.reason)==('attention','monitor','local_check_failed')
        assert fault.next_action=='inspect_runtime' and fault.next_action_at==CLOCK+timedelta(seconds=15)
    monkeypatch.setattr(r,'reconcile_source',original)
    r.reconcile(force=True)
    with Session(engine()) as db:
        assert db.scalar(select(DataHealth.id).where(DataHealth.capability=='pipeline:source_check')) is None
        assert db.scalar(select(DataHealth.id).where(DataHealth.primary_source_id==bad_id,DataHealth.capability=='index_quote'))


def test_primary_returns_only_after_two_advancing_valid_samples_across_sessions(scenario):
    primary=source_id('tencent_web')
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        q,_=source(db,scenario,'tushare_rt_k')
        receipt(db,q)
        failed=receipt(db,p,state='error',reason='timeout')
        r.evaluate(db,p,c,failed,CLOCK)
    for offset,source_offset,expected in [(5,5,'fallback'),(6,5,'fallback'),(10,10,'normal')]:
        with Session(engine()) as db, db.begin():
            p,c=db.get(Provider,primary),db.get(SourceSettings,primary)
            run=receipt(db,p,CLOCK+timedelta(seconds=offset),native=CLOCK+timedelta(seconds=source_offset))
            r.evaluate(db,p,c,run,run.received_at)
            assert row(db,p).state==expected


@pytest.mark.parametrize('reason',['permission_missing','credentials_missing','decoder_changed','schema_changed'])
def test_permanent_failure_has_an_explicit_action_without_retry_loop(scenario,reason):
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        failed=receipt(db,p,state='error',reason=reason)
        c.paused_reason=reason
        r.evaluate(db,p,c,failed,CLOCK)
        value=row(db,p)
        assert value.state=='attention' and value.next_action_at is None
        assert value.next_action in {'configure_source','inspect_schema'}


def test_pending_overdue_task_is_internal_queue_failure_even_with_fresh_data(scenario):
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        run=receipt(db,p)
        db.add(Job(owner_id=scenario,kind='source.collect',state='queued',
            payload={'source_id':str(p.id),'scheduled_realtime':True},available_at=CLOCK-timedelta(seconds=16)))
        db.flush()
        r.evaluate(db,p,c,run,CLOCK)
        value=row(db,p)
        assert value.stage=='scheduler' and value.state=='recovering'
        assert value.reason=='collection_queue_overdue' and value.next_action=='repair_queue'
        assert db.scalar(select(func.count()).select_from(Job))==1


def test_future_supplier_cooldown_is_not_shortened(scenario,monkeypatch):
    wait=CLOCK+timedelta(minutes=10)
    monkeypatch.setattr(r,'supplier_cooldown',lambda *a:wait)
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        failed=receipt(db,p,CLOCK-timedelta(minutes=5),state='error',reason='rate_limited')
        c.next_run_at=wait
        r.evaluate(db,p,c,failed,CLOCK)
        assert c.next_run_at==wait and row(db,p).next_action_at==wait


def test_session_wait_does_not_wake_collection(scenario,monkeypatch):
    afternoon=CLOCK.replace(hour=13,minute=0)
    monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime',lambda *a:afternoon)
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        failed=receipt(db,p,state='error',reason='timeout')
        before=c.next_run_at
        r.evaluate(db,p,c,failed,CLOCK)
        assert row(db,p).next_action=='wait_session' and row(db,p).next_action_at==afternoon
        assert c.next_run_at==before


def test_date_rollover_keeps_original_health_and_evidence(scenario):
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        run=receipt(db,p)
        r.evaluate(db,p,c,run,CLOCK)
        r.evaluate(db,p,c,run,CLOCK+timedelta(days=1))
        records=list(db.scalars(select(DataHealth).order_by(DataHealth.trade_day)))
        assert len(records)==2 and records[0].state=='normal' and records[1].state=='recovering'
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.source_id==p.id))==1


def test_get_health_is_read_only_and_regular_account_cannot_see_other_scope(client,scenario):
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        r.evaluate(db,p,c,receipt(db,p,state='error',reason='timeout'),CLOCK)
        other=User(username='other-health-user',role='viewer',password_hash='unused',enabled=True)
        db.add(other)
        db.flush()
        db.add(DataHealth(owner_id=other.id,primary_source_id=None,capability='private:unrelated',
            scope_key='a'*64,scope={'private':'must stay private'},trade_day=CLOCK.date(),state='attention',
            stage='source',configuration_sha256='b'*64,failure_count=0,recovery_successes=0,updated_at=CLOCK))
        db.flush()
        owner=db.get(User,scenario)
        owner.role='viewer'
    response=client.get('/api/v1/data-recovery?day=2026-10-27')
    assert response.status_code==200,response.text
    data=response.json()['data']
    assert data['health_state']=='recovering' and len(data['health'])==1
    assert data['health'][0]['capability']=='stock_quote'
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job))==0


def test_no_health_records_are_not_labelled_healthy(client,scenario):
    data=client.get('/api/v1/data-recovery').json()['data']
    assert data['health']==[] and data['health_state'] is None


def test_health_checker_failure_does_not_lose_ingested_market_batch(scenario,monkeypatch):
    def broken(*a,**k):raise RuntimeError('synthetic checker failure')
    monkeypatch.setattr(r,'evaluate',broken)
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        run=receipt(db,p)
        r.record_fetch_outcome(db,p,c,run)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==1
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.source_id==source_id('tencent_web')))==1


def test_stale_projection_is_woken_without_releasing_active_builder(scenario,monkeypatch):
    from app.read_cache import cache_scope
    from app.read_cache_models import ReadCache
    monkeypatch.setattr(settings(),'read_cache_enabled',True)
    with Session(engine()) as db, db.begin():
        owner=db.get(User,scenario)
        scope=cache_scope(db,owner)
        lease=uuid.uuid4()
        entry=ReadCache(key='c'*64,owner_id=scenario,scope=scope,operation='workbench',
            params={'view':'overview'},dependency_keys=[],dirty=True,
            built_at=CLOCK-timedelta(minutes=5),accessed_at=CLOCK,refresh_after=CLOCK+timedelta(hours=1),
            lease_token=lease,lease_until=CLOCK+timedelta(seconds=60))
        db.add(entry);db.flush()
        r.projection_health(db,CLOCK)
        status=db.scalar(select(DataHealth).where(DataHealth.capability=='pipeline:projection'))
        assert status.state=='recovering' and status.next_action=='refresh_projection'
        assert entry.lease_token==lease and entry.refresh_after>CLOCK
        entry.lease_until=CLOCK-timedelta(seconds=1)
        r.projection_health(db,CLOCK)
        assert entry.refresh_after==CLOCK and entry.lease_token==lease
        entry.built_at=CLOCK;entry.dirty=False
        r.projection_health(db,CLOCK)
        assert status.state=='normal' and status.next_action is None


def test_projection_health_does_not_fetch_saved_page_bodies(scenario,monkeypatch):
    from sqlalchemy import event
    from app.read_cache import cache_scope
    from app.read_cache_models import ReadCache
    monkeypatch.setattr(settings(),'read_cache_enabled',True)
    body={'data':{'synthetic_saved_rows':['retained-local-value']*6000}}
    with Session(engine()) as db,db.begin():
        scope=cache_scope(db,db.get(User,scenario))
        for key,params in [('a'*64,{'view':'overview'}),('b'*64,{'day':'2026-10-26','codes':'600000.SH'})]:
            db.add(ReadCache(key=key,owner_id=scenario,scope=scope,operation='workbench' if key.startswith('a') else 'details',
                params=params,dependency_keys=[],payload=body,dirty=True,built_at=CLOCK-timedelta(minutes=5),
                accessed_at=CLOCK,refresh_after=CLOCK+timedelta(hours=1)))
    queries=[]
    def selected(conn,cursor,statement,parameters,context,executemany):
        if statement.lstrip().upper().startswith('SELECT') and 'read_cache_entries' in statement:
            queries.append(statement)
    event.listen(engine(),'before_cursor_execute',selected)
    try:
        with Session(engine()) as db,db.begin():
            r.projection_health(db,CLOCK)
            state=db.scalar(select(DataHealth).where(DataHealth.capability=='pipeline:projection'))
            assert state.state=='recovering' and state.missing_count==1
            entry=db.execute(select(ReadCache.refresh_after,ReadCache.lease_token).where(ReadCache.key=='a'*64)).one()
            assert entry.refresh_after==CLOCK and entry.lease_token is None
    finally:
        event.remove(engine(),'before_cursor_execute',selected)
    assert queries and all('read_cache_entries.payload' not in statement for statement in queries)
    with Session(engine()) as db:
        assert db.scalar(select(ReadCache.payload).where(ReadCache.key=='a'*64))==body
        assert db.scalar(select(ReadCache.refresh_after).where(ReadCache.key=='b'*64))==CLOCK+timedelta(hours=1)


def test_recovery_selection_reorders_only_covered_stock(scenario):
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        q,_=source(db,scenario,'tushare_rt_k')
        receipt(db,q)
        r.evaluate(db,p,c,receipt(db,p,state='error',reason='timeout'),CLOCK)
        from types import SimpleNamespace
        families=['tencent_web','tushare_rt_k']
        assert r.serving_order(db,'stock_quote',families,SimpleNamespace(**SYMBOL))==families[::-1]
        assert r.serving_order(db,'stock_quote',families,SimpleNamespace(**{**SYMBOL,'symbol':'600001'}))==families


def test_real_minute_storage_is_checked_as_bars_and_internal_gap_stays_visible(scenario):
    from decimal import Decimal
    from app.market.collect import store_minute_batch
    from app.market.minute import MinuteBatch,MinuteObservation
    from app.market.protocol import InstrumentKey
    from app.market.recovery_models import RecoveryCoverage
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario,'sina_minute_1m')
        batch=MinuteBatch(records=[MinuteObservation(InstrumentKey(**SYMBOL),'1m',CLOCK,
            Decimal('10'),Decimal('11'),Decimal('10'),Decimal('11'),Decimal('100'),None)],expected=1)
        assert store_minute_batch(db,p,c,batch,CLOCK)==(1,0)
        run=receipt(db,p,duplicates=1)
        r.evaluate(db,p,c,run,CLOCK)
        status=row(db,p)
        assert status.capability=='stock_minute_1m' and status.state=='normal'
        db.add(RecoveryCoverage(owner_id=scenario,source_id=p.id,dataset='minute.bars',scope_key='f'*64,
            scope={'stock':'600000'},trade_day=CLOCK.date(),interval='1m',state='partial',
            expected_count=2,actual_count=1,missing_ranges=[{'count':1}],checked_at=CLOCK,
            configuration_sha256='e'*64))
        db.flush()
        r.evaluate(db,p,c,run,CLOCK)
        assert status.state=='recovering' and status.next_action=='reconcile_history'
        assert status.missing_count==1


@pytest.mark.parametrize('native,expected',[(None,'recovering'),(CLOCK-timedelta(days=1),'recovering'),
    (CLOCK+timedelta(seconds=20),'recovering'),(CLOCK,'normal')])
def test_real_depth_parser_preserves_unknown_old_future_and_current_clocks(scenario,native,expected):
    import json
    from app.market.collect import store_reference_batch
    from app.market.protocol import InstrumentKey
    from app.market.tdx_data import parse_depth
    raw={'Buyp':['10']*5,'Buyv':['1']*5,'Sellp':['11']*5,'Sellv':['1']*5,
        'RefreshTime':native.strftime('%Y%m%d%H%M%S') if native else '093100'}
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario,'tdx_depth5')
        from app.market.fuyao import code
        body=json.dumps({'rows':{code(InstrumentKey(**SYMBOL)):raw}}).encode()
        batch=parse_depth(body,[InstrumentKey(**SYMBOL)],CLOCK)
        assert len(batch.records)==1
        assert store_reference_batch(db,p,c,batch,CLOCK)==(1,0)
        run=receipt(db,p,duplicates=1)
        r.evaluate(db,p,c,run,CLOCK)
        assert row(db,p).capability=='depth5' and row(db,p).state==expected


def test_historical_auction_final_snapshot_does_not_trigger_all_day_polls(scenario):
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario,'tushare_stk_auction',symbols=[])
        completed=CLOCK.replace(minute=35,second=40)
        receipt(db,p,completed,state='error',reason='timeout')
        future=CLOCK.replace(day=28,hour=9,minute=26)
        c.next_run_at=future
        r.wake(db,p,c,CLOCK.replace(hour=10))
        assert c.next_run_at==future


def test_old_success_cannot_validate_a_replaced_credential_binding(scenario,monkeypatch):
    from app.integrations.credentials import SourceCredentials
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        q,_=source(db,scenario,'tushare_rt_k')
        receipt(db,q)
        current=uuid.uuid4()
        monkeypatch.setattr('app.integrations.credentials.resolve_source_credentials',
            lambda db,ident,*a,**k:SourceCredentials(None,'https://example.invalid',current,2)
                if ident==q.id else SourceCredentials(None,'https://example.invalid'))
        r.evaluate(db,p,c,receipt(db,p,state='error',reason='timeout'),CLOCK)
        assert row(db,p).state=='recovering' and row(db,p).active_source_id is None


def test_health_check_cannot_hold_config_while_waiting_for_ingestion_cache_lock(scenario,monkeypatch):
    import threading
    from app.read_cache_models import ReadCache
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        receipt(db,p)
        r.target(db,p,c,'stock_quote',CLOCK.date(),CLOCK)
        db.add(ReadCache(key='d'*64,owner_id=scenario,scope='e'*64,operation='workbench',
            params={'view':'overview'},dependency_keys=['routing-health'],dirty=False,
            accessed_at=CLOCK,refresh_after=CLOCK,built_at=CLOCK))
    ready=threading.Event();errors=[]
    evaluate=r.evaluate
    def checked(*a,**k):
        evaluate(*a,**k);ready.set()
    monkeypatch.setattr(r,'evaluate',checked)
    def checker():
        try:r.reconcile_source(source_id('tencent_web'))
        except Exception as error:errors.append(type(error).__name__)
    with Session(engine()) as db, db.begin():
        from sqlalchemy import text
        db.execute(text("SET LOCAL lock_timeout='1s'"))
        db.scalar(select(ReadCache).where(ReadCache.key=='d'*64).with_for_update())
        thread=threading.Thread(target=checker,daemon=True);thread.start()
        assert ready.wait(3),'checker did not reach its independent health decision'
        config=db.get(SourceSettings,source_id('tencent_web'))
        config.next_run_at=CLOCK+timedelta(seconds=30)
        db.flush()  # Must not wait for a configuration lock held by the checker.
    thread.join(5)
    assert not thread.is_alive() and errors==[]
    with Session(engine()) as db:
        assert row(db,db.get(Provider,source_id('tencent_web'))).state=='normal'


def test_disabled_source_or_reconfigured_scope_does_not_keep_old_red_status(client,scenario):
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        r.evaluate(db,p,c,receipt(db,p,state='error',reason='timeout'),CLOCK)
        p.enabled=False
    data=client.get('/api/v1/data-recovery?day=2026-10-27').json()['data']
    assert data['health']==[] and data['health_state'] is None


def test_fresh_market_last_price_cannot_hide_a_missing_stock_from_the_batch(scenario):
    from app.market.tushare_market import QUOTE_FIELDS,encoded,parse_market
    from app.market.collect import store_reference_batch
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario,'tushare_market_rt',symbols=[])
        previous=CLOCK-timedelta(seconds=60)
        def snapshot(symbols,at):
            rows=[dict(ts_code=code,name='synthetic',pre_close='10',high='11',open='10',low='10',
                close='11',vol='100',amount='1000',trade_time=at.strftime('%Y-%m-%d %H:%M:%S')) for code in symbols]
            return parse_market(encoded(QUOTE_FIELDS.split(','),rows),at)
        store_reference_batch(db,p,c,snapshot(['600000.SH','600001.SH'],previous),previous)
        store_reference_batch(db,p,c,snapshot(['600000.SH'],CLOCK),CLOCK)
        run=receipt(db,p,duplicates=1)
        r.evaluate(db,p,c,run,CLOCK)
        assert row(db,p).state=='recovering' and row(db,p).missing_count==1
        assert row(db,p).reason=='market_scope_changed'


@pytest.mark.parametrize('untrusted',['private','manual'])
def test_non_source_history_cannot_inflate_the_market_coverage_comparison(scenario,untrusted):
    with Session(engine()) as db,db.begin():
        provider,config=source(db,scenario,'tushare_market_rt',symbols=[])
        for size,at,unqualified in ((1,CLOCK-timedelta(minutes=2),False),
                                   (5,CLOCK-timedelta(minutes=1),True),(1,CLOCK,False)):
            key=uuid.uuid4().hex
            payload=dict(dataset='tushare.rt_market',observed_day=CLOCK.date().isoformat(),rows=[
                dict(ts_code=f'{600000+index}.SH',close='11',trade_time=at.strftime('%Y-%m-%d %H:%M:%S'))
                for index in range(size)])
            private=unqualified and untrusted=='private'
            db.add(EvidenceVersion(source_id=provider.id,owner_id=scenario if private else None,
                visibility='private' if private else 'shared',source_key=key,source_revision=key,
                version_number=1,kind='reference',received_at=at,available_at=at,availability='known',
                provenance='manual' if unqualified and untrusted=='manual' else 'source_reported',
                historical_eligible=False,rights_basis='Synthetic only',payload=payload,content_hash=content_hash(payload)))
        db.flush()
        quality=r.saved_quality(db,provider,config,'stock_quote',CLOCK.date(),CLOCK,receipt(db,provider,duplicates=1))
        assert quality.valid and quality.reason is None


def test_stopped_monitor_cannot_keep_a_green_normal_badge(client,scenario,monkeypatch):
    with Session(engine()) as db, db.begin():
        p,c=source(db,scenario)
        r.evaluate(db,p,c,receipt(db,p),CLOCK)
    checked=CLOCK+timedelta(seconds=61)
    monkeypatch.setattr('app.market.recovery_api.now',lambda db:checked)
    data=client.get('/api/v1/data-recovery?day=2026-10-27').json()['data']
    assert data['health_state']=='recovering' and data['health'][0]['reason']=='health_check_overdue'
    assert datetime.fromisoformat(data['health'][0]['updated_at'])==CLOCK
