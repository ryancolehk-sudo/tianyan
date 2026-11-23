"""Real disposable PostgreSQL, synthetic jobs/clocks, and no upstream network."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market.auction_schedule import CURRENT, next_collection
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response, SHANGHAI, SourceFailure
from app.market.realtime_schedule import deferred_realtime, retry_seconds, wake_session_waits
from app.market.service import enqueue_collection, schedule_sources, source_id
from app.models import AuditEvent, Job, Provider, User
from app.worker import claim, finish, process
from test_auction_tables import configure_family, job_row, new_job, owner_id
from test_data_sources import login


@pytest.fixture(autouse=True)
def reject_supplier_calls(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('fairness/retry verification must not contact any supplier')
    for name in ('fetch_https', 'fetch_auction_table', 'request_tushare'):
        monkeypatch.setattr('app.market.collect.' + name, denied)
    monkeypatch.setattr('app.market.protocol.fetch_https', denied)


def owner(client):
    login(client)
    with Session(engine()) as db:
        return db.scalar(select(User.id).where(User.username == 'owner'))


def queue(owner_id, family, due, **extra):
    with Session(engine()) as db, db.begin():
        job = Job(owner_id=owner_id, kind='source.collect',
            payload={'source_id': str(source_id(family)), **extra.pop('payload', {})},
            available_at=due, **extra)
        db.add(job)
        db.flush()
        return job.id


def test_continuous_auction_cannot_overtake_an_overdue_quote_forever(client):
    ident = owner(client)
    base = datetime.now(UTC) - timedelta(minutes=2)
    quote = queue(ident, 'tushare_rt_indices', base)
    selected = []
    for number in range(10):
        queue(ident, CURRENT, base + timedelta(seconds=3 * number))
        job = claim('realtime')
        selected.append(job.id)
        assert finish(job.id, job.lease_token)
        if job.id == quote:
            break
    assert quote in selected and len(selected) <= 6


def test_older_deadline_is_served_before_a_new_fast_feed(client):
    ident = owner(client)
    clock = datetime.now(UTC)
    quote = queue(ident, 'tushare_market_rt', clock - timedelta(seconds=40))
    queue(ident, CURRENT, clock - timedelta(seconds=1))
    assert claim('realtime').id == quote


def test_scheduled_bulk_funds_do_not_block_realtime_slots(client):
    ident = owner(client)
    due = datetime.now(UTC) - timedelta(minutes=2)
    bulk = queue(ident, 'eastmoney_funds', due, payload={'scheduled_eastmoney': True}, attempts=4)
    strength = queue(ident, 'direction_kpl_strength', due + timedelta(seconds=1))
    emotion = queue(ident, 'direction_kpl_emotion', due + timedelta(seconds=2))
    quotes = queue(ident, 'tushare_market_rt', due + timedelta(seconds=3))
    assert claim('realtime').id == strength
    sampled = claim('realtime')
    assert sampled.id == emotion
    assert finish(sampled.id, sampled.lease_token)
    assert claim('realtime').id == quotes
    recovery = claim('recovery')
    assert recovery.id == bulk and recovery.attempts == 5


def test_two_concurrent_slots_never_claim_the_same_source(client):
    ident = owner(client)
    due = datetime.now(UTC) - timedelta(seconds=60)
    queue(ident, CURRENT, due)
    queue(ident, CURRENT, due)
    queue(ident, 'tushare_rt_indices', due)
    barrier = threading.Barrier(2)
    def start():
        barrier.wait(timeout=5)
        return claim('realtime')
    with ThreadPoolExecutor(max_workers=2) as slots:
        results = [future.result(timeout=15) for future in [slots.submit(start), slots.submit(start)]]
    assert all(results)
    assert len({job.payload['source_id'] for job in results}) == 2
    assert claim('realtime') is None


@pytest.mark.parametrize('other_family', ['tdx_depth5', 'tdx_minute', 'tdx_full_minute'])
def test_native_tdx_execution_group_leaves_a_live_slot_for_http_quotes(client, other_family):
    ident = owner(client)
    due = datetime.now(UTC) - timedelta(minutes=2)
    first_id = queue(ident, 'tdx_quotes', due)
    other_id = queue(ident, other_family, due + timedelta(seconds=1))
    http_id = queue(ident, 'tushare_rt_indices', due + timedelta(seconds=2))
    barrier = threading.Barrier(2)
    def start():
        barrier.wait(timeout=5)
        return claim('realtime')
    with ThreadPoolExecutor(max_workers=2) as slots:
        jobs = [future.result(timeout=15) for future in [slots.submit(start), slots.submit(start)]]
    assert all(jobs)
    assert http_id in {job.id for job in jobs}
    active = next(job for job in jobs if job.id != http_id)
    assert active.id in {first_id, other_id}
    waiting_id = other_id if active.id == first_id else first_id
    assert job_row(waiting_id).state == 'queued'
    assert claim('realtime') is None
    assert claim('recovery') is None
    assert finish(active.id, active.lease_token)
    assert not finish(active.id, active.lease_token), 'a released token must not regain write authority'
    next_job = claim('recovery' if other_family == 'tdx_full_minute' else 'realtime')
    assert next_job and next_job.id == waiting_id
    if other_family == 'tdx_full_minute':
        # Full-market SDK work runs in recovery, holds the same SDK group, and
        # leaves realtime workers available for unrelated HTTP providers.
        queue(ident, 'tdx_minute', due)
        fresh_http = queue(ident, 'tencent_web', due + timedelta(seconds=1))
        assert claim('realtime').id == fresh_http
        assert claim('realtime') is None


def test_slow_source_valid_lease_leaves_other_slot_free(client):
    ident = owner(client)
    due = datetime.now(UTC) - timedelta(seconds=60)
    first = queue(ident, CURRENT, due)
    other = queue(ident, 'tushare_rt_indices', due)
    queue(ident, CURRENT, due)
    active = claim('realtime')
    assert active.id == first
    # Leave the first lease active, as during a slow HTTP/parse operation.
    assert claim('realtime').id == other
    assert claim('realtime') is None


def test_expired_takeover_fences_old_token_and_other_same_source_job(client):
    ident = owner(client)
    clock = datetime.now(UTC)
    old_token = uuid.uuid4()
    first = queue(ident, CURRENT, clock - timedelta(minutes=2), state='running',
        attempts=4, lease_token=old_token, lease_until=clock - timedelta(seconds=1))
    queue(ident, CURRENT, clock - timedelta(minutes=1))
    current = claim('all')
    assert current.id == first and current.attempts == 5
    assert current.lease_token != old_token
    assert not finish(first, old_token)
    assert claim('realtime') is None


def test_future_retry_is_not_pulled_forward_by_an_old_created_time(client):
    ident = owner(client)
    clock = datetime.now(UTC)
    queue(ident, CURRENT, clock + timedelta(seconds=60), created_at=clock - timedelta(days=3))
    assert claim('realtime') is None


def test_old_calendar_months_use_recovery_without_occupying_current_calendar_slot(client):
    ident = owner(client)
    clock = datetime.now(SHANGHAI)
    with Session(engine()) as db, db.begin():
        current = Job(owner_id=ident, kind='review.collect', payload={
            'source_id': str(source_id('tushare_calendar_sse')),
            'review_context': {'dataset': 'calendar_sse', 'day': clock.date().isoformat()}},
            available_at=clock - timedelta(seconds=1))
        historical = Job(owner_id=ident, kind='review.collect', payload={
            'source_id': str(source_id('tushare_calendar_szse')),
            'review_context': {'dataset': 'calendar_szse', 'day': (clock.date() - timedelta(days=31)).isoformat()}},
            available_at=clock - timedelta(minutes=1))
        db.add_all([current, historical]); db.flush()
        current_id, historical_id = current.id, historical.id
    assert claim('realtime').id == current_id
    assert claim('recovery').id == historical_id


def test_due_deadlines_and_pending_jobs_do_not_crowd_the_five_source_batch(client, monkeypatch):
    ident = owner(client)
    clock = datetime.now(UTC)
    families = ('tushare_market_rt', 'tushare_rt_indices', 'tencent_web', 'ashare_public', 'ashare_sector_heat')
    with Session(engine()) as db, db.begin():
        for family in (*families, CURRENT):
            provider = Provider(id=source_id(family), family=family, name='Synthetic fairness source', kind='api',
                adapter_version='synthetic', configured=True, enabled=True, entitlement_status='verified',
                rights={'display': 'allowed', 'cache': 'allowed'})
            db.add(provider); db.flush()
            db.add(SourceSettings(source_id=provider.id, owner_id=ident, rights_basis='synthetic test only',
                config={'symbols': []}, interval_seconds=65, configured_at=clock - timedelta(days=1),
                next_run_at=clock - timedelta(seconds=1 if family == CURRENT else 40)))
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr('app.market.service.now', lambda db: clock)
    monkeypatch.setattr('app.market.service.resolve_source_credentials', lambda *a, **k: SimpleNamespace(reason=None))
    monkeypatch.setattr('app.integrations.routing.collection_allowed', lambda *a: True)
    monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime', lambda *a: None)
    monkeypatch.setattr('app.market.intraday_schedule.deferred_intraday', lambda *a, **k: None)
    selected = []
    def enqueue(db, source, **kwargs):
        selected.append(source)
        # Intentionally keep the old config due, modeling a long-running task.
        # The pending intent must not occupy the next batch's five slots.
        db.add(Job(owner_id=ident, kind='source.collect', payload={'source_id': str(source)}, available_at=clock))
    monkeypatch.setattr('app.market.service.enqueue_collection', enqueue)
    schedule_sources()
    assert set(selected) == {source_id(family) for family in families}
    schedule_sources()
    assert selected[-1] == source_id(CURRENT) and len(selected) == 6


def test_two_slots_share_the_persisted_auction_budget(client, monkeypatch):
    from app.market.budget import AUCTION_ACTION, reserve_auction_request
    from test_auction_tables import HISTORY
    headers = login(client)
    configure_family(client, headers, CURRENT)
    configure_family(client, headers, HISTORY)
    frozen_collector(monkeypatch)
    new_job(CURRENT); new_job(HISTORY)
    current, history = claim('realtime'), claim('general')
    assert current and history
    assert reserve_auction_request(current)
    assert not reserve_auction_request(history)
    assert job_row(history.id).attempts == 0
    assert job_row(history.id).error_code == 'source_budget_wait'
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == AUCTION_ACTION)) == 1


@pytest.mark.parametrize('value,expected', [
    ('09:35:00', '09:36:05'), ('10:00:00', '10:01:05'),
    ('11:29:50', '11:30:55'), ('13:00:00', '13:01:05'),
    ('14:59:50', '15:00:55'), ('15:04:50', '09:15:00'),
])
def test_post_0935_uses_configured_interval_and_stops_at_session_end(value, expected):
    clock = datetime.fromisoformat('2026-10-27T' + value + '+08:00')
    candidate = next_collection(CURRENT, clock, 65)
    assert candidate.strftime('%H:%M:%S') == expected
    if value == '15:04:50':
        assert candidate.date() == clock.date() + timedelta(days=1)


def test_calendar_local_recheck_is_five_seconds_and_ready_wait_is_woken(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    clock = datetime.fromisoformat('2026-10-27T09:20:00+08:00')
    monkeypatch.setattr('app.market.mairui.pool_day', lambda *a, **k: (_ for _ in ()).throw(SourceFailure('calendar_not_ready')))
    with Session(engine()) as db:
        assert deferred_realtime(db, CURRENT, clock, 65) == clock + timedelta(seconds=5)
    with Session(engine()) as db, db.begin():
        config = db.get(SourceSettings, source_id(CURRENT))
        config.next_run_at = clock + timedelta(hours=1)
        waiting = Job(owner_id=config.owner_id, kind='source.collect', payload={
            'source_id': str(source_id(CURRENT)), 'scheduled_realtime': True},
            error_code='intraday_window_wait', available_at=config.next_run_at)
        cooled = Job(owner_id=config.owner_id, kind='source.collect', payload={
            'source_id': str(source_id('tushare_market_rt')), 'scheduled_realtime': True},
            error_code='source_budget_wait', available_at=clock + timedelta(minutes=3))
        db.add_all([waiting, cooled]); db.flush()
        waiting_id, cooled_id = waiting.id, cooled.id
    monkeypatch.setattr('app.market.mairui.pool_day', lambda *a, **k: clock.date())
    with Session(engine()) as db, db.begin():
        wake_session_waits(db, clock)
    assert job_row(waiting_id).available_at == clock
    assert job_row(cooled_id).available_at == clock + timedelta(minutes=3)


def frozen_collector(monkeypatch):
    # Always before the real DB clock so persisted available_at can be claimed
    # without changing PostgreSQL or depending on the hour CI runs.
    clock = [datetime.now(SHANGHAI).replace(hour=9, minute=20, second=0, microsecond=0) - timedelta(days=1)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz or UTC)
    monkeypatch.setattr('app.market.collect.datetime', Clock)
    monkeypatch.setattr('app.market.budget.now', lambda db: clock[0])
    monkeypatch.setattr('app.market.service.now', lambda db: clock[0])
    monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime', lambda *a: None)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    return clock


def test_transient_backoff_survives_new_sessions_jobs_and_legacy_all_claim(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    clock = frozen_collector(monkeypatch)
    def timeout(*a, **k):
        raise SourceFailure('timeout')
    monkeypatch.setattr('app.market.collect.fetch_auction_table', timeout)
    ident = new_job(CURRENT)
    for number, delay in enumerate((5, 15, 30, 60, 60, 60), start=1):
        task = claim('all')
        assert task and task.id == ident
        process(task)
        with Session(engine()) as db:
            config = db.get(SourceSettings, source_id(CURRENT))
            assert config.next_run_at == clock[0] + timedelta(seconds=delay)
            state = db.get(Job, ident).state
        assert state == ('failed' if number == 5 else 'queued')
        clock[0] += timedelta(seconds=delay)
        if number == 5:
            ident = new_job(CURRENT)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 6


def test_scheduled_emotion_retries_old_day_then_saves_valid_receipt(client, monkeypatch):
    from app.evidence.models import EvidenceVersion
    from app.market.kpl_emotion import FAMILY
    from test_kpl_emotion_unit import body

    headers = login(client)
    configure_family(client, headers, FAMILY, interval_seconds=60)
    clock = frozen_collector(monkeypatch)
    monkeypatch.setattr('app.market.kpl_atlas_budget.reserve', lambda *a, **k: True)
    source_day = [clock[0].date() - timedelta(days=1)]
    monkeypatch.setattr('app.market.direction_sources.fetch', lambda family: Response(
        body=body(day=str(source_day[0])), received_at=clock[0], elapsed_ms=10, status=200))
    ident = queue(owner_id(), FAMILY, clock[0], payload={'scheduled_realtime': True})
    for delay in (5, 15):
        task = claim('realtime')
        assert task and task.id == ident
        process(task)
        saved = job_row(ident)
        assert (saved.state, saved.error_code) == ('queued', 'source_day_mismatch')
        assert saved.available_at == clock[0] + timedelta(seconds=delay)
        with Session(engine()) as db:
            assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
                EvidenceVersion.source_id == source_id(FAMILY), EvidenceVersion.kind == 'reference')) == 0
        clock[0] = saved.available_at.astimezone(SHANGHAI)

    source_day[0] = clock[0].date()
    process(claim('realtime'))
    assert job_row(ident).state == 'succeeded'
    with Session(engine()) as db:
        record = db.scalar(select(EvidenceVersion).where(
            EvidenceVersion.source_id == source_id(FAMILY), EvidenceVersion.kind == 'reference'))
        assert record.received_at == clock[0]


@pytest.mark.parametrize('minute,expected',[(59,'15:01:58'),(4,'09:15:00')])
def test_retry_after_is_respected_in_the_bounded_close_receipt_window(client, monkeypatch,minute,expected):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    clock = frozen_collector(monkeypatch)
    clock[0] = clock[0].replace(hour=14 if minute==59 else 15, minute=minute, second=58)
    def limited(*a, **k):
        raise SourceFailure('rate_limited', retry_after=120)
    monkeypatch.setattr('app.market.collect.fetch_auction_table', limited)
    ident = new_job(CURRENT)
    process(claim('realtime'))
    saved = job_row(ident)
    assert saved.state == 'queued'
    assert saved.available_at.astimezone(SHANGHAI).date() == clock[0].date() + timedelta(days=0 if minute==59 else 1)
    assert saved.available_at.astimezone(SHANGHAI).strftime('%H:%M:%S') == expected


def test_first_schema_failure_rechecks_after_five_seconds_with_real_shared_permit(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    clock = frozen_collector(monkeypatch)
    calls = []
    def malformed(*a, **k):
        calls.append(clock[0])
        raise SourceFailure('schema_changed')
    monkeypatch.setattr('app.market.collect.fetch_auction_table', malformed)
    ident = new_job(CURRENT)
    process(claim('realtime'))
    with Session(engine()) as db:
        config = db.get(SourceSettings, source_id(CURRENT))
        assert config.paused_reason == 'schema_changed'
        assert config.next_run_at == clock[0] + timedelta(seconds=5)
    clock[0] += timedelta(seconds=5)
    schedule_sources()
    probe = claim('realtime')
    assert probe and probe.payload['schema_probe'] is True
    process(probe)
    assert len(calls) == 2 and (calls[1] - calls[0]).total_seconds() == 5
    with Session(engine()) as db:
        config = db.get(SourceSettings, source_id(CURRENT))
        assert config.paused_reason == 'schema_changed'
        assert config.next_run_at == clock[0] + timedelta(seconds=15)
    assert job_row(ident).state == 'failed'


@pytest.mark.parametrize('reason', ['decoder_changed', 'permission_missing', 'upstream_redirect', 'unsafe_upstream_response'])
def test_security_failures_are_never_automatically_resumed(client, monkeypatch, reason):
    headers = login(client)
    configure_family(client, headers, CURRENT)
    clock = frozen_collector(monkeypatch)
    def fail(*a, **k):
        raise SourceFailure(reason)
    monkeypatch.setattr('app.market.collect.fetch_auction_table', fail)
    process(claim_queued(CURRENT))
    clock[0] += timedelta(minutes=2)
    schedule_sources()
    assert claim('realtime') is None
    with Session(engine()) as db:
        assert db.get(SourceSettings, source_id(CURRENT)).paused_reason == reason


def claim_queued(family):
    new_job(family)
    return claim('realtime')


def test_retry_backoff_cap_is_stable():
    assert [retry_seconds(number) for number in range(1, 7)] == [5, 15, 30, 60, 60, 60]


def test_new_task_after_failures_does_not_add_the_normal_five_minute_interval(client, monkeypatch):
    headers = login(client)
    configure_family(client, headers, CURRENT, interval_seconds=300)
    clock = datetime.fromisoformat('2026-10-27T10:01:00+08:00')
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr('app.market.service.now', lambda db: clock)
    with Session(engine()) as db, db.begin():
        config = db.get(SourceSettings, source_id(CURRENT))
        config.next_run_at = clock
        db.add(DataFetchRun(source_id=config.source_id, capability='auction.table.current',
            started_at=clock - timedelta(seconds=61), received_at=clock - timedelta(seconds=60),
            status='error', reason='timeout', request_count=1))
    with Session(engine()) as db, db.begin():
        job = enqueue_collection(db, source_id(CURRENT), scheduled_realtime=True)
        assert job.available_at == clock


def one_worker_round(monkeypatch):
    import app.worker as worker
    class Stop:
        stopped = False
        def is_set(self):
            return self.stopped
        def set(self):
            self.stopped = True
        def wait(self, seconds):
            self.stopped = True
    monkeypatch.setattr(worker.threading, 'Event', Stop)
    monkeypatch.setattr(worker.signal, 'signal', lambda *a: None)
    monkeypatch.setattr('app.worker_watchdog.ProgressWatchdog', lambda *a: SimpleNamespace(
        start=lambda: None, advance=lambda: None, close=lambda: None))
    monkeypatch.setattr(worker, 'HEARTBEAT_ID', None)
    monkeypatch.setattr(worker, 'claim', lambda role: None)
    return worker


def test_standard_scheduler_does_not_run_projection_or_historical_scans(monkeypatch):
    worker = one_worker_round(monkeypatch)
    calls = []
    monkeypatch.setattr(worker, 'heartbeat', lambda *a, **k: None)
    monkeypatch.setattr('app.market.service.schedule_sources', lambda: calls.append('sources'))
    monkeypatch.setattr('app.market.resilience.reconcile', lambda **kw: calls.append('health'))
    def forbidden(*a, **k):
        pytest.fail('projections/history must never block the realtime scheduler')
    monkeypatch.setattr(worker, 'schedule_general', forbidden)
    monkeypatch.setattr(worker, 'schedule_archives', forbidden)
    monkeypatch.setattr(worker, 'reconcile_history', forbidden)
    worker.main(['--role', 'scheduler'])
    assert calls == ['sources', 'health']


def test_realtime_slots_keep_independent_execution_heartbeats(monkeypatch):
    worker = one_worker_round(monkeypatch)
    identities = []
    monkeypatch.setattr(worker, 'heartbeat', lambda ident, **k: identities.append(ident))
    for slot in ('primary', 'secondary'):
        worker.main(['--role', 'realtime', '--slot', slot])
    assert len(set(identities)) == 2
    assert identities[0].startswith('market:realtime:primary:')
    assert identities[1].startswith('market:realtime:secondary:')


@pytest.mark.parametrize('opened,live,seconds,allowed', [
    (True, True, 3600, False), (True, False, 60, False),
    (False, False, 3600, True), (None, False, 3600, False),
])
def test_legacy_all_archives_do_not_cross_live_or_near_opening(monkeypatch, opened, live, seconds, allowed):
    from app.worker import legacy_history_window
    clock = datetime.now(UTC)
    monkeypatch.setattr('app.evidence.common.now', lambda db: clock)
    monkeypatch.setattr('app.market.session.market_session', lambda *a: SimpleNamespace(
        is_trading_day=opened, automatic_live=live, next_transition_at=clock + timedelta(seconds=seconds)))
    assert legacy_history_window() is allowed
