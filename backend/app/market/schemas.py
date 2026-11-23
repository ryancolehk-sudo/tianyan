from typing import Literal
from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from app.evidence.schemas import BarPayload, NewsPayload, QuotePayload, ReferencePayload
from app.schemas import DTO, Provider, Rights
from app.market.news_feeds import NEWS_FEEDS
from app.market.tushare_specs import SPECS as TUSHARE_SPECS
from app.market.review_protocol import PUBLIC_FAMILIES
from app.market.connected_specs import FAMILIES as CONNECTED_FAMILIES
from app.market.direction_specs import PUBLIC_FAMILIES as DIRECTION_FAMILIES


class WorkbenchCapability(DTO):
    key: str
    status: Literal['available','partial','unavailable','not_configured','permission_missing','empty']
    reason: str
    source_time: AwareDatetime | None = None
    received_at: AwareDatetime | None = None


class WorkbenchIndex(DTO):
    code: str
    name: str
    price: float | None = None
    pct_change: float | None = None
    source_time: AwareDatetime | None = None
    received_at: AwareDatetime | None = None


class WorkbenchStats(DTO):
    advancing: int | None = None
    declining: int | None = None
    turnover_cny: float | None = None
    limit_up: int | None = None
    limit_down: int | None = None
    broken_count: int | None = None
    max_height: int | None = None


class WorkbenchBreadth(DTO):
    time: AwareDatetime
    up: int | None = None
    down: int | None = None


class WorkbenchStock(DTO):
    instrument_id: UUID | None = None
    ts_code: str
    name: str
    pct_change: float | None = None
    price: float | None = None
    amount_cny: float | None = None
    change_5m: float | None = None
    theme: str | None = None
    sector_code: str | None = None
    previous_height: int | None = None
    current_state: str = '待核查'
    turnover_rate: float | None = None
    seal_amount_cny: float | None = None
    first_observed_at: AwareDatetime | None = None
    last_change_at: AwareDatetime | None = None
    source_time: AwareDatetime | None = None
    received_at: AwareDatetime | None = None
    # Daily DC values have their own date; they are never recent-five-minute flow.
    net_cny: float | None = None
    fund_trade_day: date | None = None
    auction_price: float | None = None
    auction_amount_cny: float | None = None
    auction_pct_change: float | None = None
    node_0915_cny: float | None = None
    node_0920_cny: float | None = None
    node_0925_cny: float | None = None


class WorkbenchSector(DTO):
    code: str
    name: str
    net_cny: float | None = None
    pct_change: float | None = None
    change_5m: float | None = None
    rank: int | None = None
    rank_change: int | None = None
    trade_day: date | None = None


class WorkbenchAuctionDay(DTO):
    day: date
    total_count: int
    rows: list[WorkbenchStock]


class WorkbenchFundPoint(DTO):
    day: date
    sector_code: str
    net_cny: float | None = None
    pct_change: float | None = None
    turnover_cny: float | None = None
    limit_up: int | None = None


class WorkbenchView(DTO):
    view: Literal['overview','auction','ladder','funds']
    trade_day: date | None
    available_days: list[date]
    as_of_time: AwareDatetime
    source_label: str = 'Tushare · 本机有权缓存'
    mode: Literal['review','as_of']
    scope: str
    cohort_day: date | None = None
    indices: list[WorkbenchIndex] = Field(default_factory=list)
    stats: WorkbenchStats = Field(default_factory=WorkbenchStats)
    breadth: list[WorkbenchBreadth] = Field(default_factory=list)
    stocks: list[WorkbenchStock] = Field(default_factory=list)
    sectors: list[WorkbenchSector] = Field(default_factory=list)
    auction_days: list[WorkbenchAuctionDay] = Field(default_factory=list)
    fund_history: list[WorkbenchFundPoint] = Field(default_factory=list)
    broken_stocks: list[WorkbenchStock] = Field(default_factory=list)
    watchlist_ids: list[UUID] = Field(default_factory=list)
    capabilities: list[WorkbenchCapability] = Field(default_factory=list)
    selected_stock: WorkbenchStock | None = None
    selected_sector: WorkbenchSector | None = None


