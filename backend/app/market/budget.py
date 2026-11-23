"""Conservative shared anonymous budget, persisted before a network attempt."""
from datetime import datetime, time, timedelta
from uuid import UUID
from math import ceil

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import lock, now
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SHANGHAI, SourceFailure
from app.models import AuditEvent, Job, Provider

ASHARE_FAMILIES = frozenset({'ashare_public', 'ashare_breadth', 'ashare_sector_heat'})
ACTION = 'source.request.ashare'
INTERVAL_SECONDS = 35
MAIRUI_ACTION = 'source.request.mairui'
MAIRUI_DAILY_LIMIT = 450  # Free500 leaves50 for manual or other personal use.
TUSHARE_ACTION = 'source.request.tushare'
EASTMONEY_ACTION = 'source.request.eastmoney-trading'
# Eastmoney has no verified daily entitlement of 1200 requests. Govern load by
# persisted physical-request spacing and actual upstream backoff, not a daily
# counter that can disable an otherwise healthy live feed before the close.
CNINFO_NEWS_ACTION = 'source.request.cninfo-news'
CNINFO_NEWS_DAILY_LIMIT = 1600  # Local host protection, not a publisher entitlement or quota.


def protect_live_budget(db, job, clock, limit, used, families):
    """Reserve the remaining configured live polls before admitting history.

    This is conservative local capacity planning, not a supplier quota claim.
    Failed attempts still consume the shared persistent allowance.
    """
    history = job.kind in {'funds.focus', 'funds.archive', 'direction.archive', 'breadth.archive', 'review.collect'} or any(
        key in job.payload for key in ('minute_context', 'daily_history', 'dated_context', 'reference_context', 'full_minute_context'))
    if not history or job.payload.get('review_context', {}).get('dataset', '').startswith('calendar_'):
        return None
    local = clock.astimezone(SHANGHAI)
    from app.market.realtime_schedule import COLLECTION_END
    close = datetime.combine(local.date(), COLLECTION_END, SHANGHAI)
    if local >= close:
        return None
    from app.market.session import market_session
    if market_session(db, clock).is_trading_day is False:
        return None
    from app.market.minute import FAMILY_INTERVALS
    from app.market.service import permission_reason
    from app.market.cadence_policy import minimum_interval, remaining_requests
    reserve = 0
    for provider, config in db.execute(select(Provider, SourceSettings).join(
        SourceSettings, SourceSettings.source_id == Provider.id).where(Provider.family.in_(families))):
        if permission_reason(provider) or config.paused_reason:
            continue
        minimum = int(FAMILY_INTERVALS[provider.family].removesuffix('m'))*60 if provider.family in FAMILY_INTERVALS else minimum_interval(provider.family)
        # The observed one-hop allocation can consume two permits per table.
        per_round = 6 if provider.family == 'eastmoney_funds' else 1
        reserve += remaining_requests(provider.family,max(minimum, config.interval_seconds),clock) * per_round
    return close if used + 1 + reserve > limit else None


