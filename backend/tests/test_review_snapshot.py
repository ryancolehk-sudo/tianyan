"""Saved intraday fallback is explicit, rights checked, local and date bounded."""
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.market import review_read, review_snapshot
from app.market.review_metrics import build_daily_metrics
from app.market.service import source_id
from app.models import Provider
from test_terminal_workbench import setup, save, AT, DAY
from test_tushare_market import quote, limits


CLOCK = datetime.fromisoformat('2026-10-26T16:00:00+08:00')
TODAY = CLOCK.date()


def row(code='600001.SH', **changes):
    return dict(ts_code=code, trade_time='2026-10-26 13:46:00', close='11',
                pre_close='10', open='10.1', high='11', low='10', amount='1000000', vol='30000', **changes)


def evidence(rows, received=None):
    return SimpleNamespace(id=uuid.uuid4(), source_id=uuid.uuid4(), content_hash='a' * 64,
                           payload={'rows': rows}, received_at=received or CLOCK - timedelta(minutes=134) + timedelta(seconds=3))


def mocked_db(monkeypatch, saved):
    db = Mock()
    db.scalar.return_value = saved
    monkeypatch.setattr(review_snapshot, 'permitted_provider', lambda *a: SimpleNamespace(id=saved.source_id))
    return db


def test_saved_table_converts_amount_and_retains_original_row_offsets(monkeypatch):
    saved = evidence([row('920001.BJ'), row(), {**row('000001.SZ'), 'trade_time': '2026-10-23 15:00:00'},
                      {**row('000002.SZ'), 'trade_time': '2026-10-26 16:10:00'}])
    db = mocked_db(monkeypatch, saved)
    result = review_snapshot.snapshot_table(db, 'daily', TODAY, clock=CLOCK)
    assert result['basis'] == 'intraday_snapshot' and result['row_count'] == 1
    assert result['source_time'] == '2026-10-26T13:46:00+08:00'
    assert result['rows'][0]['amount'] == '1000' and result['rows'][0]['vol'] == '300'
    assert result['rows'][0]['pct_chg'] == 10
    assert review_read._row_reference(result, 0)['source_row'] == 1
    assert result['received_at'] != result['source_time']


@pytest.mark.parametrize('dataset,day,as_of', [
    ('daily', TODAY - timedelta(days=1), None), ('daily', TODAY, CLOCK),
    ('pools', TODAY, None), ('daily', TODAY + timedelta(days=1), None),
])
def test_old_days_observed_cutoffs_and_unrelated_datasets_never_use_fallback(dataset, day, as_of):
    db = Mock()
    assert review_snapshot.snapshot_table(db, dataset, day, as_of_time=as_of, clock=CLOCK) is None
    db.scalar.assert_not_called()


def test_current_source_rights_are_required(monkeypatch):
    monkeypatch.setattr(review_snapshot, 'permitted_provider', lambda *a: None)
    db = Mock()
    assert review_snapshot.snapshot_table(db, 'daily', TODAY, clock=CLOCK) is None
    db.scalar.assert_not_called()


def test_duplicate_and_wrong_date_limits_are_not_interpreted(monkeypatch):
    saved = evidence([row(), row()])
    db = mocked_db(monkeypatch, saved)
    assert review_snapshot.snapshot_table(db, 'daily', TODAY, clock=CLOCK) is None
    saved.payload['rows'] = [{'ts_code': '600001.SH', 'trade_date': '20261023', 'up_limit': '11', 'down_limit': '9'}]
    assert review_snapshot.snapshot_table(db, 'limits', TODAY, clock=CLOCK) is None


def test_unknown_amount_is_unknown_not_zero(monkeypatch):
    saved = evidence([{**row(), 'amount': None}])
    db = mocked_db(monkeypatch, saved)
    table = review_snapshot.snapshot_table(db, 'daily', TODAY, clock=CLOCK)
    assert table['rows'][0]['amount'] is None
    metrics = build_daily_metrics(trade_day=str(TODAY), daily=table['rows'], price_limits=None, pools=None)
    review_snapshot.mark_metrics(metrics, table)
    assert metrics['turnover']['amount_yuan'] is None
    assert metrics['basis'] == 'intraday_snapshot' and metrics['sample']['daily_loaded'] is False
    assert metrics['state'] == 'partial'


