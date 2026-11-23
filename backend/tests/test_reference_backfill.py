"""Real disposable PostgreSQL, synthetic daily backfills and paging; no network."""
from datetime import UTC, datetime, timedelta
import time
import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collect import store_calendar_batch
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response, SHANGHAI, SourceFailure
from app.market.reference_backfill import FAMILIES
from app.market.tushare_specs import DATED_FAMILIES
from app.market.service import source_id
from app.market.tushare_market import encoded
from app.models import AuditEvent, Job, Provider
from app.worker import claim, process
from test_data_sources import configure, configuration, login
from test_tushare import source


def setup(client, monkeypatch, family, *, opened=True):
    headers = login(client)
    monkeypatch.setattr(settings(),'tushare_token',SecretStr('synthetic-reference-test-token'))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    configure(client,headers,source(family),key='reference-source-config')
    calendar=configuration('baostock_daily');calendar['interval_seconds']=21600
    configure(client,headers,calendar,key='reference-calendar-config')
    day = datetime.now(UTC).astimezone(SHANGHAI).date()-timedelta(days=1)
    if opened is not None:
        with Session(engine()) as db,db.begin():
            provider=db.get(Provider,source_id('baostock_daily'))
            store_calendar_batch(db,provider,db.get(SourceSettings,provider.id),[(day,opened)],datetime.now(UTC),exchanges=('SSE','SZSE'))
    return headers,day


def enqueue(client,headers,family,day):
    return client.post('/api/v1/data-sources/'+str(source_id(family))+'/refresh',json={'day':str(day)},
        headers={**headers,'Idempotency-Key':uuid.uuid4().hex})


def sample(family,day,code='000001.SZ'):
    api=family.removeprefix('tushare_')
    row=dict(ts_code=code,trade_date=day.strftime('%Y%m%d'),name='合成资料',net_amount='1',
        float_share='100',pre_close='10',up_limit='11',down_limit='9',nums='3',reason='合成原因')
    if api in {'dc_index','dc_member','moneyflow_ind_dc'}:
        row['ts_code']='BK1184.DC'
    if api=='dc_member':row['con_code']=code
    if api=='top_inst':row.update(exalter='合成席位',side='0',buy='1',sell='0',net_buy='1')
    if api=='ths_daily':row.update(ts_code='885001.TI',pct_change='1.23',vol='100')
    if api=='limit_list_ths':row.update(price='10',status='首板',tag='合成标签',lu_desc='合成原因',limit_amount='100',limit_order='1')
    return row


@pytest.mark.parametrize('family',sorted(FAMILIES))
def test_explicit_daily_date_reaches_supplier_preserves_live_schedule_and_marks_fetch_mode(client,monkeypatch,family):
    headers,day=setup(client,monkeypatch,family)
    with Session(engine()) as db:before=db.get(SourceSettings,source_id(family)).next_run_at
    response=enqueue(client,headers,family,day)
    assert response.status_code==202,response.text
    assert response.json()['data']['requested_day']==str(day)
    calls=[]
    def fetch(api,params,fields,*args):
        calls.append(params.copy());row=sample(family,day)
        return Response(encoded(list(row),[row]),datetime.now(UTC),1,200)
    monkeypatch.setattr('app.market.collect.request_tushare',fetch)
    job=claim();assert job and ('dated_context' if family in DATED_FAMILIES else 'reference_context') in job.payload and 'minute_context' not in job.payload
    process(job)
    assert len(calls)==1 and calls[0]['trade_date']==day.strftime('%Y%m%d')
    with Session(engine()) as db:
        saved=db.get(Job,job.id);assert saved.state=='succeeded',saved.error_code
        assert db.get(SourceSettings,source_id(family)).next_run_at==before
    runs=client.get('/api/v1/data-fetch-runs',params={'source_id':str(source_id(family))}).json()['data']['items']
    assert runs[0]['requested_day']==str(day) and runs[0]['acquisition_mode']=='backfill'


