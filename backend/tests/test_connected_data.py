"""Versioned public-data integration against disposable real PostgreSQL only."""
import copy
import json
import uuid
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import BarPayload, NewsPayload, ReferencePayload
from app.market.protocol import InstrumentKey, Response, SourceFailure
from app.market.public import parse_public
from app.market.reference import parse_references
from app.market.registry import SOURCES
from app.models import Provider
from app.worker import claim, process
from test_data_sources import configuration, configure, login


KEY=InstrumentKey('SZSE','000001','stock')


def sdk_sample():
    day=str((datetime.now(UTC)-timedelta(days=2)).date())
    return {'datasets':[{'dataset':'bars.daily.unadjusted','title':'合成历史日K','symbol':'sz.000001','adjustment':'unadjusted',
        'fields':['date','code','open','high','low','close','volume','amount'],
        'rows':[{'date':day,'code':'sz.000001','open':'12.0000000000','high':'13.0000000000','low':'11.0000000000',
                 'close':'12.1234567890','volume':'10000','amount':'121234.56789'}]}],'errors':[],'request_count':3}


def source_config(family):
    body=configuration(family);body['interval_seconds']=SOURCES[family].minimum_interval
    if family in {'baostock_history','baostock_fundamental'}:body['symbols']=[{'exchange':'SZSE','symbol':'000001','asset_type':'stock'}]
    return body


def test_reference_preserves_date_precision_price_precision_and_declared_units():
    batch=parse_references(json.dumps(sdk_sample()).encode(),datetime.now(UTC),[KEY])
    assert batch.expected==2 and batch.rejected==0 and len(batch.bars)==1
    bar=batch.bars[0][1]
    assert bar.close=='12.123456789' and bar.bar_end_at is None and bar.trade_day is not None
    assert bar.volume_shares=='10000' and bar.adjustment=='unadjusted'
    assert batch.records[0][1].columns[2].unit=='元'


@pytest.mark.parametrize('change',['unexpected_identity','duplicate_columns','extra_cell','oversized_cell','future_day','nonintegral_volume'])
def test_invalid_reference_material_never_produces_a_fabricated_candle(change):
    sample=sdk_sample()
    if change=='unexpected_identity':sample['datasets'][0]['symbol']='sh.000001'
    elif change=='duplicate_columns':sample['datasets'][0]['fields'].append('date')
    elif change=='extra_cell':sample['datasets'][0]['rows'][0]['unknown']='value'
    elif change=='oversized_cell':sample['datasets'][0]['rows'][0]['open']='x'*501
    elif change=='future_day':sample['datasets'][0]['rows'][0]['date']='2999-01-01'
    else:sample['datasets'][0]['rows'][0]['volume']='1.5'
    if change in {'future_day','nonintegral_volume'}:
        batch=parse_references(json.dumps(sample).encode(),datetime.now(UTC),[KEY]);assert not batch.bars and batch.rejected==1
        assert not batch.records[0][1].rows
    else:
        with pytest.raises(SourceFailure):parse_references(json.dumps(sample).encode(),datetime.now(UTC),[KEY])


def test_old_bar_and_news_payload_hash_shapes_are_preserved():
    bar={'open':'1','high':'2','low':'1','close':'2','volume_shares':'10','interval':'1m','bar_end_at':'2020-01-01T01:31:00Z'}
    assert BarPayload.model_validate(bar).model_dump(mode='json')==bar
    news={'title':'合成原文','summary':None,'source_url':'https://example.com/original'}
    assert NewsPayload.model_validate(news).model_dump(mode='json')==news


def test_empty_financial_periods_stay_distinct_and_mismatched_source_period_is_rejected():
    sample={'datasets':[{'dataset':'financial.profit','title':'合成利润指标','symbol':'sz.000001','period':period,
        'fields':['code','statDate','netProfit'],'rows':[]} for period in ['2026-Q2','2026-Q3']], 'errors':[],'request_count':3}
    batch=parse_references(json.dumps(sample).encode(),datetime.now(UTC),[KEY])
    assert len(batch.records)==2 and batch.records[0][0]!=batch.records[1][0]
    assert batch.records[0][1].title.endswith('2026-Q2') and not batch.records[0][1].rows
    sample['datasets'][0]['rows']=[{'code':'sz.000001','statDate':'2026-09-30','netProfit':'10'}]
    with pytest.raises(SourceFailure):parse_references(json.dumps(sample).encode(),datetime.now(UTC),[KEY])


