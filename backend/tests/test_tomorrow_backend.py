"""Disposable-PostgreSQL workflow tests. All model calls use synthetic transports."""
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.ai import protocols
from app.ai import worker as ai_worker
from app.ai.models import AiRun
from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.models import Job, Provider, User
from app.tomorrow import jobs, render, service
from app.tomorrow.models import PlanReport, PlanRun, PlanSettings, PlanValidation
from app.worker import claim, process
from tests.test_ai_backend import enable_feature, login, mutate, news, ready_model

DAY = date(2026, 10, 23)
TARGET = date(2026, 10, 26)


@pytest.fixture
def dossier(client, monkeypatch):
    headers, owner = login(client)
    identity = news(owner)
    with Session(engine()) as db:
        evidence = db.get(EvidenceVersion, identity)
        source = db.get(Provider, evidence.source_id)
        ref = dict(id=str(identity), content_hash=evidence.content_hash, source_id=str(source.id),
                   source_name=source.name, received_at=evidence.received_at.isoformat())
    bundle = dict(source_trade_day=str(DAY), target_trade_day=str(TARGET), target_trade_day_verified=True,
        as_of_time=datetime.now(UTC).isoformat(), ready=True, rule_version='synthetic-deterministic-v1',
        market={'state': 'complete', 'indices': [{'name': '合成指数', 'close': '12.50'}], 'evidence_refs': [str(identity)]},
        sectors=[{'code': 'BK1234.DC', 'name': '合成概念', 'evidence_refs': [str(identity)]}],
        stocks=[{'code': '600001.SH', 'name': '合成标的', 'facts': {'close': {'value': '12.50', 'unit': '元',
                    'evidence': [{'id': str(identity), 'source_row': 0, 'fields': ['close']}]}},
                 'calculations': {}, 'candidate_reasons': ['合成候选'], 'evidence_refs': [str(identity)]}],
        events=[], scenarios=[], hotwords={'financial': [], 'social': []},
        evidence_refs=[ref], coverage=[], limitations=[])
    monkeypatch.setattr('app.tomorrow.data.build_bundle', lambda *args, **kwargs: bundle)
    monkeypatch.setattr('app.market.review_store.available_trade_days', lambda *args, **kwargs: [DAY])
    monkeypatch.setattr(service, 'calendar_days', lambda *args: [DAY, TARGET])
    return headers, owner, bundle, ref


def create(client, headers, **changes):
    return mutate(client, 'POST', '/api/v1/tomorrow/runs',
                  dict(source_trade_day=str(DAY), use_ai=False, **changes), headers)


def build(client, headers):
    response = create(client, headers)
    assert response.status_code == 201, response.text
    job = claim()
    with Session(engine()) as db:
        details = [(j.kind, j.state, j.attempts, j.available_at.isoformat(), datetime.now(UTC).isoformat()) for j in db.scalars(select(Job))]
    assert job is not None, details
    assert job.kind == 'tomorrow.build'
    process(job)
    run = client.get('/api/v1/tomorrow/runs/' + response.json()['data']['id']).json()['data']
    report_response = client.get('/api/v1/tomorrow/reports/' + str(run['report_id']))
    assert report_response.status_code == 200, report_response.text
    return run, report_response.json()['data']


