import type { Coverage, Row, Context, Metrics, Ref } from './model';
export type PoolComparison = {
  previous_trade_day:string|null; trade_day:string|null; comparable:boolean; candidate_n:number; promoted_n:number; failed_n:number; unknown_n:number; promotion_pct:number|null;
  records:Array<{code:string;name:string;previous_height:string|null;current_height:string|null;state:'promoted'|'failed'|'unknown';reason:string;previous_evidence:Ref|null;current_evidence:Ref|null}>;
  events:Array<{code:string;name:string;state:string;open_times:string|null;first_seal:string|null;last_seal:string|null;evidence:Ref|null;sequence_note:string}>;
};
export type Analysis = Context & {
  kind: 'emotion' | 'groups' | 'ladder' | 'stock' | 'comparison'; trade_days: string[]; comparison?:PoolComparison; scope_note?:string;
  groups?: { state: string; records: Row[]; boards: Group[]; themes: Group[]; membership_state: string; membership_day: string | null; unassigned_codes: string[]; ambiguous_n: number; scope: string; coverage: Coverage[] };
  records: Array<{ trade_day: string; state: string; basis?: string; coverage_note?: string; metrics?: Metrics; stocks?: Row[]; quote?: Row | null; listing_state?: string; listings?: Array<{ record_key: string; reason: string }> }>;
};
export type Group = { key: string; label: string; codes: string[] };
export type Rotation = Omit<Context, 'coverage'> & {
  sector_code: string | null; sector_type: string; lookback: number;
  coverage: { requested_days: number; calendar_days: number; ranking_days: number; funds_days: number };
  records: Array<{ trade_day: string; status: string; funds_status: string; selection_state: string; rank_complete?: boolean; funds_complete?: boolean; rank: number | null; in_top_ten: boolean | null; pct_change: string | null; net_amount: string | null; total_count: number; top_ten: Array<{ code: string; name: string }>; received_at: string | null; funds_received_at: string | null }>;
};
