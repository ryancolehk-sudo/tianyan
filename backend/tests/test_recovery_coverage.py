"""Coverage/queue integration on disposable PostgreSQL, without vendor calls."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market import recovery
from app.market.models import SourceSettings
from app.market.recovery_models import RecoveryCoverage
from app.market.service import source_id
from app.models import Job, Provider, User, WorkerHeartbeat
from app.worker import claim
from test_minute_recovery import setup as minute_setup, seed_window, window, FAMILY, DAY
from test_auction_tables import configure_family
from test_data_sources import login
from test_kpl_emotion_unit import body as emotion_body


CLOCK = datetime.fromisoformat('2026-10-26T18:00:00+08:00')


def test_ranges_keep_lunch_and_internal_holes_distinct():
    slots = recovery.minute_slots(date(2026, 10, 23), '1m', CLOCK)
    assert len(slots) == 240
    assert all(value.hour != 12 for value in slots)
    holes = {datetime.fromisoformat('2026-10-23T'+value+':00+08:00') for value in ('09:32', '09:33', '13:05')}
    grouped = recovery.ranges(holes, 1)
    assert [row['count'] for row in grouped] == [2, 1]


def test_minute_expectation_never_contains_future_or_lunch():
    stamp = datetime.fromisoformat('2026-10-26T09:33:00+08:00')
    slots = recovery.minute_slots(stamp.date(), '1m', stamp)
    assert len(slots) == 2 and max(slots).minute == 32
    assert recovery.minute_slots(stamp.date(), '1m', stamp.replace(hour=9, minute=0)) == set()


def test_automatic_reconciliation_checks_today_from_saved_emotion_receipts(client, monkeypatch):
    from app.market.collect import store_reference_batch
    from app.market.kpl_emotion import FAMILY as EMOTION, parse

    configure_family(client, login(client), EMOTION, interval_seconds=60)
    clock = CLOCK.replace(hour=9, minute=18)
    day = clock.date()
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(recovery, 'now', lambda db: clock)
    monkeypatch.setattr(recovery, 'closed_days', lambda *a: [])
    monkeypatch.setattr('app.market.session.market_session',
                        lambda *a: SimpleNamespace(current_day_active=True, trade_day=day))
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(EMOTION))
        config = db.get(SourceSettings, provider.id)
        for minute, source_day in ((15, day), (16, day - timedelta(days=1)), (17, day), (19, day)):
            stamp = clock.replace(minute=minute, second=10)
            batch = parse(emotion_body(day=str(source_day)), stamp)
            store_reference_batch(db, provider, config, batch, stamp)

    recovery.reconcile()

    with Session(engine()) as db:
        row = db.scalar(select(RecoveryCoverage).where(RecoveryCoverage.source_id == source_id(EMOTION)))
        assert row.trade_day == day
        assert (row.state, row.reason) == ('unsupported', 'emotion_snapshot_current_only')
        assert (row.expected_count, row.actual_count) == (3, 2)
        gap = '2026-10-26T09:16:00+08:00'
        assert row.missing_ranges == [dict(start=gap, end=gap, count=1)]
        assert db.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.parametrize('stamp,include_today', [
    ('09:18:00', False), ('11:30:00', False), ('15:04:59', False),
    ('15:05:00', True), ('18:00:00', True),
])
def test_emotion_live_sampling_preserves_closed_day_and_after_close_archives(client, monkeypatch, stamp, include_today):
    from app.market.kpl_emotion import FAMILY as EMOTION

    configure_family(client, login(client), EMOTION, interval_seconds=60)
    day = CLOCK.date()
    previous = day - timedelta(days=3)
    clock = datetime.fromisoformat(f'{day}T{stamp}+08:00')
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(recovery, 'now', lambda db: clock)
    monkeypatch.setattr(recovery, 'closed_days', lambda *a: [previous])
    monkeypatch.setattr('app.market.session.market_session',
                        lambda *a: SimpleNamespace(current_day_active=True, trade_day=day))
    snapshots, archives = [], []
    monkeypatch.setattr(recovery, 'emotion_snapshot_coverage',
                        lambda db, provider, config, target, checked: snapshots.append((target, checked)))
    def archive(db, provider, config, dataset, scope, days, checked, allowance):
        assert provider.family == EMOTION and dataset == 'emotion_history' and scope == 'all'
        archives.extend(days)
    monkeypatch.setattr(recovery, 'daily_coverage', archive)

    recovery.reconcile()

    assert snapshots == [(day, clock)]
    assert set(archives) == ({previous, day} if include_today else {previous})
    with Session(engine()) as db:
        assert not db.scalar(select(Job.id))


def test_internal_minute_hole_is_found_even_with_newest_close_present(client, monkeypatch):
    minute_setup(client, monkeypatch)
    monkeypatch.setattr('test_minute_recovery.EARLY', datetime.fromisoformat(DAY+'T16:00:00+08:00'))
    seed_window(window([('09:31', '11'), ('09:33', '12'), ('15:00', '13')]))
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(FAMILY)); config = db.get(SourceSettings, provider.id)
        recovery.minute_coverage(db, provider, config, [date.fromisoformat(DAY)], CLOCK, [0])
    with Session(engine()) as db:
        row = db.scalar(select(RecoveryCoverage))
        assert row.state == 'partial' and row.expected_count == 240 and row.actual_count == 3
        assert row.missing_ranges[0]['start'].endswith('09:32:00+08:00')
        assert any(item['end'].endswith('14:59:00+08:00') for item in row.missing_ranges)
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_full_real_minute_series_is_complete_and_reconciliation_is_idempotent(client, monkeypatch):
    minute_setup(client, monkeypatch)
    monkeypatch.setattr('test_minute_recovery.EARLY', datetime.fromisoformat(DAY+'T16:00:00+08:00'))
    stamps = sorted(recovery.minute_slots(date.fromisoformat(DAY), '1m', CLOCK))
    seed_window(window([(stamp.strftime('%H:%M'), '11') for stamp in stamps]))
    for _ in range(2):
        with Session(engine()) as db, db.begin():
            provider = db.get(Provider, source_id(FAMILY)); config = db.get(SourceSettings, provider.id)
            recovery.minute_coverage(db, provider, config, [date.fromisoformat(DAY)], CLOCK, [3])
    with Session(engine()) as db:
        rows = list(db.scalars(select(RecoveryCoverage)))
        assert len(rows) == 1 and rows[0].state == 'complete' and rows[0].missing_ranges == []
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_expired_source_window_keeps_explicit_unrecoverable_gap(client, monkeypatch):
    minute_setup(client, monkeypatch)
    old_day = date(2026, 10, 1)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(FAMILY)); config = db.get(SourceSettings, provider.id)
        recovery.minute_coverage(db, provider, config, [old_day], CLOCK, [10])
    with Session(engine()) as db:
        row = db.scalar(select(RecoveryCoverage))
        assert row.state == 'unsupported' and row.reason == 'historical_window_expired'
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_pending_progress_survives_new_sessions_without_duplicate_jobs(client, monkeypatch):
    minute_setup(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(FAMILY)); config = db.get(SourceSettings, provider.id)
        row = recovery.target(db, provider, config, 'minute.bars', date.fromisoformat(DAY), {'stock':'synthetic'}, '1m', CLOCK)
        job = Job(owner_id=config.owner_id, kind='source.collect', payload={'source_id':str(provider.id)}, available_at=CLOCK)
        db.add(job); db.flush(); row.job_id = job.id
    with Session(engine()) as db, db.begin():
        row = db.scalar(select(RecoveryCoverage))
        assert recovery.pending(db, row, CLOCK) is True and row.state == 'queued'
        assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_blocked_enqueue_error_keeps_its_reason_across_reconciliation(client, monkeypatch):
    minute_setup(client, monkeypatch)
    from app.market.protocol import SourceFailure
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(FAMILY)); config = db.get(SourceSettings, provider.id)
        row = recovery.target(db, provider, config, 'minute.bars', date.fromisoformat(DAY), {}, '1m', CLOCK)
        recovery.queue_error(row, SourceFailure('credentials_missing'), CLOCK)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(FAMILY)); config = db.get(SourceSettings, provider.id)
        row = recovery.target(db, provider, config, 'minute.bars', date.fromisoformat(DAY), {}, '1m', CLOCK)
        assert recovery.pending(db, row, CLOCK + timedelta(minutes=5))
        assert (row.state, row.reason) == ('blocked', 'credentials_missing')
        assert not recovery.pending(db, row, CLOCK + timedelta(minutes=16))


def test_binding_revision_change_releases_failed_recovery_without_changing_source_config(client, monkeypatch):
    minute_setup(client, monkeypatch)
    from app.integrations.credentials import SourceCredentials
    connection_id = uuid.uuid4()
    revision = [1]
    monkeypatch.setattr('app.integrations.credentials.resolve_source_credentials', lambda *a, **k:
        SourceCredentials(None, 'https://example.invalid', connection_id, revision[0]))
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(FAMILY)); config = db.get(SourceSettings, provider.id)
        row = recovery.target(db, provider, config, 'minute.bars', date.fromisoformat(DAY), {}, '1m', CLOCK)
        job = Job(owner_id=config.owner_id, kind='source.collect', payload={}, state='failed', error_code='permission_missing')
        db.add(job); db.flush(); row.job_id = job.id
        assert recovery.pending(db, row, CLOCK)
        previous_signature = row.configuration_sha256
        assert row.state == 'blocked'
    revision[0] = 2
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(FAMILY)); config = db.get(SourceSettings, provider.id)
        row = recovery.target(db, provider, config, 'minute.bars', date.fromisoformat(DAY), {}, '1m', CLOCK)
        assert row.configuration_sha256 != previous_signature
        assert not recovery.pending(db, row, CLOCK)
        assert row.job_id is None and (row.state, row.reason) == ('missing', None)


@pytest.mark.parametrize('state,expected', [('complete','complete'), ('missing','queued')])
def test_daily_recovery_rechecks_dates_and_never_invents_zero(client, monkeypatch, state, expected):
    minute_setup(client, monkeypatch)
    monkeypatch.setattr('app.market.review_store.load_dataset', lambda *a, **k: {'state':state,'row_count':5 if state=='complete' else 0})
    def queue(db, owner, dataset, day, **kwargs):
        assert day == date(2026,10,23) and kwargs['force'] is True
        job = Job(owner_id=owner.id, kind='review.collect', payload={'review_context':{'day':str(day),'dataset':dataset}})
        db.add(job); db.flush(); return job
    monkeypatch.setattr('app.market.review_jobs.queue_dataset', queue)
    with Session(engine()) as db, db.begin():
        provider=db.get(Provider,source_id(FAMILY)); config=db.get(SourceSettings,provider.id)
        recovery.daily_coverage(db,provider,config,'daily','all',[date(2026,10,23)],CLOCK,[2])
    with Session(engine()) as db:
        row=db.scalar(select(RecoveryCoverage))
        assert row.state==expected and row.expected_count is None


def test_recovery_get_does_not_schedule_or_contact_source(client, monkeypatch):
    minute_setup(client, monkeypatch)
    monkeypatch.setattr(recovery,'reconcile',lambda **k:pytest.fail('GET must not reconcile'))
    response=client.get('/api/v1/data-recovery')
    assert response.status_code==200 and response.json()['data']['items']==[]
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job))==0


def test_recovery_get_filters_raw_source_identity_and_day_in_counts_and_health(client, monkeypatch):
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.resilience_models import DataHealth
    minute_setup(client, monkeypatch, second_period=True)
    monkeypatch.setattr(recovery, 'reconcile', lambda **kwargs: pytest.fail('GET must not reconcile'))
    first, second = source_id(FAMILY), source_id('sina_minute_5m')
    selected_day = date.fromisoformat(DAY)
    with Session(engine()) as db, db.begin():
        providers = [db.get(Provider, identity) for identity in [first, second]]
        configs = [db.get(SourceSettings, identity) for identity in [first, second]]
        for index, (provider, config) in enumerate(zip(providers, configs)):
            selected = recovery.target(db, provider, config, 'minute.bars', selected_day, {}, '1m', CLOCK)
            selected.state = 'partial' if index == 0 else 'complete'
            recovery.target(db, provider, config, 'minute.bars', selected_day - timedelta(days=1), {}, '1m', CLOCK)
            db.add(DataHealth(owner_id=config.owner_id, primary_source_id=provider.id, active_source_id=provider.id,
                capability='synthetic:source-filter', scope_key='a'*64, scope={}, trade_day=selected_day,
                state='attention', stage='source', configuration_sha256=configuration_fingerprint(provider, config),
                updated_at=CLOCK))
        db.add(DataHealth(owner_id=configs[1].owner_id, primary_source_id=second, active_source_id=first,
            capability='synthetic:fallback-filter', scope_key='b'*64, scope={}, trade_day=selected_day,
            state='attention', stage='source', configuration_sha256=configuration_fingerprint(providers[1], configs[1]),
            updated_at=CLOCK))
    response = client.get('/api/v1/data-recovery', params={'source_id': str(first), 'day': DAY})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert len(data['items']) == 1 and data['items'][0]['source_id'] == str(first)
    assert data['items'][0]['trade_day'] == DAY and data['counts'] == {'partial': 1}
    assert not data['has_more'] and len(data['health']) == 2
    assert all(str(first) in [row['primary_source_id'], row['active_source_id']] for row in data['health'])
    empty = client.get('/api/v1/data-recovery', params={'source_id': str(uuid.uuid4()), 'day': DAY}).json()['data']
    assert empty['items'] == [] and empty['counts'] == {} and empty['health'] == []
    assert empty['health_state'] is None
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_recovery_source_filter_keeps_owner_scope(client, monkeypatch):
    minute_setup(client, monkeypatch)
    identity = source_id(FAMILY)
    with Session(engine()) as db, db.begin():
        provider, config = db.get(Provider, identity), db.get(SourceSettings, identity)
        own = recovery.target(db, provider, config, 'minute.bars', date.fromisoformat(DAY), {'tag': 'own'}, '1m', CLOCK)
        db.flush()
        own_id = own.id
        other = User(username='other-recovery-scope', role='viewer', password_hash='synthetic-unused', enabled=True)
        db.add(other); db.flush()
        foreign = recovery.target(db, provider, config, 'minute.bars', date.fromisoformat(DAY), {'tag': 'private'}, '1m', CLOCK)
        foreign.owner_id = other.id
        db.get(User, config.owner_id).role = 'viewer'
    response = client.get('/api/v1/data-recovery', params={'source_id': str(identity), 'day': DAY})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert len(data['items']) == 1 and data['items'][0]['id'] == str(own_id)
    assert data['counts'] == {'missing': 1}


@pytest.mark.parametrize('filters', [{'source_id': 'invalid-source'}, {'day': 'invalid-day'}, {'day': '2026-02-30'}])
def test_recovery_get_rejects_invalid_filters_without_scheduling(client, monkeypatch, filters):
    minute_setup(client, monkeypatch)
    monkeypatch.setattr(recovery, 'reconcile', lambda **kwargs: pytest.fail('GET must not reconcile'))
    assert client.get('/api/v1/data-recovery', params=filters).status_code == 422
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_worker_roles_cannot_take_each_others_tasks(client, monkeypatch):
    minute_setup(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        owner=db.scalar(select(User).where(User.role=='admin'))
        db.add_all([Job(owner_id=owner.id,kind='source.collect',payload={'source_id':str(source_id(FAMILY))}),
                    Job(owner_id=owner.id,kind='source.collect',payload={'source_id':str(source_id(FAMILY)), 'minute_context':{}}),
                    Job(owner_id=owner.id,kind='system.check',payload={})])
    live=claim('realtime'); general=claim('general')
    assert live and live.kind=='source.collect' and 'minute_context' not in live.payload
    # Separate roles share the source execution fence: history must wait for
    # this source's current lease, while unrelated general work can proceed.
    assert claim('recovery') is None
    from app.worker import finish
    assert finish(live.id, live.lease_token)
    history=claim('recovery')
    assert history and 'minute_context' in history.payload
    assert general and general.kind=='system.check'
    assert claim('scheduler') is None and claim('realtime') is None


def test_readiness_requires_each_configured_worker_role(client, monkeypatch):
    minute_setup(client, monkeypatch)
    monkeypatch.setattr(settings(),'required_worker_roles',['scheduler','realtime','recovery','general'])
    assert client.get('/health/ready').status_code==503
    with Session(engine()) as db, db.begin():
        stamp=datetime.now(CLOCK.tzinfo)
        db.add_all([WorkerHeartbeat(worker_id='market:'+role+':synthetic',seen_at=stamp)
                    for role in settings().required_worker_roles])
    assert client.get('/health/ready').status_code==200


def test_same_scope_does_not_assume_previous_weekday(client, monkeypatch):
    minute_setup(client, monkeypatch)
    days=[date(2026,10,19),date(2026,10,23)]
    monkeypatch.setattr('app.market.review_store.available_trade_days',lambda *a,**k:days)
    with Session(engine()) as db:
        config=db.get(SourceSettings,source_id(FAMILY))
        assert recovery.closed_days(db,config,CLOCK)==list(reversed(days))


def test_bounded_scans_keep_recent_baselines_and_rotate_unchecked_older_dates(client, monkeypatch):
    minute_setup(client, monkeypatch)
    days = [CLOCK.date()-timedelta(days=offset) for offset in range(25)]
    with Session(engine()) as db, db.begin():
        provider=db.get(Provider, source_id(FAMILY)); config=db.get(SourceSettings, provider.id)
        first=recovery.bounded_days(db, provider, config, days)
        assert len(first)==recovery.MAX_SOURCE_DAYS and first[:2]==days[:2]
        for day in first:
            recovery.target(db, provider, config, 'minute.bars', day, {}, '1m', CLOCK)
    with Session(engine()) as db:
        provider=db.get(Provider, source_id(FAMILY)); config=db.get(SourceSettings, provider.id)
        second=recovery.bounded_days(db, provider, config, days)
        assert second[:2]==days[:2] and set(first[2:]).isdisjoint(second[2:])


def test_later_source_failure_keeps_an_earlier_committed_batch_and_progress(client, monkeypatch):
    minute_setup(client, monkeypatch, second_period=True)
    from app.market.protocol import SourceFailure
    monkeypatch.setattr(recovery, 'now', lambda db: CLOCK)
    monkeypatch.setattr(recovery, 'closed_days', lambda *a: [date.fromisoformat(DAY)])
    monkeypatch.setattr('app.market.session.market_session', lambda *a: SimpleNamespace(current_day_active=False))
    def scan(db, provider, config, days, clock, allowance):
        if provider.family != FAMILY:
            raise SourceFailure('synthetic_source_failure')
        row=recovery.target(db, provider, config, 'minute.bars', days[0], {}, '1m', clock)
        row.state='partial'
    monkeypatch.setattr(recovery, 'minute_coverage', scan)
    committed=[]
    def progress():
        with Session(engine()) as db:
            committed.append(db.scalar(select(func.count()).select_from(RecoveryCoverage)))
    recovery.reconcile(force=True, progress=progress)
    with Session(engine()) as db:
        rows=list(db.scalars(select(RecoveryCoverage)))
        assert len(rows)==1 and rows[0].source_id==source_id(FAMILY)
        assert committed[-1]==1


def test_out_of_local_daily_window_remains_explicit_without_queueing(client, monkeypatch):
    minute_setup(client, monkeypatch)
    monkeypatch.setattr('app.market.review_store.load_dataset',lambda *a,**k:{'state':'missing','row_count':0})
    monkeypatch.setattr('app.market.review_jobs.queue_dataset',lambda *a,**k:pytest.fail('expired day cannot queue'))
    with Session(engine()) as db, db.begin():
        provider=db.get(Provider,source_id(FAMILY)); config=db.get(SourceSettings,provider.id)
        recovery.daily_coverage(db,provider,config,'daily','all',[CLOCK.date()-timedelta(days=366)],CLOCK,[3])
    with Session(engine()) as db:
        row=db.scalar(select(RecoveryCoverage))
        assert (row.state,row.reason)==('unsupported','local_history_window_expired')


def test_slow_source_stops_inside_its_batch_and_commits_finished_dates(client,monkeypatch):
    minute_setup(client,monkeypatch)
    elapsed=[0.0]
    monkeypatch.setattr(recovery,'monotonic',lambda:elapsed[0])
    def saved(*a,**k):
        elapsed[0]+=2
        return {'state':'complete','row_count':5}
    monkeypatch.setattr('app.market.review_store.load_dataset',saved)
    with Session(engine()) as db,db.begin():
        db.info['recovery.deadline']=1
        provider=db.get(Provider,source_id(FAMILY)); config=db.get(SourceSettings,provider.id)
        recovery.daily_coverage(db,provider,config,'daily','all',[CLOCK.date(),CLOCK.date()-timedelta(days=1)],CLOCK,[3])
    with Session(engine()) as db:
        rows=list(db.scalars(select(RecoveryCoverage)))
        assert len(rows)==1 and rows[0].state=='complete'


def test_missing_fund_scopes_are_persisted_as_unknown_and_cannot_stall_date_rotation(client,monkeypatch):
    minute_setup(client,monkeypatch)
    monkeypatch.setattr('app.market.fund_history.saved_archives',lambda *a:[])
    days=[CLOCK.date()-timedelta(days=offset) for offset in range(20)]
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(FAMILY)); config=db.get(SourceSettings,provider.id)
        first=recovery.bounded_days(db,provider,config,days)
        recovery.fund_coverage(db,provider,config,first,CLOCK)
    with Session(engine()) as db:
        provider=db.get(Provider,source_id(FAMILY)); config=db.get(SourceSettings,provider.id)
        second=recovery.bounded_days(db,provider,config,days)
        assert set(first[2:]).isdisjoint(second[2:])
        rows=list(db.scalars(select(RecoveryCoverage)))
        assert len(rows)==recovery.MAX_SOURCE_DAYS
        assert all(row.reason=='historical_scope_unverified' and row.actual_count is None and row.expected_count is None for row in rows)


def test_retired_scope_cannot_pin_a_recently_scanned_source_ahead_of_others(client,monkeypatch):
    minute_setup(client,monkeypatch,second_period=True)
    with Session(engine()) as db,db.begin():
        for family,scope,clock in ((FAMILY,'retired',CLOCK-timedelta(days=200)),
                                  (FAMILY,'current',CLOCK),('sina_minute_5m','current',CLOCK-timedelta(hours=1))):
            provider=db.get(Provider,source_id(family)); config=db.get(SourceSettings,provider.id)
            recovery.target(db,provider,config,'minute.bars',CLOCK.date(),{'scope':scope},'1m',clock)
    monkeypatch.setattr(recovery,'now',lambda db:CLOCK)
    monkeypatch.setattr(recovery,'closed_days',lambda *a:[CLOCK.date()])
    monkeypatch.setattr('app.market.session.market_session',lambda *a:SimpleNamespace(current_day_active=False))
    calls=[]
    monkeypatch.setattr(recovery,'minute_coverage',lambda db,p,*a:calls.append(p.family))
    recovery.reconcile(force=True)
    assert calls==['sina_minute_5m',FAMILY]


def test_rejected_daily_enqueue_does_not_commit_an_orphan_intent(client,monkeypatch):
    from app.market.protocol import SourceFailure
    minute_setup(client,monkeypatch)
    monkeypatch.setattr('app.market.review_store.load_dataset',lambda *a,**k:{'state':'missing','row_count':0})
    def rejected(db,owner,*a,**k):
        db.add(Job(owner_id=owner.id,kind='review.collect',payload={}))
        db.flush()
        raise SourceFailure('configuration_changed')
    monkeypatch.setattr('app.market.review_jobs.queue_dataset',rejected)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(FAMILY)); config=db.get(SourceSettings,provider.id)
        recovery.daily_coverage(db,provider,config,'daily','all',[CLOCK.date()],CLOCK,[3])
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job))==0
        assert db.scalar(select(RecoveryCoverage)).state=='blocked'


def test_tushare_budget_holds_the_lease_row_until_its_decision_finishes(client, monkeypatch):
    import threading
    from sqlalchemy.exc import OperationalError
    from sqlalchemy import text, update
    from app.market.budget import reserve_tushare_request
    from app.market.protocol import SourceFailure
    minute_setup(client,monkeypatch)
    entered,release=threading.Event(),threading.Event()
    failures=[]
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        config=db.get(SourceSettings,source_id(FAMILY))
        job=Job(owner_id=config.owner_id,kind='review.collect',payload={'source_id':str(config.source_id)},
            state='running',attempts=1,lease_token=uuid.uuid4(),lease_until=datetime.now(CLOCK.tzinfo)+timedelta(minutes=2))
        db.add(job);db.flush()
    def validation(*a):
        entered.set()
        assert release.wait(5)
        raise SourceFailure('synthetic_validation_stop')
    monkeypatch.setattr('app.market.review_jobs.validate_context',validation)
    def reserve():
        try:reserve_tushare_request(job)
        except SourceFailure as error:failures.append(error.reason)
    thread=threading.Thread(target=reserve)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(OperationalError):
            with Session(engine()) as db,db.begin():
                db.execute(text("SET LOCAL lock_timeout='100ms'"))
                db.execute(update(Job).where(Job.id==job.id).values(lease_token=uuid.uuid4()))
    finally:
        release.set();thread.join(timeout=5)
    assert not thread.is_alive() and failures==['synthetic_validation_stop']
    with Session(engine()) as db:
        assert db.get(Job,job.id).lease_token==job.lease_token


def test_backfill_reserves_remaining_live_allowance_without_blocking_live_jobs(client, monkeypatch):
    minute_setup(client,monkeypatch)
    from app.market.budget import protect_live_budget
    from app.market.minute import FAMILY_INTERVALS
    stamp=CLOCK.replace(hour=10,minute=0)
    history=SimpleNamespace(kind='source.collect',payload={'minute_context':{}})
    live=SimpleNamespace(kind='source.collect',payload={})
    with Session(engine()) as db:
        assert protect_live_budget(db,history,stamp,300,100,FAMILY_INTERVALS)==stamp.replace(hour=15,minute=5)
        assert protect_live_budget(db,history,stamp,500,10,FAMILY_INTERVALS) is None
        assert protect_live_budget(db,live,stamp,300,100,FAMILY_INTERVALS) is None
        assert protect_live_budget(db,history,CLOCK,300,299,FAMILY_INTERVALS) is None


def test_confirmed_holiday_does_not_reserve_nonexistent_live_polls(client,monkeypatch):
    minute_setup(client,monkeypatch)
    from app.market.budget import protect_live_budget
    from app.market.minute import FAMILY_INTERVALS
    monkeypatch.setattr('app.market.session.market_session',lambda *a,**k:SimpleNamespace(is_trading_day=False))
    with Session(engine()) as db:
        job=SimpleNamespace(kind='source.collect',payload={'minute_context':{}})
        assert protect_live_budget(db,job,CLOCK.replace(hour=10),300,299,FAMILY_INTERVALS) is None
