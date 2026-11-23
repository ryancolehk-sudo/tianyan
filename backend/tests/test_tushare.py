"""Synthetic responses on disposable real PostgreSQL; no paid credential in CI."""
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.error import HTTPError

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.db import engine
from app.market.collect import store_quote_batch
from app.market.models import SourceSettings
from app.market.protocol import InstrumentKey, Response, SHANGHAI, SourceFailure
from app.market.service import source_id
from app.market.tushare import (clock, parameters, parse_calendar, parse_catalog, parse_quotes,
    parse_reference, request, table)
from app.market.tushare_specs import SPECS
from app.models import AuditEvent, Job, Provider
from app.worker import claim, process
from test_data_sources import configuration, configure, login
from app.market.live import parse_tickflow

KEY = InstrumentKey('SZSE','000001','stock')
TOKEN = 'synthetic-test-token-only'


def body(fields, rows):
    return json.dumps({'code':0,'msg':None,'data':{'fields':fields,'items':rows}}).encode()


def quote_body(at=None, *, price='12.3', timed=True):
    at = at or datetime.now(UTC)-timedelta(seconds=2)
    fields = ['ts_code','name','pre_close','open','high','low','close','vol','amount']
    values = ['000001.SZ','合成Tushare股票','12','12','13','11',price,100.0,1230]
    if timed:
        fields.append('trade_time');values.append(at.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S'))
    return body(fields,[values])


def source(family):
    value = configuration(family)
    value['interval_seconds'] = SPECS[family].minimum_interval
    if family == 'tushare_dc_member':
        value['sector_codes'] = ['BK1184.DC']  # Explicit synthetic configured scope; never all members.
    if SPECS[family].selected:
        value['symbols'] = [{'exchange':'SSE','symbol':'000001','asset_type':'index'}] if family=='tushare_index_daily' else [KEY.__dict__]
    return value


def queue(client, headers, family):
    response = client.post('/api/v1/data-sources/'+str(source_id(family))+'/refresh',json={},headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
    assert response.status_code == 202
    process(claim())


def test_private_settings_and_fixed_endpoint_reject_without_echo():
    value = Settings(tushare_token=TOKEN)
    assert TOKEN not in repr(value) and TOKEN not in value.model_dump_json()
    assert Settings(tushare_endpoint='https://quotes.example.test/compat').tushare_endpoint.endswith('/compat')
    for invalid in ['http://quotes.example.test/', 'https://localhost/', 'https://api.tushare.pro/?token='+TOKEN]:
        with pytest.raises(ValidationError) as error:
            Settings(tushare_token=TOKEN,tushare_endpoint=invalid)
        assert TOKEN not in str(error.value)
    with pytest.raises(ValidationError) as error:
        Settings(tushare_token=TOKEN+'\n')
    assert TOKEN not in str(error.value)


def test_missing_key_and_unapproved_destination_never_open_network(monkeypatch):
    monkeypatch.setattr('urllib.request.build_opener',lambda *a:pytest.fail('No network permitted'))
    with pytest.raises(SourceFailure,match='credentials_missing'):
        request('daily',{},'',None,'https://api.tushare.pro/')
    with pytest.raises(SourceFailure,match='endpoint_not_allowed'):
        request('daily',{},'',SecretStr(TOKEN),'https://unapproved.example/')


def test_operator_endpoint_pins_approved_address_and_rejects_private_dns(monkeypatch):
    import urllib.request
    from app.ai import protocols
    from app.market.tushare import TushareHTTPSHandler
    handler = TushareHTTPSHandler()
    req = urllib.request.Request('https://quotes.example.test:8443/compat')
    req.timeout = 15
    monkeypatch.setattr(protocols.socket, 'getaddrinfo', lambda *a, **kw: [(None, None, None, '', ('127.0.0.1', 8443))])
    with pytest.raises(SourceFailure, match='endpoint_not_allowed'):
        handler.https_open(req)
    monkeypatch.setattr(protocols.socket, 'getaddrinfo', lambda *a, **kw: [(None, None, None, '', ('1.1.1.1', 8443))])
    monkeypatch.setattr(handler, 'do_open', lambda connect, request: connect(request.host, timeout=request.timeout))
    connection = handler.https_open(req)
    assert (connection.host, connection.pinned_address, connection.port) == ('quotes.example.test', '1.1.1.1', 8443)


def test_transport_keeps_token_in_post_only_and_discards_echo(monkeypatch):
    class Reply:
        status=200
        def __enter__(self):return self
        def __exit__(self,*a):return None
        def read(self, maximum):return json.dumps({'msg':TOKEN}).encode()
    class Opener:
        def open(self, req, timeout):
            assert req.full_url=='https://api.tushare.pro/' and TOKEN not in req.full_url
            assert json.loads(req.data)['token']==TOKEN and timeout==15
            return Reply()
    monkeypatch.setattr('urllib.request.build_opener',lambda *a:Opener())
    with pytest.raises(SourceFailure,match='unsafe_upstream_response') as error:
        request('daily',{},'',SecretStr(TOKEN),'https://api.tushare.pro/')
    assert TOKEN not in str(error.value)


@pytest.mark.parametrize('code,message,expected',[(403,'private-secret','permission_missing'),(2002,'private-secret','permission_missing'),(-2001,'每分钟频次超限','rate_limited'),(-1,'private upstream error','upstream_unavailable')])
def test_controlled_errors_never_echo_supplier_message(code,message,expected):
    with pytest.raises(SourceFailure,match=expected) as error:
        table(json.dumps({'code':code,'msg':message}).encode())
    assert message not in str(error.value)


def test_snapshot_clock_units_identity_missing_and_future_fields():
    batch = parse_quotes(quote_body(),[KEY],datetime.now(UTC))
    assert batch.records[0].price == Decimal('12.3')
    assert batch.records[0].quote_time is not None and batch.records[0].volume_shares==Decimal(100)
    unknown=parse_quotes(quote_body(timed=False),[KEY],datetime.now(UTC))
    assert unknown.records[0].quote_time is None
    future=parse_quotes(quote_body(datetime.now(UTC)+timedelta(hours=1)),[KEY],datetime.now(UTC))
    assert not future.records and future.rejected['000001.SZ']=='future_source_time'
    with pytest.raises(SourceFailure,match='unexpected_instrument'):
        parse_quotes(quote_body(),[InstrumentKey('SSE','000001','index')],datetime.now(UTC))
    duplicate=json.loads(quote_body());duplicate['data']['items']*=2
    assert parse_quotes(json.dumps(duplicate).encode(),[KEY],datetime.now(UTC)).rejected['000001.SZ']=='duplicate_instrument'


def test_daily_dates_and_bars_never_invent_source_clock_or_units():
    raw=body(['ts_code','trade_date','open','high','low','close','pre_close','vol','amount'],
        [['000001.SZ','20011231','12','13','11','12.3','12','100','120']])
    batch=parse_quotes(raw,[KEY],datetime.now(UTC),daily=True)
    q=batch.records[0]
    assert q.trade_day == date(2001,12,31) and q.quote_time is None and q.quote_kind=='daily_close'
    ref=parse_reference(raw,'tushare_daily',datetime.now(UTC),KEY)
    assert len(ref.bars)==1 and ref.bars[0][1].bar_end_at is None and ref.bars[0][1].volume_shares is None
    assert ref.records[0][1].rows[0]['vol']=='100'


def test_calendar_requires_exact_exchange_complete_month_and_unique_dates():
    raw=body(['exchange','cal_date','is_open'],[['SSE','20011230','0'],['SSE','20011231','1']])
    assert parse_calendar(raw,'SSE',date(2001,12,30),date(2001,12,31))==[(date(2001,12,30),False),(date(2001,12,31),True)]
    for exchange,start in [('SZSE',date(2001,12,30)),('SSE',date(2001,12,29))]:
        with pytest.raises(SourceFailure,match='incomplete_calendar'):
            parse_calendar(raw,exchange,start,date(2001,12,31))


def test_catalog_cannot_pick_first_conflicting_identity():
    fields=['ts_code','symbol','name','exchange','list_status']
    first=['000001.SZ','000001','合成目录','SZSE','L']
    assert parse_catalog(body(fields,[first])).records[0].instrument==KEY
    with pytest.raises(SourceFailure,match='duplicate_instrument'):
        parse_catalog(body(fields,[first,['000001.SZ','000001','冲突合成名','SZSE','L']]))


def test_numeric_leading_columns_and_missing_cells_keep_original_meaning():
    value=parse_reference(body(['date','on','1w'],[['2001-12-31',None,'2.1']]),'tushare_shibor',datetime.now(UTC))
    payload=value.records[0][1]
    assert payload.rows[0]=={'date':'2001-12-31','on':None,'field_1w':'2.1'}
    assert payload.columns[2].label=='1w' and payload.columns[2].unit is None


@pytest.mark.parametrize('fields', [[], ['trade_date','ts_code','limit']])
def test_unpublished_limit_list_is_not_a_schema_change_or_zero_list(fields):
    with pytest.raises(SourceFailure,match='daily_data_not_published'):
        parse_reference(body(fields,[]),'tushare_limit_list_d',datetime.now(UTC))
    if not fields:
        with pytest.raises(SourceFailure,match='schema_changed'):
            parse_reference(body(fields,[]),'tushare_shibor',datetime.now(UTC))


@pytest.mark.parametrize('data', [None, {}, {'fields':None,'items':[]},
    {'fields':[],'items':None}, {'fields':[],'items':[['unexpected']]},
    {'fields':['trade_date','trade_date'],'items':[]}, {'fields':['token'],'items':[]}])
def test_limit_list_empty_handling_keeps_malformed_schema_failures(data):
    raw=json.dumps({'code':0,'data':data}).encode()
    with pytest.raises(SourceFailure,match='schema_changed'):
        parse_reference(raw,'tushare_limit_list_d',datetime.now(UTC))


def test_limit_list_empty_does_not_hide_denied_permission_or_change_valid_rows():
    with pytest.raises(SourceFailure,match='permission_missing'):
        parse_reference(json.dumps({'code':403,'data':{'fields':[],'items':[]}}).encode(),
            'tushare_limit_list_d',datetime.now(UTC))
    result=parse_reference(body(['trade_date','ts_code','limit'],[['20011231','000001.SZ','U']]),
        'tushare_limit_list_d',datetime.now(UTC))
    assert result.records[0][1].rows==[{'trade_date':'20011231','ts_code':'000001.SZ','limit':'U'}]


def test_unpublished_daily_list_keeps_cache_and_next_poll_without_pausing(client,monkeypatch):
    from app.market.models import DataFetchRun
    from app.evidence.models import EvidenceVersion
    from app.market.collect import store_reference_batch
    headers=login(client)
    monkeypatch.setattr(settings(),'tushare_token',SecretStr(TOKEN))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    family='tushare_limit_list_d';configure(client,headers,source(family))
    # This regression isolates publication handling from calendar acquisition.
    monkeypatch.setattr('app.market.collect.pool_day',lambda db,clock:date(2001,12,31))
    before=datetime.now(UTC)-timedelta(days=1)
    cached=parse_reference(body(['trade_date','ts_code','limit'],[['20011231','000001.SZ','U']]),family,before)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(family));config=db.get(SourceSettings,provider.id)
        store_reference_batch(db,provider,config,cached,before)
    with Session(engine()) as db:
        hashes=list(db.scalars(select(EvidenceVersion.content_hash)))
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *a:Response(body([],[]),datetime.now(UTC),1,200))
    queue(client,headers,family)
    with Session(engine()) as db:
        config=db.get(SourceSettings,source_id(family))
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.source_id==config.source_id))
        assert config.paused_reason is None
        assert run.status=='empty' and run.reason=='daily_data_not_published' and run.request_count==1
        assert run.accepted is None and run.body_hash is not None
        assert config.next_run_at>=run.received_at+timedelta(seconds=config.interval_seconds)
        assert list(db.scalars(select(EvidenceVersion.content_hash)))==hashes


