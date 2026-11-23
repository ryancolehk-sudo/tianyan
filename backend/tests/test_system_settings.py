"""Local account/maintenance control plane on the isolated PostgreSQL."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from test_data_sources import ORIGIN, PASSWORD, login

from app import read_cache
from app.auth import hasher, verify_password
from app.config import settings
from app.db import engine
from app.evidence.models import IdempotencyKey
from app.main import app
from app.models import AuditEvent, LoginSession, User
from app.read_cache_models import ReadCache, ReadDependency
from app.schemas import Preferences
from app.system_settings import service
from app.system_settings.models import BackupRecord, MaintenanceJob, UserPreference


def body(revision=1, **values):
    return {'start_page': 'review', 'review_lookback': 60, 'news_category': '全部',
            'expected_revision': revision, **values}


def key(headers, value='settings-test-001'):
    return {**headers, 'Idempotency-Key': value}


def owner(db):
    return db.scalar(select(User).where(User.username == 'owner'))


def new_user(name='second', role='viewer'):
    with Session(engine()) as db, db.begin():
        user = User(username=name, role=role, password_hash=hasher.hash(PASSWORD))
        db.add(user)
        db.flush()
        return user.id


def authenticate(client, username):
    response = client.post('/api/v1/session', json={'username': username, 'password': PASSWORD}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return {**ORIGIN, 'X-CSRF-Token': response.json()['data']['csrf_token']}


def heartbeat():
    with Session(engine()) as db, db.begin():
        service.get_system_state(db).backup_heartbeat_at = datetime.now(UTC)


def test_preferences_default_persist_revision_replay_and_session(client):
    headers = login(client)
    assert client.get('/api/v1/me/preferences').json()['data'] == Preferences().model_dump()
    saved = client.put('/api/v1/me/preferences', json=body(), headers=key(headers))
    assert saved.status_code == 200, saved.text
    assert saved.headers['cache-control'] == 'no-store'
    assert saved.json()['data'] == {'start_page': 'review', 'review_lookback': 60, 'news_category': '全部', 'revision': 2}
    replay = client.put('/api/v1/me/preferences', json=body(), headers=key(headers))
    assert replay.status_code == 200 and replay.headers['idempotency-replayed'] == 'true'
    assert replay.json() == saved.json()
    assert client.put('/api/v1/me/preferences', json=body(), headers=key(headers, 'settings-test-002')).status_code == 409
    assert client.get('/api/v1/session').json()['data']['preferences'] == saved.json()['data']
    assert client.delete('/api/v1/session', headers=headers).status_code == 204
    authenticate(client, 'owner')
    assert client.get('/api/v1/me/preferences').json()['data'] == saved.json()['data']


@pytest.mark.parametrize('override', [dict(start_page='bad'), dict(review_lookback=30), dict(news_category='bad'), dict(expected_revision=0)])
def test_preferences_validation_and_csrf(client, override):
    headers = login(client)
    assert client.put('/api/v1/me/preferences', json=body(**override), headers=key(headers)).status_code == 422
    assert client.put('/api/v1/me/preferences', json=body(), headers=ORIGIN).status_code == 403


def test_preference_account_isolation_and_legacy_schema_default(client, monkeypatch):
    headers = login(client)
    client.put('/api/v1/me/preferences', json=body(), headers=key(headers))
    new_user()
    with TestClient(app, base_url='http://127.0.0.1:8080') as other:
        authenticate(other, 'second')
        assert other.get('/api/v1/me/preferences').json()['data']['start_page'] == 'workbench'
    class Legacy:
        def scalar(self, stmt):
            return None
    assert service.preferences(Legacy(), uuid.uuid4()) == Preferences()


def test_password_verifies_current_revokes_every_session_and_no_body_history(client):
    headers = login(client)
    with TestClient(app, base_url='http://127.0.0.1:8080') as other:
        authenticate(other, 'owner')
        fresh = 'different-local-test-password-123'
        response = client.post('/api/v1/me/password', json={'current_password': PASSWORD, 'new_password': fresh}, headers=headers)
        assert response.status_code == 204, response.text
        assert client.get('/api/v1/session').status_code == 401
        assert other.get('/api/v1/session').status_code == 401
        with Session(engine()) as db:
            assert verify_password(owner(db).password_hash, fresh)
            assert db.scalar(select(func.count()).select_from(IdempotencyKey)) == 0
            assert db.scalar(select(func.count()).select_from(LoginSession).where(LoginSession.revoked_at.is_(None))) == 0
            rows = list(db.execute(text('SELECT action,outcome,context FROM audit_events')))
            assert fresh not in str(rows) and PASSWORD not in str(rows)
            assert any(row[0] == 'settings.password' and row[1] == 'succeeded' for row in rows)


def test_password_bad_current_independent_rate_limit_and_validation(client):
    headers = login(client)
    for _ in range(5):
        response = client.post('/api/v1/me/password', json={'current_password': 'wrong', 'new_password': 'long-enough-test-value'}, headers=headers)
        assert response.status_code == 400, response.text
    assert client.post('/api/v1/me/password', json={'current_password': PASSWORD, 'new_password': 'long-enough-test-value'}, headers=headers).status_code == 429
    assert client.get('/api/v1/session').status_code == 200
    assert client.post('/api/v1/me/password', json={'current_password': PASSWORD, 'new_password': 'short'}, headers=headers).status_code == 422


def test_sessions_only_owner_revoke_others_retains_current(client):
    headers = login(client)
    new_user()
    with TestClient(app, base_url='http://127.0.0.1:8080') as other, TestClient(app, base_url='http://127.0.0.1:8080') as second:
        authenticate(other, 'owner')
        authenticate(second, 'second')
        own = client.get('/api/v1/me/sessions').json()['data']['items']
        assert len(own) == 2 and sum(row['current'] for row in own) == 1
        foreign = second.get('/api/v1/me/sessions').json()['data']['items'][0]['id']
        assert client.delete(f'/api/v1/me/sessions/{foreign}', headers=headers).status_code == 404
        assert client.delete('/api/v1/me/sessions/others', headers=headers).status_code == 204
        assert other.get('/api/v1/session').status_code == 401
        assert second.get('/api/v1/session').status_code == 200
        assert client.get('/api/v1/session').status_code == 200
        current = next(row['id'] for row in own if row['current'])
        assert client.delete(f'/api/v1/me/sessions/{current}', headers=headers).status_code == 204
        assert client.get('/api/v1/session').status_code == 401


def test_overview_unconfigured_and_diagnostics_are_local_and_redacted(client, monkeypatch):
    login(client)
    monkeypatch.setattr(settings(), 'update_feed_url', None)
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: pytest.fail('settings reads used network'))
    response = client.get('/api/v1/system/overview')
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['update'] == {'configured': False, 'state': 'unconfigured'}
    assert data['maintenance']['backup_connected'] is False
    assert data['cache'] == {'count': 0, 'bytes': 0, 'enabled': False}
    diagnostic = client.get('/api/v1/system/diagnostics')
    assert diagnostic.status_code == 200
    assert not set(diagnostic.json()['data']) & {'username', 'password', 'environment', 'database_url'}
    assert 'postgresql' not in diagnostic.text and '/workspace/' not in diagnostic.text


def test_backup_queue_singleton_idempotent_truthful_status_and_redaction(client):
    headers = login(client)
    assert client.post('/api/v1/system/maintenance-jobs', json={'kind': 'backup'}, headers=key(headers)).status_code == 503
    heartbeat()
    first = client.post('/api/v1/system/maintenance-jobs', json={'kind': 'backup'}, headers=key(headers))
    assert first.status_code == 202, first.text
    data = first.json()['data']
    assert data['status'] == data['stage'] == 'queued' and data['result'] is None
    replay = client.post('/api/v1/system/maintenance-jobs', json={'kind': 'backup'}, headers=key(headers))
    assert replay.json() == first.json()
    assert client.post('/api/v1/system/maintenance-jobs', json={'kind': 'backup'}, headers=key(headers, 'settings-test-002')).status_code == 409
    with Session(engine()) as db, db.begin():
        row = db.get(MaintenanceJob, uuid.UUID(data['id']))
        row.result = {'backup_id': str(uuid.uuid4()), 'location_key': 'private-key', 'secret': 'do-not-serialize', 'size_bytes': 14}
    public = client.get('/api/v1/system/maintenance-jobs/' + data['id'])
    assert public.status_code == 200
    assert 'private-key' not in public.text and 'do-not-serialize' not in public.text


@pytest.mark.parametrize('route,method,payload', [('/api/v1/system/cache', 'delete', None),
    ('/api/v1/system/backups', 'get', None), ('/api/v1/system/diagnostics', 'get', None),
    ('/api/v1/system/maintenance-jobs', 'post', {'kind': 'backup'})])
def test_maintenance_admin_only(client, route, method, payload):
    headers = login(client, 'viewer')
    kwargs = {'headers': key(headers)}
    if payload:
        kwargs['json'] = payload
    assert getattr(client, method)(route, **kwargs).status_code == 403


def test_backup_history_recovery_integrity_distinct_and_location_hidden(client):
    login(client)
    with Session(engine()) as db, db.begin():
        user = owner(db)
        job = service.create_job(db, user, 'backup')
        job.status = 'succeeded'
        db.add(BackupRecord(owner_id=user.id, job_id=job.id, name='本机备份', location_key='private/server/path',
            app_mode='demo', schema_revision='0017_system_settings', build_revision='test', integrity_state='passed',
            recovery_state='unverified', sha256='a' * 64, size_bytes=123, completed_at=datetime.now(UTC)))
    response = client.get('/api/v1/system/backups')
    assert response.status_code == 200, response.text
    item = response.json()['data']['items'][0]
    assert item['integrity_state'] == 'passed' and item['recovery_state'] == 'unverified'
    assert item['recovery_verified_at'] is None and 'location_key' not in item
    assert 'private/server/path' not in response.text


def test_cache_clear_only_current_scope_preserves_sources_and_fences_late_publish(client, monkeypatch):
    headers = login(client)
    monkeypatch.setattr(settings(), 'read_cache_enabled', True)
    other_id = new_user()
    with Session(engine(), expire_on_commit=False) as db:
        user = owner(db)
        scope = read_cache.cache_scope(db, user)
        entry = read_cache.ensure_entry(db, user, scope, 'details', {'day': '2026-10-23', 'codes': ['000100.SZ']})
        cache_key = entry.key
        other = db.get(User, other_id)
        other_entry = read_cache.ensure_entry(db, other, read_cache.cache_scope(db, other), 'details', {'day': '2026-10-23', 'codes': ['000100.SZ']})
        other_key = other_entry.key
        versions = list(db.execute(select(ReadDependency.key, ReadDependency.revision).order_by(ReadDependency.key)))
        source_count = db.scalar(text('SELECT count(*) FROM source_versions'))
        token = read_cache.claim(db, cache_key)
        def clear_while_building(*args):
            cleared = client.delete('/api/v1/system/cache', headers=key(headers))
            assert cleared.status_code == 200, cleared.text
            return {'data': {}, 'quality': {'status': 'empty', 'reasons': []}}
        monkeypatch.setattr(read_cache, 'compute', clear_while_building)
        assert read_cache.build(db, cache_key, token) is False
        db.expire_all()
        assert db.get(ReadCache, cache_key) is None
        assert db.get(ReadCache, other_key) is not None
        assert list(db.execute(select(ReadDependency.key, ReadDependency.revision).order_by(ReadDependency.key))) == versions
        assert db.scalar(text('SELECT count(*) FROM source_versions')) == source_count


def test_activity_filter_cursor_outcome_and_arbitrary_context_redaction(client):
    headers = login(client)
    client.put('/api/v1/me/preferences', json=body(), headers=key(headers))
    with Session(engine()) as db, db.begin():
        db.add(AuditEvent(actor_id=owner(db).id, action='session.login', created_at=datetime(2000, 1, 1, tzinfo=UTC)))
        event = service.audit(db, owner(db), 'settings.cache_clear', context={'count': 3, 'token': 'private-token', 'research': 'private-note'})
        event_id = event.id
        db.execute(text('UPDATE audit_events SET context=CAST(:context AS jsonb) WHERE id=:id'),
                   {'id': event_id, 'context': json.dumps({'count': 3, 'password': 'stored-secret', 'path': 'private/path'})})
    result = client.get('/api/v1/system/activity?limit=1')
    assert result.status_code == 200, result.text
    first = result.json()['data']
    assert first['next_cursor'] and first['items'][0]['outcome'] == 'succeeded'
    assert first['items'][0]['details'] == {'count': 3}
    assert 'stored-secret' not in result.text and 'private/path' not in result.text
    next_page = client.get('/api/v1/system/activity', params={'limit': 1, 'cursor': first['next_cursor']})
    assert next_page.status_code == 200 and next_page.json()['data']['items'][0]['id'] != first['items'][0]['id']
    assert any(row['outcome'] == 'recorded' for row in client.get('/api/v1/system/activity?kind=login').json()['data']['items'])
    assert client.get('/api/v1/system/activity?from_date=2026-10-25&to_date=2026-10-24').status_code == 422
    assert client.get('/api/v1/system/activity', params={'kind': 'cache', 'cursor': first['next_cursor']}).status_code == 422
    assert client.get('/api/v1/system/activity?to_date=9999-12-31').status_code == 200


def test_activity_non_admin_cannot_read_other_accounts(client):
    headers = login(client)
    client.put('/api/v1/me/preferences', json=body(), headers=key(headers))
    new_user()
    with TestClient(app, base_url='http://127.0.0.1:8080') as other:
        authenticate(other, 'second')
        rows = other.get('/api/v1/system/activity').json()['data']['items']
        assert rows and all(row['actor'] == 'second' for row in rows)
        assert all(row['action'] != 'settings.preferences' for row in rows)
