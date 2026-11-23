"""Retire legacy edition AI jobs while preserving rule editions and history."""
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from test_data_sources import login
from test_news_center import save, source, started

from app.ai.models import AiAttempt
from app.ai.worker import claim as ai_claim
from app.ai.worker import process as ai_process
from app.db import engine
from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion
from app.models import Job, Provider, User
from app.news_center.editions import at, create_daily, last_daily_day, schedule_editions
from app.news_center.jobs import _close_terminated_attempts, process_ai
from app.news_center.models import NewsAiVersion, NewsCenterState, NewsEdition
from app.worker import claim as data_claim


@pytest.fixture(autouse=True)
def ai_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith('JL_MODEL_') or name == 'JL_NEWS_AI_ALLOWED_FAMILIES':
            monkeypatch.delenv(name)
    def enable(*, allowed='chinanews_finance'):
        monkeypatch.setenv('JL_MODEL_BASE_URL', 'https://model.invalid/v1')
        monkeypatch.setenv('JL_MODEL_NAME', 'synthetic-job-model')
        monkeypatch.setenv('JL_MODEL_ENABLED', 'true')
        monkeypatch.setenv('JL_MODEL_API_KEY', 'synthetic-job-model-credential')
        monkeypatch.setenv('JL_NEWS_AI_ALLOWED_FAMILIES', allowed)
    return enable


def create_issue(client, *, second_source=False):
    headers = login(client)
    origin = source(client, headers)
    other_origin = source(client, headers, 'rthk_finance') if second_source else None
    current, day = datetime.now(UTC), last_daily_day(datetime.now(UTC))
    received = at(day - timedelta(days=1), 12)
    with Session(engine()) as db, db.begin():
        save(db, origin, 'ai-job', '合成公司公布项目进展', received, received, summary='合成公司公布项目进展，仅用于测试。')
        if other_origin:
            save(db, other_origin, 'second', '合成海外政策资料', received, received, summary='第二来源资料，仅用于输入范围测试。')
        edition = create_daily(db, started(db, day), day, current)
        owner = db.scalar(select(User).where(User.username == 'owner'))
        return {'id': edition.id, 'day': day, 'hash': edition.content_hash, 'manifest': edition.manifest,
                'source_id': origin, 'other_source_id': other_origin, 'headers': headers, 'owner_id': owner.id}


@pytest.fixture
def issue(client, ai_environment):
    ai_environment()
    return create_issue(client)


def legacy_job(issue, *, state='queued', attempts=0):
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        job = Job(id=uuid.uuid4(), owner_id=issue['owner_id'], kind='news.ai', state=state, attempts=attempts,
            payload={'edition_id': str(issue['id']), 'manifest_hash': issue['hash']}, available_at=datetime.now(UTC),
            lease_token=uuid.uuid4() if state == 'running' else None,
            lease_until=datetime.now(UTC) - timedelta(seconds=1) if state == 'running' else None)
        db.add(job)
    return job


def historical_version(issue, *, state='complete', legacy=False, input_subset=False):
    manifest = issue['manifest']['items'][:1] if input_subset else issue['manifest']['items']
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        items = []
        for item in manifest[:1]:
            evidence = db.get(EvidenceVersion, uuid.UUID(item['evidence_id']))
            items.append(dict(article_id=item['article_id'], evidence_id=item['evidence_id'],
                              summary=evidence.payload.get('summary'), category='公司公告', selected=True))
        journal = [{key: item[key] for key in ('article_id', 'evidence_id', 'content_hash')} for item in manifest]
        result = {'lead': '历史合成资料导读', 'items': items}
        if not legacy:
            result['_input_manifest'] = journal
        row = NewsAiVersion(id=uuid.uuid4(), edition_id=issue['id'], created_at=datetime.now(UTC) - timedelta(seconds=5),
            completed_at=datetime.now(UTC) if state == 'complete' else None, state=state, connection_id='old-environment',
            model='historical-model', prompt_version='historical-prompt', input_hash=content_hash(journal),
            result=result if state == 'complete' else {'_input_manifest': journal}, usage={'total_tokens': 15} if state == 'complete' else None)
        db.add(row)
    return row


def assert_rules_preserved(client, issue):
    response = client.get('/api/v1/news/editions/' + str(issue['id']))
    assert response.status_code == 200, response.text
    with Session(engine()) as db:
        edition = db.get(NewsEdition, issue['id'])
        assert edition.content_hash == issue['hash'] and edition.manifest == issue['manifest']
    return response.json()['data']


def test_even_enabled_legacy_configuration_never_queues_editions(issue, client):
    with Session(engine()) as db, db.begin():
        again = create_daily(db, db.get(NewsCenterState, 1), issue['day'], datetime.now(UTC))
        assert again.id == issue['id']
        schedule_editions(db, datetime.now(UTC))
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == 'news.ai')) == 0
    assert assert_rules_preserved(client, issue)['ai'] is None


@pytest.mark.parametrize('allowed', ['', 'another-source', 'chinanews_finance'])
def test_allowlist_does_not_enable_pending_edition_ai(client, ai_environment, allowed):
    ai_environment(allowed=allowed)
    create_issue(client)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == 'news.ai')) == 0


