import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

// Entirely synthetic browser session and cache responses. No credentials,
// supplier requests, real database writes or real market prices are involved.
const day = '2020-01-03';
const stock = '00000000-0000-4000-8000-000000000091';
const source = '00000000-0000-4000-8000-000000000092';
const user = '00000000-0000-4000-8000-000000000093';
const identity = { id: stock, exchange: 'SSE', symbol: '600001', asset_type: 'stock', name: '合成刷新股票（非真实行情）' };
const meta = { request_id: stock, app_mode: 'demo', generated_at: day + 'T02:05:00Z', as_of_time: null,
  quality: { status: 'partial', reasons: ['Synthetic browser refresh fixture only; not market observations'] } };
function bar(clock: string, close: string, revision = 1) {
  return { id: `00000000-0000-4000-8000-${clock.replace(':', '').padStart(10, '0')}${revision.toString().padStart(2, '0')}`,
    content_hash: revision.toString(16).padStart(64, '0'), received_at: day + 'T02:05:00Z', period_state: 'unknown', arrival_kind: 'late_observed',
    payload: { open: '10', high: '15', low: '9', close, interval: '1m', trade_day: day,
      source_clock_at: `${day}T${clock}:00+08:00`, bar_end_at: null, clock_semantics: 'unknown',
      adjustment: 'unadjusted', price_unit: 'CNY', volume_shares: null, amount_cny: null, volume_native: '0', amount_native: null } };
}
type Bar = ReturnType<typeof bar>;
type Hold = { gate: Promise<void>; release: () => void; started: boolean; finished: boolean };
function hold(): Hold {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  return { gate, release, started: false, finished: false };
}
function response(items: Bar[], state: string, gaps: number) {
  return { meta, data: { instrument: identity, interval: '1m', adjustment: 'unadjusted', observation_day: day,
    source_id: source, source_name: '合成刷新分钟源（非真实行情）', source_family: 'synthetic-refresh-only',
    items, state, reason: 'synthetic_refresh_test', data_mode: 'source_minutes', timestamp_semantics: 'unknown', finality: 'unknown',
    historical_reconstruction: false, last_success_at: day + 'T02:05:00Z', last_attempt: null,
    latest_source_time: items.at(-1)?.payload.source_clock_at ?? null, age_seconds: 0, freshness_limit_seconds: 120, gap_count: gaps } };
}
async function synthetic(page: Page) {
  const state = { rows: [bar('10:00', '11'), bar('10:02', '12')], mode: 'available' as 'available' | 'error' | 'permission_missing',
    gaps: 1, minuteCalls: 0, writes: [] as string[], held: null as Hold | null };
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url());
    if (request.method() !== 'GET') state.writes.push(url.pathname);
    if (url.pathname === '/api/v1/session') return route.fulfill({ json: { meta,
      data: { user: { id: user, username: 'synthetic-refresh-only', role: 'viewer' }, csrf_token: 'synthetic-not-a-credential' } } });
    if (url.pathname === '/api/v1/market-quotes') return route.fulfill({ json: { meta,
      data: { selection: 'instrument', items: [{ instrument: identity, state: 'not_configured', freshness_limit_seconds: 60 }] } } });
    if (url.pathname === '/api/v1/data-sources') return route.fulfill({ json: { meta, data: { items: [{ family: 'synthetic-refresh-only', provider: {
      id: source, name: '合成刷新分钟源（非真实行情）', kind: 'market', configured: true, enabled: true, entitlement_status: 'verified',
      capabilities: ['bar.minute', 'bar.1m'], rights: { display: 'allowed', cache: 'allowed', export: 'denied', redistribute: 'denied' },
    }, collection_state: 'ready', last_attempt: null, last_success_at: null }], available_adapters: [], collection_enabled: false } } });
    if (url.pathname === '/api/v1/market-bars') {
      if (url.searchParams.get('interval') !== '1m') return route.fulfill({ json: { meta, data: { instrument: identity, items: [], adjustment: 'unadjusted', source_name: null } } });
      expect(url.searchParams.get('instrument_id')).toBe(stock);
      // Latest-saved mode omits day; explicit dates must still match this fixture.
      expect([null, day]).toContain(url.searchParams.get('day'));
      state.minuteCalls++;
      const deferred = state.held; state.held = null;
      // Capture at request time, so a released old request contains an older
      // version even if the local fixture cache has changed in the meantime.
      const body = response(state.mode === 'permission_missing' ? [] : structuredClone(state.rows), state.mode, state.gaps);
      const failed = state.mode === 'error';
      if (deferred) { deferred.started = true; await deferred.gate; }
      await route.fulfill(failed ? { status: 503, json: { request_id: stock,
        error: { code: 'unavailable', message: '合成刷新缓存暂不可用', details: [] } } } : { json: body });
      if (deferred) deferred.finished = true;
      return;
    }
    return route.fulfill({ status: 503, json: { request_id: stock, error: { code: 'synthetic_unavailable', message: '合成隔离测试：其余缓存未配置', details: [] } } });
  });
  return state;
}
const chart = (page: Page) => page.getByRole('region', { name: '行情图表', exact: true });
const candles = (page: Page) => chart(page).locator('[data-bar-clock]');
async function open(page: Page) {
  await page.clock.install({ time: new Date(day + 'T02:03:00Z') });
  await page.goto('/workbench?legacy=1&instrument=' + stock);
  await expect(page.getByRole('button', { name: '退出', exact: true })).toBeVisible();
  await expect(page.getByLabel('分钟K周期', { exact: true })).toBeVisible();
  await page.clock.pauseAt(new Date(day + 'T02:03:10Z'));
  await page.getByLabel('分钟K周期', { exact: true }).selectOption('1分钟');
  await expect(candles(page)).toHaveCount(2);
  await expect(chart(page).locator('.candle-readout')).toContainText('收 12 元');
}
async function uniqueClocks(page: Page, expected: number) {
  const clocks = await candles(page).evaluateAll(nodes => nodes.map(node => node.getAttribute('data-bar-clock')));
  expect(clocks).toHaveLength(expected); expect(new Set(clocks).size).toBe(expected);
}
async function visibility(page: Page, state: 'hidden' | 'visible') {
  // Controlled visibility events exercise the real browser listener. This is
  // not a claim that OS-window background lifecycle or supplier uptime passed.
  await page.evaluate(value => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => value });
    document.dispatchEvent(new Event('visibilitychange'));
  }, state);
}

