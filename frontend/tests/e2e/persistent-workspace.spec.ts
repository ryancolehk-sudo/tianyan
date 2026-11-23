import { expect } from '@playwright/test';
import { test, openNotifications } from './isolated-browser.fixtures';
import type { Page } from '@playwright/test';
import type { SectorMembership } from '../../src/generated/schema';
import type { ThsSnapshot } from '../../src/features/terminal/thsFundsData';
import { terminalFixture, terminalDay, stockA, stockB } from './terminalTestFixtures';
import { day, days, blank } from './reviewTestFixtures';

// These records are synthetic protocol fixtures, never market evidence.
async function fixture(page: Page) {
  const state = { signedIn: true, owner: 'persistent-a', rights: 'one', responseScope: '', readState: 'fresh' as 'fresh' | 'refreshing' | 'stale', revision: 1, delay: 0, fail: 0,
    holdLogin: false, holdLogout: false, holdAnonymous: false, releaseLogin: undefined as (() => void) | undefined, releaseLogout: undefined as (() => void) | undefined, releaseAnonymous: undefined as (() => void) | undefined,
    methods: [] as string[], requests: [] as Array<{ path: string; validator?: string }>, detailRequests: [] as string[], membershipRequests: [] as string[], errors: [] as string[] };
  page.on('pageerror', error => { state.errors.push(error.message); console.error('Synthetic page failure:', error.stack); });
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const validator = request.headers()['if-none-match']; state.requests.push({ path: path + url.search, validator });
    state.methods.push(request.method() + ' ' + path);
    if (path === '/api/v1/session' && request.method() === 'POST') { if (state.holdLogin) await new Promise<void>(resolve => { state.releaseLogin = resolve; }); state.signedIn = true; }
    if (path === '/api/v1/session' && request.method() === 'DELETE') { if (state.holdLogout) await new Promise<void>(resolve => { state.releaseLogout = resolve; }); state.signedIn = false; return route.fulfill({ status: 204 }); }
    if (path === '/api/v1/chatgpt/status' && state.holdAnonymous) {
      await new Promise<void>(resolve => { state.releaseAnonymous = resolve; });
      return route.fulfill({ status: 401, json: { error: { message: '合成迟到匿名请求' } } });
    }
    const scope = `${state.owner}:${state.rights}:v1`, revision = state.revision;
    const ok = (data: unknown, headers: Record<string, string> = {}) => route.fulfill({ headers, json: { data, meta: { app_mode: 'demo', generated_at: '2026-10-25T10:00:00+08:00', quality: { status: 'partial' } } } });
    if (path === '/api/v1/chatgpt/status') return ok({ available: true, reason: null, configured: true });
    if (!state.signedIn) return route.fulfill({ status: 401, json: { error: { message: '请登录' } } });
    if (path === '/api/v1/session') return ok({ user: { id: state.owner, username: state.owner, role: 'admin' }, csrf_token: 'synthetic-secret-must-not-persist', cache_scope: scope });
    if (path === '/api/v1/workbench-view') {
      const etag = `"${scope}:${revision}:${url.searchParams.get('view')}"`;
      await new Promise(resolve => setTimeout(resolve, state.delay));
      if (state.fail) return route.fulfill({ status: state.fail, json: { error: { message: '合成后台核对失败' } } });
      const headers = { 'ETag': etag, 'X-Cache-Scope': state.responseScope || scope, 'X-Read-Cache-State': state.readState };
      if (validator === etag && state.readState === 'fresh') return route.fulfill({ status: 304, headers });
      const data = terminalFixture(url.searchParams); data.stocks[0].name = `合成甲${revision}`;
      return ok({ ...data, detail_revision: `synthetic-${revision}`, detail_codes: [stockA, stockB, '999993.SZ'] }, headers);
    }
    if (path === '/api/v1/workbench-details') {
      state.detailRequests.push(url.search);
      const base = terminalFixture(url.searchParams), codes = (url.searchParams.get('codes') ?? '').split(',').filter(Boolean);
      // Selected details replace the ranked row; one revision must agree with its page fixture.
      base.stocks[0].name = `合成甲${revision}`;
      return ok({ trade_day: base.trade_day, revision: `synthetic-${revision}`, stocks: codes.map(code => ({ ...(base.stocks.find(item => item.code === code) ?? base.stocks[0]), code })), auction_days: base.auction_days, events: [], seals: {}, sector_code: url.searchParams.get('sector'), members: base.stocks, members_trade_day: base.trade_day, funds_history: [], tracking_members: base.stocks, tracking_member_count: base.stocks.length, tracking_members_truncated: false });
    }
    if (path === '/api/v1/sector-membership') {
      state.membershipRequests.push(url.search);
      const base = terminalFixture(url.searchParams);
      return ok({ trade_day: base.trade_day, sector_code: url.searchParams.get('sector')!,
        members_trade_day: base.members_trade_day, received_at: base.latest_at, state: 'available',
        members: base.selected_members.map(({ code, name }) => ({ code, name })),
        member_count: base.selected_members.length, truncated: false } satisfies SectorMembership);
    }
    if (path === '/api/v1/ths-funds') {
      const category = url.searchParams.get('category') === 'concept' ? 'concept' : 'industry';
      const sampledDay = url.searchParams.get('sampled_day') || terminalDay;
      const received = `${sampledDay}T15:05:00+08:00`, code = `SYNTHETIC-${category}`;
      return ok({ sampled_day: sampledDay, status: 'available', received_at: received, complete: true,
        missing_pages: [], rejected_rows: 0, last_error: null,
        rows: [{ code, name: category === 'industry' ? '合成同花顺行业' : '合成同花顺概念',
          net_cny: '0', pct_change: '0', company_count: '2', received_at: received }],
        history: [{ received_at: received, points: [{ code, net_cny: '0', received_at: received }] }] } satisfies ThsSnapshot);
    }
    if (path.endsWith('/day')) return ok(day(days[1]));
    if (path.endsWith('/series')) return ok({ ...day(days[1]), trade_days: [], records: [] });
    if (path.endsWith('/notes')) return ok({ trade_day: days[1], content: { ...blank(), summary: 'private-note-must-not-persist' }, head_id: null, writable: true, revision: null });
    if (path.endsWith('/lhb')) return ok({ records: [], history_dates: [], unmatched_seat_records: [] });
    if (path.endsWith('/integrations')) return ok({ items: [], templates: [], credential_store_ready: true });
    if (path.endsWith('/ai/settings')) return ok({ enabled: false, revision: 1, features: [] });
    if (path.endsWith('/data-center/overview')) return ok({ services: [], tasks: [], issues: [], services_count: 0, running_tasks: 0, today_requests: 0, pending_issues: 0 });
    if (path.endsWith('/news/items')) return ok({ items: [], total: 0, next_cursor: null, sources: [] });
    return ok({ items: [], next_cursor: null });
  });
  return state;
}
async function stored(page: Page) {
  return page.evaluate(async () => {
    const databases = await indexedDB.databases(); if (!databases.some(item => item.name === 'tianyan-workspace-reads-v1')) return [];
    return new Promise<Array<{ key: string; scope: string; value: unknown; id: string }>>(resolve => { const open = indexedDB.open('tianyan-workspace-reads-v1'); open.onsuccess = () => { const request = open.result.transaction('records').objectStore('records').getAll(); request.onsuccess = () => { resolve(request.result); open.result.close(); }; }; });
  });
}
async function warm(page: Page) {
  await page.goto('/workbench');
  await expect(page.getByRole('region', { name: '板块追踪', exact: true })).toContainText('合成题材甲');
  await expect(page.getByRole('region', { name: '板块追踪', exact: true }).getByRole('table').locator('tbody tr')).toHaveCount(2);
  await expect.poll(async () => (await stored(page)).some(row => row.key.startsWith('terminal-detail-index:') && JSON.stringify(row.value).includes('999993.SZ'))).toBe(true);
  await expect(page.locator('.workspace-preparation-status')).toHaveCount(0);
}

