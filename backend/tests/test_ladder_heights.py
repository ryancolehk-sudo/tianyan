"""First-board evidence boundaries; synthetic prices, no upstream requests."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.market import ladder_heights as subject, terminal_read
from test_intraday_ladder import AT, DAY, project, row

PREVIOUS = DAY - timedelta(days=1)


def reader(monkeypatch, *, close='10', upper='11', unit='元', stamp=AT, day=PREVIOUS):
    stock = row(); stock.previous_height = None
    table = {'rows': [{'ts_code': stock.code, 'close': close}]}
    monkeypatch.setattr(subject, 'closing_table', lambda *_: table)
    restriction = ({'ts_code': stock.code, 'trade_date': day.strftime('%Y%m%d'), 'up_limit': upper},
                   {'up_limit': unit}, SimpleNamespace(received_at=stamp))
    source = SimpleNamespace(cutoff=AT, day=terminal_read.source_day, table=lambda *_: [restriction])
    return source, stock


@pytest.mark.parametrize('unit', [None, '元'])
def test_first_board_requires_a_verified_previous_close_below_upper_limit(monkeypatch, unit):
    source, stock = reader(monkeypatch, unit=unit)
    codes = subject.verified_first_boards(source, PREVIOUS, {stock.code: stock})
    live = project([stock], first_board_codes=codes)
    assert live.rows[0].previous_height == 0 and live.rows[0].target_height == 1
    assert live.rows[0].status == 'touched' and live.sealed_count == 0
    stock.price = 10.5
    assert project([stock], first_board_codes=codes).rows[0].status == 'opened'
    assert stock.previous_height is None  # Do not rewrite the parent stock or its evidence.


@pytest.mark.parametrize('change', [
    {'close': '11'}, {'close': '12'}, {'close': None}, {'close': 0}, {'close': True}, {'close': 'NaN'},
    {'upper': None}, {'upper': 0}, {'upper': 'Infinity'}, {'unit': '万元'},
    {'stamp': AT + timedelta(seconds=1)}, {'day': PREVIOUS - timedelta(days=1)},
])
def test_missing_equal_above_invalid_units_dates_and_late_receipts_never_imply_zero(monkeypatch, change):
    source, stock = reader(monkeypatch, **change)
    codes = subject.verified_first_boards(source, PREVIOUS, {stock.code: stock})
    assert not codes and project([stock], first_board_codes=codes).rows[0].target_height is None


def test_missing_archive_and_previous_day_do_not_use_a_partial_ladder_as_zero(monkeypatch):
    source, stock = reader(monkeypatch)
    monkeypatch.setattr(subject, 'closing_table', lambda *_: None)
    assert not subject.verified_first_boards(source, PREVIOUS, {stock.code: stock})
    assert not subject.verified_first_boards(source, None, {stock.code: stock})


def test_known_height_is_preserved_and_non_touching_stock_does_not_load_an_archive(monkeypatch):
    source, stock = reader(monkeypatch)
    monkeypatch.setattr(subject, 'closing_table', lambda *_: pytest.fail('No first-board candidate'))
    stock.previous_height = 4
    assert not subject.verified_first_boards(source, PREVIOUS, {stock.code: stock})
    assert project([stock], first_board_codes={stock.code}).rows[0].target_height == 5
    stock.previous_height = None; stock.high = 10; stock.price = 10
    assert not subject.verified_first_boards(source, PREVIOUS, {stock.code: stock})


def test_latest_limit_revision_wins_but_same_clock_conflicts_remain_unknown(monkeypatch):
    source, stock = reader(monkeypatch)
    early = ({'ts_code': stock.code, 'trade_date': PREVIOUS.strftime('%Y%m%d'), 'up_limit': '11'},
             {'up_limit': '元'}, SimpleNamespace(received_at=AT - timedelta(seconds=1)))
    late = ({**early[0], 'up_limit': '10'}, early[1], SimpleNamespace(received_at=AT))
    source.table = lambda *_: [early, late]
    assert not subject.verified_first_boards(source, PREVIOUS, {stock.code: stock})
    source.table = lambda *_: [late, early]
    assert not subject.verified_first_boards(source, PREVIOUS, {stock.code: stock})
    source.table = lambda *_: [early, ({**early[0], 'up_limit': '12'}, early[1], early[2])]
    assert not subject.verified_first_boards(source, PREVIOUS, {stock.code: stock})


def test_full_pool_first_boards_supplement_a_nonempty_two_plus_ladder(monkeypatch):
    source = SimpleNamespace(table=lambda family, _: [({'ts_code': '600001.SH', 'nums': '4'}, {}, None)]
                             if family == 'tushare_limit_step' else [])
    monkeypatch.setattr(terminal_read, 'pool_rows', lambda *_: ({}, {
        '600002.SH': (0, {'limit': 'U', 'limit_times': '1'}),
        '600003.SH': (1, {'limit': 'Z', 'limit_times': '1'}),
    }))
    assert terminal_read.prior_heights(source, PREVIOUS) == {'600001.SH': 4, '600002.SH': 1}


def publish_previous_close(rows, received):
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market.review_store import persist_pages
    from test_review_collection import page, setup_source
    _, provider, config, job = setup_source('daily', target=PREVIOUS)
    with Session(engine()) as db, db.begin():
        persist_pages(db, provider, config, job, [(page(rows), received)])
    return provider.id


def prepare_live(client, monkeypatch):
    from sqlalchemy.orm import Session
    from app.db import engine
    from test_terminal_workbench import calendar, save, setup
    from test_tushare_market import limits, quote
    setup(client, monkeypatch, ['tushare_market_rt', 'tushare_market_limits', 'tushare_limit_step',
                               'tushare_calendar_sse', 'tushare_calendar_szse'])
    monkeypatch.setattr(terminal_read, 'now', lambda _: AT)
    codes = ['600001.SH', '600002.SH', '600003.SH', '600004.SH']
    with Session(engine()) as db, db.begin():
        calendar(db)
        save(db, 'tushare_market_limits', [limits(code) for code in codes], AT - timedelta(seconds=1))
        save(db, 'tushare_market_limits', [{**limits(code), 'trade_date': PREVIOUS.strftime('%Y%m%d')}
                                          for code in codes], AT - timedelta(days=1))
        save(db, 'tushare_limit_step', [dict(ts_code=codes[0], name='合成高板', trade_date='20261022', nums='4'),
                                      dict(ts_code=codes[2], name='合成昨首板', trade_date='20261022', nums='1')], AT)
        save(db, 'tushare_market_rt', [quote(code, price='11') for code in codes], AT)
    from test_review_collection import daily
    rows = [{**daily(index, target=PREVIOUS), 'ts_code': code, 'close': '10' if index == 1 else '11'}
            for index, code in enumerate(codes)]
    return codes, rows


def test_live_workbench_resolves_first_board_from_committed_archive_and_respects_revocation(client, monkeypatch):
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.evidence.models import EvidenceVersion
    from app.market.models import DataFetchRun
    from app.models import Job, Provider
    from test_terminal_workbench import view
    codes, rows = prepare_live(client, monkeypatch)
    provider = publish_previous_close(rows, AT - timedelta(hours=18))
    def counts():
        with Session(engine()) as db:
            return tuple(db.scalar(select(func.count()).select_from(model)) for model in [Job, DataFetchRun, EvidenceVersion])
    before = counts()
    for kind in ['overview', 'ladder']:
        live = {row['code']: row for row in view(client, kind)['intraday_ladder']['rows']}
        assert live[codes[0]]['target_height'] == 5
        assert (live[codes[1]]['previous_height'], live[codes[1]]['target_height']) == (0, 1)
        assert live[codes[2]]['target_height'] == 2
        assert live[codes[3]]['target_height'] is None  # Equal yesterday: a missing streak is not zero.
    assert counts() == before
    with Session(engine()) as db, db.begin():
        db.get(Provider, provider).rights = {'display': 'unknown', 'cache': 'unknown'}
    revoked = {row['code']: row for row in view(client)['intraday_ladder']['rows']}
    assert revoked[codes[1]]['target_height'] is None


def test_previous_close_received_after_cutoff_does_not_fill_a_live_first_board(client, monkeypatch):
    from test_terminal_workbench import view
    codes, rows = prepare_live(client, monkeypatch)
    publish_previous_close(rows, AT + timedelta(seconds=1))
    live = {row['code']: row for row in view(client)['intraday_ladder']['rows']}
    assert live[codes[1]]['target_height'] is None
    monkeypatch.setattr(terminal_read, 'now', lambda _: AT + timedelta(seconds=2))
    live = {row['code']: row for row in view(client)['intraday_ladder']['rows']}
    assert live[codes[1]]['target_height'] == 1
    assert view(client, params='&as_of_time=2026-10-23T10:30:00%2B08:00')['intraday_ladder'] is None