def test_gets_are_read_only_and_settings_are_private(client, dossier):
    headers, owner, _, _ = dossier
    with Session(engine()) as db:
        before = db.scalar(select(func.count()).select_from(Job))
    assert client.get('/api/v1/tomorrow/settings').json()['data']['revision'] == 0
    assert client.get('/api/v1/tomorrow/runs').json()['data']['items'] == []
    evaluation = client.get('/api/v1/tomorrow/evaluation')
    assert evaluation.status_code == 200, evaluation.text
    assert evaluation.json()['data']['progress']['eligible_shadow_target_day_count'] == 0
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == before
    saved = mutate(client, 'PATCH', '/api/v1/tomorrow/settings', {'expected_revision': 0, 'cash': 1000, 'enabled': True}, headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()['data']['revision'] == 1
    assert mutate(client, 'PATCH', '/api/v1/tomorrow/settings', {'expected_revision': 0, 'cash': 1}, headers).status_code == 412
    with Session(engine()) as db:
        assert db.get(PlanSettings, owner).profile['cash'] == 1000


def test_rule_workflow_eight_chapters_and_idempotence(client, dossier):
    headers, _, _, _ = dossier
    run, report = build(client, headers)
    assert run['state'] == 'succeeded'
    assert len(report['sections']) == 8
    assert report['status'] == 'draft'
    assert report['dossier']['snapshot_id']
    stock_section = next(s for s in report['sections'] if s['key'] == 'stocks')
    assert stock_section['rows'][0]['收盘（元）'] == '12.50'
    assert stock_section['evidence_refs']
    assert report['dossier']['scenarios'][0]['status'] == 'insufficient_data'
    objects = client.get(f"/api/v1/tomorrow/runs/{run['id']}/objects?kind=stock&limit=1").json()['data']
    assert objects['total'] == 1
    assert objects['items'][0]['facts']['close']['value'] == '12.50'
    assert objects['items'][0]['facts']['close']['evidence']
    assert create(client, headers).json()['data']['id'] == run['id']
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AiRun)) == 0


def test_manual_revision_updates_shared_object_without_rewriting_original(client, dossier):
    headers, _, _, ref = dossier
    _, report = build(client, headers)
    original_hash = report['content_hash']
    revision = mutate(client, 'POST', f"/api/v1/tomorrow/reports/{report['id']}/revisions",
        {'expected_revision': 1, 'reason': '合成修订原因', 'judgments': [{'object_type': 'stock', 'object_id': '600001.SH',
          'judgment': '待确认的人工判断', 'support_refs': [ref['id']]}]}, headers)
    assert revision.status_code == 201, revision.text
    updated = revision.json()['data']
    assert updated['revision_number'] == 2
    assert updated['dossier']['watchlist'][0]['judgment'] == '待确认的人工判断'
    assert client.get(f"/api/v1/tomorrow/reports/{report['id']}").json()['data']['content_hash'] == original_hash
    stale = mutate(client, 'POST', f"/api/v1/tomorrow/reports/{report['id']}/confirm", {'expected_revision': 1}, headers)
    assert stale.status_code == 412
    confirmed = mutate(client, 'POST', f"/api/v1/tomorrow/reports/{updated['id']}/confirm", {'expected_revision': 2}, headers)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()['data']['status'] == 'confirmed'
    summary_edit = mutate(client, 'POST', f"/api/v1/tomorrow/reports/{updated['id']}/revisions",
        {'expected_revision': 2, 'reason': '仅修订摘要', 'summary': '人工核对后的摘要'}, headers)
    assert summary_edit.status_code == 201, summary_edit.text
    retained = summary_edit.json()['data']
    assert retained['judgments'][0]['judgment'] == '待确认的人工判断'
    removal = mutate(client, 'POST', f"/api/v1/tomorrow/reports/{retained['id']}/revisions",
        {'expected_revision': 3, 'reason': '撤回这条人工判断，原稿保留', 'judgments': []}, headers)
    assert removal.status_code == 201, removal.text
    cleared = removal.json()['data']
    assert cleared['judgments'] == []
    assert cleared['dossier']['watchlist'][0].get('judgment_authorship') != 'manual'
    assert client.get(f"/api/v1/tomorrow/reports/{updated['id']}").json()['data']['status'] == 'confirmed'


def test_unverified_target_and_missing_core_cannot_confirm(client, dossier):
    headers, _, bundle, _ = dossier
    bundle['target_trade_day_verified'] = False
    _, report = build(client, headers)
    assert mutate(client, 'POST', f"/api/v1/tomorrow/reports/{report['id']}/confirm", {'expected_revision': 1}, headers).status_code == 409


def test_future_cutoff_and_target_rejected(client, dossier):
    headers, _, _, _ = dossier
    result = create(client, headers, as_of_time=(datetime.now(UTC) + timedelta(days=1)).isoformat())
    assert result.status_code == 422
    assert create(client, headers, target_trade_day='2026-10-24').status_code == 422


