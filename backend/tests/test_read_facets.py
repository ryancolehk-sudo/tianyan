"""The derived index preserves original evidence and cannot grant visibility."""
import os

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import read_facets
from app.market.read_facets import EvidenceReadFacet
from app.market.terminal_read import workbench_view
from app.models import User
from app.read_cache import prepare_current_workbenches, run_one
from app.read_cache_models import ReadCache
from test_read_cache import seed


def test_insert_and_rollback_keep_facets_atomic_with_original(client, monkeypatch):
    seed(client, monkeypatch)
    with Session(engine()) as db:
        evidence=db.scalar(select(EvidenceVersion).where(EvidenceVersion.kind=='reference'))
        facet=db.get(EvidenceReadFacet,evidence.id)
        assert facet is not None and facet.content_hash==evidence.content_hash
        assert facet.observed_day==evidence.payload.get('observed_day')
        assert facet.dataset==evidence.payload.get('dataset')
        expected={str(row['trade_date']) for row in evidence.payload.get('rows',[]) if row.get('trade_date') is not None}
        assert set(facet.trade_dates)==expected
    from test_terminal_workbench import save,AT
    from test_tushare_market import quote
    from datetime import timedelta
    with Session(engine()) as db:
        save(db,'tushare_market_rt',[quote('000100.SZ',price='11',at=AT+timedelta(minutes=1))],AT+timedelta(minutes=1))
        inserted=db.scalar(select(EvidenceVersion).where(EvidenceVersion.kind=='reference')
            .order_by(EvidenceVersion.received_at.desc()))
        ident=inserted.id
        assert db.get(EvidenceReadFacet,ident) is not None
        db.rollback()
    with Session(engine()) as db:
        assert db.get(EvidenceVersion,ident) is None and db.get(EvidenceReadFacet,ident) is None
    # The app role can read metadata but cannot forge it independently.
    app=create_engine(os.environ['JL_TEST_APP_URL'])
    try:
        with app.connect() as connection:
            assert connection.execute(text('SELECT count(*) FROM evidence_read_facets')).scalar() > 0
            with pytest.raises(DBAPIError):connection.execute(text('DELETE FROM evidence_read_facets'))
            connection.rollback()
    finally:app.dispose()


@pytest.mark.parametrize('rows',[[],[{'dataset':None,'requested_day':None,'batch_hash':None}],
    [{'dataset':'daily','requested_day':'2026-10-23','batch_hash':'b'*64}]])
def test_optional_metadata_and_fallback_preserve_known_nulls(client,monkeypatch,rows):
    from app.evidence.schemas import ReferencePayload
    from app.market.collect import store_reference_batch
    from app.market.reference import ReferenceBatch
    from app.market.models import SourceSettings
    from app.market.service import source_id
    from app.models import Provider
    from test_terminal_workbench import AT
    seed(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_market_rt'))
        payload=ReferencePayload(dataset='review.daily',title='synthetic facets',rows=rows,
            columns=[{'key':key,'label':key} for key in ('dataset','requested_day','batch_hash')],
            source_url='https://example.invalid/synthetic',attribution='synthetic fixture',scope='synthetic fixture')
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),
            ReferenceBatch(records=[('synthetic-facets',payload,None)],expected=1),AT)
        evidence=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_key=='reference:synthetic-facets'))
        ident=evidence.id
    names=['dataset','observed_day','row_dataset','requested_day','batch_hash']
    expected=('review.daily',None,*[(rows[0] if rows else {}).get(key) for key in ('dataset','requested_day','batch_hash')])
    for state in ('present','stale','missing'):
        with Session(engine()) as db,db.begin():
            if state=='stale':db.get(EvidenceReadFacet,ident).content_hash='0'*64;db.flush()
            if state=='missing':db.execute(delete(EvidenceReadFacet).where(EvidenceReadFacet.evidence_id==ident))
            query=read_facets.with_facets(select(EvidenceVersion.id,*[read_facets.value(name) for name in names]))
            values=db.execute(query.where(EvidenceVersion.id==ident)).one()
            assert tuple(values[1:])==expected


@pytest.mark.parametrize('missing', [True,False])
def test_missing_or_stale_index_falls_back_without_changing_full_result(client,monkeypatch,missing):
    seed(client,monkeypatch)
    with Session(engine()) as db:
        owner=db.scalar(select(User.id))
        expected=workbench_view(db,owner,view='ladder').model_dump(mode='json')
    with Session(engine()) as db,db.begin():
        if missing:db.execute(delete(EvidenceReadFacet))
        else:
            for row in db.scalars(select(EvidenceReadFacet)):row.content_hash='0'*64
    with Session(engine()) as db:
        actual=workbench_view(db,owner,view='ladder').model_dump(mode='json')
    assert actual==expected


def test_background_preparation_seeds_all_views_before_any_browser_get(client,monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings(),'read_cache_enabled',True)
    seed(client,monkeypatch)
    with Session(engine(),expire_on_commit=False) as db:
        assert prepare_current_workbenches(db)==4
        assert len(list(db.scalars(select(ReadCache))))==4
    for _ in range(4):
        with Session(engine(),expire_on_commit=False) as db:assert run_one(db)
    for view in ('overview','auction','ladder','funds'):
        result=client.get('/api/v1/workbench-view?view='+view)
        assert result.status_code==200 and result.json()['data']['view']==view
    with Session(engine()) as db:
        from app.models import Job
        assert not list(db.scalars(select(Job)))
