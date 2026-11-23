"""Received catalog scope is versioned separately from accumulated search origins."""
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion, SourceVersion
from app.market.catalog_scope import capture_payloads, read_catalog_scope, MEMBERS, MANIFEST
from app.market.collect import store_catalog_batch, store_observation
from app.market.models import InstrumentOrigin, SourceSettings
from app.market.protocol import CatalogBatch, CatalogRecord, InstrumentKey, SHANGHAI, SourceFailure, parse_cninfo_catalog
from app.market.service import source_id
from app.models import Instrument, Provider
from test_data_sources import configure, configuration, login
from test_tushare import body as tushare_body, source as tushare_source, TOKEN


def native(codes=('600001','000001','920001'), extra=()):
    return json.dumps(dict(stockList=[dict(code=code,zwjc='合成'+code,orgId='synthetic-'+code,category='A股')
        for code in codes]+list(extra)),ensure_ascii=False).encode()


def setup(client, family='cninfo'):
    headers=login(client)
    configure(client,headers,configuration(family) if family=='cninfo' else tushare_source(family))
    return datetime.now(UTC)+timedelta(seconds=10)


def save(db, body, stamp, family='cninfo'):
    from app.market.tushare import parse_catalog
    provider=db.get(Provider,source_id(family))
    parsed=parse_cninfo_catalog(body) if family=='cninfo' else parse_catalog(body)
    return store_catalog_batch(db,provider,db.get(SourceSettings,provider.id),parsed,stamp,response_body=body)


def read(db, stamp, family='cninfo', max_age=timedelta(days=2)):
    return read_catalog_scope(db,family,stamp,max_age=max_age)


def test_capture_preserves_native_hash_exclusions_version_and_indexes_are_not_members(client):
    stamp=setup(client).astimezone(SHANGHAI)
    body=native(extra=[dict(code='200001',zwjc='合成B股',category='B股')])
    with Session(engine()) as db,db.begin():
        accepted,duplicates=save(db,body,stamp)
        assert accepted==7 and duplicates==0
    with Session(engine()) as db:
        scope=read(db,stamp)
        assert scope and len(scope.instruments)==3 and scope.expected==4
        assert scope.rejected=={'unsupported_share_class':1}
        assert all(key.asset_type=='stock' for key in scope.instruments)
        assert scope.body_sha256==hashlib.sha256(body).hexdigest()
        assert scope.source_version_id==db.scalar(select(SourceVersion.id).where(SourceVersion.source_id==source_id('cninfo')))
        assert not read(db,stamp-timedelta(microseconds=1))
        assert read(db,stamp.astimezone(UTC)).capture_id==scope.capture_id
        assert len(list(db.scalars(select(Instrument))))==7
        assert all(not item.historical_eligible and item.available_at is None
            for item in db.scalars(select(EvidenceVersion)))
        members=db.scalar(select(EvidenceVersion).where(EvidenceVersion.payload['dataset'].astext==MEMBERS))
        assert all(row['reported_listing_status'] is None for row in members.payload['rows'])


def test_removed_members_do_not_reappear_from_cumulative_origins_and_unchanged_recapture_is_fresh(client):
    stamp=setup(client)
    with Session(engine()) as db,db.begin():save(db,native(),stamp)
    with Session(engine()) as db,db.begin():save(db,native(('600001','000001')),stamp+timedelta(minutes=1))
    with Session(engine()) as db:
        first=read(db,stamp);second=read(db,stamp+timedelta(minutes=1))
        assert len(first.instruments)==3 and len(second.instruments)==2
        assert first.capture_id!=second.capture_id and first.scope_sha256!=second.scope_sha256
        assert db.scalar(select(func.count()).select_from(InstrumentOrigin))==7
        old_origin=db.scalar(select(InstrumentOrigin).join(Instrument,Instrument.id==InstrumentOrigin.instrument_id)
            .where(Instrument.symbol=='000001',Instrument.asset_type=='stock'))
        assert old_origin.last_observed_at==stamp
    with Session(engine()) as db,db.begin():save(db,native(('600001','000001')),stamp+timedelta(hours=1))
    with Session(engine()) as db:
        third=read(db,stamp+timedelta(hours=1),max_age=timedelta(minutes=1))
        assert third and third.capture_id!=second.capture_id and third.scope_sha256==second.scope_sha256
        assert read(db,stamp).capture_id==first.capture_id


@pytest.mark.parametrize('change',['display','cache','enabled'])
def test_current_revocation_hides_snapshot_without_deleting_history(client,change):
    stamp=setup(client)
    with Session(engine()) as db,db.begin():save(db,native(),stamp)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('cninfo'))
        if change=='enabled':provider.enabled=False
        else:provider.rights={**provider.rights,change:'denied'}
    with Session(engine()) as db:
        assert read(db,stamp) is None
        assert db.scalar(select(func.count()).select_from(EvidenceVersion))==2


def test_incomplete_newer_capture_never_falls_back_to_old_members(client):
    stamp=setup(client)
    with Session(engine()) as db,db.begin():
        save(db,native(),stamp)
        provider=db.get(Provider,source_id('cninfo'));config=db.get(SourceSettings,provider.id)
        later=stamp+timedelta(minutes=1);body=native(('600002',))
        key,payload=capture_payloads(db,provider,parse_cninfo_catalog(body),body,later)[-1]
        store_observation(db,provider,config,source_key=key,kind='reference',payload=payload,
            received_at=later,repeated_state=True)
    with Session(engine()) as db:
        assert len(read(db,stamp).instruments)==3
        assert read(db,later) is None


