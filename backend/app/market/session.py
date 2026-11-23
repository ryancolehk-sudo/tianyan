"""Shared Shanghai market context, independent of any feed's freshness.

Only observed, currently permitted exchange calendars establish an open day.
Reads never collect and an unknown calendar never becomes a weekday guess.
"""
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import AwareDatetime
from sqlalchemy import case,select

from app.evidence.common import now
from app.evidence.models import EvidenceVersion
from app.market.protocol import SHANGHAI, SourceFailure
from app.models import Provider
from app.schemas import DTO


def observation_gap_seconds(start, end):
    """Measure missing trading time; the normal lunch break is compressed.

    This changes display continuity only. It does not create samples or make
    a five-minute return valid across two distinct trading segments.
    """
    first, last = start.astimezone(SHANGHAI), end.astimezone(SHANGHAI)
    elapsed = (last-first).total_seconds()
    if first.date()!=last.date() or elapsed<0:
        return float('inf')
    lunch_start=datetime.combine(first.date(),time(11,30),SHANGHAI)
    lunch_end=datetime.combine(first.date(),time(13),SHANGHAI)
    lunch=max(0,(min(last,lunch_end)-max(first,lunch_start)).total_seconds())
    return elapsed-lunch


class MarketSession(DTO):
    server_time: AwareDatetime
    trade_day: date | None = None
    previous_trade_day: date | None = None
    phase: Literal['preopen', 'auction', 'trading', 'break', 'closed', 'unknown']
    is_trading_day: bool | None = None
    automatic_live: bool = False
    current_day_active: bool = False
    next_transition_at: AwareDatetime | None = None
    poll_seconds: int = 15


def phase_at(clock, opened):
    local = clock.astimezone(SHANGHAI)
    if opened is None:
        return 'unknown', None
    if not opened:
        return 'closed', datetime.combine(local.date() + timedelta(days=1), time.min, SHANGHAI)
    boundaries = ((time(9), 'preopen'), (time(9, 15), 'preopen'), (time(9, 30), 'auction'),
                  (time(11, 30), 'trading'), (time(13), 'break'),
                  (time(15), 'trading'))
    for boundary, phase in boundaries:
        if local.time().replace(tzinfo=None) < boundary:
            return phase, datetime.combine(local.date(), boundary, SHANGHAI)
    return 'closed', datetime.combine(local.date() + timedelta(days=1), time.min, SHANGHAI)


def market_session(db, clock=None):
    clock = clock or now(db)
    local = clock.astimezone(SHANGHAI)
    today = local.date()
    # Latest observed value for each exchange. A conflicting or absent exchange
    # is unknown, even if today's other exchange or yesterday was open.
    families = {'tushare_calendar_sse', 'tushare_calendar_szse', 'szse_public', 'baostock_daily'}
    from app.market.service import permission_reason
    providers = {p.id: p for p in db.scalars(select(Provider).where(Provider.family.in_(families)))
                 if not permission_reason(p)}
    paid=[ident for ident,p in providers.items() if p.family in {'tushare_calendar_sse','tushare_calendar_szse'}]
    baostock=[ident for ident,p in providers.items() if p.family=='baostock_daily']
    calendar_query = select(EvidenceVersion.source_id,EvidenceVersion.payload).where(
        EvidenceVersion.source_id.in_(providers), EvidenceVersion.kind == 'calendar',
        EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared',
        EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= clock,
    ).order_by(case((EvidenceVersion.source_id.in_(paid),0),(EvidenceVersion.source_id.in_(baostock),1),else_=2),
        EvidenceVersion.version_number.desc(),EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc())
    exchanges = {}
    allowed={'SSE':{'tushare_calendar_sse','baostock_daily'},'SZSE':{'tushare_calendar_szse','baostock_daily','szse_public'}}
    for source_id,payload in db.execute(calendar_query.where(EvidenceVersion.payload['trade_day'].astext == today.isoformat())):
        exchange = payload.get('exchange')
        if exchange in allowed and providers[source_id].family in allowed[exchange]:
            exchanges.setdefault(exchange, payload.get('is_open'))
        if len(exchanges) == 2:
            break
    values = [exchanges.get(exchange) for exchange in ('SSE', 'SZSE')]
    opened = values[0] if all(type(value) is bool for value in values) and values[0] == values[1] else None
    phase, transition = phase_at(clock, opened)
    automatic = opened is True and phase in {'auction', 'trading', 'break'}
    current_day_active=opened is True and local.time().replace(tzinfo=None)>=time(9)
    trading_day = today if current_day_active else None
    if trading_day is None and opened is not None:
        from app.market.mairui import pool_day
        try:
            trading_day = pool_day(db, clock)
        except SourceFailure:
            pass
    previous_day = None
    if trading_day is not None:
        # Use exactly the same current rights, receipt cutoff and paid calendar
        # precedence as today's session. A historical-rights query would make
        # yesterday disappear when an existing calendar is newly configured.
        previous = {}
        for source_id,payload in db.execute(calendar_query.where(
            EvidenceVersion.payload['trade_day'].astext < trading_day.isoformat(),
            EvidenceVersion.payload['trade_day'].astext >= (trading_day-timedelta(days=365)).isoformat())):
            exchange=payload.get('exchange')
            if exchange in allowed and providers[source_id].family in allowed[exchange]:
                previous.setdefault(payload.get('trade_day'),{}).setdefault(exchange,payload.get('is_open'))
        days=[]
        for day,values in previous.items():
            if values.get('SSE') is True and values.get('SZSE') is True:
                try: days.append(date.fromisoformat(day))
                except (TypeError,ValueError): pass
        previous_day=max(days) if days else None
    return MarketSession(server_time=clock, trade_day=trading_day, previous_trade_day=previous_day, phase=phase,
        is_trading_day=opened, automatic_live=automatic, current_day_active=current_day_active,next_transition_at=transition)


def quote_fresh_at(clock):
    """Freeze same-day feed age while continuous trading is stopped."""
    local = clock.astimezone(SHANGHAI)
    if time(11, 30) <= local.time().replace(tzinfo=None) < time(13):
        return datetime.combine(local.date(), time(11, 30), SHANGHAI)
    if local.time().replace(tzinfo=None) >= time(15):
        return datetime.combine(local.date(), time(15), SHANGHAI)
    return clock
