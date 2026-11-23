"""Run only in the project's already-verified disposable PostgreSQL suite."""
from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import collect, eastmoney_trading as transport
from app.market.intraday_funds import intraday_funds
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response, SourceFailure, SourceRedirect
from app.market.resilience_models import DataHealth
from app.models import Job, Provider
from test_data_resilience import CLOCK, scenario
from test_funds_live_isolation import configured
from test_intraday_funds import save
from test_eastmoney_funds_partial import body


@pytest.mark.parametrize('old_stock_still_fresh',[False,True])
def test_partial_transport_commits_only_successes_and_retains_honest_health(scenario,monkeypatch,old_stock_still_fresh):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls,tz=None):
            return (CLOCK+timedelta(seconds=2)).astimezone(tz) if tz else (CLOCK+timedelta(seconds=2)).replace(tzinfo=None)
    monkeypatch.setattr(transport,'datetime',FixedDateTime)
    monkeypatch.setattr(collect,'datetime',FixedDateTime)
    permits=[]
    monkeypatch.setattr(collect,'reserve_eastmoney_request',lambda job:permits.append(True))
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        provider,config=configured(db,scenario)
        if old_stock_still_fresh:
            save(db,[('600000','5')],CLOCK-timedelta(seconds=30),category='stock')
        job=Job(owner_id=scenario,kind='source.collect',state='running',attempts=1,
            payload={'source_id':str(provider.id)},created_at=CLOCK,available_at=CLOCK,
            lease_token=uuid.uuid4(),
            lease_until=datetime.now(UTC)+timedelta(hours=1))
        db.add(job);db.flush();source=provider.id
    calls=[]
    def fetch(url):
        index=len(calls);calls.append(url)
        if index==0:raise SourceFailure('transport_error')
        return Response(body(['stock','industry','concept'][index],at=CLOCK),
            CLOCK+timedelta(seconds=1),1,200)
    monkeypatch.setattr(transport,'fetch_https',fetch)
    collect.collect_job(job)
    assert len(calls)==len(permits)==3
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert (run.status,run.expected,run.accepted,run.rejected,run.request_count)==('partial',3,2,1,3)
        assert db.get(Job,job.id).state=='succeeded'
        health=db.scalar(select(DataHealth).where(DataHealth.primary_source_id==source))
        assert health.state=='recovering' and health.reason=='coverage_partial' and health.missing_count==1
        rows=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source,
            EvidenceVersion.kind=='reference')))
        assert len(rows)==(3 if old_stock_still_fresh else 2)
        assert all(row.payload['dataset']!='market.eastmoney.stock_funds'
            or old_stock_still_fresh and row.payload['rows'][0]['net_inflow_cny']=='5' for row in rows)
        view=intraday_funds(db,CLOCK.date(),CLOCK+timedelta(seconds=3),session={'phase':'trading'})
        assert len(view.sectors)==2 and len(view.points)==2 and view.state=='stale'
        assert all(row.source_time==CLOCK for row in view.sectors)
        assert all(point.time==CLOCK for point in view.points)
        assert view.latest_source_time==CLOCK and view.received_at==CLOCK+timedelta(seconds=2)
        if old_stock_still_fresh:assert view.stocks[0].source_time==CLOCK-timedelta(seconds=30)
        assert '不代表本轮完整快照' in view.scope