test('cold startup mounts the page and navigation while data reads are pending', async ({ page }) => {
  const f = await fixture(page); f.delay = 1500;
  await page.goto('/workbench');
  await expect(page.locator('.ty-terminal')).toBeVisible({ timeout: 1000 });
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('navigation', { name: '主导航', exact: true })).toBeVisible();
  await expect(page.locator('.ty-loading-strip')).toBeVisible();
  await expect(page.getByRole('region', { name: '板块追踪', exact: true })).toContainText('合成题材甲');
  expect(f.errors).toEqual([]);
});

test('reload restores the old dated view immediately and replaces it in the background', async ({ page }) => {
  const f = await fixture(page); await warm(page);
  f.delay = 1800; f.revision = 2;
  const start = Date.now(); await page.reload();
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲1', { timeout: 1000 });
  expect(Date.now() - start).toBeLessThan(1500);
  await expect(page.locator('.ty-ribbon-stamp')).toHaveAttribute('title', new RegExp(`本机最近接收 ${terminalDay} 15:05:00`));
  await expect(page.locator('.ty-ribbon-stamp')).toContainText('行情截至');
  await expect(page.locator('.ty-cache-status')).toHaveCount(0);
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲2');
  await expect(page.locator('.ty-cache-status')).toHaveCount(0);
  expect(f.requests.filter(item => item.path.includes('workbench-view') && item.validator).length).toBeGreaterThan(0);
  expect(f.errors).toEqual([]);
});