@pytest.mark.parametrize('daily',[False,True])
def test_empty_current_quotes_are_unavailable_while_daily_contract_stays_strict(daily):
    with pytest.raises(SourceFailure,match='schema_changed' if daily else 'instrument_unavailable'):
        parse_quotes(body([],[]),[KEY],datetime.now(UTC),daily=daily)


@pytest.mark.parametrize('data',[None,{}, {'fields':None,'items':[]},
    {'fields':[],'items':None},{'fields':[],'items':[['unexpected']]}])
def test_current_quotes_still_reject_malformed_empty_tables(data):
    with pytest.raises(SourceFailure,match='schema_changed'):
        parse_quotes(json.dumps({'code':0,'data':data}).encode(),[KEY],datetime.now(UTC))


@pytest.mark.parametrize('family',['tushare_rt_k','tushare_market_rt','tushare_rt_indices'])
def test_empty_current_table_keeps_next_poll_without_pausing_the_source(client,monkeypatch,family):
    from app.market.models import DataFetchRun
    from app.evidence.models import EvidenceVersion
    monkeypatch.setattr(settings(),'tushare_token',SecretStr(TOKEN))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    headers=login(client);configure(client,headers,source(family))
    with Session(engine()) as db:before=list(db.scalars(select(EvidenceVersion.content_hash)))
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *a:Response(body([],[]),datetime.now(UTC),1,200))
    queue(client,headers,family)
    with Session(engine()) as db:
        config=db.get(SourceSettings,source_id(family))
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.source_id==config.source_id))
        assert config.paused_reason is None and run.reason=='instrument_unavailable' and run.request_count==1
        assert config.next_run_at>=run.received_at+timedelta(seconds=config.interval_seconds)
        assert list(db.scalars(select(EvidenceVersion.content_hash)))==before


