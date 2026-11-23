"""Current review context overlays saved quotes without changing archive dates."""
from datetime import date, datetime, timedelta
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market import review_read as read, review_series as series, review_snapshot as snapshot
from app.market.review_store import available_trade_days
from app.models import Job, Provider


TODAY, PREVIOUS, PRIOR = date(2026, 10, 27), date(2026, 10, 26), date(2026, 10, 23)
CLOCK = datetime.fromisoformat('2026-10-27T10:00:00+08:00')
OBSERVED = datetime.fromisoformat('2026-10-20T08:00:00+08:00')


@pytest.fixture
def clock(monkeypatch):
    value = [CLOCK]
    class ClockType(type):
        def __instancecheck__(cls, obj):
            return isinstance(obj, datetime)
    class FrozenDatetime(datetime, metaclass=ClockType):
        @classmethod
        def now(cls, tz=None):
            return value[0].astimezone(tz) if tz else value[0].replace(tzinfo=None)
    monkeypatch.setattr(read, 'datetime', FrozenDatetime)
    monkeypatch.setattr(snapshot, 'datetime', FrozenDatetime)
    monkeypatch.setattr('app.market.review_store.datetime', FrozenDatetime)
    return value


def sample():
    return {'state': 'complete', 'basis': 'intraday_snapshot', 'row_count': 1,
            'source_time': '2026-10-27T09:55:00+08:00'}


def test_context_overlays_today_only_in_its_own_bounded_read_window(clock, monkeypatch):
    closed = [date(2026, 10, 22), PRIOR, PREVIOUS]
    calendar = Mock(return_value=closed)
    monkeypatch.setattr(read, 'available_trade_days', calendar)
    probe = Mock(return_value=TODAY)
    monkeypatch.setattr(read, '_current_trade_day', probe)
    result, days, selected = read._context(object(), None, 3, None)
    assert result['shared_date'] == '2026-10-27' and selected == TODAY
    assert days == [PRIOR, PREVIOUS, TODAY] and closed == [date(2026, 10, 22), PRIOR, PREVIOUS]
    assert result['calendar_state'] == 'complete'
    assert calendar.call_args.kwargs['as_of_time'] == CLOCK
    assert probe.call_args.args[1:] == (TODAY, CLOCK)


@pytest.mark.parametrize('requested,cutoff,expected', [
    (PREVIOUS, None, PREVIOUS),
    (None, CLOCK - timedelta(days=1), PREVIOUS),
    (TODAY, CLOCK, None),
    (TODAY + timedelta(days=1), None, None),
])
def test_context_explicit_history_asof_and_future_never_probe_today(clock, monkeypatch, requested, cutoff, expected):
    monkeypatch.setattr(read, 'available_trade_days', lambda *a, **k: [PRIOR, PREVIOUS])
    probe = Mock(side_effect=AssertionError('historical request must not read current evidence'))
    monkeypatch.setattr(read, '_current_trade_day', probe)
    context, _, selected = read._context(object(), requested, 3, cutoff)
    assert selected == expected
    if requested:
        assert context['shared_date'] == str(requested)
    probe.assert_not_called()


def test_context_current_slot_does_not_wait_for_a_quote(clock, monkeypatch):
    monkeypatch.setattr(read, 'available_trade_days', lambda *a, **k: [PRIOR, PREVIOUS])
    monkeypatch.setattr(read, '_current_trade_day', lambda *a, **k: TODAY)
    quote = Mock(side_effect=AssertionError('a date slot must not depend on quotes'))
    monkeypatch.setattr(read, '_current_snapshot', quote)
    assert read._context(object(), None, 3, None)[2] == TODAY
    assert read._context(object(), TODAY, 3, None)[2] == TODAY
    quote.assert_not_called()


@pytest.mark.parametrize('value,expected', [('08:59:59', None), ('09:00:00', TODAY)])
def test_context_today_slot_starts_at_nine_on_verified_open_day(clock, monkeypatch, value, expected):
    monkeypatch.setattr('app.market.mairui.pool_day', lambda *a, **k: TODAY)
    clock[0] = datetime.fromisoformat(f'2026-10-27T{value}+08:00')
    assert read._current_trade_day(object(), TODAY, clock[0]) == expected


def test_context_preopen_cannot_be_activated_by_same_day_quote(clock, monkeypatch):
    probe = Mock(side_effect=AssertionError('preopen must not probe a same-day table'))
    monkeypatch.setattr(read, 'snapshot_table', probe)
    clock[0] = CLOCK.replace(hour=9, minute=29, second=59)
    assert read._current_snapshot(object(), TODAY, clock[0]) is None
    probe.assert_not_called()


