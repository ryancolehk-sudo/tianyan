"""Real worker entry points and audited upgrade, isolated synthetic sources only."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion, SourceVersion
from app.market import direction_archive as archive, kpl_atlas_budget as budget
from app.market import kpl_atlas_source as source
from app.market.enable_sector_atlas import run
from app.market.models import SourceSettings
from app.market.protocol import Response
from app.market.registry import SOURCES
from app.market.service import schedule_sources, source_id
from app.models import AuditEvent, Job, Provider
from app.worker import process
from test_direction_recovery import setup as live_setup, fixture, at
from test_enable_sector_atlas import setup as operator_setup


def scheduled_job(identity, kind):
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        job = db.scalar(select(Job).where(Job.kind == kind, Job.state == 'queued',
            Job.payload['source_id'].astext == str(identity)).order_by(Job.created_at.desc()))
        assert job is not None
        job.state = 'running'
        job.attempts = 1
        job.lease_token = uuid.uuid4()
        job.lease_until = datetime.now(UTC) + timedelta(hours=1)
        return job


@pytest.mark.parametrize('kind', ['source.collect', 'direction.archive'])
def test_worker_rejects_old_descriptor_then_prepare_restores_scheduled_collection(client, monkeypatch, tmp_path, kind):
    headers, _, _ = operator_setup(client, monkeypatch, tmp_path)
    monkeypatch.setattr('test_direction_recovery.login', lambda _: headers)
    family = 'direction_kpl_strength'
    clock = live_setup(client, monkeypatch, family,
        clock=at('2026-10-26T16:00:00' if kind == 'direction.archive' else '2026-10-26T10:30:00'))
    identity = source_id(family)
    monkeypatch.setattr(budget, 'now', lambda db: clock[0])
    monkeypatch.setattr(archive, 'now', lambda db: clock[0])
    from app.market import collect
    monkeypatch.setattr(archive, 'datetime', collect.datetime)
    monkeypatch.setattr(archive, 'available_trade_days', lambda *a, **k: [clock[0].date()])
    monkeypatch.setattr('time.sleep', lambda _: None)
    with Session(engine()) as db, db.begin():
        db.get(Provider, identity).adapter_version = 'previous-synthetic-version'
        db.get(SourceSettings, identity).paused_reason = None
    calls = []
    def fetch(family, plate_code=None, **kwargs):
        calls.append(plate_code)
        clock[0] += timedelta(seconds=3)
        return Response(json.dumps(fixture(family, clock[0], plate_code)).encode(), clock[0], 1, 200)
    monkeypatch.setattr(source.source, 'fetch', fetch)
    schedule = archive.schedule_direction_archives if kind == 'direction.archive' else schedule_sources
    schedule()
    rejected = scheduled_job(identity, kind)
    process(rejected)
    with Session(engine()) as db:
        saved = db.get(Job, rejected.id)
        assert (saved.state, saved.error_code) == ('failed', 'adapter_version_mismatch')
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.source_id == identity)) == 0
        assert db.get(SourceSettings, identity).paused_reason is None
    assert calls == []

    run('prepare', clock[0].date())
    with Session(engine()) as db:
        assert db.get(Provider, identity).adapter_version == SOURCES[family].version
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'source.atlas_upgrade'))
        assert db.scalar(select(SourceVersion.id).where(SourceVersion.source_id == identity,
            SourceVersion.descriptor['adapter_version'].astext == SOURCES[family].version))
    clock[0] += timedelta(minutes=16)
    schedule()
    restored = scheduled_job(identity, kind)
    assert restored.id != rejected.id
    process(restored)
    with Session(engine()) as db:
        saved = db.get(Job, restored.id)
        assert (saved.state, saved.error_code) == ('succeeded', None)
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.source_id == identity,
            EvidenceVersion.payload['dataset'].astext == source.DATASET)) == 1
    assert len(calls) == (3 if kind == 'direction.archive' else 1)
