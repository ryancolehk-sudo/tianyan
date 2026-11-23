"""Canonical detail identity on disposable PostgreSQL with synthetic API data."""
from datetime import datetime, timedelta
from urllib.parse import urlencode

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collect import store_reference_batch
from app.market.models import DataFetchRun, SourceSettings
from app.models import Instrument, Provider
from test_data_resilience import CLOCK, scenario
from test_fuyao_market import batch, fm, raw, scene
from test_terminal_workbench import AT, DAY, quote, save, setup
from test_terminal_sector_tracking import member


VIEWS = ('overview', 'auction', 'ladder', 'funds')


def set_clock(monkeypatch, clock):
    monkeypatch.setattr('app.market.terminal_read.now', lambda db: clock)
    monkeypatch.setattr('app.market.read_models.now', lambda db: clock)


def parents(client, day):
    values = []
    for kind in VIEWS:
        result = client.get('/api/v1/workbench-view?' + urlencode({'view': kind, 'day': day}))
        assert result.status_code == 200, result.text
        values.append(result.json()['data'])
    assert len({value['detail_revision'] for value in values}) == 1
    return values


def details(client, parent, code, *, strict=False):
    params = {'day': parent['trade_day'], 'codes': code,
              'as_of_time' if strict else 'observation_cutoff': parent['detail_observation_cutoff']}
    result = client.get('/api/v1/workbench-details?' + urlencode(params))
    assert result.status_code == 200, result.text
    value = result.json()['data']
    if not strict:
        assert value['revision'] == parent['detail_revision']
    return value


def selected(value, code):
    return next(row for row in value['stocks'] if row['code'] == code)


def evidence_count():
    with Session(engine()) as db:
        return db.scalar(select(func.count()).select_from(EvidenceVersion))


def cache_clock(monkeypatch, first, process_skew=0):
    from app import read_cache
    from app.config import settings
    moments=[first]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value=moments[0]+timedelta(seconds=process_skew)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(read_cache,'datetime',Clock)
    monkeypatch.setattr(read_cache,'visibility_now',lambda db:moments[0])
    monkeypatch.setattr(settings(),'read_cache_enabled',True)

    def advance(clock):
        moments[0]=clock
        set_clock(monkeypatch,clock)

    advance(first)
    return advance


def test_same_receipts_change_identity_when_a_future_source_row_becomes_visible(client, monkeypatch):
    setup(client, monkeypatch, ['tushare_market_rt'])
    code = '000100.SZ'
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_market_rt', [quote(code, price='10', at=AT)], AT)
        save(db, 'tushare_market_rt', [quote(code, price='11', at=AT + timedelta(seconds=60))],
             AT + timedelta(seconds=30))
    count = evidence_count()
    set_clock(monkeypatch, AT + timedelta(seconds=40))
    before = parents(client, DAY)[0]
    assert selected(details(client, before, code), code)['price'] == 10
    set_clock(monkeypatch, AT + timedelta(seconds=45))
    unchanged_before = parents(client, DAY)[0]
    assert unchanged_before['detail_revision'] == before['detail_revision']
    set_clock(monkeypatch, AT + timedelta(seconds=61))
    after = parents(client, DAY)[0]
    assert selected(details(client, after, code), code)['price'] == 11
    assert selected(details(client, before, code), code)['price'] == 10
    assert selected(details(client, before, code, strict=True), code)['price'] == 10
    set_clock(monkeypatch, AT + timedelta(seconds=70))
    unchanged_after = parents(client, DAY)[0]
    assert unchanged_after['detail_revision'] == after['detail_revision']
    assert evidence_count() == count
    assert after['detail_revision'] != before['detail_revision']


