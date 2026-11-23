"""Shared collection state. Read APIs never contact a market vendor."""
import uuid
from datetime import datetime, time, timedelta

from sqlalchemy import String, case, cast, exists, func, or_, select
from sqlalchemy.orm import Session

from app.auth import APIError, require_instance
from app.db import engine
from app.evidence.common import content_hash, lock, normalized, now
from app.evidence.models import SourceVersion
from app.integrations.credentials import invalidate, job_context, resolve_source_credentials
from app.market.models import DataFetchRun, SourceSettings
from app.market.registry import INDEX_IDENTITIES, SOURCES
from app.market.connected_specs import FUYAO_FAMILIES, TDX_FAMILIES
from app.market.direction_specs import PUBLIC_FAMILIES as DIRECTION_FAMILIES
from app.market.breadth_history_specs import FAMILY as BREADTH_HISTORY
from app.market.news_feeds import NEWS_FAMILIES, news_metadata
from app.market.review_protocol import PUBLIC_FAMILIES
from app.market.protocol import SHANGHAI
from app.market.minute import FAMILIES as MINUTE_FAMILIES, minute_url, FAMILY_INTERVALS
from app.market.minute_schedule import next_minute, deferred_minute
from app.market.tushare_specs import FAMILIES as TUSHARE_FAMILIES, DATED_FAMILIES, SELECTED_FAMILIES
from app.market.budget import ASHARE_FAMILIES, ashare_cooldown_until, tushare_cooldown_until, sina_cooldown_until
from app.market.eastmoney_trading import FAMILIES as EASTMONEY_FAMILIES
from app.market.budget import eastmoney_cooldown_until,family_cooldown_until
from app.market.supplier_cooldown import THS_FAMILIES,shared_deadline
from app.market.schemas import RuntimeSource, SourceAdapter, SourceConfiguration, SourceIndex
from app.models import AuditEvent, Job, Provider
from app.market.minute_backfill import make_context, live_run_condition, fetch_run_dto, is_backfill
from app.market.daily_history import historical_job
from app.market.auction_schedule import CURRENT, BREADTH, DIRECTION_RECOVERY, FAST_DIRECTION_RECOVERY, RANKING_RECOVERY, SCHEMA_RECOVERY, next_collection, schema_recheck
from app.schemas import Provider as ProviderDTO


def source_id(family):
    if family not in SOURCES:
        raise APIError(422, 'unsupported_source', '该来源尚未实现，不能仅凭候选清单启用。')
    return uuid.uuid5(uuid.NAMESPACE_URL, 'jiulong-pro-max/source/' + family)


