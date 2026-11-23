"""Four ladder views share one bounded, read-only evidence projection.

Daily pools establish historical heights. Intraday heights require an exact
native quote clock, a price limit, and a proved previous close/height. Neither a
missing quote nor absence from an incomplete pool establishes elimination.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, time
from io import BytesIO
from math import isfinite
import re
import uuid
from xml.sax.saxutils import escape
from zipfile import ZipFile, ZIP_DEFLATED

from pydantic import Field
from sqlalchemy import column, func, select, true
from sqlalchemy.dialects.postgresql import JSONB

from app.auth import APIError
from app.evidence.common import now
from app.evidence.models import EvidenceVersion
from app.market.protocol import SHANGHAI
from app.market.review_analysis import ArchiveReader
from app.market.review_group_fields import enrich_groups
from app.market.review_protocol import DATASET_FAMILIES
from app.market.review_read import _context, _dated_rows, _metadata, _referenced
from app.market.review_store import permitted_provider
from app.schemas import DTO


class LadderBar(DTO):
    day: date
    open: float
    high: float
    low: float
    close: float


class LadderRecord(DTO):
    code: str
    name: str
    day: date
    height: int
    previous_height: int | None = None
    status: str
    themes: list[str] = Field(default_factory=list)
    theme_source: str | None = None
    limit_type: str | None = None
    pct_change: float | None = None
    price: float | None = None
    auction_price: float | None = None
    first_time: str | None = None
    last_time: str | None = None
    seal_amount: float | None = None
    amount: float | None = None
    actual_float: float | None = None
    actual_turnover: float | None = None
    source_time: str | None = None
    received_at: str | None = None
    price_basis: str
    stale: bool = False


class LadderDay(DTO):
    day: date
    previous_day: date | None = None
    state: str
    rows: list[LadderRecord] = Field(default_factory=list)
    eliminated: list[LadderRecord] = Field(default_factory=list)
    coverage: list[dict] = Field(default_factory=list)


class LadderObservation(DTO):
    trade_day: date | None = None
    quote_day: date | None = None
    available_days: list[date] = Field(default_factory=list)
    days: list[LadderDay] = Field(default_factory=list)
    candles: dict[str, list[LadderBar]] = Field(default_factory=dict)
    latest_quotes: dict[str, LadderRecord] = Field(default_factory=dict)
    state: str
    as_of_time: str | None = None
    built_at: str
    can_export: bool = False
    scope: str = '已存原日供应商沪深涨停池；盘中使用已核实时间的行情。题材来自原日同花顺涨停原因，可多重归属。K线为原始不复权日线。'


def num(value, *, positive=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if isfinite(value) and (not positive or value > 0) else None
    except (TypeError, ValueError):
        return None


def clock(value):
    raw = str(value or '').replace(':', '').split('.')[0].zfill(6)
    try:
        parsed = time(int(raw[:2]), int(raw[2:4]), int(raw[4:]))
        return parsed.isoformat() if time(9, 15) <= parsed <= time(15, 1) else None
    except (ValueError, TypeError):
        return None


def unique(rows):
    counts = Counter(r.get('ts_code') for r in rows)
    return {r['ts_code']: r for r in rows if counts[r.get('ts_code')] == 1 and
            re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)', str(r.get('ts_code', '')))}


def native_themes(row):
    # Preserve the supplier's own cause tokens. No inferred concept matching.
    raw = row.get('change_reason')
    return list(dict.fromkeys(s.strip() for s in re.split(r'[+＋]', raw or '') if s.strip()))[:12]


def record(row, day, quote=None, *, height=None, status='sealed', previous_height=None):
    themes = native_themes(row)
    return LadderRecord(code=row['ts_code'], name=row.get('name') or row['ts_code'], day=day,
        height=height or int(float(row['limit_times'])), previous_height=previous_height, status=status,
        themes=themes, theme_source='同花顺涨停原因' if themes else None,
        limit_type=row.get('limit_type'), pct_change=num((quote or row).get('pct_chg')),
        price=num((quote or row).get('close'), positive=True),
        first_time=clock(row.get('first_time')), last_time=clock(row.get('last_time')),
        seal_amount=num(row.get('seal_amount_yuan')), amount=num(row.get('turnover_yuan')),
        actual_float=num(row.get('actual_float_value_yuan')),
        actual_turnover=num(row.get('actual_turnover')),
        received_at=(row.get('row_reference') or {}).get('received_at'),
        price_basis='daily_close')


def build_day(day, previous, pool, prior, daily, *, complete=False):
    """Pure projection; complete empty pools and unknown pools stay distinct."""
    current = unique(pool)
    ambiguous = {code for code,n in Counter(r.get('ts_code') for r in pool).items() if n > 1}
    yesterday = unique(prior)
    prices = unique(daily)
    rows, eliminated = [], []
    for code, raw in current.items():
        h = num(raw.get('limit_times'), positive=True)
        if raw.get('limit') != 'U' or h is None or int(h) != h:
            continue
        old = yesterday.get(code, {})
        old_h = num(old.get('limit_times'), positive=True) if old.get('limit') == 'U' else None
        rows.append(record(raw, day, prices.get(code), previous_height=int(old_h) if old_h else None))
    # A complete final pool proves non-promotion; a usable same-day price is
    # also required, so partial quote coverage cannot manufacture failures.
    if complete:
        sealed_codes = {r.code for r in rows}
        for code, old in yesterday.items():
            h = num(old.get('limit_times'), positive=True)
            quote = prices.get(code)
            if old.get('limit') != 'U' or code in sealed_codes or code in ambiguous or current.get(code, {}).get('limit') == 'U' or not h or int(h) != h or not quote or num(quote.get('close'), positive=True) is None:
                continue
            today = current.get(code)
            broken = today if today and today.get('limit') == 'Z' else None
            raw = {**old, **(broken or {}), 'ts_code': code}
            # Yesterday's seal/time/amount are never attached to today's failure.
            for key in ('first_time', 'last_time', 'seal_amount_yuan', 'turnover_yuan', 'actual_float_value_yuan', 'actual_turnover'):
                raw[key] = (broken or {}).get(key)
            raw['turnover_yuan'] = (num(quote.get('amount')) * 1000) if num(quote.get('amount')) is not None else None
            item = record(raw, day, quote, height=int(h)+1,
                status='opened' if broken and broken.get('limit') == 'Z' else 'eliminated', previous_height=int(h))
            if not broken and item.themes:
                item.theme_source = f'同花顺涨停原因 · {previous}'
            eliminated.append(item)
    return LadderDay(day=day, previous_day=previous, state='complete' if complete else 'missing', rows=rows, eliminated=eliminated)


def selected_rows(reader, dataset, day, codes):
    """Validate the full manifest, project just the cohort in PostgreSQL."""
    table = reader.get(dataset, day, include_rows=False)
    if table.get('state') != 'complete' or not codes:
        return {**table, 'rows': []}
    rows = []
    for ref in table.get('evidence_refs', []):
        cells = func.jsonb_array_elements(EvidenceVersion.payload['rows']).table_valued(
            column('value', JSONB), with_ordinality='row_number').render_derived().alias('ladder_rows')
        statement = select(cells.c.value, cells.c.row_number).select_from(EvidenceVersion).join(cells, true()).where(
            EvidenceVersion.id == uuid.UUID(ref['id']), EvidenceVersion.content_hash == ref['content_hash'],
            cells.c.value['ts_code'].astext.in_(sorted(codes)))
        for raw, number in reader.db.execute(statement):
            rows.append({**raw, 'row_reference': {**ref, 'source_row': number-1}})
    return {**table, 'rows': rows}


def missing_table(dataset, day):
    """An uncollected date slot has the same metadata contract as an archive."""
    return dict(state='missing', dataset=dataset, trade_day=day.isoformat(),
        requested_day=day.isoformat(), scope='all', rows=[], row_count=None,
        evidence_refs=[], source_id=None, received_at=None)


def archived_pool(reader, day, closed):
    pool = reader.get('pools', day) if closed else missing_table('pools', day)
    raw = [_referenced(r, pool, i) for i, r in enumerate(_dated_rows(pool, day) or [])]
    details = reader.get('pool_details', day) if closed else missing_table('pool_details', day)
    basics = reader.get('group_basics', day) if closed else missing_table('group_basics', day)
    detail_rows = [_referenced(r, details, i) for i, r in enumerate(_dated_rows(details, day) or [])]
    basic_rows = [_referenced(r, basics, i) for i, r in enumerate(_dated_rows(basics, day) or [])]
    enrich_groups(raw, pool, reader.group_units(pool), {'state': 'missing'}, None, detail_rows, basic_rows)
    basic_map = unique(basic_rows)
    for row in raw:
        row['actual_turnover'] = num(basic_map.get(row['ts_code'], {}).get('turnover_rate_f'))
    return pool, raw, basic_map


def auction_prices(db, day, cutoff):
    """Original matched auction price, never the public page's seal amount."""
    provider = permitted_provider(db, 'tushare_stk_auction', cutoff)
    if provider is None:
        return {}, None
    statement = select(EvidenceVersion).where(
        EvidenceVersion.source_id == provider.id, EvidenceVersion.kind == 'reference',
        EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared',
        EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= cutoff,
        ~EvidenceVersion.source_key.startswith('review.'),
        EvidenceVersion.payload['rows'].contains([{'trade_date': day.strftime('%Y%m%d')}]),
    ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(8)
    prices = {}
    for evidence in db.scalars(statement):
        units = {c.get('key'):c.get('unit') for c in evidence.payload.get('columns', [])}
        if units.get('price') != '元':
            continue
        rows = [r for r in evidence.payload.get('rows', []) if r.get('trade_date') == day.strftime('%Y%m%d')]
        for code,row in unique(rows).items():
            if code not in prices and (price := num(row.get('price'), positive=True)) is not None:
                prices[code] = price
    return prices, provider if prices else None


def live_quotes(db, target, previous, cutoff):
    """Reuse the terminal's permission/routing/clock checks, without UI panels."""
    from app.market.terminal_read import (SavedTables, current_rows, price_universe, LIMITS,
        live_snapshot_overlay, apply_snapshot_prices, selected_quote_projection, make_stock)
    from app.market.session import market_session
    reader = SavedTables(db, cutoff, target)
    session = market_session(db, cutoff)
    snapshots = reader.market(target)
    latest = next((m for m in snapshots if current_rows(reader, m, target)), None)
    quotes = current_rows(reader, latest, target)
    limit_rows = reader.table(LIMITS, target)
    limits = {r['ts_code']: r for r, _, _ in limit_rows}
    premarket = reader.table('tushare_stk_premarket', target)
    for row, _, _ in premarket:
        limits.setdefault(row['ts_code'], row)
    stocks, names, identities = price_universe(reader, target, previous, latest, [], quotes,
        {r['ts_code']: r for r in quotes}, None, limits,
        {r['ts_code']: (r, columns) for r, columns, _ in premarket}, {}, {}, limit_rows,
        as_of_time=None, prepare_breadth=False)
    snapshot = live_snapshot_overlay(reader, target, session, latest, quotes, strict=True)
    if snapshot:
        apply_snapshot_prices(reader, snapshot, stocks, names, identities)
    for code, routed in selected_quote_projection(reader, target, strict=True).items():
        item = stocks.setdefault(code, make_stock(code, names, identities))
        payload, provenance = routed.quote, routed.provenance
        item.price, item.open, item.high = map(num, (payload.price, payload.open, payload.high))
        item.pct_change = num(routed.change_percent)
        item.amount_cny = num(payload.amount_cny)
        item.source_time = payload.quote_time.isoformat()
        item.source_received_at = provenance.received_at
        item.source_family = provenance.family
        item.price_basis = 'intraday'
        item.seal_amount_cny = None
    return stocks


def apply_live(result, stocks, yesterday, previous_prices, previous_limits, cutoff):
    from app.market.session import quote_fresh_at
    old = unique(yesterday)
    previous_quote = unique(previous_prices)
    previous_limit = unique(previous_limits)
    sealed, failed, latest = {}, {}, {}
    for code, stock in stocks.items():
        try:
            stamp = datetime.fromisoformat(stock.source_time or '')
            local = stamp.astimezone(SHANGHAI)
            valid = stock.price_basis == 'intraday' and stamp.utcoffset() is not None and local.date() == result.day and stamp <= cutoff and (stock.source_received_at is None or stock.source_received_at <= cutoff) and time(9,30) <= local.time().replace(tzinfo=None) <= time(15,1)
        except (ValueError, TypeError):
            valid = False
        if not valid or num(stock.price, positive=True) is None:
            continue
        prior = old.get(code, {})
        h = num(prior.get('limit_times'), positive=True) if prior.get('limit') == 'U' else None
        if h is None:
            close, upper = num(previous_quote.get(code, {}).get('close'), positive=True), num(previous_limit.get(code, {}).get('up_limit'), positive=True)
            if close is not None and upper is not None and close < upper:
                h = 0
        height = int(h)+1 if h is not None and int(h) == h else None
        at_limit = stock.limit_up is not None and stock.price == stock.limit_up
        opened = stock.limit_up is not None and stock.high is not None and stock.high >= stock.limit_up and stock.price < stock.limit_up
        status = 'sealed' if at_limit and (stock.seal_amount_cny or 0) > 0 else 'touched' if at_limit else 'opened' if opened else 'eliminated'
        raw = {**prior, 'ts_code': code, 'name': stock.name}
        row = record(raw, result.day, height=height or 1, status=status, previous_height=int(h) if h is not None else None)
        row.price, row.pct_change, row.amount, row.seal_amount = stock.price, stock.pct_change, stock.amount_cny, stock.seal_amount_cny
        row.first_time = row.last_time = None
        if row.themes:
            row.theme_source = f'同花顺涨停原因 · {result.previous_day}'
        row.actual_float = row.actual_turnover = None
        row.source_time, row.received_at = stock.source_time, str(stock.source_received_at) if stock.source_received_at else None
        row.price_basis = 'intraday'
        row.stale = (quote_fresh_at(cutoff)-stamp).total_seconds() > 180
        latest[code] = row
        if height is None or row.stale:
            continue
        if at_limit:
            sealed[code] = row
        elif h and stock.limit_up is not None:
            failed[code] = row
    result.rows = list(sealed.values())
    result.eliminated = list(failed.values())
    result.state = 'live' if sealed or failed else 'missing'
    return latest


def observation_view(db, owner_id, trade_day=None, lookback=6, as_of_time=None):
    current = now(db)
    cutoff = as_of_time or current
    if cutoff > current or trade_day and trade_day > cutoff.astimezone(SHANGHAI).date():
        raise APIError(422, 'future_cutoff', '日期或观察时点不能晚于服务器当前时间。')
    context, calendar, target = _context(db, None, 120, as_of_time)
    if trade_day is not None:
        target = trade_day if trade_day in calendar else None
    result = LadderObservation(trade_day=target, available_days=list(reversed(calendar)),
        state='missing', built_at=current.isoformat(), as_of_time=as_of_time.isoformat() if as_of_time else None)
    if target is None:
        return result
    reader = ArchiveReader(db, cutoff)
    quote_day = calendar[-1]
    result.quote_day = quote_day
    quote_previous = calendar[-2] if len(calendar) > 1 else None
    calendar = [d for d in calendar if d <= target]
    window = calendar[-lookback:]
    start = max(0, len(calendar)-len(window)-1)
    pools, raw, basics = {}, {}, {}
    auction_sources = []
    for day in calendar[start:]:
        closed = day < cutoff.astimezone(SHANGHAI).date() or cutoff.astimezone(SHANGHAI).time().replace(tzinfo=None) >= time(15)
        pools[day], raw[day], basics[day] = archived_pool(reader, day, closed)
    for index, day in enumerate(window):
        pos = calendar.index(day)
        previous = calendar[pos-1] if pos else None
        daily = reader.get('daily', day) if pools[day].get('state') == 'complete' else missing_table('daily', day)
        prices = _dated_rows(daily, day) or []
        priced = unique(prices)
        for row in raw[day]:
            quote = priced.get(row['ts_code'])
            row['turnover_yuan'] = num(quote.get('amount'))*1000 if quote and num(quote.get('amount')) is not None else None
        built = build_day(day, previous, raw[day], raw.get(previous, []), prices,
                          complete=pools[day].get('state') == 'complete')
        auctions, auction_provider = auction_prices(db, day, cutoff)
        if auction_provider:
            auction_sources.append(auction_provider)
        for row in [*built.rows, *built.eliminated]:
            row.auction_price = auctions.get(row.code)
        built.coverage = [_metadata(pools[day]), _metadata(daily)]
        result.days.append(built)
    selected = result.days[-1]
    # Strict replay uses only native quotes received by the cutoff too.
    if target == cutoff.astimezone(SHANGHAI).date() and selected.state != 'complete':
        previous = selected.previous_day
        old_prices = reader.get('daily', previous) if previous else {'state':'missing'}
        old_limits = reader.get('limits', previous) if previous else {'state':'missing'}
        result.latest_quotes = apply_live(selected, live_quotes(db, target, previous, cutoff),
            raw.get(previous, []), _dated_rows(old_prices, previous) or [], _dated_rows(old_limits, previous) or [], cutoff)
    codes = {r.code for d in result.days for r in [*d.rows, *d.eliminated]}
    quote_closed = quote_day < cutoff.astimezone(SHANGHAI).date() or cutoff.astimezone(SHANGHAI).time().replace(tzinfo=None) >= time(15)
    if quote_closed:
        same_day = unique(_dated_rows(reader.get('daily', quote_day), quote_day) or [])
        seeds = {r.code:r for d in result.days for r in [*d.rows, *d.eliminated]}
        for code in codes:
            if code not in same_day:
                continue
            quote, seed = same_day[code], seeds[code]
            current_row = next((r for d in result.days if d.day == quote_day for r in d.rows if r.code == code), None)
            item = (current_row or seed).model_copy(deep=True)
            item.day, item.price, item.pct_change = quote_day, num(quote.get('close'), positive=True), num(quote.get('pct_chg'))
            item.amount = num(quote.get('amount'))*1000 if num(quote.get('amount')) is not None else None
            item.price_basis = 'daily_close'
            if current_row is None:
                item.seal_amount = item.actual_float = item.actual_turnover = None
            result.latest_quotes[code] = item
    elif quote_day != target:
        from app.market.session import quote_fresh_at
        seeds = {r.code:r for d in result.days for r in [*d.rows, *d.eliminated]}
        for code, stock in live_quotes(db, quote_day, quote_previous, cutoff).items():
            if code not in seeds or stock.price_basis != 'intraday' or not stock.source_time:
                continue
            try:
                stamp = datetime.fromisoformat(stock.source_time)
                valid = stamp.utcoffset() is not None and stamp.astimezone(SHANGHAI).date() == quote_day and stamp <= cutoff and (stock.source_received_at is None or stock.source_received_at <= cutoff)
            except (ValueError,TypeError):
                valid = False
            if not valid or num(stock.price, positive=True) is None:
                continue
            item = seeds[code].model_copy(deep=True)
            item.day, item.price, item.pct_change = quote_day, stock.price, stock.pct_change
            item.amount, item.seal_amount, item.actual_float, item.actual_turnover = stock.amount_cny, stock.seal_amount_cny, None, None
            item.price_basis, item.source_time = 'intraday', stock.source_time
            item.received_at = str(stock.source_received_at) if stock.source_received_at else None
            item.stale = (quote_fresh_at(cutoff)-stamp).total_seconds() > 180
            result.latest_quotes[code] = item
    result.latest_quotes = {c:r for c,r in result.latest_quotes.items() if c in codes}
    # Thirty real sessions per cohort, not a chart-shaped placeholder. One
    # date query for all selected stocks avoids per-card requests/N+1 reads.
    for day in calendar[max(0, len(calendar)-lookback-29):]:
        if day == cutoff.astimezone(SHANGHAI).date() and cutoff.astimezone(SHANGHAI).time().replace(tzinfo=None) < time(15):
            continue
        table = selected_rows(reader, 'daily', day, codes)
        for code, row in unique(_dated_rows(table, day) or []).items():
            o,h,l,c = (num(row.get(k), positive=True) for k in ('open','high','low','close'))
            if None not in (o,h,l,c) and l <= min(o,c) <= max(o,c) <= h:
                result.candles.setdefault(code, []).append(LadderBar(day=day, open=o, high=h, low=l, close=c))
    result.days.reverse()
    result.state = 'complete' if all(d.state == 'complete' for d in result.days) else 'partial' if any(d.rows or d.eliminated for d in result.days) else 'missing'
    # Export remains a separate right. Also require every actual input source
    # (including calendar/live quotes) to allow it, rather than trusting UI.
    source_ids = {ref.get('source_id') for table in reader.cache.values() for ref in table.get('evidence_refs', [])}
    from app.models import Provider
    providers = [db.get(Provider, uuid.UUID(ident)) for ident in source_ids if ident]
    providers += auction_sources + [permitted_provider(db, 'tushare_calendar_'+exchange, cutoff) for exchange in ('sse','szse')]
    result.can_export = bool(providers) and selected.state == 'complete' and all(p and p.rights.get('export') == 'allowed' for p in providers)
    return result


def export_xlsx(view):
    if not view.can_export:
        raise APIError(403, 'export_permission_missing', '当前来源尚未授权导出。')
    rows = [['日期','名称','代码','板数','状态','涨幅%','最新价','首封','末封','封单/元','成交额/元','实际流通/元','实际换手%','题材']]
    for day in view.days:
        for r in [*day.rows, *day.eliminated]:
            rows.append([str(day.day),r.name,r.code,r.height,r.status,r.pct_change,r.price,r.first_time,r.last_time,r.seal_amount,r.amount,r.actual_float,r.actual_turnover,' / '.join(r.themes)])
    xml_rows = []
    for row in rows:
        cells = ['<c/>' if v is None else f'<c><v>{v}</v></c>' if isinstance(v,(float,int)) else '<c t="inlineStr"><is><t>'+escape(str(v))+'</t></is></c>' for v in row]
        xml_rows.append('<row>'+''.join(cells)+'</row>')
    buf = BytesIO()
    with ZipFile(buf, 'w', ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="连板观察" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(xml_rows)+'</sheetData></worksheet>')
    return buf.getvalue()
