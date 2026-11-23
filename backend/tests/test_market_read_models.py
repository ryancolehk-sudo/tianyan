"""Actual PostgreSQL read views; synthetic quotes never contact a market vendor."""
import uuid
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest

from jsonschema import Draft202012Validator, FormatChecker

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collect import store_quote_batch
from app.market.protocol import SHANGHAI, InstrumentKey, Response, parse_tencent_quotes
from app.market.service import source_id
from app.market.models import SourceSettings
from app.models import Provider
from test_data_sources import configure, login, quote_body, queue_and_process


def quote(client, instrument_id=None, cutoff=None):
    return client.get('/api/v1/market-quotes',params={**({'instrument_id':str(instrument_id)} if instrument_id else {}),
        **({'as_of_time':cutoff.isoformat()} if cutoff else {})})


def test_quote_reads_require_auth_explicit_identity_and_valid_past_cutoff(client,monkeypatch):
    assert quote(client).status_code==401
    login(client,'viewer')
    assert quote(client,uuid.uuid4()).status_code==404
    assert client.get('/api/v1/market-quotes?instrument_id=000001').status_code==422
    assert quote(client,cutoff=datetime.now(UTC)+timedelta(hours=1)).json()['error']['code']=='future_cutoff'
    assert client.get('/api/v1/market-quotes?as_of_time=2001-12-31T15:00:00').status_code==422
    def no_network(*args,**kwargs):
        raise AssertionError('A cached read must not contact a vendor')
    monkeypatch.setattr('app.market.collect.fetch_https',no_network)
    rows=quote(client).json()['data']['items']
    assert len(rows)==4 and {row['instrument']['symbol'] for row in rows}=={'000001','399001','399006','000688'}
    assert all(row['state']=='not_configured' and row['quote'] is None and row['provenance'] is None for row in rows)
    assert client.get('/api/v1/data-sources').json()['data']['today_request_count']==0


def test_saved_quotes_keep_exchange_asset_units_source_versions_and_unknowns(client,monkeypatch):
    headers=login(client);configure(client,headers)
    queue_and_process(client,headers,monkeypatch,lambda url:Response(quote_body(),datetime.now(UTC),1,200))
    search=client.get('/api/v1/instruments?q=000001').json()['data']['items']
    assert len(search)==2
    values={(item['exchange'],item['asset_type']):quote(client,item['id']).json()['data']['items'][0] for item in search}
    assert values[('SSE','index')]['quote']['price_unit']=='point'
    assert values[('SZSE','stock')]['quote']['price_unit']=='CNY'
    assert values[('SSE','index')]['quote']['volume_shares'] is None
    assert values[('SZSE','stock')]['quote']['volume_shares']=='12300'
    for row in values.values():
        assert row['state']=='available' and row['change']=='0.30' and row['change_percent']=='2.50'
        assert row['quote']['amount_cny'] is None
        assert row['provenance']['published_at'] is None and row['provenance']['received_at']
        assert row['provenance']['family']=='tencent_web' and row['provenance']['source_version_id']
        evidence=client.get('/api/v1/evidence/'+row['provenance']['evidence_version_id']).json()['data']
        assert row['provenance']['content_hash']==evidence['content_hash'] and evidence['payload']==row['quote']
    response=quote(client).json()
    spec=json.loads((Path(__file__).resolve().parents[2]/'contracts/openapi.p2.json').read_text(encoding='utf-8'))
    contract={**spec['components']['schemas']['MarketQuoteResponse'],'components':spec['components']}
    Draft202012Validator(contract,format_checker=FormatChecker()).validate(response)
    assert response['data']['selection']=='indices'
    assert quote(client,search[0]['id']).json()['data']['selection']=='instrument'


def test_internal_captured_cutoff_survives_clock_correction_without_admitting_future_query(client,monkeypatch):
    from app.market import read_models
    from app.auth import APIError
    headers=login(client);configure(client,headers)
    queue_and_process(client,headers,monkeypatch,lambda url:Response(quote_body(),datetime.now(UTC),1,200))
    with Session(engine()) as db:
        evidence=db.scalar(select(EvidenceVersion).where(EvidenceVersion.kind=='quote'))
        captured=evidence.received_at+timedelta(milliseconds=1)
        ident=evidence.instrument_id
    monkeypatch.setattr(read_models,'now',lambda db:captured-timedelta(seconds=30))
    with Session(engine()) as db:
        result=read_models.quote_batch(db,instrument_id=ident,observation_cutoff=captured)
        assert result.items[0].quote is not None
        assert result.items[0].provenance.received_at<=captured
        with pytest.raises(APIError) as failure:
            read_models.quote_batch(db,instrument_id=ident,as_of_time=captured)
        assert failure.value.code=='future_cutoff'


