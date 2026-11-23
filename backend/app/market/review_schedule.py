"""Bounded daily archives and next-session recovery, separate from live polling."""
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import APIError, require_instance
from app.config import settings
from app.db import engine
from app.evidence.common import lock, now
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.review_protocol import DAILY_DATASETS, DATASET_FAMILIES, INDEX_CODES
from app.market.review_store import available_trade_days, family_id, load_dataset
from app.market.models import SourceSettings
from app.models import AuditEvent, Job, User

ACTION = 'review.daily.schedule'
INTERVAL = timedelta(minutes=15)
DATASETS = DAILY_DATASETS + ('industry_themes', 'industry_funds', 'members')
PREOPEN_START, PREOPEN_END = time(9), time(9, 15)


def in_archive_preopen(clock):
    return PREOPEN_START <= clock.astimezone(SHANGHAI).time() < PREOPEN_END


def archive_deadline(dataset, clock):
    local = clock.astimezone(SHANGHAI)
    if in_archive_preopen(clock):
        return None
    if local.time() < PREOPEN_START:
        return datetime.combine(local.date(), PREOPEN_START, SHANGHAI)
    start = time(16, 30) if dataset in {'lhb', 'seats', 'stock_funds', 'market_funds'} else time(16)
    if local.time() < start:
        return datetime.combine(local.date(), start, SHANGHAI)
    if local.time() >= time(21, 30):
        return datetime.combine(local.date() + timedelta(days=1), PREOPEN_START, SHANGHAI)
    return None


def archive_target_day(db, clock):
    """Only a proved open session may recover its latest proved closed date."""
    if archive_deadline('daily', clock) is not None:
        return None
    today = clock.astimezone(SHANGHAI).date()
    if in_archive_preopen(clock):
        from app.market.mairui import pool_day
        try:
            if pool_day(db, clock, preopen=True) != today:
                return None
            previous = pool_day(db, clock)
        except SourceFailure:
            return None
        if previous >= today:
            return None
        days = available_trade_days(db, limit=1, end_day=today - timedelta(days=1), as_of_time=clock)
        return previous if days == [previous] else None
    return today if available_trade_days(db, limit=1, as_of_time=clock) == [today] else None


def schedule_daily_archives():
    if not settings().data_collection_enabled:
        return
    from app.maintenance_control import admit, paused
    from app.market.review_jobs import queue_dataset
    with Session(engine()) as db, db.begin():
        require_instance(db)
        if paused(db):
            return
        admit(db)
        clock = now(db)
        if archive_deadline('daily', clock) is not None:
            return
        lock(db, ACTION)
        last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == ACTION))
        if last is not None and last + INTERVAL > clock:
            return
        day = archive_target_day(db, clock)
        if day is None:
            return
        db.add(AuditEvent(action=ACTION, resource_type='review',
            changed_field_names=['previous_close_daily_recovery' if in_archive_preopen(clock)
                                 else 'current_close_daily_archives'], created_at=clock))
        selections = [(dataset, 'all') for dataset in DATASETS]
        selections += [('indices', code) for code in INDEX_CODES]
        for dataset, scope in selections:
            if archive_deadline(dataset, clock) is not None:
                continue
            config = db.get(SourceSettings, family_id(DATASET_FAMILIES[dataset]))
            owner = db.get(User, config.owner_id) if config else None
            if owner is None or not owner.enabled or owner.role != 'admin':
                continue
            saved = load_dataset(db, dataset, day, scope, as_of_time=clock, include_rows=False)
            if saved['state'] == 'complete' and saved.get('row_count', 0) > 0:
                continue
            prior = db.scalar(select(Job).where(Job.kind == 'review.collect',
                Job.payload['source_id'].astext == str(config.source_id),
                Job.payload['review_context']['dataset'].astext == dataset,
                Job.payload['review_context']['day'].astext == day.isoformat(),
                Job.payload['review_context']['scope'].astext == scope,
            ).order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
            if prior is not None:
                if prior.state in {'queued', 'running'}:
                    continue
                if prior.error_code in {'permission_missing', 'configuration_changed', 'schema_changed',
                                        'upstream_redirect', 'unsafe_upstream_response', 'endpoint_not_allowed'}:
                    continue
                if (prior.completed_at or prior.created_at) + INTERVAL > clock:
                    continue
            try:
                queue_dataset(db, owner, dataset, day, scope=scope, force=True, scheduled_archive=True)
            except APIError as error:
                if error.code not in {'review_source_unavailable', 'credentials_missing', 'source_disabled',
                                      'collection_intent_conflict', 'source_rate_limit', 'permission_missing'}:
                    raise