@pytest.mark.parametrize('clock_kind',['fresh','old','unknown','catalog_failure','catalog_deferral'])
def test_allocated_tables_recover_the_original_source_without_inventing_freshness(scenario,monkeypatch,clock_kind):
    import json
    class FixedDateTime(datetime):
        @classmethod
        def now(cls,tz=None):
            at=CLOCK+timedelta(seconds=2)
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)
    monkeypatch.setattr(transport,'datetime',FixedDateTime)
    monkeypatch.setattr(collect,'datetime',FixedDateTime)
    permits=[];calls=[]
    def reserve(job):
        if clock_kind=='catalog_deferral' and len(permits)==6:
            raise SourceFailure('rate_limited',retry_after=75)
        permits.append(True)
    monkeypatch.setattr(collect,'reserve_eastmoney_request',reserve)
    metadata_case=clock_kind.startswith('catalog_')
    if metadata_case:
        from app.market import industry_catalog
        monkeypatch.setattr(industry_catalog,'catalog_due',lambda *a:True)
        monkeypatch.setattr(industry_catalog,'fetch_catalog',lambda:(_ for _ in ()).throw(SourceFailure('timeout')))
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        provider,config=configured(db,scenario)
        config.paused_reason='upstream_redirect'
        job=Job(owner_id=scenario,kind='source.collect',state='running',attempts=1,
            payload={'source_id':str(provider.id),'schema_probe':True,'scheduled_eastmoney':metadata_case},created_at=CLOCK,available_at=CLOCK,
            lease_token=uuid.uuid4(),lease_until=datetime.now(UTC)+timedelta(hours=1))
        db.add(job);db.flush();source=provider.id
    def original(url):
        calls.append(url)
        raise SourceRedirect(302,url.replace('push2.eastmoney.com','push2delay.eastmoney.com'))
    def allocated(url,*,original_url):
        index=len(calls)//2;calls.append(url)
        stamp=CLOCK-timedelta(seconds=600) if clock_kind=='old' else CLOCK
        raw=json.loads(body(['stock','industry','concept'][index],at=stamp))
        if clock_kind=='unknown':raw['data']['diff'][0]['f124']=None
        return Response(json.dumps(raw).encode(),CLOCK+timedelta(seconds=1),1,200)
    monkeypatch.setattr(transport,'fetch_https',original)
    monkeypatch.setattr(transport,'fetch_eastmoney_allocation',allocated)
    collect.collect_job(job)
    requests=7 if clock_kind=='catalog_failure' else 6
    assert len(calls)==6 and len(permits)==requests
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert (run.status,run.request_count,run.accepted)==('partial' if metadata_case else 'complete',requests,3)
        assert db.get(SourceSettings,source).paused_reason is None
        assert db.get(SourceSettings,source).interval_seconds==150
        rows=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source)))
        assert len(rows)==3 and all('同接口分配节点' in row.payload['scope'] for row in rows)
        view=intraday_funds(db,CLOCK.date(),CLOCK+timedelta(seconds=3),session={'phase':'trading'})
        assert view.state=={'old':'stale','unknown':'missing'}.get(clock_kind,'available')
        expected=CLOCK-timedelta(seconds=600) if clock_kind=='old' else None if clock_kind=='unknown' else CLOCK
        assert view.latest_source_time==expected
        assert all(row.source_time==expected for row in [*view.stocks,*view.sectors])


def test_current_display_revocation_between_allocation_hops_prevents_the_second_request(scenario,monkeypatch):
    permits=[];calls=[]
    original_reserve=collect.reserve_eastmoney_request
    def reserve(job):
        original_reserve(job)
        permits.append(True)
    monkeypatch.setattr(collect,'reserve_eastmoney_request',reserve)
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        provider,config=configured(db,scenario)
        job=Job(owner_id=scenario,kind='source.collect',state='running',attempts=1,
            payload={'source_id':str(provider.id)},created_at=CLOCK,available_at=CLOCK,
            lease_token=uuid.uuid4(),lease_until=datetime.now(UTC)+timedelta(hours=1))
        db.add(job);db.flush();source=provider.id
    def original(url):
        calls.append(url)
        with Session(engine()) as db,db.begin():
            provider=db.get(Provider,source)
            provider.rights={**provider.rights,'display':'revoked'}
        raise SourceRedirect(302,url.replace('push2.eastmoney.com','push2delay.eastmoney.com'))
    monkeypatch.setattr(transport,'fetch_https',original)
    monkeypatch.setattr(transport,'fetch_eastmoney_allocation',lambda *a,**k:pytest.fail('revoked source cannot send HTTP'))
    collect.collect_job(job)
    assert len(calls)==len(permits)==1
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert run.reason=='permission_missing' and run.request_count==1 and run.accepted is None
        assert not list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source)))