def test_financials_select_recent_distinct_periods_instead_of_provider_row_order():
    received=datetime(2026,10,21,tzinfo=UTC)
    fields=['ts_code','ann_date','f_ann_date','end_date','report_type','revenue']
    rows=[['000001.SZ','20250301','20250301','20241231','1','10'],
          ['000001.SZ','20260820','20260820','20260630','1','20'],
          ['000001.SZ','20260420','20260420','20260331','1','30'],
          ['000001.SZ','20260320','20260320','20251231','1','40'],
          ['000001.SZ','20251020','20251020','20250930','1','50'],
          ['000001.SZ','20260821','20260821','20260630','2','999'],
          ['000001.SZ','20260822','20260822','20260630','1','21']]
    parsed=parse_reference(body(fields,rows),'tushare_income',received,KEY).records[0][1]
    assert [r['end_date'] for r in parsed.rows]==['20260630','20260331','20251231','20250930']
    assert parsed.rows[0]['revenue']=='21' and 'report_type=1' in parsed.scope
    params=parameters('tushare_income',[KEY],received)
    assert params['limit']==100 and params['report_type']=='1'
    assert (datetime.strptime(params['end_date'],'%Y%m%d')-datetime.strptime(params['start_date'],'%Y%m%d')).days==730
    for invalid in [['000001.SZ','19950310','19950310','19941231','1','1'],
                    ['000001.SZ','20261022','20261022','20260930','1','1']]:
        with pytest.raises(SourceFailure,match='invalid_source_time'):
            parse_reference(body(fields,[invalid]),'tushare_income',received,KEY)
    with pytest.raises(SourceFailure,match='schema_changed'):
        parse_reference(body(fields,[rows[0],[*rows[0][:-1],'99']]),'tushare_income',received,KEY)
    indicator_fields=['ts_code','ann_date','end_date','eps','update_flag']
    corrected=parse_reference(body(indicator_fields,[['000001.SZ','20260820','20260630','1','0'],
        ['000001.SZ','20260820','20260630','2','1']]),'tushare_fina_indicator',received,KEY).records[0][1]
    assert len(corrected.rows)==1 and corrected.rows[0]['eps']=='2'


