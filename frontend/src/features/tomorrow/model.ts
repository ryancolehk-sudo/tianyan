export type Envelope<T> = { data: T; meta?: { app_mode?: string }; request_id?: string };
export type ScenarioStatus = 'pending' | 'triggered' | 'confirmed' | 'invalidated' | 'risk_veto' | 'insufficient_data';
export type Slot = 'manual' | 'evening' | 'overnight';
export type RunView = {
  id: string; source_trade_day: string; target_trade_day: string | null; slot: Slot; state: string; stage: string;
  report_id?: string | null; as_of_time: string; created_at: string; completed_at?: string | null;
  error_code?: string | null; limitations: string[]; stages?: Array<{ role: string; state: string; ai_run_id?: string }>;
};
export type EvidenceRef = { id: string; source_name: string; source_id: string; received_at: string; source_time?: string | null; published_at?: string | null; available_at?: string | null; content_hash: string; url?: string | null; source_url?: string | null; title?: string | null };
export type Section = { key: string; title: string; state: string; summary: string; paragraphs: string[]; analysis?: Array<{ title: string; text: string; evidence_refs: string[]; authorship: string; review_required?: boolean }>; columns: Array<{ key: string; label: string }>; rows: Array<Record<string, unknown>>; evidence_refs: string[]; limitations: string[] };
export type Judgment = { object_type: 'market' | 'sector' | 'stock' | 'event'; object_id: string; judgment: string; support_refs?: string[]; refuting_refs?: string[] };
export type ReportView = {
  id: string; run_id: string; revision_number: number; based_on_id?: string | null; source_trade_day: string;
  target_trade_day: string | null; as_of_time: string; slot: Slot; status: 'draft' | 'confirmed'; summary: string;
  sections: Section[]; dossier: Record<string, unknown>; judgments: Judgment[]; evidence_refs: EvidenceRef[];
  limitations: string[]; created_at: string; confirmed_at?: string | null;
  ai_audit?: { audit_status?: string; audit_issues?: unknown; [key: string]: unknown } | null;
  audit_binding?: { visible_content_hash?: string; ai_run_id?: string; ai_input_hash?: string; input_hash?: string; audit_scope_complete?: boolean; passed?: boolean; [key: string]: unknown } | null;
};
export type Holding = { code: string; quantity: number; sellable_quantity: number; cost?: number | null; sector_codes?: string[] };
export type RiskPolicy = { approved: boolean; price_rule: 'none' | 'prior_range_20'; max_total_weight?: number | null; max_stock_weight?: number | null; max_sector_weight?: number | null; risk_per_trade?: number | null };
export type SettingsView = { revision: number; enabled: boolean; use_ai: boolean; holdings: Holding[]; cash?: number | null; risk_policy: RiskPolicy; ai_source_ids: string[]; sources?: Array<{ id: string; name: string; ai_allowed: boolean }>; evening_time: string; overnight_time: string; timezone: string };
export type Validation = { id?: string; scenario_id: string; status: ScenarioStatus; comment: string; evidence_refs: string[]; created_at?: string; report_id?: string; report_revision?: number };
export type ShadowEvaluation = {
  progress?: { eligible_shadow_target_day_count?: number; goal?: number; observation_window_complete?: boolean; acceptance_status?: string };
  reports?: { latest_count?: number; confirmed_latest_count?: number };
  model_usage?: { cost_cny?: { total?: number | string | null; known_total?: number | string | null; unknown_attempts?: number } };
  timing?: { end_to_end_ms_p95?: number | string | null };
  data_quality?: { core_data_missing?: number };
};
export type DossierKind = 'stock' | 'sector' | 'event';
export type DossierPage = { items: Array<Record<string, unknown>>; total: number; offset: number; limit: number; kind: DossierKind; as_of_time: string };
export const stateLabel: Record<string, string> = { queued: '等待处理', running: '正在研究', waiting: '等待前序资料', waiting_ai: '等待 AI', succeeded: '已完成', complete: '已完成', partial: '部分可用', draft: '草稿', confirmed: '已确认', failed: '生成失败', blocked: '需要补齐资料', outcome_unknown: '请求结果待核实', disabled: 'AI 未开启', unavailable: '数据不足', no_data: '尚无数据', insufficient_data: '数据不足', pending: '待观察', triggered: '已触发待确认', invalidated: '失效', risk_veto: '风险否决', cancelled: '已取消', skipped: '未执行', degraded: '降级完成', needs_review: '待人工审查' };
export const slotLabel: Record<Slot, string> = { manual: '手动研究', evening: '晚间完整稿', overnight: '早间补充' };
export function label(state: string) { return stateLabel[state] ?? '状态待核实'; }
export function text(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  if (Array.isArray(value)) return value.map(text).join('、');
  if (typeof value === 'object') { const row = value as Record<string, unknown>; return text(row.label ?? row.name ?? row.summary ?? row.description ?? row.value ?? null); }
  return '—';
}
export function records(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.map(item => typeof item === 'string' ? { summary: item } : item).filter((item): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item));
}
export function displayTime(value?: string | null) { if (!value || !Number.isFinite(Date.parse(value))) return '时间未提供'; return new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }); }
export function safeUrl(value?: string | null) { try { const url = new URL(value ?? ''); return ['https:', 'http:'].includes(url.protocol) ? url.href : null; } catch { return null; } }
export function isRunning(run?: RunView | null) { return !!run && ['queued', 'running', 'waiting', 'waiting_ai', 'preparing', 'pending'].includes(run.state); }
