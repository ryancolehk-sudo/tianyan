"""A dead builder's live leases recover without stealing synchronous history."""
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import User
from app.read_cache_leader import ReadCacheLeader
from app.read_cache_models import ReadCache
from test_read_cache import seed


def test_only_a_replacement_leader_reclaims_live_leases_once(client, monkeypatch):
    seed(client,monkeypatch)
    clock=datetime.now(UTC)
    today=clock.astimezone(ZoneInfo('Asia/Shanghai')).date()
    variants=[('workbench',{'view':'funds'}),
        ('workbench',{'view':'overview','day':today.isoformat()}),
        ('details',{'day':today.isoformat(),'detail_codes':['000100.SZ']}),
        ('details',{'day':today.isoformat(),'sector':'BK0001.DC'}),
        ('details',{'day':(today-timedelta(days=1)).isoformat(),'detail_codes':['000100.SZ']}),
        ('details',{'day':today.isoformat(),'as_of_time':(clock-timedelta(seconds=1)).isoformat()}),
        ('review-day',{'trade_day':today.isoformat()}),
        ('workbench',{'view':'ladder','day':today.isoformat()})]
    tokens,keys={},[]
    with Session(engine()) as db,db.begin():
        owner=db.scalar(select(User.id))
        for index,(operation,params) in enumerate(variants):
            key=f'{10000+index:064x}';keys.append(key)
            token=uuid.uuid4() if index<7 else None;tokens[key]=token
            db.add(ReadCache(key=key,owner_id=owner,scope='s'*64,operation=operation,params=params,
                dependency_keys=[],payload={'data':{'preserved':True}},dirty=False,
                lease_token=token,lease_until=clock+timedelta(seconds=90) if token else None,
                accessed_at=clock,refresh_after=clock+timedelta(seconds=90)))
    first,second=ReadCacheLeader(),ReadCacheLeader()
    try:
        assert first.acquire() and first.reclaim_pending
        # Once startup recovery has completed, later acquire calls cannot turn
        # routine work into another takeover.
        first.reclaim_pending=False
        assert first.acquire() and not first.reclaim_pending
        assert second.acquire() is False
        with pytest.raises(RuntimeError,match='read_cache_leader_required'):
            second.reclaim()
        with Session(engine()) as db:
            assert all(db.get(ReadCache,key).lease_token==token for key,token in tokens.items())
        # Closing this session is what process exit/database disconnect does.
        first.close()
        assert second.acquire() and second.reclaim()==4
        with Session(engine()) as db,db.begin():
            for index,key in enumerate(keys):
                entry=db.get(ReadCache,key)
                assert entry.payload=={'data':{'preserved':True}}
                if index<4:
                    assert entry.lease_token is None and entry.lease_until is None
                    assert entry.dirty and entry.last_error=='worker_restarted'
                else:
                    assert entry.lease_token==tokens[key] and entry.dirty is False
            replacement=uuid.uuid4()
            db.get(ReadCache,keys[0]).lease_token=replacement
            db.get(ReadCache,keys[0]).lease_until=clock+timedelta(seconds=90)
        assert second.acquire() and second.reclaim()==0
        with Session(engine()) as db:
            assert db.get(ReadCache,keys[0]).lease_token==replacement
    finally:
        second.close();first.close()
