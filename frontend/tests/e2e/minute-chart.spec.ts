import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';
import { signIn } from './researchTestHelpers';

// Isolated synthetic UI data only. These rows are not real market observations.
const stockA = '00000000-0000-4000-8000-000000000081';
const stockB = '00000000-0000-4000-8000-000000000082';
const sourceA = '00000000-0000-4000-8000-000000000083';
const sourceB = '00000000-0000-4000-8000-000000000084';
const meta = { request_id: stockA, app_mode: 'demo', generated_at: '2020-01-03T02:05:00Z', as_of_time: null,
  quality: { status: 'partial', reasons: ['Synthetic minute chart fixture; not market data'] } };
const identity = (id: string) => ({ id, exchange: 'SSE', symbol: id === stockB ? '600002' : '600001', asset_type: 'stock', name: id === stockB ? '合成分钟乙' : '合成分钟甲' });
function bar(day: string, interval: string, minute: string, close = '12', periodState = 'unknown') {
  return { id: stockA, received_at: `${day}T02:05:00Z`, content_hash: '8'.repeat(64), period_state: periodState, arrival_kind: 'backfill',
    payload: { interval, trade_day: day, open: '10', high: '13', low: '9', close, volume_shares: null, amount_cny: null,
      volume_native: '0', amount_native: null, source_clock_at: `${day}T${minute}:00+08:00`, clock_semantics: 'unknown', bar_end_at: null, adjustment: 'unadjusted', price_unit: 'CNY' } };
}
async function synthetic(page: Page) {
  const state = { mode: 'filled' as 'filled' | 'empty' | 'error' | 'denied' | 'stale', calls: [] as URL[], defer: null as null | ((route: Route) => Promise<void>) };
  await page.route('**/api/v1/market-quotes*', route => {
    const id = new URL(route.request().url()).searchParams.get('instrument_id') ?? stockA;
    return route.fulfill({ json: { meta, data: { selection: 'instrument', items: [{ instrument: identity(id), state: 'not_configured', freshness_limit_seconds: 60 }] } } });
  });
  await page.route('**/api/v1/data-sources', route => route.fulfill({ json: { meta, data: { items: [sourceA, sourceB].map((id, i) => ({
    family: 'synthetic-minute-' + i, provider: { id, name: '合成分钟源' + (i ? '乙' : '甲'), capabilities: ['bar.minute', 'bar.1m', ...(i ? [] : ['bar.5m', 'bar.15m', 'bar.30m', 'bar.60m'])], enabled: true },
  })), available_adapters: [], collection_enabled: false } } }));
  await page.route('**/api/v1/market-bars?*', async route => {
    const query = new URL(route.request().url()); state.calls.push(query);
    if (state.defer) return state.defer(route);
    if (state.mode === 'error') return route.fulfill({ status: 503, json: { request_id: stockA, error: { code: 'unavailable', message: '合成分钟缓存读取失败', details: [] } } });
    const interval = query.searchParams.get('interval') ?? '1d';
    const day = query.searchParams.get('day') ?? '2020-01-03';
    const id = query.searchParams.get('instrument_id') ?? stockA;
    const daily = interval === '1d';
    return route.fulfill({ json: { meta, data: { instrument: identity(id), interval, adjustment: query.searchParams.get('adjustment'), observation_day: day,
      source_id: query.searchParams.get('source_id') ?? sourceA, source_name: '合成分钟源甲（非真实行情）', source_family: 'synthetic-minute', historical_reconstruction: true,
      state: state.mode === 'denied' ? 'permission_missing' : state.mode === 'empty' ? 'empty' : state.mode === 'stale' ? 'stale' : 'available', reason: 'synthetic_fixture_only',
      last_success_at: `${day}T02:05:00Z`, last_attempt: null, latest_source_time: `${day}T10:02:00+08:00`, age_seconds: 180, freshness_limit_seconds: 120,
      timestamp_semantics: 'unknown', finality: 'unknown', data_mode: daily ? 'saved_daily' : 'source_minutes',
      current_daily: daily ? { ...bar(day, '1d', '10:02', '12.5', 'forming'), projected_from_quote: true } : null,
      current_daily_source_name: '合成实时日线源（非真实行情）',
      items: state.mode === 'empty' || state.mode === 'denied' ? [] : daily ? [bar('2020-01-02', '1d', '15:00')] : [bar(day, interval, '10:00'), bar(day, interval, '10:01', id === stockB ? '11.25' : '12.25')],
    } } });
  });
  return state;
}
async function openMinute(page: Page) {
  await page.goto('/workbench?legacy=1&instrument=' + stockA); await signIn(page);
  await page.getByLabel('分钟K周期', { exact: true }).selectOption('1分钟');
  await expect(page.getByRole('img', { name: '1分钟真实蜡烛图', exact: true })).toBeVisible();
}
const chart = (page: Page) => page.getByRole('region', { name: '行情图表', exact: true });

