"""Dated selected-flow rejection/recovery in disposable PostgreSQL; no HTTP."""
import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import fund_focus as focus
from app.market.collect import store_reference_batch
from app.market.intraday_funds import intraday_funds
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response
from app.market.reference import ReferenceBatch
from app.market.service import source_id
from app.models import Job, Provider
from app.worker import claim, process
from test_fund_focus import prepared, queued, wire
from test_intraday_funds import DAY


def reject_time(client, monkeypatch, kind, category='stock'):
    clock, owner = prepared(client, monkeypatch, category=category)
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a, **k: None)
    focus.schedule()
    job = queued()
    context = job.payload['funds_focus_context']
    source = clock[0] - timedelta(days=1) if kind == 'wrong_day' else clock[0] + timedelta(minutes=1)
    body = wire(context, source)
    if kind == 'time_ahead':
        value = json.loads(body)
        # This case verifies complete rejection when no elapsed minute exists;
        # mixed elapsed/future responses have their own prefix coverage tests.
        value['data']['klines'] = value['data']['klines'][-1:]
        body = json.dumps(value).encode()
    calls = []

    def fetch(url):
        calls.append(url)
        return Response(body, clock[0], 4, 200)

    monkeypatch.setattr(focus, 'fetch_https', fetch)
    process(claim('realtime'))
    with Session(engine()) as db:
        failed = db.get(Job, job.id)
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == job.id))
        assert failed.state == 'queued' and failed.attempts == 0
        assert failed.available_at == clock[0] + timedelta(seconds=5)
        assert failed.lease_token is None
        assert run.status == 'error' and run.accepted == 0 and run.request_count == 1
        assert failed.error_code == run.reason
        assert run.body_hash == hashlib.sha256(body).hexdigest()
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext == focus.DATASET)) == 0
        assert db.get(SourceSettings, source_id(focus.FAMILY)).paused_reason is None
        result = intraday_funds(db, DAY, clock[0], owner_id=owner, current=True,
            session={'phase': 'trading'})
        assert result.state == 'available' and result.target_states[0].state == 'recovering'
        assert result.target_states[0].source_time is None
        assert [row.code for row in result.sectors] == ['BK0001.DC']
        assert result.sectors[0].rank == 1 and result.sectors[0].main_net_cny == 100
        rejection = (failed.result_ref or {}).get('last_time_rejection')
        reason = run.reason
        retry_at = failed.available_at
    observed = source - timedelta(minutes=5) if kind == 'wrong_day' else source
    expected = dict(reason='historical_response_day_mismatch' if kind == 'wrong_day'
        else 'historical_response_time_ahead', requested_day=DAY.isoformat(),
        source_time=observed.astimezone(focus.SHANGHAI).isoformat(),
        received_at=clock[0].isoformat(), body_hash=hashlib.sha256(body).hexdigest())
    return clock, owner, job.id, context, calls, rejection, reason, expected, retry_at


@pytest.mark.parametrize('category', ['stock', 'concept', 'industry'])
@pytest.mark.parametrize('kind', ['wrong_day', 'time_ahead'])
def test_selected_time_rejection_recovers_without_accepting_bad_points(client, monkeypatch, category, kind):
    clock, owner, job_id, context, calls, rejection, reason, expected, retry_at = reject_time(
        client, monkeypatch, kind, category)
    clock[0] = retry_at

    def fetch(url):
        calls.append(url)
        return Response(wire(context, clock[0]), clock[0], 4, 200)

    monkeypatch.setattr(focus, 'fetch_https', fetch)
    leased = claim('realtime')
    assert leased.id == job_id
    process(leased)
    with Session(engine()) as db:
        job = db.get(Job, job_id)
        assert job.state == 'succeeded' and job.error_code is None
        runs = list(db.scalars(select(DataFetchRun).where(DataFetchRun.job_id == job_id)
            .order_by(DataFetchRun.received_at)))
        assert [run.status for run in runs] == ['error', 'complete']
        assert [run.accepted for run in runs] == [0, 1]
        result = intraday_funds(db, DAY, clock[0], owner_id=owner, current=True,
            session={'phase': 'trading'})
        target = next(row for row in result.stocks if row.code == '600123.SH') if category == 'stock' else result.focused_sectors[0]
        amount = target.value if category == 'stock' else target.main_net_cny
        assert amount == -25 and target.source_time <= clock[0]
        assert result.target_states[0].state == 'available'
        assert result.sectors[0].rank == 1 and result.sectors[0].main_net_cny == 100
        assert len(calls) == 2 and calls[0] == calls[1]
        # Keep recovery assertions ahead of diagnostics so the original program
        # can prove its recovery behavior even when the classification is absent.
        assert reason == expected['reason']
        assert rejection == expected
        assert job.result_ref['last_time_rejection'] == expected


