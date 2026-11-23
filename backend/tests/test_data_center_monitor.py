"""Operational counts and worker separation against disposable PostgreSQL."""
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.ai.models import AiAttempt, Connection
from app.cli import bootstrap
from app.credential_init import initialize
from app.db import engine
from app.integrations.monitor import _data_state, _latest_data
from app.market.models import DataFetchRun
from app.models import Job, Provider, User, WorkerHeartbeat
from app.worker import claim


def identity(client):
    bootstrap('monitor-owner', 'synthetic-monitor-password')
    result = client.post('/api/v1/session', json={'username': 'monitor-owner', 'password': 'synthetic-monitor-password'},
                         headers={'Origin': 'http://127.0.0.1:8080'})
    assert result.status_code == 201
    return uuid.UUID(result.json()['data']['user']['id'])


def connection():
    return Connection(id=uuid.uuid4(), name='离线模型记录', kind='model', template='custom_model',
                      endpoint='https://models.example.com/v1', protocol='openai_chat', model='synthetic-model',
                      enabled=False, revision=1, options={}, source_ids=[], archived=False)


def test_monitor_gets_are_read_only_and_empty_is_measured(client):
    identity(client)
    for _ in range(2):
        result = client.get('/api/v1/data-center/overview')
        assert result.status_code == 200, result.text
        assert result.json()['data']['today_requests'] == 0
        assert result.json()['data']['running_tasks'] == 0
        assert client.get('/api/v1/data-center/calls').json()['data']['items'] == []
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(AiAttempt)) == 0


def test_physical_requests_not_tasks_or_preparations_are_counted(client):
    owner = identity(client)
    clock = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        model = connection()
        db.add(model)
        db.flush()
        provider = db.scalar(select(Provider).where(Provider.kind == 'manual'))
        db.add(DataFetchRun(id=uuid.uuid4(), source_id=provider.id, capability='synthetic.collect',
                           started_at=clock, received_at=clock, status='error', reason='rate_limited',
                           request_count=2, accepted=0, duplicates=0, rejected=0, elapsed_ms=200))
        db.add(AiAttempt(id=uuid.uuid4(), owner_id=owner, connection_id=model.id, purpose='probe',
                         service_name=model.name, model=model.model, state='succeeded', started_at=clock,
                         sent_at=clock, completed_at=clock, request_count=1, safe_details={'http_status': 200}))
        db.add(AiAttempt(id=uuid.uuid4(), owner_id=owner, connection_id=model.id, purpose='probe',
                         service_name=model.name, model=model.model, state='preparing', started_at=clock,
                         request_count=0, safe_details={}))
        db.add(Job(owner_id=owner, kind='ai.generate', state='queued', attempts=0, payload={}))
    result = client.get('/api/v1/data-center/overview').json()['data']
    assert result['today_requests'] == 3
    assert result['running_tasks'] == 0
    records = client.get('/api/v1/data-center/calls').json()['data']['items']
    assert len(records) == 3
    assert next(row for row in records if row['kind'] == 'ai')['cost_cny'] is None
    errors = client.get('/api/v1/data-center/calls?errors_only=true').json()['data']['items']
    assert len(errors) == 1 and errors[0]['error_code'] == 'rate_limited'
    assert client.get('/api/v1/data-center/calls?kind=data').json()['data']['items'][0]['request_count'] == 2


def test_call_pagination_is_stable_signed_and_scoped(client):
    owner = identity(client)
    clock = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        model = connection()
        db.add(model)
        db.flush()
        for i in range(4):
            db.add(AiAttempt(id=uuid.uuid4(), owner_id=owner, connection_id=model.id, purpose='discover',
                             service_name=model.name, model=model.model, state='succeeded',
                             started_at=clock - timedelta(seconds=i), sent_at=clock - timedelta(seconds=i),
                             completed_at=clock, request_count=1, safe_details={}))
    first = client.get('/api/v1/data-center/calls?limit=2').json()['data']
    assert len(first['items']) == 2 and first['next_cursor']
    second = client.get('/api/v1/data-center/calls', params={'limit': 2, 'cursor': first['next_cursor']}).json()['data']
    assert len(second['items']) == 2 and not second['next_cursor']
    assert not {row['id'] for row in first['items']} & {row['id'] for row in second['items']}
    assert client.get('/api/v1/data-center/calls', params={'kind': 'ai', 'cursor': first['next_cursor']}).status_code == 422
    assert client.get('/api/v1/data-center/calls?from_date=2026-10-25&to_date=2026-10-24').status_code == 422


def test_market_worker_never_claims_ai_jobs_and_generic_job_remains_readable(client):
    owner = identity(client)
    with Session(engine()) as db, db.begin():
        job = Job(id=uuid.uuid4(), owner_id=owner, kind='ai.generate', state='queued', attempts=0,
                  payload={}, result_ref={'ai_run_id': str(uuid.uuid4())})
        db.add(job)
        ident = job.id
    assert claim() is None
    response = client.get('/api/v1/jobs/' + str(ident))
    assert response.status_code == 200, response.text
    assert response.json()['data']['result_ref'] is None


