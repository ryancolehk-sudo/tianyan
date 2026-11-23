import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { day, days, blank, listing, theme } from './reviewTestFixtures';
import { terminalFixture } from './terminalTestFixtures';
import { openNotifications } from './isolated-browser.fixtures';

async function workspace(page: Page, signedIn = true) {
  const state = { signedIn, owner: 'synthetic-a', failSeries: false, holdSeries: false, expireOnStatusCheck: false, deniedReads: 0,
    delay: 90, requests: [] as string[], active: 0, maxActive: 0, peakPaths: [] as string[], errors: [] as string[] };
  const activePaths = new Map<symbol, string>();
  let release: (() => void) | undefined;
  const counts = (needle: string) => state.requests.filter(item => item.includes(needle)).length;
  page.on('pageerror', error => state.errors.push(error.message));
  await page.route('**/api/v1/**', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    state.requests.push(request.method() + ' ' + path + url.search);
    if (path === '/api/v1/session' && request.method() === 'POST') state.signedIn = true;
    if (path === '/api/v1/session' && request.method() === 'DELETE') { state.signedIn = false; return route.fulfill({ status: 204 }); }
    if (path === '/api/v1/system/overview' && state.expireOnStatusCheck) { state.expireOnStatusCheck = false; state.signedIn = false; }
    if (!state.signedIn && path !== '/api/v1/chatgpt/status') { state.deniedReads++; return route.fulfill({ status: 401, json: { error: { message: '请登录' } } }); }
    const owner = state.owner;
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'demo', generated_at: '2026-10-25T10:00:00+08:00', quality: { status: 'partial' } } } });
    if (path === '/api/v1/session') return ok({ user: { id: owner, username: owner, role: 'admin' }, csrf_token: 'synthetic-csrf' });
    if (path === '/api/v1/chatgpt/status') return ok({ available: false, configured: false, accounts: [] });
    // Control-plane clock metadata has its own contract, rather than an invalid
    // generic {items: []} response or the delayed business/preparation budget.
    if (path === '/api/v1/market-session') return ok({ server_time: '2026-10-25T10:00:00+08:00', trade_day: null, phase: 'unknown', is_trading_day: null, automatic_live: false, current_day_active: false, next_transition_at: null, poll_seconds: 15 });
    // Notifications and health checks are independent control channels, like
    // the session clock above; neither prepares a business page. Give them
    // their real contracts without counting them as business preparation.
    if (path === '/api/v1/data-recovery') return ok({ server_time: '2026-10-25T10:00:00+08:00',
      items: [], counts: {}, workers: [], health_state: null, health: [], has_more: false });
    if (path === '/api/v1/workbench-updates') return route.fulfill({ contentType: 'text/event-stream', body: ': synthetic control heartbeat\n\n' });
    if (path === '/api/v1/system/overview') return ok({ version: '合成验收', app_mode: 'demo', health: [], cache: { count: 0, bytes: 0, enabled: true }, last_backup: null, maintenance: { active: false, stage: '', updater_connected: false, backup_connected: false }, update: { configured: false, state: 'unconfigured' } });
    const activeId = Symbol(path); activePaths.set(activeId, path + url.search);
    state.active++; if (state.active > state.maxActive) { state.maxActive = state.active; state.peakPaths = [...activePaths.values()]; }
    try {
      await new Promise(resolve => setTimeout(resolve, state.delay));
      if (path.endsWith('/series')) {
        if (state.holdSeries) await new Promise<void>(resolve => { release = resolve; });
        if (state.failSeries) return route.fulfill({ status: 503, json: { error: { message: '合成测试：周期资料暂不可用' } } });
        return ok({ ...day(days[1]), trade_days: days, returned_day_count: 2, records: days.map(value => ({ ...day(value), trade_day: value, themes: { records: [theme.provider_record] } })) });
      }
      if (path.endsWith('/day')) return ok(day(url.searchParams.get('trade_day') ?? days[1]));
      if (path.endsWith('/notes')) return ok({ trade_day: days[1], content: blank(), head_id: null, writable: true, revision: null });
      if (path.endsWith('/lhb')) return ok({ ...day(days[1]), records: [listing()], history_dates: [], unmatched_seat_records: [], source_listing_count: 1 });
      if (path.endsWith('/theme-history')) return ok({ ...day(days[1]), items: [{ ts_code: theme.ts_code, records: [] }] });
      if (path === '/api/v1/workbench-view') return ok({ ...terminalFixture(url.searchParams), latest_at: '2026-10-25T10:00:00+08:00' });
      if (path === '/api/v1/ths-funds') return ok({ sampled_day: url.searchParams.get('sampled_day') ?? '2026-10-25',
        status: 'missing', received_at: null, complete: false, missing_pages: [], rejected_rows: 0, last_error: null, rows: [], history: [] });
      if (path === '/api/v1/watchlist') return ok({ items: [] });
      if (path === '/api/v1/integrations') return ok({ items: [], templates: [], credential_store_ready: true });
    if (path === '/api/v1/integrations/ifind/domains') return ok([{ id: 'index', name: '合成指数目录', tools: ['index_data'] }]);
      if (path === '/api/v1/data-center/capabilities') return ok({ revision: 1, policy: 'free_first', providers: [], capabilities: [] });
      if (path === '/api/v1/ai/settings') return ok({ enabled: false, revision: 1, default_connection_id: null, features: [] });
      if (path === '/api/v1/data-center/overview') return ok({ services_count: 0, running_tasks: 0, today_requests: 0, pending_issues: 0, services: [], tasks: [], issues: [], updated_at: '2026-10-25T10:00:00+08:00' });
      if (path === '/api/v1/news/items') return ok({ items: [], next_cursor: null, sources: [], coverage_start: null });
      return ok({ items: [], next_cursor: null });
    } finally { state.active--; activePaths.delete(activeId); }
  });
  await page.route('**/health/ready', route => route.fulfill({ json: { status: 'ok' } }));
  return { state, counts, release: () => { state.holdSeries = false; release?.(); } };
}
const modal = (page: Page) => page.getByRole('dialog', { name: '工作空间后台准备' });
const openProgress = async (page: Page) => {
  const notices = await openNotifications(page);
  await notices.getByRole('button', { name: '查看准备进度', exact: true }).click();
};

