"""Disposable PostgreSQL and synthetic fixtures only; no vendor request in CI."""
import io
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from urllib.error import HTTPError

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.budget import MAIRUI_ACTION, reserve_mairui_request
from app.market.collect import store_calendar_batch, store_reference_batch
from app.market.mairui import FUNDS, POOLS, MairuiFailure, choose_pool_day, fetch_mairui, parse_mairui, pool_day
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.service import source_id
from app.models import AuditEvent, Instrument, Job, Provider
from app.worker import claim, process
from test_data_sources import configuration, configure, login

TOKEN = 'SYNTHETIC-LICENCE-FIXTURE-ONLY'
RECEIVED = datetime(2020, 1, 3, tzinfo=UTC)


def sample_row(table):
    row = {'dm': 'SYNTHETIC-BK-1' if table == 'industry_flow' else 'sz000001', 'mc': '合成参考',
        't': '2020-01-0215:40:00', 'jj': 10, 'zxj': 10, 'zdf': -1.25, 'jlr': -2, 'lrzj': 8, 'lczj': 10,
        'p': 10, 'zf': -1.25, 'lbc': 2, 'zbc': 0, 'fbt': '09:31:00', 'lbt': '10:00:00', 'hy': None}
    if table == 'broken_limit':
        row.pop('lbc')
    return row


def envelope(family='mairui_funds', *, query_day='2020-01-02'):
    tables = FUNDS if family == 'mairui_funds' else POOLS
    return {'family': family, 'query_day': query_day if family == 'mairui_pools' else None, 'request_count': len(tables),
        'tables': [{'table': key, 'rows': [sample_row(key)]} for key in tables]}


def parse(value):
    return parse_mairui(json.dumps(value).encode(), RECEIVED, value['family'])


def test_funds_preserve_negative_money_nulls_raw_clock_and_no_security_mapping():
    batch = parse(envelope())
    assert batch.expected == batch.requests == len(batch.records) == 2
    for _, payload, instrument in batch.records:
        assert instrument is None and payload.observed_day == date(2020, 1, 2)
        assert payload.rows[0]['jlr'] == '-2' and payload.rows[0]['jlrl'] is None
        assert payload.rows[0]['updated_raw'] == '2020-01-0215:40:00'
        assert '时区未注明' in payload.scope and '盘后' in payload.scope
        assert all(key not in payload.rows[0] for key in ['quote_time', 'published_at', 'event_at'])
        assert TOKEN not in payload.source_url and payload.source_url.endswith('/hanalyse')


def test_pools_preserve_requested_date_time_text_alias_units_and_missing_fields():
    value = envelope('mairui_pools');value['tables'][1]['rows'][0]['zdf'] = '-1.2500'
    batch = parse(value)
    assert batch.expected == batch.requests == len(batch.records) == 3
    for _, payload, instrument in batch.records:
        assert instrument is None and payload.observed_day == date(2020, 1, 2)
        assert payload.rows[0]['first_limit_raw'] == '09:31:00' and '请求日期' in payload.scope
        assert next(column for column in payload.columns if column.key == 'p').unit == '元'
    assert batch.records[1][1].rows[0]['lbc'] is None
    assert batch.records[0][1].rows[0]['zbc'] == '0'
    value['tables'][0]['rows'][0]['zf'] = '10.12345678901234567'
    value['tables'][0]['rows'][0]['zdf'] = '10.12345678901234567'
    assert parse(value).records[0][1].rows[0]['zf'] == '10.12345678901234567'
    value['tables'][0]['rows'][0]['fbt'] = '092500'
    assert parse(value).records[0][1].rows[0]['first_limit_raw'] == '092500'
    value['tables'][0]['rows'][0]['zdf'] = '7.125'
    payload = parse(value).records[0][1]
    assert payload.rows[0]['extra_zdf_raw'] == '7.125' and payload.rows[0]['zf'] == '10.12345678901234567'
    assert '不与zf合并' in payload.scope