def test_margin_uses_bounded_published_window_instead_of_unpublished_today(client,monkeypatch):
    headers=login(client)
    monkeypatch.setattr(settings(),'tushare_token',SecretStr(TOKEN))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    configure(client,headers,source('tushare_margin_detail'))
    observed=[]
    def fetch(api,params,*args):
        assert api=='margin_detail' and params['limit']==400 and 'trade_date' not in params
        start=datetime.strptime(params['start_date'],'%Y%m%d').date()
        end=datetime.strptime(params['end_date'],'%Y%m%d').date()
        assert (end-start).days==10
        observed.append(params)
        return Response(body(['trade_date','ts_code','rzye'],[['20011231','000001.SZ',123]]),datetime.now(UTC),1,200)
    monkeypatch.setattr('app.market.collect.request_tushare',fetch)
    queue(client,headers,'tushare_margin_detail')
    assert len(observed)==1
    items=client.get('/api/v1/reference-datasets?dataset=tushare.margin_detail').json()['data']['items']
    assert items[0]['payload']['rows'][0]['trade_date']=='20011231'
    assert '最多400行' in items[0]['payload']['scope']


@pytest.mark.parametrize('family',list(SPECS))
def test_each_adapter_configuration_is_bounded_and_registered(client,family):
    headers=login(client)
    configure(client,headers,source(family))
    params=parameters(family,[InstrumentKey(**v) for v in source(family)['symbols']],datetime.now(UTC))
    assert 1<=params['limit']<=8000
    assert len(client.get('/api/v1/data-sources').json()['data']['items'])==1


