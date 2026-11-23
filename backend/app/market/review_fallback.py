"""Read saved, licensed dated pool tables when the primary archive is empty.

Every normalized row keeps its original immutable row reference. A dated
supplier list is a disclosed universe, not a reconstruction of intraday ticks.
"""
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import re

from sqlalchemy import select

from app.evidence.models import EvidenceVersion
from app.market.protocol import SHANGHAI
from app.market.review_store import permitted_provider

FAMILY = 'mairui_pools'
GROUPS = (('limit_up', 'U'), ('limit_down', 'D'), ('broken_limit', 'Z'))
SCOPE = ('麦蕊按交易日期查询的涨停、跌停及炸板日榜；仅供应商披露范围。'
         '封单资金为日榜字段，首次/末次封板为来源原文；没有独立行情更新时间，'
         '不等同于当前盘口或本机首次观测。')


def _code(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'(sh|sz|bj)?(\d{6})', value)
    if not match:
        return None
    prefix, digits = match.groups()
    exchange = ('SH' if digits.startswith(('6', '900')) else
                'SZ' if digits.startswith(('0', '2', '3')) else
                'BJ' if digits.startswith(('4', '8', '92')) else None)
    if exchange is None or prefix and prefix.upper() != exchange:
        return None
    return digits + '.' + exchange


def _number(value, *, integer=False):
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or integer and amount != amount.to_integral_value():
            return None
        return str(int(amount)) if integer else str(amount)
    except (ValueError, InvalidOperation):
        return None


def pool_table(db, day, as_of_time=None):
    """Return one permitted saved day, or None if its tables are incomplete.

    Close-review fallback requires receipts after that day's close. Historical
    requests still honor their receipt cutoff and historical source rights.
    """
    cutoff = as_of_time or datetime.now(SHANGHAI)
    if day > cutoff.astimezone(SHANGHAI).date():
        return None
    provider = permitted_provider(db, FAMILY, as_of_time)
    if provider is None:
        return None
    rows, references, receipts, seal_units, all_seen = [], [], [], [], set()
    closing = datetime.combine(day, time(15), SHANGHAI)
    for group, kind in GROUPS:
        evidence = db.scalar(select(EvidenceVersion).where(
            EvidenceVersion.source_id == provider.id,
            EvidenceVersion.kind == 'reference', EvidenceVersion.owner_id.is_(None),
            EvidenceVersion.visibility == 'shared', EvidenceVersion.provenance == 'source_reported',
            EvidenceVersion.received_at <= cutoff, EvidenceVersion.received_at >= closing,
            EvidenceVersion.payload['dataset'].astext == 'market.mairui.' + group,
            EvidenceVersion.payload['observed_day'].astext == day.isoformat(),
        ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(1))
        if evidence is None:
            return None
        source_rows = evidence.payload.get('rows')
        if not isinstance(source_rows, list) or len(source_rows) > 8000:
            return None
        columns = {c.get('key'): c.get('unit') for c in evidence.payload.get('columns', []) if isinstance(c, dict)}
        seal_units.append(columns.get('zj') == '元')
        first, selected, seen = len(rows), [], set()
        for position, raw in enumerate(source_rows):
            if not isinstance(raw, dict) or (code := _code(raw.get('symbol'))) is None or code in seen or code in all_seen:
                return None
            seen.add(code)
            all_seen.add(code)
            rows.append(dict(trade_date=day.strftime('%Y%m%d'), ts_code=code, name=raw.get('name'),
                limit=kind, limit_times=_number(raw.get('lbc'), integer=True),
                fd_amount=_number(raw.get('zj')) if columns.get('zj') == '元' else None,
                amount=_number(raw.get('cje')) if columns.get('cje') == '元' else None,
                close=_number(raw.get('p')), pct_chg=raw.get('zf'),
                first_time=raw.get('first_limit_raw'), last_time=raw.get('last_limit_raw'),
                open_times=_number(raw.get('zbc'), integer=True), up_stat=raw.get('limit_statistic'),
                industry=raw.get('industry'), pool_basis='supplier_daily_pool'))
            selected.append(position)
        references.append(dict(id=str(evidence.id), content_hash=evidence.content_hash,
            source_id=str(evidence.source_id), received_at=evidence.received_at.isoformat(),
            start_row=first, row_count=len(selected), selected_rows=selected))
        receipts.append(evidence.received_at)
    if not rows:
        return None
    return dict(state='complete', dataset='pools', requested_day=day.isoformat(),
        trade_day=day.isoformat(), scope='all', coverage_scope=SCOPE, rows=rows, row_count=len(rows),
        evidence_refs=references, source_id=str(provider.id), source_family=FAMILY,
        received_at=max(receipts).isoformat(), source_time=None, basis='supplier_daily_pool',
        reason='primary_pool_archive_empty', seal_amount_unit_verified=all(seal_units),
        knowledge_mode='received_by_cutoff' if as_of_time else 'historical_reference')
