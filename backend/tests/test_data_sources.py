"""Real PostgreSQL, synthetic data only. Never grants rights to an actual vendor."""
import calendar
import json
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.cli import bootstrap
from app.config import settings
from app.db import engine
from app.evidence.common import content_hash, normalized
from app.evidence.models import EvidenceVersion, SourceVersion
from app.evidence.schemas import QuotePayload
from app.market.collect import store_catalog_batch, store_quote_batch
from app.market.models import DataFetchRun, InstrumentOrigin, SourceSettings
from app.market.protocol import SHANGHAI, InstrumentKey, Response, SourceFailure, parse_cninfo_catalog, parse_tencent_quotes
from app.market.service import schedule_sources, source_id
from app.models import Instrument, Job, Provider, User
from app.worker import claim, process

ORIGIN={'Origin':'http://127.0.0.1:8080'}
PASSWORD='synthetic-source-test-fixture-only'


def login(client,role='admin'):
    bootstrap('owner',PASSWORD)
    with Session(engine()) as db, db.begin():
        db.scalar(select(User).where(User.username=='owner')).role=role
    response=client.post('/api/v1/session',json={'username':'owner','password':PASSWORD},headers=ORIGIN)
    assert response.status_code==201
    return {**ORIGIN,'X-CSRF-Token':response.json()['data']['csrf_token']}


def configuration(family='tencent_web',enabled=True):
    return {'family':family,'symbols':[{'exchange':'SSE','symbol':'000001','asset_type':'index'},
        {'exchange':'SZSE','symbol':'000001','asset_type':'stock'}] if family=='tencent_web' else [],
        'interval_seconds':60 if family=='tencent_web' else 3600,'rights_basis':'Self-authored synthetic fixtures for this isolated test only; no upstream grant.',
        'rights':{'display':'allowed','cache':'allowed','export':'unknown','redistribute':'unknown'},'entitlement_status':'verified','enabled':enabled}


def configure(client,headers,body=None,key='source-config-001'):
    response=client.post('/api/v1/data-sources',json=body or configuration(),headers={**headers,'Idempotency-Key':key})
    assert response.status_code==201,response.text
    return response


def quote_body(clock=None,price='12.30'):
    clock=clock if clock is not None else datetime.now(UTC).astimezone(SHANGHAI).strftime('%Y%m%d%H%M%S')
    rows=[]
    for key,name in [('sh000001','合成指数'),('sz000001','合成股票')]:
        fields=['']*88
        for index,value in {1:name,2:'000001',3:price,4:'12',5:'12.10',6:'123',30:clock,33:'12.50',34:'12'}.items():
            fields[index]=value
        rows.append(f'v_{key}="'+'~'.join(fields)+'";')
    return '\n'.join(rows).encode('gb18030')


