"""Capability-level source choices, costs, and account-specific evidence.

The catalogue is descriptive. Only explicit, tested choices change collection.
Reading it never probes a vendor, writes a credential, or enables a job.
"""
from uuid import UUID

from sqlalchemy import select, text

from app.auth import APIError
from app.evidence.common import content_hash, lock, now
from app.integrations.routing_models import DataRoutes
from app.market.models import DataFetchRun, SourceSettings
from app.models import AuditEvent, Job, Provider

CHECKED = '2026-10-25'
FUYAO = 'https://fuyao.aicubes.cn/docs/'
TDX = 'https://help.tdx.com.cn/tdxclaw/docs/markdown/buy_points.html'
TICKFLOW = 'https://tickflow.org/pricing/'

# These are compatible consumer contracts, not vendor display-name matching.
# Financial/company-action rows retain their source namespace and semantics.
ROUTES = {
    'stock_quote': ('股票行情', '行情与图表', '按标的与行情时点选择完整报价，实际来源随数据返回。', {
        'fuyao': ('fuyao_quotes',), 'tdx_data': ('tdx_quotes',),
        'tencent': ('tencent_market_rt','tencent_web'), 'tickflow': ('tickflow_live',),
        'tushare': ('tushare_market_rt', 'tushare_rt_k'), 'ashare': ('ashare_public',)}),
    'index_quote': ('指数行情', '行情与图表', '四个固定指数独立于股票选源；不会用同号股票代替指数。', {
        'fuyao': ('fuyao_indices',), 'tencent': ('tencent_web',),
        'tickflow': ('tickflow_live',), 'tushare': ('tushare_rt_indices',), 'ashare': ('ashare_public',)}),
    'stock_daily': ('股票日K', '行情与图表', '按股票、日期和复权口径读取；不同来源不拼接同一根K线。', {
        'fuyao': ('fuyao_daily',), 'tushare': ('tushare_daily',), 'baostock': ('baostock_history',), 'tickflow': ()}),
    'index_daily': ('指数日K', '行情与图表', '仅已配置指数日线；股票日线不能作为替代。', {
        'fuyao': ('fuyao_index_daily',), 'tushare': ('tushare_index_daily',), 'baostock': ('baostock_history',), 'tickflow': ()}),
    'depth5': ('五档盘口', '行情与图表', '五档买卖价格与数量快照，保留未知时点；不等于逐笔委托或L2。', {
        'tdx_data': ('tdx_depth5',), 'tickflow': ()}),
    'full_minute': ('全市场分钟补齐', '行情与图表', '按需分段获取本地股票目录的当日1分钟数据；不自动启动，非每分钟全市场同步。', {
        'tdx_data': ('tdx_full_minute',), 'tickflow': ()}),
    'adjustment': ('原始除权事件', '财务与参考', '原始除权事件单独读取；累计复权因子保留原来源配置，二者不可互换。', {
        'fuyao': ('fuyao_adj_factor',), 'tickflow': ()}),
    'financial': ('财务报表', '财务与参考', '保留报告期、公告日、表型和源字段范围；原始参考表优先展示，不跨源拼接或自动替换研究计算。', {
        'fuyao': ('fuyao_financial',), 'tushare': ('tushare_income', 'tushare_balancesheet', 'tushare_cashflow'),
        'tickflow': ()}),
}
for _period in ('1m', '5m', '15m', '30m', '60m'):
    ROUTES['stock_minute_' + _period] = (
        _period[:-1] + '分钟K', '行情与图表', '按已配置股票和原生周期取数，缺失时段保留缺口。',
        {'sina': ('sina_minute_' + _period,), 'tickflow': (),
         **({'tdx_data': ('tdx_minute',)} if _period == '1m' else {})})