class FetchRun(DTO):
    id: UUID
    source_id: UUID
    capability: str
    job_id: UUID | None = None
    requested_day: date | None = None
    acquisition_mode: Literal['observed','backfill','daily_history'] | None = None
    requested_instrument_code: str | None = None
    requested_start_day: date | None = None
    requested_end_day: date | None = None
    started_at: AwareDatetime
    received_at: AwareDatetime
    status: Literal['complete','partial','empty','unavailable','error']
    reason: str | None
    expected: int | None
    accepted: int | None
    rejected: int | None
    duplicates: int | None
    elapsed_ms: int | None
    request_count: int
    cost_cny: str | None
    retry_not_before: AwareDatetime | None = None
    clock_regressed: bool = False

    @field_validator('cost_cny', mode='before')
    @classmethod
    def decimal_cost(cls, value):
        return format(value, 'f') if isinstance(value, Decimal) else value


class CollectionJobReceipt(DTO):
    job_id: UUID
    source_id: UUID
    state: Literal['queued', 'running']
    requested_day: date | None = None
    acquisition_mode: Literal['observed','backfill'] | None = None
    instrument_id: UUID | None = None
    requested_start_day: date | None = None
    requested_end_day: date | None = None


class DailyHistoryRequest(DTO):
    instrument_id: UUID
    start_day: date | None = None
    end_day: date | None = None
    adjustment: Literal['unadjusted','forward','backward'] = 'unadjusted'


class InstrumentSelection(DTO):
    exchange: Literal['SSE','SZSE','BSE']
    symbol: str = Field(pattern=r'^\d{6}$')
    asset_type: Literal['stock','index','etf','convertible']


class NewsSourceInfo(DTO):
    publisher: str
    region: str
    category: str


class SourceAdapter(DTO):
    family: str
    name: str
    kind: Literal['market', 'calendar', 'catalog', 'news']
    version: str
    capabilities: list[str]
    minimum_interval: int
    references: list[str]
    news: NewsSourceInfo | None = None


class RuntimeSource(DTO):
    provider: Provider
    family: str
    implementation: Literal['implemented'] = 'implemented'
    rights_basis: str
    last_attempt: FetchRun | None
    last_success_at: AwareDatetime | None
    collection_state: Literal['disabled','permission_missing','credentials_missing','not_checked','ready','partial','empty','unavailable','error','waiting_focus','waiting']
    credential_configured: bool | None = None
    collection_enabled: bool
    paused_reason: str | None
    symbols: list[InstrumentSelection]
    interval_seconds: int
    configured_at: AwareDatetime
    next_run_at: AwareDatetime
    cooldown_until: AwareDatetime | None = None
    cooldown_scope: Literal['supplier', 'source'] | None = None
    # A running source with no recorded bill is not a zero-cost measurement.
    measured_cost_cny: str | None = None
    news_coverage: dict | None = None
    board_codes: list[str] = Field(default_factory=list, max_length=3)
    sector_codes: list[str] = Field(default_factory=list, max_length=10)
    points_enabled: bool = False
    collection_mode: Literal['selected','full_market','focused'] = 'selected'
    backup_enabled: bool = False


class SourceIndex(DTO):
    items: list[RuntimeSource]
    next_cursor: None = None
    available_adapters: list[SourceAdapter]
    collection_enabled: bool
    today_request_count: int
    today_run_count: int
    today_cost_cny: str | None
    statistics_timezone: Literal['Asia/Shanghai'] = 'Asia/Shanghai'