def queue_and_process(client,headers,monkeypatch,fetch):
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr('app.market.collect.fetch_https',fetch)
    response=client.post(f'/api/v1/data-sources/{source_id("tencent_web")}/refresh',json={},headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
    assert response.status_code==202,response.text
    job=claim(); assert job and str(job.id)==response.json()['data']['job_id']
    process(job)
    return job


def test_source_endpoints_are_authenticated_and_admin_writes_are_enforced(client):
    for url in ['/api/v1/data-sources','/api/v1/data-fetch-runs']:
        assert client.get(url).status_code==401
    headers=login(client,'viewer')
    assert client.get('/api/v1/data-sources').json()['data']['items']==[]
    assert client.post('/api/v1/data-sources',json=configuration(),headers={**headers,'Idempotency-Key':'source-forbidden'}).status_code==403


def test_implemented_adapters_are_separate_from_configuration_and_usage_is_measured(client):
    headers=login(client)
    index=client.get('/api/v1/data-sources').json()['data']
    from app.market.news_feeds import NEWS_FEEDS
    from app.market.tushare_specs import SPECS as TUSHARE_SPECS
    from app.market.eastmoney_trading import FAMILIES as EASTMONEY_FAMILIES
    from app.market.review_protocol import PUBLIC_FAMILIES
    from app.market.connected_specs import FAMILIES as CONNECTED_FAMILIES
    from app.market.direction_specs import PUBLIC_FAMILIES as DIRECTION_FAMILIES
    assert index['items']==[] and len(index['available_adapters'])==30 + len(NEWS_FEEDS) + len(TUSHARE_SPECS) + len(EASTMONEY_FAMILIES) + len(PUBLIC_FAMILIES) + len(CONNECTED_FAMILIES) + len(DIRECTION_FAMILIES)
    assert {item['family'] for item in index['available_adapters']}=={'xgb_breadth_history','sina_minute_1m','sina_minute_5m','sina_minute_15m','sina_minute_30m','sina_minute_60m','tencent_web','tencent_market_rt','szse_public','cninfo','baostock_daily','baostock_history','baostock_fundamental','baostock_classification','ecb_public','worldbank_public','ecb_news','fed_public','cninfo_announcements','tickflow_live','ashare_public','ashare_breadth','hk_gov_finance','ashare_sector_heat','mairui_funds','mairui_pools', 'eastmoney_auction','tencent_auction','duanxianxia_auction','duanxianxia_auction_history', *EASTMONEY_FAMILIES, *TUSHARE_SPECS, *NEWS_FEEDS, *PUBLIC_FAMILIES, *CONNECTED_FAMILIES, *DIRECTION_FAMILIES}
    assert index['today_request_count']==0 and index['today_run_count']==0 and index['today_cost_cny'] is None
    assert index['collection_enabled'] is False
    body=configuration();body['enabled']=False;body['entitlement_status']='unknown'
    body['rights']={'display':'unknown','cache':'unknown','export':'unknown','redistribute':'unknown'}
    configure(client,headers,body)
    state=client.get('/api/v1/data-sources').json()['data']['items'][0]
    assert state['collection_state']=='disabled' and state['symbols']==body['symbols']
    assert state['interval_seconds']==body['interval_seconds'] and state['configured_at'] and state['next_run_at']
    assert client.get('/api/v1/data-sources').json()['data']['today_request_count']==0


def test_daily_usage_covers_all_attempts_in_shanghai_day_and_unknown_bill_is_not_zero(client):
    headers=login(client);configure(client,headers)
    current=datetime.now(UTC)
    start=datetime.combine(current.astimezone(SHANGHAI).date(),time.min,SHANGHAI)
    with Session(engine()) as db, db.begin():
        for at, requests, cost in [(current,2,Decimal('0.05')),(current,3,None),(start-timedelta(seconds=1),7,Decimal('1'))]:
            db.add(DataFetchRun(source_id=source_id('tencent_web'),capability='quote.l1',started_at=at,received_at=at,
                status='error',reason='timeout',expected=None,accepted=None,rejected=None,duplicates=None,
                elapsed_ms=None,request_count=requests,cost_cny=cost))
    index=client.get('/api/v1/data-sources').json()['data']
    assert index['statistics_timezone']=='Asia/Shanghai' and index['today_request_count']==5 and index['today_run_count']==2
    assert index['today_cost_cny'] is None


def test_source_configuration_preserves_family_identity_history_and_default_no_network(client):
    headers=login(client)
    response=configure(client,headers)
    data=response.json()['data']
    assert data['provider']['id']==str(source_id('tencent_web')) and data['family']=='tencent_web'
    assert data['collection_state']=='not_checked' and data['last_attempt'] is None and data['collection_enabled'] is False
    assert data['measured_cost_cny'] is None
    replay=configure(client,headers)
    assert replay.headers['Idempotency-Replayed']=='true' and replay.json()==response.json()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Provider).where(Provider.kind=='market'))==2
        assert db.scalar(select(func.count()).select_from(SourceVersion).where(SourceVersion.source_id==source_id('tencent_web')))==1
    refused=client.post(f'/api/v1/data-sources/{source_id("tencent_web")}/refresh',json={},headers={**headers,'Idempotency-Key':'disabled-global-001'})
    assert refused.status_code==422 and refused.json()['error']['code']=='collection_disabled'
    assert client.get('/api/v1/data-fetch-runs').json()['data']['items']==[]


