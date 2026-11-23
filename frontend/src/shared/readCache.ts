import { persistentGetMany, persistentPutMany, persistentScope, purgePersistentScope, setPersistentScope } from './persistentReads';

export type ReadCacheState = 'fresh' | 'refreshing' | 'stale';
export type ReadTransport<T> = { value?: T; notModified?: boolean; etag?: string; state?: ReadCacheState; builtAt?: string; validatedAt?: string };
type Snapshot = { value: unknown; expires: number; checked: number; etag?: string; state: ReadCacheState; restored?: boolean; error?: string; transientError?: boolean };
const snapshots = new Map<string, Snapshot>();
const pending = new Map<string, Promise<unknown>>();
const controllers = new Set<AbortController>();
const listeners = new Set<(key: string) => void>();
const loaders = new Map<string, () => Promise<unknown>>();
const retries = new Map<string, number>();
const retryAttempts = new Map<string, number>();
const activeReads = new Map<string, number>();
const RETRY_DELAYS = [2_000, 5_000, 15_000, 30_000];
const versions = new Map<string, number>();
let generation = 0, freshScope = 0;
let collecting: Set<string> | undefined;

export function readKey(path: string) { const url = new URL(path, location.origin); url.searchParams.sort(); return url.pathname + (url.searchParams.size ? '?' + url.searchParams : ''); }
export function readLifetime(path: string) {
  const name = path.split('?')[0];
  if (name === '/api/v1/ladder-observation') {
    const query = new URL(path, location.origin).searchParams;
    const local = new Intl.DateTimeFormat('sv-SE', { timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23' }).format(new Date()).split(' ');
    if (query.has('as_of_time') || query.get('trade_day') && query.get('trade_day') !== local[0]) return 300_000;
    return local[1] >= '09:25' && local[1] < '15:01' ? 15_000 : 60_000;
  }
  if (name === '/api/v1/auction-support') {
    const query = new URL(path, location.origin).searchParams;
    const clock = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date());
    return !query.has('as_of_time') && clock >= '09:15' && clock < '15:01' ? 15_000 : 60_000;
  }
  if (name === '/api/v1/auction-reference') {
    const query = new URL(path, location.origin).searchParams;
    const clock = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date());
    return !query.has('as_of_time') && clock >= '09:15' && clock < '09:30' ? 5_000 : 60_000;
  }
  if (name === '/api/v1/workbench-view') {
    const query = new URL(path, location.origin).searchParams;
    const parts = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date()).split(' ');
    if (query.get('view') === 'auction' && !query.has('as_of_time') && (!query.get('day') || query.get('day') === parts[0]) && parts[1] >= '09:14' && parts[1] < '09:36') return 5_000;
    if (!query.has('as_of_time') && (!query.get('day') || query.get('day') === parts[0]) && parts[1] >= '09:15' && parts[1] < '15:01') return 15_000;
  }
  if (name === '/api/v1/direction-data') return 30_000;
  if (name === '/api/v1/sector-membership') return 60_000;
  if (/^\/api\/v1\/review-data\/(day|series|lhb|stocks|theme-history|analysis)$/.test(name)) return 300_000;
  if (/^\/api\/v1\/(workbench-view|watchlist|news\/items|news\/editions)$/.test(name) || /^\/api\/v1\/news\/editions\/[^/]+$/.test(name)) return 60_000;
  if (/^\/api\/v1\/(integrations|ai\/settings|data-center\/(overview|calls)|review-data\/notes(?:\/days)?)$/.test(name)) return 30_000;
  return 0;
}
export function persistentReadAllowed(path: string) { return /^\/api\/v1\/(workbench-view|review-data\/(day|series|lhb|stocks|theme-history|analysis)|news\/(items|editions)(?:\/[^/]+)?)$/.test(path.split('?')[0]); }
const emit = (key: string) => listeners.forEach(listener => listener(key));
export function subscribeReads(listener: (key: string) => void) { listeners.add(listener); return () => { listeners.delete(listener); }; }
function stopRetry(key: string) { const timer = retries.get(key); if (timer !== undefined) window.clearTimeout(timer); retries.delete(key); retryAttempts.delete(key); }
function scheduleRetry(key: string) {
  const saved = snapshots.get(key);
  if (!saved || saved.state !== 'refreshing' || saved.error || !activeReads.has(key) || retries.has(key)) return;
  const attempt = retryAttempts.get(key) ?? 0;
  const current = generation;
  retries.set(key, window.setTimeout(() => {
    retries.delete(key);
    if (!activeReads.has(key) || current !== generation || typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    retryAttempts.set(key, attempt + 1); void loaders.get(key)?.().catch(() => {});
  }, RETRY_DELAYS[Math.min(attempt, RETRY_DELAYS.length - 1)]));
}
export function retainReads(paths: Iterable<string>) {
  const keys = [...new Set([...paths].map(readKey))];
  for (const key of keys) { activeReads.set(key, (activeReads.get(key) ?? 0) + 1); scheduleRetry(key); }
  return () => { for (const key of keys) { const count = (activeReads.get(key) ?? 1) - 1; if (count > 0) activeReads.set(key, count); else { activeReads.delete(key); stopRetry(key); } } };
}
export function captureReads<T>(load: () => T) { const previous = collecting, keys = new Set<string>(); collecting = keys; try { return { result: load(), keys }; } finally { collecting = previous; } }
export function freshRead<T>(load: () => Promise<T>): Promise<T> { freshScope++; try { return load(); } finally { freshScope--; } }
const matches = (prefix: string, key: string) => prefix.includes('?') ? key === readKey(prefix) : key === prefix || key.startsWith(prefix + '?') || key.startsWith(prefix.endsWith('/') ? prefix : prefix + '/');
// Polling expires saved values without retiring a validator already in flight.
// Context and permission changes continue to use invalidateReads.
export function expireReads(prefix: string) {
  for (const [key, saved] of snapshots) if (matches(prefix, key)) { saved.expires = 0; saved.state = 'stale'; }
}
export function invalidateReads(prefix?: string) {
  if (!prefix) { generation++; snapshots.clear(); pending.clear(); loaders.clear(); retries.forEach(timer => window.clearTimeout(timer)); retries.clear(); retryAttempts.clear(); return; }
  for (const key of new Set([...snapshots.keys(), ...pending.keys()])) if (matches(prefix, key)) {
    const saved = snapshots.get(key); if (saved) { saved.expires = 0; saved.state = 'stale'; }
    versions.set(key, (versions.get(key) ?? 0) + 1); pending.delete(key);
  }
}

export async function acceptPublications(publications: { path: string; revision: string }[]): Promise<string[]> {
  const current = generation;
  const affected = await Promise.all(publications.map(async ({ path, revision }) => {
    if (!/^\/api\/v1\/workbench-(?:view|details)\?/.test(path) || typeof revision !== 'string') return null;
    const key = readKey(path);
    // The initial stream state often arrives during the first GET. Let that
    // validator finish, then compare its version: never cancel it and fetch
    // the same large response twice, and never miss a publication it preceded.
    await pending.get(key)?.catch(() => {});
    if (current !== generation) return null;
    if (snapshots.get(key)?.etag?.replace(/^W\//, '') === '"' + revision + '"') return null;
    invalidateReads(key);
    return key;
  }));
  return [...new Set(affected.filter((path): path is string => !!path))];
}
export function invalidateMutation(path: string, notify = true) {
  if (path === '/api/v1/session') return;
  if (/^\/api\/v1\/(system|me)(?:\/|$)/.test(path)) return;
  let prefixes: string[] = [];
  if (path.startsWith('/api/v1/news/')) invalidateReads('/api/v1/news');
  else if (path.startsWith('/api/v1/review-data/notes')) invalidateReads('/api/v1/review-data/notes');
  else if (path === '/api/v1/watchlist') { invalidateReads(path); invalidateReads('/api/v1/workbench-view'); }
  else if (/^\/api\/v1\/(chatgpt|integrations|ai)(?:\/|$)/.test(path)) { invalidateReads('/api/v1/integrations'); invalidateReads('/api/v1/ai/settings'); invalidateReads('/api/v1/data-center'); }
  else invalidateReads();
  if (path.startsWith('/api/v1/news/')) prefixes = ['/api/v1/news/items']; // Edition state is patched locally; its archive pagination stays in place.
  else if (path.startsWith('/api/v1/review-data/notes')) prefixes = ['/api/v1/review-data/notes'];
  else if (path === '/api/v1/watchlist') prefixes = [path, '/api/v1/workbench-view'];
  else if (/^\/api\/v1\/(chatgpt|integrations|ai)(?:\/|$)/.test(path)) prefixes = ['/api/v1/integrations', '/api/v1/ai/settings', '/api/v1/data-center'];
  if (notify) window.dispatchEvent(new CustomEvent('read-cache-invalidated', { detail: prefixes }));
}
function clearSession() { invalidateReads(); controllers.forEach(controller => controller.abort()); controllers.clear(); setPersistentScope(null); }
window.addEventListener('session-changed', clearSession);
window.addEventListener('session-expired', clearSession);
window.addEventListener('read-permission-denied', () => { purgePersistentScope(); clearSession(); });
export function authorizeReadScope(next: string | null) { if (persistentScope() !== next) { clearSession(); setPersistentScope(next); window.dispatchEvent(new Event('read-scope-changed')); } }
export async function clearCurrentPageCache() {
  invalidateReads(); controllers.forEach(controller => controller.abort()); controllers.clear();
  const cleared = purgePersistentScope();
  window.dispatchEvent(new Event('read-cache-cleared'));
  return await cleared;
}
export function peekRead<T>(path: string, stale = false): T | undefined { const saved = snapshots.get(readKey(path)); return saved && (stale || saved.expires > Date.now()) ? saved.value as T : undefined; }
export function readStatus(path: string) { const saved = snapshots.get(readKey(path)); return saved ? { state: saved.state, updating: pending.has(readKey(path)) || saved.restored || saved.expires <= Date.now(), restored: !!saved.restored, checked: saved.checked, error: saved.error, transientError: saved.transientError } : undefined; }
function validSnapshot(value: unknown, key: string): value is Snapshot {
  if (!value || typeof value !== 'object') return false;
  const row = value as Snapshot;
  if (!row.value || typeof row.value !== 'object' || !('data' in row.value) || !Number.isFinite(row.checked) || row.etag && typeof row.etag !== 'string') return false;
  const data = (row.value as { data: Record<string, unknown> }).data;
  if (!data || typeof data !== 'object' || Array.isArray(data)) return false;
  const path = key.split('?')[0];
  if (path === '/api/v1/workbench-view') return typeof data.trade_day === 'string' && ['stocks', 'sectors', 'indices', 'auction_days', 'selected_members', 'funds_history', 'broken', 'events', 'stock_seals', 'capabilities'].every(name => Array.isArray(data[name])) && !!data.breadth && typeof data.breadth === 'object';
  if (path === '/api/v1/review-data/day') return !!data.metrics && typeof data.metrics === 'object' && Array.isArray(data.indices) && !!data.themes && typeof data.themes === 'object' && Array.isArray((data.themes as { items?: unknown }).items);
  if (path === '/api/v1/review-data/series') return Array.isArray(data.trade_days) && Array.isArray(data.records);
  if (path === '/api/v1/review-data/analysis') return ['emotion', 'groups', 'ladder', 'stock'].includes(String(data.kind)) && Array.isArray(data.trade_days) && Array.isArray(data.records);
  if (path === '/api/v1/review-data/lhb') return Array.isArray(data.records);
  if (path.startsWith('/api/v1/review-data/')) return Array.isArray(data.items);
  if (path === '/api/v1/news/items') return Array.isArray(data.items) && Array.isArray(data.sources);
  if (path === '/api/v1/news/editions') return Array.isArray(data.items);
  return typeof data.id === 'string';
}

export function cachedRead<T>(path: string, load: (signal: AbortSignal, etag?: string) => Promise<ReadTransport<T>>): Promise<T> {
  const key = readKey(path), force = freshScope > 0; collecting?.add(key);
  if (force) stopRetry(key);
  // Shared derived reads keep one validator. An uncached chart request can be
  // superseded on explicit refresh/reconnection; its older response is still
  // rejected by the generation/version checks below.
  if (force && pending.has(key) && readLifetime(key)) return pending.get(key) as Promise<T>;
  if (force) invalidateReads(key);
  const duration = readLifetime(key), current = generation, version = versions.get(key) ?? 0;
  const network = () => {
    const existing = pending.get(key); if (existing) return existing as Promise<T>;
    const networkGeneration = generation, networkVersion = versions.get(key) ?? 0;
    const controller = new AbortController(); controllers.add(controller);
    const timeout = window.setTimeout(() => controller.abort(), key.startsWith('/api/v1/review-data/series?') && new URL(path, location.origin).searchParams.get('lookback') === '60' ? 60_000 : 30_000);
    const next = load(controller.signal, snapshots.get(key)?.etag).then(response => {
      const saved = snapshots.get(key);
      if (response.notModified && !saved) throw new Error('已存资料不可用，请重新读取。');
      let value = response.notModified ? saved!.value as T : response.value!;
      if (response.notModified && value && typeof value === 'object' && 'meta' in value) {
        const envelope = value as { meta?: { read_cache?: Record<string, unknown> } };
        if (envelope.meta?.read_cache) value = { ...value, meta: { ...envelope.meta, read_cache: { ...envelope.meta.read_cache, state: response.state ?? 'fresh', ...(response.builtAt ? { built_at: response.builtAt } : {}), ...(response.validatedAt ? { validated_at: response.validatedAt } : {}) } } } as T;
      }
      if (networkGeneration !== generation || networkVersion !== (versions.get(key) ?? 0) || pending.get(key) !== next) return value;
      if (duration) {
        const state = response.state ?? 'fresh';
        const snapshot: Snapshot = { value, checked: Date.now(), expires: Date.now() + (state === 'refreshing' ? 2_000 : duration), etag: response.etag ?? saved?.etag, state,
          ...(state === 'stale' ? { error: '后台更新暂未完成，当前显示上次资料，可重试。', transientError: true } : {}) };
        snapshots.delete(key); snapshots.set(key, snapshot);
        if (snapshots.size > 100) snapshots.delete(snapshots.keys().next().value!);
        if (persistentReadAllowed(key)) persistentPutMany([{ key, value: snapshot }]);
        if (state !== 'refreshing') stopRetry(key);
        emit(key); scheduleRetry(key);
      }
      return value;
    }).catch(reason => {
      const saved = snapshots.get(key);
      const status = reason && typeof reason === 'object' && 'status' in reason ? Number(reason.status) : undefined;
      const code = reason && typeof reason === 'object' && 'code' in reason ? reason.code : undefined;
      const transientError = status !== undefined ? status >= 500 || status === 408 || status === 429 || status === 0 && (code === 'network_unavailable' || code === 'read_timeout') : reason instanceof TypeError;
      if (saved && networkGeneration === generation && networkVersion === (versions.get(key) ?? 0)) { stopRetry(key); snapshots.set(key, { ...saved, checked: Date.now(), expires: Date.now() + duration, state: 'stale', error: reason instanceof Error ? reason.message : '后台更新失败，请重试。', transientError }); emit(key); }
      throw reason;
    }).finally(() => { window.clearTimeout(timeout); controllers.delete(controller); if (pending.get(key) === next) pending.delete(key); });
    pending.set(key, next); return next;
  };
  if (duration) { loaders.delete(key); loaders.set(key, network); if (loaders.size > 100) loaders.delete(loaders.keys().next().value!); }
  const saved = snapshots.get(key);
  if (saved && !force) { if (saved.expires <= Date.now()) void network().catch(() => {}); return Promise.resolve(saved.value as T); }
  if (saved || !persistentReadAllowed(key) || !persistentScope()) return network();
  const existing = pending.get(key); if (existing) return existing as Promise<T>;
  // Only read the requested key; opening never waits for whole-database hydration.
  const restore = persistentGetMany([key], validSnapshot).then(rows => {
    if (current !== generation || version !== (versions.get(key) ?? 0)) throw new Error('读取范围已变更。');
    const row = rows.get(key); if (pending.get(key) === restore) pending.delete(key);
    if (row) { snapshots.set(key, { ...row, expires: 0, state: 'stale', restored: true }); emit(key); void network().catch(() => {}); return row.value as T; }
    return network();
  }); pending.set(key, restore); return restore;
}
