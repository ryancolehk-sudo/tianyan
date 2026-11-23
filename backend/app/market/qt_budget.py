"""Shared qt.gtimg.cn physical pacing and real backoff, never a vendor quota.

One second is a local bounded-trial policy, not a Tencent entitlement. Focused,
bulk and auction requests share persisted permits/cooldown across processes.
"""
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
from math import ceil
import re
import time
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import lock, now
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SourceFailure
from app.models import AuditEvent, Job, Provider, User

FAMILIES=frozenset({'tencent_web','tencent_market_rt','tencent_auction'})
ACTION='source.request.tencent-quotes'
SPACING=1
FOCUS_WAIT_SECONDS=2
FOCUS_WAIT_KEY='tencent_request_wait'
MAX_RETRY_AFTER=315360000


def retry_after(value, clock=None):
    clock=clock or datetime.now(UTC)
    if not isinstance(value,str) or len(value)>100:return None
    if re.fullmatch(r'\d{1,10}',value):seconds=int(value)
    else:
        try:
            deadline=parsedate_to_datetime(value)
            if deadline.tzinfo is None:return None
            seconds=max(0,ceil((deadline-clock).total_seconds()))
        except (ValueError,TypeError,OverflowError):return None
    if seconds>MAX_RETRY_AFTER:raise SourceFailure('retry_after_out_of_range')
    return seconds


def cooldown_until(db, clock):
    short=db.scalar(select(func.max(DataFetchRun.retry_not_before)).join(Provider,Provider.id==DataFetchRun.source_id)
        .where(Provider.family.in_(FAMILIES),DataFetchRun.retry_not_before>clock))
    long=db.scalar(select(func.max(SourceSettings.legacy_cooldown_until)).join(Provider,Provider.id==SourceSettings.source_id)
        .where(Provider.family.in_(FAMILIES),SourceSettings.legacy_cooldown_until>clock))
    return max(value for value in (short,long) if value is not None) if short or long else None


def record_failure(job, error):
    """Retain the actual supplier deadline even if terminal storage loses a lease.

    The existing durable cooldown column, unlike fetch-run deadlines, supports
    values longer than one day. This creates no extra request or fake fetch run.
    """
    if error.reason not in {'rate_limited','retry_after_out_of_range'}:return
    supplied=type(error.retry_after) is int and 0<error.retry_after<=MAX_RETRY_AFTER
    seconds=error.retry_after if supplied else 300
    with Session(engine()) as db,db.begin():
        lock(db,ACTION);clock=now(db)
        provider=db.get(Provider,UUID(job.payload['source_id']))
        if provider is None or provider.family not in FAMILIES:return
        config=db.get(SourceSettings,provider.id)
        if config is None:return
        if error.reason=='rate_limited':
            deadline=clock+timedelta(seconds=seconds)
            config.legacy_cooldown_until=max(config.legacy_cooldown_until or deadline,deadline)
            current=db.get(Job,job.id)
            if current is not None:
                current.result_ref={**(current.result_ref or {}),'tencent_cooldown':dict(
                    retry_not_before=deadline.isoformat(),seconds=seconds,
                    basis='supplier_retry_after' if supplied else 'local_backoff')}
        else:
            for other in db.scalars(select(SourceSettings).join(Provider,Provider.id==SourceSettings.source_id)
                    .where(Provider.family.in_(FAMILIES))):other.paused_reason=error.reason
        db.add(AuditEvent(actor_id=job.owner_id,action='source.retry_after.tencent',resource_type='job',
            resource_id=job.id,changed_field_names=['supplier_retry_after' if supplied else error.reason],created_at=clock))


def _clear_focus_wait(current):
    if current.result_ref and FOCUS_WAIT_KEY in current.result_ref:
        current.result_ref={key:value for key,value in current.result_ref.items() if key!=FOCUS_WAIT_KEY}


def _focus_wait(current, expected_configuration, clock):
    return dict(observed_at=clock.isoformat(),configuration=expected_configuration,
        lease_hash=sha256(str(current.lease_token).encode()).hexdigest())


