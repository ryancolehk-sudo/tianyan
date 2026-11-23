import { cachedRead, invalidateReads, invalidateMutation } from './readCache';
import type { ReadCacheState, ReadTransport } from './readCache';
import { persistentScope } from './persistentReads';
import type { SessionResponse, ProviderListResponse, InstrumentListResponse, Health, Error as ErrorBody,
  EvidenceInput, EvidenceVersionResponse, EvidenceVersionListResponse, SnapshotInput, SnapshotResponse,
  SnapshotListResponse, JobReceiptResponse, CalendarInput, CalendarVersionResponse, CalendarVersionListResponse,
  CalendarLookupResponse, SourceHealthListResponse, DocumentInput, RevisionInput, DocumentResponse,
  DocumentListResponse, RevisionResponse, RevisionSummaryListResponse, ObservationInput, ObservationResponse, ObservationListResponse,
  ValidationInput, ValidationResponse, ValidationListResponse, ReviewArchiveResponse, ReviewArchiveListResponse,
  RuntimeSourceListResponse, RuntimeSourceResponse, SourceConfiguration, CollectionJobResponse, FetchRunListResponse, MarketQuoteResponse,
  DatasetListResponse, Envelope_BreadthSeries_, NewsListResponse, BarSeriesResponse, DataCoverageResponse, Envelope_AuctionView_, WatchlistResponse, TradingDatasetResponse, DailyHistoryRequest, Envelope_Job_ } from '../generated/schema';

export type { WatchlistResponse, TradingDatasetResponse } from '../generated/schema';

export class ApiError extends Error {
  constructor(public status: number, message: string, public requestId?: string, public code?: string) { super(message); }
}

let sessionEpoch = 0;
let sessionTransition = 0;
let pendingLogout: Promise<void> | undefined;
function beginSessionTransition() {
  const epoch = ++sessionEpoch;
  sessionTransition = epoch;
  window.dispatchEvent(new Event('session-changed'));
  return epoch;
}
function endSessionTransition(epoch: number) {
  if (sessionTransition !== epoch) return;
  sessionTransition = 0; sessionEpoch++;
  window.dispatchEvent(new Event('session-changed'));
}
export function request<T>(path: string, init?: RequestInit, readHints?: { active?: boolean }): Promise<T> {
  if (init) { invalidateMutation(path, false); return fetchResponse<T>(path, init).finally(() => invalidateMutation(path)); }
  return cachedRead(path, (signal, etag) => fetchResponse<ReadTransport<T>>(path, { signal,
    headers: { ...(etag ? { 'If-None-Match': etag } : {}), ...(readHints?.active ? { 'X-Workbench-Active': '1' } : {}) } }, true));
}

// Details already keep their own revision-scoped cache. Do not share an
// in-flight read across two parent revisions: retiring a page must abort only
// its own request while keeping the usual session and permission checks.
export async function cancellableRead<T>(path: string, signal: AbortSignal, active = false): Promise<T> {
  const response = await fetchResponse<ReadTransport<T>>(path, { signal, ...(active ? { headers: { 'X-Workbench-Active': '1' } } : {}) }, true);
  return response.value!;
}

