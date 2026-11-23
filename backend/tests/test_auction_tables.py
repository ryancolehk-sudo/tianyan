"""Two 短线侠 auction tables on a disposable PostgreSQL.

Covers what the frozen v2 contract asks of this backend slice: source
registration, the worker path, one transaction for summary + stocks, version
behaviour (duplicate vs. changed batch), permission revocation, failure that
keeps the cached batch, the shared persisted budget across a restart, and the
actual coverage state.

HTTP is replaced by a test double; parsing, storage and reads are real. No
synthetic quote ever reaches a production path: the double is installed only
inside these tests, and every fixture is synthetic.
"""
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.budget import AUCTION_ACTION, AUCTION_INTERVAL_SECONDS
from app.market.collect import store_observation
from app.market.models import DataFetchRun, InstrumentOrigin
from app.market.protocol import Response, SourceFailure
from app.market.registry import SOURCES
from app.market.news_feeds import NEWS_FEEDS
from app.market.service import source_id
from app.models import AuditEvent, Instrument, Job, Provider, User
from app.worker import claim, process
from test_data_sources import configure, login

FIXTURES = Path(__file__).parent / 'fixtures/auction'
CURRENT = 'duanxianxia_auction'
HISTORY = 'duanxianxia_auction_history'
SUMMARY = 'market.duanxianxia.auction.summary'
STOCKS = 'market.duanxianxia.auction.stocks'


def response(payload, elapsed=12):
    # received_at is read when the response arrives, so it can never precede started_at.
    return lambda kind: Response(body=payload, received_at=datetime.now(UTC), elapsed_ms=elapsed, status=200)


@pytest.fixture
def no_pacing(monkeypatch):
    """Most tests run several collections; pacing is exercised in its own test."""
    monkeypatch.setattr('app.market.budget.AUCTION_INTERVAL_SECONDS', 0)
    monkeypatch.setattr('app.market.auction_schedule.LIVE_INTERVAL_SECONDS', 0)


def body(name):
    return (FIXTURES / name).read_bytes()


def current_body():
    return body('duanxianxia_current_table.b64')


def history_body():
    return body('duanxianxia_history_days.json')


def configuration(family):
    return {'family': family, 'symbols': [],
        'interval_seconds': 65 if family == CURRENT else 3600,
        'rights_basis': 'Self-authored synthetic fixtures for this isolated test only; no upstream grant.',
        'rights': {'display': 'allowed', 'cache': 'allowed', 'export': 'unknown', 'redistribute': 'unknown'},
        'entitlement_status': 'verified', 'enabled': True}


def configure_family(client, headers, family, **overrides):
    payload = configuration(family)
    payload.update(overrides)
    configure(client, headers, payload, key=uuid.uuid4().hex)
    return payload


def owner_id():
    with Session(engine()) as db:
        return db.scalar(select(User.id).where(User.username == 'owner'))


def new_job(family):
    # available_at is explicit: the container clock can run ahead of the host,
    # which would otherwise hide a fresh job from claim().
    with Session(engine()) as db, db.begin():
        job = Job(owner_id=owner_id(), kind='source.collect', payload={'source_id': str(source_id(family))},
                  available_at=datetime.now(UTC) - timedelta(seconds=1))
        db.add(job)
        db.flush()
        return job.id


def run(monkeypatch, family, payload, job_id=None):
    """Claim one queued job and run the real worker path against a fixed response."""
    job_id = job_id or new_job(family)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr('app.market.collect.fetch_auction_table', response(payload))
    job = claim()
    assert job is not None, 'no claimable job'
    assert str(job.payload['source_id']) == str(source_id(family))
    assert job.id == job_id, 'a requeued job must not be reclaimed before its window'
    process(job)
    return job


def run_failing(monkeypatch, family, reason='timeout', job_id=None):
    job_id = job_id or new_job(family)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)

    def refuse(kind):
        raise SourceFailure(reason)
    monkeypatch.setattr('app.market.collect.fetch_auction_table', refuse)
    job = claim()
    assert job is not None and str(job.payload['source_id']) == str(source_id(family))
    assert job.id == job_id
    process(job)
    return job


