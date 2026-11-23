import type { MarketSession } from '../../shared/marketSession';
export type { IntradayLadder } from '../../generated/schema';
import type { IntradayLadder } from '../../generated/schema';
export type TerminalView = 'overview' | 'auction' | 'ladder' | 'funds';
export type Value = number | null;
export type PoolRecord = {
  trade_day: string; state: 'U' | 'D' | 'Z'; source_name: string; received_at: string | null; scope: string;
  seal_amount_cny: Value; first_limit_time: string | null; last_limit_time: string | null;
  board_height: Value; open_times: Value;
};
export type Stock = {
  code: string; name: string; instrument_id: string | null; theme: string | null;
  ths_themes?: string[]; ths_themes_received_at?: string | null;
  pct_change: Value; speed_5m: Value; amount_cny: Value; turnover_rate: Value;
  speed_window_seconds?: Value;
  float_mv_cny: Value; price: Value; pre_close: Value; open: Value; high: Value;
  limit_up: Value; limit_down: Value; previous_height: Value; status: string;
  first_observed: string | null; latest_change: string | null; seal_amount_cny: Value;
  auction_pct: Value; auction_price: Value; auction_amount_cny: Value; volume_ratio: Value;
  node_amounts: Record<'09:15' | '09:20' | '09:25', Value>;
  node_received_at?: string | null; node_source?: string | null;
  auction_indicative_pct?: Value; auction_tags?: string[]; auction_board?: string | null;
  auction_current_seal_cny?: Value;
  main_net_cny?: Value; days_since_break?: Value; open_pct?: Value;
  source_time?: string | null;
  price_basis?: 'intraday' | 'daily_close' | 'snapshot' | null; source_received_at?: string | null;
  source_snapshot_time?: string | null;
  source_name?: string | null; source_family?: string | null;
  daily_pool?: PoolRecord | null;
};
export type Sector = {
  code: string; name: string; main_net_cny: Value; pct_change: Value;
  amount_cny: Value; rank: Value; member_count: Value;
  category?: string | null; industry_level?: number | null;
};
export type IntradayFundSector = Sector & { change_5m_cny?: Value; rank_change_5m?: Value; source_time?: string | null; received_at?: string | null; category?: string | null };
export type SectorStrength = {
  code:string; name:string; category:string|null; industry_level:number|null; trade_day:string;
  members_day:string|null; limit_up_count:number|null; member_count:number|null; quoted_count:number;
  partial:boolean; source_time:string|null; latest_source_time:string|null; received_at:string|null;
  basis:'intraday'|'daily_close';
};
export type IntradayFunds = {
  time_basis?: 'source_time' | 'received_at'; metric?: string;
  category_notes?: Partial<Record<'industry' | 'concept', string>>;
  state: 'available' | 'stale' | 'missing' | 'permission_denied';
  trade_day: string | null; source_family: string | null; source_name: string | null;
  latest_source_time: string | null; received_at: string | null; scope: string;
  coverage?: { category: 'stock' | 'industry' | 'concept'; represented_count: number; expected_count: number | null;
    latest_batch_count: number; complete: boolean | null; source_time: string | null; received_at: string | null;
    latest_batch_source_time: string | null; latest_batch_received_at: string | null }[];
  sectors: IntradayFundSector[];
  focused_sectors?: IntradayFundSector[];
  target_states?: { code: string; category: 'stock' | 'concept' | 'industry'; state: 'available' | 'stale' | 'preparing' | 'recovering' | 'unavailable'; source_time: string | null; received_at: string | null; next_attempt_at: string | null }[];
  points: { time: string; sector_code: string; value: Value }[];
  stocks: { code: string; value: Value; change_5m_cny?: Value; source_time?: string | null }[];
};
export type SectorMember = Pick<Stock, 'code' | 'name' | 'instrument_id' | 'pct_change' | 'speed_5m' | 'source_time' | 'price_basis' | 'source_received_at' | 'source_snapshot_time'> & {
  speed_window_seconds?: Value;
};
export type Capability = {
  key: string; label: string; status: string; reason: string; source_family: string | null;
  last_received_at: string | null;
};
export type WorkbenchData = {
  sector_strength?: SectorStrength[];
  market_session?: MarketSession | null;
  intraday_funds?: IntradayFunds | null;
  intraday_ladder?: IntradayLadder | null;
  detail_codes?: string[]; detail_revision?: string; detail_observation_cutoff?: string | null;
  view: TerminalView; trade_day: string; available_days: string[];
  mode: 'live' | 'review' | 'observed'; latest_at: string | null;
  indices: { code: string; name: string; price: Value; pct_change: Value; amount_cny: Value; source_time: string | null; source_name?: string | null; source_family?: string | null; price_basis?: 'intraday' | 'daily_close' | 'snapshot' | null; source_received_at?: string | null; source_snapshot_time?: string | null }[];
  breadth: { up: Value; down: Value; flat: Value; limit_up: Value; limit_down: Value;
    source_family?: string | null; source_name?: string | null;
    comparable: Value; scope: string; points: { time: string; up: Value; down: Value }[]; amount_cny?: Value;
    basis?: 'intraday' | 'daily_close'; trade_day?: string | null; source_received_at?: string | null;
    historical_series?: { source_name: string; source_family: string; scope: string; trade_day: string;
      received_at: string; points: { time: string; up: Value; down: Value }[] } | null };
  stocks: Stock[]; sectors: Sector[]; selected_stock_code: string | null; selected_sector_code: string | null;
  auction_days: { day: string; items: Stock[]; node_received_at?: string | null; node_source?: string | null;
    node_status?: 'available' | 'unavailable'; one_word_count?: Value; header_total_cny?: Value }[]; selected_members: Stock[];
  funds_history: { day: string; sector_code: string; value: Value }[];
  funds_trade_day?: string | null; members_trade_day?: string | null; previous_trade_day?: string | null;
  coverage?: Record<string, unknown>;
  broken: Stock[]; events: { time: string; label: string; code: string; kind: string }[];
  stock_seals: { time: string; value: Value }[]; capabilities: Capability[];
  relay_summary?: {
    history_high: Value; history_covered_days: number; history_expected_days: number;
    history_start: string | null; history_end: string | null; history_scope: string;
    high_sealed_count: Value; high_seal_total: number;
    first_board_codes: string[]; daily_ladder_available: boolean; daily_ladder_source: string | null;
  } | null;
};

export type TerminalDetails = {
  trade_day: string; revision: string; stocks: Stock[];
  auction_days: WorkbenchData['auction_days']; events: WorkbenchData['events'];
  seals: Record<string, WorkbenchData['stock_seals']>;
  sector_code: string | null; members: Stock[]; members_trade_day: string | null;
  tracking_members?: SectorMember[];
  tracking_member_count?: number; tracking_members_truncated?: boolean;
  funds_history?: WorkbenchData['funds_history'];
};
