"""New read/preview contracts on a disposable PostgreSQL with synthetic data."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.config import settings
from app.models import Job, Provider
from test_terminal_workbench import setup, save, calendar, DAY
from test_direction_data import setup as direction_setup


@pytest.fixture(autouse=True)
def prohibit_upstreams(monkeypatch):
    monkeypatch.setattr('urllib.request.build_opener',lambda *a,**k:pytest.fail('Unexpected supplier request'))


def seed_auction(client,monkeypatch):
    # Source authorization predates the receipt cutoff in this scenario.
    with monkeypatch.context() as registration:
        registration.setattr('app.market.service.now',lambda db:datetime.now(UTC)-timedelta(minutes=2))
        headers=setup(client,monkeypatch,['tushare_calendar_sse','tushare_calendar_szse','tushare_stk_auction'])
    stamp=datetime.now(UTC)
    with Session(engine()) as db,db.begin():
        calendar(db)
        rows=[dict(ts_code=f'{600000+i:06d}.SH',trade_date='20261023',price='11',pre_close='10',
                   amount=str(i*1000),vol='1000',turnover_rate='1.5',volume_ratio='2.3',float_share='10000') for i in range(1,252)]
        save(db,'tushare_stk_auction',rows,received=stamp)
    monkeypatch.setattr('app.screening.auction.now',lambda db:stamp+timedelta(seconds=1))
    return headers,stamp


def body(**updates):
    return dict(day=str(DAY),screen_context='auction',state='matched',limit=10,definition=dict(
        conditions=[dict(field='auction_amount_cny',operator='gte',value='201000')],
        sort_field='auction_amount_cny',sort_direction='desc'),**updates)


def test_new_analysis_requires_session_and_checks_parameters(client):
    assert client.get('/api/v1/review-data/analysis?trade_day=2026-10-23').status_code==401
    from test_data_sources import login
    login(client)
    assert client.get('/api/v1/review-data/analysis?trade_day=2026-10-23&kind=stock').status_code==422
    assert client.get('/api/v1/review-data/analysis?trade_day=2026-10-23&lookback=1000').status_code==422


def test_full_saved_auction_table_is_filtered_before_pagination(client,monkeypatch):
    headers,_=seed_auction(client,monkeypatch)
    r=client.post('/api/v1/screening/preview',json=body(),headers=headers)
    assert r.status_code==200,r.text
    v=r.json()['data']
    assert v['counts']['total']==251 and v['counts']['matched']==51
    assert v['items'][0]['code']=='600251.SH'
    assert v['items'][0]['metrics']['auction_turnover_rate']=='1.5'
    assert v['items'][0]['source_time'] is None
    assert v['coverage']['source_rows']==251 and v['has_more'] is True


def test_receipt_cutoff_and_current_rights_gate_auction(client,monkeypatch):
    headers,stamp=seed_auction(client,monkeypatch)
    r=client.post('/api/v1/screening/preview',json=body(as_of_time=(stamp-timedelta(seconds=1)).isoformat()),headers=headers)
    assert r.status_code==200,r.text
    assert r.json()['data']['counts']['total']==0
    from app.market.service import source_id
    with Session(engine()) as db,db.begin():
        db.get(Provider,source_id('tushare_stk_auction')).enabled=False
    r=client.post('/api/v1/screening/preview',json=body(),headers=headers)
    assert r.status_code==200 and r.json()['data']['counts']['total']==0
    assert r.json()['data']['coverage']['source_state']=='permission_denied'


def test_missing_yesterday_pool_keeps_group_unknown(client,monkeypatch):
    headers,_=seed_auction(client,monkeypatch)
    payload=body()
    payload['state']='insufficient'
    payload['definition']['conditions']=[dict(field='yesterday_up',operator='eq',value='1')]
    r=client.post('/api/v1/screening/preview',json=payload,headers=headers)
    assert r.status_code==200,r.text
    assert r.json()['data']['counts']['insufficient']==251
    assert r.json()['data']['counts']['matched']==0


def test_four_source_direction_collection_does_not_enqueue_kpl(client,monkeypatch):
    headers=direction_setup(client,monkeypatch)
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    r=client.post('/api/v1/direction-data/collections',json=dict(trade_day='2026-10-23',lookback=20,source_scope='four_sources'),
        headers={**headers,'Idempotency-Key':'synthetic-four-source-review'})
    assert r.status_code==202,r.text
    from app.market.service import source_id
    with Session(engine()) as db:
        rows=db.scalars(select(Job).where(Job.kind=='source.collect')).all()
        assert all(job.payload.get('source_id')!=str(source_id('direction_kpl_strength')) for job in rows)


def test_empty_historical_analysis_does_not_mutate_records_or_call_supplier(client,monkeypatch):
    direction_setup(client,monkeypatch)
    r=client.get('/api/v1/review-data/analysis?trade_day=2026-10-23&kind=groups')
    assert r.status_code==200,r.text
    assert r.json()['data']['groups']['membership_state'] in {'missing','permission_denied'}
    r=client.get('/api/v1/direction-data?trade_day=2026-10-23&lookback=20&sector_code=BK1184.DC')
    assert r.status_code==200,r.text
    assert r.json()['data']['coverage']['ranking_days']==0


def test_stock_projection_preserves_original_row_offset_and_markers(client,monkeypatch):
    setup(client,monkeypatch,['tushare_calendar_sse','tushare_calendar_szse','tushare_daily','tushare_top_list'])
    from app.market.review_store import persist_pages
    from app.market.review_protocol import ReviewPage, DATASET_FAMILIES
    from app.market.service import source_id
    from app.market.models import SourceSettings
    stamp=datetime.now(UTC)
    with Session(engine()) as db,db.begin():
        calendar(db)
        for dataset,rows in [('daily',[dict(ts_code='600001.SH',trade_date='20261023',open='10',high='12',low='9',close='11'),
                                      dict(ts_code='600002.SH',trade_date='20261023',open='20',high='22',low='19',close='21')]),
                             ('lhb',[dict(ts_code='600002.SH',trade_date='20261023',reason='合成上榜原因')])]:
            provider=db.get(Provider,source_id(DATASET_FAMILIES[dataset]))
            job=SimpleNamespace(id=uuid.uuid4(),payload={'review_context':dict(dataset=dataset,day=str(DAY),start_day=str(DAY),scope='all',configuration_sha256='a'*64)})
            page=ReviewPage(fields=list(rows[0]),rows=rows,source_url='https://example.org/synthetic',raw_hash='a'*64)
            persist_pages(db,provider,db.get(SourceSettings,provider.id),job,[(page,stamp)])
    r=client.get('/api/v1/review-data/analysis?trade_day=2026-10-23&kind=stock&stock=600002.SH')
    assert r.status_code==200,r.text
    record=next(row for row in r.json()['data']['records'] if row['trade_day']==str(DAY))
    assert record['quote']['close']=='21'
    assert record['quote']['row_reference']['source_row']==1
    assert record['listings'][0]['reason']=='合成上榜原因'


def test_four_source_review_preparation_excludes_all_legacy_public_datasets(client,monkeypatch):
    from app.models import User
    from app.market import review_jobs
    from test_data_sources import login
    login(client)
    called=[]
    def queued(db,owner,dataset,day,**kwargs):
        called.append(dataset)
        return SimpleNamespace(id=uuid.uuid4(),state='queued')
    monkeypatch.setattr(review_jobs,'queue_dataset',queued)
    monkeypatch.setattr(review_jobs,'available_trade_days',lambda *a,**k:[DAY-timedelta(days=n) for n in reversed(range(23))])
    with Session(engine()) as db,db.begin():
        job=Job(owner_id=db.scalar(select(User.id)),kind='review.prepare',state='running',lease_token=uuid.uuid4(),lease_until=datetime.now(UTC)+timedelta(minutes=5),payload=dict(
            calendar_jobs=[],review_batch_id=str(uuid.uuid4()),input=dict(trade_day=str(DAY),lookback=20,source_scope='four_sources',include_news=True)))
        db.add(job);db.flush();ident,token=job.id,job.lease_token
    review_jobs.prepare_job(SimpleNamespace(id=ident,lease_token=token))
    assert called
    assert not set(called)&{'news','tencent_minutes','sina_minutes'}


def test_new_persistent_projections_keep_etag_and_current_permissions(client,monkeypatch):
    direction_setup(client,monkeypatch)
    from test_direction_data import board_rows, NOW
    from app import read_cache
    from app.market.service import source_id
    monkeypatch.setattr(settings(),'read_cache_enabled',True)
    with Session(engine()) as db,db.begin():
        save(db,'tushare_dc_index',board_rows(),received=NOW)
    path='/api/v1/direction-data?trade_day=2026-10-23&lookback=20&sector_code=BK1211.DC'
    def ready(url):
        for _ in range(4):
            response=client.get(url)
            if response.status_code!=503:return response
            with Session(engine(),expire_on_commit=False) as db:read_cache.run_one(db)
        return response
    r=ready(path)
    assert r.status_code==200,r.text
    assert r.json()['data']['records'][-1]['rank']==12
    assert client.get(path,headers={'If-None-Match':r.headers['etag']}).status_code==304
    group=ready('/api/v1/review-data/analysis?trade_day=2026-10-23&kind=groups')
    assert group.status_code==200,group.text
    with Session(engine()) as db,db.begin():
        db.get(Provider,source_id('tushare_dc_index')).enabled=False
    hidden=ready(path)
    assert hidden.status_code==200,hidden.text
    assert all(row['rank'] is None for row in hidden.json()['data']['records'])
    assert hidden.headers['x-cache-scope']!=r.headers['x-cache-scope']


def seed_saved_close_ladder(client, monkeypatch, *, saved=True, receipt_offset=timedelta()):
    close=datetime(2026,10,23,8,30,tzinfo=UTC)
    with monkeypatch.context() as registration:
        registration.setattr('app.market.service.now',lambda db:close-timedelta(days=7))
        setup(client,monkeypatch,['tushare_calendar_sse','tushare_calendar_szse',
                                 'tushare_limit_list_d','tushare_limit_step'])
    with Session(engine()) as db,db.begin():
        calendar(db)
        if saved:
            save(db,'tushare_limit_step',[
                dict(ts_code='600001.SH',name='ST合成',trade_date='20261023',nums='6'),
                dict(ts_code='600002.SH',name='合成今日',trade_date='20261023',nums='5')],received=close+receipt_offset)
    return close


def test_saved_close_ladder_is_read_only_dated_and_has_original_row_reference(client,monkeypatch):
    from app.evidence.models import EvidenceVersion
    from app.market.service import source_id
    from sqlalchemy import func
    close=seed_saved_close_ladder(client,monkeypatch)
    with Session(engine()) as db:
        before=(db.scalar(select(func.count()).select_from(Job)),
                db.scalar(select(func.count()).select_from(EvidenceVersion)))
    path='/api/v1/review-data/analysis?trade_day=2026-10-23&kind=ladder'
    r=client.get(path)
    assert r.status_code==200,r.text
    row=r.json()['data']['records'][-1]
    assert row['trade_day']=='2026-10-23' and row['basis']=='supplier_ladder'
    assert [(s['ts_code'],s['board_height']) for s in row['stocks']]==[('600002.SH',5)]
    assert row['stocks'][0]['row_reference']['source_row']==1
    assert row['state']=='partial'
    earlier=client.get(path+'&as_of_time='+ (close-timedelta(seconds=1)).isoformat().replace('+','%2B'))
    assert earlier.status_code==200,earlier.text
    assert earlier.json()['data']['records'][-1]['stocks']==[]
    with Session(engine()) as db,db.begin():
        assert before==(db.scalar(select(func.count()).select_from(Job)),
                        db.scalar(select(func.count()).select_from(EvidenceVersion)))
        db.get(Provider,source_id('tushare_limit_step')).enabled=False
    revoked=client.get(path)
    assert revoked.status_code==200,revoked.text
    assert revoked.json()['data']['records'][-1]['stocks']==[]


def test_close_ladder_arrival_invalidates_cached_empty_result_and_full_pool_takes_priority(client,monkeypatch):
    from app import read_cache
    from app.read_cache_models import ReadCache
    from app.market.review_store import persist_pages
    from app.market.review_protocol import ReviewPage
    from app.market.models import SourceSettings
    from app.market.service import source_id
    close=seed_saved_close_ladder(client,monkeypatch,saved=False)
    monkeypatch.setattr(settings(),'read_cache_enabled',True)
    path='/api/v1/review-data/analysis?trade_day=2026-10-23&kind=ladder'
    initial=client.get(path)
    assert initial.status_code==200,initial.text
    assert initial.json()['data']['records'][-1]['stocks']==[]
    with Session(engine()) as db,db.begin():
        entry=db.scalar(select(ReadCache).where(ReadCache.operation=='review-analysis'))
        assert 'family:tushare_limit_step' in entry.dependency_keys
        save(db,'tushare_limit_step',[dict(ts_code='600002.SH',name='合成今日',trade_date='20261023',nums='5')],received=close)
    def rebuilt(matches):
        import time
        deadline=time.monotonic()+5
        response=client.get(path)
        while time.monotonic()<deadline and not matches(response):
            with Session(engine(),expire_on_commit=False) as db:read_cache.run_one(db)
            response=client.get(path)
            if not matches(response):time.sleep(.05)
        return response
    saved=rebuilt(lambda r:r.status_code==200 and bool(r.json()['data']['records'][-1]['stocks']))
    assert saved.status_code==200,saved.text
    assert saved.json()['data']['records'][-1]['stocks'][0]['board_height']==5
    assert saved.headers['etag']!=initial.headers['etag']
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_limit_list_d'))
        rows=[dict(ts_code='600003.SH',name='合成全池',trade_date='20261023',limit='U',limit_times='2')]
        job=SimpleNamespace(id=uuid.uuid4(),payload={'review_context':dict(dataset='pools',day=str(DAY),
            start_day=str(DAY),scope='all',configuration_sha256='a'*64)})
        page=ReviewPage(fields=list(rows[0]),rows=rows,source_url='https://example.org/synthetic',raw_hash='b'*64)
        persist_pages(db,provider,db.get(SourceSettings,provider.id),job,[(page,close+timedelta(minutes=1))])
    complete=rebuilt(lambda r:r.status_code==200 and r.json()['data']['records'][-1].get('basis') is None)
    assert complete.status_code==200,complete.text
    row=complete.json()['data']['records'][-1]
    with Session(engine()) as db:
        from app.market.review_store import load_dataset
        raw=load_dataset(db,'pools',DAY)
        cache=db.scalar(select(ReadCache).where(ReadCache.operation=='review-analysis'))
        diagnostic=dict(pool_state=raw['state'],pool_rows=raw['row_count'],cache_dirty=cache.dirty,
            cache_error=cache.last_error,refresh_after=str(cache.refresh_after))
    assert row.get('basis') is None and row['state']=='complete',diagnostic
    assert [(s['ts_code'],s['board_height']) for s in row['stocks']]==[('600003.SH',2)]


def test_late_backfilled_close_ladder_keeps_source_day_and_actual_receipt_cutoff(client,monkeypatch):
    close=seed_saved_close_ladder(client,monkeypatch,receipt_offset=timedelta(days=1))
    path='/api/v1/review-data/analysis?trade_day=2026-10-23&kind=ladder'
    saved=client.get(path)
    assert saved.status_code==200,saved.text
    row=saved.json()['data']['records'][-1]
    assert row['trade_day']=='2026-10-23' and row['stocks'][0]['board_height']==5
    assert row['stocks'][0]['row_reference']['received_at']==(close+timedelta(days=1)).isoformat()
    before=client.get(path+'&as_of_time='+close.isoformat().replace('+','%2B'))
    assert before.status_code==200,before.text
    assert before.json()['data']['records'][-1]['stocks']==[]