def test_new_bulk_fallback_snapshot_changes_identity_and_keeps_original_parent(client, scenario, monkeypatch):
    with Session(engine()) as db, db.begin():
        _, _, provider, config, _ = scene(db, scenario, monkeypatch)
        provider_id = provider.id
    before = parents(client, CLOCK.date())[0]
    old = selected(details(client, before, '600000.SH'), '600000.SH')
    assert old['price'] == 12 and old['price_basis'] == 'snapshot'
    later = CLOCK + timedelta(seconds=10)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, provider_id)
        config = db.get(SourceSettings, provider_id)
        store_reference_batch(db, provider, config,
            batch([{**raw(), 'last_price': '13', 'high_price': '13'}], clock=later), later)
        db.add(DataFetchRun(source_id=provider.id, capability='quote.live',
            started_at=later - timedelta(milliseconds=50), received_at=later,
            status='partial', reason='snapshot_trade_date_unverified', expected=1, accepted=1,
            duplicates=0, rejected=0, request_count=1))
    set_clock(monkeypatch, later)
    after = parents(client, CLOCK.date())[0]
    current = selected(details(client, after, '600000.SH'), '600000.SH')
    assert current['price'] == 13 and current['price_basis'] == 'snapshot'
    assert current['source_time'] is None
    assert datetime.fromisoformat(current['source_snapshot_time']) == later
    pinned = selected(details(client, before, '600000.SH'), '600000.SH')
    assert pinned['price'] == 12 and pinned['source_time'] is None
    assert datetime.fromisoformat(pinned['source_snapshot_time']) == CLOCK
    assert after['detail_revision'] != before['detail_revision']


def test_expired_bulk_snapshot_changes_identity_without_new_receipts(client, scenario, monkeypatch):
    with Session(engine()) as db, db.begin():
        scene(db, scenario, monkeypatch)
    before = parents(client, CLOCK.date())[0]
    assert selected(details(client, before, '600000.SH'), '600000.SH')['price'] == 12
    count = evidence_count()
    set_clock(monkeypatch, CLOCK + timedelta(seconds=121))
    after = parents(client, CLOCK.date())[0]
    current = selected(details(client, after, '600000.SH'), '600000.SH')
    assert current['price'] == 11 and current['source_family'] == 'tushare_market_rt'
    assert selected(details(client, before, '600000.SH'), '600000.SH')['price'] == 12
    assert evidence_count() == count
    assert after['detail_revision'] != before['detail_revision']


def test_bulk_fallback_becomes_visible_when_primary_expires_without_a_new_receipt(client, scenario, monkeypatch):
    with Session(engine()) as db, db.begin():
        scene(db, scenario, monkeypatch, primary_age=110)
    before = parents(client, CLOCK.date())[0]
    assert selected(details(client, before, '600000.SH'), '600000.SH')['price'] == 11
    count = evidence_count()
    set_clock(monkeypatch, CLOCK + timedelta(seconds=11))
    after = parents(client, CLOCK.date())[0]
    assert selected(details(client, after, '600000.SH'), '600000.SH')['price'] == 12
    assert selected(details(client, before, '600000.SH'), '600000.SH')['price'] == 11
    assert evidence_count() == count
    assert after['detail_revision'] != before['detail_revision']


def test_midnight_changes_current_day_membership_policy_without_new_receipts(client, monkeypatch):
    setup(client, monkeypatch, ['tushare_market_rt', 'tushare_dc_member'])
    previous = DAY - timedelta(days=1)
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_dc_member', [member('000100.SZ', day=previous.strftime('%Y%m%d'))],
             AT - timedelta(days=1))
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='10')], AT)

    def board(cutoff):
        response = client.get('/api/v1/workbench-details?' + urlencode({
            'day': DAY, 'sector': 'BK1184.DC', 'observation_cutoff': cutoff.isoformat()}))
        assert response.status_code == 200, response.text
        return response.json()['data']

    first_clock = AT.replace(hour=8)  # 16:30 Shanghai, after the close.
    set_clock(monkeypatch, first_clock)
    before = board(first_clock)
    assert before['members_trade_day'] == previous.isoformat()
    assert before['tracking_member_count'] == 1
    count = evidence_count()
    later = first_clock + timedelta(hours=10)
    set_clock(monkeypatch, later)
    after = board(later)
    assert after['members_trade_day'] is None and after['tracking_members'] == []
    assert board(first_clock)['members_trade_day'] == previous.isoformat()
    assert evidence_count() == count
    assert after['revision'] != before['revision']


