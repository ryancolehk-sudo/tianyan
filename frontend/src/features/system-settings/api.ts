import { request } from '../../shared/api';
import type { Preferences } from '../../shared/preferences';

export type Envelope<T> = { data: T; meta: { app_mode: string; generated_at?: string } };
export type HealthItem = { id: string; label: string; state: 'ok' | 'warning' | 'unavailable' | 'disabled' | 'unknown'; checked_at: string | null; message: string };
export type CacheStats = { count: number; bytes: number; enabled: boolean };
export type Backup = { id: string; name: string; created_at: string; completed_at: string | null; size_bytes: number | null; sha256: string | null; integrity_state: 'pending' | 'passed' | 'failed'; recovery_state: 'unverified' | 'passed' | 'failed'; recovery_verified_at: string | null; app_mode: string; schema_revision: string; build_revision: string };
export type Job = { id: string; kind: 'backup' | 'update.download' | 'update.install'; status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'manual_recovery'; stage: string; message: string | null; error_code: string | null; created_at: string; started_at: string | null; completed_at: string | null; result: Record<string, unknown> | null };
export type Overview = { version: string; app_mode: string; health: HealthItem[]; cache: CacheStats; last_backup: Backup | null; maintenance: { active: boolean; stage: string | null; updater_connected: boolean; backup_connected: boolean }; update: { configured: boolean; state: string } };
export type SessionItem = { id: string; created_at: string; expires_at: string; current: boolean };
export type Activity = { id: string; at: string; action: string; label: string; outcome: string | null; actor: string | null; details: Record<string, unknown> };
export type ActivityList = { items: Activity[]; next_cursor?: string | null };
export type Release = { id: string; version: string; published_at: string; notes: string; channel: string; automatic_supported: boolean; reason: string | null; downloaded: boolean; size_bytes: number | null };
export type Update = { configured: boolean; state: 'unconfigured' | 'unchecked' | 'checking' | 'available' | 'current' | 'error' | 'manual'; message: string | null; checked_at: string | null; current_version: string; release: Release | null; download: Job | null; install: Job | null };
const root = '/api/v1/system';
function mutation<T>(path: string, csrf: string, body?: unknown, method = 'POST', idempotent = true) {
  return request<T>(path, { method, headers: { 'X-CSRF-Token': csrf, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...(idempotent ? { 'Idempotency-Key': crypto.randomUUID() } : {}) }, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
}
export const settingsApi = {
  overview: () => request<Envelope<Overview>>(root + '/overview'),
  preferences: () => request<Envelope<Preferences>>('/api/v1/me/preferences'),
  savePreferences: (value: Preferences, csrf: string) => mutation<Envelope<Preferences>>('/api/v1/me/preferences', csrf, { start_page: value.start_page, review_lookback: value.review_lookback, news_category: value.news_category, expected_revision: value.revision }, 'PUT'),
  password: (current_password: string, new_password: string, csrf: string) => mutation<void>('/api/v1/me/password', csrf, { current_password, new_password }, 'POST', false),
  sessions: () => request<Envelope<{ items: SessionItem[] }>>('/api/v1/me/sessions'),
  revoke: (id: string, csrf: string) => mutation<void>('/api/v1/me/sessions/' + encodeURIComponent(id), csrf, undefined, 'DELETE', false),
  revokeOthers: (csrf: string) => mutation<void>('/api/v1/me/sessions/others', csrf, undefined, 'DELETE', false),
  cache: () => request<Envelope<CacheStats>>(root + '/cache'),
  clearCache: (csrf: string) => mutation<Envelope<CacheStats>>(root + '/cache', csrf, undefined, 'DELETE'),
  backups: (cursor?: string) => request<Envelope<{ items: Backup[]; next_cursor?: string | null }>>(root + '/backups' + (cursor ? '?' + new URLSearchParams({ cursor }) : '')),
  backup: (csrf: string) => mutation<Envelope<Job>>(root + '/maintenance-jobs', csrf, { kind: 'backup' }),
  job: (id: string) => request<Envelope<Job>>(root + '/maintenance-jobs/' + encodeURIComponent(id)),
  activity: (query: URLSearchParams) => request<Envelope<ActivityList>>(root + '/activity?' + query),
  diagnostics: () => request<Envelope<Record<string, unknown>>>(root + '/diagnostics'),
  updates: () => request<Envelope<Update>>(root + '/updates'),
  checkUpdate: (csrf: string) => mutation<Envelope<Update>>(root + '/updates/check', csrf, {}),
  downloadUpdate: (release_id: string, csrf: string) => mutation<Envelope<Job>>(root + '/updates/download', csrf, { release_id }),
  installUpdate: (release_id: string, csrf: string) => mutation<Envelope<Job>>(root + '/updates/install', csrf, { release_id, confirmed: true, drafts_saved: true }),
};