def test_context_postopen_requires_postopen_source_time(clock, monkeypatch):
    monkeypatch.setattr('app.market.mairui.pool_day', lambda *a, **k: TODAY)
    monkeypatch.setattr(read, 'snapshot_table', lambda *a, **k: {**sample(), 'source_time': '2026-10-27T09:20:00+08:00'})
    assert read._current_snapshot(object(), TODAY, clock[0]) is None


@pytest.mark.parametrize('value', ['11:45:00', '15:03:00'])
def test_context_lunch_and_close_handoff_keep_actual_morning_sample(clock, monkeypatch, value):
    monkeypatch.setattr('app.market.mairui.pool_day', lambda *a, **k: TODAY)
    monkeypatch.setattr(read, 'snapshot_table', lambda *a, **k: sample())
    clock[0] = datetime.fromisoformat(f'2026-10-27T{value}+08:00')
    assert read._current_snapshot(object(), TODAY, clock[0])['basis'] == 'intraday_snapshot'


def setup_saved(client, monkeypatch, clock, *, quotes=True, today_open=True):
    from app.market.collect import store_calendar_batch
    from app.market.models import SourceSettings
    from app.market.review_jobs import queue_dataset
    from app.market.review_protocol import ReviewPage
    from app.market.review_store import persist_pages
    from app.market.service import source_id
    from app.models import User
    from test_data_sources import configure, login
    from test_tushare import source
    monkeypatch.setattr(settings(), 'tushare_token', SecretStr('synthetic-live-context-token'))
    monkeypatch.setattr('app.market.service.now', lambda db: OBSERVED)
    monkeypatch.setattr('app.market.review_jobs.now', lambda db: clock[0])
    def forbidden(*a, **k):
        pytest.fail('Review reads must never contact a supplier')
    monkeypatch.setattr('app.market.tushare.request', forbidden)
    monkeypatch.setattr('app.market.review_jobs.fetch_public', forbidden)
    headers = login(client)
    for family in ('tushare_calendar_sse', 'tushare_calendar_szse', 'tushare_daily', 'tushare_market_rt'):
        configure(client, headers, source(family), key=family)
    with Session(engine()) as db, db.begin():
        dates = [(PRIOR, True), (date(2026, 10, 24), False), (date(2026, 10, 25), False),
                 (PREVIOUS, True), (TODAY, today_open)]
        for family, exchange in (('tushare_calendar_sse', 'SSE'), ('tushare_calendar_szse', 'SZSE')):
            provider = db.get(Provider, source_id(family))
            store_calendar_batch(db, provider, db.get(SourceSettings, provider.id), dates,
                                 OBSERVED, exchanges=(exchange,))
        provider = db.get(Provider, source_id('tushare_daily'))
        config = db.get(SourceSettings, provider.id)
        job = queue_dataset(db, db.get(User, config.owner_id), 'daily', PREVIOUS)
        row = {'ts_code': '600001.SH', 'trade_date': '20261026', 'open': '10', 'close': '10.1',
               'high': '10.2', 'low': '9.9', 'pre_close': '10', 'pct_chg': '1', 'amount': '2500'}
        page = ReviewPage(fields=list(row), rows=[row], source_url='https://tushare.pro/document/2',
                         raw_hash='b' * 64, scope='Synthetic prior-day archive')
        receipt = datetime.fromisoformat('2026-10-26T16:00:00+08:00')
        persist_pages(db, provider, config, job, [(page, receipt)])
        job.state, job.completed_at = 'succeeded', receipt
        if quotes:
            save_quote(db, CLOCK - timedelta(minutes=5), CLOCK - timedelta(minutes=5) + timedelta(seconds=3))


def save_quote(db, source_time, receipt):
    from test_terminal_workbench import save
    from test_tushare_market import quote
    save(db, 'tushare_market_rt', [quote('600001.SH', price='11', at=source_time)], receipt)


def test_saved_previous_archive_and_current_snapshot_default_today_without_calendar_mutation(client, monkeypatch, clock):
    setup_saved(client, monkeypatch, clock)
    with Session(engine()) as db:
        before = available_trade_days(db, limit=3)
        current = read.daily_view(db)
        previous = read.daily_view(db, PREVIOUS)
        history = series.series_view(db)
        assert current['shared_date'] == '2026-10-27'
        assert current['metrics']['basis'] == 'intraday_snapshot'
        assert current['metrics']['sample']['daily_loaded'] is False
        assert current['metrics']['calendar']['previous_trade_day'] == '2026-10-26'
        assert current['observation']['source_time'] == '2026-10-27T09:55:00+08:00'
        assert previous['shared_date'] == '2026-10-26' and previous['observation'] is None
        assert previous['metrics']['turnover']['amount_yuan'] == '2500000'
        assert history['trade_days'] == ['2026-10-23', '2026-10-26', '2026-10-27']
        assert history['records'][-1]['metrics']['basis'] == 'intraday_snapshot'
        assert '今日行' in history['scope_note'] and '不代表收盘' in history['scope_note']
        assert before == available_trade_days(db, limit=3) == [PRIOR, PREVIOUS]
        assert len(list(db.scalars(select(Job.id)))) == 1


