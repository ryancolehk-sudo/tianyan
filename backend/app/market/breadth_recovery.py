"""Strict, paced format rechecks: only on verified Shanghai trading sessions."""
from datetime import datetime, time, timedelta
from sqlalchemy import select
from app.evidence.models import EvidenceVersion
from app.market.protocol import SHANGHAI
from app.market.realtime_schedule import COLLECTION_END
from app.models import Provider

WINDOWS = ((time(9, 15), COLLECTION_END),)


def next_probe(clock, interval):
    earliest = (clock + timedelta(seconds=max(interval, 900))).astimezone(SHANGHAI)
    for offset in range(2):
        day = earliest.date() + timedelta(days=offset)
        for start, end in WINDOWS:
            candidate = max(earliest, datetime.combine(day, start, SHANGHAI))
            if candidate < datetime.combine(day, end, SHANGHAI):
                return candidate
    raise AssertionError('finite daily window lookup')


def allowed(db, clock):
    local = clock.astimezone(SHANGHAI)
    if not any(start <= local.time() < end for start, end in WINDOWS):
        return False
    rows = db.scalars(select(EvidenceVersion).join(Provider, Provider.id == EvidenceVersion.source_id).where(
        EvidenceVersion.kind == 'calendar', EvidenceVersion.visibility == 'shared', EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= clock, EvidenceVersion.available_at <= clock,
        EvidenceVersion.payload['trade_day'].astext == str(local.date()),
        Provider.family.in_(['baostock_daily', 'szse_public', 'tushare_calendar_sse', 'tushare_calendar_szse']),
        Provider.configured.is_(True), Provider.enabled.is_(True), Provider.entitlement_status == 'verified',
        Provider.rights['display'].astext == 'allowed', Provider.rights['cache'].astext == 'allowed')
        .order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.version_number.desc(), EvidenceVersion.id.desc()))
    days = {}
    for row in rows:
        exchange = row.payload.get('exchange')
        if exchange in {'SSE', 'SZSE'}:
            days.setdefault(exchange, row.payload.get('is_open'))
    return all(days.get(exchange) is True for exchange in ('SSE', 'SZSE'))
