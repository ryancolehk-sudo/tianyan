"""Five-minute changes from native, same-source immutable stock quotes."""
from datetime import timedelta

from sqlalchemy import DateTime, column, func, select, values
from sqlalchemy.dialects.postgresql import UUID

from app.evidence.models import EvidenceVersion


def quote_speeds(reader, quotes, *, strict=False):
    from app.market.terminal_read import segment, pct
    selected={}
    for code,item in quotes.items():
        payload,proof=item.quote,item.provenance
        if (payload and proof and item.instrument.id and payload.quote_time
                and payload.price_unit=='CNY' and payload.quote_kind=='snapshot'
                and payload.price is not None and segment(payload.quote_time)):
            selected[(proof.source_id,item.instrument.id)]=(code,item)
    if not selected:
        return {}
    windows=values(column('source_id',UUID(as_uuid=True)),column('instrument_id',UUID(as_uuid=True)),
        column('anchor',DateTime(timezone=True)),name='quote_speed_windows').data([
            (source,instrument,item.quote.quote_time-timedelta(seconds=300))
            for (source,instrument),(_,item) in selected.items()]).alias('quote_speed_windows')
    candidates=select(EvidenceVersion.id,windows.c.anchor).join(windows,
        (EvidenceVersion.source_id==windows.c.source_id)&(EvidenceVersion.instrument_id==windows.c.instrument_id)).where(
        EvidenceVersion.kind=='quote',EvidenceVersion.visibility=='shared',EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.provenance=='source_reported',EvidenceVersion.received_at<=reader.cutoff,
        EvidenceVersion.event_at>=windows.c.anchor-timedelta(seconds=30),
        EvidenceVersion.event_at<=windows.c.anchor+timedelta(seconds=30))
    if strict:
        candidates=candidates.where(EvidenceVersion.historical_eligible.is_(True),EvidenceVersion.available_at<=reader.cutoff)
    # Keep historical payloads out of the scan. Without this boundary PostgreSQL
    # can decode every old quote before joining the small per-stock windows.
    candidates=candidates.cte('quote_speed_candidates').prefix_with('MATERIALIZED',dialect='postgresql')
    query=select(EvidenceVersion).join(candidates,EvidenceVersion.id==candidates.c.id).where(
        EvidenceVersion.payload['price'].astext.is_not(None),
        EvidenceVersion.payload['price_unit'].astext=='CNY',
        func.coalesce(EvidenceVersion.payload['quote_kind'].astext,'snapshot')=='snapshot')
    query=query.distinct(EvidenceVersion.source_id,EvidenceVersion.instrument_id).order_by(
        EvidenceVersion.source_id,EvidenceVersion.instrument_id,
        func.abs(func.extract('epoch',EvidenceVersion.event_at-candidates.c.anchor)),
        EvidenceVersion.received_at.desc(),EvidenceVersion.version_number.desc(),EvidenceVersion.id.desc())
    result={}
    for previous in reader.db.scalars(query):
        code,item=selected[(previous.source_id,previous.instrument_id)]
        end,start=item.quote.quote_time,previous.event_at
        if segment(start)!=segment(end):
            continue
        speed=pct(item.quote.price,previous.payload.get('price'))
        if speed is not None:
            result[code]=(speed,(end-start).total_seconds())
            reader.record_input(previous)
    return result