NEW_TEMPLATES = {'fuyao', 'tdx_data'}
AUTH_FAILURES = {'credentials_missing', 'credentials_invalid', 'credentials_rejected', 'unauthorized', 'authentication_failed',
                 'permission_missing', 'permission_denied', 'forbidden', 'tdx_data_permission_missing', 'tdx_data_key_required'}
BILL_LABELS = {'free': '免费', 'free_quota': '免费额度', 'points': '积分', 'subscription': '付费套餐', 'unknown': '费用待确认'}


def _present(db):
    if 'routing.table_present' not in db.info:
        db.info['routing.table_present'] = db.scalar(text("SELECT to_regclass('data_routes')")) is not None
    return db.info['routing.table_present']


def route_settings(db):
    if not _present(db):
        return None
    if 'routing.settings' not in db.info:
        db.info['routing.settings'] = db.get(DataRoutes, 1)
    return db.info['routing.settings']


def invalidate(db):
    from app.integrations.credentials import invalidate as invalidate_connections
    invalidate_connections(db)
    for key in list(db.info):
        if isinstance(key,str) and key.startswith('routing.') and key != 'routing.table_present':
            db.info.pop(key, None)


def _definitions():
    from app.integrations.data import _definitions as supplier_definitions
    return supplier_definitions()


def provider_for_family(family):
    return next((key for key, (_, _, families, _) in _definitions().items() if family in families), None)


def families_for(capability_id, provider_id):
    from app.market.registry import SOURCES
    route = ROUTES.get(capability_id)
    return [family for family in route[3].get(provider_id, ()) if family in SOURCES] if route else []


def billing(provider_id, capability_id):
    if provider_id == 'tdx_data':
        return 'points', 'MCP与数据服务均消耗AI积分；赠送积分同样计量，实际扣减以商城明细为准。', TDX
    if provider_id == 'fuyao':
        return 'free', '当前免费开放；需本人Key和对应接口权限，存在动态限流，以平台当前规则为准。', FUYAO
    if provider_id == 'tickflow':
        kind = 'free_quota' if capability_id in {'stock_quote', 'index_quote', 'stock_daily', 'index_daily'} else 'subscription'
        return kind, '免费注册档10次/分钟、每次最多5标的；高级能力需对应套餐，本系统适配与账号解锁分别核验。', TICKFLOW
    if provider_id == 'tushare':
        return 'subscription', '使用已购服务权益；逐接口权限和额度按本人套餐，未取得账单时费用金额未知。', 'https://tushare.pro/document/1?doc_id=290'
    if provider_id in {'tencent', 'sina', 'baostock', 'eastmoney', 'ashare', 'mairui', 'duanxianxia'}:
        kind = 'free_quota' if provider_id in {'ashare', 'mairui'} else 'free'
        return kind, '当前接入的公开或免费能力；受范围、限流与来源使用条件约束，不代表该供应商全部接口免费。', {
            'tencent': 'https://gu.qq.com/', 'sina': 'https://finance.sina.com.cn/', 'baostock': 'https://www.baostock.com/',
            'eastmoney': 'https://quote.eastmoney.com/', 'ashare': 'https://ashareapi.com/pricing/',
            'mairui': 'https://www.mairuiapi.com/getlicence', 'duanxianxia': 'https://duanxianxia.com/'}[provider_id]
    definitions = _definitions()
    return 'free', '公开发布的已接入资料；内容范围及使用条件按原来源登记。', definitions.get(provider_id, ('', '', (), ''))[3]


def _connection_rows(db):
    from app.integrations.credentials import connections
    from app.integrations.data import virtual_connections
    current = list(connections(db))
    rows = {key: next((row for row in current if row.template == key and not row.archived), None) for key in _definitions()}
    virtual = {row['template']: row for row in virtual_connections(db)}
    return rows, virtual


def _signature(config, connection):
    values = dict(config.config) if config else {}
    values.pop('points_enabled', None)
    return content_hash([values, connection.endpoint if connection else None,
                         connection.secret_ref if connection else None, connection.options if connection else None])


