"""Bounded focused quotes on isolated PostgreSQL; no supplier network."""
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.market.collect import store_quote_batch
from app.market.models import SourceSettings, Watchlist
from app.market.protocol import InstrumentKey, Quote, QuoteBatch, parse_tencent_quotes
from app.market.quote_focus import LIFETIME, MAX_BATCH, focused_selection
from app.market.read_models import quote_batch
from app.market.service import source_id
from app.market.terminal_read import selected_quote_projection
from app.market.quote_speed import quote_speeds
from app.models import Instrument, Job, Provider, User
from app.read_cache_models import ReadCache
from test_data_sources import configuration, configure, login, quote_body

CLOCK=datetime(2026,11,10,2,30,tzinfo=UTC)
STOCK=InstrumentKey('SZSE','000001','stock')


def setup(client,monkeypatch):
    headers=login(client)
    monkeypatch.setattr('app.market.service.now',lambda db:CLOCK-timedelta(days=1))
    body={**configuration(), 'symbols':[], 'collection_mode':'focused', 'interval_seconds':5}
    configure(client,headers,body)
    monkeypatch.setattr('app.market.quote_focus.now',lambda db:CLOCK)
    with Session(engine()) as db,db.begin():
        owner=db.scalar(select(User))
        instrument=Instrument(id=uuid.uuid4(),exchange=STOCK.exchange,symbol=STOCK.symbol,asset_type='stock',
            name='合成股票',valid_from=None,valid_to=None,first_received_at=CLOCK-timedelta(days=1))
        db.add(instrument);db.flush()
        return headers,owner.id,instrument.id


def interest(client,headers,**changes):
    body=dict(context_id=str(uuid.uuid4()),panel='selected',trade_day=CLOCK.date().isoformat(),codes=['000001.SZ'])
    return client.put('/api/v1/quote-focus',json={**body,**changes},headers=headers)


def save_quote(db,at,price='12',*,family='tencent_web',received=None):
    provider=db.get(Provider,source_id(family));config=db.get(SourceSettings,provider.id)
    quote=Quote(instrument=STOCK,name='合成股票',price=Decimal(price),previous_close=Decimal('10'),
        open=Decimal('10'),high=Decimal('13'),low=Decimal('9'),quote_time=at,received_at=received or at,
        price_unit='CNY',volume_native=Decimal('123'),volume_shares=Decimal('12300'),amount_cny=Decimal('123000'))
    store_quote_batch(db,provider,config,QuoteBatch(records=[quote],expected=1))


def reader(db,cutoff=CLOCK):
    return SimpleNamespace(db=db,cutoff=cutoff,record_input=lambda evidence:None)


def test_enabled_focus_keeps_complete_workbench_payload_valid(client,monkeypatch):
    from app.market.terminal_read import workbench_view
    _,owner,_=setup(client,monkeypatch)
    monkeypatch.setattr('app.market.terminal_read.now',lambda db:CLOCK)
    with Session(engine()) as db:
        result=workbench_view(db,owner,view='overview')
        capability=next(item for item in result.capabilities if item.key=='focused_quotes')
        assert capability.status=='available' and capability.source_family=='tencent_web'
        assert capability.reason
        assert result.model_dump(mode='json')['view']=='overview'


def test_interest_is_authenticated_csrf_checked_and_never_collects(client,monkeypatch):
    assert client.put('/api/v1/quote-focus',json={}).status_code==401
    headers,owner,instrument=setup(client,monkeypatch)
    assert interest(client,{}).status_code==403
    response=interest(client,headers)
    assert response.status_code==200,response.text
    assert response.json()['data']==dict(active=True,accepted_count=1,expires_at=(CLOCK+LIFETIME).isoformat().replace('+00:00','Z'))
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job))==0
        row=db.scalar(select(ReadCache).where(ReadCache.operation=='quote-focus'))
        assert row.owner_id==owner and not row.dirty and row.dependency_keys==[]
        assert focused_selection(db,db.get(SourceSettings,source_id('tencent_web')),CLOCK)==[STOCK]


def test_interest_never_invents_identity_or_accepts_historical_focus(client,monkeypatch):
    headers,_,_=setup(client,monkeypatch)
    assert interest(client,headers,codes=['000001']).status_code==422
    assert interest(client,headers,codes=['999999.SZ']).status_code==422
    assert interest(client,headers,codes=['000001.SZ']*51).status_code==422
    assert interest(client,headers,codes=['000099.SZ']).json()['data']['accepted_count']==0
    context=str(uuid.uuid4())
    assert interest(client,headers,context_id=context).json()['data']['accepted_count']==1
    response=interest(client,headers,context_id=context,as_of_time=CLOCK.isoformat())
    assert response.json()['data']['active'] is False
    with Session(engine()) as db:
        assert focused_selection(db,db.get(SourceSettings,source_id('tencent_web')),CLOCK)==[]


