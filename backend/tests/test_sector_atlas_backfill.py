"""Scheduler contracts in disposable PostgreSQL; no supplier requests."""
from datetime import datetime,timedelta
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from app.config import settings
from app.db import engine
from app.models import Job
from app.market import sector_atlas_backfill as backfill
from test_direction_data import setup,DAY


def test_fifty_day_backfill_resumes_one_job_without_duplicates(client,monkeypatch):
    setup(client,monkeypatch)
    clock=[datetime.fromisoformat('2026-11-04T01:00:00+08:00')]
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr(backfill,'now',lambda db:clock[0])
    requested=[]
    def calendar(db,*,limit,as_of_time):
        requested.append(limit)
        return [DAY-timedelta(days=n) for n in reversed(range(50))]
    monkeypatch.setattr(backfill,'available_trade_days',calendar)
    backfill.schedule_atlas_backfill()
    clock[0]+=timedelta(seconds=61)
    backfill.schedule_atlas_backfill()
    with Session(engine()) as db:
        jobs=list(db.scalars(select(Job)))
        assert len(jobs)==1
        assert jobs[0].kind=='direction.archive'
        assert jobs[0].payload['day']==DAY.isoformat()
        assert jobs[0].payload['sector_atlas_backfill'] is True
        assert jobs[0].payload['configuration_sha256']
    assert requested==[50,50]


def test_history_scheduler_yields_during_live_market(client,monkeypatch):
    setup(client,monkeypatch)
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr(backfill,'now',lambda db:datetime.fromisoformat('2026-11-04T10:00:00+08:00'))
    monkeypatch.setattr(backfill,'available_trade_days',lambda *a,**k:(_ for _ in ()).throw(AssertionError('must yield before history work')))
    backfill.schedule_atlas_backfill()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job))==0
