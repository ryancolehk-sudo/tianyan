import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

// All sessions, jobs, collection receipts and bars below are isolated synthetic
// fixtures. They do not sign in to a real account or request any market supplier.
const stock = '00000000-0000-4000-8000-000000000101';
const source = '00000000-0000-4000-8000-000000000102';
const job = '00000000-0000-4000-8000-000000000103';
const runId = '00000000-0000-4000-8000-000000000104';
const user = '00000000-0000-4000-8000-000000000105';
const otherSource = '00000000-0000-4000-8000-000000000106';
const dailyJob = '00000000-0000-4000-8000-000000000107';
const ordinaryJob = '00000000-0000-4000-8000-000000000108';
const otherStock = '00000000-0000-4000-8000-000000000109';
const day = '2026-10-23';
const csrf = 'synthetic-csrf-not-a-secret';
const identity = { id: stock, exchange: 'SSE', symbol: '600001', asset_type: 'stock', name: '合成补采测试，非真实行情' };
const meta = { app_mode: 'demo', request_id: stock, generated_at: '2026-10-24T02:00:00Z', as_of_time: null,
  quality: { status: 'partial', reasons: ['Synthetic backfill UI fixture only'] } };
async function synthetic(page: Page, role: 'admin' | 'viewer' = 'admin') {
  const state = { enabled: true, supported: true, collection: true, postMode: 'queued' as 'queued' | 'conflict' | 'uncovered',
    jobState: 'queued' as 'queued' | 'succeeded', privateJob: false, runState: 'complete' as 'complete' | 'partial' | 'empty',
    posts: [] as { path: string; body: unknown; csrf?: string; key?: string }[], cacheGets: 0, queriedJobs: [] as string[],
    deferPost: null as null | Promise<void>, deferRun: null as null | Promise<void>, runStarted: false, runFinished: false,
    runDay: day, wrongJob: false, runMode: 'backfill' as 'backfill' | 'daily_history' | 'observed',
    wrongSource: false, mixedCalls: false, cacheQueries: [] as URL[] };
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname === '/api/v1/session') return route.fulfill({ json: { meta, data: { user: { id: user, username: 'synthetic-backfill', role }, csrf_token: csrf } } });
    if (url.pathname === '/api/v1/instruments') return route.fulfill({ json: { meta, data: { items: [{ ...identity, id: otherStock, symbol: '600002', name: '合成切换乙（非真实行情）' }] } } });
    if (url.pathname === '/api/v1/market-quotes') return route.fulfill({ json: { meta, data: { selection: 'instrument', items: [{ instrument: { ...identity, id: url.searchParams.get('instrument_id') ?? stock }, state: 'not_configured', freshness_limit_seconds: 60 }] } } });
    if (url.pathname === '/api/v1/data-sources') return route.fulfill({ json: { meta, data: { collection_enabled: state.collection, available_adapters: [],
      items: [source, otherSource].map(id => ({ family: 'synthetic-backfill', provider: { id, name: id === source ? '合成补采源甲' : '合成补采源乙',
        configured: true, enabled: state.enabled, entitlement_status: 'verified', capabilities: ['bar.minute', 'bar.1m', 'bar.5m'],
        rights: { display: 'allowed', cache: 'allowed', export: 'denied', redistribute: 'denied' } }, collection_enabled: state.collection,
        paused_reason: null, cooldown_until: null, last_attempt: null, last_success_at: null })) } } });
    if (url.pathname === '/api/v1/market-bars') {
      state.cacheGets++; state.cacheQueries.push(url);
      const selectedDay = url.searchParams.get('day') ?? day;
      return route.fulfill({ json: { meta, data: { instrument: { ...identity, id: url.searchParams.get('instrument_id') ?? stock }, interval: url.searchParams.get('interval') ?? '1d', adjustment: 'unadjusted',
        observation_day: selectedDay, source_id: url.searchParams.get('source_id') ?? source, source_name: '合成补采源甲', items: [],
        state: state.enabled ? 'empty' : 'disabled', backfill_supported: state.supported, backfill_lookback_days: 7, data_mode: 'source_minutes' } } });
    }
    if (url.pathname.endsWith('/refresh') && request.method() === 'POST') {
      state.posts.push({ path: url.pathname, body: request.postDataJSON(), csrf: request.headers()['x-csrf-token'], key: request.headers()['idempotency-key'] });
      if (state.deferPost) await state.deferPost;
      if (state.postMode !== 'queued') return route.fulfill({ status: state.postMode === 'conflict' ? 409 : 422, json: { request_id: stock,
        error: { code: state.postMode === 'conflict' ? 'idempotency_conflict' : 'backfill_uncovered', message: state.postMode === 'conflict' ? '合成：相同幂等键对应不同补采意图' : '合成：来源未覆盖所选日期', details: [] } } });
      return route.fulfill({ status: 202, json: { meta, data: { job_id: job, source_id: source, requested_day: day, acquisition_mode: 'backfill', state: 'queued' } } });
    }
    if (url.pathname === '/api/v1/jobs/' + job) {
      if (state.privateJob) return route.fulfill({ status: 404, json: { request_id: stock, error: { code: 'not_found', message: '合成任务当前不可读取', details: [] } } });
      return route.fulfill({ json: { meta, data: { id: job, kind: 'source.collect', state: state.jobState, attempts: 1, result_ref: null, error_code: null } } });
    }
    if (url.pathname === '/api/v1/data-fetch-runs') {
      state.queriedJobs.push(url.searchParams.get('job_id') ?? '');
      if (url.searchParams.has('job_id')) { expect(url.searchParams.get('source_id')).toBe(source); expect(url.searchParams.get('job_id')).toBe(job); }
      const run = { id: runId, job_id: state.wrongJob ? stock : job, source_id: state.wrongSource ? otherSource : source,
        requested_day: state.runDay, acquisition_mode: state.runMode, capability: state.runMode === 'daily_history' ? 'quote.eod+bar.daily' : 'bar.minute+bar.1m',
        status: state.runState, reason: state.runState === 'partial' ? 'backfill_conflict' : null,
        accepted: 1, rejected: state.runState === 'partial' ? 1 : 0, duplicates: 2, expected: 4,
        elapsed_ms: 1, cost_cny: null, request_count: 1, received_at: '2026-10-24T02:00:00Z' };
      const items = state.mixedCalls && !url.searchParams.has('job_id') ? [
        { ...run, id: runId + '-daily', job_id: dailyJob, capability: 'bar.daily', acquisition_mode: 'daily_history',
          requested_day: null, requested_instrument_code: '600002.SH', requested_start_day: '2021-10-24', requested_end_day: '2026-10-24',
          status: 'partial', reason: 'history_page_limit_reached' },
        { ...run, id: runId + '-backfill', acquisition_mode: 'backfill' },
        { ...run, id: runId + '-ordinary', job_id: ordinaryJob, acquisition_mode: 'observed', requested_day: null },
      ] : [run];
      const body = { meta, data: { items, next_cursor: null } };
      const deferred = state.deferRun; state.deferRun = null;
      if (deferred && url.searchParams.has('job_id')) { state.runStarted = true; await deferred; }
      await route.fulfill({ json: body });
      if (deferred) state.runFinished = true;
      return;
    }
    return route.fulfill({ status: 503, json: { request_id: stock, error: { code: 'synthetic_only', message: '合成隔离测试：其他缓存未接入', details: [] } } });
  });
  return state;
}
const chart = (page: Page) => page.getByRole('region', { name: '行情图表', exact: true });
const panel = (page: Page) => page.getByRole('complementary', { name: '分钟历史补采', exact: true });
async function openManagement(page: Page) {
  const details = chart(page).locator('.chart-data-details');
  if (!await details.evaluate(node => (node as HTMLDetailsElement).open)) await details.locator(':scope > summary').click();
}
async function open(page: Page) {
  await page.clock.setFixedTime(new Date('2026-10-24T02:00:00Z'));
  await page.goto('/workbench?legacy=1&instrument=' + stock);
  await page.getByLabel('分钟K周期', { exact: true }).selectOption('1分钟');
  await chart(page).getByLabel('分钟行情日期').fill(day);
  await chart(page).getByLabel('分钟行情来源').selectOption(source);
  await expect(chart(page).locator('.minute-chart-body')).toHaveAttribute('aria-busy', 'false');
}

