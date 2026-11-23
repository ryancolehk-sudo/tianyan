import { expect, test } from '@playwright/test';
import { terminalDay, terminalFixture, stockA, stockB } from './terminalTestFixtures';

test('login, default four-page workbench, retained six-module shell, reload and logout', async ({ page, request }, testInfo) => {
  await expect.poll(async () => (await request.get('/health/ready')).status(), { timeout: 60_000 }).toBe(200);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '登录工作空间' })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('login.png'), fullPage: true });
  await page.getByLabel('账号', { exact: true }).fill(process.env.JL_E2E_USERNAME ?? 'e2e');
  await page.getByLabel('密码', { exact: true }).fill(process.env.JL_E2E_PASSWORD!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('heading', { name: '工作台', exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('workbench.png'), fullPage: true });
  await expect(page.getByRole('navigation', { name: '主导航' }).locator(':scope > a')).toHaveCount(5);
  await expect(page.getByRole('navigation', { name: '工作台页面' }).getByRole('link')).toHaveCount(4);
  await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '九龙术', exact: true })).toHaveCount(0);
  await page.goto('/workbench?legacy=1');
  await expect(page.getByRole('heading', { name: '盘中工作台', exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: '主导航' }).locator(':scope > .nav-item')).toHaveCount(6);
  for (const [path, heading] of [['/auction','工作台'],['/auction?legacy=1','竞价观察'],['/news?legacy=1','实时资讯'],['/research/reviews','每日复盘'],['/research/directions','板块轮动'],['/research/plans','明日预案'],['/research/validations','次日验证'],['/data','数据管理'],['/system','系统设置']]) {
    await page.goto(path);
    await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(path.slice(1).replace(/[^a-z0-9_-]/gi, '-') + '.png'), fullPage: true });
    await page.reload();
    await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
  }
  await page.goto('/data?view=sources');
  await expect(page.getByRole('cell', { name: '未配置', exact: true })).toHaveCount(4);
  await page.getByRole('button', { name: /^(退出|退出账号)$/ }).click();
  await expect(page.getByRole('heading', { name: '登录工作空间' })).toBeVisible();
  expect((await page.request.get('/api/v1/providers')).status()).toBe(401);
});

test('an unavailable backend is not rendered as a successful empty dataset', async ({ page }, testInfo) => {
  await page.route('**/api/v1/session', route => route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({error:{code:'database_unavailable',message:'数据服务暂不可用。',details:[]},request_id:'00000000-0000-4000-8000-000000000001'})}));
  await page.goto('/data');
  await expect(page.getByRole('alert')).toContainText('数据服务暂不可用');
  await expect(page.getByRole('button', {name:'重新加载'})).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('backend-unavailable.png'), fullPage: true });
});

test('modern auction search keeps its historical context in keyboard and new-tab links', async ({ page, context }) => {
  const cutoff = terminalDay + 'T15:05:00+08:00';
  await context.route('**/api/v1/**', route => {
    const url = new URL(route.request().url());
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'real', request_id: 'synthetic',
      generated_at: cutoff, quality: { status: 'partial', reasons: ['合成搜索测试'] } } } });
    if (url.pathname.endsWith('/session')) return ok({ user: { id: 'search-context', username: 'synthetic', role: 'admin' }, csrf_token: 'synthetic' });
    if (url.pathname.endsWith('/instruments')) return ok({ items: [{ id: 'synthetic-instrument-b', symbol: '999992',
      name: '合成搜索乙', exchange: 'SSE', asset_type: 'stock' }], next_cursor: null });
    if (url.pathname.endsWith('/workbench-view')) return ok(terminalFixture(url.searchParams));
    if (url.pathname.endsWith('/ai/settings')) return ok({ enabled: false, features: [] });
    return ok({ items: [], next_cursor: null });
  });
  for (const path of ['/auction', '/workbench']) {
    const query = new URLSearchParams({ view: 'auction', trade_day: terminalDay, as_of_time: cutoff, stock: stockA });
    await page.goto(path + '?' + query);
    await page.getByRole('textbox', { name: '搜索标的', exact: true }).fill('999992');
    await page.getByRole('button', { name: '搜索标的', exact: true }).click();
    const result = page.locator('.search-results a').filter({ hasText: '合成搜索乙' });
    await expect(result).toBeVisible();
    const destination = new URL((await result.getAttribute('href'))!, 'http://synthetic.local');
    expect(destination.pathname).toBe(path);
    expect(Object.fromEntries(destination.searchParams)).toEqual({ ...Object.fromEntries(query),
      stock: stockB, instrument: 'synthetic-instrument-b' });
    const original = page.url();
    const popupPending = context.waitForEvent('page');
    await result.click({ modifiers: ['Control'] });
    const popup = await popupPending;
    await expect(popup).toHaveURL(new RegExp(path + '\\?'));
    expect(new URL(popup.url()).searchParams.get('as_of_time')).toBe(cutoff);
    expect(page.url()).toBe(original);
    await popup.close();
    await result.press('Enter');
    await expect(page).toHaveURL(url => url.pathname === path && url.searchParams.get('stock') === stockB
      && url.searchParams.get('trade_day') === terminalDay && url.searchParams.get('as_of_time') === cutoff
      && url.searchParams.get('view') === 'auction');
    await expect(page.getByRole('link', { name: '竞价观察', exact: true })).toHaveAttribute('aria-current', 'page');
  }
});

test('a session rejected with 401 returns to login', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('账号', { exact: true }).fill(process.env.JL_E2E_USERNAME ?? 'e2e');
  await page.getByLabel('密码', { exact: true }).fill(process.env.JL_E2E_PASSWORD!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('heading', { name: '工作台', exact: true })).toBeVisible();
  // Server-side expiry is tested against PostgreSQL; this checks the browser's 401 handling.
  await page.route('**/api/v1/integrations', route => route.fulfill({ status: 401,
    contentType: 'application/json', body: JSON.stringify({ error: { code: 'unauthenticated',
      message: '请登录后继续。', details: [] }, request_id: '00000000-0000-4000-8000-000000000001' }) }));
  await page.goto('/data');
  await expect(page.getByRole('heading', { name: '登录工作空间' })).toBeVisible();
});

test('an expired browser cookie requires a fresh login with the real backend', async ({ page, context }) => {
  await page.goto('/');
  await page.getByLabel('账号', { exact: true }).fill(process.env.JL_E2E_USERNAME ?? 'e2e');
  await page.getByLabel('密码', { exact: true }).fill(process.env.JL_E2E_PASSWORD!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('heading', { name: '工作台', exact: true })).toBeVisible();
  const cookies = (await context.cookies()).filter(cookie => cookie.httpOnly);
  expect(cookies).toHaveLength(1);
  // Move the real session cookie past its expiry; no intercepted API response or test backdoor.
  await context.addCookies(cookies.map(cookie => ({ ...cookie, expires: Math.floor(Date.now() / 1000) - 60 })));
  await page.reload();
  await expect(page.getByRole('heading', { name: '登录工作空间' })).toBeVisible();
  expect((await page.request.get('/api/v1/providers')).status()).toBe(401);
});