def test_missing_credential_is_visible_without_enqueuing_or_network(client,monkeypatch):
    headers=login(client);configure(client,headers,source('tushare_rt_k'))
    monkeypatch.setattr(settings(),'tushare_token',None)
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    row=client.get('/api/v1/data-sources').json()['data']['items'][0]
    assert row['credential_configured'] is False and row['collection_state']=='credentials_missing'
    reply=client.post('/api/v1/data-sources/'+str(source_id('tushare_rt_k'))+'/refresh',json={},headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
    assert reply.status_code==422 and reply.json()['error']['code']=='credentials_missing'
    with Session(engine()) as db:
        assert not db.scalar(select(Job).where(Job.kind=='source.collect'))


def test_worker_persists_paid_clock_and_units_and_revocation_falls_back(client,monkeypatch):
    headers=login(client)
    monkeypatch.setattr(settings(),'tushare_token',SecretStr(TOKEN))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    configure(client,headers,source('tushare_rt_k'))
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *a:Response(quote_body(),datetime.now(UTC),1,200))
    queue(client,headers,'tushare_rt_k')
    with Session(engine()) as db:
        p=db.get(Provider,source_id('tushare_rt_k'))
        assert p.family=='tushare_rt_k'
    instrument=client.get('/api/v1/instruments?q=000001').json()['data']['items']
    stock=next(v for v in instrument if v['asset_type']=='stock')
    path='/api/v1/market-quotes?instrument_id='+stock['id']
    current=client.get(path).json()['data']['items'][0]
    assert current['provenance']['family']=='tushare_rt_k' and current['quote']['volume_shares']=='100'
    assert current['quote']['amount_cny']=='1230'
    # A fresh whole alternative has an intentionally different previous close.
    fallback=configuration('tickflow_live');fallback['symbols']=[KEY.__dict__];fallback['interval_seconds']=60
    configure(client,headers,fallback,key=uuid.uuid4().hex)
    def save_free():
        raw={'data':[{'symbol':'000001.SZ','region':'CN','last_price':10,'prev_close':9,'open':10,'high':10,'low':9,'timestamp':int(datetime.now(UTC).timestamp()*1000)}]}
        with Session(engine()) as db,db.begin():
            p=db.get(Provider,source_id('tickflow_live'));c=db.get(SourceSettings,p.id)
            store_quote_batch(db,p,c,parse_tickflow(json.dumps(raw).encode(),[KEY],datetime.now(UTC)))
    save_free()
    assert client.get(path).json()['data']['items'][0]['provenance']['family']=='tushare_rt_k'
    # ADR0029 prefers eligible paid data. Explicit selection is retained, while
    # incomplete records and revoked display rights must still fall back whole.
    chosen=client.patch('/api/v1/data-center/routes',json={'expected_revision':1,'changes':[
        {'capability_id':'stock_quote','provider_id':'tushare'}]},headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
    assert chosen.status_code==200,chosen.text
    assert client.get(path).json()['data']['items'][0]['provenance']['family']=='tushare_rt_k'
    with Session(engine()) as db,db.begin():
        p=db.get(Provider,source_id('tushare_rt_k'));c=db.get(SourceSettings,p.id)
        store_quote_batch(db,p,c,parse_quotes(quote_body(datetime.now(UTC),price=None),[KEY],datetime.now(UTC)))
    incomplete=client.get(path).json()['data']['items'][0]
    assert incomplete['provenance']['family']=='tickflow_live' and incomplete['quote']['price']=='10'
    with Session(engine()) as db,db.begin():
        paid=db.get(Provider,source_id('tushare_rt_k'));paid.rights={**paid.rights,'display':'denied'}
    selected=client.get(path).json()['data']['items'][0]
    assert selected['provenance']['family']=='tickflow_live' and selected['quote']['previous_close']=='9'
    assert TOKEN not in client.get('/api/v1/data-sources').text


def test_budget_wait_does_not_write_false_run_or_request(client,monkeypatch):
    headers=login(client);monkeypatch.setattr(settings(),'tushare_token',SecretStr(TOKEN));monkeypatch.setattr(settings(),'data_collection_enabled',True)
    for family in ['tushare_shibor','tushare_cn_cpi']:
        configure(client,headers,source(family),key=uuid.uuid4().hex)
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *a:Response(body(['date','on'],[['2001-12-31','2.1']]),datetime.now(UTC),1,200))
    queue(client,headers,'tushare_shibor')
    with Session(engine()) as db:
        first_permit=db.scalar(select(AuditEvent.created_at).where(AuditEvent.action=='source.request.tushare'))
    monkeypatch.setattr('app.market.budget.now',lambda db:first_permit)
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *a:pytest.fail('A local wait must not contact supplier'))
    queue(client,headers,'tushare_cn_cpi')
    runtime=client.get('/api/v1/data-sources').json()['data']
    row=next(r for r in runtime['items'] if r['family']=='tushare_cn_cpi')
    assert row['last_attempt'] is None and runtime['today_request_count']==1


