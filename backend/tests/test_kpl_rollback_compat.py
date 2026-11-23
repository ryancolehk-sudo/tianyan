import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Job
from test_data_resilience import scenario

spec=importlib.util.spec_from_file_location('kpl_rollback_wait',
    Path(__file__).resolve().parents[2]/'scripts/runtime_compat/kpl_leader_wait.py')
compat=importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


@pytest.mark.parametrize('future_wait',[False,True])
def test_passive_rollback_preserves_progress_and_waits_without_an_http_request(scenario,monkeypatch,future_wait):
    monkeypatch.setattr('app.market.direction_sources.fetch',lambda *a,**k:pytest.fail('rollback must not fetch'))
    clock=datetime.now(UTC)
    token=uuid4()
    progress={'leader_completed':['801001'],'leader_failed':[]}
    waiting=clock+timedelta(minutes=20) if future_wait else clock-timedelta(seconds=1)
    with Session(engine()) as db,db.begin():
        job=Job(owner_id=scenario,kind='direction.archive',payload={'kpl_leaders':True},
            state='running',attempts=2,available_at=waiting,lease_token=token,
            lease_until=clock+timedelta(minutes=1),result_ref=progress)
        db.add(job);db.flush();identity=job.id
    assert compat.defer_kpl_leader_packet(SimpleNamespace(id=identity,lease_token=token,payload={'kpl_leaders':True}))
    with Session(engine()) as db:
        saved=db.get(Job,identity)
        assert saved.state=='queued' and saved.attempts==1
        assert saved.result_ref==progress and saved.error_code=='collector_version_wait'
        assert saved.available_at>=max(waiting,clock+timedelta(minutes=5))
        assert saved.lease_token is None and saved.lease_until is None and saved.completed_at is None


def test_passive_rollback_cannot_requeue_a_replaced_lease(scenario):
    clock=datetime.now(UTC)
    token=uuid4()
    with Session(engine()) as db,db.begin():
        job=Job(owner_id=scenario,kind='direction.archive',payload={'kpl_leaders':True},
            state='running',attempts=1,lease_token=token,lease_until=clock+timedelta(minutes=1))
        db.add(job);db.flush();identity=job.id
    assert compat.defer_kpl_leader_packet(SimpleNamespace(id=identity,lease_token=uuid4(),payload={'kpl_leaders':True}))
    with Session(engine()) as db:
        saved=db.get(Job,identity)
        assert saved.state=='running' and saved.lease_token==token and saved.attempts==1
    assert not compat.defer_kpl_leader_packet(SimpleNamespace(payload={'kpl_counts':True}))
