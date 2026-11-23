"""Ordered account preferences. A read never requests a vendor or grants rights."""
from uuid import UUID

from sqlalchemy import select

from app.auth import APIError
from app.evidence.common import lock, now
from app.market.models import Watchlist
from app.market.schemas import MarketIdentity, WatchlistItem, WatchlistView
from app.market.visibility import instrument_name_sql, instrument_visible_sql
from app.models import AuditEvent, Instrument


def watchlist(db, owner_id):
    saved = db.get(Watchlist, owner_id)
    ids = [UUID(value) for value in saved.instrument_ids] if saved else []
    if not ids:
        return WatchlistView(items=[])
    rows = {item.id: (item, name) for item, name in db.execute(select(Instrument, instrument_name_sql()).where(
        Instrument.id.in_(ids)))}
    # Revoking a source hides its name; the user's saved code/position persists.
    return WatchlistView(items=[WatchlistItem(position=position, instrument=MarketIdentity(
        id=ident, exchange=rows[ident][0].exchange, symbol=rows[ident][0].symbol,
        asset_type=rows[ident][0].asset_type, name=rows[ident][1] or '标的资料不可用'))
        for position, ident in enumerate(ids) if ident in rows])


def update_watchlist(db, owner, body, request_id):
    lock(db, f'watchlist:{owner.id}')
    ids = body.instrument_ids
    saved = db.get(Watchlist, owner.id)
    previous = set(saved.instrument_ids) if saved else set()
    additions = [ident for ident in ids if str(ident) not in previous]
    if additions:
        known = set(db.scalars(select(Instrument.id).where(Instrument.id.in_(additions),
            Instrument.valid_to.is_(None), instrument_visible_sql())))
        if not set(additions).issubset(known):
            raise APIError(422, 'instrument_unavailable', '自选包含不存在、已失效或无展示权限的标的，请重新选择。')
    values = [str(value) for value in ids]
    if saved is None:
        saved = Watchlist(owner_id=owner.id, instrument_ids=values, updated_at=now(db))
        db.add(saved)
    else:
        saved.instrument_ids, saved.updated_at = values, now(db)
    db.add(AuditEvent(actor_id=owner.id, action='watchlist.replace', request_id=request_id,
        resource_type='watchlist', resource_id=owner.id, changed_field_names=['instrument_ids']))
    db.flush()
    return watchlist(db, owner.id)
