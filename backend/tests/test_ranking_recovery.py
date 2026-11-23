"""Recovery rechecks the pinned URL; unverified redirects stay rejected."""
import pytest
from datetime import timedelta
from sqlalchemy.orm import Session
from app.db import engine
from app.market.auction_schedule import next_collection, schema_recheck
from app.market.models import SourceSettings
from app.market.service import schedule_sources, source_id
from app.worker import claim
from test_direction_recovery import setup, at

@pytest.mark.parametrize('family',['eastmoney_funds','eastmoney_boards'])
@pytest.mark.parametrize('reason',['schema_changed','upstream_redirect'])
def test_pinned_ranking_probe_is_paced_and_scheduled(client,monkeypatch,family,reason):
    current=setup(client,monkeypatch,family)
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(family)).paused_reason=reason
    assert schema_recheck(family,reason)
    assert next_collection(family,current[0],150,schema_probe=True)==current[0]+timedelta(seconds=60)
    # User's continuous-session policy also keeps the original 60s probe pace at lunch.
    assert next_collection(family,at('2026-10-26T11:29:30'),150,schema_probe=True)==at('2026-10-26T11:30:30')
    assert next_collection(family,at('2026-10-26T12:00:00'),150,schema_probe=True)==at('2026-10-26T12:01:00')
    schedule_sources()
    job=claim()
    assert job and job.payload['schema_probe'] is True
    assert job.payload['source_id']==str(source_id(family))
    schedule_sources()
    assert claim() is None

@pytest.mark.parametrize('reason',['permission_missing','decoder_changed','endpoint_not_allowed','unsafe_upstream_response'])
def test_ranking_probe_does_not_resume_unsafe_or_revoked_sources(reason):
    assert not schema_recheck('eastmoney_funds',reason)


def test_funds_probe_validates_before_clearing_pause_and_never_follows_redirect(client,monkeypatch):
    from app.market import collect, eastmoney_trading as transport
    from app.market.protocol import Response, SourceFailure
    from test_eastmoney_funds_partial import body
    current=setup(client,monkeypatch,'eastmoney_funds')
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id('eastmoney_funds')).paused_reason='upstream_redirect'
    schedule_sources();job=claim();assert job
    calls=[]
    def fetch(url):
        calls.append(url)
        return Response(body(['stock','industry','concept'][len(calls)-1],at=current[0]),current[0],1,200)
    monkeypatch.setattr(transport,'fetch_https',fetch)
    monkeypatch.setattr(collect,'reserve_eastmoney_request',lambda job:True)
    collect.collect_job(job)
    assert len(calls)==3 and all(url.startswith(transport.ENDPOINT+'?') for url in calls)
    with Session(engine()) as db:
        assert db.get(SourceSettings,source_id('eastmoney_funds')).paused_reason is None


@pytest.mark.parametrize('reason,allowed',[('upstream_redirect',True),('schema_changed',True),('permission_missing',False)])
def test_real_budget_permit_allows_only_authorized_probe(client,monkeypatch,reason,allowed):
    from app.market.budget import reserve_eastmoney_request
    from app.market.protocol import SourceFailure
    from app.models import Job
    from datetime import datetime,UTC
    import uuid
    current=setup(client,monkeypatch,'eastmoney_funds')
    monkeypatch.setattr('app.market.budget.now',lambda db:current[0])
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        config=db.get(SourceSettings,source_id('eastmoney_funds'));config.paused_reason=reason
        job=Job(owner_id=config.owner_id,kind='source.collect',state='running',attempts=1,
            payload={'source_id':str(config.source_id),'schema_probe':True},
            lease_token=uuid.uuid4(),lease_until=current[0]+timedelta(hours=1),available_at=current[0])
        db.add(job);db.flush()
    if allowed:reserve_eastmoney_request(job)
    else:
        with pytest.raises(SourceFailure,match='source_paused'):reserve_eastmoney_request(job)


@pytest.mark.parametrize('table_reason',['upstream_redirect','schema_changed','timeout','transport_error','upstream_unavailable'])
@pytest.mark.parametrize('catalog_reason',[None,'transport_error','rate_limited'])
def test_partial_probe_retains_successes_without_clearing_pause(client,monkeypatch,table_reason,catalog_reason):
    from app.market import collect, eastmoney_trading as transport
    from app.market.protocol import Response, SourceFailure
    from app.market.models import DataFetchRun
    from app.evidence.models import EvidenceVersion
    from sqlalchemy import select
    from test_eastmoney_funds_partial import body
    current=setup(client,monkeypatch,'eastmoney_funds')
    monkeypatch.setattr(transport,'datetime',collect.datetime)
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id('eastmoney_funds')).paused_reason='upstream_redirect'
    schedule_sources();job=claim();assert job
    calls=[]
    def fetch(url):
        calls.append(url)
        if len(calls)==1:raise SourceFailure(table_reason)
        return Response(body(['stock','industry','concept'][len(calls)-1],at=current[0]),current[0],1,200)
    monkeypatch.setattr(transport,'fetch_https',fetch)
    monkeypatch.setattr(collect,'reserve_eastmoney_request',lambda job:True)
    if catalog_reason:
        def catalog():raise SourceFailure(catalog_reason,retry_after=90 if catalog_reason=='rate_limited' else None)
        monkeypatch.setattr('app.market.industry_catalog.fetch_catalog',catalog)
        original=transport.fetch_rankings
        monkeypatch.setattr(collect,'fetch_rankings',lambda family,reserve,*a,**kw:original(family,reserve,include_catalog=True))
    collect.collect_job(job)
    with Session(engine()) as db:
        config=db.get(SourceSettings,source_id('eastmoney_funds'))
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        expected_reason='rate_limited' if catalog_reason=='rate_limited' else table_reason
        assert (run.status,run.accepted,run.rejected,run.reason)==('partial',2,1+int(catalog_reason is not None),expected_reason)
        assert config.paused_reason=='upstream_redirect'
        assert config.next_run_at==current[0]+timedelta(seconds=90 if catalog_reason=='rate_limited' else 300)
        assert len(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==config.source_id)).all())==2


def test_repeated_redirects_back_off_then_recover_without_manual_reset(client,monkeypatch):
    from app.market import collect, eastmoney_trading as transport
    from app.market.protocol import Response, SourceFailure
    from test_eastmoney_funds_partial import body
    from sqlalchemy import select
    from app.market.models import DataFetchRun
    current=setup(client,monkeypatch,'eastmoney_funds')
    monkeypatch.setattr(transport,'datetime',collect.datetime)
    monkeypatch.setattr(collect,'reserve_eastmoney_request',lambda job:True)
    monkeypatch.setattr(transport,'fetch_https',lambda url:(_ for _ in ()).throw(SourceFailure('upstream_redirect')))
    for seconds in [300]*5:
        schedule_sources();job=claim();assert job
        collect.collect_job(job)
        with Session(engine()) as db:
            config=db.get(SourceSettings,source_id('eastmoney_funds'))
            assert config.next_run_at==current[0]+timedelta(seconds=seconds)
        current[0]+=timedelta(seconds=seconds)
    calls=[]
    def recovered(url):
        calls.append(url)
        return Response(body(['stock','industry','concept'][len(calls)-1],at=current[0]),current[0],1,200)
    monkeypatch.setattr(transport,'fetch_https',recovered)
    schedule_sources();job=claim();assert job
    collect.collect_job(job)
    with Session(engine()) as db:
        config=db.get(SourceSettings,source_id('eastmoney_funds'))
        assert config.paused_reason is None
        assert config.next_run_at==current[0]+timedelta(seconds=300)
        assert db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id)).status=='complete'
