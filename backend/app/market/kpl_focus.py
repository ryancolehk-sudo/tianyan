"""Short-lived, owner-scoped KPL priorities; never market observations or jobs."""
from datetime import date, datetime, timedelta
import hashlib
import json
import re
from uuid import UUID

from pydantic import AwareDatetime, Field
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert

from app.auth import APIError
from app.evidence.common import lock, now
from app.evidence.models import EvidenceVersion
from app.market.kpl_atlas_source import DATASET
from app.market.models import SourceSettings
from app.market.protocol import SHANGHAI
from app.market.review_store import permitted_provider
from app.read_cache_models import ReadCache
from app.schemas import DTO

OPERATION = 'kpl-focus'
LIFETIME = timedelta(seconds=90)
MAX_CODES = 10
MAX_CONTEXTS = 16
CODE = re.compile(r'\d{6}')


class KplFocusRequest(DTO):
    context_id: UUID
    sequence: int = Field(ge=1, le=2_147_483_647)
    trade_day: date
    codes: list[str] = Field(default_factory=list, max_length=MAX_CODES)
    as_of_time: AwareDatetime | None = None


class KplFocusReceipt(DTO):
    active: bool
    accepted_count: int
    expires_at: AwareDatetime


def active_contexts(owner, clock):
    codes = ReadCache.payload['codes']
    count = case((func.jsonb_typeof(codes) == 'array', func.jsonb_array_length(codes)), else_=0)
    return select(ReadCache).where(ReadCache.owner_id == owner, ReadCache.operation == OPERATION,
        ReadCache.focused_at >= clock-LIFETIME, ReadCache.focused_at <= clock, count > 0,
        ReadCache.params['trade_day'].astext == clock.astimezone(SHANGHAI).date().isoformat())


def known_codes(db, provider, day, clock):
    """Read one saved native board list, never its full-day history."""
    from app.market.kpl_strength_api import project
    saved = db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id == provider.id,
        EvidenceVersion.kind == 'reference', EvidenceVersion.visibility == 'shared',
        EvidenceVersion.owner_id.is_(None), EvidenceVersion.provenance == 'source_reported',
        EvidenceVersion.received_at <= clock, EvidenceVersion.payload['dataset'].astext == DATASET,
        EvidenceVersion.payload['observed_day'].astext == day.isoformat(),
        EvidenceVersion.payload['rows'][0]['archive_kind'].astext == 'intraday')
        .order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(1))
    return {row['code'] for row in project([saved] if saved else [], day, clock)['rows']}


def record_interest(db, owner, body):
    clock = now(db)
    key = hashlib.sha256(f'{OPERATION}:{owner.id}:{body.context_id}'.encode()).hexdigest()
    lock(db, f'{OPERATION}:{owner.id}')
    existing = db.get(ReadCache, key)
    previous = existing.payload if existing and isinstance(existing.payload, dict) else {}
    provider = permitted_provider(db, 'direction_kpl_strength')
    config = db.get(SourceSettings, provider.id) if provider else None
    permitted = bool(config and config.owner_id == owner.id and owner.role == 'admin' and owner.enabled
        and config.paused_reason in {None, 'schema_changed'})
    eligible = permitted and body.as_of_time is None and body.trade_day == clock.astimezone(SHANGHAI).date()
    if any(not CODE.fullmatch(code) for code in body.codes):
        raise APIError(422, 'kpl_focus_identity', '关注题材只接受开盘啦六位题材代码。')
    previous_sequence = previous.get('sequence', 0)
    if isinstance(previous_sequence, int) and body.sequence <= previous_sequence:
        try:
            expires = datetime.fromisoformat(previous['expires_at'])
            active = bool(eligible and expires > clock and existing.params.get('trade_day') == body.trade_day.isoformat())
        except (KeyError, ValueError, TypeError):
            active, expires = False, clock
        return KplFocusReceipt(active=active, accepted_count=len(previous.get('codes', [])) if active else 0,
            expires_at=expires)
    codes = list(dict.fromkeys(body.codes)) if eligible else []
    if codes:
        known = known_codes(db, provider, body.trade_day, clock)
        codes = [code for code in codes if code in known]
    if codes:
        active = set(db.scalars(active_contexts(owner.id, clock).with_only_columns(ReadCache.key).limit(MAX_CONTEXTS+1)))
        if key not in active and len(active) >= MAX_CONTEXTS:
            raise APIError(429, 'kpl_focus_context_limit', '关注题材窗口较多，请稍后再试。')
    expires = clock + LIFETIME if eligible else clock
    # Keep empty tombstones for an existing context so an older in-flight
    # request cannot restore a deselected, hidden or historical interest.
    if permitted or existing is not None:
        payload = dict(codes=codes, expires_at=expires.isoformat(), sequence=body.sequence)
        values = dict(owner_id=owner.id, scope='kpl-focus-v1', operation=OPERATION,
            params={'trade_day': body.trade_day.isoformat()}, dependency_keys=[],
            revision=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(), payload=payload,
            built_at=clock, validated_at=clock, accessed_at=clock, focused_at=clock, refresh_after=expires,
            dirty=False, last_error=None, lease_token=None, lease_until=None)
        db.execute(insert(ReadCache).values(key=key, **values).on_conflict_do_update(index_elements=['key'], set_=values))
    return KplFocusReceipt(active=bool(eligible), accepted_count=len(codes), expires_at=expires)


def focused_codes(db, owner, clock):
    """Latest visible contexts first, at most ten aggregate priority codes."""
    output = []
    for entry in db.scalars(active_contexts(owner, clock).order_by(ReadCache.focused_at.desc(), ReadCache.key).limit(MAX_CONTEXTS)):
        try:
            if datetime.fromisoformat(entry.payload['expires_at']) <= clock:
                continue
            for code in entry.payload['codes'][:MAX_CODES]:
                if isinstance(code, str) and CODE.fullmatch(code) and code not in output:
                    output.append(code)
                    if len(output) == MAX_CODES:
                        return output
        except (KeyError, TypeError, ValueError):
            continue
    return output