def test_rss_rejects_entities_and_does_not_invent_missing_publication_time():
    now=datetime.now(UTC)
    with pytest.raises(SourceFailure):parse_public('fed_public',b'<!DOCTYPE rss [<!ENTITY x "bad">]><rss/>',now)
    batch=parse_public('fed_public',b'<rss><channel><item><title>synthetic original</title><link>https://example.com/item</link></item></channel></rss>',now)
    assert len(batch.records)==1 and batch.records[0][2] is None
    assert batch.records[0][1].summary is None


def test_ecb_reference_is_daily_reference_not_live_fx():
    raw=b'<Envelope><Cube><Cube time="2020-01-02"><Cube currency="CNY" rate="7.8"/></Cube></Cube></Envelope>'
    batch=parse_public('ecb_public',raw,datetime.now(UTC))
    payload=batch.records[0][1]
    assert payload.rows[0]['base']=='EUR' and payload.rows[0]['rate']=='7.8' and str(payload.observed_day)=='2020-01-02'
    assert '不是可交易实时报价' in payload.scope


def test_cninfo_date_encoding_never_becomes_a_fabricated_publication_time():
    sample={'announcements':[{'announcementTitle':'合成公告','announcementId':'synthetic-1','orgId':'synthetic',
        'announcementTime':1577894400000}]}
    batch=parse_public('cninfo_announcements',json.dumps(sample).encode(),datetime.now(UTC))
    assert batch.rejected==0 and len(batch.records)==1
    _,payload,published=batch.records[0]
    assert published is None and str(payload.published_day)=='2020-01-02'


def test_sdk_failed_login_is_bounded_counted_and_does_not_logout_on_a_dead_socket(monkeypatch):
    import baostock as bs
    import baostock.util.socketutil as transport
    from app.market.reference_reader import read
    send=lambda *_:None
    monkeypatch.setattr(transport,'send_msg',send)
    def fail_login():
        transport.send_msg('synthetic request')
        return SimpleNamespace(error_code='10002007')
    monkeypatch.setattr(bs,'login',fail_login)
    monkeypatch.setattr(bs,'logout',lambda:pytest.fail('failed connection must not send logout'))
    monkeypatch.setattr('socket.setdefaulttimeout',lambda *_:None)
    answer=read({'family':'baostock_classification','symbols':[],'now':datetime.now(UTC).isoformat()})
    assert answer=={'error':'upstream_unavailable','request_count':1}
    assert transport.send_msg is send


def test_sdk_budget_counts_only_sent_requests_and_preserves_partial_result_envelope(monkeypatch):
    import baostock as bs
    import baostock.util.socketutil as transport
    from app.market.reference_reader import read
    sent=[]
    monkeypatch.setattr(transport,'send_msg',lambda *_:sent.append(1))
    def login_success():
        transport.send_msg('synthetic login')
        return SimpleNamespace(error_code='0')
    def exhaust_budget():
        for _ in range(80):transport.send_msg('synthetic query')
    monkeypatch.setattr(bs,'login',login_success)
    monkeypatch.setattr(bs,'logout',lambda:transport.send_msg('synthetic logout'))
    for name in ['query_stock_industry','query_hs300_stocks','query_sz50_stocks','query_zz500_stocks']:
        monkeypatch.setattr(bs,name,exhaust_budget)
    monkeypatch.setattr('socket.setdefaulttimeout',lambda *_:None)
    answer=read({'family':'baostock_classification','symbols':[],'now':datetime.now(UTC).isoformat()})
    assert len(sent)==60 and answer['request_count']==60
    assert not answer['datasets'] and len(answer['errors'])==4 and 'error' not in answer


def test_reference_claims_remain_independent_and_do_not_shadow_or_retain_revoked_catalog_names(client):
    from app.market.collect import latest_source_version_id, record_instrument_origin, store_reference_batch, upsert_instrument
    from app.market.models import SourceSettings
    from app.market.visibility import instrument_name_sql
    from app.models import Instrument
    headers=login(client)
    catalog_id=uuid.UUID(configure(client,headers,source_config('cninfo'),key='synthetic-catalog').json()['data']['provider']['id'])
    history_id=uuid.UUID(configure(client,headers,source_config('baostock_history'),key='synthetic-history').json()['data']['provider']['id'])
    with Session(engine()) as db,db.begin():
        catalog=db.get(Provider,catalog_id);history=db.get(Provider,history_id);received=datetime.now(UTC)
        _,instrument=upsert_instrument(db,KEY,'仅目录有权的合成名称',received)
        record_instrument_origin(db,instrument,catalog,kind='catalog',name='仅目录有权的合成名称',content_hash_value='1'*64,
            received_at=received,source_version_id=latest_source_version_id(db,catalog))
        store_reference_batch(db,history,db.get(SourceSettings,history_id),parse_references(json.dumps(sdk_sample()).encode(),received,[KEY]),received)
        name=lambda:db.scalar(select(instrument_name_sql()).select_from(Instrument).where(Instrument.id==instrument.id))
        assert name()=='仅目录有权的合成名称'
        catalog.rights={**catalog.rights,'display':'denied'};db.flush()
        assert name()=='000001' and instrument.local_name is None
        instrument_id=str(instrument.id)
    bar=client.get('/api/v1/market-bars',params={'instrument_id':instrument_id}).json()['data']
    assert len(bar['items'])==1 and bar['instrument']['name']=='000001'