def test_original_evidence_cannot_stand_in_for_next_day_confirmation(client, dossier, monkeypatch):
    headers, _, _, ref = dossier
    _, report = build(client, headers)
    response = mutate(client, 'POST', f"/api/v1/tomorrow/reports/{report['id']}/confirm", {'expected_revision': 1}, headers)
    assert response.status_code == 200
    # Evidence is received on the real test clock. Advance beyond the frozen
    # report and target session instead of rewinding to a dated wall clock,
    # which correctly makes the original report inaccessible before this guard.
    target_session = datetime.fromisoformat(report['target_trade_day'] + 'T09:30:00+08:00')
    validation_clock = max(datetime.now(UTC), datetime.fromisoformat(report['as_of_time']),
                           target_session) + timedelta(seconds=1)
    assert datetime.fromisoformat(ref['received_at']) <= datetime.fromisoformat(report['as_of_time']) < validation_clock
    monkeypatch.setattr(service, 'now', lambda db: validation_clock)
    assert client.get(f"/api/v1/tomorrow/reports/{report['id']}").status_code == 200
    response = mutate(client, 'POST', f"/api/v1/tomorrow/reports/{report['id']}/validations",
        {'scenario_id': '600001.SH:manual', 'status': 'confirmed', 'comment': '不能用昨晚材料确认今天', 'evidence_refs': [ref['id']]}, headers)
    assert response.status_code == 422, response.text
    assert response.json()['error']['code'] == 'target_evidence_required'
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(PlanValidation).where(
            PlanValidation.report_id == uuid.UUID(report['id']))) == 0


def test_revoked_external_private_material_hides_report(client, dossier):
    headers, _, _, ref = dossier
    _, report = build(client, headers)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, uuid.UUID(ref['source_id']))
        provider.enabled = False
    assert client.get(f"/api/v1/tomorrow/reports/{report['id']}").status_code == 409


def test_viewer_and_other_owner_cannot_mutate_or_read_reports(client, dossier):
    headers, _, _, _ = dossier
    run, report = build(client, headers)
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(User).where(User.username == 'owner'))
        owner.role = 'viewer'
    assert create(client, headers).status_code == 403
    client.delete('/api/v1/session', headers=headers)
    from app.auth import hasher
    with Session(engine()) as db, db.begin():
        db.add(User(username='other', password_hash=hasher.hash('synthetic-other-password'), role='researcher'))
    logged_in = client.post('/api/v1/session', json={'username': 'other', 'password': 'synthetic-other-password'},
                            headers={'Origin': 'http://127.0.0.1:8080'}).json()['data']
    other_headers = {'Origin': 'http://127.0.0.1:8080', 'X-CSRF-Token': logged_in['csrf_token']}
    assert client.get('/api/v1/tomorrow/runs/' + run['id']).status_code == 404
    assert client.get('/api/v1/tomorrow/reports/' + report['id']).status_code == 404
    assert mutate(client, 'POST', f"/api/v1/tomorrow/reports/{report['id']}/confirm", {'expected_revision': 1}, other_headers).status_code == 404


def test_report_and_validation_tables_are_append_only_at_application_role(client, dossier):
    _, _, _, _ = dossier
    import os

    from sqlalchemy import create_engine
    app_engine = create_engine(os.environ['JL_TEST_APP_URL'])
    with app_engine.connect() as connection:
        for table in ('tomorrow_reports', 'tomorrow_confirmations', 'tomorrow_validations'):
            assert connection.scalar(text("SELECT has_table_privilege(current_user, :table, 'UPDATE')"), {'table': table}) is False
            assert connection.scalar(text("SELECT has_table_privilege(current_user, :table, 'DELETE')"), {'table': table}) is False
    app_engine.dispose()


def test_chinese_adjacent_and_fullwidth_numbers_require_fact_bindings():
    run = SimpleNamespace(input_data={'fact_catalog': {'s.close': {'value': '12.50', 'object': '合成股', 'evidence_group': 'g'}},
                                     'evidence_groups': {'g': ['a']}})
    for invalid in ('收盘999元', '涨幅99%', '价格１２３元', '涨幅百分之十', '主力净流入三亿元'):
        with pytest.raises(Exception, match='数字'):
            jobs.validate_stage_result(run, {'summary': invalid, 'items': [{'title': '观察', 'text': '待观察', 'evidence_refs': ['a']}]})
    result = jobs.validate_stage_result(run, {'summary': '待观察', 'items': [{'title': '价量', 'text': '{{fact:s.close}}', 'evidence_refs': ['a']}]})
    assert result['items'][0]['text'] == '合成股：12.50'


