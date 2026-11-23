"""Selected updates preserve the exact unrelated market projection and its scope."""
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.db import engine
from app.market import fund_projection
from app.market.fund_focus import process_job
from app.market.intraday_funds import intraday_funds
from app.read_cache import fingerprint, render
from app.worker import claim
from test_fund_focus import AT, DAY, focus, prepared, queued, wire
from app.market.protocol import Response
from test_intraday_funds import save


def test_worker_start_prepares_public_funds_without_a_user_or_source_request(client, monkeypatch):
    from sqlalchemy import func, select
    from app.market import intraday_funds as module
    from app.market.models import DataFetchRun
    from app.models import Job
    _, owner = prepared(client,monkeypatch)
    monkeypatch.setattr(fund_projection, 'now', lambda db: AT)
    with Session(engine()) as db:
        before = (db.scalar(select(func.count()).select_from(DataFetchRun)),
            db.scalar(select(func.count()).select_from(Job)))
    assert fund_projection.warm_current_funds() is True
    monkeypatch.setattr(module,'_native_funds',lambda *a,**k:pytest.fail('first visible update must reuse startup preparation'))
    with Session(engine()) as db:
        result = intraday_funds(db,DAY,AT,owner_id=owner,current=True)
        assert result.sectors[0].main_net_cny == 100 and result.target_states == []
        assert (db.scalar(select(func.count()).select_from(DataFetchRun)),
            db.scalar(select(func.count()).select_from(Job))) == before
    monkeypatch.setattr('app.maintenance_control.paused',lambda db:True)
    assert fund_projection.warm_current_funds() is False


def baseline(client, monkeypatch):
    clock, owner = prepared(client, monkeypatch)
    monkeypatch.setattr(fund_projection, 'now', lambda db: clock[0])
    with Session(engine()) as db:
        funds = intraday_funds(db, DAY, clock[0], owner_id=owner, current=True)
        from app.market.session import market_session
        session = market_session(db, clock[0])
    manifest = dict(format='synthetic-private-manifest', scope='owner-only', operation='workbench',
        params={'view':'funds','day':DAY.isoformat()}, temporal=[DAY.isoformat(),'trading'],
        dependencies={'authorization':1,'family:eastmoney_funds':1,'funds-selection:'+str(owner):1})
    payload = dict(data={'trade_day':DAY.isoformat(),'mode':'review',
        'market_session':jsonable_encoder(session),'intraday_funds':jsonable_encoder(funds),
        'stocks':[{'code':'600123.SH','price':123,'source_time':AT.isoformat()}],
        'selected_sector_code':'BK0001.DC','selected_members':[{'code':'600123.SH','main_net_cny':20}],
        'capabilities':[{'key':'quotes','status':'stale'}, {'key':'intraday_funds','status':'missing'}]},
        quality={'status':'partial','reasons':['合成隔离测试']},_dependency_manifest=copy.deepcopy(manifest))
    entry = SimpleNamespace(operation='workbench',owner_id=owner,params=manifest['params'],
        payload=payload,revision=fingerprint(manifest))
    changed = copy.deepcopy(manifest)
    changed['dependencies']['funds-selection:'+str(owner)] += 1
    return clock, owner, entry, changed


def test_native_selected_overlay_matches_full_read_without_mutating_prices_members_or_other_owners(client, monkeypatch):
    clock, owner, entry, changed = baseline(client, monkeypatch)
    original = copy.deepcopy(entry.payload)
    focus.schedule(); job = queued()
    monkeypatch.setattr('app.market.budget.reserve_eastmoney_request', lambda *a,**k: None)
    monkeypatch.setattr(focus, 'fetch_https', lambda url: Response(wire(job.payload['funds_focus_context'],clock[0]),clock[0],4,200))
    process_job(claim('realtime'))
    with Session(engine()) as db:
        refreshed = fund_projection.refresh_selected_funds(db, entry, changed)
        expected = intraday_funds(db, DAY, clock[0], owner_id=owner, current=True)
    assert refreshed is not None
    assert refreshed['data']['intraday_funds'] == jsonable_encoder(expected)
    assert next(row for row in refreshed['data']['intraday_funds']['stocks'] if row['code']=='600123.SH')['value'] == -25
    for key in ('stocks','selected_members','selected_sector_code'):
        assert refreshed['data'][key] == original['data'][key]
    assert refreshed['data']['capabilities'][0] == original['data']['capabilities'][0]
    assert entry.payload == original
    response = render(SimpleNamespace(state=SimpleNamespace(request_id='synthetic-request')),refreshed)
    assert '_dependency_manifest' not in json.loads(response.body)
    refreshed['data']['stocks'][0]['price'] = 999
    assert entry.payload == original


@pytest.mark.parametrize('change',['authorization','family:eastmoney_funds','scope','temporal','owner','historical','future_receipt','market_phase'])
def test_other_versions_permissions_history_and_clock_regression_require_full_projection(client, monkeypatch, change):
    clock, owner, entry, changed = baseline(client, monkeypatch)
    if change in {'authorization','family:eastmoney_funds'}:
        changed['dependencies'][change] += 1
    elif change in {'scope','temporal'}:
        changed[change] = 'different-boundary'
    elif change == 'owner':
        entry.owner_id = 'another-owner'
    elif change == 'historical':
        entry.params['as_of_time'] = AT.isoformat()
    elif change == 'market_phase':
        entry.payload['data']['market_session']['phase'] = 'different-phase'
    else:
        entry.payload['data']['market_session']['server_time'] = AT.replace(minute=AT.minute+1).isoformat()
    with Session(engine()) as db:
        assert fund_projection.refresh_selected_funds(db,entry,changed) is None


def test_newly_visible_native_ranking_requires_rebuilding_default_board_and_members(client, monkeypatch):
    from datetime import timedelta
    clock, owner, entry, changed = baseline(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        save(db,[('BK0002','200')],stamp=AT+timedelta(seconds=1))
    clock[0] += timedelta(seconds=2)
    with Session(engine()) as db:
        assert fund_projection.refresh_selected_funds(db,entry,changed) is None
