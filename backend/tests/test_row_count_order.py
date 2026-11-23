"""Immutable count reuse must preserve full SQL scope and concurrent fallback."""
from datetime import timedelta
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import read_cache_payloads as memo
from app.db import engine
from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion
from app.market import read_facets, resilience, row_count_order
from app.market.models import SourceSettings
from app.market.service import source_id
from app.models import Provider
from test_data_resilience import CLOCK, receipt, scenario, source


@pytest.fixture
def history(scenario):
    memo.clear()
    with Session(engine()) as db, db.begin():
        provider, _ = source(db, scenario, 'tushare_market_rt', symbols=[])
        ident = provider.id
    yield scenario, ident
    memo.clear()


def save(db, ident, size, at, **changes):
    payload = dict(dataset='tushare.rt_market', observed_day=CLOCK.date().isoformat())
    if size is not None:
        payload['rows'] = [dict(ts_code=f'{600000+index}.SH', close='11',
            trade_time=at.strftime('%Y-%m-%d %H:%M:%S')) for index in range(size)]
    payload.update(changes.pop('payload', {}))
    key = uuid.uuid4().hex
    fields = dict(source_id=ident, owner_id=None, visibility='shared', source_key=key,
        source_revision=key, version_number=1, kind='reference', received_at=at, available_at=at,
        availability='known', provenance='source_reported', historical_eligible=False,
        rights_basis='Synthetic test only', payload=payload, content_hash=content_hash(payload))
    fields.update(changes)
    row = EvidenceVersion(**fields)
    db.add(row)
    db.flush()
    return row.id


def scoped(ident, cutoff=CLOCK):
    return read_facets.with_facets(select(EvidenceVersion).join(Provider, Provider.id==EvidenceVersion.source_id)).where(
        EvidenceVersion.source_id==ident, EvidenceVersion.kind=='reference', EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.visibility=='shared', EvidenceVersion.provenance=='source_reported',
        EvidenceVersion.received_at<=cutoff, read_facets.value('dataset')=='tushare.rt_market',
        read_facets.observed_day()==CLOCK.date().isoformat(), Provider.enabled.is_(True),
        Provider.rights['display'].astext=='allowed', Provider.rights['cache'].astext=='allowed')


def test_warm_quality_still_detects_missing_stock_against_largest_not_latest(history):
    _, ident = history
    with Session(engine()) as db, db.begin():
        save(db, ident, 3, CLOCK-timedelta(seconds=60))
        save(db, ident, 2, CLOCK-timedelta(seconds=30))
        save(db, ident, 1, CLOCK)
        provider=db.get(Provider,ident);config=db.get(SourceSettings,ident)
        run=receipt(db,provider,duplicates=1)
        for _ in range(2):
            quality=resilience.saved_quality(db,provider,config,'stock_quote',CLOCK.date(),CLOCK,run)
            assert not quality.valid and quality.reason=='market_scope_changed' and quality.missing_count==2


@pytest.mark.parametrize('lookup', [1, 4096])
def test_lookup_bound_never_limits_actual_candidates(history, monkeypatch, lookup):
    _, ident = history
    monkeypatch.setattr(row_count_order,'MAX_LOOKUP',lookup)
    with Session(engine()) as db, db.begin():
        largest=save(db,ident,5,CLOCK-timedelta(minutes=5))
        save(db,ident,2,CLOCK-timedelta(minutes=1))
        for _ in range(2):assert row_count_order.largest_reference(db,scoped(ident)).id==largest


@pytest.mark.parametrize('change', ['future','other_day','other_dataset','foreign_source','private','manual'])
def test_counts_warmed_outside_current_scope_never_make_evidence_visible(history,change):
    owner, ident = history
    with Session(engine()) as db, db.begin():
        good=save(db,ident,2,CLOCK-timedelta(minutes=1))
        other=ident;at=CLOCK;extra={}
        if change=='future':at+=timedelta(seconds=1)
        elif change=='other_day':extra['payload']={'observed_day':(CLOCK-timedelta(days=1)).date().isoformat()}
        elif change=='other_dataset':extra['payload']={'dataset':'unrelated.reference'}
        elif change=='foreign_source':other=source(db,owner,'ths_industry_funds',symbols=[])[0].id
        elif change=='private':extra=dict(owner_id=owner,visibility='private')
        elif change=='manual':extra=dict(provenance='manual')
        bad=save(db,other,10,at,**extra)
        assert row_count_order.largest_reference(db,select(EvidenceVersion)).id==bad
        assert row_count_order.largest_reference(db,scoped(ident)).id==good


