"""Direction data semantics on disposable PostgreSQL, with synthetic source rows."""
import json
import pytest
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_data_sources import configure, login
from test_terminal_workbench import calendar, save
from test_tushare import source

from app.config import settings
from app.db import engine
from app.market.collect import store_calendar_batch, store_reference_batch
from app.market.direction_read import direction_view
from app.market.direction_sources import parse
from app.market.models import DataFetchRun, SourceSettings
from app.market.service import source_id
from app.market.tushare import parameters
from app.models import Job, Provider

DAY = date(2026, 10, 23)
NOW = datetime(2026, 10, 26, 8, tzinfo=UTC)
ALL = ('tushare_calendar_sse', 'tushare_calendar_szse', 'tushare_dc_index',
       'tushare_moneyflow_ind_dc', 'tushare_direction_industry', 'tushare_direction_industry_funds',
       'direction_ths_hot', 'direction_tdx_hot', 'direction_em_hot', 'direction_kpl_strength')


def setup(client, monkeypatch):
    headers = login(client)
    # The source authorization must precede this scenario's synthetic cutoff,
    # regardless of the actual wall time when the regression suite runs.
    with monkeypatch.context() as config_clock:
        config_clock.setattr('app.market.service.now', lambda db: NOW - timedelta(days=1))
        for family in ALL:
            config = source(family) if family.startswith('tushare_') else dict(family=family,
                symbols=[], interval_seconds=300, enabled=True, entitlement_status='verified',
                rights_basis='Synthetic local-only ranking test authorization.',
                rights={'display': 'allowed', 'cache': 'allowed', 'export': 'denied', 'redistribute': 'denied'})
            configure(client, headers, config, key=family)
    monkeypatch.setattr('app.market.direction_read.now', lambda db: NOW)
    with Session(engine()) as db, db.begin():
        calendar(db)
    return headers


def board_rows(day='20261023', industry=False, funds=False):
    return [dict(ts_code=f'BK{1200+i:04}.DC', trade_date=day, name=f'合成板块{i}',
        **({'net_amount': str((20-i)*100000000), 'content_type': '行业' if industry else '概念'} if funds
           else {'pct_change': str(8-i), 'idx_type': '行业板块' if industry else '概念板块'}))
        for i in range(12)]


def hot(db, received=NOW):
    rows = [dict(order=i+1, code=f'{600001+i}', name=f'合成个股{i}', rate=str(1000-i), hot_rank_chg=0)
            for i in range(10)]
    result = parse('direction_ths_hot', json.dumps({'status_code': 0, 'data': {'stock_list': rows}}).encode(), received)
    provider = db.get(Provider, source_id('direction_ths_hot'))
    store_reference_batch(db, provider, db.get(SourceSettings, provider.id), result, received)


def test_auth_get_has_no_network_or_mutations(client, monkeypatch):
    assert client.get('/api/v1/direction-data').status_code == 401
    setup(client, monkeypatch)
    monkeypatch.setattr('app.market.direction_sources.fetch', lambda *a, **k: (_ for _ in ()).throw(AssertionError('GET must be local')))
    response = client.get('/api/v1/direction-data?trade_day=2026-10-23')
    assert response.status_code == 200
    value = response.json()['data']
    assert [row['trade_day'] for row in value['days']] == ['2026-10-23','2026-10-22','2026-10-21','2026-10-20','2026-10-19']
    assert value['previous_trade_day'] == '2026-10-22'
    assert all(row['rows'] == [] for row in value['days'])
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 0


