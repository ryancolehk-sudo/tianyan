// Explicit local acceptance only. No requests, credentials, or raw market rows.
import { peekRead, readKey, readStatus, subscribeReads } from './readCache';
import { persistentScope } from './persistentReads';
import type { WorkbenchEnvelope } from '../features/terminal/data';
import type { TerminalView } from '../features/terminal/types';
import { checkPrimarySnapshot, historicalProofBaselineSha256, proofCheckerVersion, readPrimarySnapshot } from './workbenchAcceptanceProof';
import proofCheckerSource from './workbenchAcceptanceProof.ts?raw';
import './workbenchAcceptance.css';

const DATABASE = 'tianyan-local-acceptance-v1';
const LIMIT = 50_000, QUEUE_LIMIT = 100;
let installed = false;
type RecordRow = { id?: number; run: string; scope: string | null; boot: string; type: string; observedAt: string; monotonicMs: number; detail: unknown };
const digest = async (value: string) => [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)))].map(byte => byte.toString(16).padStart(2, '0')).join('');
const clock = (value: unknown) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(value) && /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) && Number.isFinite(Date.parse(value)) ? value : null;
function clockStats(rows: { source_time?: string | null; source_snapshot_time?: string | null; source_received_at?: string | null }[]) {
  const range = (values: unknown[]) => { const known = values.map(clock).filter((value): value is string => value !== null).sort((a, b) => Date.parse(a) - Date.parse(b));
    return { known: known.length, min: known[0] ?? null, max: known.at(-1) ?? null }; };
  return { rows: rows.length, trade: range(rows.map(row => row.source_time)), snapshot: range(rows.map(row => row.source_snapshot_time)), receipt: range(rows.map(row => row.source_received_at)) };
}
const complete = (tx: IDBTransaction) => new Promise<void>((resolve, reject) => { tx.oncomplete = () => resolve(); tx.onerror = tx.onabort = () => reject(new Error('local_record_write_failed')); });
function openJournal() {
  return new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 1);
    request.onupgradeneeded = () => { const store = request.result.createObjectStore('records', { keyPath: 'id', autoIncrement: true }); store.createIndex('run', 'run'); };
    request.onsuccess = () => { request.result.onversionchange = () => request.result.close(); resolve(request.result); };
    request.onerror = request.onblocked = () => reject(new Error('local_record_unavailable'));
  });
}

