"""Read compatibility only; never enables collection or changes stored hashes."""
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.common import content_hash, normalized
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import BarPayload
from app.market.collect import store_minute_batch, store_observation, text_decimal
from app.market.minute import parse_sina_minutes
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import InstrumentKey
from app.market.service import source_id, source_states
from app.models import Instrument, Job, Provider
from test_data_sources import configure, login
from test_minute_collection import configuration, minute_body

FAMILY = 'sina_minute_1m'
DAY = date(2026, 10, 23)
CLOCK = datetime(2026, 10, 24, 2, tzinfo=UTC)


def setup_source(client):
    headers = login(client)
    configure(client, headers, configuration(), key='compat-minute-source')
    with Session(engine()) as db, db.begin():
        stock = Instrument(exchange='SSE', symbol='603278', asset_type='stock', name='合成兼容验证股票')
        db.add(stock)
        db.flush()
        ident = stock.id
    return headers, ident


def save_bar(db, instrument, day, *, backfill=False, received=CLOCK-timedelta(minutes=5)):
    provider = db.get(Provider, source_id(FAMILY))
    config = db.get(SourceSettings, provider.id)
    body = {**payload(), 'trade_day': str(day), 'source_clock_at': str(day)+'T01:31:00Z'}
    if backfill:
        body['arrival_kind'] = 'backfill'
    source_key = 'compat-minute:' + str(instrument) + ':' + str(day)
    assert store_observation(db, provider, config, source_key=source_key, kind='bar', payload=body,
        received_at=received, event_at=datetime.fromisoformat(body['source_clock_at']),
        instrument_id=instrument, eligible=False, repeated_state=True)
    return db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id == provider.id,
        EvidenceVersion.source_key == source_key))


def save_run(db, day, received, *, backfill=True):
    config = db.get(SourceSettings, source_id(FAMILY))
    body = {'source_id': str(config.source_id)}
    if backfill:
        provider = db.get(Provider, config.source_id)
        fingerprint = content_hash(normalized(dict(source_id=provider.id, family=provider.family,
            adapter_version=provider.adapter_version, capabilities=provider.capabilities,
            configured=provider.configured, enabled=provider.enabled, rights=provider.rights,
            entitlement=provider.entitlement_status, owner_id=config.owner_id,
            rights_basis=config.rights_basis, interval_seconds=config.interval_seconds,
            configured_at=config.configured_at, config=config.config, paused_reason=config.paused_reason)))
        body['minute_context'] = {
            'requested_day': str(day), 'acquisition_mode': 'backfill', 'missing_only': True,
            'instrument': {'exchange': 'SSE', 'symbol': '603278', 'asset_type': 'stock'},
            'interval': '1m', 'adjustment': 'unadjusted', 'configuration_sha256': fingerprint,
        }
    job = Job(owner_id=config.owner_id, kind='source.collect', state='succeeded', payload=body,
        completed_at=received)
    db.add(job)
    db.flush()
    run = DataFetchRun(source_id=config.source_id, capability='bar.minute+bar.1m', job_id=job.id,
        started_at=received-timedelta(seconds=1), received_at=received, status='complete', reason=None,
        expected=1, accepted=1, rejected=0, duplicates=0, elapsed_ms=1, request_count=1,
        cost_cny=None, body_hash=None)
    db.add(run)
    db.flush()
    return run


def payload():
    return dict(open='10', high='12', low='9', close='11', volume_shares=None,
        interval='1m', bar_end_at=None, source_clock_at='2026-10-23T01:31:00Z',
        trade_day='2026-10-23', adjustment='unadjusted', clock_semantics='unknown')


def test_old_immutable_bar_keeps_original_shape_without_arrival_field():
    original=payload()
    parsed=BarPayload.model_validate(original)
    assert 'arrival_kind' not in parsed.model_fields_set
    assert normalized(parsed)==original
    assert content_hash(parsed)==content_hash(original)


def test_backfilled_bar_remains_readable_with_original_shape_hash_and_unknown_finality():
    original={**payload(),'arrival_kind':'backfill'}
    parsed=BarPayload.model_validate(original)
    assert parsed.arrival_kind=='backfill'
    assert parsed.bar_end_at is None and parsed.clock_semantics=='unknown'
    assert normalized(parsed)==original
    assert content_hash(parsed)==content_hash(original)


