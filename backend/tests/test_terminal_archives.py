"""Read-only terminal recovery from saved observations and dated archives."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.market import terminal_archives as archive, terminal_read
from app.market.terminal_schemas import TerminalStock
from test_terminal_workbench import AT, DAY, calendar, save, setup, view
from test_tushare_market import limits, quote


def test_visible_ladder_peers_receive_history_without_being_selected(client, monkeypatch):
    setup(client, monkeypatch, ['tushare_market_rt', 'tushare_market_limits', 'tushare_limit_step',
                              'tushare_calendar_sse', 'tushare_calendar_szse'])
    codes = [f'{100+i:06}.SZ' for i in range(7)]
    with Session(engine()) as db, db.begin():
        calendar(db)
        save(db, 'tushare_limit_step', [dict(trade_date='20261022', ts_code=code, name='合成', nums='2') for code in codes])
        save(db, 'tushare_market_limits', [limits(code) for code in codes], AT - timedelta(minutes=1))
        save(db, 'tushare_market_rt', [quote(codes[0], price='11'), quote(codes[1], price='10')])
        later = AT + timedelta(minutes=1)
        save(db, 'tushare_market_rt', [quote(code, price='11', at=later) for code in codes], later)
    data = view(client, 'ladder', '&stock=' + codes[0])
    rows = {row['code']: row for row in data['stocks']}
    assert rows[codes[0]]['first_observed'] == '10:30:00'
    assert rows[codes[1]]['first_observed'] == '10:31:00'
    assert '10:31:00' in rows[codes[1]]['latest_change']
    assert rows[codes[-1]]['first_observed'] == '10:31:00'
    assert {event['code'] for event in data['events']} == {codes[0]}
    earlier = view(client, 'ladder', '&stock=' + codes[0] + '&as_of_time=2026-10-23T10:30:30%2B08:00')
    assert next(row for row in earlier['stocks'] if row['code'] == codes[1])['first_observed'] is None


def fake_reader():
    return SimpleNamespace(tables={}, input_versions={}, providers={}, permitted={'mairui_pools': object()}, db=object(),
        cutoff=datetime.fromisoformat('2026-10-23T18:00:00+08:00'), calendar_days=[],
        table=lambda *a: [], day=terminal_read.source_day)


@pytest.mark.parametrize('raw,expected', [('93033','09:30:33'),('093033','09:30:33'),('250000',None),('99999',None)])
def test_supplier_clock_preserves_leading_zero_without_guessing_invalid_time(raw, expected):
    assert archive._clock(raw)==expected


def pool(**changes):
    return dict(state='complete', source_family='mairui_pools', received_at='2026-10-23T16:05:00+08:00',
        coverage_scope='合成供应商日榜，来源时点未知', rows=[dict(trade_date='20261023', ts_code='000100.SZ',
        name='合成', limit='U', limit_times='1', fd_amount='0', first_time='092500', last_time='150000',
        open_times='0')], **changes)


def test_day_pool_zero_and_raw_times_never_fill_live_book_or_observation(monkeypatch):
    reader = fake_reader()
    monkeypatch.setattr(archive, 'load_dataset', lambda *a, **k: {'state': 'missing'})
    monkeypatch.setattr(archive, 'pool_table', lambda *a, **k: pool())
    stock = TerminalStock(code='000100.SZ', name='合成', price=11, status='涨停价（盘口待核）')
    archive.attach_daily_pools(reader, DAY, {stock.code: stock})
    assert stock.daily_pool.seal_amount_cny == 0
    assert stock.daily_pool.first_limit_time == '09:25:00' and stock.daily_pool.open_times == 0
    assert stock.seal_amount_cny is None and stock.first_observed is None and stock.latest_change is None
    assert stock.status == '涨停价（盘口待核）'


@pytest.mark.parametrize('other_state,other_height,expected', [('U','1',0),('Z','1',None),('U','2',None)])
def test_primary_missing_seal_uses_a_whole_consistent_permitted_supplier_row(monkeypatch,other_state,other_height,expected):
    reader=fake_reader();reader.permitted['tushare_limit_list_d']=object()
    primary=pool();primary['source_family']='tushare_limit_list_d'
    primary['rows'][0].update(fd_amount='123456',first_time='093100',open_times='2')
    # The primary has no declared CNY unit: even a numeric value cannot be used.
    other=pool()
    other['rows'][0].update(limit=other_state,limit_times=other_height)
    monkeypatch.setattr(archive,'load_dataset',lambda *a,**k:primary)
    monkeypatch.setattr(archive,'pool_table',lambda *a,**k:other if 'mairui_pools' in reader.permitted else None)
    stock=TerminalStock(code='000100.SZ',name='合成')
    archive.attach_daily_pools(reader,DAY,{stock.code:stock})
    assert stock.daily_pool.seal_amount_cny==expected
    assert stock.daily_pool.state=='U'
    assert stock.daily_pool.source_name==('麦蕊日榜' if expected==0 else 'Tushare 日榜')
    assert stock.daily_pool.first_limit_time==('09:25:00' if expected==0 else '09:31:00')
    assert stock.daily_pool.open_times==(0 if expected==0 else 2)
    # Revocation is evaluated afresh; the cached original row is not authority.
    reader=fake_reader();reader.permitted={'tushare_limit_list_d':object()}
    stock=TerminalStock(code='000100.SZ',name='合成')
    archive.attach_daily_pools(reader,DAY,{stock.code:stock})
    assert stock.daily_pool.seal_amount_cny is None and stock.daily_pool.source_name=='Tushare 日榜'


@pytest.mark.parametrize('clock', ['2026-10-23T14:59:59+08:00', '2026-10-22T18:00:00+08:00'])
def test_day_pool_is_not_exposed_before_close_or_for_a_future_day(monkeypatch, clock):
    reader = fake_reader(); reader.cutoff = datetime.fromisoformat(clock)
    monkeypatch.setattr(archive, 'load_dataset', lambda *a, **k: pytest.fail('pre-close must not load daily pools'))
    assert archive.daily_pool_table(reader, DAY) is None


@pytest.mark.parametrize('covered,expected', [(9, None), (10, 7)])
def test_ten_day_high_requires_every_verified_date(monkeypatch, covered, expected):
    reader = fake_reader()
    reader.calendar_days = [date(2026, 10, day) for day in (9, 12, 13, 14, 15, 16, 19, 20, 21, 22)]
    known = {day: {'000100.SZ': 7} for day in reader.calendar_days[:covered]}
    monkeypatch.setattr(archive, 'daily_pool_table', lambda *a: pool())
    summary = archive.relay_summary(reader, DAY, {}, lambda _, day: known.get(day, {}), terminal_read.main_board)
    assert summary.history_high == expected and summary.history_covered_days == covered
    assert summary.first_board_codes == ['000100.SZ']
    assert summary.high_sealed_count is None


def test_complete_member_archive_is_loaded_at_its_original_member_day(client, monkeypatch):
    setup(client, monkeypatch, ['tushare_market_rt', 'tushare_dc_member', 'tushare_calendar_sse', 'tushare_calendar_szse'])
    clock = datetime.fromisoformat('2026-10-23T16:00:00+08:00')
    monkeypatch.setattr(terminal_read, 'now', lambda _: clock)
    with Session(engine()) as db, db.begin():
        calendar(db)
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='11')])
    def saved(db, dataset, day, scope='all', **kwargs):
        assert kwargs['as_of_time'] is not None
        if dataset == 'members' and day.isoformat() == '2026-10-22' and scope == 'BK1184.DC':
            return {'state': 'complete', 'rows': [dict(trade_date='20261022', ts_code=scope, con_code='000100.SZ', name='合成')], 'evidence_refs': []}
        return {'state': 'missing'}
    monkeypatch.setattr(archive, 'load_dataset', saved)
    response = client.get('/api/v1/workbench-details?day=2026-10-23&sector=BK1184.DC')
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['members_trade_day'] == '2026-10-22'
    assert data['tracking_members'][0]['pct_change'] == pytest.approx(10)
    assert data['tracking_members'][0]['source_time'].startswith('2026-10-23')
    strict = client.get('/api/v1/workbench-details?day=2026-10-23&sector=BK1184.DC&as_of_time=2026-10-23T10:31:00%2B08:00')
    assert strict.json()['data']['tracking_members'] == []


def test_shared_member_manifest_reused_without_mixing_selected_sector(monkeypatch):
    reader = fake_reader(); reader.permitted = {'tushare_dc_member': object()}
    calls = []
    source_rows = [dict(trade_date=DAY.strftime('%Y%m%d'), ts_code=sector, con_code=code)
                   for sector, code in [('BK1000.DC', '000100.SZ'), ('BK2000.DC', '000200.SZ')]]
    def saved(db, dataset, day, scope='all', **kwargs):
        calls.append((day, scope, kwargs['as_of_time']))
        return {'state': 'complete', 'rows': source_rows, 'evidence_refs': []} if scope == 'all' else {'state': 'missing'}
    monkeypatch.setattr(archive, 'load_dataset', saved)
    a = archive.archived_members(reader, DAY, 'BK1000.DC')
    b = archive.archived_members(reader, DAY, 'BK2000.DC')
    assert [row['con_code'] for row, _, _ in a] == ['000100.SZ']
    assert [row['con_code'] for row, _, _ in b] == ['000200.SZ']
    assert sum(scope == 'all' for _, scope, _ in calls) == 1
    assert len(source_rows) == 2 and all(cutoff == reader.cutoff for _, _, cutoff in calls)
    fresh = fake_reader(); fresh.permitted = {'tushare_dc_member': object()}
    fresh.cutoff = reader.cutoff - timedelta(hours=1)
    assert archive.archived_members(fresh, DAY, 'BK1000.DC') == a
    assert sum(scope == 'all' for _, scope, _ in calls) == 2
    assert calls[-1][2] == fresh.cutoff
    denied = fake_reader(); denied.permitted = {}
    assert archive.archived_members(denied, DAY, 'BK1000.DC') == []
    assert len(calls) == 5
