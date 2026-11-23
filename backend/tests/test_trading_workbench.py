"""Trading workbench contracts; real disposable PG, synthetic source facts only."""
import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import hasher
from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collect import store_reference_batch, store_quote_batch
from app.market.eastmoney_trading import (parse_ranking, parse_rankings, fetch_rankings, ranking_url, EastmoneyFailure)
from app.market.models import DataFetchRun, InstrumentOrigin, SourceSettings, Watchlist
from app.market.protocol import InstrumentKey, Response, SourceFailure, SHANGHAI, parse_tencent_quotes
from app.market.schemas import WatchlistUpdate
from app.market.service import source_id
from app.market.tushare import parse_reference, parameters
from app.models import AuditEvent, Instrument, Job, Provider, User
from test_data_sources import ORIGIN, PASSWORD, configure, configuration, login, quote_body, queue_and_process
from test_tushare import body, source

NOW=datetime(2026,10,23,8,tzinfo=UTC)


def ranking_fixture(*, code='BK0001', market=90, clock=True, net='-1250.5'):
    row={'f12':code,'f13':market,'f14':'合成板块','f3':'-1.5','f6':'123000','f62':net,
         'f66':None,'f72':'-1250.5','f78':None,'f84':'1250.5','f104':3,'f105':4,'f128':'合成股票','f140':'600000'}
    if clock:row['f124']=int(NOW.timestamp())
    return json.dumps({'rc':0,'data':{'total':301,'diff':[row]}},ensure_ascii=False).encode()


def test_parse_eastmoney_keeps_signed_zero_missing_units_and_actual_clock():
    table=parse_ranking(ranking_fixture(),NOW,'market.eastmoney.industry_funds','合成行业资金')
    row=table.rows[0]
    assert row['net_inflow_cny']=='-1250.5' and row['super_large_net_cny'] is None
    assert row['inflow_cny'] is None and row['outflow_cny'] is None
    assert row['source_total']=='301' and row['returned_count']=='1'
    assert row['trade_day']=='2026-10-23' and table.observed_day==date(2026,10,23)
    assert {c.key:c.unit for c in table.columns}['net_inflow_cny']=='CNY'
    unknown=parse_ranking(ranking_fixture(clock=False,net='0'),NOW,'market.eastmoney.industry_funds','合成行业资金')
    assert unknown.observed_day is None and unknown.rows[0]['source_time'] is None and unknown.rows[0]['net_inflow_cny']=='0'
    with pytest.raises(SourceFailure,match='future_source_time'):
        parse_ranking(ranking_fixture(),NOW-timedelta(hours=1),'market.eastmoney.industry_funds','合成行业资金')
    with pytest.raises(SourceFailure,match='invalid_number'):
        parse_ranking(ranking_fixture(net='nan'),NOW,'market.eastmoney.industry_funds','合成行业资金')


def test_parse_eastmoney_urls_are_bounded_and_do_not_guess_stock_identity():
    assert 'pz=200' in ranking_url('industry') and 'fid=f62' in ranking_url('concept',funds=True)
    with pytest.raises(SourceFailure,match='endpoint_not_allowed'):
        ranking_url('stocks',board_code='https://untrusted.example')
    with pytest.raises(SourceFailure,match='instrument_mismatch'):
        parse_ranking(ranking_fixture(code='600000',market=90),NOW,'market.eastmoney.constituents','合成成分')
    row=parse_ranking(ranking_fixture(code='600000',market=1),NOW,'market.eastmoney.constituents','合成成分',board_code='BK0001').rows[0]
    assert row['exchange']=='SSE' and row['board_code']=='BK0001'


