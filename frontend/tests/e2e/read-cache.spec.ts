import { expect } from '@playwright/test';
import { test } from './isolated-browser.fixtures';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

// Execute the real cache module without a browser or an authenticated server.
async function cache(clock?: { setTimeout: (run: () => void, delay: number) => number; clearTimeout: (id: number) => void }) {
  const events = new EventTarget();
  Object.assign(events, clock ?? { setTimeout, clearTimeout });
  Object.assign(globalThis, { window: events, location: { origin: 'http://127.0.0.1:5178' } });
  const source = await readFile('src/shared/readCache.ts', 'utf8');
  const storage = ts.transpileModule(await readFile('src/shared/persistentReads.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
  const storageUrl = 'data:text/javascript;base64,' + Buffer.from(storage + '\n// ' + crypto.randomUUID()).toString('base64');
  const output = ts.transpileModule(source.replace("'./persistentReads'", JSON.stringify(storageUrl)), { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
  return import('data:text/javascript;base64,' + Buffer.from(output + '\n// ' + crypto.randomUUID()).toString('base64'));
}

test('watchlist writes expire every candidate view but retain independent review results', async () => {
  const c = await cache();
  const paths = ['/api/v1/watchlist', '/api/v1/workbench-view?view=overview', '/api/v1/workbench-view?view=auction', '/api/v1/review-data/series?lookback=20'];
  for (const path of paths) await c.cachedRead(path, async () => ({ value: { marker: path } }));
  c.invalidateMutation('/api/v1/watchlist');
  for (const path of paths.slice(0, 3)) expect(c.peekRead(path)).toBeUndefined();
  expect(c.peekRead(paths[3])).toEqual({ marker: paths[3] });
});

test('a publication invalidates only its changed representation and retains the prior value', async () => {
  const c = await cache();
  const first = '/api/v1/workbench-view?view=overview', second = '/api/v1/workbench-view?view=funds';
  await c.cachedRead(first, async () => ({ value: { marker: 'overview' }, etag: '"p1"' }));
  await c.cachedRead(second, async () => ({ value: { marker: 'funds' }, etag: '"f1"' }));
  expect(await c.acceptPublications([{ path: first, revision: 'p1' }, { path: second, revision: 'f2' }])).toEqual([second]);
  expect(c.peekRead(first)).toEqual({ marker: 'overview' }); expect(c.peekRead(second)).toBeUndefined();
  expect(c.peekRead(second, true)).toEqual({ marker: 'funds' });
});

for (const received of ['current', 'older']) test(`publication during initial GET waits and detects its ${received} version`, async () => {
  const c = await cache();
  const path = '/api/v1/workbench-view?view=overview';
  let finish!: (value: { value: unknown; etag: string }) => void;
  let aborted = false;
  const initial = c.cachedRead(path, signal => { signal.addEventListener('abort', () => { aborted = true; }); return new Promise(resolve => { finish = resolve; }); });
  const publication = c.acceptPublications([{ path, revision: 'published' }]);
  finish({ value: { marker: 'initial' }, etag: received === 'current' ? '"published"' : '"old"' });
  await initial;
  expect(await publication).toEqual(received === 'current' ? [] : [path]);
  expect(aborted).toBe(false); expect(c.peekRead(path, true)).toEqual({ marker: 'initial' });
});

test('a publication awaiting a retired account cannot invalidate the next account', async () => {
  const c = await cache(), path = '/api/v1/workbench-view?view=overview';
  let finish!: (value: { value: unknown; etag: string }) => void;
  const initial = c.cachedRead(path, () => new Promise(resolve => { finish = resolve; }));
  const publication = c.acceptPublications([{ path, revision: 'old-published' }]);
  c.invalidateReads();
  await c.cachedRead(path, async () => ({ value: { marker: 'new-account' }, etag: '"new"' }));
  finish({ value: { marker: 'old-account' }, etag: '"old"' }); await initial;
  expect(await publication).toEqual([]); expect(c.peekRead(path)).toEqual({ marker: 'new-account' });
});

function fakeClock() {
  let now = 0, serial = 0;
  const jobs = new Map<number, { at: number; run: () => void }>();
  return { jobs,
    setTimeout: (run: () => void, delay: number) => { const id = ++serial; jobs.set(id, { at: now + delay, run }); return id; },
    clearTimeout: (id: number) => { jobs.delete(id); },
    nextDelay: () => Math.min(...[...jobs.values()].map(job => job.at - now)),
    next: async () => {
      const first = [...jobs].sort(([, a], [, b]) => a.at - b.at)[0]; if (!first) return;
      jobs.delete(first[0]); now = first[1].at; first[1].run();
      for (let i = 0; i < 12; i++) await Promise.resolve();
    },
  };
}

test('preload-only reads never poll and active refreshing reads continue at bounded intervals until ready', async () => {
  const clock = fakeClock(), c = await cache(clock);
  const path = '/api/v1/workbench-view?view=overview';
  let reads = 0;
  await c.cachedRead(path, async () => { reads++; return { value: { data: { marker: reads >= 8 ? 'new' : 'saved' } }, state: reads >= 8 ? 'fresh' : 'refreshing' }; });
  expect(reads).toBe(1); expect(clock.jobs.size).toBe(0);
  const release = c.retainReads([path]);
  for (const delay of [2000, 5000, 15000, 30000]) { expect(clock.nextDelay()).toBe(delay); await clock.next(); }
  expect(reads).toBe(5); expect(clock.jobs.size).toBe(1);
  expect(c.peekRead(path, true)).toEqual({ data: { marker: 'saved' } });
  expect(c.readStatus(path).error).toBeUndefined();
  for (let i = 0; i < 3; i++) { expect(clock.nextDelay()).toBe(30000); await clock.next(); }
  expect(reads).toBe(8); expect(c.peekRead(path, true)).toEqual({ data: { marker: 'new' } });
  expect(c.readStatus(path).error).toBeUndefined(); expect(clock.jobs.size).toBe(0); release();
});

test('the last reader leaving cancels continuation and independent readers retain their own lease', async () => {
  const clock = fakeClock(), c = await cache(clock), path = '/api/v1/news/items';
  const first = c.retainReads([path]), second = c.retainReads([path]);
  await c.cachedRead(path, async () => ({ value: { data: { items: [] } }, state: 'refreshing' }));
  expect(clock.jobs.size).toBe(1); first(); expect(clock.jobs.size).toBe(1); second(); expect(clock.jobs.size).toBe(0);
  await clock.next(); expect(clock.jobs.size).toBe(0);
});

test('a successful stale response preserves data and stops updating with an explicit retry message', async () => {
  const clock = fakeClock(), c = await cache(clock), path = '/api/v1/review-data/day';
  const release = c.retainReads([path]);
  await c.cachedRead(path, async () => ({ value: { data: { marker: 'authorized old result' } }, state: 'stale' }));
  expect(c.peekRead(path, true)).toEqual({ data: { marker: 'authorized old result' } });
  expect(c.readStatus(path).error).toContain('当前显示上次资料，可重试'); expect(clock.jobs.size).toBe(0); release();
});

test('canonical queries share reads and a response invalidated during a write cannot restore old data', async () => {
  const c = await cache();
  const key = '/api/v1/news/items?limit=30&category=all';
  let complete!: (value: unknown) => void;
  const old = c.cachedRead(key, () => new Promise(resolve => { complete = resolve; }));
  const same = c.cachedRead('/api/v1/news/items?category=all&limit=30', () => { throw new Error('duplicate network read'); });
  expect(old).toBe(same);
  c.invalidateMutation('/api/v1/news/items/one/state');
  await c.freshRead(() => c.cachedRead(key, async () => ({ value: { version: 2 } })));
  complete({ value: { version: 1 } }); await old;
  expect(c.peekRead(key)).toEqual({ version: 2 });
});

test('account expiry cancels reads and late results never refill the next account cache', async () => {
  const c = await cache();
  let signal!: AbortSignal; let complete!: (value: unknown) => void;
  const path = '/api/v1/review-data/day';
  const pending = c.cachedRead(path, (value: AbortSignal) => { signal = value; return new Promise(resolve => { complete = resolve; }); });
  window.dispatchEvent(new Event('session-expired'));
  expect(signal.aborted).toBe(true);
  complete({ value: { owner: 'old' } }); await pending;
  expect(c.peekRead(path)).toBeUndefined();
  await c.cachedRead(path, async () => ({ value: { owner: 'new' } }));
  expect(c.peekRead(path)).toEqual({ owner: 'new' });
});