test('synthetic minute chain exposes source clock, unknown units and finality without inventing closed bars', async ({ page }, info) => {
  const state = await synthetic(page); await openMinute(page);
  await expect(chart(page)).toContainText('合成分钟源甲（非真实行情）');
  await expect(chart(page).locator('.candle-readout')).toContainText('收 12.25 元');
  await expect(chart(page).locator('.candle-readout')).toContainText('量 0（原单位未核）');
  await expect(chart(page).locator('.candle-readout')).toContainText('额 未知');
  await expect(chart(page)).toContainText('时标语义：未核实；不推定周期结束');
  await expect(chart(page)).toContainText('尾柱不标已完成');
  await expect(chart(page).locator('[data-period-state="unknown"]')).toHaveCount(2);
  await page.getByLabel('分钟行情日期', { exact: true }).fill('2020-01-03');
  await expect(chart(page).locator('.candle-readout')).toContainText('2020/1/3');
  await page.getByLabel('分钟行情来源', { exact: true }).selectOption(sourceA);
  await expect.poll(() => state.calls.at(-1)?.searchParams.get('source_id')).toBe(sourceA);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await chart(page).scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('synthetic-minute-' + width + '.png'), animations: 'disabled' });
  }
  await page.getByRole('radio', { name: '分时', exact: true }).click();
  await expect(page.getByRole('img', { name: '真实1分钟收盘价分时图', exact: true })).toBeVisible();
  for (const period of ['5分钟', '15分钟', '30分钟', '60分钟']) {
    await page.getByLabel('分钟K周期', { exact: true }).selectOption(period);
    await expect(page.getByRole('img', { name: period + '真实蜡烛图', exact: true })).toBeVisible();
    await expect(page.getByLabel('分钟行情来源').getByRole('option', { name: '合成分钟源乙', exact: true })).toHaveCount(0);
  }
});

test('refresh errors preserve dated minutes while empty results and permission withdrawal clear them', async ({ page }) => {
  const state = await synthetic(page); await openMinute(page);
  const refresh = chart(page).getByRole('button', { name: '刷新本机分钟缓存', exact: true });
  state.mode = 'stale'; await refresh.click(); await expect(chart(page)).toContainText('行情已过期');
  state.mode = 'empty'; await refresh.click(); await expect(chart(page)).toContainText('尚无该日分钟行情');
  await expect(chart(page).locator('.candle-readout')).toHaveCount(0);
  state.mode = 'filled'; await refresh.click(); await expect(chart(page).locator('.candle-readout')).toContainText('12.25');
  state.mode = 'error'; await refresh.click();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成分钟缓存读取失败' });
  await expect(notice).toBeVisible();
  await expect(chart(page).locator('.candle-readout')).toContainText('12.25');
  await expect(chart(page)).toContainText('更新失败，保留已保存行情');
  await expect(chart(page)).not.toContainText('缓存已核对');
  state.mode = 'filled'; await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(notice).toHaveCount(0);
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(chart(page).locator('.candle-readout')).toContainText('12.25');
  state.mode = 'denied'; await refresh.click(); await expect(chart(page)).toContainText('使用范围未核实');
  await expect(chart(page).locator('.candle-readout')).toHaveCount(0);
});