export async function startAcceptanceRecorder(run: string) {
  if (installed || !/^[a-z0-9][a-z0-9-]{0,63}$/i.test(run)) return;
  installed = true;
  let checkerImplementationSha256: string;
  try { checkerImplementationSha256 = await digest(proofCheckerSource); }
  catch { installed = false; document.documentElement.dataset.acceptance = 'failed'; return; }
  const checkerIdentity = { checkerVersion: proofCheckerVersion, checkerImplementationSha256,
    checkerHashBasis: 'SHA-256 of UTF-8 bundled workbenchAcceptanceProof.ts source text' };
  const boot = crypto.randomUUID();
  let database: IDBDatabase | undefined, count = 0, queued = 0, dropped = 0, fault = '';
  let writes = Promise.resolve(), frame = 0, checking = false, again = false, last = '', lastScope: string | null = null;
  let lastData: WorkbenchEnvelope['data'] | undefined;
  let lastFrameMonotonic: number | null = null;
  let readSignals = 0, coalescedCaptureSignals = 0;
  let ownerScope: string | null = null;
  const fallback: RecordRow[] = [];
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'ty-acceptance-export'; button.setAttribute('aria-label', '保存本机验收记录');
  document.body.append(button);
  function status() {
    const own = !!persistentScope() && (!ownerScope || persistentScope() === ownerScope);
    button.hidden = !own;
    button.textContent = fault ? '验收记录中断 · 保存已有记录' : '保存本机验收记录';
    button.title = '仅保存在此浏览器；记录校验摘要，不上传或增加取数。';
    document.documentElement.dataset.acceptance = fault ? 'failed' : persistentScope() ? 'recording' : 'awaiting-authentication';
  }
  function fail(code: string) { if (!fault) fault = code; status(); }
  const initialization = openJournal().then(async db => {
    database = db;
    count = await new Promise<number>((resolve, reject) => {
      const request = db.transaction('records', 'readonly').objectStore('records').index('run').count(run);
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(new Error('local_record_unavailable'));
    });
  });
  void initialization.catch(() => {});
  function append(type: string, detail: unknown, scope = persistentScope(), at = new Date().toISOString(), monotonic = performance.now()) {
    if (!scope && (type === 'session_boundary' || type === 'heartbeat')) scope = ownerScope;
    if (!scope || ownerScope && scope !== ownerScope) return;
    const row: RecordRow = { run, scope, boot, type, observedAt: at, monotonicMs: monotonic, detail };
    fallback.push(row); if (fallback.length > 64) fallback.shift();
    if (fault) { dropped++; return; }
    if (queued >= QUEUE_LIMIT || count + queued >= LIMIT) { dropped++; fail('record_limit_reached'); return; }
    queued++;
    writes = writes.then(async () => {
      await initialization;
      if (!database) throw new Error('local_record_unavailable');
      const tx = database.transaction('records', 'readwrite');
      const done = complete(tx), store = tx.objectStore('records');
      const size = store.index('run').count(run); let committedCount = count;
      size.onsuccess = () => {
        if (size.result >= LIMIT) { fail('record_limit_reached'); tx.abort(); }
        else { store.add(row); committedCount = size.result + 1; }
      };
      await done; count = committedCount; status();
    }).catch(() => { dropped++; fail('local_record_write_failed'); }).finally(() => { queued--; });
  }
  const assets = () => [...new Set([...document.querySelectorAll<HTMLScriptElement>('script[src]')].map(node => node.src)
    .concat(performance.getEntriesByType('resource').map(entry => entry.name)).flatMap(value => {
      try { const url = new URL(value); return url.origin === location.origin && /^\/assets\/[^/]+\.js$/.test(url.pathname) ? [url.pathname] : []; } catch { return []; }
    }))].sort();
  function scopeChanged() {
    const scope = persistentScope();
    if (scope !== lastScope) {
      lastScope = scope; last = ''; lastData = undefined;
      if (ownerScope && scope && scope !== ownerScope) {
        append('scope_boundary', { authenticated: false, reason: 'authentication_scope_changed' }, ownerScope);
        fail('authentication_scope_changed'); return;
      }
      if (scope && !ownerScope) ownerScope = scope;
      append('scope_boundary', { authenticated: !!scope, format: 1, ...checkerIdentity, historicalProofBaselineSha256, assets: assets(),
        rawValuesStored: false, commitToPaintTimingKnown: false, continuousTradingDayProof: false }, scope ?? ownerScope);
    }
    status();
  }
  async function capture() {
    if (checking) { again = true; return; }
    if (fault) return;
    checking = true;
    try {
      lastFrameMonotonic = performance.now();
      scopeChanged();
      const scope = persistentScope();
      if (!scope || scope !== ownerScope || fault) return;
      if (document.visibilityState !== 'visible') return;
      const roots = [...document.querySelectorAll<HTMLElement>('.ty-terminal')].filter(node => node.getClientRects().length > 0 && !node.closest('[hidden]'));
      const root = roots.length === 1 ? roots[0] : undefined;
      if (!root) {
        const ladder = [...document.querySelectorAll('.lo-workspace')].some(node => node.getClientRects().length > 0 && !node.closest('[hidden]'));
        if (ladder) {
          const signature = 'unverifiable:ladder:independent-response';
          if (signature !== last) { last = signature; lastData = undefined; append('unverifiable', { ...checkerIdentity,
            view: 'ladder', reason: 'independent_ladder_response_not_bound' }); }
          return;
        }
        const signature = 'primary-root-count:' + roots.length + ':' + location.pathname;
        if (signature !== last) { last = signature; append('context_gap', { primaryRoots: roots.length,
          onWorkbench: location.pathname === '/workbench' || location.pathname === '/auction' }); }
        return;
      }
      const match = /\bty-terminal-(overview|auction|ladder|funds)\b/.exec(root.className);
      const path = root.getAttribute('data-acceptance-read-key');
      const revision = root.getAttribute('data-acceptance-revision');
      const requestId = root.getAttribute('data-acceptance-request');
      if (!match || !path || !revision || !requestId) {
        const signature = 'unbound:' + root.className;
        if (signature !== last) { last = signature; append('unverifiable', { reason: 'rendered_response_reference_missing' }); }
        return;
      }
      const view = match[1] as TerminalView, key = readKey(path);
      const envelope = peekRead<WorkbenchEnvelope>(key, true);
      if (!envelope || envelope.data.view !== view || envelope.meta?.read_cache?.revision !== revision || envelope.meta?.request_id !== requestId) {
        const signature = 'pending:' + key + ':' + revision + ':' + requestId;
        if (signature !== last) { last = signature; append('unverifiable', { reason: 'render_and_read_cache_reference_differ', view, revision }); }
        return;
      }
      if (view === 'auction' || view === 'funds') {
        const reason = view === 'auction' ? 'independent_auction_responses_not_bound' : 'independent_ths_response_not_bound';
        const signature = JSON.stringify(['unverifiable', view, reason, key, revision, requestId]);
        if (signature !== last) { last = signature; lastData = undefined; append('unverifiable', { ...checkerIdentity, view, reason, revision, requestId }); }
        return;
      }
      const dom = readPrimarySnapshot(view, root);
      const state = readStatus(key);
      const signature = JSON.stringify([scope, key, revision, requestId, dom, state?.state, state?.restored, !!state?.error]);
      if (signature === last && lastData === envelope.data) return;
      last = signature; lastData = envelope.data;
      const at = new Date().toISOString(), monotonic = performance.now();
      const proof = checkPrimarySnapshot(envelope.data, view, dom, true);
      if ('unverifiableReason' in proof && proof.unverifiableReason) {
        append('unverifiable', { ...checkerIdentity, view, revision, requestId, reason: proof.unverifiableReason }, scope, at, monotonic);
        return;
      }
      const regions = await Promise.all(proof.regions.map(async region => ({ name: region.name,
        expectedRows: region.expectedRows, observedRows: region.observedRows, passed: region.passed,
        expectedSha256: await digest(region.expectedCanonical), observedSha256: await digest(region.observedCanonical) })));
      const [apiDataSha256, domSha256, readKeySha256] = await Promise.all([digest(JSON.stringify(envelope.data)), digest(JSON.stringify(dom)), digest(key)]);
      if (persistentScope() !== scope) {
        append('proof_discarded', { reason: 'scope_changed_during_proof' }, scope);
        return;
      }
      const visibleCodes = new Set(dom.found ? [...dom.overview.flat(), ...dom.ladder, ...dom.auction.flatMap(list => list.rows)].flatMap(row => row.code ? [row.code] : []) : []);
      append('primary_painted', { ...checkerIdentity, view, revision, requestId, readKeySha256, apiDataSha256, domSha256,
        tradeDay: envelope.data.trade_day, mode: envelope.data.mode, cacheState: state?.state ?? null,
        restored: state?.restored ?? false, readError: !!state?.error, checkedAt: state?.checked ?? null,
        primaryMatches: proof.passed, readyForFreshPaint: proof.passed && dom.found && !dom.loading && state?.state === 'fresh' && !state.updating && !state.restored && !state.error,
        updating: state?.updating ?? null,
        loading: dom.found ? dom.loading : null, rootFound: dom.found,
        failures: proof.failures, regions,
        fundsMode: 'fundsMode' in proof ? proof.fundsMode : null,
        nativeFundPoints: envelope.data.intraday_funds?.points?.length ?? null,
        visibleQuoteClocks: clockStats(envelope.data.stocks.filter(row => visibleCodes.has(row.code))),
        displayedIndexClocks: clockStats(envelope.data.indices.filter(row => ['000001.SH', '399001.SZ', '399006.SZ'].includes(row.code))),
        auctionReceiptClocks: null,
        fundsSourceTime: clock(envelope.data.intraday_funds?.latest_source_time),
        fundsReceivedAt: clock(envelope.data.intraday_funds?.received_at),
        commitToPaintTimingKnown: false }, scope, at, monotonic);
    } catch { append('unverifiable', { reason: 'primary_capture_failed' }); }
    finally { checking = false; if (again) { again = false; schedule(); } }
  }
  function schedule() {
    if (fault) return;
    if (frame) { coalescedCaptureSignals++; return; }
    // Capture after the normal React update and a second animation frame.
    frame = requestAnimationFrame(() => { frame = requestAnimationFrame(() => { frame = 0; void capture(); }); });
  }
  const controlsChanged = (event: Event) => { if (event.target instanceof Element && event.target.closest('.ty-terminal-overview .ty-rank-controls')) schedule(); };
  document.addEventListener('input', controlsChanged); document.addEventListener('change', controlsChanged);
  const observer = new MutationObserver(schedule);
  observer.observe(document.getElementById('root') ?? document.body, { subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ['title', 'aria-describedby', 'aria-pressed', 'aria-sort', 'aria-label', 'hidden', 'data-acceptance-read-key', 'data-acceptance-revision', 'data-acceptance-request'] });
  const unsubscribe = subscribeReads(key => {
    if (key.startsWith('/api/v1/workbench-view?')) {
      const scope = persistentScope(), envelope = peekRead<WorkbenchEnvelope>(key, true);
      if (scope && (!ownerScope || scope === ownerScope) && envelope?.meta?.read_cache?.revision) {
        readSignals++;
        const at = new Date().toISOString(), monotonic = performance.now();
        const reference = { view: envelope.data.view, revision: envelope.meta.read_cache.revision, requestId: envelope.meta.request_id ?? null };
        void digest(key).then(readKeySha256 => append('read_observed', { ...reference, readKeySha256 }, scope, at, monotonic))
          .catch(() => append('unverifiable', { reason: 'read_reference_hash_failed' }, scope));
      }
    }
    schedule();
  });
  const visibility = () => { append('visibility', { state: document.visibilityState }); schedule(); };
  const lifecycle = () => { scopeChanged(); append('session_boundary', { authenticated: !!persistentScope() }); schedule(); };
  const pageError = () => append('page_error', { error: true });
  document.addEventListener('visibilitychange', visibility);
  window.addEventListener('error', pageError); window.addEventListener('unhandledrejection', pageError);
  for (const name of ['session-changed', 'session-expired', 'read-permission-denied', 'read-scope-changed']) window.addEventListener(name, lifecycle);
  const timer = window.setInterval(() => {
    scopeChanged();
    append('heartbeat', { authenticated: !!persistentScope() && persistentScope() === ownerScope,
      visible: document.visibilityState === 'visible', terminalVisible: [...document.querySelectorAll<HTMLElement>('.ty-terminal')].some(node => node.getClientRects().length > 0 && !node.closest('[hidden]')),
      lastFrameMonotonic, framePending: !!frame, readSignals, coalescedCaptureSignals, storageFailed: !!fault, dropped, queued, continuousTradingDayProof: false }); schedule();
  }, 5_000);
  button.onclick = async () => {
    const scope = persistentScope(); if (!scope || scope !== ownerScope) return;
    button.disabled = true;
    try {
      await writes;
      let rows = fallback;
      if (database) rows = await new Promise<RecordRow[]>((resolve, reject) => {
        const request = database!.transaction('records', 'readonly').objectStore('records').index('run').getAll(run);
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject(new Error('local_record_export_failed'));
      });
      if (persistentScope() !== scope) return;
      const own = rows.filter(row => row.scope === scope).map(({ scope: _scope, ...row }) => row);
      const header = { type: 'local_acceptance_export', schema: 1, run, exportedAt: new Date().toISOString(), ...checkerIdentity, historicalProofBaselineSha256,
        storedRows: own.length, exportedRows: own.length, recordingFailed: !!fault,
        storageFailed: !!fault && fault !== 'authentication_scope_changed', failureCode: fault || null, dropped,
        rawValuesStored: false, dataHashBasis: 'JSON.stringify of parsed same-tab cached data; not the raw HTTP response body',
        frameTimingBasis: 'Client two-animation-frame observation; no server commit bound',
        readSignals, coalescedCaptureSignals, rapidUiUpdatesMayCoalesce: true,
        commitToPaintTimingKnown: false, continuousTradingDayProof: false };
      const blob = new Blob([JSON.stringify(header) + '\n', ...own.map(row => JSON.stringify(row) + '\n')], { type: 'application/x-ndjson' });
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url; link.download = `天衍-本机验收-${run}.jsonl`; link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
    } catch { fail('local_record_export_failed'); }
    finally { button.disabled = false; }
  };
  try {
    await initialization;
    if (count >= LIMIT) fail('record_limit_reached');
  } catch { fail('local_record_unavailable'); }
  scopeChanged(); status(); schedule();
  return () => {
    append('recorder_stopped', { storageFailed: !!fault, dropped }); observer.disconnect(); unsubscribe(); window.clearInterval(timer);
    if (frame) cancelAnimationFrame(frame); document.removeEventListener('visibilitychange', visibility);
    document.removeEventListener('input', controlsChanged); document.removeEventListener('change', controlsChanged);
    window.removeEventListener('error', pageError); window.removeEventListener('unhandledrejection', pageError);
    for (const name of ['session-changed', 'session-expired', 'read-permission-denied', 'read-scope-changed']) window.removeEventListener(name, lifecycle);
    button.remove(); installed = false;
  };
}