def test_parse_eastmoney_fetch_is_explicit_and_failure_counts_requests(monkeypatch):
    seen=[]
    def fetch(url):
        seen.append(url)
        stock='m%3A0' in url
        return Response(ranking_fixture(code='600000' if stock else 'BK0001',market=1 if stock else 90),NOW,1,200)
    monkeypatch.setattr('app.market.eastmoney_trading.fetch_https',fetch)
    response=fetch_rankings('eastmoney_funds',lambda:None)
    result=parse_rankings(response.body,response.received_at,'eastmoney_funds')
    assert len(seen)==3 and result.requests==3
    assert {p.dataset for _,p,_ in result.records}=={'market.eastmoney.stock_funds','market.eastmoney.industry_funds','market.eastmoney.concept_funds'}
    def failed(url):raise SourceFailure('upstream_unavailable')
    monkeypatch.setattr('app.market.eastmoney_trading.fetch_https',failed)
    with pytest.raises(EastmoneyFailure) as error:fetch_rankings('eastmoney_boards',lambda:None)
    assert error.value.request_count==1 and error.value.reason=='upstream_unavailable'


def test_parse_top_inst_groups_identity_without_summing_overlapping_reasons():
    fields=['trade_date','ts_code','exalter','side','buy','sell','net_buy','reason']
    rows=[['20261023','600000.SH','合成席位','0','1000','200','800','单日合成原因'],
          ['20261023','600000.SH','合成席位','1','1000','200','800','单日合成原因'],
          ['20261023','600000.SH','合成席位','0','2000','300','1700','三日合成原因']]
    parsed=parse_reference(body(fields,rows),'tushare_top_inst',NOW).records[0][1]
    assert len(parsed.rows)==3 and parsed.rows[0]['side']=='0'
    assert {c.key:c.unit for c in parsed.columns}['buy']=='元'
    assert 'start_date' not in parameters('tushare_top_inst',[],NOW)
    repeated=parse_reference(body(fields,[rows[0],rows[0]]),'tushare_top_inst',NOW).records[0][1]
    assert len(repeated.rows)==2 and [r['source_order'] for r in repeated.rows]==['1','2']
    for malformed in [[*rows[0][:3],'2',*rows[0][4:]], [*rows[0][:4],'-1',*rows[0][5:]]]:
        with pytest.raises(SourceFailure):parse_reference(body(fields,[malformed]),'tushare_top_inst',NOW)
    with pytest.raises(SourceFailure,match='daily_data_not_published'):
        parse_reference(body([],[]),'tushare_top_inst',NOW)


def test_parse_new_tushare_units_are_endpoint_specific_and_old_unknown_payloads_untouched():
    fields=['ts_code','trade_date','net_mf_amount','net_mf_vol','buy_lg_amount']
    payload=parse_reference(body(fields,[['600000.SH','20261023','0',None,'1.25']]),'tushare_moneyflow',NOW).records[0][1]
    units={c.key:c.unit for c in payload.columns}
    assert units['net_mf_amount']=='万元' and units['net_mf_vol']=='手'
    assert payload.rows[0]['net_mf_amount']=='0' and payload.rows[0]['net_mf_vol'] is None
    payload=parse_reference(body(['ts_code','trade_date','total_mv'],[['600000.SH','20261023','1250']]),'tushare_daily_basic',NOW).records[0][1]
    assert payload.columns[-1].unit=='万元'
    with pytest.raises(ValidationError):WatchlistUpdate(instrument_ids=[uuid.UUID(int=1),uuid.UUID(int=1)])


def seed_instruments():
    with Session(engine()) as db,db.begin():
        values=[Instrument(exchange=exchange,symbol=symbol,name='合成自选'+symbol,asset_type='stock',valid_from=None,valid_to=None)
            for exchange,symbol in [('SSE','600000'),('SZSE','000001')]]
        db.add_all(values);db.flush()
        return [str(row.id) for row in values]


