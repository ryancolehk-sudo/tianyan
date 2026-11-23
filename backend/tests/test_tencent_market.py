"""Synthetic native batches and isolated PostgreSQL; no supplier network."""
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion, SourceVersion
from app.market import tencent_market as qq
from app.market.collect import store_reference_batch
from app.market.models import SourceSettings
from app.market.protocol import InstrumentKey, Response, SHANGHAI, SourceFailure, quote_url
from app.market.read_models import quote_batch
from app.market.service import source_id
from app.models import Provider
from test_catalog_scope import native, save as save_catalog
from test_data_sources import configuration, configure, login

AT=datetime(2026,10,23,2,30,tzinfo=UTC)
STOCK=InstrumentKey('SSE','600001','stock')


def body(keys=(STOCK,), *, price='11', stamp=AT, missing=()):
    lines=[]
    for key in keys:
        if key.identity in missing:continue
        fields=['']*88
        for index,value in {1:'合成股票',2:key.symbol,3:price,4:'10',5:'10',6:'123',
                9:price,10:'2',19:price,20:'0',30:stamp.astimezone(SHANGHAI).strftime('%Y%m%d%H%M%S'),
                33:'11',34:'9',35:'10:29:59/123/1234567.89',47:'11',48:'9'}.items():fields[index]=value
        lines.append('v_'+key.vendor_key+'="'+'~'.join(fields)+'";')
    return '\n'.join(lines).encode('gb18030')


def configure_all(client):
    headers=login(client)
    for family in ('cninfo',qq.FAMILY):
        config=configuration(family)
        if family==qq.FAMILY:
            config.update(interval_seconds=15,collection_mode='full_market',
                rights=dict(display='allowed',cache='allowed',export='denied',redistribute='denied'))
        configure(client,headers,config,key='qq-test-config-'+family)
    with Session(engine()) as db,db.begin():
        for row in list(db.scalars(select(SourceVersion))):
            db.add(SourceVersion(source_id=row.source_id,recorded_at=AT-timedelta(days=2),
                descriptor=row.descriptor,content_hash=row.content_hash))
        save_catalog(db,native(('600001',)),AT-timedelta(minutes=1))
    return headers


def fetched(monkeypatch, scope, stamp=AT, *, price='11', failure_page=None, missing=()):
    calls=[]
    clock=[stamp-timedelta(milliseconds=100)]
    monkeypatch.setattr(qq,'observation_clock',lambda:clock[0])
    def request(url):
        index=len(calls);calls.append(url)
        if index==failure_page:raise SourceFailure('transport_error')
        keys=scope.instruments[index*qq.PAGE_SIZE:(index+1)*qq.PAGE_SIZE]
        clock[0]=stamp
        return Response(body(keys,price=price,stamp=stamp-timedelta(seconds=1),missing=missing),stamp,50,200)
    monkeypatch.setattr(qq,'fetch_https',request)
    response,batch=qq.fetch(scope,stamp.astimezone(SHANGHAI).date(),before_request=lambda:None)
    return response,batch,calls


def save_quote(db, monkeypatch, stamp=AT, price='11'):
    scope=qq.universe(db,stamp)
    response,batch,_=fetched(monkeypatch,scope,stamp,price=price)
    provider=db.get(Provider,source_id(qq.FAMILY))
    store_reference_batch(db,provider,db.get(SourceSettings,provider.id),batch,response.received_at)
    db.flush()
    return batch