test('60 second polling appends fixture minutes, replaces same-clock revisions and later fills gaps without duplicate candles', async ({ page }) => {
  const state = await synthetic(page); await open(page);
  const initial = state.minuteCalls;
  const oldY = await candles(page).last().locator('rect').getAttribute('y');
  state.rows = [bar('10:00', '11'), bar('10:02', '13', 2), bar('10:03', '14')];
  await page.clock.runFor(59_000); expect(state.minuteCalls).toBe(initial);
  await page.clock.runFor(1_100); await expect.poll(() => state.minuteCalls).toBe(initial + 1);
  await expect(candles(page)).toHaveCount(3);
  await expect(chart(page).locator('.candle-readout')).toContainText('收 14 元');
  const revised = chart(page).locator('[data-bar-clock="' + day + 'T10:02:00+08:00"]');
  expect(await revised.locator('rect').getAttribute('y')).not.toBe(oldY);
  if (!await chart(page).locator('.chart-data-details').evaluate(node => (node as HTMLDetailsElement).open)) await chart(page).locator('.chart-data-details > summary').click();
  await chart(page).getByLabel('查看K线日期').selectOption('1');
  await expect(chart(page).locator('.candle-readout')).toContainText('收 13 元');
  await chart(page).locator('.minute-chart-evidence > summary').click();
  await expect(chart(page).locator('.minute-chart-evidence')).toContainText(bar('10:02', '13', 2).id);
  await chart(page).locator('.minute-chart-evidence > summary').click();
  await uniqueClocks(page, 3);
  state.rows = [bar('10:00', '11'), bar('10:01', '11.5'), bar('10:02', '13', 2), bar('10:03', '14')]; state.gaps = 0;
  await page.mouse.move(0, 0); await page.clock.runFor(60_000);
  await expect(candles(page)).toHaveCount(4);
  await expect(chart(page)).toContainText('已检测缺段：0（不补造）');
  if (!await chart(page).locator('.chart-data-details').evaluate(node => (node as HTMLDetailsElement).open)) await chart(page).locator('.chart-data-details > summary').click();
  await chart(page).getByLabel('查看K线日期').selectOption('1');
  await expect(chart(page).locator('.candle-readout')).toContainText('收 11.5 元');
  await uniqueClocks(page, 4);
  await page.clock.runFor(60_000); await expect.poll(() => state.minuteCalls).toBe(initial + 3);
  await expect(candles(page)).toHaveCount(4); await uniqueClocks(page, 4);
  await page.getByRole('radio', { name: '分时', exact: true }).click();
  const line = chart(page).getByRole('img', { name: '真实1分钟收盘价分时图', exact: true });
  await expect(line.locator('circle')).toHaveCount(4); await expect(line.locator('path')).toHaveCount(1);
  await expect(line.locator('path')).toHaveAttribute('d', /L/);
  expect(state.writes).toEqual([]);
});