def reserve_news_request(job, *, expected_configuration=None):
    """Persist a permit before each CNINFO news page, including failed HTTPs.

    Only this new paginated adapter uses the allowance. Existing RSS and other
    supplier rules keep their own budgets. A refused permit sends no HTTP.
    """
    from app.config import settings
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.service import permission_reason
    if not settings().data_collection_enabled:
        raise SourceFailure('collection_disabled')
    with Session(engine()) as db, db.begin():
        lock(db, CNINFO_NEWS_ACTION)
        clock = now(db)
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running',
            Job.lease_token == job.lease_token, Job.lease_until > clock).with_for_update())
        if current is None:
            raise SourceFailure('lease_expired')
        if current.payload != job.payload:
            raise SourceFailure('configuration_changed')
        provider = db.get(Provider, UUID(current.payload['source_id']))
        config = db.get(SourceSettings, provider.id) if provider else None
        if provider is None or config is None or provider.family != 'cninfo_announcements':
            raise SourceFailure('source_not_configured')
        denied = permission_reason(provider)
        if denied:
            raise SourceFailure(denied)
        if config.paused_reason:
            raise SourceFailure('source_paused')
        if (config.owner_id != current.owner_id or config.interval_seconds < 900 or config.config.get('symbols')
                or (expected_configuration is not None
                    and configuration_fingerprint(provider, config) != expected_configuration)):
            raise SourceFailure('configuration_changed')
        cooldown = db.scalar(select(func.max(DataFetchRun.retry_not_before)).join(
            Provider, Provider.id == DataFetchRun.source_id).where(
                Provider.family == 'cninfo_announcements', DataFetchRun.retry_not_before > clock))
        if cooldown:
            raise SourceFailure('rate_limited', retry_after=max(1, int((cooldown - clock).total_seconds()) + 1))
        start = datetime.combine(clock.astimezone(SHANGHAI).date(), time.min, SHANGHAI)
        end = start + timedelta(days=1)
        used = db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == CNINFO_NEWS_ACTION, AuditEvent.created_at >= start, AuditEvent.created_at < end))
        if used >= CNINFO_NEWS_DAILY_LIMIT:
            raise SourceFailure('rate_limited', retry_after=max(1, int((end - clock).total_seconds()) + 1))
        db.add(AuditEvent(actor_id=current.owner_id, action=CNINFO_NEWS_ACTION, resource_type='job',
            resource_id=current.id, changed_field_names=['personal_cninfo_request_permit'], created_at=clock))
    return True


def eastmoney_cooldown_until(db, clock, *, job=None):
    """Share real throttling; an archive transport pause belongs to archives.

    A failed minute-history endpoint is not evidence that the ranking endpoint
    rejected today's polls. Both lanes still share permits and daily budgets.
    """
    from app.market.eastmoney_trading import FAMILIES
    # A reviewed local-cap release never rewrites the original failure receipt.
    # Only explicitly identified old local-cap records are excluded; an actual
    # supplier 429/Retry-After remains shared by every lane.
    released = select(AuditEvent.resource_id).where(
        AuditEvent.action == 'source.local_budget_released',
        AuditEvent.resource_type == 'data_fetch_run')
    scope = and_(DataFetchRun.reason == 'rate_limited', DataFetchRun.id.not_in(released))
    if job is not None and job.kind == 'funds.archive':
        from app.market.fund_history import CAPABILITY
        scope = or_(scope, and_(
            DataFetchRun.source_id == UUID(job.payload['source_id']),
            DataFetchRun.capability == CAPABILITY))
    return db.scalar(select(func.max(DataFetchRun.retry_not_before)).join(Provider,Provider.id==DataFetchRun.source_id).where(
        Provider.family.in_(FAMILIES),DataFetchRun.retry_not_before>clock,scope))


