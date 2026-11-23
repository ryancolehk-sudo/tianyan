"""Read-only projections for the fixed workbench, strictly over Tushare evidence.

The request path cannot collect. Review may include a later received dated copy;
an explicit as-of cutoff excludes every later receipt before choosing versions.
Daily funds/auction trades are never projected as minute flow/order-book data.
"""
from dataclasses import dataclass
from collections import OrderedDict
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import math
import re
import json
from threading import RLock
from uuid import UUID

from sqlalchemy import select, text

from app.auth import APIError
from app.evidence.common import now
from app.evidence.models import EvidenceVersion
from app.market.models import InstrumentOrigin, Watchlist
from app.market.protocol import SHANGHAI
from app.market.schemas import (WorkbenchAuctionDay, WorkbenchBreadth, WorkbenchCapability,
    WorkbenchFundPoint, WorkbenchIndex, WorkbenchSector, WorkbenchStock,
    WorkbenchView)
from app.market.trading_datasets import distinct_date_values, source_day
from app.models import Instrument, Provider
from app.market.market_series import FAMILIES as MARKET_FAMILIES, DATASETS as MARKET_DATASETS

MARKET = 'tushare_market_rt'
LIMITS = 'tushare_market_limits'
DAILY_FAMILIES = frozenset({'tushare_limit_list_d','tushare_limit_step',
    'tushare_stk_auction','tushare_moneyflow_ind_dc','tushare_moneyflow_dc',
    'tushare_dc_index','tushare_dc_member','tushare_stk_premarket','tushare_daily_basic'})
FAMILIES = DAILY_FAMILIES | MARKET_FAMILIES | {LIMITS, 'tushare_rt_indices', 'tushare_rt_k', 'tushare_stock_basic'}
SCOPE = '沪深京A股，含ST，排除ETF与B股；仅本批实际返回的可用股票。资金为盘后日频；封单与竞价委托节点待接入。'
LADDER_SCOPE = '沪深主板、非ST；缺少名称的样本不进入梯队。仅本批实际返回的可用股票；封单与竞价委托节点待接入。'
_BREADTH_CACHE = OrderedDict()
_BREADTH_LOCK = RLock()
_BREADTH_COMPUTE_LOCK = RLock()


def decimal(value):
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def numeric(value):
    number = decimal(value)
    result = float(number) if number is not None else None
    return result if result is not None and math.isfinite(result) else None


def percentage(price, previous):
    price, previous = decimal(price), decimal(previous)
    return float((price / previous - 1) * 100) if price is not None and previous and previous > 0 else None


def main_board(code, name):
    return bool(isinstance(code, str) and re.fullmatch(r'(?:60\d{4}\.SH|00\d{4}\.SZ)', code)
                and isinstance(name, str) and name.strip() and not re.search(r'ST|退', name, re.I))


def allowed_stock(code, name, view):
    if view == 'ladder':
        return main_board(code, name)
    return bool(isinstance(code, str) and re.fullmatch(r'(?:6\d{5}\.SH|(?:0[0-3]\d{4}|30\d{4})\.SZ|[489]\d{5}\.BJ)', code))


def source_clock(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(SHANGHAI) if parsed.tzinfo is not None else parsed.replace(tzinfo=SHANGHAI)
    except ValueError:
        pass
    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y%m%d%H%M%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=SHANGHAI)
        except ValueError:
            pass
    return None


def session(clock):
    if clock is None:
        return None
    clock = clock.astimezone(SHANGHAI)
    if time(9, 30) <= clock.time() <= time(11, 30):
        return clock.date(), 'am'
    if time(13) <= clock.time() <= time(15):
        return clock.date(), 'pm'
    return None


@dataclass(frozen=True)
class SavedTable:
    id: object
    family: str
    key: str
    received_at: datetime
    payload: dict

    def units(self):
        return {column['key']: column.get('unit') for column in self.payload.get('columns', [])}

    def rows(self, day=None):
        rows = self.payload.get('rows', [])
        return rows if day is None else [row for row in rows if source_day(row.get('trade_date')) == day]


def money(row, table, field):
    """Unknown units in immutable older evidence must remain unknown."""
    factor = {'元': Decimal(1), '万元': Decimal(10000), '亿元': Decimal(100000000),
              '千元': Decimal(1000)}.get(table.units().get(field))
    number = decimal(row.get(field))
    return numeric(number * factor) if number is not None and factor is not None else None


def five_minute_change(current, earlier, current_received, earlier_received):
    """Use supplier clocks, not polling clocks or a lunch-crossing price."""
    latest, previous = source_clock(current.get('trade_time')), source_clock(earlier.get('trade_time'))
    if not latest or not previous or session(latest) is None or session(latest) != session(previous):
        return None
    elapsed = (latest - previous).total_seconds()
    if abs(elapsed - 300) > 20:
        return None
    if any(not -60 <= (received - stamp).total_seconds() <= 120 for received, stamp in
           ((current_received, latest), (earlier_received, previous))):
        return None
    return percentage(current.get('close'), earlier.get('close'))


