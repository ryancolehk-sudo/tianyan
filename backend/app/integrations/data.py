"""Data-center views and bounded probes for the existing collection adapters."""
from collections import defaultdict
from datetime import date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import case, or_, select

from app.auth import APIError
from app.evidence.common import lock, normalized
from app.evidence.models import EvidenceVersion
from app.integrations.credentials import (
    connections,
    default_endpoint,
    environment_reference,
    invalidate,
)
from app.market.models import DataFetchRun, SourceSettings
from app.market.news_feeds import NEWS_FEEDS
from app.market.public import URLS as PUBLIC_URLS
from app.market.registry import SOURCES
from app.market.connected_specs import FUYAO_ENDPOINT, TDX_ENDPOINT, FUYAO_FAMILIES, TDX_FAMILIES
from app.market.tushare_specs import allowed_endpoints
from app.market.tushare_specs import FAMILIES as TUSHARE_FAMILIES
from app.models import Job, Provider


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _definitions():
    """Supplier grouping is an explicit family contract, never a display-name guess."""
    groups = {
        'fuyao': ('同花顺 API', 'market', tuple(sorted(FUYAO_FAMILIES)), FUYAO_ENDPOINT),
        'tdx_data': ('通达信 TdxAiData', 'market', tuple(sorted(TDX_FAMILIES)), TDX_ENDPOINT),
        'tushare': ('Tushare', 'market', tuple(sorted(TUSHARE_FAMILIES)), default_endpoint('tushare_daily')),
        'mairui': ('麦蕊数据', 'market', ('mairui_funds', 'mairui_pools'), default_endpoint('mairui_funds')),
        'tickflow': ('TickFlow', 'market', ('tickflow_live',), default_endpoint('tickflow_live')),
        'tencent': ('腾讯公开行情', 'market', ('tencent_web', 'tencent_market_rt', 'tencent_auction', 'review_tencent_indices'), 'https://qt.gtimg.cn/'),
        'sina': ('新浪分钟行情', 'market', ('sina_minute_1m', 'sina_minute_5m', 'sina_minute_15m', 'sina_minute_30m', 'sina_minute_60m', 'review_sina_indices'), 'https://quotes.sina.cn/'),
        'baostock': ('BaoStock', 'market', ('baostock_daily', 'baostock_history', 'baostock_fundamental', 'baostock_classification'), 'https://www.baostock.com/'),
        'eastmoney': ('东方财富公开行情', 'market', ('eastmoney_auction', 'eastmoney_funds', 'eastmoney_boards', 'direction_em_hot'), 'https://push2.eastmoney.com/'),
        'ths_hot': ('同花顺公开人气榜', 'market', ('direction_ths_hot',), 'https://dq.10jqka.com.cn/'),
        'tdx_hot': ('通达信公开人气榜', 'market', ('direction_tdx_hot',), 'https://pul.tdx.com.cn/'),
        'kaipanla': ('开盘啦题材强度与情绪', 'market', ('direction_kpl_strength', 'direction_kpl_top', 'direction_kpl_emotion', 'review_kpl_new_highs'), 'https://apphwhq.longhuvip.com/'),
        'ashare': ('AShareAPI', 'market', ('ashare_public', 'ashare_breadth', 'ashare_sector_heat'), 'https://api.ashareapi.com/'),
        'duanxianxia': ('短线侠公开竞价', 'market', ('duanxianxia_auction', 'duanxianxia_auction_history'), 'https://duanxianxia.com/'),
        'chinanews_finance': (SOURCES['chinanews_finance'].name, 'news', ('chinanews_finance',), PUBLIC_URLS['chinanews_finance']),
        'cninfo_announcements': (SOURCES['cninfo_announcements'].name, 'news', ('cninfo_announcements',), PUBLIC_URLS['cninfo_announcements']),
    }
    covered = {family for _, _, families, _ in groups.values() for family in families}
    for family, definition in SOURCES.items():
        if family in covered:
            continue
        endpoint = PUBLIC_URLS.get(family) or (NEWS_FEEDS[family].url if family in NEWS_FEEDS else definition.references[0])
        if family == 'review_cls_news':
            endpoint = 'https://www.cls.cn/v1/roll/get_roll_list'
        groups[family] = (definition.name, 'news' if definition.kind == 'news' else 'market', (family,), endpoint)
    return {key: (name, kind, tuple(family for family in families if family in SOURCES), endpoint)
            for key, (name, kind, families, endpoint) in groups.items()}


