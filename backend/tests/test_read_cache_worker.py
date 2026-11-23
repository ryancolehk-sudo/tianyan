"""The derived-read service reports its own progress and worker identity."""
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event

import pytest

from app import read_cache_worker
from app.config import settings
from app.market import fund_projection
from app.worker_watchdog import ProgressWatchdog


def test_read_cache_watchdog_bounds_stalled_progress_beyond_its_lease():
    from app.read_cache import LEASE

    clock = [0]
    watchdog = ProgressWatchdog('read-cache', clock=lambda: clock[0])
    assert watchdog.timeout > LEASE.total_seconds()
    clock[0] = watchdog.timeout
    assert not watchdog.expired()
    clock[0] += 1
    assert watchdog.expired()
    watchdog.advance()
    assert not watchdog.expired()


@pytest.mark.parametrize('filename_kind',['foreign','spoofed'])
def test_failure_location_never_exports_foreign_paths_or_exception_details(filename_kind):
    from pathlib import Path
    message='synthetic-secret-token SQL SELECT private_payload price=123'
    filename=('/private/synthetic-secret-token.py' if filename_kind=='foreign' else
              str(Path(read_cache_worker.__file__).parent/'syntheticsensitivekey.py'))
    try:
        exec(compile('raise RuntimeError(message)', filename, 'exec'), {'message':message})
    except RuntimeError as exc:
        assert read_cache_worker.failure_location(exc)=='unavailable'


def test_lane_failure_logs_app_locations_without_message_parameters_or_locals(monkeypatch,worker_loop,caplog):
    from app import read_cache
    from app.market import terminal_read
    secret='synthetic-private-token SQL SELECT private_payload price=123'
    def fail(*args,**kwargs):
        private_local=secret
        raise AttributeError(private_local)
    monkeypatch.setattr(terminal_read,'workbench_view',fail)
    future=Future()
    try:
        read_cache.compute(None,None,'workbench',{'stock':'synthetic-sensitive-parameter'})
    except AttributeError as exc:
        future.set_exception(exc)
    lane=read_cache_worker.ReadLane('other',None,None)
    lane.future=future
    try:
        lane.collect()
        record=next(item for item in caplog.records if 'read_cache_refresh_failed' in item.message)
        assert 'lane=other kind=AttributeError location=' in record.message
        assert 'read_cache.py:' in record.message
        assert secret not in caplog.text and 'synthetic-sensitive-parameter' not in caplog.text
        assert '/backend/app/' not in record.message and '/tests/' not in record.message
        assert record.exc_info is None and record.stack_info is None
        assert lane.future is None and lane.retry_after>0
    finally:
        lane.close()


def test_failure_location_stays_bounded_on_a_deep_app_stack(monkeypatch,worker_loop):
    depth=[12]
    def nested(db,*,lane):
        if depth[0]:
            depth[0]-=1
            return read_cache_worker.refresh(lane)
        raise RuntimeError('synthetic-private-deep-message')
    monkeypatch.setattr(read_cache_worker,'run_one',nested)
    with pytest.raises(RuntimeError) as captured:
        read_cache_worker.refresh('other')
    location=read_cache_worker.failure_location(captured.value)
    assert len(location.split('>'))==6
    assert all(part.startswith('read_cache_worker.py:') for part in location.split('>'))
    assert 'synthetic-private-deep-message' not in location


