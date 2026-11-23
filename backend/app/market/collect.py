"""Collect under the worker lease and persist verified observations atomically."""
import hashlib
import calendar
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.common import content_hash, lock, normalized, now
from app.evidence.models import EvidenceVersion, SourceVersion
from app.evidence.schemas import BarPayload, CalendarPayload, EvidenceInput, QuotePayload
from app.integrations.credentials import assert_source_binding
from app.market.models import DataFetchRun, InstrumentOrigin, SourceSettings
from app.market.daily import fetch_daily, parse_daily
from app.market.protocol import (
    CNINFO_CATALOG_URL, SHANGHAI, InstrumentKey, SourceFailure, fetch_https,
    parse_cninfo_catalog, parse_szse_calendar, parse_tencent_quotes, quote_url,
)
from app.market.registry import INDEXES
from app.market.registry import SOURCES
from app.market.connected_specs import FUYAO_FAMILIES, TDX_FAMILIES
from app.market.direction_specs import PUBLIC_FAMILIES as DIRECTION_FAMILIES
from app.market.kpl_atlas_budget import FAMILIES as KPL_FAMILIES
from app.market.reference import fetch_references, parse_references
from app.market.public import URLS as PUBLIC_URLS, NewsBatch, fetch_cninfo_batch, fetch_public, parse_public
from app.market.service import permission_reason
from app.market.live import ashare_url, fetch_tickflow, parse_ashare, parse_tickflow
from app.market.breadth import URL as BREADTH_URL, parse_breadth
from app.market.sector_heat import URL as SECTOR_HEAT_URL, parse_sector_heat
from app.market.budget import (ASHARE_FAMILIES, reserve_ashare_request, reserve_auction_request,
                               reserve_mairui_request)
from app.market.mairui import FAMILIES as MAIRUI_FAMILIES, fetch_mairui, parse_mairui, pool_day
from app.market.tushare_specs import FAMILIES as TUSHARE_FAMILIES, SPECS as TUSHARE_SPECS
from app.market.eastmoney_trading import FAMILIES as EASTMONEY_FAMILIES, fetch_rankings, parse_rankings
from app.market.budget import reserve_eastmoney_request
from app.market.tushare import request as request_tushare, parameters as tushare_parameters, parse_quotes as parse_tushare_quotes, parse_catalog as parse_tushare_catalog, parse_calendar as parse_tushare_calendar, parse_reference as parse_tushare_reference
from app.market.budget import reserve_tushare_request, reserve_tushare_batch_request, reserve_sina_request
from app.market.budget import reserve_news_request
from app.market.minute_backfill import configuration_fingerprint, is_backfill, live_run_condition, target_day, validate_context
from app.market.minute import FAMILY_INTERVALS, minute_url, parse_sina_minutes
from app.market.minute_schedule import next_minute
from app.market.auction import AUCTION_FAMILIES, fetch_auction, parse_auction
from app.market.auction_source import (fetch_auction as fetch_auction_table, parse_auction as parse_auction_table,
                                       parse_history as parse_auction_history)
from app.models import AuditEvent, Instrument, Job, Provider
from app.market.daily_history import historical_job, fingerprint as history_fingerprint, validate_history
from app.market.auction_schedule import next_collection, schema_recheck
from app.market.realtime_schedule import permit_realtime, RealtimeWait

# The table kind belongs to the source, never to the response: a family can only
# ever write its own table kind.
AUCTION_TABLE_KINDS = {'duanxianxia_auction': 'current', 'duanxianxia_auction_history': 'history'}


def text_decimal(value):
    return format(value, 'f') if value is not None else None


def auction_stage_bucket(payload, clock):
    day = payload.get('observed_day')
    if not day:
        return -1
    return sum(clock >= datetime.fromisoformat(f'{day}T{stage}:00+08:00') for stage in ('09:15', '09:20', '09:25'))


def store_observation(db, provider, config, *, source_key, kind, payload, received_at, event_at=None, published_at=None, instrument_id=None, eligible=False, repeated_state=False, _quote_cache=None):
    revision=content_hash({'payload':payload,'published_at':published_at}) if published_at is not None else content_hash(payload)
    scope=(EvidenceVersion.source_id==provider.id,EvidenceVersion.owner_id.is_(None),EvidenceVersion.source_key==source_key)
    if _quote_cache is not None and (kind!='quote' or repeated_state or _quote_cache.source_id!=provider.id):
        raise ValueError('Quote lookup cache scope differs')
    if repeated_state:
        previous=db.scalar(select(EvidenceVersion).where(*scope).order_by(EvidenceVersion.version_number.desc()).limit(1))
        timed_breadth = kind == 'reference' and ((provider.family == 'ashare_breadth' and source_key == 'reference:market.ashare.breadth') or provider.family == 'tushare_market_rt' or provider.family in EASTMONEY_FAMILIES)
        timed_auction = (provider.family == 'duanxianxia_auction' and kind == 'reference'
            and normalized(payload).get('dataset') in {'market.duanxianxia.auction.summary', 'market.duanxianxia.auction.stocks'}
            and normalized(payload).get('observed_day') == received_at.astimezone(SHANGHAI).date().isoformat()
            and '09:15' <= received_at.astimezone(SHANGHAI).strftime('%H:%M') < '09:35')
        timed_breadth = timed_breadth or timed_auction
        if timed_auction and previous is not None and received_at < previous.received_at:
            return False
        if timed_breadth and previous is not None and previous.received_at == received_at and previous.payload == normalized(payload):
            return False
        # Even an unchanged response must be observed again AFTER a checkpoint
        # before a rolling future column can be used as that stage's reference.
        # Keep immutable older observations; same-stage duplicates still dedupe.
        crossed_auction_stage = (provider.family == 'duanxianxia_auction' and kind == 'reference'
            and normalized(payload).get('dataset') in {'market.duanxianxia.auction.summary', 'market.duanxianxia.auction.stocks'}
            and previous is not None
            and auction_stage_bucket(normalized(payload), received_at) > auction_stage_bucket(previous.payload, previous.received_at))
        if not timed_breadth and not crossed_auction_stage and previous is not None and previous.payload==normalized(payload) and previous.published_at==published_at:
            return False
        # A→B→A is a new observed state, not a replay of the first A. This
        # locally derived revision never invents a supplier publication clock.
        revision=content_hash({'content':revision,'previous_observation':str(previous.id) if previous else None})
    else:
        warmed=_quote_cache is not None and source_key in _quote_cache.latest
        previous=_quote_cache.latest[source_key] if warmed else None
        if warmed and previous is not None and previous.source_revision==revision:
            return False
        # A corrected native timestamp may replay an older revision. Check all
        # revisions in that rare case, exactly as the uncached path does.
        if not warmed or previous is not None:
            duplicate=db.scalar(select(EvidenceVersion).where(*scope,EvidenceVersion.source_revision==revision))
            if duplicate:return False
        if not warmed:
            previous=db.scalar(select(EvidenceVersion).where(*scope).order_by(EvidenceVersion.version_number.desc()).limit(1))
    body=EvidenceInput(source_id=provider.id,source_key=source_key,source_revision=revision,kind=kind,instrument_id=instrument_id,
        event_at=event_at,published_at=published_at,authorship='external',rights_basis=config.rights_basis,payload=payload,
        supersedes_id=previous.id if previous else None)
    row=EvidenceVersion(**body.model_dump(exclude={'payload','published_at','authorship'}),payload=normalized(payload),published_at=published_at,
        owner_id=None,visibility='shared',version_number=previous.version_number+1 if previous else 1,received_at=received_at,
        available_at=max(value for value in (received_at,event_at,published_at) if value is not None) if eligible else None,
        availability='known' if eligible else 'unknown',provenance='source_reported',historical_eligible=eligible,content_hash=content_hash(body))
    db.add(row); db.flush()
    if _quote_cache is not None:_quote_cache.latest[source_key]=row
    return True


def latest_source_version_id(db, provider):
    return db.scalar(select(SourceVersion.id).where(SourceVersion.source_id == provider.id)
                     .order_by(SourceVersion.recorded_at.desc(), SourceVersion.id.desc()).limit(1))


