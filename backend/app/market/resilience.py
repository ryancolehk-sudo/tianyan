"""Local data checks and durable next actions. This module never contacts a vendor.

The existing collector owns HTTP, shared permits, leases and ingestion. Recovery
only advances an already-authorized schedule; a read cannot enqueue work.
"""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, defer
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db import engine
from app.evidence.common import content_hash, lock, normalized, now
from app.evidence.models import EvidenceVersion
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SHANGHAI
from app.market.resilience_models import DataHealth
from app.models import AuditEvent, Instrument, Job, Provider, User

ACTION = 'market.resilience.reconcile'
HISTORY_KEYS = {'minute_context', 'daily_history', 'dated_context', 'reference_context',
                'full_minute_context', 'review_context', 'backfill_context'}
AUTH_REASONS = {'credentials_missing', 'credentials_invalid', 'credentials_rejected',
    'permission_missing', 'permission_denied', 'authentication_failed', 'unauthorized',
    'forbidden', 'points_not_enabled', 'configuration_changed',
    'tdx_data_permission_missing', 'tdx_data_key_required'}
SCHEMA_REASONS = {'schema_changed', 'decoder_changed', 'endpoint_not_allowed',
    'response_too_large', 'upstream_redirect', 'unsafe_upstream_response'}
QUOTE_FAMILIES = {'fuyao_quotes', 'fuyao_indices', 'tdx_quotes', 'tencent_web',
    'tickflow_live', 'ashare_public', 'tushare_rt_k', 'tushare_rt_indices', 'tushare_market_rt','tencent_market_rt'}


@dataclass(frozen=True)
class Quality:
    valid: bool
    reason: str | None = None
    received_at: datetime | None = None
    source_time: datetime | None = None
    missing_count: int | None = None


def stamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.utcoffset() is not None else None
    except ValueError:
        return None


def capabilities(provider, config):
    family = provider.family
    if family in QUOTE_FAMILIES:
        if family in {'fuyao_indices', 'tushare_rt_indices'}:
            return ['index_quote']
        if family in {'tushare_market_rt','tencent_market_rt'} or family=='fuyao_quotes' and config.config.get('collection_mode')=='full_market':
            return ['stock_quote']
        assets = {item.get('asset_type') for item in config.config.get('symbols', [])}
        return [key for asset, key in [('stock', 'stock_quote'), ('index', 'index_quote')] if asset in assets]
    if family.startswith('sina_minute_') or family == 'tdx_minute':
        return ['stock_minute_' + ('1m' if family == 'tdx_minute' else family.removeprefix('sina_minute_'))]
    if family == 'tdx_depth5':
        return ['depth5']
    # Source-specific tables deliberately cannot substitute for one another.
    return ['source:' + family]


def source_scope(provider, config, capability):
    asset = 'index' if capability == 'index_quote' else 'stock'
    symbols = sorted({(item.get('exchange', ''), item.get('symbol', ''), item.get('asset_type', ''))
        for item in config.config.get('symbols', []) if item.get('asset_type') == asset})
    if provider.family in {'tushare_rt_indices', 'fuyao_indices'}:
        from app.market.registry import INDEXES
        symbols = sorted((exchange, symbol, 'index') for exchange, symbol, _ in INDEXES)
    return dict(universe='whole_market' if (provider.family in {'tushare_market_rt','tencent_market_rt'} or
        provider.family=='fuyao_quotes' and config.config.get('collection_mode')=='full_market') else
        'configured_symbols' if symbols else 'source_universe', asset_type=asset,
        symbols=[dict(exchange=e, symbol=s, asset_type=a) for e, s, a in symbols])


def covers(candidate, required):
    if candidate['asset_type'] != required['asset_type']:
        return False
    if required['universe'] == 'whole_market':
        return candidate['universe'] == 'whole_market'
    if required['universe'] != 'configured_symbols':
        return False
    if candidate['universe'] == 'whole_market':
        return required['asset_type'] == 'stock'
    keys = lambda scope: {(item['exchange'], item['symbol'], item['asset_type']) for item in scope['symbols']}
    return bool(keys(required)) and keys(required) <= keys(candidate)


def latest_run(db, source_id, clock):
    from app.market.service import live_run_condition
    return db.scalar(select(DataFetchRun).where(DataFetchRun.source_id == source_id,
        DataFetchRun.received_at <= clock, live_run_condition()).order_by(
            DataFetchRun.received_at.desc(), DataFetchRun.id.desc()).limit(1))


def binding_reason(db,provider,run):
    from app.integrations.credentials import resolve_source_credentials
    bound=resolve_source_credentials(db,provider.id,provider.family)
    if bound.reason:
        return bound.reason
    if bound.connection_id is None:
        return None
    job=db.get(Job,run.job_id) if run and run.job_id else None
    context=(job.payload.get('integration_context') or {}) if job else {}
    if (context.get('connection_id')==str(bound.connection_id) and
        context.get('revision')==bound.revision and context.get('source_id')==str(provider.id)):
        return None
    # An old success under a replaced key is not evidence for the new binding.
    return 'source_verification_outdated'


