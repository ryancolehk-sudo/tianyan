import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

// Deterministic synthetic transport. Deliberately deliver retired reads late;
// no browser, database, market service, persistent storage or real HTTP is used.
type Read = {
  codes: string[]; sector: string; active: boolean; signal: AbortSignal; settled: boolean;
  query: URLSearchParams;
  resolve: (value: unknown) => void; reject: (reason: unknown) => void;
};

async function harness(observationCutoff = '', asOf = '') {
  let now = 0, serial = 0, peak = 0;
  const timers = new Map<number, { at: number; fn: () => void }>();
  const reads: Read[] = [], invalidations: string[] = [], persisted: unknown[] = [];
  const window = new EventTarget(), document = Object.assign(new EventTarget(), { visibilityState: 'visible' });
  const navigator = { onLine: true };
  const setTimeout = (fn: () => void, delay = 0) => { const id = ++serial; timers.set(id, { at: now + delay, fn }); return id; };
  const clearTimeout = (id?: number) => { if (id != null) timers.delete(id); };
  const environment = { window, document, navigator, performance: { now: () => now }, setTimeout, clearTimeout,
    invalidations, persisted,
    request: (path: string, signal: AbortSignal, active: boolean) => new Promise((resolve, reject) => {
      const query = new URL(path, 'http://synthetic.invalid').searchParams;
      reads.push({ query, codes: (query.get('codes') ?? '').split(',').filter(Boolean), sector: query.get('sector') ?? '',
        active: !!active, signal, settled: false, resolve, reject });
      // Aborted transport may still finish or fail later. The store must ignore it.
      peak = Math.max(peak, reads.filter(read => !read.settled && !read.signal.aborted).length);
    }),
  };
  Object.assign(globalThis, { __detailPriorityTest: environment });
  const module = (text: string) => 'data:text/javascript;base64,' + Buffer.from(text + '\n// ' + crypto.randomUUID()).toString('base64');
  const api = module(`const env = globalThis.__detailPriorityTest;
    export class ApiError extends Error { constructor(status, code) { super('Synthetic transport failure'); this.status = status; this.code = code; } }
    export const cancellableRead = (path, signal, active) => env.request(path, signal, active);`);
  const persistence = module(`const env = globalThis.__detailPriorityTest;
    export const persistentGetMany = async () => new Map();
    export const persistentPutMany = items => env.persisted.push(...items);`);
  const cache = module(`const env = globalThis.__detailPriorityTest;
    export const invalidateReads = path => env.invalidations.push(path);
    export const readKey = path => { const url = new URL(path, 'http://synthetic.invalid'); url.searchParams.sort(); return url.pathname + '?' + url.searchParams; };`);
  const react = module('export const useEffect = () => {}; export const useSyncExternalStore = () => 0;');
  const notifications = module('export const useReadNotification = () => {};');
  const source = (await readFile('src/features/terminal/details.ts', 'utf8'))
    .replace("'react'", JSON.stringify(react))
    .replace("'../../shared/api'", JSON.stringify(api))
    .replace("'../../shared/persistentReads'", JSON.stringify(persistence))
    .replace("'../../shared/BackgroundRefresh'", JSON.stringify(notifications))
    .replace("'../../shared/readCache'", JSON.stringify(cache));
  const binding = `const { window, document, navigator, performance, setTimeout, clearTimeout } = globalThis.__detailPriorityTest;\n`;
  const exports = `\nexport function createTestStore(day: string, revision: string, cutoff = '', asOf = '') {
    const store = storeFor({ trade_day: day, detail_revision: revision, detail_observation_cutoff: cutoff } as WorkbenchData, asOf)!;
    if (active !== store) active?.deactivate(); active = store; return store;
  }\nexport const retireTestStores = clear;`;
  const output = ts.transpileModule(binding + source + exports, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
  } }).outputText;
  const { createTestStore, retireTestStores, prepareTerminalDetails } = await import(module(output));
  const { ApiError } = await import(api);
  const day = '2026-10-30', revision = 'synthetic-detail-r1';
  const store = createTestStore(day, revision, observationCutoff, asOf);
  const drain = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
  const respond = (read: Read, name = 'synthetic current', detailRevision = revision) => {
    read.settled = true; read.resolve({ data: { trade_day: day, revision: detailRevision,
      stocks: read.codes.map(code => ({ code, name })), auction_days: [], events: [], seals: {},
      sector_code: read.sector || null, members: [], members_trade_day: day, funds_history: [],
      tracking_members: [], tracking_member_count: 0, tracking_members_truncated: false } });
  };
  const fail = (read: Read, status = 503, code?: string) => { read.settled = true; read.reject(new ApiError(status, code)); };
  const advance = async (milliseconds: number) => {
    const target = now + milliseconds;
    await drain();
    for (let n = 0; ; n++) {
      const next = [...timers].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      if (n > 100) throw Error('Synthetic timer storm');
      now = next[1].at; timers.delete(next[0]); next[1].fn(); await drain();
    }
    now = target; await drain();
  };
  await drain();
  return { store, reads, window, navigator, invalidations, persisted, respond, fail, advance, drain,
    peak: () => peak, createStore: (value: string, cutoff = observationCutoff) => createTestStore(day, value, cutoff, asOf),
    prepare: (value: string) => prepareTerminalDetails({ trade_day: day, detail_revision: value,
      detail_observation_cutoff: observationCutoff, stocks: [], sectors: [] }, asOf),
    dispose: async () => { retireTestStores(); reads.filter(read => !read.settled).forEach(read => fail(read)); await drain(); timers.clear(); },
  };
}

