import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

// Entirely synthetic cache/session fixtures. These prices and 238 rows are
// generated for UI regression only, never real observations or provider access.
const friday = '2020-01-03';
const saturday = '2020-01-04';
const nextDay = '2020-01-06';
const stockA = '00000000-0000-4000-8000-000000000071';
const stockB = '00000000-0000-4000-8000-000000000072';
const sourceA = '00000000-0000-4000-8000-000000000073';
const sourceB = '00000000-0000-4000-8000-000000000074';
const identity = (id: string) => ({ id, exchange: 'SSE', symbol: id === stockB ? '600002' : '600001',
  asset_type: 'stock', name: id === stockB ? '合成最近乙（非真实行情）' : '合成最近甲（非真实行情）' });
const meta = { request_id: stockA, app_mode: 'demo', generated_at: saturday + 'T02:00:00Z', as_of_time: null,
  quality: { status: 'partial', reasons: ['Synthetic latest-saved fixture only; not market observations'] } };
type Hold = { gate: Promise<void>; release: () => void; started: boolean; finished: boolean };
function hold(): Hold {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  return { gate, release, started: false, finished: false };
}
function rows(day: string, count: number, close: string, interval: string) {
  return Array.from({ length: count }, (_, i) => {
    const minutes = i < 119 ? 9 * 60 + 31 + i : 13 * 60 + 1 + i - 119;
    const clock = `${Math.floor(minutes / 60).toString().padStart(2, '0')}:${(minutes % 60).toString().padStart(2, '0')}`;
    return { id: `00000000-0000-4000-8000-${(i + 1).toString().padStart(12, '0')}`,
      content_hash: (i + 1).toString(16).padStart(64, '0'), received_at: day + 'T08:00:00Z',
      period_state: 'unknown', arrival_kind: 'backfill',
      payload: { open: '10', high: '25', low: '9', close, interval, trade_day: day,
        source_clock_at: `${day}T${clock}:00+08:00`, bar_end_at: null, clock_semantics: 'unknown',
        adjustment: 'unadjusted', price_unit: 'CNY', volume_shares: null, amount_cny: null, volume_native: '0', amount_native: null } };
  });
}
async function synthetic(page: Page) {
  const state = { latestA: friday, latestB: '2020-01-02', mode: 'historical' as 'historical' | 'available', calls: [] as URL[], writes: [] as string[], held: null as Hold | null };
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url());
    if (request.method() !== 'GET') state.writes.push(url.pathname);
    if (url.pathname === '/api/v1/session') return route.fulfill({ json: { meta, data: {
      user: { id: stockA, username: 'synthetic-latest-only', role: 'viewer' }, csrf_token: 'synthetic-not-a-credential',
    } } });
    if (url.pathname === '/api/v1/instruments') return route.fulfill({ json: { meta, data: { items: [identity(stockB)] } } });
    if (url.pathname === '/api/v1/market-quotes') {
      const id = url.searchParams.get('instrument_id') ?? stockA;
      return route.fulfill({ json: { meta, data: { selection: 'instrument',
        items: [{ instrument: identity(id), state: 'not_configured', freshness_limit_seconds: 60 }] } } });
    }
    if (url.pathname === '/api/v1/data-sources') return route.fulfill({ json: { meta, data: {
      items: [sourceA, sourceB].map((id, i) => ({ family: 'synthetic-latest-' + i, provider: {
        id, name: `合成最近来源${i ? '乙' : '甲'}（非真实行情）`, kind: 'market', configured: true, enabled: true,
        entitlement_status: 'verified', capabilities: ['bar.minute', 'bar.1m', 'bar.5m'],
      }, collection_state: 'ready', last_attempt: null, last_success_at: null })), available_adapters: [], collection_enabled: false,
    } } });
    if (url.pathname === '/api/v1/market-bars') {
      const interval = url.searchParams.get('interval') ?? '1d';
      const id = url.searchParams.get('instrument_id') ?? stockA;
      if (interval === '1d') return route.fulfill({ json: { meta, data: { instrument: identity(id),
        items: [], adjustment: 'unadjusted', source_name: null } } });
      expect(['1m', '5m']).toContain(interval);
      expect(url.searchParams.get('adjustment')).toBe('unadjusted');
      state.calls.push(url);
      const source = url.searchParams.get('source_id') ?? sourceA;
      expect([sourceA, sourceB]).toContain(source);
      const latest = source === sourceB ? state.latestB : state.latestA;
      const requested = url.searchParams.get('day');
      const day = requested ?? (id === stockB ? null : latest);
      const available = id === stockA && day === latest;
      const items = available ? rows(day!, source === sourceB ? 2 : interval === '5m' ? 48 : 238,
        source === sourceB ? '21' : day === nextDay ? '14' : '12', interval) : [];
      // Snapshot before a hold: late replies must not paint the next selection.
      const body = { meta, data: { instrument: identity(id), interval, adjustment: 'unadjusted', observation_day: day,
        source_id: source, source_name: `合成最近来源${source === sourceB ? '乙' : '甲'}（非真实行情）`,
        source_family: 'synthetic-latest-only', items, state: available ? state.mode : 'empty',
        reason: available ? state.mode === 'historical' ? 'saved_minutes_not_live_session' : null : 'minute_not_observed', data_mode: 'source_minutes',
        timestamp_semantics: 'unknown', finality: 'unknown', historical_reconstruction: true, last_attempt: null,
        last_success_at: day ? day + 'T08:00:00Z' : null, latest_source_time: items.at(-1)?.payload.source_clock_at ?? null,
        age_seconds: 86400, freshness_limit_seconds: 120, gap_count: 0 } };
      const deferred = state.held; state.held = null;
      if (deferred) { deferred.started = true; await deferred.gate; }
      await route.fulfill({ json: body });
      if (deferred) deferred.finished = true;
      return;
    }
    return route.fulfill({ status: 503, json: { request_id: stockA,
      error: { code: 'synthetic_unavailable', message: '合成隔离测试：其余本机缓存未配置', details: [] } } });
  });
  return state;
}
const chart = (page: Page) => page.getByRole('region', { name: '行情图表', exact: true });
const candles = (page: Page) => chart(page).locator('[data-bar-clock]');
async function open(page: Page) {
  // Saturday is intentional: a holiday must not silently force today's empty cache.
  await page.clock.install({ time: new Date(saturday + 'T02:00:00Z') });
  await page.goto('/workbench?legacy=1&instrument=' + stockA);
  await expect(page.getByRole('button', { name: '退出', exact: true })).toBeVisible();
  await expect(page.getByLabel('分钟K周期', { exact: true })).toBeVisible();
  await page.clock.pauseAt(new Date(saturday + 'T02:00:10Z'));
  await page.getByLabel('分钟K周期', { exact: true }).selectOption('1分钟');
  await expect(candles(page)).toHaveCount(238);
  await expect(chart(page).locator('.candle-readout')).toContainText('收 12 元');
}

