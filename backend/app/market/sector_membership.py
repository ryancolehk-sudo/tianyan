"""Small authorized membership read. Prices remain in the workbench projection."""
from datetime import datetime

from sqlalchemy import column, func, select, true
from sqlalchemy.dialects.postgresql import JSONB

from app.auth import APIError
from app.evidence.common import now
from app.evidence.models import EvidenceVersion
from app.market import read_facets
from app.market.protocol import SHANGHAI
from app.market.review_store import available_trade_days, load_dataset, permitted_provider
from app.market.terminal_schemas import SectorMembership, SectorMembershipEntry
from app.market.trading_datasets import source_day

MAX_MEMBERS = 8000


def read_membership(db, day, sector, *, as_of_time=None, observation_cutoff=None):
    clock = now(db)
    if as_of_time is not None and observation_cutoff is not None:
        raise APIError(422, 'conflicting_cutoffs', '资料接收截止与历史回看不能同时指定。')
    cutoff = as_of_time or observation_cutoff or clock
    if cutoff > clock or day > cutoff.astimezone(SHANGHAI).date():
        raise APIError(422, 'future_cutoff', '所选日期或资料截止不能晚于当前时间。')
    provider = permitted_provider(db, 'tushare_dc_member', cutoff)
    result = SectorMembership(trade_day=day, sector_code=sector,
                              state='missing' if provider else 'permission_denied')
    if provider is None:
        return result
    # Read only the member source's metadata, then transfer just this board's rows.
    query = read_facets.with_facets(select(
        EvidenceVersion.id, EvidenceVersion.source_key, EvidenceVersion.received_at,
        read_facets.trade_dates())).where(
        EvidenceVersion.source_id == provider.id, EvidenceVersion.kind == 'reference',
        EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared',
        EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= cutoff,
        read_facets.value('dataset') == 'tushare.dc_member',
    ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.version_number.desc(), EvidenceVersion.id.desc())
    metadata = [(ident, key, received, {parsed for raw in dates or [] if (parsed := source_day(raw))})
                for ident, key, received, dates in db.execute(query)]
    days = [day]
    if as_of_time is None and day == clock.astimezone(SHANGHAI).date():
        saved_days = {value for _, _, _, dates in metadata for value in dates if value < day}
        saved_days.update(available_trade_days(db, limit=20, end_day=day, as_of_time=cutoff))
        days.extend(sorted(saved_days - {day}, reverse=True)[:20])
    member = func.jsonb_array_elements(EvidenceVersion.payload['rows']).table_valued(column('value', JSONB)).lateral()
    for member_day in days:
        latest = {}
        for ident, key, received, dates in metadata:
            if member_day in dates:
                latest.setdefault(key, (ident, received))
        rows = {}
        received_at = None
        if latest:
            records = db.execute(select(member.c.value, EvidenceVersion.received_at)
                .select_from(EvidenceVersion).join(member, true()).where(
                    EvidenceVersion.id.in_([item[0] for item in latest.values()]),
                    member.c.value['ts_code'].astext == sector,
                    member.c.value['trade_date'].astext.in_([str(member_day), member_day.strftime('%Y%m%d')]),
                ).order_by(EvidenceVersion.received_at, EvidenceVersion.id))
            for row, received in records:
                rows[row['con_code']] = row
                received_at = max(received_at or received, received)
        if not rows:
            for scope in (sector, 'all'):
                archived = load_dataset(db, 'members', member_day, scope=scope, as_of_time=cutoff)
                if archived.get('state') != 'complete':
                    continue
                rows = {row['con_code']: row for row in archived.get('rows', [])
                        if row.get('ts_code') == sector and source_day(row.get('trade_date')) == member_day}
                if rows:
                    raw = archived.get('received_at')
                    received_at = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
                    break
        if rows:
            result.state, result.members_trade_day, result.received_at = 'available', member_day, received_at
            result.member_count, result.truncated = len(rows), len(rows) > MAX_MEMBERS
            result.members = [SectorMembershipEntry(code=code, name=row.get('name') or code)
                              for code, row in sorted(rows.items())[:MAX_MEMBERS]]
            break
    return result
