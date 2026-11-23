import { request } from '../../shared/api';

export type Envelope<T> = { data: T; meta?: Record<string, unknown> };
export type ConnectionKind = 'model' | 'market' | 'news';
export type Protocol = 'openai_chat' | 'openai_responses' | 'anthropic_messages';
export type FeatureId = 'theme_summary' | 'news_summary' | 'emotion_analysis' | 'direction_analysis' | 'plan_generation' | 'period_digest';
export type ConnectionOptions = {
  timeout_seconds: number; max_output_tokens: number;
  input_price_per_million_cny?: number | null; output_price_per_million_cny?: number | null;
};
export type ConnectionInput = {
  name: string; kind: ConnectionKind; template: string; endpoint: string;
  protocol: Protocol | null; model: string | null; api_key?: string; enabled: boolean;
  source_ids: string[]; options: ConnectionOptions; expected_revision?: number;
};
export type Capability = {
  source_id: string; family: string; name: string; state: string; last_success_at: string | null;
  data_time?: string | null; enabled: boolean; interval_seconds: number; collection_enabled?: boolean;
};
export type ConnectionTest = {
  status: 'success' | 'error' | 'unknown' | 'queued' | 'running'; at: string | null; revision: number | null;
  error_code?: string | null; elapsed_ms?: number | null; preview?: string | null;
};
export type ConnectionView = Omit<ConnectionInput, 'api_key' | 'expected_revision'> & {
  id: string; revision: number; credential_configured: boolean; owner_id?: string | null;
  credential_origin: 'vault' | 'environment' | 'none'; capabilities?: Capability[];
  credential_error_code?: string | null;
  last_test: ConnectionTest | null; tested_revision?: number | null;
};
export type ConnectionTemplate = { id: string; name: string; kind: ConnectionKind; endpoint: string; credential_required: boolean; endpoints?: string[]; source_families?: string[] };
export type ConnectionIndex = { items: ConnectionView[]; templates: ConnectionTemplate[]; credential_store_ready: boolean };
export type ProbeInput = ConnectionInput & { id?: string; source_id?: string };
export type ProbeResult = {
  status: 'success' | 'error' | 'unknown' | 'queued' | 'running'; elapsed_ms: number | null;
  error_code?: string | null; message: string; preview?: string | null;
  models?: { id: string; name?: string }[]; revision?: number; request_count: number;
  job_id?: string | null; source_id?: string | null; capability?: string | null;
  test_proof?: string | null;
};
export type FeatureBinding = {
  id: FeatureId; module: string; name: string; implemented: boolean; enabled: boolean;
  connection_id: string | null; effective_connection_id: string | null; fallback_connection_id: string | null;
};
export type AiSettings = { enabled: boolean; default_connection_id: string | null; revision: number; features: FeatureBinding[]; personal_connection_id?: string | null; personal_model_ready?: boolean };
export type ServiceState = {
  id: string; name: string; kind: ConnectionKind; status: string; latest_data_at: string | null;
  last_success_at: string | null; last_attempt_at: string | null; error_code: string | null; impact_modules: string[];
};
export type RunningTask = {
  id: string; kind: string; feature: string | null; service_name: string | null; state: string;
  stage: string | null; created_at: string; started_at: string | null; completed_at: string | null; error_code: string | null;
};
export type Issue = { id: string; title: string; message: string; connection_id: string | null; job_id?: string | null; retry_at?: string | null };
export type Overview = {
  services_count: number; running_tasks: number; today_requests: number; pending_issues: number;
  services: ServiceState[]; tasks: RunningTask[]; issues: Issue[]; updated_at: string;
};
export type CallRecord = {
  id: string; kind: 'ai' | 'data'; task: string; service_name: string; connection_id: string | null; job_id: string | null;
  status: string; started_at: string; completed_at: string | null; elapsed_ms: number | null; request_count: number;
  error_code: string | null; retry_at: string | null; input_tokens?: number | null; output_tokens?: number | null;
  cost_cny?: number | string | null; details: Record<string, unknown>;
};
export type CallFilters = { kind?: 'ai' | 'data'; connection_id?: string; errors_only?: boolean; from_date?: string; to_date?: string; cursor?: string };
export type CallIndex = { items: CallRecord[]; next_cursor: string | null };
export type EvidenceRef = { id: string; title: string; url?: string | null; source_name?: string | null; source_time?: string | null };
export type RunView = {
  id: string; job_id: string | null; feature: FeatureId; state: string; stage: string | null;
  connection_id: string | null; model: string | null; created_at: string; completed_at: string | null;
  error_code: string | null; input_hash: string; evidence_refs: EvidenceRef[]; limitations: string[];
  result: { summary: string; items: { title: string; text: string; evidence_refs: string[] }[] } | null;
  usage?: { input_tokens?: number | null; output_tokens?: number | null; cost_cny?: string | number | null } | null;
};
export type AiRunInput = { feature: FeatureId; trade_day?: string; as_of_time?: string; news_id?: string; regenerate?: boolean };
export type RunQuery = { feature?: FeatureId; trade_day?: string; news_id?: string };

