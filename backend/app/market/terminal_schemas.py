"""Typed fixed-workbench contract. Numeric gaps stay null, including auction orders."""
from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field

from app.schemas import DTO
from app.market.session import MarketSession
from app.market.intraday_funds import IntradayFunds


class TerminalPoolRecord(DTO):
    """A supplier daily pool record, never a replacement for a live order book."""
    trade_day: date
    state: Literal['U', 'D', 'Z']
    source_name: str
    received_at: AwareDatetime | None = None
    scope: str
    seal_amount_cny: float | None = None
    first_limit_time: str | None = None
    last_limit_time: str | None = None
    board_height: int | None = None
    open_times: int | None = None


class TerminalStock(DTO):
    code: str
    name: str
    instrument_id: str | None = None
    theme: str | None = None
    ths_themes: list[str] = Field(default_factory=list)
    ths_themes_received_at: AwareDatetime | None = None
    pct_change: float | None = None
    speed_5m: float | None = None
    amount_cny: float | None = None
    turnover_rate: float | None = None
    float_mv_cny: float | None = None
    price: float | None = None
    pre_close: float | None = None
    open: float | None = None
    high: float | None = None
    limit_up: float | None = None
    limit_down: float | None = None
    previous_height: int | None = None
    status: str = '行情缺失'
    first_observed: str | None = None
    latest_change: str | None = None
    seal_amount_cny: float | None = None
    auction_pct: float | None = None
    auction_price: float | None = None
    auction_amount_cny: float | None = None
    volume_ratio: float | None = None
    node_amounts: dict[str, float | None] = Field(default_factory=lambda:{'09:15':None,'09:20':None,'09:25':None})
    node_received_at: AwareDatetime | None = None
    node_source: str | None = None
    auction_indicative_pct: float | None = None
    auction_current_seal_cny: float | None = None
    auction_tags: list[str] = Field(default_factory=list)
    auction_board: str | None = None
    main_net_cny: float | None = None
    days_since_break: int | None = None
    open_pct: float | None = None
    source_time: str | None = None
    price_basis: Literal['intraday', 'daily_close', 'snapshot'] | None = None
    source_snapshot_time: AwareDatetime | None = None
    source_received_at: AwareDatetime | None = None
    speed_window_seconds: float | None = None
    funds_trade_day: date | None = None
    break_day: date | None = None
    source_family: str | None = None
    source_name: str | None = None
    daily_pool: TerminalPoolRecord | None = None


class TerminalSectorMember(DTO):
    """Saved constituent with selected-day quotes; membership day is separate."""
    code: str
    name: str
    instrument_id: str | None = None
    pct_change: float | None = None
    speed_5m: float | None = None
    source_time: str | None = None
    price_basis: Literal['intraday', 'daily_close', 'snapshot'] | None = None
    source_snapshot_time: AwareDatetime | None = None
    source_received_at: AwareDatetime | None = None
    speed_window_seconds: float | None = None


class TerminalIndex(DTO):
    code: str
    name: str
    price: float | None = None
    pct_change: float | None = None
    amount_cny: float | None = None
    source_time: str | None = None
    price_basis: Literal['intraday', 'daily_close', 'snapshot'] | None = None
    source_snapshot_time: AwareDatetime | None = None
    source_received_at: AwareDatetime | None = None
    source_family: str | None = None
    source_name: str | None = None


class TerminalSector(DTO):
    code: str
    name: str
    category: Literal['concept', 'industry'] | None = None
    industry_level: int | None = None
    main_net_cny: float | None = None
    pct_change: float | None = None
    amount_cny: float | None = None
    rank: int | None = None
    member_count: int | None = None


class TerminalBreadthPoint(DTO):
    time: str
    up: int | None = Field(default=None, description='当前涨停价家数；不是上涨家数')
    down: int | None = Field(default=None, description='当前跌停价家数；不是下跌家数')


class TerminalBreadthHistory(DTO):
    source_name: str
    source_family: str
    scope: str
    trade_day: date
    received_at: AwareDatetime
    points: list[TerminalBreadthPoint] = Field(default_factory=list)


class TerminalBreadth(DTO):
    source_family: str | None = None
    source_name: str | None = None
    up: int | None = Field(default=None, description='上涨家数')
    down: int | None = Field(default=None, description='下跌家数')
    flat: int | None = None
    limit_up: int | None = None
    limit_down: int | None = None
    comparable: int | None = None
    scope: str
    points: list[TerminalBreadthPoint] = Field(default_factory=list)
    amount_cny: float | None = None
    basis: Literal['intraday', 'daily_close'] = 'intraday'
    trade_day: date | None = None
    source_received_at: AwareDatetime | None = None
    historical_series: TerminalBreadthHistory | None = None


class TerminalAuctionDay(DTO):
    day: date
    items: list[TerminalStock]
    node_received_at: AwareDatetime | None = None
    node_source: str | None = None
    node_status: Literal['available', 'unavailable'] = 'unavailable'
    one_word_count: int | None = None
    header_total_cny: float | None = None


class TerminalFundPoint(DTO):
    day: date
    sector_code: str
    value: float | None = None


class TerminalEvent(DTO):
    time: str
    label: str
    code: str
    kind: str