async function fetchResponse<T>(path: string, init?: RequestInit, conditional = false): Promise<T> {
  const epoch = sessionEpoch;
  const requestScope = persistentScope();
  let response: Response;
  try { response = await fetch(path, { credentials: 'same-origin', ...init }); }
  catch { throw new ApiError(0, init?.signal?.aborted ? '读取超时或已取消，请重试。' : '连接暂时中断，请检查网络后重试。', undefined, init?.signal?.aborted ? 'read_timeout' : 'network_unavailable'); }
  const responseScope = response.headers.get('X-Cache-Scope');
  if (epoch !== sessionEpoch) throw new ApiError(0, '会话已变更。');
  // Background GETs issued by a retiring/anonymous screen must neither expire
  // an in-progress login nor restore its previous account's data.
  if (conditional && sessionTransition) throw new ApiError(0, '正在切换登录状态。');
  if (conditional && requestScope !== persistentScope()) throw new ApiError(0, '读取范围已变更。');
  if (responseScope && responseScope !== persistentScope()) {
    window.dispatchEvent(new Event('read-permission-denied'));
    throw new ApiError(403, '读取权限已变更，请重新核对登录状态。');
  }
  if (conditional && response.status === 304) return { notModified: true, etag: response.headers.get('ETag') ?? undefined, state: response.headers.get('X-Read-Cache-State') as ReadCacheState ?? 'fresh', builtAt: response.headers.get('X-Read-Cache-Built-At') ?? undefined, validatedAt: response.headers.get('X-Read-Cache-Validated-At') ?? undefined } as T;
  if (!response.ok) {
    if (response.status === 403) { invalidateReads(); window.dispatchEvent(new Event('read-permission-denied')); }
    const body = await response.json().catch(() => null) as ErrorBody | null;
    if (response.status === 401 && epoch === sessionEpoch && !sessionTransition) { sessionEpoch++; window.dispatchEvent(new Event('session-expired')); }
    throw new ApiError(response.status, body?.error?.message ?? '服务暂不可用，请稍后重试。', body?.request_id, body?.error?.code);
  }
  if (response.status === 204) return undefined as T;
  const value = await response.json();
  return conditional ? { value, etag: response.headers.get('ETag') ?? undefined, state: response.headers.get('X-Read-Cache-State') ?? value.meta?.read_cache?.state ?? 'fresh' } as T : value as T;
}