def test_unknown_rights_and_rate_budget_are_refused_before_enabling(client):
    headers=login(client)
    for replacement in [{'entitlement_status':'unknown'},{'rights':{'display':'unknown','cache':'unknown','export':'unknown','redistribute':'unknown'}},{'interval_seconds':2},
                        {'symbols':[{'exchange':'SSE','symbol':'000001','asset_type':'stock'}]},
                        {'symbols':[{'exchange':'SSE','symbol':'510050','asset_type':'etf'}]}]:
        body={**configuration(),**replacement}
        response=client.post('/api/v1/data-sources',json=body,headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
        assert response.status_code==422
    with Session(engine()) as db:
        assert db.get(Provider,source_id('tencent_web')) is None


def test_collection_stores_shared_versions_with_explicit_asset_units_and_no_fake_publication(client,monkeypatch):
    headers=login(client); configure(client,headers)
    calls=[]
    def fetch(url):
        calls.append(url)
        return Response(quote_body(),datetime.now(UTC),3,200)
    job=queue_and_process(client,headers,monkeypatch,fetch)
    assert calls==['https://qt.gtimg.cn/q=sh000001,sz000001']
    with Session(engine()) as db:
        assert db.get(Job,job.id).state=='succeeded'
        instruments=list(db.scalars(select(Instrument).order_by(Instrument.exchange)))
        assert [(row.exchange,row.asset_type,row.valid_from) for row in instruments]==[('SSE','index',None),('SZSE','stock',None)]
        rows=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id('tencent_web'))))
        assert len(rows)==2 and {row.payload['price_unit'] for row in rows}=={'CNY','point'}
        assert all(row.published_at is None and row.historical_eligible and row.available_at>=row.received_at and row.provenance=='source_reported' for row in rows)
        assert {row.payload['price_unit']:row.payload['volume_shares'] for row in rows}=={'point':None,'CNY':'12300'}
        assert all(row.payload['amount_cny'] is None for row in rows)
        run=db.scalar(select(DataFetchRun))
        assert run.accepted==2 and run.duplicates==0 and run.expected==2 and run.request_count==1 and run.cost_cny is None
    status=client.get('/api/v1/data-sources').json()['data']['items'][0]
    assert status['collection_state']=='ready' and status['last_attempt']['accepted']==2
    assert len(client.get('/api/v1/evidence?kind=quote').json()['data']['items'])==2


def test_duplicates_corrections_and_late_ticks_preserve_first_receipt_and_do_not_overwrite_newer_time(client,monkeypatch):
    headers=login(client); configure(client,headers)
    original=quote_body()
    queue_and_process(client,headers,monkeypatch,lambda url:Response(original,datetime.now(UTC),1,200))
    keys=[InstrumentKey('SSE','000001','index'),InstrumentKey('SZSE','000001','stock')]
    with Session(engine()) as db, db.begin():
        provider=db.get(Provider,source_id('tencent_web')); config=db.get(SourceSettings,provider.id)
        before={row.id:row.received_at for row in db.scalars(select(EvidenceVersion))}
        assert store_quote_batch(db,provider,config,parse_tencent_quotes(original,keys,datetime.now(UTC)))==(0,2)
        assert {row.id:row.received_at for row in db.scalars(select(EvidenceVersion))}==before
        source_clock=next(iter(db.scalars(select(EvidenceVersion)))).event_at.astimezone(SHANGHAI).strftime('%Y%m%d%H%M%S')
        assert store_quote_batch(db,provider,config,parse_tencent_quotes(quote_body(clock=source_clock,price='12.40'),keys,datetime.now(UTC)))==(2,0)
        late=(datetime.now(UTC)-timedelta(minutes=10)).astimezone(SHANGHAI).strftime('%Y%m%d%H%M%S')
        assert store_quote_batch(db,provider,config,parse_tencent_quotes(quote_body(clock=late,price='12.20'),keys,datetime.now(UTC)))==(2,0)
        assert db.scalar(select(func.count()).select_from(EvidenceVersion))==6
        latest=db.scalar(select(EvidenceVersion).order_by(EvidenceVersion.event_at.desc(),EvidenceVersion.version_number.desc()).limit(1))
        assert latest.payload['price']=='12.40' and latest.supersedes_id in before