def record_instrument_origin(db, instrument, provider, *, kind, name, content_hash_value, received_at, source_version_id, _quote_cache=None):
    """Keep one current claim per (instrument, source) plus its version evidence.

    The row itself is not display permission: reads must recheck the source's
    current rights, which is why this is stored separately from instruments.
    """
    if _quote_cache is not None and _quote_cache.source_id!=provider.id:
        raise ValueError('Quote origin cache scope differs')
    origin = (_quote_cache.origins.get(instrument.id) if _quote_cache is not None else
        db.scalar(select(InstrumentOrigin).where(InstrumentOrigin.instrument_id == instrument.id,
            InstrumentOrigin.source_id == provider.id)))
    if origin is None:
        origin=InstrumentOrigin(instrument_id=instrument.id, source_id=provider.id, origin_kind=kind,
            observed_name=name, first_observed_at=received_at, last_observed_at=received_at,
            last_content_hash=content_hash_value, source_version_id=source_version_id)
        db.add(origin)
        if _quote_cache is not None:
            _quote_cache.origins[instrument.id]=origin;_quote_cache.claimed.add(instrument.id)
        return
    if (origin.observed_name, origin.last_content_hash, origin.source_version_id) != (name, content_hash_value, source_version_id):
        origin.observed_name = name
        origin.last_content_hash = content_hash_value
        origin.last_observed_at = received_at
        origin.source_version_id = source_version_id


def preserve_local_name(db, instrument, *, claimed=None):
    # A pre-existing local identity must survive a later external claim/revocation.
    # Call before adding the first source claim, never on a just-created source row.
    if instrument.local_name is None and (instrument.id not in claimed if claimed is not None else
            not db.scalar(select(InstrumentOrigin.id).where(InstrumentOrigin.instrument_id == instrument.id).limit(1))):
        instrument.local_name = instrument.name


def store_quote_batch(db, provider, config, batch):
    if permission_reason(provider):
        raise SourceFailure('permission_missing')
    lock(db, f'source:records:{provider.id}')
    lock(db, 'instruments:claims')
    version_id = latest_source_version_id(db, provider)
    from app.market.quote_write_cache import warm
    quote_cache=warm(db,provider,config,batch.records)
    quote_cache_args={'_quote_cache':quote_cache} if quote_cache is not None else {}
    accepted = duplicates = 0
    for quote in batch.records:
        key = quote.instrument
        instrument = (quote_cache.instruments.get((key.exchange,key.symbol,key.asset_type)) if quote_cache is not None else
            db.scalar(select(Instrument).where(Instrument.exchange == key.exchange, Instrument.symbol == key.symbol,
                Instrument.asset_type == key.asset_type, Instrument.valid_to.is_(None)).order_by(Instrument.valid_from.desc().nulls_last()).limit(1)))
        if instrument is None:
            instrument = Instrument(id=uuid.uuid5(uuid.NAMESPACE_URL, 'jiulong-pro-max/instrument/'+key.identity),
                exchange=key.exchange,symbol=key.symbol,asset_type=key.asset_type,name=quote.name,valid_from=None,valid_to=None,first_received_at=quote.received_at)
            db.add(instrument); db.flush()
        else:
            if quote_cache is not None:preserve_local_name(db,instrument,claimed=quote_cache.claimed)
            else:preserve_local_name(db,instrument)
        payload = QuotePayload(price=text_decimal(quote.price), previous_close=text_decimal(quote.previous_close), open=text_decimal(quote.open),
            high=text_decimal(quote.high), low=text_decimal(quote.low), quote_time=quote.quote_time, price_unit=quote.price_unit,
            volume_shares=text_decimal(quote.volume_shares) if provider.family in {'tencent_web','tushare_rt_k','tushare_rt_indices'} | FUYAO_FAMILIES | TDX_FAMILIES else None,
            amount_cny=text_decimal(quote.amount_cny) if provider.family in {'tencent_web','tushare_rt_k','tushare_rt_indices'} | FUYAO_FAMILIES | TDX_FAMILIES else None, instrument_name=quote.name)
        trade_day = getattr(quote, 'trade_day', None)
        if getattr(quote,'source_snapshot_time',None) is not None:
            payload = payload.model_copy(update={'source_snapshot_time': quote.source_snapshot_time})
        if trade_day is not None:
            payload = payload.model_copy(update={'quote_kind': getattr(quote,'quote_kind','daily_close'), 'trade_day': trade_day,
                'volume_shares': quote.volume_shares, 'amount_cny': quote.amount_cny})
        revision = content_hash(payload)
        record_instrument_origin(db, instrument, provider, kind='quote', name=quote.name,
            content_hash_value=revision, received_at=quote.received_at, source_version_id=version_id,**quote_cache_args)
        # Each source time is its own series item. Late ticks cannot supersede a later tick.
        source_key = 'quote:'+key.identity+':'+(quote.quote_time.isoformat() if quote.quote_time else 'unknown:'+revision[:16])
        if trade_day is not None:
            source_key = ('daily-close' if payload.quote_kind=='daily_close' else payload.quote_kind) + ':' + key.identity + ':' + str(trade_day)
        observed = quote.quote_time is not None and quote.price is not None
        created=store_observation(db,provider,config,source_key=source_key,kind='quote',payload=payload,received_at=quote.received_at,
                                  event_at=quote.quote_time,instrument_id=instrument.id,eligible=observed,**quote_cache_args)
        accepted += int(created)
        duplicates += int(not created)
    return accepted,duplicates


def store_calendar_batch(db,provider,config,days,received_at,exchanges=('SZSE',)):
    if permission_reason(provider):
        raise SourceFailure('permission_missing')
    lock(db,f'source:records:{provider.id}')
    accepted=duplicates=0
    calendar_version=('SZSE-month:' if provider.family == 'szse_public' else provider.adapter_version + ':')+str(days[0][0])[:7]+':'+content_hash(days)
    for exchange in exchanges:
        for day,is_open in days:
            payload=CalendarPayload(exchange=exchange,trade_day=day,is_open=is_open,calendar_version=calendar_version)
            created=store_observation(db,provider,config,source_key='calendar:'+exchange+':'+str(day),kind='calendar',payload=payload,
                                      received_at=received_at,eligible=True)
            accepted += int(created)
            duplicates += int(not created)
    return accepted,duplicates


def upsert_instrument(db, key: InstrumentKey, name: str, received_at):
    """Create or refresh a current instrument row. Same identity UUID as quote ingestion."""
    instrument = db.scalar(select(Instrument).where(
        Instrument.exchange == key.exchange, Instrument.symbol == key.symbol,
        Instrument.asset_type == key.asset_type, Instrument.valid_to.is_(None))
        .order_by(Instrument.valid_from.desc().nulls_last()).limit(1))
    if instrument is None:
        instrument = Instrument(
            id=uuid.uuid5(uuid.NAMESPACE_URL, 'jiulong-pro-max/instrument/' + key.identity),
            exchange=key.exchange, symbol=key.symbol, asset_type=key.asset_type, name=name,
            valid_from=None, valid_to=None, first_received_at=received_at)
        db.add(instrument)
        db.flush()
        return True, instrument
    preserve_local_name(db, instrument)
    if instrument.name != name:
        instrument.name = name
        return True, instrument
    return False, instrument


def store_catalog_batch(db, provider, config, batch, received_at, *, response_body=None):
    """Persist identities and, with original HTTP bytes, the immutable received scope."""
    if permission_reason(provider):
        raise SourceFailure('permission_missing')
    captures=[]
    if response_body is not None:
        from app.market.catalog_scope import capture_payloads
        captures=capture_payloads(db,provider,batch,response_body,received_at)
    lock(db, f'source:records:{provider.id}')
    lock(db, 'instruments:claims')
    version_id = latest_source_version_id(db, provider)
    accepted = duplicates = 0
    # Four verified index identities stay searchable before any quote arrives.
    # They are recorded with the catalog claim so revoking its display scope
    # hides them too, instead of leaving a source-derived row visible.
    for exchange, symbol, name in INDEXES:
        key = InstrumentKey(exchange, symbol, 'index')
        created, instrument = upsert_instrument(db, key, name, received_at)
        record_instrument_origin(db, instrument, provider, kind='catalog', name=name,
            content_hash_value=content_hash({'identity': key.identity, 'name': name}),
            received_at=received_at, source_version_id=version_id)
        accepted += int(created)
        duplicates += int(not created)
    for row in batch.records:
        created, instrument = upsert_instrument(db, row.instrument, row.name, received_at)
        record_instrument_origin(db, instrument, provider, kind='catalog', name=row.name,
            content_hash_value=content_hash({'name': row.name, 'org_id': row.org_id, 'category': row.category}),
            received_at=received_at, source_version_id=version_id)
        accepted += int(created)
        duplicates += int(not created)
    for key,payload in captures:
        store_observation(db,provider,config,source_key=key,kind='reference',payload=payload,
            received_at=received_at,repeated_state=True)
    return accepted, duplicates