EXTENDED = [
 ('tushare_index_basic_sse',['ts_code','name','market'],[['000001.SH','合成沪指','SSE']]),
 ('tushare_index_basic_szse',['ts_code','name','market'],[['399001.SZ','合成深指','SZSE']]),
 ('tushare_stk_limit',['trade_date','ts_code','up_limit','down_limit'],[['20011231','000001.SZ','13','11']]),
 ('tushare_dividend',['ts_code','end_date','ann_date','div_proc','ex_date'],[['000001.SZ','20011231','20020301','预案','20991231']]),
]


@pytest.mark.parametrize('family,fields,rows',EXTENDED)
def test_extended_paid_tables_configure_collect_and_expose_original_fields(client,monkeypatch,family,fields,rows):
    headers=login(client)
    monkeypatch.setattr(settings(),'tushare_token',SecretStr(TOKEN))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    configure(client,headers,source(family))
    captured=[]
    def fetch(api,params,*args):
        captured.append(params)
        if SPECS[family].selected:assert params['ts_code']=='000001.SZ'
        else:assert params['market']==SPECS[family].exchange
        return Response(body(fields,rows),datetime.now(UTC),1,200)
    monkeypatch.setattr('app.market.collect.request_tushare',fetch)
    queue(client,headers,family)
    assert len(captured)==1
    result=client.get('/api/v1/reference-datasets',params={'family':family}).json()['data']['items']
    assert len(result)==1 and result[0]['payload']['rows']==[dict(zip(fields,row)) for row in rows]
    state=next(r for r in client.get('/api/v1/data-sources').json()['data']['items'] if r['family']==family)
    assert state['last_attempt']['status']=='complete' and state['last_attempt']['request_count']==1
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize('family,fields,rows',EXTENDED)
def test_extended_tables_reject_missing_identity_columns_and_duplicates(family,fields,rows):
    key=KEY if SPECS[family].selected else None
    with pytest.raises(SourceFailure,match='schema_changed'):
        parse_reference(body(fields[1:],[rows[0][1:]]),family,datetime.now(UTC),key)
    with pytest.raises(SourceFailure,match='duplicate_instrument'):
        parse_reference(body(fields,rows*2),family,datetime.now(UTC),key)