def test_full_page_closure_and_hashes_are_checked_without_per_instrument_writes(client):
    stamp=setup(client)
    rows=[CatalogRecord(InstrumentKey('SSE',str(600000+i),'stock'),'合成'+str(i),None,'A股') for i in range(5001)]
    batch=CatalogBatch(records=rows,expected=len(rows))
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('cninfo'));config=db.get(SourceSettings,provider.id)
        payloads=capture_payloads(db,provider,batch,b'synthetic-large-catalog-contract',stamp)
        assert [len(payload.rows) for _,payload in payloads]==[5000,1,1]
        for key,payload in payloads:
            store_observation(db,provider,config,source_key=key,kind='reference',payload=payload,
                received_at=stamp,repeated_state=True)
    with Session(engine()) as db:
        scope=read(db,stamp)
        assert scope and len(scope.instruments)==5001 and len(scope.page_ids)==2
        assert db.scalar(select(func.count()).select_from(Instrument))==0


def test_snapshot_has_exact_source_version_and_age_and_never_uses_old_unversioned_origins(client):
    stamp=setup(client)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('cninfo'));config=db.get(SourceSettings,provider.id)
        store_catalog_batch(db,provider,config,parse_cninfo_catalog(native()),stamp)
    with Session(engine()) as db:assert read(db,stamp) is None
    with Session(engine()) as db,db.begin():
        save(db,native(),stamp)
        old=db.scalar(select(SourceVersion).where(SourceVersion.source_id==source_id('cninfo')));old_id=old.id
        descriptor={**old.descriptor,'synthetic_version':'two'}
        newer=SourceVersion(source_id=old.source_id,descriptor=descriptor,
            content_hash=content_hash(descriptor),recorded_at=stamp+timedelta(seconds=5))
        db.add(newer);db.flush();new_id=newer.id
        save(db,native(),stamp+timedelta(seconds=10))
    with Session(engine()) as db:
        assert read(db,stamp).source_version_id==old_id
        assert read(db,stamp+timedelta(seconds=10)).source_version_id==new_id
        assert read(db,stamp+timedelta(seconds=21),max_age=timedelta(seconds=10)) is None


def test_invalid_capture_is_refused_before_updating_identities(client):
    stamp=setup(client)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('cninfo'));config=db.get(SourceSettings,provider.id)
        invalid=CatalogBatch(records=parse_cninfo_catalog(native()).records,expected=4)
        with pytest.raises(SourceFailure,match='invalid_catalog_capture'):
            store_catalog_batch(db,provider,config,invalid,stamp,response_body=native())
        assert db.scalar(select(func.count()).select_from(Instrument))==0
        assert not db.scalar(select(EvidenceVersion.id))


def test_tushare_listed_scope_stays_separate_from_free_catalog(client,monkeypatch):
    from app.config import settings
    from pydantic import SecretStr
    monkeypatch.setattr(settings(),'tushare_token',SecretStr(TOKEN))
    stamp=setup(client,'tushare_stock_basic')
    fields=['ts_code','symbol','name','exchange','list_status']
    body=tushare_body(fields,[['000001.SZ','000001','合成股票','SZSE','L'],
        ['600001.SH','600001','合成旧状态','SSE','D']])
    with Session(engine()) as db,db.begin():save(db,body,stamp,'tushare_stock_basic')
    with Session(engine()) as db:
        scope=read(db,stamp,'tushare_stock_basic')
        assert scope and len(scope.instruments)==1 and scope.rejected=={'instrument_mismatch':1}
        assert scope.family=='tushare_stock_basic' and read(db,stamp,'cninfo') is None
        members=db.scalar(select(EvidenceVersion).where(EvidenceVersion.payload['dataset'].astext==MEMBERS))
        assert members.payload['rows'][0]['reported_listing_status']=='L'


@pytest.mark.parametrize('outcome',['empty','corrupt_page'])
def test_newest_refused_or_changed_capture_never_reuses_an_older_scope(client,outcome):
    stamp=setup(client)
    with Session(engine()) as db,db.begin():
        save(db,native(),stamp)
        later=stamp+timedelta(minutes=1)
        if outcome=='empty':
            body=json.dumps(dict(stockList=[dict(code='600002',zwjc=name,category='A股')
                for name in ('合成一','合成冲突')]),ensure_ascii=False).encode()
            save(db,body,later)
        else:
            body=native(('600003',));provider=db.get(Provider,source_id('cninfo'))
            config=db.get(SourceSettings,provider.id)
            payloads=capture_payloads(db,provider,parse_cninfo_catalog(body),body,later)
            payloads[0][1].rows[0]['symbol']='600004'
            for key,payload in payloads:
                store_observation(db,provider,config,source_key=key,kind='reference',payload=payload,
                    received_at=later,repeated_state=True)
    with Session(engine()) as db:
        assert len(read(db,stamp).instruments)==3
        assert read(db,later) is None
