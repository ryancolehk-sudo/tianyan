"""Synthetic minute collection; real PostgreSQL, no upstream calls or grants."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.budget import SINA_ACTION, SINA_DAILY_LIMIT, reserve_sina_request
from app.market.collect import store_minute_batch
from app.market.minute import FAMILY_INTERVALS, MINIMUM_INTERVALS, parse_sina_minutes
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import InstrumentKey, Response, SHANGHAI, SourceFailure
from app.market.service import enqueue_collection, schedule_sources, source_id
from app.models import AuditEvent, Job, Provider
from app.worker import claim, process
from test_data_sources import configure, login

FAMILY = 'sina_minute_1m'
STOCK = {'exchange':'SSE', 'symbol':'603278', 'asset_type':'stock'}


def at(text):
    return datetime.fromisoformat(text + '+08:00')


def controlled_response_clock(monkeypatch):
    """Freeze this synthetic request's start just before its fixed receipt.

    The collector still enforces received >= started. Database/lease clocks stay
    real, so this does not weaken or bypass the worker's lease expiry checks.
    """
    received = at('2026-10-23T16:00:00')
    class RequestClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = received - timedelta(milliseconds=5)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
    monkeypatch.setattr('app.market.collect.datetime', RequestClock)
    return received


def configuration(family=FAMILY):
    return dict(family=family, symbols=[STOCK], interval_seconds=MINIMUM_INTERVALS[family],
        entitlement_status='verified', enabled=True,
        rights={'display':'allowed','cache':'allowed','export':'unknown','redistribute':'unknown'},
        rights_basis='Self-authored synthetic research test only; no upstream data permission.')


def minute_body(day, close='10.20', duplicate=False, invalid=False):
    row = dict(day=str(day)+' 09:31:00', open='10.00', high='11.00', low='9.00', close=close, volume='123.00')
    rows = [row, dict(row, day=str(day)+' 09:32:00')]
    if duplicate:
        rows.append(row.copy())
    if invalid:
        rows.append(dict(row, day=str(day)+' 09:33:00', close='99'))
    return ('=(' + json.dumps(rows) + ');').encode()


def running(client, monkeypatch, family=FAMILY):
    headers = login(client)
    configure(client, headers, configuration(family))
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        job = enqueue_collection(db, source_id(family))
    job = claim()
    assert job is not None
    return job, headers


@pytest.mark.parametrize('family', list(FAMILY_INTERVALS))
def test_five_sources_require_one_supported_stock_and_correct_period_budget(client, family):
    headers = login(client)
    for symbols in ([], [STOCK, dict(STOCK, symbol='600000')], [dict(STOCK, exchange='BSE', symbol='920001')],
                    [dict(STOCK, symbol='000001', asset_type='index')]):
        body = configuration(family); body['symbols'] = symbols
        response = client.post('/api/v1/data-sources', json=body,
            headers={**headers, 'Idempotency-Key':uuid.uuid4().hex})
        assert response.status_code == 422
    body = configuration(family); body['interval_seconds'] -= 1
    response = client.post('/api/v1/data-sources', json=body, headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
    assert response.status_code == 422
    configure(client, headers, configuration(family))
    item = next(x for x in client.get('/api/v1/data-sources').json()['data']['items'] if x['family']==family)
    assert item['provider']['capabilities'] == ['bar.minute', 'bar.'+FAMILY_INTERVALS[family]]
    assert item['provider']['rights']['export'] == 'unknown'


@pytest.mark.parametrize('right', ['export','redistribute'])
def test_personal_connector_cannot_grant_export_or_redistribution(client, right):
    headers = login(client); body = configuration(); body['rights'][right] = 'allowed'
    result = client.post('/api/v1/data-sources', json=body, headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
    assert result.status_code == 422 and result.json()['error']['code'] == 'permission_missing'


def test_registration_not_automatic_and_unknown_rights_do_not_enable(client):
    headers = login(client)
    index = client.get('/api/v1/data-sources').json()['data']
    assert not index['items']
    assert set(FAMILY_INTERVALS) <= {a['family'] for a in index['available_adapters']}
    body = configuration(); body['entitlement_status'] = 'unknown'
    result = client.post('/api/v1/data-sources', json=body, headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
    assert result.status_code == 422
    body['enabled'] = False
    configure(client, headers, body)
    assert client.get('/api/v1/data-sources').json()['data']['items'][0]['collection_state'] == 'disabled'


def test_one_fixed_get_stores_per_clock_native_unknown_units_and_duplicates(client, monkeypatch):
    job, _ = running(client, monkeypatch)
    received = controlled_response_clock(monkeypatch)
    calls=[]
    def fetch(url):
        calls.append(url)
        return Response(minute_body(received.date(), duplicate=True), received, 3, 200)
    monkeypatch.setattr('app.market.collect.fetch_https', fetch)
    process(job)
    assert len(calls) == 1 and calls[0].startswith('https://quotes.sina.cn/')
    assert 'symbol=sh603278' in calls[0] and 'scale=1' in calls[0]
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun))
        assert run.started_at <= run.received_at == received
        assert (run.status,run.expected,run.accepted,run.rejected,run.duplicates,run.request_count) == ('complete',2,2,0,1,1)
        rows = list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id(FAMILY)).order_by(EvidenceVersion.source_key)))
        assert len(rows) == 2 and rows[0].source_key != rows[1].source_key
        for row in rows:
            p=row.payload
            assert p['bar_end_at'] is None and p['clock_semantics']=='unknown'
            assert p['volume_shares'] is None and p['amount_cny'] is None and p['volume_native']=='123.00'
            assert p['source_clock_at'] and p['trade_day']==str(received.date())
            assert row.received_at == received and not row.historical_eligible and row.available_at is None
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==SINA_ACTION))==1


def test_adjacent_duplicates_dedupe_but_a_b_a_preserves_received_versions(client):
    headers=login(client);configure(client,headers,configuration())
    clock=at('2026-10-23T10:00:00'); instrument=InstrumentKey(**STOCK)
    with Session(engine()) as db, db.begin():
        provider=db.get(Provider,source_id(FAMILY)); config=db.get(SourceSettings,provider.id)
        for index,(close,expected) in enumerate([('10.20',(2,0)),('10.20',(0,2)),('10.30',(2,0)),('10.20',(2,0))]):
            received=clock+timedelta(minutes=index)
            batch=parse_sina_minutes(minute_body(clock.date(),close),instrument,'1m',received,clock.date())
            assert store_minute_batch(db,provider,config,batch,received)==expected
        versions=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id).order_by(EvidenceVersion.version_number)))
        assert len(versions)==6
        assert sorted(v.version_number for v in versions)==[1,1,2,2,3,3]
        for key in {v.source_key for v in versions}:
            rows=sorted((v for v in versions if v.source_key==key),key=lambda v:v.version_number)
            assert [v.payload['close'] for v in rows]==['10.20','10.30','10.20']
            assert rows[0].received_at < rows[1].received_at < rows[2].received_at
            assert rows[2].supersedes_id == rows[1].id


@pytest.mark.parametrize('invalid,status,accepted,rejected', [(True,'partial',2,1),(False,'complete',2,0)])
def test_bad_slot_is_partial_but_normal_history_and_duplicates_are_not(client,monkeypatch,invalid,status,accepted,rejected):
    job,_=running(client,monkeypatch)
    received=controlled_response_clock(monkeypatch)
    monkeypatch.setattr('app.market.collect.fetch_https',lambda url:Response(minute_body(received.date(),invalid=invalid),received,2,200))
    process(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun))
        assert run.started_at <= run.received_at == received
        assert (run.status,run.accepted,run.rejected)==(status,accepted,rejected)
        assert run.expected == accepted+rejected


def test_rights_revoked_during_request_discards_body_but_preserves_run(client,monkeypatch):
    job,_=running(client,monkeypatch)
    received=controlled_response_clock(monkeypatch)
    def fetch(url):
        with Session(engine()) as db,db.begin():
            provider=db.get(Provider,source_id(FAMILY));provider.rights={**provider.rights,'display':'denied'}
        return Response(minute_body(received.date()),received,1,200)
    monkeypatch.setattr('app.market.collect.fetch_https',fetch)
    process(job)
    with Session(engine()) as db:
        assert not db.scalar(select(EvidenceVersion.id).where(EvidenceVersion.kind=='bar'))
        run=db.scalar(select(DataFetchRun))
        assert run.started_at <= run.received_at == received
        assert run.reason=='configuration_changed'


@pytest.mark.parametrize('stamp,expected', [('2026-10-23T09:00:00','2026-10-23T09:15:00'),
    ('2026-10-23T15:05:00','2026-10-24T09:15:00'),('2026-10-23T18:00:00','2026-10-24T09:15:00')])
def test_automatic_off_window_has_no_jobs_or_permits(client,monkeypatch,stamp,expected):
    headers=login(client);configure(client,headers,configuration())
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    clock=at(stamp);monkeypatch.setattr('app.market.service.now',lambda db:clock)
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(FAMILY)).next_run_at=clock-timedelta(seconds=1)
    schedule_sources()
    with Session(engine()) as db:
        assert db.get(SourceSettings,source_id(FAMILY)).next_run_at==at(expected)
        assert not db.scalar(select(Job.id).where(Job.kind=='source.collect'))
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action==SINA_ACTION))


@pytest.mark.parametrize('stamp,collect', [('2026-10-23T11:30:00', True),
    ('2026-10-23T11:31:00', False), ('2026-10-23T11:40:00', False), ('2026-10-23T15:01:00', True)])
def test_minute_polling_respects_quiet_lunch_and_close_receipt_window(client,monkeypatch,stamp,collect):
    headers=login(client);configure(client,headers,configuration())
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    clock=at(stamp)
    monkeypatch.setattr('app.market.service.now',lambda db:clock)
    monkeypatch.setattr('app.market.budget.now',lambda db:clock)
    monkeypatch.setattr('app.market.mairui.pool_day',lambda db,at,**kw:clock.date())
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(FAMILY)).next_run_at=clock-timedelta(seconds=1)
    schedule_sources()
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=db.scalar(select(Job).where(Job.kind=='source.collect'))
        if not collect:
            assert job is None
            assert db.get(SourceSettings,source_id(FAMILY)).next_run_at == clock.replace(hour=13,minute=0,second=0)
            assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action==SINA_ACTION))
            assert not db.scalar(select(DataFetchRun.id))
            return
        assert job and job.payload['scheduled_minute'] is True
        job.state='running';job.attempts=1;job.lease_token=uuid.uuid4()
        job.lease_until=max(datetime.now(UTC),clock)+timedelta(minutes=5)
    assert reserve_sina_request(job) is True
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==SINA_ACTION))==1
        assert db.scalar(select(DataFetchRun.id)) is None  # No invented minute bars or HTTP receipt.


def test_calendar_required_and_delayed_job_rechecks_before_http(client,monkeypatch):
    headers=login(client);configure(client,headers,configuration())
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    clock=at('2026-10-23T10:00:00');monkeypatch.setattr('app.market.service.now',lambda db:clock)
    monkeypatch.setattr('app.market.mairui.pool_day',lambda db,clock:clock.date()-timedelta(days=1))
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(FAMILY)).next_run_at=clock-timedelta(seconds=1)
    schedule_sources()
    with Session(engine()) as db:
        assert not db.scalar(select(Job.id).where(Job.kind=='source.collect'))
    monkeypatch.setattr('app.market.mairui.pool_day',lambda db,clock:clock.astimezone(SHANGHAI).date())
    with Session(engine()) as db,db.begin():
        db.get(SourceSettings,source_id(FAMILY)).next_run_at=clock-timedelta(seconds=1)
    schedule_sources()
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        job=db.scalar(select(Job).where(Job.kind=='source.collect'))
        assert job.payload['scheduled_minute'] is True
        job.state='running';job.attempts=1;job.lease_token=uuid.uuid4();job.lease_until=datetime.now(UTC)+timedelta(days=1)
    monkeypatch.setattr('app.market.budget.now',lambda db:at('2026-10-23T15:05:00'))
    assert reserve_sina_request(job) is False
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.available_at==at('2026-10-24T09:15:00') and saved.attempts==0
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action==SINA_ACTION))


def test_shared_budget_survives_new_sessions_and_other_period(client,monkeypatch):
    first,_=running(client,monkeypatch)
    clock=datetime.now(UTC);monkeypatch.setattr('app.market.budget.now',lambda db:clock)
    assert reserve_sina_request(first) is True
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        provider=db.get(Provider,source_id(FAMILY))
        other=Provider(id=source_id('sina_minute_5m'),family='sina_minute_5m',name='synthetic',kind='market',
            configured=True,enabled=True,entitlement_status='verified',rights=provider.rights,capabilities=['bar.minute','bar.5m'])
        db.add(other);db.flush()
        old=db.get(SourceSettings,provider.id)
        db.add(SourceSettings(source_id=other.id,owner_id=old.owner_id,rights_basis=old.rights_basis,config=old.config,
            interval_seconds=300,configured_at=clock,next_run_at=clock))
        second=Job(owner_id=old.owner_id,kind='source.collect',payload={'source_id':str(other.id)},state='running',
            lease_token=uuid.uuid4(),lease_until=clock+timedelta(hours=1),attempts=1)
        db.add(second);db.flush()
    assert reserve_sina_request(second) is False
    with Session(engine()) as db:
        saved=db.get(Job,second.id)
        assert saved.state=='queued' and saved.attempts==0 and saved.available_at==clock+timedelta(seconds=15)
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action==SINA_ACTION))==1


def test_daily_cap_defers_without_fake_http_run(client,monkeypatch):
    job,_=running(client,monkeypatch)
    clock=datetime.now(UTC)
    with Session(engine()) as db,db.begin():
        for _ in range(SINA_DAILY_LIMIT):
            db.add(AuditEvent(actor_id=job.owner_id,action=SINA_ACTION,created_at=clock,changed_field_names=['synthetic']))
    monkeypatch.setattr('app.market.budget.now',lambda db:clock)
    assert reserve_sina_request(job) is False
    with Session(engine()) as db:
        saved=db.get(Job,job.id)
        assert saved.state=='queued' and saved.available_at.astimezone(SHANGHAI).date()==clock.astimezone(SHANGHAI).date()+timedelta(days=1)
        assert not db.scalar(select(DataFetchRun.id))


def test_upstream_429_sets_supplier_cooldown_and_no_evidence(client,monkeypatch):
    job,_=running(client,monkeypatch)
    def limited(url):
        raise SourceFailure('rate_limited',retry_after=1)
    monkeypatch.setattr('app.market.collect.fetch_https',limited)
    process(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun))
        assert run.reason=='rate_limited' and (run.retry_not_before-run.received_at).total_seconds()>=300
        assert run.request_count==1
        assert not db.scalar(select(EvidenceVersion.id).where(EvidenceVersion.kind=='bar'))
        from app.market.budget import sina_cooldown_until
        assert sina_cooldown_until(db,run.received_at)==run.retry_not_before


def test_manual_off_window_uses_actual_spacing_not_next_automatic_wake(client,monkeypatch):
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    headers=login(client);configure(client,headers,configuration())
    clock=at('2026-10-23T18:00:00');monkeypatch.setattr('app.market.service.now',lambda db:clock)
    with Session(engine()) as db,db.begin():
        config=db.get(SourceSettings,source_id(FAMILY));config.next_run_at=at('2026-10-24T09:30:00')
        db.add(DataFetchRun(source_id=config.source_id,capability='bar.minute',started_at=clock-timedelta(seconds=61),
            received_at=clock-timedelta(seconds=61),status='complete',request_count=1))
    with Session(engine()) as db,db.begin():
        job=enqueue_collection(db,source_id(FAMILY))
        assert job.payload=={'source_id':str(source_id(FAMILY))}