test('closing a page, reopening, and same-account logout/login reuse saved detail progress', async ({ page, context }) => {
  const f = await fixture(page); await warm(page);
  const before = f.detailRequests.length;
  await page.reload();
  await expect(page.getByRole('region', { name: '板块追踪', exact: true })).toContainText('合成题材甲');
  await expect(page.locator('.workspace-preparation-status')).toHaveCount(0);
  await page.waitForTimeout(750);
  expect(f.detailRequests.length).toBe(before);
  await page.getByRole('button', { name: '退出', exact: true }).click();
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  await page.getByLabel('账号', { exact: true }).fill('persistent-a');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password');
  f.delay = 1200;
  await page.getByRole('button', { name: '进入工作台 →', exact: true }).click();
  await expect(page.getByRole('region', { name: '板块追踪', exact: true })).toContainText('合成题材甲', { timeout: 1000 });
  expect((await stored(page)).some(row => row.key.startsWith('/api/v1/workbench-view'))).toBe(true);
  await page.close();
  const next = await context.newPage(); const again = await fixture(next); again.delay = 1500;
  const authenticated = next.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/session' && response.status() === 200);
  await next.goto('/workbench');
  await authenticated;
  const authenticationFinished = Date.now();
  try {
    await expect(next.getByRole('region', { name: '板块追踪', exact: true })).toContainText('合成题材甲', { timeout: 1000 });
    expect(Date.now() - authenticationFinished).toBeLessThan(1000);
  } catch (error) {
    const rows = await stored(next);
    await test.info().attach('reopen-cache-diagnostics.json', { body: JSON.stringify({ rows: rows.filter(row => row.key.startsWith('/api/v1/workbench-view')), requests: again.requests }), contentType: 'application/json' });
    throw error;
  }
  await expect.poll(() => again.requests.some(item => item.path.includes('workbench-view') && !!item.validator)).toBe(true);
  await next.close();
});

test('another owner and another permission scope cannot restore the preceding account', async ({ page }) => {
  const f = await fixture(page); await warm(page);
  f.owner = 'persistent-b'; f.delay = 1000;
  await page.reload();
  await expect(page.locator('.ty-terminal')).toBeVisible();
  await expect(page.locator('.ty-rank-panel').first()).not.toContainText('合成甲1');
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲1');
  f.rights = 'two'; await page.reload();
  await expect(page.locator('.ty-terminal')).toBeVisible();
  await expect(page.locator('.ty-rank-panel').first()).not.toContainText('合成甲1');
  expect(f.errors).toEqual([]);
});

test('failed background validation retains the dated data and provides a local retry', async ({ page }) => {
  const f = await fixture(page); await warm(page); f.fail = 503;
  await page.reload();
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲1');
  await expect(await openNotifications(page)).toContainText('合成后台核对失败');
  f.fail = 0; f.revision = 2;
  await page.getByRole('dialog', { name: '消息通知', exact: true }).getByRole('listitem').filter({ hasText: '合成后台核对失败' }).getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲2');
  expect(f.errors).toEqual([]);
});