def test_focus_scope_expires_and_is_isolated_from_another_owner(client,monkeypatch):
    headers,owner,instrument=setup(client,monkeypatch)
    assert interest(client,headers).status_code==200
    with Session(engine()) as db:
        config=db.get(SourceSettings,source_id('tencent_web'))
        assert not focused_selection(db,config,CLOCK+LIFETIME+timedelta(seconds=1))
        other=SimpleNamespace(owner_id=uuid.uuid4(),interval_seconds=config.interval_seconds,config=config.config)
        assert not focused_selection(db,other,CLOCK)


def test_many_contexts_are_bounded_but_existing_context_can_renew(client,monkeypatch):
    headers,_,_=setup(client,monkeypatch)
    context=str(uuid.uuid4())
    assert interest(client,headers,context_id=context).status_code==200
    for _ in range(15):assert interest(client,headers).status_code==200
    assert interest(client,headers).status_code==429
    assert interest(client,headers,context_id=context).status_code==200


def test_large_focus_pool_rotates_with_current_pick_retained_and_batch_capped(client,monkeypatch):
    headers,owner,instrument=setup(client,monkeypatch)
    assert interest(client,headers).status_code==200
    with Session(engine()) as db,db.begin():
        records=[Instrument(id=uuid.uuid4(),exchange='SZSE',symbol=f'{code:06}',asset_type='stock',
            name=f'合成{code}',valid_from=None,valid_to=None,first_received_at=CLOCK) for code in range(100,550)]
        db.add_all(records);db.flush()
        db.add(Watchlist(owner_id=owner,instrument_ids=[str(row.id) for row in records[:100]],updated_at=CLOCK));db.flush()
    for start in range(200,550,50):
        assert interest(client,headers,panel='sector' if start<400 else 'ranking',
            codes=[f'{code:06}.SZ' for code in range(start,start+50)]).status_code==200
    with Session(engine()) as db:
        config=db.get(SourceSettings,source_id('tencent_web'))
        first=focused_selection(db,config,CLOCK)
        batches=[focused_selection(db,config,CLOCK+timedelta(seconds=step*5)) for step in range(4)]
        all_seen={item.identity for batch in batches for item in batch}
        assert len(first)==MAX_BATCH==200 and first[0]==STOCK and len(all_seen)==451
        assert all(len(batch)==200 and batch[0]==STOCK and len(set(batch))==200 for batch in batches)