def test_missing_source_clock_stays_historically_ineligible(client,monkeypatch):
    headers=login(client); configure(client,headers)
    queue_and_process(client,headers,monkeypatch,lambda url:Response(quote_body(clock=''),datetime.now(UTC),1,200))
    with Session(engine()) as db:
        assert all(row.available_at is None and not row.historical_eligible for row in db.scalars(select(EvidenceVersion)))
        assert db.scalar(select(DataFetchRun)).status=='partial'


def test_permission_rejection_pauses_source_without_a_blind_retry(client,monkeypatch):
    headers=login(client); configure(client,headers)
    def denied(url):
        raise SourceFailure('permission_missing')
    job=queue_and_process(client,headers,monkeypatch,denied)
    with Session(engine()) as db, db.begin():
        assert db.get(Job,job.id).state=='failed'
        config=db.get(SourceSettings,source_id('tencent_web'))
        assert config.paused_reason=='permission_missing'
        config.next_run_at=datetime.now(UTC)-timedelta(seconds=1)
    schedule_sources()
    assert claim() is None
    response=client.post(f'/api/v1/data-sources/{source_id("tencent_web")}/refresh',json={},headers={**headers,'Idempotency-Key':'paused-source-001'})
    assert response.status_code==422 and response.json()['error']['code']=='source_paused'


def test_rate_limit_records_unknown_coverage_and_honors_retry_after(client,monkeypatch):
    headers=login(client); configure(client,headers)
    def limited(url):
        raise SourceFailure('rate_limited',retry_after=3600)
    job=queue_and_process(client,headers,monkeypatch,limited)
    with Session(engine()) as db:
        row=db.get(Job,job.id); run=db.scalar(select(DataFetchRun))
        assert row.state=='queued' and row.available_at>datetime.now(UTC)+timedelta(minutes=59)
        assert run.status=='error' and run.accepted is None and run.request_count==1 and run.cost_cny is None
    assert claim() is None


def test_source_runs_are_immutable_and_legacy_quote_payload_hash_does_not_gain_fields(client,monkeypatch):
    headers=login(client); configure(client,headers)
    queue_and_process(client,headers,monkeypatch,lambda url:Response(quote_body(),datetime.now(UTC),1,200))
    with pytest.raises(DBAPIError),Session(engine()) as db,db.begin():
        db.execute(delete(DataFetchRun))
    legacy={'price':'12.30','volume_shares':'100','amount_cny':'1230','quote_time':'2026-10-19T07:00:00Z'}
    parsed=QuotePayload.model_validate(legacy)
    assert normalized(parsed)==legacy and content_hash(parsed)==content_hash(legacy)


