"""Sector tracking uses full same-day members on disposable PostgreSQL only."""
from datetime import timedelta
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.main import app
from app.market import terminal_read
from app.market.models import DataFetchRun
from app.market.service import source_id
from app.market.terminal_schemas import TerminalStock
from app.models import Job, Provider
from test_terminal_workbench import AT, save, setup, view
from test_tushare_market import quote

SECTOR = 'BK1184.DC'


def member(code, *, sector=SECTOR, day='20261023'):
    return {'trade_date':day,'ts_code':sector,'con_code':code,'name':'合成成分'+code}


def tracking(client, *, sector=SECTOR, suffix=''):
    response=client.get('/api/v1/workbench-details?day=2026-10-23&sector='+sector+suffix)
    assert response.status_code==200,response.text
    return response.json()['data']


def test_full_constituency_speed_leader_survives_old_sixty_member_priority(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_dc_member'])
    monkeypatch.setattr('app.market.collect.request_tushare',lambda *a,**k:pytest.fail('GET must not collect'))
    codes=[f'{i:06}.SZ' for i in range(100,170)]+['000199.SZ']
    earlier=AT-timedelta(minutes=5)
    with Session(engine()) as db,db.begin():
        save(db,'tushare_dc_member',[member(code) for code in codes])
        save(db,'tushare_market_rt',[quote(code,price='10',at=earlier) for code in codes+['000999.SZ']],earlier)
        save(db,'tushare_market_rt',[quote(code,price='11' if code=='000199.SZ' else '12' if code=='000999.SZ' else '10',at=AT)
            for code in codes+['000999.SZ']],AT)
    original=view(client,params='&sector='+SECTOR)
    data=tracking(client)
    assert len(data['members'])==60 and '000199.SZ' not in {row['code'] for row in data['members']}
    assert data['members']==original['selected_members']
    assert 'tracking_members' not in original  # Default aggregate payload stays bounded as before.
    assert data['tracking_member_count']==71 and not data['tracking_members_truncated']
    assert len(data['tracking_members'])==71 and data['tracking_members'][0]['code']=='000199.SZ'
    assert data['tracking_members'][0]['speed_5m']==pytest.approx(10)
    assert {row['code'] for row in data['tracking_members']}==set(codes)
    assert set(data['tracking_members'][0])=={'code','name','instrument_id','pct_change','speed_5m','source_time','speed_window_seconds','price_basis','source_received_at','source_snapshot_time'}
    assert data['tracking_members'][0]['source_snapshot_time'] is None
    assert data['tracking_members'][0]['price_basis']=='intraday'
    assert data['tracking_members'][0]['source_received_at'] is not None
    spec=json.loads((Path(__file__).resolve().parents[2]/'contracts/openapi.p2.json').read_text())
    schema={**spec['components']['schemas']['TerminalDetails'],'components':spec['components']}
    Draft202012Validator(schema,format_checker=FormatChecker()).validate(data)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job))==0
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==0