test('viewer, unsupported sources and disabled collection cannot submit; cache refresh stays a GET', async ({ page }) => {
  const state = await synthetic(page, 'viewer'); await open(page);
  await expect(panel(page)).toHaveCount(0);
  const before = state.cacheGets; await chart(page).getByRole('button', { name: '刷新本机分钟缓存', exact: true }).click();
  await expect.poll(() => state.cacheGets).toBeGreaterThan(before); expect(state.posts).toEqual([]);
});

test('admin disabled or out-of-range source does not write and capability controls the entry', async ({ page }) => {
  const state = await synthetic(page); state.collection = false; await open(page);
  await openManagement(page);
  await expect(panel(page).getByRole('button', { name: '补采所选日期', exact: true })).toBeDisabled();
  state.collection = true; state.enabled = false; await page.reload();
  await page.getByLabel('分钟K周期', { exact: true }).selectOption('1分钟');
  await chart(page).getByLabel('分钟行情日期').fill(day);
  await openManagement(page);
  await expect(panel(page).getByRole('button', { name: '补采所选日期', exact: true })).toBeDisabled();
  state.enabled = true; state.supported = false; await chart(page).getByRole('button', { name: '刷新本机分钟缓存', exact: true }).click();
  await expect(panel(page)).toHaveCount(0); expect(state.posts).toEqual([]);
});