def test_current_sector_ranking_and_watchlist_share_one_200_stock_request(client,monkeypatch):
    headers,owner,instrument=setup(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        records=[Instrument(id=uuid.uuid4(),exchange='SZSE',symbol=f'{code:06}',asset_type='stock',
            name=f'合成{code}',valid_from=None,valid_to=None,first_received_at=CLOCK) for code in range(100,299)]
        db.add_all(records);db.flush()
        db.add(Watchlist(owner_id=owner,instrument_ids=[str(row.id) for row in records[100:]],updated_at=CLOCK))
    assert interest(client,headers).status_code==200
    for panel,start in [('sector',100),('ranking',150)]:
        assert interest(client,headers,panel=panel,codes=[f'{code:06}.SZ' for code in range(start,start+50)]).json()['data']['accepted_count']==50
    with Session(engine()) as db:
        selected=focused_selection(db,db.get(SourceSettings,source_id('tencent_web')),CLOCK)
        assert selected[0]==STOCK and len(selected)==200
        assert {item.symbol for item in selected}=={'000001',*(f'{code:06}' for code in range(100,299))}


def test_configured_focus_is_optional_and_other_sources_keep_their_floors(client,monkeypatch):
    headers,_,_=setup(client,monkeypatch)
    wrong={**configuration('szse_public'),'collection_mode':'focused'}
    response=client.post('/api/v1/data-sources',json=wrong,headers={**headers,'Idempotency-Key':str(uuid.uuid4())})
    assert response.status_code==422
    body={**configuration(),'interval_seconds':2}
    assert client.post('/api/v1/data-sources',json=body,headers={**headers,'Idempotency-Key':str(uuid.uuid4())}).status_code==422


def test_quote_slot_cannot_claim_slow_pagination_or_general_jobs(client,monkeypatch):
    from app.worker import claim
    _,owner,_=setup(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        slow=Job(owner_id=owner,kind='source.collect',payload={'source_id':str(source_id('ths_industry_funds'))},state='queued')
        fast=Job(owner_id=owner,kind='source.collect',payload={'source_id':str(source_id('tencent_web'))},state='queued')
        db.add_all([slow,fast]);db.flush();expected=fast.id
    assert claim('realtime',quotes_only=True).id==expected
    assert claim('realtime',quotes_only=True) is None
    with pytest.raises(ValueError):claim('general',quotes_only=True)


def test_expired_interest_between_enqueue_and_collection_makes_no_http_request(client,monkeypatch):
    from app.config import settings
    from app.worker import claim,process
    from app.market.models import DataFetchRun
    _,owner,_=setup(client,monkeypatch)
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr('app.market.collect.fetch_https',lambda *a:pytest.fail('Expired focus must not fetch'))
    with Session(engine()) as db,db.begin():
        job=Job(owner_id=owner,kind='source.collect',payload={'source_id':str(source_id('tencent_web'))},state='queued')
        db.add(job);db.flush();identity=job.id
    process(claim('realtime',quotes_only=True))
    with Session(engine()) as db:
        assert db.get(Job,identity).state=='succeeded'
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==0


def test_focus_rows_never_enter_the_projection_worker(client,monkeypatch):
    from app.read_cache import run_one
    headers,_,_=setup(client,monkeypatch)
    interest(client,headers)
    with Session(engine()) as db,db.begin():
        db.scalar(select(ReadCache)).dirty=True
    with Session(engine()) as db:
        assert run_one(db) is False


@pytest.mark.parametrize('queue',['all','quotes','fast'])
def test_worker_main_dispatches_only_the_explicit_fast_slot(monkeypatch,queue):
    import threading
    from app import worker
    stop=threading.Event();claims=[];dispatched=[]
    monkeypatch.setattr(worker.threading,'Event',lambda:stop)
    monkeypatch.setattr(worker.signal,'signal',lambda *args:None)
    monkeypatch.setattr(worker,'heartbeat',lambda *args,**kwargs:None)
    monkeypatch.setattr('app.worker_watchdog.ProgressWatchdog',lambda role:SimpleNamespace(
        start=lambda:None,advance=lambda:None,close=lambda:None))
    monkeypatch.setattr('app.market.service.schedule_sources',lambda **kwargs:dispatched.append(kwargs))
    def claim(role,**kwargs):
        claims.append((role,kwargs));stop.set()
    monkeypatch.setattr(worker,'claim',claim)
    worker.main(['--role','realtime','--queue',queue])
    assert claims==[('realtime',{'quotes_only':True} if queue=='quotes' else {'fast_only':True} if queue=='fast' else {})]
    assert bool(dispatched)==(queue!='all')
    if queue!='all':
        from app.market.cadence_policy import QUOTE_FAMILIES,FAST_FEED_FAMILIES
        assert dispatched==[{'families':QUOTE_FAMILIES if queue=='quotes' else FAST_FEED_FAMILIES}]


def test_focused_quote_projection_keeps_source_units_and_computes_real_same_source_speed(client,monkeypatch):
    _,_,instrument=setup(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        save_quote(db,CLOCK-timedelta(seconds=300),'10')
        save_quote(db,CLOCK,'11')
    with Session(engine()) as db:
        projected=selected_quote_projection(reader(db),CLOCK.date())
        assert set(projected)=={'000001.SZ'}
        assert projected['000001.SZ'].quote.amount_cny=='123000'
        assert quote_speeds(reader(db),projected)=={'000001.SZ':(pytest.approx(10),300)}
        early=reader(db,CLOCK-timedelta(seconds=1))
        earlier=selected_quote_projection(early,CLOCK.date())
        assert earlier['000001.SZ'].quote.price=='10'
        assert quote_speeds(early,earlier)=={}


def test_batched_read_keeps_source_version_at_each_quotes_own_receipt(client,monkeypatch):
    from app.evidence.common import content_hash
    from app.evidence.models import SourceVersion
    _,_,_=setup(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tencent_web'));config=db.get(SourceSettings,provider.id)
        original=db.scalar(select(SourceVersion).where(SourceVersion.source_id==provider.id))
        original_id=original.id
        save_quote(db,CLOCK-timedelta(seconds=300),'10')
        descriptor={**original.descriptor,'name':'合成来源修订'}
        version=SourceVersion(source_id=provider.id,recorded_at=CLOCK-timedelta(seconds=100),
            descriptor=descriptor,content_hash=content_hash(descriptor))
        db.add(version);db.flush();new_id=version.id
        sample=Quote(instrument=InstrumentKey('SZSE','000002','stock'),name='合成另一股票',
            price=Decimal('11'),previous_close=Decimal('10'),open=Decimal('10'),high=Decimal('12'),low=Decimal('9'),
            quote_time=CLOCK,received_at=CLOCK,price_unit='CNY')
        store_quote_batch(db,provider,config,QuoteBatch(records=[sample],expected=1))
    with Session(engine()) as db:
        projected=selected_quote_projection(reader(db),CLOCK.date())
        assert projected['000001.SZ'].provenance.source_version_id==original_id
        assert projected['000002.SZ'].provenance.source_version_id==new_id
        assert projected['000002.SZ'].provenance.source_name=='合成来源修订'
        old=selected_quote_projection(reader(db,CLOCK-timedelta(seconds=150)),CLOCK.date(),strict=True)
        assert set(old)=={'000001.SZ'} and old['000001.SZ'].provenance.source_version_id==original_id


def test_five_minute_speed_never_uses_late_receipt_or_lunch(client,monkeypatch):
    _,_,instrument=setup(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        save_quote(db,CLOCK,'11')
        save_quote(db,CLOCK-timedelta(seconds=300),'10',received=CLOCK+timedelta(seconds=1))
    with Session(engine()) as db:
        projected=selected_quote_projection(reader(db),CLOCK.date())
        assert quote_speeds(reader(db),projected)=={}
    lunch=datetime(2026,11,10,5,2,tzinfo=UTC)
    with Session(engine()) as db,db.begin():
        save_quote(db,lunch-timedelta(seconds=300),'10')
        save_quote(db,lunch,'11')
    with Session(engine()) as db:
        projected=selected_quote_projection(reader(db,lunch),CLOCK.date())
        assert quote_speeds(reader(db,lunch),projected)=={}


def test_live_overlay_does_not_expand_to_every_stock_visited_during_the_day(client,monkeypatch):
    _,_,_=setup(client,monkeypatch)
    from dataclasses import replace
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tencent_web'));config=db.get(SourceSettings,provider.id)
        sample=Quote(instrument=STOCK,name='合成股票',price=Decimal('10'),previous_close=Decimal('9'),open=Decimal('9'),
            high=Decimal('11'),low=Decimal('8'),quote_time=CLOCK,received_at=CLOCK,price_unit='CNY')
        batch=QuoteBatch(records=[replace(sample,instrument=InstrumentKey('SZSE',f'{code:06}','stock'))
            for code in range(100,451)],expected=351)
        store_quote_batch(db,provider,config,batch)
    with Session(engine()) as db:
        assert len(selected_quote_projection(reader(db),CLOCK.date()))==200
        assert len(selected_quote_projection(reader(db),CLOCK.date(),strict=True))==351


def test_native_tencent_stock_units_do_not_impose_stock_lots_on_indices():
    fields=quote_body(CLOCK.strftime('%Y%m%d%H%M%S')).decode('gb18030').splitlines()
    rows=[]
    for line in fields:
        key,raw=line.split('="');parts=raw[:-2].split('~');parts[35]='12/123/1234500';parts[37]='123.45'
        rows.append(key+'="'+'~'.join(parts)+'";')
    batch=parse_tencent_quotes('\n'.join(rows).encode('gb18030'),[InstrumentKey('SSE','000001','index'),STOCK],CLOCK)
    assert len(batch.records)==2
    assert batch.records[0].volume_shares is None
    assert batch.records[0].amount_cny is None
    assert batch.records[1].volume_shares==Decimal('12300')
    assert batch.records[1].amount_cny==Decimal('1234500')


@pytest.mark.parametrize('seconds,family',[(2,'tencent_web'),(60,'tushare_rt_k')])
def test_focus_can_only_override_preferred_source_with_recent_native_time(client,monkeypatch,seconds,family):
    headers,_,instrument=setup(client,monkeypatch)
    configure(client,headers,{**configuration('tushare_rt_k'),'symbols':[dict(exchange='SZSE',symbol='000001',asset_type='stock')],
        'interval_seconds':15},key='preferred-quote')
    with Session(engine()) as db,db.begin():
        save_quote(db,CLOCK-timedelta(seconds=seconds+10),'10',family='tushare_rt_k')
        save_quote(db,CLOCK-timedelta(seconds=seconds),'11')
    with Session(engine()) as db:
        item=quote_batch(db,instrument_id=instrument,observation_cutoff=CLOCK).items[0]
        assert item.provenance.family==family


def test_quote_request_forecast_retains_quiet_session_boundaries():
    from app.market.cadence_policy import remaining_requests,automatic_interval
    from app.market.realtime_schedule import align_realtime
    opening=datetime(2026,11,10,1,15,tzinfo=UTC)
    expected={'tushare_market_rt':981,'tushare_rt_k':261,'tushare_rt_indices':261}
    assert {family:remaining_requests(family,15 if family=='tushare_market_rt' else 60,opening)
        for family in expected}==expected
    assert sum(expected.values())==1503
    assert automatic_interval('tencent_web',5,CLOCK)==5
    assert automatic_interval('tushare_market_rt',15,opening)==60
    lunch=datetime(2026,11,10,4,tzinfo=UTC)
    assert align_realtime('tencent_web',lunch)==datetime(2026,11,10,5,tzinfo=UTC)
    assert align_realtime('ths_concept_funds',lunch)==lunch
