"""Source-date projections over immutable reference versions.

Do not rank only the latest source_key first: that loses yesterday when a daily
table is fetched today. Read date metadata from all permitted versions, choose
the latest observation containing the requested source date, then return its
original payload and hash. A receiver date is never a substitute for trade_date.
"""
from datetime import date, datetime, time, timedelta
import re

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONPATH

from app.auth import APIError
from app.evidence.common import now
from app.evidence.models import EvidenceVersion
from app.market.protocol import SHANGHAI
from app.market.schemas import DatasetItem, TradingDatasetList
from app.models import Provider

OBSERVED_SOURCE_DAY = frozenset({'mairui_funds','mairui_pools','eastmoney_funds','eastmoney_boards',
    'duanxianxia_auction','duanxianxia_auction_history'})
AFTER_MARKET = frozenset({'mairui_funds','tushare_moneyflow','tushare_top_list',
    'tushare_top_inst','tushare_limit_list_d','tushare_daily','tushare_index_daily','tushare_daily_basic',
    'tushare_margin_detail','tushare_block_trade','tushare_moneyflow_ind_dc','tushare_moneyflow_dc','tushare_limit_step','tushare_dc_index','tushare_dc_member'})
FINANCIAL_FAMILIES = frozenset({'tushare_income','tushare_balancesheet','tushare_cashflow','tushare_fina_indicator'})
ANNOUNCEMENT_FAMILIES = FINANCIAL_FAMILIES | {'tushare_dividend'}
REFERENCE_FAMILIES = ANNOUNCEMENT_FAMILIES | {'tushare_ths_index','tushare_ths_member','tushare_index_basic_sse',
    'tushare_index_basic_szse','tushare_cn_cpi'}


def reference_view(payload, family, received_at, known_day):
    """Keep raw evidence whole; the optional rows are a dated reference view.

An explicit announcement date can support a release-date reconstruction from
a later received copy. It does not grant strict as-of eligibility: the caller
has already enforced the hard receipt cutoff. Untimed directories/period-only
statistics cannot support that reconstruction and require a saved earlier copy.
"""
    received_in_time = received_at < datetime.combine(known_day + timedelta(days=1),time.min,SHANGHAI)
    if family not in ANNOUNCEMENT_FAMILIES:
        return payload.get('rows',[]) if received_in_time else []
    result=[]
    for row in payload.get('rows',[]):
        fields = ('ann_date','f_ann_date') if family in FINANCIAL_FAMILIES else ('ann_date','imp_ann_date')
        raw_dates = [row[field] for field in fields if row.get(field) not in {None,''}]
        dates = [source_day(value) for value in raw_dates]
        if any(value is None for value in dates):
            continue
        if dates and max(dates)>known_day or not dates and not received_in_time:
            continue
        if family in FINANCIAL_FAMILIES:
            period=source_day(row.get('end_date'))
            if period is None or period>known_day:
                continue
        # ex_date, record_date and pay_date are announced schedules. Their
        # future dates must not hide an already-known dividend proposal.
        result.append(row)
    return result


def distinct_date_values(field):
    # A whole-market batch can repeat one date 8,000 times. Collapse that
    # metadata in PostgreSQL instead of transferring megabytes per old batch.
    values=func.jsonb_array_elements_text(func.jsonb_path_query_array(EvidenceVersion.payload,
        cast('$.rows[*].'+field,JSONPATH))).table_valued('value')
    return select(func.array_agg(func.distinct(values.c.value))).select_from(values).scalar_subquery()


def source_day(value):
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r'\d{8}', value):
            value = value[:4] + '-' + value[4:6] + '-' + value[6:]
        return date.fromisoformat(value) if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else None
    except ValueError:
        return None