@pytest.mark.parametrize('mode', ['duplicate', 'negative_price', 'boolean_count', 'nan', 'malformed_time', 'unsafe_name', 'bad_symbol', 'missing_name', 'mixed_date', 'invalid_extra_field', 'oversized', 'wrong_family', 'wrong_table'])
def test_ambiguous_changed_and_unsafe_rows_fail_atomically(mode):
    value = envelope('mairui_pools' if mode in {'invalid_extra_field', 'malformed_time'} else 'mairui_funds')
    row = value['tables'][0]['rows'][0]
    if mode == 'duplicate':value['tables'][0]['rows'].append(dict(row))
    elif mode == 'negative_price':row['jj'] = -1
    elif mode == 'boolean_count':value = envelope('mairui_pools');value['tables'][0]['rows'][0]['lbc'] = True
    elif mode == 'nan':row['jlr'] = 'NaN'
    elif mode == 'malformed_time':row['fbt'] = '24:00:00'
    elif mode == 'unsafe_name':row['mc'] = '<script>'
    elif mode == 'bad_symbol':value['tables'][1]['rows'][0]['dm'] = 'not-a-stock'
    elif mode == 'missing_name':row.pop('mc')
    elif mode == 'mixed_date':value['tables'][0]['rows'].append({**row, 'dm': 'SYNTHETIC-BK-2', 't': '2020-01-0115:40:00'})
    elif mode == 'invalid_extra_field':row['zdf'] = 'unverified text'
    elif mode == 'oversized':value['tables'][0]['rows'] *= 8001
    elif mode == 'wrong_family':value['family'] = 'unsupported'
    else:value['tables'][0]['table'] = 'unexpected'
    with pytest.raises(SourceFailure, match='schema_changed'):parse(value)


def test_empty_corrections_and_unaware_receipts_are_not_fake_zero_or_today():
    value = envelope()
    for table in value['tables']:table['rows'] = []
    assert all(payload.rows == [] and payload.observed_day is None for _, payload, _ in parse(value).records)
    with pytest.raises(SourceFailure, match='schema_changed'):
        parse_mairui(json.dumps(value).encode(), datetime(2020, 1, 3), 'mairui_funds')


def test_calendar_uses_both_exchanges_at_open_and_known_previous_session_before_open():
    days = {date(2020, 1, 2): True, date(2020, 1, 3): True, date(2020, 1, 4): False}
    calendars = {'SSE': days, 'SZSE': days}
    assert choose_pool_day(calendars, datetime(2020, 1, 3, 9, 29, tzinfo=SHANGHAI)) == date(2020, 1, 2)
    assert choose_pool_day(calendars, datetime(2020, 1, 3, 9, 30, tzinfo=SHANGHAI)) == date(2020, 1, 3)
    assert choose_pool_day(calendars, datetime(2020, 1, 4, 12, tzinfo=SHANGHAI)) == date(2020, 1, 3)
    for invalid in [{}, {'SSE': days}, {'SSE': days, 'SZSE': {date(2020, 1, 3): False, date(2020, 1, 2): True}}]:
        with pytest.raises(SourceFailure, match='calendar_not_ready'):
            choose_pool_day(invalid, datetime(2020, 1, 3, 10, tzinfo=SHANGHAI))


class Reply:
    def __init__(self, value):self.body = value if isinstance(value, bytes) else json.dumps(value).encode()
    def read(self, maximum):return self.body[:maximum]
    def __enter__(self):return self
    def __exit__(self, *args):pass


class Opener:
    def __init__(self, replies):self.replies = iter(replies);self.calls = []
    def open(self, request, timeout):
        self.calls.append(request)
        value = next(self.replies)
        if isinstance(value, Exception):raise value
        return Reply(value)


