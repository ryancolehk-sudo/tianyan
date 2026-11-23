"""Synthetic request volume on isolated PostgreSQL; no supplier requests."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app.db import engine
from app.market.budget import TUSHARE_ACTION, reserve_tushare_request
from app.market.models import DataFetchRun
from app.market.protocol import SourceFailure
from app.market.service import source_id
from app.models import AuditEvent, Job, User
from test_data_sources import configure, login
from test_tushare import source

CLOCK=datetime(2026,11,11,6,30,tzinfo=UTC)
OBSERVED_CALLS=2500


@pytest.fixture
def request_job(client,monkeypatch):
    headers=login(client)
    for family in ('tushare_cn_cpi','tushare_shibor'):
        configure(client,headers,source(family),key=family)
    monkeypatch.setattr('app.market.budget.now',lambda db:CLOCK)
    with Session(engine()) as db,db.begin():
        owner=db.scalar(select(User.id))
        token=uuid.uuid4()
        job=Job(owner_id=owner,kind='source.collect',state='running',attempts=1,
            payload={'source_id':str(source_id('tushare_cn_cpi'))},lease_token=token,
            lease_until=CLOCK+timedelta(seconds=60),available_at=CLOCK,created_at=CLOCK)
        db.add(job);db.flush()
        db.execute(insert(AuditEvent),[dict(id=uuid.uuid4(),actor_id=owner,action=TUSHARE_ACTION,
            resource_type='job',resource_id=job.id,changed_field_names=['purchased_data_request_permit'],
            created_at=CLOCK-timedelta(seconds=3*(OBSERVED_CALLS-index))) for index in range(OBSERVED_CALLS)])
        return SimpleNamespace(id=job.id,lease_token=token,payload=dict(job.payload),headers=headers)


def test_more_than_two_thousand_daily_calls_does_not_stop_a_healthy_request(request_job):
    assert reserve_tushare_request(request_job) is True
    with Session(engine()) as db:
        saved=db.get(Job,request_job.id)
        assert saved.state=='running' and saved.lease_token==request_job.lease_token
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==TUSHARE_ACTION))==OBSERVED_CALLS+1


def test_shared_request_spacing_still_applies_above_the_old_daily_cap(request_job):
    latest=CLOCK-timedelta(seconds=1)
    with Session(engine()) as db,db.begin():
        db.add(AuditEvent(action=TUSHARE_ACTION,created_at=latest))
    assert reserve_tushare_request(request_job) is False
    with Session(engine()) as db:
        saved=db.get(Job,request_job.id)
        assert (saved.state,saved.error_code,saved.attempts)==('queued','source_budget_wait',0)
        assert saved.available_at==latest+timedelta(seconds=2)
        assert saved.lease_token is None
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==TUSHARE_ACTION))==OBSERVED_CALLS+1


@pytest.mark.parametrize('wait_for_spacing',[False,True])
def test_actual_supplier_cooldown_remains_shared_across_tushare_sources(request_job,wait_for_spacing):
    until=CLOCK+timedelta(minutes=2)
    with Session(engine()) as db,db.begin():
        db.add(DataFetchRun(source_id=source_id('tushare_shibor'),capability='reference.tushare',
            started_at=CLOCK-timedelta(seconds=1),received_at=CLOCK,status='error',reason='rate_limited',
            request_count=1,retry_not_before=until))
    assert reserve_tushare_request(request_job,wait_for_spacing=wait_for_spacing) is False
    with Session(engine()) as db:
        saved=db.get(Job,request_job.id)
        assert saved.state=='queued' and saved.available_at==until
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==TUSHARE_ACTION))==OBSERVED_CALLS


@pytest.mark.parametrize('invalid',['expired','different_token'])
def test_removing_the_daily_cap_never_bypasses_the_exact_job_lease(request_job,invalid):
    if invalid=='expired':
        with Session(engine()) as db,db.begin():
            db.get(Job,request_job.id).lease_until=CLOCK
    else:
        request_job.lease_token=uuid.uuid4()
    with pytest.raises(SourceFailure,match='lease_lost'):
        reserve_tushare_request(request_job)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==TUSHARE_ACTION))==OBSERVED_CALLS


def test_legacy_page_permit_has_no_stale_daily_cap_reference(client,monkeypatch,request_job):
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.models import SourceSettings
    from app.market.tushare_pages import reserve_followup
    from app.models import Provider
    configure(client,request_job.headers,source('tushare_moneyflow_dc'),key='legacy-followup-synthetic')
    monkeypatch.setattr('app.market.tushare_pages.now',lambda db:CLOCK)
    with Session(engine()) as db,db.begin():
        current=db.get(Job,request_job.id)
        current.payload={'source_id':str(source_id('tushare_moneyflow_dc'))}
        request_job.payload=dict(current.payload)
        provider=db.get(Provider,source_id('tushare_moneyflow_dc'))
        fingerprint=configuration_fingerprint(provider,db.get(SourceSettings,provider.id))
    assert reserve_followup(request_job,fingerprint) is None
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==TUSHARE_ACTION))==OBSERVED_CALLS+1