def saved_quality(db, provider, config, capability, day, clock, run=None, required_scope=None):
    """Validate saved date, fields and actual scope, rather than HTTP status."""
    run = run or latest_run(db, provider.id, clock)
    if not run or run.started_at < config.configured_at:
        return Quality(False, 'source_not_verified')
    if run.status not in {'complete', 'partial'}:
        # A failed poll does not erase still-fresh saved data. Preserve its
        # original clocks while the failed poll independently drives recovery.
        from app.market.service import live_run_condition
        previous = db.scalar(select(DataFetchRun).where(DataFetchRun.source_id == provider.id,
            DataFetchRun.status.in_(['complete', 'partial']), DataFetchRun.received_at <= clock,
            DataFetchRun.started_at >= config.configured_at, live_run_condition())
            .order_by(DataFetchRun.received_at.desc()).limit(1))
        if previous is None:
            return Quality(False, run.reason or 'empty_response')
        run = previous
    freshness = max(120, config.interval_seconds * 2)
    if provider.family == 'duanxianxia_auction':
        from app.market.auction_schedule import collection_spacing
        freshness = max(15, collection_spacing(provider.family, clock, config.interval_seconds) * 2)
    from app.market.session import quote_fresh_at
    freshness_clock = quote_fresh_at(clock)
    if provider.family != 'tushare_stk_auction' and (freshness_clock - run.received_at).total_seconds() > freshness:
        return Quality(False, 'source_stale')
    query = select(EvidenceVersion).where(EvidenceVersion.source_id == provider.id,
        EvidenceVersion.visibility == 'shared', EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.received_at <= clock, EvidenceVersion.provenance == 'source_reported')
    if capability.startswith('stock_minute_'):
        return minute_quality(db, query, provider, config, capability, day, clock,
            run, required_scope=required_scope)
    if capability == 'depth5':
        return depth_quality(db,query,provider,config,day,clock,required_scope=required_scope)
    if provider.family=='fuyao_quotes' and config.config.get('collection_mode')=='full_market':
        from app.market import read_facets
        from app.market.fuyao_market import DATASET, snapshot_predicate
        from app.market.session import market_session
        session=market_session(db,clock)
        if session.is_trading_day is not True or session.trade_day!=day:
            return Quality(False,'snapshot_trade_date_unverified')
        references=read_facets.with_facets(query).where(EvidenceVersion.kind=='reference',
            read_facets.value('dataset')==DATASET,
            read_facets.observed_day()==day.isoformat())
        evidence=db.scalar(references
            .order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(1))
        if evidence is None:
            return Quality(False,'current_day_not_observed')
        rows=evidence.payload.get('rows',[])
        current=snapshot_predicate(evidence.received_at,day,clock,freshness)
        valid={item.get('ts_code') for item in rows if item.get('close') is not None
            and current(item)}
        scope=required_scope or source_scope(provider,config,capability)
        if scope['universe']=='configured_symbols':
            suffix={'SSE':'SH','SZSE':'SZ','BSE':'BJ'}
            wanted={item['symbol']+'.'+suffix[item['exchange']] for item in scope['symbols']}
            missing=len(wanted-valid)
        else:
            # Validate returned membership independently of changing prices.
            from app.market.row_count_order import largest_reference
            previous=largest_reference(db,references.where(
                EvidenceVersion.received_at<evidence.received_at))
            missing=len({item.get('ts_code') for item in previous.payload.get('rows',[])}-
                {item.get('ts_code') for item in rows}) if previous else 0
        return Quality(bool(valid) and not missing,'market_scope_changed' if missing else
            None if valid else 'snapshot_missing_or_stale',evidence.received_at if valid else None,
            None,missing)
    if provider.family=='tencent_market_rt':
        from app.market.tencent_market import DATASET,saved_scope_allowed,manifest_for
        from app.market import read_facets
        evidence=db.scalar(read_facets.with_facets(query).where(EvidenceVersion.kind=='reference',
            read_facets.value('dataset')==DATASET,read_facets.observed_day()==day.isoformat())
            .order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(1))
        if evidence is None or not saved_scope_allowed(db,evidence.payload,clock,received_at=evidence.received_at):
            return Quality(False,'catalog_scope_unavailable')
        valid={row['ts_code'] for row in evidence.payload.get('rows',[]) if row.get('close') is not None
            and (at:=stamp(row.get('trade_time'))) is not None and at.astimezone(SHANGHAI).date()==day
            and at<=clock and (freshness_clock-at).total_seconds()<=freshness}
        scope=required_scope or source_scope(provider,config,capability)
        if scope['universe']=='configured_symbols':
            suffix={'SSE':'SH','SZSE':'SZ','BSE':'BJ'}
            wanted={item['symbol']+'.'+suffix[item['exchange']] for item in scope['symbols']}
            missing=len(wanted-valid)
            return Quality(not missing,'quote_missing_or_stale' if missing else None,evidence.received_at,None,missing)
        coverage=manifest_for(db,evidence.payload['rows'][0]['capture_id'],clock,received_at=evidence.received_at)
        missing=max(0,int(coverage['expected'])-len(valid)) if coverage else len(evidence.payload['rows'])
        return Quality(bool(valid),'quote_coverage_partial' if missing else None,evidence.received_at,None,missing)
    if provider.family in QUOTE_FAMILIES and provider.family != 'tushare_market_rt':
        scope = required_scope or source_scope(provider, config, capability)
        required = {(item['exchange'], item['symbol'], item['asset_type']) for item in scope['symbols']}
        if not required:
            return Quality(False, 'scope_unknown')
        rows = db.execute(query.join(Instrument, Instrument.id == EvidenceVersion.instrument_id)
            .where(EvidenceVersion.kind == 'quote', Instrument.asset_type == scope['asset_type'])
            .order_by(EvidenceVersion.instrument_id, EvidenceVersion.event_at.desc().nullslast(),
                      EvidenceVersion.received_at.desc()).distinct(EvidenceVersion.instrument_id)).scalars().all()
        identities = {item.id: (item.exchange, item.symbol, item.asset_type) for item in db.scalars(
            select(Instrument).where(Instrument.id.in_([row.instrument_id for row in rows])))}
        valid, observed = set(), []
        for row in rows:
            at = stamp(row.payload.get('quote_time'))
            key = identities.get(row.instrument_id)
            if (key in required and row.payload.get('price') is not None and at is not None
                and at.astimezone(SHANGHAI).date() == day and at <= clock
                and (freshness_clock-at).total_seconds() <= freshness):
                valid.add(key)
                observed.append((at, row.received_at))
            elif provider.family in {'fuyao_quotes','fuyao_indices'} and key in required and row.payload.get('price') is not None:
                from app.market.fuyao_market import current_snapshot
                from app.market.session import market_session
                session=market_session(db,clock)
                if session.is_trading_day is True and session.trade_day==day and current_snapshot(dict(snapshot_time=row.payload.get('source_snapshot_time')),
                        row.received_at,day,clock,freshness):
                    valid.add(key)
                    observed.append((None,row.received_at))
        missing = len(required - valid)
        return Quality(not missing, 'quote_missing_or_stale' if missing else None,
            max((item[1] for item in observed), default=None),
            min((item[0] for item in observed if item[0] is not None), default=None), missing)
    if provider.family == 'eastmoney_funds':
        from app.market import read_facets
        from app.market.intraday_funds import DATASETS
        dataset = read_facets.value('dataset')
        query = read_facets.with_facets(query).where(dataset.in_(DATASETS))
        rows = list(db.scalars(query.where(EvidenceVersion.kind == 'reference',
            read_facets.observed_day() == day.isoformat())
            .order_by(dataset,
                EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc())
            .distinct(dataset)))
        valid, observed = set(), []
        for row in rows:
            native = [stamp(value.get('source_time')) for value in row.payload.get('rows', [])
                if isinstance(value, dict) and value.get('net_inflow_cny') is not None]
            native = [at for at in native if at is not None and at <= clock
                and at.astimezone(SHANGHAI).date() == day]
            if native and (freshness_clock-max(native)).total_seconds() <= freshness:
                valid.add(row.payload['dataset'])
                observed.append((max(native), row.received_at))
        missing = len(set(DATASETS)-valid)
        if run.status == 'partial' and run.rejected:
            missing = max(missing, run.rejected)
        return Quality(not missing, 'coverage_partial' if missing else None,
            max((item[1] for item in observed), default=None),
            min((item[0] for item in observed), default=None), missing)
    from app.market import read_facets
    # Match the date used below without decoding every historical bulk table.
    # A known observed day takes precedence; legacy trade_day-only records and
    # missing/stale facets still read their original evidence fields.
    observed_day=func.coalesce(read_facets.observed_day(),EvidenceVersion.payload['trade_day'].astext)
    rows = list(db.scalars(read_facets.with_facets(query).where(EvidenceVersion.kind == 'reference',
        observed_day==day.isoformat()).order_by(
        EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(1)))
    dated = []
    for row in rows:
        payload = row.payload
        observed_day = payload.get('observed_day') or payload.get('trade_day')
        if observed_day == day.isoformat():
            dated.append(row)
    if not dated:
        return Quality(False, 'current_day_not_observed')
    evidence = dated[0]
    payload_rows = evidence.payload.get('rows', [])
    if not payload_rows:
        return Quality(False, 'empty_response')
    if provider.family == 'tushare_market_rt':
        from app.market.tushare import clock as source_clock
        from app.market.protocol import SourceFailure
        clocks, observed = [], set()
        checked = {}

        def current(value):
            if isinstance(value, str) and value in checked:
                return checked[value]
            try:
                at = source_clock(value, evidence.received_at)
            except (SourceFailure, ValueError, TypeError):
                at = None
            if at and not (at.astimezone(SHANGHAI).date() == day and at <= clock
                and (freshness_clock-at).total_seconds() <= freshness):
                at = None
            if isinstance(value, str):
                checked[value] = at
            return at

        for item in payload_rows:
            at = current(item.get('trade_time'))
            # Suspended old prices do not make the complete batch unusable.
            if at and item.get('close') is not None:
                clocks.append(at)
                observed.add(item.get('ts_code'))
        if required_scope and required_scope['universe'] == 'configured_symbols':
            suffix = {'SSE':'SH', 'SZSE':'SZ', 'BSE':'BJ'}
            wanted = {item['symbol']+'.'+suffix[item['exchange']] for item in required_scope['symbols']}
            missing = len(wanted - observed)
            return Quality(not missing, 'quote_missing_or_stale' if missing else None,
                run.received_at if clocks else None, min(clocks) if clocks else None, missing)
        # A fresh last price cannot hide a suddenly truncated market batch.
        # Compare actual same-day membership, not an invented market total;
        # suspended rows may retain old prices and still count as returned.
        from app.market.row_count_order import largest_reference
        previous = largest_reference(db,read_facets.with_facets(query).where(EvidenceVersion.source_id==provider.id,
            EvidenceVersion.kind=='reference',read_facets.value('dataset')=='tushare.rt_market',
            read_facets.observed_day()==day.isoformat(),
            EvidenceVersion.received_at<evidence.received_at))
        if previous:
            old_codes={item.get('ts_code') for item in previous.payload.get('rows',[])}
            returned={item.get('ts_code') for item in payload_rows}
            missing=len(old_codes-returned)
            if missing:
                return Quality(False,'market_scope_changed',run.received_at,
                    max(clocks) if clocks else None,missing)
        return Quality(bool(clocks), None if clocks else 'quote_missing_or_stale',
            run.received_at if clocks else None, max(clocks) if clocks else None)
    native = [stamp(item.get('source_time')) for item in payload_rows if isinstance(item, dict)]
    native = [at for at in native if at is not None and at <= clock and at.astimezone(SHANGHAI).date() == day]
    native_required = provider.family in {'eastmoney_funds','eastmoney_boards','mairui_funds'}
    if native_required and not native:
        return Quality(False,'source_time_unverified')
    if native and max(native) < freshness_clock - timedelta(seconds=freshness):
        return Quality(False, 'source_stale')
    if run.status == 'partial' and run.rejected:
        return Quality(False, 'coverage_partial', missing_count=run.rejected)
    # Tables without a supplier clock retain only their real receipt clock.
    return Quality(True, received_at=run.received_at,
        source_time=max(native) if native else None, missing_count=0)


def minute_quality(db, query, provider, config, capability, day, clock, run, *, required_scope=None):
    from app.market.session import quote_fresh_at
    from app.market.recovery_models import RecoveryCoverage
    interval = capability.removeprefix('stock_minute_')
    scope = required_scope or source_scope(provider, config, capability)
    required = {(item['exchange'], item['symbol'], item['asset_type']) for item in scope['symbols']}
    if not required:
        return Quality(False, 'scope_unknown')
    rows = list(db.scalars(query.where(EvidenceVersion.kind == 'bar',
        EvidenceVersion.payload['interval'].astext == interval,
        EvidenceVersion.payload['trade_day'].astext == day.isoformat())
        .order_by(EvidenceVersion.instrument_id, EvidenceVersion.payload['source_clock_at'].astext.desc(),
            EvidenceVersion.received_at.desc()).distinct(EvidenceVersion.instrument_id)))
    identities = {item.id:(item.exchange,item.symbol,item.asset_type) for item in db.scalars(
        select(Instrument).where(Instrument.id.in_([row.instrument_id for row in rows])))}
    freshness = max(config.interval_seconds*2, int(interval.removesuffix('m'))*120)
    valid, observed = set(), []
    for row in rows:
        at = stamp(row.payload.get('source_clock_at'))
        identity = identities.get(row.instrument_id)
        if (identity in required and at is not None and at <= clock and
            at.astimezone(SHANGHAI).date() == day and row.payload.get('close') is not None and
            (quote_fresh_at(clock)-at).total_seconds() <= freshness):
            valid.add(identity)
            observed.append((at,row.received_at))
    missing = len(required-valid)
    if missing:
        return Quality(False,'minute_scope_missing_or_stale',missing_count=missing)
    coverage = list(db.scalars(select(RecoveryCoverage).where(RecoveryCoverage.source_id == provider.id,
        RecoveryCoverage.owner_id == config.owner_id, RecoveryCoverage.trade_day == day,
        RecoveryCoverage.dataset == 'minute.bars', RecoveryCoverage.interval == interval,
        RecoveryCoverage.state.in_(['missing','partial','queued','running','waiting','blocked','unsupported']))))
    holes = sum(item.get('count',0) for entry in coverage for item in entry.missing_ranges)
    # Native source_clock_at remains an observation, never a claimed bar end.
    return Quality(not holes,'minute_coverage_pending' if holes else None,
        max(item[1] for item in observed), min(item[0] for item in observed),holes)


def depth_quality(db, query, provider, config, day, clock, *, required_scope=None):
    scope = required_scope or source_scope(provider,config,'depth5')
    required = {(item['exchange'],item['symbol'],item['asset_type']) for item in scope['symbols']}
    rows = list(db.scalars(query.where(EvidenceVersion.kind=='reference',
        EvidenceVersion.payload['dataset'].astext=='tdx.depth5')
        .order_by(EvidenceVersion.instrument_id,EvidenceVersion.received_at.desc())
        .distinct(EvidenceVersion.instrument_id)))
    identities = {item.id:(item.exchange,item.symbol,item.asset_type) for item in db.scalars(
        select(Instrument).where(Instrument.id.in_([row.instrument_id for row in rows])))}
    expected = {(side,str(level)) for side in ('bid','ask') for level in range(1,6)}
    valid, observed = set(), []
    for row in rows:
        values = row.payload.get('rows',[])
        at = {stamp(value.get('quote_time')) for value in values}
        if (len(values)==10 and {(value.get('side'),value.get('level')) for value in values}==expected
            and len(at)==1 and None not in at):
            native = next(iter(at))
            if (native<=clock and native.astimezone(SHANGHAI).date()==day and
                (clock-native).total_seconds()<=max(120,config.interval_seconds*2) and
                all(value.get('price') is not None and value.get('volume_shares') is not None for value in values)):
                valid.add(identities.get(row.instrument_id));observed.append((native,row.received_at))
    missing = len(required-valid)
    return Quality(bool(required) and not missing,'depth_missing_or_time_unverified' if missing or not required else None,
        max((item[1] for item in observed),default=None),min((item[0] for item in observed),default=None),missing)


def target(db, provider, config, capability, day, clock):
    from app.market.minute_backfill import configuration_fingerprint
    scope = source_scope(provider, config, capability)
    scope_key = content_hash(normalized(scope))
    signature = configuration_fingerprint(provider, config)
    row = db.scalar(select(DataHealth).where(DataHealth.owner_id == config.owner_id,
        DataHealth.primary_source_id == provider.id, DataHealth.capability == capability,
        DataHealth.scope_key == scope_key, DataHealth.trade_day == day).with_for_update())
    if row is None:
        row = DataHealth(owner_id=config.owner_id, primary_source_id=provider.id,
            capability=capability, scope_key=scope_key, scope=scope, trade_day=day,
            state='recovering', stage='source', failure_count=0, recovery_successes=0,
            configuration_sha256=signature, updated_at=clock)
        db.add(row)
        db.flush()
    elif row.configuration_sha256 != signature:
        row.configuration_sha256 = signature
        row.state, row.active_source_id = 'recovering', None
        row.failure_count = row.recovery_successes = 0
        row.last_run_id = row.last_valid_at = row.last_source_time = None
        row.last_probe_time = None
    return row


def provider_group(family):
    if family.startswith('tushare_'):
        return 'tushare'
    if family.startswith('ashare_'):
        return 'ashare'
    if family.startswith('sina_minute_'):
        return 'sina'
    if family.startswith(('eastmoney_', 'direction_em_')):
        return 'eastmoney'
    return family.split('_')[0]


def backup(db, provider, config, row, clock):
    from app.integrations.routing import ROUTES, collection_allowed, preferred_families
    from app.market.service import permission_reason
    route = ROUTES.get(row.capability)
    if route is None:
        return None
    families = {family for values in route[3].values() for family in values}
    ordered = preferred_families(db, row.capability, sorted(families))
    pairs = {p.family: (p, c) for p, c in db.execute(select(Provider, SourceSettings).join(
        SourceSettings, SourceSettings.source_id == Provider.id).where(Provider.family.in_(ordered)))}
    for family in ordered:
        pair = pairs.get(family)
        if pair is None:
            continue
        candidate, candidate_config = pair
        if (candidate.id == provider.id or candidate_config.owner_id != config.owner_id
            or permission_reason(candidate) or candidate_config.paused_reason
            or not collection_allowed(db, family)):
            continue
        if row.reason == 'rate_limited' and provider_group(family) == provider_group(provider.family):
            continue
        if not covers(source_scope(candidate, candidate_config, row.capability), row.scope):
            continue
        quality = saved_quality(db, candidate, candidate_config, row.capability, row.trade_day,
            clock, required_scope=row.scope)
        # An enabled-but-never-successful connection is not a backup.
        proof = latest_run(db, candidate.id, clock)
        if not proof or proof.started_at < candidate_config.configured_at:
            continue
        if binding_reason(db,candidate,proof):
            continue
        if quality.valid:
            return candidate, candidate_config, quality
        # Its own reconciliation repairs a stale candidate's schedule. Never
        # acquire a second source's write lock while holding this source's lock.
    return None


def pending_job(db, source_id):
    return db.scalar(select(Job).where(Job.kind == 'source.collect',
        Job.payload['source_id'].astext == str(source_id), Job.state.in_(['queued', 'running']),
        *[~Job.payload.has_key(key) for key in HISTORY_KEYS])
        .order_by(Job.created_at).limit(1))


def wake(db, provider, config, clock):
    from app.market.realtime_schedule import deferred_realtime
    if config.paused_reason or pending_job(db, provider.id):
        return
    last = latest_run(db, provider.id, clock)
    if last and last.retry_not_before and last.retry_not_before > clock:
        return
    if supplier_cooldown(db, provider.family, clock) is not None:
        return
    if deferred_realtime(db, provider.family, clock, config.interval_seconds) is not None:
        return
    # Only repair a stale future schedule. Do not shorten a configured interval
    # or a persisted supplier Retry-After following an ordinary failed attempt.
    from app.market.auction_schedule import next_collection
    from app.market.collection_cadence import run_anchor
    from app.market.cadence_policy import effective_interval
    anchor=run_anchor(db,provider.family,last)
    if last and next_collection(provider.family,anchor or last.received_at,
            effective_interval(provider.family,config.interval_seconds) if anchor else config.interval_seconds)>clock:
        return
    config.next_run_at = min(config.next_run_at, clock)


def supplier_cooldown(db, family, clock):
    from app.market.budget import family_cooldown_until
    return family_cooldown_until(db,family,clock)


def evaluate(db, provider, config, run, clock, *, allow_actions=True):
    from app.market.auction_schedule import schema_recheck
    from app.market.realtime_schedule import deferred_realtime
    from app.market.service import permission_reason
    from app.market.scheduling_waits import is_scheduling_wait
    day = clock.astimezone(SHANGHAI).date()
    for capability in capabilities(provider, config):
        row = target(db, provider, config, capability, day, clock)
        quality = saved_quality(db, provider, config, capability, day, clock, run)
        denied = permission_reason(provider) or binding_reason(db,provider,run)
        reason = denied or config.paused_reason or (run.reason if run and run.status not in {'complete', 'partial'} else None) or quality.reason
        pending = pending_job(db, provider.id)
        deferred = deferred_realtime(db, provider.family, clock, config.interval_seconds)
        queue_late = bool(pending and pending.state == 'queued' and
            pending.available_at <= clock-timedelta(seconds=15) and deferred is None)
        scheduling_wait = bool(is_scheduling_wait(run) and not denied and not config.paused_reason)
        retained_primary = scheduling_wait and row.state == 'normal' and row.active_source_id == provider.id
        source_ok = bool(quality.valid and not denied and not config.paused_reason and
            run and (run.status in {'complete', 'partial'} or retained_primary))
        new_run = bool(run and run.id != row.last_run_id)
        if new_run:
            row.last_run_id = run.id
            if scheduling_wait:
                pass
            elif source_ok:
                if quality.source_time is None or row.last_probe_time is None or quality.source_time > row.last_probe_time:
                    row.recovery_successes += 1
                row.last_probe_time = quality.source_time
                row.failure_count = 0
            else:
                row.failure_count += 1
                row.recovery_successes = 0
        row.updated_at = clock
        row.missing_count = quality.missing_count
        if quality.valid:
            row.last_valid_at = quality.received_at
            row.last_source_time = quality.source_time
        recovering_primary = row.state == 'fallback' and row.recovery_successes < 2
        if source_ok and not recovering_primary and not queue_late:
            row.state, row.stage, row.reason = 'normal', 'source', None
            row.active_source_id = provider.id
            row.next_action, row.next_action_at = None, None
            if scheduling_wait:
                row.reason = run.reason
                row.next_action = 'wait_session' if deferred is not None or run.reason == 'intraday_window_wait' else 'retry_source'
                row.next_action_at = max(clock, config.next_run_at, run.retry_not_before or clock,
                    deferred or clock, supplier_cooldown(db, provider.family, clock) or clock)
            continue
        row.reason = reason or ('primary_recovery_check' if recovering_primary else 'source_not_verified')
        alternative = backup(db, provider, config, row, clock)
        if alternative:
            candidate, _, available = alternative
            row.state, row.active_source_id = 'fallback', candidate.id
            row.last_valid_at, row.last_source_time = available.received_at, available.source_time
            row.next_action = 'configure_source' if reason in AUTH_REASONS else 'probe_primary'
        else:
            row.active_source_id = None
            manual = reason in AUTH_REASONS or reason in SCHEMA_REASONS and not schema_recheck(provider.family, reason)
            row.state = 'attention' if manual else 'recovering'
            row.next_action = 'configure_source' if reason in AUTH_REASONS else 'inspect_schema' if reason in SCHEMA_REASONS else 'retry_source'
            if reason == 'minute_coverage_pending':
                row.next_action = 'reconcile_history'
        row.stage = 'source'
        row.next_action_at = max(clock, config.next_run_at)
        if deferred is not None and row.state != 'attention':
            row.next_action, row.next_action_at = 'wait_session', deferred
        elif row.state != 'attention':
            if allow_actions:
                wake(db, provider, config, clock)
            row.next_action_at = max(clock, config.next_run_at, run.retry_not_before if run and run.retry_not_before else clock)
            shared_wait = supplier_cooldown(db, provider.family, clock)
            if shared_wait is not None:
                row.next_action_at = max(row.next_action_at, shared_wait)
        if row.state == 'attention':
            row.next_action_at = None
        if queue_late:
            row.stage, row.reason = 'scheduler', 'collection_queue_overdue'
            if row.state != 'fallback':
                row.state = 'recovering'
            row.next_action = 'repair_queue'
            row.next_action_at = clock


def record_fetch_outcome(db, provider, config, run, *, job=None):
    from app.market.realtime_schedule import FAMILIES
    if provider.family not in FAMILIES or job and (HISTORY_KEYS & set(job.payload)):
        return
    # Serialize the same source with the independent local checker. The caller
    # still holds the collector's valid lease and commits this with ingestion.
    # Monitoring must not roll back a successfully stored market batch if the
    # independent checker momentarily holds its row. A later scan catches up.
    try:
        with db.begin_nested():
            lock(db, 'data-health:' + str(provider.id))
            db.flush()
            evaluate(db, provider, config, run, run.received_at, allow_actions=False)
    except Exception as error:
        import logging
        logging.getLogger(__name__).warning('data_health_deferred kind=%s', type(error).__name__)


def record_committed_fetch(source_id, run_id, job_id):
    """A monitor cannot roll back ingestion or inherit its cache/config locks.

    The fetch run is already durable. If the process stops here, the independent
    checker reconstructs the same decision from that run on its next pass.
    """
    try:
        with Session(engine()) as db, db.begin():
            db.execute(text("SET LOCAL lock_timeout = '1s'"))
            db.execute(text("SET LOCAL statement_timeout = '8s'"))
            provider, config = db.get(Provider,source_id), db.get(SourceSettings,source_id)
            run, job = db.get(DataFetchRun,run_id), db.get(Job,job_id)
            if provider is not None and config is not None and run is not None:
                record_fetch_outcome(db,provider,config,run,job=job)
    except Exception as error:
        import logging
        logging.getLogger(__name__).warning('data_health_deferred kind=%s',type(error).__name__)


def projection_health(db, clock):
    from app.read_cache import cache_scope, ttl
    from app.read_cache_models import ReadCache
    if not settings().read_cache_enabled:
        return
    day = clock.astimezone(SHANGHAI).date()
    owners = list(db.scalars(select(User).where(User.enabled.is_(True), User.id.in_(
        select(ReadCache.owner_id).where(ReadCache.operation.in_(['workbench', 'details']),
            ReadCache.accessed_at >= clock-timedelta(hours=1))))))
    scope = {'operations':['workbench', 'details']}
    key = content_hash(normalized(scope))
    for owner in owners:
        current_scope = cache_scope(db, owner)
        # Health checks inspect state and may wake a row; large market bodies
        # must stay in storage. Refuse accidental lazy loading on this path.
        entries = list(db.scalars(select(ReadCache).options(defer(ReadCache.payload, raiseload=True)).where(ReadCache.owner_id == owner.id,
            ReadCache.scope == current_scope, ReadCache.operation.in_(['workbench', 'details']),
            ReadCache.accessed_at >= clock-timedelta(hours=1))))
        live = [entry for entry in entries if not entry.params.get('as_of_time') and
            (not entry.params.get('day') or entry.params['day'] == day.isoformat())]
        if not live:
            continue
        row = db.scalar(select(DataHealth).where(DataHealth.owner_id == owner.id,
            DataHealth.primary_source_id.is_(None), DataHealth.capability == 'pipeline:projection',
            DataHealth.scope_key == key, DataHealth.trade_day == day))
        if row is None:
            row = DataHealth(owner_id=owner.id, capability='pipeline:projection', scope_key=key,
                scope=scope, trade_day=day, configuration_sha256=key, state='normal', stage='projection',
                failure_count=0, recovery_successes=0, updated_at=clock)
            db.add(row)
        overdue = [entry for entry in live if entry.dirty and
            (entry.built_at or entry.accessed_at) < clock-timedelta(seconds=max(
                30 if entry.operation == 'workbench' else 60, 2*ttl(entry.operation,entry.params)))]
        row.updated_at, row.missing_count = clock, len(overdue)
        row.last_valid_at = max((entry.built_at for entry in live if entry.built_at), default=None)
        if overdue:
            row.state, row.reason = 'recovering', 'projection_overdue'
            row.next_action, row.next_action_at = 'refresh_projection', clock
            for entry in overdue:
                # Never release an active builder's lease; the process watchdog
                # owns a genuine stall. Fair scheduling then rebuilds this row.
                if entry.lease_until is None or entry.lease_until <= clock:
                    entry.refresh_after = min(entry.refresh_after, clock)
        else:
            row.state, row.reason, row.next_action, row.next_action_at = 'normal', None, None, None


def reconcile(*, force=False, checkpoint=None):
    from app.maintenance_control import paused
    from app.market.realtime_schedule import FAMILIES
    if not settings().data_collection_enabled:
        return
    with Session(engine()) as db, db.begin():
        db.execute(text("SET LOCAL lock_timeout = '1s'"))
        db.execute(text("SET LOCAL statement_timeout = '8s'"))
        if paused(db):
            return
        lock(db, ACTION)
        clock = now(db)
        previous = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == ACTION))
        if not force and previous and previous + timedelta(seconds=15) > clock:
            return
        db.add(AuditEvent(action=ACTION, resource_type='recovery',
            changed_field_names=['local_pipeline_check'], created_at=clock))
        source_ids = list(db.scalars(select(Provider.id).join(SourceSettings,
            SourceSettings.source_id == Provider.id).where(Provider.family.in_(FAMILIES),
            Provider.enabled.is_(True))))
    if checkpoint:
        checkpoint()
    for source_id in source_ids:
        try:
            reconcile_source(source_id)
        except SQLAlchemyError:
            import logging
            logging.getLogger(__name__).warning('data_health_source_deferred kind=database_busy')
        except Exception as error:
            # A malformed local check must not stop other sources or the short
            # scheduler loop. Persist a visible next action without exception
            # messages, SQL or supplier payloads.
            import logging
            logging.getLogger(__name__).error('data_health_source_failed kind=%s', type(error).__name__)
            try:
                source_check_failed(source_id)
            except Exception as record_error:
                logging.getLogger(__name__).error('data_health_failure_record_deferred kind=%s', type(record_error).__name__)
        # No health/configuration/cache transaction remains open here. Let the
        # same scheduler dispatch newly due feeds during a long health sweep,
        # including after a failed source check, without adding another thread.
        if checkpoint:
            checkpoint()
    with Session(engine()) as db, db.begin():
        db.execute(text("SET LOCAL lock_timeout = '1s'"))
        projection_health(db, now(db))
    if checkpoint:
        checkpoint()


