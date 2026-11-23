import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

// Run the real update stream, shared cache and loader together. Only their host
// clock/hooks, persistence and network transport are synthetic; no server opens.
async function harness(restored = false) {
  const originalDate = globalThis.Date;
  const prior = { window: globalThis.window, document: globalThis.document, location: globalThis.location,
    EventSource: globalThis.EventSource, clearInterval: globalThis.clearInterval };
  const epoch = originalDate.parse('2026-11-10T01:34:56Z');
  let now = epoch, serial = 0, scope: string | null = 'account-one', version = 'current';
  const jobs = new Map<number, { at: number; interval?: number; run: () => void }>();
  const cleanups: Array<() => void> = [], states: unknown[] = [];
  const calls: Array<{ at: number; version: string }> = [];
  const timer = (run: () => void, delay: number, interval?: number) => {
    const id = ++serial; jobs.set(id, { at: now + delay, interval, run }); return id;
  };
  const clear = (id: number) => { jobs.delete(id); };
  const events = Object.assign(new EventTarget(), {
    setTimeout: (run: () => void, delay: number) => timer(run, delay), clearTimeout: clear,
    setInterval: (run: () => void, delay: number) => timer(run, delay, delay), clearInterval: clear,
  });
  const doc = Object.assign(new EventTarget(), { visibilityState: 'visible' });
  class ClockDate extends originalDate {
    constructor(value?: string | number) { super(value === undefined ? now : value); }
    static now() { return now; }
  }
  class Stream extends EventTarget {
    static instances: Stream[] = [];
    onerror?: () => void;
    constructor(_url: string) { super(); Stream.instances.push(this); }
    close() {}
  }
  const envelope = (marker: string) => ({ data: { marker, trade_day: '2026-11-10',
    stocks: [], sectors: [], indices: [], auction_days: [], selected_members: [], funds_history: [],
    broken: [], events: [], stock_seals: [], capabilities: [], breadth: { up: null } } });
  const path = '/api/v1/workbench-view?view=overview';
  const storage = {
    persistentScope: () => scope, setPersistentScope: (value: string | null) => { scope = value; },
    purgePersistentScope: () => Promise.resolve(true), persistentPutMany: () => {},
    persistentGetMany: async (keys: string[], valid: (value: unknown, key: string) => boolean) => {
      const row = { value: envelope('saved-empty'), checked: epoch - 1_000, expires: 0, etag: '"saved"', state: 'fresh' };
      return new Map(restored && valid(row, keys[0]) ? [[keys[0], row]] : []);
    },
  };
  class ApiError extends Error { constructor(public status: number, message: string, _request?: string, public code?: string) { super(message); } }
  Object.assign(globalThis, { window: events, document: doc, location: { origin: 'http://synthetic.invalid' }, EventSource: Stream,
    Date: ClockDate, clearInterval: (id: number | ReturnType<typeof setInterval>) => {
      if (typeof id === 'number') clear(id); else prior.clearInterval(id);
    }, __pollingTest: { storage, ApiError,
      effect: (run: () => void | (() => void)) => { const cleanup = run(); if (cleanup) cleanups.push(cleanup); },
      ref: (value: unknown) => ({ current: value }), callback: (value: unknown) => value,
      state: (initial: unknown) => {
        const index = states.length; states.push(initial);
        return [initial, (next: unknown) => { states[index] = typeof next === 'function' ? next(states[index]) : next; }];
      },
    } });
  const module = (source: string) => 'data:text/javascript;base64,' + Buffer.from(source + '\n// ' + crypto.randomUUID()).toString('base64');
  const transpile = (source: string) => module(ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText);
  const persistence = module('export const { persistentScope, setPersistentScope, purgePersistentScope, persistentPutMany, persistentGetMany } = globalThis.__pollingTest.storage;');
  const react = module('export const { effect: useEffect, ref: useRef, callback: useCallback, state: useState } = globalThis.__pollingTest;');
  const cacheUrl = transpile((await readFile('src/shared/readCache.ts', 'utf8')).replace("'./persistentReads'", JSON.stringify(persistence)));
  const cache = await import(cacheUrl);
  const loaderUrl = transpile((await readFile('src/shared/useCachedLoader.ts', 'utf8'))
    .replace("'react'", JSON.stringify(react)).replace("'./readCache'", JSON.stringify(cacheUrl))
    .replace("'./api'", JSON.stringify(module('export const ApiError = globalThis.__pollingTest.ApiError;')))
    .replace("'./BackgroundRefresh'", JSON.stringify(module('export function useReadNotification() {}'))));
  const updatesUrl = transpile((await readFile('src/shared/workbenchUpdates.ts', 'utf8'))
    .replace("'react'", JSON.stringify(react)).replace("'./persistentReads'", JSON.stringify(persistence))
    .replace("'./readCache'", JSON.stringify(cacheUrl)));
  const { useCachedLoader } = await import(loaderUrl), { useWorkbenchUpdates } = await import(updatesUrl);
  const load = () => cache.cachedRead(path, () => {
    const received = version; calls.push({ at: now - epoch, version: received });
    return new Promise(resolve => timer(() => resolve({ value: envelope(received), etag: '"' + received + '"', state: 'fresh' }), 6_000));
  });
  useCachedLoader(load, 'account-one:overview');
  useWorkbenchUpdates('account-one', false);
  async function flush() { for (let i = 0; i < 24; i++) await Promise.resolve(); }
  await flush();
  return {
    path, cache, calls, events, doc,
    snapshot: () => states[0] as { data?: ReturnType<typeof envelope>; busy: boolean; error: ApiError | null },
    version: (value: string) => { version = value; },
    publish: (revision: string) => Stream.instances[0].dispatchEvent(new MessageEvent('update', { data: JSON.stringify({ scope, revision, publications: [{ path, revision }] }) })),
    advance: async (duration: number) => {
      await flush();
      const target = now + duration;
      while (true) {
        const next = [...jobs].filter(([, job]) => job.at <= target).sort(([, a], [, b]) => a.at - b.at)[0];
        if (!next) break;
        const [id, job] = next; now = job.at;
        if (job.interval) jobs.set(id, { ...job, at: now + job.interval }); else jobs.delete(id);
        job.run(); await flush();
      }
      now = target; await flush();
    },
    stop: () => {
      cleanups.reverse().forEach(cleanup => cleanup()); jobs.clear();
      Object.assign(globalThis, { ...prior, Date: originalDate });
      delete (globalThis as { __pollingTest?: unknown }).__pollingTest;
    },
  };
}