def test_watchlist_is_account_scoped_ordered_and_idempotent(client):
    assert client.get('/api/v1/watchlist').status_code==401
    headers=login(client,'viewer');ids=seed_instruments()
    assert client.get('/api/v1/watchlist').json()['data']['items']==[]
    def write(values,key='watchlist-save-first',extra=None):
        return client.post('/api/v1/watchlist',json={'instrument_ids':values},headers=extra or {**headers,'Idempotency-Key':key})
    assert write(ids,extra={**ORIGIN,'Idempotency-Key':'csrf-missing-test'}).status_code==403
    response=write(ids[::-1]);assert response.status_code==200,response.text
    assert [row['instrument']['id'] for row in response.json()['data']['items']]==ids[::-1]
    assert [row['position'] for row in response.json()['data']['items']]==[0,1]
    assert write(ids[::-1]).headers['Idempotency-Replayed']=='true'
    assert write(ids).status_code==409
    assert write([ids[0],ids[0]],'duplicate-watchlist').status_code==422
    assert write([str(uuid.uuid4())],'unknown-watchlist').status_code==422
    assert [row['instrument']['id'] for row in client.get('/api/v1/watchlist').json()['data']['items']]==ids[::-1]
    with Session(engine()) as db,db.begin():
        db.add(User(username='other-watch-user',password_hash=hasher.hash(PASSWORD),role='viewer',enabled=True))
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action=='watchlist.replace'))==1
    login_response=client.post('/api/v1/session',json={'username':'other-watch-user','password':PASSWORD},headers=ORIGIN)
    assert login_response.status_code==201
    assert client.get('/api/v1/watchlist').json()['data']['items']==[]


def test_watchlist_can_keep_revoked_saved_ids_while_removing_others_but_cannot_add_revoked_id(client):
    headers=login(client);ids=seed_instruments()
    configure(client,headers,configuration('tencent_web'))
    def replace(values):
        return client.post('/api/v1/watchlist',json={'instrument_ids':values},
            headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
    assert replace(ids).status_code==200
    with Session(engine()) as db,db.begin():
        clock=db.scalar(select(func.clock_timestamp()))
        hidden=Instrument(exchange='SSE',symbol='600001',name='合成未授权标的',asset_type='stock',valid_from=None,valid_to=None)
        db.add(hidden);db.flush();hidden_id=str(hidden.id)
        for ident in [uuid.UUID(ids[0]),hidden.id]:
            db.add(InstrumentOrigin(instrument_id=ident,source_id=source_id('tencent_web'),origin_kind='catalog',
                observed_name='权限已撤回的供应商名',first_observed_at=clock,last_observed_at=clock,
                last_content_hash='a'*64,source_version_id=None))
        db.get(Provider,source_id('tencent_web')).rights={'display':'denied','cache':'allowed'}
    result=replace([ids[0]])
    assert result.status_code==200 and [row['instrument']['id'] for row in result.json()['data']['items']]==[ids[0]]
    assert result.json()['data']['items'][0]['instrument']['name']=='标的资料不可用'
    assert replace([ids[0],hidden_id]).status_code==422
    assert replace([]).status_code==200
    # Once explicitly removed, a lost-rights code cannot be re-added.
    assert replace([ids[0]]).status_code==422


def test_batch_quotes_preserve_requested_order_and_do_not_request_upstream(client,monkeypatch):
    headers=login(client);configure(client,headers)
    with Session(engine()) as db,db.begin():
        clock=db.scalar(select(func.clock_timestamp()))
        raw=quote_body(clock=clock.astimezone(SHANGHAI).strftime('%Y%m%d%H%M%S'))
        provider=db.get(Provider,source_id('tencent_web'));config=db.get(SourceSettings,provider.id)
        store_quote_batch(db,provider,config,parse_tencent_quotes(raw,
            [InstrumentKey('SSE','000001','index'),InstrumentKey('SZSE','000001','stock')],clock))
    rows=client.get('/api/v1/instruments?q=000001').json()['data']['items']
    ids=[row['id'] for row in rows][::-1]
    monkeypatch.setattr('app.market.collect.fetch_https',lambda *a:pytest.fail('cached endpoint must not collect'))
    response=client.get('/api/v1/market-quotes',params={'instrument_ids':','.join(ids)})
    assert response.status_code==200 and response.json()['data']['selection']=='batch'
    assert [row['instrument']['id'] for row in response.json()['data']['items']]==ids
    assert client.get('/api/v1/market-quotes',params={'instrument_ids':','.join([ids[0]]*2)}).status_code==422
    assert client.get('/api/v1/market-quotes',params={'instrument_id':ids[0],'instrument_ids':ids[0]}).status_code==422


def save_table(family,raw,received):
    batch=parse_reference(raw,family,received)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(family));config=db.get(SourceSettings,provider.id)
        store_reference_batch(db,provider,config,batch,received)
        return db.scalar(select(EvidenceVersion.id).where(EvidenceVersion.source_id==provider.id).order_by(EvidenceVersion.version_number.desc()).limit(1))


