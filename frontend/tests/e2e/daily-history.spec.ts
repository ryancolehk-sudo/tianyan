import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

// Synthetic browser-only identities and prices. No supplier, account or DB.
const stock = '00000000-0000-4000-8000-000000000081';
const source = '00000000-0000-4000-8000-000000000082';
const jobId = '00000000-0000-4000-8000-000000000083';
const identity = { id: stock, exchange: 'SSE', symbol: '600001', asset_type: 'stock', name: '合成历史股票（测试）' };
const meta = { request_id: stock, app_mode: 'demo', generated_at: '2020-01-03T08:00:00Z', as_of_time: null,
  quality: { status: 'partial', reasons: ['Synthetic daily-history browser fixture only'] } };
function rows(count = 250, year = 2000) {
  return Array.from({ length: count }, (_, index) => {
    const stamp = new Date(Date.UTC(year, 0, 1 + index)), day = stamp.toISOString().slice(0, 10);
    return { id: `${year}-${index}`, content_hash: '2'.repeat(64), received_at: '2020-01-03T08:00:00Z',
      payload: { interval: '1d', adjustment: 'unadjusted', trade_day: day, price_unit: 'CNY',
        open: '10', high: '13', low: '9', close: String(10 + index % 3), volume_shares: null, amount_cny: null,
        source_clock_at: null, bar_end_at: null } };
  });
}
async function synthetic(page: Page, options: { empty?: boolean; viewer?: boolean; more?: boolean } = {}) {
  const state = { rows: options.empty ? [] : rows(), completed: false, failed: false, submitted: null as null | { instrument_id: string; start_day: string; end_day: string },
    writes: [] as { url: string; body: Record<string, unknown>; csrf?: string; key?: string }[], cacheReads: [] as URL[], pollCount: 0,
    runMode: 'daily_history', runCode: '600001.SH', runWrongRange: false, jobWrongId: false, jobRunning: false, forceRun: false, runStatus: 'complete' };
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname === '/api/v1/session') return route.fulfill({ json: { meta, data: {
      user: { id: stock, username: 'synthetic-history', role: options.viewer ? 'viewer' : 'admin' }, csrf_token: 'synthetic-not-a-credential',
    } } });
    if (url.pathname === '/api/v1/market-quotes') return route.fulfill({ json: { meta, data: { selection: 'instrument',
      items: [{ instrument: identity, state: 'not_configured', freshness_limit_seconds: 60 }] } } });
    if (url.pathname === '/api/v1/data-sources') return route.fulfill({ json: { meta, data: { items: [], available_adapters: [], collection_enabled: true } } });
    if (url.pathname === '/api/v1/market-history') {
      const body = request.postDataJSON();
      state.writes.push({ url: url.pathname, body, csrf: request.headers()['x-csrf-token'], key: request.headers()['idempotency-key'] });
      state.submitted = body;
      return route.fulfill({ status: 202, json: { meta, data: { job_id: jobId, source_id: source, state: 'queued',
        instrument_id: body.instrument_id, requested_start_day: body.start_day, requested_end_day: body.end_day } } });
    }
    if (url.pathname === '/api/v1/jobs/' + jobId) {
      state.pollCount++;
      return route.fulfill({ json: { meta, data: { id: state.jobWrongId ? source : jobId, kind: 'source.collect', state: state.failed ? 'failed' : state.jobRunning ? 'running' : state.completed ? 'succeeded' : 'queued', attempts: 1, result_ref: null, error_code: state.failed ? 'transport_error' : null } } });
    }
    if (url.pathname === '/api/v1/data-fetch-runs') {
      expect(url.searchParams.get('job_id')).toBe(jobId);
      expect(url.searchParams.get('source_id')).toBe(source);
      if (state.completed && !state.failed) state.rows = rows();
      return route.fulfill({ json: { meta, data: { next_cursor: null, items: !state.completed && !state.forceRun ? [] : [{ id: 'synthetic-run',
        source_id: source, job_id: jobId, capability: 'bar.daily', status: state.failed ? 'error' : state.runStatus, reason: null,
        acquisition_mode: state.runMode, requested_instrument_code: state.runCode,
        requested_start_day: state.runWrongRange ? '1990-01-01' : state.submitted?.start_day, requested_end_day: state.submitted?.end_day,
        received_at: meta.generated_at, expected: 251, accepted: 251, duplicates: 0, rejected: 0, request_count: 1, elapsed_ms: 1, cost_cny: null }] } } });
    }
    if (url.pathname === '/api/v1/market-bars') {
      state.cacheReads.push(url);
      const minute = url.searchParams.has('interval') && url.searchParams.get('interval') !== '1d';
      const adjustment = url.searchParams.get('adjustment') ?? 'unadjusted';
      const before = url.searchParams.get('before');
      const items = minute || adjustment !== 'unadjusted' ? [] : before ? rows(50, 1990) : state.rows;
      return route.fulfill({ json: { meta, data: { instrument: identity, source_id: source, source_family: 'tushare_daily',
        source_name: '合成历史来源（非真实行情）', adjustment, interval: minute ? '1m' : '1d', items,
        state: items.length ? 'historical' : 'empty', current_daily: null, requested_before: before,
        has_more: Boolean(options.more && !before), history_supported: adjustment === 'unadjusted', history_source_id: adjustment === 'unadjusted' ? source : null,
        history_reason: adjustment !== 'unadjusted' ? 'history_adjustment_unsupported' : null,
        coverage_start_day: items.length ? (options.more ? '1990-01-01' : state.rows[0].payload.trade_day) : null,
        coverage_end_day: state.rows.at(-1)?.payload.trade_day ?? null } } });
    }
    return route.fulfill({ status: 503, json: { request_id: stock, error: { code: 'synthetic_unavailable', message: '合成隔离测试：其他缓存未配置', details: [] } } });
  });
  return state;
}
const chart = (page: Page) => page.getByRole('region', { name: '行情图表', exact: true });
async function openManagement(page: Page) {
  const details = chart(page).locator('.chart-data-details');
  if (!await details.evaluate(node => (node as HTMLDetailsElement).open)) await details.locator(':scope > summary').click();
}
async function open(page: Page) {
  await page.goto('/workbench?legacy=1&instrument=' + stock);
  await page.getByRole('radio', { name: '日K', exact: true }).click();
  await openManagement(page);
  await expect(chart(page).locator('.daily-history-controls')).toBeVisible();
}