def permitted(provider):
    return bool(provider.configured and provider.enabled and provider.entitlement_status == 'verified'
                and provider.rights.get('display') == 'allowed' and provider.rights.get('cache') == 'allowed')


def table_metadata(db, providers, cutoff):
    ids = [provider.id for provider in providers.values() if permitted(provider)]
    if not ids:
        return []
    rows = []
    # Select only date/clock metadata first. rt_k has no trade_date: derive its
    # actual source dates from trade_time, never from the receiving day.
    for dated in (False, True):
        scope = [provider.id for family, provider in providers.items() if permitted(provider)
                 and (family not in MARKET_FAMILIES) == dated]
        if not scope:
            continue
        rows.extend(db.execute(select(EvidenceVersion.id, EvidenceVersion.source_id, EvidenceVersion.source_key,
            EvidenceVersion.received_at, EvidenceVersion.payload['observed_day'].astext.label('observed'),
            distinct_date_values('trade_date' if dated else 'trade_time').label('dates')).where(
            EvidenceVersion.source_id.in_(scope), EvidenceVersion.kind == 'reference',
            ~EvidenceVersion.source_key.startswith('review.'),
            EvidenceVersion.visibility == 'shared', EvidenceVersion.owner_id.is_(None),
            EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= cutoff,
            (EvidenceVersion.payload['dataset'].astext.in_(MARKET_DATASETS.values()) if not dated else True),
            (EvidenceVersion.published_at.is_(None) | (EvidenceVersion.published_at <= cutoff)),
            (EvidenceVersion.available_at.is_(None) | (EvidenceVersion.available_at <= cutoff)))
        .order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.version_number.desc(), EvidenceVersion.id.desc())))
    return sorted(rows, key=lambda row: (row.received_at, str(row.id)), reverse=True)


def actual_days(metadata, family):
    if family in MARKET_FAMILIES:
        return {stamp.date() for value in metadata.dates or [] if (stamp := source_clock(value)) is not None}
    return {day for value in metadata.dates or [] if (day := source_day(value)) is not None}


def load_tables(db, metadata, providers, target, cutoff, view='overview', *, market_family=MARKET):
    by_id = {provider.id: family for family, provider in providers.items()}
    selected = {}
    # Materialise only the latest dated daily copy per source key and day.
    # Intraday prices need at most eight minutes of bounded prior snapshots.
    market_rows = [row for row in metadata if by_id[row.source_id] == market_family and target in actual_days(row, market_family)]
    if market_family=='tencent_market_rt':
        from app.market.tencent_market import snapshot_metadata
        allowed={row.id for row in snapshot_metadata(db,providers[market_family],target,cutoff)}
        market_rows=[row for row in market_rows if row.id in allowed]
    newest = market_rows[0].received_at if market_rows else None
    keep = set()
    for row in metadata:
        family = by_id[row.source_id]
        if family in MARKET_FAMILIES:
            if row in market_rows and newest and newest - row.received_at <= timedelta(minutes=8):
                keep.add(row.id)
            continue
        if (family == 'tushare_stk_auction' and view != 'auction') or (
                family == 'tushare_moneyflow_dc' and view != 'funds'):
            continue
        dates = [value for raw in row.dates or [] if (value := source_day(raw)) is not None
                 and value <= target and value <= cutoff.astimezone(SHANGHAI).date()]
        # Only actual source dates, never the receiving date, identify daily data.
        for day in dates:
            if day < target - timedelta(days=45):
                continue
            selected.setdefault((family, row.source_key, day), row.id)
    family_days = {}
    for family, key, day in selected:
        family_days.setdefault(family, set()).add(day)
    wanted_days = {}
    for family, dates in family_days.items():
        ordered = sorted(dates, reverse=True)
        count = (10 if view == 'funds' else 1) if family == 'tushare_moneyflow_ind_dc' else 4 if family == 'tushare_stk_auction' else 6 if family in {
            'tushare_limit_list_d','tushare_limit_step'} else 1
        if family in {LIMITS,'tushare_stk_premarket','tushare_daily_basic'}:
            wanted_days[family] = {target}
        else:
            wanted_days[family] = set(ordered[:count])
    # A late same-day membership cannot be attached to earlier fund values.
    if view == 'funds' and family_days.get('tushare_moneyflow_ind_dc'):
        fund_day = max(family_days['tushare_moneyflow_ind_dc'])
        eligible = [day for day in family_days.get('tushare_dc_member', ()) if day <= fund_day]
        wanted_days['tushare_dc_member'] = {max(eligible)} if eligible else set()
    selected = {identity: ident for identity, ident in selected.items() if identity[2] in wanted_days.get(identity[0], set())}
    keep.update(selected.values())
    loaded = {evidence.id: SavedTable(evidence.id, by_id[evidence.source_id], evidence.source_key,
        evidence.received_at, evidence.payload) for evidence in db.scalars(
            select(EvidenceVersion).where(EvidenceVersion.id.in_(keep)))} if keep else {}
    daily = {}
    for (family, key, day), ident in selected.items():
        daily.setdefault(family, {}).setdefault(day, []).append(loaded[ident])
    snapshots = sorted((loaded[row.id] for row in market_rows if row.id in loaded),
                       key=lambda table: table.received_at, reverse=True)
    return daily, snapshots


