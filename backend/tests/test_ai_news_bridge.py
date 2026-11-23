"""Central controls are authoritative across the concurrently shipped news center."""
import os
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_ai_backend import (
    SECRET,
    enable_feature,
    login,
    mutate,
    news,
    patch_input,
    queue_news,
    ready_model,
    result_reply,
)
from test_ai_backend import private_vault as private_vault
from test_news_ai_jobs import create_issue

from app.ai import protocols, worker
from app.ai.models import AiAttempt, AiRun, Connection
from app.db import engine
from app.models import Job
from app.news_center.ai import NewsModelError, enhance_news


@pytest.fixture(autouse=True)
def legacy_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith('JL_MODEL_') or name == 'JL_NEWS_AI_ALLOWED_FAMILIES':
            monkeypatch.delenv(name)
    monkeypatch.setenv('JL_MODEL_BASE_URL', 'https://models.example.com/v1')
    monkeypatch.setenv('JL_MODEL_NAME', 'synthetic-legacy-model')
    monkeypatch.setenv('JL_MODEL_ENABLED', 'true')
    monkeypatch.setenv('JL_MODEL_API_KEY', SECRET)
    monkeypatch.setenv('JL_NEWS_AI_ALLOWED_FAMILIES', 'chinanews_finance')


def legacy_view(client):
    response = client.get('/api/v1/integrations')
    assert response.status_code == 200, response.text
    return next(item for item in response.json()['data']['items'] if item['name'].startswith('旧环境模型'))


def test_environment_discovery_is_read_only_disabled_and_never_exposes_key(client):
    login(client)
    first, second = legacy_view(client), legacy_view(client)
    assert first['id'] == second['id'] and first['enabled'] is False and first['last_test'] is None
    assert first['credential_origin'] == 'environment' and first['credential_configured'] is True
    response = client.get('/api/v1/integrations')
    assert SECRET not in response.text and 'known_env_ref' not in response.text and 'JL_MODEL_API_KEY' not in response.text
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Connection)) == 0
        assert db.scalar(select(func.count()).select_from(AiAttempt)) == 0


def test_legacy_blank_key_materializes_and_uses_only_central_manual_pipeline(client, monkeypatch):
    headers, owner = login(client)
    old = legacy_view(client)
    saved = mutate(client, 'PATCH', '/api/v1/integrations/' + old['id'], patch_input(old, api_key=''), headers)
    assert saved.status_code == 200, saved.text
    value = saved.json()['data']
    assert value['enabled'] is False and value['credential_origin'] == 'environment'
    calls = []
    def probe(*args, on_send, **kwargs):
        assert args[2] == SECRET
        on_send()
        calls.append('probe')
        return protocols.Reply('连接测试通过。', 8, 5)
    monkeypatch.setattr(protocols, 'exchange', probe)
    tested = mutate(client, 'POST', '/api/v1/integrations/probe', {**patch_input(value), 'id': value['id']}, headers)
    assert tested.status_code == 200 and tested.json()['data']['status'] == 'success', tested.text
    enabled = mutate(client, 'PATCH', '/api/v1/integrations/' + value['id'], patch_input(value, enabled=True), headers)
    assert enabled.status_code == 200, enabled.text
    enable_feature(client, headers, enabled.json()['data'])
    identity = news(owner)
    run = queue_news(client, headers, identity)
    def generate(*args, on_send, **kwargs):
        on_send()
        calls.append('generate')
        return result_reply(identity)
    monkeypatch.setattr(protocols, 'exchange', generate)
    worker.process(worker.claim())
    assert client.get('/api/v1/ai/runs/' + run['id']).json()['data']['state'] == 'succeeded'
    assert calls == ['probe', 'generate']
    with Session(engine()) as db:
        stored = db.get(Connection, uuid.UUID(old['id']))
        assert stored.secret_ref == 'env:model:JL_MODEL_API_KEY'
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == 'news.ai')) == 0
    assert '_credential_fingerprint' not in client.get('/api/v1/integrations').text


