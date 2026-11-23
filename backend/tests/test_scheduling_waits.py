"""Local scheduling waits preserve evidence and genuine recovery hysteresis."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine
from app.market import resilience as r
from app.market.models import DataFetchRun
from app.market.service import source_states
from test_data_resilience import CLOCK, receipt, row, scenario, source


WAITS = ('intraday_window_wait', 'source_budget_wait', 'local_request_wait')


def failed_attempt(db, provider, *, at=CLOCK, reason='local_request_wait', requests=0,
        accepted=0, retry_not_before=None):
    attempt = DataFetchRun(source_id=provider.id, capability='quote.l1', started_at=at,
        received_at=at, status='error', reason=reason, expected=1, accepted=accepted,
        duplicates=0, rejected=0, request_count=requests, retry_not_before=retry_not_before)
    db.add(attempt); db.flush()
    return attempt


@pytest.mark.parametrize('reason,requests', [('intraday_window_wait', 7), ('intraday_window_wait', 0),
    ('source_budget_wait', 0), ('local_request_wait', 0)])
def test_local_wait_is_distinct_from_error_without_rewriting_the_attempt(scenario, reason, requests):
    with Session(engine()) as db, db.begin():
        provider, config = source(db, scenario)
        attempt = failed_attempt(db, provider, reason=reason, requests=requests)
        ident = attempt.id
    with Session(engine()) as db, db.begin():
        db.execute(text('SET TRANSACTION READ ONLY'))
        state = next(s for s in source_states(db) if s.family == 'tencent_web')
        assert state.collection_state == 'waiting'
        assert state.last_attempt.status == 'error'
        assert state.last_attempt.reason == reason
        assert state.last_attempt.request_count == requests
        assert state.last_success_at is None
        assert db.get(DataFetchRun, ident).status == 'error'


@pytest.mark.parametrize('reason', ['timeout', 'rate_limited', 'upstream_unavailable', 'local_clock_unsettled'])
def test_actual_failures_and_unsettled_clock_do_not_become_scheduling_waits(scenario, reason):
    with Session(engine()) as db, db.begin():
        provider, _ = source(db, scenario)
        failed_attempt(db, provider, reason=reason)
    with Session(engine()) as db:
        state = next(s for s in source_states(db) if s.family == 'tencent_web')
        assert state.collection_state == 'error'


@pytest.mark.parametrize('gate', ['disabled', 'rights', 'paused', 'credentials'])
def test_local_wait_does_not_hide_configuration_or_permission_problems(scenario, monkeypatch, gate):
    with Session(engine()) as db, db.begin():
        provider, config = source(db, scenario)
        failed_attempt(db, provider)
        if gate == 'disabled': provider.enabled = False
        elif gate == 'rights': provider.rights = {**provider.rights, 'cache': 'denied'}
        elif gate == 'paused': config.paused_reason = 'schema_changed'
    if gate == 'credentials':
        monkeypatch.setattr('app.market.service.resolve_source_credentials', lambda *a, **kw:
            SimpleNamespace(required=True, credential=None, reason='credentials_missing'))
    with Session(engine()) as db:
        state = next(s for s in source_states(db) if s.family == 'tencent_web')
        assert state.collection_state == {'disabled': 'disabled', 'rights': 'permission_missing',
            'paused': 'error', 'credentials': 'credentials_missing'}[gate]


def test_an_inconsistent_error_with_accepted_rows_is_not_silently_reclassified(scenario):
    with Session(engine()) as db, db.begin():
        provider, _ = source(db, scenario)
        failed_attempt(db, provider, accepted=1)
    with Session(engine()) as db:
        assert next(s for s in source_states(db) if s.family == 'tencent_web').collection_state == 'error'


@pytest.mark.parametrize('reason', WAITS)
def test_wait_keeps_fresh_saved_clocks_without_counting_failure_or_new_success(scenario, reason):
    with Session(engine()) as db, db.begin():
        provider, config = source(db, scenario)
        good = receipt(db, provider)
        r.evaluate(db, provider, config, good, CLOCK, allow_actions=False)
        health = row(db, provider)
        before = (health.failure_count, health.recovery_successes, health.last_valid_at, health.last_source_time)
        waiting = failed_attempt(db, provider, at=CLOCK+timedelta(seconds=1), reason=reason,
            requests=7 if reason == 'intraday_window_wait' else 0)
        r.evaluate(db, provider, config, waiting, waiting.received_at, allow_actions=False)
        assert (health.failure_count, health.recovery_successes, health.last_valid_at, health.last_source_time) == before
        assert health.state == 'normal'
        assert health.active_source_id == provider.id
        assert health.reason == reason
        assert health.next_action in {'wait_session', 'retry_source'}
        assert health.next_action_at >= config.next_run_at
        assert health.last_run_id == waiting.id
        assert waiting.status == 'error'


@pytest.mark.parametrize('reason', WAITS)
def test_wait_without_usable_evidence_is_not_a_success_or_a_supplier_failure(scenario, reason):
    with Session(engine()) as db, db.begin():
        provider, config = source(db, scenario)
        waiting = failed_attempt(db, provider, reason=reason)
        r.evaluate(db, provider, config, waiting, CLOCK, allow_actions=False)
        health = row(db, provider)
        assert health.failure_count == 0
        assert health.recovery_successes == 0
        assert health.state == 'recovering'
        assert health.active_source_id is None
        assert health.last_valid_at is None


def test_wait_does_not_make_stale_saved_prices_usable(scenario):
    with Session(engine()) as db, db.begin():
        provider, config = source(db, scenario)
        good = receipt(db, provider, at=CLOCK-timedelta(minutes=4))
        r.evaluate(db, provider, config, good, good.received_at, allow_actions=False)
        health = row(db, provider)
        previous_count = health.failure_count
        waiting = failed_attempt(db, provider)
        r.evaluate(db, provider, config, waiting, CLOCK, allow_actions=False)
        assert health.failure_count == previous_count
        assert health.state == 'recovering'
        assert health.active_source_id is None
        assert health.last_valid_at != CLOCK


def test_wait_does_not_advance_or_reset_two_real_successes_required_after_fallback(scenario):
    with Session(engine()) as db, db.begin():
        primary, config = source(db, scenario)
        backup, _ = source(db, scenario, family='tickflow_live')
        receipt(db, backup)
        failed = failed_attempt(db, primary, reason='timeout', requests=1)
        r.evaluate(db, primary, config, failed, CLOCK, allow_actions=False)
        health = row(db, primary)
        assert health.state == 'fallback' and health.active_source_id == backup.id
        first = receipt(db, primary, at=CLOCK+timedelta(seconds=1))
        r.evaluate(db, primary, config, first, first.received_at, allow_actions=False)
        assert health.recovery_successes == 1 and health.state == 'fallback'
        waiting = failed_attempt(db, primary, at=CLOCK+timedelta(seconds=2))
        r.evaluate(db, primary, config, waiting, waiting.received_at, allow_actions=False)
        assert health.failure_count == 0 and health.recovery_successes == 1
        assert health.state == 'fallback' and health.active_source_id == backup.id
        r.evaluate(db, primary, config, waiting, waiting.received_at, allow_actions=False)
        assert health.recovery_successes == 1
        second = receipt(db, primary, at=CLOCK+timedelta(seconds=3))
        r.evaluate(db, primary, config, second, second.received_at, allow_actions=False)
        assert health.recovery_successes == 2
        assert health.state == 'normal' and health.active_source_id == primary.id


def test_wait_does_not_discard_supplier_or_local_deadlines(scenario, monkeypatch):
    with Session(engine()) as db, db.begin():
        provider, config = source(db, scenario)
        good = receipt(db, provider)
        r.evaluate(db, provider, config, good, CLOCK, allow_actions=False)
        config.next_run_at = CLOCK+timedelta(seconds=5)
        waiting = failed_attempt(db, provider, at=CLOCK+timedelta(seconds=1),
            retry_not_before=CLOCK+timedelta(seconds=30))
        monkeypatch.setattr(r, 'supplier_cooldown', lambda *a: CLOCK+timedelta(seconds=45))
        r.evaluate(db, provider, config, waiting, waiting.received_at, allow_actions=False)
        assert row(db, provider).next_action_at == CLOCK+timedelta(seconds=45)


def test_real_failure_still_counts_even_with_fresh_saved_prices(scenario):
    with Session(engine()) as db, db.begin():
        provider, config = source(db, scenario)
        good = receipt(db, provider)
        r.evaluate(db, provider, config, good, CLOCK, allow_actions=False)
        failed = failed_attempt(db, provider, at=CLOCK+timedelta(seconds=1), reason='timeout', requests=1)
        r.evaluate(db, provider, config, failed, failed.received_at, allow_actions=False)
        health = row(db, provider)
        assert health.failure_count == 1 and health.recovery_successes == 0
        assert health.state == 'recovering'


def test_a_wait_does_not_clear_an_actual_failure_using_older_fresh_data(scenario):
    with Session(engine()) as db, db.begin():
        provider, config = source(db, scenario)
        good = receipt(db, provider)
        r.evaluate(db, provider, config, good, CLOCK, allow_actions=False)
        failed = failed_attempt(db, provider, at=CLOCK+timedelta(seconds=1), reason='timeout', requests=1)
        r.evaluate(db, provider, config, failed, failed.received_at, allow_actions=False)
        waiting = failed_attempt(db, provider, at=CLOCK+timedelta(seconds=2))
        r.evaluate(db, provider, config, waiting, waiting.received_at, allow_actions=False)
        health = row(db, provider)
        assert health.failure_count == 1 and health.recovery_successes == 0
        assert health.state == 'recovering' and health.active_source_id is None


def test_a_wait_cannot_hide_an_overdue_queue(scenario):
    from app.models import Job
    with Session(engine()) as db, db.begin():
        provider, config = source(db, scenario)
        good = receipt(db, provider)
        r.evaluate(db, provider, config, good, CLOCK, allow_actions=False)
        waiting = failed_attempt(db, provider)
        db.add(Job(owner_id=scenario, kind='source.collect', state='queued',
            payload={'source_id': str(provider.id)}, available_at=CLOCK-timedelta(seconds=20)))
        db.flush()
        r.evaluate(db, provider, config, waiting, CLOCK, allow_actions=False)
        health = row(db, provider)
        assert health.stage == 'scheduler' and health.reason == 'collection_queue_overdue'
        assert health.next_action == 'repair_queue'
        assert health.failure_count == 0