test('incomplete unrelated source status cannot crash the workspace or invent a minute provider', async ({ page }) => {
  await synthetic(page);
  await page.route('**/api/v1/data-sources', route => route.fulfill({ json: { meta, data: {
    items: [{ family: 'synthetic-unrelated-breadth', collection_state: 'error' }], available_adapters: [], collection_enabled: false,
  } } }));
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  await openMinute(page);
  await expect(chart(page)).toContainText('来源状态返回不完整');
  await expect(chart(page).locator('.candle-readout')).toContainText('12.25');
  await expect(page.getByLabel('分钟行情来源').getByRole('option')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '退出', exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('late response cannot cross minute date or interval request boundaries', async ({ page }) => {
  const state = await synthetic(page); await openMinute(page);
  let release!: () => void; const gate = new Promise<void>(resolve => { release = resolve; });
  let held = false;
  state.defer = async route => { held = true; await gate; return route.fulfill({ json: { meta, data: { instrument: identity(stockA), interval: '1m', observation_day: '2020-01-01', source_id: sourceA, source_name: '迟到合成旧数据', items: [bar('2020-01-01', '1m', '09:31', '999')], adjustment: 'unadjusted', state: 'available' } } }); };
  await page.getByLabel('分钟行情日期', { exact: true }).fill('2020-01-01');
  await expect.poll(() => held).toBe(true);
  await expect(chart(page).locator('.candle-readout')).toHaveCount(0);
  state.defer = null;
  await page.getByLabel('分钟K周期', { exact: true }).selectOption('5分钟');
  await page.getByLabel('分钟行情日期', { exact: true }).fill('2020-01-02');
  await expect(chart(page).locator('.candle-readout')).toContainText('2020/1/2');
  release(); await expect(chart(page)).not.toContainText('迟到合成旧数据');
  await expect(chart(page).locator('.candle-readout')).not.toContainText('999');
  await expect(page.getByRole('img', { name: '5分钟真实蜡烛图', exact: true })).toBeVisible();
});

test('daily real OHLC projection is marked forming and daily, weekly and monthly remain separate from minute data', async ({ page }) => {
  const state = await synthetic(page); await openMinute(page);
  await page.getByRole('radio', { name: '日K', exact: true }).click();
  await expect(page.getByRole('img', { name: '日K历史蜡烛图', exact: true })).toBeVisible();
  await expect(chart(page)).toContainText('当前日柱来自实时日线原生OHLC');
  await expect(chart(page).locator('.candle-readout')).toContainText('形成中');
  await expect(chart(page)).toContainText('合成实时日线源（非真实行情）');
  expect(state.calls.at(-1)?.searchParams.get('include_live')).toBe('true');
  await page.getByLabel('K线复权口径').selectOption('forward');
  await expect.poll(() => state.calls.at(-1)?.searchParams.get('adjustment')).toBe('forward');
  for (const period of ['周K', '月K']) {
    await page.getByRole('radio', { name: period, exact: true }).click();
    await expect(page.getByRole('img', { name: period + '历史蜡烛图', exact: true })).toBeVisible();
    await expect(chart(page)).toContainText('首尾周期可能不完整');
  }
});

test('late response cannot leak the previously selected stock after a search selection', async ({ page }) => {
  const state = await synthetic(page); await openMinute(page);
  let release!: () => void; const gate = new Promise<void>(resolve => { release = resolve; });
  let held = false;
  state.defer = async route => {
    held = true; await gate;
    await route.fulfill({ json: { meta, data: { instrument: identity(stockA), interval: '1m', observation_day: '2020-01-01', source_id: sourceA, source_name: '迟到甲标的资料', adjustment: 'unadjusted', state: 'available', items: [bar('2020-01-01', '1m', '09:31', '999')] } } });
  };
  await page.getByLabel('分钟行情日期', { exact: true }).fill('2020-01-01');
  await expect.poll(() => held).toBe(true);
  await page.route('**/api/v1/instruments?*', route => route.fulfill({ json: { meta, data: { items: [identity(stockB)] } } }));
  state.defer = null;
  await page.getByRole('textbox', { name: '搜索标的', exact: true }).fill('合成分钟乙');
  await page.getByRole('button', { name: '搜索标的', exact: true }).click();
  await page.getByRole('region', { name: '标的查询结果' }).getByRole('link', { name: /合成分钟乙/ }).click();
  await page.getByLabel('分钟K周期', { exact: true }).selectOption('1分钟');
  await expect(chart(page).locator('.candle-readout')).toContainText('收 11.25 元');
  release(); await expect(chart(page)).not.toContainText('迟到甲标的资料');
  await expect(chart(page).locator('.candle-readout')).not.toContainText('999');
  await expect.poll(() => state.calls.at(-1)?.searchParams.get('instrument_id')).toBe(stockB);
});
