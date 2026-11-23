from app.worker_watchdog import ProgressWatchdog, DEADLINES


def test_lease_liveness_is_independent_of_real_progress():
    clock = [0.0]
    watchdog = ProgressWatchdog('realtime', clock=lambda: clock[0])
    clock[0] = DEADLINES['realtime']
    assert not watchdog.expired()
    clock[0] += 0.01
    assert watchdog.expired()
    watchdog.advance()
    assert not watchdog.expired()


def test_history_and_realtime_have_independent_bounds():
    clock = [0.0]
    live = ProgressWatchdog('realtime', clock=lambda: clock[0])
    history = ProgressWatchdog('recovery', clock=lambda: clock[0])
    clock[0] = DEADLINES['realtime'] + 1
    assert live.expired() and not history.expired()