def configure_source(db, owner, body: SourceConfiguration, request_id=None):
    require_instance(db)
    if owner.role != 'admin' or not owner.enabled:
        raise APIError(403, 'admin_required', '仅管理员可配置全实例数据来源。')
    definition = SOURCES[body.family]
    if body.collection_mode=='focused' and body.family!='tencent_web':
        raise APIError(422,'collection_mode_unsupported','关注模式仅用于腾讯报价。')
    if body.family in FUYAO_FAMILIES | TDX_FAMILIES:
        required_asset='index' if body.family in {'fuyao_indices','fuyao_index_daily'} else 'stock'
        if body.family=='fuyao_indices':
            if body.symbols:
                raise APIError(422,'symbols_not_applicable','四指数报价使用已核实的固定指数身份。')
        elif body.family in {'tdx_full_minute','fuyao_quotes','fuyao_auction'} and body.collection_mode=='full_market':
            if body.symbols:
                raise APIError(422,'symbols_not_applicable','全市场补齐使用本机有效股票目录，不混入手工范围。')
        elif not 1<=len(body.symbols)<=(20 if body.family in TDX_FAMILIES else 50 if body.family=='fuyao_quotes' else 4 if body.family=='fuyao_index_daily' else 3):
            raise APIError(422,'instrument_required','请明确选择有界标的范围；股票日K与财务每项最多3只，指数日K最多4个，通达信每项最多20只。')
        if any(item.asset_type!=required_asset for item in body.symbols):
            raise APIError(422,'unsupported_asset_scope','该能力的股票与指数身份不能混用。')
        if body.family not in {'tdx_full_minute','fuyao_quotes','fuyao_auction'} and body.collection_mode!='selected':
            raise APIError(422,'collection_mode_unsupported','当前能力不接受全市场范围。')
        if body.family in FUYAO_FAMILIES and body.points_enabled:
            raise APIError(422,'collection_options_not_applicable','同花顺连接不使用通达信积分选项。')
    elif body.points_enabled or (body.collection_mode!='selected' and body.family not in {'tencent_auction','tencent_market_rt'}
            and not (body.family=='tencent_web' and body.collection_mode=='focused')):
        raise APIError(422,'collection_options_not_applicable','积分与全市场配置仅用于已适配的通达信能力。')
    if body.backup_enabled and body.family not in {'fuyao_quotes','fuyao_indices'}:
        raise APIError(422,'backup_options_not_applicable','自动备用目前仅用于已核实的同花顺报价能力。')
    if body.board_codes and body.family not in {'eastmoney_boards','tushare_dc_member'}:
        raise APIError(422,'board_codes_not_applicable','仅东方财富板块来源接受明确板块代码。')
    if body.sector_codes and body.family != 'tushare_dc_member':
        raise APIError(422,'sector_codes_not_applicable','仅DC历史成分来源接受Tushare板块代码。')
    if body.family == 'tushare_dc_member':
        from app.market.sector_scope import member_sector_codes
        from app.market.protocol import SourceFailure
        try:
            member_sector_codes({'sector_codes':body.sector_codes,'board_codes':body.board_codes})
        except SourceFailure as error:
            raise APIError(422,error.reason,'请明确选择1至10个DC概念板块；旧单板块配置仍兼容，不隐式轮询全部板块。') from None
    if body.family in PUBLIC_FAMILIES | DIRECTION_FAMILIES:
        if body.symbols:
            raise APIError(422, 'symbols_not_applicable', '复盘公开连接使用固定指数或日期范围，不接受手工标的。')
        if body.rights.export == 'allowed' or body.rights.redistribute == 'allowed':
            raise APIError(422, 'permission_missing', '复盘公开连接仅供本机个人研究。')
    if body.family == BREADTH_HISTORY:
        if body.symbols:
            raise APIError(422, 'symbols_not_applicable', '历史广度按明确日期读取，不接受标的列表。')
        if body.rights.export != 'denied' or body.rights.redistribute != 'denied':
            raise APIError(422, 'permission_missing', '历史广度仅供本人本机展示与缓存，导出及再分发必须明确关闭。')
    if body.family in EASTMONEY_FAMILIES:
        if body.symbols:
            raise APIError(422,'symbols_not_applicable','该连接读取有界排行，个股不由手工标的列表推断。')
        if body.rights.export=='allowed' or body.rights.redistribute=='allowed':
            raise APIError(422,'permission_missing','公开排名适配仅用于本机个人研究，不开放行情导出或再分发。')
    if body.family in MINUTE_FAMILIES:
        if len(body.symbols) != 1:
            raise APIError(422, 'instrument_required', '每条分钟来源明确选择一个沪深股票，不轮询全市场。')
        from app.market.protocol import InstrumentKey, SourceFailure
        try:
            minute_url(InstrumentKey(**body.symbols[0].model_dump()), FAMILY_INTERVALS[body.family])
        except SourceFailure:
            raise APIError(422, 'unsupported_asset_scope', '当前分钟来源仅核验沪深A股股票。') from None
        if body.rights.export == 'allowed' or body.rights.redistribute == 'allowed':
            raise APIError(422, 'permission_missing', '该连接仅供本机个人研究，不开放行情导出或再分发。')
    if body.family in TUSHARE_FAMILIES:
        if body.family in SELECTED_FAMILIES:
            if len(body.symbols) != 1:
                raise APIError(422, 'instrument_required', '本批每条Tushare连接明确选择一个标的，逐项核验权益与覆盖。')
            required_asset = 'index' if body.family == 'tushare_index_daily' else 'stock'
            if body.symbols[0].asset_type != required_asset:
                raise APIError(422, 'unsupported_asset_scope', '该接口的股票与指数身份不能混用。')
        elif body.symbols:
            raise APIError(422, 'symbols_not_applicable', '该Tushare表接口不接受手工标的。')
    if body.interval_seconds < definition.minimum_interval:
        raise APIError(422, 'source_rate_limit', '刷新间隔低于本项目为该来源设置的请求预算。')
    if body.family in NEWS_FAMILIES and body.symbols:
        raise APIError(422, 'symbols_not_applicable', '资讯订阅不接受证券标的列表。')
    if body.family == 'tencent_web' and not body.symbols and body.collection_mode != 'focused':
        raise APIError(422, 'instrument_required', '请明确选择少量标的，不能隐式采集全市场。')
    if body.family=='tencent_market_rt':
        if body.symbols or body.collection_mode!='full_market':
            raise APIError(422,'collection_mode_unsupported','批量报价使用明确的已存名录范围，不混用手工或关注标的。')
        if body.rights.export!='denied' or body.rights.redistribute!='denied':
            raise APIError(422,'permission_missing','批量网页行情仅供本人本机展示与缓存，导出和再分发需关闭。')
    if body.family in {'tickflow_live','ashare_public'} and not 1<=len(body.symbols)<=5:
        raise APIError(422,'instrument_required','免费连接每次明确选择1至5个标的，不使用付费标的池。')
    if body.family in {'tencent_auction','fuyao_auction'} and body.collection_mode=='full_market' and body.symbols:
        raise APIError(422, 'symbols_not_applicable', '竞价盘口试采使用有权股票目录，不混用手工标的。')
    if body.family in {'eastmoney_auction','tencent_auction'} and not (body.family=='tencent_auction' and body.collection_mode=='full_market') and not 1<=len(body.symbols)<=20:
        raise APIError(422, 'instrument_required', '竞价快照每次明确选择1至20个沪深股票，不做全市场轮询。')
    if body.family in {'baostock_history','baostock_fundamental'} and not 1<=len(body.symbols)<=3:
        raise APIError(422,'instrument_required','历史和财务每来源明确选择1至3个标的，控制首次回补规模。')
    if body.family in {'cninfo','baostock_daily','baostock_classification','ecb_public','worldbank_public','ecb_news','fed_public','cninfo_announcements','ashare_breadth','hk_gov_finance','ashare_sector_heat','mairui_funds','mairui_pools','duanxianxia_auction','duanxianxia_auction_history'} and body.symbols:
        raise APIError(422, 'symbols_not_applicable', '标的目录来源由公开目录导入，不接受手工标的列表。')
    if body.family == 'szse_public' and body.symbols:
        raise APIError(422, 'symbols_not_applicable', '日历来源不接受标的列表。')
    if len({(item.exchange, item.symbol) for item in body.symbols}) != len(body.symbols):
        raise APIError(422, 'duplicate_instrument', '同一供应商代码不能映射到多个资产身份。')
    for item in body.symbols:
        if body.family in {'eastmoney_auction','tencent_auction'} and (item.asset_type != 'stock' or item.exchange == 'BSE'):
            raise APIError(422, 'unsupported_asset_scope', '竞价快照本批只接受沪深股票，指数/ETF/转债/北交所不接入。')
        if body.family.startswith('baostock_') and (item.exchange=='BSE' or (body.family=='baostock_fundamental' and item.asset_type!='stock')):
            raise APIError(422,'unsupported_asset_scope','该参考接口仅核验沪深股票；历史源另支持已登记指数。')
        if item.asset_type not in {'stock','index'}:
            raise APIError(422,'unsupported_asset_scope','本批尚未验收ETF或转债字段，不能仅凭相似报价格式启用。')
        known_index=(item.exchange,item.symbol) in INDEX_IDENTITIES
        if known_index != (item.asset_type=='index'):
            raise APIError(422,'instrument_identity_unverified','指数与股票身份未匹配本批已核实目录，请先补充标的来源依据。')
    if body.enabled and (body.entitlement_status != 'verified' or body.rights.cache != 'allowed' or body.rights.display != 'allowed'):
        raise APIError(422, 'permission_missing', '启用前须分别核实连接权益、展示与缓存范围；未知不能自动放行。')
    ident = source_id(body.family)
    lock(db, f'source:configuration:{ident}')
    provider = db.get(Provider, ident)
    if provider is None:
        provider = Provider(id=ident, family=body.family, kind=definition.kind, name=definition.name)
        db.add(provider)
    if provider.family != body.family:
        raise APIError(409, 'source_identity_conflict', '来源身份不一致，原数据已保留。')
    provider.adapter_version = definition.version
    provider.configured, provider.enabled = True, body.enabled
    provider.entitlement_status = body.entitlement_status
    provider.capabilities, provider.rights = list(definition.capabilities), body.rights.model_dump()
    recorded = now(db)
    settings = db.get(SourceSettings, ident)
    prior_mode=settings.config.get('collection_mode','selected') if settings else 'selected'
    cooldown = ashare_cooldown_until(db, recorded) if body.family in ASHARE_FAMILIES else None
    if body.family in TUSHARE_FAMILIES:
        cooldown = tushare_cooldown_until(db, recorded)
    if body.family in MINUTE_FAMILIES:
        cooldown = sina_cooldown_until(db, recorded)
    from app.market.qt_budget import FAMILIES as QT_FAMILIES,cooldown_until as qt_cooldown
    if body.family in QT_FAMILIES:
        cooldown=qt_cooldown(db,recorded)
    config_values={'symbols':normalized(body.symbols)}
    if body.family in TDX_FAMILIES:
        config_values.update(points_enabled=body.points_enabled,collection_mode=body.collection_mode)
    if body.family in {'fuyao_quotes','tencent_auction','fuyao_auction','tencent_market_rt'} and body.collection_mode=='full_market':
        config_values['collection_mode']='full_market'
    if body.family=='tencent_web' and body.collection_mode=='focused':
        config_values['collection_mode']='focused'
    if body.backup_enabled:
        config_values['backup_enabled']=True
    if body.family=='eastmoney_boards':config_values['board_codes']=body.board_codes
    if body.family=='tushare_dc_member':
        # Keep the submitted representation. Existing persisted board_codes and
        # their frozen job fingerprints are never migrated on a read or refresh.
        if body.sector_codes:config_values['sector_codes']=body.sector_codes
        if body.board_codes:config_values['board_codes']=body.board_codes
    values = dict(owner_id=owner.id, rights_basis=body.rights_basis, config=config_values,
                  interval_seconds=body.interval_seconds, configured_at=recorded, next_run_at=max(recorded, cooldown or recorded), paused_reason=None)
    if settings is None:
        settings = SourceSettings(source_id=ident, **values)
        db.add(settings)
    else:
        for name, value in values.items():
            setattr(settings, name, value)
    # Keep the original provider descriptor shape so historical snapshot hashes survive.
    descriptor = normalized(ProviderDTO.model_validate(provider))
    previous = db.scalar(select(SourceVersion).where(SourceVersion.source_id == ident).order_by(SourceVersion.recorded_at.desc()).limit(1))
    digest = content_hash(descriptor)
    if previous is None or previous.content_hash != digest:
        db.add(SourceVersion(source_id=ident, recorded_at=recorded, descriptor=descriptor, content_hash=digest))
    db.add(AuditEvent(actor_id=owner.id, action='source.configure', request_id=request_id, resource_type='provider', resource_id=ident,
                      changed_field_names=['configured','enabled','capabilities','rights','schedule']))
    db.flush()
    if body.family=='tencent_web' and prior_mode!=body.collection_mode:
        # Publish the enabled focus capability without making interest updates
        # or ordinary cadence changes invalidate all terminal reads.
        db.execute(select(func.bump_read_dependency('family:tencent_web')))
    invalidate(db)
    return provider