def test_new_read_endpoints_require_normal_authentication(client):
    for path in ['reference-datasets','market-news','data-coverage','market-bars?instrument_id='+str(uuid.uuid4())]:
        assert client.get('/api/v1/'+path).status_code==401


def test_reference_worker_persistence_current_permissions_and_45_operation_contract(client,monkeypatch):
    from app.market import collect
    monkeypatch.setenv('JL_DATA_COLLECTION_ENABLED','true');settings.cache_clear()
    try:
        headers=login(client);configured=configure(client,headers,source_config('baostock_history')).json()['data'];ident=configured['provider']['id']
        monkeypatch.setattr(collect,'fetch_references',lambda *_:Response(json.dumps(sdk_sample()).encode(),datetime.now(UTC),3,200))
        queued=client.post('/api/v1/data-sources/'+ident+'/refresh',json={},headers={**headers,'Idempotency-Key':'synthetic-history-run'})
        assert queued.status_code==202
        job=claim();assert str(job.id)==queued.json()['data']['job_id'];process(job)
        rows=client.get('/api/v1/reference-datasets').json()['data']['items'];assert len(rows)==1
        instrument=rows[0]['instrument_id'];bar=client.get('/api/v1/market-bars',params={'instrument_id':instrument}).json()['data']
        assert len(bar['items'])==1 and bar['historical_reconstruction'] and bar['items'][0]['payload']['bar_end_at'] is None
        coverage=client.get('/api/v1/data-coverage').json()['data']['items']
        assert len(coverage)==31 and next(row for row in coverage if row['id']=='D06')['state']=='partial'
        # P3-008 registers two auction adapters: without a stored batch D13 is pending, not blocked.
        assert next(row for row in coverage if row['id']=='D13')['state']=='pending'
        with Session(engine()) as db,db.begin():
            provider=db.get(Provider,uuid.UUID(ident));provider.rights={**provider.rights,'display':'denied'}
        assert client.get('/api/v1/reference-datasets').json()['data']['items']==[]
        assert client.get('/api/v1/market-bars',params={'instrument_id':instrument}).json()['data']['items']==[]
        coverage=client.get('/api/v1/data-coverage').json()['data']['items'];assert next(row for row in coverage if row['id']=='D06')['state']=='pending'
    finally:settings.cache_clear()


def test_news_worker_keeps_source_publication_receipt_and_timestamp_only_corrections(client,monkeypatch):
    from app.market import collect
    from app.market.collect import store_news_batch
    from app.market.models import SourceSettings
    monkeypatch.setenv('JL_DATA_COLLECTION_ENABLED','true');settings.cache_clear()
    try:
        headers=login(client);ident=configure(client,headers,source_config('fed_public')).json()['data']['provider']['id']
        original=b'<rss><channel><item><title>synthetic official item</title><link>https://example.com/item</link><pubDate>Thu, 02 Jan 2020 10:00:00 GMT</pubDate></item></channel></rss>'
        monkeypatch.setattr(collect,'fetch_public',lambda *_:Response(original,datetime.now(UTC),2,200))
        r=client.post('/api/v1/data-sources/'+ident+'/refresh',json={},headers={**headers,'Idempotency-Key':'synthetic-news-run'});assert r.status_code==202
        job=claim();process(job)
        rows=client.get('/api/v1/market-news').json()['data']['items'];assert len(rows)==1 and rows[0]['published_at'].startswith('2020-01-02T10:00')
        correction=original.replace(b'10:00:00',b'10:01:00');received=datetime.now(UTC)
        with Session(engine()) as db,db.begin():
            provider=db.get(Provider,uuid.UUID(ident));config=db.get(SourceSettings,provider.id)
            added,repeated=store_news_batch(db,provider,config,parse_public('fed_public',correction,received),received)
            assert added==1 and repeated==0
            assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.source_id==provider.id))==2
        latest=client.get('/api/v1/market-news').json()['data']['items'];assert len(latest)==1 and latest[0]['published_at'].startswith('2020-01-02T10:01')
        with Session(engine()) as db,db.begin():
            provider=db.get(Provider,uuid.UUID(ident));provider.rights={**provider.rights,'cache':'denied'}
        assert client.get('/api/v1/market-news').json()['data']['items']==[]
    finally:settings.cache_clear()