test('Saturday defaults to actual latest-saved Friday with 238 synthetic bars and an explicit historical date', async ({ page }) => {
  const state = await synthetic(page); await open(page);
  expect(state.calls.length).toBeGreaterThan(0);
  expect(state.calls.every(url => !url.searchParams.has('day'))).toBe(true);
  await expect(chart(page).getByLabel('分钟行情日期')).toHaveValue(friday);
  await expect(chart(page)).toContainText('最近已保存 · ' + friday);
  await expect(chart(page)).toContainText('历史分钟非当前实时');
  await expect(chart(page).getByLabel('分钟行情状态')).toContainText('保存日期：' + friday);
  await expect(chart(page).locator('.candle-readout strong')).toContainText('2020/1/3');
  await expect(chart(page).locator('.candle-readout')).toContainText('量 0（原单位未核）');
  await expect(chart(page).getByRole('button', { name: '最近已保存', exact: true })).toBeDisabled();
  await chart(page).getByRole('button', { name: '刷新本机分钟缓存', exact: true }).click();
  await expect(chart(page).locator('.minute-chart-body')).toHaveAttribute('aria-busy', 'false');
  expect(state.calls.at(-1)?.searchParams.has('day')).toBe(false);
  await page.getByRole('radio', { name: '分时', exact: true }).click();
  await expect(chart(page).getByRole('img', { name: '真实1分钟收盘价分时图', exact: true }).locator('circle')).toHaveCount(238);
  expect(state.writes).toEqual([]);
});

test('an explicitly selected empty Saturday never borrows Friday, and latest-saved restores automatic lookup', async ({ page }) => {
  const state = await synthetic(page); await open(page);
  await chart(page).getByLabel('分钟行情日期').fill(saturday);
  await expect.poll(() => state.calls.at(-1)?.searchParams.get('day')).toBe(saturday);
  await expect(chart(page).getByLabel('分钟行情日期')).toHaveValue(saturday);
  await expect(chart(page)).toContainText('尚无该日分钟行情');
  await expect(candles(page)).toHaveCount(0);
  await expect(chart(page).locator('.candle-readout')).toHaveCount(0);
  await page.clock.runFor(60_000);
  expect(state.calls.at(-1)?.searchParams.get('day')).toBe(saturday);
  await expect(candles(page)).toHaveCount(0);
  await chart(page).getByRole('button', { name: '最近已保存', exact: true }).click();
  await expect(candles(page)).toHaveCount(238);
  expect(state.calls.at(-1)?.searchParams.has('day')).toBe(false);
  await expect(chart(page).getByLabel('分钟行情日期')).toHaveValue(friday);
  expect(state.writes).toEqual([]);
});

