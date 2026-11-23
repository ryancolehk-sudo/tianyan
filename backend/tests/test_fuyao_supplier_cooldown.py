"""Real supplier waits must not be clipped by the Fuyao local transport."""
from datetime import UTC,datetime,timedelta
from email.utils import format_datetime
from types import SimpleNamespace
import json
import uuid

import pytest

from app.market import fuyao_transport as transport
from app.market.protocol import SourceFailure,Response

STAMP=datetime(2026,11,12,5,tzinfo=UTC)


@pytest.mark.parametrize('status',[429,503])
@pytest.mark.parametrize('hint',['172800',format_datetime(STAMP+timedelta(days=2),usegmt=True)])
def test_fuyao_preserves_two_day_wait_for_status_and_both_header_forms(monkeypatch,status,hint):
    monkeypatch.setattr(transport,'datetime',SimpleNamespace(now=lambda *args:STAMP))
    error=transport.status_failure(status,{'Retry-After':hint})
    assert error.retry_after==172800
    assert error.reason==('rate_limited' if status==429 else 'upstream_unavailable')


@pytest.mark.parametrize('hint,expected',[('0',0),('',None),('not-a-delay',None)])
def test_zero_and_missing_hint_are_not_invented_supplier_waits(hint,expected):
    error=transport.status_failure(429,{'Retry-After':hint})
    assert error.retry_after==expected


def test_unrepresentable_wait_is_not_shortened_and_diagnostics_do_not_echo_keys():
    error=transport.status_failure(429,{'Retry-After':'99999999999','Location':'https://other.invalid/?key=synthetic-private'})
    assert error.reason=='retry_after_out_of_range' and error.http_status==429
    assert 'synthetic' not in repr(transport.safe_diagnostic(error))


def configured(client,monkeypatch,families=('fuyao_quotes','fuyao_indices')):
    from sqlalchemy.orm import Session
    from app.config import settings
    from app.db import engine
    from app.market import fuyao
    from test_data_sources import login,configure
    from test_fuyao_data import config
    from test_integration_data import connection
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr('app.ai.vault.read_secret',lambda reference:'synthetic-fuyao-private-key')
    headers=login(client)
    for family in families:configure(client,headers,config(family),key=family)
    with Session(engine()) as db,db.begin():connection(db,families,template='fuyao',endpoint=fuyao.FUYAO_ENDPOINT)


def running(db,family,clock):
    from app.integrations.credentials import resolve_source_credentials,job_context
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.models import SourceSettings
    from app.market.service import source_id
    from app.models import Job,Provider
    identity=source_id(family);config=db.get(SourceSettings,identity);provider=db.get(Provider,identity)
    bound=resolve_source_credentials(db,identity,family)
    job=Job(owner_id=config.owner_id,kind='source.collect',state='running',attempts=1,
        lease_token=uuid.uuid4(),lease_until=clock+timedelta(days=1),payload={
            'source_id':str(identity),'integration_context':job_context(bound,identity,probe=True)})
    db.add(job);db.flush();fingerprint=configuration_fingerprint(provider,config);db.expunge(job)
    return job,fingerprint


def test_two_day_wait_is_shared_persistent_and_survives_expired_reply_lease(client,monkeypatch):
    from sqlalchemy import func,select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market import budget,fuyao_cooldown as cooldown
    from app.market.models import DataFetchRun
    from app.models import Job
    configured(client,monkeypatch);clock=datetime.now(UTC)
    monkeypatch.setattr(cooldown,'now',lambda db:clock);monkeypatch.setattr(budget,'now',lambda db:clock)
    with Session(engine()) as db,db.begin():
        first,_=running(db,'fuyao_quotes',clock);other,fingerprint=running(db,'fuyao_indices',clock)
        db.get(Job,first.id).lease_until=clock-timedelta(seconds=1)
    cooldown.record_failure(first,SourceFailure('rate_limited',retry_after=172800))
    cooldown.record_failure(first,SourceFailure('rate_limited',retry_after=600))
    assert not budget.reserve_fuyao_request(other,expected_configuration=fingerprint)
    with Session(engine()) as db:
        assert budget.fuyao_cooldown_until(db,clock)==clock+timedelta(days=2)
        saved=db.get(Job,other.id)
        assert saved.state=='queued' and saved.attempts==0 and saved.available_at==clock+timedelta(days=2)
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==0


