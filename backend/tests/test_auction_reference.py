"""Stage replacement, receipt persistence and same-source evidence boundaries."""
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.auction_reference import summarize
from app.market.auction_source import FAMILY, HISTORY_FAMILY
from app.market.service import source_id
from app.models import Provider
from test_terminal_auction_live import at, prepare, seal_batch
from test_auction_tables import configure_family


def setup_reference(client, monkeypatch, clock='2026-10-26T09:29:00'):
    headers = prepare(client, monkeypatch, clock=clock)
    monkeypatch.setattr('app.market.auction_reference.now', lambda db: at(clock))
    return headers


def read(client, **params):
    response = client.get('/api/v1/auction-reference', params=params)
    assert response.status_code == 200, response.text
    return response.json()['data']


def test_reference_requires_login(client):
    assert client.get('/api/v1/auction-reference').status_code == 401


def test_each_new_stage_replaces_the_previous_without_exposing_future_columns(client, monkeypatch):
    setup_reference(client, monkeypatch)
    assert read(client)['report'] is None
    seal_batch(at('2026-10-26T09:15:11'), cells=('10亿', '12亿', '20亿'))
    first = read(client)['report']
    assert first['stage'] == '09:15'
    assert first['totals'] == {'09:15':'420000000', '09:20':None, '09:25':None}
    assert first['strongest'][0]['node_amounts'] == {'09:15':'1000000000', '09:20':None, '09:25':None}
    assert first['total_change_cny'] is None and first['additions'] == first['reductions'] == []
    seal_batch(at('2026-10-26T09:20:17'), cells=('10亿', '12亿', '20亿'))
    second = read(client)['report']
    assert second['stage'] == '09:20' and second['totals']['09:25'] is None
    assert second['total_change_cny'] == '290000000'
    assert second['additions'][0]['change_cny'] == '200000000'
    seal_batch(at('2026-10-26T09:25:06'), cells=('10亿', '12亿', '20亿'))
    final = read(client)['report']
    assert final['stage'] == '09:25' and final['total_change_cny'] == '50000000'
    assert final['strongest'][0]['seal_cny'] == '2000000000'
    assert final['ready_for_trade'] is False
    seal_batch(at('2026-10-26T09:25:36'), cells=('10亿', '12亿', '90亿'))
    assert read(client)['report'] == final  # A bulletin is stable inside its stage.


def test_close_weekend_restart_and_next_day_before_first_success_retain_last_bulletin(client, monkeypatch):
    setup_reference(client, monkeypatch, '2026-11-07T18:00:00')
    seal_batch(at('2026-11-06T09:25:06'), day='2026-11-06')
    final = read(client)['report']
    assert final['trade_day'] == '2026-11-06' and final['stage'] == '09:25'
    # New sessions/GETs reconstruct the same immutable record, no memory timer.
    with Session(engine()) as db:
        count = db.scalar(select(func.count()).select_from(EvidenceVersion))
    assert read(client)['report'] == final
    monkeypatch.setattr('app.market.auction_reference.now', lambda db: at('2026-11-09T09:16:00'))
    assert read(client)['report'] == final
    seal_batch(at('2026-11-09T09:15:12'), day='2026-11-09', cells=('11亿','20亿','30亿'))
    current = read(client)['report']
    assert current['trade_day'] == '2026-11-09' and current['stage'] == '09:15'
    assert current['totals']['09:20'] is None
    with Session(engine()) as db:
        after_write = db.scalar(select(func.count()).select_from(EvidenceVersion))
    read(client)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(EvidenceVersion)) == after_write
    assert after_write > count