def permission_reason(provider):
    from app.provider_access import cloud_share_allowed
    if not cloud_share_allowed(provider):
        return 'permission_missing'
    if not provider.configured or not provider.enabled:
        return 'source_disabled'
    if provider.entitlement_status != 'verified' or provider.rights.get('display') != 'allowed' or provider.rights.get('cache') != 'allowed':
        return 'permission_missing'
    if provider.family in MINUTE_FAMILIES and any(provider.rights.get(key) == 'allowed' for key in ('export', 'redistribute')):
        return 'permission_missing'
    if provider.family in EASTMONEY_FAMILIES and any(provider.rights.get(key)=='allowed' for key in ('export','redistribute')):
        return 'permission_missing'
    if provider.family in PUBLIC_FAMILIES | DIRECTION_FAMILIES and any(provider.rights.get(key)=='allowed' for key in ('export','redistribute')):
        return 'permission_missing'
    if provider.family == BREADTH_HISTORY and any(provider.rights.get(key)!='denied' for key in ('export','redistribute')):
        return 'permission_missing'
    return None


def _live_run_summaries(db, source_ids, *, clock=None):
    """Read the same live history once for all configured sources.

    Repeating the historical-job exclusion twice per source can occupy every
    API database connection while several pages check their source health.
    The window sees all permitted runs before DISTINCT chooses the last one.
    """
    if not source_ids:
        return {}
    clock=clock if clock is not None else func.now()
    success = func.max(DataFetchRun.received_at).filter(
        DataFetchRun.status.in_(['complete', 'partial', 'empty'])).over(
            partition_by=DataFetchRun.source_id)
    rows = db.execute(select(DataFetchRun, success).where(
        DataFetchRun.source_id.in_(source_ids),DataFetchRun.received_at<=clock,live_run_condition()
    ).distinct(DataFetchRun.source_id).order_by(
        DataFetchRun.source_id, DataFetchRun.received_at.desc(), DataFetchRun.id.desc()))
    return {row.source_id: (row, succeeded_at) for row, succeeded_at in rows}


