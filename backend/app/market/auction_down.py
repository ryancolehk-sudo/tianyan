"""Read-only downside auction observations from existing, dated evidence.

Matched turnover, indicative prices and sell-side orders are distinct values.
No read requests the supplier or invents a missing historical order book.
"""
from datetime import date, datetime, time, timedelta
import re

from pydantic import Field

from app.evidence.models import EvidenceVersion
from app.market import read_facets
from app.market.protocol import SHANGHAI
from app.market.terminal_read import Meta, cny, current_rows, number, pct, units
from app.models import Provider
from app.schemas import DTO

STAGES = ('09:15', '09:20', '09:25')


class AuctionDownRow(DTO):
    code: str
    name: str
    tags: list[str] = Field(default_factory=list)
    previous_height: int | None = None
    node_amounts: dict[str, float | None] = Field(default_factory=dict)
    node_pct: dict[str, float | None] = Field(default_factory=dict)
    node_times: dict[str, str | None] = Field(default_factory=dict)
    auction_pct: float | None = None
    auction_amount_cny: float | None = None
    pct_change: float | None = None
    price_basis: str | None = None
    source_time: str | None = None
    pool_seal_cny: float | None = None
    pool_received_at: str | None = None
    current_seal_cny: float | None = None
    book_time: str | None = None


class AuctionDownDay(DTO):
    day: date
    rows: list[AuctionDownRow] = Field(default_factory=list)
    seal_coverage: int = 0
    stage_coverage: dict[str, int] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    source_name: str = '已存竞价成交与竞价卖一盘口'
    seal_basis: str = '卖一价等于当日跌停价、买一挂单为0时，卖一价×已核单位的卖一挂单量。'
    latest_at: str | None = None
    received_at: str | None = None
    live: bool = False
    book_state: str | None = None
    book_coverage: int | None = None
    book_expected: int | None = None


def code_map(rows):
    result, duplicates = {}, set()
    for row in rows:
        code = row.get('ts_code')
        if not isinstance(code, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', code):
            continue
        if code in result:
            duplicates.add(code)
        result[code] = row
    return {code: row for code, row in result.items() if code not in duplicates}


def at_lower(price, lower):
    price, lower = number(price), number(lower)
    return price is not None and lower is not None and lower > 0 and price == lower


def sell_seal(row, columns, lower):
    """A price-only quote or matched amount cannot establish a sell seal."""
    factor = {'股': 1, '手': 100}.get(columns.get('ask_volume1'))
    bid_factor = {'股': 1, '手': 100}.get(columns.get('bid_volume1'))
    ask, bid = number(row.get('ask_volume1')), number(row.get('bid_volume1'))
    if (factor is None or bid_factor is None or bid != 0 or ask is None or ask < 0
            or not at_lower(row.get('close'), lower) or not at_lower(row.get('ask_price1'), lower)):
        return None
    return number(row['ask_price1'], ask * factor)


def stage_rows(reader, day, stage):
    start = datetime.combine(day, time.fromisoformat(stage), SHANGHAI)
    if start > reader.cutoff:
        return None, [], {}
    # Select metadata first, not the latest 500 batches or all intraday payloads.
    # One bounded batch per stage; the native timestamp is checked per stock.
    query = read_facets.with_facets(reader.query({'tushare_market_rt'}, 'reference')).where(
        read_facets.observed_day() == day.isoformat(),
        EvidenceVersion.received_at >= start,
        EvidenceVersion.received_at <= min(reader.cutoff, start + timedelta(seconds=90)))
    query = query.with_only_columns(EvidenceVersion.id, EvidenceVersion.source_id, Provider.family,
        EvidenceVersion.source_key, EvidenceVersion.received_at, EvidenceVersion.content_hash)
    record = reader.db.execute(query.order_by(EvidenceVersion.received_at, EvidenceVersion.id).limit(1)).first()
    if record is None:
        return None, [], {}
    meta = Meta(*record, frozenset({day}))
    # The final auction node ends at 09:25. Receipt latency must never turn a
    # later quote into a 09:25 auction observation.
    source_start = start - timedelta(seconds=90) if stage == '09:25' else start
    source_end = start if stage == '09:25' else start + timedelta(seconds=90)
    rows = [row for row in current_rows(reader, meta, day)
            if (stamp := reader.at(row, meta.received_at)) is not None
            and source_start <= stamp <= min(reader.cutoff, source_end)]
    return meta, rows, units(reader.evidence(meta).payload)


def project_down(day, stages, auctions, auction_units, limits, prices, prior):
    """Pure, exact-day projection; all inputs have passed receipt/rights gates."""
    candidates = set()
    for rows, columns, _times in stages.values():
        candidates.update(code for code, row in rows.items()
                          if (seal := sell_seal(row, columns, limits.get(code, {}).get('down_limit')))
                          is not None and seal > 0)
    candidates.update(code for code, row in auctions.items()
                      if at_lower(row.get('price'), limits.get(code, {}).get('down_limit')))
    result = []
    for code in sorted(candidates):
        lower = limits.get(code, {}).get('down_limit')
        auction, quote = auctions.get(code, {}), prices.get(code, {})
        node_amounts, node_times = {}, {}
        for stage in STAGES:
            rows, columns, times = stages.get(stage, ({}, {}, {}))
            row = rows.get(code, {})
            node_amounts[stage] = sell_seal(row, columns, lower)
            node_times[stage] = times.get(code)
        source = quote or auction or next((values[0][code] for values in stages.values() if code in values[0]), {})
        height = number(prior.get(code, {}).get('limit_times'))
        result.append(AuctionDownRow(code=code, name=source.get('name') or code,
            previous_height=int(height) if height is not None and height > 0 and height.is_integer() else None,
            node_amounts=node_amounts, node_times=node_times,
            auction_pct=pct(auction.get('price'), auction.get('pre_close')),
            auction_amount_cny=cny(auction, 'amount', auction_units.get(code, {})),
            pct_change=pct(quote.get('close'), quote.get('pre_close')),
            price_basis=quote.get('_basis'), source_time=quote.get('trade_time')))
    return AuctionDownDay(day=day, rows=result,
        seal_coverage=sum(any(value is not None for value in row.node_amounts.values()) for row in result),
        stage_coverage={stage: sum(row.node_amounts.get(stage) is not None for row in result) for stage in STAGES})


def downside_day(reader, day, *, auctions, auction_units, limits, prices, prior):
    stages, evidence = {}, set()
    for stage in STAGES:
        meta, rows, columns = stage_rows(reader, day, stage)
        mapped = code_map(rows)
        stages[stage] = (mapped, columns,
                         {code: reader.at(row, meta.received_at).isoformat() for code, row in mapped.items()} if meta else {})
        if meta:
            evidence.add(str(meta.id))
    result = project_down(day, stages, auctions, auction_units, limits, prices, prior)
    result.evidence_ids = sorted(evidence)
    return result
