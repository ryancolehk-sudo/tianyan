import { useEffect, useSyncExternalStore } from 'react';
import { ApiError, cancellableRead } from '../../shared/api';
import { persistentGetMany, persistentPutMany } from '../../shared/persistentReads';
import { invalidateReads, readKey } from '../../shared/readCache';
import type { SectorMember, Stock, TerminalDetails, WorkbenchData } from './types';
import { useReadNotification } from '../../shared/BackgroundRefresh';

type Detail = { stock: Stock; auctions: WorkbenchData['auction_days']; events: WorkbenchData['events']; seals: WorkbenchData['stock_seals'] };
type SectorDetail = { members: Stock[]; trackingMembers?: SectorMember[]; trackingMemberCount?: number; trackingTruncated?: boolean; day: string | null; history: WorkbenchData['funds_history'] };
const contexts = new Map<string, DetailStore>();
let active: DetailStore | undefined;
let prepared: DetailStore | undefined;
let hintTimer: ReturnType<typeof setTimeout> | undefined;
const retryDelays = [2_000, 5_000, 15_000, 30_000];
const preparingRetryDelays = [250, 500, 1_000, 2_000];
const connected = () => navigator.onLine !== false;
class DetailVersionMismatch extends Error {}

function detailQuery(day: string, codes: string[], sector: string, asOf: string, observationCutoff: string) {
  return new URLSearchParams({ day, ...(codes.length ? { codes: codes.join(',') } : {}), ...(sector ? { sector } : {}),
    ...(asOf ? { as_of_time: asOf } : observationCutoff ? { observation_cutoff: observationCutoff } : {}) });
}
type BackgroundBatch = { codes: string[]; sector: string; retired: boolean };
type DetailRequest = { controller: AbortController; batch?: BackgroundBatch; retired: boolean };