def source_states(db):
    from app.config import settings
    from app.news_center.models import NewsSourceCoverage
    from app.market.scheduling_waits import is_scheduling_wait
    cache_key = 'integrations.runtime_states'
    if cache_key in db.info:
        return db.info[cache_key]
    result = []
    clock = now(db)
    shared_cooldown = ashare_cooldown_until(db, clock)
    paid_cooldown = tushare_cooldown_until(db, clock)
    minute_cooldown = sina_cooldown_until(db, clock)
    public_cooldown = eastmoney_cooldown_until(db,clock)
    ths_cooldown=shared_deadline(db,clock,THS_FAMILIES)
    fuyao_cooldown=family_cooldown_until(db,'fuyao_quotes',clock)
    configured = db.execute(select(Provider, SourceSettings).join(
        SourceSettings, SourceSettings.source_id == Provider.id).order_by(Provider.family)).all()
    summaries = _live_run_summaries(db, [provider.id for provider, _ in configured],clock=clock)
    for provider, config in configured:
        last, success = summaries.get(provider.id, (None, None))
        denied = permission_reason(provider)
        state = 'disabled' if denied == 'source_disabled' else 'permission_missing' if denied else 'not_checked' if last is None else 'ready' if last.status == 'complete' else last.status
        if not denied and not config.paused_reason and is_scheduling_wait(last):
            state = 'waiting'
        bound = resolve_source_credentials(db, provider.id, provider.family)
        credential = bool(bound.credential) if bound.required else None
        if provider.family=='tencent_web' and config.config.get('collection_mode')=='focused' and not denied:
            from app.market.quote_focus import focused_selection
            if not focused_selection(db,config,clock):
                state='waiting_focus'
        if bound.reason == 'source_disabled':
            state = 'disabled'
        elif bound.reason:
            state = 'credentials_missing' if not bound.credential and bound.required else 'unavailable'
        own_cooldown = last.retry_not_before if last and last.retry_not_before and last.retry_not_before > clock else None
        cooldown = shared_cooldown if provider.family in ASHARE_FAMILIES else own_cooldown
        if provider.family in TUSHARE_FAMILIES:
            cooldown = paid_cooldown
        if provider.family in MINUTE_FAMILIES:
            cooldown = minute_cooldown
        if provider.family in EASTMONEY_FAMILIES:
            cooldown = public_cooldown
        if provider.family in THS_FAMILIES:
            cooldown=ths_cooldown
        if provider.family in FUYAO_FAMILIES:
            cooldown=fuyao_cooldown
        news_coverage = db.get(NewsSourceCoverage, provider.id) if provider.kind == 'news' else None
        result.append(RuntimeSource(provider=ProviderDTO.model_validate(provider), family=provider.family, credential_configured=credential, rights_basis=config.rights_basis, collection_enabled=settings().data_collection_enabled, paused_reason=config.paused_reason,
                                    news_coverage=news_coverage.coverage if news_coverage and not denied else None,
                                    symbols=config.config.get('symbols', []), interval_seconds=config.interval_seconds, configured_at=config.configured_at, next_run_at=max(config.next_run_at, cooldown or config.next_run_at),
                                    cooldown_until=cooldown, cooldown_scope=('supplier' if provider.family in ASHARE_FAMILIES | TUSHARE_FAMILIES | MINUTE_FAMILIES | EASTMONEY_FAMILIES | THS_FAMILIES | FUYAO_FAMILIES else 'source') if cooldown else None,
                                    last_attempt=fetch_run_dto(db,last) if last else None, last_success_at=success, collection_state=state,
                                    board_codes=config.config.get('board_codes',[]),
                                    points_enabled=config.config.get('points_enabled',False),collection_mode=config.config.get('collection_mode','selected'),
                                    backup_enabled=config.config.get('backup_enabled',False),
                                    sector_codes=(config.config.get('sector_codes') or [code+'.DC' for code in config.config.get('board_codes',[])])
                                        if provider.family=='tushare_dc_member' else config.config.get('sector_codes',[])))
    db.info[cache_key] = result
    return result


def source_index(db):
    from app.config import settings
    current = now(db).astimezone(SHANGHAI)
    start = datetime.combine(current.date(), time.min, SHANGHAI)
    count, requests, known_costs, total_cost = db.execute(select(func.count(), func.sum(DataFetchRun.request_count),
        func.count(DataFetchRun.cost_cny), func.sum(DataFetchRun.cost_cny)).where(
        DataFetchRun.received_at >= start, DataFetchRun.received_at < start + timedelta(days=1))).one()
    # Zero requests is measured; a missing bill (including no calls) is not zero cost.
    return SourceIndex(items=source_states(db), available_adapters=[SourceAdapter(
        family=value.family, name=value.name, kind=value.kind, version=value.version, capabilities=list(value.capabilities),
        minimum_interval=value.minimum_interval, references=list(value.references), news=news_metadata(value.family)) for value in SOURCES.values()],
        collection_enabled=settings().data_collection_enabled, today_request_count=requests or 0, today_run_count=count,
        today_cost_cny=format(total_cost, 'f') if count and known_costs == count else None)