test('selected empty stock queues one authenticated bounded history task then displays saved K lines', async ({ page }) => {
  const state = await synthetic(page, { empty: true });
  await open(page);
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0].body.instrument_id).toBe(stock);
  expect(state.writes[0].csrf).toBe('synthetic-not-a-credential');
  expect(state.writes[0].key).toBeTruthy();
  expect((new Date(state.writes[0].body.end_day as string).getTime() - new Date(state.writes[0].body.start_day as string).getTime()) / 86400000).toBeLessThanOrEqual(1830);
  await expect(chart(page).getByText(/历史采集已排队/)).toBeVisible();
  state.completed = true;
  await expect(chart(page).getByRole('img', { name: '日K历史蜡烛图', exact: true })).toBeVisible();
  await expect(chart(page).locator('[data-bar-clock]')).toHaveCount(80);
  expect(state.writes).toHaveLength(1);
  expect(state.cacheReads.some(url => url.searchParams.get('limit') === '2000')).toBeTruthy();
});

test('cached histories expose all loaded dates through viewport controls instead of discarding older candles', async ({ page }) => {
  const state = await synthetic(page);
  await open(page);
  const plot = chart(page);
  await expect(plot.locator('[data-bar-clock]')).toHaveCount(80);
  const position = plot.getByRole('slider', { name: 'K线历史位置' });
  await position.focus(); await position.press('Home');
  if (!await plot.locator('.chart-data-details').evaluate(node => (node as HTMLDetailsElement).open)) await plot.locator('.chart-data-details > summary').click();
  await expect(plot.getByRole('combobox', { name: '查看K线日期' }).locator('option').first()).toHaveText('2000-01-01');
  await position.press('End');
  await expect(plot.getByRole('combobox', { name: '查看K线日期' }).locator('option').last()).toHaveText(state.rows.at(-1)!.payload.trade_day);
  await plot.getByRole('button', { name: '前一段K线', exact: true }).click();
  await expect(plot.getByRole('button', { name: '后一段K线', exact: true })).toBeEnabled();
  expect(state.writes).toHaveLength(0);
});

test('cache paging requests a strictly older page and can return to latest without supplier writes', async ({ page }) => {
  const state = await synthetic(page, { more: true });
  await open(page);
  await chart(page).getByRole('button', { name: '载入更早缓存', exact: true }).click();
  await openManagement(page);
  await expect(chart(page).getByRole('combobox', { name: '查看K线日期' }).locator('option').first()).toHaveText('1990-01-01');
  expect(state.cacheReads.some(url => url.searchParams.get('before') === '2000-01-01')).toBeTruthy();
  await chart(page).getByRole('button', { name: '回到最近日K', exact: true }).click();
  await expect(chart(page).locator('[data-bar-clock]')).toHaveCount(80);
  expect(state.writes).toHaveLength(0);
});