def test_same_day_recovery_is_labelled_and_never_visible_before_receipt(client, monkeypatch):
    headers = setup_reference(client, monkeypatch, '2026-11-09T18:00:00')
    configure_family(client, headers, HISTORY_FAMILY)
    seal_batch(at('2026-11-06T09:25:06'), day='2026-11-06')
    final = read(client)['report']
    seal_batch(at('2026-11-09T11:34:06'), day='2026-11-09')
    recovered = read(client)['report']
    assert recovered['trade_day'] == '2026-11-09'
    assert recovered['stage'] == '09:25' and recovered['recovered'] is True
    from datetime import datetime
    assert datetime.fromisoformat(recovered['received_at']) == at('2026-11-09T11:34:06')
    assert read(client, as_of_time='2026-11-09T11:34:05+08:00')['report'] == final
    seal_batch(at('2026-11-09T09:25:06'), day='2026-11-06')
    seal_batch(at('2026-11-09T09:25:16'), day='2026-11-09', kind='history')
    assert read(client)['report'] == recovered


@pytest.mark.parametrize('broken', ['missing_summary', 'mismatched_hash'])
def test_unpaired_batches_cannot_replace_a_good_stage(client, monkeypatch, broken):
    setup_reference(client, monkeypatch)
    seal_batch(at('2026-10-26T09:20:17'), cells=('10亿','12亿','20亿'))
    previous = read(client)['report']
    seal_batch(at('2026-10-26T09:25:06'), broken=broken)
    assert read(client)['report'] == previous


def test_cutoffs_and_date_navigation_are_isolated_from_latest_stage(client, monkeypatch):
    setup_reference(client, monkeypatch)
    seal_batch(at('2026-10-23T09:25:06'), day='2026-10-23')
    seal_batch(at('2026-10-26T09:15:11'))
    seal_batch(at('2026-10-26T09:25:06'))
    assert read(client)['report']['stage'] == '09:25'
    early = read(client, as_of_time='2026-10-26T09:20:00+08:00')['report']
    assert early['stage'] == '09:15' and early['totals']['09:25'] is None
    assert read(client, trade_day='2026-10-23')['report']['trade_day'] == '2026-10-23'
    assert read(client)['report']['stage'] == '09:25'


def test_permission_revocation_clears_an_already_stored_report(client, monkeypatch):
    setup_reference(client, monkeypatch)
    seal_batch(at('2026-10-26T09:25:06'))
    assert read(client)['report'] is not None
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id(FAMILY))
        provider.rights = {**provider.rights, 'display':'denied'}
    result = read(client)
    assert result['state'] == 'unavailable' and result['report'] is None


def summary(rows, **header):
    base = dict(trade_day='2026-10-26', table_kind='current', row_count=str(len(rows)), batch_hash='a'*64,
                total_fd_0915_cny='100', total_fd_0920_cny='200', total_fd_0925_cny='300')
    normalized = [dict(trade_day='2026-10-26', batch_hash='a'*64, name=f'合成{i}', code=f'{600100+i:06}', **row)
                  for i, row in enumerate(rows)]
    return summarize({**base, **header}, normalized, stage='09:25', received_at=at('2026-10-26T09:25:06'),
                     source_name='合成封单', evidence_ids=[uuid4(),uuid4()])


def test_missing_is_not_withdrawal_zero_is_real_and_identical_themes_are_one_group():
    report = summary([
        dict(fd_0920_cny='50', fd_0925_cny='0', tags_raw='机器人|AI|3板'),
        dict(fd_0920_cny='60', fd_0925_cny=None, tags_raw='机器人|AI|2板'),
        dict(fd_0920_cny=None, fd_0925_cny='20', tags_raw='机器人|AI|首板'),
    ])
    assert report.valid_count == 2
    assert len(report.reductions) == 1 and report.reductions[0].change_cny == '-50'
    assert report.strongest[0].change_cny is None and report.additions == []
    assert len(report.themes) == 1 and report.themes[0].name == '机器人/AI'
    assert report.themes[0].total_cny == '20'


def test_missing_current_totals_never_become_an_empty_strength_verdict():
    assert summary([dict(fd_0925_cny='20', tags_raw='机器人')], total_fd_0925_cny=None) is None
