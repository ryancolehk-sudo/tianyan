"""Explicit selected-instrument daily history, bounded to one paid-source request."""
from datetime import date

from sqlalchemy import func, select

from app.auth import APIError
from app.evidence.common import content_hash, lock, normalized, now
from app.evidence.models import EvidenceVersion
from app.integrations.credentials import assert_source_binding, job_context, resolve_source_credentials
from app.market.models import InstrumentOrigin, SourceSettings
from app.market.protocol import InstrumentKey, SHANGHAI, SourceFailure
from app.models import AuditEvent, Instrument, Job, Provider

PAGE_LIMIT = 6000
MAX_DAYS = 5 * 366
FAMILIES = ('tushare_daily', 'tushare_index_daily', 'baostock_history', 'fuyao_daily', 'fuyao_index_daily')


def supports_daily(provider):
    # Existing paid daily adapters advertise their native EOD capability;
    # their verified OHLC conversion also yields daily bars. Preserve that
    # descriptor and all existing source-version identities during upgrade.
    capability='bar.daily' if provider.family in {'baostock_history','fuyao_daily','fuyao_index_daily'} else 'quote.eod'
    return provider.family in FAMILIES and capability in provider.capabilities


def historical_job(payload):
    return isinstance(payload.get('daily_history'), dict)


def fingerprint(provider, config):
    return content_hash(normalized(dict(source_id=provider.id, family=provider.family,
        adapter_version=provider.adapter_version, capabilities=provider.capabilities,
        configured=provider.configured, enabled=provider.enabled, rights=provider.rights,
        entitlement=provider.entitlement_status, config=config.config,
        interval_seconds=config.interval_seconds, owner_id=config.owner_id,
        rights_basis=config.rights_basis, configured_at=config.configured_at, paused_reason=config.paused_reason)))


def history_identity(db, instrument_id):
    from app.market.registry import INDEXES
    instrument=db.get(Instrument,instrument_id)
    if instrument is None or instrument.valid_to is not None:
        raise SourceFailure('instrument_unavailable')
    if instrument.asset_type=='index':
        if not any((instrument.exchange,instrument.symbol)==(e,s) for e,s,_ in INDEXES):
            raise SourceFailure('instrument_identity_unverified')
    elif instrument.asset_type=='stock':
        # The request selects an existing permitted directory claim, never an
        # arbitrary caller-supplied upstream code or a quote-only guess.
        permitted=db.scalar(select(InstrumentOrigin.id).join(Provider,Provider.id==InstrumentOrigin.source_id).where(
            InstrumentOrigin.instrument_id==instrument.id,InstrumentOrigin.origin_kind=='catalog',
            Provider.configured.is_(True),Provider.enabled.is_(True),Provider.entitlement_status=='verified',
            Provider.rights['display'].astext=='allowed',Provider.rights['cache'].astext=='allowed').limit(1))
        if not permitted:
            raise SourceFailure('instrument_identity_unverified')
    else:
        raise SourceFailure('unsupported_asset_scope')
    return InstrumentKey(instrument.exchange,instrument.symbol,instrument.asset_type)


def range_days(start_day,end_day,clock):
    today=clock.astimezone(SHANGHAI).date()
    end=end_day or today
    try:
        default_start=end.replace(year=end.year-5)
    except ValueError:
        default_start=end.replace(year=end.year-5,day=28)
    start=start_day or default_start
    if end>today:
        raise SourceFailure('future_day')
    if start>end or start<date(1990,1,1) or (end-start).days>MAX_DAYS:
        raise SourceFailure('history_range_invalid')
    return start,end


def history_provider(db,key):
    family='tushare_index_daily' if key.asset_type=='index' else 'tushare_daily'
    return db.scalar(select(Provider).where(Provider.family==family))