def source_check_failed(source_id):
    from app.market.minute_backfill import configuration_fingerprint
    with Session(engine()) as db, db.begin():
        db.execute(text("SET LOCAL lock_timeout = '1s'"))
        config, provider = db.get(SourceSettings, source_id), db.get(Provider, source_id)
        if config is None or provider is None or not provider.enabled:
            return
        lock(db, 'data-health:' + str(source_id))
        clock = now(db)
        day = clock.astimezone(SHANGHAI).date()
        scope = {'universe':'source_universe', 'asset_type':'source', 'symbols':[]}
        key = content_hash(normalized(scope))
        signature = configuration_fingerprint(provider, config)
        row = db.scalar(select(DataHealth).where(DataHealth.owner_id == config.owner_id,
            DataHealth.primary_source_id == source_id, DataHealth.capability == 'pipeline:source_check',
            DataHealth.scope_key == key, DataHealth.trade_day == day).with_for_update())
        if row is None:
            row = DataHealth(owner_id=config.owner_id, primary_source_id=source_id,
                capability='pipeline:source_check', scope_key=key, scope=scope,
                trade_day=day, failure_count=0, recovery_successes=0)
            db.add(row)
        row.configuration_sha256 = signature
        row.state, row.stage, row.reason = 'attention', 'monitor', 'local_check_failed'
        row.failure_count += 1
        row.next_action, row.next_action_at = 'inspect_runtime', clock + timedelta(seconds=15)
        row.updated_at = clock


