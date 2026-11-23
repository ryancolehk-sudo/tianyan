"""Auction API on disposable PostgreSQL: pairing, cache retention, revocation and day switch."""
import uuid
import pytest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.auction import parse_auction
from app.market.collect import store_reference_batch
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import InstrumentKey
from app.market.service import source_id
from app.models import Provider
from test_data_sources import configure, login

FIXTURES = Path(__file__).parent / 'fixtures/auction'
SELECTION = [InstrumentKey('SSE', '600000', 'stock'), InstrumentKey('SZSE', '000001', 'stock')]



@pytest.fixture(autouse=True)
def auction_read_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 10, 21, 1, 26, tzinfo=UTC)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr('app.market.auction_read.datetime', Clock)


def body(name):
    return (FIXTURES / name).read_bytes()


def auction_configuration(family='eastmoney_auction'):
    return {'family': family,
        'symbols': [{'exchange': 'SSE', 'symbol': '600000', 'asset_type': 'stock'},
                    {'exchange': 'SZSE', 'symbol': '000001', 'asset_type': 'stock'}],
        'interval_seconds': 60,
        'rights_basis': 'Self-authored synthetic fixtures for this isolated test only; no upstream grant.',
        'rights': {'display': 'allowed', 'cache': 'allowed', 'export': 'unknown', 'redistribute': 'unknown'},
        'entitlement_status': 'verified', 'enabled': True}


def store(client, headers, received, family='eastmoney_auction', fixture='eastmoney_0920.json', records=None):
    configure(client, headers, auction_configuration(family), key=uuid.uuid4().hex)
    batch = parse_auction(body(fixture), received, SELECTION, family)
    if records is not None:
        batch.records = batch.records[:records]
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(family))
        config = db.get(SourceSettings, source_id(family))
        store_reference_batch(db, provider, config, batch, received)
    return batch


def previous_day_body():
    import json
    payload = json.loads(body('eastmoney_0920.json'))
    for row in payload['data']['diff']:
        row['f124'] = row['f124'] - 86400
    return json.dumps(payload).encode()


def view(client, headers, **params):
    query = '&'.join(f'{key}={value}' for key, value in params.items())
    response = client.get('/api/v1/market-auction' + ('?' + query if query else ''), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()['data']


def test_first_read_has_no_batches_and_no_invented_zeroes(client):
    headers = login(client)
    result = view(client, headers)
    assert result['items'] == []
    assert {row['family'] for row in result['sources']} == {'eastmoney_auction', 'tencent_auction'}
    assert all(row['state'] == 'empty' for row in result['sources'])


def test_successful_batch_pairs_summary_and_detail(client):
    headers = login(client)
    received = datetime(2026, 10, 21, 1, 20, 40, tzinfo=UTC)
    batch = store(client, headers, received)
    result = view(client, headers)
    assert len(result['items']) == 1
    item = result['items'][0]
    assert item['batch_hash'] == batch.batch_hash
    assert item['state'] == 'available'
    assert item['checkpoint'] == '09:20' and item['checkpoint_basis'] == 'source_time'
    assert item['summary']['quoted_count'] == 2 and item['summary']['total_amount_cny'] == '61675840'
    assert [row['code'] for row in item['detail']] == ['000001', '600000']
    assert item['detail'][1]['volume_shares'] == '3812000'
    assert item['age_seconds'] >= 0
    # A stored batch without any run record is readable, not reported as "no data".
    assert next(row for row in result['sources'] if row['family'] == 'eastmoney_auction')['state'] == 'available'


def test_failed_run_keeps_the_previous_batch_as_stale(client):
    headers = login(client)
    received = datetime(2026, 10, 21, 1, 20, 40, tzinfo=UTC)
    store(client, headers, received)
    with Session(engine()) as db, db.begin():
        db.add(DataFetchRun(source_id=source_id('eastmoney_auction'), capability='market.auction.summary',
            started_at=received + timedelta(minutes=5), received_at=received + timedelta(minutes=5),
            status='error', reason='timeout', expected=3, accepted=None, rejected=None, duplicates=None,
            elapsed_ms=None, request_count=1, cost_cny=None, body_hash=None))
    result = view(client, headers)
    assert len(result['items']) == 1
    assert result['items'][0]['state'] == 'stale'
    assert 'timeout' in result['items'][0]['note']
    assert result['sources'][0]['state'] == 'error'


def test_revoked_display_hides_history_and_says_so(client):
    headers = login(client)
    store(client, headers, datetime(2026, 10, 21, 1, 20, 40, tzinfo=UTC))
    assert view(client, headers)['items']
    with Session(engine()) as db, db.begin():
        db.execute(update(Provider).where(Provider.id == source_id('eastmoney_auction'))
                   .values(rights={'display': 'denied', 'cache': 'allowed', 'export': 'unknown', 'redistribute': 'unknown'}))
    result = view(client, headers)
    assert result['items'] == []
    state = next(row for row in result['sources'] if row['family'] == 'eastmoney_auction')
    assert state['state'] == 'permission_missing'
    with Session(engine()) as db:
        assert db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id == source_id('eastmoney_auction')).limit(1)) is not None


def test_trade_day_filter_does_not_carry_another_day(client):
    headers = login(client)
    configure(client, headers, auction_configuration(), key=uuid.uuid4().hex)
    received = datetime(2026, 10, 20, 1, 20, 40, tzinfo=UTC)
    batch = parse_auction(previous_day_body(), received, SELECTION, 'eastmoney_auction')
    with Session(engine()) as db, db.begin():
        store_reference_batch(db, db.get(Provider, source_id('eastmoney_auction')),
                              db.get(SourceSettings, source_id('eastmoney_auction')), batch, received)
    assert view(client, headers, trade_day='2026-10-20')['items']
    assert view(client, headers, trade_day='2026-10-21')['items'] == []


def test_missing_detail_table_is_reported_unpaired(client):
    headers = login(client)
    # Evidence is immutable: simulate a summary-only write instead of deleting a row.
    store(client, headers, datetime(2026, 10, 21, 1, 20, 40, tzinfo=UTC), records=1)
    item = view(client, headers)['items'][0]
    assert item['state'] == 'unpaired' and item['detail'] == []
    assert item['summary'] is not None


def test_tencent_batch_is_read_under_its_own_family(client):
    headers = login(client)
    received = datetime(2026, 10, 21, 1, 25, 20, tzinfo=UTC)
    store(client, headers, received, family='tencent_auction', fixture='tencent_0925.txt')
    result = view(client, headers, family='tencent_auction')
    assert len(result['items']) == 1
    assert result['items'][0]['family'] == 'tencent_auction'
    assert result['items'][0]['checkpoint'] == '09:25'
    assert view(client, headers, family='eastmoney_auction')['items'] == []
