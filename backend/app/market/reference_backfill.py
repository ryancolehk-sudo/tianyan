"""Explicit single-day reference backfill; never alters minute job contexts."""
from datetime import date

from app.market.minute_backfill import calendar_open, configuration_fingerprint
from app.market.protocol import SHANGHAI, SourceFailure

FAMILIES = frozenset({'tushare_dc_index','tushare_dc_member','tushare_moneyflow_ind_dc',
    'tushare_moneyflow_dc','tushare_stk_premarket','tushare_limit_step',
    'tushare_stk_auction','tushare_limit_list_d','tushare_limit_list_ths','tushare_top_list','tushare_top_inst','tushare_ths_daily'})
LOOKBACK_DAYS = 45


def is_backfill(payload):
    return isinstance(payload.get('reference_context'), dict) and payload['reference_context'].get('acquisition_mode') == 'backfill'


def target_day(payload):
    try:
        return date.fromisoformat(payload['reference_context']['requested_day']) if is_backfill(payload) else None
    except (KeyError, TypeError, ValueError):
        raise SourceFailure('invalid_backfill_context') from None


def make_context(db, provider, config, day, clock):
    from app.market.service import permission_reason
    if provider.family not in FAMILIES:
        raise SourceFailure('backfill_unsupported')
    if permission_reason(provider) or config.paused_reason:
        raise SourceFailure('permission_missing')
    today = clock.astimezone(SHANGHAI).date()
    if day > today:
        raise SourceFailure('future_day')
    if (today-day).days > (120 if provider.family == 'tushare_ths_daily' else LOOKBACK_DAYS):
        raise SourceFailure('backfill_day_out_of_range')
    # Require actual, currently permitted calendar evidence on both exchanges.
    calendar_open(db, day, 'SSE', clock)
    calendar_open(db, day, 'SZSE', clock)
    return dict(requested_day=day.isoformat(), acquisition_mode='backfill',
        dataset_family=provider.family, configuration_sha256=configuration_fingerprint(provider, config))


def validate_context(db, payload, provider, config, clock):
    if not is_backfill(payload):
        if 'reference_context' in payload:
            raise SourceFailure('invalid_backfill_context')
        return None
    if any(key in payload for key in ('minute_context','daily_history','dated_context')):
        raise SourceFailure('invalid_backfill_context')
    day = target_day(payload)
    if payload['reference_context'] != make_context(db, provider, config, day, clock):
        raise SourceFailure('configuration_changed')
    return day
