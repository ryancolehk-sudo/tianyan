"""Synthetic recovery windows on disposable PostgreSQL; no real market calls.

Only the collector request clock is controlled. Worker claim, lease renewal,
final commit fencing and persisted supplier permits use their real DB clocks.
New Session instances model lost process objects, not a proven OS restart or a
real supplier outage. Late observations never acquire strict-history eligibility.
"""
import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import collect
from app.market.budget import SINA_ACTION, reserve_sina_request
from app.market.minute import parse_sina_minutes
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import InstrumentKey, Response, SourceFailure
from app.market.service import enqueue_collection, source_id
from app.models import AuditEvent, Job, Provider
from app.worker import claim, process
from test_data_sources import configure, login
from test_minute_collection import configuration

FAMILY = 'sina_minute_1m'
KEY = InstrumentKey('SSE', '603278', 'stock')
DAY = '2026-10-23'
EARLY = datetime.fromisoformat(DAY + 'T10:00:00+08:00')
RECEIVED = datetime.fromisoformat(DAY + 'T16:00:00+08:00')


def setup(client, monkeypatch, *, second_period=False):
    headers = login(client)
    configure(client, headers, configuration(), key='recovery-primary')
    if second_period:
        configure(client, headers, configuration('sina_minute_5m'), key='recovery-second')
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    return headers


def request_clock(monkeypatch):
    class RequestClock(datetime):
        @classmethod
        def now(cls, tz=None):
            # A timeout uses this same clock for start and receipt; a successful
            # Response declares a receipt 5 ms later. Do not patch worker/DB.
            value = RECEIVED - timedelta(milliseconds=5)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
    monkeypatch.setattr('app.market.collect.datetime', RequestClock)


def window(slots):
    return ('=(' + json.dumps([dict(day=DAY + ' ' + stamp + ':00', open='10', high='13', low='9',
        close=close, volume='0', amount=None) for stamp, close in slots]) + ');').encode()


def enqueue(family=FAMILY):
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        return enqueue_collection(db, source_id(family)).id


def seed_window(body):
    batch = parse_sina_minutes(body, KEY, '1m', EARLY, EARLY.date())
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(FAMILY))
        config = db.get(SourceSettings, provider.id)
        return collect.store_minute_batch(db, provider, config, batch, EARLY)


def bar_fingerprint(db):
    return [(row.id, row.source_key, row.version_number, row.content_hash, row.payload, row.received_at)
        for row in db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == source_id(FAMILY),
            EvidenceVersion.kind == 'bar').order_by(EvidenceVersion.source_key, EvidenceVersion.version_number))]


def test_timeout_preserves_saved_bars_and_records_failure_and_persisted_retry(client, monkeypatch):
    setup(client, monkeypatch)
    assert seed_window(window([('09:31', '11'), ('09:33', '12')])) == (2, 0)
    with Session(engine()) as db:
        original = bar_fingerprint(db)
    ident = enqueue()
    job = claim()
    assert job is not None and job.id == ident
    request_clock(monkeypatch)
    calls = []
    def timeout(url):
        calls.append(url)
        raise SourceFailure('timeout')
    monkeypatch.setattr('app.market.collect.fetch_https', timeout)
    process(job)
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == ident))
        saved = db.get(Job, ident)
        assert len(calls) == 1 and run.status == 'error' and run.reason == 'timeout'
        assert run.request_count == 1 and run.body_hash is None
        assert run.started_at <= run.received_at
        assert bar_fingerprint(db) == original
        assert saved.state == 'queued' and saved.error_code == 'timeout' and saved.attempts == 1
        assert saved.lease_token is None and saved.lease_until is None
        assert saved.available_at > run.received_at
        assert saved.available_at == db.get(SourceSettings, source_id(FAMILY)).next_run_at
    # A fresh connection observes both the immutable cached bars and the same
    # retry, rather than treating loss of Python objects as an empty cache.
    with Session(engine()) as db:
        assert bar_fingerprint(db) == original
        assert db.get(Job, ident).state == 'queued'
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == SINA_ACTION)) == 1


def test_expired_worker_cannot_store_after_new_session_reclaims_its_job(client, monkeypatch):
    setup(client, monkeypatch)
    ident = enqueue()
    stale_worker = claim()
    assert stale_worker is not None and stale_worker.id == ident
    original_token = stale_worker.lease_token
    request_clock(monkeypatch)
    replacements = []
    calls = []
    def interrupted_response(url):
        calls.append(url)
        # Simulate a worker that stalled past a real lease deadline. Production
        # SQL still checks its real clock and the new worker's actual token.
        with Session(engine()) as db, db.begin():
            live = db.get(Job, ident)
            assert live.lease_token == original_token
            live.lease_until = db.scalar(select(func.now())) - timedelta(seconds=1)
        replacement = claim()
        assert replacement is not None and replacement.id == ident
        assert replacement.lease_token != original_token and replacement.attempts == 2
        replacements.append(replacement)
        return Response(window([('09:31', '12')]), RECEIVED, 5, 200)
    monkeypatch.setattr('app.market.collect.fetch_https', interrupted_response)
    process(stale_worker)
    assert len(calls) == len(replacements) == 1
    with Session(engine()) as db:
        live = db.get(Job, ident)
        assert live.state == 'running' and live.lease_token == replacements[0].lease_token
        assert live.attempts == 2
        assert not db.scalar(select(EvidenceVersion.id).where(EvidenceVersion.source_id == source_id(FAMILY)))
        assert not db.scalar(select(DataFetchRun.id).where(DataFetchRun.job_id == ident))
        # The old attempt's already persisted permit remains spent; losing a
        # lease does not erase the supplier pacing window.
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == SINA_ACTION)) == 1
    # A stale process object cannot issue a second request under the new lease.
    monkeypatch.setattr('app.market.collect.fetch_https', lambda _: (_ for _ in ()).throw(AssertionError('stale HTTP')))
    process(stale_worker)
    with Session(engine()) as db:
        assert db.get(Job, ident).lease_token == replacements[0].lease_token
        assert not db.scalar(select(DataFetchRun.id).where(DataFetchRun.job_id == ident))


