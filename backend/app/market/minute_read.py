"""Single-source minute cache reads; never initiate an upstream request."""
from datetime import datetime, time, timedelta

from sqlalchemy import func, select

from app.auth import APIError
from app.evidence.common import now
from app.evidence.models import EvidenceVersion
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SHANGHAI
from app.market.read_models import market_identities
from app.market.schemas import BarItem, BarSeries
from app.market.minute_backfill import LOOKBACK_DAYS, fetch_run_dto
from app.market.minute import FAMILY_INTERVALS
from app.market.service import permission_reason
from app.integrations.routing import preferred_families
from app.models import Provider, Job

INTERVAL_SECONDS = {'1m':60,'5m':300,'15m':900,'30m':1800,'60m':3600}


def _clock(payload):
    raw=payload.get('source_clock_at') or payload.get('bar_end_at')
    if not isinstance(raw,str):return None
    try:
        value=datetime.fromisoformat(raw)
        return value if value.utcoffset() is not None else None
    except ValueError:return None


def _latest_saved_day(db, provider_id, instrument_id, adjustment, interval, clock, requested_day=None):
    """Choose an actually stored day within this permitted series, not weekdays.

    Receipt and source-clock bounds stay independent. Iterate a bounded cursor
    and stop at the latest valid day; do not load all historical price payloads
    into memory or borrow another provider/stock/period to hide an empty series.
    """
    query=select(EvidenceVersion.id,func.row_number().over(partition_by=EvidenceVersion.source_key,
        order_by=EvidenceVersion.version_number.desc()).label('position')).where(
        EvidenceVersion.source_id==provider_id,EvidenceVersion.instrument_id==instrument_id,
        EvidenceVersion.kind=='bar',EvidenceVersion.owner_id.is_(None),EvidenceVersion.visibility=='shared',
        EvidenceVersion.received_at<=clock,EvidenceVersion.payload['interval'].astext==interval,
        EvidenceVersion.payload['adjustment'].astext==adjustment)
    if requested_day is not None:query=query.where(EvidenceVersion.payload['trade_day'].astext==requested_day.isoformat())
    ranked=query.subquery()
    candidates=db.execute(select(EvidenceVersion.payload).join(ranked,ranked.c.id==EvidenceVersion.id)
        .where(ranked.c.position==1,EvidenceVersion.payload['trade_day'].astext<=clock.astimezone(SHANGHAI).date().isoformat())
        .order_by(EvidenceVersion.payload['trade_day'].astext.desc())).yield_per(128)
    try:
        for (payload,) in candidates:
            stamp=_clock(payload)
            if stamp is not None and stamp<=clock and payload.get('trade_day')==stamp.astimezone(SHANGHAI).date().isoformat():
                return stamp.astimezone(SHANGHAI).date()
    finally:
        candidates.close()
    return None


