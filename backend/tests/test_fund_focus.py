"""Leased selected-object reads of the existing funds endpoint; no real HTTP."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market import fund_focus as focus
from app.market.collect import upsert_instrument
from app.market.intraday_funds import intraday_funds
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import InstrumentKey, Response, SourceFailure, SourceRedirect
from app.market.service import source_id
from app.models import Job, Provider, User
from app.read_cache import cache_scope
from app.read_cache_models import ReadCache, ReadDependency
from app.worker import claim, process
from test_intraday_funds import setup, save, AT, DAY


def prepared(client, monkeypatch, *, category='stock'):
    setup(client)
    clock = [AT]
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(settings(), 'read_cache_enabled', True)
    monkeypatch.setattr(focus, 'now', lambda db: clock[0])
    monkeypatch.setattr('app.market.budget.now', lambda db: clock[0])
    monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime', lambda *a, **k: None)
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(focus, 'datetime', FrozenDateTime)
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(User))
        scope = cache_scope(db, owner)
        save(db, [('BK0001', '100')])
        save(db, [('600000', '100')], category='stock')
        _, instrument = upsert_instrument(db, InstrumentKey('SSE', '600123', 'stock'), '合成所选股', AT)
        params = dict(view='overview', day=DAY.isoformat(), as_of_time=None,
            detail_codes=['600123.SH'] if category == 'stock' else [],
            sector=None if category == 'stock' else 'BK0999.DC')
        data = {'stocks': [{'code': '600123.SH', 'instrument_id': str(instrument.id)}] if category == 'stock' else [],
            'sector_code': params['sector']}
        db.add(ReadCache(key='a'*64, owner_id=owner.id, scope=scope, operation='details', params=params,
            dependency_keys=[], payload={'data': data}, focused_at=AT, accessed_at=AT, refresh_after=AT, dirty=False))
        if category != 'stock':
            db.add(ReadCache(key='b'*64, owner_id=owner.id, scope=scope, operation='workbench',
                params={'view':'funds'}, dependency_keys=[], payload={'data':{'trade_day':DAY.isoformat(),
                    'sectors':[{'code':'BK0999.DC','name':'合成榜外','category':category}]}},
                accessed_at=AT, refresh_after=AT, dirty=False))
        owner_id = owner.id
    return clock, owner_id


def wire(context, clock, *, amount='-25'):
    code = context['code']
    market = 1 if context['category'] == 'stock' else 90
    value = {'rc':0, 'data':{'code':code, 'market':market, 'name':'合成所选对象',
        'klines':[f'{at.astimezone(focus.SHANGHAI):%Y-%m-%d %H:%M},{value},-1,-1,10,10'
            for at, value in [(clock-timedelta(minutes=5), '-30'), (clock, amount)]]}}
    return json.dumps(value).encode()


def queued():
    with Session(engine()) as db:
        return db.scalar(select(Job).where(Job.kind == focus.KIND).order_by(Job.created_at.desc()).limit(1))


@pytest.mark.parametrize('category',['stock','concept','industry'])
def test_outside_ranking_uses_one_dated_identity_without_replacing_rankings(client, monkeypatch, category):
    clock, owner = prepared(client, monkeypatch, category=category)
    calls = []
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a, **k: None)
    monkeypatch.setattr(focus, 'fetch_https', lambda url: pytest.fail('scheduling must not perform HTTP'))
    focus.schedule(); job = queued(); assert job is not None
    context = job.payload['funds_focus_context']
    def fetch(url):
        calls.append(url)
        return Response(wire(context, clock[0]), clock[0], 4, 200)
    monkeypatch.setattr(focus, 'fetch_https', fetch)
    leased = claim('realtime'); assert leased.id == job.id; process(leased)
    with Session(engine()) as db:
        result = intraday_funds(db, DAY, clock[0], owner_id=owner, current=True, session={'phase':'trading'})
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == job.id))
        assert run.capability == focus.CAPABILITY and run.status == 'complete' and run.accepted == 1
        assert [row.code for row in result.sectors] == ['BK0001.DC']
        assert result.sectors[0].rank == 1 and result.latest_source_time == AT
        if category == 'stock':
            target = next(row for row in result.stocks if row.code == '600123.SH')
            assert target.value == -25 and target.change_5m_cny == 5
        else:
            assert result.focused_sectors[0].code == 'BK0999.DC' and result.focused_sectors[0].rank is None
            assert result.focused_sectors[0].main_net_cny == -25
            assert result.focused_sectors[0].pct_change is None and result.focused_sectors[0].amount_cny is None
            assert len([point for point in result.points if point.sector_code == 'BK0999.DC']) == 2
        assert result.target_states[0].state == 'available' and result.target_states[0].source_time == AT
        # Source observations remain available only after their real receipt.
        earlier = intraday_funds(db, DAY, AT-timedelta(seconds=1), owner_id=owner, session={'phase':'trading'})
        assert not earlier.focused_sectors and not any(row.code == '600123.SH' for row in earlier.stocks)
        isolated = intraday_funds(db, DAY, AT, owner_id=uuid.uuid4(), current=True, session={'phase':'trading'})
        assert not isolated.focused_sectors and not isolated.target_states
        assert not any(row.code == '600123.SH' for row in isolated.stocks)
    assert len(calls) == 1 and '/api/qt/stock/fflow/kline/get?' in calls[0]
    focus.schedule()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == focus.KIND)) == 1


def test_selected_timeout_is_persisted_with_capped_backoff_and_does_not_stale_rankings(client, monkeypatch):
    clock, owner = prepared(client, monkeypatch)
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a, **k: None)
    monkeypatch.setattr(focus, 'fetch_https', lambda url: (_ for _ in ()).throw(SourceFailure('timeout')))
    focus.schedule()
    with Session(engine()) as db:
        interval = db.get(SourceSettings, source_id('eastmoney_funds')).interval_seconds
    for delay in (interval,)*5:
        # The selected view remains actively watched throughout this scenario.
        with Session(engine()) as db, db.begin():
            db.execute(update(ReadCache).values(focused_at=clock[0], accessed_at=clock[0]))
            # Independent ranking collection remains healthy over this longer
            # scenario; selected-detail failures must not make it stale.
            save(db, [('BK0001', '100')], clock[0])
            save(db, [('600000', '100')], clock[0], category='stock')
        job = claim('realtime'); assert job is not None
        at = clock[0]; process(job)
        with Session(engine()) as db:
            saved = db.get(Job, job.id)
            assert saved.state == 'queued' and saved.available_at == at + timedelta(seconds=delay)
            assert saved.lease_token is None and saved.attempts == 0
            result = intraday_funds(db, DAY, at, owner_id=owner, current=True, session={'phase':'trading'})
            assert result.state == 'available' and result.target_states[0].state == 'recovering'
            clock[0] = saved.available_at
    with Session(engine()) as db:
        from app.market.minute_backfill import live_run_condition
        assert db.scalar(select(func.count()).select_from(DataFetchRun).where(live_run_condition())) == 0
        assert db.scalar(select(func.count()).select_from(DataFetchRun).where(DataFetchRun.capability == focus.CAPABILITY)) == 5


def test_rejected_redirect_metadata_survives_budget_wait_and_real_data_recovery(client, monkeypatch):
    clock, _ = prepared(client, monkeypatch, category='industry')
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a, **k: None)
    calls = []
    def rejected(url):
        calls.append(url)
        raise SourceRedirect(302, url.replace('push2.eastmoney.com', 'push2delay.eastmoney.com'))
    monkeypatch.setattr(focus, 'fetch_https', rejected)
    focus.schedule()
    first = claim('realtime'); process(first)
    with Session(engine()) as db:
        job = db.get(Job, first.id)
        diagnostic = job.result_ref['last_transport_rejection']
        assert diagnostic['target_host'] == 'push2delay.eastmoney.com'
        assert diagnostic['request_path'] == diagnostic['target_path'] == 'funds_minute'
        assert diagnostic['same_query'] is True and diagnostic['secure_target'] is True
        assert job.error_code == 'upstream_redirect' and job.state == 'queued'
        assert job.result_ref['retry_step'] == 1
        assert job.available_at == clock[0] + timedelta(seconds=5)
        assert 'BK0999' not in json.dumps(diagnostic) and 'secid' not in json.dumps(diagnostic)
        clock[0] = job.available_at
    assert len(calls) == 1  # Recording a rejection does not follow it or add a probe.
    def cooldown(*a, **k):
        raise SourceFailure('rate_limited', retry_after=120)
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', cooldown)
    monkeypatch.setattr(focus, 'fetch_https', lambda url: pytest.fail('a shared budget wait cannot send HTTP'))
    process(claim('realtime'))
    with Session(engine()) as db:
        job = db.get(Job, first.id)
        assert job.result_ref['last_transport_rejection'] == diagnostic
        assert db.scalar(select(func.count()).select_from(DataFetchRun).where(DataFetchRun.job_id == first.id)) == 1
        clock[0] = job.available_at
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a, **k: None)
    monkeypatch.setattr(focus, 'fetch_https', lambda url: Response(wire(first.payload['funds_focus_context'], clock[0]), clock[0], 4, 200))
    process(claim('realtime'))
    with Session(engine()) as db:
        job = db.get(Job, first.id)
        assert job.state == 'succeeded' and job.error_code is None
        assert job.result_ref['last_transport_rejection'] == diagnostic
        assert job.result_ref['retry_step'] == 0
        run = db.scalars(select(DataFetchRun).where(DataFetchRun.job_id == first.id).order_by(DataFetchRun.received_at.desc())).first()
        assert run.status == 'complete' and run.accepted == 1


def minute_allocation_wire(monkeypatch, context, clock, *, second_redirect=False, body=None):
    from app.market import protocol
    calls = []
    class WireResponse:
        status = 200
        headers = {}
        def read(self, limit):
            return (body() if body else wire(context, clock[0]))[:limit]
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def opener(handler):
        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                if request.full_url.startswith('https://push2.eastmoney.com/'):
                    return handler.redirect_request(request, None, 302, '', {},
                        request.full_url.replace('push2.eastmoney.com', 'push2delay.eastmoney.com'))
                if second_redirect:
                    return handler.redirect_request(request, None, 302, '', {}, request.full_url)
                return WireResponse()
        return Opener()
    monkeypatch.setattr(protocol.urllib.request, 'build_opener', opener)
    monkeypatch.setattr(protocol, 'datetime', focus.datetime)
    return calls


@pytest.mark.parametrize('category', ['stock', 'industry', 'concept'])
def test_reobserved_minute_allocation_rechecks_each_physical_request_and_keeps_source_times(client, monkeypatch, category):
    clock, owner = prepared(client, monkeypatch, category=category)
    focus.schedule(); first = queued()
    calls = minute_allocation_wire(monkeypatch, first.payload['funds_focus_context'], clock)
    permits = []
    def reserve(*a, **k):
        permits.append(len(calls))
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', reserve)
    process(claim('realtime'))
    with Session(engine()) as db:
        saved = db.get(Job, first.id)
        assert saved.state == 'queued' and saved.error_code == 'upstream_redirect'
        clock[0] = saved.available_at
    assert len(calls) == 1  # A new redirect is observed, never immediately followed.
    process(claim('realtime'))
    with Session(engine()) as db:
        saved = db.get(Job, first.id)
        assert saved.state == 'succeeded' and saved.error_code is None
        runs = db.scalars(select(DataFetchRun).where(DataFetchRun.job_id == first.id).order_by(DataFetchRun.started_at)).all()
        assert [r.request_count for r in runs] == [1, 2]
        assert [r.status for r in runs] == ['error', 'complete']
        assert runs[-1].accepted == 1
        result = intraday_funds(db, DAY, clock[0], owner_id=owner, current=True, session={'phase': 'trading'})
        target = result.target_states[0]
        assert target.state == 'available' and target.source_time == clock[0].replace(second=0, microsecond=0)
        assert target.received_at == clock[0]
        assert result.sectors[0].rank == 1  # The selected response does not replace ranking data.
    assert permits == [0, 1, 2] and len(calls) == 3
    assert calls[-1].full_url == calls[-2].full_url.replace('push2.eastmoney.com', 'push2delay.eastmoney.com')
    assert calls[-1].data is None and calls[-1].get_method() == 'GET'
    assert not any(calls[-1].has_header(h) for h in ['Cookie', 'Authorization', 'Proxy-Authorization'])


@pytest.mark.parametrize('guard', ['permission', 'selection', 'configuration', 'lease', 'window', 'cooldown'])
def test_minute_allocation_cannot_bypass_changes_after_its_second_permit(client, monkeypatch, guard):
    clock, _ = prepared(client, monkeypatch, category='industry')
    focus.schedule(); first = queued()
    calls = minute_allocation_wire(monkeypatch, first.payload['funds_focus_context'], clock)
    permits = []
    def reserve(*a, **k):
        permits.append(len(calls))
        if len(permits) != 3:
            return
        if guard == 'cooldown':
            raise SourceFailure('rate_limited', retry_after=120)
        with Session(engine()) as db, db.begin():
            if guard == 'permission':
                db.get(Provider, source_id(focus.FAMILY)).enabled = False
            elif guard == 'selection':
                db.get(ReadCache, 'a'*64).focused_at = clock[0] - timedelta(minutes=6)
            elif guard == 'configuration':
                db.get(SourceSettings, source_id(focus.FAMILY)).interval_seconds += 1
            elif guard == 'lease':
                db.get(Job, first.id).lease_until = clock[0] - timedelta(seconds=1)
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', reserve)
    if guard == 'window':
        monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime',
            lambda *a, **k: clock[0] + timedelta(hours=2) if len(permits) >= 3 else None)
    process(claim('realtime'))
    with Session(engine()) as db:
        clock[0] = db.get(Job, first.id).available_at
    process(claim('realtime'))
    assert len(calls) == 2 and all(r.full_url.startswith('https://push2.eastmoney.com/') for r in calls)
    with Session(engine()) as db:
        assert not db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == first.id, DataFetchRun.status == 'complete'))


def test_minute_allocation_does_not_follow_a_second_redirect(client, monkeypatch):
    clock, _ = prepared(client, monkeypatch, category='industry')
    focus.schedule(); first = queued()
    calls = minute_allocation_wire(monkeypatch, first.payload['funds_focus_context'], clock, second_redirect=True)
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a, **k: None)
    process(claim('realtime'))
    with Session(engine()) as db:
        clock[0] = db.get(Job, first.id).available_at
    process(claim('realtime'))
    assert len(calls) == 3
    with Session(engine()) as db:
        saved = db.get(Job, first.id)
        assert saved.state == 'queued' and saved.error_code == 'upstream_redirect'
        latest = db.scalars(select(DataFetchRun).where(DataFetchRun.job_id == first.id).order_by(DataFetchRun.received_at.desc())).first()
        assert latest.request_count == 2 and latest.accepted == 0


@pytest.mark.parametrize('bad_time', ['old_day', 'future_minute'])
def test_allocated_minute_data_keeps_time_rejection_and_can_later_recover(client, monkeypatch, bad_time):
    clock, owner = prepared(client, monkeypatch, category='industry')
    focus.schedule(); first = queued()
    bad = [True]
    def body():
        shift = timedelta(days=-1) if bad_time == 'old_day' else timedelta(minutes=2)
        raw = wire(first.payload['funds_focus_context'], clock[0] + shift if bad[0] else clock[0])
        if bad[0] and bad_time == 'future_minute':
            value = json.loads(raw)
            # This recovery case has no elapsed point to accept. The mixed
            # elapsed/future allocation case is covered separately.
            value['data']['klines'] = value['data']['klines'][-1:]
            raw = json.dumps(value).encode()
        return raw
    calls = minute_allocation_wire(monkeypatch, first.payload['funds_focus_context'], clock, body=body)
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a, **k: None)
    process(claim('realtime'))
    with Session(engine()) as db:
        clock[0] = db.get(Job, first.id).available_at
    process(claim('realtime'))
    with Session(engine()) as db:
        saved = db.get(Job, first.id)
        assert saved.state == 'queued'
        assert saved.error_code in {'historical_response_day_mismatch', 'historical_response_time_ahead'}
        assert 'last_time_rejection' in saved.result_ref and 'last_transport_rejection' in saved.result_ref
        state = intraday_funds(db, DAY, clock[0], owner_id=owner, current=True, session={'phase': 'trading'})
        assert not state.focused_sectors
        clock[0] = saved.available_at
    bad[0] = False
    process(claim('realtime'))
    with Session(engine()) as db:
        saved = db.get(Job, first.id)
        assert saved.state == 'succeeded' and saved.error_code is None
        assert 'last_time_rejection' in saved.result_ref and 'last_transport_rejection' in saved.result_ref
        runs = db.scalars(select(DataFetchRun).where(DataFetchRun.job_id == first.id).order_by(DataFetchRun.received_at)).all()
        assert [r.request_count for r in runs] == [1, 2, 2]
        assert [r.accepted for r in runs] == [0, 0, 1]
    assert len(calls) == 5


@pytest.mark.parametrize('location', [None, 'file:///must_not_be_exported'])
def test_urllib_pre_rejected_redirect_is_recorded_but_never_followed(client, monkeypatch, location):
    from test_redirect_diagnostic import install_stdlib_redirect_wire
    clock, _ = prepared(client, monkeypatch, category='industry')
    focus.schedule(); first = queued()
    calls = install_stdlib_redirect_wire(monkeypatch, location=location)
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a, **k: None)
    for _ in range(2):
        process(claim('realtime'))
        with Session(engine()) as db:
            saved = db.get(Job, first.id)
            assert saved.error_code == 'upstream_redirect' and saved.state == 'queued'
            diagnostic = saved.result_ref['last_transport_rejection']
            assert diagnostic['http_status'] == 302
            assert diagnostic['target_valid'] is False or diagnostic['secure_target'] is False
            assert 'must_not_be_exported' not in json.dumps(diagnostic)
            clock[0] = saved.available_at
    assert len(calls) == 2 and all(x.startswith('https://push2.eastmoney.com/') for x in calls)
    with Session(engine()) as db:
        assert not db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == first.id, DataFetchRun.status == 'complete'))


def test_shared_retry_after_defers_same_job_without_http_or_extending_deadline(client, monkeypatch):
    clock, _ = prepared(client, monkeypatch)
    focus.schedule()
    deadline = AT + timedelta(seconds=90)
    with Session(engine()) as db, db.begin():
        db.add(DataFetchRun(source_id=source_id(focus.FAMILY), capability='market.eastmoney.funds', started_at=AT,
            received_at=AT, status='error', reason='rate_limited', expected=3, accepted=0, rejected=0, duplicates=0,
            request_count=1, retry_not_before=deadline))
    monkeypatch.setattr(focus, 'fetch_https', lambda url: pytest.fail('shared 429 must prevent HTTP'))
    job = claim('realtime'); process(job)
    with Session(engine()) as db:
        saved = db.get(Job, job.id)
        assert saved.state == 'queued' and saved.available_at == deadline and saved.attempts == 0
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 1


@pytest.mark.parametrize('change',['source_revoked','selection_expired','historical','identity_unknown'])
def test_current_permission_and_selection_are_rechecked_after_the_permit(client, monkeypatch, change):
    clock, _ = prepared(client, monkeypatch)
    focus.schedule()
    def reserve(*args, **kwargs):
        with Session(engine()) as db, db.begin():
            entry = db.get(ReadCache, 'a'*64)
            if change == 'source_revoked':
                db.get(Provider, source_id(focus.FAMILY)).enabled = False
            elif change == 'selection_expired':
                entry.focused_at = AT - timedelta(minutes=6)
            elif change == 'historical':
                entry.params = {**entry.params, 'as_of_time':AT.isoformat()}
            else:
                entry.payload = {'data':{'stocks':[{'code':'600123.SH','instrument_id':str(uuid.uuid4())}]}}
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', reserve)
    monkeypatch.setattr(focus, 'fetch_https', lambda url: pytest.fail('a stale or revoked intent cannot leave the machine'))
    job = claim('realtime'); process(job)
    with Session(engine()) as db:
        assert db.get(Job, job.id).state == 'failed'
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 0


def test_historical_or_speculative_detail_never_schedules_an_upstream_call(client, monkeypatch):
    clock, _ = prepared(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        db.get(ReadCache, 'a'*64).focused_at = None
    focus.schedule(); assert queued() is None
    with Session(engine()) as db, db.begin():
        entry = db.get(ReadCache, 'a'*64); entry.focused_at = clock[0]
        entry.params = {**entry.params, 'as_of_time':clock[0].isoformat()}
    focus.schedule(); assert queued() is None


def test_valid_row_in_latest_rank_page_avoids_an_extra_selected_request(client, monkeypatch):
    clock, _ = prepared(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        save(db, [('600123','0')], category='stock', stamp=AT+timedelta(seconds=1))
    clock[0] += timedelta(seconds=2)
    focus.schedule(); assert queued() is None


def test_second_account_reuses_public_observation_without_another_supplier_request(client, monkeypatch):
    clock, owner = prepared(client, monkeypatch)
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a, **k: None)
    calls = []
    focus.schedule(); first = queued()
    def fetch(url):
        calls.append(url)
        return Response(wire(first.payload['funds_focus_context'], clock[0]), clock[0], 4, 200)
    monkeypatch.setattr(focus, 'fetch_https', fetch)
    process(claim('realtime'))
    with Session(engine()) as db, db.begin():
        original = db.get(ReadCache, 'a'*64)
        other = User(id=uuid.uuid4(), username='synthetic-focus-viewer', password_hash='synthetic-unused-hash', role='viewer', enabled=True)
        db.add(other); db.flush(); second_owner = other.id
        db.add(ReadCache(key='c'*64, owner_id=other.id, scope=cache_scope(db,other), operation='details',
            params=original.params, dependency_keys=[], payload=original.payload,
            focused_at=clock[0], accessed_at=clock[0], refresh_after=clock[0], dirty=False))
    focus.schedule(); process(claim('realtime'))
    with Session(engine()) as db:
        result = intraday_funds(db, DAY, AT, owner_id=second_owner, current=True, session={'phase':'trading'})
        assert next(row for row in result.stocks if row.code == '600123.SH').value == -25
        assert len(result.target_states) == 1 and result.target_states[0].state == 'available'
        assert db.scalar(select(func.count()).select_from(DataFetchRun).where(DataFetchRun.capability == focus.CAPABILITY)) == 1
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == focus.KIND)) == 2
    assert len(calls) == 1


@pytest.mark.parametrize('commit', [False, True])
def test_selected_observation_invalidates_only_subscribed_owners_and_is_transactional(client, monkeypatch, commit):
    from app.evidence.models import EvidenceVersion
    from app.market.collect import store_reference_batch
    from app.market.reference import ReferenceBatch
    from app.read_cache import dependency_keys
    clock, owner = prepared(client, monkeypatch)
    focus.schedule(); original = queued(); context = original.payload['funds_focus_context']
    entries = {}
    with Session(engine()) as db, db.begin():
        users = [db.get(User, owner)]
        for index in range(2):
            user = User(id=uuid.uuid4(), username=f'synthetic-detail-owner-{index}',
                password_hash='synthetic-unused-hash', role='viewer', enabled=True)
            db.add(user); db.flush(); users.append(user)
            other = dict(context, requester_id=str(user.id), code=context['code'] if index == 0 else '600124')
            db.add(Job(id=uuid.uuid4(), owner_id=original.owner_id, kind=focus.KIND, state='succeeded',
                payload={'source_id':original.payload['source_id'],'funds_focus_context':other},
                created_at=clock[0],available_at=clock[0],completed_at=clock[0]))
        for index, user in enumerate(users):
            variants = [('workbench', {'view':view}) for view in ['overview','funds','auction','ladder']]
            variants += [('details', {'detail_codes':['600123.SH']}),
                ('workbench', {'view':'funds','as_of_time':AT.isoformat()})]
            for offset, (operation, params) in enumerate(variants):
                key = f'{1000 + index * 10 + offset:064x}'
                entries[index, offset] = key
                db.add(ReadCache(key=key, owner_id=user.id, scope=cache_scope(db,user), operation=operation,
                    params=params, dependency_keys=dependency_keys(operation,user.id,params), payload={'data':{}},
                    dirty=False,accessed_at=AT,refresh_after=AT))
        provider_id = original.payload['source_id']
    with Session(engine()) as db:
        family_before = db.get(ReadDependency, 'family:eastmoney_funds').revision
        provider = db.get(Provider, uuid.UUID(provider_id)); cfg = db.get(SourceSettings, provider.id)
        payload = focus.parse(wire(context, clock[0]), clock[0], context)
        store_reference_batch(db, provider, cfg, ReferenceBatch(records=[('funds.selected:'+':'.join(
            (context['day'],context['category'],context['code'])), payload, None)], expected=1, requests=1), clock[0])
        assert {(index,offset):db.get(ReadCache,key).dirty for (index,offset),key in entries.items()} == {
            (index,offset):index < 2 and offset < 2 for index,offset in entries}
        assert db.get(ReadDependency, 'family:eastmoney_funds').revision == family_before
        db.commit() if commit else db.rollback()
    with Session(engine()) as db, db.begin():
        assert {(index,offset):db.get(ReadCache,key).dirty for (index,offset),key in entries.items()} == {
            (index,offset):commit and index < 2 and offset < 2 for index,offset in entries}
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext==focus.DATASET)) == int(commit)
        db.execute(update(ReadCache).where(ReadCache.key.in_(entries.values())).values(dirty=False))
        save(db, [('BK0001','200')], stamp=clock[0]+timedelta(seconds=1))
    with Session(engine()) as db:
        # The complete native ranking still invalidates all dependent reads.
        assert all(db.get(ReadCache,key).dirty for key in entries.values())


def test_window_is_rechecked_after_the_permit_and_lost_lease_cannot_publish(client, monkeypatch):
    clock, _ = prepared(client, monkeypatch)
    focus.schedule(); job = queued()
    window = [None]
    monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime', lambda *a,**k: window[0])
    def defer(*a,**k):
        window[0] = AT+timedelta(hours=3)
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', defer)
    monkeypatch.setattr(focus, 'fetch_https', lambda url: pytest.fail('a permit cannot override the new trading window'))
    process(claim('realtime'))
    with Session(engine()) as db, db.begin():
        current = db.get(Job, job.id)
        assert current.state == 'queued' and current.available_at == AT+timedelta(hours=3)
        current.available_at = AT
    window[0] = None
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a,**k: None)
    def losing_lease(url):
        with Session(engine()) as db, db.begin():
            db.get(Job, job.id).lease_token = uuid.uuid4()
        return Response(wire(job.payload['funds_focus_context'], clock[0]), clock[0], 4, 200)
    monkeypatch.setattr(focus, 'fetch_https', losing_lease)
    process(claim('realtime'))
    with Session(engine()) as db:
        from app.evidence.models import EvidenceVersion
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.payload['dataset'].astext == focus.DATASET)) == 0
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 0


def test_selection_health_only_dirties_owners_current_funds_and_overview(client, monkeypatch):
    clock, owner = prepared(client, monkeypatch)
    from app.read_cache import dependency_keys
    keys = {}
    with Session(engine()) as db, db.begin():
        user = db.get(User,owner)
        for i,view in enumerate(['overview','funds','auction','ladder']):
            key = f'{i+10:064x}'; keys[view] = key
            params = {'view':view}
            dependencies = dependency_keys('workbench',owner,params)
            db.add(ReadCache(key=key, owner_id=owner, scope=cache_scope(db,user), operation='workbench',
                params=params,dependency_keys=dependencies,payload={'data':{}},dirty=False,
                accessed_at=AT,refresh_after=AT))
        assert not any(key.startswith('funds-selection:') for key in dependency_keys('details',owner,{}))
        assert not any(key.startswith('funds-selection:') for key in dependency_keys('workbench',owner,{'view':'funds','as_of_time':AT.isoformat()}))
        assert not any(key.startswith('funds-selection:') for key in dependency_keys('workbench',owner,{'view':'funds','day':'2000-01-01'}))
    focus.schedule()
    with Session(engine()) as db:
        assert {view:db.get(ReadCache,key).dirty for view,key in keys.items()} == {
            'overview':True,'funds':True,'auction':False,'ladder':False}
