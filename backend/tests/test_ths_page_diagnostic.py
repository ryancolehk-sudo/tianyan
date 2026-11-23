"""Operational page failure shapes, with synthetic HTML and isolated PostgreSQL."""
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import re
from types import SimpleNamespace
import uuid

import pytest

from app.market import ths_funds as funds
from app.market.protocol import SourceFailure
from test_ths_funds_unit import page

STAMP = datetime(2026, 11, 13, 6, 30, tzinfo=UTC)


def test_wrong_page_failure_retains_structure_without_html_or_market_values():
    marker = 'synthetic-private-marker-must-not-be-retained'
    body = page(pager='1/8') + ('<script>' + marker + '</script>').encode()
    with pytest.raises(SourceFailure, match='incomplete_page') as failed:
        funds.parse_page(body, 'hyzjl', 2, STAMP)
    diagnostic = getattr(failed.value, 'ths_page_diagnostic', None)
    assert diagnostic is not None
    assert diagnostic['checkpoint'] == 'page_mismatch'
    assert diagnostic['requested_page'] == 2 and diagnostic['actual_page'] == 1
    assert diagnostic['total_pages'] == 8 and diagnostic['row_count'] == 1
    assert diagnostic['body_bytes'] == len(body) and diagnostic['body_sha256'] == sha256(body).hexdigest()
    stored = json.dumps(diagnostic, ensure_ascii=False)
    assert marker not in stored and '半导体' not in stored and '881121' not in stored
    assert len(stored) < 1000


def malformed(kind):
    body = page()
    if kind == 'decode':
        return b'\xff'
    if kind == 'pagination_count':
        return body.split(b'<span')[0]
    if kind == 'pagination_format':
        return page(pager='untrusted-pager-text')
    if kind == 'duplicate_pagination':
        return body + b'<span class="page_info">1/1</span>'
    if kind == 'page_total':
        return page(pager='1/17')
    if kind == 'empty_rows':
        return re.sub(rb'<tr><td>.*?</tr>', b'', body)
    if kind == 'too_many_rows':
        row = re.search(rb'<tr><td>.*?</tr>', body)[0]
        return body.replace(b'</table>', row * 50 + b'</table>')
    if kind == 'headings':
        return page(unit='万')
    if kind == 'row_width':
        return body.replace(b'<td>1</td>', b'', 1)
    if kind == 'numeric_field':
        return page(net='NaN')
    raise AssertionError(kind)


@pytest.mark.parametrize('kind,checkpoint,reason', [
    ('decode', 'decode', 'schema_changed'),
    ('pagination_count', 'pagination_count', 'incomplete_page'),
    ('pagination_format', 'pagination_format', 'incomplete_page'),
    ('duplicate_pagination', 'pagination_count', 'incomplete_page'),
    ('page_total', 'page_total', 'incomplete_page'),
    ('empty_rows', 'row_count', 'incomplete_page'),
    ('too_many_rows', 'row_count', 'incomplete_page'),
    ('headings', 'headings', 'schema_changed'),
    ('row_width', 'row_width', 'schema_changed'),
    ('numeric_field', 'numeric_field', 'schema_changed'),
])
def test_existing_rejection_reasons_gain_specific_safe_shapes(kind, checkpoint, reason):
    with pytest.raises(SourceFailure, match=reason) as failed:
        funds.parse_page(malformed(kind), 'hyzjl', 1, STAMP)
    assert failed.value.ths_page_diagnostic['checkpoint'] == checkpoint
    assert 'untrusted-pager-text' not in json.dumps(failed.value.ths_page_diagnostic)


def diagnostic():
    with pytest.raises(SourceFailure) as failed:
        funds.parse_page(page(pager='1/2'), 'hyzjl', 2, STAMP)
    event = dict(failed.value.ths_page_diagnostic, attempt=1,
        received_at=STAMP.isoformat(), reason='incomplete_page')
    return dict(version=1, family='ths_industry_funds', events=[event])


