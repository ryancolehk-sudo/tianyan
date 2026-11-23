"""Real disposable PostgreSQL, synthetic public-flow samples, no HTTP."""
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collect import store_reference_batch
from app.market.eastmoney_trading import parse_ranking
from app.market.intraday_funds import intraday_funds
from app.market.models import DataFetchRun, SourceSettings
from app.market.reference import ReferenceBatch
from app.market.service import source_id
from app.models import Job, Provider
from test_data_sources import configure, configuration, login

DAY = date(2026, 10, 23)
AT = datetime(2026, 10, 23, 2, 30, tzinfo=UTC)


def test_previous_sample_index_preserves_window_source_category_and_receipt_ties():
    from app.market.intraday_funds import Sample, Snapshot, _previous, _previous_index

    def snapshot(seconds, *, receipt=0, source='same', category='concept'):
        row = Sample('BK0001.DC', '合成板块', AT - timedelta(seconds=seconds), Decimal(1), None, None)
        return Snapshot(SimpleNamespace(source_id=source, received_at=AT + timedelta(seconds=receipt)),
                        category, {row.code: row})

    current = snapshot(0)
    wrong_source = snapshot(300, source='other', receipt=100)
    wrong_category = snapshot(300, category='industry', receipt=100)
    early, late = snapshot(330), snapshot(270)
    out_early, out_late = snapshot(331), snapshot(269)
    candidates = [wrong_source, wrong_category, early, late, out_early, out_late]
    index = _previous_index(candidates)
    # Both interval edges count. With equal distance, the later source clock wins.
    assert _previous(current.rows['BK0001.DC'], current, candidates, index=index)[0] is late
    older, newer, same_tie = snapshot(300), snapshot(300, receipt=1), snapshot(300, receipt=1)
    candidates.extend([older, newer, same_tie])
    index = _previous_index(candidates)
    # Receipt corrections beat an older receipt; an exact tie keeps input order.
    assert _previous(current.rows['BK0001.DC'], current, candidates, index=index)[0] is newer
    assert _previous(current.rows['BK0001.DC'], current, [wrong_source, wrong_category, out_early, out_late]) is None


def test_previous_sample_index_cannot_bridge_the_lunch_break():
    from app.market.intraday_funds import Sample, Snapshot, _previous, _previous_index

    def snapshot(stamp):
        row = Sample('BK0001.DC', '合成板块', stamp, Decimal(1), None, None)
        return Snapshot(SimpleNamespace(source_id='same', received_at=stamp), 'concept', {row.code: row})

    current = snapshot(AT.replace(hour=5, minute=0))
    lunch = snapshot(current.rows['BK0001.DC'].stamp - timedelta(minutes=5))
    morning = snapshot(AT.replace(hour=3, minute=30))
    assert _previous(current.rows['BK0001.DC'], current, [lunch, morning],
                     index=_previous_index([lunch, morning])) is None


def test_bounded_previous_index_keeps_independent_current_clocks_and_receipt_ties():
    from app.market.intraday_funds import Sample, Snapshot, _previous, _previous_index

    def snapshot(code, seconds, *, source='same', category='concept', receipt=0):
        row = Sample(code, '合成板块', AT - timedelta(seconds=seconds), Decimal(1), None, None)
        return Snapshot(SimpleNamespace(source_id=source, received_at=AT + timedelta(seconds=receipt)),
                        category, {code: row})

    current = [snapshot('BK0001.DC', 0), snapshot('BK0002.DC', 120),
               snapshot('BK0001.DC', 600, source='other', category='industry')]
    candidates = [snapshot('BK0001.DC', n) for n in (0, 269, 270, 300, 330, 331, 3600)]
    correction = snapshot('BK0001.DC', 300, receipt=1)
    candidates += [correction, snapshot('BK0001.DC', 300, receipt=1),
                   snapshot('BK0002.DC', 450), snapshot('BK0002.DC', 389),
                   snapshot('BK0001.DC', 900, source='other', category='industry'),
                   snapshot('BK9999.DC', 300)]
    bounded = _previous_index(candidates, current=current)
    whole = _previous_index(candidates)
    for now in current:
        row = next(iter(now.rows.values()))
        assert _previous(row, now, candidates, index=bounded) == _previous(row, now, candidates, index=whole)
    assert _previous(current[0].rows['BK0001.DC'], current[0], candidates, index=bounded)[0] is correction
    assert _previous(current[1].rows['BK0002.DC'], current[1], candidates, index=bounded)[1].stamp == AT - timedelta(seconds=450)
    assert sum(len(entries) for _, entries in bounded.values()) == 7
    assert _previous_index(candidates, current=[]) == {}


