"""Read-time display scope for shared instrument identities.

An instrument identity can be claimed by several sources. A saved row is shown
when it has no source claim (manual or legacy local record) or when at least one
claiming source still permits display and cache. `enabled` deliberately does not
participate: closing collection stops new fetches, while revoking rights hides
already saved values.
"""
from sqlalchemy import case, func, or_, select

from app.market.models import InstrumentOrigin
from app.models import Instrument, Provider


def allowed_origin_sql():
    from app.provider_access import cloud_share_sql
    return (
        Provider.configured.is_(True),
        Provider.entitlement_status == 'verified',
        Provider.rights['display'].astext == 'allowed',
        Provider.rights['cache'].astext == 'allowed',
        cloud_share_sql(),
    )


def instrument_visible_sql():
    has_origin = select(InstrumentOrigin.id).where(InstrumentOrigin.instrument_id == Instrument.id).exists()
    has_allowed_origin = select(InstrumentOrigin.id).join(
        Provider, Provider.id == InstrumentOrigin.source_id).where(
        InstrumentOrigin.instrument_id == Instrument.id, *allowed_origin_sql()).exists()
    return or_(Instrument.local_name.is_not(None), ~has_origin, has_allowed_origin)


def instrument_name_sql():
    """Select a permitted claim's name, never a revoked source's shared cache."""
    name = select(InstrumentOrigin.observed_name).join(
        Provider, Provider.id == InstrumentOrigin.source_id).where(
        InstrumentOrigin.instrument_id == Instrument.id, *allowed_origin_sql()).order_by(
        case((InstrumentOrigin.observed_name == Instrument.symbol, 1), else_=0),
        InstrumentOrigin.last_observed_at.desc(), InstrumentOrigin.source_id).limit(1).correlate(Instrument).scalar_subquery()
    has_origin = select(InstrumentOrigin.id).where(InstrumentOrigin.instrument_id == Instrument.id).exists()
    # A source may know only the code. It must neither shadow a permitted human
    # name nor copy another source's name into its own permission claim.
    return func.coalesce(func.nullif(name, Instrument.symbol), Instrument.local_name, name, case((~has_origin, Instrument.name)))