class DetailStore {
  stocks = new Map<string, Detail>();
  sectors = new Map<string, SectorDetail>();
  failures = new Map<string, string>();
  private listeners = new Set<() => void>();
  private pending = new Map<string, DetailRequest>();
  private queue: string[] = [];
  private boards: string[] = [];
  private attempted = new Set<string>();
  private completed = new Set<string>();
  private completedBoards = new Set<string>();
  private hydrated: Promise<void>;
  private bytes = 0;
  private background: BackgroundBatch | undefined;
  private backgroundRetryAfter = 0;
  private backgroundFailures = 0;
  private foreground = 0;
  private selectionEpoch = 0;
  private versionFailures = new Set<string>();
  private preparing = new Set<string>();
  private retryable = new Set<string>();
  private retryTimer: ReturnType<typeof setTimeout> | undefined;
  private retryStep = 0;
  private selected = { code: '', sector: '' };
  private epoch = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  stopped = false;
  version = 0;
  constructor(readonly day: string, readonly asOf: string, readonly revision: string, readonly observationCutoff = '') {
    this.hydrated = persistentGetMany([this.indexKey], (value): value is { stocks: string[]; sectors: string[]; sectorFormat?: number } => {
      if (!value || typeof value !== 'object') return false;
      const row = value as { stocks?: unknown; sectors?: unknown };
      return Array.isArray(row.stocks) && row.stocks.every(code => typeof code === 'string') && Array.isArray(row.sectors) && row.sectors.every(code => typeof code === 'string');
    }).then(rows => { if (this.stopped) return; const saved = rows.get(this.indexKey); saved?.stocks.forEach(code => this.completed.add(code)); if (saved?.sectorFormat === 2) saved.sectors.forEach(code => this.completedBoards.add(code)); this.emit(); });
  }
  private get contextKey() { return JSON.stringify([this.day, this.asOf, this.revision]); }
  private get indexKey() { return `terminal-detail-index:${this.contextKey}`; }
  private stockKey(code: string) { return `terminal-detail:${this.contextKey}:stock:${code}`; }
  // Older saved board projections only contain the bounded funds panel list.
  // They cannot satisfy the full-sector tracker or its completion manifest.
  private sectorKey(code: string) { return `terminal-detail:${this.contextKey}:sector-v2:${code}`; }
  private async restore(codes: string[], board: string) {
    const stocks = await persistentGetMany(codes.map(code => this.stockKey(code)), (value, key): value is Detail => {
      const detail = value as Detail | undefined;
      return !!detail?.stock && key === this.stockKey(detail.stock.code) && Array.isArray(detail.auctions) && Array.isArray(detail.events) && Array.isArray(detail.seals);
    });
    if (this.stopped) return;
    for (const code of codes) { const saved = stocks.get(this.stockKey(code)); if (saved) { this.stocks.set(code, saved); this.completed.add(code); this.bytes += JSON.stringify(saved).length * 2; } else this.completed.delete(code); }
    if (board) {
      const sectors = await persistentGetMany([this.sectorKey(board)], (value): value is SectorDetail => {
        const detail = value as SectorDetail | undefined;
        return !!detail && Array.isArray(detail.members) && Array.isArray(detail.history) && (typeof detail.day === 'string' || detail.day === null)
          && Array.isArray(detail.trackingMembers) && detail.trackingMembers.every(row => typeof row.code === 'string' && typeof row.name === 'string')
          && Number.isInteger(detail.trackingMemberCount) && detail.trackingMemberCount! >= detail.trackingMembers.length && typeof detail.trackingTruncated === 'boolean';
      });
      if (this.stopped) return;
      const saved = sectors.get(this.sectorKey(board)); if (saved) { this.sectors.set(board, saved); this.completedBoards.add(board); } else this.completedBoards.delete(board);
    }
    this.emit();
  }
  private persistIndex() { persistentPutMany([{ key: this.indexKey, value: { stocks: [...this.completed], sectors: [...this.completedBoards], sectorFormat: 2 } }]); }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  snapshot = () => this.version;
  isPreparing(key: string) { return this.preparing.has(key); }
  emit() { this.version++; this.listeners.forEach(listener => listener()); }
  select(code: string, sector: string) {
    if (code !== this.selected.code || sector !== this.selected.sector) {
      clearTimeout(this.retryTimer); this.retryTimer = undefined; this.retryStep = 0;
      // The newest click owns the foreground slot, including before hydration finishes.
      this.selectionEpoch++;
      this.foreground = 0;
      for (const [key, request] of this.pending) {
        if (request.batch) continue;
        request.retired = true; request.controller.abort(); this.pending.delete(key);
      }
      this.selected = { code, sector };
    }
    this.ensure(code, sector); this.scheduleRetry();
  }
  published(paths: string[]) {
    if (this.stopped || active !== this || this.asOf || document.visibilityState !== 'visible' || !connected()) return;
    const matches = (key: string, board = false) => {
      if (!key || !this.retryable.has(key) || !this.failures.has(key)) return false;
      const query = detailQuery(this.day, board ? [] : [key], board ? key : '', this.asOf, this.observationCutoff);
      const path = readKey('/api/v1/workbench-details?' + query);
      return paths.some(prefix => prefix === '/api/v1/workbench-details' || readKey(prefix) === path);
    };
    const code = matches(this.selected.code) ? this.selected.code : '';
    const board = matches(this.selected.sector, true) ? this.selected.sector : '';
    if (code || board) this.ensure(code, board, true);
  }
  private scheduleRetry() {
    if (this.retryTimer || this.foreground || this.stopped || active !== this || document.visibilityState !== 'visible' || !connected()) return;
    if (![this.selected.code, this.selected.sector].some(key => key && this.retryable.has(key) && this.failures.has(key))) return;
    const preparing = [this.selected.code, this.selected.sector].some(key =>
      this.preparing.has(key) && !this.versionFailures.has(key));
    const epoch = this.epoch;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = undefined;
      if (epoch !== this.epoch || this.stopped || active !== this || document.visibilityState !== 'visible' || !connected()) return;
      this.retryStep = Math.min(this.retryStep + 1, retryDelays.length - 1);
      this.ensure(this.selected.code, this.selected.sector, true);
    }, (preparing ? preparingRetryDelays : retryDelays)[this.retryStep]);
  }
  resume() { this.schedule(); this.scheduleRetry(); }
  reconnect() {
    if (this.stopped || active !== this || document.visibilityState !== 'visible' || !connected()) return;
    clearTimeout(this.retryTimer); this.retryTimer = undefined; this.retryStep = 0;
    this.backgroundRetryAfter = 0; this.backgroundFailures = 0;
    const retryable = (key: string) => key && (!this.failures.has(key) || this.retryable.has(key)) ? key : '';
    this.ensure(retryable(this.selected.code), retryable(this.selected.sector), true);
    this.resume();
  }
  deactivate() {
    this.epoch++; clearTimeout(this.timer); clearTimeout(this.retryTimer);
    this.timer = this.retryTimer = undefined;
    for (const key of this.pending.keys()) this.attempted.delete(key);
    if (this.background) {
      this.background.retired = true;
      for (const key of [...this.background.codes, ...(this.background.sector ? [this.background.sector] : [])]) this.attempted.delete(key);
    }
    new Set(this.pending.values()).forEach(request => { request.retired = true; request.controller.abort(); }); this.pending.clear();
    this.foreground = 0; this.background = undefined;
  }
  enqueue(codes: string[]) { this.queue = [...new Set([...codes, ...this.queue])].filter(code => !this.attempted.has(code)); this.schedule(); }
  enqueueBoards(codes: string[]) { this.boards = [...new Set([...codes, ...this.boards])]; this.schedule(); }
  schedule() {
    if (this.timer || this.stopped || this.versionFailures.size || active !== this || !connected()) return;
    this.timer = setTimeout(() => { this.timer = undefined; void this.next(); }, Math.max(500, this.backgroundRetryAfter - performance.now()));
  }
  private async next() {
    if (this.stopped || this.versionFailures.size || active !== this || document.visibilityState !== 'visible' || !connected() || this.background || this.foreground) return;
    if (performance.now() < this.backgroundRetryAfter) { this.schedule(); return; }
    // Budget speculative reads to two contexts / 192 MiB of serialized detail. A full
    // cache stops speculative reads, rather than fetching/evicting in a loop.
    if (this.bytes > 96 * 1024 * 1024) return;
    await this.hydrated;
    if (this.stopped || active !== this || this.foreground || document.visibilityState !== 'visible' || !connected()) return;
    const codes = this.queue.filter(code => !this.completed.has(code) && !this.stocks.has(code) && !this.pending.has(code) && !this.attempted.has(code) && !this.failures.has(code)).slice(0, 24);
    const board = this.boards.find(code => !this.completedBoards.has(code) && !this.sectors.has(code) && !this.pending.has(code) && !this.attempted.has(code) && !this.failures.has(code)) ?? '';
    if (!codes.length && !board) return;
    if (board) this.attempted.add(board);
    codes.forEach(code => this.attempted.add(code));
    const batch: BackgroundBatch = { codes, sector: board, retired: false };
    this.background = batch;
    const epoch = this.epoch;
    try { await this.load(codes, board, false, batch); } finally {
      if (epoch === this.epoch && this.background === batch) { this.background = undefined; this.schedule(); }
    }
  }
  private promoteSelection(code: string, sector: string) {
    const batch = this.background;
    if (!batch || ![code === this.selected.code ? code : '', sector === this.selected.sector ? sector : '']
      .some(key => key && this.pending.get(key)?.batch === batch)) return;
    // Retire the whole speculative batch before taking the foreground slot.
    // Its other keys stay queued and become eligible again after selection.
    batch.retired = true; this.background = undefined;
    const requests = new Set<DetailRequest>();
    for (const [key, request] of this.pending) {
      if (request.batch !== batch) continue;
      request.retired = true; requests.add(request); this.pending.delete(key);
    }
    for (const key of [...batch.codes, ...(batch.sector ? [batch.sector] : [])]) this.attempted.delete(key);
    requests.forEach(request => request.controller.abort());
  }
  ensure(code: string, sector: string, retry = false) {
    if (this.stopped || active !== this) return;
    if (this.foreground) return;
    this.promoteSelection(code, sector);
    const codes = code && !this.stocks.has(code) && !this.pending.has(code) && (retry || !this.failures.has(code)) ? [code] : [];
    const board = sector && !this.sectors.has(sector) && !this.pending.has(sector) && (retry || !this.failures.has(sector)) ? sector : '';
    if (!codes.length && !board) return;
    // One foreground request can overtake the single background batch. Further
    // rapid selections wait for a completion notification and only the latest
    // selected identity is requested by the hook.
    this.foreground++;
    const epoch = this.epoch, selectionEpoch = this.selectionEpoch;
    const current = () => !this.stopped && epoch === this.epoch && selectionEpoch === this.selectionEpoch && active === this;
    void (async () => {
      await this.hydrated;
      if (!current()) return;
      await this.restore(codes, board);
      if (!current()) return;
      const missing = codes.filter(item => !this.stocks.has(item) && !this.pending.has(item));
      const missingBoard = board && !this.sectors.has(board) && !this.pending.has(board) ? board : '';
      if (!connected()) {
        for (const key of [...missing, ...(missingBoard ? [missingBoard] : [])]) {
          this.failures.set(key, '网络已断开，联网后自动继续。'); this.retryable.add(key);
        }
        return;
      }
      // The board the user just picked is ready before unrelated stock history.
      if (missingBoard) await this.load([], missingBoard, true, undefined, selectionEpoch);
      if (current() && missing.length) await this.load(missing, '', true, undefined, selectionEpoch);
    })().finally(() => { if (current()) { this.foreground--; this.emit(); this.resume(); } });
  }
  private async load(codes: string[], sector: string, visible = false, batch?: BackgroundBatch, selectionEpoch?: number) {
    if (batch?.retired) return;
    if (codes.length && sector) {
      // A slow or failed constituency read must not discard a valid stock
      // detail. Keep the existing single-request budget and fetch sequentially.
      const epoch = this.epoch;
      await this.load(codes, '', visible, batch);
      if (!batch?.retired && !this.stopped && active === this && epoch === this.epoch && connected() && !this.versionFailures.size
          && !this.sectors.has(sector) && !this.pending.has(sector)) await this.load([], sector, visible, batch);
      return;
    }
    const epoch = this.epoch, controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30_000);
    const keys = [...codes, ...(sector ? [sector] : [])];
    const request: DetailRequest = { controller, batch, retired: false };
    const current = () => !request.retired && !batch?.retired && !this.stopped && epoch === this.epoch && active === this
      && (selectionEpoch === undefined || selectionEpoch === this.selectionEpoch);
    keys.forEach(key => this.pending.set(key, request));
    const query = detailQuery(this.day, codes, sector, this.asOf, this.observationCutoff);
    this.emit();
    try {
      const foreground = visible && document.visibilityState === 'visible'
        && (codes.length === 1 && codes[0] === this.selected.code || !!sector && sector === this.selected.sector);
      const body = await cancellableRead<{ data: TerminalDetails }>('/api/v1/workbench-details?' + query, controller.signal, foreground);
      if (!current()) return;
      if (!body.data || body.data.trade_day !== this.day) throw new Error('详情日期不一致，请重试。');
      if (body.data.revision !== this.revision) {
        invalidateReads('/api/v1/workbench-view');
        window.dispatchEvent(new CustomEvent('read-cache-invalidated', { detail: '/api/v1/workbench-view' }));
        throw new DetailVersionMismatch('行情版本已更新，正在准备对应详情。将自动重试。');
      }
      // A matching response proves this parent revision is available again.
      // Release failed speculative batches without accepting any mismatched data.
      for (const key of this.versionFailures) { this.failures.delete(key); this.preparing.delete(key); this.retryable.delete(key); this.attempted.delete(key); }
      this.versionFailures.clear();
      keys.forEach(key => { this.failures.delete(key); this.preparing.delete(key); this.retryable.delete(key); });
      const persisted: Array<{ key: string; value: unknown }> = [];
      this.backgroundRetryAfter = 0; this.backgroundFailures = 0;
      for (const stock of body.data.stocks) {
        if (!codes.includes(stock.code)) continue;
        const detail = { stock, auctions: body.data.auction_days.map(day => ({ day: day.day, items: day.items.filter(item => item.code === stock.code) })), events: body.data.events.filter(item => item.code === stock.code), seals: body.data.seals[stock.code] ?? [] };
        this.bytes += JSON.stringify(detail).length * 2;
        this.stocks.set(stock.code, detail);
        this.completed.add(stock.code); persisted.push({ key: this.stockKey(stock.code), value: detail });
      }
      if (sector && body.data.sector_code === sector) {
        const board = { members: body.data.members, trackingMembers: body.data.tracking_members,
          trackingMemberCount: body.data.tracking_member_count, trackingTruncated: body.data.tracking_members_truncated,
          day: body.data.members_trade_day, history: body.data.funds_history ?? [] };
        this.bytes += JSON.stringify(board).length * 2;
        this.sectors.set(sector, board);
        this.completedBoards.add(sector); persisted.push({ key: this.sectorKey(sector), value: board });
      }
      persistentPutMany(persisted); this.persistIndex();
      if (![this.selected.code, this.selected.sector].some(key => this.retryable.has(key))) { clearTimeout(this.retryTimer); this.retryTimer = undefined; this.retryStep = 0; }
    } catch (reason) {
      if (current()) {
        const preparing = reason instanceof DetailVersionMismatch || reason instanceof ApiError && reason.code === 'read_cache_warming';
        const retry = preparing || reason instanceof ApiError && (reason.status === 0 || reason.status === 429 || reason.status >= 500);
        if (retry && !visible) {
          this.backgroundRetryAfter = performance.now() + retryDelays[Math.min(this.backgroundFailures++, retryDelays.length - 1)];
        }
        keys.forEach(key => { this.failures.set(key, reason instanceof Error ? reason.message : '详情读取失败，请重试。');
          if (preparing) this.preparing.add(key); else this.preparing.delete(key);
          if (retry) this.retryable.add(key); if (reason instanceof DetailVersionMismatch) this.versionFailures.add(key); });
      }
    } finally {
      clearTimeout(timeout);
      keys.forEach(key => { if (this.pending.get(key) === request) this.pending.delete(key); });
      if (current()) { this.emit(); this.scheduleRetry(); }
    }
  }
  stop() { this.stopped = true; this.deactivate(); this.stocks.clear(); this.sectors.clear(); this.listeners.clear(); }
}

