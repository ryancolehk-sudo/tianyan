"""Snapshots keep a bounded close receipt window; daily archives stay separate."""
from datetime import date, datetime, timedelta
import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market.models import DataFetchRun, SourceSettings
from app.market.realtime_schedule import FAMILIES, align_realtime, permit_realtime
from app.market.review_schedule import schedule_daily_archives
from app.market.service import source_id
from app.models import AuditEvent, Job
from test_data_sources import configure, login
from test_tushare import TOKEN, source


def test_newer_free_calendar_cannot_override_verified_paid_market_day(client,monkeypatch):
    from app.market.collect import store_calendar_batch
    from app.market.mairui import pool_day
    from app.models import Provider
    from test_data_sources import configuration
    monkeypatch.setattr(settings(),'tushare_token',SecretStr(TOKEN))
    headers=login(client)
    for family in ('tushare_calendar_sse','tushare_calendar_szse','baostock_daily'):
        config=source(family) if family.startswith('tushare_') else {**configuration(family),'interval_seconds':21600}
        configure(client,headers,config,key=family)
    clock=at('10:00:00');prior=date(2026,10,23);today=date(2026,10,26)
    with Session(engine()) as db,db.begin():
        for family,exchange in (('tushare_calendar_sse','SSE'),('tushare_calendar_szse','SZSE')):
            provider=db.get(Provider,source_id(family))
            store_calendar_batch(db,provider,db.get(SourceSettings,provider.id),[(prior,True),(today,True)],
                clock-timedelta(days=1),exchanges=(exchange,))
        provider=db.get(Provider,source_id('baostock_daily'))
        store_calendar_batch(db,provider,db.get(SourceSettings,provider.id),[(prior,True),(today,False)],
            clock-timedelta(minutes=1),exchanges=('SSE','SZSE'))
        assert pool_day(db,clock)==today
        for family in ('tushare_calendar_sse','tushare_calendar_szse'):
            provider=db.get(Provider,source_id(family));provider.enabled=False
        db.flush()
        assert pool_day(db,clock)==prior


def at(value):
    return datetime.fromisoformat('2026-10-26T' + value + '+08:00')


@pytest.mark.parametrize('family', sorted(FAMILIES))
def test_automatic_snapshot_families_respect_distinct_local_windows(family):
    # These are the accepted local schedules, not claimed supplier limits.
    # Auction-only feeds, quiet quotes and continuous rankings are distinct.
    auctions = {'tencent_auction': '09:26:00', 'fuyao_auction': '09:29:00'}
    quiet_quotes = {'tencent_web', 'tencent_market_rt', 'tushare_rt_k',
                    'tushare_market_rt', 'tushare_rt_indices', 'fuyao_quotes', 'fuyao_indices'}
    opening = ('09:30:00' if family == 'tencent_market_rt' else
               '09:26:00' if family == 'tushare_stk_auction' else '09:15:00')
    popularity = family in {'direction_ths_hot', 'direction_tdx_hot', 'direction_em_hot'}
    close = auctions.get(family, '23:00:00' if popularity else '15:05:00')
    next_opening = at(opening) + timedelta(days=1)
    for clock in ('09:25:59', '09:26:00', '09:28:59', '09:29:00',
                  '11:29:59', '11:30:00', '11:30:59', '11:31:00',
                  '12:00:00', '12:59:59', '13:00:00', '15:00:00', '15:04:59'):
        if clock < opening:
            expected = at(opening)
        elif clock >= close:
            expected = next_opening
        elif (family in quiet_quotes and '11:31:00' <= clock < '13:00:00'
              or family in {'direction_kpl_top', 'direction_kpl_emotion'} and '11:30:00' <= clock < '13:00:00'):
            expected = at('13:00:00')
        else:
            expected = at(clock)
        assert align_realtime(family, at(clock)) == expected, (family, clock)
    next_day = align_realtime(family, at(close))
    assert next_day == next_opening
    if popularity:
        for stamp in ('15:05:00', '16:30:00', '22:59:59'):
            assert align_realtime(family, at(stamp)) == at(stamp)
    else:
        assert align_realtime(family, at('15:23:00')) == next_day
    assert align_realtime(family,at('09:14:59'))==at(opening)


def configured(client, monkeypatch, family='tushare_market_rt'):
    monkeypatch.setattr(settings(), 'tushare_token', SecretStr(TOKEN))
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    headers = login(client)
    configure(client, headers, source(family))
    return source_id(family)


def running(ident, payload):
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        config = db.get(SourceSettings, ident)
        job = Job(owner_id=config.owner_id, kind='source.collect', state='running', attempts=1,
            payload={'source_id': str(ident), **payload}, lease_token=uuid.uuid4(),
            lease_until=at('23:59:59'))
        db.add(job)
        db.flush()
        return job