@pytest.mark.parametrize('backfill', [False, True])
def test_compat_api_reads_both_shapes_without_mutating_immutable_evidence(client, monkeypatch, backfill):
    _, instrument = setup_source(client)
    monkeypatch.setattr('app.market.minute_read.now', lambda db: CLOCK)
    with Session(engine()) as db, db.begin():
        row = save_bar(db, instrument, DAY, backfill=backfill)
        before = (row.id, row.content_hash, row.payload.copy(), row.received_at,
                  row.version_number, row.historical_eligible, row.available_at)
    result = client.get('/api/v1/market-bars', params={
        'instrument_id': str(instrument), 'interval': '1m', 'day': str(DAY)})
    assert result.status_code == 200, result.text
    view = result.json()['data']
    assert len(view['items']) == 1 and view['finality'] == 'unknown'
    item = view['items'][0]
    assert item['id'] == str(before[0]) and item['content_hash'] == before[1]
    assert item['payload'] == before[2]
    assert item['arrival_kind'] == ('backfill' if backfill else 'late_observed')
    assert item['period_state'] == 'unknown'
    assert view['historical_reconstruction'] is False
    with Session(engine()) as db:
        row = db.get(EvidenceVersion, before[0])
        assert (row.id, row.content_hash, row.payload, row.received_at, row.version_number,
                row.historical_eligible, row.available_at) == before
        assert row.historical_eligible is False and row.available_at is None
        assert db.scalar(select(func.count()).select_from(EvidenceVersion)
            .where(EvidenceVersion.kind == 'bar')) == 1


def test_compat_ordinary_source_health_does_not_take_newer_backfill_run(client):
    setup_source(client)
    observed_at = CLOCK-timedelta(hours=1)
    with Session(engine()) as db, db.begin():
        observed = save_run(db, CLOCK.date(), observed_at, backfill=False)
        observed_id = observed.id
        save_run(db, DAY, CLOCK-timedelta(minutes=1))
    with Session(engine()) as db:
        state = next(row for row in source_states(db) if row.family == FAMILY)
        assert state.last_attempt.id == observed_id
        assert state.last_success_at == observed_at
    result = client.get('/api/v1/data-sources')
    assert result.status_code == 200
    state = next(item for item in result.json()['data']['items'] if item['family'] == FAMILY)
    assert state['last_attempt']['id'] == str(observed_id)
    assert datetime.fromisoformat(state['last_success_at']) == observed_at


def test_compat_date_scoped_attempts_and_latest_saved_day_do_not_move_backwards(client, monkeypatch):
    _, instrument = setup_source(client)
    monkeypatch.setattr('app.market.minute_read.now', lambda db: CLOCK)
    previous = DAY-timedelta(days=1)
    with Session(engine()) as db, db.begin():
        save_bar(db, instrument, DAY, backfill=True, received=CLOCK-timedelta(minutes=3))
        save_bar(db, instrument, previous, backfill=True, received=CLOCK-timedelta(minutes=1))
        current_run = save_run(db, DAY, CLOCK-timedelta(minutes=3))
        current_id = current_run.id
        earlier_run = save_run(db, previous, CLOCK-timedelta(minutes=1))
        earlier_id = earlier_run.id
    def read(day=None):
        params = {'instrument_id': str(instrument), 'interval': '1m'}
        if day is not None:
            params['day'] = str(day)
        response = client.get('/api/v1/market-bars', params=params)
        assert response.status_code == 200, response.text
        return response.json()['data']
    latest = read()
    assert latest['observation_day'] == str(DAY)
    assert latest['last_attempt']['id'] == str(current_id)
    assert latest['items'][0]['payload']['trade_day'] == str(DAY)
    earlier = read(previous)
    assert earlier['last_attempt']['id'] == str(earlier_id)
    assert earlier['items'][0]['payload']['trade_day'] == str(previous)
    for other_day in (CLOCK.date(), previous-timedelta(days=1)):
        empty = read(other_day)
        assert empty['items'] == [] and empty['last_attempt'] is None
        assert empty['last_success_at'] is None
    state = next(item for item in client.get('/api/v1/data-sources').json()['data']['items']
                 if item['family'] == FAMILY)
    assert state['last_attempt'] is None and state['last_success_at'] is None