def dated_rows(tables, family, day):
    return [(row, table) for table in tables.get(family, {}).get(day, []) for row in table.rows(day)]


def latest_day(tables, family, target):
    return max((day for day in tables.get(family, {}) if day <= target), default=None)


def code_rows(tables, family, day):
    # A source family has a stable identity. Overlapping instrument-specific
    # tables use the latest actual receipt; never take arbitrary database order.
    result = {}
    for row, table in sorted(dated_rows(tables, family, day), key=lambda pair: pair[1].received_at):
        result[row.get('ts_code')] = row, table
    return result


def build_projection(*, view, target, cutoff, available_days, tables, snapshots,
                     identities=None, names=None, selected_code=None, sector_code=None, watchlist_ids=None, mode='review'):
    """Pure projection core, also usable with synthetic evidence in unit tests."""
    identities = identities or {}
    names = names or {}
    result = WorkbenchView(view=view, trade_day=target, available_days=available_days,
        as_of_time=cutoff, mode=mode, scope=LADDER_SCOPE if view == 'ladder' else SCOPE, watchlist_ids=watchlist_ids or [])
    if target is None:
        return result
    current = snapshots[0] if snapshots else None
    limit_rows = code_rows(tables, LIMITS, target)
    native_limits=bool(current and current.family=='tencent_market_rt')
    if native_limits:
        limit_rows={row['ts_code']:(row,current) for row in current.rows()}
    premarket = code_rows(tables, 'tushare_stk_premarket', target)
    # Only a current-day premarket table is an acceptable limit-price fallback.
    if not native_limits:
        for code, value in premarket.items():
            limit_rows.setdefault(code, value)
    # Review may use current limits. Historical curve points are handled by
    # breadth_series separately, which enforces each point's earlier receipt.
    daily_stats = code_rows(tables, 'tushare_daily_basic', target)
    ladder_day = max((day for family in ('tushare_limit_list_d', 'tushare_limit_step')
                      for day in tables.get(family, {}) if day < target), default=None)
    result.cohort_day = ladder_day
    previous = {}
    if ladder_day:
        previous.update(code_rows(tables, 'tushare_limit_step', ladder_day))
        previous.update(code_rows(tables, 'tushare_limit_list_d', ladder_day))
    previous = {code: pair for code, pair in previous.items() if pair[0].get('limit', 'U') == 'U'}
    directory_day = latest_day(tables, 'tushare_dc_index', target)
    directory = code_rows(tables, 'tushare_dc_index', directory_day)
    sector_names = {code: row.get('name') or code for code, (row, _) in directory.items()}
    fund_day = latest_day(tables, 'tushare_moneyflow_ind_dc', target)
    member_day = latest_day(tables, 'tushare_dc_member', fund_day if view == 'funds' and fund_day else target)
    membership = {}
    if member_day:
        for row, _ in dated_rows(tables, 'tushare_dc_member', member_day):
            membership.setdefault(row.get('con_code'), []).append(row.get('ts_code'))
    for row, table in dated_rows(tables, 'tushare_moneyflow_ind_dc', fund_day):
        code = row.get('ts_code')
        if not code:
            continue
        sector_names.setdefault(code, row.get('name') or code)
        result.sectors.append(WorkbenchSector(code=code, name=sector_names[code],
            net_cny=money(row, table, 'net_amount'), pct_change=numeric(row.get('pct_change')),
            trade_day=fund_day))
    result.sectors.sort(key=lambda row: (row.net_cny is not None, row.net_cny or 0), reverse=True)
    for rank, sector in enumerate(result.sectors, 1):
        sector.rank = rank
    selected_sector = sector_code or (result.sectors[0].code if result.sectors else None)
    result.selected_sector = next((row for row in result.sectors if row.code == selected_sector), None)
    if selected_sector and result.selected_sector is None and selected_sector in sector_names:
        result.selected_sector = WorkbenchSector(code=selected_sector, name=sector_names[selected_sector])
    stock_fund_day = latest_day(tables, 'tushare_moneyflow_dc', target)
    stock_funds = code_rows(tables, 'tushare_moneyflow_dc', stock_fund_day) if view == 'funds' else {}
    universe = {}
    past_prices = [(past, {row.get('ts_code'): row for row in past.rows()}) for past in snapshots[1:]]
    if current:
        for row in current.rows():
            code, name = row.get('ts_code'), row.get('name')
            stamp = source_clock(row.get('trade_time'))
            if not allowed_stock(code, name, view) or stamp is None or stamp.date() != target or stamp > cutoff:
                continue
            item = WorkbenchStock(instrument_id=identities.get(code), ts_code=code, name=name or code,
                price=numeric(row.get('close')), pct_change=percentage(row.get('close'), row.get('pre_close')),
                amount_cny=numeric(row.get('amount')), source_time=stamp, received_at=current.received_at,
                current_state='未判定')
            price, high = decimal(row.get('close')), decimal(row.get('high'))
            limits = limit_rows.get(code)
            if limits:
                upper = decimal(limits[0].get('up_limit'))
                lower = decimal(limits[0].get('down_limit'))
                if upper and upper > 0 and price is not None:
                    # Remove only an immaterial binary tail at a quoted tick.
                    if abs(price - upper) <= Decimal('0.00000001'):
                        item.current_state = '触及涨停价'
                    elif high is not None and abs(high - upper) <= Decimal('0.00000001'):
                        item.current_state = '触板后回落'
                    else:
                        item.current_state = '未触涨停'
                if lower and lower > 0 and price is not None and abs(price - lower) <= Decimal('0.00000001'):
                    item.current_state = '触及跌停价'
            if code in previous:
                row_previous = previous[code][0]
                item.previous_height = int(numeric(row_previous.get('limit_times') or row_previous.get('nums')) or 1)
            if code in daily_stats:
                item.turnover_rate = numeric(daily_stats[code][0].get('turnover_rate'))
            elif code in premarket:
                shares = decimal(premarket[code][0].get('float_share'))
                volume = decimal(row.get('vol'))
                # rt_k volume is shares; premarket float_share is ten-thousand shares.
                if shares and shares > 0 and volume is not None and premarket[code][1].units().get('float_share') == '万股':
                    item.turnover_rate = float(volume / (shares * 100))
            sectors = sorted(set(value for value in membership.get(code, []) if value))
            if sectors:
                item.sector_code = selected_sector if selected_sector in sectors else sectors[0]
                item.theme = sector_names.get(item.sector_code, item.sector_code)
            if code in stock_funds:
                item.net_cny = money(*stock_funds[code], 'net_amount')
                item.fund_trade_day = stock_fund_day
            for past, prices in past_prices:
                elapsed = (current.received_at - past.received_at).total_seconds()
                if not 180 <= elapsed <= 420:
                    continue
                old = prices.get(code)
                if old:
                    value = five_minute_change(row, old, current.received_at, past.received_at)
                    if value is not None:
                        item.change_5m = value
                        break
            universe[code] = item
    # The previous cohort is useful even when current quote coverage is absent.
    for code, (row, table) in previous.items():
        if code in universe or not main_board(code, row.get('name')):
            continue
        universe[code] = WorkbenchStock(instrument_id=identities.get(code), ts_code=code,
            name=row.get('name'), previous_height=int(numeric(row.get('limit_times') or row.get('nums')) or 1),
            received_at=table.received_at, current_state='当日行情缺失')
    stocks = sorted(universe.values(), key=lambda stock: (stock.pct_change is not None, stock.pct_change or 0), reverse=True)
    if view == 'funds':
        for code, (row, table) in stock_funds.items():
            if code in universe or not allowed_stock(code, row.get('name') or names.get(code), view):
                continue
            item = WorkbenchStock(instrument_id=identities.get(code), ts_code=code,
                name=row.get('name') or names.get(code) or code, price=numeric(row.get('close')),
                pct_change=numeric(row.get('pct_change')), net_cny=money(row, table, 'net_amount'),
                fund_trade_day=stock_fund_day, received_at=table.received_at, current_state='盘后参考')
            universe[code] = item
            member_sectors = sorted(set(value for value in membership.get(code, []) if value))
            if member_sectors:
                item.sector_code = selected_sector if selected_sector in member_sectors else member_sectors[0]
                item.theme = sector_names.get(item.sector_code, item.sector_code)
            stocks.append(item)
    result.stocks = stocks
    if view == 'funds' and selected_sector:
        members = {code for code, sectors in membership.items() if selected_sector in sectors}
        result.stocks = [stock for stock in stocks if stock.ts_code in members]
    if view == 'ladder':
        result.stocks = sorted((stock for stock in stocks if stock.previous_height),
            key=lambda stock: (stock.previous_height, stock.pct_change or -100), reverse=True)
    if selected_code:
        result.selected_stock = universe.get(selected_code)
    elif result.stocks:
        result.selected_stock = result.stocks[0]
    quote_stocks = [stock for stock in stocks if stock.source_time is not None]
    if quote_stocks:
        result.stats.advancing = sum(stock.pct_change > 0 for stock in quote_stocks if stock.pct_change is not None)
        result.stats.declining = sum(stock.pct_change < 0 for stock in quote_stocks if stock.pct_change is not None)
        amounts = [stock.amount_cny for stock in quote_stocks if stock.amount_cny is not None]
        result.stats.turnover_cny = sum(amounts) if amounts and len(amounts) == len(quote_stocks) else None
        if any(stock.current_state != '未判定' for stock in quote_stocks):
            result.stats.limit_up = sum(stock.current_state == '触及涨停价' for stock in quote_stocks)
            result.stats.limit_down = sum(stock.current_state == '触及跌停价' for stock in quote_stocks)
            result.stats.broken_count = sum(stock.current_state == '触板后回落' for stock in quote_stocks)
    heights = [int(numeric(row.get('limit_times') or row.get('nums')) or 1)
        for family in ('tushare_limit_list_d', 'tushare_limit_step')
        for row, _ in dated_rows(tables, family, target)
        if main_board(row.get('ts_code'), row.get('name')) and row.get('limit', 'U') == 'U']
    result.stats.max_height = max(heights) if heights else None
    auction_days = sorted((day for day in tables.get('tushare_stk_auction', {}) if day <= target), reverse=True)[:4] if view == 'auction' else []
    watched = set(result.watchlist_ids)
    retained_codes = {code for code, ident in identities.items() if ident in watched}
    if selected_code:
        retained_codes.add(selected_code)
    selected_auction = None
    for day in auction_days:
        candidates = []
        for row, table in dated_rows(tables, 'tushare_stk_auction', day):
            code = row.get('ts_code')
            name = row.get('name') or names.get(code) or (universe[code].name if code in universe else None)
            if not allowed_stock(code, name, view):
                continue
            candidates.append((row, table, code, name, money(row, table, 'amount')))
        candidates.sort(key=lambda entry: (entry[4] is not None, entry[4] or 0), reverse=True)
        # Choose from every actual row before bounding the response. The saved
        # raw table remains complete; only the visible DTOs are materialised.
        actual_selected = next((entry for entry in candidates if entry[2] == selected_code), None)
        visible_codes = retained_codes | {entry[2] for entry in candidates[:20]}
        rows = []
        for row, table, code, name, amount in candidates:
            if code not in visible_codes:
                continue
            # Historical prices/names do not borrow today's performance.
            item = WorkbenchStock(instrument_id=identities.get(code), ts_code=code, name=name or code,
                auction_price=numeric(row.get('price')), auction_amount_cny=amount,
                auction_pct_change=percentage(row.get('price'), row.get('pre_close')),
                received_at=table.received_at, current_state='竞价成交参考')
            rows.append(item)
        if actual_selected is not None and selected_auction is None:
            selected_auction = next((stock for stock in rows if stock.ts_code == selected_code), None)
        result.auction_days.append(WorkbenchAuctionDay(day=day, total_count=len(candidates), rows=rows))
    if view == 'auction' and result.auction_days:
        result.stocks = next((day.rows for day in result.auction_days if day.day == target), [])
        result.selected_stock = selected_auction if selected_code else (result.stocks[0] if result.stocks else None)
    # Missing from a bounded successor list is not proof of a broken streak.
    # Only an explicit failed-board daily row confirms that day in this v1.
    broken = {}
    for day in sorted(tables.get('tushare_limit_list_d', {}))[-4:]:
        if day >= target:
            continue
        for row, _ in dated_rows(tables, 'tushare_limit_list_d', day):
            code = row.get('ts_code')
            height = numeric(row.get('limit_times'))
            if row.get('limit') == 'Z' and height and height >= 3 and code in universe:
                item = universe[code].model_copy()
                item.previous_height = int(height)
                broken[code] = item
    result.broken_stocks = sorted(broken.values(), key=lambda row: row.previous_height or 0, reverse=True)
    for day in sorted(tables.get('tushare_moneyflow_ind_dc', {}))[-10:] if view == 'funds' else []:
        for row, table in dated_rows(tables, 'tushare_moneyflow_ind_dc', day):
            result.fund_history.append(WorkbenchFundPoint(day=day, sector_code=row['ts_code'],
                net_cny=money(row, table, 'net_amount'), pct_change=numeric(row.get('pct_change'))))
    return result


