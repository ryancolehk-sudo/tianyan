export type Ref = { id: string; content_hash?: string; source_id?: string; received_at?: string; source_row?: number; row_index_base?: number };
export type Row = Record<string, unknown> & { row_reference?: Ref };
export type Coverage = { dataset: string; trade_day: string | null; state: string; scope?: string; reason?: string | null; received_at?: string | null; row_count?: number | null; evidence_refs?: Ref[] };
export type Envelope<T> = { data: T; meta: { generated_at: string; quality: { status: string; reasons?: string[] } } };
export type Counts = { state?: string; N?: number | null; valid_n?: number; missing_n?: number | null; [key: string]: unknown };
export type Cohort = Counts & { open_mean_pct?: string | null; close_mean_pct?: string | null; open_median_pct?: string | null; close_median_pct?: string | null; known_close_mean_pct?: string | null; promotion_pct?: string | null; promotion_n?: number | null; down_limit_n?: number | null; samples?: Row[]; membership_complete?: boolean; classification_unknown_n?: number | null };
export type IntradayLimits = Counts & { up_valid_n: number; down_valid_n: number; up_unknown_n: number | null; down_unknown_n: number | null; known_opened_up: number | null; known_opened_down: number | null; opened_up: number | null; opened_down: number | null; touched_up: number | null; touched_down: number | null; close_up: number | null; close_down: number | null; no_price_limit_n?: number | null; no_trade_n?: number | null; quality_flags?: Metrics['quality_flags'] };
export type Metrics = {
  basis?: 'daily' | 'intraday_snapshot'; source_time?: string | null; source_received_at?: string | null; scope?: string;
  state: string; sample?: Counts & { daily_loaded?: boolean }; breadth?: Counts & { up: number | null; down: number | null; flat: number | null; bins: Record<string, number | null>; distribution?: { version: string; state: string; bins: Record<string, number | null>; valid_n: number; missing_n: number | null; limit_unverified_n: number | null; no_limit_n: number | null } };
  turnover?: { amount_yuan: string | null; change_pct?: string | null; top20_share_pct?: string | null; previous?: { amount_yuan: string | null; top20_share_pct?: string | null } };
  limits?: { supplier_reported: Counts & { up: number | null; down: number | null; broken: number | null; first_board: number | null; second_board: number | null; third_plus: number | null; max_height: number | null; second_height?: number | null; ladder: Record<string, number> | null; broken_rate_pct: string | null; up_down_ratio: string | null; max_disclosed_seal_amount_yuan?: string | null; seal_amount_state?: string }; price_verified: Counts & { up?: number | null; down?: number | null; broken?: number | null; unverified_n?: number; broken_unverified_n?: number }; intraday?: IntradayLimits };
  relay?: { by_height?: Record<string, Cohort>; previous_trade_day?: string | null; prior_trade_day?: string | null; unclassified_previous_limit_up_n?: number | null; first_board: Cohort; consecutive: Cohort; broken: Cohort; failed: Cohort; yesterday_limit_up: Cohort; failed_classification: Counts & { confirmed_failed_n: number | null; unknown_n: number | null; continued_n: number | null } };
  quality_flags?: Array<{ code: string; field?: string; count?: number; examples?: unknown[] }>;
};
export type Index = { ts_code: string; name: string; state: string; record: Row | null; coverage?: Coverage };
export type Intraday = { ts_code: string; name: string; state: string; selected_source: string | null; points: Row[]; segments: Row[][]; standard_point_count: number; missing_standard_minutes: string[]; extra_points: Row[] };
export type Theme = { ts_code: string; name: string; provider_record: Row; fund_records: Row[]; detail_available: boolean; disclosed_total: number | null; returned_member_count: number; truncated: boolean; member_records: Row[]; stock_records: Row[]; member_sample: { disclosed_member_n: number | null; matched_daily_n: number | null; known_up: number | null; known_down: number | null; known_flat: number | null; pool_up?: number | null; pool_broken?: number | null; known_turnover_yuan: string | null; missing_quote_n: number | null } };
export type Observation = { basis: string; source_time: string | null; received_at: string | null; scope: string; sample_count: number };
export type Context = { shared_date: string | null; state: string; calendar_state: string; knowledge_mode?: string; as_of_time?: string | null; coverage?: Coverage[]; evidence_refs?: Ref[]; observation?: Observation | null };
export type Day = Context & { metrics: Metrics; indices: Index[]; intraday: Intraday[]; themes: { items: Theme[] }; pools: { records: Row[] }; news: { records: Row[]; scope_note: string }; selection: { stock_records: Row[] } };
export type SeriesRecord = { observation?: Observation | null; trade_day: string; state: string; metrics: Metrics; indices: Index[]; themes: { records: Row[] }; coverage: Coverage[]; evidence_refs: Ref[] };
export type Series = Context & { trade_days: string[]; returned_day_count: number; calendar_window_complete: boolean; records: SeriesRecord[] };
export type Side = { state: string; records: Row[]; extra_rows: Row[]; disclosed_row_count: number; amount_yuan: string | null; known_amount_yuan: string | null };
export type Listing = { record_key: string; trade_day: string; ts_code: string; reason: string; source_rows: Row[]; headline: { l_buy: string | null; l_sell: string | null; net_amount: string | null }; headline_state: string; buy_top5: Side; sell_top5: Side; period: { reason_text: string } };
export type Lhb = Context & { records: Listing[]; selected_record: Listing | null; history_dates: string[]; unmatched_seat_records: Row[]; source_listing_count: number | null };
export type Seat = Context & { seat_name: string; records: Array<{ trade_day: string; ts_code: string; name: string | null; reason: string; amount_yuan: string | null; seat_record: Row; listing_record: Row | null; lhb_record_key: string }>; matched_record_count: number; truncated: boolean };
export type StockDetail = { ts_code: string; name: string | null; quote: Row | null; pool_record: Row | null; state: string };
export type StockView = Context & { items: StockDetail[] };
export type ThemeHistory = Context & { items: Array<{ ts_code: string; records: Array<{ trade_day: string; catalog_record: Row | null; fund_record: Row | null; state: string }> }> };
export const stages = ['未标记', '退潮', '冰点', '修复', '启动', '发酵', '分歧', '局部修复', '轮动', '低迷'] as const;
export const slots = ['主线候选', '次主线', '新方向', '轮动'] as const;
export const tags = ['持续跟踪', '待验证', '观察', '新增', '风险提示'] as const;
export type NotesContent = { format: 'tianyan.review-notes.v1'; stage: string; summary: string; money_effect: string; judgment: string;
  directions: Array<{ slot: string; sector_code: string; name: string; rationale: string }>;
  theme_stages: Array<{ sector_code: string; stage: string }>;
  core_stocks: Array<{ code: string; name: string; sector_code: string | null; role: string; rationale: string; tag: string }>;
  observations: Array<{ text: string; tag: string }>;
  lhb_notes: Array<{ record_key: string; stock_code: string; text: string; confirmed: boolean; evidence_ids: string[] }>;
  catalyst_links: Array<{ evidence_id: string; source_row: number; sector_code: string; comment: string }>;
  support_evidence: string; refuting_evidence: string; data_evidence_ids: string[]; confirmed_sections: string[];
};
export type Notes = { trade_day: string; state: string; content: NotesContent; revision: { id: string; version_number: number; received_at: string; content_hash: string } | null; head_id: string | null; is_current: boolean; writable?: boolean; evidence_status?: Array<{ evidence_id: string; state: string }> };
export const blankNotes = (): NotesContent => ({ format: 'tianyan.review-notes.v1', stage: '未标记', summary: '', money_effect: '', judgment: '', support_evidence: '', refuting_evidence: '', data_evidence_ids: [], directions: [], theme_stages: [], core_stocks: [], observations: [], lhb_notes: [], catalyst_links: [], confirmed_sections: [] });
export function number(value: unknown): number | null { if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null; const result = Number(value); return Number.isFinite(result) ? result : null; }
export const text = (value: unknown) => value === null || value === undefined || value === '' ? '—' : String(value);
export function pct(value: unknown, signed = true) { const n = number(value); return n === null ? '—' : `${signed && n > 0 ? '+' : ''}${n.toFixed(2)}%`; }
export function amount(value: unknown) { const n = number(value); return n === null ? '—' : `${n < 0 ? '−' : ''}${(Math.abs(n) / 1e8).toFixed(2)}亿`; }
export function turnover(value: unknown) { const n = number(value); return n === null ? '—' : `${(n / 1e12).toFixed(3)}万亿`; }
export const tone = (value: unknown) => (number(value) ?? 0) > 0 ? 'rise' : (number(value) ?? 0) < 0 ? 'fall' : 'neutral';
export const isIntradaySnapshot = (metrics: Metrics) => metrics.basis === 'intraday_snapshot';
export function snapshotTime(metrics: Metrics) {
  const raw = metrics.source_time;
  if (!raw || !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) || !Number.isFinite(Date.parse(raw))) return '时点待核';
  return new Date(raw).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit' });
}
export const metricLabel = (metrics: Metrics, value: string) => isIntradaySnapshot(metrics)
  ? value.replaceAll('收盘', '采样时点').replaceAll('今收', '现价').replaceAll('日线样本', '行情样本').replaceAll('日线', '行情') : value;

export function referencesFrom(...values: unknown[]): Ref[] {
  const refs = new Map<string, Ref>();
  function visit(value: unknown, reference = false) {
    if (!value || typeof value !== 'object') return;
    if (Array.isArray(value)) { value.forEach(v => visit(v, reference)); return; }
    const row = value as Record<string, unknown>;
    if (reference && typeof row.id === 'string') refs.set(row.id, row as Ref);
    for (const [key, item] of Object.entries(row)) visit(item, key === 'row_reference' || key === 'evidence_refs');
  }
  values.forEach(v => visit(v));
  return [...refs.values()];
}

export function feedbackCount(group: Cohort | undefined, kind: 'promotion' | 'down_limit') {
  const N = number(group?.N), valid = number(group?.[`${kind}_valid_n`]), count = number(group?.[`${kind}_n`]);
  if (N === null || valid === null || count === null) return '—';
  if (N === 0) return '0';
  if (valid === 0) return `待核${N}`;
  return valid < N ? `已核${count} · 待核${N - valid}` : String(count);
}
