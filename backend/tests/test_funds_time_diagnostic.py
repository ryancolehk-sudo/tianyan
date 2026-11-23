"""Real collect path rejects future rows and retains only one bounded receipt."""
from datetime import UTC, datetime, timedelta
import hashlib
import json
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import collect as collector
from app.market import eastmoney_trading as m
from app.market.fund_time_diagnostic import safe_diagnostic
from app.market.models import DataFetchRun
from app.market.protocol import Response, SourceFailure
from app.models import Job
from app.worker_watchdog import progress_reporting
from test_data_resilience import CLOCK, scenario
from test_fund_universe import upstream
from test_funds_live_isolation import configured, task


def future_fetch(upstream, monkeypatch, seconds, target=('stock', 1)):
    upstream['at'] = CLOCK
    upstream['totals'] = {'stock': 101, 'industry': 1, 'concept': 1}
    original = m.fetch_https
    def fetch(url):
        response = original(url)
        if upstream['calls'][-1] == target:
            body = json.loads(response.body)
            body['data']['diff'][0]['f124'] = int((response.received_at + timedelta(seconds=seconds)).timestamp())
            body['data']['diff'][0]['f14'] = 'sensitive name not retained'
            body['private'] = 'secret/https://example.invalid/?token=private'
            return Response(json.dumps(body).encode(), response.received_at, 1, 200)
        return response
    monkeypatch.setattr(m, 'fetch_https', fetch)
    return fetch


@pytest.mark.parametrize('seconds,stage,requests', [(1, 'capture_validation', 4), (61, 'page_validation', 1)])
def test_future_rejection_keeps_original_bound_and_exact_page(upstream, monkeypatch, seconds, stage, requests):
    future_fetch(upstream, monkeypatch, seconds)
    permits = []; progress = []
    with progress_reporting(progress.append), pytest.raises(SourceFailure) as caught:
        response = m.fetch_rankings('eastmoney_funds', lambda: permits.append(True), full_boards=True)
        m.parse_rankings(response.body, response.received_at, 'eastmoney_funds')
    diagnostic = safe_diagnostic(caught.value)
    assert caught.value.reason == 'future_source_time'
    assert len(permits) == requests and len(progress) == (4 if seconds == 1 else 0)
    assert diagnostic['stage'] == stage and diagnostic['table'] == 'stock_funds'
    assert diagnostic['page'] == 1 and diagnostic['row_index'] == 0 and diagnostic['code'] == '600100'
    assert diagnostic['ahead_us'] == seconds * 1_000_000
    assert diagnostic['source_epoch'] == int((CLOCK + timedelta(seconds=seconds+1)).timestamp())
    assert len(json.dumps(diagnostic)) <= 1024
    assert all(s not in json.dumps(diagnostic) for s in ['sensitive', 'private', 'https', 'token'])


@pytest.mark.parametrize('target,table,page', [(('stock', 2), 'stock_funds', 2),
    (('industry', 1), 'industry_funds', 1), (('concept', 1), 'concept_funds', 1)])
def test_diagnostic_binds_first_bad_page_in_each_table(upstream, monkeypatch, target, table, page):
    future_fetch(upstream, monkeypatch, 1, target)
    response = m.fetch_rankings('eastmoney_funds', lambda: None, full_boards=True)
    with pytest.raises(SourceFailure) as caught:
        m.parse_rankings(response.body, response.received_at, 'eastmoney_funds')
    result = safe_diagnostic(caught.value)
    saved_page = next(p for t in json.loads(response.body)['tables'] if t['table'] == table
        for p in t['pages'] if p['page'] == page)
    assert result['table'] == table and result['page'] == page
    assert result['page_hash'] == hashlib.sha256(saved_page['body'].encode()).hexdigest()
    assert result['page_received_at'] == datetime.fromisoformat(saved_page['received_at']).astimezone(UTC).isoformat()
    assert result['capture_received_at'] == response.received_at.astimezone(UTC).isoformat()


@pytest.mark.parametrize('field,value', [('extra', 'secret'), ('page', 81), ('page', True),
    ('row_index', 200), ('table', 'https://secret'), ('code', 'x'*2000),
    ('source_time', 'x'*2000), ('page_hash', 'x'*64), ('ahead_us', 0),
    ('source_epoch', True), ('stage', []), ('capture_received_at', None)])
def test_malformed_or_unbounded_metadata_is_not_persistable(upstream, monkeypatch, field, value):
    future_fetch(upstream, monkeypatch, 1)
    response = m.fetch_rankings('eastmoney_funds', lambda: None, full_boards=True)
    with pytest.raises(SourceFailure) as caught:
        m.parse_rankings(response.body, response.received_at, 'eastmoney_funds')
    caught.value.funds_time_rejection[field] = value
    assert safe_diagnostic(caught.value) is None
    assert caught.value.reason == 'future_source_time'


@pytest.mark.parametrize('seconds', [1, 61])
def test_collect_persists_failure_receipt_with_matching_run_without_market_rows(scenario, upstream, monkeypatch, seconds):
    future_fetch(upstream, monkeypatch, seconds)
    monkeypatch.setattr(collector, 'datetime', m.datetime)
    monkeypatch.setattr(collector, 'reserve_eastmoney_request', lambda job: None)
    original = {'existing': 'preserved', 'funds_page_progress': {'page': 9}}
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        provider, _ = configured(db, scenario)
        job = task(db, scenario, provider, state='running')
        job.result_ref = original
    collector.collect_job(job)
    with Session(engine()) as db:
        saved = db.get(Job, job.id)
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == job.id))
        receipt = saved.result_ref['funds_time_rejection']
        assert saved.state == 'failed' and saved.error_code == run.reason == 'future_source_time'
        assert saved.lease_token is None and saved.completed_at is not None
        assert run.status == 'error' and run.accepted is None and run.request_count == (4 if seconds == 1 else 1)
        assert receipt['run_id'] == str(run.id) and receipt['body_hash'] == run.body_hash
        assert bool(run.body_hash) == (seconds == 1)
        assert len(receipt['page_hash']) == 64
        assert all(saved.result_ref[k] == v for k, v in original.items())
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.source_id == provider.id)) == 0
        assert len(json.dumps(receipt)) < 1200


@pytest.mark.parametrize('lost', ['expired', 'replaced', 'finished'])
def test_no_diagnostic_or_run_is_written_by_lost_lease(scenario, upstream, monkeypatch, lost):
    fetch = future_fetch(upstream, monkeypatch, 61)
    monkeypatch.setattr(collector, 'datetime', m.datetime)
    monkeypatch.setattr(collector, 'reserve_eastmoney_request', lambda job: None)
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        provider, _ = configured(db, scenario)
        job = task(db, scenario, provider, state='running')
        job.result_ref = {'existing': 'preserved'}
    def lose_lease(url):
        response = fetch(url)
        with Session(engine()) as db, db.begin():
            current = db.get(Job, job.id)
            if lost == 'expired': current.lease_until = CLOCK - timedelta(days=1)
            elif lost == 'replaced': current.lease_token = uuid.uuid4()
            else: current.state = 'succeeded'
        return response
    monkeypatch.setattr(m, 'fetch_https', lose_lease)
    collector.collect_job(job)
    with Session(engine()) as db:
        assert db.get(Job, job.id).result_ref == {'existing': 'preserved'}
        assert db.scalar(select(func.count()).select_from(DataFetchRun).where(DataFetchRun.job_id == job.id)) == 0