def test_extended_tables_keep_market_price_time_and_planned_event_semantics():
    with pytest.raises(SourceFailure,match='instrument_mismatch'):
        parse_reference(body(['ts_code','name','market'],[['000001.SH','合成','SZSE']]),
            'tushare_index_basic_sse',datetime.now(UTC))
    with pytest.raises(SourceFailure,match='inconsistent_price_range'):
        parse_reference(body(['ts_code','trade_date','up_limit','down_limit'],[['000001.SZ','20011231','10','11']]),
            'tushare_stk_limit',datetime.now(UTC),KEY)
    with pytest.raises(SourceFailure,match='invalid_source_time'):
        parse_reference(body(['ts_code','end_date','ann_date','div_proc'],[['000001.SZ','20011231','20990101','预案']]),
            'tushare_dividend',datetime.now(UTC),KEY)
    # A future announced action date remains a schedule, never a future receipt.
    planned=parse_reference(body(EXTENDED[3][1],EXTENDED[3][2]),'tushare_dividend',datetime.now(UTC),KEY)
    assert planned.records[0][1].rows[0]['ex_date']=='20991231'
    assert '不当已发生事件' in planned.records[0][1].scope
    params=parameters('tushare_dividend',[KEY],datetime.now(UTC))
    assert params=={'limit':100,'ts_code':'000001.SZ'}


@pytest.mark.parametrize('symbol',['H12345.SH','ABCDEF123456.SH','TEST.SH','A12345.SZ'])
def test_index_directory_retains_provider_alphanumeric_code_without_stock_claim(symbol):
    family='tushare_index_basic_sse' if symbol.endswith('.SH') else 'tushare_index_basic_szse'
    fields=['ts_code','name','market'];market=SPECS[family].exchange
    batch=parse_reference(body(fields,[[symbol,'合成供应商指数',market]]),family,datetime.now(UTC))
    assert batch.records[0][2] is None and batch.records[0][1].rows[0]['ts_code']==symbol
    assert not batch.bars


@pytest.mark.parametrize('symbol',['bad code.SH','bad/code.SH','bad:code.SH','中文.SH','A'*33+'.SH','H12345.SZ','000001.BJ'])
def test_index_directory_still_rejects_bad_identifier_or_wrong_market(symbol):
    with pytest.raises(SourceFailure,match='instrument_mismatch'):
        parse_reference(body(['ts_code','name','market'],[[symbol,'合成指数','SSE']]),
            'tushare_index_basic_sse',datetime.now(UTC))