@pytest.mark.parametrize('opened,reason',[(False,'backfill_day_closed'),(None,'calendar_not_ready')])
def test_reference_backfill_rejects_closed_or_unknown_days_before_job_or_request(client,monkeypatch,opened,reason):
    # The compatible reference-only path retains its 45-day/calendar policy;
    # the eight dated-table families retain their explicit 365-day semantics.
    headers,day=setup(client,monkeypatch,'tushare_top_list',opened=opened)
    response=enqueue(client,headers,'tushare_top_list',day)
    assert response.status_code==422 and response.json()['error']['code']==reason
    with Session(engine()) as db:assert db.scalar(select(Job.id).where(Job.kind=='source.collect')) is None


def test_reference_configuration_changes_before_claim_prevent_http(client,monkeypatch):
    family='tushare_limit_step';headers,day=setup(client,monkeypatch,family)
    assert enqueue(client,headers,family,day).status_code==202
    with Session(engine()) as db,db.begin():db.get(SourceSettings,source_id(family)).interval_seconds+=1
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *a,**k:pytest.fail('changed context must not request'))
    job=claim();process(job)
    with Session(engine()) as db:
        assert db.get(Job,job.id).error_code=='configuration_changed'
        assert db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id)).request_count==0


def test_reference_wrong_returned_day_is_not_published(client,monkeypatch):
    family='tushare_limit_step';headers,day=setup(client,monkeypatch,family)
    assert enqueue(client,headers,family,day).status_code==202
    row=sample(family,day-timedelta(days=1))
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *a:Response(encoded(list(row),[row]),datetime.now(UTC),1,200))
    job=claim();process(job)
    with Session(engine()) as db:
        assert db.get(Job,job.id).error_code=='history_response_out_of_range'
        assert db.scalar(select(EvidenceVersion.id).where(EvidenceVersion.source_id==source_id(family),EvidenceVersion.kind=='reference')) is None


def test_moneyflow_dc_three_pages_spend_separate_two_second_permits_and_publish_only_complete_batch(client,monkeypatch):
    family='tushare_moneyflow_dc';headers,day=setup(client,monkeypatch,family)
    assert enqueue(client,headers,family,day).status_code==202
    calls=[]
    def fetch(api,params,fields,*args):
        offset=params.get('offset',0);calls.append((offset,time.monotonic()))
        assert params['limit']==3000
        rows=[sample(family,day,f'{index:06d}.SZ') for index in range(offset+1,offset+(3000 if offset<6000 else 30)+1)]
        return Response(encoded(list(rows[0]),rows),datetime.now(UTC),1,200)
    monkeypatch.setattr('app.market.collect.request_tushare',fetch)
    job=claim();process(job)
    assert [offset for offset,_ in calls]==[0,3000,6000]
    with Session(engine()) as db:
        saved=db.get(Job,job.id);assert saved.state=='succeeded',saved.error_code
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id));assert run.request_count==3
        permits=list(db.scalars(select(AuditEvent).where(AuditEvent.action=='source.request.tushare',
            AuditEvent.resource_id==job.id).order_by(AuditEvent.created_at)))
        assert len(permits)==3 and all((right.created_at-left.created_at).total_seconds()>=2 for left,right in zip(permits,permits[1:]))
        evidence=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id(family),EvidenceVersion.kind=='reference'))
        assert len(evidence.payload['rows'])==6030
        assert {row['source_page_offset'] for row in evidence.payload['rows']}=={'0','3000','6000'}
        assert all(len(row['source_page_sha256'])==64 for row in evidence.payload['rows'])


