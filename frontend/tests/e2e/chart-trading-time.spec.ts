import { expect, test } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';

// Isolated synthetic OHLC/cache/session fixtures only; not market observations.
// No actual provider, user credential or database is involved in these tests.
const day = '2020-01-03';
const stock = '00000000-0000-4000-8000-000000000061';
const source = '00000000-0000-4000-8000-000000000062';
const identity = { id: stock, exchange: 'SSE', symbol: '600001', asset_type: 'stock', name: '合成交易时段测试（非真实行情）' };
const meta = { request_id: stock, app_mode: 'demo', generated_at: day + 'T08:00:00Z', as_of_time: null,
  quality: { status: 'partial', reasons: ['Synthetic trading-time axis fixture only; not real prices'] } };
const periods = [
  { label: '1分钟', interval: '1m', clocks: ['09:31', '11:29', '11:30', '13:01', '13:02', '15:00'], first: 1 },
  { label: '5分钟', interval: '5m', clocks: ['09:35', '11:25', '11:30', '13:05', '13:10', '15:00'], first: 5 },
  { label: '15分钟', interval: '15m', clocks: ['09:45', '11:15', '11:30', '13:15', '13:30', '15:00'], first: 15 },
  { label: '30分钟', interval: '30m', clocks: ['10:00', '11:00', '11:30', '13:30', '14:00', '15:00'], first: 30 },
  { label: '60分钟', interval: '60m', clocks: ['10:30', '11:30', '14:00', '15:00'], first: 60 },
];
function bar(clock: string, interval: string, i: number, tradeDay = day) {
  return { id: `00000000-0000-4000-8000-${(i + 1).toString().padStart(12, '0')}`,
    received_at: tradeDay + 'T08:00:00Z', content_hash: (i + 1).toString(16).padStart(64, '0'),
    arrival_kind: 'backfill', period_state: 'unknown',
    payload: { interval, trade_day: tradeDay, source_clock_at: `${tradeDay}T${clock}:00+08:00`,
      bar_end_at: null, clock_semantics: 'unknown', adjustment: 'unadjusted', price_unit: 'CNY',
      open: '10', high: '20', low: '8', close: String(11 + i), volume_shares: null, amount_cny: null, volume_native: '0', amount_native: null } };
}
async function synthetic(page: Page) {
  const state = { mode: 'normal' as 'normal' | 'boundary' | 'gaps' | 'unknown' | 'outside' | 'cross_day', calls: [] as URL[], writes: [] as string[] };
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (route.request().method() !== 'GET') state.writes.push(url.pathname);
    if (url.pathname === '/api/v1/session') return route.fulfill({ json: { meta, data: {
      user: { id: stock, username: 'synthetic-axis-only', role: 'viewer' }, csrf_token: 'synthetic-not-a-credential',
    } } });
    if (url.pathname === '/api/v1/market-quotes') return route.fulfill({ json: { meta, data: { selection: 'instrument',
      items: [{ instrument: identity, state: 'not_configured', freshness_limit_seconds: 60 }] } } });
    if (url.pathname === '/api/v1/data-sources') return route.fulfill({ json: { meta, data: {
      items: [{ family: 'synthetic-axis-only', provider: { id: source, name: '合成时段来源（非真实行情）', enabled: true,
        capabilities: ['bar.minute', ...periods.map(period => 'bar.' + period.interval)] } }], available_adapters: [], collection_enabled: false,
    } } });
    if (url.pathname === '/api/v1/market-bars') {
      state.calls.push(url);
      const interval = url.searchParams.get('interval') ?? '1d';
      const daily = interval === '1d';
      const clocks = state.mode === 'boundary' ? ['11:29', '11:30', '13:00', '13:01']
        : state.mode === 'gaps' ? ['09:31', '09:32', '11:28', '11:30', '13:01', '13:02', '15:00']
        : state.mode === 'outside' ? ['09:25', '11:30', '13:01', '15:00']
        : periods.find(period => period.interval === interval)?.clocks ?? [];
      const items = daily ? ['2020-01-02', day, '2020-01-06'].map((tradeDay, i) => bar('15:00', '1d', i, tradeDay))
        : clocks.map((clock, i) => bar(clock, interval, i, state.mode === 'cross_day' && i === clocks.length - 1 ? '2020-01-06' : day));
      return route.fulfill({ json: { meta, data: { instrument: state.mode === 'unknown' ? { ...identity, exchange: 'BSE' } : identity,
        interval, adjustment: url.searchParams.get('adjustment'), observation_day: url.searchParams.get('day') ?? day,
        source_id: source, source_name: '合成时段来源（非真实行情）', source_family: 'synthetic-axis-only',
        items, state: 'historical', reason: 'saved_minutes_not_live_session', data_mode: daily ? 'saved_daily' : 'source_minutes',
        timestamp_semantics: 'unknown', finality: 'unknown', historical_reconstruction: true, last_attempt: null,
        last_success_at: day + 'T08:00:00Z', latest_source_time: items.at(-1)?.payload.source_clock_at ?? null,
        age_seconds: 86400, freshness_limit_seconds: 120, gap_count: 0, current_daily: null,
      } } });
    }
    return route.fulfill({ status: 503, json: { request_id: stock,
      error: { code: 'synthetic_unavailable', message: '合成隔离测试：其余缓存未配置', details: [] } } });
  });
  return state;
}
const chart = (page: Page) => page.getByRole('region', { name: '行情图表', exact: true });
async function open(page: Page, label = '1分钟') {
  await page.goto('/workbench?legacy=1&instrument=' + stock);
  await expect(page.getByRole('button', { name: '退出', exact: true })).toBeVisible();
  await page.getByLabel('分钟K周期', { exact: true }).selectOption(label);
  await expect(chart(page).getByRole('img', { name: label + '真实蜡烛图', exact: true })).toBeVisible();
}
async function positions(svg: Locator) {
  return svg.locator('[data-bar-clock] > line').evaluateAll(nodes => nodes.map(node => Number(node.getAttribute('x1'))));
}
for (const period of periods) test(period.label + ' uses a joined-session axis while retaining every real source clock and native OHLC', async ({ page }) => {
  const state = await synthetic(page); await open(page, period.label);
  const svg = chart(page).getByRole('img', { name: period.label + '真实蜡烛图', exact: true });
  await expect(svg).toHaveAttribute('data-time-axis', 'ashare_sessions');
  await expect(svg.locator('[data-bar-clock]')).toHaveCount(period.clocks.length);
  const x = await positions(svg);
  const boundary = period.clocks.indexOf('11:30');
  // The afternoon's first interval has the same spacing as its next real interval,
  // without inserting 90 closed-market minutes (or a fabricated midday candle).
  expect(x[boundary + 1] - x[boundary]).toBeCloseTo(x[boundary + 2] - x[boundary + 1], 6);
  expect(x[0]).toBeCloseTo(60 + period.first / 240 * 748, 6);
  expect(x.at(-1)).toBeCloseTo(808, 6);
  await expect(svg.locator('[data-session-tick]')).toHaveText(['09:30', '10:30', '11:30 / 13:00', '14:00', '15:00']);
  const clocks = await svg.locator('[data-bar-clock]').evaluateAll(nodes => nodes.map(node => node.getAttribute('data-bar-clock')));
  expect(clocks).toEqual(period.clocks.map(clock => `${day}T${clock}:00+08:00`));
  expect(new Set(clocks).size).toBe(period.clocks.length);
  await expect(chart(page).locator('.candle-readout')).toContainText('量 0（原单位未核）');
  await expect(chart(page).locator('.candle-readout')).toContainText('额 未知');
  await expect(chart(page)).toContainText('最终性：未确认');
  expect(state.calls.at(-1)?.searchParams.has('day')).toBe(false);
  await expect(chart(page).getByLabel('分钟行情日期')).toHaveValue(day);
  expect(state.writes).toEqual([]);
});

