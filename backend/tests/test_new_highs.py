"""Synthetic protocol/lineage/count tests; no supplier traffic or real prices."""
import json
from datetime import UTC, date, datetime, timedelta
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.db import engine
from app.models import Job
from app.market import new_highs as read
from app.market.new_high_protocol import group_rows, history_page, history_rows, stocks_page
from app.market.protocol import SourceFailure
from test_data_sources import login

DAY=date(2026,11,6)
def encoded(value):return json.dumps(value,ensure_ascii=False).encode()
def native(code='600001',flag=1):return [code,'合成新高',12.3,2.4,'合成题材',10000,100,500,-400,500000,900000,flag,'801001','合成板块',2.0]
def groups(rows=None,day=str(DAY)):
    return encoded(dict(errcode='0',Date=day,GroupCount=1,GroupList=[dict(GroupID=801001,GroupName='合成板块',List=rows if rows is not None else [native()])]))
def history(total=1,new=1):return encoded(dict(errcode='0',x=[f'20261106_{total}_{new}_0']))
def context():return dict(day=str(DAY),start_day=str(DAY))
def table(day,items):
    return dict(state='complete',rows=[dict(kind='summary',trade_date=day.strftime('%Y%m%d'),total=str(len(items)),new_count=str(sum(r['is_new']=='1' for r in items))),*items],row_count=len(items)+1,evidence_refs=[],dataset='new_highs',trade_day=str(day),scope='all')
def row(day,code,flag='1'):
    return dict(kind='stock',trade_date=day.strftime('%Y%m%d'),ts_code=code,name='合成股票',is_new=flag,group_name='合成板块',price='10',pct_chg='1',themes='合成',amount='100',main_net='2',actual_float='1000',actual_turnover='10')

def test_native_schema_units_and_flags_are_preserved_and_counts_close():
    page=stocks_page([groups()],history(),str(DAY),'https://apphis.longhuvip.com/')
    r=page.rows[1]
    assert r['ts_code']=='600001.SH' and r['amount']=='10000' and r['actual_float']=='500000'
    assert r['actual_turnover']=='2.0' and r['main_sell']=='-400' and r['is_new']=='1'
    assert r['group_name']=='合成板块' and page.rows[0]['total']=='1'

@pytest.mark.parametrize('total,new',[(2,1),(1,0)])
def test_truncated_or_wrong_new_count_never_publishes(total,new):
    with pytest.raises(SourceFailure,match='review_highs_incomplete'):stocks_page([groups()],history(total,new),str(DAY),'')

def test_duplicate_stock_or_wrong_native_date_is_rejected():
    with pytest.raises(SourceFailure,match='review_duplicate_row'):group_rows(groups([native(),native()]),str(DAY))
    with pytest.raises(SourceFailure,match='review_highs_day_mismatch'):group_rows(groups(day='2026-11-05'),str(DAY))

@pytest.mark.parametrize('position,value',[(2,None),(3,float('nan')),(5,-1),(11,True),(11,2),(14,-1)])
def test_invalid_row_is_not_coerced_to_zero(position,value):
    r=native();r[position]=value
    with pytest.raises(SourceFailure):group_rows(groups([r]),str(DAY))

def test_complete_empty_has_explicit_source_day_and_history_zero():
    page=stocks_page([groups([])],history(0,0),str(DAY),'')
    assert len(page.rows)==1 and page.rows[0]['total']=='0'
    with pytest.raises(SourceFailure):stocks_page([groups([])],history(1,1),str(DAY),'')

def test_history_dates_and_new_flags_are_source_values():
    assert history_rows(history())[0]['trade_date']=='20261106'
    assert len(history_page(history(),context(),'').rows)==1
    with pytest.raises(SourceFailure,match='review_highs_day_unavailable'):
        history_page(history(),dict(day='2026-11-09',start_day=str(DAY)),'')
    with pytest.raises(SourceFailure):history_rows(encoded(dict(errcode='0',x=['20261106_1_2_0'])))