const codes = Array.from({ length: 24 }, (_, i) => `${String(i + 1).padStart(6, '0')}.SZ`);

test('selected and background reads retain the exact parent cutoff including microseconds', async () => {
  const cutoff = '2026-10-30T10:30:00.123456+08:00', h = await harness(cutoff);
  try {
    h.store.enqueue(codes); await h.advance(500);
    expect(h.reads[0].query.get('observation_cutoff')).toBe(cutoff);
    h.store.select(codes[7], ''); await h.drain();
    expect(h.reads[1].query.get('observation_cutoff')).toBe(cutoff);
    expect(h.reads[1].query.has('as_of_time')).toBe(false);
    h.respond(h.reads[1]); await h.drain(); await h.advance(500);
    expect(h.reads[2].query.get('observation_cutoff')).toBe(cutoff);
    expect(h.invalidations).toEqual([]);
  } finally { await h.dispose(); }
});

test('publication of a pinned selected read wakes its retry without a timer or parent reload', async () => {
  const cutoff = '2026-10-30T10:30:00.123456+08:00', h = await harness(cutoff);
  try {
    h.store.select(codes[0], ''); await h.drain();
    h.fail(h.reads[0], 503); await h.drain();
    const query = new URLSearchParams({ observation_cutoff: cutoff, codes: codes[0], day: '2026-10-30' });
    h.store.published(['/api/v1/workbench-details?' + query]); await h.drain();
    expect(h.reads).toHaveLength(2);
    expect(h.reads[1].query.get('observation_cutoff')).toBe(cutoff);
    h.respond(h.reads[1]); await h.drain();
    expect(h.store.stocks.has(codes[0])).toBe(true);
    expect(h.invalidations).toEqual([]);
  } finally { await h.dispose(); }
});

test('identical canonical data revisions share a prepared detail context across parent cutoffs', async () => {
  const cutoff = '2026-10-30T10:30:00.123456+08:00', h = await harness(cutoff);
  try {
    h.store.select(codes[0], ''); await h.drain(); h.respond(h.reads[0]); await h.drain();
    const same = h.createStore('synthetic-detail-r1', '2026-10-30T10:30:00.223456+08:00');
    same.select(codes[0], ''); await h.drain();
    expect(same).toBe(h.store); expect(h.reads).toHaveLength(1);
  } finally { await h.dispose(); }
});

test('a legacy unpinned context is retired when its parent begins supplying a cutoff', async () => {
  const h = await harness();
  try {
    h.store.select(codes[0], ''); await h.drain(); const legacy = h.reads[0];
    const next = h.createStore('synthetic-detail-r1', '2026-10-30T10:30:00.123456+08:00');
    next.select(codes[0], ''); await h.drain();
    expect(legacy.signal.aborted).toBe(true); expect(next).not.toBe(h.store);
    expect(h.reads[1].query.has('observation_cutoff')).toBe(true);
    h.respond(legacy, 'synthetic retired'); h.respond(h.reads[1], 'synthetic pinned'); await h.drain();
    expect(next.stocks.get(codes[0]).stock.name).toBe('synthetic pinned');
  } finally { await h.dispose(); }
});

test('explicit historical replay keeps its original cutoff semantics', async () => {
  const asOf = '2026-10-30T10:30:00.123456+08:00', h = await harness(asOf, asOf);
  try {
    h.store.select(codes[0], ''); await h.drain();
    expect(h.reads[0].query.get('as_of_time')).toBe(asOf);
    expect(h.reads[0].query.has('observation_cutoff')).toBe(false);
  } finally { await h.dispose(); }
});