def test_fetch_counts_each_attempt_but_keeps_key_out_of_body_provenance_and_errors(monkeypatch):
    opener = Opener([[sample_row(key)] for key in FUNDS]);permits = []
    monkeypatch.setattr('app.market.mairui.urllib.request.build_opener', lambda *args: opener)
    response = fetch_mairui('mairui_funds', SecretStr(TOKEN), lambda: permits.append(True))
    assert len(permits) == len(opener.calls) == 2 and TOKEN.encode() not in response.body
    assert all(request.full_url.startswith('https://api.mairuiapi.com/') and request.full_url.endswith('/' + TOKEN) for request in opener.calls)
    parse_mairui(response.body, datetime.now(UTC), 'mairui_funds')
    error = HTTPError('https://api.mairuiapi.com/private/' + TOKEN, 503, TOKEN, {}, io.BytesIO())
    opener = Opener([[sample_row('industry_flow')], error])
    with pytest.raises(MairuiFailure) as failure:
        fetch_mairui('mairui_funds', SecretStr(TOKEN), lambda: None)
    assert failure.value.reason == 'upstream_unavailable' and failure.value.request_count == 2 and TOKEN not in str(failure.value)


@pytest.mark.parametrize('code,reason', [(101, 'rate_limited'), (102, 'permission_missing'), (103, 'rate_limited'), (503, 'upstream_unavailable')])
def test_vendor_errors_never_become_empty_success_and_quota_waits_until_reset(monkeypatch, code, reason):
    opener = Opener([{'code': code, 'message': 'synthetic only'}])
    monkeypatch.setattr('app.market.mairui.urllib.request.build_opener', lambda *args: opener)
    with pytest.raises(MairuiFailure) as failure:fetch_mairui('mairui_funds', SecretStr(TOKEN), lambda: None)
    assert failure.value.reason == reason and failure.value.request_count == 1
    if code in {101, 103}:assert 0 < failure.value.retry_after <= 86401


def test_missing_key_budget_refusal_and_echoed_secret_never_enter_a_saved_body(monkeypatch):
    with pytest.raises(MairuiFailure) as missing:fetch_mairui('mairui_funds', None, lambda: None)
    assert missing.value.request_count == 0
    def refuse():raise SourceFailure('rate_limited', retry_after=60)
    with pytest.raises(MairuiFailure) as denied:fetch_mairui('mairui_funds', SecretStr(TOKEN), refuse)
    assert denied.value.request_count == 0
    monkeypatch.setattr('app.market.mairui.urllib.request.build_opener', lambda *args: Opener([[{'mc': TOKEN}]]))
    with pytest.raises(MairuiFailure, match='unsafe_upstream_response'):
        fetch_mairui('mairui_funds', SecretStr(TOKEN), lambda: None)
    private = Settings(mairui_licence=TOKEN)
    assert TOKEN not in repr(private) and TOKEN not in private.model_dump_json()


def setup_source(client, family, monkeypatch):
    headers = login(client)
    configure(client, headers, configuration(family))
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(settings(), 'mairui_licence', SecretStr(TOKEN))
    return headers


@pytest.mark.parametrize('family', ['mairui_funds', 'mairui_pools'])
def test_actual_worker_reserves_each_request_and_persists_immutable_reference_without_key(client, monkeypatch, family):
    headers = setup_source(client, family, monkeypatch)
    if family == 'mairui_pools':monkeypatch.setattr('app.market.collect.pool_day', lambda db, clock: date(2020, 1, 2))
    tables = FUNDS if family == 'mairui_funds' else POOLS
    opener = Opener([[sample_row(key)] for key in tables])
    monkeypatch.setattr('app.market.mairui.urllib.request.build_opener', lambda *args: opener)
    response = client.post('/api/v1/data-sources/' + str(source_id(family)) + '/refresh', json={}, headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
    assert response.status_code == 202
    process(claim())
    result = client.get('/api/v1/reference-datasets').json()['data']['items']
    assert len(result) == len(tables) and TOKEN not in json.dumps(result)
    assert all(row['instrument_id'] is None and row['published_at'] is None for row in result)
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.source_id == source_id(family)))
        assert run.status == 'complete' and run.expected == run.accepted == run.request_count == len(tables)
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == MAIRUI_ACTION)) == len(tables)
        assert db.scalar(select(func.count()).select_from(Instrument)) == 0
        assert all(not row.historical_eligible and row.event_at is None for row in db.scalars(select(EvidenceVersion)))
    coverage = client.get('/api/v1/data-coverage').json()['data']['items']
    assert next(row for row in coverage if row['id'] == ('D10' if family == 'mairui_funds' else 'D11'))['state'] == 'partial'
    current = client.get('/api/v1/data-sources').json()['data']['items'][0]
    assert current['credential_configured'] is True and TOKEN not in json.dumps(current)
    revoked = configuration(family, enabled=False);revoked['rights']['display'] = 'denied'
    configure(client, headers, revoked, key=uuid.uuid4().hex)
    assert client.get('/api/v1/reference-datasets').json()['data']['items'] == []