def enqueue_collection(db, source, *, request_id=None, scheduled_intraday=False, scheduled_publication=False, scheduled_minute=False, scheduled_eastmoney=False, scheduled_realtime=False, scheduled_reference=False, day=None, integration_probe=None):
    from app.config import settings
    if not settings().data_collection_enabled:
        raise APIError(422, 'collection_disabled', '本机采集未启用。')
    config = db.get(SourceSettings, source)
    provider = db.get(Provider, source)
    if config is None or provider is None or provider.family not in SOURCES:
        raise APIError(404, 'not_found', '该来源尚未配置。')
    if provider.family in TDX_FAMILIES and config.config.get('points_enabled') is not True:
        raise APIError(422,'points_not_enabled','该能力会消耗通达信积分，请先在该能力明确启用积分使用。')
    if provider.family=='tdx_full_minute':
        from app.market.tdx_full_minute import enqueue_full_minute
        return enqueue_full_minute(db,source,day=day,request_id=request_id,integration_probe=integration_probe)
    if provider.family in FUYAO_FAMILIES | TDX_FAMILIES and integration_probe is None:
        from app.integrations.routing import collection_allowed
        if not collection_allowed(db,provider.family):
            raise APIError(422,'source_route_not_selected','请先检测并选择该项能力的数据来源。')
    bound = resolve_source_credentials(db, source, provider.family, allow_disabled=integration_probe is not None, refresh=True)
    if bound.reason:
        raise APIError(422, bound.reason, '请在数据中心核对接入开关与凭据；未使用旧凭据替代。')
    binding_context = job_context(bound, source, probe=integration_probe is not None)
    if integration_probe is not None and integration_probe != binding_context:
        raise APIError(409, 'configuration_changed', '检测连接或能力配置已变化，请刷新后重试。')
    reason = permission_reason(provider)
    if reason:
        raise APIError(422, reason, '来源尚未启用或使用范围未核实，未发起外部请求。')
    if config.paused_reason:
        raise APIError(422, 'source_paused', '该来源因权限或响应变化暂停，请先核对配置与最新调用记录。')
    if provider.family == BREADTH_HISTORY:
        from app.market.breadth_history import enqueue_breadth_archive
        return enqueue_breadth_archive(db, provider, config, day, request_id=request_id,
            integration_probe=integration_probe)
    if provider.family in PUBLIC_FAMILIES:
        from app.market.review_jobs import enqueue_public_source
        return enqueue_public_source(db, provider, config, day, integration_probe=integration_probe)
    current = now(db)
    context = None
    reference_context = None
    if day is not None:
        from app.market.protocol import SourceFailure
        try:
            if provider.family in DATED_FAMILIES:
                from app.market.terminal_dates import make_dated_context
                context = make_dated_context(db,provider,config,day,current)
            else:
                from app.market.reference_backfill import FAMILIES as REFERENCE_BACKFILL, make_context as reference_context_for
                if provider.family in REFERENCE_BACKFILL:
                    reference_context = reference_context_for(db,provider,config,day,current)
                else:
                    context = make_context(db,provider,config,day,current)
        except SourceFailure as exc:
            raise APIError(422,exc.reason,'目标日期、来源权限或交易日历尚未满足补采条件；未发起请求。') from None
    lock(db, f'source:queue:{source}')
    current=now(db)
    from app.market.realtime_schedule import FAMILIES as REALTIME_FAMILIES,automatic
    explicit_realtime=(provider.family in REALTIME_FAMILIES and day is None and not any((
        scheduled_intraday,scheduled_publication,scheduled_minute,scheduled_eastmoney,scheduled_realtime,scheduled_reference)))
    pending_jobs=db.scalars(select(Job).where(Job.kind == 'source.collect', Job.payload['source_id'].astext == str(source), Job.state.in_(['queued','running'])).order_by(Job.created_at))
    for pending in pending_jobs:
        if pending.payload.get('integration_context') != binding_context:
            raise APIError(409, 'collection_intent_conflict', '该来源已有不同接入配置或检测方式的任务，请等待其结束。')
        # Old v2 reference-context tasks remain immutable and collectible. They
        # must not be silently reused as a newly requested dated/live intent.
        if (reference_context is not None or 'reference_context' in pending.payload) and pending.payload.get('reference_context') != reference_context:
            raise APIError(409,'collection_intent_conflict','该来源已有不同日期、模式或配置的资料采集任务，请等待其结束。')
        if provider.family in DATED_FAMILIES and pending.payload.get('dated_context') != context:
            raise APIError(409,'collection_intent_conflict','该来源已有不同日期或配置的采集任务，请等待其结束。')
        if provider.family in MINUTE_FAMILIES and pending.payload.get('minute_context') != context:
            raise APIError(409,'collection_intent_conflict','该来源已有不同日期、模式或配置的采集任务，请等待其结束。')
        if historical_job(pending.payload):
            raise APIError(409,'collection_intent_conflict','该来源已有所选标的历史任务，请等待其结束。')
        # Keep the old automatic intent and its future session wake immutable.
        # A separate explicit request still needs real spacing and cooldown.
        if (explicit_realtime and pending.state=='queued' and pending.available_at>current
                and pending.error_code in {'intraday_window_wait','calendar_not_ready'} and automatic(pending.payload)):
            continue
        return pending
    current = now(db)
    cooldown=family_cooldown_until(db,provider.family,current)
    if cooldown is not None:
        raise APIError(429, 'source_rate_limit', '供应商尚在限流冷却中，请在数据中心查看期限与已保存数据。')
    last_run = db.scalar(select(DataFetchRun).where(DataFetchRun.source_id == source,DataFetchRun.received_at<=current,live_run_condition())
        .order_by(DataFetchRun.received_at.desc(), DataFetchRun.id.desc()).limit(1))
    last = last_run.received_at if last_run else None
    if last and reference_context is None and not (day is not None and provider.family in DATED_FAMILIES):
        from app.market.auction_schedule import collection_spacing
        spacing = collection_spacing(provider.family,current,config.interval_seconds)
        if provider.family=='tushare_ths_member' and integration_probe is not None:
            # A failed initial schema probe must be repairable today. Keep
            # supplier cooldown/physical request budgets above and in collect;
            # successful captures retain the normal six-hour refresh spacing.
            latest_probe=db.scalar(select(DataFetchRun).where(DataFetchRun.source_id==source)
                .order_by(DataFetchRun.received_at.desc(),DataFetchRun.id.desc()).limit(1))
            if latest_probe and latest_probe.status=='error' and latest_probe.reason in {'instrument_mismatch','schema_changed','pagination_overlap'}:
                spacing=min(spacing,60)
        if scheduled_realtime:
            latest_reason = db.scalar(select(DataFetchRun.reason).where(DataFetchRun.source_id == source,
                live_run_condition()).order_by(DataFetchRun.received_at.desc(), DataFetchRun.id.desc()).limit(1))
            if provider.family not in EASTMONEY_FAMILIES and latest_reason in {'timeout', 'transport_error', 'upstream_unavailable', 'rate_limited'}:
                from app.market.realtime_schedule import RETRY_SECONDS
                # After the fifth attempt a new durable task continues the
                # capped retry, rather than adding another normal interval.
                # Shared permits and Retry-After above remain mandatory.
                spacing = min(spacing, RETRY_SECONDS[-1])
        if scheduled_publication:
            from app.market.publication_schedule import publication_spacing
            latest = db.scalar(select(DataFetchRun).where(DataFetchRun.source_id == source,
                live_run_condition()).order_by(DataFetchRun.received_at.desc()).limit(1))
            unpublished = (latest is not None and latest.reason == 'daily_data_not_published'
                and latest.received_at.astimezone(SHANGHAI).date() == current.astimezone(SHANGHAI).date())
            spacing = publication_spacing(provider.family, config.interval_seconds, unpublished=unpublished)
        from app.market.collection_cadence import run_anchor
        anchor = run_anchor(db, provider.family, last_run) if scheduled_realtime else None
        from app.market.cadence_policy import effective_interval
        earliest = (anchor or last) + timedelta(seconds=effective_interval(provider.family, spacing) if anchor else spacing)
        # A future automatic trading-window wake is not a supplier cooldown.
        # Explicit requests retain actual spacing and the shared cooldown above.
        probe = binding_context is not None and binding_context.get('probe') is True
        from app.market.reference_schedule import FAMILIES as STATIC_REFERENCES
        explicit_reference = provider.family in STATIC_REFERENCES and not scheduled_reference
        if not probe and not explicit_reference and not explicit_realtime and (provider.family not in {'tushare_rt_k','tushare_market_rt','tushare_rt_indices','tushare_market_limits', *MINUTE_FAMILIES} or scheduled_intraday or scheduled_publication or scheduled_minute):
            earliest = max(earliest, config.next_run_at)
        if current < earliest:
            raise APIError(429, 'source_rate_limit', '尚在该来源的刷新间隔内，请稍后查看已保存数据。')
    job = Job(owner_id=config.owner_id, kind='source.collect', payload={'source_id':str(source)})
    if scheduled_realtime:
        job.payload = {**job.payload, 'scheduled_realtime': True}
        # Carry the original due time into the queue. A delayed scheduler must
        # not reset an overdue feed's deadline when it finally enqueues it.
        job.available_at = min(current, config.next_run_at)
    if binding_context is not None:
        job.payload = {**job.payload, 'integration_context': binding_context}
    if context is not None:
        job.payload = {**job.payload, 'dated_context' if provider.family in DATED_FAMILIES else 'minute_context': context}
    if reference_context is not None:
        job.payload = {**job.payload,'reference_context':reference_context}
    if scheduled_intraday and provider.family in {'tushare_rt_k','tushare_market_rt','tushare_rt_indices'}:
        job.payload = {**job.payload, 'scheduled_intraday': True}
    if scheduled_minute and provider.family in MINUTE_FAMILIES:
        job.payload = {**job.payload, 'scheduled_minute': True}
    if scheduled_publication:
        job.payload = {**job.payload, 'scheduled_publication': True}
    if scheduled_eastmoney and provider.family in EASTMONEY_FAMILIES:
        job.payload={**job.payload,'scheduled_eastmoney':True}
    if scheduled_reference:
        job.payload={**job.payload,'scheduled_reference':True}
    db.add(job); db.flush()
    if not is_backfill(job.payload) and 'dated_context' not in job.payload and reference_context is None:
        config.next_run_at = (next_minute(provider.family, current, config.interval_seconds)
                              if provider.family in MINUTE_FAMILIES else next_collection(provider.family, current, config.interval_seconds))
    db.add(AuditEvent(actor_id=config.owner_id, action='source.refresh', request_id=request_id, resource_type='job', resource_id=job.id, changed_field_names=['queued']))
    return job