class SourceConfiguration(DTO):
    family: Literal['xgb_breadth_history','sina_minute_1m','sina_minute_5m','sina_minute_15m','sina_minute_30m','sina_minute_60m','tencent_web','tencent_market_rt','szse_public','cninfo','baostock_daily','baostock_history','baostock_fundamental','baostock_classification','ecb_public','worldbank_public','ecb_news','fed_public','cninfo_announcements','tickflow_live','ashare_public','ashare_breadth','hk_gov_finance','ashare_sector_heat','mairui_funds','mairui_pools','eastmoney_funds','eastmoney_boards', 'eastmoney_auction','tencent_auction','duanxianxia_auction','duanxianxia_auction_history', *TUSHARE_SPECS, *NEWS_FEEDS, *sorted(PUBLIC_FAMILIES), *sorted(CONNECTED_FAMILIES), *sorted(DIRECTION_FAMILIES)]
    symbols: list[InstrumentSelection] = Field(default_factory=list, max_length=50)
    interval_seconds: int = Field(default=60, ge=3, le=86400)
    rights_basis: str = Field(min_length=10, max_length=2000)
    rights: Rights
    entitlement_status: Literal['verified','denied','unknown'] = 'unknown'
    enabled: bool = False
    board_codes: list[str] = Field(default_factory=list, max_length=3)
    sector_codes: list[str] = Field(default_factory=list, max_length=10)
    points_enabled: bool = False
    collection_mode: Literal['selected','full_market','focused'] = 'selected'
    backup_enabled: bool = False

    @field_validator('sector_codes')
    @classmethod
    def valid_sector_codes(cls, values):
        import re
        if len(set(values)) != len(values) or any(not re.fullmatch(r'BK\d{4,8}\.DC', value) for value in values):
            raise ValueError('expected unique Tushare DC board codes')
        return values

    @field_validator('board_codes')
    @classmethod
    def valid_board_codes(cls, values):
        import re
        if len(set(values)) != len(values) or any(not re.fullmatch(r'BK\d{4}',value) for value in values):
            raise ValueError('expected unique Eastmoney board codes')
        return values


class MarketIdentity(InstrumentSelection):
    id: UUID | None
    name: str


class QuoteProvenance(DTO):
    source_id: UUID
    family: str
    source_name: str
    source_version_id: UUID
    adapter_version: str
    evidence_version_id: UUID
    content_hash: str
    received_at: AwareDatetime
    published_at: AwareDatetime | None
    available_at: AwareDatetime | None
    historical_eligible: bool


class MarketQuote(DTO):
    instrument: MarketIdentity
    state: Literal['not_configured','disabled','permission_missing','empty','unavailable','partial','stale','available','daily_close','delayed_reference']
    reason: str | None = None
    quote: QuotePayload | None = None
    provenance: QuoteProvenance | None = None
    age_seconds: int | None = None
    freshness_limit_seconds: int
    change: str | None = None
    change_percent: str | None = None


class QuoteBatch(DTO):
    items: list[MarketQuote]
    selection: Literal['indices', 'instrument', 'batch']
    query_time: AwareDatetime
    historical: bool


class DatasetItem(DTO):
    id: UUID
    source_id: UUID
    source_name: str
    family: str
    source_key: str
    instrument_id: UUID | None
    received_at: AwareDatetime
    published_at: AwareDatetime | None
    content_hash: str
    payload: ReferencePayload
    # Projection metadata; payload/hash still identify the unmodified evidence.
    trade_day: date | None = None
    data_mode: Literal['intraday','after_market','reference'] = 'reference'
    view_rows: list[dict[str, str | None]] | None = Field(default=None,max_length=8000)


class DatasetList(DTO):
    items: list[DatasetItem]


class TradingDatasetList(DatasetList):
    available_days: list[date]
    trade_day: date | None


class WatchlistItem(DTO):
    instrument: MarketIdentity
    position: int = Field(ge=0, le=99)


class WatchlistView(DTO):
    items: list[WatchlistItem]