test('login mounts the current page immediately while five modules prepare in the background', async ({ page }, info) => {
  const f = await workspace(page, false); f.state.holdSeries = true;
  await page.goto('/workbench');
  await page.getByLabel('账号', { exact: true }).fill('synthetic-a');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-test-only');
  await page.getByRole('button', { name: '进入工作台 →', exact: true }).click();
  await expect(modal(page)).toHaveCount(0);
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  await expect(page.locator('.workspace-preparation-status')).toHaveCount(0);
  await openProgress(page);
  await expect(modal(page)).toBeVisible();
  await expect(page.getByRole('progressbar')).not.toHaveAttribute('value', '5');
  await page.screenshot({ path: info.outputPath('登录后准备弹窗.png') });
  // Keep the original five-read bound for other business/preparation paths.
  // The mounted overview now also reads each independent THS category once.
  const thsReads = f.state.peakPaths.map(path => new URL(path, 'http://synthetic.invalid'))
    .filter(url => url.pathname === '/api/v1/ths-funds');
  expect(thsReads.length).toBeLessThanOrEqual(2);
  expect(new Set(thsReads.map(url => url.searchParams.get('category'))).size).toBe(thsReads.length);
  for (const url of thsReads) {
    expect(['industry', 'concept']).toContain(url.searchParams.get('category'));
    expect([...url.searchParams.keys()]).toEqual(['category']);
  }
  expect(f.state.maxActive - thsReads.length, 'Other concurrent paths: ' + JSON.stringify(f.state.peakPaths)).toBeLessThanOrEqual(5);
  f.release();
  await expect(page.getByRole('progressbar')).toHaveAttribute('value', '5');
  await page.getByRole('button', { name: '收起进度 · 后台继续' }).click();
  await expect(modal(page)).toHaveCount(0);
  await expect(page.locator('.workspace-preparation-status')).toHaveCount(0);
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  const before = f.state.requests.length;
  const timings: Record<string, number> = {};
  for (const label of ['每日复盘', '资讯中心', '数据管理', '工作台']) {
    const start = Date.now();
    await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: label, exact: true }).click();
    if (label === '每日复盘') await expect(page.getByRole('heading', { name: '情绪周期表', exact: true })).toBeVisible();
    if (label === '资讯中心') await expect(page.getByTestId('news-center')).toBeVisible();
    if (label === '数据管理') await expect(page.getByRole('navigation', { name: '数据管理页面' })).toBeVisible();
    if (label === '工作台') await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
    timings[label] = Date.now() - start;
  }
  // The new local iFinD domain directory is read once when its panel mounts.
  // It is not one of the five warmed datasets and never queries the supplier.
  const repeated = f.state.requests.slice(before).filter(item => /workbench-view|review-data\/(day|series)|news\/items|integrations(?:\?|$)|data-center\/overview/.test(item));
  expect(repeated).toEqual([]);
  expect(f.state.requests.filter(item => item === 'GET /api/v1/integrations/ifind/domains')).toHaveLength(1);
  expect(f.state.requests.filter(item => !item.startsWith('GET') && !item.startsWith('POST /api/v1/session'))).toEqual([]);
  expect(f.state.errors).toEqual([]);
  await info.attach('warm-navigation.json', { body: JSON.stringify(timings), contentType: 'application/json' });
});