def test_750_is_bulk_only_and_native_units_and_clocks_are_preserved(monkeypatch):
    keys=tuple(InstrumentKey('SSE',str(600000+i),'stock') for i in range(751))
    scope=qq.Universe(keys,uuid.uuid4(),uuid.uuid4(),AT,'previous_quote_batch','a'*64)
    with pytest.raises(SourceFailure,match='invalid_instrument_batch'):quote_url(list(keys[:201]))
    assert len(quote_url(list(keys[:750]),maximum=750))<9000
    response,batch,calls=fetched(monkeypatch,scope)
    assert len(calls)==2 and response.request_count==2 and batch.requests==2
    snapshot,manifest=[record[1] for record in batch.records]
    assert len(snapshot.rows)==751 and len(manifest.rows)==3
    row=snapshot.rows[0]
    assert row['vol']=='12300' and row['amount']=='1234567.89'
    assert row['bid_volume1']=='200' and row['ask_volume1']=='0'
    assert row['up_limit']=='11' and row['down_limit']=='9'
    assert row['trade_time']=='2026-10-23 10:29:59'
    assert set(row)==set(qq.FIELDS)
    coverage=qq.closed_coverage(manifest.model_dump(mode='json'),row['capture_id'],AT)
    assert coverage['expected']=='751' and coverage['stat_limit_up']=='751'
    assert coverage['snapshot_revision']==content_hash(snapshot)
    assert not qq.closed_coverage({**manifest.model_dump(mode='json'),'rows':manifest.rows[1:]},row['capture_id'],AT)


def test_physical_failure_and_prepermit_failure_count_only_real_requests(monkeypatch):
    keys=tuple(InstrumentKey('SSE',str(600000+i),'stock') for i in range(751))
    scope=qq.Universe(keys,uuid.uuid4(),uuid.uuid4(),AT,'previous_quote_batch','a'*64)
    with pytest.raises(SourceFailure,match='transport_error') as failed:fetched(monkeypatch,scope,failure_page=1)
    assert failed.value.request_count==2
    def denied():raise SourceFailure('permission_missing')
    monkeypatch.setattr(qq,'fetch_https',lambda *a:pytest.fail('No HTTP before permission'))
    with pytest.raises(SourceFailure,match='permission_missing') as failed:
        qq.fetch(scope,AT.date(),before_request=denied)
    assert failed.value.request_count==0


def test_partial_stock_rows_keep_denominator_and_native_clock_lead_remains_bounded(monkeypatch):
    other=InstrumentKey('SZSE','000001','stock')
    scope=qq.Universe((STOCK,other),uuid.uuid4(),uuid.uuid4(),AT,'previous_quote_batch','a'*64)
    _,batch,_=fetched(monkeypatch,scope,missing=(other.identity,))
    assert len(batch.records[0][1].rows)==1 and batch.rejected==1
    assert batch.records[1][1].rows[-1]['expected']=='2'
    page=Response(body(stamp=AT+timedelta(seconds=1)),AT,1,200)
    rows,rejected=qq.parse_page(page,[STOCK])
    assert len(rows)==1 and not rejected  # held until an actual later observation
    page=Response(body(stamp=AT+timedelta(seconds=61)),AT,1,200)
    rows,rejected=qq.parse_page(page,[STOCK])
    assert not rows and list(rejected.values())==['future_source_time']


def test_free_catalog_scope_is_explicit_not_listing_status_or_cumulative_identity(client):
    configure_all(client)
    with Session(engine()) as db:
        scope=qq.universe(db,AT)
        assert scope.kind=='public_catalog_listing_unknown' and scope.instruments==(STOCK,)
        assert qq.scope_allowed(db,scope,AT)
        assert not qq.scope_allowed(db,scope,AT+timedelta(days=3))


