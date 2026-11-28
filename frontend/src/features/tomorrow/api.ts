import { request } from '../../shared/api';
import type { ApiReviewInput, CodexReviewInput, DossierKind, DossierPage, Envelope, EvidenceRef, Judgment, ReportView, RunView, SettingsView, ShadowEvaluation, Slot, Validation } from './model';
const root = '/api/v1/tomorrow';
export const runsPath = (day: string) => root + '/runs' + (day ? '?' + new URLSearchParams({ source_trade_day: day }) : '');
function mutate<T>(path: string, body: unknown, csrf: string, method = 'POST', key: string = crypto.randomUUID()) {
  return request<Envelope<T>>(root + path, { method, headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': key }, body: JSON.stringify(body) });
}
export const tomorrowApi = {
  evaluation: () => request<Envelope<ShadowEvaluation>>(root + '/evaluation'),
  runs: (day = '') => request<Envelope<{ items: RunView[] }>>(runsPath(day)),
  run: (id: string) => request<Envelope<RunView>>(root + '/runs/' + encodeURIComponent(id)),
  objects: (id: string, kind: DossierKind, offset: number) => request<Envelope<DossierPage>>(root + '/runs/' + encodeURIComponent(id) + '/objects?' + new URLSearchParams({ kind, offset: String(offset), limit: '10' })),
  generate: (body: { source_trade_day?: string; target_trade_day?: string; as_of_time?: string; slot: Slot; use_ai: boolean; model_connection_id?: string; codex_review?: CodexReviewInput; api_review?: ApiReviewInput; prepare?: boolean; regenerate?: boolean; expected_settings_revision?: number }, csrf: string, key?: string) => mutate<RunView>('/runs', body, csrf, 'POST', key),
  enableAi: (connectionId: string, csrf: string) => mutate<SettingsView>('/settings/enable-ai', { model_connection_id: connectionId }, csrf),
  prepare: (body: { source_trade_day?: string; candidates: string[] }, csrf: string) => mutate<Record<string, unknown>>('/prepare', body, csrf),
  report: (id: string) => request<Envelope<ReportView>>(root + '/reports/' + encodeURIComponent(id)),
  settings: () => request<Envelope<SettingsView>>(root + '/settings'),
  saveSettings: (body: Partial<SettingsView> & { expected_revision: number }, csrf: string) => mutate<SettingsView>('/settings', body, csrf, 'PATCH'),
  revise: (id: string, body: { expected_revision: number; summary?: string; judgments?: Judgment[]; reason: string }, csrf: string) => mutate<ReportView>('/reports/' + encodeURIComponent(id) + '/revisions', body, csrf),
  confirm: (id: string, revision: number, csrf: string) => mutate<ReportView>('/reports/' + encodeURIComponent(id) + '/confirm', { expected_revision: revision }, csrf),
  validations: (id: string) => request<Envelope<{ items: Validation[] }>>(root + '/reports/' + encodeURIComponent(id) + '/validations'),
  validationEvidence: (id: string) => request<Envelope<{ items: EvidenceRef[] }>>(root + '/reports/' + encodeURIComponent(id) + '/validation-evidence'),
  validate: (id: string, body: Omit<Validation, 'id' | 'created_at'>, csrf: string) => mutate<Validation>('/reports/' + encodeURIComponent(id) + '/validations', body, csrf),
};