@pytest.mark.parametrize('broken', ['checkpoint', 'reason', 'hash', 'clock', 'naive_clock',
    'count', 'attempt', 'money_type', 'too_many_events', 'family', 'version'])
def test_malformed_operational_metadata_cannot_break_collection_or_leak_text(broken):
    from app.market.ths_page_diagnostic import safe
    value = diagnostic()
    event = value['events'][0]
    if broken in {'checkpoint', 'reason'}:
        event[broken] = ['private-text']
    elif broken == 'hash':
        event['body_sha256'] = 'private-text'
    elif broken == 'clock':
        event['received_at'] = {'unexpected': 'private-text'}
    elif broken == 'naive_clock':
        event['received_at'] = STAMP.replace(tzinfo=None).isoformat()
    elif broken == 'count':
        event['row_count'] = True
    elif broken == 'attempt':
        event['attempt'] = 3
    elif broken == 'money_type':
        event['money_headings_match'] = 'private-text'
    elif broken == 'too_many_events':
        value['events'] *= 19
    elif broken == 'family':
        value['family'] = 'other-source'
    else:
        value['version'] = True
    assert safe(value, 'ths_industry_funds') is None


def test_unknown_fields_are_removed_and_attempt_history_is_bounded_without_overwriting_progress():
    from app.market.ths_page_diagnostic import record
    value = diagnostic()
    value['events'][0]['html'] = 'private-text'
    value['html'] = 'private-text'
    current = SimpleNamespace(result_ref={'existing': 'kept', 'ths_cooldown': {'seconds': 172800}})
    identities = [uuid.uuid4() for _ in range(6)]
    for identity in identities:
        record(current, SimpleNamespace(id=identity, request_count=3, status='partial'),
            'ths_industry_funds', value)
    history = current.result_ref['ths_page_failures']['runs']
    assert len(history) == 5 and history[0]['run_id'] == str(identities[1])
    assert history[-1]['run_id'] == str(identities[-1])
    assert current.result_ref['existing'] == 'kept' and current.result_ref['ths_cooldown']['seconds'] == 172800
    assert 'private-text' not in json.dumps(current.result_ref)


@pytest.mark.parametrize('recovered', [True, False])
def test_page_retry_keeps_actual_request_count_and_diagnostics_out_of_market_rows(monkeypatch, recovered):
    results = iter([
        (page(pager='1/2'), STAMP), (page(pager='1/2'), STAMP + timedelta(seconds=1)),
        (page(identity='881122', pager='2/2' if recovered else '1/2'), STAMP + timedelta(seconds=2)),
    ])
    calls = []
    def request(category, index, token):
        calls.append(index)
        return next(results)
    monkeypatch.setattr(funds, 'request', request)
    monkeypatch.setattr(funds.time, 'sleep', lambda seconds: None)
    response, batch = funds.fetch('ths_industry_funds', lambda: True)
    assert calls == [1, 2, 2] and response.request_count == batch.requests == 3
    rows = batch.records[0][1].rows
    assert rows[0]['complete'] == ('1' if recovered else '0')
    assert rows[0]['missing_pages'] == ('' if recovered else '2')
    assert batch.rejected == (0 if recovered else 1)
    assert len(batch.ths_page_diagnostics['events']) == (1 if recovered else 2)
    assert batch.ths_page_diagnostics['events'][0]['received_at'] == (STAMP + timedelta(seconds=1)).isoformat()
    assert all(not any('diagnostic' in key for key in row) for row in rows)