def _source_connection(db, source_id):
    """Follow the credential resolver's source ownership, including tombstones."""
    from app.integrations.credentials import connections
    matches = [row for row in connections(db) if str(source_id) in row.source_ids]
    active = [row for row in matches if not row.archived]
    if len(active) > 1:
        return None, False
    if active:
        return active[0], True
    return (matches[0], False) if matches else (None, True)


def _current_source_success(db, source, config, success, choice):
    connection, binding_valid = _source_connection(db, source.id)
    success_job = db.get(Job, success.job_id) if success and success.job_id else None
    # Configuration and job creation share the database clock; vendor receipts
    # are not a replacement for that configuration boundary.
    proof_created_at = success_job.created_at if success_job else success.received_at if success else None
    if not binding_valid or proof_created_at is None or proof_created_at < config.configured_at:
        return False, connection
    context = success_job.payload.get('integration_context') if success_job else None
    if connection is None:
        # Only never-bound sources retain the legacy environment path. A stored
        # task context cannot silently become an environment-account proof.
        return context is None, None
    proof = connection.last_test if isinstance(connection.last_test, dict) else {}
    preserved = choice.get('checks', {}).get(str(source.id), {})
    current = (isinstance(context, dict)
        and context.get('connection_id') == str(connection.id)
        and context.get('source_id') == str(source.id)
        and context.get('revision') == connection.revision)
    current = (current
        or (proof.get('status') == 'success' and proof.get('revision') == connection.revision
            and proof.get('source_id') == str(source.id))
        or preserved.get('signature') == _signature(config, connection))
    return current, connection


def _source_snapshot(db):
    if 'routing.source_snapshot' in db.info:
        return db.info['routing.source_snapshot']
    from app.market.service import source_states
    states = {row.family: row for row in source_states(db)}
    configs = {row.source_id: row for row in db.scalars(select(SourceSettings))}
    rows = {row.family: row for row in db.scalars(select(Provider).where(Provider.family.is_not(None)))}
    latest = {row.source_id: row for row in db.scalars(select(DataFetchRun)
              .distinct(DataFetchRun.source_id).order_by(DataFetchRun.source_id, DataFetchRun.received_at.desc(), DataFetchRun.id.desc()))}
    successes = {row.source_id: row for row in db.scalars(select(DataFetchRun).where(
        DataFetchRun.status.in_(['complete', 'partial']),
        (DataFetchRun.accepted > 0) | (DataFetchRun.duplicates > 0))
        .distinct(DataFetchRun.source_id).order_by(DataFetchRun.source_id, DataFetchRun.received_at.desc(), DataFetchRun.id.desc()))}
    result = (states, configs, rows, latest, successes)
    db.info['routing.source_snapshot'] = result
    return result