def test_compat_refresh_remains_observed_only_and_rejects_day_before_queue_or_http(client, monkeypatch):
    headers, _ = setup_source(client)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    calls = []
    monkeypatch.setattr('app.market.collect.fetch_https', lambda url: calls.append(url))
    route = f'/api/v1/data-sources/{source_id(FAMILY)}/refresh'
    refused = client.post(route, json={'day': str(DAY)},
        headers={**headers, 'Idempotency-Key': uuid4().hex})
    assert refused.status_code == 422
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 0
    queued = client.post(route, json={}, headers={**headers, 'Idempotency-Key': uuid4().hex})
    assert queued.status_code == 202, queued.text
    with Session(engine()) as db:
        jobs = list(db.scalars(select(Job).where(Job.kind == 'source.collect')))
        assert len(jobs) == 1
        assert jobs[0].payload == {'source_id': str(source_id(FAMILY))}
    assert calls == []


def test_compat_normal_observation_dedupes_backfill_tag_but_preserves_a_b_a_versions(client):
    _, instrument = setup_source(client)
    key = InstrumentKey(exchange='SSE', symbol='603278', asset_type='stock')
    received = CLOCK-timedelta(minutes=5)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(FAMILY))
        config = db.get(SourceSettings, provider.id)
        original = parse_sina_minutes(minute_body(DAY), key, '1m', received, DAY)
        assert len(original.records) == 2
        # Reproduce only the already-persisted new-format evidence. The rollback
        # collector itself remains observed-only and cannot enqueue a backfill.
        for observation in original.records:
            body = BarPayload(open=text_decimal(observation.open), high=text_decimal(observation.high),
                low=text_decimal(observation.low), close=text_decimal(observation.close),
                volume_shares=None, amount_cny=None, volume_native=text_decimal(observation.volume_native),
                amount_native=text_decimal(observation.amount_native), interval=observation.interval,
                bar_end_at=None, source_clock_at=observation.source_time, clock_semantics='unknown',
                trade_day=DAY, price_unit='CNY', adjustment='unadjusted', arrival_kind='backfill')
            source_key = (f'minute:{key.identity}:1m:unadjusted:'
                          + observation.source_time.astimezone(UTC).isoformat())
            assert store_observation(db, provider, config, source_key=source_key, kind='bar',
                payload=body, received_at=received, event_at=observation.source_time,
                instrument_id=instrument, eligible=False, repeated_state=True)
        rows = list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.kind == 'bar')))
        originals = {row.id: (row.content_hash, row.payload.copy(), row.version_number) for row in rows}
        assert store_minute_batch(db, provider, config, original, received+timedelta(seconds=1)) == (0, 2)
        unchanged = list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.kind == 'bar')))
        assert {row.id: (row.content_hash, row.payload, row.version_number) for row in unchanged} == originals
        changed = parse_sina_minutes(minute_body(DAY, close='10.30'), key, '1m', received, DAY)
        assert store_minute_batch(db, provider, config, changed, received+timedelta(seconds=2)) == (2, 0)
        assert store_minute_batch(db, provider, config, original, received+timedelta(seconds=3)) == (2, 0)
        versions = list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.kind == 'bar')
            .order_by(EvidenceVersion.source_key, EvidenceVersion.version_number)))
        assert len(versions) == 6
        for source_key in {row.source_key for row in versions}:
            series = [row for row in versions if row.source_key == source_key]
            assert [row.version_number for row in series] == [1, 2, 3]
            assert [row.payload['close'] for row in series] == ['10.20', '10.30', '10.20']
            assert (series[0].content_hash, series[0].payload, series[0].version_number) == originals[series[0].id]
            assert series[0].payload['arrival_kind'] == 'backfill'
            assert 'arrival_kind' not in series[1].payload and 'arrival_kind' not in series[2].payload
            assert series[2].supersedes_id == series[1].id