def reserve_eastmoney_request(job, *, minimum_interval=2):
    """Persistent shared pacing; bulk archives may request a slower interval."""
    import time as timer

    from app.market.eastmoney_trading import FAMILIES
    from app.market.service import permission_reason
    if type(minimum_interval) is not int or not 2<=minimum_interval<=30:
        raise ValueError('Public request spacing must be between 2 and 30 seconds.')
    for _ in range(4):
        delay=0
        with Session(engine()) as db, db.begin():
            lock(db,EASTMONEY_ACTION)
            clock=now(db)
            current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',Job.lease_token==job.lease_token,
                Job.lease_until>clock).with_for_update())
            if current is None:
                raise SourceFailure('lease_expired')
            provider=db.get(Provider,UUID(current.payload['source_id']))
            config=db.get(SourceSettings,provider.id) if provider else None
            if provider is None or config is None or provider.family not in FAMILIES or permission_reason(provider):
                raise SourceFailure('permission_missing')
            from app.market.auction_schedule import schema_recheck
            probe=current.payload.get('schema_probe') is True and schema_recheck(provider.family,config.paused_reason)
            if config.paused_reason and not probe:
                raise SourceFailure('source_paused')
            if current.payload.get('scheduled_eastmoney') is True or probe:
                from app.market.intraday_schedule import deferred_intraday
                deadline=deferred_intraday(db,clock,config.interval_seconds,[],batch=True)
                if deadline is not None:
                    raise SourceFailure('intraday_window_wait',retry_after=max(1,int((deadline-clock).total_seconds())+1))
            cooldown=eastmoney_cooldown_until(db,clock,job=current)
            if cooldown:
                raise SourceFailure('rate_limited',retry_after=max(1,int((cooldown-clock).total_seconds())+1))
            if current.kind == 'funds.archive':
                sources = [str(value) for value in db.scalars(select(Provider.id).where(
                    Provider.family.in_(FAMILIES), Provider.enabled.is_(True)))]
                due = db.scalar(select(Job.id).where(
                    Job.kind.in_(['source.collect', 'funds.focus']),
                    Job.payload['source_id'].astext.in_(sources),
                    or_(and_(Job.state == 'queued', Job.available_at <= clock),
                        and_(Job.state == 'running', Job.lease_until > clock))).limit(1))
                if due is not None:
                    raise SourceFailure('local_request_wait', retry_after=minimum_interval)
            last=db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action==EASTMONEY_ACTION))
            delay=max(0,(last+timedelta(seconds=minimum_interval)-clock).total_seconds()) if last else 0
            if not delay:
                if current.kind=='funds.archive' and current.payload.get('funds_history_context',{}).get('mode')=='intraday_recovery':
                    from app.market.fund_history import _validate
                    _validate(db,current,clock)
                db.add(AuditEvent(actor_id=current.owner_id,action=EASTMONEY_ACTION,resource_type='job',resource_id=job.id,
                    changed_field_names=['public_ranking_request_permit'],created_at=clock))
                return
        timer.sleep(min(delay+.02,minimum_interval+.1))
    raise SourceFailure('local_request_wait',retry_after=minimum_interval)


def tushare_cooldown_until(db, clock):
    from app.market.tushare_cooldown import cooldown_until
    return cooldown_until(db,clock)


def fuyao_cooldown_until(db,clock):
    from app.market.fuyao_cooldown import cooldown_until
    return cooldown_until(db,clock)


def reserve_fuyao_request(job,*,expected_configuration=None,in_progress=False):
    """Every physical request, including manual probes, respects shared 429."""
    from app.market.connected_specs import FUYAO_FAMILIES
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.service import permission_reason
    from app.market.fuyao_cooldown import ACTION as FUYAO_COOLDOWN_ACTION
    with Session(engine()) as db,db.begin():
        lock(db,FUYAO_COOLDOWN_ACTION)
        clock=now(db)
        current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',
            Job.lease_token==job.lease_token,Job.lease_until>clock).with_for_update())
        if current is None:
            raise SourceFailure('lease_lost')
        provider=db.get(Provider,UUID(current.payload['source_id']))
        config=db.get(SourceSettings,provider.id) if provider else None
        if provider is None or config is None or provider.family not in FUYAO_FAMILIES:
            raise SourceFailure('source_not_configured')
        if current.payload!=job.payload or current.owner_id!=config.owner_id or (
                expected_configuration is not None and configuration_fingerprint(provider,config)!=expected_configuration):
            raise SourceFailure('configuration_changed')
        denied=permission_reason(provider)
        if denied:
            raise SourceFailure(denied)
        if config.paused_reason=='retry_after_out_of_range':raise SourceFailure('source_paused')
        until=fuyao_cooldown_until(db,clock)
        if until is not None:
            if in_progress:
                error=SourceFailure('local_request_wait',retry_after=max(1,ceil((until-clock).total_seconds())))
                error.request_count=0
                raise error
            current.state,current.available_at='queued',until
            current.attempts=max(0,current.attempts-1)
            current.error_code='source_budget_wait'
            current.lease_token=current.lease_until=None
            config.next_run_at=max(config.next_run_at,until)
            return False
    return True