def minute_bars(db,instrument_id,adjustment,interval,day=None,source_id=None):
    automatic_day=day is None
    clock=now(db);local=clock.astimezone(SHANGHAI);day=day or local.date()
    from app.market.session import market_session
    live_day=automatic_day and market_session(db,clock).automatic_live
    lookup_day=day if live_day or not automatic_day else None
    instrument=market_identities(db,instrument_id)[0]
    if day>local.date():raise APIError(422,'future_day','不能读取未来交易日的行情。')
    if adjustment!='unadjusted':raise APIError(422,'minute_adjustment_unsupported','当前分钟来源仅支持不复权原始价格。')
    if interval not in INTERVAL_SECONDS:raise APIError(422,'unsupported_interval','分钟周期尚未实现。')
    base=dict(instrument=instrument,source_name=None,adjustment=adjustment,interval=interval,observation_day=None if automatic_day else day,
              items=[],data_mode='source_minutes',timestamp_semantics='unknown',finality='unknown',historical_reconstruction=False)
    if live_day:base['observation_day']=day
    providers=list(db.scalars(select(Provider).where(Provider.family.is_not(None))))
    candidates=[p for p in providers if 'bar.minute' in p.capabilities and 'bar.'+interval in p.capabilities]
    if source_id is not None:
        candidates=[p for p in candidates if p.id==source_id]
        if not candidates:raise APIError(422,'minute_source_unsupported','该来源不提供所选分钟周期。')
    def matches(provider):
        config=db.get(SourceSettings,provider.id)
        return bool(config and (provider.family=='tdx_full_minute' and config.config.get('collection_mode')=='full_market'
            or any(s.get('exchange')==instrument.exchange and s.get('symbol')==instrument.symbol
                    and s.get('asset_type')==instrument.asset_type for s in config.config.get('symbols',[]))))
    # Pick one provider for the entire response. Future paid minute adapters can
    # opt into the same capabilities; no cross-provider per-bar filling occurs.
    families=preferred_families(db,'stock_minute_'+interval,[p.family for p in candidates],scope=instrument)
    if source_id is None and day==local.date():
        from app.market.resilience import serving_order
        families=serving_order(db,'stock_minute_'+interval,families,scope=instrument)
    if source_id is None:candidates=[p for p in candidates if p.family in families]
    candidates.sort(key=lambda p:(bool(permission_reason(p)),not matches(p),families.index(p.family) if p.family in families else len(families),str(p.id)))
    scoped=[p for p in candidates if source_id or matches(p)]
    provider=next(iter(scoped),None)
    saved_days={}
    if source_id is None:
        available=[]
        for candidate in scoped:
            if permission_reason(candidate):continue
            saved=_latest_saved_day(db,candidate.id,instrument_id,adjustment,interval,clock,lookup_day)
            saved_days[candidate.id]=saved
            if saved is None:continue
            cfg=db.get(SourceSettings,candidate.id)
            failed=bool(cfg and cfg.paused_reason)
            if saved==local.date():
                attempt=db.scalar(select(DataFetchRun).where(DataFetchRun.source_id==candidate.id,
                    DataFetchRun.received_at<=clock).order_by(DataFetchRun.received_at.desc(),DataFetchRun.id.desc()).limit(1))
                failed=failed or bool(attempt and attempt.status in {'error','unavailable'})
            available.append((candidate,saved,failed))
        if available:
            # Select one whole permitted series that actually covers this day.
            # Empty/failed primaries cannot hide a matching healthy fallback.
            newest=max(item[1] for item in available)
            same_day=[item for item in available if item[1]==newest]
            healthy=[item for item in same_day if not item[2]] or same_day
            provider=healthy[0][0]
    if provider is None:return BarSeries(**base,state='not_configured',reason='minute_source_not_configured')
    config=db.get(SourceSettings,provider.id)
    base.update(source_id=provider.id,source_name=provider.name,source_family=provider.family)
    denied=permission_reason(provider)
    if denied or config is None:
        return BarSeries(**base,state='disabled' if denied=='source_disabled' else 'permission_missing',reason=denied or 'source_not_configured')
    base['backfill_supported']=provider.family in FAMILY_INTERVALS and matches(provider) and not config.paused_reason
    base['backfill_lookback_days']=LOOKBACK_DAYS if base['backfill_supported'] else None
    if automatic_day:
        saved_day=saved_days.get(provider.id) or _latest_saved_day(db,provider.id,instrument_id,adjustment,interval,clock,lookup_day)
        day=saved_day or day
        base['observation_day']=day if live_day else saved_day
    base['freshness_limit_seconds']=max(120,config.interval_seconds*2)
    start=datetime.combine(day,time.min,SHANGHAI)
    historical_jobs=select(Job.id).where(Job.kind=='source.collect',
        Job.payload['minute_context']['acquisition_mode'].astext=='backfill')
    day_jobs=select(Job.id).where(Job.kind=='source.collect',
        Job.payload['minute_context']['acquisition_mode'].astext=='backfill',
        Job.payload['minute_context']['requested_day'].astext==day.isoformat())
    scoped=((DataFetchRun.job_id.in_(day_jobs)) | ((DataFetchRun.job_id.is_(None) | ~DataFetchRun.job_id.in_(historical_jobs))
        & (DataFetchRun.received_at>=start) & (DataFetchRun.received_at<start+timedelta(days=1))))
    attempt=db.scalar(select(DataFetchRun).where(DataFetchRun.source_id==provider.id,scoped)
                      .order_by(DataFetchRun.received_at.desc(),DataFetchRun.id.desc()).limit(1))
    base['last_attempt']=fetch_run_dto(db,attempt) if attempt else None
    base['last_success_at']=db.scalar(select(func.max(DataFetchRun.received_at)).where(
        DataFetchRun.source_id==provider.id,scoped,DataFetchRun.status.in_(['complete','partial','empty'])))
    # Filter before ranking/limit so daily bars, older dates, other intervals or
    # another provider cannot crowd the requested day's minute observations out.
    ranks=select(EvidenceVersion.id,func.row_number().over(partition_by=EvidenceVersion.source_key,
        order_by=EvidenceVersion.version_number.desc()).label('position')).where(
        EvidenceVersion.source_id==provider.id,EvidenceVersion.instrument_id==instrument_id,
        EvidenceVersion.kind=='bar',EvidenceVersion.owner_id.is_(None),EvidenceVersion.visibility=='shared',
        EvidenceVersion.received_at<=clock,EvidenceVersion.payload['interval'].astext==interval,
        EvidenceVersion.payload['adjustment'].astext==adjustment,
        EvidenceVersion.payload['trade_day'].astext==day.isoformat()).subquery()
    rows=list(db.scalars(select(EvidenceVersion).join(ranks,ranks.c.id==EvidenceVersion.id)
        .where(ranks.c.position==1).order_by(EvidenceVersion.source_key).limit(2000)))
    rows=[row for row in rows if _clock(row.payload) and _clock(row.payload)<=clock]
    rows.sort(key=lambda row:(_clock(row.payload),row.source_key))
    if rows:
        base['latest_source_time']=_clock(rows[-1].payload)
        base['age_seconds']=max(0,int((clock-base['latest_source_time']).total_seconds()))
    for row in rows:
        stamp=_clock(row.payload)
        # Without an upstream interval-end convention/finality marker, even a
        # later timestamp does not prove an earlier candle is vendor-final.
        state='unknown'
        if row.payload.get('clock_semantics') in {'start','end'} and row.payload.get('bar_end_at'):
            end=datetime.fromisoformat(row.payload['bar_end_at'])
            state='elapsed' if end<=clock else 'forming'
        base['items'].append(BarItem(id=row.id,received_at=row.received_at,content_hash=row.content_hash,
            payload=row.payload,period_state=state,
            arrival_kind='backfill' if row.payload.get('arrival_kind')=='backfill' else 'late_observed' if row.received_at-stamp>timedelta(seconds=INTERVAL_SECONDS[interval]*2) else 'observed'))
    # Report observed intra-session spacing, not a claim of missing trades or
    # suspension. Never manufacture rows across a gap or the lunch break.
    stamps=[_clock(row.payload).astimezone(SHANGHAI) for row in rows]
    base['gap_count']=sum((b-a).total_seconds()>INTERVAL_SECONDS[interval] for a,b in zip(stamps,stamps[1:])
        if (a.time()<=time(11,30) and b.time()<=time(11,30)) or (a.time()>=time(13) and b.time()>=time(13)))
    reason=None
    if not rows:state='empty';reason='minute_not_observed'
    elif day<local.date() or not(time(9,30)<=local.time()<=time(11,31) or time(13)<=local.time()<=time(15,1)):
        state='historical';reason='saved_minutes_not_live_session'
    elif base['age_seconds']>base['freshness_limit_seconds']:state='stale';reason='minute_source_lag'
    else:state='available';reason='source_clock_semantics_unconfirmed'
    if day==local.date() and attempt and attempt.status in {'error','unavailable'}:
        state='partial' if rows else 'unavailable';reason=attempt.reason
    elif day==local.date() and attempt and attempt.status=='partial':
        state='partial';reason=attempt.reason or 'upstream_rows_rejected'
    if config.paused_reason and day==local.date():state='partial' if rows else 'unavailable';reason=config.paused_reason
    return BarSeries(**base,state=state,reason=reason)