export const api = {
  // Ephemeral collection interests must not invalidate shared market reads.
  noteQuoteFocus: (body: import('../generated/schema').QuoteFocusRequest, csrf: string, signal?: AbortSignal) =>
    fetchResponse<import('../generated/schema').Envelope_QuoteFocusReceipt_>('/api/v1/quote-focus', {
      method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(body), signal }),
  noteKplFocus: (body: import('../generated/schema').KplFocusRequest, csrf: string, signal?: AbortSignal) =>
    fetchResponse<import('../generated/schema').Envelope_KplFocusReceipt_>('/api/v1/kpl-focus', {
      method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(body), signal }),
  chatGPTStatus: () => request<import('../features/chatgpt/model').ChatGPTEnvelope<import('../features/chatgpt/model').ChatGPTStatus>>('/api/v1/chatgpt/status'),
  chatGPTAccounts: () => request<import('../features/chatgpt/model').ChatGPTEnvelope<{ items: import('../features/chatgpt/model').ChatGPTAccount[] }>>('/api/v1/chatgpt/accounts'),
  chatGPTLogin: (body: { mode: 'login' | 'connect'; account_id?: string; new_account?: boolean }, csrf?: string) => request<import('../features/chatgpt/model').ChatGPTEnvelope<{ authorization_url: string }>>('/api/v1/chatgpt/login', { method: 'POST', headers: { 'Content-Type': 'application/json', ...(csrf ? { 'X-CSRF-Token': csrf } : {}) }, body: JSON.stringify(body) }),
  chatGPTWelcome: (id: string, csrf: string) => write<import('../features/chatgpt/model').ChatGPTEnvelope<import('../features/chatgpt/model').ChatGPTAccount>>('/api/v1/chatgpt/accounts/' + encodeURIComponent(id) + '/welcome', {}, csrf, crypto.randomUUID()),
  chatGPTUpdate: (id: string, body: { expected_revision: number; model?: string; use_for_ai?: boolean }, csrf: string) => request<import('../features/chatgpt/model').ChatGPTEnvelope<import('../features/chatgpt/model').ChatGPTAccount>>('/api/v1/chatgpt/accounts/' + encodeURIComponent(id), { method: 'PATCH', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify(body) }),
  chatGPTModels: (id: string, csrf: string) => write<import('../features/chatgpt/model').ChatGPTEnvelope<import('../features/data-center-live/api').ProbeResult>>('/api/v1/chatgpt/accounts/' + encodeURIComponent(id) + '/models', {}, csrf, crypto.randomUUID()),
  chatGPTProbe: (id: string, csrf: string) => write<import('../features/chatgpt/model').ChatGPTEnvelope<import('../features/data-center-live/api').ProbeResult>>('/api/v1/chatgpt/accounts/' + encodeURIComponent(id) + '/probe', {}, csrf, crypto.randomUUID()),
  chatGPTDisconnect: (id: string, csrf: string) => request<import('../features/chatgpt/model').ChatGPTEnvelope<{ account: import('../features/chatgpt/model').ChatGPTAccount; remote_revocation_unconfirmed: boolean }>>('/api/v1/chatgpt/accounts/' + encodeURIComponent(id), { method: 'DELETE', headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': crypto.randomUUID() } }),
  reviewDay: (query: URLSearchParams) => request<import('../features/daily-review/model').Envelope<import('../features/daily-review/model').Day>>('/api/v1/review-data/day?' + query),
  reviewAnalysis: (query: URLSearchParams) => request<import('../features/daily-review/model').Envelope<import('../features/daily-review/analysisTypes').Analysis>>('/api/v1/review-data/analysis?' + query),
  reviewSeries: (query: URLSearchParams) => request<import('../features/daily-review/model').Envelope<import('../features/daily-review/model').Series>>('/api/v1/review-data/series?' + query),
  reviewOverview: (query: URLSearchParams) => request<import('../features/daily-review/model').Envelope<import('../features/daily-review/PostcloseOverview').Postclose>>('/api/v1/review-data/overview?' + query),
  reviewLhb: (query: URLSearchParams) => request<import('../features/daily-review/model').Envelope<import('../features/daily-review/model').Lhb>>('/api/v1/review-data/lhb?' + query),
  reviewSeat: (query: URLSearchParams) => request<import('../features/daily-review/model').Envelope<import('../features/daily-review/model').Seat>>('/api/v1/review-data/seat-disclosures?' + query),
  reviewStocks: (query: URLSearchParams) => request<import('../features/daily-review/model').Envelope<import('../features/daily-review/model').StockView>>('/api/v1/review-data/stocks?' + query),
  reviewThemes: (query: URLSearchParams) => request<import('../features/daily-review/model').Envelope<import('../features/daily-review/model').ThemeHistory>>('/api/v1/review-data/theme-history?' + query),
  reviewNotes: (query: URLSearchParams) => request<import('../features/daily-review/model').Envelope<import('../features/daily-review/model').Notes>>('/api/v1/review-data/notes?' + query),
  reviewNoteDays: (query: URLSearchParams) => request<import('../features/daily-review/model').Envelope<{ items: Array<{ trade_day: string; stage: string; theme_stages: Array<{ sector_code: string; stage: string }> }> }>>('/api/v1/review-data/notes/days?' + query),
  reviewNoteHistory: (day: string) => request<import('../features/daily-review/model').Envelope<{ items: import('../features/daily-review/model').Notes[] }>>('/api/v1/review-data/notes/history?' + new URLSearchParams({ trade_day: day })),
  saveReviewNotes: (body: { trade_day: string; expected_head: string | null; content: import('../features/daily-review/model').NotesContent }, csrf: string, key: string) => write<import('../features/daily-review/model').Envelope<import('../features/daily-review/model').Notes>>('/api/v1/review-data/notes', body, csrf, key),
  collectReview: (body: unknown, csrf: string, key: string) => write<import('../features/daily-review/model').Envelope<{ batch_id: string; state: string }>>('/api/v1/review-data/collections', body, csrf, key),
  reviewCollection: (id: string) => request<import('../features/daily-review/model').Envelope<{ state: string; counts: Record<string, number>; jobs: Array<{ dataset: string; error_code?: string | null; state: string }> }>>('/api/v1/review-data/collections/' + encodeURIComponent(id)),
  workbenchView: (query: URLSearchParams) => request<import('../features/workbench-v2/model').WorkbenchResponse>('/api/v1/workbench-v2?' + query),
  session: () => request<SessionResponse>('/api/v1/session'),
  login: async (username: string, password: string) => {
    // A late DELETE must finish before a new POST can set the next session cookie.
    if (pendingLogout) await pendingLogout.catch(() => {});
    const epoch = beginSessionTransition();
    try { return await request<SessionResponse>('/api/v1/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) }); }
    finally { endSessionTransition(epoch); }
  },
  logout: async (csrf: string) => {
    if (pendingLogout) return pendingLogout;
    const epoch = beginSessionTransition();
    const operation = request<void>('/api/v1/session', { method: 'DELETE', headers: { 'X-CSRF-Token': csrf } }).finally(() => endSessionTransition(epoch));
    pendingLogout = operation;
    try { await operation; } finally { if (pendingLogout === operation) pendingLogout = undefined; }
  },
  providers: () => request<ProviderListResponse>('/api/v1/providers'),
  dataSources: () => request<RuntimeSourceListResponse>('/api/v1/data-sources'),
  configureSource: (body: SourceConfiguration, csrf: string, key: string) => write<RuntimeSourceResponse>('/api/v1/data-sources', body, csrf, key),
  refreshSource: (id: string, csrf: string, key: string, options?: { day: string }) => write<CollectionJobResponse>('/api/v1/data-sources/' + encodeURIComponent(id) + '/refresh', options ?? {}, csrf, key),
  job: (id: string) => request<Envelope_Job_>('/api/v1/jobs/' + encodeURIComponent(id)),
  fetchRuns: (source?: string, cursor?: string | null, jobId?: string) => request<FetchRunListResponse>('/api/v1/data-fetch-runs?' + new URLSearchParams({ ...(source ? { source_id: source } : {}), ...(cursor ? { cursor } : {}), ...(jobId ? { job_id: jobId } : {}) })),
  collectMarketHistory: (body: DailyHistoryRequest, csrf: string, key: string) => write<CollectionJobResponse>('/api/v1/market-history', body, csrf, key),
  instruments: (query: string) => request<InstrumentListResponse>('/api/v1/instruments?q=' + encodeURIComponent(query)),
  health: () => request<Health>('/health/ready'),
  marketQuotes: (instrument?: string, asOf?: string) => {
    const query = new URLSearchParams();
    if (instrument) query.set('instrument_id', instrument);
    if (asOf) query.set('as_of_time', asOf);
    return request<MarketQuoteResponse>('/api/v1/market-quotes' + (query.size ? '?' + query : ''));
  },
  marketQuotesBatch: (instruments: string[]) => request<MarketQuoteResponse>('/api/v1/market-quotes?' + new URLSearchParams({ instrument_ids: instruments.join(',') })),
  watchlist: () => request<WatchlistResponse>('/api/v1/watchlist'),
  saveWatchlist: (instruments: string[], csrf: string, key: string) => write<WatchlistResponse>('/api/v1/watchlist', { instrument_ids: instruments }, csrf, key),
  tradingDatasets: (dataset?: string, family?: string, day?: string, instrument?: string, source?: string) => {
    const query = new URLSearchParams();
    if (dataset) query.set('dataset', dataset);
    if (family) query.set('family', family);
    if (day) query.set('day', day);
    if (instrument) query.set('instrument_id', instrument);
    if (source) query.set('source_id', source);
    return request<TradingDatasetResponse>('/api/v1/trading-datasets' + (query.size ? '?' + query : ''));
  },
  datasets: (dataset?: string, family?: string, day?: string, instrument?: string) => {
    const query = new URLSearchParams();
    if (dataset) query.set('dataset', dataset);
    if (family) query.set('family', family);
    if (day) query.set('day', day);
    if (instrument) query.set('instrument_id', instrument);
    return request<DatasetListResponse>('/api/v1/reference-datasets' + (query.size ? '?' + query : ''));
  },
  breadthLatest: () => request<Envelope_BreadthSeries_>('/api/v1/market-breadth-history?latest_only=true'),
  breadthHistory: (day?: string, source: 'auto' | 'tushare_market_rt' | 'tencent_market_rt' | 'ashare_breadth' = 'auto') => {
    const query = new URLSearchParams({ ...(day ? { day } : {}), ...(source !== 'auto' ? { source } : {}) });
    return request<Envelope_BreadthSeries_>('/api/v1/market-breadth-history' + (query.size ? '?' + query : ''));
  },
  marketNews: (family?: string) => request<NewsListResponse>('/api/v1/market-news' + (family ? '?family=' + encodeURIComponent(family) : '')),
  marketBars: (instrument: string, adjustment: 'unadjusted' | 'forward' | 'backward', options?: {
    interval?: '1m' | '5m' | '15m' | '30m' | '60m' | '1d'; day?: string; source_id?: string; include_live?: boolean;
    before?: string; start_day?: string; end_day?: string; limit?: number;
  }) => request<BarSeriesResponse>('/api/v1/market-bars?' + new URLSearchParams({ instrument_id: instrument, adjustment,
    ...(options?.interval ? { interval: options.interval } : {}), ...(options?.day ? { day: options.day } : {}),
    ...(options?.source_id ? { source_id: options.source_id } : {}), ...(options?.include_live ? { include_live: 'true' } : {}),
    ...(options?.before ? { before: options.before } : {}), ...(options?.start_day ? { start_day: options.start_day } : {}),
    ...(options?.end_day ? { end_day: options.end_day } : {}), ...(options?.limit ? { limit: String(options.limit) } : {}),
  })),
  dataCoverage: () => request<DataCoverageResponse>('/api/v1/data-coverage'),
  marketAuction: (day?: string, family?: string) => request<Envelope_AuctionView_>('/api/v1/market-auction' + (day || family ? '?' + new URLSearchParams({ ...(day ? { trade_day: day } : {}), ...(family ? { family } : {}) }) : '')),
  evidence: (cursor?: string | null, key?: string) => request<EvidenceVersionListResponse>('/api/v1/evidence?' + new URLSearchParams({ ...(cursor ? { cursor } : {}), ...(key ? { source_key: key } : {}) })),
  evidenceVersion: (id: string) => request<EvidenceVersionResponse>('/api/v1/evidence/' + encodeURIComponent(id)),
  calendarEvidence: () => request<EvidenceVersionListResponse>('/api/v1/evidence?kind=calendar&limit=500'),
  saveEvidence: (body: EvidenceInput, csrf: string, key: string) => write<EvidenceVersionResponse>('/api/v1/evidence', body, csrf, key),
  sourceHealth: () => request<SourceHealthListResponse>('/api/v1/source-health'),
  snapshots: (cursor?: string | null) => request<SnapshotListResponse>('/api/v1/snapshots' + (cursor ? '?cursor=' + encodeURIComponent(cursor) : '')),
  snapshot: (id: string) => request<SnapshotResponse>('/api/v1/snapshots/' + encodeURIComponent(id)),
  createSnapshot: (body: SnapshotInput, csrf: string, key: string) => write<JobReceiptResponse>('/api/v1/snapshots', body, csrf, key),
  calendars: (cursor?: string | null) => request<CalendarVersionListResponse>('/api/v1/calendar-versions' + (cursor ? '?cursor=' + encodeURIComponent(cursor) : '')),
  saveCalendar: (body: CalendarInput, csrf: string, key: string) => write<CalendarVersionResponse>('/api/v1/calendar-versions', body, csrf, key),
  calendarSession: (exchange: string, version: string, day: string) => request<CalendarLookupResponse>('/api/v1/trading-sessions?' + new URLSearchParams({ exchange, calendar_version: version, trade_day: day })),
  nextSession: (id: string, day: string) => request<CalendarLookupResponse>('/api/v1/calendar-versions/' + encodeURIComponent(id) + '/next-session?' + new URLSearchParams({ after: day })),
  reviewArchives: (day: string, cursor?: string | null) => request<ReviewArchiveListResponse>('/api/v1/review-archives?' + new URLSearchParams({ ...(day ? { target_trade_day: day } : {}), ...(cursor ? { cursor } : {}) })),
  reviewArchive: (id: string) => request<ReviewArchiveResponse>('/api/v1/review-archives/' + encodeURIComponent(id)),
  researchDocuments: (kind: DocumentInput['kind'], day: string, cursor?: string | null) => request<DocumentListResponse>('/api/v1/research-documents?' + new URLSearchParams({ kind, source_trade_day: day, ...(cursor ? { cursor } : {}) })),
  targetPlans: (day: string, cursor?: string | null) => request<DocumentListResponse>('/api/v1/research-documents?' + new URLSearchParams({ kind: 'plan', target_trade_day: day, ...(cursor ? { cursor } : {}) })),
  observations: (plan: string, scenario?: string, cursor?: string | null) => request<ObservationListResponse>('/api/v1/auction-observations?' + new URLSearchParams({ plan_revision_id: plan, ...(scenario ? { scenario_id: scenario } : {}), ...(cursor ? { cursor } : {}) })),
  observation: (id: string) => request<ObservationResponse>('/api/v1/auction-observations/' + encodeURIComponent(id)),
  saveObservation: (body: ObservationInput, csrf: string, key: string) => write<ObservationResponse>('/api/v1/auction-observations', body, csrf, key),
  validations: (plan: string, scenario?: string, cursor?: string | null) => request<ValidationListResponse>('/api/v1/validations?' + new URLSearchParams({ plan_revision_id: plan, ...(scenario ? { scenario_id: scenario } : {}), ...(cursor ? { cursor } : {}) })),
  validation: (id: string) => request<ValidationResponse>('/api/v1/validations/' + encodeURIComponent(id)),
  saveValidation: (body: ValidationInput, csrf: string, key: string) => write<ValidationResponse>('/api/v1/validations', body, csrf, key),
  researchDocument: (id: string) => request<DocumentResponse>('/api/v1/research-documents/' + encodeURIComponent(id)),
  researchRevision: (id: string) => request<RevisionResponse>('/api/v1/revisions/' + encodeURIComponent(id)),
  researchHistory: (id: string, cursor?: string | null) => request<RevisionSummaryListResponse>('/api/v1/research-documents/' + encodeURIComponent(id) + '/revisions' + (cursor ? '?cursor=' + encodeURIComponent(cursor) : '')),
  saveResearchDocument: (body: DocumentInput, csrf: string, key: string) => write<DocumentResponse>('/api/v1/research-documents', body, csrf, key),
  saveResearchRevision: (id: string, body: RevisionInput, csrf: string, key: string, head: number) => write<RevisionResponse>('/api/v1/research-documents/' + encodeURIComponent(id) + '/revisions', body, csrf, key, head),
  publishResearchRevision: (id: string, csrf: string, key: string, head: number) => write<RevisionResponse>('/api/v1/revisions/' + encodeURIComponent(id) + '/publish', {}, csrf, key, head),
};

export function write<T>(path: string, body: unknown, csrf: string, key: string, head?: number) {
  return request<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': key, ...(head === undefined ? {} : { 'If-Match': '"' + head + '"' }) }, body: JSON.stringify(body) });
}
