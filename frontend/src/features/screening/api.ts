import { request, write } from '../../shared/api';
import type { Catalogue, ContextInput, Envelope, ObservationEvent, PreviewInput, ResultState, Rule, RuleSave, RunResult, RunSummary } from './types';

const base = '/api/v1/screening';
const ruleQuery = (ruleId?: string) => ruleId ? '?' + new URLSearchParams({ rule_id: ruleId }) : '';

export const screeningApi = {
  catalogue: () => request<Envelope<Catalogue>>(base + '/catalogue'),
  rules: () => request<Envelope<{ items: Rule[]; limit: number }>>(base + '/rules'),
  save: (body: RuleSave, csrf: string, key: string) => write<Envelope<Rule>>(base + '/rules', body, csrf, key),
  preview: (body: PreviewInput, csrf: string) => write<Envelope<RunResult>>(base + '/preview', body, csrf, crypto.randomUUID()),
  run: (id: string, body: ContextInput, csrf: string, key: string) => write<Envelope<RunResult>>(base + '/rules/' + encodeURIComponent(id) + '/runs', body, csrf, key),
  runs: (ruleId: string) => request<Envelope<{ items: RunSummary[] }>>(base + '/runs' + ruleQuery(ruleId)),
  result: (id: string, state?: ResultState, offset = 0) => request<Envelope<RunResult>>(base + '/runs/' + encodeURIComponent(id) + '?' + new URLSearchParams({ ...(state ? { state } : {}), offset: String(offset), limit: '100' })),
  events: (ruleId?: string) => request<Envelope<{ items: ObservationEvent[] }>>(base + '/events' + ruleQuery(ruleId)),
};