def test_eviction_and_missing_rows_preserve_original_null_order(history,monkeypatch):
    _, ident = history
    monkeypatch.setattr(memo,'MAX_BYTES',16)
    with Session(engine()) as db,db.begin():
        save(db,ident,3,CLOCK-timedelta(minutes=1))
        missing=save(db,ident,None,CLOCK-timedelta(minutes=2))
        original=db.scalar(scoped(ident).order_by(func.jsonb_array_length(EvidenceVersion.payload['rows']).desc(),
            EvidenceVersion.received_at.desc()).limit(1))
        assert original.id==missing
        for _ in range(2):assert row_count_order.largest_reference(db,scoped(ident)).id==missing


@pytest.mark.parametrize('facet_state',['missing','stale'])
def test_warm_counts_still_use_original_when_derived_facets_are_missing_or_stale(history,facet_state):
    from app.market.read_facets import EvidenceReadFacet
    _,ident=history
    with Session(engine()) as db,db.begin():
        largest=save(db,ident,5,CLOCK-timedelta(minutes=2))
        save(db,ident,2,CLOCK-timedelta(minutes=1))
        assert row_count_order.largest_reference(db,scoped(ident)).id==largest
        if facet_state=='missing':db.execute(delete(EvidenceReadFacet))
        else:
            for row in db.scalars(select(EvidenceReadFacet)):row.content_hash='0'*64
        db.flush()
        assert row_count_order.largest_reference(db,scoped(ident)).id==largest


def test_wrong_content_hash_count_does_not_change_order(history):
    _,ident=history
    with Session(engine()) as db,db.begin():
        largest=save(db,ident,5,CLOCK-timedelta(minutes=2))
        small=save(db,ident,2,CLOCK-timedelta(minutes=1))
        memo.put(('market-row-count-v1',str(small),'0'*64),
            dict(evidence_id=str(small),content_hash='0'*64,row_count=100))
        assert row_count_order.largest_reference(db,scoped(ident)).id==largest


def test_equal_row_count_retains_latest_receipt_tiebreaker_and_empty_scope(history):
    _,ident=history
    with Session(engine()) as db,db.begin():
        assert row_count_order.largest_reference(db,scoped(ident)) is None
        save(db,ident,2,CLOCK-timedelta(minutes=2))
        latest=save(db,ident,2,CLOCK-timedelta(minutes=1))
        for _ in range(2):assert row_count_order.largest_reference(db,scoped(ident)).id==latest


def test_new_larger_row_committed_after_memo_lookup_still_participates(history,monkeypatch):
    _, ident=history
    with Session(engine()) as db,db.begin():save(db,ident,2,CLOCK-timedelta(minutes=2))
    inserted=[]
    with Session(engine()) as db:
        row_count_order.largest_reference(db,scoped(ident))
        original=db.scalar
        def final_select(statement,*args,**kwargs):
            if not inserted:
                with Session(engine()) as writer,writer.begin():
                    inserted.append(save(writer,ident,5,CLOCK-timedelta(minutes=1)))
            return original(statement,*args,**kwargs)
        monkeypatch.setattr(db,'scalar',final_select)
        assert row_count_order.largest_reference(db,scoped(ident)).id==inserted[0]


def test_rights_revocation_between_lookup_and_final_select_is_rechecked(history,monkeypatch):
    _,ident=history
    with Session(engine()) as db,db.begin():save(db,ident,2,CLOCK-timedelta(minutes=2))
    with Session(engine()) as db:
        assert row_count_order.largest_reference(db,scoped(ident))
        original=db.scalar;changed=[]
        def final_select(statement,*args,**kwargs):
            if not changed:
                with Session(engine()) as writer,writer.begin():
                    provider=writer.get(Provider,ident);provider.rights={**provider.rights,'display':'denied'}
                changed.append(True)
            return original(statement,*args,**kwargs)
        monkeypatch.setattr(db,'scalar',final_select)
        assert row_count_order.largest_reference(db,scoped(ident)) is None