def test_late_receipt_cannot_replace_later_source_time_and_correction_uses_whole_version(client,monkeypatch):
    headers=login(client);configure(client,headers)
    first=quote_body()
    queue_and_process(client,headers,monkeypatch,lambda url:Response(first,datetime.now(UTC),1,200))
    keys=[InstrumentKey('SSE','000001','index'),InstrumentKey('SZSE','000001','stock')]
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tencent_web'));config=db.get(SourceSettings,provider.id)
        original=db.scalar(select(EvidenceVersion).where(EvidenceVersion.kind=='quote'))
        selected_id=original.instrument_id
        cutoff=datetime.now(UTC)
        clock=original.event_at.astimezone(SHANGHAI).strftime('%Y%m%d%H%M%S')
        store_quote_batch(db,provider,config,parse_tencent_quotes(quote_body(clock=clock,price='12.40'),keys,datetime.now(UTC)))
        late=(original.event_at-timedelta(minutes=10)).astimezone(SHANGHAI).strftime('%Y%m%d%H%M%S')
        store_quote_batch(db,provider,config,parse_tencent_quotes(quote_body(clock=late,price='12.20'),keys,datetime.now(UTC)))
    current=quote(client,selected_id).json()['data']['items'][0]
    previous=quote(client,selected_id,cutoff).json()['data']['items'][0]
    assert current['quote']['price']=='12.40' and previous['quote']['price']=='12.30'
    assert current['provenance']['evidence_version_id']!=previous['provenance']['evidence_version_id']
    assert previous['provenance']['content_hash']!=current['provenance']['content_hash']


def test_backfilled_old_quote_is_stale_today_and_absent_before_first_receipt(client,monkeypatch):
    headers=login(client);configure(client,headers)
    cutoff=datetime.now(UTC)
    queue_and_process(client,headers,monkeypatch,lambda url:Response(quote_body(clock='20011231150000'),datetime.now(UTC),1,200))
    row=quote(client).json()['data']['items'][0]
    assert row['quote']['price']=='12.30' and row['state']=='stale'
    assert row['age_seconds']>row['freshness_limit_seconds']
    assert row['provenance']['received_at'].startswith(str(datetime.now(UTC).year))
    assert quote(client,row['instrument']['id'],cutoff).json()['data']['items'][0]['quote'] is None
    assert quote(client,row['instrument']['id'],datetime.now(UTC)).json()['data']['historical'] is True


def test_unknown_quote_time_is_visible_as_partial_but_never_strict_history(client,monkeypatch):
    headers=login(client);configure(client,headers)
    queue_and_process(client,headers,monkeypatch,lambda url:Response(quote_body(clock=''),datetime.now(UTC),1,200))
    row=quote(client).json()['data']['items'][0]
    assert row['state']=='partial' and row['quote']['quote_time'] is None and row['age_seconds'] is None
    assert row['provenance']['historical_eligible'] is False
    historic=quote(client,row['instrument']['id'],datetime.now(UTC)).json()['data']['items'][0]
    assert historic['state']=='empty' and historic['quote'] is None


def test_current_rights_revocation_hides_cached_values_in_current_and_historical_reads(client,monkeypatch):
    headers=login(client);configure(client,headers)
    queue_and_process(client,headers,monkeypatch,lambda url:Response(quote_body(),datetime.now(UTC),1,200))
    selected=quote(client).json()['data']['items'][0]['instrument']['id']
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tencent_web'))
        provider.entitlement_status='denied'
    for cutoff in [None,datetime.now(UTC)]:
        row=quote(client,selected,cutoff).json()['data']['items'][0]
        assert row['state']=='permission_missing' and row['quote'] is None and row['provenance'] is None
    with Session(engine()) as db,db.begin():
        db.get(Provider,source_id('tencent_web')).enabled=False
    assert quote(client,selected).json()['data']['items'][0]['state']=='disabled'


def test_mixed_index_coverage_preserves_each_missing_position_and_never_claims_full_market(client,monkeypatch):
    headers=login(client);configure(client,headers)
    queue_and_process(client,headers,monkeypatch,lambda url:Response(quote_body(),datetime.now(UTC),1,200))
    response=quote(client).json()
    assert response['meta']['quality']['status']=='partial'
    states=[row['state'] for row in response['data']['items']]
    assert states==['available','empty','empty','empty']
    assert all(row['quote'] is None for row in response['data']['items'][1:])