def test_parallel_budget_reservations_share_450_limit_and_count_failures(client, monkeypatch):
    headers = setup_source(client, 'mairui_funds', monkeypatch)
    configure(client, headers, configuration('mairui_pools'), key=uuid.uuid4().hex)
    with Session(engine()) as db, db.begin():
        owner = db.get(SourceSettings, source_id('mairui_funds')).owner_id
        db.add_all([AuditEvent(action=MAIRUI_ACTION, created_at=datetime.now(UTC)) for _ in range(449)])
        db.add_all([Job(owner_id=owner, kind='source.collect', payload={'source_id': str(source_id(family))})
                    for family in ('mairui_funds', 'mairui_pools')])
    jobs = [claim(), claim()]
    assert all(jobs) and len({job.payload['source_id'] for job in jobs}) == 2
    def reserve(job):
        try:reserve_mairui_request(job);return 'allowed'
        except SourceFailure as error:return error.reason
    with ThreadPoolExecutor(max_workers=2) as pool:results = list(pool.map(reserve, jobs))
    assert sorted(results) == ['allowed', 'rate_limited']
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == MAIRUI_ACTION)) == 450


def test_expired_lease_and_revoked_permissions_do_not_consume_a_permit(client, monkeypatch):
    setup_source(client, 'mairui_funds', monkeypatch)
    with Session(engine()) as db, db.begin():
        cfg = db.get(SourceSettings, source_id('mairui_funds'))
        db.add(Job(owner_id=cfg.owner_id, kind='source.collect', payload={'source_id': str(cfg.source_id)}))
    job = claim()
    with Session(engine()) as db, db.begin():db.get(Provider, source_id('mairui_funds')).rights = {'display': 'denied', 'cache': 'allowed'}
    with pytest.raises(SourceFailure, match='permission_missing'):reserve_mairui_request(job)
    with Session(engine()) as db, db.begin():db.execute(update(Job).where(Job.id == job.id).values(lease_until=datetime.now(UTC) - timedelta(seconds=1)))
    with pytest.raises(SourceFailure, match='lease_expired'):reserve_mairui_request(job)
    with Session(engine()) as db:assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == MAIRUI_ACTION)) == 0


def test_calendar_reader_uses_current_rights_and_explicit_evidence_for_both_exchanges(client):
    headers = login(client)
    configure(client, headers, {**configuration('baostock_daily'), 'interval_seconds': 21600})
    clock = datetime.now(UTC);today = clock.astimezone(SHANGHAI).date()
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('baostock_daily'));cfg = db.get(SourceSettings, provider.id)
        store_calendar_batch(db, provider, cfg, [(today - timedelta(days=1), True), (today, True)], clock - timedelta(seconds=1), exchanges=('SSE', 'SZSE'))
    with Session(engine()) as db:assert pool_day(db, clock) in {today, today - timedelta(days=1)}
    with Session(engine()) as db, db.begin():db.get(Provider, source_id('baostock_daily')).rights = {'display': 'denied', 'cache': 'allowed'}
    with Session(engine()) as db:
        with pytest.raises(SourceFailure, match='calendar_not_ready'):pool_day(db, clock)