def test_intraday_totals_never_gain_closing_comparison(monkeypatch):
    saved = evidence([row()])
    db = mocked_db(monkeypatch, saved)
    table = review_snapshot.snapshot_table(db, 'daily', TODAY, clock=CLOCK)
    previous = {**table['rows'][0], 'trade_date': '20261023', 'amount': '2000'}
    metrics = build_daily_metrics(trade_day=str(TODAY), daily=table['rows'], price_limits=None,
                                  pools=None, previous_daily=[previous])
    assert metrics['turnover']['change_pct'] == '-50'
    review_snapshot.mark_metrics(metrics, table)
    assert metrics['turnover']['change_pct'] is None
    assert metrics['turnover']['amount_yuan'] == '1000000'
    assert metrics['sample']['snapshot_loaded'] is True


def test_intraday_limits_keep_sample_basis_and_never_claim_closing_scope(monkeypatch):
    saved = evidence([row()])
    table = review_snapshot.snapshot_table(mocked_db(monkeypatch, saved), 'daily', TODAY, clock=CLOCK)
    metrics = build_daily_metrics(trade_day=str(TODAY), daily=table['rows'], pools=None,
                                  price_limits=[{'trade_date': str(TODAY), 'ts_code': '600001.SH', 'up_limit': '11', 'down_limit': '9'}])
    before = dict(metrics['limits']['intraday'])
    review_snapshot.mark_metrics(metrics, table)
    intraday = metrics['limits']['intraday']
    assert intraday['basis'] == 'intraday_snapshot'
    assert intraday['source_time'] == table['source_time']
    assert '采样时点' in intraday['scope'] and '收盘' not in intraday['scope']
    assert intraday['close_up'] == 1
    assert {key: intraday[key] for key in before if key != 'scope'} == {
        key: value for key, value in before.items() if key != 'scope'}


@pytest.mark.parametrize('state', ['complete', 'permission_denied', 'broken_lineage'])
def test_manifest_or_revoked_and_broken_tables_are_never_replaced(monkeypatch, state):
    monkeypatch.setattr(review_read, 'load_dataset', lambda *a, **k: {'state': state, 'rows': []})
    fallback = Mock()
    monkeypatch.setattr(review_read, 'snapshot_table', fallback)
    assert review_read._Reader(object(), None).get('daily', TODAY)['state'] == state
    fallback.assert_not_called()


def test_snapshot_index_preserves_preclose_and_object_evidence(monkeypatch):
    saved = evidence([])
    saved.payload = {'price': '3001', 'previous_close': '3000', 'quote_time': '2026-10-26T13:46:00+08:00'}
    db = mocked_db(monkeypatch, saved)
    result = review_snapshot.snapshot_table(db, 'indices', TODAY, '000001.SH', clock=CLOCK)
    assert result['rows'][0]['pre_close'] == '3000'
    assert result['rows'][0]['price_basis'] == 'intraday_snapshot'
    assert 'source_row' not in review_read._row_reference(result, 0)


def test_real_storage_fallback_respects_revocation_and_exact_day(client, monkeypatch):
    setup(client, monkeypatch, ['tushare_market_rt', 'tushare_market_limits'])
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_market_rt', [quote(price='11', at=AT)])
        save(db, 'tushare_market_limits', [limits()])
        table = review_snapshot.snapshot_table(db, 'daily', DAY, clock=AT + timedelta(minutes=1))
        caps = review_snapshot.snapshot_table(db, 'limits', DAY, clock=AT + timedelta(minutes=1))
        assert table['rows'][0]['close'] == 11
        metrics = build_daily_metrics(trade_day=str(DAY), daily=table['rows'], price_limits=caps['rows'], pools=None)
        review_snapshot.mark_metrics(metrics, table)
        assert metrics['limits']['price_verified']['up'] == 1
        source = db.get(Provider, source_id('tushare_market_rt'))
        source.rights = {**source.rights, 'display': 'denied'}
        db.flush()
        assert review_snapshot.snapshot_table(db, 'daily', DAY, clock=AT + timedelta(minutes=1)) is None