@pytest.fixture
def worker_loop(monkeypatch):
    """Control leadership and completion without sharing database sessions."""
    events = []
    watchdogs = []

    class Watchdog:
        def __init__(self, role):
            assert role == 'read-cache'
            self.advances, self.closed = 0, False
            watchdogs.append(self)

        def start(self):
            events.append('start')

        def advance(self):
            self.advances += 1

        def close(self):
            self.closed = True

    class Database:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            events.append('session')
            return self

        def __exit__(self, *args):
            pass

        def rollback(self):
            events.append('rollback')

    class Leader:
        reclaim_pending = True

        def acquire(self):
            events.append('acquire')
            return True

        def reclaim(self):
            events.append('reclaim')
            self.reclaim_pending = False
            return 0

        def close(self):
            events.append('leader-close')

    def heartbeat(identity):
        assert identity.startswith('read-cache:')
        events.append('heartbeat')

    def warm():
        events.append('warm')
        return True

    class Wakeup:
        def wait(self):
            events.append('wait')
            raise KeyboardInterrupt

        def close(self):events.append('wake-close')

    class Pool:
        def __init__(self, *, max_workers, thread_name_prefix):
            assert max_workers == 2

        def submit(self, work):
            result = Future()
            try:
                result.set_result(work())
            except Exception as exc:
                result.set_exception(exc)
            return result

        def shutdown(self, **kwargs):
            assert kwargs == {'wait': True, 'cancel_futures': True}
            events.append('pool-drained')

    monkeypatch.setattr(settings(), 'read_cache_enabled', True)
    monkeypatch.setattr(read_cache_worker, 'ProgressWatchdog', Watchdog)
    monkeypatch.setattr(read_cache_worker, 'Session', Database)
    monkeypatch.setattr(read_cache_worker, 'engine', lambda: None)
    monkeypatch.setattr(read_cache_worker, 'paused', lambda db: False)
    monkeypatch.setattr(read_cache_worker, 'prepare_current_workbenches', lambda db: events.append('prepare'))
    monkeypatch.setattr(read_cache_worker, 'heartbeat', heartbeat)
    monkeypatch.setattr(read_cache_worker, 'ReadCacheWakeup', Wakeup)
    monkeypatch.setattr(read_cache_worker, 'ReadCacheLeader', Leader)
    monkeypatch.setattr(read_cache_worker, 'ThreadPoolExecutor', Pool)
    monkeypatch.setattr(fund_projection, 'warm_current_funds', warm)
    return events, watchdogs


@pytest.mark.parametrize('prepare_fails',[False,True])
@pytest.mark.parametrize('heartbeat_fails',[False,True])
def test_read_cache_worker_advances_only_after_completed_iteration_and_closes(monkeypatch,worker_loop,prepare_fails,heartbeat_fails):
    events, watchdogs = worker_loop

    def refresh(db, *, lane):
        events.append(lane)
        return False

    def prepare(db):
        events.append('prepare')
        if prepare_fails:raise RuntimeError('synthetic preparation failure')

    def heartbeat(identity):
        assert identity.startswith('read-cache:')
        events.append('heartbeat')
        if heartbeat_fails:raise RuntimeError('synthetic heartbeat failure')

    monkeypatch.setattr(read_cache_worker, 'run_one', refresh)
    monkeypatch.setattr(read_cache_worker, 'prepare_current_workbenches', prepare)
    monkeypatch.setattr(read_cache_worker, 'heartbeat', heartbeat)
    with pytest.raises(KeyboardInterrupt):
        read_cache_worker.main()
    assert events.count('focused') == events.count('other') == 1
    assert events.count('reclaim') == 2 and events.count('warm') == events.count('prepare') == 1
    assert events.index('warm') < events.index('prepare') < events.index('other')
    assert ('rollback' in events) == prepare_fails
    assert events.count('heartbeat') == 1
    assert events.index('pool-drained') < events.index('leader-close') < events.index('wake-close')
    assert len(watchdogs) == 3 and all(w.closed and w.advances for w in watchdogs)


@pytest.mark.parametrize('connection_fails',[False,True])
def test_read_cache_worker_waits_without_leadership_then_recovers_and_closes(monkeypatch,worker_loop,connection_fails):
    events, watchdogs = worker_loop

    def acquire(self):
        events.append('acquire')
        if events.count('acquire') == 1:
            if connection_fails:raise RuntimeError('synthetic leader connection failure')
            return False
        return True

    def wake(self):
        events.append('wait')
        if events.count('wait') > 1:
            raise KeyboardInterrupt

    def refresh(db, *, lane):
        events.append(lane)
        return False

    monkeypatch.setattr(read_cache_worker.ReadCacheLeader, 'acquire', acquire)
    monkeypatch.setattr(read_cache_worker.ReadCacheWakeup, 'wait', wake)
    monkeypatch.setattr(read_cache_worker, 'run_one', refresh)
    with pytest.raises(KeyboardInterrupt):
        read_cache_worker.main()
    before_wait = events[:events.index('wait')]
    assert not {'session', 'reclaim', 'focused', 'other', 'warm', 'prepare'} & set(before_wait)
    assert events.count('focused') == events.count('other') == 1
    assert events.index('pool-drained') < events.index('leader-close')
    assert all(w.closed for w in watchdogs)