def reserve_tushare_request(job, *, wait_for_spacing=False, preserve_lease_on_cooldown=False):
    with Session(engine()) as db, db.begin():
        lock(db, TUSHARE_ACTION)
        clock = now(db)
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running',
            Job.lease_token == job.lease_token, Job.lease_until > clock).with_for_update())
        if current is None:
            raise SourceFailure('lease_lost')
        from app.market.tushare_cooldown import assert_request_not_paused
        assert_request_not_paused(db,current)
        from app.market.daily_history import historical_job, validate_history
        history=historical_job(current.payload)
        if current.kind == 'review.collect':
            from app.market.review_jobs import validate_context
            if current.payload != job.payload:
                raise SourceFailure('configuration_changed')
            validate_context(db, current, clock)
            if current.payload.get('postclose_archive') is True:
                from app.market.postclose_schedule import defer_archive
                if defer_archive(current, clock):
                    return False
            if current.payload.get('scheduled_daily_archive') is True:
                from app.market.review_schedule import archive_deadline
                deadline = archive_deadline(current.payload['review_context']['dataset'], clock)
                if deadline is not None:
                    current.state, current.available_at = 'queued', deadline
                    current.attempts = max(0, current.attempts - 1)
                    current.error_code = 'publication_window_wait'
                    current.lease_token = current.lease_until = None
                    return False
        if current.kind == 'tomorrow.collect':
            from app.tomorrow.prepare import validate_preparation
            if current.payload != job.payload:
                raise SourceFailure('configuration_changed')
            validate_preparation(db, current, clock)
        if 'dated_context' in current.payload:
            from app.market.terminal_dates import validate_dated_context
            if current.payload != job.payload:
                raise SourceFailure('configuration_changed')
            provider=db.get(Provider,UUID(current.payload['source_id']))
            config=db.get(SourceSettings,provider.id) if provider else None
            if provider is None or config is None:
                raise SourceFailure('source_not_configured')
            validate_dated_context(db,current.payload,provider,config,clock)
        if 'reference_context' in current.payload:
            from app.market.reference_backfill import validate_context as validate_reference
            if current.payload != job.payload:
                raise SourceFailure('configuration_changed')
            provider=db.get(Provider,UUID(current.payload['source_id']))
            config=db.get(SourceSettings,provider.id) if provider else None
            if provider is None or config is None:
                raise SourceFailure('source_not_configured')
            validate_reference(db,current.payload,provider,config,clock)
        if history:
            if current.payload != job.payload:
                raise SourceFailure('configuration_changed')
            lock(db,f"source:configuration:{current.payload['source_id']}")
            provider=db.get(Provider,UUID(current.payload['source_id']))
            config=db.get(SourceSettings,provider.id) if provider else None
            if provider is None or config is None:
                raise SourceFailure('source_not_configured')
            validate_history(db,current.payload,provider,config,clock)
        if current.payload.get('scheduled_intraday') is True:
            from app.market.intraday_schedule import FAMILIES as LIVE
            from app.market.intraday_schedule import deferred_intraday
            provider = db.get(Provider, UUID(current.payload['source_id']))
            config = db.get(SourceSettings, provider.id) if provider else None
            if provider is None or config is None or provider.family not in LIVE:
                raise SourceFailure('source_not_configured')
            deadline = deferred_intraday(db, clock, config.interval_seconds, config.config.get('symbols', []), batch=provider.family in {'tushare_market_rt','tushare_rt_indices'})
            if deadline is not None:
                current.state, current.available_at = 'queued', deadline
                config.next_run_at = max(config.next_run_at, deadline)
                current.attempts -= 1
                current.error_code = 'intraday_window_wait'
                current.lease_token = current.lease_until = None
                return False
        if current.payload.get('scheduled_publication') is True:
            from app.market.publication_schedule import deferred_publication
            provider=db.get(Provider,UUID(current.payload['source_id']))
            config=db.get(SourceSettings,provider.id) if provider else None
            if provider is None or config is None:
                raise SourceFailure('source_not_configured')
            from app.market.service import permission_reason
            denied=permission_reason(provider)
            if denied:
                raise SourceFailure(denied)
            if config.paused_reason:
                raise SourceFailure(config.paused_reason)
            deadline=deferred_publication(db,provider.family,clock,config.interval_seconds)
            if deadline is not None:
                current.state,current.available_at='queued',deadline
                config.next_run_at=max(config.next_run_at,deadline)
                current.attempts-=1
                current.error_code='publication_window_wait'
                current.lease_token=current.lease_until=None
                return False
        if current.payload.get('scheduled_reference') is True:
            from app.market.reference_schedule import FAMILIES as STATIC_REFERENCES, deferred_reference
            from app.market.service import permission_reason
            provider=db.get(Provider,UUID(current.payload['source_id']))
            config=db.get(SourceSettings,provider.id) if provider else None
            if (current.kind != 'source.collect' or current.payload != job.payload
                    or provider is None or provider.family not in STATIC_REFERENCES
                    or config is None or config.owner_id != current.owner_id):
                raise SourceFailure('configuration_changed')
            denied=permission_reason(provider)
            if denied:
                raise SourceFailure(denied)
            if config.paused_reason:
                raise SourceFailure('source_paused')
            deadline=deferred_reference(provider.family,clock)
            if deadline is not None:
                deadline=max(deadline,tushare_cooldown_until(db,clock) or deadline)
                current.state,current.available_at='queued',deadline
                config.next_run_at=max(config.next_run_at,deadline)
                current.attempts=max(0,current.attempts-1)
                current.error_code='publication_window_wait'
                current.lease_token=current.lease_until=None
                return False
        last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == TUSHARE_ACTION))
        deadline = tushare_cooldown_until(db, clock)
        supplier_wait=deadline
        if last is not None and last + timedelta(seconds=2) > clock:
            deadline = max(deadline or clock, last + timedelta(seconds=2))
        # No verified daily quota is available for this compatible service.
        # Keep usage auditing without an arbitrary next-day stop or a
        # history reservation derived from an unverified daily ceiling.
        if deadline is not None:
            if preserve_lease_on_cooldown and supplier_wait is not None:
                error=SourceFailure('local_request_wait',retry_after=max(1,ceil((deadline-clock).total_seconds())))
                error.request_count=0
                raise error
            # Atomic page sets may retain their lease while waiting for their
            # own short shared spacing, never through supplier Retry-After.
            if (wait_for_spacing and supplier_wait is None and last is not None
                and deadline==last+timedelta(seconds=2)
                and 0<(deadline-clock).total_seconds()<=4):
                raise SourceFailure('source_spacing_wait',retry_after=max(1,ceil((deadline-clock).total_seconds())))
            current.state, current.available_at = 'queued', deadline
            config = db.get(SourceSettings, UUID(current.payload['source_id']))
            if config and current.kind not in {'review.collect', 'tomorrow.collect'} and not history and 'reference_context' not in current.payload and 'dated_context' not in current.payload:
                config.next_run_at = max(config.next_run_at, deadline)
            current.attempts -= 1
            current.error_code = 'source_budget_wait'
            current.lease_token = current.lease_until = None
            return False
        db.add(AuditEvent(actor_id=current.owner_id, action=TUSHARE_ACTION, resource_type='job', resource_id=job.id,
            changed_field_names=['purchased_data_request_permit'], created_at=clock))
    return True