test('storage contains only allowed derived reads and tolerates unavailable storage', async ({ page }) => {
  const f = await fixture(page); await warm(page);
  const rows = await stored(page), serialized = JSON.stringify(rows);
  expect(serialized).not.toContain('synthetic-secret-must-not-persist');
  expect(serialized).not.toContain('private-note-must-not-persist');
  expect(rows.every(row => row.key.startsWith('terminal-detail') || /^\/api\/v1\/(workbench-view|review-data\/(day|series|lhb|stocks|theme-history)|news\/(items|editions))/.test(row.key))).toBe(true);
  await page.addInitScript(() => { Object.defineProperty(window, 'indexedDB', { get() { throw new DOMException('Synthetic storage denial', 'SecurityError'); } }); });
  await page.reload(); await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲1');
  expect(f.errors).toEqual([]);
});

test('403 removes inaccessible persisted reads and explicit dates never fall back to latest', async ({ page }) => {
  const f = await fixture(page); await warm(page); f.fail = 403;
  await page.reload(); await expect(await openNotifications(page)).toContainText('读取权限已变更');
  await expect(page.locator('.ty-rank-panel').first()).not.toContainText('合成甲1');
  await expect.poll(async () => (await stored(page)).filter(row => row.scope === 'persistent-a:one:v1').length).toBe(0);
  f.fail = 503;
  await page.goto('/workbench?trade_day=2000-01-03&as_of_time=2000-01-03T15%3A00%3A00%2B08%3A00');
  await expect(await openNotifications(page)).toContainText('合成后台核对失败');
  await expect(page.locator('.ty-rank-panel').first()).not.toContainText('合成甲1');
});

test('a mismatched response scope purges saved data and rechecks authentication', async ({ page }) => {
  const f = await fixture(page); await warm(page); const before = f.requests.filter(row => row.path === '/api/v1/session').length;
  f.responseScope = 'server-new-permission-scope'; await page.reload();
  await expect(await openNotifications(page)).toContainText('读取权限已变更');
  await expect(page.locator('.ty-rank-panel').first()).not.toContainText('合成甲1');
  await expect.poll(async () => (await stored(page)).filter(row => row.key.startsWith('/api/v1/workbench-view')).length).toBe(0);
  expect(f.requests.filter(row => row.path === '/api/v1/session').length).toBeGreaterThan(before);
  expect(f.errors).toEqual([]);
});

for (const corrupt of [true, false]) test(`corrupt or expired persisted page is discarded (${corrupt ? 'corrupt' : 'expired'})`, async ({ page }) => {
  const f = await fixture(page); await warm(page);
  await page.evaluate(async (corrupt) => {
    await new Promise<void>(resolve => { const open = indexedDB.open('tianyan-workspace-reads-v1'); open.onsuccess = () => {
      const tx = open.result.transaction(['records', 'index'], 'readwrite'), records = tx.objectStore('records'), request = records.getAll();
      request.onsuccess = () => { for (const row of request.result) if (row.key.startsWith('/api/v1/workbench-view')) { if (corrupt) row.value.value.data = { trade_day: 'not-a-date' }; else row.savedAt = Date.now() - 8 * 24 * 60 * 60_000; records.put(row); const { value: _value, ...index } = row; tx.objectStore('index').put(index); } };
      tx.oncomplete = () => { open.result.close(); resolve(); };
    }; });
  }, corrupt);
  f.delay = 1200; await page.reload();
  await expect(page.locator('.ty-terminal')).toBeVisible();
  await expect(page.locator('.ty-rank-panel').first()).not.toContainText('合成甲1');
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲1');
  expect(f.errors).toEqual([]);
});

test('quota failures fall back to memory and updates remain usable', async ({ page }) => {
  const f = await fixture(page);
  await page.addInitScript(() => { const put = IDBObjectStore.prototype.put; IDBObjectStore.prototype.put = function(value, key) { if (this.name === 'records') throw new DOMException('Synthetic quota exhausted', 'QuotaExceededError'); return key === undefined ? put.call(this, value) : put.call(this, value, key); }; });
  await page.goto('/workbench');
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲1');
  await expect(page.locator('.workspace-preparation-status')).toHaveCount(0);
  await page.getByRole('navigation', { name: '工作台页面', exact: true }).getByRole('link', { name: '资金动向', exact: true }).click();
  await expect(page.locator('.ty-funds-ranking')).toContainText('合成同花顺行业');
  await expect(page.locator('.ty-members-panel')).toContainText('同花顺成员映射与个股资金尚缺');
  await expect(page.locator('.ty-members-panel .ty-stock-name')).toHaveCount(0);
  expect(f.errors).toEqual([]);
});

