import { expect } from '@playwright/test';
import { test, openNotifications } from './isolated-browser.fixtures';
import type { Page } from '@playwright/test';
import type { RecoveryItem, RecoveryStatus } from '../../src/features/data-center-live/RecoveryPanel';

// Frontend-only fixtures. Every API request is intercepted; these are neither
// supplier checks nor evidence that any production record has been repaired.
const isolated = (() => {
  try { const url = new URL(process.env.JL_E2E_BASE_URL ?? ''); return ['127.0.0.1', 'localhost'].includes(url.hostname) && (url.port === '18128' || /^185\d\d$/.test(url.port)); }
  catch { return false; }
})();
test.skip(!isolated, 'requires a local frontend-only preview on port18128 or18500–18599');
const owner = '00000000-0000-4000-8000-000000000101';
const at = '2026-10-26T10:00:00Z';
const source = '00000000-0000-4000-8000-000000000201';
const meta = { request_id: owner, app_mode: 'demo', generated_at: at, as_of_time: null, quality: { status: 'partial', reasons: ['合成界面测试'] } };
function item(id: string, extra: Partial<RecoveryItem> = {}): RecoveryItem {
  return { id, source_id: source, dataset: 'minute.bars', scope: { instrument: { exchange: 'SH', symbol: '600000', asset_type: 'stock' }, adjustment: 'unadjusted' },
    trade_day: '2026-10-26', interval: '1m', state: 'partial', expected_count: 240, actual_count: 225,
    missing_ranges: [{ start: '2026-10-26T13:46:00+08:00', end: '2026-10-26T14:00:00+08:00', count: 15 }],
    job_id: null, reason: 'nominal_slots_missing', checked_at: at, next_retry_at: '2026-10-26T10:15:00Z', ...extra };
}
function records(): RecoveryItem[] {
  return [item('partial'), item('unsupported', { dataset: 'realtime.snapshots', scope: { scope: 'configured_source' }, interval: 'sample', state: 'unsupported', actual_count: 168, reason: 'snapshot_replay_not_verified', next_retry_at: null }),
    item('complete', { dataset: 'review.daily', scope: { scope: 'all' }, interval: 'day', state: 'complete', actual_count: 5574, expected_count: null, missing_ranges: [], reason: null, next_retry_at: null }),
    item('running', { dataset: 'funds.minute_history', scope: { category: 'concept', code: 'BK0001' }, state: 'running', job_id: 'synthetic-job', reason: null }),
    item('waiting', { dataset: 'direction.daily_history', scope: { scope: 'top10' }, interval: 'day', state: 'waiting', actual_count: null, expected_count: 10, missing_ranges: [], reason: 'rate_limited' }),
    item('blocked', { state: 'blocked', actual_count: 0, reason: 'permission_missing', next_retry_at: null }),
    item('missing', { state: 'missing', reason: 'empty_not_confirmed' }), item('queued', { state: 'queued', reason: null, job_id: 'synthetic-queued' })];
}
async function fixture(page: Page, role = 'admin') {
  const state = { items: records(), health: undefined as RecoveryStatus['health'], healthState: null as RecoveryStatus['health_state'],
    failure: 0, reads: [] as URLSearchParams[], writes: [] as string[], outside: [] as string[], errors: [] as string[], holdDay: '', release: () => {} };
  const origin = new URL(test.info().project.use.baseURL as string).origin;
  page.on('pageerror', error => { state.errors.push(error.message); console.error(error.stack ?? error.message); });
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), () => page.keyboard.press('Escape'));
  await page.route('**/*', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    if (url.origin !== origin) { state.outside.push(url.origin); return route.abort('blockedbyclient'); }
    if (!path.startsWith('/api/') && !path.startsWith('/health/')) return route.continue();
    const ok = (data: unknown) => route.fulfill({ json: { meta, data } });
    if (request.method() !== 'GET') { state.writes.push(`${request.method()} ${path}`); return route.fulfill({ status: 405, json: { error: { message: '禁止写入' } } }); }
    if (path === '/api/v1/session') return ok({ user: { id: owner, username: 'synthetic-recovery', role }, csrf_token: 'synthetic-only' });
    if (path === '/api/v1/integrations') return ok({ items: [{ id: 'synthetic-connection', name: '合成行情来源', kind: 'market', source_ids: [source], capabilities: [{ source_id: source, name: '合成分钟能力', family: 'synthetic_minute' }], enabled: true, credential_configured: false, last_test: null, options: {} }], templates: [], credential_store_ready: false });
    if (path === '/api/v1/ai/settings') return ok({ enabled: false, revision: 1, default_connection_id: null, features: [] });
    if (path === '/api/v1/data-center/capabilities') return ok({ revision: 1, policy: 'free_first', providers: [], capabilities: [] });
    if (path === '/api/v1/data-center/overview') return ok({ services_count: 1, running_tasks: 0, today_requests: 0, pending_issues: 0, services: [], tasks: [], issues: [], updated_at: at });
    if (path === '/api/v1/data-center/calls') return ok({ items: [], next_cursor: null });
    if (path === '/api/v1/chatgpt/status') return ok({ available: false, authenticated: false, local_only: true, accounts: [] });
    if (path === '/api/v1/data-recovery') {
      state.reads.push(url.searchParams);
      const snapshot = structuredClone(state.items).filter(row => !url.searchParams.has('day') || row.trade_day === url.searchParams.get('day'));
      if (state.holdDay === url.searchParams.get('day')) await new Promise<void>(resolve => { state.release = resolve; });
      if (state.failure) return route.fulfill({ status: state.failure, json: { error: { message: '合成读取失败' }, request_id: owner } });
      const offset = Number(url.searchParams.get('offset') ?? 0), limit = Number(url.searchParams.get('limit') ?? 100);
      const counts = snapshot.reduce<Record<string, number>>((result, row) => { result[row.state] = (result[row.state] ?? 0) + 1; return result; }, {});
      const data: RecoveryStatus = { server_time: at, items: snapshot.slice(offset, offset + limit), counts, has_more: snapshot.length > offset + limit,
        workers: role === 'admin' ? [{ role: 'recovery', seen_at: at, progress_at: '2026-10-26T09:58:00Z', job_id: 'synthetic-job' }] : [],
        ...(state.health === undefined ? {} : { health: structuredClone(state.health), health_state: state.healthState }) };
      return ok(data);
    }
    if (path === '/health/ready') return route.fulfill({ json: { status: 'ok' } });
    return ok({ items: [], next_cursor: null });
  });
  return state;
}
async function open(page: Page) {
  await page.goto('/data?view=monitor');
  const panel = page.locator('.dc-recovery-panel');
  await expect(panel.getByRole('heading', { name: '数据完整性与补漏' })).toBeVisible();
  return panel;
}