@pytest.mark.parametrize('status',[429,503])
def test_collect_retains_actual_wait_and_other_capability_sends_zero_http(client,monkeypatch,status):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market import collect
    from app.market.models import DataFetchRun,SourceSettings
    from app.market.service import source_id
    from app.models import Job
    configured(client,monkeypatch);clock=datetime.now(UTC);calls=[]
    def request(*args,**kwargs):calls.append(args);raise transport.status_failure(status,{'Retry-After':'172800'})
    monkeypatch.setattr(transport,'request',request)
    with Session(engine()) as db,db.begin():
        first,_=running(db,'fuyao_quotes',clock);other,_=running(db,'fuyao_indices',clock)
    collect.collect_job(first);collect.collect_job(other)
    with Session(engine()) as db:
        rows=list(db.scalars(select(DataFetchRun)))
        assert len(calls)==sum(row.request_count for row in rows)==1
        run=next(row for row in rows if row.job_id==first.id)
        assert run.reason==('rate_limited' if status==429 else 'upstream_unavailable') and run.retry_not_before is None
        deadline=db.get(SourceSettings,source_id('fuyao_quotes')).legacy_cooldown_until
        assert deadline>=clock+timedelta(days=2)
        waiting=db.get(Job,other.id)
        assert waiting.state=='queued' and waiting.attempts==0 and waiting.available_at>=deadline


def test_financial_daily_interval_is_not_a_shared_vendor_cooldown(client,monkeypatch):
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market import collect,fuyao_cooldown as cooldown
    from app.models import Job
    configured(client,monkeypatch,('fuyao_financial','fuyao_quotes'))
    clock=datetime.now(UTC)
    def request(*args,**kwargs):raise transport.status_failure(429,{})
    monkeypatch.setattr(transport,'request',request)
    with Session(engine()) as db,db.begin():job,_=running(db,'fuyao_financial',clock)
    collect.collect_job(job)
    with Session(engine()) as db:
        deadline=cooldown.cooldown_until(db,datetime.now(UTC))
        assert clock+timedelta(seconds=300)<=deadline<clock+timedelta(seconds=305)
        receipt=db.get(Job,job.id).result_ref['fuyao_cooldown']
        assert receipt['basis']=='local_backoff' and receipt['seconds']==300


def test_mid_statement_cooldown_preserves_the_one_http_already_sent(client,monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.evidence.models import EvidenceVersion
    from app.market import collect
    from app.market.models import DataFetchRun,SourceSettings
    from app.market.service import source_id
    from app.models import Job
    configured(client,monkeypatch,('fuyao_financial','fuyao_quotes'))
    clock=datetime.now(UTC);calls=[]
    def request(*args,**kwargs):
        calls.append(args)
        with Session(engine()) as db,db.begin():
            db.get(SourceSettings,source_id('fuyao_quotes')).legacy_cooldown_until=clock+timedelta(days=2)
        return Response(json.dumps({'code':0,'data':{'item':[]}}).encode(),datetime.now(UTC),1,200)
    monkeypatch.setattr(transport,'request',request)
    with Session(engine()) as db,db.begin():job,_=running(db,'fuyao_financial',clock)
    collect.collect_job(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==job.id))
        assert len(calls)==run.request_count==1 and run.reason=='local_request_wait'
        waiting=db.get(Job,job.id)
        assert waiting.state=='queued' and waiting.attempts==0 and waiting.available_at>=clock+timedelta(days=2)
        assert not list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id('fuyao_financial'))))


def test_unrepresentable_wait_pauses_other_capability_at_final_request(client,monkeypatch):
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market import budget,fuyao_cooldown as cooldown
    configured(client,monkeypatch);clock=datetime.now(UTC)
    with Session(engine()) as db,db.begin():
        first,_=running(db,'fuyao_quotes',clock);other,fingerprint=running(db,'fuyao_indices',clock)
    cooldown.record_failure(first,SourceFailure('retry_after_out_of_range'))
    with pytest.raises(SourceFailure):budget.reserve_fuyao_request(other,expected_configuration=fingerprint)