@pytest.mark.parametrize('edit_during_audit', [False, True])
def test_staged_ai_uses_existing_worker_and_preserves_input(client, dossier, monkeypatch, tmp_path, edit_during_audit):
    headers, owner, _, ref = dossier
    root, master = tmp_path / 'credentials', tmp_path / 'master.key'
    root.mkdir()
    master.write_bytes(Fernet.generate_key())
    monkeypatch.setattr(settings(), 'ai_secret_directory', str(root))
    monkeypatch.setattr(settings(), 'ai_master_key_file', str(master))
    connection = ready_model(client, headers, monkeypatch)
    enable_feature(client, headers, connection, 'plan_generation')
    saved = mutate(client, 'PATCH', '/api/v1/tomorrow/settings', {'expected_revision': 0, 'ai_source_ids': [ref['source_id']]}, headers)
    assert saved.status_code == 200
    calls = []
    def fake(endpoint, protocol, key, model, prompt=None, *, on_send, **kwargs):
        on_send()
        material = json.loads(prompt.split('\n', 1)[1])
        calls.append(material)
        role = material['input']['research_role']
        item = {'title': '证据研究', 'text': '原始资料支持观察，仍需确认反证。', 'evidence_refs': [ref['id']]}
        response = {'summary': '研究待确认', 'items': [item]}
        if role == 'conditional_plan':
            item.update(object_type='stock', object_id='600001.SH', next_checks=[dict(window='09:25–09:59',
                subject='合成标的与合成概念', support='同组多数保持开盘方向且个股承接未破坏，记录人工观察。',
                invalidation='同组同步转弱且个股承接消失。', missing_data='缺开盘实际观察则暂停确认。')])
        if role == 'review_write':
            response.update(audit_status='passed', audit_issues=[])
            if edit_during_audit:
                current = client.get('/api/v1/tomorrow/runs/' + parent_run_id).json()['data']
                edited = mutate(client, 'POST', '/api/v1/tomorrow/reports/' + current['report_id'] + '/revisions',
                    {'expected_revision': 1, 'reason': '模型审查期间保留本人最新意见', 'summary': '人工修订后仍待核验'}, headers)
                assert edited.status_code == 201, edited.text
        return protocols.Reply(json.dumps(response, ensure_ascii=False), 20, 20)
    monkeypatch.setattr(protocols, 'exchange', fake)
    response = mutate(client, 'POST', '/api/v1/tomorrow/runs', {'source_trade_day': str(DAY), 'use_ai': True}, headers)
    assert response.status_code == 201, response.text
    parent_run_id = response.json()['data']['id']
    parent = claim()
    process(parent)
    for _ in range(4):
        stage_job = ai_worker.claim()
        assert stage_job is not None
        ai_worker.process(stage_job)
        jobs.advance_runs()
    result = client.get('/api/v1/tomorrow/runs/' + response.json()['data']['id']).json()['data']
    assert result['state'] == ('partial' if edit_during_audit else 'succeeded'), result
    assert len(calls) == 4
    report = client.get('/api/v1/tomorrow/reports/' + result['report_id']).json()['data']
    assert len(report['ai_completed_roles']) == 4
    assert len(report['sections']) == 8
    assert report['dossier']['stocks'][0]['facts']['close']['value'] == '12.50'
    assert report['audit_binding']['passed'] is (not edit_during_audit)
    if edit_during_audit:
        assert report['summary'] == '人工修订后仍待核验'
    assert report['dossier']['scenarios'][0]['id'] == '600001.SH:research'


def test_portfolio_constraints_and_missing_prices_do_not_create_allocations():
    bundle = {'stocks': [{'code': '600001.SH', 'facts': {'close': {'value': '10'}}}]}
    profile = {'cash': 100, 'holdings': [{'code': '600001.SH', 'quantity': 100, 'sellable_quantity': 0, 'sector_codes': ['BK1', 'BK1']}],
               'risk_policy': {'approved': True, 'max_stock_weight': .2, 'max_sector_weight': .5, 'max_total_weight': .8}}
    result = render.portfolio(bundle, profile)
    assert result['state'] == 'partial'
    assert len(result['limitations']) == 3
    assert result['sector_exposure']['BK1'] == result['allocations'][0]['weight']
    assert render.portfolio({'stocks': []}, profile)['state'] == 'unavailable'