def test_trading_datasets_read_actual_dates_all_versions_and_receipt_cutoff(client):
    headers=login(client);configure(client,headers,source('tushare_top_list'))
    clock=datetime.now(UTC)-timedelta(minutes=5)
    first=save_table('tushare_top_list',body(['trade_date','ts_code','reason','net_amount'],
        [['20011231','600000.SH','合成原因','-1']]),clock)
    second=save_table('tushare_top_list',body(['trade_date','ts_code','reason','net_amount'],
        [['20020102','600000.SH','合成原因','0']]),clock+timedelta(minutes=1))
    read=lambda **params:client.get('/api/v1/trading-datasets',params={'family':'tushare_top_list',**params})
    data=read().json()['data']
    assert data['trade_day']=='2002-01-02' and data['available_days']==['2002-01-02','2001-12-31']
    assert data['items'][0]['id']==str(second)
    assert read(day='2001-12-31').json()['data']['items'][0]['id']==str(first)
    data=read(as_of_time=(clock+timedelta(seconds=30)).isoformat()).json()['data']
    assert data['trade_day']=='2001-12-31' and data['items'][0]['id']==str(first)
    assert read(day='2002-01-01').json()['data']['items']==[]
    assert read(source_id=str(uuid.uuid4())).json()['data']['items']==[]
    assert read(as_of_time=(datetime.now(UTC)+timedelta(days=1)).isoformat()).status_code==422
    assert str(date.today()) not in data['available_days']
    with Session(engine()) as db,db.begin():
        db.get(Provider,source_id('tushare_top_list')).rights={'display':'denied','cache':'allowed'}
    assert read(day='2001-12-31').json()['data']['items']==[]


@pytest.mark.parametrize('family',['tushare_income','tushare_balancesheet','tushare_cashflow','tushare_fina_indicator'])
def test_reference_financial_comparison_uses_announcement_day_without_mutating_original_evidence(client,family):
    headers=login(client);configure(client,headers,source(family))
    fields=['ts_code','ann_date','f_ann_date','end_date','report_type','n_income']
    rows=[['000001.SZ','20260820','20260820','20260630','1','100'],
          ['000001.SZ','20260420','20260420','20260331','1','80'],
          ['000001.SZ','20260320','20260320','20251231','1','70'],
          ['000001.SZ','20251020','20251020','20250930','1','60']]
    ident=save_table(family,body(fields,rows),NOW)
    with Session(engine()) as db:
        saved=db.get(EvidenceVersion,ident)
        original=saved.payload.copy();digest=saved.content_hash
    read=lambda **params:client.get('/api/v1/trading-datasets',params={'family':family,**params}).json()['data']
    historical=read(day='2026-05-01')
    item=historical['items'][0]
    assert item['trade_day'] is None and item['data_mode']=='reference' and historical['available_days']==[]
    assert item['payload']==original and item['content_hash']==digest and item['id']==str(ident)
    assert [row['end_date'] for row in item['view_rows']]==['20260331','20251231','20250930']
    assert len(item['payload']['rows'])==4
    assert len(read(day='2026-10-23')['items'][0]['view_rows'])==4
    # The public release date supports a reference reconstruction; explicit
    # as-of still excludes this later received copy entirely.
    assert read(day='2026-05-01',as_of_time='2026-10-22T23:59:59Z')['items']==[]
    with Session(engine()) as db:
        saved=db.get(EvidenceVersion,ident)
        assert saved.payload==original and saved.content_hash==digest


