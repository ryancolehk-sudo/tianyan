"""Synthetic protocol/arithmetic/queue tests; never call a live vendor."""
import copy
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session
from sqlalchemy import literal, select

from app.db import engine
from app.market import postclose as read, postclose_schedule as schedule
from app.market.review_fetch import emotion_history_page, parameters, validate_table
from app.market.review_protocol import ReviewPage
from app.market.review_store import load_dataset, persist_pages
from app.market.protocol import SourceFailure
from test_review_collection import encoded, setup_source

DAY = date(2026, 11, 6)


def at(clock='16:30:00', day=DAY):
    return datetime.fromisoformat(f'{day}T{clock}+08:00')


def history(rows=None):
    return json.dumps({'errcode': '0', 'info': rows or [
        {'Day': '2026-11-06', 'strong': '34'}, {'Day': '2026-11-05', 'strong': '83'}]}).encode()


def ctx(dataset):
    return dict(dataset=dataset, day=str(DAY), start_day='2026-11-05', scope='all')


def test_emotion_is_native_dated_history_and_keeps_zero():
    page = emotion_history_page(history([{'Day': str(DAY), 'strong': '0'}]), ctx('emotion_history'))
    assert page.rows == [{'trade_date': '20261106', 'emotion': '0'}]
    assert 'HisHomeDingPan' in page.source_url and 'Token=' not in page.source_url


@pytest.mark.parametrize('values', [
    [{'Day': str(DAY), 'strong': '101'}], [{'Day': str(DAY), 'strong': '-1'}],
    [{'Day': str(DAY), 'strong': True}], [{'Day': str(DAY), 'strong': 'NaN'}],
    [{'Day': str(DAY), 'strong': '34'}, {'Day': str(DAY), 'strong': '34'}],
    [{'Day': '2026-11-05', 'strong': '34'}], [{'Day': 'bad', 'strong': '34'}],
])
def test_invalid_or_wrong_day_emotion_never_becomes_a_close(values):
    with pytest.raises(SourceFailure):
        emotion_history_page(history(values), ctx('emotion_history'))


@pytest.mark.parametrize('dataset,api', [('stock_funds', 'moneyflow_dc'), ('market_funds', 'moneyflow_mkt_dc')])
def test_fund_requests_pin_the_exact_day(dataset, api):
    name, params, fields = parameters(ctx(dataset), offset=5000)
    assert name == api and params['trade_date'] == '20261106' and params['offset'] == 5000
    assert 'net_amount' in fields


def test_market_funds_has_no_synthetic_stock_identity_and_rejects_another_date():
    context = dict(ctx('market_funds'), start_day=str(DAY))
    row = {'trade_date': '20261106', 'net_amount': '-52500000000'}
    assert validate_table(encoded([row]), context).rows[0]['net_amount'] == '-52500000000'
    with pytest.raises(SourceFailure):
        validate_table(encoded([dict(row, trade_date='20261105')]), context)


@pytest.mark.parametrize('time_text,expected', [('16:29:59', '16:30:00'), ('16:30:00', None),
    ('21:29:59', None), ('21:30:00', '09:00:00')])
def test_1630_boundary_and_restart_deadlines(time_text, expected):
    value = schedule.deadline(DAY, at(time_text))
    assert value.time().isoformat() == expected if expected else value is None


def test_weekend_recovery_uses_original_closed_day_not_a_weekday_guess():
    assert schedule.deadline(DAY, at('10:00:00', date(2026, 11, 8))) == at('16:30:00', date(2026, 11, 8))
    assert schedule.deadline(date(2026, 11, 9), at('10:00:00', date(2026, 11, 9))) == at('16:30:00', date(2026, 11, 9))


def test_pre1630_complete_data_is_refreshed_and_1730_rechecks_late_data():
    saved = {'state': 'complete', 'row_count': 1, 'received_at': at('16:00:00').isoformat()}
    assert schedule.needs_refresh(saved, DAY, at())
    saved['received_at'] = at('16:31:00').isoformat()
    assert not schedule.needs_refresh(saved, DAY, at('16:40:00'))
    assert schedule.needs_refresh(saved, DAY, at('17:30:00'))
    assert not schedule.needs_refresh(saved, DAY, at('19:00:00', date(2026, 11, 7)))


