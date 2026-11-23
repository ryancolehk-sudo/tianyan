"""Synthetic fixtures on real disposable PostgreSQL, never actual market data."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.breadth import DATASET, parse_breadth
from app.market.budget import ACTION, reserve_ashare_request
from app.market.models import DataFetchRun
from app.market.models import SourceSettings
from app.market.collect import store_observation, store_reference_batch
from app.market.protocol import Response, SourceFailure
from app.market.service import source_id
from app.models import AuditEvent, Job, Provider
from app.worker import claim, process
from test_data_sources import configuration, configure, login


def sample():
    row = {'上涨': '12', '下跌': '8', '平盘': '0', '停牌': None, '涨停': '2', '跌停': '1', '上涨占比': '60%'}
    return {'ok': True, 'endpoint': 'changedist', 'tier': 'free', 'source': 'synthetic',
        'data': '### 市场涨跌分布（沪深A股）\nSynthetic test only', 'structured': [row],
        'tables': [[row], [{'区间': '涨停', '家数': '2', '方向': '涨'}, {'区间': '跌停', '家数': '1', '方向': '跌'}]]}


def test_breadth_does_not_invent_source_day_clock_unknown_counts_or_amount():
    parsed = parse_breadth(json.dumps(sample()).encode(), datetime.now(UTC))
    assert parsed.expected == 2 and parsed.requests == 1 and len(parsed.records) == 2
    headline = parsed.records[0][1]
    assert headline.observed_day is None and parsed.records[0][2] is None
    assert headline.rows[0]['unchanged'] == '0' and headline.rows[0]['suspended'] is None
    assert '成交额未知' in headline.scope and '源' not in headline.rows[0]
    assert 'amount' not in headline.rows[0] and 'broken_limit' not in headline.rows[0]


@pytest.mark.parametrize('bad', ['-1', '1.5', True, 'NaN', '1000000'])
def test_breadth_rejects_invalid_counts_instead_of_coercing_them(bad):
    value = sample(); value['structured'][0]['涨停'] = bad
    with pytest.raises(SourceFailure, match='schema_changed'):
        parse_breadth(json.dumps(value).encode(), datetime.now(UTC))


def test_breadth_rejects_changed_market_scope_and_inconsistent_limits():
    value = sample(); value['data'] = '### Unverified market scope'
    with pytest.raises(SourceFailure, match='schema_changed'):
        parse_breadth(json.dumps(value).encode(), datetime.now(UTC))
    value = sample(); value['structured'][0]['涨停'] = '13'
    with pytest.raises(SourceFailure, match='schema_changed'):
        parse_breadth(json.dumps(value).encode(), datetime.now(UTC))


def setup_sources(client, monkeypatch):
    headers = login(client)
    configure(client, headers, configuration('ashare_breadth'))
    quote = configuration('tencent_web'); quote.update(family='ashare_public', interval_seconds=300)
    configure(client, headers, quote, key=uuid.uuid4().hex)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    return headers


def queue(client, headers, family):
    result = client.post('/api/v1/data-sources/' + str(source_id(family)) + '/refresh', json={}, headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
    assert result.status_code == 202


def test_worker_stores_versioned_reference_and_exact_dataset_read_rechecks_rights(client, monkeypatch):
    headers = setup_sources(client, monkeypatch)
    monkeypatch.setattr('app.market.collect.fetch_https', lambda *_: Response(json.dumps(sample()).encode(), datetime.now(UTC), 2, 200))
    queue(client, headers, 'ashare_breadth'); process(claim())
    url = '/api/v1/reference-datasets?dataset=' + DATASET
    rows = client.get(url).json()['data']['items']
    assert len(rows) == 1 and rows[0]['payload']['observed_day'] is None
    assert rows[0]['payload']['rows'][0]['limit_up'] == '2'
    assert client.get('/api/v1/reference-datasets?dataset=not.found').json()['data']['items'] == []
    assert client.get('/api/v1/reference-datasets?dataset=../invalid').status_code == 422
    progress = client.get('/api/v1/data-coverage').json()['data']['items']
    assert next(row for row in progress if row['id'] == 'D12')['state'] == 'partial'
    assert next(row for row in progress if row['id'] == 'D11')['state'] == 'pending'
    with Session(engine()) as db, db.begin():
        records = db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == source_id('ashare_breadth'))).all()
        assert len(records) == 2 and all(not row.historical_eligible and row.event_at is None and row.published_at is None for row in records)
    revoked = configuration('ashare_breadth', enabled=False); revoked['rights']['display'] = 'denied'
    configure(client, headers, revoked, key=uuid.uuid4().hex)
    assert client.get(url).json()['data']['items'] == []


def test_shared_budget_fences_concurrent_workers_and_wait_does_not_spend_a_retry(client, monkeypatch):
    headers = setup_sources(client, monkeypatch)
    queue(client, headers, 'ashare_public'); queue(client, headers, 'ashare_breadth')
    jobs = [claim(), claim()]
    with ThreadPoolExecutor(max_workers=2) as threads:
        admitted = list(threads.map(reserve_ashare_request, jobs))
    assert sorted(admitted) == [False, True]
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == ACTION)) == 1
        waiting = db.get(Job, jobs[admitted.index(False)].id)
        assert waiting.state == 'queued' and waiting.attempts == 0 and waiting.lease_token is None
        assert waiting.available_at > datetime.now(UTC)
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 0


def test_dataset_filter_never_resurrects_the_previous_name_after_a_correction(client, monkeypatch):
    setup_sources(client, monkeypatch)
    payload = parse_breadth(json.dumps(sample()).encode(), datetime.now(UTC)).records[0][1]
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('ashare_breadth'))
        config = db.get(SourceSettings, provider.id)
        for value in [payload, payload.model_copy(update={'dataset': 'market.corrected'})]:
            assert store_observation(db, provider, config, source_key='reference:' + DATASET, kind='reference', payload=value, received_at=datetime.now(UTC))
    assert client.get('/api/v1/reference-datasets?dataset=' + DATASET).json()['data']['items'] == []
    assert len(client.get('/api/v1/reference-datasets?dataset=market.corrected').json()['data']['items']) == 1


def test_breadth_return_to_an_earlier_value_is_a_new_observation_not_a_stuck_cache(client, monkeypatch):
    setup_sources(client, monkeypatch)
    for total in ['2', '3', '2', '2']:
        value = sample(); value['structured'][0]['涨停'] = total
        batch = parse_breadth(json.dumps(value).encode(), datetime.now(UTC))
        with Session(engine()) as db, db.begin():
            provider = db.get(Provider, source_id('ashare_breadth'))
            result = store_reference_batch(db, provider, db.get(SourceSettings, provider.id), batch, datetime.now(UTC))
        current = client.get('/api/v1/reference-datasets?dataset=' + DATASET).json()['data']['items'][0]
        assert current['payload']['rows'][0]['limit_up'] == total
    assert result == (1, 1)
    with Session(engine()) as db:
        rows = db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == source_id('ashare_breadth'), EvidenceVersion.source_key == 'reference:' + DATASET).order_by(EvidenceVersion.version_number)).all()
        assert [row.payload['rows'][0]['limit_up'] for row in rows] == ['2', '3', '2', '2']
        assert len({row.source_revision for row in rows}) == 4
        assert all(row.event_at is None and not row.historical_eligible for row in rows)


def test_shared_budget_defers_without_http_then_worker_resumes_normally(client, monkeypatch):
    headers = setup_sources(client, monkeypatch)
    queue(client, headers, 'ashare_breadth')
    with Session(engine()) as db, db.begin():
        db.add(AuditEvent(action=ACTION, created_at=datetime.now(UTC)))
    calls = []
    def fetch(*args):
        calls.append(args)
        return Response(json.dumps(sample()).encode(), datetime.now(UTC), 1, 200)
    monkeypatch.setattr('app.market.collect.fetch_https', fetch)
    job = claim(); process(job)
    assert calls == []
    with Session(engine()) as db, db.begin():
        saved = db.get(Job, job.id)
        assert saved.state == 'queued' and saved.attempts == 0
        saved.available_at = datetime.now(UTC) - timedelta(seconds=1)
        db.execute(update(AuditEvent).where(AuditEvent.action == ACTION).values(created_at=datetime.now(UTC) - timedelta(seconds=40)))
    process(claim())
    assert len(calls) == 1
    with Session(engine()) as db:
        saved = db.get(Job, job.id)
        assert saved.state == 'succeeded' and saved.attempts == 1
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 1
