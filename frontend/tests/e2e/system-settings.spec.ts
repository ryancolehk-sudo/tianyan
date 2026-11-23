import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { terminalFixture } from './terminalTestFixtures';
import { blank, day, days, listing } from './reviewTestFixtures';

// All accounts, maintenance records and market values in this suite are synthetic.
async function setup(page: Page, options: { signedIn?: boolean; role?: string; start?: string; lookback?: number; category?: string } = {}) {
  const state = { signedIn: options.signedIn ?? true, role: options.role ?? 'admin', preferences: { start_page: options.start ?? 'workbench', review_lookback: options.lookback ?? 20, news_category: options.category ?? '精选', revision: 1 }, requests: [] as { method: string; path: string; body: unknown; headers: Record<string, string> }[], failOverview: false, failPreference: false, configured: false, downloaded: false, release: false, connected: true, backupPolls: 0, failJobOnce: false, otherSession: true, errors: [] as string[] };
  page.on('pageerror', error => state.errors.push(error.message));
  const meta = { app_mode: 'demo', generated_at: '2001-12-31T10:00:00+08:00', quality: { status: 'partial' } };
  const job = (kind = 'backup', status = 'queued') => ({ id: 'synthetic-job', kind, status, stage: status === 'succeeded' ? 'completed' : status === 'running' ? 'exporting' : 'queued', message: '合成验收任务 · 非真实备份', error_code: null, created_at: '2001-12-31T10:00:00+08:00', started_at: null, completed_at: status === 'succeeded' ? '2001-12-31T10:01:00+08:00' : null, result: null });
  const overview = () => ({ version: '合成验收版本 0.1', app_mode: 'demo', health: [{ id: 'database', label: '数据库（合成）', state: 'ok', checked_at: '2001-12-31T10:00:00+08:00', message: '合成验收状态，不代表真实运行情况' }, { id: 'worker', label: '资料准备（合成）', state: 'warning', checked_at: '2001-12-31T10:00:00+08:00', message: '用于测试局部状态显示' }], cache: { count: 4, bytes: 524288, enabled: true }, last_backup: null, maintenance: { active: false, stage: '', backup_connected: state.connected, updater_connected: state.connected }, update: { configured: state.configured, state: state.configured ? 'available' : 'unconfigured' } });
  const update = () => ({ configured: state.configured, state: state.configured ? state.release ? 'available' : 'current' : 'unconfigured', message: state.configured ? '合成版本说明，仅用于验收' : '本实例还没有设置官方更新源，暂时无法判断是否有新版。', checked_at: null, current_version: '合成验收版本 0.1', release: state.release ? { id: 'synthetic-release', version: '合成验收版本 0.2', published_at: '2001-12-31T10:00:00+08:00', notes: '合成更新说明\n用于测试下载与安装分离', channel: 'stable', automatic_supported: true, reason: null, downloaded: state.downloaded, size_bytes: 12345678 } : null, download: null, install: null });
  await page.route('**/api/v1/**', async route => {
    const req = route.request(), url = new URL(req.url()), path = url.pathname, method = req.method();
    state.requests.push({ method, path: path + url.search, body: req.postDataJSON(), headers: req.headers() });
    const ok = (data: unknown, status = 200) => route.fulfill({ status, json: { data, meta } });
    const fail = (text: string, status = 503) => route.fulfill({ status, json: { error: { message: text } } });
    if (path === '/api/v1/chatgpt/status') return ok({ available: false, configured: false });
    if (path === '/api/v1/session' && method === 'POST') state.signedIn = true;
    if (path === '/api/v1/session' && method === 'DELETE') { state.signedIn = false; return route.fulfill({ status: 204 }); }
    if (!state.signedIn) return fail('请登录', 401);
    if (path === '/api/v1/session') return ok({ user: { id: 'synthetic-settings', username: 'synthetic-settings', role: state.role }, csrf_token: 'synthetic-csrf', cache_scope: 'synthetic-scope', preferences: state.preferences });
    if (path === '/api/v1/market-session') return ok({ server_time: meta.generated_at, trade_day: null, phase: 'unknown', is_trading_day: null, automatic_live: false, current_day_active: false, next_transition_at: null, poll_seconds: 15 });
    if (path === '/api/v1/ai/settings') return ok({ enabled: false, revision: 1, default_connection_id: null, features: [] });
    if (path === '/api/v1/me/preferences' && method === 'GET') return ok(state.preferences);
    if (path === '/api/v1/me/preferences') {
      if (state.failPreference) return fail('合成：偏好保存暂时失败');
      const data = req.postDataJSON(); state.preferences = { ...state.preferences, ...data, revision: state.preferences.revision + 1 }; return ok(state.preferences);
    }
    if (path === '/api/v1/me/password') { state.signedIn = false; return route.fulfill({ status: 204 }); }
    if (path === '/api/v1/me/sessions') return ok({ items: [{ id: 'synthetic-current', current: true, created_at: '2001-12-31T10:00:00+08:00', expires_at: '2002-01-01T10:00:00+08:00' }, ...(state.otherSession ? [{ id: 'synthetic-other', current: false, created_at: '2001-12-30T10:00:00+08:00', expires_at: '2002-01-01T10:00:00+08:00' }] : [])] });
    if (path.startsWith('/api/v1/me/sessions/')) { state.otherSession = false; return route.fulfill({ status: 204 }); }
    if (path === '/api/v1/system/overview') return state.failOverview ? fail('合成：状态检查暂时失败') : ok(overview());
    if (path === '/api/v1/system/cache') return ok({ count: method === 'DELETE' ? 0 : 4, bytes: method === 'DELETE' ? 0 : 524288, enabled: true });
    if (path === '/api/v1/system/backups') return ok({ items: state.backupPolls > 0 ? [{ id: 'synthetic-backup', name: '合成验收备份 · 非真实资料', created_at: '2001-12-31T10:00:00+08:00', completed_at: '2001-12-31T10:01:00+08:00', size_bytes: 12345678, sha256: '0'.repeat(64), integrity_state: 'passed', recovery_state: 'unverified', recovery_verified_at: null, app_mode: 'demo', schema_revision: 'synthetic-0017', build_revision: 'synthetic-build' }] : [] });
    if (path === '/api/v1/system/maintenance-jobs') return ok(job(), 202);
    if (path.startsWith('/api/v1/system/maintenance-jobs/')) { if (state.failJobOnce) { state.failJobOnce = false; return fail('合成：进度读取暂时中断'); } state.backupPolls++; return ok(job('backup', 'succeeded')); }
    if (path === '/api/v1/system/updates') return ok(update());
    if (path.endsWith('/updates/check')) return ok(update());
    if (path.endsWith('/updates/download')) { state.downloaded = true; return ok(job('update.download', 'succeeded'), 202); }
    if (path.endsWith('/updates/install')) return ok(job('update.install'), 202);
    if (path === '/api/v1/system/activity') return ok({ items: [{ id: 'synthetic-activity', at: '2001-12-31T10:00:00+08:00', action: 'settings.preferences.update', label: '合成偏好修改记录', outcome: 'succeeded', actor: 'synthetic-settings', details: { field_count: 3 } }], next_cursor: null });
    if (path === '/api/v1/system/diagnostics') return ok({ version: 'synthetic', app_mode: 'demo', health: [], maintenance: { active: false }, errors: [] });
    if (path === '/api/v1/workbench-view') return ok(terminalFixture(url.searchParams));
    if (path.endsWith('/review-data/day')) return ok(day(url.searchParams.get('trade_day') ?? days[1]));
    if (path.endsWith('/review-data/series')) return ok({ ...day(days[1]), trade_days: days, records: days.map(value => ({ ...day(value), trade_day: value, themes: { records: [] } })) });
    if (path.endsWith('/review-data/notes')) return ok({ trade_day: days[1], content: blank(), head_id: null, writable: true, revision: null });
    if (path.endsWith('/review-data/lhb')) return ok({ ...day(days[1]), records: [listing()], history_dates: [], unmatched_seat_records: [], source_listing_count: 1 });
    if (path === '/api/v1/news/items') return ok({ items: [], next_cursor: null, sources: [], total: 0, coverage_start: null, focus: null });
    return ok({ items: [], next_cursor: null });
  });
  await page.route('**/health/**', route => route.fulfill({ json: { status: 'ok' } }));
  return state;
}
const tab = (page: Page, label: string) => page.getByRole('tab', { name: label, exact: true });