def reconcile_source(source_id):
    from app.integrations.routing import collection_allowed
    with Session(engine()) as db, db.begin():
        db.execute(text("SET LOCAL lock_timeout = '1s'"))
        db.execute(text("SET LOCAL statement_timeout = '8s'"))
        # No configuration row locks while health triggers invalidate caches:
        # ingestion may already own those cache rows before it updates its schedule.
        config = db.get(SourceSettings,source_id)
        if config is None:
            return
        lock(db, 'data-health:' + str(source_id))
        provider = db.get(Provider, source_id)
        owner = db.get(User, config.owner_id)
        if not provider or not provider.enabled or not owner or not owner.enabled or owner.role != 'admin' or not collection_allowed(db, provider.family):
            return
        clock = now(db)
        evaluate(db, provider, config, latest_run(db, source_id, clock), clock,allow_actions=False)
        db.execute(delete(DataHealth).where(DataHealth.owner_id == config.owner_id,
            DataHealth.primary_source_id == source_id, DataHealth.capability == 'pipeline:source_check',
            DataHealth.trade_day == clock.astimezone(SHANGHAI).date()))
    # The schedule write is independent and owns no health/dependency/cache locks.
    # It rechecks the current config, pauses, phase and supplier cooldown.
    with Session(engine()) as db, db.begin():
        db.execute(text("SET LOCAL lock_timeout = '1s'"))
        config = db.scalar(select(SourceSettings).where(SourceSettings.source_id==source_id).with_for_update(skip_locked=True))
        provider = db.get(Provider,source_id)
        if config is not None and provider is not None and provider.enabled:
            from app.market.service import permission_reason
            if not permission_reason(provider) and collection_allowed(db,provider.family):
                wake(db,provider,config,now(db))


def serving_order(db, capability, families, scope=None, *, source_families=None):
    """Apply circuit hysteresis to compatible current reads, never replay."""
    key = ('resilience.serving', capability)
    if key not in db.info:
        today = now(db).astimezone(SHANGHAI).date()
        db.info[key] = list(db.scalars(select(DataHealth).where(DataHealth.capability == capability,
            DataHealth.trade_day == today, DataHealth.state == 'fallback').order_by(DataHealth.updated_at.desc())))
    by_id = source_families if source_families is not None else {
        provider.id: provider.family for provider in db.scalars(select(Provider).where(Provider.family.in_(families)))}
    for row in db.info[key]:
        primary, active = by_id.get(row.primary_source_id), by_id.get(row.active_source_id)
        if not primary or not active or primary not in families or active not in families:
            continue
        if scope is not None and row.scope['universe'] == 'configured_symbols':
            identity = (scope.exchange, scope.symbol, scope.asset_type)
            if identity not in {(item['exchange'], item['symbol'], item['asset_type']) for item in row.scope['symbols']}:
                continue
        if families.index(primary) < families.index(active):
            output = list(families)
            output.remove(active)
            output.insert(output.index(primary), active)
            return output
    return families
