"""Elapsed source minutes survive a future tail; all HTTP here is synthetic."""
import hashlib
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import fund_focus as focus, fund_history as history
from app.market.intraday_funds import intraday_funds
from app.market.models import DataFetchRun
from app.market.protocol import Response, SourceFailure
from app.models import Job
from app.read_cache_models import ReadCache
from app.worker import claim, process
from test_fund_focus import prepared, queued, wire, minute_allocation_wire
from test_fund_history import CLOSE, body, context
from test_intraday_funds import DAY, save


@pytest.mark.parametrize('category', ['stock', 'concept', 'industry'])
def test_current_selected_retains_elapsed_rows_and_later_updates(client, monkeypatch, category):
    clock, owner = prepared(client, monkeypatch, category=category)
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a, **k: None)
    focus.schedule()
    first = queued()
    selected = first.payload['funds_focus_context']
    received = clock[0]
    future = received + timedelta(minutes=1)
    value = json.loads(wire(selected, received))
    value['data']['klines'].append(
        f'{future.astimezone(focus.SHANGHAI):%Y-%m-%d %H:%M},-999,-1,-1,10,10')
    raw = json.dumps(value).encode()
    calls = []

    def fetch(url):
        calls.append(url)
        return Response(raw, clock[0], 4, 200)

    monkeypatch.setattr(focus, 'fetch_https', fetch)
    process(claim('realtime'))
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == first.id))
        assert run.status == 'partial' and run.reason == 'historical_response_time_ahead'
        assert run.accepted == 1 and run.request_count == 1
        job = db.get(Job, first.id)
        assert job.state == 'succeeded' and job.error_code is None
        diagnostic = job.result_ref['last_time_rejection']
        assert diagnostic == dict(reason='historical_response_time_ahead', requested_day=DAY.isoformat(),
            source_time=future.astimezone(focus.SHANGHAI).isoformat(), received_at=received.isoformat(),
            retained_minutes=2, excluded_future_minutes=1, body_hash=hashlib.sha256(raw).hexdigest())
        saved = db.scalar(select(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext == focus.DATASET))
        original = json.dumps(saved.payload, sort_keys=True)
        assert len(saved.payload['rows']) == 2
        assert all(row['archive_complete'] == 'false' for row in saved.payload['rows'])
        assert all(datetime.fromisoformat(row['source_time']) <= received
            for row in saved.payload['rows'])
        assert '未来' in saved.payload['scope']
        result = intraday_funds(db, DAY, received, owner_id=owner, current=True,
            session={'phase': 'trading'})
        target = next(r for r in result.stocks if r.code == '600123.SH') if category == 'stock' else result.focused_sectors[0]
        assert (target.value if category == 'stock' else target.main_net_cny) == -25
        assert target.source_time == received
        assert result.target_states[0].state == 'available'
        assert result.sectors[0].main_net_cny == 100 and result.sectors[0].rank == 1
        before = intraday_funds(db, DAY, received-timedelta(seconds=1), owner_id=owner,
            session={'phase': 'trading'})
        assert not before.focused_sectors and not any(r.code == '600123.SH' for r in before.stocks)
        evidence_id = saved.id
    assert len(calls) == 1

    # A later ordinary selection cycle keeps the original interval and obtains
    # a new value; excluded future values never become a delayed publication.
    clock[0] += timedelta(minutes=3)
    with Session(engine()) as db, db.begin():
        db.execute(update(ReadCache).values(focused_at=clock[0], accessed_at=clock[0]))
        save(db, [('BK0001', '100')], clock[0])
        save(db, [('600000', '100')], clock[0], category='stock')
    focus.schedule()
    second = queued()
    assert second.id != first.id

    def recovered(url):
        calls.append(url)
        return Response(wire(second.payload['funds_focus_context'], clock[0], amount='-20'), clock[0], 4, 200)

    monkeypatch.setattr(focus, 'fetch_https', recovered)
    process(claim('realtime'))
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == second.id))
        assert run.status == 'complete' and run.reason is None and run.accepted == 1
        assert json.dumps(db.get(EvidenceVersion, evidence_id).payload, sort_keys=True) == original
        result = intraday_funds(db, DAY, clock[0], owner_id=owner, current=True,
            session={'phase': 'trading'})
        target = next(r for r in result.stocks if r.code == '600123.SH') if category == 'stock' else result.focused_sectors[0]
        assert (target.value if category == 'stock' else target.main_net_cny) == -20
        assert target.source_time == clock[0]
    assert len(calls) == 2 and calls[0] == calls[1]


def elapsed_body():
    received = CLOSE.replace(hour=9, minute=33, second=17)
    slots = [received.replace(minute=m, second=0) for m in (31, 32, 34)]
    return received, body(slots=slots)