def _waiting_focus(db, clock):
    """Only a currently eligible request already waiting at this guard counts.

    This short-lived scheduling hint is not a source request or quota. Do not
    lock another job: its collector must be able to reserve while bulk sleeps.
    """
    from app.integrations.credentials import assert_source_binding
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.realtime_schedule import deferred_realtime
    from app.market.service import permission_reason
    provider=db.scalar(select(Provider).where(Provider.family=='tencent_web'))
    if provider is None or permission_reason(provider):return False
    config=db.get(SourceSettings,provider.id)
    if config is None or config.paused_reason or config.config.get('collection_mode')!='focused':return False
    owner=db.get(User,config.owner_id)
    if owner is None or not owner.enabled:return False
    fingerprint=configuration_fingerprint(provider,config)
    waiting=db.scalars(select(Job).where(Job.kind=='source.collect',Job.state=='running',
        Job.owner_id==config.owner_id,Job.lease_token.is_not(None),Job.lease_until>clock,
        Job.payload['source_id'].astext==str(provider.id),
        Job.payload['scheduled_realtime'].astext=='true',Job.result_ref.has_key(FOCUS_WAIT_KEY)))
    for candidate in waiting:
        if candidate.payload.get('scheduled_realtime') is not True:continue
        hint=(candidate.result_ref or {}).get(FOCUS_WAIT_KEY)
        if not isinstance(hint,dict):continue
        if (hint.get('configuration')!=fingerprint
                or hint.get('lease_hash')!=sha256(str(candidate.lease_token).encode()).hexdigest()):continue
        try:
            observed=datetime.fromisoformat(hint.get('observed_at'))
            if observed.tzinfo is None or not 0<=(clock-observed).total_seconds()<FOCUS_WAIT_SECONDS:continue
            assert_source_binding(db,provider.id,provider.family,candidate.payload)
            if deferred_realtime(db,provider.family,clock,config.interval_seconds) is not None:continue
        except (ValueError,TypeError,SourceFailure):continue
        return True
    return False


def _defer(current, config, deadline, reason):
    _clear_focus_wait(current)
    current.state,current.available_at='queued',deadline
    current.attempts=max(0,current.attempts-1)
    current.error_code=reason
    current.lease_token=current.lease_until=None
    config.next_run_at=max(config.next_run_at,deadline)


def reserve(job, *, expected_configuration, in_progress=False):
    from app.config import settings
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.service import permission_reason
    from app.integrations.credentials import assert_source_binding
    from app.market.realtime_schedule import automatic,deferred_realtime
    from app.maintenance_control import admit,paused
    if not settings().data_collection_enabled:raise SourceFailure('collection_disabled')
    # A received earlier page must not be abandoned just because a short local
    # wall-clock correction makes the last persisted permit appear ahead.
    attempts=16 if in_progress else 5
    yielded_focus=False
    for attempt in range(attempts):
        with Session(engine()) as db,db.begin():
            lock(db,ACTION);clock=now(db)
            if paused(db):raise SourceFailure('maintenance_paused')
            admit(db)
            current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',
                Job.lease_token==job.lease_token,Job.lease_until>clock).with_for_update())
            if current is None:raise SourceFailure('lease_lost')
            provider=db.get(Provider,UUID(current.payload['source_id']))
            config=db.get(SourceSettings,provider.id) if provider else None
            owner=db.get(User,current.owner_id)
            if (provider is None or provider.family not in FAMILIES or config is None
                    or config.owner_id!=current.owner_id or current.payload!=job.payload
                    or configuration_fingerprint(provider,config)!=expected_configuration):
                raise SourceFailure('configuration_changed')
            if not owner or not owner.enabled:raise SourceFailure('permission_missing')
            denied=permission_reason(provider)
            if denied:raise SourceFailure(denied)
            if config.paused_reason:raise SourceFailure('source_paused')
            assert_source_binding(db,provider.id,provider.family,current.payload)
            if automatic(current.payload):
                deadline=deferred_realtime(db,provider.family,clock,config.interval_seconds)
                if deadline is not None:
                    if in_progress:raise SourceFailure('intraday_window_wait',retry_after=max(1,ceil((deadline-clock).total_seconds())))
                    _defer(current,config,deadline,'intraday_window_wait');return False
            deadline=cooldown_until(db,clock)
            if deadline is not None:
                if in_progress:raise SourceFailure('source_budget_wait',retry_after=max(1,ceil((deadline-clock).total_seconds())))
                _defer(current,config,deadline,'source_budget_wait');return False
            last=db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action==ACTION))
            wait=max(0,(last+timedelta(seconds=SPACING)-clock).total_seconds()) if last else 0
            # Give an already waiting focused request one opportunity, then let
            # this page compete normally. A stalled focus cannot hold bulk up
            # indefinitely or create a supplier cooldown/fictitious HTTP run.
            if not wait and provider.family=='tencent_market_rt' and not yielded_focus and _waiting_focus(db,clock):
                wait=SPACING
                yielded_focus=True
            if not wait:
                _clear_focus_wait(current)
                db.add(AuditEvent(actor_id=current.owner_id,action=ACTION,resource_type='job',resource_id=current.id,
                    changed_field_names=['physical_quote_request_permit'],created_at=clock))
                return True
            if attempt==attempts-1:
                if in_progress:raise SourceFailure('source_budget_wait',retry_after=max(1,ceil(wait)))
                _defer(current,config,clock+timedelta(seconds=wait),'source_budget_wait');return False
            if (provider.family=='tencent_web' and config.config.get('collection_mode')=='focused'
                    and current.kind=='source.collect' and current.payload.get('scheduled_realtime') is True):
                current.result_ref={**(current.result_ref or {}),FOCUS_WAIT_KEY:_focus_wait(current,expected_configuration,clock)}
        time.sleep(min(wait+.02,1.05))
    return False