def test_current_day_response_fills_real_missing_slots_and_revisions_remain_idempotent(client, monkeypatch):
    setup(client, monkeypatch)
    assert seed_window(window([('09:31', '11'), ('09:33', '11')])) == (2, 0)
    with Session(engine()) as db:
        original = bar_fingerprint(db)
    raw = window([('09:31', '11'), ('09:32', '12'), ('09:33', '12')])
    ident = enqueue(); job = claim()
    assert job is not None and job.id == ident
    request_clock(monkeypatch)
    calls = []
    def recovered(url):
        calls.append(url)
        return Response(raw, RECEIVED, 5, 200)
    monkeypatch.setattr('app.market.collect.fetch_https', recovered)
    process(job)
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == ident))
        assert (run.status, run.expected, run.accepted, run.duplicates, run.rejected) == ('complete', 3, 2, 1, 0)
        assert run.body_hash == hashlib.sha256(raw).hexdigest()
        assert db.get(Job, ident).state == 'succeeded' and len(calls) == 1
        records = list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == source_id(FAMILY))
            .order_by(EvidenceVersion.source_key, EvidenceVersion.version_number)))
        assert len(records) == 4 and len({row.source_key for row in records}) == 3
        assert any(row.id == original[0][0] and row.content_hash == original[0][3] for row in records)
        corrected = [row for row in records if row.payload['source_clock_at'].endswith('01:33:00Z')]
        assert [row.payload['close'] for row in corrected] == ['11', '12']
        assert corrected[1].supersedes_id == corrected[0].id
        assert corrected[1].received_at == RECEIVED and corrected[0].received_at == EARLY
        assert all(row.historical_eligible is False and row.available_at is None for row in records)
        before_repeat = bar_fingerprint(db)
    # Repeated cached delivery uses a fresh transaction, not an in-memory set.
    batch = parse_sina_minutes(raw, KEY, '1m', RECEIVED + timedelta(seconds=1), RECEIVED.date())
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(FAMILY)); config = db.get(SourceSettings, provider.id)
        assert collect.store_minute_batch(db, provider, config, batch, RECEIVED + timedelta(seconds=1)) == (0, 3)
        assert bar_fingerprint(db) == before_repeat
    assert len(calls) == 1


def test_shared_budget_and_queued_wait_survive_loss_of_session_objects(client, monkeypatch):
    setup(client, monkeypatch, second_period=True)
    first_id = enqueue(); second_id = enqueue('sina_minute_5m')
    first = claim(); assert first is not None and first.id == first_id
    assert reserve_sina_request(first) is True
    with Session(engine()) as db:
        permit = db.scalar(select(AuditEvent.created_at).where(AuditEvent.action == SINA_ACTION))
        assert permit is not None
    second = claim(); assert second is not None and second.id == second_id
    assert reserve_sina_request(second) is False
    with Session(engine()) as db:
        deferred = db.get(Job, second_id)
        assert deferred.state == 'queued' and deferred.attempts == 0 and deferred.lease_token is None
        assert deferred.available_at == permit + timedelta(seconds=15)
        assert db.get(SourceSettings, source_id('sina_minute_5m')).next_run_at >= deferred.available_at
    # The real worker clock cannot claim this wait early; a fresh connection
    # does not reset or bypass its supplier permit ledger.
    assert claim() is None
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == SINA_ACTION)) == 1
        assert not db.scalar(select(DataFetchRun.id))
        assert not db.scalar(select(EvidenceVersion.id).where(EvidenceVersion.kind == 'bar'))


def test_unclaimed_queued_job_is_recovered_once_by_new_session_and_normal_worker(client, monkeypatch):
    setup(client, monkeypatch)
    ident = enqueue()
    with Session(engine()) as db:
        persisted = db.get(Job, ident)
        assert persisted.state == 'queued' and persisted.attempts == 0
        payload = dict(persisted.payload)
    # Only persisted ID/payload survive; no synthetic running token is supplied.
    job = claim()
    assert job is not None and job.id == ident and job.payload == payload
    assert job.attempts == 1 and job.lease_token is not None
    request_clock(monkeypatch)
    calls = []
    def fetch(url):
        calls.append(url)
        return Response(window([('09:31', '11')]), RECEIVED, 5, 200)
    monkeypatch.setattr('app.market.collect.fetch_https', fetch)
    process(job)
    assert claim() is None
    with Session(engine()) as db:
        saved = db.get(Job, ident)
        assert saved.state == 'succeeded' and saved.attempts == 1 and saved.lease_token is None
        assert saved.completed_at is not None
        assert db.scalar(select(func.count()).select_from(DataFetchRun).where(DataFetchRun.job_id == ident)) == 1
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.source_id == source_id(FAMILY))) == 1
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == SINA_ACTION)) == 1
    assert len(calls) == 1