def test_moneyflow_dc_second_page_transport_failure_does_not_publish_first_page(client,monkeypatch):
    family='tushare_moneyflow_dc';headers,day=setup(client,monkeypatch,family)
    assert enqueue(client,headers,family,day).status_code==202
    def fetch(api,params,fields,*args):
        if params.get('offset'):raise SourceFailure('transport_error')
        rows=[sample(family,day,f'{index:06d}.SZ') for index in range(1,3001)]
        return Response(encoded(list(rows[0]),rows),datetime.now(UTC),1,200)
    monkeypatch.setattr('app.market.collect.request_tushare',fetch)
    job=claim();process(job)
    with Session(engine()) as db:
        assert db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id)).request_count==2
        assert db.scalar(select(EvidenceVersion.id).where(EvidenceVersion.source_id==source_id(family),EvidenceVersion.kind=='reference')) is None


def test_persisted_v2_reference_job_and_legacy_member_scope_survive_without_reconfiguration(client,monkeypatch):
    from app.market.reference_backfill import make_context
    from app.evidence.common import now
    family='tushare_dc_member';headers,day=setup(client,monkeypatch,family)
    legacy={'symbols':[],'board_codes':['BK1184']}
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(family));config.config=legacy
        provider=db.get(Provider,config.source_id)
        context=make_context(db,provider,config,day,now(db))
        job=Job(owner_id=config.owner_id,kind='source.collect',payload={'source_id':str(provider.id),'reference_context':context})
        db.add(job);db.flush();ident=job.id
        before=config.next_run_at
    state=next(row for row in client.get('/api/v1/data-sources').json()['data']['items'] if row['family']==family)
    assert state['board_codes']==['BK1184'] and state['sector_codes']==['BK1184.DC']
    # A different new intent cannot take ownership of the preexisting old job.
    assert enqueue(client,headers,family,day).status_code==409
    calls=[]
    def fetch(api,params,fields,*args):
        calls.append(params.copy());row=sample(family,day)
        return Response(encoded(list(row),[row]),datetime.now(UTC),1,200)
    monkeypatch.setattr('app.market.collect.request_tushare',fetch)
    running=claim();assert running.id==ident
    process(running)
    assert len(calls)==1 and calls[0]['ts_code']=='BK1184.DC'
    with Session(engine()) as db:
        saved=db.get(Job,ident)
        assert saved.state=='succeeded',saved.error_code
        assert saved.payload['reference_context']==context and 'dated_context' not in saved.payload
        assert db.get(SourceSettings,source_id(family)).config==legacy
        assert db.get(SourceSettings,source_id(family)).next_run_at==before
        evidence=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id(family),EvidenceVersion.kind=='reference'))
        assert evidence.source_key=='reference:dc_member:BK1184.DC'


@pytest.mark.parametrize('old_context',[False,True])
def test_paged_funds_configuration_change_after_first_response_prevents_second_http_and_publish(client,monkeypatch,old_context):
    from app.market.reference_backfill import make_context
    from app.evidence.common import now
    family='tushare_moneyflow_dc';headers,day=setup(client,monkeypatch,family)
    if old_context:
        with Session(engine()) as db,db.begin():
            provider=db.get(Provider,source_id(family));config=db.get(SourceSettings,provider.id)
            db.add(Job(owner_id=config.owner_id,kind='source.collect',payload={'source_id':str(provider.id),
                'reference_context':make_context(db,provider,config,day,now(db))}))
    else:
        assert enqueue(client,headers,family,day).status_code==202
    calls=[]
    def fetch(api,params,fields,*args):
        calls.append(params.copy())
        rows=[sample(family,day,f'{index:06d}.SZ') for index in range(1,3001)]
        with Session(engine()) as db,db.begin():
            db.get(SourceSettings,source_id(family)).interval_seconds+=1
        return Response(encoded(list(rows[0]),rows),datetime.now(UTC),1,200)
    monkeypatch.setattr('app.market.collect.request_tushare',fetch)
    job=claim();process(job)
    assert len(calls)==1
    with Session(engine()) as db:
        assert db.get(Job,job.id).error_code=='configuration_changed'
        assert db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id)).request_count==1
        assert db.scalar(select(EvidenceVersion.id).where(EvidenceVersion.source_id==source_id(family),EvidenceVersion.kind=='reference')) is None
