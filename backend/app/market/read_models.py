"""Cached quote views only. Reading a page never requests upstream data."""
from sqlalchemy import case, or_, select
from types import SimpleNamespace
from app.market.tushare_market import quote_from_snapshot
from zoneinfo import ZoneInfo

from app.auth import APIError
from app.evidence.common import now
from app.evidence.models import EvidenceVersion, SourceVersion
from app.evidence.schemas import QuotePayload
from app.market.models import SourceSettings
from app.market.quote_values import changes
from app.market.registry import INDEXES
from app.market.schemas import MarketIdentity, MarketQuote, QuoteBatch, QuoteProvenance
from app.market.service import permission_reason, source_id
from app.market.visibility import instrument_name_sql
from app.integrations.routing import preferred_families
from app.models import Instrument, Provider


def market_identities(db, instrument_id=None):
    if instrument_id is not None:
        row = db.execute(select(Instrument, instrument_name_sql()).where(
            Instrument.id == instrument_id)).first()
        if row is None:
            raise APIError(404, 'instrument_unknown', '标的不存在，请从本机标的查询结果重新选择。')
        instrument, display_name = row
        return [MarketIdentity(id=instrument.id, exchange=instrument.exchange, symbol=instrument.symbol,
            asset_type=instrument.asset_type, name=display_name or '标的资料不可用')]
    result = []
    for exchange, symbol, name in INDEXES:
        instrument = db.scalar(select(Instrument).where(Instrument.exchange == exchange, Instrument.symbol == symbol,
            Instrument.asset_type == 'index', Instrument.valid_to.is_(None))
            .order_by(Instrument.valid_from.desc().nulls_last(), Instrument.id).limit(1))
        # These labels identify the four fixed index positions. They supply no prices.
        result.append(MarketIdentity(id=instrument.id if instrument else None, exchange=exchange, symbol=symbol,
            asset_type='index', name=name))
    return result


def live_quote_records(db, candidates, identities, cutoff):
    """One latest immutable record per supplier/instrument in this read snapshot.

    This only batches the existing live lookup. Historical authorization and
    the separately stored whole-market reference batch keep their own paths.
    """
    sources = [provider.id for provider, _ in candidates
               if provider.family not in {'tushare_market_rt','tencent_market_rt'} and not permission_reason(provider)]
    instruments = [identity.id for identity in identities if identity.id is not None]
    if not sources or not instruments:
        return {}
    daily = [provider.id for provider, _ in candidates
             if provider.family in {'baostock_daily', 'ashare_public', 'tushare_daily', 'tushare_index_daily'}]
    trade_day = case((EvidenceVersion.source_id.in_(daily), EvidenceVersion.payload['trade_day'].astext))
    query = select(EvidenceVersion).where(EvidenceVersion.source_id.in_(sources),
        EvidenceVersion.instrument_id.in_(instruments), EvidenceVersion.kind == 'quote',
        EvidenceVersion.visibility == 'shared', EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= cutoff,
        or_(EvidenceVersion.event_at.is_(None), EvidenceVersion.event_at <= cutoff))
    rows = db.scalars(query.distinct(EvidenceVersion.source_id, EvidenceVersion.instrument_id)
        .order_by(EvidenceVersion.source_id, EvidenceVersion.instrument_id, trade_day.desc().nulls_last(),
            EvidenceVersion.event_at.desc().nulls_last(), EvidenceVersion.version_number.desc(),
            EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()))
    return {(row.source_id, row.instrument_id): row for row in rows}