def test_vault_initialization_preserves_master_and_refuses_orphan_ciphertext(tmp_path):
    master, ciphertext = tmp_path / 'master', tmp_path / 'credentials'
    initialize(master, ciphertext, owner=os.getuid())
    before = (master / 'ai_master_key').read_bytes()
    initialize(master, ciphertext, owner=os.getuid())
    assert (master / 'ai_master_key').read_bytes() == before
    (ciphertext / 'synthetic.key').write_bytes(b'ciphertext-placeholder')
    (master / 'ai_master_key').chmod(0o600)
    (master / 'ai_master_key').unlink()
    with pytest.raises(RuntimeError, match='without their master'):
        initialize(master, ciphertext, owner=os.getuid())


def test_viewer_cannot_see_other_users_model_attempts(client):
    owner = identity(client)
    with Session(engine()) as db, db.begin():
        other = User(id=uuid.uuid4(), username='other-operator', password_hash='unused', role='researcher', enabled=True)
        db.add(other)
        model = connection()
        db.add(model)
        db.flush()
        db.add(AiAttempt(owner_id=other.id, connection_id=model.id, purpose='probe', service_name=model.name,
                         model=model.model, state='failed', request_count=0, safe_details={}))
        db.get(User, owner).role = 'viewer'
    result = client.get('/api/v1/data-center/calls?kind=ai')
    assert result.status_code == 200 and result.json()['data']['items'] == []


def test_supplier_status_and_date_precision_do_not_overstate_coverage():
    assert _data_state([{'enabled': True, 'state': 'ready'}, {'enabled': True, 'state': 'not_checked'}]) == 'partial'
    assert _data_state([{'enabled': True, 'state': 'ready'}, {'enabled': True, 'state': 'partial'}]) == 'partial'
    assert _data_state([{'enabled': True, 'state': 'ready'}, {'enabled': False, 'state': 'error'}]) == 'ready'
    assert _data_state([{'enabled': True, 'state': 'ready', 'collection_enabled': False}]) == 'collection_disabled'
    assert _latest_data(['20261023', '2026-10-24']) == '2026-10-24'
    assert _latest_data(['20261023', '2026-10-23T10:00:00+08:00']) == '2026-10-23T10:00:00+08:00'


def test_historical_calls_retain_execution_connection_after_source_is_rebound(client):
    owner = identity(client)
    clock = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        provider = db.scalar(select(Provider).where(Provider.kind == 'manual'))
        old, current = connection(), connection()
        for model in (old, current):
            model.kind = 'market'
            model.source_ids = [str(provider.id)]
        old.name, old.archived = '原接入服务', True
        current.name = '后续接入服务'
        db.add_all([old, current])
        db.flush()
        ids = []
        for model in (old, current):
            job = Job(id=uuid.uuid4(), owner_id=owner, kind='data.collect', state='succeeded', attempts=1,
                      payload={'integration_context': {'connection_id': str(model.id), 'revision': 1}})
            db.add(job)
            db.flush()
            run = DataFetchRun(id=uuid.uuid4(), source_id=provider.id, job_id=job.id, capability='synthetic.collect',
                               started_at=clock, received_at=clock, status='complete', request_count=1,
                               accepted=1, duplicates=0, rejected=0)
            db.add(run)
            ids.append(str(run.id))
        old_id, current_id = old.id, current.id
    all_records = client.get('/api/v1/data-center/calls?kind=data').json()['data']['items']
    assert {row['service_name'] for row in all_records} == {'原接入服务', '后续接入服务'}
    for connection_id, record_id in zip((old_id, current_id), ids, strict=True):
        records = client.get('/api/v1/data-center/calls', params={'connection_id': str(connection_id)}).json()['data']['items']
        assert len(records) == 1 and records[0]['id'] == record_id
        assert records[0]['details']['connection_attribution'] == '执行时绑定'


def test_missing_ciphertext_overrides_previous_success_without_writing_or_sending(client, monkeypatch):
    owner = identity(client)
    clock = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        model = connection()
        model.enabled = True
        model.last_test = {'status': 'success', 'revision': 1}
        db.add(model)
        db.flush()
        db.add(AiAttempt(owner_id=owner, connection_id=model.id, purpose='probe', service_name=model.name,
                         model=model.model, state='succeeded', request_count=1, safe_details={},
                         started_at=clock, sent_at=clock, completed_at=clock))
    monkeypatch.setattr('app.ai.service.model_credential_health',
                        lambda _: {'configured': False, 'error_code': 'credential_missing'})
    result = client.get('/api/v1/data-center/overview').json()['data']
    service = next(row for row in result['services'] if row['kind'] == 'model')
    assert service['status'] == 'unavailable' and service['error_code'] == 'credential_missing'
    assert result['pending_issues'] >= 1
    assert result['today_requests'] == 1


def test_ready_health_matches_current_migration(client):
    with Session(engine()) as db, db.begin():
        db.add(WorkerHeartbeat(worker_id='market:synthetic', seen_at=datetime.now(UTC)))
    result = client.get('/health/ready')
    assert result.status_code == 200, result.text


def test_ai_heartbeat_does_not_hide_a_missing_collection_worker(client):
    with Session(engine()) as db, db.begin():
        db.execute(delete(WorkerHeartbeat))
        db.add(WorkerHeartbeat(worker_id='ai:synthetic', seen_at=datetime.now(UTC)))
    assert client.get('/health/ready').status_code == 503