test('automatic polling keeps omitting day and advances its displayed date when new saved data arrives', async ({ page }) => {
  const state = await synthetic(page); await open(page);
  const initial = state.calls.length;
  const deferred = hold(); state.held = deferred;
  await page.clock.runFor(60_000); await expect.poll(() => deferred.started).toBe(true);
  // Same-key cache stays visible during polling; resolving the date does not remount.
  await expect(candles(page)).toHaveCount(238);
  await expect(chart(page).getByLabel('分钟行情日期')).toHaveValue(friday);
  state.latestA = nextDay; state.mode = 'available';
  await page.clock.setSystemTime(new Date(nextDay + 'T02:00:00Z'));
  deferred.release(); await expect.poll(() => deferred.finished).toBe(true);
  await expect(chart(page).locator('.minute-chart-body')).toHaveAttribute('aria-busy', 'false');
  await page.clock.runFor(60_000);
  await expect(chart(page).getByLabel('分钟行情日期')).toHaveValue(nextDay);
  await expect(chart(page).locator('.candle-readout strong')).toContainText('2020/1/6');
  await expect(chart(page).locator('.candle-readout')).toContainText('收 14 元');
  await expect(chart(page)).toContainText('请按来源状态和源时点核对时效。');
  await expect(chart(page)).not.toContainText('历史分钟非当前实时');
  await expect(candles(page)).toHaveCount(238);
  expect(state.calls.length).toBe(initial + 2);
  expect(state.calls.every(url => !url.searchParams.has('day'))).toBe(true);
  const clocks = await candles(page).evaluateAll(nodes => nodes.map(node => node.getAttribute('data-bar-clock')));
  expect(new Set(clocks).size).toBe(238);
  expect(clocks.every(clock => clock?.startsWith(nextDay))).toBe(true);
  expect(state.writes).toEqual([]);
});

test('latest-saved mode remains bound to the selected source and interval, including late old-source replies', async ({ page }) => {
  const state = await synthetic(page); await open(page);
  await chart(page).getByLabel('分钟行情来源').selectOption(sourceA);
  await expect.poll(() => state.calls.at(-1)?.searchParams.get('source_id')).toBe(sourceA);
  await expect(candles(page)).toHaveCount(238);
  const deferred = hold(); state.held = deferred;
  await chart(page).getByRole('button', { name: '刷新本机分钟缓存', exact: true }).click();
  await expect.poll(() => deferred.started).toBe(true);
  await chart(page).getByLabel('分钟行情来源').selectOption(sourceB);
  await expect(candles(page)).toHaveCount(2);
  await expect(chart(page).getByLabel('分钟行情日期')).toHaveValue(state.latestB);
  await expect(chart(page).locator('.candle-readout')).toContainText('收 21 元');
  deferred.release(); await expect.poll(() => deferred.finished).toBe(true);
  await expect(candles(page)).toHaveCount(2);
  await expect(chart(page).getByLabel('分钟行情日期')).toHaveValue(state.latestB);
  await expect(chart(page).getByLabel('分钟行情状态')).toContainText('合成最近来源乙');
  await page.getByLabel('分钟K周期', { exact: true }).selectOption('5分钟');
  await expect(chart(page).getByRole('img', { name: '5分钟真实蜡烛图', exact: true })).toBeVisible();
  expect(state.calls.at(-1)?.searchParams.get('interval')).toBe('5m');
  expect(state.calls.at(-1)?.searchParams.get('source_id')).toBe(sourceB);
  expect(state.calls.at(-1)?.searchParams.has('day')).toBe(false);
  expect(state.writes).toEqual([]);
});

test('a newly selected stock with no saved minutes cannot reuse the previous stock date or late chart', async ({ page }) => {
  const state = await synthetic(page); await open(page);
  const deferred = hold(); state.held = deferred;
  await chart(page).getByRole('button', { name: '刷新本机分钟缓存', exact: true }).click();
  await expect.poll(() => deferred.started).toBe(true);
  await page.getByRole('textbox', { name: '搜索标的', exact: true }).fill('合成最近乙');
  await page.getByRole('button', { name: '搜索标的', exact: true }).click();
  await page.getByRole('region', { name: '标的查询结果' }).getByRole('link', { name: /合成最近乙/ }).click();
  await page.getByLabel('分钟K周期', { exact: true }).selectOption('1分钟');
  await expect.poll(() => state.calls.at(-1)?.searchParams.get('instrument_id')).toBe(stockB);
  await expect(chart(page)).toContainText('尚无该日分钟行情');
  await expect(chart(page).getByLabel('分钟行情日期')).toHaveValue('');
  await expect(candles(page)).toHaveCount(0);
  await expect(chart(page).locator('.candle-readout')).toHaveCount(0);
  deferred.release(); await expect.poll(() => deferred.finished).toBe(true);
  await expect(candles(page)).toHaveCount(0);
  await expect(chart(page).getByLabel('分钟行情日期')).toHaveValue('');
  await expect(chart(page)).not.toContainText('保存日期：' + friday);
  await expect(chart(page)).toContainText('该标的、来源与周期尚无已保存分钟日期。');
  await expect(chart(page)).not.toContainText('正在核对该标的、来源与周期的实际保存日期');
  expect(state.calls.at(-1)?.searchParams.has('day')).toBe(false);
  expect(state.writes).toEqual([]);
});
