"""Synthetic cloud accounts and source rights on disposable PostgreSQL only."""
import importlib.util
from pathlib import Path
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import read_cache
from app.auth import hasher
from app.cli import bootstrap
from app.config import settings
from app.db import engine
from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import ReferencePayload
from app.models import AuditEvent, Instrument, Job, Provider, User
from app.read_cache_models import ReadCache
from test_research import DAY, ORIGIN, PASSWORD, create, post, revision, workspace

ADMIN_FILE = Path(__file__).resolve().parents[2] / 'scripts/cloud/admin.py'
SPEC = importlib.util.spec_from_file_location('cloud_account_operations', ADMIN_FILE)
admin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admin)


def login(client, name):
    result = client.post('/api/v1/session', json={'username': name, 'password': PASSWORD}, headers=ORIGIN)
    assert result.status_code == 201, result.text
    return {**ORIGIN, 'X-CSRF-Token': result.json()['data']['csrf_token']}


def test_cloud_host_account_creation_requires_admin_and_never_replaces_an_account(client):
    bootstrap('operator', PASSWORD)
    account = admin.create_user('operator', 'one', PASSWORD, 'researcher')
    with Session(engine()) as db:
        saved = db.get(User, uuid.UUID(account['id']))
        assert saved.password_hash != PASSWORD and hasher.verify(saved.password_hash, PASSWORD)
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == 'cloud.user.create'))
        assert event.resource_id == saved.id and 'password' not in event.changed_field_names
    with pytest.raises(ValueError, match='admin_required'):
        admin.create_user('one', 'injected-admin', PASSWORD, 'admin')
    with pytest.raises(ValueError, match='user_exists'):
        admin.create_user('operator', 'one', 'different-password-long-enough')
    with pytest.raises(ValueError, match='invalid_password'):
        admin.create_user('operator', 'short-password', 'short')
    assert login(client, 'one')


def test_cloud_other_researcher_cannot_read_modify_or_copy_private_resources(workspace, monkeypatch):
    w = workspace
    monkeypatch.setattr(settings(), 'deployment_mode', 'cloud')
    document = create(w)
    admin.create_user('research-owner', 'second', PASSWORD, 'researcher')
    with Session(engine()) as db:
        task = db.scalar(select(Job).where(Job.owner_id == uuid.UUID(w['owner'])))
        task_id = str(task.id)
    headers = login(w['client'], 'second')
    for path in (f"/api/v1/evidence/{w['evidence']}", f"/api/v1/snapshots/{w['snapshot']}",
                 f"/api/v1/research-documents/{document['id']}",
                 f"/api/v1/revisions/{document['head_revision_id']}", f'/api/v1/jobs/{task_id}'):
        response = w['client'].get(path)
        assert response.status_code == 404, (path, response.text)
        assert 'Synthetic research evidence' not in response.text
    for path in ('/api/v1/evidence', '/api/v1/snapshots', '/api/v1/research-documents', '/api/v1/review-archives'):
        assert w['client'].get(path).json()['data']['items'] == []
    write = post(w['client'], f"/api/v1/research-documents/{document['id']}/revisions",
                 revision(w), headers, etag='"1"')
    assert write.status_code == 404
    copied = post(w['client'], '/api/v1/research-documents',
        {'kind': 'review', 'source_trade_day': DAY, 'revision': revision(w)}, headers)
    assert copied.status_code in {404, 409}
    for path in ('/api/v1/system/backups', '/api/v1/system/diagnostics'):
        assert w['client'].get(path).status_code == 403