test('background preparation cannot evict the visible pinned detail context', async () => {
  const h = await harness('2026-10-30T10:30:00.123456+08:00');
  try {
    h.store.select(codes[0], ''); await h.drain(); h.respond(h.reads[0]); await h.drain();
    h.prepare('synthetic-prepared-a'); h.prepare('synthetic-prepared-b'); await h.drain();
    expect(h.store.stopped).toBe(false);
    expect(h.store.stocks.has(codes[0])).toBe(true);
    expect(h.createStore('synthetic-detail-r1')).toBe(h.store);
    expect(h.reads).toHaveLength(1);
  } finally { await h.dispose(); }
});

test('a selected stock overtakes its pending background batch and the remaining stocks resume', async () => {
  const h = await harness();
  try {
    h.store.enqueue(codes); await h.advance(500);
    expect(h.reads).toHaveLength(1); const background = h.reads[0];
    expect(background.codes).toEqual(codes); expect(background.active).toBe(false);
    h.store.select(codes[7], ''); await h.drain();
    expect(background.signal.aborted).toBe(true);
    expect(h.reads).toHaveLength(2); const foreground = h.reads[1];
    expect(foreground.codes).toEqual([codes[7]]); expect(foreground.active).toBe(true);
    expect(h.peak()).toBe(1);
    h.respond(foreground); await h.drain();
    await h.advance(500);
    expect(h.reads).toHaveLength(3);
    expect(h.reads[2].codes).toEqual(codes.filter(code => code !== codes[7]));
    expect(h.reads[2].active).toBe(false);
    h.respond(background, 'synthetic retired'); await h.drain();
    expect(h.store.stocks.get(codes[7]).stock.name).toBe('synthetic current');
    expect(h.store.stocks.has(codes[0])).toBe(false);
    h.respond(h.reads[2]); await h.drain(); await h.advance(1_000);
    expect(h.store.stocks.size).toBe(24); expect(h.reads).toHaveLength(3);
  } finally { await h.dispose(); }
});

for (const completion of ['response', 'failure']) test(`a retired batch's late ${completion} cannot remove or fail the new foreground read`, async () => {
  const h = await harness();
  try {
    h.store.enqueue(codes); await h.advance(500);
    const old = h.reads[0], selected = codes[5];
    h.store.select(selected, ''); await h.drain();
    const foreground = h.reads[1];
    if (completion === 'response') h.respond(old, 'synthetic retired', 'wrong-retired-revision');
    else h.fail(old);
    await h.drain();
    expect(h.store.failures.has(selected)).toBe(false); expect(h.invalidations).toEqual([]);
    h.store.select(selected, ''); await h.drain();
    expect(h.reads).toHaveLength(2); expect(foreground.signal.aborted).toBe(false);
    h.respond(foreground); await h.drain(); await h.advance(500);
    expect(h.reads).toHaveLength(3); expect(h.reads[2].codes).toHaveLength(23);
    h.respond(h.reads[2]); await h.drain();
    expect(h.store.stocks.get(selected).stock.name).toBe('synthetic current');
    expect(h.store.stocks.size).toBe(24); expect(h.peak()).toBe(1);
  } finally { await h.dispose(); }
});

test('a retired batch cannot clear its successor or start its deferred sector request', async () => {
  const h = await harness(), board = 'SYNTHETIC.SECTOR';
  try {
    h.store.enqueue(codes); h.store.enqueueBoards([board]); await h.advance(500);
    const old = h.reads[0];
    h.store.select(codes[4], ''); await h.drain();
    h.respond(h.reads[1]); await h.drain(); await h.advance(500);
    const successor = h.reads[2]; expect(successor.codes).toHaveLength(23);
    h.respond(old, 'synthetic retired'); await h.drain();
    h.store.enqueue(['999999.SZ']); await h.advance(1_000);
    expect(h.reads).toHaveLength(3); expect(successor.signal.aborted).toBe(false);
    h.respond(successor); await h.drain();
    expect(h.reads).toHaveLength(4); expect(h.reads[3].sector).toBe(board);
    expect(h.reads[3].active).toBe(false);
    h.respond(h.reads[3]); await h.drain(); await h.advance(500);
    expect(h.reads).toHaveLength(5); expect(h.reads[4].codes).toEqual(['999999.SZ']);
    h.respond(h.reads[4]); await h.drain(); await h.advance(1_000);
    expect(h.store.stocks.size).toBe(25); expect(h.store.sectors.has(board)).toBe(true);
    expect(h.reads).toHaveLength(5); expect(h.peak()).toBe(1);
  } finally { await h.dispose(); }
});

