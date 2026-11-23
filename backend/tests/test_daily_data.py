"""Synthetic protocol and real PostgreSQL acceptance; no network data in Git."""
import calendar
import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cli import configure_free_data
from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.daily import parse_daily
from app.market.protocol import Response, SourceFailure
from app.market.service import source_id
from app.models import Provider
from app.worker import claim, process
from test_data_sources import configuration, configure, login


def sample():
    current = datetime.now(UTC).astimezone(ZoneInfo('Asia/Shanghai'))
    day = str(current.date() - timedelta(days=1))
    rows, names = [], {}
    for code in ('sh.000001', 'sz.399001', 'sz.399006', 'sh.600000', 'sz.000001'):
        rows.append(dict(date=day, code=code, open='12.0000000000', high='13.0000000000', low='11.0000000000',
            close='12.3000000000', preclose='12.0000000000', volume='12300', amount='151290.0000000000', adjustflag='3'))
        names[code] = '合成盘后标的' + code
    return {'trade_day': day, 'rows': rows, 'names': names, 'request_count': 9,
        'calendar': [{'calendar_date': f'{current:%Y-%m}-{n:02}', 'is_trading_day': '1'}
            for n in range(1, calendar.monthrange(current.year, current.month)[1]+1)]}


def test_daily_dates_units_and_same_symbol_identity():
    data = sample()
    batch = parse_daily(json.dumps(data).encode(), datetime.now(UTC))
    assert len(batch.records) == 5 and batch.expected == 6
    assert batch.rejected == ['index_not_returned']
    same_code = [r for r in batch.records if r.instrument.symbol == '000001']
    assert {r.instrument.identity for r in same_code} == {'SSE:index:000001', 'SZSE:stock:000001'}
    assert all(r.quote_time is None and str(r.trade_day) == data['trade_day'] for r in batch.records)
    assert all(r.volume_shares is None and r.amount_cny is None for r in batch.records if r.price_unit == 'point')
    stock = next(r for r in batch.records if r.instrument.symbol == '600000')
    assert stock.volume_shares == '12300' and stock.amount_cny == '151290' and stock.price == Decimal('12.3')


@pytest.mark.parametrize('change', ['incomplete_calendar', 'future_day', 'adjusted', 'duplicate', 'range', 'fractional_shares'])
def test_invalid_or_conflicting_daily_material_never_becomes_a_live_tick(change):
    data = sample()
    if change == 'incomplete_calendar':
        data['calendar'].pop()
    elif change == 'future_day':
        data['trade_day'] = '2999-01-01'
    elif change == 'adjusted':
        data['rows'][-1]['adjustflag'] = '2'
    elif change == 'duplicate':
        data['rows'].append(copy.deepcopy(data['rows'][-1]))
    elif change == 'range':
        data['rows'][-1]['low'] = '99'
    else:
        data['rows'][-1]['volume'] = '1.5'
    if change in {'incomplete_calendar', 'future_day'}:
        with pytest.raises(SourceFailure):
            parse_daily(json.dumps(data).encode(), datetime.now(UTC))
    else:
        batch = parse_daily(json.dumps(data).encode(), datetime.now(UTC))
        assert not any(r.instrument.exchange == 'SZSE' and r.instrument.symbol == '000001' for r in batch.records)
        assert batch.rejected


def test_daily_worker_persists_whole_snapshots_calendar_and_revocation(client, monkeypatch):
    headers = login(client)
    body = configuration('baostock_daily')
    body['interval_seconds'] = 21600
    configure(client, headers, body)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr('app.market.collect.fetch_daily', lambda: Response(json.dumps(sample()).encode(), datetime.now(UTC), 7, 200))
    response = client.post(f'/api/v1/data-sources/{source_id("baostock_daily")}/refresh', json={},
        headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
    assert response.status_code == 202
    process(claim())
    data = client.get('/api/v1/market-quotes').json()['data']
    assert [r['state'] for r in data['items']] == ['daily_close', 'daily_close', 'daily_close', 'empty']
    row = data['items'][0]
    assert row['quote']['quote_time'] is None and row['quote']['trade_day']
    assert row['provenance']['family'] == 'baostock_daily' and not row['provenance']['historical_eligible']
    past = client.get('/api/v1/market-quotes', params={'as_of_time': datetime.now(UTC).isoformat()}).json()['data']['items']
    assert all(r['quote'] is None for r in past)
    index = client.get('/api/v1/data-sources').json()['data']
    run = index['items'][0]['last_attempt']
    assert index['today_request_count'] == 9 and run['status'] == 'partial' and run['rejected'] == 1
    assert run['accepted'] == 5 + 2 * len(sample()['calendar']) and run['cost_cny'] is None
    with Session(engine()) as db:
        days = list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.kind == 'calendar')))
        assert {r.payload['exchange'] for r in days} == {'SSE', 'SZSE'}
    body['rights']['display'] = 'denied'; body['enabled'] = False
    configure(client, headers, body, key=uuid.uuid4().hex)
    assert all(r['quote'] is None for r in client.get('/api/v1/market-quotes').json()['data']['items'])


def test_free_operator_preset_is_local_admin_only_and_preserves_existing_configuration(client, monkeypatch):
    login(client)
    with pytest.raises(ValueError):
        configure_free_data('missing')
    monkeypatch.setattr(settings(), 'public_origin', 'https://example.org')
    with pytest.raises(ValueError):
        configure_free_data('owner')
    monkeypatch.setattr(settings(), 'public_origin', 'http://127.0.0.1:8080')
    assert all(r['result'] == 'configured_without_fetch' for r in configure_free_data('owner'))
    with Session(engine()) as db, db.begin():
        db.get(Provider, source_id('baostock_daily')).enabled = False
    assert all(r['result'] == 'existing_configuration_preserved' for r in configure_free_data('owner'))
    with Session(engine()) as db:
        assert not db.get(Provider, source_id('baostock_daily')).enabled
    assert client.get('/api/v1/data-sources').json()['data']['today_request_count'] == 0