def test_saved_explicit_today_details_follow_live_date_but_asof_stays_closed(client, monkeypatch, clock):
    from app.market.review_details import stocks_view
    setup_saved(client, monkeypatch, clock)
    with Session(engine()) as db:
        stocks = stocks_view(db, TODAY, ['600001.SH'])
        assert stocks['shared_date'] == '2026-10-27'
        assert stocks['items'][0]['quote']['price_basis'] == 'intraday_snapshot'
        result = read.daily_view(db, as_of_time=CLOCK)
        assert result['shared_date'] == '2026-10-26' and result['observation'] is None
        explicit = read.daily_view(db, TODAY, as_of_time=CLOCK)
        assert explicit['calendar_state'] == 'unavailable' and explicit['observation'] is None
        assert not any(item['trade_day'] == '2026-10-27' and item.get('evidence_refs') for item in explicit['coverage'])


@pytest.mark.parametrize('kind', ['absent', 'future_receipt', 'future_source', 'preopen_source'])
def test_saved_absent_or_future_current_evidence_cannot_switch_context(client, monkeypatch, clock, kind):
    setup_saved(client, monkeypatch, clock, quotes=False)
    if kind != 'absent':
        with Session(engine()) as db, db.begin():
            source_time = CLOCK - timedelta(minutes=5)
            receipt = CLOCK - timedelta(seconds=1)
            if kind == 'future_receipt':
                receipt = CLOCK + timedelta(minutes=1)
            elif kind == 'future_source':
                source_time = CLOCK + timedelta(seconds=20)
            else:
                source_time = CLOCK.replace(hour=9, minute=20)
            save_quote(db, source_time, receipt)
    with Session(engine()) as db:
        current = read.daily_view(db)
        assert current['shared_date'] == '2026-10-27'
        assert current['observation'] is None
        assert current['metrics']['sample']['N'] is None
        assert current['metrics']['turnover']['amount_yuan'] is None


@pytest.mark.parametrize('restriction', ['closed_calendar', 'revoked_quotes'])
def test_saved_quote_alone_cannot_override_calendar_or_rights(client, monkeypatch, clock, restriction):
    from app.market.service import source_id
    setup_saved(client, monkeypatch, clock, today_open=restriction != 'closed_calendar')
    with Session(engine()) as db, db.begin():
        if restriction == 'revoked_quotes':
            provider = db.get(Provider, source_id('tushare_market_rt'))
            provider.rights = {**provider.rights, 'display': 'denied'}
            db.flush()
        result = read.daily_view(db)
        assert result['shared_date'] == ('2026-10-26' if restriction == 'closed_calendar' else '2026-10-27')
        assert result['observation'] is None


def test_saved_nine_oclock_empty_slot_and_same_key_fills_when_quote_arrives(client, monkeypatch, clock):
    setup_saved(client, monkeypatch, clock, quotes=False)
    clock[0] = CLOCK.replace(hour=8, minute=59, second=59)
    with Session(engine()) as db:
        assert read.daily_view(db)['shared_date'] == '2026-10-26'
    clock[0] = CLOCK.replace(hour=9, minute=0)
    with Session(engine()) as db:
        empty = read.daily_view(db)
        history = series.series_view(db)
        assert empty['shared_date'] == '2026-10-27'
        assert empty['metrics']['calendar']['previous_trade_day'] == '2026-10-26'
        assert empty['metrics']['sample']['N'] is None and empty['observation'] is None
        assert empty['metrics']['turnover']['amount_yuan'] is None
        assert history['trade_days'] == ['2026-10-23', '2026-10-26', '2026-10-27']
        assert history['records'][-1]['metrics']['sample']['N'] is None
        assert '09:00预留今日行' in history['scope_note']
        previous_amount = history['records'][-2]['metrics']['turnover']['amount_yuan']
    clock[0] = CLOCK.replace(hour=9, minute=31)
    with Session(engine()) as db, db.begin():
        save_quote(db, clock[0] - timedelta(seconds=30), clock[0] - timedelta(seconds=27))
    with Session(engine()) as db:
        filled = read.daily_view(db)
        history = series.series_view(db)
        assert filled['shared_date'] == empty['shared_date'] == '2026-10-27'
        assert filled['metrics']['basis'] == 'intraday_snapshot'
        assert filled['metrics']['sample']['N'] == 1
        assert history['records'][-2]['trade_day'] == '2026-10-26'
        assert history['records'][-2]['metrics']['turnover']['amount_yuan'] == previous_amount == '2500000'
        assert available_trade_days(db, limit=3) == [PRIOR, PREVIOUS]
