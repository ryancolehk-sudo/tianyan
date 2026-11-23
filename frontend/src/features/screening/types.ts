// Mirrors backend/app/screening/schemas.py. Financial values stay decimal strings.
export type Metric = 'price' | 'pct_change' | 'speed_5m' | 'amount_cny' | 'turnover_rate'
  | 'float_mv_cny' | 'open_pct' | 'previous_height' | 'volume_ratio';
export type Operator = 'gt' | 'gte' | 'lt' | 'lte' | 'eq';
export type ResultState = 'matched' | 'not_matched' | 'insufficient';
export type Condition = { field: Metric; operator: Operator; value: string };
export type Definition = { conditions: Condition[]; sort_field: Metric; sort_direction: 'asc' | 'desc' };
export type RuleSave = {
  id?: string; expected_version?: number; name: string; scope: 'market' | 'watchlist';
  definition: Definition; enabled: boolean; cooldown_seconds: number;
};
export type Rule = Omit<RuleSave, 'expected_version' | 'id'> & {
  id: string; version: number; last_run_at: string | null; last_error: string | null;
  created_at: string; updated_at: string;
};
export type ContextInput = { day?: string; as_of_time?: string };
export type PreviewInput = ContextInput & { definition: Definition; scope: Rule['scope']; state?: ResultState; offset?: number; limit?: number };
export type Check = { field: Metric; operator: Operator; target: string; actual: string | null; state: ResultState; reason: string };
export type ResultItem = {
  code: string; name: string; instrument_id: string | null; source_time: string | null; received_at: string | null;
  metrics: Record<string, string | null>; state: ResultState; checks: Check[];
};
export type Counts = { total: number; matched: number; not_matched: number; insufficient: number };
export type RunSummary = {
  id: string; rule_id: string; rule_version: number; rule_name: string; trade_day: string;
  as_of_time: string; mode: 'review' | 'observed' | 'monitor'; counts: Counts; created_at: string;
  event_count: number; input_hash: string; engine_version: string;
};
export type SourceRef = { evidence_id: string; source_id: string; content_hash: string; received_at: string };
export type RunResult = Omit<RunSummary, 'id' | 'rule_id' | 'rule_version' | 'rule_name'> & {
  id: string | null; rule_id: string | null; rule_version: number | null; rule_name: string | null;
  definition: Definition; scope: Rule['scope']; items: ResultItem[]; offset: number; limit: number;
  has_more: boolean; data_revision: string; source_refs: SourceRef[]; reasons: string[];
};
export type ObservationEvent = {
  id: string; rule_id: string; rule_name: string; rule_version: number; run_id: string;
  code: string; name: string; direction: 'entered' | 'exited'; source_time: string | null; created_at: string;
};
export type MetricInfo = { field: Metric; label: string; unit: string; description: string };
export type Catalogue = { engine_version: string; metrics: MetricInfo[]; interval_seconds: number; scope: string; boundaries: string[] };
export type Envelope<T> = { data: T; meta?: { app_mode?: string; quality?: { status?: string; reasons?: string[] } } };