test('modern system overview has six real tabs, correct navigation, and no market preloads', async ({ page }) => {
  const state = await setup(page); await page.goto('/system');
  await expect(page.getByRole('heading', { name: '系统设置', exact: true })).toBeVisible();
  await expect(page.getByRole('tab')).toHaveCount(6);
  await expect(page.locator('.primary-navigation a[aria-current=page]')).toHaveText('系统设置');
  await expect(page.locator('.ty-tabbar')).toHaveCount(0);
  await expect(page.getByText('合成验收版本 0.1', { exact: true })).toBeVisible();
  await page.waitForTimeout(500);
  expect(state.requests.filter(item => /workbench-view|review-data|news\/items/.test(item.path))).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('activation and payment placeholders collect nothing and send no writes', async ({ page }) => {
  const state = await setup(page); await page.goto('/system');
  for (const label of ['激活码', '付费入口']) {
    await page.getByRole('button', { name: new RegExp(label + '.*暂未开放') }).click();
    const dialog = page.getByRole('dialog', { name: label }); await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('textbox')).toHaveCount(0); await expect(dialog).toContainText('暂未开放');
    await page.keyboard.press('Escape'); await expect(dialog).toHaveCount(0);
  }
  expect(state.requests.filter(item => item.method !== 'GET')).toEqual([]);
});

test('preferences remain while switching tabs and save with revision, CSRF and idempotency', async ({ page }) => {
  const state = await setup(page); await page.goto('/system?view=preferences');
  await page.getByLabel('默认首页', { exact: true }).selectOption('news');
  await page.getByLabel('复盘默认周期', { exact: true }).selectOption('60');
  await page.getByLabel('资讯默认分类', { exact: true }).selectOption('全部');
  await tab(page, '账号安全').click(); await tab(page, '使用偏好').click();
  await expect(page.getByLabel('默认首页', { exact: true })).toHaveValue('news');
  await page.getByRole('button', { name: '保存偏好', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('使用偏好已保存');
  const request = state.requests.find(item => item.method === 'PUT')!;
  expect(request.body).toEqual({ start_page: 'news', review_lookback: 60, news_category: '全部', expected_revision: 1 });
  expect(request.headers['x-csrf-token']).toBe('synthetic-csrf'); expect(request.headers['idempotency-key']).toBeTruthy();
  await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: '资讯中心', exact: true }).click();
  await expect(page.getByTestId('news-center')).toBeVisible();
  await expect.poll(() => state.requests.some(item => item.path.startsWith('/api/v1/news/items') && new URL(item.path, 'http://local').searchParams.get('category') === '全部')).toBeTruthy();
});

test('failed preference save retains the editable form and explicit restore needs saving', async ({ page }) => {
  const state = await setup(page); state.failPreference = true; await page.goto('/system?view=preferences');
  await page.getByLabel('默认首页', { exact: true }).selectOption('news'); await page.getByRole('button', { name: '保存偏好', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('合成：偏好保存暂时失败');
  await expect(page.getByLabel('默认首页', { exact: true })).toHaveValue('news');
  await page.getByRole('button', { name: '恢复默认', exact: true }).click(); await expect(page.getByLabel('默认首页', { exact: true })).toHaveValue('workbench');
  expect(state.requests.filter(item => item.method === 'PUT')).toHaveLength(1);
});

test('session root preference controls the entry, while explicit deep links and filters win', async ({ page }) => {
  const state = await setup(page, { start: 'news', category: '全部', lookback: 60 });
  await page.goto('/'); await expect(page).toHaveURL(/\/news$/); await expect(page.getByTestId('news-center')).toBeVisible();
  await page.goto('/research/reviews?view=emotion&lookback=20&trade_day=2025-01-02&as_of_time=2025-01-02T15%3A10%3A00%2B08%3A00');
  await expect.poll(() => state.requests.some(item => item.path.startsWith('/api/v1/review-data/series') && item.path.includes('lookback=20') && item.path.includes('as_of_time='))).toBeTruthy();
  await expect(page.getByRole('button', { name: '近20日', exact: true })).toHaveClass('selected');
  await page.goto('/news?category=精选'); await expect.poll(() => state.requests.some(item => item.path.startsWith('/api/v1/news/items') && new URL(item.path, 'http://local').searchParams.get('category') === '精选' && new URL(item.path, 'http://local').searchParams.get('limit') === '30')).toBeTruthy();
});

test('saved review defaults and background preparation request the same 60-day range', async ({ page }) => {
  const state = await setup(page, { lookback: 60 }); await page.goto('/research/reviews?view=emotion');
  await expect(page.getByRole('button', { name: '近60日', exact: true })).toHaveClass('selected');
  await page.waitForTimeout(900);
  const series = state.requests.filter(item => item.path.startsWith('/api/v1/review-data/series'));
  expect(series.length).toBeGreaterThan(0); expect(series.every(item => new URL(item.path, 'http://local').searchParams.get('lookback') === '60')).toBeTruthy();
});

test('overview refresh failures keep the same-query service cards available', async ({ page }) => {
  const state = await setup(page); await page.goto('/system'); await expect(page.getByText('数据库（合成）', { exact: true })).toBeVisible();
  state.failOverview = true; await page.getByRole('button', { name: '检查状态', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('合成：状态检查暂时失败'); await expect(page.getByText('数据库（合成）', { exact: true })).toBeVisible();
});

test('password mismatch stays local; successful change expires the whole signed-in screen', async ({ page }) => {
  const state = await setup(page); await page.goto('/system?view=security');
  await page.getByLabel('当前密码', { exact: true }).fill('synthetic-current-password'); await page.getByLabel('新密码', { exact: true }).fill('synthetic-next-password'); await page.getByLabel('确认新密码', { exact: true }).fill('synthetic-wrong-password');
  await page.getByRole('button', { name: '修改密码并重新登录', exact: true }).click(); await expect(page.getByRole('alert')).toContainText('两次输入的新密码不一致');
  expect(state.requests.filter(item => item.path === '/api/v1/me/password')).toHaveLength(0);
  await page.getByLabel('确认新密码', { exact: true }).fill('synthetic-next-password'); await page.getByRole('button', { name: '修改密码并重新登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '登录工作空间' })).toBeVisible(); await expect(page.getByTestId('system-settings')).toHaveCount(0);
  const req = state.requests.find(item => item.path === '/api/v1/me/password')!; expect(req.headers['idempotency-key']).toBeUndefined();
});

test('revoking other sessions preserves the current login and never invents device details', async ({ page }) => {
  const state = await setup(page); await page.goto('/system?view=security');
  await expect(page.getByText('其他登录会话', { exact: true })).toBeVisible(); await page.getByRole('button', { name: '退出其他会话', exact: true }).click();
  await expect(page.getByText('其他登录会话', { exact: true })).toHaveCount(0); await expect(page.getByText('当前登录', { exact: true })).toBeVisible(); expect(state.signedIn).toBeTruthy();
});

test('cache clearing preserves drafts, removes only the current scope, and rejects late cache publication', async ({ page }) => {
  const state = await setup(page);
  await page.addInitScript(() => {
    const fetch = window.fetch.bind(window);
    let release!: () => void, claimed = false;
    const gate = new Promise<void>(resolve => { release = resolve; });
    const late = { entered: false, parsed: false, key: '', release: () => release() };
    (window as any).syntheticLate = late;
    window.fetch = async (input, init) => {
      const url = new URL(input instanceof Request ? input.url : String(input), location.origin);
      if (!claimed && url.pathname === '/api/v1/news/items' && url.searchParams.get('category') === '精选' && url.searchParams.get('limit') === '30') {
        claimed = true;
        // An adversarial transport deliberately ignores abort. Its real response
        // must reach the application's existing cache after clearing finishes.
        const response = await fetch(input, { ...init, signal: undefined });
        url.searchParams.sort(); late.key = url.pathname + '?' + url.searchParams;
        const json = response.json.bind(response);
        response.json = async () => { const value = await json(); late.parsed = true; return value; };
        late.entered = true; await gate; return response;
      }
      return fetch(input, init);
    };
  });
  // Start actual application reads before opening settings; /system itself
  // intentionally has no market/news preparation or development-module imports.
  await page.goto('/workbench');
  await expect(page.locator('.ty-terminal')).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as any).syntheticLate.entered)).toBe(true);
  const heldKey = await page.evaluate(() => (window as any).syntheticLate.key as string);
  const records = () => page.evaluate(async () => {
    const db = await new Promise<IDBDatabase>((resolve, reject) => { const open = indexedDB.open('tianyan-workspace-reads-v1', 1); open.onsuccess = () => resolve(open.result); open.onerror = () => reject(open.error); });
    return new Promise<Array<{ key: string; scope: string }>>((resolve, reject) => { const read = db.transaction('records').objectStore('records').getAll(); read.onsuccess = () => { db.close(); resolve(read.result); }; read.onerror = () => { db.close(); reject(read.error); }; });
  });
  await expect.poll(async () => (await records()).filter(row => row.scope === 'synthetic-scope').length).toBeGreaterThan(0);
  await page.evaluate(async () => {
    const db = await new Promise<IDBDatabase>((resolve, reject) => { const open = indexedDB.open('tianyan-workspace-reads-v1', 1); open.onsuccess = () => resolve(open.result); open.onerror = () => reject(open.error); });
    const tx = db.transaction(['records', 'index'], 'readwrite'), now = Date.now();
    const row = { id: 'other-account\u0000synthetic-other', scope: 'other-account', key: 'synthetic-other', savedAt: now, touched: now, size: 256, value: { marker: 'other' } };
    tx.objectStore('records').put(row); const { value: _value, ...metadata } = row; tx.objectStore('index').put(metadata);
    await new Promise<void>((resolve, reject) => { tx.oncomplete = () => resolve(); tx.onerror = tx.onabort = () => reject(tx.error); }); db.close();
  });
  const before = await records();
  expect(before.filter(row => row.scope === 'synthetic-scope').length).toBeGreaterThan(0);
  expect(before.filter(row => row.scope === 'other-account')).toHaveLength(1);
  await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: '系统设置', exact: true }).click();
  await tab(page, '使用偏好').click();
  await page.getByLabel('默认首页', { exact: true }).selectOption('news');
  await tab(page, '数据维护').click(); await page.getByRole('button', { name: '清理浏览器页面缓存', exact: true }).click(); await page.getByRole('button', { name: '确认清理', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('浏览器页面缓存已清理');
  expect((await records()).filter(row => row.scope === 'synthetic-scope')).toHaveLength(0);
  await page.evaluate(() => (window as any).syntheticLate.release());
  await expect.poll(() => page.evaluate(() => (window as any).syntheticLate.parsed)).toBe(true);
  // Let the real API/cache promise chain consume that response before reading
  // the stores. This is an event-loop barrier, not a network-idle condition.
  await page.evaluate(() => new Promise<void>(resolve => setTimeout(resolve, 0)));
  const after = await records();
  const result = { own: after.filter(row => row.scope === 'synthetic-scope').length, other: after.filter(row => row.scope === 'other-account').length, late: after.some(row => row.scope === 'synthetic-scope' && row.key === heldKey) };
  expect(result).toEqual({ own: 0, other: 1, late: false });
  await tab(page, '使用偏好').click(); await expect(page.getByLabel('默认首页', { exact: true })).toHaveValue('news');
  const canonical = (path: string) => { const url = new URL(path, 'http://synthetic.invalid'); url.searchParams.sort(); return url.pathname + '?' + url.searchParams; };
  const reads = () => state.requests.filter(request => request.method === 'GET' && canonical(request.path) === heldKey).length;
  const previousReads = reads();
  await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: '资讯中心', exact: true }).click();
  await expect(page.getByTestId('news-center')).toBeVisible();
  await expect.poll(reads).toBeGreaterThan(previousReads);
  expect(state.errors).toEqual([]);
});

test('backup tracks real phases and distinguishes file checks from recovery verification', async ({ page }) => {
  const state = await setup(page); await page.goto('/system?view=maintenance'); await page.getByRole('button', { name: '立即备份', exact: true }).click();
  await expect(page.locator('.ss-job')).toContainText('排队中'); await expect(page.locator('.ss-job')).toContainText('已完成', { timeout: 10000 });
  await expect(page.getByRole('region', { name: '备份历史' })).toContainText('文件校验通过'); await expect(page.getByRole('region', { name: '备份历史' })).toContainText('尚未实际恢复验证');
  expect(state.requests.filter(item => item.path === '/api/v1/system/maintenance-jobs' && item.method === 'POST')).toHaveLength(1); await expect(page.getByRole('progressbar')).toHaveCount(0);
});

test('unavailable browser storage clears memory without claiming saved files were removed', async ({ page }) => {
  await page.addInitScript(() => { Object.defineProperty(window, 'indexedDB', { configurable: true, get() { throw new DOMException('Synthetic unavailable storage'); } }); });
  await setup(page); await page.goto('/system?view=maintenance'); await expect(page.getByText('仅内存缓存', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '清理浏览器页面缓存', exact: true }).click(); await page.getByRole('button', { name: '确认清理', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('无法确认持久缓存已清理'); await expect(page.getByRole('status')).toContainText('原始资料与草稿保留');
});

test('ordinary users can clear their browser but cannot run server maintenance', async ({ page }) => {
  const state = await setup(page, { role: 'researcher' }); await page.goto('/system?view=maintenance');
  await expect(page.getByRole('button', { name: '清理浏览器页面缓存', exact: true })).toBeEnabled(); await expect(page.getByRole('button', { name: '清理本人服务端结果', exact: true })).toBeDisabled(); await expect(page.getByRole('button', { name: '立即备份', exact: true })).toBeDisabled();
  await tab(page, '版本与更新').click(); await expect(page.getByRole('heading', { name: '版本更新由管理员维护' })).toBeVisible();
  expect(state.requests.filter(item => item.path === '/api/v1/system/backups' || item.path === '/api/v1/system/updates')).toEqual([]);
});

test('a failed backup progress read retains the known phase and local retry clears its error', async ({ page }) => {
  const state = await setup(page); state.failJobOnce = true; await page.goto('/system?view=maintenance'); await page.getByRole('button', { name: '立即备份', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('合成：进度读取暂时中断', { timeout: 10000 }); await expect(page.locator('.ss-job')).toContainText('排队中');
  await page.getByRole('button', { name: '重试', exact: true }).click(); await expect(page.locator('.ss-job')).toContainText('已完成'); await expect(page.getByText('合成：进度读取暂时中断', { exact: true })).toHaveCount(0);
  expect(state.requests.filter(item => item.path === '/api/v1/system/maintenance-jobs' && item.method === 'POST')).toHaveLength(1);
});

test('unconfigured update source is honest and performs no external or update request', async ({ page }) => {
  const state = await setup(page); await page.goto('/system?view=updates'); await expect(page.getByRole('heading', { name: '更新源未配置', exact: true })).toBeVisible(); await expect(page.getByRole('button', { name: '检查更新', exact: true })).toBeDisabled(); await expect(page.getByText('已是最新版', { exact: true })).toHaveCount(0); expect(state.requests.filter(item => item.method !== 'GET')).toEqual([]);
});

test('download is separate from installation and unsaved edits block upgrade confirmation', async ({ page }) => {
  const state = await setup(page); state.configured = true; state.release = true;
  await page.goto('/system?view=updates'); await page.getByRole('button', { name: '下载并校验更新包', exact: true }).click();
  expect(state.requests.some(item => item.path.endsWith('/updates/download'))).toBeTruthy(); expect(state.requests.some(item => item.path.endsWith('/updates/install'))).toBeFalsy();
  await page.getByRole('button', { name: '检查更新', exact: true }).click(); await expect(page.getByRole('button', { name: '选择现在升级', exact: true })).toBeEnabled();
  await tab(page, '使用偏好').click(); await page.getByLabel('默认首页', { exact: true }).selectOption('news'); await tab(page, '版本与更新').click(); await page.getByRole('button', { name: '选择现在升级', exact: true }).click(); await expect(page.getByRole('alert')).toContainText('还有未保存');
  await tab(page, '使用偏好').click(); await page.getByRole('button', { name: '保存偏好', exact: true }).click(); await expect(page.getByRole('status')).toContainText('使用偏好已保存'); await tab(page, '版本与更新').click(); await page.getByRole('button', { name: '选择现在升级', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '确认现在升级' })).toBeVisible(); await expect(page.getByRole('button', { name: '备份并开始升级', exact: true })).toBeDisabled(); await page.keyboard.press('Escape'); expect(state.requests.some(item => item.path.endsWith('/updates/install'))).toBeFalsy();
});

test('back and forward restore tabs and activity filters use the backend kind contract', async ({ page }) => {
  const state = await setup(page); await page.goto('/system?view=overview'); await tab(page, '使用偏好').click(); await tab(page, '操作记录').click(); await page.getByLabel('操作类型', { exact: true }).selectOption('password');
  await expect.poll(() => state.requests.some(item => item.path.includes('/system/activity?') && item.path.includes('kind=password'))).toBeTruthy(); await page.goBack(); await expect(tab(page, '操作记录')).toHaveAttribute('aria-selected', 'true'); await page.goBack(); await expect(tab(page, '使用偏好')).toHaveAttribute('aria-selected', 'true'); await page.goForward(); await expect(tab(page, '操作记录')).toHaveAttribute('aria-selected', 'true');
  expect(state.errors).toEqual([]);
});

test('confirmed install sends the acknowledged request and opens the independent maintenance page', async ({ page }) => {
  const state = await setup(page); state.configured = true; state.release = true; state.downloaded = true;
  await page.route('**/maintenance/**', route => route.fulfill({ contentType: 'text/html', body: '<h1>合成维护进度 · 测试页面</h1>' }));
  await page.goto('/system?view=updates'); await page.getByRole('button', { name: '选择现在升级', exact: true }).click();
  await page.getByRole('checkbox', { name: '我已保存所有内容，现在可以进入维护并升级' }).check();
  await page.getByRole('button', { name: '备份并开始升级', exact: true }).click();
  await expect(page).toHaveURL(/\/maintenance\/\?job=synthetic-job$/);
  const req = state.requests.find(item => item.path.endsWith('/updates/install'))!;
  expect(req.body).toEqual({ release_id: 'synthetic-release', confirmed: true, drafts_saved: true }); expect(req.headers['x-csrf-token']).toBe('synthetic-csrf'); expect(req.headers['idempotency-key']).toBeTruthy();
});

test('diagnostic export is a local download with no credentials or business data', async ({ page }) => {
  await setup(page); await page.goto('/system');
  const pending = page.waitForEvent('download'); await page.getByRole('button', { name: '导出诊断 仅脱敏运行摘要' }).click(); const download = await pending;
  expect(download.suggestedFilename()).toMatch(/^天衍诊断-/); const stream = await download.createReadStream(); const chunks: Buffer[] = []; for await (const chunk of stream!) chunks.push(chunk); const data = Buffer.concat(chunks).toString('utf8'); expect(data).toContain('synthetic'); expect(data).not.toContain('synthetic-csrf'); expect(data).not.toContain('password'); expect(data).not.toContain('synthetic-settings');
});

test('activity rejects an inverted date window without transmitting the invalid filter', async ({ page }) => {
  const state = await setup(page); await page.goto('/system?view=activity'); await page.getByLabel('开始日期', { exact: true }).fill('2002-01-01'); await page.getByLabel('结束日期', { exact: true }).fill('2001-12-31');
  await expect(page.getByRole('alert')).toContainText('开始日期不能晚于结束日期');
  expect(state.requests.filter(item => item.path.includes('to_date=2001-12-31'))).toEqual([]);
});

test('desktop and mobile settings remain legible without page horizontal overflow', async ({ page }, info) => {
  await setup(page); await page.goto('/system'); await expect(page.getByText('合成验收版本 0.1', { exact: true })).toBeVisible();
  for (const [name, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]] as const) {
    await page.setViewportSize({ width, height });
    for (const [key, label] of [['overview', '系统概览'], ['preferences', '使用偏好'], ['security', '账号安全'], ['maintenance', '数据维护'], ['updates', '版本与更新'], ['activity', '操作记录']]) {
      await tab(page, label).click(); await expect(page.locator('#ss-panel-' + key)).toBeVisible();
      if (key === 'security') await expect(page.getByText('当前登录', { exact: true })).toBeVisible();
      if (key === 'maintenance') await expect(page.getByRole('button', { name: '立即备份', exact: true })).toBeEnabled();
      if (key === 'updates') await expect(page.getByRole('heading', { name: '更新源未配置', exact: true })).toBeVisible();
      if (key === 'activity') await expect(page.getByText('合成偏好修改记录', { exact: true })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
      await page.screenshot({ path: info.outputPath(`${name}-${key}-synthetic.png`), fullPage: true });
    }
  }
});
