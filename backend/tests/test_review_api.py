"""Authenticated review cache contracts on disposable PostgreSQL; no network."""
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_data_sources import configuration, configure, login

from app.config import settings
from app.db import engine
from app.market.models import SourceSettings
from app.market.review_protocol import PUBLIC_FAMILIES, ReviewCollectionInput
from app.market.service import source_id
from app.models import Job, User
from app.worker import claim


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: pytest.fail('No external calls permitted'))


def test_review_reads_and_writes_require_login(client):
    for path in ('coverage', 'series', 'day', 'lhb?trade_day=2026-10-23',
                 'seat-disclosures?trade_day=2026-10-23&seat_name=Synthetic',
                 'stocks?trade_day=2026-10-23&codes=600001.SH',
                 'theme-history?trade_day=2026-10-23&sector_codes=BK1184.DC',
                 'notes?trade_day=2026-10-23', 'notes/history?trade_day=2026-10-23',
                 'notes/days?trade_day=2026-10-23'):
        assert client.get('/api/v1/review-data/' + path).status_code == 401
    assert client.post('/api/v1/review-data/collections', json={}).status_code in (401, 403)


def test_manual_notes_http_csrf_idempotency_and_cas(client):
    headers = login(client, role='researcher')
    body = {'trade_day': '2026-10-23', 'expected_head': None,
            'content': {'summary': 'Synthetic manual journal; not market evidence.'}}
    assert client.post('/api/v1/review-data/notes', json=body).status_code == 403
    saved = client.post('/api/v1/review-data/notes', json=body,
                        headers={**headers, 'Idempotency-Key': 'notes-http-0001'})
    assert saved.status_code == 201, saved.text
    data = saved.json()['data']
    assert data['revision']['version_number'] == 1 and data['head_id'] == data['revision']['id']
    repeated = client.post('/api/v1/review-data/notes', json=body,
                           headers={**headers, 'Idempotency-Key': 'notes-http-0001'})
    assert repeated.status_code == 201 and repeated.json() == saved.json()
    stale = client.post('/api/v1/review-data/notes', json=body,
                        headers={**headers, 'Idempotency-Key': 'notes-http-stale'})
    assert stale.status_code == 409
    history = client.get('/api/v1/review-data/notes/history?trade_day=2026-10-23').json()['data']
    assert len(history['items']) == 1 and history['items'][0]['revision']['id'] == data['head_id']
    assert client.get('/api/v1/review-data/stocks?trade_day=2026-10-23&codes=830001.BJ').status_code == 422
    assert client.get('/api/v1/review-data/theme-history?trade_day=2026-10-23&sector_codes=bad').status_code == 422
    assert client.get('/api/v1/review-data/notes/days?trade_day=2026-10-23').status_code == 200


def test_viewer_can_read_only_own_empty_journal(client):
    headers = login(client, role='viewer')
    assert client.get('/api/v1/review-data/notes?trade_day=2026-10-23').json()['data']['state'] == 'empty'
    assert client.post('/api/v1/review-data/notes', json={
        'trade_day': '2026-10-23', 'expected_head': None, 'content': {},
    }, headers={**headers, 'Idempotency-Key': 'viewer-notes-denied'}).status_code == 403


def test_statistical_window_and_member_window_are_explicitly_separate():
    body = ReviewCollectionInput()
    assert body.lookback == 60 and body.member_lookback == 5
    assert ReviewCollectionInput(member_lookback=60).member_lookback == 60


