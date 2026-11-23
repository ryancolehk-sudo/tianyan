"""Strict direction-source recovery on disposable PG; no real vendor calls."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.auction_schedule import DIRECTION_RECOVERY, next_collection, schema_recheck
from app.market.collect import collect_job, store_calendar_batch
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response
from app.market.service import permit_direction_probe, schedule_sources, source_id
from app.models import AuditEvent, Job, Provider, User
from app.worker import claim
from test_data_sources import configure, configuration, login


def at(value):
    return datetime.fromisoformat(value + '+08:00')


AT = at('2026-10-26T10:30:00')
CALENDAR = 'baostock_daily'


def setup(client, monkeypatch, family, *, clock=AT, opened=True):
    headers = login(client)
    body = configuration(family)
    body['interval_seconds'] = 300
    configure(client, headers, body, key='recovery-' + family)
    calendar_body = configuration(CALENDAR)
    calendar_body['interval_seconds'] = 21600
    configure(client, headers, calendar_body, key='recovery-calendar')
    current = [clock]
    class CollectorClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz) if tz is not None else current[0].replace(tzinfo=None)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr('app.market.service.now', lambda db: current[0])
    monkeypatch.setattr('app.market.realtime_schedule.now', lambda db: current[0])
    monkeypatch.setattr('app.market.collect.now', lambda db: current[0])
    monkeypatch.setattr('app.market.collect.datetime', CollectorClock)
    with Session(engine()) as db, db.begin():
        calendar_provider = db.get(Provider, source_id(CALENDAR))
        store_calendar_batch(db, calendar_provider, db.get(SourceSettings, calendar_provider.id),
            [(clock.date() - timedelta(days=1), True), (clock.date(), opened)],
            clock - timedelta(days=2), exchanges=('SSE', 'SZSE'))
        # The calendar is already seeded. This scenario dispatches only the
        # direction source, even when its synthetic afternoon is ahead of the
        # actual clock that configured the calendar connection.
        db.get(SourceSettings, calendar_provider.id).next_run_at = clock + timedelta(days=1)
        cfg = db.get(SourceSettings, source_id(family))
        cfg.paused_reason = 'schema_changed'
        cfg.next_run_at = clock - timedelta(seconds=1)
    return current


def fixture(family, clock, plate_code=None):
    if plate_code:
        return {'errcode':'0','Day':[clock.date().isoformat()],'Count':0,'list':[]}
    if family == 'direction_em_hot':
        return {'code': 0, 'data': [{'sc': 'SZ000001', 'rk': 1}, {'sc': 'SH600000', 'rk': 2}]}
    return {'errcode': '0', 'Day': [clock.date().isoformat()], 'Count': 2,
        'list': [['801001', '合成题材甲', 100, *([0] * 16)],
                 ['801002', '合成题材乙', 90, *([0] * 16)]]}


def forbid_fetch(*args, **kwargs):
    pytest.fail('A waiting/revoked direction probe must spend no HTTP')


@pytest.mark.parametrize('family', sorted(DIRECTION_RECOVERY))
@pytest.mark.parametrize('clock, expected', [
    ('2026-10-26T09:00:00', '2026-10-26T09:15:00'),
    ('2026-10-26T10:00:00', '2026-10-26T10:15:00'),
    ('2026-10-26T11:20:00', '2026-10-26T13:00:00'),
    ('2026-10-26T14:46:00', '2026-10-26T15:01:00'),
    ('2026-10-26T14:50:00', '2026-10-27T09:15:00'),
    ('2026-10-26T23:59:59', '2026-10-27T09:15:00'),
])
def test_probe_spacing_is_at_least_fifteen_minutes_and_window_aligned(family, clock, expected):
    assert next_collection(family, at(clock), 300, schema_probe=True) == at(expected)
    assert next_collection(family, at(clock).astimezone(UTC), 300, schema_probe=True) == at(expected)
    assert schema_recheck(family, 'schema_changed')
    for reason in ('permission_missing', 'decoder_changed', 'endpoint_not_allowed',
                   'upstream_redirect', 'response_too_large', 'unsafe_upstream_response'):
        assert not schema_recheck(family, reason)
    assert not schema_recheck('direction_ths_hot', 'schema_changed')
    assert not schema_recheck('direction_tdx_hot', 'schema_changed')


@pytest.mark.parametrize('family', sorted(DIRECTION_RECOVERY))
@pytest.mark.parametrize('valid', [True, False])
def test_only_full_validation_resumes_and_restart_does_not_duplicate(client, monkeypatch, family, valid):
    current = setup(client, monkeypatch, family)
    schedule_sources()
    job = claim()
    assert job and job.payload['schema_probe'] is True
    assert job.kind == 'source.collect' and job.payload['source_id'] == str(source_id(family))
    with Session(engine()) as db, db.begin():
        db.get(SourceSettings, source_id(family)).next_run_at = AT - timedelta(seconds=1)
    schedule_sources()
    assert claim() is None
    body = json.dumps(fixture(family, current[0]) if valid else {}).encode()
    monkeypatch.setattr('app.market.direction_sources.fetch',
        lambda source, **kwargs: Response(json.dumps(fixture(family,current[0],kwargs.get('plate_code'))).encode() if valid else body, current[0], 1, 200))
    collect_job(job)
    with Session(engine()) as db:
        cfg = db.get(SourceSettings, source_id(family))
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.source_id == cfg.source_id))
        assert cfg.paused_reason == (None if valid else 'schema_changed')
        assert run.status == ('complete' if valid else 'error')
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.source_id == cfg.source_id)) == ((2 if family=='direction_kpl_strength' else 1) if valid else 0)
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == 'source.request.direction-probe')) == 1
        if valid:
            assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'source.schema_recovered'))
        else:
            assert cfg.next_run_at >= run.received_at + timedelta(seconds=900)


@pytest.mark.parametrize('family', sorted(DIRECTION_RECOVERY))
def test_existing_failure_receipt_also_fences_first_probe_after_upgrade(client, monkeypatch, family):
    current = setup(client, monkeypatch, family)
    with Session(engine()) as db, db.begin():
        db.add(DataFetchRun(source_id=source_id(family), capability='market.direction',
            started_at=AT - timedelta(minutes=5), received_at=AT - timedelta(minutes=5),
            status='error', reason='schema_changed', expected=None, accepted=None,
            rejected=None, duplicates=None, elapsed_ms=1, request_count=1, cost_cny=None))
    schedule_sources()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.get(SourceSettings, source_id(family)).next_run_at == AT + timedelta(minutes=10)
    current[0] += timedelta(minutes=10)
    schedule_sources()
    assert claim().payload['schema_probe'] is True


@pytest.mark.parametrize('start, stop, expected', [
    ('2026-10-26T15:04:50', '2026-10-26T15:05:00', '2026-10-27T09:15:00'),
])
@pytest.mark.parametrize('family', sorted(DIRECTION_RECOVERY))
def test_queued_probe_crossing_collection_end_returns_attempt_without_fake_run(
        client, monkeypatch, family, start, stop, expected):
    current = setup(client, monkeypatch, family, clock=at(start))
    schedule_sources()
    job = claim()
    assert job and job.attempts == 1
    assert job.kind == 'source.collect' and job.payload['source_id'] == str(source_id(family))
    with Session(engine()) as db, db.begin():
        # The test's synthetic afternoon clock can be ahead of the actual DB
        # clock used by claim(); retain a valid lease across that simulated edge.
        db.get(Job, job.id).lease_until = max(datetime.now(UTC), at(stop)) + timedelta(minutes=5)
    current[0] = at(stop)
    monkeypatch.setattr('app.market.direction_sources.fetch', forbid_fetch)
    # Exercise the actual collector with this test's synthetic market clock.
    # The worker's independent real-time lease-renewal loop is covered by its
    # own/auction regressions and would mix two clocks in this boundary case.
    collect_job(job)
    with Session(engine()) as db:
        saved = db.get(Job, job.id)
        assert saved.state == 'queued' and saved.attempts == 0
        assert saved.lease_token is None and saved.lease_until is None
        assert saved.available_at == at(expected)
        assert db.scalar(select(DataFetchRun.id)) is None
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'source.request.direction-probe')) is None
        assert db.get(SourceSettings, source_id(family)).paused_reason == 'schema_changed'


@pytest.mark.parametrize('start, stop', [
    ('2026-10-26T11:29:50', '2026-10-26T11:30:00'),
    ('2026-10-26T14:59:50', '2026-10-26T15:01:00'),
])
@pytest.mark.parametrize('family', sorted(DIRECTION_RECOVERY))
def test_queued_probe_continues_through_lunch_and_close_receipt_window(
        client, monkeypatch, family, start, stop):
    current = setup(client, monkeypatch, family, clock=at(start))
    schedule_sources()
    job = claim()
    assert job and job.payload['schema_probe'] is True
    with Session(engine()) as db, db.begin():
        db.get(Job, job.id).lease_until = max(datetime.now(UTC), at(stop)) + timedelta(minutes=5)
    current[0] = at(stop)
    requests = []
    def fetch(source, **kwargs):
        requests.append(source)
        return Response(json.dumps(fixture(family, current[0],kwargs.get('plate_code'))).encode(), current[0], 1, 200)
    monkeypatch.setattr('app.market.direction_sources.fetch', fetch)
    collect_job(job)
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.source_id == source_id(family)))
        assert requests == [family] and run.status == 'complete'
        assert db.get(SourceSettings, source_id(family)).paused_reason is None
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == 'source.request.direction-probe')) == 1


@pytest.mark.parametrize('opened', [False, None])
def test_closed_or_missing_calendar_cannot_dispatch_recovery(client, monkeypatch, opened):
    setup(client, monkeypatch, 'direction_em_hot', opened=opened is True)
    if opened is None:
        with Session(engine()) as db, db.begin():
            db.get(Provider, source_id(CALENDAR)).rights = {'display': 'denied', 'cache': 'allowed'}
    monkeypatch.setattr('app.market.direction_sources.fetch', forbid_fetch)
    schedule_sources()
    with Session(engine()) as db:
        assert db.scalar(select(Job.id)) is None
        assert db.scalar(select(DataFetchRun.id)) is None
        assert db.get(SourceSettings, source_id('direction_em_hot')).paused_reason == 'schema_changed'


def test_queued_probe_rechecks_calendar_permission_immediately_before_http(client, monkeypatch):
    setup(client, monkeypatch, 'direction_em_hot')
    schedule_sources()
    job = claim()
    with Session(engine()) as db, db.begin():
        db.get(Provider, source_id(CALENDAR)).rights = {'display': 'denied', 'cache': 'allowed'}
    monkeypatch.setattr('app.market.direction_sources.fetch', forbid_fetch)
    collect_job(job)
    with Session(engine()) as db:
        saved = db.get(Job, job.id)
        assert saved.state == 'queued' and saved.attempts == 0 and saved.lease_token is None
        assert db.scalar(select(DataFetchRun.id)) is None
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'source.request.direction-probe')) is None


@pytest.mark.parametrize('reason', ['permission_missing', 'decoder_changed', 'unsafe_upstream_response'])
def test_other_pause_reasons_are_never_auto_recovered(client, monkeypatch, reason):
    setup(client, monkeypatch, 'direction_em_hot')
    with Session(engine()) as db, db.begin():
        db.get(SourceSettings, source_id('direction_em_hot')).paused_reason = reason
    monkeypatch.setattr('app.market.direction_sources.fetch', forbid_fetch)
    schedule_sources()
    with Session(engine()) as db:
        assert db.scalar(select(Job.id)) is None


def test_persisted_permit_blocks_another_worker_probe_before_fifteen_minutes(client, monkeypatch):
    current = setup(client, monkeypatch, 'direction_em_hot')
    schedule_sources()
    first = claim()
    assert permit_direction_probe(first)
    with Session(engine()) as db, db.begin():
        db.get(Job, first.id).state = 'succeeded'
        db.get(SourceSettings, source_id('direction_em_hot')).next_run_at = AT - timedelta(seconds=1)
    current[0] += timedelta(minutes=5)
    schedule_sources()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.get(SourceSettings, source_id('direction_em_hot')).next_run_at == AT + timedelta(minutes=15)


def test_ordinary_task_has_no_probe_marker_or_probe_side_effects(client, monkeypatch):
    setup(client, monkeypatch, 'direction_em_hot')
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(User.id).where(User.username == 'owner'))
        job = Job(owner_id=owner, kind='source.collect', payload={'source_id': str(source_id('direction_em_hot'))})
        db.add(job)
        db.flush()
        ident = job.id
    schedule_sources()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.get(Job, ident).payload == {'source_id': str(source_id('direction_em_hot'))}
    monkeypatch.setattr('app.market.service.engine', lambda: pytest.fail('Ordinary tasks must immediately bypass probe guard'))
    assert permit_direction_probe(SimpleNamespace(payload={'source_id': 'ordinary'})) is True