test('renders dated coverage, null counts and unsupported snapshots without claiming lost trades', async ({ page }, info) => {
  const state = await fixture(page);
  const panel = await open(page);
  const rows = panel.locator('.dc-recovery-table tbody tr');
  await expect(rows).toHaveCount(8);
  await expect(rows.nth(0)).toContainText('已存 225');
  await expect(rows.nth(0)).toContainText('合成行情来源 · 合成分钟能力');
  await expect(rows.nth(0)).toContainText('上交所 · 600000 · 股票');
  await rows.nth(0).locator('summary').click();
  await expect(rows.nth(0)).toContainText('13:46:00 — 14:00:00');
  await expect(rows.nth(1)).toContainText('待源支持');
  await expect(rows.nth(1)).toContainText('历史快照回放能力尚未验证；现有样本仍保留，不代表数据全部丢失');
  await expect(rows.nth(2)).toContainText('基准总量待核');
  await expect(rows.nth(2)).toContainText('已存 5,574');
  await expect(rows.nth(4)).toContainText('已存数量待核');
  await expect(rows.nth(5)).toContainText('已存 0');
  await expect(panel.getByLabel('当前筛选的补漏统计')).toContainText('当前筛选共 8 项');
  await expect(panel).toContainText('分钟时点齐备不等于成交完整');
  await panel.locator('.dc-recovery-workers > summary').click();
  await expect(panel.getByLabel('补漏后台执行记录')).toContainText('历史补漏');
  await expect(panel.getByLabel('补漏后台执行记录')).toContainText('17:58:00');
  await expect(panel.getByRole('button', { name: /立即补采|开始补采/ })).toHaveCount(0);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await panel.screenshot({ path: info.outputPath(`recovery-monitor-${width}.png`) });
  }
  expect(state.reads[0].get('limit')).toBe('100');
  expect(state.writes).toEqual([]); expect(state.outside).toEqual([]); expect(state.errors).toEqual([]);
});