@pytest.mark.parametrize('marker', ['scheduled_realtime', 'scheduled_intraday', 'scheduled_eastmoney'])
def test_delayed_snapshot_job_after_restart_is_deferred_without_fetch_or_permit(client, monkeypatch, marker):
    ident = configured(client, monkeypatch)
    job = running(ident, {marker: True})
    monkeypatch.setattr('app.market.realtime_schedule.now', lambda db: at('15:23:00'))
    assert permit_realtime(job) is False
    with Session(engine()) as db:
        saved = db.get(Job, job.id)
        assert saved.state == 'queued' and saved.attempts == 0 and saved.lease_token is None
        assert saved.available_at.date() == date(2026, 10, 27)
        assert saved.error_code == 'intraday_window_wait'
        assert not db.scalar(select(DataFetchRun.id))
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action.like('source.request.%')))


@pytest.mark.parametrize('clock',['09:15:00','11:30:00','13:20:00','15:00:00','15:04:59'])
def test_intraday_restart_keeps_eligible_current_job(client, monkeypatch,clock):
    ident = configured(client, monkeypatch)
    job = running(ident, {'scheduled_realtime': True})
    monkeypatch.setattr('app.market.realtime_schedule.now', lambda db: at(clock))
    monkeypatch.setattr('app.market.mairui.pool_day', lambda *a, **k: date(2026, 10, 26))
    assert permit_realtime(job) is True
    with Session(engine()) as db:
        assert db.get(Job, job.id).state == 'running'


@pytest.mark.parametrize('clock', ['11:31:00', '12:00:00', '12:59:59'])
def test_intraday_restart_defers_quiet_lunch_without_spending_requests(client, monkeypatch, clock):
    ident = configured(client, monkeypatch)
    job = running(ident, {'scheduled_realtime': True})
    monkeypatch.setattr('app.market.realtime_schedule.now', lambda db: at(clock))
    monkeypatch.setattr('app.market.mairui.pool_day', lambda *a, **k: date(2026, 10, 26))
    assert permit_realtime(job) is False
    with Session(engine()) as db:
        saved = db.get(Job, job.id)
        assert saved.state == 'queued' and saved.available_at == at('13:00:00')
        assert saved.error_code == 'intraday_window_wait' and saved.attempts == 0
        assert saved.lease_token is None
        assert not db.scalar(select(DataFetchRun.id))
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action.like('source.request.%')))


@pytest.mark.parametrize('start,end,expected',[
    ('11:29:00','13:01:00',120),('11:30:00','13:00:00',0),
    ('11:20:00','13:05:00',900),('10:00:00','10:10:00',600),
    ('11:45:00','12:15:00',0),('13:00:00','13:05:00',300),
])
def test_normal_lunch_is_compressed_but_missing_trading_time_remains(start,end,expected):
    from app.market.session import observation_gap_seconds
    assert observation_gap_seconds(at(start),at(end))==expected
    assert observation_gap_seconds(at(start),at(end)+timedelta(days=1))==float('inf')
    if start!=end:assert observation_gap_seconds(at(end),at(start))==float('inf')


def prepare_archive(client, monkeypatch):
    ident = configured(client, monkeypatch, 'tushare_daily')
    clock = [at('16:00:00')]
    monkeypatch.setattr('app.market.review_schedule.now', lambda db: clock[0])
    monkeypatch.setattr('app.market.review_jobs.now', lambda db: clock[0])
    monkeypatch.setattr('app.market.review_schedule.available_trade_days', lambda *a, **k: [date(2026, 10, 26)])
    return ident, clock


def test_daily_archive_is_whole_date_scope_and_deduplicates_on_restart(client, monkeypatch):
    ident, clock = prepare_archive(client, monkeypatch)
    schedule_daily_archives()
    schedule_daily_archives()
    with Session(engine()) as db:
        jobs = list(db.scalars(select(Job).where(Job.kind == 'review.collect')))
        assert len(jobs) == 1
        assert jobs[0].payload['scheduled_daily_archive'] is True
        assert jobs[0].payload['review_context']['day'] == '2026-10-26'
        assert jobs[0].payload['review_context']['scope'] == 'all'
        assert db.get(SourceSettings, ident).config['symbols'][0]['symbol'] == '000001'
        assert not db.scalar(select(DataFetchRun.id))