def job_row(job_id):
    with Session(engine()) as db:
        return db.get(Job, job_id)


def runs(family):
    with Session(engine()) as db:
        return list(db.scalars(select(DataFetchRun).where(DataFetchRun.source_id == source_id(family))
            .order_by(DataFetchRun.received_at, DataFetchRun.id)))


def batches(family):
    with Session(engine()) as db:
        return list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == source_id(family))
            .order_by(EvidenceVersion.source_key)))


def dataset(client, headers, name):
    response = client.get('/api/v1/reference-datasets?dataset=' + name, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()['data']['items']


def test_both_tables_are_registered_and_take_no_symbol_list(client):
    headers = login(client)
    index = client.get('/api/v1/data-sources', headers=headers).json()['data']
    adapters = {item['family']: item for item in index['available_adapters']}
    from app.market.tushare_specs import SPECS as TUSHARE_SPECS
    from app.market.eastmoney_trading import FAMILIES as EASTMONEY_FAMILIES
    from app.market.review_protocol import PUBLIC_FAMILIES
    from app.market.connected_specs import FAMILIES as CONNECTED_FAMILIES
    from app.market.direction_specs import PUBLIC_FAMILIES as DIRECTION_FAMILIES
    assert set(adapters) == set(SOURCES)
    # The full-market Tencent adapter is separate from focused Tencent quotes.
    assert {'tencent_web', 'tencent_market_rt'} <= set(adapters)
    assert len(SOURCES) == len(adapters) == 30 + len(NEWS_FEEDS) + len(TUSHARE_SPECS) + len(EASTMONEY_FAMILIES) + len(PUBLIC_FAMILIES) + len(CONNECTED_FAMILIES) + len(DIRECTION_FAMILIES)
    assert PUBLIC_FAMILIES == {'review_tencent_indices', 'review_sina_indices', 'review_cls_news', 'review_kpl_new_highs'}
    assert 'chinanews_finance' in NEWS_FEEDS and 'cninfo_announcements' in adapters
    for family in (CURRENT, HISTORY):
        assert adapters[family]['capabilities'] == [SUMMARY, STOCKS]
        assert adapters[family]['kind'] == 'market'
    assert adapters[CURRENT]['minimum_interval'] == 65 and adapters[HISTORY]['minimum_interval'] == 3600

    with_symbols = configuration(CURRENT)
    with_symbols['symbols'] = [{'exchange': 'SSE', 'symbol': '600000', 'asset_type': 'stock'}]
    rejected = client.post('/api/v1/data-sources', json=with_symbols,
                           headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
    assert rejected.status_code == 422 and rejected.json()['error']['code'] == 'symbols_not_applicable'

    # Rights are re-checked at configure time: an unknown scope cannot be enabled.
    unknown = configuration(CURRENT)
    unknown['rights'] = {'display': 'unknown', 'cache': 'unknown', 'export': 'unknown', 'redistribute': 'unknown'}
    denied = client.post('/api/v1/data-sources', json=unknown,
                         headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
    assert denied.status_code == 422 and denied.json()['error']['code'] == 'permission_missing'


def test_current_table_writes_summary_and_stocks_as_one_batch(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    job = run(monkeypatch, CURRENT, current_body())
    assert job_row(job.id).state == 'succeeded'

    stored = batches(CURRENT)
    assert sorted(row.payload['dataset'] for row in stored) == sorted([SUMMARY, STOCKS])
    summary = next(row for row in stored if row.payload['dataset'] == SUMMARY)
    stocks = next(row for row in stored if row.payload['dataset'] == STOCKS)
    head = summary.payload['rows'][0]
    batch_hash = head['batch_hash']
    assert batch_hash and len(batch_hash) == 64
    assert {row['batch_hash'] for row in stocks.payload['rows']} == {batch_hash}
    assert summary.source_key.endswith(':summary') and stocks.source_key.endswith(':stocks')
    assert summary.source_key.rsplit(':', 1)[0] == stocks.source_key.rsplit(':', 1)[0]

    assert head['trade_day'] == '2026-10-21' and head['source_date_raw'] == '2026-10-21'
    assert head['table_kind'] == 'current' and head['row_count'] == '4'
    assert (head['header_total_cny'], head['total_fd_0915_cny'], head['total_fd_0925_cny']) == (
        '760000000', '420000000', '760000000')
    rows = stocks.payload['rows']
    assert [(row['source_order'], row['code']) for row in rows] == [
        ('1', '600001'), ('2', '000002'), ('3', '300003'), ('4', '600004')]
    assert rows[0]['fd_0915_cny'] == '320000000'
    # The vendor gives a date and three header columns, never a row-level clock.
    for row in rows + [head]:
        assert 'checkpoint' not in row and 'quote_time' not in row

    recorded = runs(CURRENT)
    assert len(recorded) == 1
    assert (recorded[0].status, recorded[0].expected, recorded[0].accepted, recorded[0].duplicates) == (
        'complete', 5, 2, 0)
    assert recorded[0].request_count == 1 and recorded[0].capability == SUMMARY + '+' + STOCKS
    assert len(dataset(client, headers, SUMMARY)) == 1
    assert len(dataset(client, headers, STOCKS)) == 1
    # The vendor table never creates a local security identity.
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Instrument)) == 0
        assert db.scalar(select(func.count()).select_from(InstrumentOrigin)) == 0


def test_identical_table_is_a_duplicate_and_writes_no_version(client, monkeypatch, no_pacing):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    run(monkeypatch, CURRENT, current_body())
    first = {row.id: row.content_hash for row in batches(CURRENT)}
    assert len(first) == 2

    run(monkeypatch, CURRENT, current_body())
    assert len(batches(CURRENT)) == 2, 'the same table must not be written twice'
    assert {row.id: row.content_hash for row in batches(CURRENT)} == first
    assert all(row.version_number == 1 for row in batches(CURRENT))
    repeated = runs(CURRENT)[-1]
    assert (repeated.accepted, repeated.duplicates, repeated.status) == (0, 2, 'complete')
    assert len(dataset(client, headers, SUMMARY)) == 1


def test_history_response_stores_every_day_in_the_same_transaction(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, HISTORY)
    run(monkeypatch, HISTORY, history_body())
    stored = batches(HISTORY)
    assert len(stored) == 4
    summaries = [row for row in stored if row.payload['dataset'] == SUMMARY]
    assert sorted(row.payload['rows'][0]['trade_day'] for row in summaries) == ['2026-10-19', '2026-10-20']
    assert all(row.payload['rows'][0]['table_kind'] == 'history' for row in summaries)
    hashes = {row.payload['rows'][0]['batch_hash'] for row in summaries}
    assert len(hashes) == 1, 'all dates share the same raw HTTP response hash; day remains part of the pair key'
    stocks = [row for row in stored if row.payload['dataset'] == STOCKS]
    assert [len(row.payload['rows']) for row in stocks] == [1, 2]
    assert all(row['change_pct'] is None for row in stocks[0].payload['rows'] + stocks[1].payload['rows'])

    recorded = runs(HISTORY)[-1]
    assert (recorded.status, recorded.accepted, recorded.rejected) == ('complete', 4, 0)
    assert recorded.expected == 5  # one summary plus the raw rows of each day
    assert recorded.request_count == 1
    assert len(dataset(client, headers, SUMMARY)) == 2


def test_a_changed_day_appends_a_new_batch_and_keeps_the_old_one(client, monkeypatch, no_pacing):
    headers = login(client)
    configure_family(client, headers, HISTORY)
    run(monkeypatch, HISTORY, history_body())
    before = batches(HISTORY)
    old_day_hashes = {}
    for row in before:
        if row.payload['dataset'] == SUMMARY:
            old_day_hashes.setdefault(row.payload['rows'][0]['trade_day'], set()).add(
                row.payload['rows'][0]['batch_hash'])

    payload = json.loads(history_body())
    # Only the 09:25 封单 amount of one row on 2026-10-20 changes; that is the
    # content the vendor batch hash covers.
    payload['2026-10-20']['table'] = payload['2026-10-20']['table'].replace('<span>560万</span>', '<span>660万</span>')
    run(monkeypatch, HISTORY, json.dumps(payload).encode())

    after = batches(HISTORY)
    assert len(after) == 8, 'a changed raw response appends a version for both dates'
    assert {row.id for row in before} <= {row.id for row in after}
    day_hashes = {}
    for row in after:
        if row.payload['dataset'] == SUMMARY:
            day = row.payload['rows'][0]['trade_day']
            day_hashes.setdefault(day, set()).add(row.payload['rows'][0]['batch_hash'])
    assert old_day_hashes['2026-10-19'] < day_hashes['2026-10-19'], 'the changed raw response identity is preserved for each day'
    assert old_day_hashes['2026-10-20'] < day_hashes['2026-10-20'], 'the previous day batch stays readable'
    assert len(day_hashes['2026-10-20']) == 2

    changed = runs(HISTORY)[-1]
    assert (changed.accepted, changed.duplicates, changed.status) == (4, 0, 'complete')
    assert len(dataset(client, headers, SUMMARY)) == 2


def test_dropped_rows_are_summed_and_never_reported_as_a_clean_success(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, HISTORY)
    payload = json.loads(history_body())
    duplicate = ("<td code='600021' class='fd st600021' ><b>样本七号<i><p>军工</p></i></b><br>"
                 "<span>-</span> | <span>-</span> | <span>-</span></td>")
    payload['2026-10-19']['table'] = payload['2026-10-19']['table'].replace('</table>', duplicate + '</table>')
    run(monkeypatch, HISTORY, json.dumps(payload).encode())

    recorded = runs(HISTORY)[-1]
    assert recorded.expected == 6, 'expected sums one summary plus every raw row of each day'
    assert (recorded.accepted, recorded.rejected, recorded.status) == (4, 1, 'partial')
    assert len(batches(HISTORY)) == 4, 'the kept rows are stored; the dropped duplicate is not invented'
    assert len(dataset(client, headers, STOCKS)) == 2


def test_a_failed_batch_rolls_back_every_day_of_the_response(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, HISTORY)
    calls = {'count': 0}
    original = store_observation

    def failing(*args, **kwargs):
        calls['count'] += 1
        if calls['count'] == 3:  # first day is written, second day fails
            raise SourceFailure('schema_changed')
        return original(*args, **kwargs)
    monkeypatch.setattr('app.market.collect.store_observation', failing)
    job_id = new_job(HISTORY)
    job = run(monkeypatch, HISTORY, history_body(), job_id=job_id)
    assert job_row(job.id).state == 'failed'
    assert batches(HISTORY) == [], 'no day may survive a failed response'
    assert runs(HISTORY) == []


def test_an_empty_table_is_unavailable_and_writes_nothing(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, HISTORY)
    payload = json.loads(history_body())
    payload['2026-10-19']['table'] = '<table class="table stock"></table>'
    job_id = new_job(HISTORY)
    job = run(monkeypatch, HISTORY, json.dumps(payload).encode(), job_id=job_id)

    failed = job_row(job.id)
    assert (failed.state, failed.error_code) == ('failed', 'instrument_unavailable')
    assert batches(HISTORY) == []
    recorded = runs(HISTORY)
    assert len(recorded) == 1
    assert (recorded[0].status, recorded[0].reason, recorded[0].accepted) == ('unavailable', 'instrument_unavailable', None)
    assert dataset(client, headers, SUMMARY) == []


def test_failure_keeps_the_cached_batch(client, monkeypatch, no_pacing):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    run(monkeypatch, CURRENT, current_body())
    job_id = new_job(CURRENT)
    job = run_failing(monkeypatch, CURRENT, 'timeout', job_id=job_id)

    requeued = job_row(job.id)
    assert (requeued.state, requeued.error_code) == ('queued', 'timeout'), 'a timeout is retried, not terminal'
    recorded = runs(CURRENT)[-1]
    assert (recorded.status, recorded.reason) == ('error', 'timeout')
    assert len(batches(CURRENT)) == 2, 'a failed run must not delete the cached batch'
    assert len(dataset(client, headers, SUMMARY)) == 1
    states = client.get('/api/v1/data-sources', headers=headers).json()['data']['items']
    assert next(row for row in states if row['family'] == CURRENT)['collection_state'] == 'error'


def test_revoked_display_hides_reads_and_collection_without_deleting_rows(client, monkeypatch, no_pacing):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    run(monkeypatch, CURRENT, current_body())
    assert dataset(client, headers, SUMMARY)

    with Session(engine()) as db, db.begin():
        db.execute(update(Provider).where(Provider.id == source_id(CURRENT))
                   .values(rights={'display': 'denied', 'cache': 'allowed', 'export': 'unknown', 'redistribute': 'unknown'}))
    assert dataset(client, headers, SUMMARY) == []
    assert dataset(client, headers, STOCKS) == []

    job_id = new_job(CURRENT)
    job = run(monkeypatch, CURRENT, current_body(), job_id=job_id)
    assert job_row(job.id).state == 'failed' and job_row(job.id).error_code == 'permission_missing'
    assert len(batches(CURRENT)) == 2, 'revocation hides history, it does not delete it'
    states = client.get('/api/v1/data-sources', headers=headers).json()['data']['items']
    assert next(row for row in states if row['family'] == CURRENT)['collection_state'] == 'permission_missing'

    # Restoring the display right makes the same stored batch readable again.
    with Session(engine()) as db, db.begin():
        db.execute(update(Provider).where(Provider.id == source_id(CURRENT))
                   .values(rights={'display': 'allowed', 'cache': 'allowed', 'export': 'unknown', 'redistribute': 'unknown'}))
    assert len(dataset(client, headers, SUMMARY)) == 1


def test_shared_budget_is_persisted_across_both_tables_and_restarts(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    configure_family(client, headers, HISTORY)
    run(monkeypatch, CURRENT, current_body())
    assert len(runs(CURRENT)) == 1

    waiting = new_job(HISTORY)
    job = run(monkeypatch, HISTORY, history_body(), job_id=waiting)
    queued = job_row(job.id)
    assert (queued.state, queued.error_code, queued.attempts) == ('queued', 'source_budget_wait', 0)
    assert runs(HISTORY) == [], 'a paced run must not reach the vendor'

    # A restart loses nothing: the permit is a row, not process memory.
    engine().dispose()
    assert client.get('/api/v1/reference-datasets?dataset=' + SUMMARY, headers=headers).status_code == 200
    after_restart = new_job(CURRENT)
    job = run(monkeypatch, CURRENT, current_body(), job_id=after_restart)
    assert job_row(job.id).error_code == 'source_budget_wait'
    assert len(runs(CURRENT)) == 1

    with Session(engine()) as db, db.begin():
        db.execute(update(AuditEvent).where(AuditEvent.action == AUCTION_ACTION)
                   .values(created_at=datetime.now(UTC) - timedelta(seconds=AUCTION_INTERVAL_SECONDS + 5)))
    later = new_job(HISTORY)
    job = run(monkeypatch, HISTORY, history_body(), job_id=later)
    assert job_row(job.id).state == 'succeeded'
    assert len(runs(HISTORY)) == 1 and len(batches(HISTORY)) == 4


def test_concurrent_workers_store_one_batch(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr('app.market.budget.AUCTION_INTERVAL_SECONDS', 0)
    monkeypatch.setattr('app.market.auction_schedule.LIVE_INTERVAL_SECONDS', 0)
    first, second = new_job(CURRENT), new_job(CURRENT)
    active = claim()
    assert active and active.id in {first, second}
    assert claim() is None, 'the same source cannot acquire two valid running leases'
    barrier = threading.Barrier(2)
    fetched = []
    def concurrent_response(kind):
        result = response(current_body())(kind)
        fetched.append(kind)
        barrier.wait(timeout=15)
        return result
    monkeypatch.setattr('app.market.collect.fetch_auction_table', concurrent_response)
    # Defensively exercise duplicate delivery of the same valid lease. Both
    # handlers fetch before either stores; only its one fenced outcome commits.
    jobs = [active, active]
    errors = []

    def work(job):
        try:
            process(job)
        except Exception as error:  # pragma: no cover - surfaced by the assert below
            errors.append(error)
    threads = [threading.Thread(target=work, args=(job,)) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(fetched) == 2
    assert job_row(active.id).state == 'succeeded'
    next_job = claim()
    assert next_job and next_job.id == ({first, second} - {active.id}).pop()
    monkeypatch.setattr('app.market.collect.fetch_auction_table', response(current_body()))
    process(next_job)
    assert len(batches(CURRENT)) == 2, 'two workers must not double-write one batch'
    assert sum(row.accepted or 0 for row in runs(CURRENT)) == 2
    assert sum(row.duplicates or 0 for row in runs(CURRENT)) == 2


def test_restart_reproduces_the_same_batches(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, HISTORY)
    run(monkeypatch, HISTORY, history_body())
    before = sorted((row.source_key, row.content_hash, len(row.payload['rows'])) for row in batches(HISTORY))
    engine().dispose()
    after = sorted((row.source_key, row.content_hash, len(row.payload['rows'])) for row in batches(HISTORY))
    assert after == before
    items = dataset(client, headers, STOCKS)
    assert len(items) == 2 and sorted(len(item['payload']['rows']) for item in items) == [1, 2]
    summaries = {item['payload']['rows'][0]['trade_day']: item['payload']['rows'][0]['batch_hash']
                 for item in dataset(client, headers, SUMMARY)}
    for item in items:
        row = item['payload']['rows'][0]
        assert row['batch_hash'] == summaries[row['trade_day']], 'summary and stocks stay paired'


def test_b_type_batches_are_not_served_by_the_market_auction_route(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    run(monkeypatch, CURRENT, current_body())

    # B 型 stays on reference-datasets; the A-type route keeps its two-value family.
    assert client.get('/api/v1/market-auction', headers=headers).json()['data']['items'] == []
    assert client.get('/api/v1/market-auction?family=' + CURRENT, headers=headers).status_code == 422
    assert len(dataset(client, headers, SUMMARY)) == 1


def test_reads_never_contact_the_vendor(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    run(monkeypatch, CURRENT, current_body())
    monkeypatch.setattr('app.market.collect.fetch_auction_table',
        lambda kind: pytest.fail('GET must not collect'))
    for url in ['/api/v1/reference-datasets', '/api/v1/reference-datasets?dataset=' + SUMMARY,
                '/api/v1/data-sources', '/api/v1/data-coverage']:
        assert client.get(url, headers=headers).status_code == 200


def test_coverage_reports_the_actual_state_of_the_new_families(client, monkeypatch):
    headers = login(client)

    def capability(ident):
        items = client.get('/api/v1/data-coverage', headers=headers).json()['data']['items']
        return next(item for item in items if item['id'] == ident)

    before = capability('D13')
    assert {CURRENT, HISTORY} <= set(before['families'])
    assert before['state'] == 'pending' and capability('D14')['state'] == 'pending'
    assert capability('D15')['state'] == 'blocked'

    configure_family(client, headers, HISTORY)
    run(monkeypatch, HISTORY, history_body())
    assert capability('D13')['state'] == 'partial' and capability('D14')['state'] == 'partial'
    assert capability('D15')['state'] == 'blocked', 'L2 fields are still absent'
