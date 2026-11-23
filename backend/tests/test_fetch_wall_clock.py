"""Captured receipt clocks and immutable runs survive real OS wall-clock steps."""
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import engine
from app.market.models import DataFetchRun
from app.market.schemas import FetchRun
from test_data_resilience import CLOCK,scenario,source


def run(provider,**values):
    return DataFetchRun(source_id=provider.id,capability='quote.live',
        started_at=CLOCK+timedelta(milliseconds=700),received_at=CLOCK,
        status='complete',reason=None,expected=1,accepted=1,rejected=0,duplicates=0,
        elapsed_ms=80,request_count=1,cost_cny=None,**values)


def test_negative_wall_interval_is_saved_exactly_and_reported_separately(scenario):
    with Session(engine()) as db,db.begin():
        provider,_=source(db,scenario,'tushare_market_rt',symbols=[])
        record=run(provider);db.add(record);db.flush()
        assert record.received_at==CLOCK and record.started_at==CLOCK+timedelta(milliseconds=700)
        dto=FetchRun.model_validate(record)
        assert dto.clock_regressed and dto.elapsed_ms==80
        assert dto.received_at==record.received_at and dto.started_at==record.started_at


def test_received_run_is_still_immutable_after_clock_compatibility(scenario):
    with Session(engine()) as db,db.begin():
        provider,_=source(db,scenario,'tushare_market_rt',symbols=[])
        record=run(provider);db.add(record);db.flush()
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                record.received_at=CLOCK+timedelta(seconds=1);db.flush()


def test_elapsed_duration_and_request_count_constraints_are_unchanged(scenario):
    with Session(engine()) as db,db.begin():
        provider,_=source(db,scenario,'tushare_market_rt',symbols=[])
        for field in ('elapsed_ms','request_count'):
            with pytest.raises(IntegrityError):
                with db.begin_nested():
                    record=run(provider);setattr(record,field,-1);db.add(record);db.flush()
