"""Local limit-price breadth by exact DC membership; never a vendor score."""
from datetime import datetime, time
from math import isfinite

from app.market.protocol import SHANGHAI
from app.market.session import quote_fresh_at
from app.market.terminal_schemas import SectorStrength


def count_strength(sectors, memberships, stocks, day, cutoff, *, daily_close=False):
    result = []
    reference = quote_fresh_at(cutoff)
    for sector in sectors:
        membership = memberships.get(sector.code, (None, set(), False))
        member_day, codes = membership[:2]
        complete_members = membership[2] if len(membership) > 2 else True
        counted, hits, clocks, receipts = 0, 0, [], []
        for code in codes:
            stock = stocks.get(code)
            if stock is None or any(v is None or not isfinite(v) or v <= 0 for v in (stock.price, stock.limit_up)):
                continue
            stamp = None
            if stock.price_basis == 'daily_close' and daily_close:
                pass
            elif stock.price_basis == 'intraday' and stock.source_time:
                try:
                    stamp = datetime.fromisoformat(stock.source_time)
                    local = stamp.astimezone(SHANGHAI)
                    # Match the terminal's accepted closing minute, including
                    # real provider stamps such as 15:00:02. Keep the exact
                    # stamp; never reinterpret a later 15:04 receipt as close.
                    minute = local.time().replace(second=0,microsecond=0,tzinfo=None)
                    if (stamp.utcoffset() is None or stamp > cutoff or local.date() != day
                            or not (time(9, 30) <= minute <= time(11, 30)
                                    or time(13) <= minute <= time(15))
                            or day == cutoff.astimezone(SHANGHAI).date() and (reference - stamp).total_seconds() > 180):
                        continue
                except (ValueError, TypeError):
                    continue
            else:
                continue
            if stock.source_received_at and stock.source_received_at > cutoff:
                continue
            counted += 1
            hits += int(stock.price == stock.limit_up)
            if stamp: clocks.append(stamp)
            if stock.source_received_at: receipts.append(stock.source_received_at)
        result.append(SectorStrength(code=sector.code, name=sector.name, category=sector.category,
            industry_level=sector.industry_level, trade_day=day, members_day=member_day,
            limit_up_count=hits if counted else None, member_count=len(codes) if member_day else None,
            quoted_count=counted, partial=not complete_members or not member_day or member_day != day or counted != len(codes),
            source_time=min(clocks) if clocks else None, latest_source_time=max(clocks) if clocks else None,
            received_at=max(receipts) if receipts else None, basis='daily_close' if daily_close else 'intraday'))
    return sorted(result, key=lambda r: (r.limit_up_count is None, -(r.limit_up_count or 0), r.code))


def sector_strength(reader, sectors, stocks, day, *, current=False, daily_close=False):
    from app.market.review_store import load_dataset
    from app.market.terminal_archives import _remember
    memberships = {}
    wanted = {s.code for s in sectors}
    if 'tushare_dc_member' in reader.permitted:
        # Whole saved manifests are loaded once per date, never one request per
        # checkbox or one query per member. Older directories remain labelled.
        days = [day]
        if current:
            days += sorted((value for value in set(reader.dates('tushare_dc_member', day)) | set(reader.calendar_days)
                if value < day), reverse=True)[:5]
        for member_day in days:
            if member_day > day: continue
            rows = [row for row, _, _ in reader.table('tushare_dc_member', member_day)]
            key = ('terminal_members_dataset', member_day, 'all')
            if key not in reader.tables:
                reader.tables[key] = load_dataset(reader.db, 'members', member_day, as_of_time=reader.cutoff)
            archive = reader.tables[key]
            if archive.get('state') == 'complete':
                _remember(reader, archive)
                # A committed complete same-day archive takes precedence.
                rows = archive['rows']
            groups = {}
            for row in rows:
                if reader.day(row.get('trade_date')) != member_day: continue
                code, member = row.get('ts_code'), row.get('con_code')
                if code in wanted and isinstance(member, str): groups.setdefault(code, set()).add(member)
            for code, members in groups.items(): memberships.setdefault(code, (member_day, members, archive.get('state') == 'complete'))
            if wanted <= memberships.keys() or archive.get('state') == 'complete': break
    return count_strength(sectors, memberships, stocks, day, reader.cutoff, daily_close=daily_close)