test('legacy sector projections refresh the full tracker while prepared stocks remain reusable', async ({ page }) => {
  const f = await fixture(page); await warm(page);
  await expect.poll(async () => (await stored(page)).filter(row => row.key.includes(':sector-v2:')).length).toBe(2);
  await page.evaluate(async () => {
    await new Promise<void>(resolve => { const open = indexedDB.open('tianyan-workspace-reads-v1'); open.onsuccess = () => {
      const tx = open.result.transaction(['records', 'index'], 'readwrite'), records = tx.objectStore('records'), index = tx.objectStore('index'), request = records.getAll();
      request.onsuccess = () => { for (const row of request.result) {
        if (row.key.includes(':sector-v2:')) {
          records.delete(row.id); index.delete(row.id);
          row.key = row.key.replace(':sector-v2:', ':sector:'); row.id = row.id.replace(':sector-v2:', ':sector:');
          row.value = { members: row.value.members.slice(0, 1), day: row.value.day, history: row.value.history };
        } else if (row.key.startsWith('terminal-detail-index:')) delete row.value.sectorFormat;
        else continue;
        records.put(row); const { value: _value, ...metadata } = row; index.put(metadata);
      } };
      tx.oncomplete = () => { open.result.close(); resolve(); };
    }; });
  });
  const before = f.detailRequests.length, membershipBefore = f.membershipRequests.length;
  await page.reload();
  await expect(page.getByRole('region', { name: '板块追踪', exact: true }).getByRole('table').locator('tbody tr')).toHaveCount(2);
  await expect(page.locator('.workspace-preparation-status')).toHaveCount(0);
  await expect.poll(() => f.membershipRequests.length).toBeGreaterThan(membershipBefore);
  await expect.poll(() => f.detailRequests.length).toBeGreaterThan(before);
  const resumed = f.detailRequests.slice(before).map(query => new URLSearchParams(query));
  expect(resumed.length).toBeGreaterThan(0);
  expect(resumed.every(query => !!query.get('sector') && !query.get('codes'))).toBe(true);
  expect(f.errors).toEqual([]);
});

test('HTTP200 stale background results keep readable data and show a retry instead of endless loading', async ({ page }) => {
  const f = await fixture(page); await warm(page); f.readState = 'stale';
  await page.reload();
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲1');
  await expect(await openNotifications(page)).toContainText('后台更新暂未完成');
  await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false');
  await expect(page.locator('.ty-cache-status')).toHaveCount(0);
  f.readState = 'fresh'; f.revision = 2;
  await page.getByRole('dialog', { name: '消息通知', exact: true }).getByRole('listitem').filter({ hasText: '后台更新暂未完成' }).getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲2');
  await expect(page.locator('.ty-connection-error')).toHaveCount(0);
  expect(f.errors).toEqual([]);
});

test('an account change aborting a cache write does not disable same-account disk restoration', async ({ page }) => {
  const f = await fixture(page); await warm(page);
  await page.evaluate(() => {
    const getAll = IDBObjectStore.prototype.getAll;
    const success = Object.getOwnPropertyDescriptor(IDBRequest.prototype, 'onsuccess')!.set!;
    let expire = true;
    IDBObjectStore.prototype.getAll = function(...args) {
      const request = getAll.apply(this, args);
      if (this.name === 'index' && this.transaction.mode === 'readwrite') Object.defineProperty(request, 'onsuccess', { configurable: true, set(listener) {
        success.call(request, function(event: Event) { if (expire) { expire = false; window.dispatchEvent(new Event('session-expired')); } listener?.call(request, event); });
      } });
      return request;
    };
    window.dispatchEvent(new Event('focus'));
  });
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  f.delay = 1500;
  await page.getByLabel('账号', { exact: true }).fill('persistent-a');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password');
  await page.getByRole('button', { name: '进入工作台 →', exact: true }).click();
  await expect(page.getByRole('region', { name: '板块追踪', exact: true })).toContainText('合成题材甲', { timeout: 1000 });
  expect(f.errors).toEqual([]);
});