def test_review_request_is_idempotent_and_keeps_existing_settings(client, monkeypatch):
    from pydantic import SecretStr
    monkeypatch.setattr(settings(), 'tushare_token', SecretStr('synthetic-review-api-token'))
    headers = login(client)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    for family in ('tushare_calendar_sse', 'tushare_calendar_szse'):
        configure(client, headers, {**configuration(family), 'interval_seconds': 86400}, key=family)
    with Session(engine()) as db:
        original = {row.source_id: (row.config, row.next_run_at, row.configured_at)
                    for row in db.scalars(select(SourceSettings))}
    key = {**headers, 'Idempotency-Key': 'review-start-001'}
    response = client.post('/api/v1/review-data/collections', json={'lookback': 20}, headers=key)
    assert response.status_code == 202, response.text
    repeated = client.post('/api/v1/review-data/collections', json={'lookback': 20}, headers=key)
    assert repeated.status_code == 202 and repeated.json() == response.json()
    assert repeated.headers['Idempotency-Replayed'] == 'true'
    assert client.post('/api/v1/review-data/collections', json={'lookback': 60}, headers=key).status_code == 409
    batch = response.json()['data']['batch_id']
    for job_id in response.json()['data']['calendar_job_ids'] + [response.json()['data']['prepare_job_id']]:
        job_response = client.get('/api/v1/jobs/' + job_id)
        assert job_response.status_code == 200, job_response.text
        assert job_response.json()['data']['kind'] in {'review.collect', 'review.prepare'}
    state = client.get('/api/v1/review-data/collections/' + batch)
    assert state.status_code == 200 and state.json()['data']['counts']['queued'] == 3
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 3
        assert {row.source_id: (row.config, row.next_run_at, row.configured_at)
                for row in db.scalars(select(SourceSettings))} == original
    with Session(engine()) as db, db.begin():
        prepare = db.get(Job, uuid.UUID(response.json()['data']['prepare_job_id']))
        prepare.result_ref = {'queued_jobs': 0, 'collection_job_ids': [], 'unavailable_sources': []}
        prepare.state = 'succeeded'
    finished = client.get('/api/v1/jobs/' + response.json()['data']['prepare_job_id'])
    assert finished.status_code == 200 and finished.json()['data']['result_ref'] is None


def test_empty_cache_reads_never_create_jobs_or_fake_metrics(client):
    login(client)
    for path in ('coverage?lookback=60', 'series?lookback=60', 'day?trade_day=2026-10-23&stock=002896.SZ',
                 'lhb?trade_day=2026-10-23', 'seat-disclosures?trade_day=2026-10-23&seat_name=Synthetic'):
        response = client.get('/api/v1/review-data/' + path)
        assert response.status_code == 200, response.text
        assert response.json()['data']['state'] == 'unavailable'
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert client.get('/api/v1/review-data/day?stock=000001.BJ').status_code == 422
    assert client.get('/api/v1/review-data/series?lookback=21').status_code == 422
    assert client.get('/api/v1/review-data/day?as_of_time=2026-10-23T15:00:00').status_code == 422
    assert client.get('/api/v1/review-data/collections/' + str(uuid.uuid4())).status_code == 404


def test_review_collection_is_admin_only(client, monkeypatch):
    headers = login(client, role='researcher')
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    response = client.post('/api/v1/review-data/collections', json={},
                           headers={**headers, 'Idempotency-Key': 'denied-review-001'})
    assert response.status_code == 403


def test_large_backfill_keeps_normal_jobs_first_and_daily_before_members(client):
    login(client)
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(User))
        tasks = {}
        for dataset, day in [('members', '2026-10-23'), ('news', '2026-10-23'),
                             ('daily', '2026-10-22'), ('daily', '2026-10-23'),
                             ('indices', '2026-10-23')]:
            job = Job(owner_id=owner.id, kind='review.collect',
                      payload={'review_context': {'dataset': dataset, 'day': day}})
            db.add(job)
            db.flush()
            tasks[(dataset, day)] = job.id
        normal = Job(owner_id=owner.id, kind='system.check', payload={})
        db.add(normal)
        db.flush()
        expected = [normal.id, tasks[('indices', '2026-10-23')], tasks[('daily', '2026-10-23')],
                    tasks[('daily', '2026-10-22')], tasks[('members', '2026-10-23')], tasks[('news', '2026-10-23')]]
    # Deliberately leave claimed leases active: each selection is distinct.
    assert [claim().id for _ in expected] == expected


@pytest.mark.parametrize('family', sorted(PUBLIC_FAMILIES))
def test_public_review_adapters_are_explicit_personal_sources(client, family):
    headers = login(client)
    body = configuration(family)
    body['interval_seconds'] = 86400 if family == 'review_kpl_new_highs' else 21600
    assert configure(client, headers, body, key=family).status_code == 201
    invalid = {**body, 'rights': {**body['rights'], 'export': 'allowed'}}
    assert client.post('/api/v1/data-sources', json=invalid,
                       headers={**headers, 'Idempotency-Key': family + '-export'}).status_code == 422
    with Session(engine()) as db:
        config = db.get(SourceSettings, source_id(family))
        assert config.config['symbols'] == []