def test_szse_calendar_persists_only_its_source_declared_days_and_no_guessed_other_exchange(client,monkeypatch):
    headers=login(client); configure(client,headers,configuration('szse_public'))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    def fetch(url):
        current=datetime.now(UTC).astimezone(SHANGHAI)
        count=calendar.monthrange(current.year,current.month)[1]
        rows=[{'jyrq':f'{current.year}-{current.month:02d}-{day:02d}','jybz':'1' if day==1 else '0'} for day in range(1,count+1)]
        return Response(json.dumps({'data':rows}).encode(),datetime.now(UTC),2,200)
    monkeypatch.setattr('app.market.collect.fetch_https',fetch)
    response=client.post(f'/api/v1/data-sources/{source_id("szse_public")}/refresh',json={},headers={**headers,'Idempotency-Key':'calendar-refresh-001'})
    assert response.status_code==202,response.text
    job=claim(); process(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun)); records=list(db.scalars(select(EvidenceVersion)))
        assert run.status=='complete' and run.accepted==len(records) and run.expected==len(records)
        assert all(row.kind=='calendar' and row.payload['exchange']=='SZSE' and row.event_at is None for row in records)
        assert len({row.payload['calendar_version'] for row in records})==1


def test_cninfo_catalog_imports_identities_for_search_without_prices(client,monkeypatch):
    from pathlib import Path
    headers=login(client)
    body=configuration('cninfo'); body['interval_seconds']=3600
    refused=client.post('/api/v1/data-sources',json={**body,'symbols':[{'exchange':'SZSE','symbol':'002724','asset_type':'stock'}]},
                        headers={**headers,'Idempotency-Key':'catalog-symbols-refused'})
    assert refused.status_code==422 and refused.json()['error']['code']=='symbols_not_applicable'
    configure(client,headers,body,key='catalog-config-001')
    sample=(Path(__file__).parent/'fixtures'/'cninfo_catalog_sample.json').read_bytes()
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr('app.market.collect.fetch_https',
                        lambda url: Response(sample,datetime.now(UTC),5,200) if 'cninfo.com.cn' in url else (_ for _ in ()).throw(SourceFailure('endpoint_not_allowed')))
    response=client.post(f'/api/v1/data-sources/{source_id("cninfo")}/refresh',json={},headers={**headers,'Idempotency-Key':'catalog-refresh-001'})
    assert response.status_code==202,response.text
    job=claim(); process(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.source_id==source_id('cninfo')))
        assert run.capability=='instrument.catalog' and run.status=='partial' and run.accepted==10 and run.rejected==1
        captures=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id('cninfo'))))
        assert len(captures)==2 and all(item.kind=='reference' for item in captures)
        assert {item.payload['dataset'] for item in captures}=={'instrument.catalog.members','instrument.catalog.capture'}
        assert db.scalar(select(func.count()).select_from(Instrument))==10
    hit=client.get('/api/v1/instruments',params={'q':'002724'}).json()
    assert hit['meta']['quality']['status']=='partial'
    assert hit['data']['items'][0]['symbol']=='002724' and hit['data']['items'][0]['name']=='海洋王'
    assert hit['data']['items'][0]['exchange']=='SZSE' and hit['data']['items'][0]['asset_type']=='stock'
    index=client.get('/api/v1/instruments',params={'q':'上证'}).json()['data']['items']
    assert any(row['symbol']=='000001' and row['asset_type']=='index' and row['exchange']=='SSE' for row in index)
    quotes=client.get('/api/v1/market-quotes').json()['data']['items']
    assert len(quotes)==4 and all(row['quote'] is None for row in quotes)
    assert all(row['instrument']['asset_type']=='index' for row in quotes)


def catalog_sample():
    from pathlib import Path
    return (Path(__file__).parent/'fixtures'/'cninfo_catalog_sample.json').read_bytes()


def import_catalog_sample():
    with Session(engine()) as db, db.begin():
        provider=db.get(Provider,source_id('cninfo')); settings=db.get(SourceSettings,provider.id)
        return store_catalog_batch(db,provider,settings,parse_cninfo_catalog(catalog_sample()),datetime.now(UTC))