def test_shared_batch_serves_quote_rank_speed_and_independent_breadth(client,monkeypatch):
    configure_all(client)
    with Session(engine()) as db,db.begin():
        save_quote(db,monkeypatch,AT,price='10')
        save_quote(db,monkeypatch,AT+timedelta(minutes=5),price='11')
    monkeypatch.setattr('app.market.terminal_read.now',lambda db:AT+timedelta(minutes=5,seconds=2))
    from app.market.terminal_read import workbench_view
    from app.models import User
    with Session(engine()) as db:
        owner=db.scalar(select(User.id))
        result=workbench_view(db,owner,day=AT.date())
        stock=next(item for item in result.stocks if item.code=='600001.SH')
        assert stock.source_family==qq.FAMILY and stock.price==11
        assert stock.speed_5m==10 and stock.speed_window_seconds==300
        assert result.breadth.source_family==qq.FAMILY and result.breadth.limit_up==1
        assert len([point for point in result.breadth.points if point.up is not None])==2
        assert any(point.up is None for point in result.breadth.points)
        series=qq.breadth_series(db,AT.date(),AT+timedelta(minutes=6))
        assert series.source_family==qq.FAMILY and len(series.items)==2
        assert series.latest.limit_up==1 and series.latest.total_amount_cny=='1234567.89'
        ident=next(item.instrument_id for item in result.stocks if item.code=='600001.SH')
        quote=quote_batch(db,instrument_id=uuid.UUID(ident),observation_cutoff=AT+timedelta(minutes=5,seconds=2)).items[0]
        assert quote.provenance.family==qq.FAMILY and quote.quote.price=='11'
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.kind=='quote'))==0


@pytest.mark.parametrize('change',['scope_rights','quote_rights','missing_manifest','wrong_revision'])
def test_read_rechecks_scope_and_exact_closed_batch(client,monkeypatch,change):
    configure_all(client)
    with Session(engine()) as db,db.begin():
        scope=qq.universe(db,AT)
        response,batch,_=fetched(monkeypatch,scope)
        if change=='missing_manifest':batch.records=batch.records[:1]
        elif change=='wrong_revision':
            for row in batch.records[1][1].rows:row['snapshot_revision']='f'*64
        provider=db.get(Provider,source_id(qq.FAMILY))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),batch,response.received_at)
    with Session(engine()) as db,db.begin():
        if change in {'scope_rights','quote_rights'}:
            provider=db.get(Provider,source_id('cninfo' if change=='scope_rights' else qq.FAMILY))
            provider.rights={**provider.rights,'display':'denied'}
    with Session(engine()) as db:
        assert qq.breadth_series(db,AT.date(),AT+timedelta(seconds=1)) is None


def test_historical_receipts_and_native_ticks_do_not_use_future_batch(client,monkeypatch):
    configure_all(client)
    with Session(engine()) as db,db.begin():
        save_quote(db,monkeypatch,AT,price='10')
        save_quote(db,monkeypatch,AT+timedelta(minutes=5),price='11')
    with Session(engine()) as db:
        old=qq.breadth_series(db,AT.date(),AT+timedelta(seconds=1))
        assert len(old.items)==1 and old.latest.limit_up==0
        assert qq.breadth_series(db,AT.date(),AT-timedelta(seconds=1)) is None
        assert len(qq.breadth_series(db,AT.date(),AT+timedelta(days=10)).items)==2


def test_larger_aggregate_amount_is_validated_separately_from_single_stock_decimal(client,monkeypatch):
    configure_all(client)
    with Session(engine()) as db,db.begin():save_quote(db,monkeypatch)
    with Session(engine()) as db:
        meta=qq.snapshot_metadata(db,db.get(Provider,source_id(qq.FAMILY)),AT.date(),AT)[0]
        meta.coverage={**meta.coverage,'stat_total_amount_cny':'999999999999999999.00'}
        assert qq.comparison(db,meta,AT)['total_amount_cny']=='999999999999999999.00'
        meta.coverage['stat_total_amount_cny']='NaN'
        assert qq.comparison(db,meta,AT)=={}


