"""New trading-day ladder reads use real archived metadata on disposable PG."""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion, SourceVersion
from app.market import ladder_observation as ladder, review_read
from app.market.collect import store_calendar_batch
from app.market.models import DataFetchRun, SourceSettings
from app.market.review_protocol import ReviewPage
from app.market.review_store import persist_pages
from app.market.service import source_id
from app.models import Job, Provider, User
from test_data_sources import configure, login
from test_review_collection import new_job
from test_tushare import source

TODAY=date(2026,11,11)
PREVIOUS=date(2026,11,10)
PRIOR=date(2026,11,9)
OBSERVED=datetime.fromisoformat('2026-11-08T16:00:00+08:00')
RECEIVED=datetime.fromisoformat('2026-11-10T16:00:00+08:00')
CODE='600001.SH'


@pytest.fixture
def saved_ladder(client,monkeypatch):
    clock=[datetime.fromisoformat('2026-11-11T09:00:00+08:00')]
    class ClockType(type):
        def __instancecheck__(cls,obj):return isinstance(obj,datetime)
    class FrozenDatetime(datetime,metaclass=ClockType):
        @classmethod
        def now(cls,tz=None):return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(ladder,'now',lambda db:clock[0])
    monkeypatch.setattr(review_read,'datetime',FrozenDatetime)
    monkeypatch.setattr('app.market.tushare.request',lambda *a,**k:pytest.fail('GET cannot collect'))
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *a,**k:pytest.fail('GET cannot collect'))
    families=('tushare_calendar_sse','tushare_calendar_szse','tushare_daily','tushare_limit_list_d',
        'tushare_limit_list_ths','tushare_daily_basic','tushare_market_limits','tushare_market_rt')
    headers=login(client)
    for family in families:configure(client,headers,source(family),key=family)
    with Session(engine()) as db,db.begin():
        for version in list(db.scalars(select(SourceVersion))):
            db.add(SourceVersion(source_id=version.source_id,recorded_at=OBSERVED,
                descriptor=version.descriptor,content_hash=version.content_hash))
        for family,exchange in (('tushare_calendar_sse','SSE'),('tushare_calendar_szse','SZSE')):
            provider=db.get(Provider,source_id(family));config=db.get(SourceSettings,provider.id)
            store_calendar_batch(db,provider,config,[(PRIOR,True),(PREVIOUS,True),(TODAY,True)],
                OBSERVED,exchanges=(exchange,))
        owner=db.scalar(select(User).where(User.username=='owner'))
        for dataset,family,rows in (
            ('pools','tushare_limit_list_d',[dict(ts_code=CODE,name='合成昨二板',trade_date='20261110',
                limit='U',limit_times='2',first_time='093001',last_time='145001')]),
            ('daily','tushare_daily',[dict(ts_code=CODE,trade_date='20261110',open='10',high='11',low='10',
                close='11',pre_close='10',pct_chg='10',amount='100')]),
            ('limits','tushare_market_limits',[dict(ts_code=CODE,trade_date='20261110',up_limit='11',down_limit='9')]),
        ):
            provider=db.get(Provider,source_id(family));config=db.get(SourceSettings,provider.id)
            task=new_job(db,owner,provider,config,dataset,target=PREVIOUS)
            page=ReviewPage(fields=list(rows[0]),rows=rows,source_url='https://tushare.pro/document/2',
                raw_hash='a'*64,scope='Synthetic isolated rollover evidence')
            persist_pages(db,provider,config,task,[(page,RECEIVED)])
            task.state='succeeded';task.completed_at=RECEIVED
    return clock


def counts():
    with Session(engine()) as db:
        return tuple(db.scalar(select(func.count()).select_from(model)) for model in (Job,DataFetchRun,EvidenceVersion))


@pytest.mark.parametrize('stamp',['09:00:00','09:15:00','09:30:00'])
def test_new_day_empty_slot_returns_metadata_without_reading_unclosed_pool_or_inventing_elimination(client,monkeypatch,saved_ladder,stamp):
    clock=saved_ladder;clock[0]=datetime.fromisoformat(str(TODAY)+'T'+stamp+'+08:00')
    calls=[];original=ladder.ArchiveReader.get
    def read(reader,dataset,day,*a,**k):
        calls.append((dataset,day));return original(reader,dataset,day,*a,**k)
    monkeypatch.setattr(ladder.ArchiveReader,'get',read)
    before=counts()
    response=client.get('/api/v1/ladder-observation',params={'lookback':2})
    assert response.status_code==200,response.text
    data=response.json()['data']
    assert data['trade_day']==str(TODAY) and data['quote_day']==str(TODAY)
    current=data['days'][0];previous=data['days'][1]
    assert current['day']==str(TODAY) and current['previous_day']==str(PREVIOUS)
    assert current['state']=='missing' and not current['rows'] and not current['eliminated']
    assert {(item['dataset'],item['trade_day'],item['scope']) for item in current['coverage']}=={
        ('pools',str(TODAY),'all'),('daily',str(TODAY),'all')}
    assert all(item['state']=='missing' and item['row_count'] is None and item['received_at'] is None
        and not item['evidence_refs'] for item in current['coverage'])
    assert previous['state']=='complete' and previous['rows'][0]['height']==2
    assert previous['rows'][0]['day']==str(PREVIOUS) and not previous['eliminated']
    assert not any(day==TODAY and dataset in {'pools','pool_details','group_basics','daily'} for dataset,day in calls)
    assert not data['can_export'] and counts()==before