def store_reference_batch(db,provider,config,batch,received_at):
    if permission_reason(provider):raise SourceFailure('permission_missing')
    lock(db,f'source:records:{provider.id}');lock(db,'instruments:claims')
    accepted=duplicates=0
    def identity(key):
        if key is None:return None
        row=db.scalar(select(Instrument).where(Instrument.exchange==key.exchange,Instrument.symbol==key.symbol,
            Instrument.asset_type==key.asset_type,Instrument.valid_to.is_(None)).limit(1))
        if row is None:
            _,row=upsert_instrument(db,key,key.symbol,received_at)
        else:
            preserve_local_name(db,row)
        record_instrument_origin(db,row,provider,kind='quote',name=key.symbol,content_hash_value=content_hash(key.identity),
            received_at=received_at,source_version_id=latest_source_version_id(db,provider))
        return row.id
    for key,payload,instrument in batch.records:
        created=store_observation(db,provider,config,source_key='reference:'+key,kind='reference',payload=payload,
            received_at=received_at,instrument_id=identity(instrument),repeated_state=provider.family in {'ashare_breadth','ashare_sector_heat','tushare_market_rt',*MAIRUI_FAMILIES,*AUCTION_TABLE_KINDS,*EASTMONEY_FAMILIES,*FUYAO_FAMILIES,*TDX_FAMILIES,*DIRECTION_FAMILIES})
        accepted+=int(created);duplicates+=int(not created)
    for instrument,payload in batch.bars:
        key=f'bar:{instrument.identity}:{payload.interval}:{payload.adjustment}:{payload.trade_day}'
        created=store_observation(db,provider,config,source_key=key,kind='bar',payload=payload,received_at=received_at,instrument_id=identity(instrument),
            repeated_state=provider.family in {'tushare_daily','tushare_index_daily','baostock_history',*FUYAO_FAMILIES})
        accepted+=int(created);duplicates+=int(not created)
    return accepted,duplicates


def store_minute_batch(db, provider, config, batch, received_at, *, backfill=False):
    """One source clock per identity/period; immutable A→B→A observations.

    Unknown clock semantics never become claimed close times or strict-history
    eligibility. Nothing fills missing minutes, normalizes unknown units or
    relabels an old observation as an earlier receipt.
    """
    if permission_reason(provider):
        raise SourceFailure('permission_missing')
    lock(db, f'source:records:{provider.id}')
    lock(db, 'instruments:claims')
    accepted = duplicates = 0
    for observation in batch.records:
        key = observation.instrument
        instrument = db.scalar(select(Instrument).where(Instrument.exchange == key.exchange,
            Instrument.symbol == key.symbol, Instrument.asset_type == key.asset_type,
            Instrument.valid_to.is_(None)).limit(1))
        if instrument is None:
            _, instrument = upsert_instrument(db, key, key.symbol, received_at)
        else:
            preserve_local_name(db, instrument)
        payload = BarPayload(open=text_decimal(observation.open), high=text_decimal(observation.high),
            low=text_decimal(observation.low), close=text_decimal(observation.close),
            volume_shares=None, amount_cny=None, volume_native=text_decimal(observation.volume_native),
            amount_native=text_decimal(observation.amount_native), interval=observation.interval,
            bar_end_at=None, source_clock_at=observation.source_time, clock_semantics='unknown',
            trade_day=observation.source_time.astimezone(SHANGHAI).date(), price_unit='CNY', adjustment='unadjusted')
        if provider.family in TDX_FAMILIES:
            payload.volume_shares=text_decimal(observation.volume_native)
            payload.amount_cny=text_decimal(observation.amount_native)
        source_key = (f'minute:{key.identity}:{observation.interval}:unadjusted:'
                      + observation.source_time.astimezone(UTC).isoformat())
        # Arrival mode is provenance metadata, never a new market revision.
        previous = db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id,
            EvidenceVersion.source_key==source_key, EvidenceVersion.owner_id.is_(None),
            EvidenceVersion.kind=='bar').order_by(EvidenceVersion.version_number.desc()).limit(1))
        if previous is not None:
            old = {k:v for k,v in previous.payload.items() if k!='arrival_kind'}
            if old == normalized(payload):
                duplicates += 1
                continue
            if backfill:
                batch.rejected['existing:'+source_key] = 'backfill_conflict_existing_observation'
                continue
        if backfill:
            payload.arrival_kind = 'backfill'
        created = store_observation(db, provider, config, source_key=source_key, kind='bar', payload=payload,
            received_at=received_at, event_at=observation.source_time, instrument_id=instrument.id,
            eligible=False, repeated_state=True)
        accepted += int(created)
        duplicates += int(not created)
    return accepted, duplicates


def store_auction_batches(db, provider, config, batches, received_at):
    """Persist summary + stocks of every batch inside the caller's transaction.

    One response is one unit of truth: if any batch fails to store, nothing from
    that response is kept, including historical days already written in it.
    Re-observing an identical table writes no new version.
    """
    if permission_reason(provider):
        raise SourceFailure('permission_missing')
    accepted = duplicates = 0
    for batch in batches:
        added, repeated = store_reference_batch(db, provider, config, batch, received_at)
        accepted += added
        duplicates += repeated
    return accepted, duplicates


def store_news_batch(db,provider,config,batch,received_at):
    if permission_reason(provider):raise SourceFailure('permission_missing')
    lock(db,f'source:records:{provider.id}')
    accepted=duplicates=0
    for key,payload,published in batch.records:
        created=store_observation(db,provider,config,source_key='news:'+key,kind='news',payload=payload,received_at=received_at,
            published_at=published,eligible=published is not None)
        accepted+=int(created);duplicates+=int(not created)
    from app.news_center.service import sync_news_index
    sync_news_index(db,provider,config,batch,received_at)
    return accepted,duplicates