@pytest.mark.parametrize('guard', ['permission', 'window', 'shared_429'])
def test_selected_time_retry_keeps_original_admission_guards(client, monkeypatch, guard):
    from app.market import budget
    reserve = budget.reserve_eastmoney_request
    clock, _, job_id, _, calls, _, _, expected, retry_at = reject_time(
        client, monkeypatch, 'time_ahead')
    clock[0] = retry_at
    deadline = clock[0] + timedelta(seconds=90)
    with Session(engine()) as db, db.begin():
        if guard == 'permission':
            provider = db.get(Provider, source_id(focus.FAMILY))
            provider.rights = dict(provider.rights, display='denied')
        elif guard == 'shared_429':
            db.add(DataFetchRun(source_id=source_id(focus.FAMILY), capability='market.eastmoney.funds',
                started_at=clock[0], received_at=clock[0], status='error', reason='rate_limited',
                expected=3, accepted=0, rejected=0, duplicates=0, request_count=1,
                retry_not_before=deadline))
    if guard == 'window':
        monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime', lambda *a, **k: deadline)
    if guard == 'shared_429':
        monkeypatch.setattr(budget, 'reserve_eastmoney_request', reserve)
    monkeypatch.setattr(focus, 'fetch_https', lambda url: pytest.fail('the original guard must prevent HTTP'))
    process(claim('realtime'))
    with Session(engine()) as db:
        job = db.get(Job, job_id)
        if guard == 'permission':
            # Revocation may invalidate the checked scope before the source
            # permission gate. Both must stop without another request or write.
            assert job.error_code in {'funds_focus_expired', 'permission_missing'}
        else:
            assert job.error_code == {'window': 'intraday_window_wait', 'shared_429': 'rate_limited'}[guard]
        assert job.state == ('failed' if guard == 'permission' else 'queued')
        if guard != 'permission':
            assert job.available_at == deadline
        assert job.result_ref['last_time_rejection'] == expected
        assert db.scalar(select(func.count()).select_from(DataFetchRun).where(
            DataFetchRun.capability == focus.CAPABILITY)) == 1
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext == focus.DATASET)) == 0
    assert len(calls) == 1


def test_selected_time_rejection_can_reuse_a_later_valid_public_observation(client, monkeypatch):
    clock, _, job_id, context, calls, _, _, expected, retry_at = reject_time(
        client, monkeypatch, 'wrong_day')
    clock[0] = retry_at
    payload = focus.parse(wire(context, clock[0]), clock[0], context)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(focus.FAMILY))
        config = db.get(SourceSettings, provider.id)
        key = 'funds.selected:' + ':'.join((context['day'], context['category'], context['code']))
        store_reference_batch(db, provider, config,
            ReferenceBatch(records=[(key, payload, None)], expected=1, requests=1), clock[0])
    monkeypatch.setattr(focus, 'fetch_https', lambda url: pytest.fail('reuse must avoid another HTTP request'))
    process(claim('realtime'))
    with Session(engine()) as db:
        job = db.get(Job, job_id)
        assert job.state == 'succeeded' and job.result_ref['reused_saved_observation']
        assert job.result_ref['last_time_rejection'] == expected
        assert db.scalar(select(func.count()).select_from(DataFetchRun).where(
            DataFetchRun.capability == focus.CAPABILITY)) == 1
    assert len(calls) == 1


def test_time_rejection_keeps_previous_selected_values_and_historical_cutoff(client, monkeypatch):
    clock, owner = prepared(client, monkeypatch)
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a, **k: None)
    focus.schedule()
    first = queued()
    context = first.payload['funds_focus_context']
    original_time = clock[0]
    calls = []

    def fetch(url):
        calls.append(url)
        source = clock[0] if len(calls) == 1 else clock[0] + timedelta(minutes=1)
        return Response(wire(context, source), clock[0], 4, 200)

    monkeypatch.setattr(focus, 'fetch_https', fetch)
    process(claim('realtime'))
    with Session(engine()) as db:
        evidence = db.scalar(select(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext == focus.DATASET))
        original_payload = json.dumps(evidence.payload, sort_keys=True)
    clock[0] += timedelta(seconds=151)
    focus.schedule()
    next_job = claim('realtime')
    assert next_job.id != first.id
    process(next_job)
    with Session(engine()) as db:
        assert db.get(Job, next_job.id).error_code == 'historical_response_time_ahead'
        rows = list(db.scalars(select(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext == focus.DATASET)))
        assert len(rows) == 1 and json.dumps(rows[0].payload, sort_keys=True) == original_payload
        current = intraday_funds(db, DAY, clock[0], owner_id=owner, current=True,
            session={'phase': 'trading'})
        historical = intraday_funds(db, DAY, original_time, owner_id=owner, current=False,
            session={'phase': 'trading'})
        for result in [current, historical]:
            selected = next(row for row in result.stocks if row.code == '600123.SH')
            assert selected.value == -25 and selected.source_time == original_time
            assert result.latest_source_time == original_time
            assert result.target_states[0].received_at == original_time
            assert result.sectors[0].rank == 1 and result.sectors[0].main_net_cny == 100
        assert current.target_states[0].state == 'recovering'
        assert historical.target_states[0].state == 'available'
    assert len(calls) == 2
