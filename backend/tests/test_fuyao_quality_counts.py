"""Fuyao count reuse keeps largest-history membership and the original scope."""
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import read_cache_payloads as memo
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import resilience, row_count_order
from app.market.collect import store_reference_batch
from test_data_resilience import CLOCK, scenario
from test_fuyao_market import batch, raw, scene
from test_row_count_order import save as save_reference


@pytest.fixture(autouse=True)
def clean_memo():
    memo.clear()
    yield
    memo.clear()


@pytest.mark.parametrize('lookup',[1,4096])
def test_warm_full_snapshot_compares_largest_history_beyond_lookup_bound(scenario,monkeypatch,lookup):
    monkeypatch.setattr(row_count_order,'MAX_LOOKUP',lookup)
    with Session(engine()) as db,db.begin():
        _,_,provider,config,run=scene(db,scenario,monkeypatch)
        at=CLOCK-timedelta(seconds=60)
        store_reference_batch(db,provider,config,batch([raw(),raw('600001.SH'),raw('600002.SH')],at),at)
        at=CLOCK-timedelta(seconds=30)
        store_reference_batch(db,provider,config,batch([raw(),raw('600001.SH')],at),at)
        for _ in range(2):
            quality=resilience.saved_quality(db,provider,config,'stock_quote',CLOCK.date(),CLOCK,run)
            assert not quality.valid and quality.reason=='market_scope_changed' and quality.missing_count==2
            assert quality.received_at==CLOCK and quality.source_time is None


@pytest.mark.parametrize('outside',['future','private','manual','other_day'])
def test_warm_outside_scope_counts_cannot_reject_an_unchanged_whole_snapshot(scenario,monkeypatch,outside):
    with Session(engine()) as db,db.begin():
        _,_,provider,config,run=scene(db,scenario,monkeypatch)
        at=CLOCK+timedelta(seconds=1) if outside=='future' else CLOCK-timedelta(seconds=30)
        payload=batch([raw(),raw('600001.SH')],at).records[0][1].model_dump(mode='json')
        extra={}
        if outside=='private':extra=dict(visibility='private',owner_id=scenario)
        elif outside=='manual':extra=dict(provenance='manual')
        elif outside=='other_day':
            payload['observed_day']=(CLOCK-timedelta(days=1)).date().isoformat()
        wrong=save_reference(db,provider.id,None,at,payload=payload,**extra)
        assert row_count_order.largest_reference(db,select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id)).id==wrong
        for _ in range(2):
            quality=resilience.saved_quality(db,provider,config,'stock_quote',CLOCK.date(),CLOCK,run)
            assert quality.valid and quality.reason is None and quality.missing_count==0
            assert quality.received_at==CLOCK and quality.source_time is None