@pytest.mark.parametrize('dataset', schedule.DATASETS + ('emotion_history',))
def test_current_closing_tables_have_a_slot_without_waiting_for_history(dataset):
    from app.models import Job
    from app.worker import lane_expression
    _, _, _, job = setup_source(dataset, target=DAY)
    with Session(engine()) as db, db.begin():
        saved = db.get(Job, job.id)
        def lane(stamp):
            return db.scalar(select(lane_expression(clock=literal(stamp))).where(Job.id == job.id))
        assert lane(at('16:29:59')) == 'recovery'
        assert lane(at('16:30:00')) == 'general'
        assert lane(at('17:30:00')) == 'general'
        assert lane(at('21:30:00')) == 'recovery'
        assert lane(at('17:00:00', DAY + timedelta(days=3))) == 'recovery'
        saved.payload = {**saved.payload, 'review_context': {**saved.payload['review_context'], 'scope': 'selected'}}
        db.flush()
        assert lane(at('17:00:00')) == 'recovery'


def test_a_single_day_emotion_archive_does_not_suppress_automatic_history_catchup():
    saved = dict(state='complete', row_count=1, received_at=at('18:00:00').isoformat(),
                 requested_start_day=str(DAY), requested_end_day=str(DAY))
    assert schedule.needs_refresh(saved, DAY, at('19:00:00', date(2026,11,8)), required_start=date(2026,10,1))
    saved['requested_start_day'] = '2026-10-01'
    assert not schedule.needs_refresh(saved, DAY, at('19:00:00', date(2026,11,8)), required_start=date(2026,10,1))


def test_automatic_overview_keeps_the_last_published_day_until_1630(monkeypatch):
    monkeypatch.setattr(read, 'available_trade_days', lambda *a, **k: [date(2026,11,5), DAY])
    assert read.default_day(object(), at('16:29:59')) == date(2026,11,5)
    assert read.default_day(object(), at()) == DAY


def test_deferred_persistent_job_is_not_a_failed_request():
    job = SimpleNamespace(payload={'review_context': {'day': str(DAY)}}, state='running', attempts=2,
                          lease_token='held', lease_until=at())
    assert schedule.defer_archive(job, at('16:29:59'))
    assert job.state == 'queued' and job.available_at == at() and job.attempts == 1
    assert job.lease_token is None and job.error_code == 'publication_window_wait'


def quote(code='600001.SH', **changes):
    return dict(ts_code=code, trade_date='20261106', close='11', pre_close='10', pct_chg='10',
                open='10', high='11', low='10', amount='100000', **changes)


def test_rank_money_units_actual_float_and_free_float_turnover_are_distinct():
    basic = dict(ts_code='600001.SH', free_share='10000', close='11', turnover_rate_f='12')
    flow = dict(ts_code='600001.SH', name='合成股票', net_amount='-1234')
    row = read.stock_rows(DAY, [quote()], [basic], [], [flow], {})[0]
    assert Decimal(row['amount_cny']) == 100000000
    assert Decimal(row['main_net_cny']) == -12340000
    assert Decimal(row['actual_float_cny']) == 1100000000
    assert row['actual_turnover_pct'] == '12'
    basic['close'] = '10'
    assert read.stock_rows(DAY, [quote()], [basic], [], [flow], {})[0]['actual_float_cny'] is None


def test_interval_returns_chain_daily_ex_date_preclose_and_do_not_fill_gaps():
    accumulators = {2: dict(stocks={}, missing_days=0, loaded_days=0)}
    read.accumulate(accumulators, 0, 2, [quote()], [])
    # Corporate action: prior close 11, next raw close/preclose 5.5/5.5.
    q = quote(); q.update(close='5.5', pre_close='5.5')
    read.accumulate(accumulators, 1, 2, [q], [])
    item = accumulators[2]['stocks']['600001.SH']
    assert (item['factor'] - 1) * 100 == Decimal('10')
    assert item['turnover_n'] == 0 and item['n'] == 2
    read.accumulate(accumulators, 1, 2, None, [])
    assert accumulators[2]['missing_days'] == 1


