"""Whole-market selection keeps source rights, cutoff and native-clock boundaries."""
from datetime import UTC,datetime,timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion,SourceVersion
from app.evidence.schemas import ReferencePayload
from app.market import tencent_market as qq
from app.market.catalog_scope import capture_payloads
from app.market.collect import store_reference_batch
from app.market.market_series import choose
from app.market.models import SourceSettings
from app.market.protocol import parse_cninfo_catalog
from app.market.read_facets import EvidenceReadFacet
from app.market.reference import ReferenceBatch
from app.market.service import source_id
from app.models import Provider
from test_catalog_scope import native
from test_data_sources import configuration,configure,login
from test_tushare_market import quote, store

AT=datetime(2026,10,23,2,30,tzinfo=UTC)


def table(dataset,rows):
    return ReferencePayload(dataset=dataset,title='Synthetic market selection',
        source_url='https://example.invalid/synthetic',attribution='Synthetic fixture',
        scope='One synthetic closed receipt; not a collector test.',observed_day=AT.date(),
        columns=[dict(key=key,label=key) for key in rows[0]],rows=rows)


def setup(client):
    headers=login(client)
    for family in ('cninfo',qq.FAMILY,'tushare_market_rt'):
        config=configuration(family)
        if family in {qq.FAMILY,'tushare_market_rt'}:config['interval_seconds']=15
        if family==qq.FAMILY:config.update(collection_mode='full_market',
            rights=dict(display='allowed',cache='allowed',export='denied',redistribute='denied'))
        configure(client,headers,config,key='market-series-'+family)
    with Session(engine()) as db,db.begin():
        for row in list(db.scalars(select(SourceVersion))):
            db.add(SourceVersion(source_id=row.source_id,recorded_at=AT-timedelta(days=2),
                descriptor=row.descriptor,content_hash=row.content_hash))
        # These are saved read-model fixtures, independent of collector signatures.
        source=db.get(Provider,source_id('cninfo'));stamp=AT-timedelta(minutes=1);body=native(('600001',))
        records=capture_payloads(db,source,parse_cninfo_catalog(body),body,stamp)
        store_reference_batch(db,source,db.get(SourceSettings,source.id),
            ReferenceBatch(records=[(key,payload,None) for key,payload in records],expected=len(records)),stamp)
        db.flush()
        scope=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source.id,
            EvidenceVersion.payload['dataset'].astext=='instrument.catalog.capture'))
        capture='a'*64
        payload=table(qq.DATASET,[dict(quote('600001.SH'),capture_id=capture)])
        common=dict(capture_id=capture,scope_source_id=str(source.id),scope_evidence_id=str(scope.id),
            scope_kind='public_catalog_listing_unknown',scope_received_at=stamp.isoformat(),
            universe_hash=scope.payload['rows'][0]['scope_sha256'],snapshot_revision=content_hash(payload),
            calculation=qq.CALCULATION,clock_policy=qq.CLOCK_POLICY,clock_wait_ms='0',clock_regressed='false')
        page=dict(common,row_type='page',page='0',requested='1',returned='1',
            request_started_at=(AT-timedelta(milliseconds=1)).isoformat(),received_at=AT.isoformat(),
            sha256='b'*64,elapsed_ms='1')
        coverage=dict(common,row_type='coverage',expected='1',accepted='1',rejected='0',received_at=AT.isoformat())
        keys=dict.fromkeys(page|coverage)
        manifest=table(qq.MANIFEST,[keys|page,keys|coverage])
        source=db.get(Provider,source_id(qq.FAMILY))
        store_reference_batch(db,source,db.get(SourceSettings,source.id),
            ReferenceBatch(records=[(qq.DATASET,payload,None),(qq.MANIFEST,manifest,None)],expected=2),AT)
        store(db,'tushare_market_rt',[quote('600001.SH',at=AT-timedelta(minutes=2))],AT)


def providers(db):
    return {row.family:row for row in db.scalars(select(Provider))}


@pytest.mark.parametrize('facets',['present','missing','stale'])
def test_receipt_cutoff_native_freshness_and_historical_preference(client,facets):
    setup(client)
    with Session(engine()) as db,db.begin():
        store(db,'tushare_market_rt',[quote('600001.SH',at=AT+timedelta(seconds=10))],AT+timedelta(seconds=10))
        if facets=='missing':db.execute(delete(EvidenceReadFacet))
        if facets=='stale':
            for row in db.scalars(select(EvidenceReadFacet)):row.content_hash='0'*64
    with Session(engine()) as db:
        sources=providers(db)
        cutoff=AT+timedelta(seconds=1)
        assert choose(db,sources,AT.date(),cutoff)=='tencent_market_rt'
        assert choose(db,sources,AT.date(),cutoff,historical=True)=='tushare_market_rt'
        assert choose(db,sources,AT.date(),AT+timedelta(seconds=11))=='tushare_market_rt'
        # On a later day, source order wins without relabelling old observations.
        assert choose(db,sources,AT.date(),AT+timedelta(days=1))=='tushare_market_rt'


@pytest.mark.parametrize('family,expected',[
    ('cninfo','tushare_market_rt'),
    ('tencent_market_rt','tushare_market_rt'),
    ('tushare_market_rt','tencent_market_rt'),
])
def test_current_source_or_catalog_revocation_is_rechecked(client,family,expected):
    setup(client)
    cutoff=AT+timedelta(seconds=1)
    with Session(engine()) as db:
        assert choose(db,providers(db),AT.date(),cutoff)=='tencent_market_rt'
    with Session(engine()) as db,db.begin():
        source=db.get(Provider,source_id(family))
        source.rights={**source.rights,'display':'denied'}
    with Session(engine()) as db:
        assert choose(db,providers(db),AT.date(),cutoff)==expected


@pytest.mark.parametrize('clock',['previous_day','future_tick'])
def test_wrong_day_and_future_native_tick_do_not_make_paid_batch_fresh(client,clock):
    setup(client)
    native=AT-timedelta(days=1) if clock=='previous_day' else AT+timedelta(seconds=10)
    with Session(engine()) as db,db.begin():
        store(db,'tushare_market_rt',[quote('600001.SH',at=native)],AT+timedelta(seconds=1))
    with Session(engine()) as db:
        assert choose(db,providers(db),AT.date(),AT+timedelta(seconds=2))=='tencent_market_rt'