def templates(db=None):
    from app.integrations.ifind import template
    return [template()] + [{'id': key, 'name': name, 'kind': kind, 'endpoint': endpoint, 'source_families': list(families),
             'credential_required': key in {'tushare', 'mairui', 'tickflow', 'fuyao', 'tdx_data'},
             'endpoints': sorted(allowed_endpoints()) if key == 'tushare' else [endpoint]}
            for key, (name, kind, families, endpoint) in _definitions().items()]


def _source_template(family):
    return next((key for key, (_, _, families, _) in _definitions().items() if family in families), None)


def validate_data_input(db, body):
    template = _get(body, 'template')
    if _get(body, 'id'):
        from app.ai.models import Connection
        existing = db.get(Connection, _get(body, 'id'))
        if existing and existing.template != template and 'ifind_mcp' in {template, existing.template}:
            raise APIError(422, 'ifind_identity_immutable', '官方iFinD需要独立接入，不能替换已有供应商或解除原能力绑定。')
    if template == 'ifind_mcp':
        from app.integrations.ifind import validate
        return validate(body)
    definition = _definitions().get(template)
    if definition is None:
        raise APIError(422, 'data_template_unknown', '请选择已实现的数据供应商模板。')
    _, kind, families, endpoint = definition
    if _get(body, 'kind') != kind:
        raise APIError(422, 'data_template_kind', '接入类型与供应商模板不一致。')
    allowed = allowed_endpoints() if template == 'tushare' else {endpoint}
    if _get(body, 'endpoint') not in allowed:
        raise APIError(422, 'endpoint_not_allowed', '该数据模板仅允许已核实的固定接口地址。')
    identities = [str(value) for value in _get(body, 'source_ids', [])]
    if len(set(identities)) != len(identities):
        raise APIError(422, 'duplicate_source_binding', '同一能力不能重复绑定。')
    if not identities and _get(body, 'enabled', False):
        raise APIError(422, 'source_binding_required', '启用前请先选择本机已配置的数据能力。')
    for identity in sorted(identities):
        lock(db, 'integration:source-binding:' + identity)
    rows = {str(row.id): row for row in db.scalars(select(Provider).where(Provider.family.in_(families)))}
    for identity in identities:
        if identity not in rows or db.get(SourceSettings, rows[identity].id) is None:
            raise APIError(422, 'source_binding_invalid', '所选来源不属于该供应商模板或尚未配置。')
    own_id = str(_get(body, 'id') or '')
    for other in connections(db, refresh=True):
        if str(other.id) == own_id and not set(other.source_ids).issubset(identities):
            raise APIError(422, 'source_binding_removal_requires_archive', '已绑定能力不能直接解绑；关闭单项请在能力配置中调整，或移除整个接入后重新建立绑定。')
        if not other.archived and str(other.id) != own_id and set(identities).intersection(other.source_ids):
            raise APIError(409, 'source_binding_conflict', '该来源已绑定其他接入，请先修改原接入。')
    return body


