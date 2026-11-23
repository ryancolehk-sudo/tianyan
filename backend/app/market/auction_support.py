"""Yesterday's confirmed limit cohort and today's matched auction, read only."""
from collections import Counter
from datetime import date, time
from decimal import Decimal, InvalidOperation
import re
from typing import Literal

from pydantic import AwareDatetime, Field

from app.evidence.common import now
from app.market.protocol import SHANGHAI
from app.market.terminal_read import (SavedTables, cny, number, current_rows, price_universe,
    live_snapshot_overlay, apply_snapshot_prices)
from app.market.review_analysis import ArchiveReader
from app.market.review_read import _referenced, _dated_rows
from app.market.review_group_fields import enrich_groups
from app.market.terminal_archives import _remember
from app.market.session import market_session
from app.schemas import DTO
from app.market.auction_down import AuctionDownDay, downside_day


class AuctionSupportRow(DTO):
    code: str
    name: str
    market: str
    previous_state: str | None = None
    previous_height: int | None = None
    themes: list[str] = Field(default_factory=list)
    auction_pct: float | None = None
    support_pct: float | None = None
    auction_amount_cny: float | None = None
    auction_turnover_pct: float | None = None
    auction_volume_ratio: float | None = None
    pct_change: float | None = None
    current_seal_cny: float | None = None
    current_bid_cny: float | None = None
    auction_net_cny: float | None = None
    amount_cny: float | None = None
    volume_change_pct: float | None = None
    actual_float_cny: float | None = None
    float_mv_cny: float | None = None
    previous_type: str | None = None
    previous_close: float | None = None
    previous_amount_cny: float | None = None
    previous_turnover_pct: float | None = None
    previous_seal_cny: float | None = None
    previous_seal_time: str | None = None
    previous_open_times: int | None = None
    popularity_rank: int | None = None
    change_reason: str | None = None
    lhb_status: Literal['listed', 'not_listed', 'unavailable'] = 'unavailable'
    lhb_reasons: list[str] = Field(default_factory=list)
    auction_at_limit: bool | None = None
    auction_at_down_limit: bool | None = None
    at_limit: bool | None = None
    at_down_limit: bool | None = None
    previous_broken: bool = False
    previous_lhb: bool = False
    price_basis: str | None = None
    source_time: str | None = None
    source_received_at: AwareDatetime | None = None


class AuctionSupportSource(DTO):
    dataset: str
    trade_day: date | None = None
    state: str
    received_at: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class AuctionSupportView(DTO):
    trade_day: date
    previous_trade_day: date | None = None
    available_days: list[date] = Field(default_factory=list)
    as_of_time: AwareDatetime
    state: Literal['available', 'unavailable']
    rows: list[AuctionSupportRow] = Field(default_factory=list)
    down_days: list[AuctionDownDay] = Field(default_factory=list)
    up_trial_days: list[AuctionDownDay] = Field(default_factory=list)
    sources: list[AuctionSupportSource] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    support_basis: str = '竞价成交量（股）÷昨日全天成交量（股）×100%；分母缺失或为0则不可计算。'
    volume_basis: str = '当日累计成交量÷昨日全天成交量×100%；不是竞价量比。'