def _direction_probe_deadline(db, provider, config, clock):
    """An old pause and a new process share the same persisted probe spacing."""
    from app.market.kpl_order_recovery import interval_seconds,latest_diagnostic
    spacing=interval_seconds(provider.family,config.interval_seconds,latest_diagnostic(db,provider,clock))
    last_permit = db.scalar(select(func.max(AuditEvent.created_at)).where(
        AuditEvent.action == 'source.request.direction-probe', AuditEvent.resource_type == 'provider',
        AuditEvent.resource_id == provider.id))
    last_attempt = db.scalar(select(func.max(DataFetchRun.received_at)).where(
        DataFetchRun.source_id == provider.id, DataFetchRun.request_count > 0,
        DataFetchRun.received_at <= clock))
    latest = max((stamp for stamp in (last_permit, last_attempt) if stamp is not None), default=None)
    if latest is not None and latest + timedelta(seconds=spacing) > clock:
        if provider.family in FAST_DIRECTION_RECOVERY:
            from app.market.realtime_schedule import align_realtime
            return align_realtime(provider.family,latest+timedelta(seconds=spacing))
        from app.market.intraday_schedule import align_intraday
        return align_intraday(latest + timedelta(seconds=spacing))
    return None


def permit_direction_probe(job, *, expected_configuration=None, frozen_binding=None):
    """Fence only automatic schema probes immediately before their one HTTP.

    Waiting returns the lease/attempt without inventing a failed fetch or a
    consumed request. Ordinary explicit tasks do not gain an automatic marker.
    """
    if job.payload.get('schema_probe') is not True:
        return True
    from app.integrations.credentials import assert_source_binding
    from app.market.intraday_schedule import deferred_intraday
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.protocol import SourceFailure
    with Session(engine()) as db, db.begin():
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running',
            Job.lease_token == job.lease_token).with_for_update())
        if current is None:
            return False
        source = uuid.UUID(current.payload['source_id'])
        provider = db.get(Provider, source)
        config = db.get(SourceSettings, source)
        if provider is None or config is None:
            raise SourceFailure('source_not_configured')
        if provider.family not in DIRECTION_RECOVERY | FAST_DIRECTION_RECOVERY:
            return True
        lock(db, 'source:direction-schema:' + str(source))
        clock = now(db)
        if current.lease_until is None or current.lease_until <= clock:
            return False
        if current.payload != job.payload or current.owner_id != config.owner_id:
            raise SourceFailure('configuration_changed')
        denied = permission_reason(provider)
        if denied:
            raise SourceFailure(denied)
        if config.paused_reason not in {None, 'schema_changed'}:
            raise SourceFailure('source_paused')
        if (expected_configuration is not None
                and configuration_fingerprint(provider, config) != expected_configuration):
            raise SourceFailure('configuration_changed')
        assert_source_binding(db, source, provider.family, current.payload, frozen=frozen_binding)
        if provider.family in FAST_DIRECTION_RECOVERY:
            from app.market.realtime_schedule import deferred_realtime
            deadline=deferred_realtime(db,provider.family,clock,max(900,config.interval_seconds))
        else:
            deadline = deferred_intraday(db, clock, max(900, config.interval_seconds), [], batch=True)
        interval_deadline = _direction_probe_deadline(db, provider, config, clock)
        if interval_deadline is not None:
            deadline = max(deadline or clock, interval_deadline)
        if deadline is not None:
            current.state, current.available_at = 'queued', deadline
            current.attempts = max(0, current.attempts - 1)
            current.error_code = 'intraday_window_wait' if interval_deadline is None else 'source_budget_wait'
            current.lease_token = current.lease_until = None
            config.next_run_at = max(config.next_run_at, deadline)
            return False
        db.add(AuditEvent(actor_id=current.owner_id, action='source.request.direction-probe',
            resource_type='provider', resource_id=source,
            changed_field_names=['strict_schema_probe_permit'], created_at=clock))
    return True


