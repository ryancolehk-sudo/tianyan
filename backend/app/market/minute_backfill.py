"""Bounded, explicit missing-only collection; never a historical availability claim."""
from datetime import date

from sqlalchemy import case, func, or_, select

from app.evidence.common import content_hash, normalized
from app.evidence.models import EvidenceVersion
from app.market.minute import FAMILY_INTERVALS
from app.market.protocol import InstrumentKey, SHANGHAI, SourceFailure
from app.models import Job, Provider

LOOKBACK_DAYS = 7  # Local safety bound, not a supplier coverage guarantee.


def configuration_fingerprint(provider, config):
    return content_hash(normalized(dict(source_id=provider.id, family=provider.family,
        adapter_version=provider.adapter_version, capabilities=provider.capabilities,
        configured=provider.configured, enabled=provider.enabled, rights=provider.rights,
        entitlement=provider.entitlement_status, owner_id=config.owner_id,
        rights_basis=config.rights_basis, interval_seconds=config.interval_seconds,
        configured_at=config.configured_at, config=config.config, paused_reason=config.paused_reason)))


def is_backfill(payload):
    return isinstance(payload.get('minute_context'), dict) and payload['minute_context'].get('acquisition_mode') == 'backfill'


def target_day(payload):
    try:
        return date.fromisoformat(payload['minute_context']['requested_day']) if is_backfill(payload) else None
    except (KeyError, TypeError, ValueError):
        raise SourceFailure('invalid_backfill_context') from None


def calendar_open(db, day, exchange, clock):
    if clock.utcoffset() is None:
        raise SourceFailure('calendar_not_ready')
    families = {'SSE': ('tushare_calendar_sse', 'baostock_daily'),
                'SZSE': ('tushare_calendar_szse', 'baostock_daily', 'szse_public')}
    candidates = families.get(exchange)
    if not candidates:
        raise SourceFailure('calendar_not_ready')
    # Rank before inspecting the payload: a newer correction cannot resurrect an
    # older open day. Calendar receipt uses today's knowledge, not target-day time.
    ranks = select(EvidenceVersion.id, func.row_number().over(partition_by=(EvidenceVersion.source_id,
        EvidenceVersion.source_key), order_by=EvidenceVersion.version_number.desc()).label('position')).where(
        EvidenceVersion.kind == 'calendar', EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.visibility == 'shared', EvidenceVersion.received_at <= clock).subquery()
    rows = db.execute(select(EvidenceVersion, Provider).join(ranks, ranks.c.id == EvidenceVersion.id)
        .join(Provider, Provider.id == EvidenceVersion.source_id).where(ranks.c.position == 1,
            Provider.family.in_(candidates),
            EvidenceVersion.provenance == 'source_reported', EvidenceVersion.payload['trade_day'].astext == day.isoformat(),
            EvidenceVersion.payload['exchange'].astext == exchange,
            Provider.configured.is_(True), Provider.enabled.is_(True), Provider.entitlement_status == 'verified',
            Provider.rights['cache'].astext == 'allowed', Provider.rights['display'].astext == 'allowed')
        # A newer free receipt cannot override a currently permitted paid
        # calendar. Missing paid target-day evidence or revoked current rights
        # permit fallback; a paid closed/invalid result never does.
        .order_by(case((Provider.family == candidates[0], 0), else_=1),
            EvidenceVersion.received_at.desc(), EvidenceVersion.version_number.desc(), EvidenceVersion.id.desc()))
    for row, provider in rows:
        if provider.family in candidates:
            opened = row.payload.get('is_open')
            if type(opened) is not bool:
                raise SourceFailure('calendar_not_ready')
            if not opened:
                raise SourceFailure('backfill_day_closed')
            return
    raise SourceFailure('calendar_not_ready')


def make_context(db, provider, config, day, clock):
    from app.market.service import permission_reason
    if provider.family not in FAMILY_INTERVALS:
        raise SourceFailure('backfill_unsupported')
    if permission_reason(provider):
        raise SourceFailure('permission_missing')
    today = clock.astimezone(SHANGHAI).date()
    if day > today:
        raise SourceFailure('future_day')
    if (today - day).days > LOOKBACK_DAYS:
        raise SourceFailure('backfill_day_out_of_range')
    selection = config.config.get('symbols', [])
    if len(selection) != 1:
        raise SourceFailure('invalid_instrument_batch')
    key = InstrumentKey(**selection[0])
    calendar_open(db, day, key.exchange, clock)
    return dict(requested_day=day.isoformat(), acquisition_mode='backfill', missing_only=True,
        instrument=normalized(selection[0]), interval=FAMILY_INTERVALS[provider.family], adjustment='unadjusted',
        configuration_sha256=configuration_fingerprint(provider, config))


def validate_context(db, payload, provider, config, clock):
    if not is_backfill(payload):
        if 'minute_context' in payload:
            raise SourceFailure('invalid_backfill_context')
        return None
    day = target_day(payload)
    expected = make_context(db, provider, config, day, clock)
    if payload['minute_context'] != expected:
        raise SourceFailure('configuration_changed')
    return day


def live_run_condition():
    from app.market.review_protocol import PUBLIC_FAMILIES
    from app.market.review_store import family_id
    historical_jobs = select(Job.id).where(or_(
        Job.kind.in_(['funds.focus', 'funds.archive', 'direction.archive', 'breadth.archive']),
        or_(*(Job.payload.has_key(key) for key in ('minute_context', 'reference_context',
            'daily_history', 'dated_context', 'full_minute_context', 'backfill_context',
            'funds_history_context'))),
        (Job.kind == 'source.collect') & Job.payload.has_key('review_context'),
        (Job.kind == 'review.collect') & ~Job.payload['source_id'].astext.in_([str(family_id(family)) for family in PUBLIC_FAMILIES])))
    from app.market.models import DataFetchRun
    return (DataFetchRun.job_id.is_(None) | ~DataFetchRun.job_id.in_(historical_jobs))


def fetch_run_dto(db, row):
    from app.market.schemas import FetchRun
    result = FetchRun.model_validate(row)
    job = db.get(Job, row.job_id) if row.job_id else None
    if job and job.kind == 'review.collect' and isinstance(job.payload.get('review_context'), dict):
        return result.model_copy(update={'requested_day': date.fromisoformat(job.payload['review_context']['day']), 'acquisition_mode': 'backfill'})
    if job and is_backfill(job.payload):
        return result.model_copy(update={'requested_day': target_day(job.payload), 'acquisition_mode': 'backfill'})
    if job and isinstance(job.payload.get('dated_context'),dict):
        return result.model_copy(update={'requested_day':date.fromisoformat(job.payload['dated_context']['requested_day']), 'acquisition_mode':'backfill'})
    if job:
        from app.market.reference_backfill import is_backfill as reference_job, target_day as reference_day
        if reference_job(job.payload):
            return result.model_copy(update={'requested_day':reference_day(job.payload),'acquisition_mode':'backfill'})
    if job and isinstance(job.payload.get('daily_history'),dict):
        context=job.payload['daily_history']
        from app.market.tushare import code
        instrument=InstrumentKey(**context['instrument'])
        return FetchRun.model_validate({**result.model_dump(), 'acquisition_mode':'daily_history',
            'requested_instrument_code':code(instrument),
            'requested_start_day':context['start_day'],'requested_end_day':context['end_day']})
    return result.model_copy(update={'acquisition_mode': 'observed'})