function storeFor(data: WorkbenchData, asOf: string) {
  if (!data.detail_revision) return undefined;
  const key = `${data.trade_day}:${asOf}:${data.detail_revision}`;
  let store = contexts.get(key);
  const observationCutoff = asOf ? '' : data.detail_observation_cutoff ?? '';
  if (store && !store.observationCutoff && observationCutoff) { store.stop(); contexts.delete(key); store = undefined; }
  if (!store) {
    store = new DetailStore(data.trade_day, asOf, data.detail_revision, observationCutoff); contexts.set(key, store);
    while (contexts.size > 2) {
      const oldest = [...contexts.keys()].find(candidate => candidate !== key && contexts.get(candidate) !== active);
      if (oldest === undefined) break;
      contexts.get(oldest)!.stop(); contexts.delete(oldest);
    }
  }
  return store;
}
export function prepareTerminalDetails(data: WorkbenchData, asOf = '') {
  prepared = storeFor(data, asOf);
  prepared?.enqueue([...data.stocks.map(item => item.code), ...(data.detail_codes ?? [])]);
  prepared?.enqueueBoards(data.sectors.slice(0, 10).map(item => item.code));
}
export function startPreparedDetails() { if (prepared && !prepared.stopped && !active) { active = prepared; active.schedule(); } }
function clear() { clearTimeout(hintTimer); contexts.forEach(store => store.stop()); contexts.clear(); active = prepared = undefined; }
export function hintDetail(code: string, sector = false) {
  clearTimeout(hintTimer);
  hintTimer = setTimeout(() => active?.ensure(sector ? '' : code, sector ? code : ''), 150);
}
window.addEventListener('session-changed', clear);
window.addEventListener('session-expired', clear);
window.addEventListener('read-permission-denied', clear);
window.addEventListener('read-scope-changed', clear);
window.addEventListener('read-cache-cleared', clear);
window.addEventListener('online', () => active?.reconnect());
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') active?.resume(); });
const noop = () => () => {};
const zero = () => 0;
export function useTerminalDetails(data: WorkbenchData | null, code: string, sector: string, asOf: string, priority: string[]) {
  const store = data ? storeFor(data, asOf) : undefined;
  const version = useSyncExternalStore(store?.subscribe ?? noop, store?.snapshot ?? zero);
  const priorities = priority.join(',');
  useEffect(() => {
    if (!store || asOf || !data || (!code && !sector)) return;
    let pending: AbortController | undefined;
    const hint = async () => {
      if (store.stopped || active !== store || document.visibilityState !== 'visible' || !connected() || pending) return;
      if (store.day !== new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date())) return;
      const controller = new AbortController(); pending = controller;
      const timeout = setTimeout(() => controller.abort(), 5_000);
      try {
        // Keep only the visible selection interested while prices are unchanged.
        // These local reads do not wait for an upstream request.
        const queries = [code ? detailQuery(store.day, [code], '', '', store.observationCutoff) : null,
          sector ? detailQuery(store.day, [], sector, '', store.observationCutoff) : null];
        for (const query of queries) {
          if (query && !controller.signal.aborted) await cancellableRead('/api/v1/workbench-details?' + query, controller.signal, true);
        }
      } catch { /* Ordinary reads retain their own visible retry state. */ }
      finally { clearTimeout(timeout); if (pending === controller) pending = undefined; }
    };
    const timer = setInterval(() => void hint(), 60_000);
    return () => { clearInterval(timer); pending?.abort(); };
  }, [store, code, sector, asOf]);
  useEffect(() => {
    if (!store) return;
    if (active !== store) active?.deactivate();
    active = store; store.resume();
    const published = (event: Event) => {
      const detail = (event as CustomEvent<string | string[]>).detail;
      if (detail) store.published(typeof detail === 'string' ? [detail] : detail);
    };
    window.addEventListener('read-cache-invalidated', published);
    return () => { window.removeEventListener('read-cache-invalidated', published); if (active === store) { store.deactivate(); active = undefined; } };
  }, [store]);
  useEffect(() => {
    if (!store || !data) return;
    store.select(code, sector);
    store.enqueue([code, ...priority, ...data.selected_members.map(item => item.code), ...(data.detail_codes ?? [])].filter(Boolean));
    store.enqueueBoards([sector, ...data.sectors.slice(0, 10).map(item => item.code)].filter(Boolean));
  }, [store, code, sector, priorities, version]);
  const detail = store?.stocks.get(code);
  const board = store?.sectors.get(sector);
  const error = store?.isPreparing(code) ? '' : store?.failures.get(code) || '';
  const sectorError = store?.isPreparing(sector) ? '' : store?.failures.get(sector) || '';
  const busy = !!store && !!code && !detail && !error;
  const sectorBusy = !!store && !!sector && !board && !sectorError;
  const retry = () => store?.ensure(code, '', true), retrySector = () => store?.ensure('', sector, true);
  useReadNotification({ key: JSON.stringify([data?.trade_day, asOf, code]), title: `个股详情${code ? ' · ' + code : ''}`,
    waiting: busy, error, message: '详情准备较慢，后台会继续重试。', action: retry, actionLabel: '重试详情' });
  useReadNotification({ key: JSON.stringify([data?.trade_day, asOf, sector]), title: `板块成分${sector ? ' · ' + (data?.sectors.find(item => item.code === sector)?.name || sector) : ''}`,
    waiting: sectorBusy, error: sectorError, message: '成分资料准备较慢，后台会继续重试。', action: retrySector, actionLabel: '重试板块' });
  return { detail, board, busy, sectorBusy, error, retry, sectorError, retrySector };
}