def test_current_instrument_catalog_changes_retire_the_detail_identity(client, monkeypatch):
    setup(client, monkeypatch, [])
    set_clock(monkeypatch, AT + timedelta(seconds=40))
    with Session(engine()) as db, db.begin():
        identity = Instrument(exchange='SZSE', symbol='000100', asset_type='stock', name='合成旧名称',
                              valid_from=DAY, first_received_at=AT)
        db.add(identity)
        db.flush()
        instrument_id = identity.id
    before = parents(client, DAY)[0]
    assert selected(details(client, before, '000100.SZ'), '000100.SZ')['name'] == '合成旧名称'
    with Session(engine()) as db, db.begin():
        db.get(Instrument, instrument_id).name = '合成新名称'
    after = parents(client, DAY)[0]
    assert selected(details(client, after, '000100.SZ'), '000100.SZ')['name'] == '合成新名称'
    assert after['detail_revision'] != before['detail_revision']


@pytest.mark.parametrize('process_skew',[-30,0,30])
def test_live_cached_parent_revalidates_at_the_source_visibility_boundary(client, monkeypatch, process_skew):
    from sqlalchemy import update
    from app.read_cache_models import ReadCache
    from test_read_cache import ready_parent, worker

    setup(client, monkeypatch, ['tushare_market_rt'])
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='10', at=AT)], AT)
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='11', at=AT + timedelta(seconds=60))],
             AT + timedelta(seconds=30))
    advance=cache_clock(monkeypatch,AT+timedelta(seconds=40),process_skew)
    path = '/api/v1/workbench-view?view=overview&day=2026-10-23'
    first = ready_parent(client, path)
    assert selected(first.json()['data'], '000100.SZ')['price'] == 10
    advance(AT+timedelta(seconds=61))
    with Session(engine()) as db, db.begin():
        db.execute(update(ReadCache).values(refresh_after=AT + timedelta(seconds=60)))
    assert worker()
    current = client.get(path, headers={'If-None-Match': first.headers['etag']})
    assert current.status_code == 200, 'a changed visible quote must not be returned as unchanged 304'
    assert selected(current.json()['data'], '000100.SZ')['price'] == 11
    assert current.json()['data']['detail_revision'] != first.json()['data']['detail_revision']


def test_partial_bulk_snapshot_expiry_changes_the_actual_overlay_identity(client, scenario, monkeypatch):
    received=CLOCK+timedelta(seconds=10)
    with Session(engine()) as db, db.begin():
        _,_,provider,config,_=scene(db,scenario,monkeypatch)
        mixed=fm.parse([(raw(),int(CLOCK.timestamp()*1000),received),
            ({**raw('600001.SH'),'last_price':'14','high_price':'14'},int(received.timestamp()*1000),received)],received)
        store_reference_batch(db,provider,config,mixed,received)
        db.add(DataFetchRun(source_id=provider.id,capability='quote.live',started_at=received-timedelta(milliseconds=50),
            received_at=received,status='partial',reason='snapshot_trade_date_unverified',expected=2,accepted=2,
            rejected=0,duplicates=0,request_count=1))
    set_clock(monkeypatch,received)
    before=parents(client,CLOCK.date())[0]
    assert selected(details(client,before,'600000.SH'),'600000.SH')['price']==12
    count=evidence_count()
    set_clock(monkeypatch,CLOCK+timedelta(seconds=121))
    after=parents(client,CLOCK.date())[0]
    assert selected(details(client,after,'600000.SH'),'600000.SH')['price']==11
    remaining=selected(details(client,after,'600001.SH'),'600001.SH')
    assert remaining['price']==14 and remaining['price_basis']=='snapshot'
    assert evidence_count()==count
    assert after['detail_revision']!=before['detail_revision']


