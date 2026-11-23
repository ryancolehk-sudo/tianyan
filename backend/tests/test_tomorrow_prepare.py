"""Synthetic source/transport examples on the repository's disposable database."""
import copy
import io
import json
import socket
import uuid
from datetime import UTC, datetime, time, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion, SourceVersion
from app.market import preparation_fetch as fetch
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SHANGHAI, Response, SourceFailure
from app.market.review_store import family_id
from app.models import Job, Provider, User
from app.tomorrow import prepare


@pytest.fixture(autouse=True)
def no_live_source(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('preparation tests must not contact a real source')
    for name in ('fetch_public', 'fetch_https', 'fetch_document', 'tushare_request'):
        monkeypatch.setattr(prepare, name, denied)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(settings(), 'tushare_token', SecretStr('synthetic-preparation-token'))


def setup_source(family='review_cls_news', *, selected=None):
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        owner = User(username='prepare-' + uuid.uuid4().hex, password_hash='synthetic-disabled', role='admin')
        db.add(owner)
        db.flush()
        provider = Provider(id=family_id(family), family=family, name='Synthetic preparation source',
            kind='news' if family in {'review_cls_news', 'cninfo_announcements'} else 'market',
            configured=True, enabled=True, entitlement_status='verified',
            adapter_version='synthetic-preparation-v1', capabilities=['reference'],
            rights={'display': 'allowed', 'cache': 'allowed', 'export': 'unknown', 'redistribute': 'unknown'})
        db.add(provider)
        db.flush()
        db.add(SourceVersion(source_id=provider.id, recorded_at=datetime(1970, 1, 1, tzinfo=UTC),
            descriptor={'configured': True, 'enabled': True, 'entitlement_status': 'verified',
                        'rights': provider.rights}, content_hash='a' * 64))
        config = SourceSettings(source_id=provider.id, owner_id=owner.id,
            rights_basis='Synthetic fixture in disposable database only',
            config={'symbols': selected or []}, interval_seconds=21600,
            configured_at=datetime.now(UTC), next_run_at=datetime.now(UTC) + timedelta(hours=6))
        db.add(config)
        db.flush()
        return owner, provider, config


def window():
    day = datetime.now(UTC).astimezone(SHANGHAI).date() - timedelta(days=2)
    return day, datetime.combine(day, time(23, 30), SHANGHAI)


def running_job(owner, family, operation, code=None):
    day, cutoff = window()
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        job = prepare._queue(db, owner.id, family, operation, day, cutoff, code)
        job.state, job.attempts = 'running', 1
        job.lease_token, job.lease_until = uuid.uuid4(), datetime.now(UTC) + timedelta(minutes=10)
        db.flush()
        return job


def reclaim(job):
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        current = db.get(Job, job.id)
        assert current.state == 'queued'
        current.state, current.attempts = 'running', current.attempts + 1
        current.lease_token, current.lease_until = uuid.uuid4(), datetime.now(UTC) + timedelta(minutes=10)
        db.flush()
        return current


def cls_response(stamps, *, first_id=1, content='Synthetic source content'):
    return Response(json.dumps({'errno': 0, 'data': {'roll_data': [
        {'id': first_id + number, 'ctime': int(stamp.timestamp()), 'title': 'Synthetic title',
         'content': content, 'stock_list': []} for number, stamp in enumerate(stamps)]}}).encode(),
         datetime.now(UTC), 1, 200)


def encoded(fields, rows):
    return json.dumps({'code': 0, 'data': {'fields': fields, 'items': rows}}).encode()


def test_news_window_pages_persist_then_resume_without_replaying_completed_page(monkeypatch):
    owner, _, _ = setup_source()
    job = running_job(owner, 'review_cls_news', 'news_window')
    day, cutoff = window()
    pages = [cls_response([cutoff - timedelta(minutes=i + 1) for i in range(20)], content='x' * 1100),
             cls_response([cutoff - timedelta(minutes=i + 21) for i in range(20)], first_id=21),
             cls_response([datetime.combine(day, time.min, SHANGHAI) - timedelta(seconds=1)], first_id=41)]
    calls = []
    def transport(url):
        calls.append(url)
        # Receipt time belongs to the actual mocked request, not fixture setup.
        value = pages[len(calls) - 1]
        return Response(value.body, datetime.now(UTC), value.elapsed_ms, value.status)
    monkeypatch.setattr(prepare, '_reserve_cls', lambda job: True)
    monkeypatch.setattr(prepare, 'fetch_public', transport)
    prepare.process_preparation(job)
    with Session(engine()) as db:
        current = db.get(Job, job.id)
        assert current.state == 'queued'
        assert current.result_ref['row_count'] == 20
        evidence = db.scalar(select(EvidenceVersion).where(EvidenceVersion.payload['dataset'].astext == 'tomorrow.news_window'))
        assert len(evidence.payload['rows']) == 60  # source text survives the old 500-char summary cap
    prepare.process_preparation(reclaim(job))
    prepare.process_preparation(reclaim(job))
    with Session(engine()) as db:
        current = db.get(Job, job.id)
        assert current.state == 'succeeded'
        assert current.result_ref['complete'] is True
        assert current.result_ref['requests'] == 3
        assert current.result_ref['row_count'] == 40
        receipts = list(db.scalars(select(DataFetchRun).where(DataFetchRun.job_id == job.id)))
        assert sum(row.request_count for row in receipts) == 3
    assert len(set(calls)) == 3


def test_short_source_history_is_not_claimed_as_full_day(monkeypatch):
    owner, _, _ = setup_source()
    job = running_job(owner, 'review_cls_news', 'news_window')
    _, cutoff = window()
    monkeypatch.setattr(prepare, '_reserve_cls', lambda job: True)
    monkeypatch.setattr(prepare, 'fetch_public', lambda url: cls_response([cutoff - timedelta(minutes=1)]))
    prepare.process_preparation(job)
    with Session(engine()) as db:
        current = db.get(Job, job.id)
        assert current.state == 'succeeded'
        assert current.result_ref['complete'] is False
        assert current.result_ref['reason'] == 'news_window_unavailable'


def test_cutoff_excludes_text_modified_after_the_window():
    day, cutoff = window()
    response = cls_response([cutoff - timedelta(minutes=1)])
    raw = json.loads(response.body)
    raw['data']['roll_data'][0]['modified_time'] = int((cutoff + timedelta(minutes=1)).timestamp())
    result = fetch.parse_news_window(Response(json.dumps(raw).encode(), datetime.now(UTC), 1, 200),
        datetime.combine(day, time.min, SHANGHAI), cutoff)
    assert result[0] == []


def test_selected_candidate_never_changes_global_symbols_or_schedule(monkeypatch):
    selected = [{'exchange': 'SZSE', 'symbol': '000001', 'asset_type': 'stock'}]
    owner, provider, config = setup_source('tushare_daily_basic', selected=selected)
    day, _ = window()
    original = copy.deepcopy(config.config)
    job = running_job(owner, provider.family, 'selected', '600001.SH')
    calls = []
    def request(api, params, fields, *_args):
        calls.append(params)
        return Response(encoded(['ts_code', 'trade_date', 'pe_ttm'],
            [['600001.SH', day.strftime('%Y%m%d'), '8.5']]), datetime.now(UTC), 1, 200)
    monkeypatch.setattr('app.market.budget.reserve_tushare_request', lambda job: True)
    monkeypatch.setattr(prepare, 'tushare_request', request)
    prepare.process_preparation(job)
    with Session(engine()) as db:
        after = db.get(SourceSettings, provider.id)
        assert after.config == original
        assert after.next_run_at == config.next_run_at
        assert db.get(Job, job.id).state == 'succeeded'
        evidence = db.scalar(select(EvidenceVersion).where(EvidenceVersion.payload['dataset'].astext == 'tushare.daily_basic'))
        assert evidence.payload['rows'][0]['ts_code'] == '600001.SH'
    assert calls[0]['ts_code'] == '600001.SH'
    assert calls[0]['end_date'] == day.strftime('%Y%m%d')


def test_future_calendar_has_explicit_ninety_day_scope(monkeypatch):
    owner, provider, _ = setup_source('tushare_calendar_sse')
    job = running_job(owner, provider.family, 'calendar')
    day, _ = window()
    fields = ['exchange', 'cal_date', 'is_open']
    rows = [['SSE', (day + timedelta(days=i)).strftime('%Y%m%d'), '0'] for i in range(91)]
    requested = []
    monkeypatch.setattr('app.market.budget.reserve_tushare_request', lambda job: True)
    def request(api, params, *_args):
        requested.append(params)
        return Response(encoded(fields, rows), datetime.now(UTC), 1, 200)
    monkeypatch.setattr(prepare, 'tushare_request', request)
    prepare.process_preparation(job)
    with Session(engine()) as db:
        values = list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.kind == 'calendar')))
        assert len(values) == 91
        assert max(row.payload['trade_day'] for row in values) == str(day + timedelta(days=90))
    assert requested[0]['end_date'] == (day + timedelta(days=90)).strftime('%Y%m%d')