@pytest.fixture
def collection_job(client, monkeypatch):
    from sqlalchemy.orm import Session
    from app.config import settings
    from app.db import engine
    from app.market.models import SourceSettings
    from app.market.service import source_id
    from app.models import Job
    from test_data_sources import configuration, configure, login
    headers = login(client)
    configure(client, headers, {**configuration('ths_industry_funds'), 'interval_seconds': 30}, key='page-diagnostic-config')
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(funds.time, 'sleep', lambda seconds: None)
    with Session(engine()) as db, db.begin():
        config = db.get(SourceSettings, source_id('ths_industry_funds'))
        job = Job(owner_id=config.owner_id, kind='source.collect', state='running', attempts=1,
            lease_token=uuid.uuid4(), lease_until=datetime.now(UTC) + timedelta(minutes=5),
            payload={'source_id': str(config.source_id)}, result_ref={'existing': 'kept'})
        db.add(job)
        db.flush()
        db.expunge(job)
    return job


@pytest.mark.parametrize('recovered', [True, False])
def test_actual_collector_records_the_fetch_run_link_without_changing_payload_or_counts(collection_job, monkeypatch, recovered):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.evidence.models import EvidenceVersion
    from app.market.collect import collect_job
    from app.market.models import DataFetchRun
    from app.models import Job
    calls = []
    def request(category, index, token):
        calls.append(index)
        pager = '1/2' if len(calls) < 3 or not recovered else '2/2'
        return page(identity='881121' if len(calls) < 3 else '881122', pager=pager), datetime.now(UTC)
    monkeypatch.setattr(funds, 'request', request)
    collect_job(collection_job)
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == collection_job.id))
        saved = db.get(Job, collection_job.id)
        assert run is not None and run.request_count == 3 and calls == [1, 2, 2]
        assert run.status == ('complete' if recovered else 'partial')
        assert saved.result_ref['existing'] == 'kept'
        record = saved.result_ref['ths_page_failures']['runs'][0]
        assert record['run_id'] == str(run.id) and record['status'] == run.status
        assert len(record['diagnostic']['events']) == (1 if recovered else 2)
        evidence = db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id == run.source_id,
            EvidenceVersion.payload['dataset'].astext == 'market.ths.hyzjl'))
        assert evidence is not None and 'ths_page_failures' not in json.dumps(evidence.payload)
        assert evidence.payload['rows'][0]['complete'] == ('1' if recovered else '0')


def test_rate_limit_after_bad_page_preserves_supplier_deadline_and_prior_page_evidence(collection_job, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market.collect import collect_job
    from app.market.models import DataFetchRun, SourceSettings
    from app.models import Job
    calls = []
    before = datetime.now(UTC)
    def request(category, index, token):
        calls.append(index)
        if len(calls) == 3:
            raise SourceFailure('rate_limited', retry_after=172800, http_status=429)
        return page(pager='1/2'), datetime.now(UTC)
    monkeypatch.setattr(funds, 'request', request)
    collect_job(collection_job)
    after = datetime.now(UTC)
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == collection_job.id))
        config = db.get(SourceSettings, run.source_id)
        saved = db.get(Job, collection_job.id)
        assert calls == [1, 2, 2] and run.request_count == 3
        assert run.reason == 'rate_limited' and run.retry_not_before is None
        assert before + timedelta(days=2) <= config.legacy_cooldown_until <= after + timedelta(days=2)
        assert saved.available_at >= config.legacy_cooldown_until
        assert saved.result_ref['ths_cooldown']['seconds'] == 172800
        assert saved.result_ref['ths_page_failures']['runs'][0]['run_id'] == str(run.id)
        assert saved.result_ref['existing'] == 'kept'


def test_lost_lease_cannot_write_diagnostics_or_market_records(collection_job, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market.collect import collect_job
    from app.market.models import DataFetchRun
    from app.models import Job
    calls = []
    def request(category, index, token):
        calls.append(index)
        if len(calls) == 2:
            with Session(engine()) as db, db.begin():
                db.get(Job, collection_job.id).lease_until = datetime.now(UTC) - timedelta(seconds=1)
        return page(pager='1/2'), datetime.now(UTC)
    monkeypatch.setattr(funds, 'request', request)
    collect_job(collection_job)
    with Session(engine()) as db:
        assert len(calls) == 2
        assert db.get(Job, collection_job.id).result_ref == {'existing': 'kept'}
        assert db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == collection_job.id)) is None