def test_cloud_preferences_watchlist_and_write_identity_stay_account_scoped(client, monkeypatch):
    bootstrap('operator', PASSWORD)
    admin.create_user('operator', 'one', PASSWORD)
    admin.create_user('operator', 'two', PASSWORD)
    monkeypatch.setattr(settings(), 'deployment_mode', 'cloud')
    with Session(engine()) as db, db.begin():
        instrument = Instrument(exchange='SZSE', asset_type='stock', symbol='000001', name='Synthetic instrument')
        db.add(instrument)
        db.flush()
        ident = str(instrument.id)
    first = login(client, 'one')
    changed = client.put('/api/v1/me/preferences', headers={**first, 'Idempotency-Key': str(uuid.uuid4())},
        json={'start_page': 'review', 'review_lookback': 60, 'news_category': '全部', 'expected_revision': 1})
    assert changed.status_code == 200, changed.text
    selected = post(client, '/api/v1/watchlist', {'instrument_ids': [ident]}, first)
    assert selected.status_code == 200, selected.text
    second = login(client, 'two')
    assert client.get('/api/v1/me/preferences').json()['data']['review_lookback'] == 20
    assert client.get('/api/v1/watchlist').json()['data']['items'] == []
    spoofed = post(client, '/api/v1/watchlist', {'instrument_ids': [ident], 'owner_id': 'one'}, second)
    assert spoofed.status_code == 422
    login(client, 'one')
    assert client.get('/api/v1/me/preferences').json()['data']['review_lookback'] == 60
    assert len(client.get('/api/v1/watchlist').json()['data']['items']) == 1


def test_cloud_cache_does_not_reuse_another_account_or_local_deployment_scope(client, monkeypatch):
    bootstrap('operator', PASSWORD)
    admin.create_user('operator', 'two', PASSWORD)
    monkeypatch.setattr(settings(), 'read_cache_enabled', True)
    monkeypatch.setattr(settings(), 'deployment_mode', 'local')
    with Session(engine()) as db:
        one = db.scalar(select(User).where(User.username == 'operator'))
        two = db.scalar(select(User).where(User.username == 'two'))
        local_scope = read_cache.cache_scope(db, one)
        monkeypatch.setattr(settings(), 'deployment_mode', 'cloud')
        first_scope = read_cache.cache_scope(db, one)
        second_scope = read_cache.cache_scope(db, two)
        assert len({local_scope, first_scope, second_scope}) == 3
        first = read_cache.ensure_entry(db, one, first_scope, 'workbench', {})
        second = read_cache.ensure_entry(db, two, second_scope, 'workbench', {})
        assert first.key != second.key and first.owner_id == one.id and second.owner_id == two.id
        assert len(list(db.scalars(select(ReadCache)))) == 2


def test_cloud_shared_source_requires_explicit_right_and_revocation_is_immediate(client, monkeypatch):
    bootstrap('operator', PASSWORD)
    login(client, 'operator')
    payload = ReferencePayload(dataset='synthetic.cloud.reference', title='Synthetic cloud test',
        source_url='https://example.com/synthetic', attribution='Self-created test', scope='synthetic',
        columns=[{'key': 'value', 'label': 'Value', 'unit': None}], rows=[{'value': '123'}],
        observed_day=date(2001, 12, 31)).model_dump(mode='json')
    moment = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        provider = Provider(name='Synthetic private-use provider', family='cloud_private_fixture', kind='market',
            configured=True, enabled=True, entitlement_status='verified', adapter_version='synthetic-v1',
            capabilities=['synthetic.cloud.reference'],
            rights={'display': 'allowed', 'cache': 'allowed', 'export': 'denied', 'redistribute': 'denied'})
        db.add(provider)
        db.flush()
        evidence = EvidenceVersion(source_id=provider.id, owner_id=None, visibility='shared',
            source_key='synthetic.cloud.reference', source_revision='1', version_number=1, kind='reference',
            event_at=moment, received_at=moment, available_at=None, availability='unknown',
            provenance='source_reported', historical_eligible=False, rights_basis='Synthetic fixture only',
            payload=payload, content_hash=content_hash(payload))
        db.add(evidence)
        db.flush()
        source, target = provider.id, str(evidence.id)
    monkeypatch.setattr(settings(), 'deployment_mode', 'local')
    assert client.get('/api/v1/evidence/' + target).status_code == 200
    monkeypatch.setattr(settings(), 'deployment_mode', 'cloud')
    assert client.get('/api/v1/evidence/' + target).status_code == 404
    assert admin.source_scope_check()['blocked_sources'] == 1
    with Session(engine()) as db, db.begin():
        row = db.get(Provider, source)
        row.rights = {**row.rights, 'redistribute': 'allowed'}
    assert client.get('/api/v1/evidence/' + target).status_code == 200
    with Session(engine()) as db, db.begin():
        row = db.get(Provider, source)
        row.rights = {**row.rights, 'redistribute': 'denied'}
    assert client.get('/api/v1/evidence/' + target).status_code == 404