def collect_job(job):
    if not settings().data_collection_enabled:
        # Read-only deployments never acquire a source connection by accident.
        from app.worker import finish
        finish(job.id,job.lease_token,error_code='collection_disabled')
        return
    started=datetime.now(UTC)
    from app.market.collection_timings import CollectionTiming
    timing=CollectionTiming(job,started)
    first_request_started=None
    response=batch=days=daily=reference=news=None
    news_coverage=None
    direction_diagnostic=None
    ths_page_diagnostic=None
    transport_diagnostic=None
    funds_time_diagnostic=None
    eastmoney_tables_complete=False
    auction_batches=None
    minutes=None
    reason=None
    backfill=is_backfill(job.payload)
    from app.market.reference_backfill import is_backfill as reference_job
    reference_backfill=reference_job(job.payload)
    warning_reason=None
    history=historical_job(job.payload)
    requests=0
    limited_wait=None
    with Session(engine()) as db:
        source=uuid.UUID(job.payload['source_id'])
        provider=db.get(Provider,source)
        config=db.get(SourceSettings,source)
        if provider is None or config is None:
            raise SourceFailure('source_not_configured')
        family=provider.family
        timing.bind(family)
        book_trial=family in {'tencent_auction','fuyao_auction'} and config.config.get('collection_mode')=='full_market'
        book_offset=0
        book_context_reason=None
        if book_trial:
            from app.market.tdx_full_minute import universe
            selection_book=universe(db,config)
            if family=='fuyao_auction':
                from app.market.fuyao_auction import selection as native_selection
                try:selection_book=native_selection(db,config,started)
                except SourceFailure as exc:
                    selection_book=[];book_context_reason=exc.reason
            book_scope=content_hash([key.identity for key in selection_book])
            last_book=db.scalar(select(Job).where(Job.kind=='source.collect',Job.state=='succeeded',
                Job.payload['source_id'].astext==str(source)).order_by(Job.completed_at.desc()).limit(1))
            if last_book and isinstance(last_book.result_ref,dict) and last_book.result_ref.get('universe_hash')==book_scope:
                book_offset=last_book.result_ref.get('next_offset',0)
        configuration=configuration_fingerprint(provider,config)
        if history:
            configuration=history_fingerprint(provider,config)
        denied=permission_reason(provider) or book_context_reason
        bound = None
        try:
            bound = assert_source_binding(db, source, family, job.payload)
        except SourceFailure as error:
            denied = denied or error.reason
        if config.paused_reason and not (job.payload.get('schema_probe') is True and schema_recheck(family, config.paused_reason)):
            denied = denied or 'source_paused'
        selection=[InstrumentKey(**item) for item in config.config.get('symbols',[])]
        if family=='tencent_web' and config.config.get('collection_mode')=='focused':
            from app.market.quote_focus import focused_selection
            selection=focused_selection(db,config,started)
        names={InstrumentKey(item.exchange,item.symbol,item.asset_type).identity:item.name for item in db.scalars(
            select(Instrument).where(Instrument.valid_to.is_(None)))} if family in FUYAO_FAMILIES | TDX_FAMILIES else {}
        if family=='fuyao_indices':selection=[InstrumentKey(exchange,symbol,'index') for exchange,symbol,_ in INDEXES]
        full_minute_context=None
        if family=='tdx_full_minute':
            from app.market.tdx_full_minute import validate_context as validate_full_minute
            try:
                full_minute_context=validate_full_minute(db,provider,config,job.payload)
                selection=[InstrumentKey(**item) for item in full_minute_context['symbols']]
            except SourceFailure as exc:denied=denied or exc.reason
        if family in TDX_FAMILIES and config.config.get('points_enabled') is not True:denied=denied or 'points_not_enabled'
        board_codes=config.config.get('board_codes',[])
        sector_codes=config.config.get('sector_codes',[])
        catalog=None
    market_scope=None
    @timing.permission
    def bounded_permission(permit, *, in_progress=False, paid_in_progress=False):
        nonlocal first_request_started
        # Bulk continuation uses the equivalent transactional qt guard, which
        # preserves the lease so completed HTTP requests get a terminal run.
        if not in_progress and not permit_realtime(job, expected_configuration=configuration):
            raise RealtimeWait()
        # Freeze one credential for the whole page set, but recheck its revision
        # before every physical request and again before evidence is persisted.
        with Session(engine()) as check:
            assert_source_binding(check, source, family, job.payload, frozen=bound)
            if family in FUYAO_FAMILIES | TDX_FAMILIES and family!='tdx_full_minute' and not (
                isinstance(job.payload.get('integration_context'),dict) and job.payload['integration_context'].get('probe') is True):
                from app.integrations.routing import collection_allowed
                if not collection_allowed(check,family):raise SourceFailure('source_route_not_selected')
        allowed=(timing.call('source_gate',permit,job,in_progress=paid_in_progress) if permit is reserve_tushare_batch_request
            else timing.call('source_gate',permit,job))
        from app.market.qt_budget import FAMILIES as QT_FAMILIES,reserve as reserve_qt
        if allowed and family in QT_FAMILIES:
            allowed=timing.call('qt_gate',reserve_qt,job,expected_configuration=configuration,in_progress=in_progress)
        if allowed and (permit is reserve_tushare_batch_request or family in KPL_FAMILIES | QT_FAMILIES):
            if not in_progress and not permit_realtime(job,expected_configuration=configuration):
                raise RealtimeWait()
            with Session(engine()) as check:
                assert_source_binding(check,source,family,job.payload,frozen=bound)
        if allowed and first_request_started is None:
            first_request_started=datetime.now(UTC)
        return allowed
    fuyao_permits=0
    def fuyao_permission():
        nonlocal fuyao_permits
        from app.market.budget import reserve_fuyao_request
        try:
            allowed=bounded_permission(lambda task:reserve_fuyao_request(task,expected_configuration=configuration,in_progress=fuyao_permits>0))
        except SourceFailure as error:
            error.request_count=0
            raise
        if not allowed:
            raise RealtimeWait()
        fuyao_permits+=1
        return True
    try:
        if denied:
            raise SourceFailure(denied)
        if not permit_realtime(job, expected_configuration=configuration):
            return
        from app.market.bulk_quote_queue import permit as permit_bulk_background
        if not permit_bulk_background(job,configuration):
            return
        if family=='tushare_dc_member':
            from app.market.sector_scope import member_sector_codes
            sector_codes=member_sector_codes(config.config)
        if family not in SOURCES:
            raise SourceFailure('capability_not_implemented')
        if family in ASHARE_FAMILIES and not bounded_permission(reserve_ashare_request):
            return
        if family in TUSHARE_FAMILIES:
            if not bound.credential:
                raise SourceFailure('credentials_missing')
        if family in AUCTION_TABLE_KINDS and not bounded_permission(reserve_auction_request):
            return
        requests=0 if family in FAMILY_INTERVALS or family in KPL_FAMILIES or family in {'tencent_web','tencent_market_rt','tencent_auction'} else 1
        if family in FUYAO_FAMILIES:
            requests=0
            from app.market import fuyao
            whole_snapshot=family=='fuyao_quotes' and config.config.get('collection_mode')=='full_market'
            if family=='fuyao_auction':
                from app.market.fuyao_auction import fetch as fetch_native_auction
                response,reference=fetch_native_auction(selection_book if book_trial else selection,started,
                    bound.credential,endpoint=bound.endpoint,before_request=fuyao_permission,offset=book_offset)
                reference.book_progress['universe_hash']=book_scope if book_trial else None
            elif whole_snapshot:
                from app.market import fuyao_market
                response,outputs=fuyao_market.fetch(bound.credential,endpoint=bound.endpoint,
                    before_request=fuyao_permission)
                requests=response.request_count
                reference=fuyao_market.parse(outputs,response.received_at)
            else:
                response,outputs=fuyao.fetch(family,selection,bound.credential,endpoint=bound.endpoint,clock=started,
                    before_request=fuyao_permission)
            requests=response.request_count
            if family=='fuyao_auction':
                pass
            elif whole_snapshot:
                warning_reason='snapshot_trade_date_unverified'
            elif family in {'fuyao_quotes','fuyao_indices'}:
                batch=fuyao.parse_quotes(outputs[0][2],selection,response.received_at,names)
            else:
                from app.market.reference import ReferenceBatch
                reference=ReferenceBatch()
                for key,statement,body in outputs:
                    parsed=fuyao.parse_reference(family,body,key,response.received_at,statement=statement)
                    reference.records.extend(parsed.records);reference.bars.extend(parsed.bars)
                    reference.expected+=parsed.expected;reference.rejected+=parsed.rejected
        elif family in TDX_FAMILIES:
            requests=0
            from app.market import tdx_data
            from datetime import date
            day=date.fromisoformat(full_minute_context['day']) if full_minute_context else started.astimezone(SHANGHAI).date()
            response=tdx_data.fetch(family,selection,bound.credential,endpoint=bound.endpoint,day=day,
                before_request=lambda:bounded_permission(lambda task:True))
            requests=response.request_count
            if family=='tdx_quotes':batch=tdx_data.parse_quotes(response.body,selection,response.received_at,names)
            elif family=='tdx_depth5':reference=tdx_data.parse_depth(response.body,selection,response.received_at)
            else:minutes=tdx_data.parse_minutes(response.body,selection,response.received_at,day)
        elif family in FAMILY_INTERVALS:
            if len(selection) != 1:
                raise SourceFailure('invalid_instrument_batch')
            url = minute_url(selection[0], FAMILY_INTERVALS[family])
            if not bounded_permission(reserve_sina_request):
                return
            requests = 1
            response = fetch_https(url)
            minutes = parse_sina_minutes(response.body, selection[0], FAMILY_INTERVALS[family],
                response.received_at, target_day(job.payload) or response.received_at.astimezone(SHANGHAI).date())
        elif family in TUSHARE_FAMILIES:
            requests = 0
            spec = TUSHARE_SPECS[family]
            if history:
                with Session(engine()) as db:
                    hp=db.get(Provider,source);hc=db.get(SourceSettings,source)
                    key,params=validate_history(db,job.payload,hp,hc,now(db))
                selection=[key]
            else:
                params = tushare_parameters(family, selection, started)
            dated_day = None
            if 'reference_context' in job.payload:
                from app.market.reference_backfill import validate_context as validate_reference
                with Session(engine()) as db:
                    dated_day=validate_reference(db,job.payload,provider,config,started)
            if 'dated_context' in job.payload:
                from app.market.terminal_dates import validate_dated_context
                with Session(engine()) as db:
                    dated_day = validate_dated_context(db,job.payload,provider,config,started)
            if spec.api in {'limit_list_d','limit_list_ths','top_list','top_inst','stk_auction'} or spec.mode in {'dated_table','dated_members'}:
                with Session(engine()) as db:
                    query_day=(dated_day or (pool_day(db,started,auction=True) if spec.api=='stk_auction' else
                        pool_day(db,started,preopen=True) if spec.api=='stk_premarket' else pool_day(db,started)))
                    params['trade_date']=query_day.strftime('%Y%m%d')
            if spec.mode == 'ths_members':
                from app.market.ths_members import MembershipPages, request_page
                pages = MembershipPages()
                def fetch_member_page():
                    nonlocal requests
                    if requests:time.sleep(2.05)
                    if not bounded_permission(reserve_tushare_batch_request,paid_in_progress=requests>0):raise RealtimeWait()
                    requests += 1
                    return request_tushare(spec.api,pages.params,spec.fields,bound.credential,bound.endpoint)
                while not pages.finished:
                    response = request_page(fetch_member_page)
                    pages.add(response)
                reference = pages.batch()
            elif spec.mode == 'dated_members':
                if not 1 <= len(sector_codes) <= 10:
                    raise SourceFailure('sector_codes_required')
                from app.market.reference import ReferenceBatch
                reference = ReferenceBatch(expected=len(sector_codes))
                hashes = []
                for sector_code in sector_codes:
                    # The persisted supplier permit requires two seconds
                    # between POSTs. A bounded group never spins/requeues on
                    # its own immediately preceding request.
                    if requests:time.sleep(2.05)
                    if not bounded_permission(reserve_tushare_batch_request,paid_in_progress=requests>0):return
                    requests += 1
                    response = request_tushare(spec.api,{**params,'ts_code':sector_code},spec.fields,bound.credential,bound.endpoint)
                    part = parse_tushare_reference(response.body,family,response.received_at)
                    for source_key,payload,instrument in part.records:
                        if any(row['trade_date'] != params['trade_date'] or row['ts_code'] != sector_code for row in payload.rows):
                            raise SourceFailure('backfill_day_not_returned' if reference_backfill else 'history_response_out_of_range')
                        reference.records.append((source_key+':'+sector_code,payload,instrument))
                    hashes.append(hashlib.sha256(response.body).hexdigest())
                # Hash the ordered response hashes for this bounded request set.
                response=type(response)(json.dumps(hashes).encode(),response.received_at,response.elapsed_ms,response.status)
            elif spec.api=='moneyflow_dc':
                from app.market.tushare import DCFundPagination, DC_FUND_PAGE_SIZE
                pagination=DCFundPagination(params['trade_date'])
                hashes=[]
                while not pagination.finished:
                    if requests:time.sleep(2.05)
                    if not bounded_permission(reserve_tushare_batch_request,paid_in_progress=requests>0):return
                    requests+=1
                    response=request_tushare(spec.api,{**params,'limit':DC_FUND_PAGE_SIZE,'offset':pagination.offset},
                        spec.fields,bound.credential,bound.endpoint)
                    pagination.add(response.body,response.received_at)
                    hashes.append(hashlib.sha256(response.body).hexdigest())
                # All pages must succeed and close before any evidence write.
                # This is one immutable dated table, so an old page cannot be
                # accidentally paired with a newer incomplete request set.
                reference=pagination.batch()
                response=type(response)(json.dumps(hashes).encode(),response.received_at,response.elapsed_ms,response.status)
            else:
                if not bounded_permission(reserve_tushare_request):
                    return
                requests = 1
                response = request_tushare(spec.api, params, spec.fields, bound.credential, bound.endpoint)
            if spec.mode in {'dated_members','ths_members'} or spec.api=='moneyflow_dc':
                pass
            elif spec.mode in {'market_snapshot','limit_snapshot'}:
                from app.market.tushare_market import parse_market
                reference=parse_market(response.body,response.received_at,limits=spec.mode=='limit_snapshot')
            elif spec.mode == 'fixed_indices':
                batch=parse_tushare_quotes(response.body,[InstrumentKey(e,s,'index') for e,s,_ in INDEXES],response.received_at)
            elif spec.mode == 'catalog':
                catalog = parse_tushare_catalog(response.body)
            elif spec.mode == 'calendar':
                from app.market.tushare import day as tushare_day
                days = parse_tushare_calendar(response.body,spec.exchange,tushare_day(params['start_date']),tushare_day(params['end_date']))
            elif spec.mode in {'snapshot','daily'}:
                if not history:
                    batch = parse_tushare_quotes(response.body,selection,response.received_at,daily=spec.mode=='daily')
                if spec.mode == 'daily':
                    reference = parse_tushare_reference(response.body,family,response.received_at,selection[0])
                    if history:
                        from app.market.tushare import table as history_table
                        _,raw_rows=history_table(response.body)
                        ctx=job.payload['daily_history']
                        if len(raw_rows)>ctx['page_limit'] or any(not ctx['start_day']<=str(bar.trade_day)<=ctx['end_day'] for _,bar in reference.bars):
                            raise SourceFailure('history_response_out_of_range')
                        if len(raw_rows)==ctx['page_limit']:
                            warning_reason='history_page_limit_reached'
                        elif not reference.bars:
                            warning_reason='history_window_empty'
            else:
                reference = parse_tushare_reference(response.body,family,response.received_at,selection[0] if selection else None)
                if spec.mode == 'dated_table' and any(row['trade_date'] != params['trade_date'] for _,payload,_ in reference.records for row in payload.rows):
                    raise SourceFailure('backfill_day_not_returned' if reference_backfill else 'history_response_out_of_range')
            if dated_day is not None and reference is not None and any(row.get('trade_date') != params['trade_date'] for _,payload,_ in reference.records for row in payload.rows):
                raise SourceFailure('backfill_day_not_returned' if reference_backfill else 'history_response_out_of_range')
        elif family in DIRECTION_FAMILIES:
            from app.market.direction_sources import fetch as fetch_direction, parse as parse_direction
            from app.market.service import permit_direction_probe
            if not permit_direction_probe(job, expected_configuration=configuration, frozen_binding=bound):
                return
            if family in {'ths_industry_funds','ths_concept_funds'}:
                from app.market.ths_funds import fetch as fetch_ths_funds
                from app.market.ths_funds_cooldown import reserve as reserve_ths
                response,reference=fetch_ths_funds(family,lambda:bounded_permission(
                    lambda task:reserve_ths(task,expected_configuration=configuration)))
                ths_page_diagnostic=getattr(reference,'ths_page_diagnostics',None)
                if reference.rejected:warning_reason='incomplete_page'
            elif family == 'direction_kpl_top':
                from app.market.kpl_atlas_budget import reserve as reserve_kpl, record_failure
                from app.market.kpl_top import parse as parse_top
                if not bounded_permission(lambda task: reserve_kpl(task, expected_configuration=configuration)):
                    return
                try:
                    requests = 1
                    response = fetch_direction('direction_kpl_strength')
                    reference = parse_top(response.body, response.received_at,
                        expected_day=response.received_at.astimezone(SHANGHAI).date())
                except SourceFailure as error:
                    record_failure(job, error)
                    raise
            elif family == 'direction_kpl_emotion':
                from app.market.kpl_atlas_budget import reserve as reserve_kpl, record_failure
                from app.market.kpl_emotion import parse as parse_emotion
                if not bounded_permission(lambda task: reserve_kpl(task, expected_configuration=configuration)):
                    return
                try:
                    requests = 1
                    response = fetch_direction(family)
                    expected_day = (response.received_at.astimezone(SHANGHAI).date()
                                    if job.payload.get('scheduled_realtime') is True else None)
                    reference = parse_emotion(response.body, response.received_at, expected_day=expected_day)
                except SourceFailure as error:
                    record_failure(job, error)
                    raise
            elif family == 'direction_kpl_strength':
                from app.market.kpl_atlas_source import fetch as fetch_atlas
                from app.market.kpl_atlas_budget import reserve as reserve_kpl, record_failure
                response, reference = fetch_atlas(include_leaders=False, include_counts=False, before_request=lambda: bounded_permission(
                    lambda task: reserve_kpl(task, expected_configuration=configuration)),
                    on_failure=lambda error:record_failure(job,error))
                if reference.rejected:
                    warning_reason=('incomplete_page' if reference.records[0][1].rows[0].get('coverage') == 'partial_pages'
                                    else 'leader_details_incomplete')
            else:
                response = fetch_direction(family)
                reference = parse_direction(family, response.body, response.received_at)
            requests = response.request_count
        elif family in EASTMONEY_FAMILIES:
            requests=0
            include_catalog=False
            if family=='eastmoney_funds' and job.payload.get('scheduled_eastmoney') is True:
                from app.market.industry_catalog import catalog_due
                with Session(engine()) as db:
                    include_catalog=catalog_due(db,source,started)
            response=fetch_rankings(family,lambda:bounded_permission(reserve_eastmoney_request),board_codes,
                **({'include_catalog':True} if include_catalog else {}),
                **({'full_boards':True} if family=='eastmoney_funds' else {}))
            requests=response.request_count
            reference=parse_rankings(response.body,response.received_at,family)
            catalog_result=json.loads(response.body)
            # A partial table failure is still a persisted recovery attempt.
            # Keep the valid independent tables and retry the pinned URLs.
            table_failure=next((item['reason'] for item in catalog_result.get('failed_tables', [])), None)
            if catalog_result.get('format')=='eastmoney-funds-pages-v1':
                table_failure=next((attempt['reason'] for table in catalog_result['tables']
                    for attempt in table['attempts'] if attempt['reason']),None)
                board_tables=[payload for _,payload,_ in reference.records if payload.dataset in {
                    'market.eastmoney.concept_funds','market.eastmoney.industry_funds','market.eastmoney.stock_funds'}]
                if not table_failure and (len(board_tables)!=3 or any(not payload.rows or
                        payload.rows[0].get('capture_complete')!='1' for payload in board_tables)):
                    table_failure='funds_pages_incomplete'
            eastmoney_tables_complete=not table_failure
            if table_failure:
                warning_reason=table_failure
            catalog_failure=catalog_result.get('catalog_failure') or catalog_result.get('catalog_deferred')
            if catalog_failure:
                # The optional catalog cannot hide a broken live funds table.
                # A shared 429 still takes priority and fences every request.
                if catalog_failure['reason']=='rate_limited' or not table_failure:
                    warning_reason=('rate_limited' if catalog_failure['reason']=='rate_limited'
                        else 'industry_catalog_'+catalog_failure['reason'])
                if catalog_failure['reason']=='rate_limited':
                    limited_wait=catalog_failure.get('retry_after') or config.interval_seconds
        elif family in MAIRUI_FAMILIES:
            requests=0
            if not bound.credential:
                raise SourceFailure('credentials_missing')
            with Session(engine()) as db:
                query_day=pool_day(db,started) if family=='mairui_pools' else None
            response=fetch_mairui(family,bound.credential,lambda:bounded_permission(reserve_mairui_request),query_day=query_day)
            requests=json.loads(response.body)['request_count']
            reference=parse_mairui(response.body,response.received_at,family)
        elif family=='tickflow_live':
            if not bound.credential:
                requests=0
                raise SourceFailure('credentials_missing')
            response=fetch_tickflow(selection,bound.credential)
            batch=parse_tickflow(response.body,selection,response.received_at)
        elif family=='ashare_public':
            response=fetch_https(ashare_url(selection))
            batch=parse_ashare(response.body,selection,response.received_at)
        elif family=='ashare_breadth':
            response=fetch_https(BREADTH_URL)
            reference=parse_breadth(response.body,response.received_at)
        elif family=='ashare_sector_heat':
            response=fetch_https(SECTOR_HEAT_URL)
            reference=parse_sector_heat(response.body,response.received_at)
        elif family=='tencent_market_rt':
            from app.market import tencent_market
            with Session(engine()) as db:
                market_scope=tencent_market.universe(db,started)
                market_day=pool_day(db,started)
            requests=0
            bulk_started=False
            def bulk_permission():
                nonlocal bulk_started
                with Session(engine()) as check:
                    if not tencent_market.scope_allowed(check,market_scope,datetime.now(UTC)):
                        raise SourceFailure('permission_missing')
                if not bounded_permission(lambda task:True,in_progress=bulk_started):raise RealtimeWait()
                bulk_started=True
            response,reference=tencent_market.fetch(market_scope,market_day,before_request=bulk_permission)
            requests=response.request_count
        elif family=='tencent_web':
            if not selection:
                # Interests may expire after enqueue. No HTTP or fake quote is
                # produced; the next ordinary scheduler checks the scope again.
                from app.worker import finish
                finish(job.id,job.lease_token)
                return
            if not bounded_permission(lambda task:True):
                return
            url=quote_url(selection)
            requests=1
            response=fetch_https(url)
            batch=parse_tencent_quotes(response.body,selection,response.received_at)
            if not batch.records:
                reason='instrument_unavailable'
        elif family=='szse_public':
            month=started.astimezone(SHANGHAI)
            response=fetch_https(f'https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month={month:%Y-%m}')
            days=parse_szse_calendar(response.body,month.year,month.month)
        elif family == 'baostock_daily':
            response = fetch_daily()
            raw_count = json.loads(response.body).get('request_count')
            requests = raw_count if isinstance(raw_count, int) and 0 <= raw_count <= 100 else 1
            daily = parse_daily(response.body, response.received_at)
            batch = daily
        elif family in {'baostock_history','baostock_fundamental','baostock_classification'}:
            response=fetch_references(family,selection)
            raw_count=json.loads(response.body).get('request_count')
            requests=raw_count if type(raw_count) is int and 0<=raw_count<=60 else 1
            reference=parse_references(response.body,response.received_at,selection)
            requests=reference.requests
        elif book_trial:
            from app.market.auction_book import fetch_books
            from app.market.auction_book_budget import reserve_book_request
            response,reference=fetch_books(selection_book,started,
                lambda:bounded_permission(lambda task:reserve_book_request(task,expected_configuration=configuration)),
                offset=book_offset)
            requests=response.request_count
        elif family in AUCTION_FAMILIES:
            if family=='tencent_auction':
                if not bounded_permission(lambda task:True):return
                requests=1
            response=fetch_auction(family,selection)
            reference=parse_auction(response.body,response.received_at,selection,family)
            requests=reference.requests
        elif family in AUCTION_TABLE_KINDS:
            kind=AUCTION_TABLE_KINDS[family]
            response=fetch_auction_table(kind)
            requests=response.request_count
            if kind=='current':
                auction_batches=[parse_auction_table(response.body,response.received_at,'current')]
            else:
                auction_batches=parse_auction_history(response.body,response.received_at)
        elif family=='cninfo_announcements':
            response,news=fetch_cninfo_batch(reserve=lambda: bounded_permission(
                lambda current_job: reserve_news_request(current_job,expected_configuration=configuration)))
            requests=news.requests
            news_coverage=news.coverage
            if news.partial:
                warning_reason='news_coverage_incomplete'
        elif family in PUBLIC_URLS:
            response=fetch_public(family)
            parsed=parse_public(family,response.body,response.received_at)
            if isinstance(parsed,NewsBatch):
                news=parsed
                news_coverage=news.coverage
            else:reference=parsed
        else:
            response=fetch_https(CNINFO_CATALOG_URL)
            catalog=parse_cninfo_catalog(response.body)
    except RealtimeWait:
        return
    except SourceFailure as error:
        reason=error.reason
        retry_after=error.retry_after
        if family == 'eastmoney_funds':
            from app.market.fund_time_diagnostic import safe_diagnostic
            funds_time_diagnostic = safe_diagnostic(error)
        if family in FUYAO_FAMILIES:
            from app.market.fuyao_transport import safe_diagnostic
            transport_diagnostic=safe_diagnostic(error)
        if family in DIRECTION_FAMILIES:
            requests=getattr(error,'request_count',requests)
            diagnostic=getattr(error,'direction_diagnostic',None)
            if isinstance(diagnostic,dict) and diagnostic.get('version')==1 and len(json.dumps(diagnostic))<=4096:
                direction_diagnostic=diagnostic
        if error.reason == 'rate_limited':
            # Header values are already bounded by the transport. Missing/zero
            # hints use a conservative local wait, never an invented recovery.
            from app.market.qt_budget import FAMILIES as QT_FAMILIES
            limited_wait=min(315360000 if family in KPL_FAMILIES | QT_FAMILIES else 86400, max(300 if family in ASHARE_FAMILIES or family in FAMILY_INTERVALS else 1,
                retry_after if isinstance(retry_after, int) and not isinstance(retry_after, bool) and retry_after > 0
                else 300 if family in ASHARE_FAMILIES or family in FAMILY_INTERVALS or family in QT_FAMILIES else config.interval_seconds))
            retry_after=limited_wait
        if family in {'ths_industry_funds','ths_concept_funds'}:
            ths_page_diagnostic=getattr(error,'ths_page_diagnostics',None)
            if type(error.retry_after) is int and error.retry_after>=0:
                retry_after=limited_wait=error.retry_after
            elif error.reason=='rate_limited':
                retry_after=limited_wait=300  # Local fallback only; no supplier hint.
            if requests:
                from app.market.ths_funds_cooldown import record_failure as record_ths_failure
                record_ths_failure(job,error)
        if family in TUSHARE_FAMILIES:
            if type(error.retry_after) is int and error.retry_after>=0:
                retry_after=limited_wait=error.retry_after
            if requests:
                from app.market.tushare_cooldown import record_failure as record_tushare_failure
                record_tushare_failure(job,error,fallback_seconds=limited_wait or config.interval_seconds)
        if book_trial:
            requests=getattr(error,'request_count',requests)
        if family in MAIRUI_FAMILIES or family in AUCTION_TABLE_KINDS or family in EASTMONEY_FAMILIES or family=='cninfo_announcements' or family in FUYAO_FAMILIES | TDX_FAMILIES:
            requests=getattr(error,'request_count',requests)
        if family in FUYAO_FAMILIES:
            if type(error.retry_after) is int and error.retry_after>=0:
                retry_after=limited_wait=error.retry_after
            elif error.reason=='rate_limited':
                retry_after=limited_wait=300  # Shared local fallback, not a slow data family's interval.
            if requests:
                from app.market.fuyao_cooldown import record_failure as record_fuyao_failure
                record_fuyao_failure(job,error)
        if family=='tencent_market_rt':requests=getattr(error,'request_count',requests)
        if family in {'tencent_web','tencent_market_rt','tencent_auction'} and requests:
            from app.market.qt_budget import record_failure as record_qt_failure
            record_qt_failure(job,error)
        if family=='cninfo_announcements':
            news_coverage=getattr(error,'coverage',None)
    else:
        retry_after=limited_wait or (news_coverage.get('retry_after') if news_coverage else None)
        if retry_after is not None:
            limited_wait=retry_after
    received=response.received_at if response else datetime.now(UTC)
    timing.storage_started(received,datetime.now(UTC))
    with Session(engine()) as db, db.begin():
        # No data or terminal job state is committed by an expired worker.
        current=db.scalar(select(Job).where(Job.id==job.id,Job.state=='running',Job.lease_token==job.lease_token,
                                            Job.lease_until>func.now()).with_for_update())
        if current is None:
            return
        if limited_wait is not None and family in ASHARE_FAMILIES:
            lock(db, 'source:anonymous-budget:ashare')
        if limited_wait is not None and family in FAMILY_INTERVALS:
            from app.market.budget import SINA_ACTION
            lock(db, SINA_ACTION)
        if history:
            lock(db,f'source:configuration:{source}')
        provider=db.get(Provider,source)
        config=db.get(SourceSettings,source)
        latest=configuration_fingerprint(provider,config)
        original_next_run_at=config.next_run_at
        if market_scope is not None and reason is None:
            from app.market.tencent_market import scope_allowed
            if not scope_allowed(db,market_scope,now(db)):reason='permission_missing'
        if history:
            latest=history_fingerprint(provider,config)
            if reason is None:
                try:
                    if current.payload!=job.payload:
                        raise SourceFailure('configuration_changed')
                    validate_history(db,current.payload,provider,config,now(db))
                except SourceFailure as exc:
                    reason=exc.reason
        if (configuration != latest or permission_reason(provider)) and reason is None:
            # Rights changed under the run: keep the fetched body out of the store.
            # A run that already failed keeps its own reason (permission_missing,
            # schema_changed, ...) instead of being relabelled as a config change.
            reason='configuration_changed'
        if reason is None:
            try:
                assert_source_binding(db, source, family, current.payload, frozen=bound)
                if book_trial:
                    from app.market.tdx_full_minute import universe
                    check_selection=(native_selection(db,config,now(db)) if family=='fuyao_auction' else universe(db,config))
                    if content_hash([key.identity for key in check_selection])!=book_scope:
                        raise SourceFailure('configuration_changed')
            except SourceFailure as error:
                reason = error.reason
        if reason is None and family in FAMILY_INTERVALS:
            try:
                if current.payload != job.payload:
                    raise SourceFailure('configuration_changed')
                validate_context(db,current.payload,provider,config,now(db))
            except SourceFailure as exc:
                reason=exc.reason
        if reason is None and family=='tdx_full_minute':
            from app.market.tdx_full_minute import validate_context as validate_full_minute
            try:validate_full_minute(db,provider,config,current.payload)
            except SourceFailure as exc:reason=exc.reason
        if reason is None and 'dated_context' in job.payload:
            from app.market.terminal_dates import validate_dated_context
            try:
                if current.payload != job.payload:
                    raise SourceFailure('configuration_changed')
                validate_dated_context(db,current.payload,provider,config,now(db))
            except SourceFailure as exc:
                reason=exc.reason
        if reason is None and 'reference_context' in job.payload:
            from app.market.reference_backfill import validate_context as validate_reference
            try:
                if current.payload != job.payload:
                    raise SourceFailure('configuration_changed')
                validate_reference(db,current.payload,provider,config,now(db))
            except SourceFailure as exc:
                reason=exc.reason
        accepted=duplicates=None
        if reason is None and minutes is not None:
            accepted,duplicates=store_minute_batch(db,provider,config,minutes,response.received_at,backfill=backfill)
            duplicates += minutes.duplicates
        elif reason is None and auction_batches is not None:
            accepted,duplicates=store_auction_batches(db,provider,config,auction_batches,response.received_at)
        elif reason is None and batch is not None:
            accepted,duplicates=store_quote_batch(db,provider,config,batch)
            if reference is not None:
                added, repeated = store_reference_batch(db,provider,config,reference,response.received_at)
                accepted += added
                duplicates += repeated
            if daily is not None:
                added, repeated = store_calendar_batch(db, provider, config, daily.days, response.received_at, exchanges=('SSE', 'SZSE'))
                accepted += added
                duplicates += repeated
        elif reason is None and days is not None:
            exchanges = (TUSHARE_SPECS[family].exchange,) if family in TUSHARE_FAMILIES else ('SZSE',)
            accepted,duplicates=store_calendar_batch(db,provider,config,days,response.received_at,exchanges=exchanges)
        elif reason is None and catalog is not None:
            accepted,duplicates=store_catalog_batch(db,provider,config,catalog,response.received_at,response_body=response.body)
        elif reason is None and reference is not None:
            accepted,duplicates=store_reference_batch(db,provider,config,reference,response.received_at)
        elif reason is None and news is not None:
            accepted,duplicates=store_news_batch(db,provider,config,news,response.received_at)
            duplicates+=getattr(news,'duplicates',0)
        capability='+'.join(SOURCES[family].capabilities)
        if family in FAMILY_INTERVALS or family in {'tdx_minute','tdx_full_minute'}:
            expected=minutes.expected if minutes is not None else None
        elif family=='szse_public':
            expected=calendar.monthrange(started.astimezone(SHANGHAI).year,started.astimezone(SHANGHAI).month)[1]
        elif family=='baostock_daily':
            expected=daily.expected + 2 * len(daily.days) if daily else None
        elif family in {'cninfo','tushare_stock_basic'}:
            expected=catalog.expected + len(INDEXES) if catalog else None
        elif family in TUSHARE_FAMILIES and days is not None:
            expected=len(days)
        elif family == 'tushare_rt_indices':
            expected=len(INDEXES)
        elif family in MAIRUI_FAMILIES:
            expected=2 if family=='mairui_funds' else 3
        elif family in AUCTION_TABLE_KINDS:
            # The history table answers with several days; both counters are sums
            # over every batch in that one response.
            expected=sum(item.expected for item in auction_batches or [])
        elif SOURCES[family].kind=='news':
            expected=news.expected if news is not None else None
        elif reference is not None or news is not None:
            expected=(reference or news).expected
        else:
            expected=len(selection)
        rejected=((sum(item.rejected for item in auction_batches) if auction_batches else None) if family in AUCTION_TABLE_KINDS
                  else len(batch.rejected) if batch else len(catalog.rejected) if catalog else (reference or news).rejected if reference is not None or news is not None else 0 if days else None)
        status=('unavailable' if reason in {'permission_missing','source_disabled','configuration_changed','instrument_unavailable','capability_not_implemented'}
                else 'empty' if reason == 'daily_data_not_published'
                else 'error' if reason
                else 'partial' if (batch and (batch.rejected or any((q.quote_time is None and getattr(q,'trade_day',None) is None) or q.price is None for q in batch.records)))
                or (catalog and catalog.rejected) or ((reference or news) and (reference or news).rejected)
                or (news and getattr(news,'partial',False))
                else 'empty' if accepted==0 and duplicates==0 or (reference is not None and not reference.bars and not any(p.rows for _,p,_ in reference.records)) else 'complete')
        if family in FAMILY_INTERVALS or family in {'tdx_minute','tdx_full_minute'}:
            rejected = len(minutes.rejected) if minutes is not None else None
            if reason is None:
                status = 'partial' if rejected else 'complete' if accepted or duplicates else 'empty'
                if backfill:
                    warning_reason=('backfill_conflict_existing_observation' if any(v=='backfill_conflict_existing_observation' for v in minutes.rejected.values())
                        else 'backfill_rows_rejected' if rejected else 'backfill_day_not_returned' if not minutes.records else None)
        if family in AUCTION_TABLE_KINDS and reason is None:
            # Replaying an identical table is a duplicate batch, not an empty run.
            # Dropped rows are partial, and nothing stored is never "complete".
            status='partial' if rejected else 'complete' if (accepted or duplicates) else 'empty'
        if history and reason is None:
            status='partial' if reference.rejected or warning_reason=='history_page_limit_reached' else 'complete' if reference.bars else 'empty'
            expected=1+len(reference.bars)+reference.rejected
            rejected=reference.rejected
        if family=='tdx_depth5' and reason is None and reference and any(
            any(row.get('quote_time') is None for row in payload.rows) for _,payload,_ in reference.records):
            status='partial';warning_reason='snapshot_trade_date_unverified'
        if family=='fuyao_quotes' and reason is None and config.config.get('collection_mode')=='full_market':
            status='partial';warning_reason='snapshot_trade_date_unverified'
        if family in {'fuyao_quotes','fuyao_indices','tdx_quotes'} and reason is None and batch and any(
            quote.quote_time is None for quote in batch.records):
            warning_reason='quote_time_unknown'
        from app.market.realtime_schedule import FAMILIES as REALTIME_FAMILIES, retry_seconds, MAX_LIVE_ATTEMPTS, align_realtime
        from app.market.auction_schedule import CURRENT
        live_retry = family in REALTIME_FAMILIES and not (backfill or history or reference_backfill or 'dated_context' in job.payload)
        transient = reason in {'timeout', 'transport_error', 'upstream_unavailable', 'rate_limited', 'local_request_wait'}
        if family=='tencent_market_rt' and reason in {'source_budget_wait','intraday_window_wait','local_clock_unsettled'}:
            transient=True
        if book_trial and reason=='auction_book_unavailable':
            transient=True
        if family=='fuyao_auction' and reason in {'auction_data_not_ready','auction_limits_unavailable'}:
            transient=True;status='empty'
        if family == 'direction_kpl_emotion' and reason == 'source_day_mismatch':
            transient = True
        retry_delay = None
        ranking_failure = (reason or warning_reason) if family in EASTMONEY_FAMILIES | {'fuyao_quotes','fuyao_indices'} else None
        if live_retry and (transient or (family == CURRENT and reason == 'schema_changed')
                           or ranking_failure in {'timeout','transport_error','upstream_unavailable','upstream_redirect','schema_changed'}):
            # Actual persisted failures, rather than process memory or job IDs,
            # preserve 5/15/30/60 backoff after restart or a new task. A real
            # successful/empty response resets the streak.
            previous_reasons = db.scalars(select(DataFetchRun.reason).where(
                DataFetchRun.source_id == source, live_run_condition(), DataFetchRun.received_at <= received
            ).order_by(DataFetchRun.received_at.desc(), DataFetchRun.id.desc()).limit(3)).all()
            failures = 1
            for prior_reason in previous_reasons:
                if prior_reason not in {'timeout', 'transport_error', 'upstream_unavailable', 'rate_limited',
                                        'schema_changed', 'upstream_redirect', 'source_day_mismatch'}:
                    break
                failures += 1
            retry_delay = retry_seconds(failures)
            if family in EASTMONEY_FAMILIES:
                # An unavailable public feed must not be polled faster than
                # its healthy configured cadence; real Retry-After still wins.
                retry_delay = max(retry_delay, config.interval_seconds)
        from app.market.collection_cadence import successful_anchor, successful_next
        sampling_start=successful_anchor(family,job.payload,first_request_started,received,status,requests)
        if sampling_start is not None:
            current.result_ref={**(current.result_ref or {}), 'sample_request_started_at':sampling_start.isoformat()}
        run=DataFetchRun(source_id=source,capability=capability,job_id=job.id,started_at=sampling_start or started,received_at=received,status=status,reason=reason or warning_reason,
            expected=expected,accepted=accepted,rejected=rejected,duplicates=duplicates,
            elapsed_ms=response.elapsed_ms if response else None,request_count=requests,cost_cny=None,
            retry_not_before=received+timedelta(seconds=limited_wait) if limited_wait is not None and
                (family not in KPL_FAMILIES | TUSHARE_FAMILIES | FUYAO_FAMILIES | {'tencent_web','tencent_market_rt','tencent_auction','ths_industry_funds','ths_concept_funds'} or limited_wait<=86400) else None,
            body_hash=hashlib.sha256(response.body).hexdigest() if response else None)
        db.add(run)
        if ths_page_diagnostic is not None:
            from app.market.ths_page_diagnostic import record as record_ths_page_diagnostic
            db.flush()
            record_ths_page_diagnostic(current,run,family,ths_page_diagnostic)
        if funds_time_diagnostic is not None:
            db.flush()
            current.result_ref={**(current.result_ref or {}), 'funds_time_rejection':
                dict(funds_time_diagnostic, run_id=str(run.id), body_hash=run.body_hash)}
        if direction_diagnostic is not None:
            current.result_ref={'direction_diagnostic':direction_diagnostic}
        if transport_diagnostic is not None:
            current.result_ref={**(current.result_ref or {}),'transport_diagnostic':transport_diagnostic}
        if family=='tdx_full_minute' and full_minute_context:
            from app.market.tdx_full_minute import progress
            current.result_ref=progress(full_minute_context,minutes if reason is None else None,db=db,current_job_id=current.id)
        if news_coverage:
            from app.news_center.service import store_source_coverage
            store_source_coverage(db,run,news_coverage)
        if reason is None and (family not in EASTMONEY_FAMILIES or eastmoney_tables_complete) and job.payload.get('schema_probe') is True and schema_recheck(family, config.paused_reason):
            config.paused_reason = None
            db.add(AuditEvent(actor_id=current.owner_id, action='source.schema_recovered', resource_type='provider',
                resource_id=provider.id, changed_field_names=['paused_reason'], created_at=now(db)))
        config.next_run_at=next_collection(family, received, config.interval_seconds,
            schema_probe=schema_recheck(family, reason) or schema_recheck(family, config.paused_reason),
            unpublished=reason == 'daily_data_not_published',probe_diagnostic=direction_diagnostic)
        sampling_next=successful_next(family,job.payload,sampling_start,received,status,requests,config.interval_seconds)
        if sampling_next is not None:
            config.next_run_at=sampling_next
        if book_trial and reason is None:
            current.result_ref=reference.book_progress
            config.next_run_at=align_realtime(family,max(received+timedelta(seconds=1),started+timedelta(seconds=config.interval_seconds)))
        if family in FAMILY_INTERVALS:
            config.next_run_at = next_minute(family, received, config.interval_seconds)
        if retry_delay is not None:
            config.next_run_at = align_realtime(family, received + timedelta(seconds=retry_delay))
        if retry_after:
            deadline = received + timedelta(seconds=retry_after)
            config.next_run_at=max(config.next_run_at, align_realtime(family, deadline) if live_retry else deadline)
        if reason in {'permission_missing','tdx_data_permission_missing','tdx_data_key_required',
                      'schema_changed','decoder_changed','endpoint_not_allowed','response_too_large','upstream_redirect','unsafe_upstream_response'}:
            config.paused_reason=reason
        if reason=='retry_after_out_of_range' and family in {'tencent_web','tencent_market_rt','tencent_auction'}:
            for other in db.scalars(select(SourceSettings).join(Provider,Provider.id==SourceSettings.source_id)
                    .where(Provider.family.in_({'tencent_web','tencent_market_rt','tencent_auction'}))):
                other.paused_reason=reason
        if family in TUSHARE_FAMILIES | FUYAO_FAMILIES | {'ths_industry_funds','ths_concept_funds'} and reason=='local_request_wait':
            current.attempts=max(0,current.attempts-1)
        retry=transient and current.attempts < (MAX_LIVE_ATTEMPTS if live_retry else 3)
        current.state='queued' if retry else 'failed' if reason else 'succeeded'
        current.error_code=reason
        current.completed_at=None if retry else now(db)
        if retry:
            current.available_at=(received+timedelta(seconds=max(config.interval_seconds,retry_after or 0)) if backfill or history or reference_backfill or 'dated_context' in job.payload else config.next_run_at)
        elif transient and not live_retry:
            config.next_run_at=received+timedelta(seconds=max(300,config.interval_seconds,retry_after or 0))
        if backfill:
            config.next_run_at=max(original_next_run_at,config.next_run_at)
        if history or 'dated_context' in job.payload or reference_backfill:
            config.next_run_at=original_next_run_at
        current.lease_token=current.lease_until=None
        db.flush()
        run_id = run.id
        committed_source_id = source
        committed_job_id = current.id
    timing.committed()
    # Health checks take their own locks only after market evidence and the
    # job outcome are committed. A missed callback is repaired by reconciliation.
    from app.market.resilience import record_committed_fetch
    health_returned=False
    try:
        record_committed_fetch(committed_source_id, run_id, committed_job_id)
        health_returned=True
    finally:
        timing.finish(run_id,status,requests,health_returned=health_returned)