def setup(client, family='eastmoney_funds'):
    headers = login(client)
    config = configuration(family)
    config['interval_seconds'] = 150
    configure(client, headers, config, key='synthetic-' + family)


def save(db, rows, stamp=AT, *, category='concept', received=None, family='eastmoney_funds',
         units=None):
    received = received or stamp
    payload = []
    for code, net in rows:
        payload.append({'f12': code, 'f13': 1 if category == 'stock' else 90,
            'f14': '合成' + code, 'f3': '1.25', 'f6': '2000', 'f62': net,
            'f124': int(stamp.timestamp())})
    dataset = 'market.eastmoney.' + category + '_funds'
    table = parse_ranking(json.dumps({'rc': 0, 'data': {'total': len(payload), 'diff': payload}}).encode(),
                          received, dataset, '合成资金样本')
    if units:
        table = table.model_copy(update={'columns': [column.model_copy(update={'unit': units.get(column.key, column.unit)})
                                                     for column in table.columns]})
    provider = db.get(Provider, source_id(family))
    return store_reference_batch(db, provider, db.get(SourceSettings, provider.id),
        ReferenceBatch(records=[(dataset, table, None)], expected=1, requests=1), received)


def read(cutoff=AT, **kwargs):
    with Session(engine()) as db:
        return intraday_funds(db, DAY, cutoff, session={'phase': 'trading'}, **kwargs)


def test_old_single_page_shows_actual_and_expected_counts_without_claiming_full_market(client):
    setup(client)
    with Session(engine()) as db, db.begin():
        provider=db.get(Provider,source_id('eastmoney_funds'))
        for category,total in [('stock',5927),('industry',128),('concept',531)]:
            raw=dict(rc=0,data=dict(total=total,diff=[dict(f12='600000' if category=='stock' else 'BK1046' if category=='industry' else 'BK2000',
                f13=1 if category=='stock' else 90,f14='synthetic only',f62='0',f124=int(AT.timestamp()))]))
            dataset='market.eastmoney.'+category+'_funds'
            table=parse_ranking(json.dumps(raw).encode(),AT,dataset,'synthetic',industry_level=2 if category=='industry' else None)
            store_reference_batch(db,provider,db.get(SourceSettings,provider.id),ReferenceBatch(records=[(dataset,table,None)],expected=1,requests=1),AT)
    value=read()
    assert value.state=='stale' and len(value.stocks)==1 and len(value.sectors)==2
    coverage={row.category:row for row in value.coverage}
    for category,total in [('stock',5927),('industry',128),('concept',531)]:
        item=coverage[category]
        assert item.represented_count==item.latest_batch_count==1 and item.expected_count==total
        assert item.complete is False and item.source_time==AT and item.received_at==AT


def test_stock_history_window_retains_real_predecessors_and_late_old_receipts(client):
    setup(client)
    with Session(engine()) as db,db.begin():
        for n in range(20):
            save(db,[('600001','999')],AT-timedelta(seconds=600+n*10),category='stock')
        save(db,[('600001','10')],AT-timedelta(seconds=300),category='stock')
        save(db,[('600001','50')],AT,category='stock')
        save(db,[('600001','5')],AT-timedelta(seconds=240),received=AT+timedelta(seconds=3),category='stock')
    row=read(AT+timedelta(seconds=3)).stocks[0]
    assert row.code=='600001.SH' and row.value==50 and row.change_5m_cny==40 and row.source_time==AT


def test_independent_old_stock_clock_keeps_its_own_saved_previous_window(client):
    setup(client)
    with Session(engine()) as db,db.begin():
        save(db,[('600002','1')],AT-timedelta(seconds=2300),category='stock')
        # A supplied table can contain independent per-stock clocks. Keep both
        # originals; the latest stock's clock cannot erase the older one's pair.
        dataset='market.eastmoney.stock_funds'
        table=parse_ranking(json.dumps(dict(rc=0,data=dict(total=2,diff=[
            dict(f12='600001',f13=1,f14='synthetic current',f62='50',f124=int(AT.timestamp())),
            dict(f12='600002',f13=1,f14='synthetic independent clock',f62='5',f124=int((AT-timedelta(seconds=2000)).timestamp())),
        ]))).encode(),AT,dataset,'synthetic')
        provider=db.get(Provider,source_id('eastmoney_funds'))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),ReferenceBatch(records=[(dataset,table,None)],expected=1,requests=1),AT)
    rows={row.code:row for row in read().stocks}
    assert rows['600001.SH'].value==50
    assert rows['600002.SH'].value==5 and rows['600002.SH'].change_5m_cny==4