def test_cached_bulk_snapshot_expiry_publishes_a_new_representation(client, scenario, monkeypatch):
    from test_read_cache import ready_parent,worker
    with Session(engine()) as db,db.begin():scene(db,scenario,monkeypatch)
    advance=cache_clock(monkeypatch,CLOCK)
    path='/api/v1/workbench-view?'+urlencode({'view':'overview','day':CLOCK.date()})
    first=ready_parent(client,path)
    assert selected(first.json()['data'],'600000.SH')['price']==12
    count=evidence_count()
    advance(CLOCK+timedelta(seconds=121))
    assert worker()
    current=client.get(path,headers={'If-None-Match':first.headers['etag']})
    assert current.status_code==200
    assert selected(current.json()['data'],'600000.SH')['price']==11
    assert current.json()['data']['detail_revision']!=first.json()['data']['detail_revision']
    assert '_next_visibility_at' not in current.text and '_visibility_epoch' not in current.text
    assert evidence_count()==count


def test_cached_future_bulk_snapshot_uses_its_real_boundary_without_new_receipts(client, scenario, monkeypatch):
    from app.read_cache_models import ReadCache
    from test_read_cache import ready_parent,worker
    received=CLOCK+timedelta(seconds=1)
    native=CLOCK+timedelta(seconds=30)
    with Session(engine()) as db,db.begin():
        _,_,provider,config,_=scene(db,scenario,monkeypatch)
        future=fm.parse([(raw(),int(native.timestamp()*1000),received)],received)
        store_reference_batch(db,provider,config,future,received)
        db.add(DataFetchRun(source_id=provider.id,capability='quote.live',started_at=received-timedelta(milliseconds=50),
            received_at=received,status='partial',reason='snapshot_trade_date_unverified',expected=1,accepted=1,
            rejected=0,duplicates=0,request_count=1))
    advance=cache_clock(monkeypatch,CLOCK+timedelta(seconds=10))
    path='/api/v1/workbench-view?'+urlencode({'view':'overview','day':CLOCK.date()})
    first=ready_parent(client,path)
    assert selected(first.json()['data'],'600000.SH')['price']==11
    count=evidence_count()
    advance(CLOCK+timedelta(seconds=26))
    assert client.get(path,headers={'If-None-Match':first.headers['etag']}).status_code==304
    with Session(engine()) as db:
        entry=db.scalar(select(ReadCache).where(ReadCache.operation=='workbench'))
        assert entry.refresh_after<=native, 'metadata renewal must retain the earlier visibility deadline'
    advance(CLOCK+timedelta(seconds=31))
    assert worker()
    current=client.get(path,headers={'If-None-Match':first.headers['etag']})
    assert current.status_code==200
    stock=selected(current.json()['data'],'600000.SH')
    assert stock['price']==12 and stock['source_time'] is None
    assert datetime.fromisoformat(stock['source_snapshot_time'])==native
    assert evidence_count()==count


def test_cache_build_crossing_a_visibility_boundary_cannot_publish_its_older_candidate(client, monkeypatch):
    from app import read_cache
    from app.read_cache_models import ReadCache
    from test_read_cache import worker
    setup(client,monkeypatch,['tushare_market_rt'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote('000100.SZ',price='10',at=AT)],AT)
        save(db,'tushare_market_rt',[quote('000100.SZ',price='11',at=AT+timedelta(seconds=60))],
             AT+timedelta(seconds=30))
    advance=cache_clock(monkeypatch,AT+timedelta(seconds=40))
    original=read_cache.compute

    def crosses(*args,**kwargs):
        value=original(*args,**kwargs)
        advance(AT+timedelta(seconds=61))
        return value

    monkeypatch.setattr(read_cache,'compute',crosses)
    path='/api/v1/workbench-view?view=overview&day=2026-10-23'
    assert client.get(path).status_code==503
    worker()
    with Session(engine()) as db:
        entry=db.scalar(select(ReadCache).where(ReadCache.operation=='workbench'))
        assert entry.payload is None, 'an obsolete pre-boundary candidate must not become a published result'
    assert client.get(path).status_code==503
    assert worker()
    ready=client.get(path)
    assert ready.status_code==200
    assert selected(ready.json()['data'],'000100.SZ')['price']==11