def test_duplicate_instruments_fail_the_universe_and_unknown_does_not_rank_as_zero():
    assert read.indexed([quote(), quote()]) is None
    panel = read.ranked([{'ts_code': 'A', 'v': None}, {'ts_code': 'B', 'v': '0'}, {'ts_code': 'C', 'v': '-1'}], 'v', 10)
    assert [r['ts_code'] for r in panel['rows']] == ['B', 'C']
    assert not read.ranked(panel['rows'], 'v', 10, ready=False)['rows']


@pytest.mark.parametrize('missing,empty_pool,turn_missing', [(False,False,False),(True,False,False),(False,True,False),(False,False,True)])
def test_full_projection_uses_verified_sessions_and_withholds_incomplete_interval(monkeypatch, missing, empty_pool, turn_missing):
    days = [DAY - timedelta(days=i) for i in reversed(range(32))]
    monkeypatch.setattr(read, 'available_trade_days', lambda *a, **k: days)
    monkeypatch.setattr(read, 'now', lambda db: at('18:00:00'))
    monkeypatch.setattr(read, 'auction_metrics', lambda *a: dict(auction_up=0, auction_down=None, auction_seal='0'))
    monkeypatch.setattr('app.market.ths_members.attach_themes', lambda *a: None)
    cutoffs = []
    def table(db, dataset, day, as_of_time=None):
        cutoffs.append(as_of_time)
        q = quote(); q['trade_date'] = day.strftime('%Y%m%d')
        data = {
            'daily': [q], 'group_basics': [dict(q, free_share='10000', turnover_rate_f='2')],
            'pools': [dict(q, limit='U', limit_times='2.0', fd_amount='0', name='合成数据')],
            'limits': [dict(q, up_limit='11', down_limit='9')],
            'emotion_history': [dict(trade_date=q['trade_date'], emotion='34')],
            'market_funds': [dict(trade_date=q['trade_date'], net_amount='-100000000')],
            'stock_funds': [dict(q, name='合成数据', net_amount='1')],
        }
        unavailable = missing and dataset == 'daily' and day == days[-15]
        if turn_missing and dataset == 'group_basics' and day == days[-25]:
            data[dataset][0]['turnover_rate_f'] = None
        rows = [] if unavailable or empty_pool and dataset == 'pools' else data[dataset]
        return dict(rows=rows, state='missing' if unavailable else 'complete', row_count=len(rows), evidence_refs=[])
    monkeypatch.setattr(read, 'load_dataset', table)
    db = SimpleNamespace(execute=lambda stmt: [])
    result = read.overview_view(db, DAY, as_of_time=at('18:00:00'))
    assert len(result['records']) == 20
    assert result['records'][-1]['values']['emotion'] == '34'
    assert result['records'][-1]['values']['main_net'] == '-100000000'
    assert result['records'][-1]['values']['height'] == (None if empty_pool else 2)
    if empty_pool:
        assert result['records'][-1]['values']['limit_up'] is None
        assert result['rankings']['limit_up']['state'] == 'unavailable'
    assert result['records'][-1]['values']['auction_down'] is None
    assert result['rankings']['5']['state'] == 'ready'
    if missing:
        assert result['rankings']['20']['state'] == 'unavailable' and not result['rankings']['30']['rows']
    else:
        row = result['rankings']['30']['rows'][0]
        assert Decimal(row['interval_pct']).quantize(Decimal('.01')) == ((Decimal('1.1')**30 - 1)*100).quantize(Decimal('.01'))
        assert row['interval_turnover_pct'] == (None if turn_missing else '60')
        if turn_missing:
            assert '29/30' in row['field_reasons']['interval_turnover_pct']
            assert 'interval_turnover_pct' not in result['rankings']['5']['rows'][0]['field_reasons']
        assert row['main_net_cny'] == '10000'
    assert set(cutoffs) == {at('18:00:00')}


def test_native_auction_and_classification_refs_are_openable_from_coverage():
    reader = read.References(object(), at(), None)
    for index, dataset in enumerate(('market.duanxianxia.auction.summary', 'tushare.ths_member')):
        reader.record_input(SimpleNamespace(id=str(index), content_hash='verified-hash',
            source_id='provider', received_at=at(), payload=dict(dataset=dataset,
            observed_day=str(DAY), rows=[dict(trade_day=str(DAY))])))
    assert len(reader.coverage) == 2
    for key, entry in reader.coverage.items():
        assert entry['trade_day'] == str(DAY) and entry['row_count'] == 1
        assert entry['evidence_refs'] == [reader.refs[key]]
        assert entry['evidence_refs'][0]['content_hash'] == 'verified-hash'