def test_multi_day_unions_members_and_new_flags_by_code_not_sum():
    a=DAY-timedelta(days=1)
    first=table(a,[row(a,'600001.SH'),row(a,'600002.SH','0')])
    last=table(DAY,[row(DAY,'600001.SH','0'),row(DAY,'600003.SH')])
    rows,missing=read.union_rows({a:first,DAY:last},[a,DAY])
    assert not missing and len(rows)==3 and sum(r['is_new'] for r in rows)==2
    assert next(r for r in rows if r['ts_code']=='600001.SH')['membership_day']==str(DAY)
    result,gaps=read.union_rows({a:dict(state='missing'),DAY:last},[a,DAY])
    assert len(result)==2 and gaps==[str(a)]

def test_small_groups_are_aggregated_but_keep_filter_members():
    rows=[dict(group_name='甲',is_new=True),dict(group_name='乙',is_new=False),*[dict(group_name='丙',is_new=True) for _ in range(3)]]
    result=read.aggregate_groups(rows)
    assert result[0]['name']=='丙' and result[-1]['count']==2 and set(result[-1]['members'])=={'甲','乙'}

def test_earlier_quotes_are_withheld_or_filled_only_with_selected_day_tables():
    earlier=DAY-timedelta(days=1)
    r={**row(earlier,'600001.SH'), 'membership_day':str(earlier),'quote_day':str(earlier)}
    read.apply_end_quotes([r],DAY,{})
    assert r['price'] is None and r['main_net'] is None and r['native_values']['price']=='10'
    daily=dict(state='complete',rows=[dict(trade_date='20261106',ts_code='600001.SH',close='12',pct_chg='3',amount='4',vol='20')])
    basic=dict(state='complete',rows=[dict(trade_date='20261106',ts_code='600001.SH',close='12',free_share='10')])
    funds=dict(state='complete',rows=[dict(trade_date='20261106',ts_code='600001.SH',net_amount='2')])
    read.apply_end_quotes([r],DAY,dict(daily=daily,group_basics=basic,stock_funds=funds))
    assert r['price']=='12' and r['amount']=='4000' and r['main_net']=='20000'
    assert r['actual_float']=='1200000' and r['actual_turnover']=='2' and r['quote_day']==str(DAY)

def test_cutoff_is_forwarded_to_every_archive_and_unknown_day_is_not_replaced(monkeypatch):
    stamp=datetime(2026,11,9,tzinfo=UTC);calls=[]
    monkeypatch.setattr(read,'available_trade_days',lambda *a,**k:[DAY])
    def load(db,dataset,day,**kw):
        calls.append((dataset,day,kw['as_of_time']))
        return table(DAY,[row(DAY,'600001.SH')]) if dataset=='new_highs' else dict(state='missing',rows=[],evidence_refs=[])
    monkeypatch.setattr(read,'load_dataset',load)
    result=read.new_highs_view(object(),DAY,as_of_time=stamp)
    assert result['total']==result['new_count']==1 and result['state']=='complete'
    assert all(v[2]==stamp for v in calls)
    assert read.new_highs_view(object(),DAY-timedelta(days=1),as_of_time=stamp)['trade_day']=='2026-11-05'

def test_endpoint_requires_auth_and_get_never_creates_jobs_or_calls_vendor(client,monkeypatch):
    assert client.get('/api/v1/review-data/new-highs').status_code==401
    login(client,role='researcher')
    monkeypatch.setattr('urllib.request.build_opener',lambda *a:pytest.fail('GET must not fetch'))
    with Session(engine()) as db:before=db.scalar(select(func.count()).select_from(Job))
    response=client.get('/api/v1/review-data/new-highs?trade_day=2026-11-06')
    assert response.status_code==200 and response.json()['data']['total'] is None
    assert client.get('/api/v1/review-data/new-highs?trade_day=2026-11-06&start_day=2026-11-09').status_code==422
    assert client.get('/api/v1/review-data/new-highs?as_of_time=2099-01-01T00:00:00Z').status_code==422
    assert client.post('/api/v1/review-data/new-highs/collections',json={}).status_code in {403,422}
    with Session(engine()) as db:assert db.scalar(select(func.count()).select_from(Job))==before