def _option(db, provider_id, capability_id, name, families, choice):
    states, configs, providers, latest, successes = _source_snapshot(db)
    ids, valid, failures, clocks, scope_items, active, source_switches, attempts = [], [], [], [], [], [], [], []
    for family in families:
        source = providers.get(family)
        if source is None or source.id not in configs:
            continue
        config = configs[source.id]
        symbols = config.config.get('symbols', [])
        desired_asset = 'index' if capability_id in {'index_quote', 'index_daily'} else 'stock' if capability_id in {
            'stock_quote', 'stock_daily', 'adjustment', 'financial', 'depth5', 'full_minute',
            *['stock_minute_' + period for period in ('1m', '5m', '15m', '30m', '60m')]} else None
        if symbols and desired_asset and not any(symbol.get('asset_type') == desired_asset for symbol in symbols):
            continue
        ids.append(str(source.id))
        source_switches.append(source.enabled)
        state = states.get(family)
        run, success = latest.get(source.id), successes.get(source.id)
        if run:
            attempts.append(run)
        if run and run.reason:
            failures.append(run.reason)
        current_success, _ = _current_source_success(db, source, config, success, choice)
        if current_success and source.entitlement_status == 'verified' and all(source.rights.get(key) == 'allowed' for key in ('display', 'cache')):
            valid.append(str(source.id))
            clocks.append(success.received_at.isoformat())
        if state:
            active.append(state.collection_state)
        if symbols:
            scope_items.extend(symbol['exchange'] + ':' + symbol['symbol'] for symbol in symbols[:5])
        if family=='fuyao_quotes' and config.config.get('collection_mode')=='full_market':
            scope_items.append('全市场接口实际返回的价格快照；逐股行情日期和时刻待核')
        if family in {'fuyao_quotes','fuyao_indices'} and config.config.get('backup_enabled'):
            scope_items.append('已启用自动备用，主来源正常时保持原优先级')
    kind, note, reference = billing(provider_id, capability_id)
    declared = bool(families)
    auth_failure = next((reason for reason in failures if reason in AUTH_FAILURES), None)
    entitlement = 'locked' if auth_failure else 'verified' if valid else 'unknown'
    state = 'not_adapted' if not declared else 'not_configured' if not ids else 'not_checked'
    if valid:
        state = 'ready' if all(value in {'ready', 'disabled', 'not_checked'} for value in active) else active[0] if active else 'ready'
    elif active:
        state = next((value for value in active if value not in {'disabled', 'not_checked'}), active[0])
    if auth_failure:
        state = 'permission_missing'
    if source_switches and not any(source_switches):
        state = 'disabled'
    if valid and capability_id in {'stock_quote', 'index_quote'} and any(
            reason in {'snapshot_trade_date_unverified', 'quote_time_unknown'} for reason in failures):
        state = 'time_unverified'
    if provider_id == 'tdx_data' and not any(configs[UUID(ident)].config.get('points_enabled') for ident in ids):
        state = 'points_not_enabled'
    scope = '、'.join(dict.fromkeys(scope_items)) or ('按当前配置范围' if ids else '连接后沿用已有标的范围')
    if capability_id == 'full_minute':
        scope = '本地已知股票目录；手动分段补齐，每段有独立进度和请求上限'
    if attempts:
        attempt = max(attempts, key=lambda item: item.received_at)
        scope += f'；该来源最近批次：存入{attempt.accepted or 0}、重复{attempt.duplicates or 0}、拒绝{attempt.rejected or 0}'
    return dict(provider_id=provider_id, capability_id=capability_id, name=name,
        billing=kind, billing_label=BILL_LABELS[kind], billing_note=note, reference_url=reference, checked_at=CHECKED,
        entitlement=entitlement, implemented=declared, state=state, last_success_at=max(clocks) if clocks else None,
        source_ids=ids, selected=choice.get('provider_id') == provider_id,
        reason='source_disabled' if source_switches and not any(source_switches) else auth_failure or (failures[0] if failures else None), scope=scope)


