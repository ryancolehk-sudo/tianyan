"""Old archive queues must never delay fresh market jobs; no supplier is called."""
from datetime import UTC,datetime,timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.market.service import source_id
from app.models import Job,User
from app.worker import claim
from test_data_sources import login


@pytest.mark.parametrize('kind',['funds.archive','direction.archive','breadth.archive'])
def test_old_archive_cannot_delay_a_new_current_market_job(client,kind):
    login(client)
    clock=datetime.now(UTC)
    with Session(engine(),expire_on_commit=False) as db,db.begin():
        owner=db.scalar(select(User).where(User.username=='owner'))
        old=Job(owner_id=owner.id,kind=kind,created_at=clock-timedelta(days=1),
            available_at=clock-timedelta(days=1),payload={'source_id':str(source_id('eastmoney_funds'))})
        current=Job(owner_id=owner.id,kind='source.collect',available_at=clock-timedelta(seconds=1),
            payload={'source_id':str(source_id('tushare_market_rt'))})
        db.add_all([old,current]);db.flush()
    assert claim().id==current.id
    assert claim().id==old.id