test('polls every 30 seconds and keeps the last result on failure until a GET retry succeeds', async ({ page }) => {
  await page.clock.install({ time: new Date(at) });
  const state = await fixture(page);
  state.items = [item('only')];
  const panel = await open(page);
  await expect(panel.locator('.dc-recovery-table')).toContainText('已存 225');
  expect(state.reads).toHaveLength(1);
  await page.clock.runFor(29_000); expect(state.reads).toHaveLength(1);
  state.failure = 503;
  await page.clock.runFor(1_000);
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成读取失败' });
  await expect(notice).toBeVisible();
  await expect(panel).toContainText('本次读取失败，保留上次结果');
  await expect(panel.locator('.dc-recovery-table')).toContainText('已存 225');
  state.failure = 0;
  state.items = [item('only', { state: 'complete', actual_count: 240, missing_ranges: [], reason: null, next_retry_at: null })];
  await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(panel.locator('.dc-recovery-table')).toContainText('已存 240');
  await expect(notice).toHaveCount(0);
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await page.getByRole('button', { name: '刷新状态', exact: true }).click();
  await expect.poll(() => state.reads.length).toBe(4);
  expect(state.writes).toEqual([]); expect(state.outside).toEqual([]); expect(state.errors).toEqual([]);
});

test('initial failure retries automatically without fabricating an empty or complete result', async ({ page }) => {
  await page.clock.install({ time: new Date(at) });
  const state = await fixture(page);
  state.failure = 503;
  const panel = await open(page);
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成读取失败' });
  await expect(notice).toBeVisible();
  await expect(panel.getByText('当前日期范围暂无核验记录')).toHaveCount(0);
  await expect(panel.getByLabel('当前筛选的补漏统计')).toHaveCount(0);
  state.failure = 0;
  await page.clock.runFor(35_000);
  await expect(panel.locator('.dc-recovery-table tbody tr')).toHaveCount(8);
  await expect(notice).toHaveCount(0);
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  expect(state.writes).toEqual([]); expect(state.outside).toEqual([]); expect(state.errors).toEqual([]);
});

test('date filters and page navigation preserve record dates and discard a late previous filter', async ({ page }) => {
  const state = await fixture(page);
  state.items = Array.from({ length: 101 }, (_, index) => item(`today-${index}`, { dataset: `合成资料 ${index}` }));
  state.items.push(item('old-day', { trade_day: '2026-10-23', dataset: '合成昨日资料' }));
  const panel = await open(page);
  await expect(panel.locator('.dc-recovery-table tbody tr')).toHaveCount(100);
  await expect(panel.getByLabel('当前筛选的补漏统计')).toContainText('当前筛选共 102 项');
  await panel.getByRole('button', { name: '下一页' }).click();
  await expect(panel.locator('.dc-recovery-table tbody tr')).toHaveCount(2);
  expect(state.reads.at(-1)?.get('offset')).toBe('100');
  await expect(panel.locator('.dc-recovery-table')).toContainText('2026-10-23');
  state.holdDay = '2026-10-23';
  await panel.getByLabel('补漏数据日期').fill('2026-10-23');
  await expect.poll(() => state.reads.at(-1)?.get('day')).toBe('2026-10-23');
  expect(state.reads.at(-1)?.has('offset')).toBe(false);
  await panel.getByLabel('补漏数据日期').fill('2026-10-26');
  await expect(panel.locator('.dc-recovery-table tbody tr')).toHaveCount(100);
  state.release();
  await expect(panel.locator('.dc-recovery-table')).not.toContainText('2026-10-23');
  await expect(panel.getByLabel('当前筛选的补漏统计')).toContainText('当前筛选共 101 项');
  await panel.getByLabel('补漏数据日期').fill('2026-10-22');
  await expect(panel.getByText('当前日期范围暂无核验记录')).toBeVisible();
  await expect(panel).toContainText('暂无记录不代表数据已经完整');
  await panel.getByRole('button', { name: '全部日期' }).click();
  await expect(panel.locator('.dc-recovery-table tbody tr')).toHaveCount(100);
  expect(state.reads.at(-1)?.has('day')).toBe(false);
  expect(state.writes).toEqual([]); expect(state.outside).toEqual([]); expect(state.errors).toEqual([]);
});