def _data_time(payload, published_at, event_at, family=None):
    if family=='tencent_market_rt' and payload.get('dataset')=='tencent.market_capture':
        return next((row.get('stat_source_time_max') for row in payload.get('rows',[])
            if row.get('row_type')=='coverage'),None)
    for value in (published_at, event_at):
        if value is not None:
            return value.isoformat()
    from app.market.direction_specs import PUBLIC_FAMILIES as DIRECTION_FAMILIES
    if family in DIRECTION_FAMILIES:
        # Public heat lists can only expose a local snapshot day. Only dates
        # explicitly supplied by the vendor qualify as data time here.
        values = (row.get('source_time') or row.get('source_day')
                  for row in payload.get('rows', []))
        return max((value for value in values if isinstance(value, str) and value), default=None)
    # Prefer actual source clocks/dates over an observation-day label. Tushare
    # reference tables use observed_day for the local receiving day, and Mairui
    # pools use the requested day; neither is independent publication evidence.
    rows = payload.get('rows', [])
    groups = [
        ((payload,), ('quote_time', 'source_time')),
        (rows, ('trade_time', 'quote_time')),
        ((payload,), ('trade_day', 'data_day', 'period_end', 'date', 'month')),
        (rows, ('trade_date', 'cal_date', 'date', 'data_date', 'f_ann_date', 'ann_date')),
    ]
    if family not in TUSHARE_FAMILIES and family != 'mairui_pools':
        groups.append(((payload,), ('observed_day',)))
    for records, keys in groups:
        latest = None
        for row in records:
            for key in keys:
                value = row.get(key)
                if not isinstance(value, (str, date, datetime)) or not value:
                    continue
                value = value.isoformat() if hasattr(value, 'isoformat') else value
                if len(value) == 8 and value.isdigit():
                    try:
                        value = date.fromisoformat(value).isoformat()
                    except ValueError:
                        continue
                if latest is None or value > latest:
                    latest = value
        if latest is not None:
            return latest
    return None


def _latest_data_times(db, families):
    if not families:
        return {}
    evidence = EvidenceVersion
    # Sort only receipt keys. Sorting the historical JSON bodies can exhaust
    # the database before it returns even the first supplier.
    newest = (select(evidence.id)
              .where(evidence.owner_id.is_(None), evidence.source_id.in_(families))
              .distinct(evidence.source_id)
              .order_by(evidence.source_id, evidence.received_at.desc(), evidence.id.desc())
              .subquery())
    capture_source = next((key for key, family in families.items() if family == 'tencent_market_rt'), None)
    needs_body = or_(evidence.source_id == capture_source,
                     evidence.published_at.is_(None) & evidence.event_at.is_(None))
    clocks = select(evidence.source_id, case((needs_body, evidence.payload)),
                    evidence.published_at, evidence.event_at).join(newest, newest.c.id == evidence.id)
    # Some sources publish a whole reference table in one receipt.
    return {source: _data_time(payload or {}, published, event, families[source])
            for source, payload, published, event in db.execute(clocks.execution_options(yield_per=1))}


def _snapshot(db, *, include_data_time=True):
    key = 'integrations.source_snapshot' if include_data_time else 'integrations.routing_snapshot'
    if key not in db.info:
        from app.market.service import source_states
        states = source_states(db)
        families = {state.provider.id: state.family for state in states}
        times = _latest_data_times(db, families) if include_data_time else {}
        db.info[key] = {str(state.provider.id): {
            'source_id': str(state.provider.id), 'family': state.family, 'name': state.provider.name,
            'state': state.collection_state, 'enabled': state.provider.enabled,
            'last_success_at': state.last_success_at.isoformat() if state.last_success_at else None,
            'data_time': times.get(state.provider.id), 'interval_seconds': state.interval_seconds,
            'credential_configured': state.credential_configured,
            'last_attempt': normalized(state.last_attempt) if state.last_attempt else None,
            'cooldown_until': state.cooldown_until.isoformat() if state.cooldown_until else None,
            'collection_enabled': state.collection_enabled,
            'news_coverage': normalized(state.news_coverage) if state.news_coverage else None,
        } for state in states}
    return db.info[key]


def capabilities(db, conn):
    snapshot = _snapshot(db)
    return [dict(snapshot[identity]) for identity in [str(item) for item in _get(conn, 'source_ids', [])]
            if identity in snapshot]


