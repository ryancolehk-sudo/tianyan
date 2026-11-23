"""Synthetic sector fixtures on disposable PostgreSQL; never live upstream in CI."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collect import store_reference_batch
from app.market.budget import ACTION, ASHARE_FAMILIES
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response, SourceFailure
from app.market.sector_heat import DATASET, URL, parse_sector_heat
from app.market.service import source_id
from app.models import AuditEvent, Instrument, Job, Provider
from app.worker import claim, process
from test_data_sources import configuration, configure, login


def sample():
    return {'ok': True, 'endpoint': 'hot', 'tier': 'free', 'source': 'synthetic', 'data': [
        {'name': '合成行业', 'symbol': 'pt01000001', 'stock_type': 'BK-HY-1', 'rank': '2', 'rankdelta': '0', 'date': '2020-01-02 08:25:00', 'zxj': 'not-a-price'},
        {'name': '合成海外板块', 'symbol': 'us10000001', 'stock_type': 'BK', 'rank': '1', 'rankdelta': '-2', 'date': None}]}


def parse(value):
    return parse_sector_heat(json.dumps(value).encode(), datetime.now(UTC))


def test_sector_rank_and_supplier_namespace_are_preserved_without_quote_or_time_invention():
    batch=parse(sample()); payload=batch.records[0][1]
    assert len(batch.records)==batch.expected==batch.requests==1 and batch.records[0][2] is None
    assert [row['rank'] for row in payload.rows]==['1','2']
    assert payload.rows[0]['symbol']=='us10000001' and payload.rows[0]['rank_delta']=='-2'
    assert payload.rows[1]['ranking_updated_raw']=='2020-01-02 08:25:00' and payload.rows[1]['rank_delta']=='0'
    assert payload.observed_day is None and '时区未注明' in payload.scope and '不是涨幅或资金排名' in payload.scope
    assert not {'zxj','price','zdf','amount','instrument_id','quote_time'}.intersection(payload.rows[0])
    concept=sample();concept['data'][0]['symbol']='pt01GN0001';concept['data'][0]['stock_type']='BK'
    assert parse(concept).records[0][1].rows[1]['symbol']=='pt01GN0001'


def test_empty_board_list_and_unknown_rank_change_remain_explicit():
    value=sample(); value['data']=[]
    assert parse(value).records[0][1].rows==[]
    value=sample(); value['data'][0]['rankdelta']=None
    assert parse(value).records[0][1].rows[1]['rank_delta'] is None


@pytest.mark.parametrize('field,bad', [('symbol','sh600000'),('symbol','pt01000001<script>'),('stock_type','stock'),
    ('rank',True),('rank','0'),('rank','51'),('rankdelta',1.5),('rankdelta','NaN'),('name','<img src=x>'),
    ('name',''),('date','2020-02-30 08:25:00'),('date','2020-01-02T08:25:00Z')])
def test_changed_or_ambiguous_row_is_rejected_instead_of_filled(field,bad):
    value=sample(); value['data'][0][field]=bad
    with pytest.raises(SourceFailure,match='schema_changed'):parse(value)


def test_duplicate_rank_or_identity_and_oversized_batch_are_not_silently_merged():
    for mode in ['rank','symbol','oversized']:
        value=sample()
        if mode=='oversized':value['data']*=16
        else:value['data'][1][mode]=value['data'][0][mode]
        with pytest.raises(SourceFailure,match='schema_changed'):parse(value)


def test_endpoint_tier_error_and_unaware_receipt_are_separate_failures():
    for field,value in [('endpoint','quote'),('tier','pro'),('data',{})]:
        body=sample();body[field]=value
        with pytest.raises(SourceFailure,match='schema_changed'):parse(body)
    body=sample();body['ok']=False
    with pytest.raises(SourceFailure,match='upstream_unavailable'):parse(body)
    with pytest.raises(SourceFailure,match='schema_changed'):
        parse_sector_heat(json.dumps(sample()).encode(),datetime.now())


def test_configuration_refuses_symbols_and_too_fast_collection(client):
    headers=login(client)
    for change in [{'symbols':[{'exchange':'SSE','symbol':'000001','asset_type':'index'}]}, {'interval_seconds':299}]:
        body={**configuration('ashare_sector_heat'),**change}
        result=client.post('/api/v1/data-sources',json=body,headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
        assert result.status_code==422


def test_worker_uses_shared_budget_and_rechecks_rights_without_creating_security_identities(client,monkeypatch):
    headers=login(client);configure(client,headers,configuration('ashare_sector_heat'))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    assert 'ashare_sector_heat' in ASHARE_FAMILIES
    calls=[]
    def fetch(url):
        calls.append(url);return Response(json.dumps(sample()).encode(),datetime.now(UTC),2,200)
    monkeypatch.setattr('app.market.collect.fetch_https',fetch)
    with Session(engine()) as db,db.begin():db.add(AuditEvent(action=ACTION,created_at=datetime.now(UTC)))
    queued=client.post('/api/v1/data-sources/'+str(source_id('ashare_sector_heat'))+'/refresh',json={},headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
    assert queued.status_code==202
    task=claim();process(task);assert calls==[]
    with Session(engine()) as db,db.begin():
        saved=db.get(Job,task.id);assert saved.state=='queued' and saved.attempts==0
        saved.available_at=datetime.now(UTC)-timedelta(seconds=1)
        db.execute(update(AuditEvent).where(AuditEvent.action==ACTION).values(created_at=datetime.now(UTC)-timedelta(seconds=40)))
    process(claim());assert calls==[URL]
    url='/api/v1/reference-datasets?dataset='+DATASET
    rows=client.get(url).json()['data']['items'];assert len(rows)==1 and len(rows[0]['payload']['rows'])==2
    coverage=client.get('/api/v1/data-coverage').json()['data']['items']
    assert next(row for row in coverage if row['id']=='D09')['state']=='partial'
    assert all(next(row for row in coverage if row['id']==ident)['state']=='pending' for ident in ['D10','D11'])
    with Session(engine()) as db:
        evidence=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id('ashare_sector_heat')))
        assert evidence.event_at is None and evidence.published_at is None and not evidence.historical_eligible and evidence.instrument_id is None
        assert db.scalar(select(func.count()).select_from(Instrument))==0
        attempt=db.scalar(select(DataFetchRun));assert attempt.accepted==attempt.expected==attempt.request_count==1
    revoked=configuration('ashare_sector_heat',enabled=False);revoked['rights']['display']='denied'
    configure(client,headers,revoked,key=uuid.uuid4().hex)
    assert client.get(url).json()['data']['items']==[]


def test_return_to_previous_ranking_preserves_an_observation_and_empty_correction_hides_old_rows(client):
    headers=login(client);configure(client,headers,configuration('ashare_sector_heat'))
    for label in ['合成行业A','合成行业B','合成行业A','合成行业A']:
        value=sample();value['data'][0]['name']=label
        with Session(engine()) as db,db.begin():
            provider=db.get(Provider,source_id('ashare_sector_heat'))
            saved=store_reference_batch(db,provider,db.get(SourceSettings,provider.id),parse(value),datetime.now(UTC))
    assert saved==(0,1)
    with Session(engine()) as db:
        rows=db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id('ashare_sector_heat')).order_by(EvidenceVersion.version_number)).all()
        assert [row.payload['rows'][1]['name'] for row in rows]==['合成行业A','合成行业B','合成行业A']
        assert len({row.source_revision for row in rows})==3
    value=sample();value['data']=[]
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('ashare_sector_heat'))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),parse(value),datetime.now(UTC))
    current=client.get('/api/v1/reference-datasets?dataset='+DATASET).json()['data']['items'][0]
    assert current['payload']['rows']==[]