test('ordinary users see only returned rows and no absent administrative worker records', async ({ page }) => {
  const state = await fixture(page, 'researcher');
  state.items = [item('own-record')];
  const panel = await open(page);
  await expect(panel.locator('.dc-recovery-table tbody tr')).toHaveCount(1);
  await expect(panel.locator('.dc-recovery-workers')).toHaveCount(0);
  expect(state.writes).toEqual([]); expect(state.outside).toEqual([]); expect(state.errors).toEqual([]);
});

test('a permission failure clears previously visible coverage instead of preserving restricted rows', async ({ page }) => {
  const state = await fixture(page);
  const panel = await open(page);
  await expect(panel.locator('.dc-recovery-table tbody tr')).toHaveCount(8);
  state.failure = 403;
  await panel.getByRole('button', { name: '刷新补漏状态' }).click();
  await expect(await openNotifications(page)).toContainText(/读取权限|合成读取失败/);
  await expect(panel.locator('.dc-recovery-table tbody tr')).toHaveCount(0);
  await expect(panel.locator('.dc-recovery-workers')).toHaveCount(0);
  expect(state.writes).toEqual([]); expect(state.outside).toEqual([]); expect(state.errors).toEqual([]);
});

test('explains a local source check failure and its scheduled retry, then removes the warning after recovery', async ({ page }, info) => {
  await page.clock.install({ time: new Date(at) });
  const state = await fixture(page);
  const failedId = '00000000-0000-4000-8000-000000000301';
  const healthyId = '00000000-0000-4000-8000-000000000302';
  const otherSource = '00000000-0000-4000-8000-000000000202';
  state.healthState = 'attention';
  state.health = [{ id: failedId, capability: 'pipeline:source_check', scope: { scope: 'configured_source' },
    trade_day: '2026-10-26', state: 'attention', stage: 'monitor', primary_source_id: source, active_source_id: null,
    last_valid_at: null, last_source_time: null, reason: 'local_check_failed', next_action: 'inspect_runtime',
    next_action_at: '2026-10-26T10:00:15Z', missing_count: null, updated_at: at, impact_modules: ['工作台'] }];
  state.health.push({ ...state.health[0], id: healthyId, capability: 'stock_quote', state: 'normal', stage: 'source',
    primary_source_id: otherSource, active_source_id: otherSource, last_valid_at: at, last_source_time: at,
    reason: null, next_action: null, next_action_at: null });
  const panel = await open(page);
  const health = panel.getByRole('region', { name: '数据运行与恢复', exact: true });
  await expect(health.locator('.dc-health-summary')).toContainText('需要处理');
  await expect(panel.locator('.dc-recovery-table tbody tr')).toHaveCount(8);
  await health.getByText('查看核验与恢复记录（2 项）', { exact: true }).click();
  const row = health.locator(`[data-health-id="${failedId}"]`);
  const healthy = health.locator(`[data-health-id="${healthyId}"]`);
  await expect(healthy).toContainText('正常');
  await expect(row).toContainText('运行状态核验');
  await expect(row).toContainText('该来源的本机检查异常，其他来源继续运行');
  await expect(row).toContainText('核查本机检查异常并自动复测');
  await expect(row).toContainText('核验于 2026/10/26 18:00:00');
  await expect(row).toContainText('计划于 2026/10/26 18:00:15');
  await expect(row.getByText('local_check_failed', { exact: true })).toHaveCount(0);
  await expect(row.getByText('inspect_runtime', { exact: true })).toHaveCount(0);
  await panel.screenshot({ path: info.outputPath('local-check-failed-chinese.png') });
  state.healthState = 'normal';
  state.health = state.health.filter(item => item.id !== failedId);
  await page.clock.runFor(30_000);
  await expect(row).toHaveCount(0);
  await expect(healthy).toContainText('正常');
  await expect(health.locator('.dc-health-summary')).toContainText('正常');
  await expect(health).not.toContainText('本机检查异常');
  await expect(panel.locator('.dc-recovery-table tbody tr')).toHaveCount(8);
  expect(state.writes).toEqual([]); expect(state.outside).toEqual([]); expect(state.errors).toEqual([]);
});