def test_stock_projection_batches_do_not_consume_fact_budget_for_unread_objects(client, dossier):
    from copy import deepcopy
    headers, _, bundle, ref = dossier
    template = bundle['stocks'][0]
    bundle['stocks'] = [dict(deepcopy(template), code=f'{600001+i}.SH', name=f'合成标的{i}') for i in range(40)]
    _, report = build(client, headers)
    mutate(client, 'PATCH', '/api/v1/tomorrow/settings', {'expected_revision': 0, 'ai_source_ids': [ref['source_id']]}, headers)
    with Session(engine()) as db:
        run = db.get(PlanRun, uuid.UUID(report['run_id']))
        run.config_snapshot = dict(run.config_snapshot, profile={'ai_source_ids': [ref['source_id']]})
        first, _ = jobs.project_input(db, run, 'sectors_stocks')
        second, _ = jobs.project_input(db, run, 'sectors_stocks', 15)
        assert len(first['stocks']) == len(second['stocks']) == 15
        assert {s['code'] for s in first['stocks']}.isdisjoint({s['code'] for s in second['stocks']})
        assert any(fact['value'] == '12.50' for fact in second['fact_catalog'].values())
        assert len(jobs.workflow_steps(run)) == 6


def test_postclose_readback_is_immutable_idempotent_and_does_not_confirm_open_conditions(client, dossier, monkeypatch):
    from app.tomorrow import verification
    headers, owner, _, _ = dossier
    _, report = build(client, headers)
    assert mutate(client, 'POST', f"/api/v1/tomorrow/reports/{report['id']}/confirm", {'expected_revision': 1}, headers).status_code == 200
    # news() intentionally subtracts five seconds from its input clock.
    received_after_report=datetime.now(UTC)+timedelta(minutes=1)
    with monkeypatch.context() as patch:
        patch.setattr('tests.test_ai_backend.datetime', SimpleNamespace(now=lambda tz: received_after_report))
        evidence_id = news(owner)
    with Session(engine()) as db:
        evidence = db.get(EvidenceVersion, evidence_id)
        ref = {'id': str(evidence.id), 'content_hash': evidence.content_hash, 'source_id': str(evidence.source_id)}
    def load(db, dataset, day, **kwargs):
        return dict(state='complete', evidence_refs=[ref], rows=[dict(ts_code='600001.SH', close='15.00', pct_chg='20.00', up_limit='15.00', down_limit='10.00', limit='U', limit_times=2)])
    monkeypatch.setattr(verification, 'load_dataset', load)
    with Session(engine()) as db, db.begin():
        identity = uuid.UUID(report['id'])
        assert verification.append_readback(db, identity, datetime(2026, 10, 26, 8, tzinfo=UTC)) == 0
        assert verification.append_readback(db, identity, received_after_report+timedelta(days=1)) == 1
        db.flush()
        assert verification.append_readback(db, identity, received_after_report+timedelta(days=1,hours=1)) == 0
        result = db.scalar(select(PlanValidation).where(PlanValidation.report_id == identity))
        assert result.status == 'insufficient_data'
        assert result.observation['facts']['close'] == '15.00'
        assert db.get(PlanReport, identity).content_hash == report['content_hash']


def test_collection_permission_failure_finishes_the_job_without_crashing_worker(monkeypatch):
    from contextlib import nullcontext

    from app import worker
    from app.market.protocol import SourceFailure
    job = SimpleNamespace(kind='tomorrow.collect', id=uuid.uuid4(), lease_token=uuid.uuid4())
    monkeypatch.setattr(worker, 'lease_renewal', lambda job: nullcontext())
    monkeypatch.setattr('app.tomorrow.prepare.process_preparation', lambda job: (_ for _ in ()).throw(SourceFailure('configuration_changed')))
    recorded = []
    monkeypatch.setattr(worker, 'finish', lambda identity, token, **kw: recorded.append((identity, token, kw)))
    worker.process(job)
    assert recorded == [(job.id, job.lease_token, {'error_code': 'configuration_changed'})]