def test_reference_financial_actual_announcement_controls_corrections_and_prior_batch_fallback(client):
    headers=login(client);configure(client,headers,source('tushare_income'))
    fields=['ts_code','ann_date','f_ann_date','end_date','report_type','n_income']
    first=save_table('tushare_income',body(fields,[['000001.SZ','20260320','20260320','20251231','1','70']]),
        datetime(2026,4,1,tzinfo=UTC))
    # A later revision of the same report has an earlier nominal ann_date but
    # a later actual announcement; it must not leak into April's reference.
    last=save_table('tushare_income',body(fields,[['000001.SZ','20260320','20260820','20251231','1','99']]),NOW)
    read=lambda day:client.get('/api/v1/trading-datasets',params={'family':'tushare_income','day':day}).json()['data']
    assert read('2026-04-30')['items'][0]['id']==str(first)
    assert read('2026-04-30')['items'][0]['view_rows'][0]['n_income']=='70'
    assert read('2026-09-01')['items'][0]['id']==str(last)


def test_reference_dividend_filters_announcement_but_keeps_known_future_action_dates(client):
    headers=login(client);configure(client,headers,source('tushare_dividend'))
    fields=['ts_code','end_date','ann_date','imp_ann_date','div_proc','record_date','ex_date','pay_date']
    rows=[['000001.SZ','20251231','20260301',None,'预案','20991230','20991231','21000101'],
          ['000001.SZ','20251231','20260801',None,'预案',None,None,None],
          ['000001.SZ','20251231','20260301','20260601','实施',None,None,None],
          ['000001.SZ','20251231',None,None,'预案',None,None,None]]
    # Parse with an explicit instrument, as the production selected endpoint.
    from test_tushare import KEY
    batch=parse_reference(body(fields,rows),'tushare_dividend',NOW,KEY)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_dividend'));config=db.get(SourceSettings,provider.id)
        store_reference_batch(db,provider,config,batch,NOW)
    result=client.get('/api/v1/trading-datasets?family=tushare_dividend&day=2026-03-02').json()['data']['items'][0]
    assert len(result['payload']['rows'])==4 and len(result['view_rows'])==1
    assert result['view_rows'][0]['ex_date']=='20991231' and result['view_rows'][0]['pay_date']=='21000101'
    assert result['trade_day'] is None and result['data_mode']=='reference'


def test_reference_missing_dividend_announcement_uses_receipt_day_without_inventing_release(client):
    from test_tushare import KEY
    headers=login(client);configure(client,headers,source('tushare_dividend'))
    fields=['ts_code','end_date','ann_date','div_proc','ex_date']
    raw=body(fields,[['000001.SZ','20251231',None,'预案','20991231']])
    received=datetime(2026,3,2,2,tzinfo=UTC)
    batch=parse_reference(raw,'tushare_dividend',received,KEY)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_dividend'));config=db.get(SourceSettings,provider.id)
        store_reference_batch(db,provider,config,batch,received)
    path='/api/v1/trading-datasets?family=tushare_dividend'
    assert client.get(path+'&day=2026-03-01').json()['data']['items']==[]
    result=client.get(path+'&day=2026-03-02').json()['data']['items'][0]
    assert result['view_rows'][0]['ann_date'] is None and result['view_rows'][0]['ex_date']=='20991231'
    assert result['trade_day'] is None and result['published_at'] is None


