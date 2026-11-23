import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { calculateMacd, movingAverage, reliableVwap } from '../../src/features/market/chartMath';
import { chartFixturePath, serveTradingFixture } from '../fixtureServer';

test('chart calculations retain warm-up gaps and use the declared MACD seed', () => {
  expect(movingAverage([1, 2, 3, 4, 5, 6], 5)).toEqual([null, null, null, null, 3, 4]);
  const steady = calculateMacd(Array.from({ length: 50 }, () => 10));
  expect(steady.dif.slice(0, 25)).toEqual(Array(25).fill(null));
  expect(steady.histogram.slice(0, 33)).toEqual(Array(33).fill(null));
  expect(steady.histogram.slice(33)).toEqual(Array(17).fill(0));
  const rising = calculateMacd(Array.from({ length: 60 }, (_, i) => i + 1));
  expect(rising.dif[25]).toBeCloseTo(7);
  expect(rising.dea[33]).toBeCloseTo(7);
});

test('VWAP rejects missing or unknown units, midday gaps and incomplete opening coverage', () => {
  const row = (clock: string, volume = '100', amount = '1000') => ({ trade_day: '2020-01-03', source_clock_at: `2020-01-03T${clock}:00+08:00`, volume_shares: volume, amount_cny: amount });
  const rows = [row('09:31'), row('09:32', '300', '3300')];
  expect(reliableVwap(rows, false)).toEqual([10, 10.75]);
  expect(reliableVwap(rows, true)).toBeNull();
  expect(reliableVwap([row('10:00')], false)).toBeNull();
  expect(reliableVwap([row('09:31'), row('09:33')], false)).toBeNull();
  expect(reliableVwap([{ ...rows[0], volume_shares: null }], false)).toBeNull();
  expect(reliableVwap([row('09:31', '0', '0'), row('09:32')], false)).toEqual([null, 10]);
  expect(reliableVwap([row('09:31', '0', '100')], false)).toBeNull();
});

const stock = '00000000-0000-4000-8000-000000000071';
const source = '00000000-0000-4000-8000-000000000072';
const day = '2020-01-03';
const identity = { id: stock, exchange: 'SSE', symbol: '600001', asset_type: 'stock', name: '合成图表（非真实行情）' };
const meta = { app_mode: 'demo', quality: { status: 'partial', reasons: ['Synthetic chart tests only'] } };
async function fixture(page: Page, known = true, direct = false) {
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/v1/session') return route.fulfill({ json: { meta, data: { user: { id: stock, username: 'synthetic-chart', role: 'viewer' }, csrf_token: 'synthetic' } } });
    if (url.pathname === '/api/v1/market-quotes') return route.fulfill({ json: { meta, data: { selection: 'instrument', items: [{ instrument: identity, state: 'not_configured', freshness_limit_seconds: 60 }] } } });
    if (url.pathname === '/api/v1/data-sources') return route.fulfill({ json: { meta, data: { items: [{ family: 'synthetic', provider: { id: source, name: '合成来源', enabled: true, capabilities: ['bar.minute', 'bar.1m'] } }], available_adapters: [] } } });
    if (url.pathname === '/api/v1/market-bars') {
      const interval = url.searchParams.get('interval') ?? '1d';
      const items = Array.from({ length: 100 }, (_, i) => {
        const clock = new Date(Date.parse(day + 'T09:31:00+08:00') + i * 60_000).toISOString();
        return { id: stock, received_at: clock, content_hash: 'a'.repeat(64), period_state: 'unknown', payload: { interval, trade_day: interval === '1d' ? new Date(Date.parse('2019-01-01') + i * 86400_000).toISOString().slice(0, 10) : day, source_clock_at: clock, bar_end_at: null, clock_semantics: 'unknown', open: '10', high: '15', low: '9', close: (10 + i / 100).toFixed(2), volume_shares: known ? '100' : null, amount_cny: known ? (1000 + i).toString() : null, volume_native: known ? null : '10', price_unit: 'CNY', adjustment: 'unadjusted' } };
      });
      return route.fulfill({ json: { meta, data: { instrument: identity, interval, adjustment: 'unadjusted', source_id: source, source_name: '合成图表来源', observation_day: day, state: 'historical', items, gap_count: 0, finality: 'unknown', timestamp_semantics: 'unknown' } } });
    }
    return route.fulfill({ status: 503, json: { error: { code: 'synthetic', message: '隔离测试：其他资料未配置' } } });
  });
  if (direct) {
    const diagnostics: string[] = [];
    page.on('pageerror', error => diagnostics.push(error.message));
    page.on('console', message => { if (message.type() === 'error') diagnostics.push(message.text()); });
    await serveTradingFixture(page);
    await page.goto(chartFixturePath);
    try { await expect(page.locator('[data-bar-clock]')).toHaveCount(80); }
    catch (error) { throw new Error(diagnostics.join('\n') || String(error)); }
  } else {
    await page.goto('/workbench?legacy=1&instrument=' + stock);
    await expect(page.getByRole('button', { name: '退出', exact: true })).toBeVisible();
  }
}

