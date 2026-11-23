"""Start-anchored cadence for successful automatic whole-market samples only."""
from datetime import datetime, timedelta

from app.models import Job
from app.market.cadence_policy import FAST_FLOORS

FAMILIES = frozenset({'direction_kpl_strength', *FAST_FLOORS})
HISTORY_KEYS = frozenset({'minute_context', 'reference_context', 'dated_context',
    'daily_history', 'backfill_context', 'full_minute_context', 'review_context', 'funds_history_context'})


def automatic_sample(family, payload):
    return (family in FAMILIES and payload.get('scheduled_realtime') is True
        and payload.get('schema_probe') is not True
        and not HISTORY_KEYS.intersection(payload)
        and not (isinstance(payload.get('integration_context'), dict)
            and payload['integration_context'].get('probe') is True))


def successful_anchor(family, payload, started, received, status, requests):
    if (not automatic_sample(family, payload) or status not in {'complete', 'partial'}
            or not requests or started is None or received is None or started > received):
        return None
    return started


def run_anchor(db, family, run):
    """Legacy/manual/detail runs retain receipt spacing unless auto intent is proven."""
    if family not in FAMILIES or run is None or run.job_id is None:
        return None
    job = db.get(Job, run.job_id)
    if job is None or job.kind != 'source.collect' or job.payload.get('source_id') != str(run.source_id):
        return None
    # Old runs used task entry time. Only new, explicitly recorded physical
    # request starts may shorten their receipt-based scheduling gate.
    try:
        recorded = datetime.fromisoformat((job.result_ref or {})['sample_request_started_at'])
    except (KeyError, TypeError, ValueError):
        return None
    if recorded != run.started_at:
        return None
    return successful_anchor(family, job.payload, run.started_at, run.received_at,
        run.status, run.request_count)


def successful_next(family, payload, started, received, status, requests, interval):
    anchor = successful_anchor(family, payload, started, received, status, requests)
    if anchor is None:
        return None
    from app.market.realtime_schedule import align_realtime
    from app.market.cadence_policy import automatic_interval
    # A slow request never queues catch-up samples in the past. The physical
    # permit, window, supplier cooldown and configured intervals still apply.
    return align_realtime(family, max(received, anchor + timedelta(seconds=automatic_interval(family, interval, anchor))))