def test_allocated_elapsed_prefix_preserves_both_rejection_diagnostics(client, monkeypatch):
    clock, owner = prepared(client, monkeypatch, category='industry')
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a, **k: None)
    focus.schedule()
    first = queued()
    selected = first.payload['funds_focus_context']

    def mixed():
        value = json.loads(wire(selected, clock[0]))
        future = clock[0] + timedelta(minutes=1)
        value['data']['klines'].append(
            f'{future.astimezone(focus.SHANGHAI):%Y-%m-%d %H:%M},-999,-1,-1,10,10')
        return json.dumps(value).encode()

    calls = minute_allocation_wire(monkeypatch, selected, clock, body=mixed)
    process(claim('realtime'))
    with Session(engine()) as db:
        job = db.get(Job, first.id)
        assert job.state == 'queued' and job.error_code == 'upstream_redirect'
        assert 'last_time_rejection' not in job.result_ref
        clock[0] = job.available_at
    process(claim('realtime'))
    with Session(engine()) as db:
        job = db.get(Job, first.id)
        assert job.state == 'succeeded' and job.error_code is None
        transport = job.result_ref['last_transport_rejection']
        assert transport['same_path'] and transport['same_query'] and transport['secure_target']
        assert transport['target_host'] == 'push2delay.eastmoney.com'
        assert job.result_ref['last_time_rejection']['excluded_future_minutes'] == 1
        runs = list(db.scalars(select(DataFetchRun).where(DataFetchRun.job_id == first.id)
            .order_by(DataFetchRun.received_at)))
        assert [r.request_count for r in runs] == [1, 2]
        assert [r.accepted for r in runs] == [0, 1]
        assert [r.status for r in runs] == ['error', 'partial']
        result = intraday_funds(db, DAY, clock[0], owner_id=owner, current=True,
            session={'phase': 'trading'})
        assert result.focused_sectors[0].main_net_cny == -25
        assert result.focused_sectors[0].source_time <= clock[0]
        assert result.sectors[0].main_net_cny == 100
    assert len(calls) == 3


def test_elapsed_parser_keeps_original_stamps_and_archive_parser_stays_strict():
    received, value = elapsed_body()
    raw = json.dumps(value).encode()
    with pytest.raises(SourceFailure, match='historical_response_time_ahead'):
        history.parse_history(raw, received, context())
    payload, complete, rejected = history.parse_elapsed_history(raw, received, context())
    assert not complete and len(payload.rows) == 2
    assert [r['source_time'] for r in payload.rows] == [
        '2026-10-23T09:31:00+08:00', '2026-10-23T09:32:00+08:00']
    assert rejected['retained_minutes'] == 2 and rejected['excluded_future_minutes'] == 1
    assert rejected['source_time'] == '2026-10-23T09:34:00+08:00'


@pytest.mark.parametrize('kind,reason', [
    ('wrong_identity', 'instrument_mismatch'), ('wrong_day', 'historical_response_day_mismatch'),
    ('duplicate_future', 'historical_series_order'), ('out_of_order', 'historical_series_order'),
    ('future_nan', 'schema_changed'), ('future_lunch', 'invalid_source_time')])
def test_elapsed_prefix_never_hides_invalid_identity_date_order_or_tail(kind, reason):
    received, value = elapsed_body()
    lines = value['data']['klines']
    if kind == 'wrong_identity': value['data']['code'] = 'BK9999'
    elif kind == 'wrong_day': lines[-1] = lines[-1].replace('2026-10-23', '2026-10-24')
    elif kind == 'duplicate_future': lines.append(lines[-1])
    elif kind == 'out_of_order': lines[-2], lines[-1] = lines[-1], lines[-2]
    elif kind == 'future_nan': lines[-1] = lines[-1].replace(',20,', ',NaN,')
    elif kind == 'future_lunch': lines[-1] = lines[-1].replace('09:34', '12:00')
    with pytest.raises(SourceFailure, match=reason):
        history.parse_elapsed_history(json.dumps(value).encode(), received, context())


def test_elapsed_parser_rejects_all_future_and_accepts_exact_receipt_boundary():
    received, value = elapsed_body()
    value['data']['klines'] = value['data']['klines'][-1:]
    raw = json.dumps(value).encode()
    with pytest.raises(SourceFailure, match='historical_response_time_ahead'):
        history.parse_elapsed_history(raw, received, context())
    boundary = received.replace(minute=34, second=0)
    payload, complete, rejected = history.parse_elapsed_history(raw, boundary, context())
    assert not complete and rejected is None and len(payload.rows) == 1
    assert payload.rows[0]['source_time'] == boundary.isoformat()