def test_maintenance_pause_never_starts_preparation_or_claims(monkeypatch, worker_loop):
    events, watchdogs = worker_loop
    monkeypatch.setattr(read_cache_worker, 'paused', lambda db: True)
    monkeypatch.setattr(read_cache_worker, 'run_one', lambda *a, **kw: pytest.fail('paused worker claimed'))
    with pytest.raises(KeyboardInterrupt):
        read_cache_worker.main()
    assert not {'reclaim', 'warm', 'prepare', 'focused', 'other'} & set(events)
    assert all(w.closed for w in watchdogs)


def test_pause_arriving_during_preparation_is_rechecked_before_claim(monkeypatch, worker_loop):
    events, _ = worker_loop
    checks = iter([False, True])
    monkeypatch.setattr(read_cache_worker, 'paused', lambda db: next(checks))
    monkeypatch.setattr(read_cache_worker, 'run_one', lambda *a, **kw: pytest.fail('new pause was ignored'))
    assert read_cache_worker.refresh('other', lambda db: events.append('prepared')) is False
    assert events.count('prepared') == 1


def test_reconnected_leader_drains_its_own_running_attempt_before_reclaim(monkeypatch, worker_loop):
    events, watchdogs = worker_loop
    submitted = []

    def acquire(self):
        events.append('acquire')
        if events.count('acquire') == 2:
            self.reclaim_pending = True
        return True

    def submit(self, work):
        future = Future()
        if not submitted:
            future.set_result(False)
        submitted.append(future)
        return future

    def wait(*args, **kwargs):
        events.append('build-wait')
        if events.count('build-wait') == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(read_cache_worker.ReadCacheLeader, 'acquire', acquire)
    monkeypatch.setattr(read_cache_worker.ThreadPoolExecutor, 'submit', submit)
    monkeypatch.setattr(read_cache_worker, 'wait', wait)
    with pytest.raises(KeyboardInterrupt):
        read_cache_worker.main()
    assert events.count('reclaim') == 1
    assert len(submitted) == 2 and not submitted[1].done()
    assert events.index('pool-drained') < events.index('leader-close')
    assert all(w.closed for w in watchdogs)


def test_fast_lane_progress_cannot_hide_a_stuck_lane_or_enqueue_duplicate_work(monkeypatch):
    clock, started, release = [0], Event(), Event()

    def watchdog(role):
        result = ProgressWatchdog(role, clock=lambda: clock[0])
        result.start = lambda: None
        result.close = lambda: None
        return result

    monkeypatch.setattr(read_cache_worker, 'ProgressWatchdog', watchdog)

    def slow():
        started.set()
        assert release.wait(3)
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        slow_lane = read_cache_worker.ReadLane('other', pool, slow)
        fast_lane = read_cache_worker.ReadLane('focused', pool, lambda: True)
        try:
            slow_lane.launch(True)
            assert started.wait(1)
            original = slow_lane.future
            clock[0] = slow_lane.watchdog.timeout + 1
            for _ in range(3):
                fast_lane.launch(True)
                assert fast_lane.future.result(timeout=1)
                fast_lane.collect()
                slow_lane.launch(True)
                slow_lane.collect()
            assert slow_lane.future is original and not original.done()
            assert slow_lane.watchdog.expired() and not fast_lane.watchdog.expired()
            release.set()
            assert original.result(timeout=1)
            slow_lane.collect()
            assert not slow_lane.watchdog.expired()
        finally:
            release.set()
            slow_lane.close()
            fast_lane.close()