def read_breadth(db, metadata, families, target, cutoff, view, *, market_family=MARKET):
    """Aggregate saved raw rows inside PostgreSQL, transferring only scalars.

    Each tiny cached point binds immutable snapshot/limit identities and scope.
    Permissions and receipt cutoffs are applied before reaching this function.
    A future-tolerant source clock is not cached past a different as-of cutoff.
    """
    if market_family=='tencent_market_rt' and view!='ladder':
        from app.market.tencent_market import breadth_series
        series=breadth_series(db,target,cutoff)
        return [WorkbenchBreadth(time=point.source_time_max,up=point.limit_up,down=point.limit_down)
            for point in (series.items if series else []) if point.source_time_max]
    snapshots = [row for row in metadata if families[row.source_id] == market_family
                 and target in actual_days(row, market_family)][:500]
    if market_family=='tencent_market_rt':
        from app.market.tencent_market import snapshot_metadata
        provider=db.get(Provider,snapshots[0].source_id) if snapshots else None
        allowed={row.id for row in snapshot_metadata(db,provider,target,cutoff)} if provider else set()
        snapshots=[row for row in snapshots if row.id in allowed]
    limits = [row for row in metadata if families[row.source_id] in {LIMITS, 'tushare_stk_premarket'}
              and target in {source_day(value) for value in row.dates or []}]
    entries, pending, points = [], [], {}
    scope = 'ladder' if view == 'ladder' else 'general'
    for meta in snapshots:
        known = next((row for row in limits if families[row.source_id] == LIMITS and row.received_at <= meta.received_at), None) if market_family==MARKET else None
        if known is None and market_family==MARKET:
            known = next((row for row in limits if row.received_at <= meta.received_at), None)
        precise = cutoff if cutoff < meta.received_at + timedelta(seconds=60) else None
        key = (meta.id, known.id if known else None, scope, precise)
        entries.append(key)
        with _BREADTH_LOCK:
            cached = _BREADTH_CACHE.get(key)
            if cached is not None:
                _BREADTH_CACHE.move_to_end(key)
                points[key] = cached
                continue
        pending.append({'snapshot_id':str(meta.id),'limit_id':str(known.id) if known else None})
    if pending:
        # Raw rows were validated by the market adapter. Canonicalise only the
        # same <=1e-8 binary tail used by price_decimal, retaining other prices.
        sql = text(r'''
WITH pairs AS MATERIALIZED (
    SELECT snapshot_id::uuid, limit_id::uuid
    FROM jsonb_to_recordset(CAST(:pairs AS jsonb)) AS p(snapshot_id text, limit_id text)
), limit_rows AS MATERIALIZED (
    SELECT e.id, jsonb_object_agg(r->>'ts_code', jsonb_build_array(
        CASE WHEN abs(NULLIF(r->>'up_limit','')::numeric-round(NULLIF(r->>'up_limit','')::numeric,2))<=0.00000001
            THEN round(NULLIF(r->>'up_limit','')::numeric,2) ELSE NULLIF(r->>'up_limit','')::numeric END,
        CASE WHEN abs(NULLIF(r->>'down_limit','')::numeric-round(NULLIF(r->>'down_limit','')::numeric,2))<=0.00000001
            THEN round(NULLIF(r->>'down_limit','')::numeric,2) ELSE NULLIF(r->>'down_limit','')::numeric END)) AS prices
    FROM evidence_versions e
    CROSS JOIN LATERAL jsonb_array_elements(e.payload->'rows') AS r
    WHERE e.id IN (SELECT DISTINCT limit_id FROM pairs WHERE limit_id IS NOT NULL)
      AND r->>'trade_date' IN (:compact_day, :target)
    GROUP BY e.id
)
SELECT p.snapshot_id, a.* FROM pairs p JOIN evidence_versions e ON e.id=p.snapshot_id
LEFT JOIN limit_rows l ON l.id=p.limit_id
CROSS JOIN LATERAL (
 WITH raw AS (
    SELECT r->>'ts_code' AS code,
        (r->>'ts_code' ~ '^(60[0-9]{4}\.SH|00[0-9]{4}\.SZ)$'
            AND NULLIF(trim(r->>'name'),'') IS NOT NULL AND r->>'name' !~* 'ST|退') AS ladder,
        NULLIF(r->>'close','')::numeric AS raw_price,
        NULLIF(r->>'pre_close','')::numeric AS previous_price,
        (CASE WHEN :native_limits THEN r->>'up_limit' ELSE l.prices->(r->>'ts_code')->>0 END)::numeric AS upper_price,
        (CASE WHEN :native_limits THEN r->>'down_limit' ELSE l.prices->(r->>'ts_code')->>1 END)::numeric AS lower_price,
        CASE WHEN r->>'trade_time' ~ '^\d{14}$'
            THEN (substring(r->>'trade_time',1,4)||'-'||substring(r->>'trade_time',5,2)||'-'||
                  substring(r->>'trade_time',7,2)||' '||substring(r->>'trade_time',9,2)||':'||
                  substring(r->>'trade_time',11,2)||':'||substring(r->>'trade_time',13,2))
            WHEN r->>'trade_time' ~ '(Z|[+-]\d{2}:\d{2})$'
                THEN to_char((r->>'trade_time')::timestamptz AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS.US')
            ELSE replace(NULLIF(r->>'trade_time',''),'T',' ') END AS source_clock
    FROM jsonb_array_elements(e.payload->'rows') AS r
    WHERE r->>'ts_code' ~ '^(6[0-9]{5}\.SH|(0[0-3][0-9]{4}|30[0-9]{4})\.SZ|[489][0-9]{5}\.BJ)$'
), typed AS MATERIALIZED (
    SELECT *, CASE WHEN abs(raw_price-round(raw_price,2))<=0.00000001
        THEN round(raw_price,2) ELSE raw_price END AS price
    FROM raw
    WHERE left(source_clock,10)=:target AND source_clock<=:cutoff
)
 SELECT max(t.source_clock) AS source_time, max(t.source_clock) FILTER (WHERE t.ladder) AS ladder_time,
    count(*) FILTER (WHERE t.price>0 AND t.previous_price>0 AND t.upper_price>0 AND t.lower_price>0) AS comparable,
    count(*) FILTER (WHERE t.price>0 AND t.previous_price>0 AND t.upper_price>0 AND t.lower_price>0
        AND t.price=t.upper_price) AS up,
    count(*) FILTER (WHERE t.price>0 AND t.previous_price>0 AND t.upper_price>0 AND t.lower_price>0
        AND t.price=t.lower_price) AS down,
    count(*) FILTER (WHERE t.ladder AND t.price>0 AND t.previous_price>0 AND t.upper_price>0 AND t.lower_price>0) AS ladder_comparable,
    count(*) FILTER (WHERE t.ladder AND t.price>0 AND t.previous_price>0 AND t.upper_price>0 AND t.lower_price>0
        AND t.price=t.upper_price) AS ladder_up,
    count(*) FILTER (WHERE t.ladder AND t.price>0 AND t.previous_price>0 AND t.upper_price>0 AND t.lower_price>0
        AND t.price=t.lower_price) AS ladder_down
 FROM typed t
) a
''')
        key_by_id = {key[0]:key for key in entries}
        # Keep each statement bounded under concurrent load. A single-flight
        # lock prevents simultaneous cold requests expanding the same large raw
        # batches. Earlier chunks remain reusable if a later request is stopped.
        for start in range(0, len(pending), 24):
            with _BREADTH_COMPUTE_LOCK:
                chunk = []
                for pair in pending[start:start+24]:
                    key = key_by_id[UUID(pair['snapshot_id'])]
                    with _BREADTH_LOCK:
                        if key in _BREADTH_CACHE:
                            points[key] = _BREADTH_CACHE[key]
                        else:
                            chunk.append(pair)
                if not chunk:
                    continue
                values = {}
                for row in db.execute(sql, {'pairs':json.dumps(chunk), 'compact_day':target.strftime('%Y%m%d'),
                        'native_limits':market_family=='tencent_market_rt',
                        'target':target.isoformat(), 'cutoff':cutoff.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S.%f')}):
                    values[row.snapshot_id] = {
                        'general': WorkbenchBreadth(time=source_clock(row.source_time), up=row.up if row.comparable else None,
                            down=row.down if row.comparable else None) if row.source_time else False,
                        'ladder': WorkbenchBreadth(time=source_clock(row.ladder_time), up=row.ladder_up if row.ladder_comparable else None,
                            down=row.ladder_down if row.ladder_comparable else None) if row.ladder_time else False}
                for pair in chunk:
                    key = key_by_id[UUID(pair['snapshot_id'])]
                    points[key] = values.get(key[0], {}).get(scope, False)
                    with _BREADTH_LOCK:
                        for point_scope, point in values.get(key[0], {scope:False}).items():
                            point_key = (key[0], key[1], point_scope, key[3])
                            _BREADTH_CACHE[point_key] = point
                            _BREADTH_CACHE.move_to_end(point_key)
                        while len(_BREADTH_CACHE) > 4096:
                            _BREADTH_CACHE.popitem(last=False)
    result = []
    for key in reversed(entries):
        point = points[key]
        if point is False or session(point.time) is None or result and point.time <= result[-1].time:
            continue
        result.append(point.model_copy())
    return result


def workbench_view(db, owner_id, *, view, trade_day=None, instrument_id=None, sector_code=None, as_of_time=None):
    clock = now(db)
    if as_of_time and as_of_time > clock:
        raise APIError(422, 'future_cutoff', '观察截止时点不能晚于服务器当前时间。')
    cutoff = as_of_time or clock
    if trade_day and trade_day > cutoff.astimezone(SHANGHAI).date():
        raise APIError(422, 'future_trade_day', '观察日期不能晚于查询截止日期。')
    providers = {provider.family: provider for provider in db.scalars(select(Provider).where(Provider.family.in_(FAMILIES)))}
    metadata = table_metadata(db, providers, cutoff)
    family_by_id = {provider.id: family for family, provider in providers.items()}
    days = set()
    for row in metadata:
        days.update(day for day in actual_days(row, family_by_id[row.source_id]) if day <= cutoff.astimezone(SHANGHAI).date())
    available = sorted(days, reverse=True)
    target = trade_day or (available[0] if available else None)
    from app.market.market_series import choose
    market_family=choose(db,providers,target,cutoff,historical=as_of_time is not None) if target else MARKET
    tables, snapshots = load_tables(db, metadata, providers, target, cutoff, view,market_family=market_family) if target else ({}, [])
    identities = {}
    names = {}
    selected_code = None
    # Only stable local IDs are reused. Display names always come from the
    # permitted Tushare evidence, never another source's cached identity name.
    for instrument in db.scalars(select(Instrument).where(Instrument.asset_type == 'stock')):
        suffix = {'SSE': 'SH', 'SZSE': 'SZ', 'BSE': 'BJ'}.get(instrument.exchange)
        if suffix:
            code = instrument.symbol + '.' + suffix
            identities[code] = instrument.id
            if instrument.id == instrument_id:
                selected_code = code
    catalog = providers.get('tushare_stock_basic')
    if catalog and permitted(catalog):
        for instrument, origin in db.execute(select(Instrument, InstrumentOrigin).join(InstrumentOrigin,
            InstrumentOrigin.instrument_id == Instrument.id).where(InstrumentOrigin.source_id == catalog.id,
            InstrumentOrigin.last_observed_at <= cutoff)):
            suffix = {'SSE':'SH','SZSE':'SZ','BSE':'BJ'}.get(instrument.exchange)
            if suffix:
                names[instrument.symbol + '.' + suffix] = origin.observed_name
    if instrument_id is not None and selected_code is None:
        raise APIError(404, 'instrument_not_found', '所选股票不存在。')
    saved_watchlist = db.get(Watchlist, owner_id)
    result = build_projection(view=view, target=target, cutoff=cutoff, available_days=available,
        tables=tables, snapshots=snapshots, identities=identities, names=names, selected_code=selected_code,
        sector_code=sector_code + '.DC' if sector_code and '.' not in sector_code else sector_code,
        watchlist_ids=saved_watchlist.instrument_ids if saved_watchlist else [],
        mode='as_of' if as_of_time else 'review')
    if target:
        index_provider = providers.get('tushare_rt_indices')
        if index_provider and permitted(index_provider):
            quotes = db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == index_provider.id,
                EvidenceVersion.kind == 'quote', EvidenceVersion.visibility == 'shared', EvidenceVersion.owner_id.is_(None),
                EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= cutoff,
                EvidenceVersion.payload['quote_time'].astext >= target.isoformat(),
                EvidenceVersion.payload['quote_time'].astext < (target + timedelta(days=1)).isoformat())
                .order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.version_number.desc()))
            seen = set()
            for evidence in quotes:
                stamp = source_clock(evidence.payload.get('quote_time', '').replace('+08:00', ''))
                if stamp is None:
                    try:
                        stamp = datetime.fromisoformat(evidence.payload.get('quote_time', ''))
                    except ValueError:
                        continue
                if stamp > cutoff or evidence.instrument_id in seen:
                    continue
                instrument = db.get(Instrument, evidence.instrument_id)
                if instrument is None:
                    continue
                seen.add(evidence.instrument_id)
                code = instrument.symbol + '.' + {'SSE': 'SH', 'SZSE': 'SZ'}[instrument.exchange]
                result.indices.append(WorkbenchIndex(code=code, name=evidence.payload.get('instrument_name') or code,
                    price=numeric(evidence.payload.get('price')), pct_change=percentage(evidence.payload.get('price'), evidence.payload.get('previous_close')),
                    source_time=stamp, received_at=evidence.received_at))
        if view in {'overview', 'ladder'}:
            result.breadth = read_breadth(db, metadata, family_by_id, target, cutoff, view,market_family=market_family)
    modules = {'market_quotes': market_family, 'auction_trade': 'tushare_stk_auction',
        'ladder_daily': 'tushare_limit_list_d', 'sector_funds_daily': 'tushare_moneyflow_ind_dc',
        'stock_funds_daily': 'tushare_moneyflow_dc', 'sector_members': 'tushare_dc_member'}
    for key, family in modules.items():
        provider = providers.get(family)
        used = snapshots if family in MARKET_FAMILIES else [table for values in tables.get(family, {}).values() for table in values]
        # A page need not decode an unrelated daily section to report that its
        # permitted immutable cache exists. The same cutoff already filtered it.
        if family in {'tushare_stk_auction', 'tushare_moneyflow_dc'} and not used and provider:
            used = [row for row in metadata if row.source_id == provider.id and target is not None
                    and any(target - timedelta(days=45) <= day <= target for day in actual_days(row, family))]
        status = 'not_configured' if provider is None or not provider.configured else 'permission_missing' if not permitted(provider) else 'partial' if used else 'empty'
        latest = max((table.received_at for table in used), default=None)
        reason = ('尚未配置此Tushare连接。' if status == 'not_configured' else
            '来源未启用或缺少已核实的展示与缓存权限。' if status == 'permission_missing' else
            '查询日没有可用的本机已存证据；本页面不会触发采集。' if not used else
            '本机有界缓存；统计日期和接收时点分开，未验证全市场完整覆盖。')
        result.capabilities.append(WorkbenchCapability(key=key, status=status, reason=reason, received_at=latest))
    result.capabilities.extend([
        WorkbenchCapability(key='auction_order', status='unavailable', reason='Tushare竞价表为成交结果，不能提供09:15、09:20、09:25封单节点。'),
        WorkbenchCapability(key='seal_book', status='unavailable', reason='买一盘口字段尚未取得可用数据；价格达到涨停价不能据此认定封住。'),
        WorkbenchCapability(key='fund_intraday', status='unavailable', reason='DC资金为盘后日频，分钟资金、近5分钟资金变化与盘中名次变化待接入。'),
        WorkbenchCapability(key='ai_observation', status='unavailable', reason='AI观察及预案核对框架已预留，当前尚未接入分析。')])
    return result
