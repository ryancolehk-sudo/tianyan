"""Synthetic classification evidence; no supplier calls."""
import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from app.market import eastmoney_trading as transport
from app.market import industry_catalog as catalog
from app.market.protocol import Response, SourceFailure
from test_eastmoney_funds_partial import NOW, body, fetcher, local_only


def catalog_body():
    return json.dumps({'bklist':[
        {'code':'BK0001','type':2,'flag':1,'name':'一级'},
        {'code':'BK0002','type':2,'flag':2,'name':'无需罗马数字后缀'},
        {'code':'BK0003','type':2,'flag':3,'name':'三级'},
        {'code':'BK0004','type':3,'flag':0,'name':'概念Ⅱ'},
    ]}).encode()


def test_official_levels_are_explicit_and_names_never_decide():
    parsed=catalog.parse_catalog(catalog_body(),NOW)
    assert parsed.observed_day==NOW.date()
    assert parsed.rows==[{'code':'BK0001','industry_level':'1'},
        {'code':'BK0002','industry_level':'2'},{'code':'BK0003','industry_level':'3'}]
    assert parsed.source_url==catalog.URL
    assert '来源未提供' in parsed.scope
    assert parse_qs(urlsplit(transport.ranking_url('industry_l2',funds=True)).query)['fs']==['m:90+s:4']
    assert parse_qs(urlsplit(transport.ranking_url('industry')).query)['fs']==['m:90+t:2']


@pytest.mark.parametrize('change',['duplicate','invalid_level','boolean_level','missing_l2','wrong_code'])
def test_changed_or_ambiguous_catalog_is_rejected(change):
    raw=json.loads(catalog_body())
    if change=='duplicate':raw['bklist'].append(raw['bklist'][1])
    elif change=='invalid_level':raw['bklist'][1]['flag']=4
    elif change=='boolean_level':raw['bklist'][1]['flag']=True
    elif change=='missing_l2':raw['bklist'][1]['flag']=1
    else:raw['bklist'][1]['code']='guess'
    with pytest.raises(SourceFailure,match='schema_changed'):
        catalog.parse_catalog(json.dumps(raw).encode(),NOW)


def test_new_l2_scope_roundtrips_while_legacy_payload_is_unclassified(monkeypatch):
    fetcher(monkeypatch)
    response=transport.fetch_rankings('eastmoney_funds',lambda:None)
    value=json.loads(response.body)
    result=transport.parse_rankings(response.body,response.received_at,'eastmoney_funds')
    industry=next(p for _,p,_ in result.records if p.dataset.endswith('industry_funds'))
    assert industry.rows[0]['industry_level']=='2'
    next(p for p in value['tables'] if p['table']=='industry_funds').pop('industry_filter')
    legacy=transport.parse_rankings(json.dumps(value).encode(),response.received_at,'eastmoney_funds')
    assert next(p for _,p,_ in legacy.records if p.dataset.endswith('industry_funds')).rows[0]['industry_level'] is None


def test_catalog_is_one_budgeted_request_and_original_classification_is_saved(monkeypatch):
    calls=fetcher(monkeypatch)
    metadata=[]
    monkeypatch.setattr(catalog,'fetch_catalog',lambda:(metadata.append(True) or Response(catalog_body(),NOW,1,200)))
    permits=[]
    response=transport.fetch_rankings('eastmoney_funds',lambda:permits.append(True),include_catalog=True)
    parsed=transport.parse_rankings(response.body,response.received_at,'eastmoney_funds')
    assert len(calls)==3 and len(metadata)==1 and len(permits)==4
    assert parsed.expected==4 and parsed.requests==4 and parsed.rejected==0
    assert parsed.records[-1][1].dataset==catalog.DATASET


@pytest.mark.parametrize('reason',['transport_error','schema_changed','permission_missing','rate_limited'])
def test_catalog_failure_preserves_valid_financial_tables_and_retry_hint(monkeypatch,reason):
    fetcher(monkeypatch)
    def fail():raise SourceFailure(reason,retry_after=90 if reason=='rate_limited' else None)
    monkeypatch.setattr(catalog,'fetch_catalog',fail)
    response=transport.fetch_rankings('eastmoney_funds',lambda:None,include_catalog=True)
    parsed=transport.parse_rankings(response.body,response.received_at,'eastmoney_funds')
    assert len(parsed.records)==3 and parsed.expected==4 and parsed.rejected==1 and parsed.requests==4
    if reason=='rate_limited':assert json.loads(response.body)['catalog_failure']['retry_after']==90


@pytest.mark.parametrize('reason',['rate_limited','intraday_window_wait'])
def test_optional_catalog_budget_boundary_preserves_tables_without_a_fourth_request(monkeypatch,reason):
    calls=fetcher(monkeypatch)
    permits=[]
    def reserve():
        permits.append(True)
        if len(permits)==4:raise SourceFailure(reason,retry_after=120)
    def forbidden():raise AssertionError('Deferred catalog must not contact supplier')
    monkeypatch.setattr(catalog,'fetch_catalog',forbidden)
    response=transport.fetch_rankings('eastmoney_funds',reserve,include_catalog=True)
    parsed=transport.parse_rankings(response.body,response.received_at,'eastmoney_funds')
    assert len(calls)==3 and parsed.requests==3
    assert len(parsed.records)==3 and parsed.expected==4 and parsed.rejected==1
    assert json.loads(response.body)['catalog_deferred']=={'reason':reason,'retry_after':120}


@pytest.mark.parametrize('reason',['permission_missing','lease_expired','configuration_changed','source_paused'])
def test_optional_catalog_does_not_swallow_revocation_or_lost_lease(monkeypatch,reason):
    fetcher(monkeypatch)
    permits=[]
    def reserve():
        permits.append(True)
        if len(permits)==4:raise SourceFailure(reason)
    with pytest.raises(transport.EastmoneyFailure,match=reason) as caught:
        transport.fetch_rankings('eastmoney_funds',reserve,include_catalog=True)
    assert caught.value.request_count==3


def test_deferred_catalog_cannot_claim_a_physical_request(monkeypatch):
    fetcher(monkeypatch)
    response=transport.fetch_rankings('eastmoney_funds',lambda:None)
    raw=json.loads(response.body)
    raw['catalog_deferred']={'reason':'rate_limited','retry_after':120}
    raw['request_count']=4
    with pytest.raises(SourceFailure,match='schema_changed'):
        transport.parse_rankings(json.dumps(raw).encode(),response.received_at,'eastmoney_funds')