def test_catalog_display_revocation_hides_search_without_deleting_saved_rows(client):
    headers=login(client); body=configuration('cninfo'); configure(client,headers,body,key='catalog-display-001')
    import_catalog_sample()
    assert len(client.get('/api/v1/instruments',params={'q':'002724'}).json()['data']['items'])==1
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Instrument))==10
        origins=list(db.scalars(select(InstrumentOrigin)))
        assert len(origins)==10 and all(row.source_version_id and len(row.last_content_hash)==64 for row in origins)

    revoked={**body,'enabled':False,'entitlement_status':'denied',
             'rights':{key:'denied' for key in body['rights']}}
    configure(client,headers,revoked,key='catalog-display-revoke')
    source=next(row for row in client.get('/api/v1/data-sources').json()['data']['items'] if row['family']=='cninfo')
    assert source['collection_state']=='disabled' and source['provider']['rights']['display']=='denied'
    assert client.get('/api/v1/instruments',params={'q':'002724'}).json()['data']['items']==[]
    # The read gate must not solve the problem by deleting shared identities or evidence.
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Instrument))==10
        assert db.scalar(select(func.count()).select_from(InstrumentOrigin))==10

    # Closing collection is not the same as revoking display: saved identities stay searchable.
    closed={**body,'enabled':False}
    configure(client,headers,closed,key='catalog-display-closed')
    assert len(client.get('/api/v1/instruments',params={'q':'002724'}).json()['data']['items'])==1
    # Restoring display without collecting again also restores the saved catalogue.
    configure(client,headers,body,key='catalog-display-restore')
    assert len(client.get('/api/v1/instruments',params={'q':'002724'}).json()['data']['items'])==1


def test_catalog_revocation_does_not_hide_other_authorized_source_or_manual_record(client,monkeypatch):
    headers=login(client)
    catalog_body=configuration('cninfo'); configure(client,headers,catalog_body,key='catalog-multi-001')
    import_catalog_sample()
    with Session(engine()) as db, db.begin():
        db.add(Instrument(id=uuid.uuid4(),exchange='SSE',symbol='600999',asset_type='stock',name='人工合成记录',
                          valid_from=None,valid_to=None,first_received_at=None))
    quote_body_for_600000=_single_quote_body('sh600000','600000','合成浦发名称')
    tencent=configuration('tencent_web')
    tencent['symbols']=[{'exchange':'SSE','symbol':'600000','asset_type':'stock'}]
    configure(client,headers,tencent,key='catalog-multi-tencent')
    queue_and_process(client,headers,monkeypatch,lambda url:Response(quote_body_for_600000,datetime.now(UTC),2,200))

    revoked={**catalog_body,'enabled':False,'entitlement_status':'denied',
             'rights':{key:'denied' for key in catalog_body['rights']}}
    configure(client,headers,revoked,key='catalog-multi-revoke')
    assert client.get('/api/v1/instruments',params={'q':'002724'}).json()['data']['items']==[]
    # An independently authorized source keeps the same identity searchable.
    assert client.get('/api/v1/instruments',params={'q':'600000'}).json()['data']['items'][0]['name']=='合成浦发名称'
    # A permitted identity must not expose a different source's revoked name.
    assert client.get('/api/v1/instruments',params={'q':'浦发银行'}).json()['data']['items']==[]
    # A local record with no source claim is never hidden by an unrelated revocation.
    assert client.get('/api/v1/instruments',params={'q':'600999'}).json()['data']['items'][0]['name']=='人工合成记录'