def matrix(db):
    settings = route_settings(db)
    choices = settings.choices if settings else {}
    definitions = _definitions()
    connections, virtuals = _connection_rows(db)
    suppliers = {}
    for key, (name, kind, _, endpoint) in definitions.items():
        row, virtual = connections.get(key), virtuals.get(key)
        credential = bool(row.secret_ref) if row else bool(virtual and virtual.get('credential_configured'))
        suppliers[key] = dict(id=key, name={'fuyao': '同花顺 API', 'tdx_data': '通达信'}.get(key, name),
            template=key, kind=kind, endpoint=endpoint, connection_id=str(row.id) if row else str(virtual['id']) if virtual else None,
            credential_required=key in {'fuyao', 'tdx_data', 'tushare', 'tickflow', 'mairui'},
            credential_configured=credential, enabled=bool(row.enabled) if row else bool(virtual and virtual['enabled']),
            serving_count=0, capabilities=[])
    capabilities, covered = [], set()
    for ident, (name, group, description, mapping) in ROUTES.items():
        options = []
        choice = choices.get(ident, {})
        for key, families in mapping.items():
            if key not in suppliers:
                continue
            option = _option(db, key, ident, name, families, choice)
            options.append(option)
            covered.update(families)
        preferred = sorted(options, key=lambda item: (
            not suppliers[item['provider_id']]['enabled'], item['entitlement'] != 'verified',
            item['provider_id']!='tushare',
            {'free': 0, 'free_quota': 1, 'subscription': 2, 'points': 3, 'unknown': 4}[item['billing']]))
        unusable = AUTH_FAILURES | {'error', 'failed', 'unavailable', 'disabled', 'points_not_enabled', 'time_unverified'}
        default = next((item['provider_id'] for item in preferred if item['entitlement'] == 'verified'
            and item['state'] not in unusable and suppliers[item['provider_id']]['enabled']), None)
        current = choice.get('provider_id') or default
        effective = current if any(item['provider_id'] == current and item['entitlement'] == 'verified' and item['state'] not in unusable for item in options) else default
        for option in options:
            option['selected'] = option['provider_id'] == current
            suppliers[option['provider_id']]['capabilities'].append(option)
        if current in suppliers:
            suppliers[current]['serving_count'] += 1
        capabilities.append(dict(id=ident, name=name, group=group, description=description, routable=True,
            current_provider_id=current, effective_provider_id=effective, options=options))
    from app.market.registry import SOURCES
    for key, (_, _, families, _) in definitions.items():
        for family in families:
            if family in covered or family not in SOURCES:
                continue
            name = SOURCES[family].name
            ident = 'source:' + family
            option = _option(db, key, ident, name, (family,), {})
            option['selected'] = bool(option['source_ids'] and suppliers[key]['enabled'])
            suppliers[key]['capabilities'].append(option)
            suppliers[key]['serving_count'] += int(option['selected'])
            direction = family.startswith('direction_') or family in {'tushare_dc_index', 'tushare_moneyflow_ind_dc', 'tushare_direction_industry', 'tushare_direction_industry_funds'}
            capabilities.append(dict(id=ident, name=name, group='板块与人气榜' if direction else '资讯与其他来源' if suppliers[key]['kind'] == 'news' else '财务与参考',
                description='沿用现有来源管理与采集范围；本项在来源详情中配置。', routable=False,
                current_provider_id=key if option['selected'] else None, effective_provider_id=key if option['entitlement'] == 'verified' else None, options=[option]))
    order = {'fuyao': 0, 'tdx_data': 1, 'tushare': 2, 'tickflow': 3}
    providers = sorted(suppliers.values(), key=lambda value: (order.get(value['id'], 10), not value['serving_count'], value['name']))
    return dict(revision=settings.revision if settings else 1, policy='purchased_first', providers=providers, capabilities=capabilities)


def backup_families(db, capability_id):
    family = {'stock_quote':'fuyao_quotes', 'index_quote':'fuyao_indices'}.get(capability_id)
    if family is None:
        return set()
    key = 'routing.backups:' + capability_id
    if key not in db.info:
        db.info[key] = {family for config in db.scalars(select(SourceSettings).join(
            Provider, Provider.id == SourceSettings.source_id).where(Provider.family == family))
            if config.config.get('backup_enabled') is True}
    return db.info[key]


def preferred_families(db, capability_id, families, scope=None):
    settings = route_settings(db)
    choice = (settings.choices if settings else {}).get(capability_id, {})
    selected = families_for(capability_id, choice.get('provider_id'))
    backups = backup_families(db, capability_id)
    permitted = [family for family in families
        if (not family.startswith('fuyao_') or family in selected or family in backups)
        and (not family.startswith('tdx_') or (family in selected and choice.get('allow_points')))]
    rank = {'tushare': 0, 'fuyao': 1, 'tencent': 2, 'sina': 2, 'baostock': 2, 'tickflow': 3, 'ashare': 3, 'tdx_data': 4}
    return sorted(permitted, key=lambda family: (family not in selected,
        100 if family in backups else rank.get(provider_for_family(family), 5), list(families).index(family)))