test('hidden documents pause polling, visibility return refreshes immediately and unmount removes timer and listener', async ({ page }) => {
  const state = await synthetic(page); await open(page); const initial = state.minuteCalls;
  await visibility(page, 'hidden');
  state.rows = [bar('10:00', '11'), bar('10:02', '12'), bar('10:03', '14')];
  await page.clock.runFor(180_000); expect(state.minuteCalls).toBe(initial);
  await expect(candles(page)).toHaveCount(2);
  await visibility(page, 'visible'); await expect.poll(() => state.minuteCalls).toBe(initial + 1);
  await expect(candles(page)).toHaveCount(3); await expect(chart(page).locator('.candle-readout')).toContainText('收 14 元');
  await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: '系统设置', exact: true }).click();
  await expect(chart(page)).toHaveCount(0); const before = state.minuteCalls;
  await page.clock.runFor(120_000); await visibility(page, 'visible'); expect(state.minuteCalls).toBe(before);
  expect(state.writes).toEqual([]);
});

test('same-key polling keeps old synthetic bars during a request and discards an older overlapping response', async ({ page }) => {
  const state = await synthetic(page); await open(page);
  const deferred = hold(); state.held = deferred;
  await page.clock.runFor(60_000); await expect.poll(() => deferred.started).toBe(true);
  // Retaining the same-key cache avoids the old periodic blank-chart flash.
  await expect(candles(page)).toHaveCount(2);
  await expect(chart(page).locator('.candle-readout')).toContainText('收 12 元');
  state.rows = [bar('10:00', '11'), bar('10:02', '13', 2), bar('10:03', '14')];
  await visibility(page, 'visible');
  await expect(candles(page)).toHaveCount(3); await expect(chart(page).locator('.candle-readout')).toContainText('收 14 元');
  deferred.release(); await expect.poll(() => deferred.finished).toBe(true);
  await expect(candles(page)).toHaveCount(3); await expect(chart(page).locator('.candle-readout')).toContainText('收 14 元');
  await uniqueClocks(page, 3); expect(state.writes).toEqual([]);
});

test('scheduled errors retain dated prices with a warning; permission withdrawal clears them', async ({ page }) => {
  const state = await synthetic(page); await open(page);
  state.mode = 'error'; await page.clock.runFor(60_000);
  await expect(await openNotifications(page)).toContainText('合成刷新缓存暂不可用');
  await expect(candles(page)).toHaveCount(2); await expect(chart(page).locator('.candle-readout')).toBeVisible();
  await expect(chart(page)).toContainText('更新失败，保留已保存行情');
  await expect(chart(page)).not.toContainText('缓存已核对');
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  state.mode = 'available'; await visibility(page, 'visible'); await expect(candles(page)).toHaveCount(2);
  state.mode = 'permission_missing'; await page.clock.runFor(60_000);
  await expect(chart(page)).toContainText('使用范围未核实');
  await expect(candles(page)).toHaveCount(0); await expect(chart(page).locator('.candle-readout')).toHaveCount(0);
  state.mode = 'available'; await visibility(page, 'visible'); await expect(candles(page)).toHaveCount(2);
  await uniqueClocks(page, 2); expect(state.writes).toEqual([]);
});

test('a selected source clock remains selected when an earlier missing minute arrives and its own candle is revised', async ({ page }) => {
  const state = await synthetic(page); await open(page);
  const selectedClockLabel = await chart(page).getByLabel('查看K线日期').locator('option').filter({ hasText: '10:02:00' }).textContent();
  expect(selectedClockLabel).toBeTruthy();
  await chart(page).locator('.chart-data-details > summary').click();
  await chart(page).getByLabel('查看K线日期').selectOption({ label: selectedClockLabel! });
  await expect(chart(page).locator('.candle-readout strong')).toContainText('10:02:00');
  state.rows = [bar('10:00', '11'), bar('10:01', '11.5'), bar('10:02', '13', 2), bar('10:03', '14')]; state.gaps = 0;
  await page.mouse.move(0, 0); await page.clock.runFor(60_000);
  await expect(candles(page)).toHaveCount(4);
  await expect(chart(page).locator('.candle-readout strong')).toContainText('10:02:00');
  await expect(chart(page).locator('.candle-readout')).toContainText('收 13 元');
  await expect(chart(page).getByLabel('查看K线日期').locator('option:checked')).toContainText('10:02:00');
  await uniqueClocks(page, 4); expect(state.writes).toEqual([]);
});
