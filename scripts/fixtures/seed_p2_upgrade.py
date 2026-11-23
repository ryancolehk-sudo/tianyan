"""Disposable 0002 fixture only. Never used by the application or normal bootstrap."""
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.cli import bootstrap
from app.db import engine
from app.evidence import schemas as s, service
from app.evidence.models import Snapshot
from app.models import Provider, User
from app.worker import claim, process

if os.getenv("JL_APP_MODE") != "demo" or not re.fullmatch(r"jlpm-check-[a-f0-9]{10}-p2upgrade", os.getenv("JL_TEST_PROJECT", "")):
    raise SystemExit("Refusing to seed outside an explicitly named disposable upgrade fixture")

password = sys.stdin.readline().rstrip()
if not password:
    raise SystemExit("Fixture password must arrive privately through stdin")
with engine().connect() as connection:
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002_evidence"
bootstrap("p2-upgrade", password)
with Session(engine(), expire_on_commit=False) as db, db.begin():
    user = db.scalar(select(User).where(User.username == "p2-upgrade"))
    source = db.scalar(select(Provider).where(Provider.kind == "manual"))
    evidence, _, _ = service.import_evidence(db, user, s.EvidenceInput(source_id=source.id, source_key="synthetic-existing-p2",
        source_revision="1", kind="manual", authorship="original", payload={"text": "Synthetic P2 evidence before the research migration"},
        rights_basis="Self-created disposable upgrade fixture"), uuid.uuid4())
    calendar, _, _ = service.import_calendar(db, user, s.CalendarInput(exchange="SSE", version="synthetic-existing-p2-calendar",
        source_ref="Synthetic declaration, not an actual exchange calendar", rights_basis="self-created",
        coverage_start="2001-01-01", coverage_end="2001-01-01", sessions=[{"trade_day": "2001-01-01", "is_open": True,
            "segments": [{"start": "09:30", "end": "11:30", "kind": "continuous"}]}]), uuid.uuid4())
selection = service.snapshot_selection(user.id, s.SnapshotInput(as_of_time=datetime.now(UTC), required_capabilities=["evidence.manual"]))
with Session(engine()) as db, db.begin():
    receipt, _, _ = service.queue_snapshot(db, user, selection, uuid.uuid4())
job = claim()
assert job is not None and job.kind == "snapshot.create" and job.id == receipt["job_id"]
process(job)
with Session(engine()) as db:
    snapshot = db.get(Snapshot, receipt["result_ref"]["id"])
    assert snapshot.state == "ready"
    frozen = service.snapshot_dto(snapshot)
print(json.dumps({"evidence": evidence.model_dump(mode="json"), "calendar": calendar.model_dump(mode="json"),
                  "snapshot": frozen.model_dump(mode="json")}))