def collection_allowed(db, family):
    if family == 'fuyao_auction':
        # Dedicated, explicitly configured source; no quote-route side effect.
        return True
    if family == 'tdx_full_minute':
        return False
    settings = route_settings(db)
    choices = settings.choices if settings else {}
    related = [key for key in ROUTES if any(family in values for values in ROUTES[key][3].values())]
    selected = [key for key in related if family in families_for(key, choices.get(key, {}).get('provider_id'))]
    if family.startswith(('fuyao_', 'tdx_')):
        return (any(family in backup_families(db, key) for key in related)
            or bool(selected) and (not family.startswith('tdx_') or any(choices[key].get('allow_points') for key in selected)))
    # Existing sources also serve wider scopes and other projections. A small
    # selected-stock feed cannot prove replacement of a whole-market service.
    # Keep those configured schedules as backups; route only compatible reads.
    return True


def apply_routes(db, owner, body, request_id=None):
    lock(db, 'data-center:routes')
    existing = db.get(DataRoutes, 1)
    if body.expected_revision != (existing.revision if existing else 1):
        raise APIError(409, 'routing_changed', '来源选择已变化，请刷新后再应用。')
    if len({item.capability_id for item in body.changes}) != len(body.changes):
        raise APIError(422, 'duplicate_capability', '同一能力只能选择一个首选来源。')
    snapshot = matrix(db)
    capabilities = {item['id']: item for item in snapshot['capabilities']}
    _, configs, providers, _, successes = _source_snapshot(db)
    choices = dict(existing.choices) if existing else {}
    for item in body.changes:
        capability = capabilities.get(item.capability_id)
        option = next((option for option in capability['options'] if option['provider_id'] == item.provider_id), None) if capability else None
        if not capability or not capability['routable'] or not option or not option['implemented']:
            raise APIError(422, 'route_not_supported', '该来源尚不能替换所选能力。')
        if option['billing'] == 'points' and not item.allow_points:
            raise APIError(422, 'points_confirmation_required', '请明确启用该能力的积分消耗。')
        if option['entitlement'] != 'verified':
            raise APIError(409, 'capability_test_required', '请先检测这一项能力，非空返回通过后再应用。')
        if option['state'] == 'disabled':
            raise APIError(409, 'source_disabled', '该项采集配置已关闭，请先在详情的高级配置中启用。')
        if option['state'] in {'error', 'failed', 'unavailable', 'credentials_missing', 'empty', 'time_unverified'}:
            raise APIError(409, 'capability_test_required', '最近一次检测未通过，请重新检测该项能力后再应用。')
        checks = {}
        for family in families_for(item.capability_id, item.provider_id):
            source = providers.get(family)
            if not source or source.id not in successes:
                continue
            config = configs[source.id]
            valid, connection = _current_source_success(db, source, config, successes[source.id],
                choices.get(item.capability_id, {}))
            if not valid or source.entitlement_status != 'verified' or any(
                    source.rights.get(key) != 'allowed' for key in ('display', 'cache')):
                continue
            if family.startswith('tdx_'):
                config.config = {**config.config, 'points_enabled': True}
            checks[str(source.id)] = {'signature': _signature(config, connection), 'run_id': str(successes[source.id].id)}
            if connection and not connection.enabled:
                connection.enabled = True
                # Enable only the source's already-tested connection on explicit
                # apply; another capability's saved connection stays untouched.
        choices[item.capability_id] = dict(provider_id=item.provider_id, allow_points=item.allow_points, checks=checks)
    if existing is None:
        existing = DataRoutes(id=1, revision=1, choices={}, updated_at=now(db))
        db.add(existing)
    existing.choices, existing.revision, existing.updated_at = choices, existing.revision + 1, now(db)
    db.add(AuditEvent(actor_id=owner.id, action='data.routes.update', resource_type='data_routes', resource_id=None,
        request_id=request_id, changed_field_names=[item.capability_id for item in body.changes]))
    db.flush()
    invalidate(db)
    return matrix(db), 'complete', []