def test_reference_return_to_prior_value_and_empty_correction_do_not_resurrect_old_rows(client):
    headers = login(client);configure(client, headers, configuration('mairui_funds'))
    for name in ['合成A', '合成B', '合成A', '合成A']:
        value = envelope();value['tables'][0]['rows'][0]['mc'] = name
        with Session(engine()) as db, db.begin():
            provider = db.get(Provider, source_id('mairui_funds'))
            store_reference_batch(db, provider, db.get(SourceSettings, provider.id), parse(value), datetime.now(UTC))
    with Session(engine()) as db:
        rows = db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_key == 'reference:market.mairui.industry_flow').order_by(EvidenceVersion.version_number)).all()
        assert [row.payload['rows'][0]['name'] for row in rows] == ['合成A', '合成B', '合成A']
    value = envelope()
    for table in value['tables']:table['rows'] = []
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('mairui_funds'))
        store_reference_batch(db, provider, db.get(SourceSettings, provider.id), parse(value), datetime.now(UTC))
    assert all(not row['payload']['rows'] for row in client.get('/api/v1/reference-datasets').json()['data']['items'])


def test_failed_second_request_counts_both_attempts_and_persists_no_partial_tables(client, monkeypatch):
    headers = setup_source(client, 'mairui_funds', monkeypatch)
    opener = Opener([[sample_row('industry_flow')], HTTPError('https://api.mairuiapi.com/' + TOKEN, 503, TOKEN, {}, io.BytesIO())])
    monkeypatch.setattr('app.market.mairui.urllib.request.build_opener', lambda *args: opener)
    queued = client.post('/api/v1/data-sources/' + str(source_id('mairui_funds')) + '/refresh', json={}, headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
    assert queued.status_code == 202
    process(claim())
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun))
        assert run.reason == 'upstream_unavailable' and run.request_count == run.expected == 2 and run.accepted is None
        assert db.scalar(select(func.count()).select_from(EvidenceVersion)) == 0
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == MAIRUI_ACTION)) == 2
    assert TOKEN not in json.dumps(client.get('/api/v1/data-fetch-runs').json())


def test_pool_worker_without_verified_calendar_never_uses_upstream_or_quota(client, monkeypatch):
    headers = setup_source(client, 'mairui_pools', monkeypatch)
    def forbidden(*args, **kwargs):raise AssertionError('Must not request an inferred weekday.')
    monkeypatch.setattr('app.market.collect.fetch_mairui', forbidden)
    assert client.post('/api/v1/data-sources/' + str(source_id('mairui_pools')) + '/refresh', json={}, headers={**headers, 'Idempotency-Key': uuid.uuid4().hex}).status_code == 202
    process(claim())
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun));assert run.reason == 'calendar_not_ready' and run.request_count == 0
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == MAIRUI_ACTION)) == 0


def test_missing_credential_and_fast_or_symbol_configuration_are_refused(client, monkeypatch):
    headers = setup_source(client, 'mairui_funds', monkeypatch)
    monkeypatch.setattr(settings(), 'mairui_licence', None)
    response = client.post('/api/v1/data-sources/' + str(source_id('mairui_funds')) + '/refresh', json={}, headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
    assert response.status_code == 422 and response.json()['error']['code'] == 'credentials_missing'
    for change in [{'interval_seconds': 3599}, {'symbols': [{'exchange': 'SSE', 'symbol': '000001', 'asset_type': 'index'}]}]:
        response = client.post('/api/v1/data-sources', json={**configuration('mairui_funds'), **change}, headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
        assert response.status_code == 422
    result = client.get('/api/v1/data-sources').json()['data']['items'][0]
    assert result['credential_configured'] is False and result['collection_state'] == 'credentials_missing'


def test_daily_budget_ignores_previous_beijing_day_reservations(client, monkeypatch):
    setup_source(client, 'mairui_funds', monkeypatch)
    with Session(engine()) as db, db.begin():
        cfg = db.get(SourceSettings, source_id('mairui_funds'))
        db.add_all([AuditEvent(action=MAIRUI_ACTION, created_at=datetime.now(UTC) - timedelta(days=2)) for _ in range(450)])
        db.add(Job(owner_id=cfg.owner_id, kind='source.collect', payload={'source_id': str(cfg.source_id)}))
    reserve_mairui_request(claim())
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == MAIRUI_ACTION)) == 451