def test_absent_feed_and_empty_saved_day_never_substitute_daily_data(client, monkeypatch):
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: pytest.fail('GET must not collect'))
    assert read().state == 'missing'
    setup(client)
    result = read()
    assert result.state == 'missing' and result.sectors == [] and result.points == []
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 0


def test_signed_zero_null_and_units_remain_distinct(client):
    setup(client)
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '0'), ('BK0002', None), ('BK0003', '-12.5')])
    result = read()
    values = {row.code: row for row in result.sectors}
    assert result.state == 'available' and values['BK0001.DC'].main_net_cny == 0
    assert values['BK0002.DC'].main_net_cny is None
    assert values['BK0003.DC'].main_net_cny == -12.5
    assert all(row.change_5m_cny is None and row.rank_change_5m is None for row in result.sectors)
    assert values['BK0001.DC'].pct_change == 1.25 and values['BK0001.DC'].amount_cny == 2000
    assert '实际返回' in result.scope and '不代表完整市场' in result.scope
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '200')], AT + timedelta(seconds=1), units={'net_inflow_cny': '万元'})
    assert read(AT + timedelta(seconds=2)).sectors[0].main_net_cny is None


def test_two_saved_same_segment_samples_compute_cash_and_real_rank_changes(client):
    setup(client)
    before = AT - timedelta(minutes=5)
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '10'), ('BK0002', '30')], before)
        save(db, [('BK0001', '50'), ('BK0002', '20')])
        save(db, [('600000', '1')], before, category='stock')
        save(db, [('600000', '6')], category='stock')
    result = read()
    values = {row.code: row for row in result.sectors}
    assert [row.code for row in result.sectors] == ['BK0001.DC', 'BK0002.DC']
    assert values['BK0001.DC'].change_5m_cny == 40 and values['BK0001.DC'].rank_change_5m == 1
    assert values['BK0002.DC'].change_5m_cny == -10 and values['BK0002.DC'].rank_change_5m == -1
    assert result.stocks[0].code == '600000.SH' and result.stocks[0].change_5m_cny == 5
    assert [point.time for point in result.points] == [before, before, AT, AT]


def test_changed_ranking_sample_scope_hides_rank_change_but_keeps_valid_cash_change(client):
    setup(client)
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '10'), ('BK0002', '30')], AT - timedelta(minutes=5))
        save(db, [('BK0001', '50'), ('BK0003', '20')])
    values = {row.code: row for row in read().sectors}
    assert values['BK0001.DC'].change_5m_cny == 40
    assert values['BK0003.DC'].change_5m_cny is None
    assert all(row.rank_change_5m is None for row in values.values())


@pytest.mark.parametrize('elapsed', [269, 331, 60, 240, 360])
def test_missing_five_minute_window_never_rescales_other_intervals(client, elapsed):
    setup(client)
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '10')], AT - timedelta(seconds=elapsed))
        save(db, [('BK0001', '50')])
    row = read().sectors[0]
    assert row.change_5m_cny is None and row.rank_change_5m is None


def test_lunch_and_day_boundaries_never_generate_five_minute_changes(client):
    setup(client)
    lunch_end = AT.replace(hour=5, minute=0)
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '10')], lunch_end - timedelta(minutes=5))
        save(db, [('BK0001', '50')], lunch_end)
        save(db, [('BK0002', '999')], AT - timedelta(days=1))
    result = read(lunch_end)
    assert len(result.sectors) == 1 and result.sectors[0].change_5m_cny is None
    assert all(point.time.date() == DAY for point in result.points)
    assert all(point.sector_code != 'BK0002.DC' for point in result.points)


def test_receipt_cutoff_source_future_and_late_older_sample_are_independent(client):
    setup(client)
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '10')], AT - timedelta(minutes=5))
        save(db, [('BK0001', '50')], received=AT + timedelta(seconds=2))
    assert read().sectors[0].main_net_cny == 10
    assert read(AT + timedelta(seconds=2)).sectors[0].main_net_cny == 50
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '5')], AT - timedelta(minutes=4), received=AT + timedelta(seconds=3))
    assert read(AT + timedelta(seconds=3)).sectors[0].main_net_cny == 50
    with Session(engine()) as db, db.begin():
        save(db, [('BK0002', '999')], AT + timedelta(seconds=30), received=AT)
    assert [row.code for row in read(AT + timedelta(seconds=3)).sectors] == ['BK0001.DC']
    assert [row.code for row in read(AT + timedelta(seconds=31)).sectors] == ['BK0002.DC']


