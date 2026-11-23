export type Value = number | null;
export type Stock = {instrument_id: string | null; ts_code: string; name: string; pct_change: Value; price: Value; amount_cny: Value; change_5m: Value; theme: string | null; sector_code: string | null; previous_height: Value; current_state: string | null; turnover_rate: Value; seal_amount_cny: Value; first_observed_at: string | null; last_change_at: string | null; source_time: string | null; received_at: string | null; net_cny?: Value; fund_trade_day?: string | null; auction_price?: Value; auction_amount_cny?: Value; auction_pct_change?: Value; node_0915_cny?: Value; node_0920_cny?: Value; node_0925_cny?: Value};
export type Sector = {code: string; name: string; net_cny: Value; pct_change: Value; change_5m: Value; rank: Value; rank_change: Value; trade_day: string | null};
export type WorkbenchModel = {
  view: string; cohort_day?: string | null; trade_day: string | null; available_days: string[]; as_of_time: string; source_label: string; mode: 'review' | 'as_of'; scope: string;
  indices: {code: string; name: string; price: Value; pct_change: Value; source_time: string | null; received_at: string | null}[];
  stats: {advancing: Value; declining: Value; turnover_cny: Value; limit_up: Value; limit_down: Value; broken_count: Value; max_height: Value};
  breadth: {time: string; up: Value; down: Value}[]; stocks: Stock[]; sectors: Sector[];
  auction_days: {day: string; total_count?: number; rows: Stock[]}[];
  fund_history: {day: string; sector_code: string; net_cny: Value; pct_change: Value; turnover_cny: Value; limit_up: Value}[];
  broken_stocks: Stock[]; watchlist_ids: string[];
  capabilities: {key: string; status: string; reason: string; source_time: string | null; received_at: string | null}[];
  selected_stock: Stock | null; selected_sector: Sector | null;
};
export type WorkbenchResponse = {data: WorkbenchModel; meta: {app_mode: string}};