test('minute line connects actual lunch-adjacent samples but leaves missing trading minutes disconnected', async ({ page }) => {
  const state = await synthetic(page); state.mode = 'gaps'; await open(page);
  await page.getByRole('radio', { name: '分时', exact: true }).click();
  const svg = chart(page).getByRole('img', { name: '真实1分钟收盘价分时图', exact: true });
  await expect(svg).toHaveAttribute('data-time-axis', 'ashare_sessions');
  await expect(svg.locator('circle')).toHaveCount(7);
  const paths = await svg.locator('path').evaluateAll(nodes => nodes.map(node => node.getAttribute('d') ?? ''));
  // 09:31→09:32; isolated 11:28; 11:30→13:01→13:02; isolated 15:00.
  expect(paths.map(value => (value.match(/ L /g) ?? []).length)).toEqual([1, 0, 2, 0]);
  expect((paths.join(' ').match(/M /g) ?? []).length).toBe(4);
  await expect(svg.locator('[data-source-clock="' + day + 'T12:00:00+08:00"]')).toHaveCount(0);
  expect(state.writes).toEqual([]);
});

test('both real midday boundary clocks remain distinct, selectable and correctly hoverable on desktop and mobile', async ({ page }) => {
  const state = await synthetic(page); state.mode = 'boundary'; await open(page);
  const svg = chart(page).getByRole('img', { name: '1分钟真实蜡烛图', exact: true });
  const x = await positions(svg);
  expect(x[2] - x[1]).toBeGreaterThan(0); expect(x[2] - x[1]).toBeLessThan(4);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const [index, clock, close] of [[1, '11:30', '12'], [2, '13:00', '13']] as const) {
      if (!await chart(page).locator('.chart-data-details').evaluate(node => (node as HTMLDetailsElement).open)) await chart(page).locator('.chart-data-details > summary').click();
      await chart(page).getByLabel('查看K线日期').selectOption(String(index));
      await expect(chart(page).locator('.candle-readout strong')).toContainText(clock + ':00');
      await expect(chart(page).locator('.candle-readout')).toContainText('收 ' + close + ' 元');
      await svg.scrollIntoViewIfNeeded();
      // Read the browser's actual SVG transform; do not alter styles/geometry.
      const point = await svg.locator('[data-bar-clock]').nth(index).locator('rect').evaluate(node => {
        if (!(node instanceof SVGGraphicsElement)) throw new Error('Synthetic target is not an SVG graphic');
        const matrix = node.getScreenCTM();
        if (!matrix) throw new Error('Synthetic SVG is not laid out');
        const rect = node.getBBox();
        const point = new DOMPoint(rect.x + rect.width / 2, rect.y).matrixTransform(matrix);
        return { x: point.x, y: point.y };
      });
      await page.mouse.move(point.x, point.y);
      await expect(chart(page).locator('.candle-readout strong')).toContainText(clock + ':00');
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.mouse.move(0, 0);
  }
  await expect(svg.locator('[data-bar-clock]')).toHaveCount(4);
  expect(state.writes).toEqual([]);
});