class WatchlistUpdate(DTO):
    instrument_ids: list[UUID] = Field(max_length=100)

    @field_validator('instrument_ids')
    @classmethod
    def unique_instruments(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('duplicate instrument')
        return value


class BreadthPoint(DTO):
    id: UUID
    received_at: AwareDatetime
    limit_up: int | None = Field(ge=0, le=999999)
    limit_down: int | None = Field(ge=0, le=999999)
    advancing: int | None = Field(default=None, ge=0)
    declining: int | None = Field(default=None, ge=0)
    unchanged: int | None = Field(default=None, ge=0)
    returned: int | None = Field(default=None, ge=0)
    comparable: int | None = Field(default=None, ge=0)
    unknown: int | None = Field(default=None, ge=0)
    old_day: int | None = Field(default=None, ge=0)
    fresh: int | None = Field(default=None, ge=0)
    source_time_min: AwareDatetime | None = None
    source_time_max: AwareDatetime | None = None
    limit_evidence_id: UUID | None = None
    total_amount_cny: str | None = None


class BreadthSeries(DTO):
    observation_day: date
    timezone: Literal['Asia/Shanghai'] = 'Asia/Shanghai'
    time_basis: Literal['received_at'] = 'received_at'
    source_name: str | None
    items: list[BreadthPoint]
    truncated: bool = False
    source_family: str | None = None
    scope: str | None = None
    latest: BreadthPoint | None = None


class NewsItem(DTO):
    id: UUID
    source_id: UUID
    source_name: str
    family: str
    received_at: AwareDatetime
    published_at: AwareDatetime | None
    content_hash: str
    payload: NewsPayload


class NewsList(DTO):
    items: list[NewsItem]


class BarItem(DTO):
    id: UUID
    received_at: AwareDatetime
    content_hash: str
    payload: BarPayload
    period_state: Literal['forming','elapsed','unknown'] = 'unknown'
    arrival_kind: Literal['backfill','observed','late_observed'] = 'observed'
    projected_from_quote: bool = False


class BarSeries(DTO):
    backfill_supported: bool = False
    backfill_lookback_days: int | None = None
    has_more: bool = False
    coverage_start_day: date | None = None
    coverage_end_day: date | None = None
    requested_start_day: date | None = None
    requested_end_day: date | None = None
    history_supported: bool = False
    requested_before: date | None = None
    history_source_id: UUID | None = None
    history_reason: str | None = None
    instrument: MarketIdentity
    source_name: str | None
    adjustment: Literal['unadjusted','forward','backward']
    items: list[BarItem]
    historical_reconstruction: bool = True
    interval: Literal['1m','5m','15m','30m','60m','1d'] = '1d'
    observation_day: date | None = None
    source_id: UUID | None = None
    source_family: str | None = None
    state: Literal['available','stale','empty','permission_missing','disabled','unavailable','historical','not_configured','partial'] = 'empty'
    reason: str | None = None
    last_success_at: AwareDatetime | None = None
    last_attempt: FetchRun | None = None
    latest_source_time: AwareDatetime | None = None
    age_seconds: int | None = None
    freshness_limit_seconds: int = 120
    timestamp_semantics: Literal['start','end','unknown'] = 'unknown'
    finality: Literal['unknown','confirmed'] = 'unknown'
    data_mode: Literal['saved_daily','source_minutes','live_daily'] = 'saved_daily'
    gap_count: int = 0
    current_daily: BarItem | None = None
    current_daily_source_name: str | None = None
    current_daily_source_family: str | None = None
    current_daily_state: str | None = None
    current_daily_age_seconds: int | None = None
    current_daily_reason: str | None = None


class AuctionSummary(DTO):
    trade_day: date | None
    checkpoint: Literal['09:15', '09:20', '09:25'] | None
    instrument_count: int
    quoted_count: int
    rejected_count: int
    total_volume_shares: str | None
    total_amount_cny: str | None
    up_count: int
    down_count: int
    flat_count: int


class AuctionDetailRow(DTO):
    code: str
    name: str
    price_cny: str | None
    prev_close_cny: str | None
    change_pct: str | None
    volume_shares: str | None
    amount_cny: str | None
    quote_time: AwareDatetime | None


class AuctionBatch(DTO):
    batch_hash: str
    source_id: UUID
    source_name: str
    family: str
    trade_day: date | None
    trade_day_basis: Literal['source_time', 'received_at', 'unknown']
    checkpoint: Literal['09:15', '09:20', '09:25'] | None
    checkpoint_basis: Literal['source_time', 'unknown']
    received_at: AwareDatetime
    quote_time: AwareDatetime | None
    age_seconds: int
    state: Literal['available', 'stale', 'unpaired']
    note: str | None
    summary: AuctionSummary | None
    detail: list[AuctionDetailRow]


class AuctionSourceState(DTO):
    family: str
    source_name: str
    state: Literal['available', 'partial', 'empty', 'stale', 'disabled', 'permission_missing', 'error', 'unavailable']
    last_received_at: AwareDatetime | None = None
    last_success_at: AwareDatetime | None = None
    reason: str | None = None
    batch_count: int = 0


class AuctionView(DTO):
    trade_day: date | None
    timezone: Literal['Asia/Shanghai'] = 'Asia/Shanghai'
    items: list[AuctionBatch]
    sources: list[AuctionSourceState]


class DataCapability(DTO):
    id: str
    name: str
    state: Literal['partial','pending','blocked']
    detail: str
    families: list[str]
    dataset_ids: list[UUID]


class DataCoverage(DTO):
    items: list[DataCapability]
