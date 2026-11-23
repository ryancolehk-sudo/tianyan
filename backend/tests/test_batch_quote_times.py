"""Repeated batch clocks must not weaken per-stock or per-read validation."""
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.market import fuyao_market as fm, resilience, tushare
from app.market.protocol import SHANGHAI
from test_data_resilience import CLOCK, receipt, scenario, source
from test_fuyao_market import batch, raw, scene
from test_row_count_order import save


FAMILIES = ('fuyao_quotes', 'tushare_market_rt')


def row(family, code, at=CLOCK, close='11'):
    key = 'snapshot_time' if family == 'fuyao_quotes' else 'trade_time'
    value = at.isoformat() if family == 'fuyao_quotes' else at.strftime('%Y-%m-%d %H:%M:%S')
    return dict(ts_code=code, close=close, **{key: value})


def bulk(db, owner, monkeypatch, family, rows, received=CLOCK):
    provider, config = source(db, owner, family, symbols=[])
    if family == 'fuyao_quotes':
        config.config = {'symbols': [], 'collection_mode': 'full_market', 'backup_enabled': True}
        monkeypatch.setattr('app.market.session.market_session',
            lambda *a: SimpleNamespace(is_trading_day=True, trade_day=CLOCK.date()))
    run = receipt(db, provider, at=received, duplicates=1)
    save(db, provider.id, None, received, payload=dict(rows=rows,
        dataset=fm.DATASET if family == 'fuyao_quotes' else 'tushare.rt_market'))
    return provider, config, run


@pytest.mark.parametrize('family', FAMILIES)
def test_quality_checks_each_distinct_string_once_in_each_call(scenario, monkeypatch, family):
    calls = []
    module, name = (fm, 'current_snapshot') if family == 'fuyao_quotes' else (tushare, 'clock')
    original = getattr(module, name)
    key = 'snapshot_time' if family == 'fuyao_quotes' else 'trade_time'

    def checked(value, *args):
        calls.append(value.get(key) if family == 'fuyao_quotes' else value)
        return original(value, *args)

    monkeypatch.setattr(module, name, checked)
    values = [row(family, '600000.SH')[key], 'invalid', '', None, [], {'bad': 'clock'}]
    if family == 'tushare_market_rt':
        values += [CLOCK.strftime('%Y%m%d%H%M%S'), CLOCK.strftime('%Y-%m-%dT%H:%M:%S')]
    rows = [dict(ts_code=f'{600000+i}.SH', close='11', **{key: value})
        for i, value in enumerate(values * 8)]
    with Session(engine()) as db, db.begin():
        p, c, run = bulk(db, scenario, monkeypatch, family, rows)
        for _ in range(2):
            calls.clear()
            quality = resilience.saved_quality(db, p, c, 'stock_quote', CLOCK.date(), CLOCK, run)
            assert quality.valid and quality.missing_count == (0 if family == 'fuyao_quotes' else None)
            assert quality.received_at == CLOCK
            assert quality.source_time == (None if family == 'fuyao_quotes' else CLOCK)
            assert Counter(v for v in calls if isinstance(v, str)) == Counter({v: 1 for v in values if isinstance(v, str)})


@pytest.mark.parametrize('family', FAMILIES)
def test_shared_time_does_not_share_price_or_configured_membership(scenario, monkeypatch, family):
    rows = [row(family, '600000.SH', close=None), row(family, '600001.SH'), row(family, '600002.SH')]
    with Session(engine()) as db, db.begin():
        p, c, run = bulk(db, scenario, monkeypatch, family, rows)
        scope = lambda codes: dict(universe='configured_symbols', symbols=[
            dict(exchange='SSE', symbol=code, asset_type='stock') for code in codes])
        missing = resilience.saved_quality(db, p, c, 'stock_quote', CLOCK.date(), CLOCK, run,
            required_scope=scope(['600000', '600001']))
        assert not missing.valid and missing.missing_count == 1
        valid = resilience.saved_quality(db, p, c, 'stock_quote', CLOCK.date(), CLOCK, run,
            required_scope=scope(['600001', '600002']))
        assert valid.valid and valid.missing_count == 0
        assert valid.source_time == (None if family == 'fuyao_quotes' else CLOCK)


@pytest.mark.parametrize('family', FAMILIES)
@pytest.mark.parametrize('boundary', ['expiry', 'future', 'receipt', 'wrong_day', 'bad_then_valid'])
def test_time_reuse_never_survives_the_next_read_context(scenario, monkeypatch, family, boundary):
    stamp, received = CLOCK, CLOCK
    if boundary == 'expiry':
        received = CLOCK + timedelta(seconds=60)
        cutoffs = [(CLOCK+timedelta(seconds=120), True), (CLOCK+timedelta(seconds=121), False)]
    elif boundary == 'future':
        stamp = CLOCK + timedelta(seconds=60)
        cutoffs = [(CLOCK, False), (CLOCK+timedelta(seconds=61), True)]
    elif boundary == 'receipt':
        received = CLOCK + timedelta(seconds=2)
        cutoffs = [(CLOCK, False), (received, True)]
    elif boundary == 'wrong_day':
        stamp = CLOCK - timedelta(days=1)
        cutoffs = [(CLOCK, False), (CLOCK+timedelta(seconds=1), False)]
    else:
        cutoffs = [(CLOCK, True), (CLOCK, True)]
    rows = [row(family, '600000.SH', stamp), row(family, '600001.SH', stamp)]
    if boundary == 'bad_then_valid':
        key = 'snapshot_time' if family == 'fuyao_quotes' else 'trade_time'
        rows.insert(0, dict(ts_code='600002.SH', close='11', **{key: 'invalid'}))
    with Session(engine()) as db, db.begin():
        p, c, run = bulk(db, scenario, monkeypatch, family, rows, received)
        for cutoff, expected in cutoffs:
            quality = resilience.saved_quality(db, p, c, 'stock_quote', CLOCK.date(), cutoff, run)
            assert quality.valid is expected
            if expected:
                assert quality.source_time == (None if family == 'fuyao_quotes' else stamp)


def test_live_fallback_reuses_time_without_reordering_rows(scenario, monkeypatch):
    from app.market import terminal_read
    from app.market.collect import store_reference_batch
    from app.market.models import DataFetchRun

    with Session(engine()) as db, db.begin():
        _, _, p, c, _ = scene(db, scenario, monkeypatch, snapshot_age=5)
        store_reference_batch(db, p, c, batch([raw('600002.SH'), raw(), raw('600001.SH')]), CLOCK)
        db.add(DataFetchRun(source_id=p.id, capability='quote.live', started_at=CLOCK-timedelta(milliseconds=50),
            received_at=CLOCK, status='partial', reason='snapshot_trade_date_unverified', expected=1,
            accepted=1, duplicates=0, rejected=0, request_count=1))
        db.flush()
        calls = []
        original = fm.current_snapshot

        def checked(value, *args):
            calls.append(value.get('snapshot_time'))
            return original(value, *args)

        monkeypatch.setattr(fm, 'current_snapshot', checked)
        reader = terminal_read.SavedTables(db, CLOCK)
        session = terminal_read.market_session(db, CLOCK)
        snapshot = terminal_read.live_snapshot_overlay(reader, CLOCK.astimezone(SHANGHAI).date(),
            session, None, [], strict=False)
        assert snapshot is not None
        assert [value['ts_code'] for value in snapshot[1]] == ['600002.SH', '600000.SH', '600001.SH']
        # One quality check and one independent display filter, each scoped to its invocation.
        value = snapshot[1][0]['snapshot_time']
        assert calls == [value, value]