@pytest.mark.parametrize('family,fields,old,new',[
    ('tushare_ths_index',['ts_code','name','list_date'],['885001.TI','合成旧目录','20200101'],['885002.TI','合成新目录','20200101']),
    ('tushare_index_basic_sse',['ts_code','name','market','base_date'],['000001.SH','合成沪指','SSE','19901219'],['000002.SH','合成新沪指','SSE','19901219']),
    ('tushare_index_basic_szse',['ts_code','name','market','list_date'],['399001.SZ','合成深指','SZSE','19940101'],['399002.SZ','合成新深指','SZSE','19940101']),
    ('tushare_cn_cpi',['month','nt_yoy'],['202603','2.1'],['202603','2.2']),
])
def test_reference_untimed_directory_and_period_statistic_require_a_batch_received_by_review_day(client,family,fields,old,new):
    headers=login(client);configure(client,headers,source(family))
    first=save_table(family,body(fields,[old]),datetime(2026,4,1,tzinfo=UTC))
    last=save_table(family,body(fields,[new]),datetime(2026,6,1,tzinfo=UTC))
    read=lambda **params:client.get('/api/v1/trading-datasets',params={'family':family,**params}).json()['data']
    result=read(day='2026-05-01')
    item=result['items'][0]
    assert item['id']==str(first) and item['trade_day'] is None and item['data_mode']=='reference'
    assert result['available_days']==[] and item['view_rows']==item['payload']['rows']==[dict(zip(fields,old))]
    assert read(day='2026-03-31')['items']==[]
    assert read()['items'][0]['id']==str(last)
    assert read(day='2026-07-01',as_of_time='2026-04-02T00:00:00Z')['items'][0]['id']==str(first)


def test_reference_empty_latest_directory_does_not_resurrect_old_members(client):
    headers=login(client);configure(client,headers,source('tushare_ths_index'))
    fields=['ts_code','name']
    save_table('tushare_ths_index',body(fields,[['885001.TI','合成已移除目录项']]),datetime(2026,4,1,tzinfo=UTC))
    last=save_table('tushare_ths_index',body(fields,[]),datetime(2026,4,2,tzinfo=UTC))
    result=client.get('/api/v1/trading-datasets?family=tushare_ths_index&day=2026-05-01').json()['data']['items'][0]
    assert result['id']==str(last) and result['view_rows']==[] and result['payload']['rows']==[]


def test_unconfigured_new_sources_are_not_reported_connected_or_enabled(client):
    headers=login(client)
    sources=client.get('/api/v1/data-sources').json()['data']
    assert sources['items']==[]
    assert {'eastmoney_funds','eastmoney_boards','tushare_top_inst'} <= {item['family'] for item in sources['available_adapters']}
    value=configuration('eastmoney_boards');value.update(enabled=True,entitlement_status='unknown',interval_seconds=120)
    assert client.post('/api/v1/data-sources',json=value,headers={**headers,'Idempotency-Key':'eastmoney-rights-unknown'}).status_code==422
    value.update(entitlement_status='verified',board_codes=['BK0001'])
    response=configure(client,headers,value,key='eastmoney-explicit-board')
    assert response.json()['data']['board_codes']==['BK0001']
    assert response.json()['data']['collection_state']=='not_checked'
    value['board_codes']=['BK0001','BK0001']
    assert client.post('/api/v1/data-sources',json=value,headers={**headers,'Idempotency-Key':'eastmoney-duplicate-board'}).status_code==422


def running_job(family):
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        config=db.get(SourceSettings,source_id(family))
        clock=db.scalar(select(func.clock_timestamp()))
        job=Job(owner_id=config.owner_id,kind='source.collect',payload={'source_id':str(config.source_id)},
            state='running',attempts=1,lease_token=uuid.uuid4(),lease_until=clock+timedelta(minutes=2))
        db.add(job);db.flush()
        return job