def trading_datasets(db, instrument_id=None, dataset=None, family=None, day=None, source_id=None, as_of_time=None):
    clock = now(db)
    if as_of_time is not None and as_of_time > clock:
        raise APIError(422, 'future_cutoff', '资料查询截止时间不能晚于服务器当前时间。')
    cutoff = as_of_time or clock
    if day is not None and day > clock.astimezone(SHANGHAI).date():
        raise APIError(422, 'future_trade_day', '观察日期不能晚于服务器当前日期。')
    # Only extract date columns in the first pass. Do not materialise many
    # 8,000-row raw tables merely to discover historical dates.
    stmt = select(EvidenceVersion.id, EvidenceVersion.source_id, EvidenceVersion.source_key,
        Provider.family, EvidenceVersion.received_at, EvidenceVersion.payload['observed_day'].astext,
        distinct_date_values('trade_date'),distinct_date_values('trade_day'),distinct_date_values('date')).join(
        Provider, Provider.id == EvidenceVersion.source_id).where(
            EvidenceVersion.kind == 'reference', EvidenceVersion.visibility == 'shared',
            EvidenceVersion.owner_id.is_(None), EvidenceVersion.provenance == 'source_reported',
            EvidenceVersion.received_at <= cutoff,
            Provider.enabled.is_(True), Provider.configured.is_(True), Provider.entitlement_status == 'verified',
            Provider.rights['display'].astext == 'allowed', Provider.rights['cache'].astext == 'allowed')
    if instrument_id is not None:
        stmt = stmt.where(EvidenceVersion.instrument_id == instrument_id)
    if dataset is None or not dataset.startswith('review.'):
        stmt = stmt.where(~EvidenceVersion.source_key.startswith('review.'))
    if dataset is not None:
        stmt = stmt.where(EvidenceVersion.payload['dataset'].astext == dataset)
    if family is not None:
        stmt = stmt.where(Provider.family == family)
    if source_id is not None:
        stmt = stmt.where(EvidenceVersion.source_id == source_id)
    stmt = stmt.order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.version_number.desc(), EvidenceVersion.id.desc())
    candidates, available, reference_candidates = {}, set(), {}
    known_day=min(day or cutoff.astimezone(SHANGHAI).date(),cutoff.astimezone(SHANGHAI).date())
    for ident, provider_id, key, source_family, received_at, observed, trade_dates, trade_days, data_dates in db.execute(stmt).yield_per(50):
        dates = {parsed for raw in (trade_dates or []) + (trade_days or []) + (data_dates or [])
                 if (parsed := source_day(raw)) is not None and parsed <= cutoff.astimezone(SHANGHAI).date()}
        if source_family in REFERENCE_FAMILIES:
            dates=set()
        if not dates and source_family in OBSERVED_SOURCE_DAY and (parsed := source_day(observed)):
            dates = {parsed}
        if not dates:
            if source_family in ANNOUNCEMENT_FAMILIES or received_at < datetime.combine(known_day+timedelta(days=1),time.min,SHANGHAI):
                reference_candidates.setdefault((provider_id,key),[]).append(ident)
            continue
        available.update(dates)
        for actual_day in dates:
            candidates.setdefault((provider_id, key, actual_day), ident)
    target = day or (max(available) if available else None)
    selected = {}
    for (provider_id, key, actual_day), ident in candidates.items():
        if actual_day == target:
            selected.setdefault((provider_id, key), (ident, actual_day))
    # Choose one most-recent batch with at least one knowable reference row.
    # When the latest copy is entirely later announcements, an older saved
    # batch may still contain the appropriate report/proposal at the cutoff.
    views={}
    for identity, versions in reference_candidates.items():
        for ident in versions:
            evidence=db.get(EvidenceVersion,ident)
            provider_family=db.get(Provider,evidence.source_id).family
            view=reference_view(evidence.payload,provider_family,evidence.received_at,known_day)
            if view or provider_family not in ANNOUNCEMENT_FAMILIES:
                selected[identity]=(ident,None)
                views[ident]=view
                break
    if not selected:
        return TradingDatasetList(items=[], available_days=sorted(available, reverse=True), trade_day=target)
    days_by_id = {ident: actual_day for ident, actual_day in selected.values()}
    rows = db.execute(select(EvidenceVersion, Provider).join(Provider, Provider.id == EvidenceVersion.source_id).where(
        EvidenceVersion.id.in_(days_by_id)).order_by(Provider.family, EvidenceVersion.source_key))
    items = []
    for evidence, provider in rows:
        mode = ('intraday' if provider.family in {'eastmoney_funds','eastmoney_boards'} else
                'after_market' if provider.family in AFTER_MARKET else 'reference')
        items.append(DatasetItem(id=evidence.id, source_id=provider.id, source_name=provider.name, family=provider.family,
            source_key=evidence.source_key, instrument_id=evidence.instrument_id, received_at=evidence.received_at,
            published_at=evidence.published_at, content_hash=evidence.content_hash, payload=evidence.payload,
            trade_day=days_by_id[evidence.id], data_mode='reference' if evidence.id in views else mode,
            view_rows=views.get(evidence.id)))
    return TradingDatasetList(items=items, available_days=sorted(available, reverse=True), trade_day=target)