def test_only_independent_worker_claims_and_cancels_old_queue(issue, monkeypatch, client):
    queued = legacy_job(issue)
    monkeypatch.setattr('app.news_center.ai.enhance_news', lambda *a, **kw: pytest.fail('Pending feature sent a request'))
    assert data_claim() is None
    claimed = ai_claim()
    assert claimed and claimed.id == queued.id
    ai_process(claimed)
    ai_process(claimed)
    with Session(engine()) as db:
        active = db.get(Job, queued.id)
        assert active.state == 'cancelled' and active.error_code == 'feature_not_implemented'
        assert active.lease_token is None and active.lease_until is None and active.completed_at
        assert db.scalar(select(func.count()).select_from(AiAttempt)) == 0
        assert db.scalar(select(func.count()).select_from(NewsAiVersion)) == 0
    assert ai_claim() is None and assert_rules_preserved(client, issue)['ai'] is None


def test_expired_legacy_attempt_becomes_unknown_without_resend(issue, monkeypatch):
    version, job = historical_version(issue, state='running'), legacy_job(issue, state='running', attempts=3)
    monkeypatch.setattr('app.news_center.ai.enhance_news', lambda *a, **kw: pytest.fail('Expired request resent'))
    assert data_claim() is None and ai_claim() is None
    with Session(engine()) as db:
        active, receipt = db.get(Job, job.id), db.get(NewsAiVersion, version.id)
        assert active.state == 'cancelled' and active.error_code == 'feature_not_implemented'
        assert receipt.state == 'failed' and receipt.error_code == 'news_ai_outcome_unknown'
        assert receipt.result == version.result


@pytest.mark.parametrize('change', ['expired', 'replaced-token'])
def test_stale_compatibility_handler_cannot_finish_or_send(issue, monkeypatch, change):
    queued = legacy_job(issue)
    claimed = ai_claim()
    with Session(engine()) as db, db.begin():
        active = db.get(Job, queued.id)
        if change == 'expired':
            active.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        else:
            active.lease_token = uuid.uuid4()
    monkeypatch.setattr('app.news_center.ai.enhance_news', lambda *a, **kw: pytest.fail('Stale handler sent request'))
    process_ai(claimed)
    with Session(engine()) as db:
        assert db.get(Job, queued.id).state == 'running'
        assert db.scalar(select(func.count()).select_from(NewsAiVersion)) == 0


def test_cancelling_old_job_preserves_complete_history_and_rule_manifest(client, issue):
    version, queued = historical_version(issue), legacy_job(issue)
    ai_process(ai_claim())
    with Session(engine()) as db:
        stored = db.get(NewsAiVersion, version.id)
        assert stored.state == 'complete' and stored.result == version.result and stored.usage == version.usage
        assert db.get(Job, queued.id).state == 'cancelled'
    assert assert_rules_preserved(client, issue)['ai']['model'] == 'historical-model'


@pytest.mark.parametrize('legacy', [False, True])
def test_omitted_input_permission_revocation_hides_historical_enhancement(client, ai_environment, legacy):
    ai_environment()
    issue = create_issue(client, second_source=True)
    historical_version(issue, legacy=legacy)
    assert assert_rules_preserved(client, issue)['ai'] is not None
    with Session(engine()) as db, db.begin():
        omitted_source = db.get(Provider, issue['other_source_id'])
        omitted_source.rights = {**omitted_source.rights, 'cache': 'denied'}
    assert assert_rules_preserved(client, issue)['ai'] is None


def test_unrelated_source_revocation_preserves_precisely_journaled_history(client, ai_environment):
    ai_environment()
    issue = create_issue(client, second_source=True)
    version = historical_version(issue, input_subset=True)
    journal_ids = {item['evidence_id'] for item in version.result['_input_manifest']}
    with Session(engine()) as db, db.begin():
        unrelated = next(item for item in issue['manifest']['items'] if item['evidence_id'] not in journal_ids)
        origin = db.get(EvidenceVersion, uuid.UUID(unrelated['evidence_id'])).source_id
        provider = db.get(Provider, origin)
        provider.rights = {**provider.rights, 'display': 'denied'}
    assert assert_rules_preserved(client, issue)['ai'] is not None


def test_invalid_historical_input_journal_fails_closed(client, issue):
    version = historical_version(issue)
    with Session(engine()) as db, db.begin():
        stored = db.get(NewsAiVersion, version.id)
        stored.result = {**stored.result, '_input_manifest': [dict(article_id=str(uuid.uuid4()),
            evidence_id=issue['manifest']['items'][0]['evidence_id'], content_hash='0' * 64)]}
    assert assert_rules_preserved(client, issue)['ai'] is None


def test_retirement_and_receipt_change_roll_back_atomically(issue):
    version, queued = historical_version(issue, state='running'), legacy_job(issue)
    claimed = ai_claim()
    def reject(mapper, connection, target):
        if target.state == 'failed':
            raise RuntimeError('synthetic receipt storage failure')
    event.listen(NewsAiVersion, 'before_update', reject)
    try:
        with pytest.raises(RuntimeError, match='synthetic receipt storage failure'):
            process_ai(claimed)
    finally:
        event.remove(NewsAiVersion, 'before_update', reject)
    with Session(engine()) as db:
        assert db.get(Job, queued.id).state == 'running' and db.get(NewsAiVersion, version.id).state == 'running'


def test_previously_terminal_job_closes_running_receipt_without_network(issue):
    version, queued = historical_version(issue, state='running'), legacy_job(issue)
    with Session(engine()) as db, db.begin():
        db.get(Job, queued.id).state = 'cancelled'
    with Session(engine()) as db, db.begin():
        _close_terminated_attempts(db)
    with Session(engine()) as db:
        receipt = db.get(NewsAiVersion, version.id)
        assert receipt.state == 'failed' and receipt.error_code == 'news_ai_outcome_unknown'