test('unverified rules, preopen observations and cross-day data keep their source-time axis rather than dropping or compressing records', async ({ page }) => {
  const state = await synthetic(page); state.mode = 'unknown'; await open(page);
  const svg = chart(page).getByRole('img', { name: '1分钟真实蜡烛图', exact: true });
  for (const mode of ['unknown', 'outside', 'cross_day'] as const) {
    state.mode = mode;
    await chart(page).getByRole('button', { name: '刷新本机分钟缓存', exact: true }).click();
    await expect(chart(page).locator('.minute-chart-body')).toHaveAttribute('aria-busy', 'false');
    await expect(svg).toHaveAttribute('data-time-axis', 'source_time');
    await expect(chart(page)).toContainText('按原始源时标显示');
    await expect(svg.locator('[data-session-tick]')).toHaveCount(0);
    await expect(svg.locator('[data-bar-clock]')).toHaveCount(mode === 'outside' ? 4 : 6);
  }
  const clocks = await svg.locator('[data-bar-clock]').evaluateAll(nodes => nodes.map(node => node.getAttribute('data-bar-clock')));
  expect(clocks.at(-1)).toBe('2020-01-06T15:00:00+08:00');
  expect(state.writes).toEqual([]);
});

test('saved daily, weekly and monthly candles keep their original period axis and OHLC without intraday compression', async ({ page }) => {
  const state = await synthetic(page); await open(page);
  for (const [period, count] of [['日K', 3], ['周K', 2], ['月K', 1]] as const) {
    await page.getByRole('radio', { name: period, exact: true }).click();
    const svg = chart(page).getByRole('img', { name: period + '历史蜡烛图', exact: true });
    await expect(svg).toHaveAttribute('data-time-axis', 'saved_periods');
    await expect(svg.locator('[data-bar-clock]')).toHaveCount(count);
    await expect(svg.locator('[data-session-tick]')).toHaveCount(0);
    await expect(chart(page).getByLabel('K线复权口径')).toHaveValue('unadjusted');
    if (period === '日K') {
      const x = await positions(svg);
      expect(x[1] - x[0]).toBeCloseTo(x[2] - x[1], 6);
      await expect(chart(page).locator('.candle-readout')).toContainText('2020-01-06');
      await expect(chart(page).locator('.candle-readout')).toContainText('收 13 元');
    }
  }
  expect(state.writes).toEqual([]);
});