def reserve_tushare_batch_request(job,*,in_progress=False):
    """Bounded short waits survive wall-clock backsteps without restarting page 1."""
    import time as timer
    end=timer.monotonic()+12
    continuing={'preserve_lease_on_cooldown':True} if in_progress else {}
    for _ in range(6):
        try:
            return reserve_tushare_request(job,wait_for_spacing=True,**continuing)
        except SourceFailure as exc:
            if exc.reason!='source_spacing_wait':
                raise
            remaining=end-timer.monotonic()
            if remaining<=0:
                break
            timer.sleep(min(exc.retry_after+.05,remaining))
    # Ordinary queueing preserves the supplier fence if the wall clock keeps
    # moving backwards. A partial page set still writes no market evidence.
    if in_progress:
        try:return reserve_tushare_request(job,wait_for_spacing=True,**continuing)
        except SourceFailure as error:
            if error.reason!='source_spacing_wait':raise
            waiting=SourceFailure('local_request_wait',retry_after=error.retry_after)
            waiting.request_count=0
            raise waiting from None
    return reserve_tushare_request(job)
# Both 短线侠 tables are served from one public host, so they share one window
# instead of each spending its own. The permit is a persisted audit row, not
# process memory: a restart or a second worker cannot re-open the window.
AUCTION_FAMILIES = frozenset({'duanxianxia_auction', 'duanxianxia_auction_history'})
AUCTION_ACTION = 'source.request.auction'
AUCTION_INTERVAL_SECONDS = 65


