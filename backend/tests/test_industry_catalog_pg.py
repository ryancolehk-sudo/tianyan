"""Disposable database verification for received-time industry classification."""
from datetime import timedelta
import json
import pytest

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collect import store_reference_batch
from app.market.eastmoney_trading import parse_ranking
from app.market.industry_catalog import parse_catalog, saved_catalog, catalog_due
from app.market.intraday_funds import intraday_funds
from app.market.models import SourceSettings, DataFetchRun
from app.market.reference import ReferenceBatch
from app.market.read_facets import EvidenceReadFacet
from app.market.service import source_id
from app.models import Provider
from test_intraday_funds import setup, save, DAY, AT
from test_industry_catalog import catalog_body


def store_catalog(db,stamp):
    provider=db.get(Provider,source_id('eastmoney_funds'))
    payload=parse_catalog(catalog_body(),stamp)
    store_reference_batch(db,provider,db.get(SourceSettings,provider.id),
        ReferenceBatch(records=[(payload.dataset,payload,None)],expected=1,requests=1),stamp)


def test_same_day_catalog_classifies_saved_rows_without_future_or_other_day_leakage(client):
    setup(client)
    later=AT+timedelta(seconds=10)
    with Session(engine()) as db,db.begin():
        save(db,[('BK0001','50'),('BK0002','20'),('BK0003','10')],category='industry')
        save(db,[('BK0004','100')],category='concept')
        store_catalog(db,later)
    with Session(engine()) as db:
        old=intraday_funds(db,DAY,AT,session={'phase':'trading'})
        assert all(row.industry_level is None for row in old.sectors)
        inputs=[]
        current=intraday_funds(db,DAY,later,input_manifest=inputs,session={'phase':'trading'})
        levels={row.code:row.industry_level for row in current.sectors}
        assert levels=={'BK0001.DC':1,'BK0002.DC':2,'BK0003.DC':3,'BK0004.DC':None}
        assert all(row.rank==1 for row in current.sectors)
        evidence=saved_catalog(db,source_id('eastmoney_funds'),DAY,later)
        assert any(item['evidence_id']==str(evidence.id) for item in inputs)
        assert saved_catalog(db,source_id('eastmoney_funds'),DAY-timedelta(days=1),later) is None
        assert not catalog_due(db,source_id('eastmoney_funds'),later)
        assert catalog_due(db,source_id('eastmoney_funds'),later+timedelta(days=1))


def test_industry_rank_change_is_not_contaminated_by_concept_rank(client):
    setup(client)
    before=AT-timedelta(minutes=5)
    with Session(engine()) as db,db.begin():
        store_catalog(db,before)
        save(db,[('BK0002','20')],before,category='industry')
        save(db,[('BK0004','5')],before,category='concept')
        save(db,[('BK0002','30')],category='industry')
        save(db,[('BK0004','100')],category='concept')
    with Session(engine()) as db:
        result=intraday_funds(db,DAY,AT,session={'phase':'trading'})
        assert all(row.rank==1 and row.rank_change_5m==0 for row in result.sectors)
        assert next(row for row in result.sectors if row.category=='industry').change_5m_cny==10


@pytest.mark.parametrize('facets',['current','stale','missing'])
def test_catalog_facets_preserve_same_day_latest_receipt_and_original_fallback(client,facets):
    setup(client)
    identity=source_id('eastmoney_funds')
    old,later=AT-timedelta(seconds=10),AT+timedelta(seconds=10)
    with Session(engine()) as db,db.begin():
        store_catalog(db,old)
        earlier=saved_catalog(db,identity,DAY,old).id
        provider=db.get(Provider,identity)
        corrected=parse_catalog(catalog_body(),later).model_copy(update={'scope':'Synthetic corrected catalog'})
        store_reference_batch(db,provider,db.get(SourceSettings,identity),
            ReferenceBatch(records=[(corrected.dataset,corrected,None)],expected=1),later)
        latest=saved_catalog(db,identity,DAY,later).id
        assert earlier!=latest
        # A more recent catalog from a different observation day must not win.
        another=corrected.model_copy(update={'observed_day':DAY-timedelta(days=1)})
        store_reference_batch(db,provider,db.get(SourceSettings,identity),
            ReferenceBatch(records=[(another.dataset,another,None)],expected=1),later+timedelta(seconds=1))
        records=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==identity)))
        assert all(db.get(EvidenceReadFacet,item.id) is not None for item in records)
        if facets=='missing':db.execute(delete(EvidenceReadFacet))
        elif facets=='stale':
            for item in records:db.get(EvidenceReadFacet,item.id).content_hash='0'*64
    with Session(engine()) as db:
        assert saved_catalog(db,identity,DAY,old-timedelta(microseconds=1)) is None
        assert saved_catalog(db,identity,DAY,old).id==earlier
        assert saved_catalog(db,identity,DAY,later-timedelta(microseconds=1)).id==earlier
        assert saved_catalog(db,identity,DAY,later).id==latest
        assert saved_catalog(db,identity,DAY,later+timedelta(seconds=2)).id==latest
        assert saved_catalog(db,identity,DAY+timedelta(days=1),later+timedelta(seconds=2)) is None


@pytest.mark.parametrize('reason,rejected,expected_state',[
    ('industry_catalog_transport_error',1,'available'),
    ('industry_catalog_intraday_window_wait',1,'available'),
    ('rate_limited',1,'available'),
    ('rate_limited',2,'stale'),
    (None,1,'stale'),
])
def test_metadata_failure_does_not_mislabel_successful_financial_tables(client,reason,rejected,expected_state):
    setup(client)
    received=AT+timedelta(seconds=1)
    with Session(engine()) as db,db.begin():
        save(db,[('BK0002','20')],category='industry',received=received)
        save(db,[('BK0004','30')],category='concept',received=received)
        save(db,[('600000','10')],category='stock',received=received)
        db.add(DataFetchRun(source_id=source_id('eastmoney_funds'),capability='funds.sector',
            started_at=AT,received_at=received,status='partial',reason=reason,
            expected=4,accepted=4-rejected,duplicates=0,rejected=rejected,request_count=4,
            retry_not_before=received+timedelta(seconds=90) if reason=='rate_limited' else None))
    with Session(engine()) as db:
        view=intraday_funds(db,DAY,received,session={'phase':'trading'})
        assert view.state==expected_state
        assert ('本轮部分资金表未能更新' in view.scope)==(expected_state=='stale')
        if expected_state=='available':assert '行业分类目录暂未更新' in view.scope
        if reason=='rate_limited':
            from app.market.budget import eastmoney_cooldown_until
            assert eastmoney_cooldown_until(db,received)==received+timedelta(seconds=90)