def validate_history(db,payload,provider,config,clock):
    from app.market.service import permission_reason
    ctx=payload.get('daily_history')
    if not isinstance(ctx,dict):
        raise SourceFailure('invalid_history_context')
    if permission_reason(provider) or config.paused_reason:
        raise SourceFailure(permission_reason(provider) or 'source_paused')
    assert_source_binding(db, provider.id, provider.family, payload)
    if not supports_daily(provider):
        raise SourceFailure('capability_not_implemented')
    try:
        from uuid import UUID
        ident=UUID(ctx['instrument_id'])
        start,end=range_days(date.fromisoformat(ctx['start_day']),date.fromisoformat(ctx['end_day']),clock)
        key=history_identity(db,ident)
        expected=dict(instrument_id=str(ident),instrument=normalized(dict(exchange=key.exchange,symbol=key.symbol,
            asset_type=key.asset_type)),start_day=start.isoformat(),end_day=end.isoformat(),adjustment='unadjusted',
            page_limit=PAGE_LIMIT,configuration_sha256=fingerprint(provider,config))
        family='tushare_index_daily' if key.asset_type=='index' else 'tushare_daily'
        if ctx!=expected or provider.family!=family:
            raise SourceFailure('configuration_changed')
        return key,dict(ts_code=key.symbol+'.'+{'SSE':'SH','SZSE':'SZ','BSE':'BJ'}[key.exchange],
            start_date=start.strftime('%Y%m%d'),end_date=end.strftime('%Y%m%d'),limit=PAGE_LIMIT)
    except (KeyError,ValueError,TypeError):
        raise SourceFailure('invalid_history_context') from None


def enqueue_history(db,owner,body,request_id=None):
    from app.config import settings
    from app.market.service import permission_reason
    from app.market.budget import tushare_cooldown_until
    if not settings().data_collection_enabled:
        raise APIError(422,'collection_disabled','本机采集尚未启用。')
    try:
        key=history_identity(db,body.instrument_id)
        start,end=range_days(body.start_day,body.end_day,now(db))
        if body.adjustment!='unadjusted':
            raise SourceFailure('history_adjustment_unsupported')
        provider=history_provider(db,key)
        config=db.get(SourceSettings,provider.id) if provider else None
        if provider is None or config is None:
            raise SourceFailure('history_source_not_configured')
        bound = resolve_source_credentials(db, provider.id, provider.family, refresh=True)
        if bound.reason:
            raise SourceFailure(bound.reason)
        if permission_reason(provider) or config.paused_reason:
            raise SourceFailure(permission_reason(provider) or 'source_paused')
        if not supports_daily(provider):
            raise SourceFailure('capability_not_implemented')
        context=dict(instrument_id=str(body.instrument_id),instrument=normalized(dict(exchange=key.exchange,
            symbol=key.symbol,asset_type=key.asset_type)),start_day=start.isoformat(),end_day=end.isoformat(),
            adjustment='unadjusted',page_limit=PAGE_LIMIT,configuration_sha256=fingerprint(provider,config))
    except SourceFailure as exc:
        raise APIError(422,exc.reason,'该标的、历史范围、来源或复权条件尚未满足；未发起外部请求。') from None
    lock(db,f'source:queue:{provider.id}')
    pending=db.scalar(select(Job).where(Job.kind=='source.collect',Job.payload['source_id'].astext==str(provider.id),
        Job.state.in_(['queued','running'])).order_by(Job.created_at).limit(1))
    if pending:
        if pending.payload.get('daily_history')!=context or pending.payload.get('integration_context') != job_context(bound, provider.id):
            raise APIError(409,'collection_intent_conflict','该来源已有其他标的或范围的任务，请等待完成。')
        return pending
    clock=now(db)
    cooldown=tushare_cooldown_until(db,clock)
    # Queue during pacing; the existing permit rechecks and defers before HTTP.
    payload = {'source_id':str(provider.id),'daily_history':context}
    binding_context = job_context(bound, provider.id)
    if binding_context is not None:
        payload['integration_context'] = binding_context
    job=Job(owner_id=config.owner_id,kind='source.collect',payload=payload,
        available_at=max(clock,cooldown or clock))
    db.add(job);db.flush()
    db.add(AuditEvent(actor_id=owner.id,action='source.history',request_id=request_id,resource_type='job',resource_id=job.id,
        changed_field_names=['instrument','requested_range','queued']))
    return job


def ordinary_run_condition():
    from app.market.models import DataFetchRun
    jobs=select(Job.id).where(Job.kind=='source.collect',
        Job.payload.has_key('daily_history') | Job.payload.has_key('minute_context'))
    return DataFetchRun.job_id.is_(None) | ~DataFetchRun.job_id.in_(jobs)