test('offline selection sends no HTTP and reconnect restores only the visible request first', async () => {
  const h = await harness();
  try {
    h.store.enqueue(codes); await h.advance(500);
    h.navigator.onLine = false; h.store.select(codes[6], ''); await h.drain();
    expect(h.reads[0].signal.aborted).toBe(true);
    await h.advance(60_000); expect(h.reads).toHaveLength(1);
    h.navigator.onLine = true; h.window.dispatchEvent(new Event('online')); await h.drain();
    expect(h.reads).toHaveLength(2); expect(h.reads[1].codes).toEqual([codes[6]]);
    expect(h.reads[1].active).toBe(true);
    h.respond(h.reads[1]); await h.drain(); await h.advance(500);
    expect(h.reads).toHaveLength(3); expect(h.reads[2].codes).toHaveLength(23);
    h.respond(h.reads[2]); await h.drain(); await h.advance(30_000);
    expect(h.reads).toHaveLength(3); expect(h.peak()).toBe(1);
  } finally { await h.dispose(); }
});

for (const [status, event] of [[401, 'session-expired'], [403, 'read-permission-denied']] as const)
  test(`${status} retirement stops promoted reads and ignores their late completions`, async () => {
    const h = await harness();
    try {
      h.store.enqueue(codes); await h.advance(500);
      h.store.select(codes[2], ''); await h.drain();
      h.window.dispatchEvent(new Event(event)); h.fail(h.reads[1], status); await h.drain();
      expect(h.reads[1].signal.aborted).toBe(true);
      h.respond(h.reads[0], 'synthetic retired'); await h.drain();
      h.window.dispatchEvent(new Event('online')); await h.advance(60_000);
      expect(h.reads).toHaveLength(2); expect(h.store.stocks.size).toBe(0);
      expect(h.persisted).toEqual([]);
    } finally { await h.dispose(); }
  });

test('rapid selections retain the foreground budget and the latest selection can continue', async () => {
  const h = await harness();
  try {
    h.store.enqueue(codes); await h.advance(500);
    h.store.select(codes[0], ''); await h.drain();
    for (let i = 0; i < 100; i++) h.store.select(codes[1 + i % 23], '');
    h.store.select(codes[23], ''); await h.drain(); await h.advance(1_000);
    expect(h.reads[1].signal.aborted).toBe(true);
    expect(h.reads).toHaveLength(3); expect(h.reads[2].codes).toEqual([codes[23]]);
    h.respond(h.reads[1], 'synthetic obsolete selection'); await h.drain();
    expect(h.store.stocks.has(codes[0])).toBe(false);
    await h.advance(1_000); expect(h.reads).toHaveLength(3);
    h.respond(h.reads[2]); await h.drain(); await h.advance(500);
    expect(h.reads).toHaveLength(4); expect(h.reads[3].codes).toHaveLength(23);
    h.respond(h.reads[3]); await h.drain(); await h.advance(30_000);
    expect(h.reads).toHaveLength(4); expect(h.peak()).toBe(1);
  } finally { await h.dispose(); }
});

test('the newest board bypasses an unfinished selection and cached returns issue no new request', async () => {
  const h = await harness(), first = 'SYNTHETIC-A', next = 'SYNTHETIC-B';
  try {
    h.store.select(codes[0], first); await h.drain();
    expect(h.reads[0].sector).toBe(first);
    h.store.select(codes[0], next); await h.drain();
    expect(h.reads[0].signal.aborted).toBe(true);
    expect(h.reads[1].sector).toBe(next);
    h.respond(h.reads[1]); await h.drain();
    expect(h.store.sectors.has(next)).toBe(true);
    expect(h.reads[2].codes).toEqual([codes[0]]);
    h.respond(h.reads[2]); h.respond(h.reads[0], 'synthetic obsolete board'); await h.drain();
    const count = h.reads.length;
    h.store.select(codes[0], first); await h.drain();
    h.respond(h.reads[count]); await h.drain();
    h.store.select(codes[0], next); await h.drain();
    expect(h.reads).toHaveLength(count + 1);
    expect(h.store.sectors.get(next)).toBeDefined();
    expect(h.peak()).toBe(1);
  } finally { await h.dispose(); }
});

