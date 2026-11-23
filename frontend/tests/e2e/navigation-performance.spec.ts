import { expect, test } from '@playwright/test';

// Bootstrap behavior is covered separately; these tests exercise each existing page.
test.beforeEach(async ({ page }) => {
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), async () => {
    await page.keyboard.press('Escape');
  });
});
import type { Page } from '@playwright/test';
import { terminalDay, terminalFixture } from './terminalTestFixtures';

// Network delay is deliberate. These fixtures measure navigation mechanics,
// never market-data freshness or live backend latency.
async function workspace(page: Page) {
  const state = { authorized: true, reads: new Map<string, number>(), errors: [] as string[] };
  page.on('pageerror', error => { state.errors.push(error.message); console.error('Synthetic page failure:', error.stack); });
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const key = path === '/api/v1/workbench-view' ? path + '?' + url.searchParams.get('view') : path;
    state.reads.set(key, (state.reads.get(key) ?? 0) + 1);
    if (path === '/api/v1/session' && route.request().method() === 'POST') state.authorized = true;
    if (!state.authorized) return route.fulfill({ status: 401, json: { error: { message: '合成会话失效' } } });
    const ok = (data: unknown) => route.fulfill({ json: { meta: { app_mode: 'demo' }, data } });
    if (path === '/api/v1/session') return ok({ user: { id: 'navigation-fixture', username: 'synthetic', role: 'admin' }, csrf_token: 'synthetic-only' });
    if (path === '/api/v1/watchlist') return ok({ items: [] });
    if (path === '/api/v1/workbench-view') {
      await new Promise(resolve => setTimeout(resolve, 350));
      return ok(terminalFixture(url.searchParams));
    }
    if (path === '/api/v1/integrations') return ok({ items: [], templates: [], credential_store_ready: true });
    if (path === '/api/v1/data-center/capabilities') return ok({ revision: 1, policy: 'free_first', providers: [], capabilities: [] });
    if (path === '/api/v1/ai/settings') return ok({ enabled: false, default_connection_id: null, revision: 1, features: [] });
    if (path === '/api/v1/data-center/overview') return ok({ services_count: 0, running_tasks: 0, today_requests: 0, pending_issues: 0, services: [], tasks: [], issues: [], updated_at: '2026-10-25T00:00:00Z' });
    return ok({ items: [], next_cursor: null });
  });
  return state;
}

test('terminal tabs retain shared reads and display a cached return without another loading pass', async ({ page }, info) => {
  const state = await workspace(page);
  await page.goto(`/workbench?view=overview&trade_day=${terminalDay}`);
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  await page.evaluate(() => { (window as any).__terminalRoot = document.querySelector('.ty-terminal'); });
  const nav = page.getByRole('navigation', { name: '工作台页面', exact: true });
  await nav.getByRole('link', { name: '竞价观察', exact: true }).click();
  await expect(page.locator('.ty-terminal-auction')).toHaveAttribute('aria-busy', 'false');
  const started = Date.now();
  await nav.getByRole('link', { name: '综合盯盘', exact: true }).click();
  await expect(page.locator('.ty-terminal-overview')).toHaveAttribute('aria-busy', 'false');
  const elapsed = Date.now() - started;
  expect(await page.evaluate(() => (window as any).__terminalRoot === document.querySelector('.ty-terminal'))).toBe(true);
  expect(state.reads.get('/api/v1/watchlist')).toBe(1);
  expect(state.reads.get('/api/v1/workbench-view?overview')).toBe(1);
  await expect(page.locator('.ty-loading-strip')).toHaveCount(0);
  await page.goBack();
  await expect(page.locator('.ty-terminal-auction')).toHaveAttribute('aria-busy', 'false');
  expect(state.reads.get('/api/v1/workbench-view?auction')).toBe(1);
  expect(state.errors).toEqual([]);
  await info.attach('cached-navigation.json', { body: JSON.stringify({ cached_return_ms: elapsed, overview_reads: 1, watchlist_reads: 1 }), contentType: 'application/json' });
});

test('another date loads its own data and a refresh still reaches the server', async ({ page }) => {
  const state = await workspace(page);
  await page.goto(`/workbench?view=overview&trade_day=${terminalDay}`);
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  // A document refresh must not reuse the previous document's in-memory data.
  await page.reload();
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  expect(state.reads.get('/api/v1/workbench-view?overview')).toBe(2);
  await page.goto('/workbench?view=overview&trade_day=2026-10-22');
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  expect(state.reads.get('/api/v1/workbench-view?overview')).toBe(3);
  expect(state.errors).toEqual([]);
});

test('expired sessions discard the terminal cache even when the same account signs in again', async ({ page }) => {
  const state = await workspace(page);
  await page.goto(`/workbench?view=overview&trade_day=${terminalDay}`);
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  // Expire the authoritative session recheck. An unrelated preparation GET
  // must not remove the navigation before the intended action can execute.
  const expire = async (route: import('@playwright/test').Route) => {
    if (route.request().method() === 'GET') state.authorized = false;
    return route.fallback();
  };
  await page.route('**/api/v1/session', expire);
  const expired = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/session' && response.status() === 401);
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expired;
  await page.unroute('**/api/v1/session', expire);
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  await page.getByLabel('账号', { exact: true }).fill('synthetic');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-only');
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  await page.getByRole('navigation', { name: '工作台页面', exact: true }).getByRole('link', { name: '综合盯盘', exact: true }).click();
  await expect(page.locator('.ty-terminal-overview')).toHaveAttribute('aria-busy', 'false');
  expect(state.reads.get('/api/v1/workbench-view?overview')).toBe(2);
  expect(state.reads.get('/api/v1/watchlist')).toBe(2);
});

test('data-center navigation shares configuration but explicit refresh reads it again', async ({ page }) => {
  const state = await workspace(page);
  await page.goto('/data?view=connections');
  const nav = page.getByRole('navigation', { name: '数据管理页面', exact: true });
  await expect(nav).toBeVisible();
  await expect.poll(() => state.reads.get('/api/v1/integrations')).toBe(1);
  await nav.getByRole('button', { name: '运行监控', exact: true }).click();
  await nav.getByRole('button', { name: '数据来源', exact: true }).click();
  expect(state.reads.get('/api/v1/integrations')).toBe(1);
  await nav.getByRole('button', { name: '运行监控', exact: true }).click();
  await page.getByRole('button', { name: '刷新状态', exact: true }).click();
  await expect.poll(() => state.reads.get('/api/v1/integrations')).toBe(2);
  expect(state.errors).toEqual([]);
});
