"""Existing minute path only: a later normal archive retry can use one hop."""
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.market import fund_history as h
from app.market.models import DataFetchRun
from app.market.protocol import Response, SourceRedirect, SourceFailure
from app.models import Job
from test_fund_history import CLOSE,DAY,create_plan,body
from test_intraday_funds import setup


@pytest.mark.parametrize('revoke_before_hop',[False,True])
def test_archive_retains_first_rejection_then_only_a_later_normal_retry_can_follow_once(client,monkeypatch,revoke_before_hop):
    setup(client);clock=[CLOSE]
    monkeypatch.setattr(h,'settings',lambda:SimpleNamespace(data_collection_enabled=True))
    monkeypatch.setattr(h,'now',lambda db:clock[0])
    monkeypatch.setattr('app.market.review_store.available_trade_days',lambda *a,**k:[DAY])
    class Fixed(datetime):
        @classmethod
        def now(cls,tz=None):return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(h,'datetime',Fixed)
    permits=[]
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request',lambda job,minimum_interval:permits.append(minimum_interval))
    with Session(engine()) as db,db.begin():create_plan(db,count=1)
    calls=[];allocated=[]
    def redirected(url):
        calls.append(url);raise SourceRedirect(302,url.replace('https://push2.','https://push2delay.',1))
    def continuation(url,**kwargs):
        allocated.append((url,kwargs))
        return Response(json.dumps(body()).encode(),clock[0],1,200)
    monkeypatch.setattr(h,'fetch_https',redirected)
    monkeypatch.setattr(h,'fetch_eastmoney_minute_allocation',continuation)
    def take():
        with Session(engine(),expire_on_commit=False) as db,db.begin():
            job=db.scalar(select(Job).where(Job.kind=='funds.archive',Job.state=='queued'))
            assert job is not None
            job.state='running';job.attempts=1;job.lease_token=uuid.uuid4()
            job.lease_until=datetime.now(UTC)+timedelta(hours=1)
            return job
    h.schedule_fund_archives();first=take();h.process_job(first)
    with Session(engine()) as db:
        receipt=db.get(Job,first.id)
        assert receipt.state=='failed' and receipt.error_code=='upstream_redirect'
        diagnostic=receipt.result_ref['last_transport_rejection']
        assert diagnostic['same_query'] and diagnostic['secure_target'] and not allocated
        assert db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==first.id)).request_count==1
    clock[0]+=timedelta(minutes=16)
    h.schedule_fund_archives();second=take()
    assert second.result_ref['last_transport_rejection']==diagnostic
    checked=[];validate=h._validate
    def fenced(*args,**kwargs):
        checked.append(True)
        if revoke_before_hop and len(checked)==3:raise SourceFailure('permission_missing')
        return validate(*args,**kwargs)
    monkeypatch.setattr(h,'_validate',fenced)
    h.process_job(second)
    with Session(engine()) as db:
        receipt=db.get(Job,second.id)
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==second.id))
        assert receipt.state==('failed' if revoke_before_hop else 'succeeded')
        assert run.request_count==(1 if revoke_before_hop else 2)
        assert run.accepted==(0 if revoke_before_hop else 1)
    assert len(allocated)==(0 if revoke_before_hop else 1)
    assert len(calls)==2 and permits==[10,10,10]