def ashare_cooldown_until(db, clock):
    """Read immutable deadlines; old logs retain their existing schedule only."""
    deadline = db.scalar(select(func.max(DataFetchRun.retry_not_before)).join(
        Provider, Provider.id == DataFetchRun.source_id).where(Provider.family.in_(ASHARE_FAMILIES),
            DataFetchRun.retry_not_before > clock))
    legacy = db.scalar(select(func.max(SourceSettings.legacy_cooldown_until)).join(
        Provider, Provider.id == SourceSettings.source_id).where(Provider.family.in_(ASHARE_FAMILIES),
            SourceSettings.legacy_cooldown_until > clock))
    return max(value for value in (deadline, legacy) if value is not None) if deadline or legacy else None


def family_cooldown_until(db, family, clock):
    from app.market.qt_budget import FAMILIES as QT_FAMILIES,cooldown_until as qt_cooldown
    from app.market.connected_specs import FUYAO_FAMILIES
    from app.market.supplier_cooldown import THS_FAMILIES,shared_deadline
    if family in QT_FAMILIES:
        return qt_cooldown(db,clock)
    if family in FUYAO_FAMILIES:
        return fuyao_cooldown_until(db,clock)
    if family in THS_FAMILIES:
        return shared_deadline(db,clock,THS_FAMILIES)
    """One supplier's persisted cooldown also applies to its sibling feeds."""
    from app.market.tushare_specs import FAMILIES as TUSHARE_FAMILIES
    from app.market.minute import FAMILY_INTERVALS
    from app.market.eastmoney_trading import FAMILIES as EASTMONEY_FAMILIES
    if family in ASHARE_FAMILIES:
        return ashare_cooldown_until(db, clock)
    if family in TUSHARE_FAMILIES:
        return tushare_cooldown_until(db, clock)
    if family in FAMILY_INTERVALS:
        return sina_cooldown_until(db, clock)
    if family in EASTMONEY_FAMILIES:
        return eastmoney_cooldown_until(db, clock)
    return None