def test_announcements_fetch_real_body_separately_and_record_parse_failure(monkeypatch):
    owner, _, _ = setup_source('cninfo_announcements')
    job = running_job(owner, 'cninfo_announcements', 'announcements', '600001.SH')
    day, _ = window()
    item = {'secCode': '600001', 'announcementId': '1234', 'announcementTitle': 'Synthetic risk disclosure',
            'announcementTime': int(datetime.combine(day, time.min, SHANGHAI).timestamp() * 1000),
            'adjunctUrl': f'finalpage/{day}/1234.PDF'}
    monkeypatch.setattr('app.market.budget.reserve_news_request', lambda *a, **kw: True)
    requests = []
    def request(url, **kwargs):
        requests.append((url, kwargs['data']))
        raw = [{'code': '600001', 'orgId': 'synthetic_org'}] if url == fetch.CNINFO_LOOKUP else {
            'totalAnnouncement': 1, 'hasMore': False, 'announcements': [item]}
        return Response(json.dumps(raw).encode(), datetime.now(UTC), 1, 200)
    monkeypatch.setattr(prepare, 'fetch_https', request)
    prepare.process_preparation(job)
    prepare.process_preparation(reclaim(job))
    monkeypatch.setattr(prepare, 'fetch_document', lambda url: Response(b'%PDF-synthetic-invalid', datetime.now(UTC), 1, 200))
    monkeypatch.setattr(prepare, 'extract_pdf', lambda body: ('', 'parse_failed'))
    prepare.process_preparation(reclaim(job))
    with Session(engine()) as db:
        current = db.get(Job, job.id)
        assert current.state == 'succeeded'
        assert current.result_ref['complete'] is False
        assert current.result_ref['reason'] == 'parse_failed'
        texts = [row for ev in db.scalars(select(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext == 'tomorrow.announcement_text')) for row in ev.payload['rows']]
        assert all(row['text'] is None for row in texts)
        assert all(row['published_at'] is None for row in texts)
        assert any(row['text_status'] == 'parse_failed' and row['document_sha256'] for row in texts)
    assert requests[0][0] == fetch.CNINFO_LOOKUP
    assert b'stock=600001%2Csynthetic_org' in requests[1][1]


