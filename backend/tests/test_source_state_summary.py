"""Batch live-source health without admitting newer historical collections."""
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.db import engine
from app.market.models import DataFetchRun
from app.market.service import _live_run_summaries
from app.models import Job, Provider, User


@pytest.mark.parametrize('successful_status', ['complete', 'partial', 'empty'])
def test_batch_preserves_latest_tie_success_and_backfill_exclusion(successful_status):
    clock = datetime(2026, 10, 30, 8, tzinfo=UTC)
    with Session(engine()) as db, db.begin():
        owner = User(username='synthetic-health', password_hash='unused-fixture', role='admin')
        providers = [Provider(name=f'Synthetic health {i}', family=f'synthetic-health-{i}',
            kind='market', rights={}) for i in range(4)]
        db.add_all([owner, *providers]); db.flush()
        source_ids = [row.id for row in providers]
        ordinary = Job(owner_id=owner.id, kind='source.collect', payload={})
        historical = Job(owner_id=owner.id, kind='source.collect', payload={'minute_context': {}})
        db.add_all([ordinary, historical]); db.flush()

        def run(source, received, status, *, ident=None, job=None):
            row = DataFetchRun(id=ident or uuid4(), source_id=source, capability='quote',
                job_id=job.id if job else None, started_at=received, received_at=received,
                status=status, request_count=1)
            db.add(row)
            return row

        run(source_ids[0], clock, successful_status)
        run(source_ids[0], clock+timedelta(minutes=1), 'error', ident=UUID(int=1))
        latest = run(source_ids[0], clock+timedelta(minutes=1), 'error',
            ident=UUID(int=2), job=ordinary)
        run(source_ids[0], clock+timedelta(minutes=2), 'complete', job=historical)
        run(source_ids[1], clock+timedelta(minutes=2), 'complete', job=historical)
        run(source_ids[3], clock+timedelta(minutes=3), 'complete')
        latest_id = latest.id

    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine(), 'before_cursor_execute', capture)
    try:
        with Session(engine()) as db:
            rows = _live_run_summaries(db, source_ids[:3])
            assert set(rows) == {source_ids[0]}
            assert rows[source_ids[0]][0].id == latest_id
            assert rows[source_ids[0]][1] == clock
    finally:
        event.remove(engine(), 'before_cursor_execute', capture)
    assert len(statements) == 1


def test_no_configured_sources_needs_no_history_query():
    class UnusedDatabase:
        def execute(self, *args, **kwargs):
            raise AssertionError('Empty source list should not query history')
    assert _live_run_summaries(UnusedDatabase(), []) == {}
