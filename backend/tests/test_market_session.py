"""Real PostgreSQL, synthetic clocks/calendars; no market network calls."""
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.market.collect import store_calendar_batch
from app.market.models import SourceSettings
from app.market.service import source_id
from app.market.session import phase_at, quote_fresh_at
from app.models import Provider
from test_terminal_workbench import AT, calendar, save, setup
from test_tushare_market import quote


def clock(value):
    return datetime.fromisoformat(value + '+08:00')


@pytest.mark.parametrize('value,phase', [
    ('09:14:59', 'preopen'), ('09:15:00', 'auction'), ('09:29:59', 'auction'),
    ('09:30:00', 'trading'), ('11:29:59', 'trading'), ('11:30:00', 'break'),
    ('12:59:59', 'break'), ('13:00:00', 'trading'), ('14:59:59', 'trading'),
    ('15:00:00', 'closed'), ('15:01:00', 'closed'),
])
def test_shanghai_phase_boundaries_are_independent_of_host_zone(value, phase):
    local = clock('2026-10-23T' + value)
    assert phase_at(local, True)[0] == phase
    assert phase_at(local.astimezone(UTC), True) == phase_at(local, True)
    assert phase_at(local, None) == ('unknown', None)
    assert phase_at(local, False)[0] == 'closed'


def test_session_authenticated_unknown_calendar_does_not_guess_weekday(client, monkeypatch):
    assert client.get('/api/v1/market-session').status_code == 401
    setup(client, monkeypatch, [])
    monkeypatch.setattr('app.market.session.now', lambda db: AT)
    result = client.get('/api/v1/market-session')
    assert result.status_code == 200
    assert result.headers['cache-control'] == 'no-store'
    data = result.json()['data']
    assert data['phase'] == 'unknown' and data['is_trading_day'] is None
    assert data['automatic_live'] is False


def test_lunch_preserves_today_and_morning_feed_but_afternoon_requires_new_quote(client, monkeypatch):
    setup(client, monkeypatch, ['tushare_calendar_sse', 'tushare_calendar_szse', 'tushare_market_rt'])
    morning = clock('2026-10-23T11:30:00')
    with Session(engine()) as db, db.begin():
        calendar(db)
        save(db, 'tushare_market_rt', [quote(at=morning)], morning + timedelta(seconds=10))
    lunch = clock('2026-10-23T12:30:00')
    monkeypatch.setattr('app.market.terminal_read.now', lambda db: lunch)
    monkeypatch.setattr('app.market.session.now', lambda db: lunch)
    data = client.get('/api/v1/workbench-view').json()['data']
    assert data['trade_day'] == '2026-10-23' and data['mode'] == 'live'
    assert data['market_session']['phase'] == 'break'
    assert data['market_session']['automatic_live'] is True
    assert next(item for item in data['capabilities'] if item['key'] == 'quotes')['status'] != 'stale'
    afternoon = clock('2026-10-23T13:05:00')
    monkeypatch.setattr('app.market.terminal_read.now', lambda db: afternoon)
    data = client.get('/api/v1/workbench-view').json()['data']
    assert data['market_session']['phase'] == 'trading'
    assert next(item for item in data['capabilities'] if item['key'] == 'quotes')['status'] == 'stale'
    assert quote_fresh_at(afternoon) == afternoon


@pytest.mark.parametrize('opening',['09:00:00','09:30:00'])
def test_opening_auto_advances_all_four_views_without_new_receipt_and_keeps_explicit_history(client, monkeypatch, opening):
    setup(client, monkeypatch, ['tushare_calendar_sse', 'tushare_calendar_szse', 'tushare_market_rt'])
    monday = clock('2026-10-26T'+opening)
    with Session(engine()) as db, db.begin():
        calendar(db)
        for family, exchange in [('tushare_calendar_sse', 'SSE'), ('tushare_calendar_szse', 'SZSE')]:
            provider = db.get(Provider, source_id(family))
            store_calendar_batch(db, provider, db.get(SourceSettings, provider.id),
                [(date(2026, 10, 26), True)], AT, exchanges=(exchange,))
        save(db, 'tushare_market_rt', [quote()], AT)
    monkeypatch.setattr('app.market.terminal_read.now', lambda db: monday)
    for view in ('overview', 'auction', 'ladder', 'funds'):
        data = client.get('/api/v1/workbench-view?view=' + view).json()['data']
        assert data['trade_day'] == '2026-10-26'
        assert data['market_session']['previous_trade_day'] == '2026-10-23'
        assert data['coverage']['quote_count'] == 0
        assert all(item['price'] is None for item in data['stocks'])
        fixed = client.get('/api/v1/workbench-view?view=' + view + '&day=2026-10-23').json()['data']
        assert fixed['trade_day'] == '2026-10-23'
        assert fixed['coverage']['quote_count'] == 1
    observed = client.get('/api/v1/workbench-view?as_of_time=2026-10-23T10:31:00%2B08:00').json()['data']
    assert observed['trade_day'] == '2026-10-23' and observed['mode'] == 'observed'


