"""Owner-scoped short-lived quote interests, never portfolio or market evidence."""
from collections import deque
from datetime import date, datetime, timedelta
import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.auth import APIError
from app.evidence.common import now
from app.market.models import SourceSettings, Watchlist
from app.market.protocol import InstrumentKey, MAX_TENCENT_QUOTE_BATCH, SHANGHAI
from app.models import Instrument, Provider
from app.read_cache_models import ReadCache
from app.schemas import DTO

OPERATION = 'quote-focus'
LIFETIME = timedelta(seconds=90)
MAX_CONTEXT_CODES = 50
MAX_BATCH = MAX_TENCENT_QUOTE_BATCH
CODE = re.compile(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ|[489]\d{5}\.BJ)')
EXCHANGE = {'SH':'SSE','SZ':'SZSE','BJ':'BSE'}


class QuoteFocusRequest(DTO):
    context_id: UUID
    panel: Literal['selected','sector','ranking']
    trade_day: date
    codes: list[str] = Field(default_factory=list, max_length=MAX_CONTEXT_CODES)
    as_of_time: AwareDatetime | None = None


class QuoteFocusReceipt(DTO):
    active: bool
    accepted_count: int
    expires_at: AwareDatetime


def record_interest(db, owner, body):
    from app.market.service import permission_reason, source_id
    clock = now(db)
    expires = clock + LIFETIME
    provider = db.get(Provider, source_id('tencent_web'))
    config = db.get(SourceSettings, source_id('tencent_web'))
    if (provider is None or config is None or permission_reason(provider)
            or config.config.get('collection_mode') != 'focused' or config.owner_id != owner.id):
        return QuoteFocusReceipt(active=False, accepted_count=0, expires_at=clock)
    codes = list(dict.fromkeys(body.codes))
    if any(not CODE.fullmatch(code) for code in codes):
        raise APIError(422, 'quote_focus_identity', '关注报价只接受带市场后缀的股票代码。')
    active = body.as_of_time is None and body.trade_day == clock.astimezone(SHANGHAI).date()
    if not active:
        codes = []
    if codes:
        from sqlalchemy import tuple_
        identities = {(EXCHANGE[code[-2:]], code[:6]) for code in codes}
        known = set(db.execute(select(Instrument.exchange, Instrument.symbol).where(
            Instrument.asset_type == 'stock', Instrument.valid_to.is_(None),
            tuple_(Instrument.exchange, Instrument.symbol).in_(identities))))
        codes = [code for code in codes if (EXCHANGE[code[-2:]], code[:6]) in known]
    key = hashlib.sha256(f'{OPERATION}:{owner.id}:{body.context_id}'.encode()).hexdigest()
    existing = db.get(ReadCache, key)
    if existing is None:
        contexts = list(db.scalars(select(ReadCache.key).where(ReadCache.owner_id == owner.id,
            ReadCache.operation == OPERATION, ReadCache.focused_at >= clock-LIFETIME).limit(17)))
        if len(contexts) >= 16:
            raise APIError(429, 'quote_focus_context_limit', '关注窗口较多，请稍后再试。')
    payload = {'codes': codes, 'expires_at': expires.isoformat()}
    values = dict(owner_id=owner.id, scope='quote-focus-v1', operation=OPERATION,
        params={'panel': body.panel, 'trade_day': body.trade_day.isoformat()},
        dependency_keys=[], revision=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        payload=payload, built_at=clock, validated_at=clock, accessed_at=clock,
        focused_at=clock, refresh_after=expires, dirty=False, last_error=None,
        lease_token=None, lease_until=None)
    db.execute(insert(ReadCache).values(key=key, **values).on_conflict_do_update(
        index_elements=['key'], set_=values))
    return QuoteFocusReceipt(active=active, accepted_count=len(codes), expires_at=expires)


def focused_selection(db, config, clock):
    """Current pick first; bounded round-robin avoids starving larger watchlists."""
    day = clock.astimezone(SHANGHAI).date().isoformat()
    groups = {'selected': [], 'sector': [], 'ranking': []}
    rows = db.scalars(select(ReadCache).where(ReadCache.owner_id == config.owner_id,
        ReadCache.operation == OPERATION, ReadCache.focused_at >= clock-LIFETIME,
        ReadCache.focused_at <= clock, ReadCache.params['trade_day'].astext == day)
        .order_by(ReadCache.focused_at.desc()).limit(16))
    for entry in rows:
        try:
            if datetime.fromisoformat(entry.payload['expires_at']) <= clock:
                continue
            panel = entry.params['panel']
            if panel not in groups or not isinstance(entry.payload['codes'], list):
                continue
            groups[panel].extend(InstrumentKey(EXCHANGE[code[-2:]], code[:6], 'stock')
                for code in entry.payload['codes'][:MAX_CONTEXT_CODES] if isinstance(code, str) and CODE.fullmatch(code))
        except (KeyError, TypeError, ValueError):
            continue
    watch = db.get(Watchlist, config.owner_id)
    watched = list(db.scalars(select(Instrument).where(Instrument.id.in_(watch.instrument_ids),
        Instrument.asset_type == 'stock', Instrument.valid_to.is_(None)).order_by(Instrument.id))) if watch and watch.instrument_ids else []
    watch_group = [InstrumentKey(row.exchange, row.symbol, 'stock') for row in watched]
    manual = [InstrumentKey(**item) for item in config.config.get('symbols', [])]
    output, seen = [], set()
    def append(item):
        if item.identity not in seen and len(output) < MAX_BATCH:
            output.append(item); seen.add(item.identity)
    for item in groups['selected']:
        append(item)
    slot = int(clock.timestamp()) // max(3, config.interval_seconds)
    unique_groups = [list(dict.fromkeys(group)) for group in (groups['sector'], watch_group, groups['ranking'], manual) if group]
    # Advance by each group's share; a large watchlist need not move only ten
    # places on every sample when the request has room for many more stocks.
    stride = max(1, (MAX_BATCH-len(output)) // max(1, len(unique_groups)))
    rotating = []
    for unique in unique_groups:
        offset = slot * stride % len(unique)
        rotating.append(deque(unique[offset:] + unique[:offset]))
    while rotating and len(output) < MAX_BATCH:
        for group in rotating:
            if group:
                append(group.popleft())
        rotating = [group for group in rotating if group]
    return output
