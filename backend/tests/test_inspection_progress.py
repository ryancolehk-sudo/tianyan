from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.integrations import inspection_runs
from app.market.protocol import Response
from app.models import Job
from app.worker import claim, process
from test_data_sources import login
from test_system_inspection import URL, post, prepare


def start(client, headers, key):
    response = client.post(URL + '/runs', headers={**headers, 'Idempotency-Key': key}, json={})
    assert response.status_code == 202, response.text
    return response.json()['data']


def status(client, key=None):
    result = client.get(URL + '/progress', params={'operation_key': key} if key else {})
    assert result.status_code == 200, result.text
    return result.json()['data']


def test_worker_handoff_restores_exact_receipt_and_verifies_source(client, monkeypatch):
    headers, source = prepare(client, monkeypatch)
    key = str(uuid4())
    run = start(client, headers, key)
    assert run['phase'] == 'queued' and run['batch'] is None
    assert start(client, headers, str(uuid4()))['id'] == start(client, headers, key)['id'] == run['id']
    assert status(client, str(uuid4()))['run'] is None
    progress = status(client, key)
    assert progress['run']['id'] == run['id']
    assert {s['id'] for s in progress['services'] if s['online']} >= {'api', 'database'}
    with Session(engine()) as db:
        assert db.scalar(select(func.count(Job.id))) == 1
    job = claim('general')
    assert str(job.id) == run['id']
    process(job)
    for operation in [key, None]:
        saved = status(client, operation)['run']
        assert saved['id'] == run['id'] and saved['phase'] == 'submitted'
        row = next(a for a in saved['batch']['actions'] if a['source_id'] == str(source))
        assert row['state'] == 'working' and row['job_id']
    assert client.get(URL).json()['data']['last_repair']['started_at'] == saved['batch']['started_at']
    original = b'<rss><channel><item><title>source verified</title><link>https://example.com/item</link><pubDate>Thu, 02 Jan 2020 10:00:00 GMT</pubDate></item></channel></rss>'
    monkeypatch.setattr('app.market.collect.fetch_public', lambda *_:Response(original, datetime.now(UTC), 2, 200))
    # A mixed-installation test replaces only the inspection worker.
    from app.worker import process as collect
    collect(claim())
    assert next(a for a in status(client)['run']['batch']['actions'] if a['source_id'] == str(source))['state'] == 'verified'


def test_steps_visible_before_commit_and_lost_lease_rolls_back_actions(client, monkeypatch):
    headers, _ = prepare(client, monkeypatch)
    key = str(uuid4())
    run_id = UUID(start(client, headers, key)['id'])
    original = inspection_runs.repair
    seen = []
    def observe(db, owner, *, progress):
        assert status(client, key)['run']['phase'] == 'scanning'
        def report(checked, total, current):
            progress(checked, total, current)
            observed = status(client, key)['run']
            seen.append((observed['checked'], observed['total'], observed['current']))
            assert observed['batch'] is None and observed['phase'] == 'applying'
        batch = original(db, owner, progress=report)
        with Session(engine()) as other, other.begin():
            other.get(Job, run_id).lease_token = uuid4()
        return batch
    monkeypatch.setattr(inspection_runs, 'repair', observe)
    process(claim('general'))
    assert seen[0][0] == 0 and seen[-1][0] == seen[-1][1] > 0
    with Session(engine()) as db:
        assert db.get(Job, run_id).state == 'running'
        assert db.scalar(select(func.count(Job.id)).where(Job.kind == 'source.collect')) == 0


def test_old_batch_recovered_without_a_second_post(client, monkeypatch):
    headers, _ = prepare(client, monkeypatch)
    old = post(client, headers)
    recovered = status(client)['run']
    assert recovered['id'] is None and recovered['phase'] == 'submitted'
    assert recovered['batch']['started_at'] == old['started_at']
    assert status(client, str(uuid4()))['run'] is None
    with Session(engine()) as db:
        assert db.scalar(select(func.count(Job.id)).where(Job.kind == inspection_runs.KIND)) == 0


def test_auction_publication_delay_is_followed_without_requesting_credentials(client, monkeypatch):
    headers, source = prepare(client, monkeypatch)
    action = next(item for item in post(client, headers)['actions'] if item['source_id'] == str(source))
    with Session(engine()) as db, db.begin():
        job = db.get(Job, UUID(action['job_id']))
        job.state, job.error_code, job.completed_at = 'failed', 'auction_data_not_ready', datetime.now(UTC)
    result = next(item for item in status(client)['run']['batch']['actions'] if item['source_id'] == str(source))
    assert result['state'] == 'waiting' and '竞价窗口' in result['advice'] and result['job_id'] is None


def test_progress_and_start_require_admin(client):
    assert client.get(URL + '/progress').status_code == 401
    headers = login(client, role='viewer')
    assert client.get(URL + '/progress').status_code == 403
    assert client.post(URL + '/runs', headers={**headers, 'Idempotency-Key':str(uuid4())}, json={}).status_code == 403


def test_page_receipt_distinguishes_queue_active_lease_and_confirmed_unchanged_data(client, monkeypatch):
    from app.config import settings
    from app.integrations.inspection_repair import verify_batch
    from app.integrations.inspection_schemas import RepairBatch, RepairReceipt
    from app.models import User
    from app.read_cache import cache_scope
    from app.read_cache_models import ReadCache
    login(client)
    monkeypatch.setattr(settings(), 'read_cache_enabled', True)
    start_time = datetime.now(UTC) - timedelta(minutes=3)
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(User).where(User.role == 'admin'))
        key = uuid4().hex
        row = ReadCache(key=key, owner_id=owner.id, scope=cache_scope(db,owner), operation='news-items',
            params={}, dependency_keys=[], built_at=start_time - timedelta(minutes=1), revision='valid',
            validated_at=start_time - timedelta(minutes=1), payload={'data':{}}, dirty=True)
        db.add(row)
        batch = RepairBatch(started_at=start_time, actions=[
            RepairReceipt(id='cache:'+key, area='cache', state='working', title='页面数据', advice='')])
        def state():
            db.flush()
            return verify_batch(db, owner, batch).actions[0]
        assert state().state == 'waiting'
        row.lease_until = datetime.now(UTC) + timedelta(minutes=1)
        assert state().state == 'working'
        row.lease_until = None
        row.validated_at = datetime.now(UTC)
        assert '新数据' in state().advice
        row.dirty = False
        assert state().state == 'verified'
        row.dirty, row.last_error, row.last_attempt_at = True, 'OperationalError', datetime.now(UTC)
        row.refresh_after = datetime.now(UTC) + timedelta(minutes=1)
        assert state().state == 'waiting' and state().retry_at == row.refresh_after
        row.scope = 'old-scope'
        assert state().state == 'manual'
