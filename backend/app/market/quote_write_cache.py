"""Per-transaction lookup cache for a known focused Tencent quote batch.

The caller already holds source:records and instruments:claims. Writes, hashes,
versions, duplicates and flush order retain the existing per-observation path.
Nothing is shared with another request or source.
"""
from types import SimpleNamespace

from sqlalchemy import select,tuple_
from sqlalchemy.orm import undefer

from app.evidence.models import EvidenceVersion
from app.market.models import InstrumentOrigin
from app.models import Instrument


def warm(db,provider,config,records):
    if (provider.family!='tencent_web' or (getattr(config,'config',None) or {}).get('collection_mode')!='focused'
            or not isinstance(records,(list,tuple)) or len(records)<2
            or any(quote.quote_time is None or getattr(quote,'trade_day',None) is not None for quote in records)):
        return None
    wanted={(quote.instrument.exchange,quote.instrument.symbol,quote.instrument.asset_type) for quote in records}
    instruments=list(db.scalars(select(Instrument).options(undefer(Instrument.local_name)).where(Instrument.valid_to.is_(None),
        tuple_(Instrument.exchange,Instrument.symbol,Instrument.asset_type).in_(wanted))))
    # Unknown or ambiguous identities retain the complete old ingestion path.
    if len(instruments)!=len(wanted):return None
    identities={(row.exchange,row.symbol,row.asset_type):row for row in instruments}
    if set(identities)!=wanted:return None
    ids=[row.id for row in instruments]
    claimed=set(db.scalars(select(InstrumentOrigin.instrument_id).where(
        InstrumentOrigin.instrument_id.in_(ids)).distinct()))
    origins={row.instrument_id:row for row in db.scalars(select(InstrumentOrigin).where(
        InstrumentOrigin.instrument_id.in_(ids),InstrumentOrigin.source_id==provider.id))}
    keys={'quote:'+quote.instrument.identity+':'+quote.quote_time.isoformat() for quote in records}
    latest=dict.fromkeys(keys)
    for row in db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id,
            EvidenceVersion.owner_id.is_(None),EvidenceVersion.source_key.in_(keys))
            .distinct(EvidenceVersion.source_key).order_by(EvidenceVersion.source_key,
                EvidenceVersion.version_number.desc())):
        latest[row.source_key]=row
    return SimpleNamespace(source_id=provider.id,instruments=identities,claimed=claimed,origins=origins,latest=latest)