function query(input: Record<string, string | boolean | undefined>) {
  const params = new URLSearchParams();
  Object.entries(input).forEach(([key, value]) => { if (value !== undefined && value !== '') params.set(key, String(value)); });
  return params.size ? '?' + params : '';
}

function mutate<T>(path: string, body: unknown, csrf: string, method = 'POST', key: string = crypto.randomUUID()) {
  return request<Envelope<T>>(path, { method, headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': key }, ...(method === 'DELETE' ? {} : { body: JSON.stringify(body) }) });
}

export const dataCenterApi = {
  integrations: () => request<Envelope<ConnectionIndex>>('/api/v1/integrations'),
  save: (body: ConnectionInput, csrf: string, id?: string) => mutate<ConnectionView>('/api/v1/integrations' + (id ? '/' + encodeURIComponent(id) : ''), body, csrf, id ? 'PATCH' : 'POST'),
  remove: (id: string, csrf: string) => mutate<void>('/api/v1/integrations/' + encodeURIComponent(id), null, csrf, 'DELETE'),
  probe: (body: ProbeInput, csrf: string) => mutate<ProbeResult>('/api/v1/integrations/probe', body, csrf),
  discover: (body: ProbeInput, csrf: string) => mutate<ProbeResult>('/api/v1/integrations/discover-models', body, csrf),
  settings: () => request<Envelope<AiSettings>>('/api/v1/ai/settings'),
  saveSettings: (body: { enabled?: boolean; default_connection_id?: string | null; expected_revision?: number }, csrf: string) => mutate<AiSettings>('/api/v1/ai/settings', body, csrf, 'PATCH'),
  saveBinding: (feature: FeatureId, body: Pick<FeatureBinding, 'enabled' | 'connection_id' | 'fallback_connection_id'>, csrf: string) => mutate<AiSettings>('/api/v1/ai/bindings/' + feature, body, csrf, 'PATCH'),
  overview: () => request<Envelope<Overview>>('/api/v1/data-center/overview'),
  calls: (filters: CallFilters) => request<Envelope<CallIndex>>('/api/v1/data-center/calls' + query(filters)),
  createRun: (body: AiRunInput, csrf: string, key?: string) => mutate<RunView>('/api/v1/ai/runs', body, csrf, 'POST', key),
  runs: (filters: RunQuery) => request<Envelope<{ items: RunView[]; next_cursor: string | null }>>('/api/v1/ai/runs' + query(filters)),
  run: (id: string) => request<Envelope<RunView>>('/api/v1/ai/runs/' + encodeURIComponent(id)),
};

export function connectionInput(item: ConnectionView): ConnectionInput {
  return { name: item.name, kind: item.kind, template: item.template, endpoint: item.endpoint, protocol: item.protocol, model: item.model,
    enabled: item.enabled, source_ids: item.source_ids, options: item.options, expected_revision: item.revision };
}

export function credentialError(item: ConnectionView): string | null {
  return item.kind === 'model' ? item.credential_error_code || (item.credential_configured ? null : 'credentials_missing') : null;
}