def reserve_mairui_request(job):
    from app.market.service import permission_reason
    from app.models import Provider
    with Session(engine()) as db, db.begin():
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running', Job.lease_token == job.lease_token,
            Job.lease_until > func.now()).with_for_update())
        if current is None:
            raise SourceFailure('lease_expired')
        provider = db.get(Provider, UUID(current.payload['source_id']))
        if provider is None or provider.family not in {'mairui_funds', 'mairui_pools'} or permission_reason(provider):
            raise SourceFailure('permission_missing')
        lock(db, 'source:daily-budget:mairui')
        clock = now(db)
        local = clock.astimezone(SHANGHAI)
        start = datetime.combine(local.date(), time.min, SHANGHAI)
        end = start + timedelta(days=1)
        used = db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == MAIRUI_ACTION,
            AuditEvent.created_at >= start, AuditEvent.created_at < end))
        if used >= MAIRUI_DAILY_LIMIT:
            raise SourceFailure('rate_limited', retry_after=max(1, int((end - local).total_seconds()) + 1))
        db.add(AuditEvent(actor_id=current.owner_id, action=MAIRUI_ACTION, resource_type='job', resource_id=job.id,
            changed_field_names=['licensed_request_permit'], created_at=clock))


def reserve_auction_request(job):
    """Spend the shared auction window, or requeue without counting an attempt.

    The window is keyed on the persisted permit row, so a worker restart cannot
    reset it and two workers cannot both pass. A refused permit is our own
    pacing, not an upstream retry, so the claimed attempt is given back.
    """
    from app.market.service import permission_reason
    from app.models import Provider
    with Session(engine()) as db, db.begin():
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running', Job.lease_token == job.lease_token,
            Job.lease_until > func.now()).with_for_update())
        if current is None:
            return False
        provider = db.get(Provider, UUID(current.payload['source_id']))
        if provider is None:
            raise SourceFailure('source_not_configured')
        if provider.family not in AUCTION_FAMILIES:
            raise SourceFailure('capability_not_implemented')
        denied = permission_reason(provider)
        if denied:
            raise SourceFailure(denied)
        lock(db, 'source:anonymous-budget:auction')
        clock = now(db)
        last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == AUCTION_ACTION))
        from app.market.auction_schedule import CURRENT, LIVE_INTERVAL_SECONDS, SCHEMA_INTERVAL_SECONDS, in_window
        spacing = (SCHEMA_INTERVAL_SECONDS if current.payload.get('schema_probe') else
                   LIVE_INTERVAL_SECONDS if in_window(CURRENT, clock) else AUCTION_INTERVAL_SECONDS
                   ) if provider.family == CURRENT else AUCTION_INTERVAL_SECONDS
        if last is not None and clock < last + timedelta(seconds=spacing):
            current.state, current.available_at = 'queued', last + timedelta(seconds=spacing)
            current.attempts -= 1
            current.error_code = 'source_budget_wait'
            current.lease_token = current.lease_until = None
            return False
        db.add(AuditEvent(actor_id=current.owner_id, action=AUCTION_ACTION, resource_type='provider', resource_id=provider.id,
            changed_field_names=['anonymous_request_permit'], created_at=clock))
    return True


def reserve_ashare_request(job):
    # The audit entry records a permit, not a claimed successful HTTP request.
    # The existing fetch log records actual outcomes. No credential or URL here.
    with Session(engine()) as db, db.begin():
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running', Job.lease_token == job.lease_token,
            Job.lease_until > func.now()).with_for_update())
        if current is None:
            return False
        lock(db, 'source:anonymous-budget:ashare')
        clock = now(db)
        last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == ACTION))
        deadline = ashare_cooldown_until(db, clock)
        if current.payload.get('schema_probe') is True:
            provider = db.get(Provider, UUID(current.payload['source_id']))
            if provider and provider.family == 'ashare_breadth':
                from app.market.breadth_recovery import allowed, next_probe
                if not allowed(db, clock):
                    deadline = max(deadline or clock, next_probe(clock, 900))
        if last is not None and clock < last + timedelta(seconds=INTERVAL_SECONDS):
            deadline = max(deadline or clock, last + timedelta(seconds=INTERVAL_SECONDS))
        if deadline is not None:
            current.state, current.available_at = 'queued', deadline
            config = db.get(SourceSettings, UUID(current.payload['source_id']))
            if config is not None:
                config.next_run_at = max(config.next_run_at, deadline)
            current.attempts -= 1  # Waiting for our budget is not an upstream retry.
            current.error_code = 'source_budget_wait'
            current.lease_token = current.lease_until = None
            return False
        db.add(AuditEvent(actor_id=current.owner_id, action=ACTION, resource_type='job', resource_id=job.id,
            changed_field_names=['anonymous_request_permit'], created_at=clock))
    return True