def current_daily(db,instrument_id,adjustment):
    """Project only actual paid-source daily OHLC; do not infer intraday bars."""
    if adjustment!='unadjusted':return None
    from app.market.read_models import quote_batch
    view=quote_batch(db,instrument_id=instrument_id).items[0]
    payload=view.quote;provenance=view.provenance
    if not payload or not provenance or provenance.family not in {'tushare_market_rt','tushare_rt_k','tushare_rt_indices','fuyao_quotes','fuyao_indices','tdx_quotes'}:
        return None
    stamp=payload.quote_time
    if not stamp or stamp.astimezone(SHANGHAI).date()!=now(db).astimezone(SHANGHAI).date():return None
    if any(getattr(payload,key) is None for key in ['open','high','low','price']):return None
    from app.evidence.schemas import BarPayload
    bar=BarPayload(open=payload.open,high=payload.high,low=payload.low,close=payload.price,
        volume_shares=payload.volume_shares,amount_cny=payload.amount_cny,interval='1d',bar_end_at=None,
        source_clock_at=stamp,clock_semantics='unknown',trade_day=stamp.astimezone(SHANGHAI).date(),
        adjustment='unadjusted',price_unit=payload.price_unit)
    item=BarItem(id=provenance.evidence_version_id,received_at=provenance.received_at,
        content_hash=provenance.content_hash,payload=bar,period_state='forming' if now(db).astimezone(SHANGHAI).time()<time(15) else 'elapsed',
        projected_from_quote=True)
    return item,provenance,view