test('request binds source and day, CSRF/idempotency and queue receipt; exact job result refreshes cache without claiming full coverage', async ({ page }) => {
  const state = await synthetic(page); await open(page);
  await openManagement(page);
  await panel(page).getByRole('button', { name: '补采所选日期', exact: true }).click();
  await expect.poll(() => state.posts.length).toBe(1);
  expect(state.posts[0]).toMatchObject({ path: '/api/v1/data-sources/' + source + '/refresh', body: { day }, csrf });
  expect(state.posts[0].key).toMatch(/^[a-f0-9-]{36}$/);
  await openManagement(page);
  await expect(panel(page)).toContainText('排队不等于成功'); await expect(panel(page)).toContainText(job);
  await openManagement(page);
  await expect(panel(page).getByRole('button', { name: '补采所选日期', exact: true })).toBeDisabled();
  state.jobState = 'succeeded'; state.runState = 'partial';
  await openManagement(page);
  const before = state.cacheGets; await panel(page).getByRole('button', { name: '检查补采任务', exact: true }).click();
  await openManagement(page);
  await expect(panel(page)).toContainText('来源部分返回'); await expect(panel(page)).toContainText('未覆盖原有记录');
  await expect.poll(() => state.cacheGets).toBeGreaterThan(before);
  expect(state.queriedJobs).toContain(job); expect(state.posts).toHaveLength(1);
});

test('409 intent conflicts and uncovered dates remain explicit and retries reuse the same idempotency intent', async ({ page }) => {
  const state = await synthetic(page); state.postMode = 'conflict'; await open(page);
  await openManagement(page);
  const submit = panel(page).getByRole('button', { name: '补采所选日期', exact: true });
  await openManagement(page);
  await submit.click(); await expect(panel(page).getByRole('alert')).toContainText('不同补采意图');
  await submit.click(); await expect.poll(() => state.posts.length).toBe(2);
  expect(state.posts[1].key).toBe(state.posts[0].key);
  state.postMode = 'uncovered'; await submit.click();
  await openManagement(page);
  await expect(panel(page).getByRole('alert')).toContainText('来源未覆盖所选日期');
  expect(state.posts.every(item => JSON.stringify(item.body) === JSON.stringify({ day }))).toBe(true);
});

test('a delayed queued receipt is isolated after changing source, date or period', async ({ page }) => {
  const state = await synthetic(page); await open(page);
  let release!: () => void; state.deferPost = new Promise<void>(resolve => { release = resolve; });
  await openManagement(page);
  await panel(page).getByRole('button', { name: '补采所选日期', exact: true }).click();
  await expect.poll(() => state.posts.length).toBe(1);
  await chart(page).getByLabel('分钟行情日期').fill('2026-10-22');
  await chart(page).getByLabel('分钟行情来源').selectOption(otherSource);
  await page.getByLabel('分钟K周期', { exact: true }).selectOption('5分钟');
  release(); await expect(chart(page).locator('.minute-chart-body')).toHaveAttribute('aria-busy', 'false');
  await openManagement(page);
  await expect(panel(page)).not.toContainText(job); await expect(panel(page)).not.toContainText('排队不等于成功');
  expect(state.posts).toHaveLength(1);
});