def test_direct_probe_materializes_virtual_model_without_a_false_foreign_key_error(client, monkeypatch):
    headers, _ = login(client)
    old = legacy_view(client)
    def fake(*args, on_send, **kwargs):
        on_send()
        return protocols.Reply('测试通过。', 2, 2)
    monkeypatch.setattr(protocols, 'exchange', fake)
    response = mutate(client, 'POST', '/api/v1/integrations/probe', {**patch_input(old), 'id': old['id']}, headers)
    assert response.status_code == 200 and response.json()['data']['status'] == 'success', response.text
    with Session(engine()) as db:
        assert db.get(Connection, uuid.UUID(old['id'])).enabled is False
        assert db.scalar(select(func.sum(AiAttempt.request_count))) == 1


def test_changed_environment_key_requires_new_probe_and_invalidates_old_queue(client, monkeypatch):
    headers, owner = login(client)
    old = legacy_view(client)
    def fake(*args, on_send, **kwargs):
        on_send()
        return protocols.Reply('测试通过。', 2, 2)
    monkeypatch.setattr(protocols, 'exchange', fake)
    response = mutate(client, 'POST', '/api/v1/integrations/probe', {**patch_input(old), 'id': old['id']}, headers)
    assert response.json()['data']['status'] == 'success'
    response = mutate(client, 'PATCH', '/api/v1/integrations/' + old['id'], patch_input(old, enabled=True), headers)
    assert response.status_code == 200, response.text
    enabled = response.json()['data']
    enable_feature(client, headers, enabled)
    queued = queue_news(client, headers, news(owner))
    monkeypatch.setenv('JL_MODEL_API_KEY', 'synthetic-replaced-environment-secret')
    state = legacy_view(client)
    assert not state['credential_configured'] and state['credential_error_code'] == 'credential_changed_requires_test'
    response = mutate(client, 'POST', '/api/v1/integrations/probe', {**patch_input(enabled), 'id': enabled['id']}, headers)
    assert response.json()['data']['status'] == 'success'
    monkeypatch.setattr(protocols, 'exchange', lambda *a, **kw: pytest.fail('Old environment key task resent'))
    worker.process(worker.claim())
    assert client.get('/api/v1/ai/runs/' + queued['id']).json()['data']['state'] == 'cancelled'
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AiAttempt).where(AiAttempt.purpose == 'generate')) == 0


def test_archived_environment_declaration_does_not_reappear(client):
    headers, _ = login(client)
    old = legacy_view(client)
    assert client.delete('/api/v1/integrations/' + old['id'], headers=headers).status_code == 204
    assert all(item['id'] != old['id'] for item in client.get('/api/v1/integrations').json()['data']['items'])


def test_global_switch_never_enables_edition_ai_or_legacy_transport(client, monkeypatch):
    issue = create_issue(client)
    model = ready_model(client, issue['headers'], monkeypatch)
    enable_feature(client, issue['headers'], model)
    status = client.get('/api/v1/news/ai-status')
    assert status.status_code == 200, status.text
    assert status.json()['data'] == {'state': 'disabled', 'connection_id': model['id'], 'model': model['model'], 'reason': 'period_digest_not_implemented'}
    feed = client.get('/api/v1/news/items').json()['data']
    assert feed['ai_status']['reason'] == 'period_digest_not_implemented'
    features = client.get('/api/v1/ai/settings').json()['data']['features']
    assert {row['id'] for row in features if row['implemented']} == {'theme_summary', 'news_summary', 'plan_generation'}
    blocked = mutate(client, 'PATCH', '/api/v1/ai/bindings/period_digest', {'enabled': True, 'connection_id': None, 'fallback_connection_id': None}, issue['headers'])
    assert blocked.status_code == 409
    mutate(client, 'PATCH', '/api/v1/ai/settings', {'enabled': False}, issue['headers'])
    monkeypatch.setattr(protocols, 'exchange', lambda *a, **kw: pytest.fail('Global-off request sent'))
    with pytest.raises(NewsModelError, match='feature_not_implemented'):
        enhance_news([], snapshot_id=str(issue['id']), transport=lambda *a, **kw: pytest.fail('Legacy transport used'))
    assert client.get('/api/v1/news/ai-status').json()['data']['state'] == 'disabled'
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == 'news.ai')) == 0
        assert db.scalar(select(func.count()).select_from(AiRun)) == 0