test('active refreshing results replace the existing page after bounded background validation', async ({ page }) => {
  const f = await fixture(page); await warm(page); f.readState = 'refreshing';
  await page.reload();
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲1');
  await expect(page.locator('.ty-ribbon-stamp')).toHaveAttribute('title', new RegExp(`本机最近接收 ${terminalDay} 15:05:00`));
  await expect(page.locator('.ty-cache-status')).toHaveCount(0);
  await expect(page.locator('.background-refresh-warning')).toHaveCount(0);
  f.readState = 'fresh'; f.revision = 2;
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲2', { timeout: 8000 });
  await expect(page.locator('.ty-cache-status')).toHaveCount(0);
  expect(f.errors).toEqual([]);
});

test('a late anonymous401 cannot cancel a successful same-page login or expose old scope data', async ({ page }) => {
  const f = await fixture(page); await warm(page); f.holdAnonymous = true;
  // Keep this synthetic request alive after cancellation to exercise the epoch
  // guard as if its response headers had already reached the fetch callback.
  await page.evaluate(() => { const fetch = window.fetch.bind(window); window.fetch = (input, init) => {
    const path = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href).pathname;
    return fetch(input, path === '/api/v1/chatgpt/status' ? { ...init, signal: undefined } : init);
  }; });
  await page.getByRole('button', { name: '退出', exact: true }).click();
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  await expect.poll(() => !!f.releaseAnonymous).toBe(true);
  f.holdLogin = true; f.owner = 'persistent-b'; f.revision = 2; f.delay = 800;
  await page.getByLabel('账号', { exact: true }).fill('persistent-b');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password');
  await page.getByRole('button', { name: '进入工作台 →', exact: true }).click();
  await expect.poll(() => !!f.releaseLogin).toBe(true);
  const late401 = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/chatgpt/status' && response.status() === 401);
  f.releaseAnonymous!(); await late401;
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.locator('.ty-rank-panel .ty-stock-name')).toHaveCount(0);
  f.releaseLogin!();
  await expect(page.locator('.ty-sidebar')).toContainText('persistent-b');
  await expect(page.locator('.ty-rank-panel').first()).not.toContainText('合成甲1');
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲2');
  await expect(page.getByText('会话已变更。', { exact: true })).toHaveCount(0);
  expect(f.errors).toEqual([]);
});

test('a delayed logoutDELETE finishes before a new loginPOST can replace its cookie', async ({ page }) => {
  const f = await fixture(page); await warm(page); f.holdLogout = true;
  await page.getByRole('button', { name: '退出', exact: true }).click();
  await expect.poll(() => !!f.releaseLogout).toBe(true);
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  await expect(page.locator('.ty-rank-panel .ty-stock-name')).toHaveCount(0);
  const before = f.methods.filter(value => value === 'POST /api/v1/session').length;
  await page.getByLabel('账号', { exact: true }).fill('persistent-a');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password');
  await page.getByRole('button', { name: '进入工作台 →', exact: true }).click();
  await expect(page.getByRole('button', { name: '正在登录…', exact: true })).toBeDisabled();
  expect(f.methods.filter(value => value === 'POST /api/v1/session').length).toBe(before);
  f.releaseLogout!();
  await expect(page.getByRole('region', { name: '板块追踪', exact: true })).toContainText('合成题材甲');
  expect(f.methods.filter(value => value === 'POST /api/v1/session').length).toBe(before + 1);
  expect(f.signedIn).toBe(true); expect(f.errors).toEqual([]);
});

test('the optional official login becomes available after logout settles while password login stays visible', async ({ page }) => {
  const f = await fixture(page); await warm(page); f.holdLogout = true;
  await page.getByRole('button', { name: '退出', exact: true }).click();
  await expect.poll(() => !!f.releaseLogout).toBe(true);
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '进入工作台 →', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Continue with ChatGPT', exact: true })).toHaveCount(0);
  f.releaseLogout!();
  await expect(page.getByRole('button', { name: 'Continue with ChatGPT', exact: true })).toBeEnabled();
  await expect(page.getByText('正在结束上次登录…', { exact: true })).toHaveCount(0);
  expect(f.errors).toEqual([]);
});
