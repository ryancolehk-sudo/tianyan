"""Capability proofs use the actual source binding; disposable PostgreSQL only."""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_capability_routes import patch, read
from test_integration_data import configured, connection

from app.db import engine
from app.integrations.credentials import job_context, resolve_source_credentials
from app.integrations.routing import _signature
from app.integrations.routing_models import DataRoutes
from app.market.models import DataFetchRun, SourceSettings
from app.market.service import source_id
from app.models import Job, User


def receipt(db, family, context=None):
    clock = db.scalar(select(func.clock_timestamp()))
    source = source_id(family)
    owner = db.scalar(select(User).where(User.username == 'owner'))
    payload = {'source_id': str(source)}
    if context is not None:
        payload['integration_context'] = context
    job = Job(id=uuid.uuid4(), owner_id=owner.id, kind='source.collect', state='succeeded',
        attempts=1, payload=payload, result_ref={}, created_at=clock, available_at=clock,
        completed_at=clock, lease_token=None, lease_until=None, error_code=None)
    db.add(job)
    db.flush()
    run = DataFetchRun(id=uuid.uuid4(), source_id=source, job_id=job.id,
        capability='synthetic.binding-proof', started_at=clock, received_at=clock,
        status='complete', reason=None, expected=1, accepted=1, rejected=0,
        duplicates=0, request_count=1, elapsed_ms=1, cost_cny=None, body_hash=None)
    db.add(run)
    db.flush()
    return job, run


def option(client, family):
    capability = {'tushare_rt_k': 'stock_quote', 'tushare_daily': 'stock_daily',
        'tushare_income': 'financial'}.get(family, 'source:' + family)
    row = next(row for row in read(client)['capabilities'] if row['id'] == capability)
    return next(item for item in row['options'] if item['provider_id'] == 'tushare')


@pytest.mark.parametrize('family', ['tushare_rt_k', 'tushare_calendar_sse', 'tushare_stock_basic', 'tushare_income'])
def test_unbound_environment_success_survives_another_capability_saved_connection(client, monkeypatch, family):
    configured(client, monkeypatch, (family, 'tushare_daily'))
    with Session(engine()) as db, db.begin():
        connection(db, ('tushare_daily',))
        receipt(db, family)
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: pytest.fail('Read must not contact supplier'))
    result = option(client, family)
    assert result['entitlement'] == 'verified'
    assert result['last_success_at'] is not None
    assert result['source_ids'] == [str(source_id(family))]


def test_each_saved_source_uses_its_own_connection_revision(client, monkeypatch):
    configured(client, monkeypatch, ('tushare_rt_k', 'tushare_daily'))
    with Session(engine()) as db, db.begin():
        actual = connection(db, ('tushare_rt_k',))
        actual.revision = 3
        actual.updated_at = db.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
        other = connection(db, ('tushare_daily',))
        other.revision = 9
        other.updated_at = db.scalar(select(func.clock_timestamp()))
        bound = resolve_source_credentials(db, source_id('tushare_rt_k'), 'tushare_rt_k', refresh=True)
        receipt(db, 'tushare_rt_k', job_context(bound, source_id('tushare_rt_k')))
    assert option(client, 'tushare_rt_k')['entitlement'] == 'verified'


@pytest.mark.parametrize('field', ['connection_id', 'source_id', 'revision'])
def test_equal_revision_on_an_unrelated_binding_is_not_account_proof(client, monkeypatch, field):
    configured(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        actual = connection(db)
        bound = resolve_source_credentials(db, source_id('tushare_rt_k'), 'tushare_rt_k', refresh=True)
        context = job_context(bound, source_id('tushare_rt_k'))
        context[field] = actual.revision + 1 if field == 'revision' else str(uuid.uuid4())
        receipt(db, 'tushare_rt_k', context)
    assert option(client, 'tushare_rt_k')['entitlement'] == 'unknown'


@pytest.mark.parametrize('case', ['archived', 'conflict', 'legacy_before_binding', 'unbound_with_stored_context'])
def test_a_stored_binding_never_silently_falls_back_to_environment_proof(client, monkeypatch, case):
    configured(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        source = source_id('tushare_rt_k')
        if case == 'unbound_with_stored_context':
            receipt(db, 'tushare_rt_k', {'connection_id': str(uuid.uuid4()), 'source_id': str(source), 'revision': 1})
        elif case == 'legacy_before_binding':
            receipt(db, 'tushare_rt_k')
            connection(db)
        else:
            actual = connection(db)
            bound = resolve_source_credentials(db, source, 'tushare_rt_k', refresh=True)
            receipt(db, 'tushare_rt_k', job_context(bound, source))
            if case == 'archived':
                actual.archived = True
            else:
                connection(db)
    assert option(client, 'tushare_rt_k')['entitlement'] == 'unknown'


@pytest.mark.parametrize('proof', ['job', 'test', 'route'])
def test_current_own_binding_proofs_still_qualify_without_enabling_it(client, monkeypatch, proof):
    configured(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        source = source_id('tushare_rt_k')
        actual = connection(db, enabled=False)
        bound = resolve_source_credentials(db, source, 'tushare_rt_k', allow_disabled=True, refresh=True)
        context = job_context(bound, source, probe=True) if proof == 'job' else None
        _, run = receipt(db, 'tushare_rt_k', context)
        if proof == 'test':
            actual.last_test = {'status': 'success', 'revision': actual.revision, 'source_id': str(source)}
        elif proof == 'route':
            config = db.get(SourceSettings, source)
            checks = {str(source): {'signature': _signature(config, actual), 'run_id': str(run.id)}}
            db.add(DataRoutes(id=1, revision=2, choices={'stock_quote': {'provider_id': 'tushare', 'checks': checks}}, updated_at=func.now()))
        identity = actual.id
    assert option(client, 'tushare_rt_k')['entitlement'] == 'verified'
    from app.ai.models import Connection
    with Session(engine()) as db:
        assert db.get(Connection, identity).enabled is False


def test_route_preserves_each_qualifying_source_own_material_signature(client, monkeypatch):
    families = ('tushare_income', 'tushare_balancesheet', 'tushare_cashflow')
    headers = configured(client, monkeypatch, families)
    with Session(engine()) as db, db.begin():
        first = connection(db, families[:1], secret_ref='vault:first')
        second = connection(db, families[1:], secret_ref='vault:second')
        second.revision = 3
        expected = {}
        for family in families:
            source = source_id(family)
            bound = resolve_source_credentials(db, source, family, refresh=True)
            receipt(db, family, job_context(bound, source))
            own = first if family == families[0] else second
            expected[str(source)] = _signature(db.get(SourceSettings, source), own)
    response = patch(client, headers, 1, [{'capability_id': 'financial', 'provider_id': 'tushare'}])
    assert response.status_code == 200, response.text
    with Session(engine()) as db:
        checks = db.get(DataRoutes, 1).choices['financial']['checks']
        assert {source: row['signature'] for source, row in checks.items()} == expected