for (const restored of [false, true]) test(`five-second fallback accepts a six-second GET instead of starving ${restored ? 'a restored empty snapshot' : 'the initial visible view'}`, async () => {
  const h = await harness(restored);
  try {
    if (restored) expect(h.snapshot().data?.data.marker).toBe('saved-empty');
    await h.advance(6_100);
    expect(h.snapshot().data?.data.marker).toBe('current');
    expect(h.cache.peekRead(h.path, true)?.data.marker).toBe('current');
    expect(h.calls).toEqual([{ at: 0, version: 'current' }]);
    await h.advance(24_000);
    expect(h.snapshot().data?.data.marker).toBe('current');
    expect(h.calls.length).toBeLessThanOrEqual(4);
  } finally { h.stop(); }
});

for (const received of ['current', 'older']) for (const coalesced of [false, true]) test(`an exact publication ${coalesced ? 'coalesced with fallback' : 'during the six-second GET'} still checks its ${received} ETag`, async () => {
  const h = await harness();
  try {
    const at = coalesced ? 5_000 : 1_000;
    await h.advance(at);
    const published = received === 'current' ? 'current' : 'new-revision';
    h.version(published); h.publish(published);
    await h.advance(6_100 - at);
    expect(h.calls.length).toBe(received === 'current' ? 1 : 2);
    await h.advance(6_000);
    expect(h.snapshot().data?.data.marker).toBe(published);
    expect(h.cache.peekRead(h.path, true)?.data.marker).toBe(published);
  } finally { h.stop(); }
});

test('permission revocation clears the slow read and late completion cannot restore or restart it', async () => {
  const h = await harness(true);
  try {
    await h.advance(1_000);
    h.events.dispatchEvent(new Event('read-permission-denied'));
    await h.advance(30_000);
    expect(h.snapshot().data).toBeUndefined();
    expect(h.snapshot().error?.status).toBe(403);
    expect(h.cache.peekRead(h.path, true)).toBeUndefined();
    expect(h.calls).toHaveLength(1);
  } finally { h.stop(); }
});

test('a hidden view pauses polling and visibility resumes a six-second validator', async () => {
  const h = await harness();
  try {
    await h.advance(6_100);
    h.doc.visibilityState = 'hidden'; h.version('after-resume');
    await h.advance(30_000);
    expect(h.snapshot().data?.data.marker).toBe('current');
    expect(h.calls).toHaveLength(1);
    h.doc.visibilityState = 'visible'; h.doc.dispatchEvent(new Event('visibilitychange'));
    await h.advance(6_100);
    expect(h.snapshot().data?.data.marker).toBe('after-resume');
    expect(h.calls).toHaveLength(2);
  } finally { h.stop(); }
});

test('explicit context invalidation still retires a six-second GET', async () => {
  const h = await harness();
  try {
    await h.advance(1_000);
    h.version('new-context'); h.cache.invalidateReads(h.path);
    h.events.dispatchEvent(new CustomEvent('read-cache-invalidated', { detail: [h.path] }));
    await h.advance(5_100);
    expect(h.cache.peekRead(h.path, true)).toBeUndefined();
    await h.advance(1_000);
    expect(h.snapshot().data?.data.marker).toBe('new-context');
    expect(h.calls).toHaveLength(2);
  } finally { h.stop(); }
});