def test_archive_missing_table_retries_later_but_never_each_worker_tick(client, monkeypatch):
    ident, clock = prepare_archive(client, monkeypatch)
    schedule_daily_archives()
    with Session(engine()) as db, db.begin():
        job = db.scalar(select(Job).where(Job.kind == 'review.collect'))
        job.state, job.error_code, job.completed_at = 'failed', 'review_required_table_empty', clock[0]
    clock[0] += timedelta(minutes=14)
    schedule_daily_archives()
    with Session(engine()) as db:
        assert len(list(db.scalars(select(Job.id).where(Job.kind == 'review.collect')))) == 1
    clock[0] += timedelta(minutes=2)
    schedule_daily_archives()
    with Session(engine()) as db:
        assert len(list(db.scalars(select(Job.id).where(Job.kind == 'review.collect')))) == 2


def test_complete_nonempty_archive_is_not_refetched(client, monkeypatch):
    prepare_archive(client, monkeypatch)
    monkeypatch.setattr('app.market.review_schedule.load_dataset', lambda *a, **k: {'state':'complete', 'row_count':5574})
    schedule_daily_archives()
    with Session(engine()) as db:
        assert not db.scalar(select(Job.id).where(Job.kind == 'review.collect'))


@pytest.mark.parametrize('clock', ['09:30:00', '13:30:00', '21:30:00'])
def test_archive_not_queued_during_realtime_session_or_after_publication_window(client, monkeypatch, clock):
    prepare_archive(client, monkeypatch)
    monkeypatch.setattr('app.market.review_schedule.now', lambda db: at(clock))
    schedule_daily_archives()
    with Session(engine()) as db:
        assert not db.scalar(select(Job.id).where(Job.kind == 'review.collect'))


def test_unpublished_long_interval_keeps_a_today_retry():
    from app.market.publication_schedule import next_publication
    assert next_publication('tushare_dc_index', at('16:00:00'), 21600, unpublished=True) == at('16:15:00')
    assert next_publication('tushare_dc_index', at('21:20:00'), 21600, unpublished=True).date() == date(2026, 10, 27)
    assert next_publication('tushare_dc_index', at('16:00:00'), 21600).date() == date(2026, 10, 27)


def test_scheduled_empty_publication_retry_is_not_blocked_by_old_six_hour_interval(client, monkeypatch):
    from app.market.service import schedule_sources
    ident = configured(client, monkeypatch, 'tushare_dc_index')
    monkeypatch.setattr('app.market.service.now', lambda db: at('16:15:00'))
    monkeypatch.setattr('app.market.mairui.pool_day', lambda *a, **k: date(2026, 10, 26))
    with Session(engine()) as db, db.begin():
        # A plan persisted by the previous release must also recover today.
        db.get(SourceSettings, ident).next_run_at = at('16:00:00') + timedelta(days=1)
        db.add(DataFetchRun(source_id=ident, capability='reference', started_at=at('16:00:00'),
            received_at=at('16:00:00'), status='empty', reason='daily_data_not_published', request_count=1))
    schedule_sources()
    with Session(engine()) as db:
        job = db.scalar(select(Job).where(Job.kind == 'source.collect'))
        assert job is not None and job.payload['scheduled_publication'] is True


def test_archive_delayed_past_evening_window_defers_before_supplier_permit(client, monkeypatch):
    from app.market.budget import reserve_tushare_request
    prepare_archive(client, monkeypatch)
    schedule_daily_archives()
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        job = db.scalar(select(Job).where(Job.kind == 'review.collect'))
        job.state, job.attempts = 'running', 1
        job.lease_token, job.lease_until = uuid.uuid4(), at('23:59:59')
    monkeypatch.setattr('app.market.budget.now', lambda db: at('21:30:00'))
    assert reserve_tushare_request(job) is False
    with Session(engine()) as db:
        saved = db.get(Job, job.id)
        assert saved.state == 'queued' and saved.attempts == 0
        assert saved.available_at.date() == date(2026, 10, 27)
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'source.request.tushare'))


@pytest.mark.parametrize('family', ['direction_ths_hot', 'direction_tdx_hot', 'direction_em_hot'])
def test_evening_popularity_scheduler_uses_ranking_window(client, monkeypatch, family):
    from test_direction_data import setup
    from app.market.service import schedule_sources
    setup(client, monkeypatch)
    clock = at('22:00:00')
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr('app.market.service.now', lambda db: clock)
    monkeypatch.setattr('app.market.mairui.pool_day', lambda *a, **k: clock.date())
    with Session(engine()) as db, db.begin():
        for config in db.scalars(select(SourceSettings)):
            config.next_run_at = clock + timedelta(days=1)
        db.get(SourceSettings, source_id(family)).next_run_at = clock - timedelta(minutes=1)
    schedule_sources()
    with Session(engine()) as db:
        jobs = list(db.scalars(select(Job).where(Job.kind == 'source.collect')))
        assert len(jobs) == 1
        assert jobs[0].payload['source_id'] == str(source_id(family))
        assert jobs[0].payload['scheduled_realtime'] is True