def test_healthy_paid_source_stays_first_and_old_native_quotes_can_fail_over(client,monkeypatch):
    from app.market.market_series import choose
    from test_tushare_market import store, quote
    headers=configure_all(client)
    configure(client,headers,{**configuration('tushare_market_rt'),'interval_seconds':15},key='qq-paid-priority-config')
    with Session(engine()) as db,db.begin():
        save_quote(db,monkeypatch)
        store(db,'tushare_market_rt',[quote('600001.SH',at=AT-timedelta(minutes=2))],AT)
    with Session(engine()) as db:
        providers={row.family:row for row in db.scalars(select(Provider))}
        assert choose(db,providers,AT.date(),AT+timedelta(seconds=1))==qq.FAMILY
        assert choose(db,providers,AT.date(),AT+timedelta(seconds=1),historical=True)=='tushare_market_rt'
    with Session(engine()) as db,db.begin():
        store(db,'tushare_market_rt',[quote('600001.SH',at=AT+timedelta(seconds=10))],AT+timedelta(seconds=10))
    with Session(engine()) as db:
        providers={row.family:row for row in db.scalars(select(Provider))}
        assert choose(db,providers,AT.date(),AT+timedelta(seconds=11))=='tushare_market_rt'
        assert len(qq.breadth_series(db,AT.date(),AT+timedelta(seconds=11)).items)==1


@pytest.mark.parametrize('revoke_scope',[False,True])
def test_collector_uses_real_job_lease_and_final_scope_permission(client,monkeypatch,revoke_scope):
    from app.config import settings
    from app.market.collect import collect_job
    from app.market.models import DataFetchRun
    from app.market.service import enqueue_collection
    from app.worker import claim
    configure_all(client)
    stamp=datetime.now(UTC)
    with Session(engine()) as db,db.begin():save_catalog(db,native(('600001',)),stamp-timedelta(seconds=1))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr('app.market.collect.pool_day',lambda *a,**k:stamp.astimezone(SHANGHAI).date())
    clock=[stamp-timedelta(milliseconds=10)]
    monkeypatch.setattr(qq,'observation_clock',lambda:clock[0])
    calls=[]
    def request(url):
        calls.append(url)
        if revoke_scope:
            with Session(engine()) as db,db.begin():db.get(Provider,source_id('cninfo')).enabled=False
        clock[0]=stamp
        return Response(body(stamp=stamp-timedelta(seconds=1)),stamp,50,200)
    monkeypatch.setattr(qq,'fetch_https',request)
    with Session(engine()) as db,db.begin():
        from app.models import Job
        enqueue_collection(db,source_id(qq.FAMILY))
        db.flush()
        db.scalar(select(Job).where(Job.kind=='source.collect')).available_at=stamp-timedelta(minutes=1)
    job=claim('general');assert job is not None
    collect_job(job)
    with Session(engine()) as db:
        runs=list(db.scalars(select(DataFetchRun).where(DataFetchRun.source_id==source_id(qq.FAMILY))))
        assert len(calls)==len(runs)==1 and runs[0].request_count==1
        assert runs[0].status==('unavailable' if revoke_scope else 'complete')
        quotes=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.payload['dataset'].astext==qq.DATASET)))
        assert len(quotes)==(0 if revoke_scope else 1)