def test_each_day_sorted_before_top_ten_and_yesterday_funds_exact(client, monkeypatch):
    setup(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_dc_index', list(reversed(board_rows())), NOW)
        save(db, 'tushare_moneyflow_ind_dc', board_rows(day='20261022', funds=True), NOW)
        # A newer date's larger amount cannot replace the previous session.
        save(db, 'tushare_moneyflow_ind_dc', board_rows(funds=True), NOW + timedelta(seconds=1))
    value = client.get('/api/v1/direction-data?trade_day=2026-10-23').json()['data']
    assert value['days'][0]['rows'][0]['pct_change'] == '8'
    assert value['days'][0]['rows'][-1]['pct_change'] == '-1'
    assert len(value['days'][0]['rows']) == 10
    assert value['funds']['trade_day'] == '2026-10-22'
    assert value['funds']['rows'][0]['net_amount'] == '2000000000'
    assert value['funds']['source_time'] is None


def test_industry_is_separate_family_and_requires_explicit_classification(client, monkeypatch):
    setup(client, monkeypatch)
    assert parameters('tushare_direction_industry', [], NOW)['idx_type'] == '行业板块'
    assert parameters('tushare_direction_industry_funds', [], NOW)['content_type'] == '行业'
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_direction_industry', board_rows(industry=True), NOW)
        save(db, 'tushare_direction_industry_funds', board_rows(day='20261022', industry=True, funds=True), NOW)
        data = direction_view(db, None, trade_day=DAY, sector_type='industry')
        assert data['days'][0]['status'] == 'ready'
        assert data['funds']['status'] == 'ready'
        assert direction_view(db, None, trade_day=DAY, sector_type='concept')['days'][0]['rows'] == []


def test_latest_snapshot_is_dated_and_older_history_never_substitutes_it(client, monkeypatch):
    setup(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        hot(db)
    value = client.get('/api/v1/direction-data?trade_day=2026-10-23').json()['data']['popularity']['ths']
    assert value['trade_day'] == '2026-10-26' and value['status'] == 'partial'
    assert value['source_time'] is None and value['received_at']
    assert '不代表' in value['reason']
    older = client.get('/api/v1/direction-data?trade_day=2026-10-22').json()['data']
    assert older['popularity']['ths']['rows'] == []


def current_calendar(db, *, include_today=True):
    days = [(date(2026, 10, 26), True)] + ([(date(2026, 10, 27), True)] if include_today else [])
    for family, exchange in [('tushare_calendar_sse', 'SSE'), ('tushare_calendar_szse', 'SZSE')]:
        provider = db.get(Provider, source_id(family))
        store_calendar_batch(db, provider, db.get(SourceSettings, provider.id), days, NOW, exchanges=(exchange,))


@pytest.mark.parametrize('clock', ['09:40:00', '12:00:00', '16:00:00'])
@pytest.mark.parametrize('mode', ['automatic', 'automatic_explicit', 'explicit'])
def test_automatic_current_rankings_do_not_stick_to_yesterday_exact_rows(client, monkeypatch, clock, mode):
    setup(client, monkeypatch)
    current = datetime.fromisoformat('2026-10-27T' + clock + '+08:00')
    monkeypatch.setattr('app.market.direction_read.now', lambda db: current)
    monkeypatch.setattr('app.market.direction_sources.fetch', lambda *a, **k: pytest.fail('current GET must only read saved data'))
    with Session(engine()) as db, db.begin():
        current_calendar(db)
        hot(db, NOW)  # Yesterday exists and must not shadow today's exact rows.
        hot(db, current - timedelta(minutes=1))
        save(db, 'tushare_dc_index', board_rows(day='20261026'), NOW)
        save(db, 'tushare_moneyflow_ind_dc', board_rows(day='20261026', funds=True), NOW)
    query = 'trade_day=2026-10-27' if mode == 'explicit' else 'automatic_current=true' + ('&trade_day=2026-10-27' if mode == 'automatic_explicit' else '')
    response = client.get('/api/v1/direction-data?' + query)
    assert response.status_code == 200, response.text
    value = response.json()['data']
    assert value['automatic_current'] is True and value['snapshot_day'] == '2026-10-27'
    assert value['popularity']['ths']['trade_day'] == '2026-10-27'
    assert value['funds_day'] == value['funds']['trade_day'] == '2026-10-26'
    if clock < '15:05:00':
        assert value['days'][0]['requested_day'] == '2026-10-27'
        assert value['days'][0]['rows'] == [] and value['days'][0]['status'] == 'missing'
        assert value['days'][1]['trade_day'] == '2026-10-26'
    # An explicit historical date never opts into the new current mode.
    old = client.get('/api/v1/direction-data?trade_day=2026-10-26').json()['data']
    assert old['popularity']['ths']['trade_day'] == '2026-10-26'
    assert old['automatic_current'] is False


def test_current_missing_keeps_today_empty_and_asof_remains_fixed(client, monkeypatch):
    setup(client, monkeypatch)
    current = datetime.fromisoformat('2026-10-27T10:00:00+08:00')
    monkeypatch.setattr('app.market.direction_read.now', lambda db: current)
    with Session(engine()) as db, db.begin():
        current_calendar(db)
        hot(db, NOW)
    value = client.get('/api/v1/direction-data?automatic_current=true&trade_day=2026-10-27').json()['data']
    assert value['snapshot_day'] == '2026-10-27'
    assert value['popularity']['ths']['requested_day'] == '2026-10-27'
    assert value['popularity']['ths']['trade_day'] is None
    assert value['popularity']['ths']['rows'] == [] and value['popularity']['ths']['status'] == 'missing'
    strict = client.get('/api/v1/direction-data?automatic_current=true&trade_day=2026-10-26&as_of_time=2026-10-26T16:00:00%2B08:00')
    assert strict.status_code == 200, strict.text
    assert strict.json()['data']['automatic_current'] is False
    assert strict.json()['data']['snapshot_day'] == '2026-10-26'
    past = client.get('/api/v1/direction-data?automatic_current=true&trade_day=2026-10-26')
    assert past.status_code == 200
    assert past.json()['data']['snapshot_day'] == '2026-10-26'
    assert past.json()['data']['automatic_current'] is False


@pytest.mark.parametrize('clock', ['08:59:59', '09:00:00', '13:40:00'])
@pytest.mark.parametrize('verified', [False, True])
def test_explicit_today_slot_requires_nine_and_verified_calendar(client, monkeypatch, clock, verified):
    setup(client, monkeypatch)
    current = datetime.fromisoformat('2026-10-27T' + clock + '+08:00')
    monkeypatch.setattr('app.market.direction_read.now', lambda db: current)
    with Session(engine()) as db, db.begin():
        current_calendar(db, include_today=verified)
    response = client.get('/api/v1/direction-data?trade_day=2026-10-27')
    assert response.status_code == (200 if verified and clock >= '09:00:00' else 422)
    if response.status_code == 200:
        value = response.json()['data']
        assert value['snapshot_day'] == '2026-10-27'
        assert value['days'][0]['requested_day'] == '2026-10-27'
        assert value['days'][0]['rows'] == []


@pytest.mark.parametrize('clock,expected', [('08:59:59', '2026-10-26'), ('09:00:00', '2026-10-27')])
def test_direction_today_slot_starts_at_nine_without_today_quotes(client, monkeypatch, clock, expected):
    setup(client, monkeypatch)
    current = datetime.fromisoformat('2026-10-27T' + clock + '+08:00')
    monkeypatch.setattr('app.market.direction_read.now', lambda db: current)
    monkeypatch.setattr('app.market.direction_sources.fetch', lambda *a, **k: pytest.fail('slot creation is read-only'))
    with Session(engine()) as db, db.begin():
        current_calendar(db)
        hot(db, NOW)
        save(db, 'tushare_dc_index', board_rows(day='20261026'), NOW)
    value = client.get('/api/v1/direction-data?automatic_current=true').json()['data']
    assert value['snapshot_day'] == expected
    assert value['days'][0]['requested_day'] == expected
    if expected == '2026-10-27':
        assert value['days'][0]['status'] == 'missing' and value['days'][0]['rows'] == []
        assert value['days'][1]['trade_day'] == '2026-10-26'
        assert value['popularity']['ths']['rows'] == []
        assert value['funds_day'] == '2026-10-26'
    else:
        assert value['automatic_current'] is False
        assert value['popularity']['ths']['trade_day'] == '2026-10-26'


def test_today_data_fills_the_reserved_column_without_rewriting_yesterday(client, monkeypatch):
    setup(client, monkeypatch)
    current = datetime.fromisoformat('2026-10-27T09:00:00+08:00')
    monkeypatch.setattr('app.market.direction_read.now', lambda db: current)
    with Session(engine()) as db, db.begin():
        current_calendar(db)
        save(db, 'tushare_dc_index', board_rows(day='20261026'), NOW)
        hot(db, NOW)
    path = '/api/v1/direction-data?automatic_current=true'
    before = client.get(path).json()['data']
    current += timedelta(minutes=10)
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_dc_index', board_rows(day='20261027'), current)
        hot(db, current)
    after = client.get(path).json()['data']
    assert [item['requested_day'] for item in before['days']] == [item['requested_day'] for item in after['days']]
    assert before['days'][0]['rows'] == [] and len(after['days'][0]['rows']) == 10
    assert after['days'][0]['status'] == 'partial' and '尚未收盘' in after['days'][0]['reason']
    assert before['days'][1] == after['days'][1]
    assert after['popularity']['ths']['trade_day'] == '2026-10-27'
    historical = client.get('/api/v1/direction-data?trade_day=2026-10-26').json()['data']
    assert historical['days'][0] == before['days'][1]
    assert historical['popularity']['ths']['trade_day'] == '2026-10-26'


def test_automatic_snapshot_day_requires_the_verified_current_calendar(client, monkeypatch):
    setup(client, monkeypatch)
    current = datetime.fromisoformat('2026-10-27T10:00:00+08:00')
    monkeypatch.setattr('app.market.direction_read.now', lambda db: current)
    with Session(engine()) as db, db.begin():
        current_calendar(db, include_today=False)
        hot(db, NOW)
        hot(db, current - timedelta(minutes=1))
    value = client.get('/api/v1/direction-data?automatic_current=true').json()['data']
    assert value['automatic_current'] is False and value['snapshot_day'] == '2026-10-26'
    assert value['popularity']['ths']['trade_day'] == '2026-10-26'


def test_cutoff_and_source_revocation_hide_saved_rankings(client, monkeypatch):
    setup(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        hot(db)
        earlier = direction_view(db, None, trade_day=DAY, as_of_time=NOW-timedelta(seconds=1))
        assert earlier['popularity']['ths']['rows'] == []
        provider = db.get(Provider, source_id('direction_ths_hot'))
        provider.rights = {**provider.rights, 'display': 'denied'}
    value = client.get('/api/v1/direction-data?trade_day=2026-10-23').json()['data']
    assert value['popularity']['ths']['status'] == 'disabled'
    assert value['popularity']['ths']['rows'] == []


@pytest.mark.parametrize('status,reason',[('error','schema_changed'),('empty','upstream_unavailable'),('partial','historical_top_ten_incomplete')])
def test_later_failure_keeps_saved_ranking_but_marks_it_stale(client, monkeypatch,status,reason):
    setup(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        hot(db, NOW - timedelta(minutes=20))
        db.add(DataFetchRun(source_id=source_id('direction_ths_hot'), capability='reference',
            started_at=NOW - timedelta(minutes=10), received_at=NOW - timedelta(minutes=10),
            status=status, reason=reason, request_count=1))
    value = client.get('/api/v1/direction-data?trade_day=2026-10-23').json()['data']['popularity']['ths']
    assert value['status'] == 'stale' and len(value['rows']) == 10
    assert value['rows'][0]['rank_change'] == 0
    assert value['rank_change_basis'] == 'source_reported'
    assert '后续采集失败' in value['reason']


def test_current_board_missing_reports_publication_without_inventing_rank(client, monkeypatch):
    setup(client, monkeypatch)
    from app.market.direction_read import board_panel
    with Session(engine()) as db, db.begin():
        db.add(DataFetchRun(source_id=source_id('tushare_dc_index'), capability='reference',
            started_at=NOW - timedelta(minutes=1), received_at=NOW - timedelta(minutes=1),
            status='empty', reason='daily_data_not_published', request_count=1))
        db.flush()
        value = board_panel(db, 'themes', NOW.date(), None)
        assert value['status'] == 'missing' and value['rows'] == []
        assert '尚未发布' in value['reason'] and '不代表全板块涨幅榜' in value['reason']


def test_historical_window_does_not_move_fund_day(client, monkeypatch):
    setup(client, monkeypatch)
    value = client.get('/api/v1/direction-data?trade_day=2026-10-23&end_day=2026-10-21').json()['data']
    assert value['days'][0]['trade_day'] == '2026-10-21'
    assert value['trade_day'] == '2026-10-23' and value['previous_trade_day'] == '2026-10-22'
    assert client.get('/api/v1/direction-data?trade_day=2026-10-23&end_day=2026-10-26').status_code == 422
    assert client.get('/api/v1/direction-data?trade_day=2026-10-24').status_code == 422


def test_collection_csrf_idempotence_permissions_and_receipt(client, monkeypatch):
    from pydantic import SecretStr
    headers = setup(client, monkeypatch)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(settings(), 'tushare_token', SecretStr('synthetic-direction-test-token'))
    assert client.post('/api/v1/direction-data/collections', json={}).status_code == 403
    body = {'trade_day': '2026-10-23'}
    key = {**headers, 'Idempotency-Key': 'direction-request-001'}
    result = client.post('/api/v1/direction-data/collections', json=body, headers=key)
    assert result.status_code == 202, result.text
    assert result.json()['data']['skipped'] == []
    assert len(result.json()['data']['jobs']) == 19
    replay = client.post('/api/v1/direction-data/collections', json=body, headers=key)
    assert replay.status_code == 202 and replay.json() == result.json()
    assert replay.headers['Idempotency-Replayed'] == 'true'
    assert client.post('/api/v1/direction-data/collections', json={'trade_day': '2026-10-22'}, headers=key).status_code == 409
    batch = result.json()['data']['batch_id']
    progress = client.get('/api/v1/direction-data/collections/' + batch)
    assert progress.status_code == 200
    assert progress.json()['data']['counts'] == dict(queued=19, running=0, succeeded=1, failed=0, cancelled=0)
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 20
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 0


def test_public_collection_worker_persists_audited_snapshot(client, monkeypatch):
    from app.market.protocol import Response
    from app.worker import claim, process
    headers = setup(client, monkeypatch)
    class ScenarioTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr('app.market.collect.datetime', ScenarioTime)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    rows = [dict(order=i+1, code=f'{600001+i}', name=f'合成个股{i}', rate=str(1000-i)) for i in range(10)]
    response = Response(json.dumps({'status_code': 0, 'data': {'stock_list': rows}}).encode(), NOW, 1, 200)
    monkeypatch.setattr('app.market.direction_sources.fetch', lambda *args, **kwargs: response)
    queued = client.post(f'/api/v1/data-sources/{source_id("direction_ths_hot")}/refresh', json={},
        headers={**headers, 'Idempotency-Key': 'direction-real-worker-fixture'})
    assert queued.status_code == 202, queued.text
    job = claim()
    assert str(job.id) == queued.json()['data']['job_id']
    process(job)
    value = client.get('/api/v1/direction-data?trade_day=2026-10-23').json()['data']['popularity']['ths']
    assert len(value['rows']) == 10 and value['rows'][0]['code'] == '600001'
    assert value['trade_day'] == '2026-10-26' and value['received_at']
    with Session(engine()) as db:
        assert db.get(Job, job.id).state == 'succeeded'
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.job_id == job.id))
        assert run.status == 'complete' and run.accepted == 1 and run.request_count == 1