@pytest.mark.parametrize('value', ['http://127.0.0.1/a.pdf', 'https://static.cninfo.com.cn@127.0.0.1/a.pdf',
    'https://static.cninfo.com.cn/finalpage/2026-10-23/a.pdf?redirect=x',
    'finalpage/2026-10-23/../../evil.pdf', 'https://evil.test/finalpage/2026-10-23/a.pdf'])
def test_document_sources_are_not_client_chosen_urls(value):
    with pytest.raises(SourceFailure):
        fetch.document_url(value)


def test_pdf_host_resolving_to_private_address_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(SourceFailure, match='document_host_not_public'):
        fetch.fetch_document('finalpage/2026-10-23/123.PDF')


def test_invalid_pdf_never_uses_its_title_as_body():
    assert fetch.extract_pdf(b'not a PDF') == ('', 'invalid_pdf')


def test_real_pdf_text_is_extracted_in_the_bounded_child_process():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    contents = DecodedStreamObject()
    contents.set_data(b'BT /F1 12 Tf 10 100 Td (Synthetic disclosure facts) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(contents)
    output = io.BytesIO()
    writer.write(output)
    text, status = fetch.extract_pdf(output.getvalue())
    assert status == 'extracted'
    assert 'Synthetic disclosure facts' in text


def test_encrypted_pdf_is_an_explicit_gap():
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt('synthetic-password')
    output = io.BytesIO()
    writer.write(output)
    assert fetch.extract_pdf(output.getvalue()) == ('', 'encrypted')


def test_queue_records_permissions_as_gaps_and_does_not_configure_sources():
    owner, provider, _ = setup_source('tushare_daily_basic')
    day, cutoff = window()
    with Session(engine()) as db, db.begin():
        row = db.get(Provider, provider.id)
        row.entitlement_status = 'unknown'
        result = prepare.queue_preparation(db, owner.id, day, cutoff, ['600001.SH'])
        assert result['job_ids'] == []
        assert all(row['state'] == 'gap' for row in result['coverage'])
        assert any(row.get('reason') == 'permission_missing' for row in result['coverage'])
        assert db.get(Provider, provider.id).entitlement_status == 'unknown'


def test_job_refuses_configuration_change_before_any_request():
    owner, provider, _ = setup_source()
    job = running_job(owner, provider.family, 'news_window')
    with Session(engine()) as db, db.begin():
        config = db.get(SourceSettings, provider.id)
        config.interval_seconds += 1
    with pytest.raises(SourceFailure, match='configuration_changed'):
        prepare.process_preparation(job)