@pytest.mark.parametrize('revoke', ['disabled', 'display', 'cache', 'entitlement'])
def test_current_permission_revocation_hides_all_cached_samples(client, revoke):
    setup(client)
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '10')])
    assert read().sectors
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('eastmoney_funds'))
        if revoke == 'disabled':
            provider.enabled = False
        elif revoke == 'entitlement':
            provider.entitlement_status = 'unknown'
        else:
            provider.rights = {**provider.rights, revoke: 'denied'}
    manifest = []
    result = read(input_manifest=manifest)
    assert result.state == 'permission_denied'
    assert result.sectors == [] and result.points == [] and result.stocks == [] and manifest == []


def test_other_provider_batches_cannot_fill_missing_eastmoney_funds(client):
    setup(client, 'eastmoney_boards')
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '10')], family='eastmoney_boards')
    assert read().state == 'missing'


def test_curves_keep_selected_outside_top_ten_and_manifest_only_real_receipts(client):
    setup(client)
    old = AT - timedelta(minutes=5)
    entries = [('BK' + str(i).zfill(4), str(i)) for i in range(1, 13)]
    with Session(engine()) as db, db.begin():
        save(db, entries, old)
        save(db, entries)
    manifest = []
    result = read(sector='BK0001.DC', input_manifest=manifest)
    plotted = {point.sector_code for point in result.points}
    assert 'BK0001.DC' in plotted and 'BK0002.DC' not in plotted and len(plotted) == 11
    assert len(result.points) == 22 and len(manifest) == 2
    with Session(engine()) as db:
        expected = {str(row.id): row for row in db.scalars(select(EvidenceVersion).where(
            EvidenceVersion.source_id == source_id('eastmoney_funds')))}
    assert {item['evidence_id'] for item in manifest} == set(expected)
    assert all(item['content_hash'] == expected[item['evidence_id']].content_hash for item in manifest)


def test_lunch_keeps_morning_close_available_and_old_morning_sample_is_stale(client):
    setup(client)
    close = AT.replace(hour=3, minute=30)
    lunch = AT.replace(hour=4, minute=20)
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '10')], close)
    with Session(engine()) as db:
        result = intraday_funds(db, DAY, lunch, session={'phase': 'break'})
    assert result.state == 'available' and result.latest_source_time == close
    assert read(close + timedelta(minutes=8)).state == 'stale'


def test_fresh_stock_sample_cannot_hide_stale_board_ranking(client):
    setup(client)
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '10')], AT - timedelta(minutes=8))
        save(db, [('600000', '20')], category='stock')
    result = read()
    assert result.state == 'stale' and result.latest_source_time == AT


@pytest.mark.parametrize('reason', ['paused', 'error', 'unavailable', 'empty'])
def test_new_collection_failure_or_pause_retains_but_marks_saved_sample_stale(client, reason):
    setup(client)
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '10')])
        if reason == 'paused':
            db.get(SourceSettings, source_id('eastmoney_funds')).paused_reason = 'schema_changed'
        else:
            db.add(DataFetchRun(source_id=source_id('eastmoney_funds'), capability='market.eastmoney.funds',
                started_at=AT + timedelta(seconds=1), received_at=AT + timedelta(seconds=2),
                status=reason, reason='transport_error' if reason != 'empty' else None,
                expected=None, accepted=None, rejected=None, duplicates=None, elapsed_ms=1,
                request_count=1, cost_cny=None))
    result = read(AT + timedelta(seconds=3))
    assert result.state == 'stale' and result.sectors[0].main_net_cny == 10
    if reason != 'paused':
        assert read().state == 'available'  # A later failure cannot leak through a historical cutoff.


def test_real_lunch_source_clock_is_kept_and_does_not_become_a_five_minute_trade_change(client):
    setup(client)
    noon = AT.replace(hour=4, minute=0)
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '10')], noon - timedelta(minutes=5))
        save(db, [('BK0001', '20')], noon)
    with Session(engine()) as db:
        result = intraday_funds(db, DAY, noon, session={'phase': 'break'})
    assert result.state == 'available' and result.latest_source_time == noon
    assert result.sectors[0].main_net_cny == 20 and result.sectors[0].change_5m_cny is None
    assert result.points[-1].time == noon
