import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

let server;
let SectorRotation;
const day = '2026-11-13';
const stock = (rank) => ({ stock_code: `TEST${rank}`, name: `原始股票${rank}`, label: ['龙一', '龙二', '龙三', '龙四', '龙五'][rank - 1], rank: String(rank) });
const observed = (extra = {}) => ({ rows: [stock(1)], coverage: 'complete', archive_kind: 'intraday', received_at: `${day}T09:29:55.123456+08:00`, ...extra });

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../../', import.meta.url)),
    configFile: false,
    envDir: false,
    cacheDir: fileURLToPath(new URL('../../../output/classified-refresh-20261111.private/leader-evidence-test-cache', import.meta.url)),
    appType: 'custom',
    logLevel: 'silent',
    server: { middlewareMode: true, hmr: false, watch: null },
    plugins: [{
      name: 'isolated-sector-rotation-read',
      enforce: 'pre',
      resolveId(source, importer) {
        if (!importer?.replaceAll('\\', '/').endsWith('/SectorRotation.tsx')) return;
        if (source === '../../shared/useCachedLoader') return '\0leader-test:read';
        if (source === '../../shared/workspaceState') return '\0leader-test:state';
        if (source === '../../shared/api') return '\0leader-test:api';
      },
      load(id) {
        if (id === '\0leader-test:read') return 'export const useCachedLoader = () => globalThis.__leaderEvidenceRead;';
        if (id === '\0leader-test:state') return 'export const useWorkspaceState = (_, initial) => [initial, () => {}];';
        if (id === '\0leader-test:api') return 'export const request = () => { throw new Error("Static UI test must not request data"); };';
      },
    }],
  });
  ({ SectorRotation } = await server.ssrLoadModule('/src/features/daily-review/SectorRotation.tsx'));
});

after(async () => {
  delete globalThis.__leaderEvidenceRead;
  await server?.close();
});

function renderLeaders(leader) {
  const panel = {
    requested_day: day, trade_day: day, source_label: '开盘啦', archive_kind: 'intraday', status: 'ready',
    received_at: `${day}T15:04:51+08:00`,
    rows: [{ code: 'TEST_BOARD', name: '原始板块', rank: 1, strength: '100' }],
    leaders: leader === undefined ? {} : { TEST_BOARD: leader },
  };
  globalThis.__leaderEvidenceRead = { data: { data: { days: [panel], limitations: [] } }, busy: false, error: null, reload() {} };
  const html = renderToStaticMarkup(createElement(SectorRotation, { day, asOf: `${day}T15:05:00+08:00`, refreshEpoch: 0, ownerId: 'synthetic-leader-clock' }));
  const row = html.match(/<tr class="sr-leaders">(.*?)<\/tr>/s)?.[1];
  assert.ok(row, 'render the actual rotation table leader row');
  return row;
}

test('leader details show their own receipt, independently of the afternoon ranking', () => {
  const row = renderLeaders(observed());
  assert.equal(row.includes('接收09:29'), true, 'show the independently received leader time');
  assert.equal(row.includes('2026-11-13 09:29:55'), true, 'expose the full Shanghai receipt in the tooltip');
  assert.equal(row.includes('盘前接收'), true, 'identify this intraday observation as received before opening');
  assert.equal(row.includes('15:04'), false, 'never borrow the parent ranking receipt');
});

test('missing leader receipt remains unknown even when its parent has a receipt', () => {
  const row = renderLeaders(observed({ received_at: null }));
  assert.equal(row.includes('接收未知'), true);
  assert.equal(row.includes('15:04'), false);
});

test('a complete empty observation does not claim that the day has no leaders', () => {
  const row = renderLeaders(observed({ rows: [] }));
  assert.equal(row.includes('未返回标记'), true);
  assert.equal(row.includes('当日无领涨'), false);
  assert.equal(row.includes('接收09:29'), true);
});

test('an empty partial observation is distinct from never having collected it', () => {
  const row = renderLeaders(observed({ rows: [], coverage: 'returned_markers' }));
  assert.equal(row.includes('未返回标记'), true);
  assert.equal(row.includes('部分标记'), true);
  assert.equal(row.includes('未采集'), false);
});

test('partial markers retain native rows and expose partial coverage', () => {
  const row = renderLeaders(observed({ rows: [stock(1), stock(4)], coverage: 'returned_markers' }));
  assert.equal(row.includes('部分标记'), true);
  assert.equal(row.includes('龙一'), true);
  assert.equal(row.includes('龙四'), true);
  assert.equal(row.includes('龙二'), false, 'do not derive missing native labels');
});

test('no saved observation keeps the missing state without fabricating a receipt', () => {
  const row = renderLeaders(undefined);
  assert.equal(row.includes('未采集'), true);
  assert.equal(row.includes('接收未知'), false);
  assert.equal(row.includes('未返回标记'), false);
});

test('a receipt on another Shanghai date exposes that date', () => {
  const row = renderLeaders(observed({ received_at: '2026-11-13T16:10:00Z', archive_kind: 'daily' }));
  assert.equal(row.includes('接收11-14'), true);
  assert.equal(row.includes('2026-11-14 00:10:00'), true);
  assert.equal(row.includes('日终归档'), true);
  assert.equal(row.includes('盘前接收'), false, 'a daily archive is not a pre-opening intraday observation');
});

test('UTC and explicit Shanghai timestamps display the same receipt', () => {
  const utc = renderLeaders(observed({ received_at: '2026-11-13T01:29:55Z' }));
  const shanghai = renderLeaders(observed({ received_at: '2026-11-13T09:29:55+08:00' }));
  assert.equal(utc.includes('接收09:29'), true);
  assert.equal(shanghai.includes('接收09:29'), true);
});

test('invalid, impossible and timezone-free receipts stay unknown', () => {
  for (const received_at of ['invalid', '2026-11-13T09:29:55', '2026-02-30T09:29:55+08:00', '2026-11-13T25:01:00+08:00', '2026-11-13T09:29:55+25:00']) {
    const row = renderLeaders(observed({ received_at }));
    assert.equal(row.includes('接收未知'), true, `reject ambiguous receipt ${received_at}`);
    assert.equal(row.includes('15:04'), false);
  }
});

test('all five native labels and their source order survive the metadata display', () => {
  const rows = [5, 1, 3, 2, 4].map(stock);
  const row = renderLeaders(observed({ rows }));
  let previous = -1;
  for (const item of rows) {
    const position = row.indexOf(item.name);
    assert.ok(position > previous, 'preserve the native row order');
    assert.equal(row.includes(item.label), true);
    previous = position;
  }
  assert.equal(row.includes('部分标记'), false);
  assert.equal(row.includes('接收09:29'), true);
});
