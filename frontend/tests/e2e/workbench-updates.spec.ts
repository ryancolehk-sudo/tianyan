import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

async function harness(publish?: (items: { path: string }[]) => Promise<string[]>) {
  let cleanup: (() => void) | undefined;
  const events = new EventTarget(), doc = new EventTarget();
  const timeouts = new Map<number, () => void>(), intervals = new Map<number, () => void>();
  const invalidations: string[] = [];
  let serial = 0, scope = 'account-one', wakes = 0;
  class Stream extends EventTarget {
    static instances: Stream[] = [];
    closed = false;
    onerror?: () => void;
    constructor(public url: string) { super(); Stream.instances.push(this); }
    close() { this.closed = true; }
    event(name: string, value: unknown) { this.dispatchEvent(new MessageEvent(name, { data: JSON.stringify(value) })); }
  }
  Object.assign(events, { setTimeout: (fn: () => void) => { const id = ++serial; timeouts.set(id, fn); return id; },
    clearTimeout: (id: number) => timeouts.delete(id), setInterval: (fn: () => void) => { const id = ++serial; intervals.set(id, fn); return id; },
    clearInterval: (id: number) => intervals.delete(id) });
  Object.assign(doc, { visibilityState: 'visible' });
  events.addEventListener('read-cache-invalidated', () => wakes++);
  Object.assign(globalThis, { window: events, document: doc, EventSource: Stream,
    __updateTest: { effect: (fn: () => () => void) => { cleanup = fn(); }, scope: () => scope, invalidate: (path: string) => invalidations.push(path),
      publications: async (items: { path: string }[]) => { if (publish) return publish(items); const paths = items.map(item => item.path); invalidations.push(...paths); return paths; } } });
  const module = (text: string) => 'data:text/javascript;base64,' + Buffer.from(text + '\n// ' + crypto.randomUUID()).toString('base64');
  const source = (await readFile('src/shared/workbenchUpdates.ts', 'utf8'))
    .replace("'react'", JSON.stringify(module('export const useEffect = globalThis.__updateTest.effect;')))
    .replace("'./persistentReads'", JSON.stringify(module('export const persistentScope = globalThis.__updateTest.scope;')))
    .replace("'./readCache'", JSON.stringify(module('export const expireReads = globalThis.__updateTest.invalidate; export const acceptPublications = globalThis.__updateTest.publications;')));
  const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
  const { useWorkbenchUpdates } = await import(module(output+'\n// '+crypto.randomUUID()));
  return { start: (historical = false) => useWorkbenchUpdates('owner', historical), streams: Stream.instances,
    invalidations, events, timeouts, intervals, stop: () => cleanup?.(), wakes: () => wakes,
    scope: (value: string) => { scope = value; }, flush: () => { const jobs = [...timeouts.values()]; timeouts.clear(); return Promise.all(jobs.map(fn => fn())); } };
}

test('publication invalidates real reads, coalesces duplicate events and cleans up', async () => {
  const h = await harness(); h.start();
  expect(h.streams).toHaveLength(1);
  h.streams[0].event('update', { scope: 'account-one', revision: 'r1' });
  h.streams[0].event('update', { scope: 'account-one', revision: 'r1' });
  h.flush();
  expect(h.invalidations).toEqual(['/api/v1/workbench-view', '/api/v1/workbench-details']);
  expect(h.wakes()).toBe(1);
  h.stop(); expect(h.streams[0].closed).toBe(true); expect(h.intervals.size).toBe(0);
});

test('scope changes and revoked permissions stop events and reconnects', async () => {
  const h = await harness(); h.start();
  h.streams[0].event('update', { scope: 'other-account', revision: 'private' }); h.flush();
  expect(h.wakes()).toBe(0);
  h.streams[0].event('permission-denied', { status: 403 });
  h.streams[0].event('update', { scope: 'account-one', revision: 'late' });
  h.events.dispatchEvent(new Event('online')); h.flush();
  expect(h.wakes()).toBe(0); expect(h.streams).toHaveLength(1); expect(h.streams[0].closed).toBe(true);
  h.stop();
});

test('disconnect retries once and conditional reads continue during recovery', async () => {
  const h = await harness(); h.start();
  h.streams[0].onerror?.(); expect(h.streams[0].closed).toBe(true); h.flush();
  expect(h.streams).toHaveLength(2);
  [...h.intervals.values()][0](); h.flush(); expect(h.wakes()).toBe(1);
  h.stop();
});

test('historical replay creates no live subscription or timer', async () => {
  const h = await harness(); h.start(true);
  expect(h.streams).toHaveLength(0); expect(h.intervals.size).toBe(0); h.stop();
});

test('targeted publications coalesce affected paths without invalidating other pages', async () => {
  const h = await harness(); h.start();
  const parent = '/api/v1/workbench-view?view=funds', detail = '/api/v1/workbench-details?codes=000100.SZ&day=2026-10-29';
  h.streams[0].event('update', { scope: 'account-one', revision: 'r1', publications: [{ path: detail, revision: 'd1' }] });
  h.streams[0].event('update', { scope: 'account-one', revision: 'r2', publications: [{ path: parent, revision: 'p2' }, { path: detail, revision: 'd2' }] });
  h.flush(); await Promise.resolve(); await Promise.resolve();
  expect(h.invalidations).toEqual([detail, parent]); expect(h.wakes()).toBe(1);
  h.stop();
});


for (const retirement of ['none', 'cleanup', 'account', 'permission']) test(`a slow unrelated publication does not block a ready page (${retirement})`, async () => {
  let release!: () => void;
  const slow = new Promise<void>(resolve => { release = resolve; });
  const current = '/api/v1/workbench-view?view=auction', other = '/api/v1/workbench-view?view=funds';
  const h = await harness(async items => {
    if (items.some(item => item.path === other)) await slow;
    return items.map(item => item.path);
  });
  const delivered: string[][] = [];
  h.events.addEventListener('read-cache-invalidated', event => delivered.push((event as CustomEvent<string[]>).detail));
  h.start(); h.streams[0].event('update', { scope: 'account-one', revision: 'r1', publications: [{ path: other, revision: 'f1' }, { path: current, revision: 'a1' }] });
  const completed = h.flush();
  await expect.poll(() => delivered).toEqual([[current]]);
  if (retirement === 'cleanup') h.stop();
  if (retirement === 'account') h.scope('account-two');
  if (retirement === 'permission') h.streams[0].event('permission-denied', { status: 403 });
  release(); await completed;
  expect(delivered).toEqual(retirement === 'none' ? [[current], [other]] : [[current]]);
  h.stop();
});