def read_daily(db,instrument_id,adjustment,*,source_id=None,before=None,start_day=None,end_day=None,limit=400):
    """One permitted source, date ordering before LIMIT, explicit older-page flag."""
    from app.market.service import permission_reason
    from app.market.read_models import market_identities
    clock=now(db)
    if not 1<=limit<=2000:
        raise APIError(422,'history_limit_invalid','单页最多读取2000根已保存日K。')
    today=clock.astimezone(SHANGHAI).date()
    if (end_day and end_day>today) or (start_day and end_day and start_day>end_day):
        raise APIError(422,'history_range_invalid','历史查询日期范围无效。')
    ranks=select(EvidenceVersion.id,func.row_number().over(partition_by=(EvidenceVersion.source_id,EvidenceVersion.source_key),
        order_by=EvidenceVersion.version_number.desc()).label('position')).where(EvidenceVersion.kind=='bar',
        EvidenceVersion.owner_id.is_(None),EvidenceVersion.visibility=='shared',EvidenceVersion.instrument_id==instrument_id,
        EvidenceVersion.received_at<=clock).subquery()
    # Legacy payloads without a price basis keep their original unadjusted meaning.
    from sqlalchemy import case
    basis=case((EvidenceVersion.payload.has_key('adjustment'),EvidenceVersion.payload['adjustment'].astext),else_='unadjusted')
    stmt=select(EvidenceVersion).join(ranks,ranks.c.id==EvidenceVersion.id).where(ranks.c.position==1,
        EvidenceVersion.payload['interval'].astext=='1d',basis==adjustment,
        EvidenceVersion.payload['trade_day'].astext<=today.isoformat())
    providers=list(db.scalars(select(Provider).where(Provider.family.in_(FAMILIES))))
    allowed=[p for p in providers if not permission_reason(p) and supports_daily(p) and (source_id is None or p.id==source_id)]
    from app.integrations.routing import preferred_families
    identity=market_identities(db,instrument_id)[0]
    families=preferred_families(db,'index_daily' if identity.asset_type=='index' else 'stock_daily',[p.family for p in allowed],scope=identity)
    if source_id is None:allowed=[p for p in allowed if p.family in families]
    allowed.sort(key=lambda p:(families.index(p.family) if p.family in families else len(families),str(p.id)))
    requested=stmt
    if before:requested=requested.where(EvidenceVersion.payload['trade_day'].astext<before.isoformat())
    if start_day:requested=requested.where(EvidenceVersion.payload['trade_day'].astext>=start_day.isoformat())
    if end_day:requested=requested.where(EvidenceVersion.payload['trade_day'].astext<=end_day.isoformat())
    provider=next((p for p in allowed if db.scalar(requested.where(EvidenceVersion.source_id==p.id).limit(1))) ,None)
    if provider is None:
        provider=next(iter(allowed),None)
    coverage_start=coverage_end=None;rows=[];more=False
    if provider:
        scoped=stmt.where(EvidenceVersion.source_id==provider.id)
        # Aggregate the already scoped current revisions directly, without a
        # second scan of all evidence and a redundant ID membership join.
        coverage_days=scoped.with_only_columns(EvidenceVersion.payload['trade_day'].astext.label('trade_day')).subquery()
        coverage=db.execute(select(func.min(coverage_days.c.trade_day),
            func.max(coverage_days.c.trade_day))).one()
        coverage_start=date.fromisoformat(coverage[0]) if coverage[0] else None
        coverage_end=date.fromisoformat(coverage[1]) if coverage[1] else None
        scoped=requested.where(EvidenceVersion.source_id==provider.id)
        raw=list(db.scalars(scoped.order_by(EvidenceVersion.payload['trade_day'].astext.desc(),
            EvidenceVersion.version_number.desc(),EvidenceVersion.id.desc()).limit(limit+1)))
        more=len(raw)>limit
        rows=list(reversed(raw[:limit]))
    instrument=market_identities(db,instrument_id)[0]
    key=InstrumentKey(instrument.exchange,instrument.symbol,instrument.asset_type)
    history=history_provider(db,key) if key.asset_type in {'stock','index'} else None
    config=db.get(SourceSettings,history.id) if history else None
    reason=('history_adjustment_unsupported' if adjustment!='unadjusted' else 'history_source_not_configured' if not history or not config
        else permission_reason(history) or ('source_paused' if config.paused_reason else 'capability_not_implemented' if not supports_daily(history) else None))
    # Capability is separate from cached data and must not turn an unknown local
    # identity into permission to make a network request.
    if reason is None:
        try:history_identity(db,instrument_id)
        except SourceFailure as exc:reason=exc.reason
    if reason is None:
        from app.config import settings
        bound = resolve_source_credentials(db, history.id, history.family)
        reason='collection_disabled' if not settings().data_collection_enabled else bound.reason
    return rows,dict(source_id=provider.id if provider else None,source_family=provider.family if provider else None,
        source_name=provider.name if provider else None,has_more=more,coverage_start_day=coverage_start,
        coverage_end_day=coverage_end,requested_start_day=start_day,requested_end_day=end_day,requested_before=before,
        history_supported=reason is None,history_source_id=history.id if history and reason is None else None,history_reason=reason)
