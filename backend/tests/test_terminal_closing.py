"""Closing archives replace whole-market values without inventing tick clocks."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.market import terminal_archives as archive, terminal_read
from app.market.collect import store_quote_batch
from app.market.models import SourceSettings
from app.market.protocol import InstrumentKey
from app.market.review_store import persist_pages
from app.market.service import source_id
from app.market.tushare import parse_quotes
from app.market.tushare_market import encoded
from app.models import Provider
from test_review_collection import page, setup_source
from test_terminal_archives import fake_reader
from test_terminal_workbench import AT, DAY, calendar, save, setup, view
from test_tushare_market import limits, quote


CLOSE_RECEIPT = datetime.fromisoformat('2026-10-23T16:05:00+08:00')


def daily(code='600000.SH', close='11', amount='10'):
    return dict(ts_code=code, trade_date='20261023', close=close, pre_close='10',
                open='10', high='12', low='9', vol='100', amount=amount)


def publish(rows, dataset='daily'):
    _, provider, config, job = setup_source(dataset, target=DAY)
    with Session(engine()) as db, db.begin():
        persist_pages(db, provider, config, job, [(page(rows, dataset=dataset), CLOSE_RECEIPT)])
    return provider


def test_closing_archive_replaces_whole_market_ranking_amount_and_breadth(client, monkeypatch):
    setup(client, monkeypatch, ['tushare_market_rt', 'tushare_market_limits', 'tushare_limit_step',
                              'tushare_calendar_sse', 'tushare_calendar_szse'])
    codes = [f'{600000 + index:06}.SH' for index in range(205)]
    with Session(engine()) as db, db.begin():
        calendar(db)
        save(db, 'tushare_market_limits', [limits(code) for code in codes], AT - timedelta(minutes=1))
        save(db, 'tushare_limit_step', [dict(ts_code=code, trade_date='20261022', name='合成', nums='2') for code in codes[:200]])
        save(db, 'tushare_market_rt', [quote(codes[0], price='9'), quote(codes[1], price='11')], AT)
        save(db, 'tushare_market_rt', [quote(codes[0], price='10', at=AT + timedelta(minutes=5)), quote(codes[1], price='10', at=AT + timedelta(minutes=5))], AT + timedelta(minutes=5))
    rows = [daily(code, close='10') for code in codes]
    rows[0]['close'] = '11'; rows[1]['close'] = '9'; rows[-1]['close'] = '12'
    provider = publish(rows)
    data = view(client)
    by_code = {stock['code']: stock for stock in data['stocks']}
    assert set(by_code) == set(codes)
    assert data['coverage']['quote_count'] == 205
    assert data['breadth']['basis'] == 'daily_close'
    assert (data['breadth']['up'], data['breadth']['down'], data['breadth']['flat']) == (2, 1, 202)
    assert (data['breadth']['limit_up'], data['breadth']['limit_down']) == (1, 1)
    assert data['breadth']['amount_cny'] == 2_050_000
    assert by_code[codes[0]]['amount_cny'] == 10_000
    assert by_code[codes[0]]['price'] == 11 and by_code[codes[0]]['pct_change'] == pytest.approx(10)
    assert all(stock['source_time'] is None and stock['speed_5m'] is None and stock['seal_amount_cny'] is None for stock in by_code.values())
    assert datetime.fromisoformat(by_code[codes[0]]['source_received_at']) == CLOSE_RECEIPT
    assert all(point['time'][11:16] < '15:00' for point in data['breadth']['points'])
    response = client.get('/api/v1/workbench-details?day=2026-10-23&codes=' + codes[0])
    assert response.status_code == 200, response.text
    assert response.json()['data']['stocks'][0]['price_basis'] == 'daily_close'
    assert response.json()['data']['revision'] == data['detail_revision']
    # A later archive may not rewrite a saved historical slice.
    before = view(client, params='&as_of_time=2026-10-23T15:30:00%2B08:00')
    assert before['breadth']['basis'] == 'intraday' and before['coverage']['quote_count'] == 2
    assert all(stock['price_basis'] != 'daily_close' for stock in before['stocks'])
    # Current revoked rights block the archive even if it was once received.
    with Session(engine()) as db, db.begin():
        db.get(Provider, provider.id).rights = {'display': 'unknown', 'cache': 'unknown'}
    revoked = view(client)
    assert revoked['breadth']['basis'] == 'intraday' and revoked['detail_revision'] != data['detail_revision']


def test_archive_only_day_and_index_closes_are_selected_without_fake_timestamps(client, monkeypatch):
    setup(client, monkeypatch, [])
    publish([daily()])
    publish([daily('000001.SH', close='12', amount='20'), daily('399001.SZ', close='9', amount='30')], 'indices')
    response = client.get('/api/v1/workbench-view?view=overview')
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['trade_day'] == DAY.isoformat() and data['stocks'][0]['price'] == 11
    index = {item['code']: item for item in data['indices']}
    assert index['000001.SH']['price'] == 12 and index['000001.SH']['amount_cny'] == 20_000
    assert all(item['price_basis'] == 'daily_close' and item['source_time'] is None for item in index.values())


def test_real_closing_index_quote_keeps_its_supplier_clock(client, monkeypatch):
    setup(client, monkeypatch, ['tushare_rt_indices'])
    stamp = datetime.fromisoformat('2026-10-23T15:00:00+08:00')
    row = quote('000001.SH', price='13', at=stamp)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('tushare_rt_indices'))
        batch = parse_quotes(encoded(list(row), [row]), [InstrumentKey('SSE', '000001', 'index')], stamp)
        store_quote_batch(db, provider, db.get(SourceSettings, provider.id), batch)
    publish([daily('000001.SH', close='12')], 'indices')
    index = view(client)['indices'][0]
    assert index['price'] == 13 and index['price_basis'] == 'intraday'
    assert datetime.fromisoformat(index['source_time']) == stamp


@pytest.mark.parametrize('changes', [
    {'state': 'missing'}, {'state': 'permission_denied'}, {'state': 'broken_lineage'},
    {'rows': []}, {'rows': [daily(), daily()]}, {'rows': [{**daily(), 'trade_date': '20261022'}]},
    {'received_at': '2026-10-23T14:59:59+08:00'}, {'received_at': '2026-10-23T19:00:00+08:00'},
])
def test_ineligible_closing_tables_never_replace_saved_quotes(monkeypatch, changes):
    reader = fake_reader(); reader.permitted = {'tushare_daily': object()}
    saved = dict(state='complete', rows=[daily()], received_at=CLOSE_RECEIPT.isoformat(), evidence_refs=[])
    saved.update(changes)
    monkeypatch.setattr(archive, 'load_dataset', lambda *a, **k: saved)
    assert archive.closing_table(reader, DAY) is None


def test_daily_amount_unknown_is_not_zero_and_exact_limit_equality_is_preserved():
    rows = [daily(close='10.999999'), daily('000100.SZ', close='9', amount=None)]
    result = archive.closing_counts({'rows': rows}, [limits('600000.SH'), limits('000100.SZ')], DAY)
    assert result['limit_up'] == 0 and result['limit_down'] == 1
    assert result['advancing'] == 1 and result['declining'] == 1
    assert result['total_amount_cny'] is None


@pytest.mark.parametrize('stamp', ['2026-10-23T14:59:59+08:00', '2026-10-22T18:00:00+08:00'])
def test_closing_table_never_loads_before_the_selected_day_has_closed(monkeypatch, stamp):
    reader = fake_reader(); reader.permitted = {'tushare_daily': object()}
    reader.cutoff = datetime.fromisoformat(stamp)
    monkeypatch.setattr(archive, 'load_dataset', lambda *a, **k: pytest.fail('not a closed day'))
    assert archive.closing_table(reader, DAY) is None