def test_collection_closes_native_counts_and_publishes_one_atomic_manifest(client,monkeypatch):
    from app.config import settings
    from app.market import review_jobs as jobs
    from app.market.protocol import Response
    from app.market.review_store import load_dataset
    from test_review_collection import setup_source
    _,provider,config,job=setup_source('new_highs',target=DAY)
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr(jobs,'_reserve_public',lambda *a:True)
    monkeypatch.setattr(jobs.time,'sleep',lambda seconds:None)
    monkeypatch.setattr(jobs,'fetch_public',lambda url:Response(history() if 'GetDayNewHigh' in url else groups(),datetime.now(UTC),1,200))
    jobs.collect_job(job)
    with Session(engine()) as db:
        saved=load_dataset(db,'new_highs',DAY)
        assert db.get(Job,job.id).state=='succeeded' and saved['state']=='complete'
        assert saved['row_count']==2 and saved['rows'][1]['ts_code']=='600001.SH'


def test_1630_new_highs_window_and_restart_reuse(client,monkeypatch):
    from app.market import postclose_schedule as schedule
    from test_review_collection import setup_source
    setup_source('new_highs',target=DAY)
    clock=datetime.fromisoformat('2026-11-06T16:30:00+08:00')
    monkeypatch.setattr(schedule,'available_trade_days',lambda *a,**k:[DAY])
    monkeypatch.setattr(schedule,'load_dataset',lambda *a,**k:dict(state='missing'))
    calls=[]
    def queue(db,owner,dataset,day,**kwargs):
        from app.market.new_high_protocol import FAMILY
        from app.market.review_store import family_id
        calls.append((dataset,day,kwargs))
        db.add(Job(owner_id=owner.id,kind='review.collect',state='queued',payload={
            'source_id':str(family_id(FAMILY)),'review_context':dict(dataset=dataset,day=str(day),scope='all')}))
    monkeypatch.setattr('app.market.review_jobs.queue_dataset',queue)
    with Session(engine()) as db,db.begin():assert schedule.queue_due(db,clock-timedelta(seconds=1))==0
    with Session(engine()) as db,db.begin():assert schedule.queue_due(db,clock)==1
    with Session(engine()) as db,db.begin():assert schedule.queue_due(db,clock+timedelta(seconds=61))==0
    assert calls[0][0]=='new_high_history' and calls[0][2]['postclose_archive'] is True


def test_only_latest_native_reads_and_probe_use_general_lane(client,monkeypatch):
    from app.worker import lane_expression
    from test_review_collection import setup_source
    _,_,_,job=setup_source('new_highs',target=DAY)
    with Session(engine()) as db,db.begin():
        saved=db.get(Job,job.id)
        assert db.scalar(select(lane_expression()).where(Job.id==job.id))=='recovery'
        saved.payload={**saved.payload,'review_latest':True};db.flush()
        assert db.scalar(select(lane_expression()).where(Job.id==job.id))=='general'
        saved.payload={k:v for k,v in saved.payload.items() if k!='review_latest'}
        saved.payload={**saved.payload,'integration_context':{'probe':True}};db.flush()
        assert db.scalar(select(lane_expression()).where(Job.id==job.id))=='general'


def test_old_latest_priority_cannot_fetch_after_calendar_moves(client,monkeypatch):
    from app.market import review_jobs as jobs
    from test_review_collection import setup_source
    _,_,_,job=setup_source('new_highs',target=DAY)
    job.payload={**job.payload,'review_latest':True}
    monkeypatch.setattr(jobs,'available_trade_days',lambda *a,**k:[DAY+timedelta(days=3)])
    with Session(engine()) as db,pytest.raises(SourceFailure,match='review_archive_day_mismatch'):
        jobs.validate_context(db,job,datetime(2026,11,9,tzinfo=UTC))