def finite(value, *, nonnegative=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and (not nonnegative or result >= 0) else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def ratio(numerator, denominator):
    a, b = finite(numerator, nonnegative=True), finite(denominator, nonnegative=True)
    return float(a / b * 100) if a is not None and b is not None and b > 0 else None


def integer(value):
    result = finite(value, nonnegative=True)
    return int(result) if result is not None and result == int(result) else None


def native_clock(value):
    text = str(value) if value is not None and not isinstance(value, bool) else ''
    if re.fullmatch(r'\d{5,6}', text):
        text = text.zfill(6)
        text = f'{text[:2]}:{text[2:4]}:{text[4:]}'
    if re.fullmatch(r'\d{2}:\d{2}:\d{2}', text):
        try:
            return time.fromisoformat(text).isoformat()
        except ValueError:
            pass
    return None


def unique(rows):
    counts = Counter(row.get('ts_code') for row in rows)
    return {row['ts_code']: row for row in rows if isinstance(row.get('ts_code'), str)
            and re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', row['ts_code']) and counts[row['ts_code']] == 1}


def market(code):
    return ('北交所' if code.endswith('.BJ') else '科创板' if code.startswith('688')
            else '创业板' if code.startswith(('300', '301')) else '沪主板' if code.endswith('.SH') else '深主板')


def build_rows(stocks, previous, current_daily, previous_daily, auctions, auction_units,
               basics, previous_basics, limits, old_limits, details, node_tags, lhb,
               *, auction_available=True, previous_lhb_available=False, current_daily_available=False, live_volumes=None):
    """All inputs are exact-day, authorized archives bounded by one receipt cutoff."""
    result = []
    for stock in stocks:
        code = stock.code
        old, daily, prior = previous.get(code, {}), current_daily.get(code, {}), previous_daily.get(code, {})
        auction, columns = auctions.get(code, {}), auction_units.get(code, {})
        basic, prior_basic, restriction = basics.get(code, {}), previous_basics.get(code, {}), limits.get(code, {})
        if not auction_available:
            auction = {}
        auction_volume = number(auction.get('vol')) if columns.get('vol') == '股' else None
        old_volume = number(prior.get('vol'), 100)
        current_volume = number(daily.get('vol'), 100) if current_daily_available else (live_volumes or {}).get(code)
        price = finite(stock.price, nonnegative=True)
        free_shares = finite(basic.get('free_share', prior_basic.get('free_share')), nonnegative=True)
        float_shares = finite(basic.get('float_share', prior_basic.get('float_share')), nonnegative=True)
        upper, lower = number(restriction.get('up_limit')), number(restriction.get('down_limit'))
        auction_price = number(auction.get('price'))
        original_close = number(auction.get('pre_close'))
        tags = [tag.strip() for tag in node_tags.get(code, []) if isinstance(tag, str) and tag.strip()]
        if not tags and details.get(code, {}).get('lu_desc'):
            tags = [part.strip() for part in re.split(r'[+＋、;；]', details[code]['lu_desc']) if part.strip()][:8]
        tags = [tag for tag in tags if not re.fullmatch(r'(?:昨)?(?:\d+板|首板)|昨日涨停', tag)]
        board = integer(old.get('limit_times'))
        source_seal = stock.seal_amount_cny
        if stock.price_basis == 'daily_close' and stock.daily_pool is not None:
            source_seal = stock.daily_pool.seal_amount_cny
        old_seal = number(old.get('seal_amount_yuan'))
        broken_price, old_upper = number(prior.get('close')), number(old_limits.get(code, {}).get('up_limit'))
        result.append(AuctionSupportRow(code=code, name=stock.name, market=market(code),
            previous_state=old.get('limit'), previous_height=board if board and board > 0 else None,
            themes=list(dict.fromkeys(tags)),
            auction_pct=(auction_price / original_close - 1) * 100 if auction_price is not None and original_close is not None and original_close > 0 else None,
            support_pct=ratio(auction_volume, old_volume), auction_amount_cny=cny(auction, 'amount', columns),
            auction_turnover_pct=number(auction.get('turnover_rate')), auction_volume_ratio=number(auction.get('volume_ratio')),
            pct_change=stock.pct_change, current_seal_cny=source_seal,
            current_bid_cny=source_seal if stock.price is not None and upper is not None and stock.price == upper else None,
            amount_cny=stock.amount_cny, volume_change_pct=ratio(current_volume, old_volume),
            actual_float_cny=float(free_shares * 10000 * price) if free_shares is not None and price is not None and price > 0 else None,
            float_mv_cny=stock.float_mv_cny if stock.float_mv_cny is not None else float(float_shares * 10000 * price) if float_shares is not None and price is not None and price > 0 else None,
            previous_type=old.get('limit_type'), previous_close=number(prior.get('close')),
            previous_amount_cny=number(prior.get('amount'), 1000), previous_turnover_pct=number(prior_basic.get('turnover_rate')),
            previous_seal_cny=old_seal, previous_seal_time=native_clock(old.get('last_time')), previous_open_times=integer(old.get('open_times')),
            change_reason=old.get('change_reason'), lhb_status=old.get('lhb_status', 'unavailable'),
            lhb_reasons=old.get('lhb_reasons', []),
            auction_at_limit=auction_price == upper if auction_price is not None and upper is not None else None,
            auction_at_down_limit=auction_price == lower if auction_price is not None and lower is not None else None,
            at_limit=stock.price == upper if stock.price is not None and upper is not None else None,
            at_down_limit=stock.price == lower if stock.price is not None and lower is not None else None,
            previous_broken=bool(old.get('_prior_limit') and broken_price is not None and old_upper is not None and broken_price < old_upper),
            previous_lhb=previous_lhb_available and code in lhb,
            price_basis=stock.price_basis, source_time=stock.source_time, source_received_at=stock.source_received_at))
    return result


def support_view(db, owner_id, *, trade_day=None, as_of_time=None):
    clock = now(db)
    cutoff = as_of_time or clock
    from app.auth import APIError
    if cutoff > clock or trade_day is not None and trade_day > cutoff.astimezone(SHANGHAI).date():
        raise APIError(422, 'future_cutoff', '日期或观察截止时间不能晚于当前时间。')
    reader = SavedTables(db, cutoff, trade_day)
    session = market_session(db, cutoff)
    target = trade_day or (session.trade_day if session.current_day_active and as_of_time is None else None)
    if target is None:
        from app.market.auction_book_read import latest_book_day
        candidates=[next(iter(reader.dates('tushare_stk_auction', cutoff.astimezone(SHANGHAI).date())), None),latest_book_day(reader)]
        target=max((value for value in candidates if value is not None),default=None)
    target = target or max(reader.available_days, default=cutoff.astimezone(SHANGHAI).date())
    previous = reader.previous_day(target)
    before = reader.previous_day(previous) if previous else None
    archives = ArchiveReader(db, cutoff)
    def table(dataset, day):
        value = archives.get(dataset, day)
        # Only the explicit completed original-day table is eligible here.
        rows = _dated_rows(value, day) or []
        return value, unique([_referenced(row, value, index) for index, row in enumerate(rows)]) if value.get('state') == 'complete' else {}
    old_table, old = table('pools', previous)
    current_pool_table, current_pool = table('pools', target)
    detail_table, detail = table('pool_details', previous)
    current_detail_table, current_detail = table('pool_details', target)
    basic_table, basic = table('group_basics', target)
    old_basic_table, old_basic = table('group_basics', previous)
    daily_table, daily = table('daily', target)
    prior_table, prior = table('daily', previous)
    lhb_table, _ = table('lhb', previous)
    lhb_values = (_dated_rows(lhb_table, previous) or []) if lhb_table.get('state') == 'complete' else []
    lhb_rows = {row['ts_code'] for row in lhb_values if isinstance(row.get('ts_code'), str)
                and re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', row['ts_code'])}
    _, prior_pool = table('pools', before)
    enriched = [dict(row) for row in old.values()]
    unit_maps = archives.group_units(old_table)
    # Native THS seal values are accepted only by the existing price/quantity check.
    enrich_groups(enriched, old_table, unit_maps, lhb_table, lhb_values if lhb_table.get('state') == 'complete' else None,
                  list(detail.values()), list(old_basic.values()))
    old = {row['ts_code']: row for row in enriched}
    for code, row in prior_pool.items():
        if row.get('limit') == 'U':
            old.setdefault(code, {})['_prior_limit'] = True
    auctions = reader.table('tushare_stk_auction', target)
    auction_map = unique([row for row, _, _ in auctions])
    auction_units = {row['ts_code']: cols for row, cols, _ in auctions if row.get('ts_code') in auction_map}
    limits = unique([row for row, _, _ in reader.table('tushare_market_limits', target)])
    old_limits = unique([row for row, _, _ in reader.table('tushare_market_limits', previous)]) if previous else {}
    tags = {}
    if previous and (batch := reader.auction.batch(previous)):
        from app.market.terminal_auction import stock_code
        tags = {stock_code(row.get('code')): (row.get('tags_raw') or '').split('|') for row in batch.rows}
    # Reuse the existing price projection once, without building chart histories,
    # board memberships or a second SavedTables/workbench in this read request.
    after_close = target < cutoff.astimezone(SHANGHAI).date() or cutoff.astimezone(SHANGHAI).time() >= time(15)
    closing = {**daily_table, 'rows': list(daily.values())} if after_close and daily_table.get('state') == 'complete' else None
    for value in (old_table, current_pool_table, daily_table):
        if value.get('state') == 'complete':
            _remember(reader, value)
    latest, quotes = None, []
    if not closing:
        latest = next((meta for meta in reader.market(target) if meta.family == 'tushare_market_rt' and current_rows(reader, meta, target)), None)
        quotes = current_rows(reader, latest, target)
    quote_map = unique(closing['rows'] if closing else quotes)
    names = {}
    for records in (old, current_pool, quote_map):
        for code, row in records.items():
            if row.get('name'):
                names.setdefault(code, row['name'])
    premarket = {row['ts_code']: (row, columns) for row, columns, _ in reader.table('tushare_stk_premarket', target)}
    heights = {code: integer(row.get('limit_times')) for code, row in old.items() if row.get('limit') == 'U'}
    stocks, names, identities = price_universe(reader, target, previous, latest, [], quotes, quote_map,
        closing, limits, premarket, heights, names, [], as_of_time=as_of_time, prepare_breadth=False)
    snapshot = None if closing else live_snapshot_overlay(reader, target, session, latest, quotes, strict=as_of_time is not None)
    if snapshot:
        apply_snapshot_prices(reader, snapshot, stocks, names, identities)
    # Keep auction/listing cohorts visible even when the current quote is absent.
    from app.market.terminal_schemas import TerminalStock
    for code in set(auction_map) | set(old) | lhb_rows:
        stocks.setdefault(code, TerminalStock(code=code, name=names.get(code, code)))
    current_enriched = [dict(row) for row in current_pool.values()]
    if after_close:
        enrich_groups(current_enriched, current_pool_table, archives.group_units(current_pool_table), {}, None,
                      list(current_detail.values()), list(basic.values()))
        for row in current_enriched:
            if row['ts_code'] in stocks and stocks[row['ts_code']].price_basis == 'daily_close' and (
                    stocks[row['ts_code']].price == number(basic.get(row['ts_code'], {}).get('close'))):
                stocks[row['ts_code']].seal_amount_cny = number(row.get('seal_amount_yuan'))
    live_columns = {col.get('key'): col.get('unit') for col in reader.evidence(latest).payload.get('columns', [])} if latest else {}
    native_volumes = {row['ts_code']: number(row.get('vol')) for row in quotes} if live_columns.get('vol') == '股' else {}
    live_volumes = {stock.code: native_volumes.get(stock.code) for stock in stocks.values() if stock.source_family == 'tushare_market_rt'}
    rows = build_rows(list(stocks.values()), old, daily, prior, auction_map, auction_units, basic, old_basic, limits, old_limits, detail, tags, lhb_rows,
        auction_available=target < cutoff.astimezone(SHANGHAI).date() or cutoff.astimezone(SHANGHAI).time() >= time(9, 25),
        previous_lhb_available=lhb_table.get('state') == 'complete',
        current_daily_available=daily_table.get('state') == 'complete' and (target < cutoff.astimezone(SHANGHAI).date() or cutoff.astimezone(SHANGHAI).time() >= time(15)),
        live_volumes=live_volumes)
    sources = [AuctionSupportSource(**{key: value.get(key) for key in ('dataset', 'trade_day', 'state', 'received_at')},
                   evidence_ids=[str(ref['id']) for ref in value.get('evidence_refs', [])])
               for value in (old_table, current_pool_table, detail_table, current_detail_table, basic_table, old_basic_table, daily_table, prior_table, lhb_table)]
    auction_meta = {meta.id: meta for _, _, meta in auctions}
    sources.append(AuctionSupportSource(dataset='stk_auction', trade_day=target, state='available' if auctions else 'missing',
        received_at=max((meta.received_at for meta in auction_meta.values()), default=None).isoformat() if auction_meta else None,
        evidence_ids=[str(ident) for ident in auction_meta]))
    if latest:
        sources.append(AuctionSupportSource(dataset='market_rt', trade_day=target, state='available', received_at=latest.received_at.isoformat(), evidence_ids=[str(latest.id)]))
    down_days, up_trial_days = [], []
    for day in (target, previous):
        if day is None:
            continue
        day_auction = auctions if day == target else reader.table('tushare_stk_auction', day)
        day_map = auction_map if day == target else unique([row for row, _, _ in day_auction])
        day_units = {row['ts_code']: cols for row, cols, _ in day_auction if row.get('ts_code') in day_map}
        if day == cutoff.astimezone(SHANGHAI).date() and cutoff.astimezone(SHANGHAI).time() < time(9, 25):
            day_map, day_units = {}, {}
        day_limits = limits if day == target else old_limits
        # Reuse the already loaded close/current prices; no second full universe.
        if day == target:
            day_prices = {stock.code: {'name': stock.name, 'close': stock.price, 'pre_close': stock.pre_close,
                'trade_time': stock.source_time, '_basis': stock.price_basis} for stock in stocks.values()}
            day_prior = old
        else:
            day_prices = {code: {**row, 'name': names.get(code) or row.get('name'), '_basis': 'daily_close'} for code, row in prior.items()} if prior_table.get('state') == 'complete' else {}
            day_prior = prior_pool
        down = downside_day(reader, day, auctions=day_map, auction_units=day_units,
                            limits=day_limits, prices=day_prices, prior=day_prior)
        day_price_table = daily_table if day == target else prior_table
        price_refs = {str(ref['id']) for ref in day_price_table.get('evidence_refs', [])} if day_price_table.get('state') == 'complete' else set()
        if day == target and latest:
            price_refs.add(str(latest.id))
        if day == target and snapshot:
            price_refs.update(str(item['evidence_id']) for item in reader.input_versions.values())
        down.evidence_ids = sorted(set(down.evidence_ids) | {str(meta.id) for _, _, meta in day_auction}
                                  | {str(meta.id) for _, _, meta in reader.table('tushare_market_limits', day)} | price_refs)
        from app.market.auction_book_read import book_views, attach_book
        native=reader.auction.native
        native_down=native.day(day,'down')
        book=((native_down,native.day(day,'up'),native.data(day).live_context) if native_down is not None
              else book_views(reader,day,down_codes=[row.code for row in down.rows]))
        if book is not None:
            native_down,native_up,live_context=book
            up_trial_days.append(native_up)
            if live_context or (not day_map and native_down.rows):
                down=native_down
            else:
                down=attach_book(down,native_down)
            sources.append(AuctionSupportSource(dataset='auction_book_trial',trade_day=day,
                state=native_down.book_state or 'missing',received_at=native_down.received_at,
                evidence_ids=native_down.evidence_ids))
        down_days.append(down)
    return AuctionSupportView(trade_day=target, previous_trade_day=previous, as_of_time=cutoff,
        available_days=sorted(reader.available_days, reverse=True)[:63], rows=rows, down_days=down_days,up_trial_days=up_trial_days,
        state='available' if any(row.previous_state == 'U' for row in rows) else 'unavailable', sources=sources,
        missing_fields=['竞价净额', '人气排名'])