test('private generic job state does not masquerade as failure and foreign run receipts are ignored', async ({ page }) => {
  const state = await synthetic(page); state.privateJob = true; state.wrongJob = true; await open(page);
  await openManagement(page);
  await panel(page).getByRole('button', { name: '补采所选日期', exact: true }).click();
  await openManagement(page);
  await expect(panel(page)).toContainText('当前账号不能读取任务状态');
  await expect(panel(page)).not.toContainText('实际结果：');
  state.wrongJob = false; state.runState = 'empty';
  await openManagement(page);
  await panel(page).getByRole('button', { name: '检查补采任务', exact: true }).click();
  await openManagement(page);
  await expect(panel(page)).toContainText('来源未覆盖所选日期');
  await openManagement(page);
  await expect(panel(page)).toContainText('实际结果：无数据');
});


test('dates beyond seven natural days or in the future cannot issue a backfill request', async ({ page }) => {
  const state = await synthetic(page); await open(page);
  await chart(page).getByLabel('分钟行情日期').fill('2026-10-16');
  await openManagement(page);
  await expect(panel(page)).toContainText('日期超出本机安全补采范围');
  await openManagement(page);
  await expect(panel(page).getByRole('button', { name: '补采所选日期', exact: true })).toBeDisabled();
  await chart(page).getByLabel('分钟行情日期').fill('2026-10-25');
  await openManagement(page);
  await expect(panel(page).getByRole('button', { name: '补采所选日期', exact: true })).toBeDisabled();
  expect(state.posts).toEqual([]);
});

test('the data center exposes the actual collection purpose, requested date and task identity', async ({ page }) => {
  await synthetic(page); await page.clock.setFixedTime(new Date('2026-10-24T02:00:00Z'));
  await page.goto('/data?view=calls');
  const table = page.getByRole('region', { name: '来源调用记录表格', exact: true });
  await expect(table.getByRole('columnheader').filter({ hasText: /用途|目标日期/ })).toHaveCount(1);
  await expect(table.locator('tbody tr').first()).toContainText(day);
  await expect(table.locator('tbody tr').first()).toContainText(job);
  await expect(table.locator('tbody tr').first()).toContainText('补录');
});


test('mixed daily-history, minute-backfill and ordinary calls retain distinct purposes, target dates and all job IDs', async ({ page }) => {
  const state = await synthetic(page); state.mixedCalls = true;
  await page.goto('/data?view=calls');
  const table = page.getByRole('region', { name: '来源调用记录表格', exact: true });
  await expect(table.locator('tbody tr')).toHaveCount(3);
  const daily = table.locator('tbody tr').filter({ hasText: dailyJob });
  await expect(daily).toContainText('按需日K');
  await expect(daily).toContainText('600002.SH');
  await expect(daily).toContainText('2021-10-24 至 2026-10-24');
  await expect(daily).toContainText('历史窗口达到单次行数上限，未证明完整');
  await expect(daily).not.toContainText('分钟补录'); await expect(daily).not.toContainText('常规采集');
  const minute = table.locator('tbody tr').filter({ hasText: job });
  await expect(minute).toContainText('分钟补录'); await expect(minute).toContainText(day);
  await expect(minute).not.toContainText('按需日K');
  const ordinary = table.locator('tbody tr').filter({ hasText: ordinaryJob });
  await expect(ordinary).toContainText('常规采集'); await expect(ordinary).toContainText('随实际响应日期');
  await expect(ordinary).not.toContainText('分钟补录'); await expect(ordinary).not.toContainText('按需日K');
  expect(state.posts).toEqual([]);
});