def _seed_sources(db, owner, provider_id, families, allow_points):
    from app.market.registry import INDEXES, SOURCES
    from app.market.schemas import SourceConfiguration
    from app.market.service import configure_source, source_id
    stock = []
    for config in db.scalars(select(SourceSettings).order_by(SourceSettings.configured_at.desc())):
        stock = [symbol for symbol in config.config.get('symbols', []) if symbol.get('asset_type') == 'stock' and symbol.get('exchange') in {'SSE', 'SZSE', 'BSE'}]
        if stock:
            break
    stock = stock[:1]
    indices = [dict(exchange=exchange, symbol=symbol, asset_type='index') for exchange, symbol, _ in INDEXES]
    for family in families:
        identity = source_id(family)
        config = db.get(SourceSettings, identity)
        if config:
            if family.startswith('tdx_') and allow_points and not config.config.get('points_enabled'):
                config.config = {**config.config, 'points_enabled': True}
            continue
        if provider_id not in NEW_TEMPLATES:
            continue
        symbols = [] if family == 'fuyao_indices' else indices if family == 'fuyao_index_daily' else stock
        if not symbols and family != 'fuyao_indices':
            raise APIError(422, 'instrument_required', '请先在已有数据配置中选择一个股票，再连接此数据源。')
        values = dict(family=family, symbols=symbols, interval_seconds=max(60, SOURCES[family].minimum_interval),
            rights_basis='管理员登记本人平台账号用于本机个人研究，允许本机展示与缓存；不开放导出或再分发。',
            rights=dict(display='allowed', cache='allowed', export='denied', redistribute='denied'),
            entitlement_status='verified', enabled=True)
        if family.startswith('tdx_'):
            values.update(points_enabled=allow_points, collection_mode='selected')
        configure_source(db, owner, SourceConfiguration.model_validate(values))


def connect_provider(db, owner, provider_id, body, request_id=None):
    from app.ai import service
    from app.ai.models import Connection
    from app.ai.schemas import ConnectionInput
    from app.integrations.data import virtual_connections
    from app.market.service import source_id
    definition = _definitions().get(provider_id)
    if not definition:
        raise APIError(404, 'provider_unknown', '数据供应商不存在。')
    lock(db, 'data-center:connect:' + provider_id)
    name, kind, families, endpoint = definition
    _seed_sources(db, owner, provider_id, families, body.allow_points)
    invalidate(db)
    rows = list(db.scalars(select(Connection).where(Connection.template == provider_id, Connection.archived.is_(False))))
    if len(rows) > 1:
        raise APIError(409, 'multiple_connections', '该供应商有多个连接，请在原接入配置中分别管理。')
    row = rows[0] if rows else None
    virtual = next((item for item in virtual_connections(db) if item['template'] == provider_id), None)
    identities = [source_id(family) for family in families if db.get(SourceSettings, source_id(family))]
    if not identities:
        raise APIError(422, 'source_scope_required', '请先在高级数据工具登记该来源的采集范围。')
    # Editing a credential saves a stopped connection. An unchanged existing
    # configuration keeps its collection gate and key.
    identity = row.id if row else UUID(virtual['id']) if virtual else None
    values = dict(name=row.name if row else name, kind=kind, template=provider_id, endpoint=row.endpoint if row else endpoint,
        protocol=None, model=None, enabled=bool(row and row.enabled and not body.api_key),
        source_ids=identities, options=row.options if row else {}, api_key=body.api_key,
        expected_revision=row.revision if row else None)
    service.store_connection(db, owner, ConnectionInput.model_validate(values), identity, request_id)
    invalidate(db)
    return matrix(db), 'complete', []
