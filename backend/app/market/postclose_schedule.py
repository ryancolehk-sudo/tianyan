"""16:30 Shanghai archive publication with persistent, bounded catch-up.

Runs in the existing scheduler/worker, independently of browser sessions.
Calendar evidence, source rights, supplier budget and job leases still apply.
"""
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import APIError, require_instance
from app.config import settings
from app.db import engine
from app.evidence.common import lock, now
from app.market.models import SourceSettings
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.review_protocol import DATASET_FAMILIES
from app.market.review_store import available_trade_days, family_id, load_dataset
from app.models import AuditEvent, Job, User

ACTION = 'review.postclose.schedule'
DATASETS = ('daily', 'limits', 'pools', 'group_basics', 'pool_details', 'stock_funds', 'market_funds')
HISTORY_SESSIONS = 32
HEIGHT_SESSIONS = 120
MAX_QUEUED = 12
RETRY = timedelta(minutes=15)
TERMINAL = {'permission_missing', 'configuration_changed', 'schema_changed',
            'upstream_redirect', 'unsafe_upstream_response', 'endpoint_not_allowed'}


def deadline(day, clock):
    local = clock.astimezone(SHANGHAI)
    if day < local.date() and time(9) <= local.time() < time(9, 15):
        return None
    opening = time(16, 30)
    if local.time() < opening:
        return datetime.combine(local.date(), opening, SHANGHAI)
    if local.time() >= time(21, 30):
        return datetime.combine(local.date() + timedelta(days=1), time(9), SHANGHAI)
    return None


def validate_archive(db, job, config, clock):
    ctx = job.payload['review_context']
    owner = db.get(User, job.owner_id)
    day = datetime.fromisoformat(ctx['day']).date()
    days = available_trade_days(db, limit=HEIGHT_SESSIONS, as_of_time=clock)
    if (owner is None or not owner.enabled or owner.role != 'admin' or config.owner_id != owner.id
            or job.payload.get('scheduled_daily_archive') is True):
        raise SourceFailure('permission_missing')
    if day not in days or ctx['scope'] != 'all' or ctx['dataset'] not in (*DATASETS, 'emotion_history', 'new_highs', 'new_high_history'):
        raise SourceFailure('review_archive_day_mismatch')
    if ctx['dataset'] == 'emotion_history':
        if day != days[-1] or ctx['start_day'] != days[-HISTORY_SESSIONS:][0].isoformat():
            raise SourceFailure('review_context_invalid')
    elif ctx['dataset'] == 'new_high_history':
        if day != days[-1] or ctx['start_day'] != days[-60:][0].isoformat():
            raise SourceFailure('review_context_invalid')
    elif ctx['start_day'] != ctx['day']:
        raise SourceFailure('review_context_invalid')


def defer_archive(job, clock):
    target = datetime.fromisoformat(job.payload['review_context']['day']).date()
    wait = deadline(target, clock)
    if wait is None:
        return False
    job.state, job.available_at = 'queued', wait
    job.attempts = max(0, job.attempts - 1)
    job.error_code = 'publication_window_wait'
    job.lease_token = job.lease_until = None
    return True


def needs_refresh(saved, day, clock, *, required_start=None):
    if saved.get('state') != 'complete' or not saved.get('row_count'):
        return True
    if required_start is not None and (saved.get('requested_start_day', '9999') > required_start.isoformat()
            or saved.get('requested_end_day', '') < day.isoformat()):
        return True
    # Refresh pre-16:30 archives once; recheck corrected/late publications at
    # 17:30. A completed HTTP response is not a promise of full market coverage.
    local = clock.astimezone(SHANGHAI)
    if day != local.date():
        return False
    received = saved.get('received_at')
    if not received:
        return True
    stamp = datetime.fromisoformat(received) if isinstance(received, str) else received
    anchor = time(17, 30) if local.time() >= time(17, 30) else time(16, 30)
    return stamp < datetime.combine(day, anchor, SHANGHAI)


def queue_due(db, clock):
    """One bounded wave; persisted jobs deduplicate across restarts/workers."""
    from app.market.review_jobs import queue_dataset
    navigation = available_trade_days(db, limit=HEIGHT_SESSIONS, as_of_time=clock)
    days = navigation[-HISTORY_SESSIONS:]
    if not days or deadline(days[-1], clock) is not None:
        return 0
    last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == ACTION))
    if last is not None and last + timedelta(seconds=60) > clock:
        return 0
    db.add(AuditEvent(action=ACTION, resource_type='review',
                      changed_field_names=['postclose_1630_archive'], created_at=clock))
    selections = [('new_high_history', days[-1], navigation[-60:][0]),
                  ('new_highs', days[-1], days[-1]), ('emotion_history', days[-1], days[0])]
    selections += [(dataset, day, day) for day in reversed(days) for dataset in DATASETS
                   if dataset in {'daily', 'group_basics'} or day in days[-20:]]
    selections += [('pools', day, day) for day in reversed(navigation[:-20])]
    selections += [('new_highs', day, day) for day in reversed(navigation[-60:-1])]
    queued = 0
    for dataset, day, first in selections:
        config = db.get(SourceSettings, family_id(DATASET_FAMILIES[dataset]))
        owner = db.get(User, config.owner_id) if config else None
        if config is None or config.paused_reason or owner is None or not owner.enabled or owner.role != 'admin':
            continue
        saved = load_dataset(db, dataset, day, as_of_time=clock, include_rows=False)
        if not needs_refresh(saved, day, clock, required_start=first if dataset in {'emotion_history','new_high_history'} else None):
            continue
        prior = db.scalar(select(Job).where(Job.kind == 'review.collect',
            Job.payload['review_context']['dataset'].astext == dataset,
            Job.payload['review_context']['day'].astext == day.isoformat(),
            Job.payload['review_context']['scope'].astext == 'all',
            Job.payload['source_id'].astext == str(config.source_id),
        ).order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
        repaired = prior is not None and config.configured_at > (prior.completed_at or prior.created_at)
        if prior and (prior.state in {'queued', 'running'} or not repaired and (
                prior.error_code in TERMINAL or (prior.completed_at or prior.created_at) + RETRY > clock)):
            continue
        try:
            queue_dataset(db, owner, dataset, day, start_day=first, force=True, postclose_archive=True,
                          review_latest=dataset in {'new_highs','new_high_history'} and day==navigation[-1])
            queued += 1
        except APIError as error:
            if error.code not in {'review_source_unavailable', 'credentials_missing', 'source_disabled',
                                  'collection_intent_conflict', 'source_rate_limit', 'permission_missing'}:
                raise
        if queued >= MAX_QUEUED:
            break
    return queued


def schedule_postclose():
    if not settings().data_collection_enabled:
        return
    from app.maintenance_control import admit, paused
    with Session(engine()) as db, db.begin():
        require_instance(db)
        if paused(db):
            return
        admit(db)
        lock(db, ACTION)
        queue_due(db, now(db))
