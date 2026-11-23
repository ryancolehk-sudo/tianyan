"""Read-cost regressions with real disposable PostgreSQL and synthetic inputs."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.market import terminal_read
from app.market.protocol import SHANGHAI
from app.market.service import source_id
from app.models import Provider
from test_terminal_workbench import AT, DAY, REVIEW, calendar, save, setup, view
from test_tushare_market import quote


def test_shared_quote_clock_is_parsed_once_per_receipt_without_losing_speed(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    previous=AT-timedelta(minutes=5)
    codes=[f'{index:06}.SZ' for index in range(100,340)]
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote(code,price='10',at=previous) for code in codes],previous)
        save(db,'tushare_market_rt',[quote(code,price='11') for code in codes],AT)
    original=terminal_read.source_clock
    calls=[]
    def counted(value,received_at):
        calls.append((value,received_at))
        return original(value,received_at)
    monkeypatch.setattr(terminal_read,'source_clock',counted)
    data=view(client,'auction')
    assert data['coverage']['quote_count']==240 and len(data['stocks'])==200
    assert all(item['speed_5m']==pytest.approx(10) for item in data['stocks'])
    assert all(item['speed_window_seconds']==300 for item in data['stocks'])
    assert data['breadth']['up']==240
    # Identical raw timestamps across a batch must not be parsed per stock,
    # then again for the speed, freshness and capability panels.
    assert len(calls)==2 and {receipt for _,receipt in calls}=={AT,previous}


@pytest.mark.parametrize('selected',['000100.SZ','000369.SZ',None])
def test_four_day_auction_ranks_entire_sample_but_only_builds_visible_items(client,monkeypatch,selected):
    setup(client,monkeypatch,['tushare_stk_auction','tushare_calendar_sse','tushare_calendar_szse'])
    rows_by_day={}
    with Session(engine()) as db,db.begin():
        calendar(db)
        for offset in range(4):
            day=DAY-timedelta(days=offset)
            # Adjacent equal values exercise stable input order. Nulls stay
            # below an actual zero and a selected tail row must remain visible.
            rows=[dict(trade_date=day.strftime('%Y%m%d'),ts_code=f'{index:06}.SZ',
                price='11',pre_close='10',amount=str((369-index)//2) if index<368 else None,
                volume_ratio='1.5') for index in range(100,370)]
            rows_by_day[day]=rows
            save(db,'tushare_stk_auction',rows,AT)
    original=terminal_read.make_stock
    built=[]
    def counted(code,names,identities):
        built.append(code)
        return original(code,names,identities)
    monkeypatch.setattr(terminal_read,'make_stock',counted)
    with Session(engine()) as db:
        reader=terminal_read.SavedTables(db,REVIEW)
        result=terminal_read.auction_projection(reader,DAY,{}, {},selected)
    assert len(result)==4 and len(built)==800
    for entry in result:
        ordered=rows_by_day[entry.day]
        expected=[row['ts_code'] for row in ordered[:200]]
        if selected and selected not in expected:expected=expected[:199]+[selected]
        assert [item.code for item in entry.items]==expected
        assert all(item.auction_pct==pytest.approx(10) for item in entry.items)
        assert all(item.node_amounts=={'09:15':None,'09:20':None,'09:25':None} for item in entry.items)
        if selected=='000369.SZ':assert entry.items[-1].auction_amount_cny is None


def test_request_reuse_cannot_leak_late_receipts_or_revoked_sources(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_stk_auction'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote('000100.SZ',price='10')],AT)
        save(db,'tushare_stk_auction',[dict(trade_date='20261023',ts_code='000100.SZ',
            price='11',pre_close='10',amount='125')],AT+timedelta(minutes=1))
    assert view(client,'auction')['stocks'][0]['auction_amount_cny']==125
    observed=view(client,'auction','&as_of_time=2026-10-23T10:30:30%2B08:00')
    assert observed['stocks'][0]['price']==10
    assert observed['stocks'][0]['auction_amount_cny'] is None
    assert all(not entry['items'] for entry in observed['auction_days'])
    with Session(engine()) as db,db.begin():
        for family in ('tushare_market_rt','tushare_stk_auction'):
            provider=db.get(Provider,source_id(family))
            provider.rights={**provider.rights,'display':'denied'}
    denied=view(client,'auction')
    assert denied['stocks']==[] and denied['breadth']['up'] is None
    assert all(not entry['items'] for entry in denied['auction_days'])
    assert {item['key'] for item in denied['capabilities'] if item['status']=='permission_denied'}>={'quotes','auction_trades'}


def test_request_clock_cache_keeps_receipt_guard_and_strict_cutoff(client,monkeypatch):
    setup(client,monkeypatch,[])
    with Session(engine()) as db:
        reader=terminal_read.SavedTables(db,REVIEW)
        future=AT+timedelta(seconds=61)
        row={'trade_time':future.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')}
        assert reader.at(row,AT+timedelta(seconds=1))==future
        assert reader.at(row,AT) is None
        assert reader.at(row,future)==future
        # Invalid reference dates retain their old unknown-date behavior.
        assert reader.day([]) is None and reader.day({'day':'20261023'}) is None


@pytest.mark.parametrize('raw',[[],['2026-10-23 10:30:00'],{},
    {'time':'2026-10-23 10:30:00'},True,123,10.3,None])
def test_malformed_saved_clocks_are_ignored_without_hiding_valid_rows(client,monkeypatch,raw):
    setup(client,monkeypatch,[])
    valid=quote('000100.SZ',price='11')
    malformed={**quote('000101.SZ'), 'trade_time':raw}
    meta=SimpleNamespace(id='synthetic-malformed-clock-batch',received_at=AT)
    with Session(engine()) as db:
        reader=terminal_read.SavedTables(db,REVIEW)
        # Model an already stored legacy JSON row without passing through the
        # current ingestion validator or altering immutable database evidence.
        monkeypatch.setattr(reader,'evidence',lambda meta:SimpleNamespace(payload={'rows':[malformed,valid]}))
        assert reader.at(malformed,AT) is None
        assert terminal_read.current_rows(reader,meta,DAY)==[valid]


def test_permission_snapshot_avoids_per_provider_family_queries(client, monkeypatch):
    from sqlalchemy import event
    setup(client, monkeypatch, ['tushare_market_rt', 'tushare_calendar_sse', 'tushare_calendar_szse'])
    with Session(engine()) as db, db.begin():
        calendar(db)
    queries = []
    def selected(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().startswith('SELECT providers.family AS providers_family'):
            queries.append(statement)
    event.listen(engine(), 'before_cursor_execute', selected)
    try:
        with Session(engine()) as db:
            reader = terminal_read.SavedTables(db, AT, DAY)
            assert 'tushare_market_rt' in reader.permitted
            assert reader.providers['tushare_calendar_sse'].family == 'tushare_calendar_sse'
    finally:
        event.remove(engine(), 'before_cursor_execute', selected)
    assert queries == []
