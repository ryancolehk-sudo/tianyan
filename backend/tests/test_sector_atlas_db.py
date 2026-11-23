"""Disposable PostgreSQL: archive identity, late receipts and pure GETs."""
from datetime import timedelta
from dataclasses import replace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.market.collect import store_reference_batch
from app.market.kpl_atlas_source import Pagination
from app.market.models import SourceSettings, DataFetchRun
from app.market.sector_atlas import sector_atlas
from app.market.service import source_id
from app.models import Provider, Job
from test_direction_data import setup, DAY, NOW
from test_kpl_atlas_source import page


def test_complete_archive_keeps_unranked_strength_and_cutoff(client,monkeypatch):
    setup(client,monkeypatch)
    monkeypatch.setattr('app.market.sector_atlas.now',lambda db:NOW+timedelta(seconds=5))
    pages=Pagination(DAY)
    pages.add(replace(page(0,30,30,DAY),received_at=NOW));pages.add(replace(page(30,1,1,DAY),received_at=NOW))
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('direction_kpl_strength'))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),pages.batch(),NOW)
    # This historical date was received later. A prior point-in-time read
    # cannot borrow it even though its requested trading date is earlier.
    with Session(engine()) as db:
        before=sector_atlas(db,DAY,10,'kpl',NOW-timedelta(seconds=1))
        assert not before['days'][0]['rows']
        value=sector_atlas(db,DAY,10,'kpl',NOW,selected_codes='801030')
        assert value['days'][0]['details'][0]['rank']==31
        assert value['days'][0]['total_count']==31
        assert value['days'][0]['archive_kind']=='daily'
    monkeypatch.setattr('app.market.kpl_atlas_source.fetch',lambda **k:(_ for _ in ()).throw(AssertionError('read must be local')))
    result=client.get('/api/v1/direction-data/sector-atlas?trade_day=2026-10-23&lookback=10&source=kpl&selected_codes=801030')
    assert result.status_code==200
    assert result.json()['data']['days'][0]['details'][0]['code']=='801030'
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job))==0
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==0


def test_legacy_top_ten_never_becomes_complete_atlas(client,monkeypatch):
    from app.market.direction_sources import parse
    setup(client,monkeypatch)
    response=page(0,10,241,DAY)
    batch=parse('direction_kpl_strength',response.body,response.received_at,requested_day=DAY)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('direction_kpl_strength'))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),batch,NOW)
    with Session(engine()) as db:
        result=sector_atlas(db,DAY,10,'kpl',NOW)
    assert len(result['days'][0]['rows'])==10
    assert result['days'][0]['details']==[]
    assert result['coverage']['full_days']==0 and result['coverage']['daily_days']==0


def test_atlas_auth_and_query_limits(client,monkeypatch):
    assert client.get('/api/v1/direction-data/sector-atlas').status_code==401
    setup(client,monkeypatch)
    for query in ['lookback=51','source=ths_hot','selected_codes=600001.SH','selected_codes=bad']:
        assert client.get('/api/v1/direction-data/sector-atlas?'+query).status_code==422