@pytest.mark.parametrize('rows,observed,scope,expected_day,count',[
    ([dict(trade_day='2026-11-05',trade_date='2026-11-04'),{}],'2026-11-06','原始范围','2026-11-05',2),
    ([dict(trade_date='2026-11-04')],'2026-11-06','','2026-11-04',1),
    ([], '2026-11-06',None,'2026-11-06',0),
    (None,None,None,None,0),
])
def test_reference_metadata_keeps_original_date_scope_and_empty_rows(rows,observed,scope,expected_day,count):
    cold=read.References(object(),at(),None)
    warm=read.References(object(),at(),None)
    common=dict(id='synthetic-ref',content_hash='verified-hash',source_id='provider',received_at=at())
    cold.record_input(SimpleNamespace(**common,payload=dict(dataset='synthetic.reference',
        rows=rows,observed_day=observed,scope=scope)))
    warm.record_input(SimpleNamespace(**common,reference_summary=dict(dataset='synthetic.reference',
        trade_day=expected_day,scope=scope,row_count=count)))
    assert cold.refs==warm.refs
    assert cold.coverage==warm.coverage
    item=warm.coverage['synthetic-ref']
    assert item['trade_day']==expected_day and item['row_count']==count
    assert item['scope']==(scope or '来源原始归档；分类采用所选日以前已接收的完整采集批次。')


def test_schedule_restarts_do_not_duplicate_pending_jobs(client, monkeypatch):
    from app.models import Job
    _, _, _, _ = setup_source('daily', target=DAY)
    monkeypatch.setattr(schedule, 'available_trade_days', lambda *a, **k: [DAY])
    monkeypatch.setattr(schedule, 'load_dataset', lambda *a, **k: dict(state='missing'))
    calls = []
    def queue(db, owner, dataset, day, **kwargs):
        from app.market.review_protocol import DATASET_FAMILIES
        from app.market.review_store import family_id
        calls.append(dataset)
        db.add(Job(owner_id=owner.id, kind='review.collect', state='queued', payload={
            'source_id': str(family_id(DATASET_FAMILIES[dataset])),
            'review_context': dict(dataset=dataset, day=str(day), scope='all')}))
    monkeypatch.setattr('app.market.review_jobs.queue_dataset', queue)
    # setup_source's fixture job is already running, so a restart reuses it.
    with Session(engine()) as db, db.begin():
        assert schedule.queue_due(db, at()) == 0
    assert calls == []


def test_atomic_history_manifest_filters_each_original_source_day_and_cutoff(client):
    _, provider, config, job = setup_source('emotion_history', target=DAY, start=date(2026, 11, 5))
    receipt = datetime.now(UTC)
    page = emotion_history_page(history(), ctx('emotion_history'))
    with Session(engine()) as db, db.begin():
        persist_pages(db, db.merge(provider), db.merge(config), db.merge(job), [(page, receipt)])
    with Session(engine()) as db:
        assert load_dataset(db, 'emotion_history', DAY)['rows'][0]['emotion'] == '34'
        old = load_dataset(db, 'emotion_history', date(2026, 11, 5))
        assert old['rows'] == [{'trade_date': '20261105', 'emotion': '83'}]
        assert old['evidence_refs'][0]['selected_rows'] == [1]
        assert load_dataset(db, 'emotion_history', DAY, as_of_time=receipt-timedelta(seconds=1))['state'] == 'missing'


def test_overview_endpoint_is_authenticated_and_read_only(client, monkeypatch):
    from test_data_sources import login
    from app.models import Job
    from sqlalchemy import func, select
    assert client.get('/api/v1/review-data/overview').status_code == 401
    login(client, role='researcher')
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: pytest.fail('GET must not fetch'))
    with Session(engine()) as db:
        before = db.scalar(select(func.count()).select_from(Job))
    response = client.get('/api/v1/review-data/overview?trade_day=2026-11-06')
    assert response.status_code == 200
    assert response.json()['data']['records'] == []
    assert client.get('/api/v1/review-data/overview?lookback=999').status_code == 422
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == before
