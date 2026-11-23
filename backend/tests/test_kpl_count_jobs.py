"""Disposable database: all-board rotation, restart fencing and independent counts."""
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import ReferencePayload
from app.market import kpl_count_jobs as counts
from app.market.collect import store_reference_batch
from app.market.kpl_limit_counts import DATASET, batch, latest
from app.market.kpl_atlas_source import DATASET as RANKING
from app.market.kpl_strength_api import project, read
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response, SourceFailure
from app.models import Job, Provider
from test_direction_archive import setup, DAY, CLOCK


def prepare(client, monkeypatch, size=24, *, clock_at=None):
    ident, clock = setup(client, monkeypatch, 'direction_kpl_strength')
    if clock_at is not None: clock[0]=clock_at
    monkeypatch.setattr(counts, 'now', lambda db: clock[0])
    monkeypatch.setattr('app.market.kpl_strength_api.now', lambda db: clock[0])
    monkeypatch.setattr('app.market.session.market_session', lambda *a: SimpleNamespace(is_trading_day=True))
    rows=[dict(code=f'{801000+i}',name=f'合成题材{i}',strength=str(100-i),source_day=str(DAY),
        coverage='complete_pages',total_count=str(size),archive_kind='intraday') for i in range(size)]
    payload=ReferencePayload(dataset=RANKING,title='合成强度',observed_day=DAY,
        attribution='Synthetic test only',scope='Synthetic 24-board universe; not market data.',
        source_url='https://apphwhq.longhuvip.com/w1/api/index.php',
        columns=[dict(key=k,label=k) for k in rows[0]],rows=rows)
    from app.market.reference import ReferenceBatch
    with Session(engine()) as db, db.begin():
        store_reference_batch(db,db.get(Provider,ident),db.get(SourceSettings,ident),
            ReferenceBatch(records=[(RANKING,payload,None)],expected=1,requests=0),clock[0])
    reserved=[]
    monkeypatch.setattr('app.market.kpl_atlas_budget.reserve', lambda job, **kw: reserved.append(job.id) or True)
    def fetch(family, *, plate_code, plate_summary):
        assert family=='direction_kpl_strength' and plate_summary
        clock[0]+=timedelta(seconds=3)
        return response(clock[0],int(plate_code)%3)
    monkeypatch.setattr('app.market.direction_sources.fetch',fetch)
    return ident, clock, reserved


def response(stamp=CLOCK, count=0):
    body=json.dumps(dict(errcode='0',Date=str(DAY),List=[1,100,1000,20,1,count,10,5])).encode()
    return Response(body,stamp,1,200)


def take_job():
    with Session(engine(),expire_on_commit=False) as db, db.begin():
        job=db.scalar(select(Job).where(Job.payload['kpl_counts'].astext=='true',Job.state=='queued').order_by(Job.created_at))
        assert job is not None
        job.state='running';job.attempts=1;job.lease_token=uuid.uuid4()
        job.lease_until=datetime.now(UTC)+timedelta(minutes=5)
        return job


def test_full_universe_moves_past_tenth_board_and_stops_after_final_round(client,monkeypatch):
    ident, clock, reserved=prepare(client,monkeypatch)
    for _ in range(3):
        counts.schedule();counts.schedule()
        job=take_job()
        assert len(job.payload['codes'])==8
        counts.process_job(job)
    with Session(engine()) as db:
        data=read(db,DAY)
        assert data['limit_count_coverage']['available']==24
        assert len(latest(db,ident,DAY,clock[0]))==24
        assert data['rows'][20]['limit_up_count'] is not None
        assert data['rows'][0]['strength']=='100'
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==24
        assert {j.state for j in db.scalars(select(Job))}=={'succeeded'}
    counts.schedule()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job))==3
    assert len(reserved)==24


def test_failed_board_preserves_others_and_retries_after_cooldown(client,monkeypatch):
    ident,clock,_=prepare(client,monkeypatch,9)
    def fetch(family, **kw):
        clock[0]+=timedelta(seconds=3)
        if kw['plate_code']=='801000': raise SourceFailure('timeout')
        return response(clock[0],0)
    monkeypatch.setattr('app.market.direction_sources.fetch',fetch)
    counts.schedule();counts.process_job(take_job())
    with Session(engine()) as db:
        value=read(db,DAY)
        assert value['rows'][0]['limit_up_count'] is None
        assert [row['limit_up_count'] for row in value['rows'][1:8]]==[0]*7
    counts.schedule();next_job=take_job()
    assert [row['code'] for row in next_job.payload['codes']]==['801008']
    counts.process_job(next_job)
    clock[0]+=timedelta(minutes=6)
    counts.schedule();retry=take_job()
    assert retry.payload['codes'][0]['code']=='801000'