def test_eastmoney_collection_saves_separate_actual_day_tables_and_read_never_fetches(client,monkeypatch):
    from app.market.collect import collect_job
    headers=login(client);configure(client,headers,configuration('eastmoney_funds'))
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr('app.market.collect.reserve_eastmoney_request',lambda job:None)
    with Session(engine()) as db:
        observed=db.scalar(select(func.clock_timestamp()))-timedelta(seconds=2)
    class SourceClock(datetime):
        @classmethod
        def now(cls,tz=None):return observed.astimezone(tz) if tz else observed.replace(tzinfo=None)
    monkeypatch.setattr('app.market.eastmoney_trading.datetime',SourceClock)
    monkeypatch.setattr('app.market.collect.datetime',SourceClock)
    def fetch(url):
        stock='m%3A0' in url
        # This complete-table case has one actual row. The shared parser fixture
        # deliberately declares 301 rows for separate bounded-page coverage tests.
        body=json.loads(ranking_fixture(code='600000' if stock else 'BK0001',market=1 if stock else 90))
        body['data']['total']=len(body['data']['diff'])
        return Response(json.dumps(body).encode(),observed,1,200)
    monkeypatch.setattr('app.market.eastmoney_trading.fetch_https',fetch)
    job=running_job('eastmoney_funds');collect_job(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert run.status=='complete' and run.accepted==3 and run.request_count==3
        assert db.get(Job,job.id).state=='succeeded'
    monkeypatch.setattr('app.market.eastmoney_trading.fetch_https',lambda *args:pytest.fail('read must not collect'))
    result=client.get('/api/v1/trading-datasets?family=eastmoney_funds&day=2026-10-23').json()['data']
    assert result['trade_day']=='2026-10-23' and len(result['items'])==3
    assert all(row['data_mode']=='intraday' for row in result['items'])
    assert all(row['payload']['rows'][0]['inflow_cny'] is None for row in result['items'])


def test_public_permit_rechecks_rights_and_persists_budget_across_families(client):
    from app.market.budget import EASTMONEY_ACTION, reserve_eastmoney_request
    headers=login(client)
    for family in ['eastmoney_funds','eastmoney_boards']:
        configure(client,headers,configuration(family),key=family)
    job=running_job('eastmoney_funds')
    reserve_eastmoney_request(job)
    with Session(engine()) as db,db.begin():
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==EASTMONEY_ACTION))==1
        db.get(Provider,source_id('eastmoney_funds')).rights={'display':'denied','cache':'allowed'}
    with pytest.raises(SourceFailure,match='permission_missing'):
        reserve_eastmoney_request(job)


def test_bulk_history_spacing_shares_the_persisted_public_permit_clock(client,monkeypatch):
    from app.market import budget
    headers=login(client)
    for family in ['eastmoney_funds','eastmoney_boards']:
        configure(client,headers,configuration(family),key=family)
    clock=[datetime.now(UTC)]
    monkeypatch.setattr(budget,'now',lambda db:clock[0])
    def advance(seconds):clock[0]+=timedelta(seconds=seconds)
    monkeypatch.setattr('time.sleep',advance)
    budget.reserve_eastmoney_request(running_job('eastmoney_boards'))
    budget.reserve_eastmoney_request(running_job('eastmoney_funds'),minimum_interval=10)
    with Session(engine()) as db:
        stamps=list(db.scalars(select(AuditEvent.created_at).where(AuditEvent.action==budget.EASTMONEY_ACTION).order_by(AuditEvent.created_at)))
        assert len(stamps)==2 and (stamps[1]-stamps[0]).total_seconds()>=10


def test_parse_sample_amount_is_one_source_actual_day_and_unknown_is_not_zero():
    from app.market.tushare_market import compare_limits
    from test_tushare_market import quote,limits,AT
    row=quote();other=quote('000001.SZ');old=quote('300001.SZ',at=AT-timedelta(days=1))
    assert compare_limits([row,other,old],[limits()],AT)['total_amount_cny']=='2000'
    other['amount']=None
    assert compare_limits([row,other],[limits()],AT)['total_amount_cny'] is None
    row['amount']='0'
    assert compare_limits([row],[limits()],AT)['total_amount_cny']=='0'