def test_explicit_yesterday_keeps_original_day_and_never_uses_it_as_today(client,saved_ladder):
    before=counts()
    response=client.get('/api/v1/ladder-observation',params={'trade_day':str(PREVIOUS),'lookback':1})
    assert response.status_code==200,response.text
    data=response.json()['data'];day=data['days'][0]
    assert data['trade_day']==str(PREVIOUS) and day['day']==str(PREVIOUS)
    assert day['state']=='complete' and day['rows'][0]['height']==2 and day['rows'][0]['price']==11
    assert not data['latest_quotes'] and counts()==before


@pytest.mark.parametrize('revoked',[False,True])
def test_same_current_read_recovers_when_real_saved_quote_arrives_without_borrowing_yesterdays_board(client,saved_ladder,revoked):
    from test_terminal_workbench import save
    from test_tushare_market import quote, limits
    clock=saved_ladder;clock[0]=clock[0].replace(hour=9,minute=30)
    before=client.get('/api/v1/ladder-observation',params={'lookback':2})
    assert before.status_code==200 and not before.json()['data']['days'][0]['rows']
    native=clock[0]+timedelta(minutes=1);receipt=native+timedelta(seconds=2)
    with Session(engine()) as db,db.begin():
        restriction={**limits(CODE,upper='12.10',lower='9.90'),'trade_date':TODAY.strftime('%Y%m%d'),'pre_close':'11'}
        save(db,'tushare_market_limits',[restriction],receipt)
        save(db,'tushare_market_rt',[quote(CODE,price='12.10',previous='11',at=native)],receipt)
        if revoked:db.get(Provider,source_id('tushare_market_rt')).enabled=False
    clock[0]=receipt+timedelta(seconds=1)
    saved=counts()
    response=client.get('/api/v1/ladder-observation',params={'lookback':2})
    assert response.status_code==200,response.text
    day=response.json()['data']['days'][0]
    if revoked:
        assert day['state']=='missing' and not day['rows'] and not day['eliminated']
    else:
        assert day['state']=='live' and len(day['rows'])==1 and not day['eliminated']
        row=day['rows'][0]
        assert row['day']==str(TODAY) and row['height']==3 and row['previous_height']==2
        assert row['status']=='touched' and row['price_basis']=='intraday' and row['first_time'] is None
        assert datetime.fromisoformat(row['source_time'])==native
    assert counts()==saved


@pytest.mark.parametrize('cutoff,expected',[('15:59:00','missing'),('16:01:00','complete')])
def test_historical_cutoff_does_not_borrow_a_later_archive_and_missing_daily_metadata_is_valid(client,saved_ladder,cutoff,expected):
    before=counts()
    response=client.get('/api/v1/ladder-observation',params={'trade_day':str(PREVIOUS),'lookback':1,
        'as_of_time':str(PREVIOUS)+'T'+cutoff+'+08:00'})
    assert response.status_code==200,response.text
    day=response.json()['data']['days'][0]
    assert day['state']==expected
    assert all(item['trade_day']==str(PREVIOUS) and item['scope']=='all' for item in day['coverage'])
    if expected=='missing':
        assert not day['rows'] and not day['eliminated']
        assert all(item['row_count'] is None and not item['evidence_refs'] for item in day['coverage'])
    else:
        assert day['rows'][0]['height']==2
        assert all(datetime.fromisoformat(item['received_at'])<=datetime.fromisoformat(str(PREVIOUS)+'T'+cutoff+'+08:00')
            for item in day['coverage'] if item['received_at'])
    assert counts()==before


@pytest.mark.parametrize('cutoff',[None,'2026-11-10T16:01:00+08:00'])
def test_revoked_pool_permission_never_leaks_historical_rows_on_retry(client,saved_ladder,cutoff):
    with Session(engine()) as db,db.begin():
        db.get(Provider,source_id('tushare_limit_list_d')).rights={'display':'denied','cache':'allowed'}
    query={'trade_day':str(PREVIOUS),'lookback':1}
    if cutoff:query['as_of_time']=cutoff
    response=client.get('/api/v1/ladder-observation',params=query)
    assert response.status_code==200,response.text
    data=response.json()['data'];day=data['days'][0]
    assert day['state']=='missing' and not day['rows'] and not day['eliminated'] and not data['can_export']
    assert day['coverage'][0]['state']=='permission_denied' and not day['coverage'][0]['evidence_refs']


def test_revoked_calendar_does_not_guess_current_day_or_return_yesterdays_pool(client,saved_ladder):
    with Session(engine()) as db,db.begin():db.get(Provider,source_id('tushare_calendar_sse')).enabled=False
    response=client.get('/api/v1/ladder-observation',params={'lookback':2})
    assert response.status_code==200,response.text
    data=response.json()['data']
    assert data['trade_day'] is None and not data['days'] and data['state']=='missing' and not data['can_export']