test('failed module is identified and retries without throwing away successful modules', async ({ page }) => {
  const f = await workspace(page); f.state.failSeries = true;
  await page.goto('/research/reviews');
  await expect(await openNotifications(page)).toContainText('资料未完成');
  await openProgress(page);
  await expect(page.getByRole('button', { name: '重试未完成项' })).toBeVisible();
  await expect(modal(page)).toContainText('合成测试：周期资料暂不可用');
  const reads = f.counts('/workbench-view');
  const seriesBefore = f.counts('/review-data/series');
  f.state.failSeries = false;
  await page.getByRole('button', { name: '重试未完成项' }).click();
  await expect(page.getByRole('progressbar')).toHaveAttribute('value', '5');
  await page.getByRole('button', { name: '收起进度 · 后台继续' }).click();
  await expect(modal(page)).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '情绪周期表', exact: true })).toBeVisible();
  expect(f.counts('/workbench-view')).toBe(reads);
  expect(f.counts('/review-data/series')).toBe(seriesBefore + 1);
  expect(f.counts('/review-data/series')).toBeGreaterThanOrEqual(2);
  expect(f.state.errors).toEqual([]);
});

test('entering early keeps preparation running and the progress can be reopened', async ({ page }) => {
  const f = await workspace(page); f.state.holdSeries = true;
  await page.goto('/workbench'); await expect(modal(page)).toHaveCount(0);
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  await expect(page.locator('.workspace-preparation-status')).toHaveCount(0);
  await openProgress(page);
  await expect(modal(page)).toContainText('资料准备');
  await expect(modal(page)).toBeVisible();
  await expect.poll(() => f.counts('/review-data/series')).toBeGreaterThan(0);
  await expect.poll(() => f.counts('/news/items')).toBeGreaterThan(0);
  // The existing five-second checks continue during the delay grace period;
  // opening and closing progress must not start another page read.
  const reads = f.counts('/workbench-view?view=overview');
  await page.getByRole('dialog').press('Escape');
  await expect(modal(page)).toHaveCount(0);
  f.release();
  await expect(page.locator('.workspace-preparation-status')).toHaveCount(0);
  expect(f.counts('/workbench-view?view=overview')).toBe(reads);
  expect(f.state.errors).toEqual([]);
});

