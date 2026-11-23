"""Free tiers, credential boundaries and fallback on real disposable PostgreSQL."""
import json,uuid
from datetime import UTC,datetime,timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr,ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings,settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collect import store_quote_batch
from app.market.live import fetch_tickflow,parse_ashare,parse_tickflow
from app.market.models import SourceSettings
from app.market.protocol import InstrumentKey,Response,SourceFailure
from app.market.service import source_id
from app.models import Provider
from app.worker import claim,process
from test_data_sources import configuration,configure,login

KEYS=[InstrumentKey('SSE','000001','index'),InstrumentKey('SZSE','000001','stock')]

def response_body(at=None):
    at=at or datetime.now(UTC)-timedelta(seconds=3)
    return json.dumps({'data':[{'symbol':code,'region':'CN','last_price':12.3,'prev_close':12,'open':12.1,'high':12.5,'low':12,
        'timestamp':int(at.timestamp()*1000),'volume':100,'amount':1230} for code in ['000001.SH','000001.SZ']]}).encode()

def source_body(family):
    body=configuration();body['family']=family;body['interval_seconds']=30 if family=='tickflow_live' else 300
    return body

def test_live_parser_uses_source_milliseconds_and_preserves_unknown_units():
    batch=parse_tickflow(response_body(),KEYS,datetime.now(UTC))
    assert len(batch.records)==2 and batch.records[0].price==Decimal('12.3')
    assert batch.records[0].quote_time is not None
    assert batch.records[0].volume_shares is None and batch.records[0].amount_cny is None
    raw=json.loads(response_body());raw['data'][0]['timestamp']=int((datetime.now(UTC)+timedelta(hours=1)).timestamp()*1000)
    broken=parse_tickflow(json.dumps(raw).encode(),KEYS,datetime.now(UTC))
    assert len(broken.records)==1 and 'future_source_time' in broken.rejected.values()
    raw['data'][0]['symbol']='600000.SH'
    with pytest.raises(SourceFailure,match='unexpected_instrument'):parse_tickflow(json.dumps(raw).encode(),KEYS,datetime.now(UTC))

def test_missing_key_does_not_connect_and_invalid_settings_do_not_echo_input(monkeypatch):
    def no_network(*args,**kwargs):raise AssertionError('No key must mean no network')
    monkeypatch.setattr('urllib.request.build_opener',no_network)
    with pytest.raises(SourceFailure,match='credentials_missing'):fetch_tickflow(KEYS,None)
    secret='synthetic-secret-contains-newline\n'
    with pytest.raises(ValidationError) as error:Settings(tickflow_api_key=secret)
    assert secret not in str(error.value)
    value=Settings(tickflow_api_key='synthetic-secret-format-only')
    assert 'synthetic-secret-format-only' not in repr(value) and 'synthetic-secret-format-only' not in value.model_dump_json()

def test_anonymous_reference_never_invents_intraday_clock_previous_close_or_zero_units():
    raw={'ok':True,'endpoint':'quote','tier':'free','data':[{'symbol':key.vendor_key,'date':'2026-01-02','last':'12.30',
        'open':'12.10','high':'12.50','low':'12','volume':'100','amount':'1230','turnover':'1'} for key in KEYS]}
    batch=parse_ashare(json.dumps(raw).encode(),KEYS,datetime.now(UTC))
    assert len(batch.records)==2 and all(q.quote_time is None and q.quote_kind=='delayed_reference' for q in batch.records)
    assert all(q.previous_close is None and q.volume_shares is None for q in batch.records)
    raw['data'].append(raw['data'][0])
    bad=parse_ashare(json.dumps(raw).encode(),KEYS,datetime.now(UTC))
    assert len(bad.records)==1 and 'duplicate_source_day' in bad.rejected.values()

def test_missing_credential_visible_without_queue_or_key_disclosure(client,monkeypatch):
    headers=login(client);monkeypatch.setattr(settings(),'tickflow_api_key',None)
    configure(client,headers,source_body('tickflow_live'))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    row=client.get('/api/v1/data-sources').json()['data']['items'][0]
    assert row['credential_configured'] is False and row['collection_state']=='credentials_missing'
    result=client.post('/api/v1/data-sources/'+str(source_id('tickflow_live'))+'/refresh',json={},headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
    assert result.status_code==422 and result.json()['error']['code']=='credentials_missing'
    assert client.get('/api/v1/data-sources').json()['data']['today_request_count']==0

def test_live_worker_persists_real_clock_and_revoked_source_falls_back_whole_reference(client,monkeypatch):
    headers=login(client);monkeypatch.setattr(settings(),'tickflow_api_key',SecretStr('synthetic-private-test-key-only'))
    configure(client,headers,source_body('tickflow_live'))
    configure(client,headers,source_body('ashare_public'),key=uuid.uuid4().hex)
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr('app.market.collect.fetch_tickflow',lambda selection,credential:Response(response_body(),datetime.now(UTC),2,200))
    result=client.post('/api/v1/data-sources/'+str(source_id('tickflow_live'))+'/refresh',json={},headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
    assert result.status_code==202;process(claim())
    rows=client.get('/api/v1/market-quotes').json()['data']['items']
    assert rows[0]['provenance']['family']=='tickflow_live' and rows[0]['state']=='available'
    live_id=rows[0]['instrument']['id'];before=datetime.now(UTC)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('ashare_public'));config=db.get(SourceSettings,provider.id)
        raw={'ok':True,'endpoint':'quote','tier':'free','data':[{'symbol':key.vendor_key,'date':datetime.now(UTC).date().isoformat(),
            'last':'10','open':'10','high':'10','low':'10'} for key in KEYS]}
        reference=parse_ashare(json.dumps(raw).encode(),KEYS,datetime.now(UTC))
        assert store_quote_batch(db,provider,config,reference)==(2,0)
        assert store_quote_batch(db,provider,config,reference)==(0,2)
        # JSON assignment is explicit, so this revocation reaches PostgreSQL.
        live=db.get(Provider,source_id('tickflow_live'));live.rights={**live.rights,'display':'denied'}
    fallback=client.get('/api/v1/market-quotes',params={'instrument_id':live_id}).json()['data']['items'][0]
    assert fallback['state']=='delayed_reference' and fallback['quote']['price']=='10'
    assert fallback['quote']['previous_close'] is None and fallback['provenance']['family']=='ashare_public'
    assert not fallback['provenance']['historical_eligible']
    historical=client.get('/api/v1/market-quotes',params={'instrument_id':live_id,'as_of_time':before.isoformat()}).json()['data']['items'][0]
    assert historical['quote'] is None
    assert 'synthetic-private-test-key-only' not in client.get('/api/v1/data-sources').text
    with Session(engine()) as db:
        assert all('synthetic-private-test-key-only' not in json.dumps(row.payload) for row in db.scalars(select(EvidenceVersion)))

def test_free_quote_configuration_rejects_over_budget_batches(client):
    headers=login(client);body=source_body('tickflow_live')
    body['symbols']=[{'exchange':'SZSE','symbol':f'{n:06d}','asset_type':'stock'} for n in range(1,7)]
    result=client.post('/api/v1/data-sources',json=body,headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
    assert result.status_code==422