def test_speed_sort_preserves_zero_negative_null_and_stable_code_ties(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_dc_member'])
    earlier=AT-timedelta(minutes=5)
    codes=[f'{i:06}.SZ' for i in range(100,106)]
    with Session(engine()) as db,db.begin():
        save(db,'tushare_dc_member',[member(code) for code in reversed(codes)])
        save(db,'tushare_market_rt',[quote(code,price='10',at=earlier) for code in codes[:4]],earlier)
        save(db,'tushare_market_rt',[quote(code,price=price,at=AT) for code,price in zip(codes,['10.1','10.1','10','9'])]+
            [quote('000105.SZ',price='11',at=AT)],AT)
    data=tracking(client)
    rows=data['tracking_members']
    assert [row['code'] for row in rows]==codes
    assert [row['speed_5m'] for row in rows[:4]]==pytest.approx([1,1,0,-10])
    assert all(row['speed_5m'] is None and row['speed_window_seconds'] is None for row in rows[4:])
    assert rows[4]['source_time'] is None and rows[4]['pct_change'] is None
    assert rows[5]['pct_change']==pytest.approx(10)  # Day gain is never substituted for speed.
    assert all(row['speed_window_seconds']==300 for row in rows[:4])


def test_requested_sector_contains_only_its_same_day_members(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_dc_member'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_dc_member',[member('000100.SZ'),member('000101.SZ',sector='BK1185.DC'),member('000102.SZ',day='20261022')])
        save(db,'tushare_market_rt',[quote(f'{i:06}.SZ',price='11') for i in range(100,103)])
    first=tracking(client)
    second=tracking(client,sector='BK1185.DC')
    assert first['members_trade_day']=='2026-10-23' and first['tracking_member_count']==1
    assert [row['code'] for row in first['tracking_members']]==['000100.SZ']
    assert [row['code'] for row in second['tracking_members']]==['000101.SZ']
    assert tracking(client,sector='BK1186.DC')['tracking_members']==[]


def test_previous_day_constituency_and_quotes_are_never_borrowed(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_dc_member'])
    yesterday=AT-timedelta(days=1)
    with Session(engine()) as db,db.begin():
        save(db,'tushare_dc_member',[member('000100.SZ',day='20261022')],yesterday)
        save(db,'tushare_dc_member',[member('000101.SZ')],AT)
        save(db,'tushare_market_rt',[quote('000101.SZ',price='11',at=yesterday)],yesterday)
    data=tracking(client)
    assert data['tracking_member_count']==1 and data['tracking_members'][0]['code']=='000101.SZ'
    assert all(data['tracking_members'][0][key] is None for key in ['speed_5m','source_time','pct_change','speed_window_seconds'])


def test_cutoff_excludes_late_member_version_and_future_source_clock(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_dc_member'])
    earlier=AT-timedelta(minutes=5)
    with Session(engine()) as db,db.begin():
        save(db,'tushare_dc_member',[member('000100.SZ')],earlier)
        save(db,'tushare_dc_member',[member('000100.SZ'),member('000101.SZ')],AT+timedelta(minutes=1))
        save(db,'tushare_market_rt',[quote('000100.SZ',price='10',at=earlier)],earlier)
        save(db,'tushare_market_rt',[quote('000100.SZ',price='11',at=AT)],AT)
        save(db,'tushare_market_rt',[quote('000100.SZ',price='12',at=AT+timedelta(minutes=1))],AT+timedelta(seconds=5))
    data=tracking(client,suffix='&as_of_time=2026-10-23T10:30:10%2B08:00')
    assert data['tracking_member_count']==1 and data['tracking_members'][0]['pct_change']==pytest.approx(10)
    assert data['tracking_members'][0]['speed_5m']==pytest.approx(10)
    assert tracking(client)['tracking_member_count']==2


@pytest.mark.parametrize('elapsed',[269,331])
def test_tracking_reuses_strict_speed_window_without_filling_gaps(client,monkeypatch,elapsed):
    setup(client,monkeypatch,['tushare_market_rt','tushare_dc_member'])
    earlier=AT-timedelta(seconds=elapsed)
    with Session(engine()) as db,db.begin():
        save(db,'tushare_dc_member',[member('000100.SZ')])
        save(db,'tushare_market_rt',[quote('000100.SZ',price='10',at=earlier)],AT-timedelta(minutes=5))
        save(db,'tushare_market_rt',[quote('000100.SZ',price='11',at=AT)],AT)
    row=tracking(client)['tracking_members'][0]
    assert row['speed_5m'] is None and row['speed_window_seconds'] is None
    assert row['pct_change']==pytest.approx(10)


def test_tracking_cap_is_after_full_sort_and_reports_exact_count(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_dc_member'])
    monkeypatch.setattr(terminal_read,'TRACKING_MEMBER_LIMIT',3)
    earlier=AT-timedelta(minutes=5)
    codes=[f'{i:06}.SZ' for i in range(100,104)]
    with Session(engine()) as db,db.begin():
        save(db,'tushare_dc_member',[member(code) for code in codes])
        save(db,'tushare_market_rt',[quote(code,price='10',at=earlier) for code in codes],earlier)
        save(db,'tushare_market_rt',[quote(code,price='11' if code==codes[-1] else '10',at=AT) for code in codes])
    data=tracking(client)
    assert data['tracking_member_count']==4 and data['tracking_members_truncated']
    assert [row['code'] for row in data['tracking_members']]==[codes[3],codes[0],codes[1]]
    assert len(data['members'])==4  # Existing response cap is independent.


def test_tracking_projection_hard_wire_limit_keeps_tail_leader(client,monkeypatch):
    setup(client,monkeypatch,[])
    members=[TerminalStock(code=f'{i:06}.SZ',name='合成',speed_5m=0) for i in range(8001)]
    members[-1].speed_5m=2
    rows=terminal_read.tracking_projection(members)
    assert len(rows)==8000 and rows[0].code=='008000.SZ'
    assert rows[-1].code=='007998.SZ'


def test_permission_revocation_clears_tracking_and_schema_version_changes_revision(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_dc_member'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_dc_member',[member('000100.SZ')])
        save(db,'tushare_market_rt',[quote('000100.SZ')])
    before=tracking(client)
    monkeypatch.setattr(terminal_read,'DETAIL_CONTRACT_VERSION','sector-tracking-test-next')
    changed=tracking(client)
    assert changed['revision']!=before['revision'] and changed['tracking_members']==before['tracking_members']
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_dc_member'))
        provider.rights={**provider.rights,'display':'denied'}
    denied=tracking(client)
    assert denied['revision']!=changed['revision']
    assert denied['tracking_members']==[] and denied['tracking_member_count']==0 and not denied['tracking_members_truncated']


def test_sector_tracking_runtime_models_match_declared_contract(client,monkeypatch):
    setup(client,monkeypatch,[])
    spec=json.loads((Path(__file__).resolve().parents[2]/'contracts/openapi.p2.json').read_text())
    actual=app.openapi()
    for name in ['TerminalDetails','TerminalSectorMember']:
        assert spec['components']['schemas'][name]==actual['components']['schemas'][name]
    assert spec['components']['schemas']['TerminalDetails']['properties']['tracking_members']['maxItems']==8000