test('refresh bypasses cache, dates and receipt cutoffs have independent results', async ({ page }) => {
  const f = await workspace(page);
  await page.goto('/research/reviews'); await expect(modal(page)).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByRole('heading', { name: '情绪周期表', exact: true })).toBeVisible();
  await expect(page.locator('.workspace-preparation-status')).toHaveCount(0);
  const initial = f.counts('/review-data/series');
  await page.getByRole('button', { name: '重读缓存', exact: true }).click();
  await expect.poll(() => f.counts('/review-data/series')).toBe(initial + 1);
  await page.getByLabel('复盘日期', { exact: true }).fill('2026-10-22');
  await expect.poll(() => f.state.requests.some(item => item.includes('/review-data/day?trade_day=2026-10-22'))).toBe(true);
  await page.getByRole('button', { name: '接收截止', exact: true }).click();
  await page.getByLabel('复盘接收截止时间', { exact: true }).fill('2026-10-22T16:00');
  await expect.poll(() => f.state.requests.some(item => item.includes('/review-data/day?') && item.includes('as_of_time=2026-10-22'))).toBe(true);
  expect(f.state.errors).toEqual([]);
});

test('session expiry cancels preparation; a second owner never inherits the first cache', async ({ page }) => {
  const f = await workspace(page); f.state.holdSeries = true;
  await page.goto('/workbench'); await expect(modal(page)).toHaveCount(0);
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  await expect(page.locator('.workspace-preparation-status')).toHaveCount(0);
  await expect.poll(() => f.counts('/review-data/series')).toBeGreaterThan(0);
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '系统设置' }).click();
  await expect(page).toHaveURL(/\/system(?:\?|$)/);
  // A background 401 can correctly remove navigation before a later click.
  // Arm expiry only for the existing status-check action, so the browser
  // receives a real401 after the click. Works against built pages too.
  const checkStatus = page.getByRole('button', { name: '检查状态', exact: true });
  await expect(checkStatus).toBeEnabled();
  f.state.expireOnStatusCheck = true;
  await checkStatus.click();
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  expect(f.state.deniedReads).toBeGreaterThan(0);
  f.state.owner = 'synthetic-b'; f.release();
  await page.getByLabel('账号', { exact: true }).fill('synthetic-b');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-test-only');
  await page.getByRole('button', { name: '进入工作台 →', exact: true }).click();
  await expect(page.getByLabel('账号', { exact: true })).toHaveCount(0);
  await expect(modal(page)).toHaveCount(0);
  await expect(page.locator('.workspace-preparation-status')).toHaveCount(0, { timeout: 15000 });
  // Opening settings does not restart the market/review warm-up queue. Each
  // destination reads its newly authenticated owner's exact scope on demand.
  expect(f.counts('/workbench-view?view=overview')).toBe(1);
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '工作台', exact: true }).click();
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  expect(f.counts('/workbench-view?view=overview')).toBe(2);
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '每日复盘', exact: true }).click();
  await expect(page.getByRole('heading', { name: '情绪周期表', exact: true })).toBeVisible();
  expect(f.counts('/review-data/series')).toBeGreaterThanOrEqual(2);
  expect(f.state.errors).toEqual([]);
});

test('a stalled read times out and remains retryable', async ({ page }) => {
  const f = await workspace(page); f.state.holdSeries = true;
  await page.clock.install();
  await page.goto('/workbench');
  await expect.poll(() => f.counts('/review-data/series')).toBeGreaterThan(0);
  await openProgress(page);
  await page.clock.fastForward(31_000);
  await expect(page.getByRole('button', { name: '重试未完成项' })).toBeVisible();
  await expect(modal(page)).toContainText('读取超时或已取消');
  f.release();
});

