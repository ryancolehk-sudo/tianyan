"""Lookup reduction must preserve real PostgreSQL quote history and rights."""
import uuid
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.collect import store_quote_batch
from app.market.models import InstrumentOrigin, SourceSettings
from app.market.protocol import InstrumentKey, Quote, QuoteBatch, SourceFailure
from app.market.quote_write_cache import warm
from app.market.service import source_id
from app.models import Instrument, Provider
from test_quote_focus import CLOCK, setup


def fixture_batch(client, monkeypatch, count=2):
    setup(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        db.add_all([Instrument(id=uuid.uuid4(), exchange='SZSE', symbol=f'{code:06}',
            asset_type='stock', name=f'本地{code}', valid_from=None, valid_to=None,
            first_received_at=CLOCK-timedelta(days=1)) for code in range(100,100+count)])
    return QuoteBatch(records=[Quote(instrument=InstrumentKey('SZSE',f'{code:06}','stock'),
        name=f'来源{code}', price=Decimal('12.3'), previous_close=Decimal('12'),
        open=Decimal('12.1'), high=Decimal('12.5'), low=Decimal('12'), quote_time=CLOCK,
        received_at=CLOCK, price_unit='CNY', volume_shares=Decimal('12300'),
        amount_cny=Decimal('123456.78')) for code in range(100,100+count)], expected=count)


def source(db):
    provider=db.get(Provider,source_id('tencent_web'))
    return provider,db.get(SourceSettings,provider.id)


def snapshot(db):
    evidence=[{key:getattr(row,key) for key in ('source_id','source_key','source_revision',
        'kind','instrument_id','event_at','published_at','owner_id','visibility','version_number',
        'received_at','available_at','availability','provenance','historical_eligible',
        'rights_basis','payload','content_hash','supersedes_id')}
        for row in db.scalars(select(EvidenceVersion).order_by(EvidenceVersion.source_key))]
    origins=[{key:getattr(row,key) for key in ('instrument_id','source_id','origin_kind',
        'observed_name','first_observed_at','last_observed_at','last_content_hash','source_version_id')}
        for row in db.scalars(select(InstrumentOrigin).order_by(InstrumentOrigin.instrument_id))]
    names=[(row.id,row.name,row.local_name) for row in db.scalars(select(Instrument).order_by(Instrument.id))]
    return evidence,origins,names


@pytest.mark.parametrize('count',[20,200])
def test_batch_lookups_preserve_every_stored_value_with_constant_selects(client,monkeypatch,count):
    batch=fixture_batch(client,monkeypatch,count)
    variants=[]; queries=[]
    for cached in (False,True):
        with monkeypatch.context() as patch,Session(engine()) as db:
            if not cached:patch.setattr('app.market.quote_write_cache.warm',lambda *args:None)
            provider,config=source(db); statements=[]
            def counted(connection,cursor,statement,parameters,context,many):
                if statement.lstrip().upper().startswith('SELECT'):statements.append(statement)
            event.listen(engine(),'before_cursor_execute',counted)
            try:
                assert store_quote_batch(db,provider,config,batch)==(count,0)
            finally:event.remove(engine(),'before_cursor_execute',counted)
            queries.append(len(statements)); variants.append(snapshot(db))
            db.rollback()
    assert variants[0]==variants[1]
    assert queries[0]>=count*5,queries
    assert queries[1]<=10,queries


def test_same_native_timestamp_corrections_and_old_replay_keep_version_chain(client,monkeypatch):
    batch=fixture_batch(client,monkeypatch)
    changed=replace(batch.records[0],price=Decimal('12.4'),received_at=CLOCK+timedelta(seconds=1))
    replay=replace(batch.records[0],received_at=CLOCK+timedelta(seconds=2))
    with Session(engine()) as db,db.begin():
        provider,config=source(db)
        assert store_quote_batch(db,provider,config,QuoteBatch(records=[batch.records[0],changed,replay]))==(2,1)
        assert store_quote_batch(db,provider,config,QuoteBatch(records=[changed,changed]))==(0,2)
        versions=list(db.scalars(select(EvidenceVersion).order_by(EvidenceVersion.version_number)))
        assert [row.version_number for row in versions]==[1,2]
        assert versions[1].supersedes_id==versions[0].id
        assert [row.payload['price'] for row in versions]==['12.3','12.4']
        assert all(row.received_at<=CLOCK+timedelta(seconds=1) for row in versions)


@pytest.mark.parametrize('case',['single','unknown_time','unfocused','missing_identity','ambiguous_identity'])
def test_unproven_batch_keeps_original_lookup_path(client,monkeypatch,case):
    batch=fixture_batch(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        provider,config=source(db)
        if case=='single':batch.records=batch.records[:1]
        if case=='unknown_time':batch.records[0]=replace(batch.records[0],quote_time=None)
        if case=='unfocused':config.config={**config.config,'collection_mode':'configured'}
        if case=='missing_identity':batch.records[0]=replace(batch.records[0],instrument=InstrumentKey('SZSE','000999','stock'))
        if case=='ambiguous_identity':
            db.add(Instrument(id=uuid.uuid4(),exchange='SZSE',symbol='000100',asset_type='stock',
                name='另一时期身份',valid_from=date(2026,1,1),valid_to=None,first_received_at=CLOCK))
            db.flush()
        assert warm(db,provider,config,batch.records) is None
        accepted,duplicates=store_quote_batch(db,provider,config,batch)
        assert accepted==len(batch.records) and duplicates==0


def test_next_batch_rechecks_rights_and_existing_source_claim_preserves_local_name(client,monkeypatch):
    batch=fixture_batch(client,monkeypatch)
    with Session(engine()) as db,db.begin():
        provider,config=source(db)
        instrument=db.scalar(select(Instrument).where(Instrument.symbol=='000100'))
        db.add(InstrumentOrigin(instrument_id=instrument.id,source_id=provider.id,origin_kind='quote',
            observed_name='已有来源名称',first_observed_at=CLOCK-timedelta(days=1),
            last_observed_at=CLOCK-timedelta(days=1),last_content_hash='a'*64,source_version_id=None))
        db.flush()
        assert store_quote_batch(db,provider,config,batch)==(2,0)
        assert instrument.local_name is None
        provider.rights={**provider.rights,'cache':'denied'}
        with pytest.raises(SourceFailure,match='permission_missing'):
            store_quote_batch(db,provider,config,batch)
        assert len(list(db.scalars(select(EvidenceVersion))))==2