def virtual_connections(db, *, include_data_time=True):
    stored = connections(db)
    bound = {identity for row in stored for identity in row.source_ids}
    groups = defaultdict(list)
    for identity, state in _snapshot(db, include_data_time=include_data_time).items():
        if identity not in bound:
            groups[_source_template(state['family'])].append(state)
    output = []
    for template, items in groups.items():
        if template is None:
            continue
        name, kind, _, endpoint = _definitions()[template]
        reference = environment_reference(items[0]['family'])
        # Adding another legacy capability must not break an already open form.
        # A materialized identity remains addressable in the table; its residual
        # unbound group gets another deterministic identity without colliding.
        identity_basis = template + '/unbound/' + ','.join(sorted(str(row.id) for row in stored if row.template == template))
        output.append(dict(id=str(uuid5(NAMESPACE_URL, 'tianyan/data-center/connection/' + identity_basis)), name=name,
            kind=kind, template=template, endpoint=endpoint, protocol=None, model=None,
            enabled=any(item['enabled'] for item in items), revision=1,
            credential_configured=all(item['credential_configured'] is not False for item in items),
            credential_origin='environment' if reference and any(item['credential_configured'] for item in items) else 'none',
            known_env_ref=reference, options={'timeout_seconds': 60, 'max_output_tokens': 1200},
            source_ids=[item['source_id'] for item in items], capabilities=[dict(item) for item in items],
            last_test=None, tested_revision=None))
    return output


def connection_test(db, conn):
    saved = _get(conn, 'last_test')
    if not saved:
        return None
    result = dict(saved)
    revision = _get(conn, 'revision')
    if result.get('revision') != revision:
        return {**result, 'status': 'unknown', 'message': '接入配置已修改，请重新检测。'}
    if _get(conn, 'template') == 'ifind_mcp':
        return result
    try:
        job_id, source_id = UUID(result['job_id']), UUID(result['source_id'])
    except (ValueError, TypeError, KeyError):
        return {**result, 'status': 'unknown'}
    if str(source_id) not in [str(value) for value in _get(conn, 'source_ids', [])]:
        return {**result, 'status': 'unknown', 'message': '检测能力已不属于此接入。'}
    job = db.get(Job, job_id)
    if job is None or job.payload.get('source_id') != str(source_id):
        return {**result, 'status': 'unknown'}
    runs = list(db.scalars(select(DataFetchRun).where(DataFetchRun.job_id == job_id, DataFetchRun.source_id == source_id)
                          .order_by(DataFetchRun.received_at.desc(), DataFetchRun.id.desc())))
    result['request_count'] = sum(row.request_count for row in runs)
    # Review pages and their aggregate can share the same source timestamp.
    # A page still awaiting validation must not hide its completed aggregate.
    terminal = [row for row in runs if row.reason != 'review_page_pending']
    latest = terminal[0] if terminal else runs[0] if runs else None
    if job.state in {'queued', 'running'}:
        result['status'] = job.state
        result['retry_at'] = job.available_at.isoformat() if job.state == 'queued' else None
    elif job.state == 'succeeded' and latest and latest.status == 'partial' and latest.reason in {
            'quote_time_unknown','snapshot_trade_date_unverified'} and latest.rejected==0 and (
            latest.accepted+latest.duplicates)>0 and latest.expected==latest.accepted+latest.duplicates and (
            (provider:=db.get(Provider,source_id)) is not None and provider.family in {'fuyao_quotes','fuyao_indices'}):
        # The key/transport/field contract can pass while the market clock is
        # explicitly partial. Enabling this connection does not qualify native
        # trade times, history, every security, or unrelated capabilities.
        result['status']='success'
        result['message']='连接、凭据与返回字段检测通过；逐股行情日期及成交时刻未知，仅按明确标注的价格快照使用。'
    elif job.state == 'succeeded' and latest and latest.status == 'partial' and latest.reason is None and (
            (provider:=db.get(Provider,source_id)) is not None and provider.family=='fuyao_auction'
            and latest.expected==1 and latest.accepted+latest.duplicates==1):
        result['status']='success'
        result['message']='竞价连接与原生字段校验通过；有效股票覆盖未齐，范围继续按实际回执显示。'
    elif job.state == 'succeeded' and latest and latest.status == 'complete':
        result['status'] = 'success'
        result['message'] = '指定单项能力检测通过；其他能力需分别核验。'
    else:
        result['status'] = 'error' if job.state in {'failed', 'succeeded', 'cancelled'} else 'unknown'
        result['error_code'] = job.error_code or (latest.reason or latest.status if latest else None)
        result['message'] = '该单项检测未完整通过，请查看调用记录。'
    if latest:
        result['elapsed_ms'] = latest.elapsed_ms
        result['completed_at'] = latest.received_at.isoformat()
    return result


