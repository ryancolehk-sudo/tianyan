"""Targeted read against isolated PostgreSQL; no collection or heavy projections."""
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.main import app
from app.market import sector_membership
from app.market.models import DataFetchRun
from app.market.service import source_id
from app.models import Job, Provider
from test_terminal_sector_tracking import member, SECTOR
from test_terminal_workbench import AT, DAY, REVIEW, save, setup


def prepare(client, monkeypatch, families=('tushare_dc_member',)):
    monkeypatch.setattr('app.market.service.now', lambda db: AT-timedelta(days=1))
    setup(client, monkeypatch, families)
    monkeypatch.setattr(sector_membership, 'now', lambda db: REVIEW)
    monkeypatch.setattr('app.market.terminal_read.workbench_view', lambda *a, **k: pytest.fail('no heavy read'))
    monkeypatch.setattr('app.market.collect.request_tushare', lambda *a, **k: pytest.fail('no upstream GET'))


def read(client, suffix='', sector=SECTOR):
    response = client.get(f'/api/v1/sector-membership?day={DAY}&sector={sector}'+suffix)
    assert response.status_code == 200, response.text
    return response.json()['data']


def test_authenticated_targeted_read_has_no_collection_or_quote_work(client, monkeypatch):
    assert client.get(f'/api/v1/sector-membership?day={DAY}&sector={SECTOR}').status_code == 401
    prepare(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_dc_member', [member('000100.SZ'), member('000101.SZ', sector='BK1185.DC'),
            member('000102.SZ', day='20261022')])
    data = read(client)
    assert data['state'] == 'available' and data['members_trade_day'] == str(DAY)
    assert data['members'] == [{'code': '000100.SZ', 'name': '合成成分000100.SZ'}]
    assert data['member_count'] == 1 and not data['truncated']
    assert read(client, sector='BK1185.DC')['members'][0]['code'] == '000101.SZ'
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 0


def test_full_membership_is_not_limited_by_quote_priority(client, monkeypatch):
    prepare(client, monkeypatch)
    codes = [f'{i:06}.SZ' for i in range(100, 1700)]
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_dc_member', [member(code) for code in reversed(codes)])
    data = read(client)
    assert data['member_count'] == 1600 and [row['code'] for row in data['members']] == codes


def test_latest_source_version_and_receipt_cutoff(client, monkeypatch):
    prepare(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_dc_member', [member('000100.SZ')], AT)
        save(db, 'tushare_dc_member', [member('000101.SZ')], AT+timedelta(minutes=1))
    assert read(client)['members'][0]['code'] == '000101.SZ'
    for field in ['as_of_time', 'observation_cutoff']:
        value = AT.isoformat().replace('+', '%2B')
        assert read(client, f'&{field}={value}')['members'][0]['code'] == '000100.SZ'


def test_prior_day_membership_only_for_current_non_replay_context(client, monkeypatch):
    prepare(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_dc_member', [member('000100.SZ', day='20261022')])
    assert read(client)['state'] == 'missing'
    monkeypatch.setattr(sector_membership, 'now', lambda db: AT)
    data = read(client)
    assert data['members_trade_day'] == '2026-10-22'
    assert read(client, '&as_of_time='+AT.isoformat().replace('+', '%2B'))['state'] == 'missing'


def test_permission_revocation_precedes_data_read(client, monkeypatch):
    prepare(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_dc_member', [member('000100.SZ')])
    assert read(client)['members']
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('tushare_dc_member'))
        provider.rights = {**provider.rights, 'display': 'denied'}
    denied = read(client)
    assert denied['state'] == 'permission_denied' and denied['members'] == []


def test_unconfigured_and_missing_stay_distinct(client, monkeypatch):
    prepare(client, monkeypatch, ())
    assert read(client)['state'] == 'permission_denied'


def test_only_complete_archive_for_exact_board_and_day_is_used(client, monkeypatch):
    prepare(client, monkeypatch)
    calls = []
    def archived(db, dataset, day, *, scope, as_of_time):
        calls.append((dataset, day, scope, as_of_time))
        return {'state': 'partial' if scope == SECTOR else 'complete', 'received_at': AT.isoformat(),
            'rows': [member('000100.SZ'), member('000101.SZ', sector='BK1185.DC'),
                member('000102.SZ', day='20261022')]}
    monkeypatch.setattr(sector_membership, 'load_dataset', archived)
    result = read(client)
    assert [row['code'] for row in result['members']] == ['000100.SZ']
    assert [call[2] for call in calls] == [SECTOR, 'all']
    assert all(call[3] == REVIEW for call in calls)


@pytest.mark.parametrize('suffix', [
    '&as_of_time=2026-10-25T02:00:00Z',
    '&as_of_time=2026-10-24T02:00:00Z&observation_cutoff=2026-10-24T02:00:00Z',
])
def test_future_and_conflicting_cutoffs_rejected(client, monkeypatch, suffix):
    prepare(client, monkeypatch)
    assert client.get(f'/api/v1/sector-membership?day={DAY}&sector={SECTOR}'+suffix).status_code == 422


def test_runtime_contract_matches_declared(client, monkeypatch):
    prepare(client, monkeypatch)
    spec = json.loads((Path(__file__).resolve().parents[2]/'contracts/openapi.p2.json').read_text())
    runtime = app.openapi()
    for name in ['SectorMembership', 'SectorMembershipEntry', 'Envelope_SectorMembership_']:
        assert spec['components']['schemas'][name] == runtime['components']['schemas'][name]
    assert spec['paths']['/api/v1/sector-membership'] == runtime['paths']['/api/v1/sector-membership']
