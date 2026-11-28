"""Latest attempts and successes per source, using the collection history index."""
from sqlalchemy import select, true, tuple_
from sqlalchemy.orm import aliased, load_only, undefer

from app.market.models import DataFetchRun
from app.models import Job


def receipt_jobs(db, runs):
    ids = {run.job_id for run in runs if run.job_id is not None}
    if not ids:
        return {}
    jobs = select(Job).where(Job.id.in_(ids)).options(load_only(Job.id, Job.kind, Job.payload, Job.created_at))
    return {job.id: job for job in db.scalars(jobs)}


def _source_capability_keys(source_ids):
    # The first two columns of ix_fetch_source_capability_time identify a
    # stream. Seek to its successor so enumeration reads one key per stream,
    # even when years of receipts share the same source and capability.
    run = DataFetchRun
    columns = (run.source_id, run.capability)
    first = select(*columns).order_by(*columns).limit(1).subquery()
    keys = select(first.c.source_id, first.c.capability).cte('source_capabilities', recursive=True)
    following = (select(*columns)
                 .where(tuple_(*columns) > tuple_(keys.c.source_id, keys.c.capability))
                 .order_by(*columns).limit(1).correlate(keys).lateral('next_stream'))
    keys = keys.union_all(select(following.c.source_id, following.c.capability)
                          .select_from(keys).join(following, true()))
    return select(keys.c.source_id, keys.c.capability).where(keys.c.source_id.in_(source_ids)).subquery()


def latest_streams(db, source_ids, clock):
    """Newest committed receipt in each recorded source/capability stream, including archives."""
    if not source_ids:
        return []
    keys = _source_capability_keys(source_ids)
    run = DataFetchRun
    head = (select(run).where(run.source_id == keys.c.source_id, run.capability == keys.c.capability,
        run.received_at <= clock).order_by(run.received_at.desc(), run.id.desc()).limit(1).correlate(keys))
    item = aliased(run, head.lateral('latest_stream'))
    return list(db.scalars(select(item).select_from(keys).join(item, true())))


def latest_source_runs(db, source_ids, success, *conditions):
    if not source_ids:
        return {}, {}
    run = DataFetchRun
    # Capabilities used by older runs can differ from today's adapter registry.
    keys = _source_capability_keys(source_ids)
    newest = (select(run).where(run.source_id == keys.c.source_id, run.capability == keys.c.capability, *conditions)
              .order_by(run.received_at.desc(), run.id.desc()).limit(1).correlate(keys))
    attempt = aliased(run, newest.lateral('latest_attempt'))
    succeeded = aliased(run, newest.where(success).lateral('latest_success'))
    pairs = db.execute(select(attempt, succeeded).select_from(keys)
                       .outerjoin(attempt, true()).outerjoin(succeeded, true())
                       .options(undefer(attempt.retry_not_before))).all()

    def latest_per_source(rows):
        found = {}
        for row in rows:
            if row is None:
                continue
            prior = found.get(row.source_id)
            if prior is None or (row.received_at, row.id) > (prior.received_at, prior.id):
                found[row.source_id] = row
        return found

    return latest_per_source(pair[0] for pair in pairs), latest_per_source(pair[1] for pair in pairs)
