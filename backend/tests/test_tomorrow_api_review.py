"""API selection through the real plan endpoint and workers using an offline model."""
import json
import uuid

import pytest

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import protocols, worker as ai_worker
from app.ai.models import AiAttempt
from app.db import engine
from app.models import User
from app.tomorrow import jobs
from app.tomorrow.models import PlanRun, PlanSettings
from app.worker import claim, process
from tests.test_ai_backend import SECRET, body, mock_model, mutate, patch_input, private_vault
from tests.test_tomorrow_backend import DAY, TARGET, build, dossier


def research_reply(role, ref):
    item = {'title': '证据研究', 'text': '原始资料支持观察，仍需确认反证。', 'evidence_refs': [ref['id']]}
    response = {'summary': '研究待确认', 'items': [item]}
    if role == 'conditional_plan':
        item.update(object_type='stock', object_id='600001.SH', next_checks=[dict(window='09:25–09:59',
            subject='合成标的与合成概念', support='同组多数保持开盘方向且个股承接未破坏，记录人工观察。',
            invalidation='同组同步转弱且个股承接消失。', missing_data='缺开盘实际观察则暂停确认。')])
    if role == 'review_write':
        response.update(audit_status='passed', audit_issues=[])
    return protocols.Reply(json.dumps(response, ensure_ascii=False), 20, 20)


def test_click_tested_inactive_api_and_complete_report(client, dossier, monkeypatch):
    headers, owner, _, ref = dossier
    connection = mutate(client, 'POST', '/api/v1/integrations',
        body(name='DeepSeek 测试连接', model='deepseek-offline'), headers).json()['data']

    def plan_settings():
        return client.get('/api/v1/tomorrow/settings').json()['data']

    review = {'connection_id': connection['id'], 'connection_revision': connection['revision'],
              'source_ids': [ref['source_id']], 'enable_ai': True, 'enable_connection': True}
    payload = {'source_trade_day': str(DAY), 'target_trade_day': str(TARGET), 'slot': 'manual',
               'regenerate': True, 'use_ai': True, 'expected_settings_revision': 0, 'api_review': review}

    def submit(**updates):
        return mutate(client, 'POST', '/api/v1/tomorrow/runs', {**payload, 'api_review': {**review, **updates}}, headers)

    assert plan_settings()['api_connections'][0]['state'] == 'needs_test'
    assert submit().json()['error']['code'] == 'model_not_ready'
    probes = []
    mock_model(monkeypatch, probes)
    probe = mutate(client, 'POST', '/api/v1/integrations/probe', {**patch_input(connection), 'id': connection['id']}, headers)
    assert probe.json()['data']['status'] == 'success' and len(probes) == 1
    assert plan_settings()['api_connections'] == [dict(id=connection['id'], model=connection['model'],
        name=connection['name'], revision=connection['revision'], state='needs_enable')]
    assert not plan_settings()['model_options']
    assert submit(enable_connection=False).json()['error']['code'] == 'api_not_enabled'
    assert submit(connection_revision=connection['revision'] + 1).status_code == 412
    assert submit(source_ids=[str(uuid.uuid4())]).json()['error']['code'] == 'input_permission_changed'
    assert not client.get('/api/v1/ai/settings').json()['data']['enabled']
    assert plan_settings()['api_connections'][0]['state'] == 'needs_enable'
    assert mutate(client, 'POST', '/api/v1/tomorrow/runs', {**payload, 'model_connection_id': connection['id']}, headers).status_code == 422
    assert mutate(client, 'POST', '/api/v1/tomorrow/runs', {**payload, 'use_ai': False}, headers).status_code == 422
    with Session(engine()) as db, db.begin():
        db.get(User, owner).role = 'researcher'
    assert submit().status_code == 403
    with Session(engine()) as db, db.begin():
        db.get(User, owner).role = 'admin'

    calls = []
    def generate(endpoint, protocol, key, model, prompt=None, *, on_send, **kwargs):
        assert key == SECRET and model == connection['model'] and endpoint == connection['endpoint']
        on_send()
        role = json.loads(prompt.split('\n', 1)[1])['input']['research_role']
        calls.append(role)
        return research_reply(role, ref)

    monkeypatch.setattr(protocols, 'exchange', generate)
    review.pop('source_ids')
    materials = plan_settings()['review_materials']
    assert materials['count'] >= 1
    assert submit(materials_digest='0' * 64).status_code == 412
    review['materials_digest'] = materials['digest']
    replay_key = str(uuid.uuid4())
    response = mutate(client, 'POST', '/api/v1/tomorrow/runs', payload, headers, key=replay_key)
    assert response.status_code == 201, response.text
    run = response.json()['data']
    assert run['requested_model'] == connection['model'] and run['state'] == 'queued'
    assert mutate(client, 'POST', '/api/v1/tomorrow/runs', payload, headers, key=replay_key).json()['data']['id'] == run['id']
    assert plan_settings()['api_connections'][0]['state'] == 'ready'
    assert client.get('/api/v1/ai/settings').json()['data']['default_connection_id'] is None
    process(claim())
    for _ in range(4):
        stage = ai_worker.claim()
        assert stage is not None
        ai_worker.process(stage)
        jobs.advance_runs()
    result = client.get('/api/v1/tomorrow/runs/' + run['id']).json()['data']
    assert result['state'] == 'succeeded', result
    report = client.get('/api/v1/tomorrow/reports/' + result['report_id']).json()['data']
    assert len(report['sections']) == 8 and report['generation_models'] == [connection['model']]
    assert calls == ['market_events', 'sectors_stocks', 'conditional_plan', 'review_write']
    with Session(engine()) as db:
        frozen = db.get(PlanRun, uuid.UUID(run['id'])).config_snapshot
        assert frozen['one_time_sources'] and frozen['ai']['fallback_connection_id'] is None
        assert ref['source_id'] in frozen['profile']['ai_source_ids']
        assert frozen['ai']['plan_connection_id'] == connection['id']
        assert db.get(PlanSettings, owner) is None
        assert len(db.scalars(select(AiAttempt).where(AiAttempt.purpose == 'generate')).all()) == 4


@pytest.mark.parametrize("selection", [{"ai":{"plan_model":"account-model"}}, {"requested_model":"api-model"}])
def test_direct_audit_with_partial_consent_only_receives_authorized_facts(client, dossier, selection):
    headers, _, _, ref = dossier
    task, _ = build(client, headers)
    with Session(engine()) as db, db.no_autoflush:
        run = db.get(PlanRun, uuid.UUID(task['id']))
        private = dict(ref, id=str(uuid.uuid4()), source_id=str(uuid.uuid4()), source_name='excluded-source')
        run.config_snapshot = dict(profile={'ai_source_ids':[ref['source_id']]}, one_time_sources=True, **selection)
        run.bundle = {**run.bundle, 'evidence_refs':[ref, private], 'limitations':['excluded-source']}
        material, refs = jobs.project_input(db, run, 'review_write')
        assert all(r['source_id'] == ref['source_id'] for r in refs)
        assert material['fact_catalog'] and not material['audit_scope_complete']
        assert 'excluded-source' not in json.dumps(material)
        assert material['visible_draft'] == {}