@pytest.mark.parametrize('denial',['permission','configuration','next_day','calendar','disabled'])
def test_fenced_task_never_contacts_supplier_after_context_changes(client,monkeypatch,denial):
    ident,clock,_=prepare(client,monkeypatch,2)
    counts.schedule();job=take_job()
    if denial in {'permission','configuration'}:
        with Session(engine()) as db,db.begin():
            if denial=='permission':db.get(Provider,ident).enabled=False
            else:db.get(SourceSettings,ident).interval_seconds+=60
    if denial=='next_day':clock[0]+=timedelta(days=1)
    if denial=='calendar':monkeypatch.setattr('app.market.session.market_session',lambda *a:SimpleNamespace(is_trading_day=False))
    if denial=='disabled':monkeypatch.setattr(counts.settings(),'data_collection_enabled',False)
    monkeypatch.setattr('app.market.direction_sources.fetch',lambda *a,**k:pytest.fail('fenced context contacted source'))
    with pytest.raises(SourceFailure):counts.process_job(job)


def test_latest_per_code_and_cutoff_not_last_global_events(client,monkeypatch):
    ident,clock,_=prepare(client,monkeypatch,2)
    before=clock[0]
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,ident);config=db.get(SourceSettings,ident)
        store_reference_batch(db,provider,config,batch(response(before,2),DAY,'801000','合成甲'),before)
        for i in range(5):
            stamp=before+timedelta(seconds=i+1)
            store_reference_batch(db,provider,config,batch(response(stamp,i),DAY,'801001','合成乙'),stamp)
    clock[0]+=timedelta(seconds=6)
    with Session(engine()) as db:
        rows=read(db,DAY)['rows']
        assert [r['limit_up_count'] for r in rows]==[2,4]
        assert len(latest(db,ident,DAY,before))==1
    with Session(engine()) as db,db.begin():db.get(Provider,ident).enabled=False
    with Session(engine()) as db:assert read(db,DAY)['rows']==[]


def test_shared_budget_wait_is_resumable_without_inventing_requests(client,monkeypatch):
    _,clock,_=prepare(client,monkeypatch,2)
    counts.schedule();job=take_job()
    def busy(*a,**k):raise SourceFailure('local_request_wait',retry_after=3)
    monkeypatch.setattr('app.market.kpl_atlas_budget.reserve',busy)
    monkeypatch.setattr('app.market.direction_sources.fetch',lambda *a,**k:pytest.fail('budget wait must not fetch'))
    counts.process_job(job)
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.state=='queued' and saved.attempts==0 and saved.lease_token is None
        assert saved.available_at==clock[0]+timedelta(seconds=3)
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==0


def test_queued_previous_day_does_not_block_new_day_counts(client,monkeypatch):
    prepare(client,monkeypatch,2);counts.schedule()
    with Session(engine()) as db,db.begin():
        old=db.scalar(select(Job));old_id=old.id
        old.payload={**old.payload,'day':str(DAY-timedelta(days=1))}
    counts.schedule()
    with Session(engine()) as db:
        assert db.get(Job,old_id).state=='cancelled'
        assert db.scalar(select(func.count()).select_from(Job).where(Job.state=='queued'))==1


def test_refreshing_existing_counts_does_not_starve_missing_boards(client,monkeypatch):
    ident,_,_=prepare(client,monkeypatch)
    stamp=CLOCK-timedelta(hours=1)
    with Session(engine()) as db,db.begin():
        store_reference_batch(db,db.get(Provider,ident),db.get(SourceSettings,ident),
            batch(response(stamp,2),DAY,'801000','合成题材0'),stamp)
    counts.schedule();job=take_job()
    codes=[row['code'] for row in job.payload['codes']]
    assert codes[0]=='801000' and len(codes)==8
    assert codes[1:]==[f'{801000+i}' for i in range(1,8)]


def test_new_ranking_cannot_erase_old_counts_and_new_zero_wins_by_receipt():
    from test_kpl_strength_series_unit import batch as ranking, DAY as day, CLOCK as clock
    older=ranking(clock-timedelta(minutes=1),('5','2'))
    older.payload['rows'][1].update(limit_up_count='3',limit_up_received_at=older.received_at.isoformat())
    newest=ranking(clock,('7','4'))
    assert project([newest,older],day,clock)['rows'][1]['limit_up_count']==3
    counter=SimpleNamespace(received_at=clock,payload=dict(observed_day=str(day),rows=[dict(
        code='800001',source_day=str(day),limit_up_count='0',limit_up_received_at=clock.isoformat())]))
    result=project([newest,older],day,clock,[counter])
    assert result['rows'][1]['limit_up_count']==0 and result['rows'][1]['strength']=='4'
    assert len(result['history'])==2
    assert project([newest,older],day,clock-timedelta(seconds=1),[counter])['rows'][1]['limit_up_count']==3