def schedule_sources(*, families=None):
    if families is not None:
        from app.market.cadence_policy import FAST_FEED_FAMILIES
        if not set(families) <= FAST_FEED_FAMILIES:
            raise ValueError('The lightweight dispatcher only serves registered short feeds')
    from app.config import settings
    if not settings().data_collection_enabled:
        return
    with Session(engine()) as db, db.begin():
        require_instance(db)
        from app.maintenance_control import admit, paused
        if paused(db):
            return
        admit(db)
        clock = now(db)
        from app.market.publication_schedule import repair_unpublished_waits
        if families is None:
            repair_unpublished_waits(db, clock)
        from app.market.realtime_schedule import wake_session_waits, service_grace
        if families is None:
            wake_session_waits(db, clock)
        db.flush()
        grace = case(*[(Provider.family == family, timedelta(seconds=service_grace(family)))
                       for family in ('tencent_web','direction_kpl_top','duanxianxia_auction', 'tushare_calendar_sse', 'tushare_calendar_szse',
                                      'eastmoney_funds', 'eastmoney_boards', 'mairui_funds')],
                     else_=timedelta(seconds=15))
        due = list(db.scalars(select(SourceSettings).join(Provider, Provider.id == SourceSettings.source_id).where(
            True if families is None else Provider.family.in_(families),
            SourceSettings.next_run_at <= clock,
            ~exists(select(Job.id).where(Job.kind == 'source.collect',
                Job.payload['source_id'].astext == cast(SourceSettings.source_id, String),
                Job.state.in_(['queued', 'running']))),
            Provider.family != BREADTH_HISTORY,
            or_(SourceSettings.paused_reason.is_(None),
                (Provider.family.in_(SCHEMA_RECOVERY)) & (SourceSettings.paused_reason == 'schema_changed'),
                (Provider.family.in_(RANKING_RECOVERY)) & SourceSettings.paused_reason.in_({'schema_changed','upstream_redirect'})),
            Provider.enabled.is_(True)).order_by(SourceSettings.next_run_at + grace,
                SourceSettings.next_run_at, SourceSettings.source_id).limit(5)))
        for config in due:
            try:
                provider = db.get(Provider, config.source_id)
                if provider.family=='tencent_web' and config.config.get('collection_mode')=='focused':
                    from app.market.quote_focus import focused_selection
                    if not focused_selection(db,config,clock):
                        config.next_run_at=next_collection(provider.family,clock,config.interval_seconds)
                        continue
                from app.market.budget import family_cooldown_until
                cooldown = family_cooldown_until(db, provider.family, clock)
                if cooldown is not None:
                    # Restore the supplier wait before checking calendars or
                    # routes: a local five-second recheck cannot shorten it.
                    config.next_run_at = max(config.next_run_at, cooldown)
                    continue
                from app.integrations.routing import collection_allowed
                if provider.family=='tdx_full_minute' or not collection_allowed(db,provider.family):
                    config.next_run_at=clock+timedelta(seconds=max(300,config.interval_seconds))
                    continue
                if provider.family in TDX_FAMILIES and config.config.get('points_enabled') is not True:
                    config.next_run_at=clock+timedelta(seconds=max(300,config.interval_seconds))
                    continue
                from app.market.reference_schedule import FAMILIES as STATIC_REFERENCES, deferred_reference
                deadline = deferred_reference(provider.family, clock)
                from app.market.bulk_quote_queue import deferred_background
                deadline = deadline or deferred_background(db,provider.family,clock)
                if deadline is not None:
                    config.next_run_at = max(config.next_run_at, deadline)
                    continue
                bound = resolve_source_credentials(db, provider.id, provider.family, refresh=True)
                if bound.reason:
                    raise APIError(422, 'source_disabled' if bound.reason == 'source_disabled' else 'credentials_missing', '数据接入尚不可采集。')
                from app.market.realtime_schedule import FAMILIES as REALTIME, deferred_realtime
                if provider.family in REALTIME:
                    deadline = deferred_realtime(db, provider.family, clock, config.interval_seconds)
                    if deadline is not None:
                        config.next_run_at = max(config.next_run_at, deadline)
                        continue
                if provider.family in MINUTE_FAMILIES:
                    deadline = deferred_minute(db, provider.family, clock, config.interval_seconds, config.config.get('symbols', []))
                    if deadline is not None:
                        config.next_run_at = max(config.next_run_at, deadline)
                        continue
                from app.market.publication_schedule import WINDOWS as PUBLICATION_WINDOWS, deferred_publication
                if provider.family in PUBLICATION_WINDOWS:
                    deadline=deferred_publication(db,provider.family,clock,config.interval_seconds)
                    if deadline is not None:
                        config.next_run_at=max(config.next_run_at,deadline)
                        continue
                from app.market.intraday_schedule import FAMILIES as LIVE, deferred_intraday
                if provider.family in EASTMONEY_FAMILIES | (DIRECTION_FAMILIES - {'direction_ths_hot', 'direction_tdx_hot', 'direction_em_hot'}):
                    deadline=deferred_intraday(db,clock,config.interval_seconds,[],batch=True)
                    if deadline is not None:
                        config.next_run_at=max(config.next_run_at,deadline)
                        continue
                if provider.family in LIVE:
                    deadline = deferred_intraday(db, clock, config.interval_seconds, config.config.get('symbols', []), batch=provider.family in {'tushare_market_rt','tushare_rt_indices'})
                    if deadline is not None:
                        config.next_run_at = max(config.next_run_at, deadline)
                        continue
                if schema_recheck(provider.family, config.paused_reason):
                    # Keep the pause visible until an entire response validates.
                    # Persist the next probe before dispatch; restart cannot loop.
                    if permission_reason(provider):
                        config.next_run_at = next_collection(provider.family, clock, config.interval_seconds, schema_probe=True)
                        continue
                    if provider.family == BREADTH:
                        from app.market.breadth_recovery import allowed
                        if not allowed(db, clock):
                            config.next_run_at = next_collection(provider.family, clock, config.interval_seconds, schema_probe=True)
                            continue
                    if provider.family in DIRECTION_RECOVERY | FAST_DIRECTION_RECOVERY:
                        deadline = _direction_probe_deadline(db, provider, config, clock)
                        if deadline is not None:
                            config.next_run_at = max(config.next_run_at, deadline)
                            continue
                    lock(db, f'source:queue:{config.source_id}')
                    pending = db.scalar(select(Job.id).where(Job.kind == 'source.collect',
                        Job.payload['source_id'].astext == str(config.source_id), Job.state.in_(['queued', 'running'])).limit(1))
                    if not pending:
                        payload = {'source_id': str(config.source_id), 'schema_probe': True}
                        binding_context = job_context(bound, config.source_id)
                        if binding_context is not None:
                            payload['integration_context'] = binding_context
                        db.add(Job(owner_id=config.owner_id, kind='source.collect',
                            payload=payload, available_at=min(clock, config.next_run_at)))
                    from app.market.kpl_order_recovery import latest_diagnostic
                    config.next_run_at = next_collection(provider.family, clock, config.interval_seconds, schema_probe=True,
                        probe_diagnostic=latest_diagnostic(db,provider,clock))
                else:
                    # Mark only newly created automatic tasks. Returning an
                    # existing explicit task must not silently relabel it.
                    enqueue_collection(db, config.source_id, scheduled_intraday=provider.family in LIVE,
                        scheduled_publication=provider.family in PUBLICATION_WINDOWS,
                        scheduled_minute=provider.family in MINUTE_FAMILIES,
                        scheduled_eastmoney=provider.family in EASTMONEY_FAMILIES,
                        scheduled_realtime=provider.family in REALTIME,
                        scheduled_reference=provider.family in STATIC_REFERENCES | {'baostock_daily','baostock_history','baostock_fundamental','baostock_classification'})
            except APIError as error:
                if error.code == 'collection_intent_conflict':
                    # A manual date-bound job is already pending. Do not roll
                    # back unrelated queues, postpone its plan or prevent the
                    # worker loop from claiming that very job after scheduling.
                    continue
                if error.code not in {'source_disabled','permission_missing','source_rate_limit','source_paused','credentials_missing','calendar_not_ready'}:
                    raise
                # A refused/limited source must not be reconsidered on each worker tick.
                current = now(db)
                cooldown = ashare_cooldown_until(db, current) if db.get(Provider, config.source_id).family in ASHARE_FAMILIES else None
                if db.get(Provider, config.source_id).family in MINUTE_FAMILIES:
                    cooldown = sina_cooldown_until(db, current)
                config.next_run_at = max(current + timedelta(seconds=config.interval_seconds), cooldown or current)