test('viewer never initiates upstream history acquisition', async ({ page }) => {
  const state = await synthetic(page, { empty: true, viewer: true });
  await open(page);
  await expect(chart(page).getByRole('button', { name: '更新近五年日K', exact: true })).toBeDisabled();
  await expect(chart(page).getByText('当前账号可查看历史；获取新股票历史由管理员执行。')).toBeVisible();
  expect(state.writes).toHaveLength(0);
});

test('failed history acquisition keeps previously saved candles and distinguishes cache refresh', async ({ page }) => {
  const state = await synthetic(page);
  await open(page);
  await chart(page).getByRole('button', { name: '更新近五年日K', exact: true }).click();
  await expect.poll(() => state.writes.length).toBe(1);
  await expect(chart(page).locator('[data-bar-clock]')).toHaveCount(80);
  state.failed = true; state.completed = true;
  await expect(chart(page).getByText(/本次历史采集未完成/)).toBeVisible();
  await expect(chart(page).locator('[data-bar-clock]')).toHaveCount(80);
  await chart(page).getByRole('button', { name: '刷新已存K线', exact: true }).click();
  await expect(chart(page).locator('[data-bar-clock]')).toHaveCount(80);
  expect(state.writes).toHaveLength(1);
});

test('unsupported adjusted history is explicit and does not relabel unadjusted bars', async ({ page }) => {
  const state = await synthetic(page);
  await open(page);
  await page.getByRole('combobox', { name: 'K线复权口径' }).selectOption('forward');
  await openManagement(page);
  await expect(chart(page).getByText('当前按需历史仅支持不复权；前后复权只显示实际已存口径。')).toBeVisible();
  await expect(chart(page).getByRole('button', { name: '更新近五年日K', exact: true })).toBeDisabled();
  await expect(chart(page).locator('[data-bar-clock]')).toHaveCount(0);
  expect(state.writes).toHaveLength(0);
});

for (const mismatch of ['purpose', 'instrument', 'range'] as const) {
  test(`a matching daily job cannot borrow a result with a different ${mismatch}`, async ({ page }) => {
    const state = await synthetic(page);
    if (mismatch === 'purpose') state.runMode = 'backfill';
    if (mismatch === 'instrument') state.runCode = '600002.SH';
    if (mismatch === 'range') state.runWrongRange = true;
    state.completed = true;
    await open(page);
    await chart(page).getByRole('button', { name: '更新近五年日K', exact: true }).click();
    await expect.poll(() => state.pollCount).toBeGreaterThanOrEqual(2);
    await expect(chart(page)).not.toContainText('历史已返回');
    await expect(chart(page).locator('[data-bar-clock]')).toHaveCount(80);
    expect(state.writes).toHaveLength(1);
  });
}

test('a succeeded job with an error run preserves candles and never claims returned history', async ({ page }) => {
  const state = await synthetic(page); state.completed = true; state.runStatus = 'error';
  await open(page);
  await chart(page).getByRole('button', { name: '更新近五年日K', exact: true }).click();
  await expect(chart(page)).toContainText('本次来源调用失败或不可用');
  await expect(chart(page)).not.toContainText('历史已返回');
  await expect(chart(page).locator('[data-bar-clock]')).toHaveCount(80);
  expect(state.writes).toHaveLength(1);
});

test('a retrying history job keeps monitoring its earlier partial receipt until the task is terminal', async ({ page }) => {
  const state = await synthetic(page); state.jobRunning = true; state.forceRun = true; state.runStatus = 'partial';
  await open(page);
  await chart(page).getByRole('button', { name: '更新近五年日K', exact: true }).click();
  await expect.poll(() => state.pollCount).toBeGreaterThanOrEqual(2);
  await expect(chart(page)).not.toContainText('本次窗口仅部分返回');
  await expect(chart(page).getByRole('button', { name: '历史采集中…', exact: true })).toBeDisabled();
  state.jobRunning = false; state.completed = true;
  await expect(chart(page)).toContainText('本次窗口仅部分返回');
  await expect(chart(page).locator('[data-bar-clock]')).toHaveCount(80);
  expect(state.writes).toHaveLength(1);
});

test('foreign task identity cannot claim a correctly shaped daily result', async ({ page }) => {
  const state = await synthetic(page); state.jobWrongId = true; state.completed = true;
  await open(page);
  await chart(page).getByRole('button', { name: '更新近五年日K', exact: true }).click();
  await expect(chart(page)).toContainText('任务身份不一致');
  await expect(chart(page)).not.toContainText('历史已返回');
  await expect(chart(page).locator('[data-bar-clock]')).toHaveCount(80);
  expect(state.writes).toHaveLength(1);
});