test('mobile preparation fits the viewport and keyboard focus stays inside the dialog', async ({ page }, info) => {
  const f = await workspace(page); f.state.holdSeries = true;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/workbench'); await expect(modal(page)).toHaveCount(0);
  await expect.poll(() => f.counts('/review-data/series')).toBeGreaterThan(0);
  await openProgress(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.keyboard.press('Tab');
  expect(await modal(page).evaluate(node => node.contains(document.activeElement))).toBe(true);
  await page.screenshot({ path: info.outputPath('登录后准备弹窗-手机.png') });
  f.release();
});


test('monitor refreshes and polling retain unrelated warmed review data', async ({ page }) => {
  const f = await workspace(page);
  await page.clock.install();
  await page.goto('/workbench');
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false', { timeout: 15000 });
  await expect(page.locator('.workspace-preparation-status')).toHaveCount(0);
  await expect.poll(() => f.counts('/review-data/series')).toBeGreaterThan(0);
  await expect.poll(() => f.state.active).toBe(0);
  const initial = f.counts('/review-data/series');
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '数据管理', exact: true }).click();
  await page.getByRole('button', { name: '运行监控', exact: true }).click();
  await expect(page.locator('.dc-monitor')).toBeVisible();
  expect(f.state.errors).toEqual([]);
  const overviewReads = f.counts('/data-center/overview');
  const refreshed = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/data-center/overview' && response.request().method() === 'GET');
  await page.getByRole('button', { name: '刷新状态', exact: true }).click();
  await expect.poll(() => f.counts('/data-center/overview')).toBeGreaterThan(overviewReads);
  const response = await refreshed; expect(response.status()).toBe(200); await response.finished();
  await expect(page.getByRole('button', { name: '刷新状态', exact: true })).toBeEnabled();
  expect(f.state.errors).toEqual([]);
  await page.clock.fastForward(31_000);
  await expect.poll(() => { expect(f.state.errors).toEqual([]); return f.counts('/data-center/overview'); }).toBeGreaterThan(overviewReads + 1);
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '每日复盘', exact: true }).click();
  await expect(page.getByRole('region', { name: '情绪周期每日统计', exact: true })).toBeVisible();
  expect(f.counts('/review-data/series')).toBe(initial);
  expect(f.state.errors).toEqual([]);
});


test('automatically marking news as read does not discard warmed market data', async ({ page }) => {
  const f = await workspace(page);
  const article = { id: '00000000-0000-4000-8000-000000000077', evidence_id: '00000000-0000-4000-8000-000000000078', title: '合成启动测试资讯', summary: '用于验证已读状态仅更新资讯缓存，不影响其他页面资料。', category: '政策', source_name: '合成来源', publisher: '合成来源', family: 'synthetic', source_url: 'https://example.invalid', published_at: '2026-10-25T09:00:00+08:00', published_day: '2026-10-25', received_at: '2026-10-25T09:00:00+08:00', content_hash: 'a'.repeat(64), is_read: false, is_saved: false, is_following: false, related_sources: [], late: false };
  let writes = 0;
  await page.route('**/api/v1/news/items**', route => {
    const url = new URL(route.request().url());
    if (route.request().method() === 'POST') { writes++; article.is_read = true; }
    const data = url.pathname === '/api/v1/news/items' ? { items: [article], sources: [], next_cursor: null, coverage_start: null } : article;
    return route.fulfill({ json: { data, meta: { app_mode: 'demo' } } });
  });
  await page.goto('/workbench');
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false', { timeout: 15000 });
  await expect(page.locator('.workspace-preparation-status')).toHaveCount(0);
  await expect.poll(() => f.counts('/review-data/series')).toBeGreaterThan(0);
  await expect.poll(() => f.state.active).toBe(0);
  const seriesReads = f.counts('/review-data/series');
  const workbenchReads = f.counts('/workbench-view?view=overview');
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '资讯中心', exact: true }).click();
  await page.getByRole('button', { name: article.title, exact: true }).first().click();
  await expect.poll(() => writes).toBe(1);
  await page.getByRole('dialog').press('Escape');
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '每日复盘', exact: true }).click();
  await expect(page.getByRole('region', { name: '情绪周期每日统计', exact: true })).toBeVisible();
  expect(f.counts('/review-data/series')).toBe(seriesReads);
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '工作台', exact: true }).click();
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  expect(f.counts('/workbench-view?view=overview')).toBe(workbenchReads);
  expect(f.state.errors).toEqual([]);
});
