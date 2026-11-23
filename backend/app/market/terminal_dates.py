"""Explicit dated Tushare tables; a backfill receipt never becomes an old observation."""
from datetime import date

from app.market.minute_backfill import configuration_fingerprint
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.tushare_specs import DATED_FAMILIES

LOOKBACK_DAYS = 365


def make_dated_context(db, provider, config, day, clock):
    from app.market.service import permission_reason
    if provider.family not in DATED_FAMILIES:
        raise SourceFailure('backfill_unsupported')
    if permission_reason(provider):
        raise SourceFailure('permission_missing')
    today = clock.astimezone(SHANGHAI).date()
    if day > today or (today-day).days > LOOKBACK_DAYS:
        raise SourceFailure('backfill_day_out_of_range')
    # The explicit date is passed through to the supplier, including closed
    # days. A successful empty publication is a gap, never an invented session.
    return {'requested_day':day.isoformat(), 'acquisition_mode':'backfill',
            'configuration_sha256':configuration_fingerprint(provider,config)}


def validate_dated_context(db, payload, provider, config, clock):
    context = payload.get('dated_context')
    if 'dated_context' not in payload:return None
    if any(key in payload for key in ('reference_context','minute_context','daily_history')):
        raise SourceFailure('invalid_backfill_context')
    try:
        day = date.fromisoformat(context['requested_day'])
    except (TypeError, KeyError, ValueError):
        raise SourceFailure('invalid_backfill_context') from None
    if context != make_dated_context(db,provider,config,day,clock):
        raise SourceFailure('configuration_changed')
    return day