# A local personal-research safety cap, not a published upstream entitlement.
SINA_ACTION = 'source.request.sina-minute'
SINA_INTERVAL_SECONDS = 15
SINA_DAILY_LIMIT = 500


def sina_cooldown_until(db, clock):
    from app.market.minute import FAMILIES
    return db.scalar(select(func.max(DataFetchRun.retry_not_before)).join(Provider, Provider.id == DataFetchRun.source_id).where(
        Provider.family.in_(FAMILIES), DataFetchRun.retry_not_before > clock))


def reserve_sina_request(job):
    """One persistent permit across all five periods; no SDK hidden requests."""
    from app.market.minute import FAMILY_INTERVALS, minute_url
    from app.market.minute_schedule import deferred_minute
    from app.market.protocol import InstrumentKey
    from app.market.service import permission_reason
    with Session(engine()) as db, db.begin():
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running', Job.lease_token == job.lease_token,
            Job.lease_until > func.now()).with_for_update())
        if current is None:
            return False
        if current.payload != job.payload:
            raise SourceFailure('configuration_changed')
        provider = db.get(Provider, UUID(current.payload['source_id']))
        config = db.get(SourceSettings, provider.id) if provider else None
        if provider is None or config is None or provider.family not in FAMILY_INTERVALS:
            raise SourceFailure('source_not_configured')
        denied = permission_reason(provider)
        if denied:
            raise SourceFailure(denied)
        if config.paused_reason:
            raise SourceFailure('source_paused')
        selection = config.config.get('symbols', [])
        if len(selection) != 1:
            raise SourceFailure('invalid_instrument_batch')
        minute_url(InstrumentKey(**selection[0]), FAMILY_INTERVALS[provider.family])
        lock(db, SINA_ACTION)
        clock = now(db)
        from app.market.minute_backfill import validate_context
        validate_context(db,current.payload,provider,config,clock)
        deadline = sina_cooldown_until(db, clock)
        wait_reason = 'source_budget_wait'
        if current.payload.get('scheduled_minute') is True:
            window = deferred_minute(db, provider.family, clock, config.interval_seconds, selection)
            if window is not None:
                deadline = max(deadline or clock, window)
                wait_reason = 'intraday_window_wait'
        last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == SINA_ACTION))
        start = datetime.combine(clock.astimezone(SHANGHAI).date(), time.min, SHANGHAI)
        end = start + timedelta(days=1)
        used = db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == SINA_ACTION,
            AuditEvent.created_at >= start, AuditEvent.created_at < end))
        if last is not None and last + timedelta(seconds=SINA_INTERVAL_SECONDS) > clock:
            deadline = max(deadline or clock, last + timedelta(seconds=SINA_INTERVAL_SECONDS))
        if used >= SINA_DAILY_LIMIT:
            deadline = max(deadline or clock, end)
        protected = protect_live_budget(db,current,clock,SINA_DAILY_LIMIT,used,FAMILY_INTERVALS)
        if protected:
            deadline = max(deadline or clock, protected)
        if deadline is not None:
            current.state, current.available_at = 'queued', deadline
            current.attempts -= 1
            current.error_code = wait_reason
            current.lease_token = current.lease_until = None
            if 'minute_context' not in current.payload:
                config.next_run_at = max(config.next_run_at, deadline)
            return False
        db.add(AuditEvent(actor_id=current.owner_id, action=SINA_ACTION, resource_type='job', resource_id=current.id,
            changed_field_names=['personal_minute_request_permit'], created_at=clock))
    return True