def quote_batch(db, *, instrument_id=None, instrument_ids=None, as_of_time=None, observation_cutoff=None):
    # Workbench code passes a cutoff already captured and validated by the
    # server. Reuse that clock throughout its read: a wall-clock correction
    # between two subreads must not turn a legitimate live read into future_cutoff.
    # Public query cutoffs still use the current server clock and are rejected.
    current = observation_cutoff if observation_cutoff is not None and as_of_time is None else now(db)
    from app.market.session import quote_fresh_at
    freshness_clock=quote_fresh_at(current) if as_of_time is None else as_of_time
    if as_of_time is not None and as_of_time > current:
        raise APIError(422, 'future_cutoff', '报价查询截止时间不能晚于服务器当前时间。')
    cutoff = as_of_time or observation_cutoff or current
    if cutoff>current:
        raise APIError(422,'future_cutoff','报价查询截止时间不能晚于服务器当前时间。')
    if instrument_ids is not None:
        records = {item.id: (item, name) for item, name in db.execute(select(Instrument, instrument_name_sql()).where(
            Instrument.id.in_(instrument_ids)))}
        if set(records) != set(instrument_ids):
            raise APIError(404, 'instrument_unknown', '批量报价包含不存在的标的，请从本机标的目录重新选择。')
        identities = [MarketIdentity(id=ident, exchange=records[ident][0].exchange, symbol=records[ident][0].symbol,
            asset_type=records[ident][0].asset_type, name=records[ident][1] or '标的资料不可用') for ident in instrument_ids]
    else:
        identities = market_identities(db, instrument_id)
    candidates=[]
    for family in ['fuyao_quotes','fuyao_indices','tdx_quotes','tushare_market_rt','tencent_market_rt','tushare_rt_indices','tushare_rt_k','tickflow_live','tencent_web','ashare_public','tushare_daily','tushare_index_daily','baostock_daily']:
        provider=db.get(Provider,source_id(family));config=db.get(SourceSettings,source_id(family))
        if provider is not None and config is not None:candidates.append((provider,config))
    result = []
    market_snapshots = {}
    market_scope_permissions = {}
    live_records = None
    versions = {}
    source_families = {provider.id: provider.family for provider, _ in candidates}
    def source_version(provider, at):
        key = (provider.id, at)
        if key not in versions:
            versions[key] = db.scalar(select(SourceVersion).where(SourceVersion.source_id == provider.id,
                SourceVersion.recorded_at <= at).order_by(SourceVersion.recorded_at.desc(), SourceVersion.id.desc()).limit(1))
        return versions[key]
    for identity in identities:
        families=preferred_families(db,'index_quote' if identity.asset_type=='index' else 'stock_quote',
            [p.family for p,c in candidates],scope=identity)
        if as_of_time is None:
            from app.market.resilience import serving_order
            families=serving_order(db,'index_quote' if identity.asset_type=='index' else 'stock_quote',families,
                scope=identity,source_families=source_families)
        ordered=sorted([(p,c) for p,c in candidates if p.family in families],key=lambda item:families.index(item[0].family))
        permitted=[(p,c) for p,c in ordered if not permission_reason(p)]
        provider,config=(permitted or ordered or [(None,None)])[0]
        limit=max(120,config.interval_seconds*2) if config else 120
        denied=permission_reason(provider) if provider and config else 'source_not_configured'
        base = dict(instrument=identity, freshness_limit_seconds=limit)
        if denied:
            state = {'source_not_configured': 'not_configured', 'source_disabled': 'disabled'}.get(denied, 'permission_missing')
            result.append(MarketQuote(**base, state=state, reason=denied))
            continue
        if identity.id is None:
            result.append(MarketQuote(**base, state='empty', reason='instrument_not_observed'))
            continue
        def saved_record(candidate):
            nonlocal live_records
            whole_fuyao=as_of_time is None and candidate.family=='fuyao_quotes' and next(c for p,c in candidates
                if p.id==candidate.id).config.get('collection_mode')=='full_market'
            if candidate.family in {'tushare_market_rt','tencent_market_rt'} or whole_fuyao:
                if identity.asset_type != 'stock':return None
                # Historical replay remains unavailable until reference snapshot
                # eligibility is explicitly supported; never leak a later batch.
                if as_of_time is not None: return None
                if candidate.id not in market_snapshots:
                    market_snapshots[candidate.id]=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==candidate.id,
                        EvidenceVersion.kind=='reference', EvidenceVersion.visibility=='shared',
                        EvidenceVersion.owner_id.is_(None), EvidenceVersion.payload['dataset'].astext==
                            ('fuyao.market_snapshot' if whole_fuyao else 'tencent.rt_market' if candidate.family=='tencent_market_rt' else 'tushare.rt_market'),
                        EvidenceVersion.received_at<=cutoff).order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(1))
                saved=market_snapshots[candidate.id]
                if saved is None: return None
                if candidate.family=='tencent_market_rt':
                    from app.market.tencent_market import saved_scope_allowed
                    if saved.id not in market_scope_permissions:
                        market_scope_permissions[saved.id]=saved_scope_allowed(db,saved.payload,cutoff,received_at=saved.received_at)
                    if not market_scope_permissions[saved.id]:return None
                if whole_fuyao:
                    from app.market.fuyao_market import quote_from_snapshot as fuyao_quote
                    quote=fuyao_quote(saved.payload,identity,saved.received_at)
                else:
                    quote=quote_from_snapshot(saved.payload,identity,saved.received_at)
                if quote is None or quote.quote_time and quote.quote_time>cutoff:return None
                decimal=lambda value:format(value,'f') if value is not None else None
                payload=QuotePayload(price=decimal(quote.price),previous_close=decimal(quote.previous_close),
                    open=decimal(quote.open),high=decimal(quote.high),low=decimal(quote.low),
                    volume_shares=decimal(quote.volume_shares),amount_cny=decimal(quote.amount_cny),
                    quote_time=quote.quote_time,price_unit='CNY',instrument_name=quote.name).model_dump(mode='json')
                if quote.source_snapshot_time is not None:
                    payload['source_snapshot_time']=quote.source_snapshot_time.isoformat()
                return SimpleNamespace(id=saved.id,payload=payload,event_at=quote.quote_time,received_at=saved.received_at,
                    content_hash=saved.content_hash,published_at=None,available_at=None,historical_eligible=False)
            if as_of_time is None:
                if live_records is None:
                    live_records = live_quote_records(db, candidates, identities, cutoff)
                return live_records.get((candidate.id, identity.id))
            if as_of_time is not None:
                historical_source=source_version(candidate,cutoff)
                descriptor=historical_source.descriptor if historical_source else {}
                if (not descriptor.get('enabled') or not descriptor.get('configured') or descriptor.get('entitlement_status')!='verified'
                    or descriptor.get('rights',{}).get('display')!='allowed' or descriptor.get('rights',{}).get('cache')!='allowed'):
                    return None
            query = select(EvidenceVersion).where(EvidenceVersion.source_id == candidate.id,
            EvidenceVersion.instrument_id == identity.id, EvidenceVersion.kind == 'quote',
            EvidenceVersion.visibility == 'shared', EvidenceVersion.owner_id.is_(None), EvidenceVersion.provenance == 'source_reported',
            EvidenceVersion.received_at <= cutoff,
            or_(EvidenceVersion.event_at.is_(None), EvidenceVersion.event_at <= cutoff))
            if as_of_time is not None:
                query=query.where(EvidenceVersion.historical_eligible.is_(True),EvidenceVersion.available_at<=cutoff)
            if candidate.family in {'baostock_daily','ashare_public','tushare_daily','tushare_index_daily'}:
                query=query.order_by(EvidenceVersion.payload['trade_day'].astext.desc().nulls_last())
            return db.scalar(query.order_by(EvidenceVersion.event_at.desc().nulls_last(),EvidenceVersion.version_number.desc(),
                EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(1))
        observations=[]
        for candidate,candidate_config in permitted:
            observed=saved_record(candidate)
            if observed is not None:
                source_day=(observed.event_at.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
                    if observed.event_at else observed.payload.get('trade_day') or '')
                fresh = bool(observed.payload.get('quote_time') and observed.event_at and
                    (freshness_clock-observed.event_at).total_seconds() <= max(120,candidate_config.interval_seconds*2))
                if as_of_time is None and candidate.family in {'fuyao_quotes','fuyao_indices'}:
                    from app.market.fuyao_market import current_snapshot
                    if current_snapshot(dict(snapshot_time=observed.payload.get('source_snapshot_time')),
                            observed.received_at,cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date(),cutoff,
                            max(120,candidate_config.interval_seconds*2)):
                        # This is only read priority. It does not populate a
                        # trade day/time or change the partial quote state.
                        source_day=cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
                        fresh=True
                observations.append((source_day,fresh,candidate,candidate_config,observed))
        row=None
        if observations:
            # A newer row with a missing price must not hide a whole usable
            # alternative. Retain the incomplete row only when none has a price.
            priced=[item for item in observations if item[-1].payload.get('price') is not None]
            observations=priced or observations
            # Newer observed market day takes precedence over a frozen older
            # feed. On the same day keep the documented source priority.
            _,_,provider,config,row=max(observations,key=lambda item:(item[0],item[1]))
            if as_of_time is None:
                # Only deliberately enabled focus data with a newer native
                # clock may take over between whole-market batches.
                focused=[entry for entry in observations if entry[2].family=='tencent_web'
                    and entry[3].config.get('collection_mode')=='focused'
                    and entry[-1].event_at is not None
                    and entry[0]==cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
                    and 0 <= (freshness_clock-entry[-1].event_at).total_seconds()
                        <= min(30,max(10,entry[3].interval_seconds*3))]
                if focused:
                    latest=max(focused,key=lambda entry:entry[-1].event_at)
                    if row.event_at is None or latest[-1].event_at>row.event_at:
                        _,_,provider,config,row=latest
            base['freshness_limit_seconds']=limit=max(120,config.interval_seconds*2)
            if provider.family=='tencent_web' and config.config.get('collection_mode')=='focused':
                base['freshness_limit_seconds']=limit=min(30,max(10,config.interval_seconds*3))
        if row is None:
            result.append(MarketQuote(**base, state='empty', reason='no_quote_at_cutoff' if as_of_time else 'no_quote_observed'))
            continue
        version = source_version(provider,row.received_at)
        if version is None:
            result.append(MarketQuote(**base, state='unavailable', reason='source_version_missing'))
            continue
        payload = QuotePayload.model_validate(row.payload)
        # Pick one immutable quote version, including its nulls. Never merge fields
        # from different suppliers, corrections or observations to fill gaps.
        age = max(0, int((freshness_clock - payload.quote_time).total_seconds())) if payload.quote_time else None
        state = 'partial' if payload.price is None or payload.quote_time is None else 'stale' if age > limit else 'available'
        if payload.quote_kind == 'daily_close' and payload.price is not None and payload.trade_day is not None:
            state = 'daily_close'
        if payload.quote_kind=='delayed_reference' and payload.price is not None and payload.trade_day is not None:
            state='delayed_reference'
        reason = ('quote_time_unknown' if payload.price is not None and payload.quote_time is None else 'quote_fields_unknown') if state == 'partial' else 'quote_older_than_display_limit' if state == 'stale' else None
        change, percent = changes(payload.price, payload.previous_close)
        result.append(MarketQuote(**base, state=state, reason=reason, quote=payload, age_seconds=age,
            change=change, change_percent=percent, provenance=QuoteProvenance(source_id=provider.id, family=provider.family,
                source_name=version.descriptor.get('name', provider.name), source_version_id=version.id,
                adapter_version=version.descriptor.get('adapter_version', 'unknown'), evidence_version_id=row.id,
                content_hash=row.content_hash, received_at=row.received_at, published_at=row.published_at,
                available_at=row.available_at, historical_eligible=row.historical_eligible)))
    return QuoteBatch(items=result, selection='batch' if instrument_ids is not None else 'instrument' if instrument_id else 'indices',
        query_time=cutoff, historical=as_of_time is not None)