def test_catalog_claim_on_same_manual_identity_preserves_local_name_after_revocation(client):
    headers=login(client); body=configuration('cninfo')
    configure(client,headers,body,key='catalog-manual-overlap')
    manual_id=uuid.uuid4()
    with Session(engine()) as db, db.begin():
        db.add(Instrument(id=manual_id,exchange='SZSE',symbol='002724',asset_type='stock',
                          name='独立人工名称',valid_from=None,valid_to=None,first_received_at=None))
    import_catalog_sample()
    allowed=client.get('/api/v1/instruments',params={'q':'002724'}).json()['data']['items']
    assert allowed[0]['id']==str(manual_id) and allowed[0]['name']=='海洋王'
    revoked={**body,'enabled':False,'entitlement_status':'denied',
             'rights':{key:'denied' for key in body['rights']}}
    configure(client,headers,revoked,key='catalog-manual-overlap-revoke')
    saved=client.get('/api/v1/instruments',params={'q':'002724'}).json()['data']['items']
    assert saved[0]['id']==str(manual_id) and saved[0]['name']=='独立人工名称'
    assert client.get('/api/v1/instruments',params={'q':'海洋王'}).json()['data']['items']==[]
    assert client.get('/api/v1/instruments',params={'q':'独立人工名称'}).json()['data']['items']==saved
    assert client.get('/api/v1/market-quotes',params={'instrument_id':str(manual_id)}).json()['data']['items'][0]['instrument']['name']=='独立人工名称'


def test_revoked_catalog_identity_is_not_leaked_through_saved_quote_url(client):
    headers=login(client); body=configuration('cninfo')
    configure(client,headers,body,key='catalog-uuid-gate')
    import_catalog_sample()
    item=client.get('/api/v1/instruments',params={'q':'002724'}).json()['data']['items'][0]
    assert client.get('/api/v1/market-quotes',params={'instrument_id':item['id']}).status_code==200
    configure(client,headers,{**body,'enabled':False,'rights':{**body['rights'],'display':'denied'}},key='catalog-uuid-revoke')
    saved=client.get('/api/v1/market-quotes',params={'instrument_id':item['id']})
    assert saved.status_code==200
    assert saved.json()['data']['items'][0]['instrument']['name']=='标的资料不可用'
    assert '海洋王' not in saved.text


def _single_quote_body(key,code,name,price='12.30'):
    fields=['']*88
    for index,value in {1:name,2:code,3:price,4:'12',5:'12.10',6:'123',
                        30:datetime.now(UTC).astimezone(SHANGHAI).strftime('%Y%m%d%H%M%S'),33:'12.50',34:'12'}.items():
        fields[index]=value
    return (f'v_{key}="' + '~'.join(fields) + '";').encode('gb18030')


def test_conflicting_catalog_batch_cannot_overwrite_a_valid_name_but_a_single_rename_can(client):
    headers=login(client)
    configure(client,headers,configuration('cninfo'),key='catalog-conflict-001')
    with Session(engine()) as db, db.begin():
        db.add(Instrument(id=uuid.uuid4(),exchange='SSE',symbol='600123',asset_type='stock',name='Existing valid name',
                          valid_from=None,valid_to=None,first_received_at=None))
    rows=[{'category':'A股','code':'600123','orgId':'synthetic-same-company','zwjc':name}
          for name in ['Synthetic name A','Synthetic name B']]
    conflict=parse_cninfo_catalog(json.dumps({'stockList':rows}).encode())
    assert conflict.records==[] and set(conflict.rejected.values())=={'conflicting_instrument'}
    with Session(engine()) as db, db.begin():
        provider=db.get(Provider,source_id('cninfo')); settings=db.get(SourceSettings,provider.id)
        store_catalog_batch(db,provider,settings,conflict,datetime.now(UTC))
    with Session(engine()) as db:
        assert db.scalar(select(Instrument).where(Instrument.symbol=='600123')).name=='Existing valid name'
    # A later, non-conflicting source-backed observation is a legitimate rename.
    renamed=parse_cninfo_catalog(json.dumps({'stockList':[rows[1]]}).encode())
    with Session(engine()) as db, db.begin():
        provider=db.get(Provider,source_id('cninfo')); settings=db.get(SourceSettings,provider.id)
        store_catalog_batch(db,provider,settings,renamed,datetime.now(UTC))
    with Session(engine()) as db:
        assert db.scalar(select(Instrument).where(Instrument.symbol=='600123')).name=='Synthetic name B'