def test_funds_current_day_uses_current_market_context_without_claiming_daily_funds(client, monkeypatch):
    setup(client, monkeypatch, ['tushare_market_rt', 'tushare_moneyflow_ind_dc'])
    current = AT + timedelta(days=3)
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_moneyflow_ind_dc', [{'trade_date': '20261023', 'ts_code': 'BK1184.DC',
            'name': '合成板块', 'net_amount': '100'}], AT + timedelta(hours=8))
        save(db, 'tushare_market_rt', [quote('000001.SZ', at=current)], current)
    monkeypatch.setattr('app.market.terminal_read.now', lambda db: current + timedelta(seconds=30))
    data = client.get('/api/v1/workbench-view?view=funds').json()['data']
    assert data['trade_day'] == '2026-10-26' and data['coverage']['quote_count'] == 1
    assert data['funds_trade_day'] is None
    assert data['intraday_funds']['state'] == 'missing'


def test_current_context_can_use_last_known_membership_with_original_date(client, monkeypatch):
    setup(client, monkeypatch, ['tushare_market_rt', 'tushare_dc_member'])
    current = AT + timedelta(days=3)
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_dc_member', [{'trade_date': '20261023', 'ts_code': 'BK1184.DC',
            'con_code': '000001.SZ', 'name': '合成成分'}], AT)
        save(db, 'tushare_market_rt', [quote('000001.SZ', at=current)], current)
    monkeypatch.setattr('app.market.terminal_read.now', lambda db: current + timedelta(seconds=30))
    data = client.get('/api/v1/workbench-view?sector=BK1184.DC').json()['data']
    assert data['trade_day'] == '2026-10-26' and data['members_trade_day'] == '2026-10-23'
    assert data['selected_members'][0]['source_time'].startswith('2026-10-26')
    fixed = client.get('/api/v1/workbench-view?day=2026-10-22&sector=BK1184.DC').json()['data']
    assert fixed['selected_members'] == [] and fixed['members_trade_day'] is None


def test_opening_minute_auto_waits_for_today_while_explicit_history_stays_readable(client, monkeypatch):
    from test_minute_read import setup as minute_setup, store, payload
    from test_data_sources import configure
    from test_tushare import source
    headers, ident, _ = minute_setup(client)
    for family in ('tushare_calendar_sse', 'tushare_calendar_szse'):
        configure(client, headers, source(family), key=family)
    monday = clock('2026-10-26T10:00:00')
    with Session(engine()) as db, db.begin():
        store(db, ident, payload())
        for family, exchange in [('tushare_calendar_sse', 'SSE'), ('tushare_calendar_szse', 'SZSE')]:
            provider = db.get(Provider, source_id(family))
            store_calendar_batch(db, provider, db.get(SourceSettings, provider.id),
                [(date(2026, 10, 26), True)], AT, exchanges=(exchange,))
    monkeypatch.setattr('app.market.minute_read.now', lambda db: monday)
    query = {'instrument_id': str(ident), 'interval': '1m'}
    value = client.get('/api/v1/market-bars', params=query).json()['data']
    assert value['observation_day'] == '2026-10-26' and value['items'] == []
    assert value['state'] == 'empty'
    history = client.get('/api/v1/market-bars', params={**query, 'day': '2026-10-23'}).json()['data']
    assert history['observation_day'] == '2026-10-23' and len(history['items']) == 1
    # A real new day's sample advances the same automatic query without
    # changing its date parameter or borrowing another period or provider.
    with Session(engine()) as db, db.begin():
        store(db, ident, {**payload('2026-10-26T09:31:00+08:00'), 'trade_day': '2026-10-26'}, received=monday)
    current = client.get('/api/v1/market-bars', params=query).json()['data']
    assert current['observation_day'] == '2026-10-26' and len(current['items']) == 1