test('matching job ID cannot substitute a daily-history, ordinary, foreign-source or foreign-date result for a minute backfill', async ({ page }) => {
  // Control the real polling timer. Every invalid result must finish returning
  // before the next fixture changes, and completion must not race a manual click.
  await page.clock.install({ time: new Date('2026-10-24T02:00:00Z') });
  const state = await synthetic(page); state.privateJob = true; state.runMode = 'daily_history'; await open(page);
  await page.clock.pauseAt(new Date('2026-10-24T02:00:10Z'));
  async function returned(action: () => Promise<unknown>, expected: Record<string, string>) {
    const received = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === '/api/v1/data-fetch-runs' && url.searchParams.get('source_id') === source && url.searchParams.get('job_id') === job;
    });
    await action();
    const response = await received;
    expect(response.status()).toBe(200);
    const body = await response.json() as { data: { items: Record<string, unknown>[] } };
    expect(body.data.items).toHaveLength(1);
    expect(body.data.items[0]).toMatchObject(expected);
    await response.finished();
    // Flush the browser task/render after this exact response was consumed.
    await page.clock.runFor(100);
  }
  async function rejected() {
  await openManagement(page);
    await expect(panel(page)).toContainText('当前账号不能读取任务状态');
  await openManagement(page);
    await expect(panel(page).getByRole('button', { name: '检查补采任务', exact: true })).toBeVisible();
    await expect(panel(page)).not.toContainText('实际结果：');
  }
  await openManagement(page);
  await returned(() => panel(page).getByRole('button', { name: '补采所选日期', exact: true }).click(),
    { job_id: job, source_id: source, acquisition_mode: 'daily_history', requested_day: day });
  await rejected();
  state.runMode = 'observed';
  await returned(() => page.clock.runFor(3000), { job_id: job, source_id: source, acquisition_mode: 'observed', requested_day: day });
  await rejected();
  state.runMode = 'backfill'; state.wrongSource = true;
  await returned(() => page.clock.runFor(3000), { job_id: job, source_id: otherSource, acquisition_mode: 'backfill', requested_day: day });
  await rejected();
  state.wrongSource = false; state.runDay = '2026-10-22';
  await returned(() => page.clock.runFor(3000), { job_id: job, source_id: source, acquisition_mode: 'backfill', requested_day: '2026-10-22' });
  await rejected();
  state.runDay = day; state.runState = 'partial';
  // Let the next scheduled poll accept the valid result; the pending button is
  // correctly removed on completion and is not a target for a racing click.
  await returned(() => page.clock.runFor(3000), { job_id: job, source_id: source, acquisition_mode: 'backfill', requested_day: day, status: 'partial' });
  await openManagement(page);
  await expect(panel(page)).toContainText('实际结果：部分返回');
  await expect(panel(page).getByRole('button', { name: '检查补采任务', exact: true })).toHaveCount(0);
  expect(state.posts).toHaveLength(1);
});

test('late task-result reads cannot cross the selected stock or turn daily history into minute success', async ({ page }) => {
  const state = await synthetic(page); state.privateJob = true; await open(page);
  let release!: () => void; state.deferRun = new Promise<void>(resolve => { release = resolve; });
  await openManagement(page);
  await panel(page).getByRole('button', { name: '补采所选日期', exact: true }).click();
  await expect.poll(() => state.runStarted).toBe(true);
  await page.getByRole('textbox', { name: '搜索标的', exact: true }).fill('合成切换乙');
  await page.getByRole('button', { name: '搜索标的', exact: true }).click();
  await page.getByRole('region', { name: '标的查询结果' }).getByRole('link', { name: /合成切换乙/ }).click();
  await page.getByLabel('分钟K周期', { exact: true }).selectOption('1分钟');
  await expect.poll(() => state.cacheQueries.at(-1)?.searchParams.get('instrument_id')).toBe(otherStock);
  release(); await expect.poll(() => state.runFinished).toBe(true);
  await expect(chart(page)).not.toContainText(job);
  await expect(chart(page)).not.toContainText('实际结果：');
  await page.getByRole('radio', { name: '日K', exact: true }).click();
  await expect(panel(page)).toHaveCount(0);
  await expect(chart(page)).not.toContainText('实际结果：');
  expect(state.posts).toHaveLength(1);
});

test('latest-saved minute mode binds a backfill to its displayed actual date without pinning automatic cache reads', async ({ page }) => {
  const state = await synthetic(page); await page.clock.setFixedTime(new Date('2026-10-24T02:00:00Z'));
  await page.goto('/workbench?legacy=1&instrument=' + stock);
  await page.getByLabel('分钟K周期', { exact: true }).selectOption('1分钟');
  await expect(chart(page).getByLabel('分钟行情日期')).toHaveValue(day);
  expect(state.cacheQueries.at(-1)?.searchParams.has('day')).toBe(false);
  await openManagement(page);
  await panel(page).getByRole('button', { name: '补采所选日期', exact: true }).click();
  await expect.poll(() => state.posts.length).toBe(1);
  expect(state.posts[0].body).toEqual({ day });
  expect(state.posts[0].path).toBe('/api/v1/data-sources/' + source + '/refresh');
  await chart(page).getByRole('button', { name: '刷新本机分钟缓存', exact: true }).click();
  await expect(chart(page).locator('.minute-chart-body')).toHaveAttribute('aria-busy', 'false');
  expect(state.cacheQueries.at(-1)?.searchParams.has('day')).toBe(false);
  expect(state.posts).toHaveLength(1);
});
