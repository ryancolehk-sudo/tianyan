export type DirectionSectorType = 'concept' | 'industry';
export type DirectionRankStatus = 'ready' | 'partial' | 'missing' | 'stale' | 'disabled' | 'error';

export interface DirectionRankRow {
  rank: number;
  code: string;
  name: string;
  pct_change?: string | null;
  quote_state?: string;
  quote_source?: string | null;
  quote_time?: string | null;
  quote_received_at?: string | null;
  net_amount?: string | null;
  heat?: string | null;
  rank_change?: number | null;
  strength?: string | null;
}

export interface DirectionRankPanel {
  trade_day: string | null;
  requested_day?: string | null;
  status: DirectionRankStatus;
  source_label: string;
  source_time: string | null;
  received_at: string | null;
  reason: string | null;
  rows: DirectionRankRow[];
  total_count?: number;
  truncated?: boolean;
  rank_change_basis?: 'source_reported' | 'not_provided' | 'previous_trading_day';
  rank_comparison_day?: string | null;
}

export interface DirectionBoardData {
  trade_day: string;
  snapshot_day?: string;
  funds_day?: string | null;
  automatic_current?: boolean;
  sector_type: DirectionSectorType;
  previous_trade_day: string | null;
  days: DirectionRankPanel[];
  funds: DirectionRankPanel;
  popularity: {
    ths: DirectionRankPanel;
    tdx: DirectionRankPanel;
    eastmoney: DirectionRankPanel;
  };
  strength: DirectionRankPanel;
}

export interface DirectionBoardEnvelope {
  data: DirectionBoardData;
  meta?: {
    app_mode?: string;
    generated_at?: string;
  };
}

export interface DirectionBoardProps {
  day: string;
  asOf: string;
  refreshEpoch: number;
  isAdmin: boolean;
  ownerId?: string;
  onResearch?: () => void;
  inactive?: boolean;
  onWindowChange?: (endDay: string, lookback?: number) => void;
}