const chart = (page: Page) => page.getByRole('region', { name: '行情图表', exact: true });
test('synchronized volume, MA/MACD, crosshair, pan, zoom and fullscreen use the real chart', async ({ page }) => {
  await fixture(page);
  const area = chart(page);
  await expect(area.locator('[data-volume-clock]')).toHaveCount(80);
  await expect(area.locator('[data-indicator="MA20"]')).toHaveAttribute('points', /\d/);
  await area.getByLabel('图表副图').selectOption('macd');
  await expect(area.locator('[data-macd-clock]')).toHaveCount(67);
  const svg = area.getByRole('img', { name: '日K历史蜡烛图', exact: true });
  await svg.scrollIntoViewIfNeeded();
  const bounds = (await svg.boundingBox())!;
  await page.mouse.move(bounds.x + bounds.width * 0.6, bounds.y + bounds.height * 0.3);
  await expect(area.getByTestId('chart-crosshair').locator('line')).toHaveCount(2);
  await area.getByRole('button', { name: '放大', exact: true }).click();
  await expect(area.locator('[data-volume-clock]')).toHaveCount(56);
  const lastClock = await area.locator('[data-volume-clock]').last().getAttribute('data-volume-clock');
  await area.getByRole('button', { name: '向前查看', exact: true }).click();
  expect(await area.locator('[data-volume-clock]').last().getAttribute('data-volume-clock')).not.toBe(lastClock);
  await area.getByRole('button', { name: '重置视图', exact: true }).click();
  await expect(area.locator('[data-volume-clock]')).toHaveCount(80);
  await area.getByRole('button', { name: '全屏图表 / 退出全屏', exact: true }).click();
  await expect.poll(() => page.evaluate(() => Boolean(document.fullscreenElement))).toBe(true);
  await area.getByRole('button', { name: '全屏图表 / 退出全屏', exact: true }).click();
  await expect.poll(() => page.evaluate(() => Boolean(document.fullscreenElement))).toBe(false);
  for (const width of [1440, 390]) { await page.setViewportSize({ width, height: 1000 }); expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true); }
});

test('VWAP appears only for trustworthy opening-minute amounts', async ({ page }) => {
  await fixture(page);
  await page.getByRole('radio', { name: '分时', exact: true }).click();
  await expect(chart(page).locator('[data-indicator="VWAP"]')).toHaveCount(1);
  await expect(chart(page).locator('[data-volume-clock]')).toHaveCount(100);
});

test('native unknown-unit volumes cannot become a VWAP price', async ({ page }) => {
  await fixture(page, false);
  await page.getByRole('radio', { name: '分时', exact: true }).click();
  await expect(chart(page).locator('[data-indicator="VWAP"]')).toHaveCount(0);
  await expect(chart(page)).toContainText('均价线暂不绘制');
  await expect(chart(page).locator('[data-volume-clock]')).toHaveCount(100);
});

test('controlled historical day excludes later daily bars before weekly and monthly grouping', async ({ page }) => {
  await fixture(page, true, true);
  for (const period of ['日K', '周K', '月K']) {
    await page.evaluate(({ stock, period }) => {
      const host = window as unknown as { testReact: { createElement: (...args: unknown[]) => unknown }; testChart: unknown; testRoot: { render: (node: unknown) => void } };
      host.testRoot.render(host.testReact.createElement(host.testChart, { instrument: stock, period, day: '2019-02-02' }));
    }, { stock, period });
    const controlled = page.locator('#controlled-chart-test');
    await expect(controlled.locator('[data-bar-clock]').last()).toHaveAttribute('data-bar-clock', '2019-02-02');
    const dates = await controlled.locator('[data-bar-clock]').evaluateAll(nodes => nodes.map(node => node.getAttribute('data-bar-clock')!));
    expect(dates.every(date => date <= '2019-02-02')).toBe(true);
    await expect(controlled.locator('.candle-readout')).toContainText('收 10.32 元');
    await expect(controlled.locator('.candle-readout')).not.toContainText('10.99');
  }
});

test('pointer drag pans the shared price and volume window and mobile controls remain usable', async ({ page }, info) => {
  await fixture(page, true, true);
  const area = chart(page);
  await area.getByRole('button', { name: '放大', exact: true }).click();
  const svg = area.getByRole('img', { name: '日K历史蜡烛图', exact: true });
  await svg.scrollIntoViewIfNeeded();
  const bounds = (await svg.boundingBox())!;
  const previous = await area.locator('[data-volume-clock]').last().getAttribute('data-volume-clock');
  await page.mouse.move(bounds.x + bounds.width * 0.4, bounds.y + bounds.height * 0.35);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width * 0.65, bounds.y + bounds.height * 0.35, { steps: 8 });
  await page.mouse.up();
  expect(await area.locator('[data-volume-clock]').last().getAttribute('data-volume-clock')).not.toBe(previous);
  expect(await area.locator('[data-bar-clock]').last().getAttribute('data-bar-clock')).toBe(await area.locator('[data-volume-clock]').last().getAttribute('data-volume-clock'));
  await page.setViewportSize({ width: 390, height: 1000 });
  await area.getByLabel('图表副图').selectOption('macd');
  await area.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await area.screenshot({ path: info.outputPath('trading-chart-mobile.png') });
});
