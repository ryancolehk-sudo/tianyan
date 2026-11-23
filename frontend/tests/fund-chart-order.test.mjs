import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

// Render the real chart without launching a browser or requesting market data.
// Imported read-cache code registers session events at module load; rendering
// does not use browser storage, network, or effects.
globalThis.window = new EventTarget();
const cacheDir = await mkdtemp(join(tmpdir(), 'tianyan-fund-chart-'));
const server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)),
  cacheDir, configFile: false, server: { middlewareMode: true, hmr: false, watch: null } });
after(async () => { await server.close(); await rm(cacheDir, { recursive: true, force: true }); });
const { FundsChart } = await server.ssrLoadModule('/src/features/terminal/Charts.tsx');
function curve(times, values = times.map((_, i) => i + 1)) {
  const markup = renderToStaticMarkup(createElement(FundsChart, {
    sectors: [{code:'SYN-A',name:'synthetic'}], selected:'SYN-A', history:[], intraday:true,
    intradayPoints: times.map((time,i)=>({time,sector_code:'SYN-A',value:values[i]})),
  }));
  const group = markup.match(/<g[^>]*data-sector-code="SYN-A"[^>]*>([\s\S]*?)<\/g>/)?.[1];
  assert.ok(group, 'the actual chart renders the supplied board');
  return { path: group.match(/<path d="([^"]*)"/)?.[1] ?? '',
    xs: [...group.matchAll(/class="ty-fund-sample"[^>]*cx="([^"]+)"/g)].map(m=>Number(m[1])) };
}

test('mixed timezone encodings retain chronological progression and one connected segment', () => {
  const result = curve(['2026-11-03T02:03:00Z','2026-11-03T10:00:00+08:00','2026-11-03T02:06:00Z']);
  assert.deepEqual(result.xs, [...result.xs].sort((a,b)=>a-b));
  assert.equal((result.path.match(/M/g) ?? []).length, 1);
  assert.equal((result.path.match(/L/g) ?? []).length, 2);
});

test('equivalent all-UTC and mixed-offset observations produce the same path', () => {
  const utc = curve(['2026-11-03T02:00:00Z','2026-11-03T02:03:00Z','2026-11-03T02:06:00Z']);
  const mixed = curve(['2026-11-03T10:00:00+08:00','2026-11-03T02:03:00Z','2026-11-03T10:06:00+08:00']);
  assert.equal(mixed.path, utc.path);
});

test('a real trading-time gap is not filled by chronological sorting', () => {
  const result = curve(['2026-11-03T10:00:00+08:00','2026-11-03T02:10:00Z']);
  assert.equal((result.path.match(/M/g) ?? []).length, 2);
  assert.equal((result.path.match(/L/g) ?? []).length, 0);
});

test('lunch compression preserves connection without fabricating observations', () => {
  const result = curve(['2026-11-03T11:30:00+08:00','2026-11-03T05:01:00Z']);
  assert.equal(result.xs.length, 2);
  assert.equal((result.path.match(/M/g) ?? []).length, 1);
  assert.equal((result.path.match(/L/g) ?? []).length, 1);
});

test('null samples continue to break the path', () => {
  const result = curve(['2026-11-03T10:00:00+08:00','2026-11-03T02:03:00Z','2026-11-03T10:06:00+08:00'], [1,null,2]);
  assert.equal((result.path.match(/M/g) ?? []).length, 2);
  assert.equal((result.path.match(/L/g) ?? []).length, 0);
});
