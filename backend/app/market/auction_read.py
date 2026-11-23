"""Read stored auction batches: permission recheck first, then strict batch pairing.

The two tables are joined by (source_id, batch_hash) only. A missing or mismatched
detail table is reported as unpaired; totals are never rebuilt from one side.
"""
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.models import EvidenceVersion
from app.market.auction import AUCTION_FAMILIES, LEGACY_DETAIL_DATASET, STOCKS_DATASET, SUMMARY_DATASET
from app.market.models import DataFetchRun
from app.market.protocol import SHANGHAI
from app.market.registry import SOURCES
from app.market.schemas import (AuctionBatch, AuctionDetailRow, AuctionSourceState, AuctionSummary, AuctionView)
from app.market.service import source_states
from app.models import Provider

MAX_BATCHES = 60
STALE_AFTER_SECONDS = 6 * 3600


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _rows(summary):
    rows = summary.payload.get('rows') or []
    return rows[0] if rows and isinstance(rows[0], dict) else None


def _summary_dto(row):
    return AuctionSummary(trade_day=date.fromisoformat(row['trade_day']) if row.get('trade_day') else None,
        checkpoint=row.get('checkpoint') or None, instrument_count=_int(row.get('instrument_count')),
        quoted_count=_int(row.get('quoted_count')), rejected_count=_int(row.get('rejected_count')),
        total_volume_shares=row.get('total_volume_shares'), total_amount_cny=row.get('total_amount_cny'),
        up_count=_int(row.get('up_count')), down_count=_int(row.get('down_count')), flat_count=_int(row.get('flat_count')))


def _detail_dto(rows, batch_hash, trade_day, checkpoint):
    if not rows:
        return None
    detail = []
    for row in rows:
        if row.get('batch_hash') != batch_hash or row.get('trade_day') != trade_day or (row.get('checkpoint') or None) != checkpoint:
            return None
        detail.append(AuctionDetailRow(code=row['code'], name=row['name'], price_cny=row.get('price_cny'),
            prev_close_cny=row.get('prev_close_cny'), change_pct=row.get('change_pct'),
            volume_shares=row.get('volume_shares'), amount_cny=row.get('amount_cny'), quote_time=row.get('quote_time')))
    return detail


def auction_view(db: Session, *, trade_day: date | None = None, family: str | None = None) -> AuctionView:
    families = {family} if family in AUCTION_FAMILIES else set(AUCTION_FAMILIES)
    stmt = select(EvidenceVersion, Provider).join(Provider, Provider.id == EvidenceVersion.source_id).where(
        EvidenceVersion.kind == 'reference', EvidenceVersion.visibility == 'shared',
        EvidenceVersion.payload['dataset'].astext == SUMMARY_DATASET, Provider.family.in_(families),
        Provider.enabled.is_(True), Provider.configured.is_(True), Provider.entitlement_status == 'verified',
        Provider.rights['display'].astext == 'allowed', Provider.rights['cache'].astext == 'allowed')
    if trade_day is not None:
        stmt = stmt.where(EvidenceVersion.payload['rows'][0]['trade_day'].astext == trade_day.isoformat())
    rows = list(db.execute(stmt.order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(MAX_BATCHES * 4)))
    now = datetime.now(UTC)
    today = now.astimezone(SHANGHAI).date()
    latest_run = {}
    for provider in db.scalars(select(Provider).where(Provider.family.in_(families))):
        latest_run[provider.id] = db.scalar(select(DataFetchRun).where(DataFetchRun.source_id == provider.id)
            .order_by(DataFetchRun.received_at.desc(), DataFetchRun.id.desc()).limit(1))
    items, seen = [], set()
    for summary, provider in rows:
        row = _rows(summary)
        if row is None:
            continue
        batch_hash = row.get('batch_hash')
        if not batch_hash:
            continue
        key = (provider.id, row.get('trade_day'), row.get('checkpoint'))
        if key in seen:
            continue  # newest batch per source/day/checkpoint; older ones stay stored
        seen.add(key)
        stock_base = summary.source_key.rsplit(':', 1)[0]
        stock_row = db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id == provider.id,
            EvidenceVersion.source_key.in_((stock_base + ':stocks', stock_base + ':detail')),
            EvidenceVersion.kind == 'reference',
            EvidenceVersion.visibility == 'shared').order_by(EvidenceVersion.version_number.desc()).limit(1))
        detail = None
        if stock_row is not None and stock_row.payload.get('dataset') in {STOCKS_DATASET, LEGACY_DETAIL_DATASET}:
            detail = _detail_dto(stock_row.payload.get('rows') or [], batch_hash, row.get('trade_day'), row.get('checkpoint') or None)
        run = latest_run.get(provider.id)
        age = max(0, int((now - summary.received_at.astimezone(UTC)).total_seconds()))
        state, note = 'available', None
        if detail is None:
            state, note = 'unpaired', '汇总与明细未按 batch_hash 配成一对，不展示合计或推算缺失行。'
        elif run is not None and run.status in {'error', 'unavailable'} and run.received_at > summary.received_at:
            state, note = 'stale', f'最近一次采集失败（{run.reason or run.status}），显示上次成功批次。'
        elif trade_day is None and age > STALE_AFTER_SECONDS:
            state, note = 'stale', '旧缓存批次，非当前快照；以接收时间与年龄为准。'
        items.append(AuctionBatch(batch_hash=batch_hash, source_id=provider.id, source_name=provider.name,
            family=provider.family, trade_day=date.fromisoformat(row['trade_day']) if row.get('trade_day') else None,
            trade_day_basis='source_time' if row.get('trade_day') else 'received_at',
            checkpoint=row.get('checkpoint') or None,
            checkpoint_basis='source_time' if row.get('checkpoint') else 'unknown',
            received_at=summary.received_at, quote_time=row.get('quote_time'),
            age_seconds=age, state=state, note=note, summary=_summary_dto(row), detail=detail or []))
        if len(items) >= MAX_BATCHES:
            break
    states = {row.family: row for row in source_states(db) if row.family in families}
    sources = []
    for name in sorted(families):
        definition = SOURCES[name]
        configured = states.get(name)
        if configured is None:
            sources.append(AuctionSourceState(family=name, source_name=definition.name, state='empty', reason='not_configured'))
            continue
        mapped = {'ready': 'available', 'partial': 'partial', 'empty': 'empty', 'error': 'error',
                  'unavailable': 'unavailable', 'disabled': 'disabled', 'permission_missing': 'permission_missing',
                  'not_checked': 'empty', 'credentials_missing': 'unavailable'}[configured.collection_state]
        count = sum(1 for item in items if item.family == name)
        # A stored batch with no run record (e.g. restored data) is readable, not "no data".
        if mapped == 'empty' and count:
            mapped = 'available'
        sources.append(AuctionSourceState(family=name, source_name=definition.name, state=mapped,
            last_received_at=configured.last_attempt.received_at if configured.last_attempt else None,
            last_success_at=configured.last_success_at,
            reason=configured.paused_reason or (configured.last_attempt.reason if configured.last_attempt else None),
            batch_count=count))
    return AuctionView(trade_day=trade_day, items=items, sources=sources)