test('a selected local projection is read promptly when its background build becomes ready', async () => {
  const h = await harness();
  try {
    h.store.select('', 'SYNTHETIC-A'); await h.drain();
    h.fail(h.reads[0], 503, 'read_cache_warming'); await h.drain();
    await h.advance(249); expect(h.reads).toHaveLength(1);
    await h.advance(1); expect(h.reads).toHaveLength(2);
    expect(h.reads[1].active).toBe(true);
    h.respond(h.reads[1]); await h.drain();
    expect(h.store.sectors.has('SYNTHETIC-A')).toBe(true);
    await h.advance(30_000); expect(h.reads).toHaveLength(2);
  } finally { await h.dispose(); }
});

test('hovering over a queued stock does not retire the background batch', async () => {
  const h = await harness();
  try {
    h.store.enqueue(codes); await h.advance(500);
    h.store.ensure(codes[8], ''); await h.drain();
    expect(h.reads).toHaveLength(1); expect(h.reads[0].signal.aborted).toBe(false);
  } finally { await h.dispose(); }
});

test('the promoted response must still match its parent revision', async () => {
  const h = await harness();
  try {
    h.store.enqueue(codes); await h.advance(500);
    h.store.select(codes[9], ''); await h.drain();
    h.respond(h.reads[1], 'synthetic newer', 'unmatched-parent'); await h.drain();
    expect(h.store.stocks.has(codes[9])).toBe(false);
    expect(h.invalidations).toEqual(['/api/v1/workbench-view']);
    expect(h.store.failures.has(codes[9])).toBe(true);
  } finally { await h.dispose(); }
});

test('a replacement detail context cannot receive a retired context response', async () => {
  const h = await harness();
  try {
    h.store.enqueue(codes); await h.advance(500);
    h.store.select(codes[3], ''); await h.drain();
    const replacement = h.createStore('synthetic-detail-r2');
    replacement.select(codes[3], ''); await h.drain();
    expect(h.reads).toHaveLength(3); expect(h.reads[1].signal.aborted).toBe(true);
    h.respond(h.reads[0], 'synthetic retired'); h.fail(h.reads[1]); await h.drain();
    replacement.select(codes[3], ''); await h.drain(); expect(h.reads).toHaveLength(3);
    h.respond(h.reads[2], 'synthetic replacement', 'synthetic-detail-r2'); await h.drain();
    expect(replacement.stocks.get(codes[3]).stock.name).toBe('synthetic replacement');
    expect(h.store.stocks.size).toBe(0); expect(h.invalidations).toEqual([]);
  } finally { await h.dispose(); }
});

test('reactivating a retired context resumes its reserved sector after the stocks', async () => {
  const h = await harness(), board = 'SYNTHETIC.SECTOR';
  try {
    h.store.enqueue(codes); h.store.enqueueBoards([board]); await h.advance(500);
    const old = h.reads[0];
    h.createStore('synthetic-detail-r2');
    const restored = h.createStore('synthetic-detail-r1');
    restored.resume(); await h.advance(500);
    expect(h.reads).toHaveLength(2); expect(h.reads[1].codes).toEqual(codes);
    h.respond(old, 'synthetic retired', 'wrong-retired-revision');
    h.respond(h.reads[1]); await h.drain();
    expect(h.reads).toHaveLength(3); expect(h.reads[2].sector).toBe(board);
    h.respond(h.reads[2]); await h.drain(); await h.advance(1_000);
    expect(restored.sectors.has(board)).toBe(true); expect(h.invalidations).toEqual([]);
    expect(h.reads).toHaveLength(3); expect(h.peak()).toBe(1);
  } finally { await h.dispose(); }
});

test('a selected sector can replace its pending speculative read without refetching completed stocks', async () => {
  const h = await harness(), board = 'SYNTHETIC.SECTOR';
  try {
    h.store.enqueue(codes); h.store.enqueueBoards([board]); await h.advance(500);
    h.respond(h.reads[0]); await h.drain();
    expect(h.reads).toHaveLength(2); const background = h.reads[1];
    expect(background.sector).toBe(board); expect(background.active).toBe(false);
    h.store.select(codes[0], board); await h.drain();
    expect(background.signal.aborted).toBe(true); expect(h.reads).toHaveLength(3);
    expect(h.reads[2].codes).toEqual([]); expect(h.reads[2].sector).toBe(board);
    expect(h.reads[2].active).toBe(true);
    h.fail(background); await h.drain(); expect(h.store.failures.has(board)).toBe(false);
    h.respond(h.reads[2]); await h.drain(); await h.advance(1_000);
    expect(h.store.stocks.size).toBe(24); expect(h.store.sectors.has(board)).toBe(true);
    expect(h.reads).toHaveLength(3); expect(h.peak()).toBe(1);
  } finally { await h.dispose(); }
});