def test_regressed_page_clocks_are_retained_and_wait_for_actual_observation(monkeypatch):
    keys=tuple(InstrumentKey('SSE',str(600000+i),'stock') for i in range(751))
    scope=qq.Universe(keys,uuid.uuid4(),uuid.uuid4(),AT,'previous_quote_batch','a'*64)
    clock=[AT];monotonic=[0.];calls=[]
    monkeypatch.setattr(qq,'observation_clock',lambda:clock[0])
    monkeypatch.setattr(qq.timer,'monotonic',lambda:monotonic[0])
    def sleep(seconds):
        monotonic[0]+=seconds;clock[0]+=timedelta(seconds=seconds)
    monkeypatch.setattr(qq.timer,'sleep',sleep)
    def request(url):
        index=len(calls);calls.append(url)
        clock[0]=AT-timedelta(seconds=4-index)
        native=clock[0]+timedelta(seconds=1)
        return Response(body(keys[index*750:(index+1)*750],stamp=native),clock[0],50,200)
    monkeypatch.setattr(qq,'fetch_https',request)
    response,batch=qq.fetch(scope,AT.date(),before_request=lambda:None)
    assert response.received_at==AT and 2.9<=monotonic[0]<=3.1
    snapshot,manifest=[item[1] for item in batch.records]
    assert manifest.rows[0]['request_started_at']==AT.isoformat()
    assert datetime.fromisoformat(manifest.rows[0]['received_at'])==AT-timedelta(seconds=4)
    assert [row['clock_regressed'] for row in manifest.rows]==['true','true','true']
    capture=snapshot.rows[0]['capture_id']
    assert qq.closed_coverage(manifest.model_dump(mode='json'),capture,AT)
    assert max(datetime.fromisoformat(row['trade_time']).replace(tzinfo=SHANGHAI) for row in snapshot.rows)<=response.received_at
    altered=manifest.model_dump(mode='json')
    altered['rows'][0]['clock_regressed']='false'
    assert not qq.closed_coverage(altered,capture,AT)
    legacy=manifest.model_dump(mode='json')
    for row in legacy['rows']:
        for key in ('clock_policy','clock_regressed','clock_wait_ms'):row.pop(key)
    assert not qq.closed_coverage(legacy,capture,AT)  # no relabelling old invalid manifests


def test_native_tick_is_not_published_until_clock_catches_up(monkeypatch):
    scope=qq.Universe((STOCK,),uuid.uuid4(),uuid.uuid4(),AT,'previous_quote_batch','a'*64)
    clock=[AT];monotonic=[0.]
    monkeypatch.setattr(qq,'observation_clock',lambda:clock[0])
    monkeypatch.setattr(qq.timer,'monotonic',lambda:monotonic[0])
    def sleep(seconds):
        monotonic[0]+=seconds;clock[0]+=timedelta(seconds=seconds)
    monkeypatch.setattr(qq.timer,'sleep',sleep)
    monkeypatch.setattr(qq,'fetch_https',lambda url:Response(body(stamp=AT+timedelta(seconds=2)),AT,50,200))
    response,batch=qq.fetch(scope,AT.date(),before_request=lambda:None)
    assert response.received_at==AT+timedelta(seconds=2)
    assert datetime.fromisoformat(batch.records[-1][1].rows[-1]['stat_source_time_max'])==response.received_at
    assert 1.9<=monotonic[0]<=2.1
    clock[0]=AT;monotonic[0]=0
    monkeypatch.setattr(qq.timer,'sleep',lambda seconds:monotonic.__setitem__(0,monotonic[0]+seconds))
    with pytest.raises(SourceFailure,match='local_clock_unsettled') as failed:
        qq.fetch(scope,AT.date(),before_request=lambda:None)
    assert failed.value.request_count==1 and monotonic[0]==qq.MAX_CLOCK_WAIT


def test_shared_snapshot_scope_check_is_once_per_read_and_rechecked_after_revocation(client,monkeypatch):
    from app.models import Instrument
    configure_all(client)
    symbols=tuple(str(600001+i) for i in range(20))
    with Session(engine()) as db,db.begin():
        save_catalog(db,native(symbols),AT-timedelta(seconds=1))
        save_quote(db,monkeypatch)
    original=qq.saved_scope_allowed;calls=[]
    def checked(*args,**kwargs):calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(qq,'saved_scope_allowed',checked)
    with Session(engine()) as db:
        ids=list(db.scalars(select(Instrument.id).where(Instrument.symbol.in_(symbols))))
        assert len(ids)==20
        result=quote_batch(db,instrument_ids=ids,observation_cutoff=AT+timedelta(seconds=1))
        assert all(item.quote is not None for item in result.items) and len(calls)==1
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('cninfo'));provider.rights={**provider.rights,'display':'denied'}
    with Session(engine()) as db:
        result=quote_batch(db,instrument_ids=ids,observation_cutoff=AT+timedelta(seconds=1))
        assert all(item.quote is None for item in result.items) and len(calls)==2
