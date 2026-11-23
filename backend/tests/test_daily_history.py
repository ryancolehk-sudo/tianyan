"""Selected-history contract on disposable PostgreSQL; synthetic responses only."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collect import store_catalog_batch, store_reference_batch
from app.market.daily_history import enqueue_history
from app.market.datasets import bars
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import InstrumentKey, Response, SHANGHAI
from app.market.schemas import DailyHistoryRequest
from app.market.service import source_id, source_states
from app.market.tushare import parse_catalog, parse_reference
from app.models import AuditEvent, Instrument, Job, Provider, User
from app.worker import claim, process
from test_data_sources import configure, login
from test_tushare import body, source
from test_daily_minute_isolation import bulk_current, daily

FAMILY='tushare_daily'
KEY=InstrumentKey('SSE','603278','stock')
FIELDS=['ts_code','trade_date','open','high','low','close','pre_close','vol','amount']


def setup(client,monkeypatch):
    headers=login(client)
    for family in ('tushare_stock_basic',FAMILY,'tushare_index_daily'):
        configure(client,headers,source(family),key='history-'+family)
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr(settings(),'tushare_token',SecretStr('synthetic-history-test-only'))
    raw=body(['ts_code','symbol','name','exchange','list_status'],[
        ['603278.SH','603278','合成史料股票','SSE','L'],['600000.SH','600000','合成第二股票','SSE','L']])
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_stock_basic'))
        store_catalog_batch(db,provider,db.get(SourceSettings,provider.id),parse_catalog(raw),datetime.now(UTC))
        ident=db.scalar(select(Instrument.id).where(Instrument.exchange=='SSE',Instrument.symbol=='603278',Instrument.asset_type=='stock'))
        second=db.scalar(select(Instrument.id).where(Instrument.exchange=='SSE',Instrument.symbol=='600000',Instrument.asset_type=='stock'))
        index=db.scalar(select(Instrument.id).where(Instrument.exchange=='SSE',Instrument.symbol=='000001',Instrument.asset_type=='index'))
    return headers,ident,second,index


def enqueue(client,headers,ident,**kwargs):
    return client.post('/api/v1/market-history',json={'instrument_id':str(ident),**kwargs},
        headers={**headers,'Idempotency-Key':uuid.uuid4().hex})


def raw_days(days,code='603278.SH'):
    return body(FIELDS,[[code,day.strftime('%Y%m%d'),'10','13','9','11','10','100','120'] for day in days])


def fake_vendor(monkeypatch,raw,after=None):
    calls=[]
    def request(api,params,fields,*private):
        calls.append((api,dict(params),fields))
        if after:after()
        return Response(raw,datetime.now(UTC),1,200)
    monkeypatch.setattr('app.market.collect.request_tushare',request)
    return calls


def history_rows(db):
    return list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.kind=='bar').order_by(EvidenceVersion.source_key)))


def test_selected_second_stock_five_year_job_keeps_existing_source_configuration_and_schedule(client,monkeypatch):
    headers,ident,_,_=setup(client,monkeypatch)
    end=datetime.now(UTC).astimezone(SHANGHAI).date()
    days=[end-timedelta(days=900),end-timedelta(days=100),end-timedelta(days=1)]
    future=datetime.now(UTC)+timedelta(days=3)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(FAMILY));config.next_run_at=future
        original=(config.config,config.configured_at,config.interval_seconds,config.paused_reason)
    response=enqueue(client,headers,ident)
    assert response.status_code==202,response.text
    receipt=response.json()['data'];assert receipt['instrument_id']==str(ident)
    assert (date.fromisoformat(receipt['requested_end_day'])-date.fromisoformat(receipt['requested_start_day'])).days>=1825
    calls=fake_vendor(monkeypatch,raw_days(days))
    job=claim();assert job and str(job.id)==receipt['job_id']
    process(job)
    assert len(calls)==1 and calls[0][0]=='daily'
    assert calls[0][1]['ts_code']=='603278.SH' and calls[0][1]['limit']==6000
    with Session(engine()) as db:
        config=db.get(SourceSettings,source_id(FAMILY))
        assert (config.config,config.configured_at,config.interval_seconds,config.paused_reason)==original
        assert config.next_run_at==future
        rows=history_rows(db);assert len(rows)==3
        assert all(row.instrument_id==ident and not row.historical_eligible and row.available_at is None for row in rows)
        assert not db.scalar(select(EvidenceVersion.id).where(EvidenceVersion.source_id==source_id(FAMILY),EvidenceVersion.kind=='quote'))
        live=next(x for x in source_states(db) if x.family==FAMILY)
        assert live.last_attempt is None and live.last_success_at is None
    result=client.get('/api/v1/market-bars',params={'instrument_id':str(ident),'limit':2000}).json()['data']
    assert [item['payload']['trade_day'] for item in result['items']]==[str(day) for day in sorted(days)]
    assert result['coverage_start_day']==str(min(days)) and result['coverage_end_day']==str(max(days))
    assert result['source_family']==FAMILY and result['history_supported'] is True and result['has_more'] is False
    runs=client.get('/api/v1/data-fetch-runs',params={'job_id':receipt['job_id']}).json()['data']['items']
    assert len(runs)==1 and runs[0]['job_id']==receipt['job_id'] and runs[0]['status']=='complete'
    assert runs[0]['acquisition_mode']=='daily_history' and runs[0]['requested_instrument_code']=='603278.SH'
    assert runs[0]['requested_start_day']==receipt['requested_start_day'] and runs[0]['requested_end_day']==receipt['requested_end_day']


def test_history_get_and_refresh_cache_never_calls_supplier(client,monkeypatch):
    _,ident,_,_=setup(client,monkeypatch)
    calls=fake_vendor(monkeypatch,b'')
    for _ in range(2):
        result=client.get('/api/v1/market-bars',params={'instrument_id':str(ident),'include_live':True})
        assert result.status_code==200 and result.json()['data']['items']==[]
    assert calls==[]
    with Session(engine()) as db:assert not db.scalar(select(Job.id).where(Job.kind=='source.collect'))


def test_explicit_same_intent_reuses_job_other_instrument_or_range_conflicts(client,monkeypatch):
    headers,ident,second,_=setup(client,monkeypatch)
    first=enqueue(client,headers,ident)
    duplicate=enqueue(client,headers,ident)
    assert first.status_code==duplicate.status_code==202
    assert first.json()['data']['job_id']==duplicate.json()['data']['job_id']
    for args in ({'ident':second},{'ident':ident,'start_day':'2026-01-01'}):
        response=enqueue(client,headers,**args)
        assert response.status_code==409 and response.json()['error']['code']=='collection_intent_conflict'
    response=client.post('/api/v1/data-sources/'+str(source_id(FAMILY))+'/refresh',json={},
        headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
    assert response.status_code==409


def test_concurrent_identical_history_intent_has_one_job(client,monkeypatch):
    _,ident,_,_=setup(client,monkeypatch)
    def queued(_):
        with Session(engine(),expire_on_commit=False) as db,db.begin():
            owner=db.scalar(select(User).where(User.username=='owner'))
            return enqueue_history(db,owner,DailyHistoryRequest(instrument_id=ident)).id
    with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(queued,range(2)))
    assert ids[0]==ids[1]


@pytest.mark.parametrize('args,reason', [({'adjustment':'forward'},'history_adjustment_unsupported'),
    ({'start_day':'1990-01-01','end_day':'2026-01-01'},'history_range_invalid'),
    ({'start_day':'2026-02-01','end_day':'2026-01-01'},'history_range_invalid'),
    ({'end_day':'2099-01-01'},'future_day')])
def test_invalid_price_basis_and_range_fail_without_job_or_http(client,monkeypatch,args,reason):
    headers,ident,_,_=setup(client,monkeypatch)
    calls=fake_vendor(monkeypatch,b'')
    response=enqueue(client,headers,ident,**args)
    assert response.status_code==422 and response.json()['error']['code']==reason
    assert calls==[]
    with Session(engine()) as db:assert not db.scalar(select(Job.id).where(Job.kind=='source.collect'))


@pytest.mark.parametrize('change',['rights','adapter_version','interval_seconds','catalog_rights','catalog_disabled'])
def test_configuration_or_directory_rights_changed_while_queued_makes_zero_http(client,monkeypatch,change):
    headers,ident,_,_=setup(client,monkeypatch)
    response=enqueue(client,headers,ident);assert response.status_code==202
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_stock_basic' if change in {'catalog_rights','catalog_disabled'} else FAMILY))
        if change in {'rights','catalog_rights'}:provider.rights={**provider.rights,'cache':'denied'}
        elif change=='catalog_disabled':provider.enabled=False
        elif change=='adapter_version':provider.adapter_version='synthetic-changed'
        else:db.get(SourceSettings,provider.id).interval_seconds+=1
    calls=fake_vendor(monkeypatch,b'')
    job=claim();assert job
    process(job)
    assert calls==[]
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert run and run.request_count==0 and run.reason in {'configuration_changed','permission_missing','instrument_identity_unverified'}
        assert history_rows(db)==[]


def test_final_rights_recheck_discards_fetched_history(client,monkeypatch):
    headers,ident,_,_=setup(client,monkeypatch)
    assert enqueue(client,headers,ident).status_code==202
    def revoke():
        with Session(engine()) as db,db.begin():
            provider=db.get(Provider,source_id(FAMILY));provider.rights={**provider.rights,'display':'denied'}
    calls=fake_vendor(monkeypatch,raw_days([date(2025,1,2)]),after=revoke)
    process(claim())
    assert len(calls)==1
    with Session(engine()) as db:
        assert history_rows(db)==[]
        assert db.scalar(select(DataFetchRun)).reason in {'configuration_changed','permission_missing'}


def test_index_history_produces_point_bars_and_uses_index_source(client,monkeypatch):
    headers,_,_,index=setup(client,monkeypatch)
    response=enqueue(client,headers,index);assert response.status_code==202
    calls=fake_vendor(monkeypatch,raw_days([date(2025,1,2)],'000001.SH'))
    process(claim())
    assert calls[0][0]=='index_daily'
    result=client.get('/api/v1/market-bars',params={'instrument_id':str(index)}).json()['data']
    assert result['source_family']=='tushare_index_daily' and len(result['items'])==1
    assert result['items'][0]['payload']['price_unit']=='point'


def test_shared_persistent_budget_defers_second_history_without_moving_live_plan(client,monkeypatch):
    headers,ident,_,index=setup(client,monkeypatch)
    assert enqueue(client,headers,ident).status_code==202
    calls=fake_vendor(monkeypatch,raw_days([date(2025,1,2)]))
    process(claim())
    with Session(engine()) as db:
        first_permit=db.scalar(select(AuditEvent.created_at).where(AuditEvent.action=='source.request.tushare'))
    # Control only permit pacing, without relying on test-machine execution
    # taking less than two seconds. Worker/DB lease clocks remain real.
    monkeypatch.setattr('app.market.budget.now',lambda db:first_permit+timedelta(seconds=1))
    future=datetime.now(UTC)+timedelta(days=4)
    with Session(engine()) as db,db.begin():db.get(SourceSettings,source_id('tushare_index_daily')).next_run_at=future
    assert enqueue(client,headers,index).status_code==202
    job=claim();assert job
    process(job)
    with Session(engine()) as db:
        queued=db.get(Job,job.id)
        assert queued.state=='queued' and queued.error_code=='source_budget_wait' and queued.attempts==0
        assert db.get(SourceSettings,source_id('tushare_index_daily')).next_run_at==future
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action=='source.request.tushare'))==1
    assert len(calls)==1


def test_daily_date_order_and_before_pagination_precede_limit_not_receipt_order(client,monkeypatch):
    _,ident,_,_=setup(client,monkeypatch)
    days=[date(2000,1,1)+timedelta(days=i) for i in range(2301)]
    with Session(engine()) as db,db.begin():bulk_current(db,ident,FAMILY,[daily(day=day) for day in reversed(days)])
    with Session(engine()) as db:
        first=bars(db,ident,'unadjusted',limit=2000)
        assert len(first.items)==2000 and first.has_more
        assert [item.payload.trade_day for item in first.items]==days[-2000:]
        second=bars(db,ident,'unadjusted',before=first.items[0].payload.trade_day,limit=2000)
        assert [item.payload.trade_day for item in second.items]==days[:-2000] and not second.has_more
        assert first.coverage_start_day==min(days) and first.coverage_end_day==max(days)
        window=bars(db,ident,'unadjusted',start_day=days[100],end_day=days[120],limit=100)
        assert [item.payload.trade_day for item in window.items]==days[100:121]


def test_paid_series_never_fills_missing_dates_from_other_provider(client,monkeypatch):
    headers,ident,_,_=setup(client,monkeypatch)
    free=source('tushare_daily');free['family']='baostock_history';free['interval_seconds']=21600
    configure(client,headers,free,key='history-free')
    with Session(engine()) as db,db.begin():
        bulk_current(db,ident,FAMILY,[daily(day=date(2020,1,2))])
        bulk_current(db,ident,'baostock_history',[daily(day=date(2020,1,2),close='12'),daily(day=date(2020,1,3))])
    with Session(engine()) as db:
        paid=bars(db,ident,'unadjusted',source_id=source_id(FAMILY))
        assert paid.source_family==FAMILY and len(paid.items)==1
        free=bars(db,ident,'unadjusted',source_id=source_id('baostock_history'))
        assert free.source_family=='baostock_history' and len(free.items)==2


def test_duplicate_source_dates_and_wrong_instrument_do_not_write_bars(client,monkeypatch):
    headers,ident,_,_=setup(client,monkeypatch)
    assert enqueue(client,headers,ident).status_code==202
    calls=fake_vendor(monkeypatch,raw_days([date(2025,1,2),date(2025,1,2)]))
    process(claim())
    with Session(engine()) as db:
        assert history_rows(db)==[]
        assert db.scalar(select(DataFetchRun)).reason=='duplicate_source_day'
    assert len(calls)==1


def test_daily_a_b_a_keeps_new_observation_while_adjacent_same_payload_dedupes(client,monkeypatch):
    _,ident,_,_=setup(client,monkeypatch)
    stamp=datetime.now(UTC)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(FAMILY));config=db.get(SourceSettings,provider.id)
        for offset,close in enumerate(['11','11','12','11']):
            raw=raw_days([date(2025,1,2)]).replace(b'"11"',('"'+close+'"').encode())
            batch=parse_reference(raw,FAMILY,stamp+timedelta(seconds=offset),KEY)
            store_reference_batch(db,provider,config,batch,stamp+timedelta(seconds=offset))
        rows=history_rows(db)
        rows.sort(key=lambda row:row.version_number)
        assert [row.version_number for row in rows]==[1,2,3]
        assert [row.payload['close'] for row in rows]==['11','12','11']
        assert rows[2].supersedes_id==rows[1].id
        assert all(row.instrument_id==ident for row in rows)
    # Advance only this read's cutoff past the synthetic receipt sequence.
    monkeypatch.setattr('app.market.daily_history.now',lambda db:stamp+timedelta(seconds=4))
    with Session(engine()) as db:
        view=bars(db,ident,'unadjusted')
        assert len(view.items)==1 and view.items[0].payload.close=='11'


def test_history_post_requires_admin_csrf_and_unknown_identity_is_not_queried(client,monkeypatch):
    headers,ident,_,_=setup(client,monkeypatch)
    assert enqueue(client,headers,uuid.uuid4()).status_code==422
    result=client.post('/api/v1/market-history',json={'instrument_id':str(ident)},
        headers={'Origin':'http://127.0.0.1:8080','Idempotency-Key':uuid.uuid4().hex})
    assert result.status_code==403
    with Session(engine()) as db,db.begin():db.scalar(select(User).where(User.username=='owner')).role='researcher'
    assert enqueue(client,headers,ident).status_code==403


@pytest.mark.parametrize('setting,value,reason',[('data_collection_enabled',False,'collection_disabled'),('tushare_token',None,'credentials_missing')])
def test_history_capability_reflects_local_collection_and_credential_state(client,monkeypatch,setting,value,reason):
    _,ident,_,_=setup(client,monkeypatch)
    monkeypatch.setattr(settings(),setting,value)
    result=client.get('/api/v1/market-bars',params={'instrument_id':str(ident)}).json()['data']
    assert result['history_supported'] is False and result['history_reason']==reason


def test_native_eod_capability_converts_to_bars_without_reconfiguring_legacy_sources(client,monkeypatch):
    headers,ident,_,_=setup(client,monkeypatch)
    with Session(engine()) as db:
        provider=db.get(Provider,source_id(FAMILY))
        assert provider.capabilities==['quote.eod']
    result=client.get('/api/v1/market-bars',params={'instrument_id':str(ident)}).json()['data']
    assert result['history_supported'] is True
    assert enqueue(client,headers,ident).status_code==202


def test_missing_native_history_capability_blocks_collection_and_hides_its_bars(client,monkeypatch):
    headers,ident,_,_=setup(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        bulk_current(db,ident,FAMILY,[daily(day=date(2020,1,2))])
        provider=db.get(Provider,source_id(FAMILY));provider.capabilities=[]
    result=client.get('/api/v1/market-bars',params={'instrument_id':str(ident)}).json()['data']
    assert result['items']==[] and result['history_supported'] is False
    assert result['history_reason']=='capability_not_implemented'
    assert enqueue(client,headers,ident).status_code==422
