import { request } from '../../shared/api';
import type { Envelope } from './api';

export type CapabilityOption = {
  provider_id: string; capability_id: string; name: string;
  billing: 'free' | 'free_quota' | 'points' | 'subscription' | 'unknown';
  billing_label: string; billing_note: string; reference_url: string; checked_at: string | null;
  entitlement: 'verified' | 'locked' | 'unknown'; implemented: boolean; state: string;
  last_success_at: string | null; source_ids: string[]; selected: boolean; reason: string | null; scope: string;
};
export type Supplier = {
  id: string; name: string; template: string; kind: 'market' | 'news' | 'catalog' | 'calendar';
  endpoint: string; connection_id: string | null; credential_required: boolean;
  credential_configured: boolean; enabled: boolean; serving_count: number; capabilities: CapabilityOption[];
};
export type RouteCapability = {
  id: string; name: string; group: string; description: string; routable?: boolean;
  current_provider_id: string | null; fallback_provider_ids?: string[]; effective_provider_id: string | null; options: CapabilityOption[];
};
export type CapabilityMatrix = { revision: number; policy: 'purchased_first' | 'free_first'; providers: Supplier[]; capabilities: RouteCapability[] };
export type RouteChange = { capability_id: string; provider_id: string; fallback_provider_ids?: string[]; allow_points?: boolean };
export type PortScope = { provider_id?: string; capability_id?: string };
export type PortResult = { provider_id: string; capability_id: string; state: 'configured' | 'existing' | 'attention'; reason: string };
export type ConfiguredPorts = { matrix: CapabilityMatrix; items: PortResult[] };
export type MinuteJob = {
  job_id: string; state: string; error_code: string | null;
  progress: null | { day: string; mode: string; scope_count: number; chunk_offset: number; chunk_requested: number;
    chunk_completed: number; failed_count: number; completed_through: number; remaining: number; request_limit: number;
    next_cursor: string | null; failed_instruments: string[]; is_complete: boolean; scope_traversed?: boolean; scope_complete?: boolean };
};

function write<T = CapabilityMatrix>(path: string, body: unknown, csrf: string, method = 'POST') {
  return request<Envelope<T>>(path, { method, headers: {
    'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': crypto.randomUUID(),
  }, body: JSON.stringify(body) });
}
export const capabilityApi = {
  read: () => request<Envelope<CapabilityMatrix>>('/api/v1/data-center/capabilities'),
  routes: (expected_revision: number, changes: RouteChange[], csrf: string) => write('/api/v1/data-center/routes', { expected_revision, changes }, csrf, 'PATCH'),
  configure: (expected_revision: number, scope: PortScope, csrf: string) => write<ConfiguredPorts>('/api/v1/data-center/routes/configure', { expected_revision, ...scope }, csrf),
  connect: (id: string, apiKey: string, csrf: string, pointsSourceId?: string) => write('/api/v1/data-center/providers/' + encodeURIComponent(id) + '/connect', { ...(apiKey ? { api_key: apiKey } : {}), ...(pointsSourceId ? { allow_points: true, points_source_id: pointsSourceId } : {}) }, csrf),
  fullMinute: (body: { day?: string; cursor?: string; full_market: boolean; allow_points: true }, csrf: string) => request<Envelope<{ job_id: string; state: string; request_limit: number; scope_count: number }>>('/api/v1/data-center/full-minute', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify(body),
  }),
  minuteJob: (id: string) => request<Envelope<MinuteJob>>('/api/v1/data-center/minute-jobs/' + encodeURIComponent(id)),
};
