"""Choose one whole-market source for a read, never splice supplier histories."""
from datetime import timedelta

from sqlalchemy import func, select, true

from app.evidence.models import EvidenceVersion
from app.market.models import SourceSettings
from app.market.protocol import SHANGHAI
from app.market import read_facets

FAMILIES=frozenset({'tushare_market_rt','tencent_market_rt'})
DATASETS={'tushare_market_rt':'tushare.rt_market','tencent_market_rt':'tencent.rt_market'}


def fresh_native_clock(db, ident, day, cutoff, seconds):
    from app.market.tushare import clock as source_clock
    from app.market.protocol import SourceFailure
    from app.market.session import quote_fresh_at
    rows=func.jsonb_array_elements(EvidenceVersion.payload['rows']).table_valued('value')
    value=rows.c.value.op('->>')('trade_time')
    price=rows.c.value.op('->>')('close')
    clocks=db.scalars(select(value).select_from(EvidenceVersion).join(rows,true()).where(
        EvidenceVersion.id==ident,price.is_not(None),price.not_in(['','0','0.0','0.00'])).distinct())
    for value in clocks:
        try:stamp=source_clock(value,cutoff)
        except SourceFailure:continue
        if (stamp and stamp<=cutoff and stamp.astimezone(SHANGHAI).date()==day
                and quote_fresh_at(cutoff)-stamp<=timedelta(seconds=seconds)):return True
    return False


def choose(db, providers, day, cutoff, *, historical=False):
    from app.integrations.routing import preferred_families
    from app.market.service import permission_reason
    from app.market.tencent_market import saved_scope_allowed
    from app.market.session import quote_fresh_at
    order=preferred_families(db,'stock_quote',list(DATASETS))
    order=[family for family in order if family in FAMILIES]
    if not order:order=list(DATASETS)
    candidates=[]
    for family in order:
        provider=providers.get(family)
        if provider is None or permission_reason(provider):continue
        query=read_facets.with_facets(select(EvidenceVersion.id,EvidenceVersion.received_at)).where(
            EvidenceVersion.source_id==provider.id,EvidenceVersion.kind=='reference',
            EvidenceVersion.owner_id.is_(None),EvidenceVersion.visibility=='shared',
            EvidenceVersion.received_at<=cutoff,read_facets.value('dataset')==DATASETS[family],
            read_facets.observed_day()==day.isoformat())
        query=query.order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(1)
        if family=='tencent_market_rt':
            # Select the authorized latest receipt before opening its raw table.
            # Otherwise a hash-join/sort plan can decode every historical batch.
            latest=query.subquery()
            query=select(latest.c.id,latest.c.received_at,EvidenceVersion.payload['rows'][0].label('first')).join(
                EvidenceVersion,EvidenceVersion.id==latest.c.id)
        latest=db.execute(query).first()
        if latest is None:continue
        if family=='tencent_market_rt' and not saved_scope_allowed(db,{'rows':[latest._mapping['first']]},cutoff,received_at=latest.received_at):continue
        config=db.get(SourceSettings,provider.id)
        seconds=max(30,(config.interval_seconds if config else 60)*2)
        native=(historical or day!=cutoff.astimezone(SHANGHAI).date()
            or fresh_native_clock(db,latest.id,day,cutoff,seconds))
        candidates.append((family,latest.received_at,seconds,native))
    if historical or day!=cutoff.astimezone(SHANGHAI).date():
        return candidates[0][0] if candidates else 'tushare_market_rt'
    clock=quote_fresh_at(cutoff)
    fresh=[entry for entry in candidates if entry[3] and clock-entry[1]<=timedelta(seconds=entry[2])]
    return (fresh or candidates or [('tushare_market_rt',None,0)])[0][0]