def probe_data(db, connection_or_input, credential=None):
    if _get(connection_or_input, 'template') == 'ifind_mcp':
        from app.integrations.ifind import probe
        return probe(db, connection_or_input)
    from app.ai.models import Connection
    from app.config import settings
    from app.market.service import enqueue_collection
    supplied = connection_or_input
    identity = _get(supplied, 'id')
    try:
        conn = db.get(Connection, UUID(str(identity))) if identity else None
    except (ValueError, TypeError):
        conn = None
    if conn is None or conn.archived or conn.kind not in {'market', 'news'}:
        raise APIError(422, 'data_probe_requires_saved_connection', '请先保存禁用的数据接入，再选择一项已有能力检测。')
    if not isinstance(supplied, Connection):
        if _get(supplied, 'api_key'):
            raise APIError(422, 'data_probe_requires_saved_connection', '新凭据尚未保存，请先保存禁用接入，再检测该凭据。')
        if any(_get(supplied, key) != getattr(conn, key) for key in ('template', 'endpoint')) or (
                sorted(str(value) for value in _get(supplied, 'source_ids', [])) != sorted(conn.source_ids)):
            raise APIError(409, 'configuration_unsaved', '数据接入有未保存修改，请先保存再检测。')
        if _get(supplied, 'expected_revision') not in {None, conn.revision}:
            raise APIError(409, 'configuration_changed', '接入配置已修改，请刷新后检测。')
    if not settings().data_collection_enabled:
        raise APIError(422, 'collection_disabled', '本机采集未启用，未发起检测请求。')
    selected = _get(supplied, 'source_id') or _get(supplied, '_probe_source_id')
    if selected is None:
        if len(conn.source_ids) != 1:
            raise APIError(422, 'probe_capability_required', '请选择本次要检测的单项数据能力。')
        selected = conn.source_ids[0]
    if str(selected) not in conn.source_ids:
        raise APIError(422, 'source_binding_invalid', '检测能力不属于此接入。')
    provider = db.get(Provider, UUID(str(selected)))
    if provider is None or _source_template(provider.family) != conn.template:
        raise APIError(422, 'source_binding_invalid', '所选数据能力身份不匹配。')
    job = enqueue_collection(db, provider.id, integration_probe={'connection_id': str(conn.id),
                                                               'revision': conn.revision, 'source_id': str(provider.id), 'probe': True})
    invalidate(db)
    return {'status': 'queued', 'job_id': str(job.id), 'source_id': str(provider.id), 'revision': conn.revision,
            'capability': provider.family, 'message': '指定单项能力检测已排队，结果见调用记录', 'request_count': 0}


def sync_connection(db, conn):
    """Connection is an extra collection gate; per-capability rights/settings survive."""
    if _get(conn,'template') in {'fuyao','tdx_data'}:
        # Saving a replacement credential must allow an explicit single-item
        # recheck after an authentication refusal. Schema/range failures remain
        # paused; saving never queues or enables collection.
        for identity in _get(conn,'source_ids',[]):
            config=db.get(SourceSettings, UUID(str(identity)))
            if config and config.paused_reason in {'permission_missing','credentials_rejected','credentials_missing','tdx_data_permission_missing'}:
                config.paused_reason=None
    invalidate(db)