class TerminalSeal(DTO):
    time: str
    value: float | None = None


class TerminalCapability(DTO):
    key: str
    label: str
    status: Literal['available','partial','after_market','collecting','missing','permission_denied','stale','pending']
    reason: str
    source_family: str | None = None
    last_received_at: AwareDatetime | None = None


class TerminalCoverage(DTO):
    quote_count: int = 0
    displayed_stock_count: int = 0
    member_count: int = 0
    displayed_member_count: int = 0
    quote_scope: str
    members_scope: str


class TerminalRelaySummary(DTO):
    history_high: int | None = None
    history_covered_days: int = 0
    history_expected_days: int = 10
    history_start: date | None = None
    history_end: date | None = None
    history_scope: str
    high_sealed_count: int | None = None
    high_seal_total: int = 0
    first_board_codes: list[str] = Field(default_factory=list)
    daily_ladder_available: bool = False
    daily_ladder_source: str | None = None


class IntradayLadderStock(DTO):
    code: str
    name: str
    previous_height: int | None = None
    target_height: int | None = None
    status: Literal['sealed', 'touched', 'opened', 'waiting', 'unknown']
    freshness: Literal['current', 'stale', 'unknown']
    source_time: AwareDatetime | None = None
    source_name: str | None = None
    pct_change: float | None = None


class IntradayLadder(DTO):
    trade_day: date
    previous_trade_day: date | None = None
    as_of_time: AwareDatetime
    phase: str
    source_time: AwareDatetime | None = None
    sample_count: int
    sealed_count: int
    stale_count: int
    rows: list[IntradayLadderStock] = Field(default_factory=list)
    scope: str


class SectorStrength(DTO):
    code: str
    name: str
    category: str | None = None
    industry_level: int | None = None
    trade_day: date
    members_day: date | None = None
    limit_up_count: int | None = None
    member_count: int | None = None
    quoted_count: int = 0
    partial: bool = True
    source_time: AwareDatetime | None = None
    latest_source_time: AwareDatetime | None = None
    received_at: AwareDatetime | None = None
    basis: Literal['intraday', 'daily_close'] = 'intraday'


class TerminalView(DTO):
    view: Literal['overview','auction','ladder','funds']
    trade_day: date
    available_days: list[date]
    mode: Literal['live','review','observed']
    market_session: MarketSession | None = None
    intraday_funds: IntradayFunds | None = None
    intraday_ladder: IntradayLadder | None = None
    sector_strength: list[SectorStrength] = Field(default_factory=list)
    latest_at: AwareDatetime | None = None
    indices: list[TerminalIndex] = Field(default_factory=list)
    breadth: TerminalBreadth
    stocks: list[TerminalStock] = Field(default_factory=list)
    sectors: list[TerminalSector] = Field(default_factory=list)
    selected_stock_code: str | None = None
    selected_sector_code: str | None = None
    auction_days: list[TerminalAuctionDay] = Field(default_factory=list)
    selected_members: list[TerminalStock] = Field(default_factory=list)
    funds_history: list[TerminalFundPoint] = Field(default_factory=list)
    broken: list[TerminalStock] = Field(default_factory=list)
    events: list[TerminalEvent] = Field(default_factory=list)
    stock_seals: list[TerminalSeal] = Field(default_factory=list)
    capabilities: list[TerminalCapability] = Field(default_factory=list)
    funds_trade_day: date | None = None
    members_trade_day: date | None = None
    previous_trade_day: date | None = None
    coverage: TerminalCoverage
    detail_codes: list[str] = Field(default_factory=list)
    detail_revision: str = ''
    detail_observation_cutoff: AwareDatetime | None = Field(default=None,
        description='详情读取沿用的本机资料接收截止；不是供应商时点，也不改变严格历史as_of_time语义。')
    relay_summary: TerminalRelaySummary | None = None


class SectorMembershipEntry(DTO):
    code: str
    name: str


class SectorMembership(DTO):
    trade_day: date
    sector_code: str
    members_trade_day: date | None = None
    received_at: AwareDatetime | None = None
    state: Literal['available', 'missing', 'permission_denied']
    members: list[SectorMembershipEntry] = Field(default_factory=list, max_length=8000)
    member_count: int = Field(default=0, ge=0)
    truncated: bool = False


class TerminalDetails(DTO):
    trade_day: date
    revision: str
    stocks: list[TerminalStock] = Field(default_factory=list)
    auction_days: list[TerminalAuctionDay] = Field(default_factory=list)
    events: list[TerminalEvent] = Field(default_factory=list)
    seals: dict[str, list[TerminalSeal]] = Field(default_factory=dict)
    sector_code: str | None = None
    members: list[TerminalStock] = Field(default_factory=list)
    tracking_members: list[TerminalSectorMember] = Field(default_factory=list, max_length=8000,
        description='已存成分按所选行情日已验证5分钟涨速降序；成员日独立显示，缺值置末；最多8000只。')
    tracking_member_count: int = Field(default=0, ge=0, description='截断前同日实际成分去重数量；不是供应商目录估算。')
    tracking_members_truncated: bool = False
    members_trade_day: date | None = None
    funds_history: list[TerminalFundPoint] = Field(default_factory=list)
