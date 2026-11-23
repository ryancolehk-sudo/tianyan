"""Read an already saved, licensed close ladder when the full pool is pending."""
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
import re

from sqlalchemy import select

from app.evidence.models import EvidenceVersion
from app.market.protocol import SHANGHAI
from app.market.review_store import permitted_provider

FAMILY = 'tushare_limit_step'


def saved_ladder_table(db, day, as_of_time=None):
    """Keep one dated source batch and original row offsets; never collect here.

    This is only the supplier's 2+ board list, not the complete U/D/Z pool.
    Current and historical permissions, receipt cutoffs and the close boundary
    still apply. A corrupt/ambiguous latest batch is never merged with an older one.
    """
    cutoff = as_of_time or datetime.now(UTC)
    if day > cutoff.astimezone(SHANGHAI).date():
        return None
    closing = datetime.combine(day, time(15), SHANGHAI)
    if cutoff < closing:
        return None
    provider = permitted_provider(db, FAMILY, as_of_time)
    if provider is None:
        return None
    evidence = db.scalar(select(EvidenceVersion).where(
        EvidenceVersion.source_id == provider.id, EvidenceVersion.kind == 'reference',
        EvidenceVersion.visibility == 'shared', EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.provenance == 'source_reported',
        EvidenceVersion.received_at >= closing, EvidenceVersion.received_at <= cutoff,
        EvidenceVersion.payload['dataset'].astext == 'tushare.limit_step',
        # observed_day is the receipt date, including late backfills. Match
        # the actual source trading date instead and keep the receipt cutoff.
        EvidenceVersion.payload['rows'].contains([{'trade_date': day.strftime('%Y%m%d')}]),
    ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.version_number.desc(),
               EvidenceVersion.id.desc()).limit(1))
    if evidence is None:
        return None
    source_rows = evidence.payload.get('rows')
    if not isinstance(source_rows, list) or not source_rows or len(source_rows) >= 2000:
        return None
    rows, positions, seen = [], [], set()
    for position, raw in enumerate(source_rows):
        if not isinstance(raw, dict) or raw.get('trade_date') != day.strftime('%Y%m%d'):
            return None
        code, name = raw.get('ts_code'), raw.get('name')
        if (not isinstance(code, str) or not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)', code)
                or code in seen or not isinstance(name, str) or not name.strip()):
            return None
        seen.add(code)
        try:
            height = Decimal(str(raw.get('nums')))
            if not height.is_finite() or height != height.to_integral_value() or height < 2:
                return None
        except (InvalidOperation, ValueError, TypeError):
            return None
        # The full pool excludes ST and this analysis has an SH/SZ universe.
        # Use the original dated name, never today's mutable stock directory.
        if code.endswith('.BJ') or 'ST' in name.upper():
            continue
        rows.append(dict(trade_date=raw['trade_date'], ts_code=code, name=name,
                         board_height=int(height)))
        positions.append(position)
    if not rows:
        return None
    reference = dict(id=str(evidence.id), content_hash=evidence.content_hash,
        source_id=str(evidence.source_id), received_at=evidence.received_at.isoformat(),
        start_row=0, row_count=len(rows), selected_rows=positions)
    return dict(state='complete', dataset='ladder', trade_day=day.isoformat(), scope='reported_2plus',
        source_family=FAMILY, source_id=str(provider.id), basis='supplier_ladder',
        coverage_scope='已购兼容通道原日收盘连板榜，沪深非ST、2板及以上；首板仍待完整涨停池。',
        rows=rows, row_count=len(rows), evidence_refs=[reference],
        received_at=evidence.received_at.isoformat())
