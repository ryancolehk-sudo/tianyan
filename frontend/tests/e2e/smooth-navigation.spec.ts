import { openLegacyWorkbench } from './legacyWorkbench';
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { signIn } from './researchTestHelpers';

async function probe(page: Page) {
  return page.evaluate(() => {
    const value = { token: crypto.randomUUID(), origin: performance.timeOrigin,
      shell: document.querySelector('.app'), sidebar: document.querySelector('.sidebar'),
      header: document.querySelector('.topbar'), market: document.querySelector('.market-strip'),
      logo: document.querySelector('.sidebar .brand img'), flashes: 0 };
    (window as any).__navigationProbe = value;
    new MutationObserver(() => {
      if (document.querySelector('.loading')?.textContent?.includes('正在连接天衍工作空间')) value.flashes++;
    }).observe(document.getElementById('root')!, { childList: true, subtree: true });
    return value.token;
  });
}
async function continuous(page: Page, token: string) {
  expect(await page.evaluate(() => {
    const value = (window as any).__navigationProbe;
    return value ? { token: value.token, sameDocument: value.origin === performance.timeOrigin,
      stable: ['shell', 'sidebar', 'header', 'market', 'logo'].every(key => value[key]?.isConnected), flashes: value.flashes } : null;
  })).toEqual({ token, sameDocument: true, stable: true, flashes: 0 });
}
const mainNav = (page: Page) => page.getByRole('navigation', { name: '主导航', exact: true });
async function openRetainedLink(page: Page, name: '数据管理' | '每日复盘') {
  const link = mainNav(page).getByRole('link', { name, exact: true });
  // Legacy regression paths are explicit; ordinary links keep the new defaults.
  await link.evaluate((anchor, href) => anchor.setAttribute('href', href),
    name === '数据管理' ? '/data?view=sources' : '/research/reviews?legacy=1');
  await link.click();
}

test('default workbench tabs preserve their shell and legacy module links switch shells within one document', async ({ page }) => {
  await page.goto('/workbench'); await signIn(page);
  await expect(page.getByRole('heading', { name: '工作台', exact: true })).toBeVisible();
  await page.evaluate(() => {
    (window as any).__modernNavigationProbe = { origin: performance.timeOrigin, header: document.querySelector('.ty-topbar') };
  });
  const documents: string[] = [];
  page.on('request', request => { if (request.isNavigationRequest() && request.frame() === page.mainFrame()) documents.push(request.url()); });
  const tabs = page.getByRole('navigation', { name: '工作台页面', exact: true });
  for (const name of ['竞价观察', '连板观察', '资金动向', '综合盯盘']) {
    await tabs.getByRole('link', { name, exact: true }).click();
    await expect(tabs.getByRole('link', { name, exact: true })).toHaveAttribute('aria-current', 'page');
    expect(await page.evaluate(() => (window as any).__modernNavigationProbe.header.isConnected)).toBe(true);
  }
  await mainNav(page).getByRole('link', { name: '系统设置', exact: true }).click();
  await expect(page.getByRole('heading', { name: '系统设置', exact: true })).toBeVisible();
  await expect(page.getByRole('tablist', { name: '系统设置页面' })).toBeVisible();
  expect(await page.evaluate(() => (window as any).__modernNavigationProbe.header.isConnected)).toBe(true);
  await mainNav(page).getByRole('link', { name: '工作台', exact: true }).click();
  await mainNav(page).getByRole('link', { name: '数据管理', exact: true }).click();
  await expect(page.getByRole('heading', { name: '数据管理', exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as any).__modernNavigationProbe.header.isConnected)).toBe(false);
  await expect(page.locator('.app .sidebar')).toBeVisible();
  await page.evaluate(() => { (window as any).__modernNavigationProbe.legacyHeader = document.querySelector('.topbar'); });
  await mainNav(page).getByRole('link', { name: '工作台', exact: true }).click();
  await expect(page.getByRole('heading', { name: '工作台', exact: true })).toBeVisible();
  await expect(page.locator('.ty-sidebar')).toBeVisible();
  expect(await page.evaluate(() => {
    const probe = (window as any).__modernNavigationProbe;
    return { sameDocument: performance.timeOrigin === probe.origin, oldShellConnected: probe.legacyHeader.isConnected };
  })).toEqual({ sameDocument: true, oldShellConnected: false });
  expect(documents).toEqual([]);
});

test('fixed terminal phone login and keyboard drawer navigation remain usable', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/workbench');
  await page.getByLabel('账号', { exact: true }).fill(process.env.JL_E2E_USERNAME ?? 'e2e');
  await page.getByLabel('密码', { exact: true }).fill(process.env.JL_E2E_PASSWORD!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  // Logout is inside the initially closed Terminal drawer. Verify the protected
  // page instead; the legacy phone case below still tests its visible logout.
  await expect(page.getByRole('heading', { name: '工作台', exact: true })).toBeVisible();
  const origin = await page.evaluate(() => performance.timeOrigin);
  const opener = page.getByRole('button', { name: '打开主导航', exact: true });
  await expect(mainNav(page)).not.toBeVisible();
  await opener.click(); await expect(mainNav(page)).toBeVisible();
  await page.keyboard.press('Escape'); await expect(opener).toBeFocused();
  await expect(mainNav(page)).not.toBeVisible();
  await opener.click();
  const destination = mainNav(page).getByRole('link', { name: '数据管理', exact: true });
  await destination.focus(); await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { name: '数据管理', exact: true })).toBeVisible();
  await expect(mainNav(page)).not.toBeVisible();
  await expect(page.locator('#main-content')).toBeFocused();
  expect(await page.evaluate(() => performance.timeOrigin)).toBe(origin);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('terminal-keyboard-navigation-390.png') });
});

test('retained legacy module and research links keep one document and the actual shell DOM with no startup flash', async ({ page }, info) => {
  await page.goto('/workbench?legacy=1'); await signIn(page);
  const token = await probe(page);
  const documents: string[] = []; page.on('request', request => { if (request.isNavigationRequest() && request.frame() === page.mainFrame()) documents.push(request.url()); });
  const nav = mainNav(page);
  for (const name of ['竞价观察', '数据管理', '盘中工作台']) {
    if (name === '竞价观察' || name === '盘中工作台') await openLegacyWorkbench(page, name);
    else if (name === '数据管理') await openRetainedLink(page, name);
    else await nav.getByRole('link', { name, exact: true }).click();
    await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
    await continuous(page, token);
    await expect(page.locator('#main-content')).toBeFocused();
    await expect(page).toHaveTitle(name + ' · 天衍系统');
  }
  await nav.getByRole('button', { name: '复盘研究', exact: true }).click();
  for (const name of ['每日复盘', '板块轮动', '明日预案', '次日验证']) {
    if (name === '每日复盘') await openRetainedLink(page, name);
    else await nav.getByRole('link', { name, exact: true }).click();
    await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
    await continuous(page, token);
  }
  await page.screenshot({ path: info.outputPath('smooth-validation-1440.png') });
  await nav.getByRole('button', { name: '实时资讯', exact: true }).click();
  await nav.getByRole('link', { name: '事件分析', exact: true }).click();
  await expect(page.getByRole('radio', { name: '事件分析', exact: true })).toBeChecked();
  await continuous(page, token);
  expect(documents).toEqual([]);
  await page.screenshot({ path: info.outputPath('smooth-news-1440.png') });
});

test('query controls, same-path links and browser history initialize the right view without reloading', async ({ page }) => {
  await page.goto('/news?view=feed&legacy=1'); await signIn(page); const token = await probe(page);
  const views = page.getByRole('radiogroup', { name: '资讯工作视图' });
  await page.getByRole('textbox', { name: '资讯关键词', exact: true }).fill('合成筛选词');
  await views.getByRole('radio', { name: '事件分析', exact: true }).click();
  await views.getByRole('radio', { name: '题材专题', exact: true }).click();
  await page.goBack(); await expect(views.getByRole('radio', { name: '事件分析', exact: true })).toBeChecked();
  await page.goForward(); await expect(views.getByRole('radio', { name: '题材专题', exact: true })).toBeChecked();
  await expect(page.getByRole('textbox', { name: '资讯关键词', exact: true })).toHaveValue('合成筛选词');
  await mainNav(page).getByRole('link', { name: '我的关注', exact: true }).click();
  await expect(views.getByRole('radio', { name: '我的关注', exact: true })).toBeChecked();
  await continuous(page, token);
  await openRetainedLink(page, '数据管理');
  await page.getByRole('radio', { name: '研究材料', exact: true }).click();
  await expect(page.getByLabel('材料编号', { exact: true })).toBeVisible();
  await openLegacyWorkbench(page);
  await page.goBack(); await expect(page.getByLabel('材料编号', { exact: true })).toBeVisible();
  await continuous(page, token);
  await page.reload(); await expect(page.getByLabel('材料编号', { exact: true })).toBeVisible();
});

test('returning to a long review restores scroll and hash links focus their actual destination', async ({ page }, info) => {
  await page.goto('/research/reviews?date=2001-12-31'); await signIn(page); const token = await probe(page);
  await page.locator('.review-core').evaluate(element => window.scrollTo(0, element.getBoundingClientRect().top + scrollY - 100));
  const position = await page.evaluate(() => scrollY); expect(position).toBeGreaterThan(300);
  // Clicking sticky navigation may scroll it into view first. Restore the actual
  // departure position, captured after that adjustment and before navigation.
  await mainNav(page).getByRole('link', { name: /^(?:盘中)?工作台$/, exact: true }).evaluate(link => {
    link.addEventListener('pointerdown', () => { (window as any).__reviewDepartureY = scrollY; }, { once: true });
  });
  await openLegacyWorkbench(page);
  const departure = await page.evaluate(() => Number((window as any).__reviewDepartureY));
  expect(departure).toBeGreaterThan(0);
  await expect(page.getByRole('heading', { name: '盘中工作台', exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(0);
  await page.goBack(); await expect(page.getByRole('heading', { name: '每日复盘', exact: true })).toBeVisible();
  await expect.poll(async () => Math.abs(await page.evaluate(() => scrollY) - departure)).toBeLessThan(2);
  await continuous(page, token); await page.screenshot({ path: info.outputPath('smooth-return-scroll-1440.png') });
  await page.getByRole('link', { name: '查看当日记录', exact: true }).click();
  await expect(page.locator('#research-records')).toBeFocused();
  await expect(page).toHaveURL(/#research-records$/); await continuous(page, token);
  await page.getByRole('link', { name: '跳至页面内容', exact: true }).focus(); await page.keyboard.press('Enter');
  await expect(page.locator('#main-content')).toBeFocused(); await continuous(page, token);
});

test('keyboard and phone navigation close the drawer while new tabs and downloads keep native behavior', async ({ page, context }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/workbench?legacy=1'); await signIn(page); const token = await probe(page);
  await page.getByRole('button', { name: '打开主导航', exact: true }).click();
  const link = mainNav(page).getByRole('link', { name: '竞价观察', exact: true });
  await link.evaluate(anchor => anchor.setAttribute('href', '/auction?legacy=1'));
  await link.focus(); await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { name: '竞价观察', exact: true })).toBeVisible();
  await expect(mainNav(page)).not.toBeVisible(); await continuous(page, token);
  await expect(page.locator('#main-content')).toBeFocused();
  await page.screenshot({ path: info.outputPath('smooth-auction-390.png') });
  await page.getByRole('button', { name: '打开主导航', exact: true }).click();
  await mainNav(page).getByRole('button', { name: '复盘研究', exact: true }).click();
  await openRetainedLink(page, '每日复盘');
  await expect(page.getByRole('heading', { name: '每日复盘', exact: true })).toBeVisible();
  await expect(mainNav(page)).not.toBeVisible(); await continuous(page, token);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('smooth-review-390.png') });
  // These harmless synthetic links exercise browser contracts, not business data or external services.
  await page.evaluate(() => {
    const links = document.createElement('div'); links.id = 'native-fixtures';
    links.innerHTML = '<a id="new-tab-fixture" href="/news" target="_blank">合成新标签</a> <a id="download-fixture" href="data:text/plain,synthetic" download="synthetic.txt">合成下载</a> <a id="modifier-fixture" href="/auction">合成修饰键链接</a>';
    document.querySelector('#main-content')!.prepend(links);
  });
  const [popup] = await Promise.all([context.waitForEvent('page'), page.locator('#new-tab-fixture').click()]);
  await popup.waitForURL(/\/news$/); await popup.close(); await continuous(page, token);
  const [download] = await Promise.all([page.waitForEvent('download'), page.locator('#download-fixture').click()]);
  expect(download.suggestedFilename()).toBe('synthetic.txt'); await continuous(page, token);
  const [modified] = await Promise.all([context.waitForEvent('page'), page.locator('#modifier-fixture').click({ modifiers: ['Control'] })]);
  await modified.waitForURL(/\/auction$/); await modified.close(); await continuous(page, token);
});

test('background session checks keep the shell on temporary errors and real expiry removes protected content', async ({ page, context }, info) => {
  await page.goto('/workbench?legacy=1'); await signIn(page); const token = await probe(page);
  await page.route('**/api/v1/session', route => route.request().method() === 'GET' ? route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'database_unavailable', message: '合成临时服务故障。', details: [] } }) }) : route.continue());
  await openLegacyWorkbench(page, '竞价观察');
  await expect(page.getByRole('alert')).toContainText('合成临时服务故障'); await continuous(page, token);
  await page.screenshot({ path: info.outputPath('smooth-session-error-1440.png') });
  await page.unroute('**/api/v1/session');
  const recoveredCheck = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/session' && response.request().method() === 'GET' && response.status() === 200);
  await page.getByRole('button', { name: '重新加载', exact: true }).click();
  await (await recoveredCheck).finished();
  // The alert clears when a check starts. Drain its completed response before
  // expiring cookies, otherwise the next navigation can share that old GET.
  await page.evaluate(() => new Promise<void>(resolve => setTimeout(resolve, 0)));
  await expect(page.getByRole('alert')).toHaveCount(0); await continuous(page, token);
  // Isolate navigation's session check: an in-flight cache GET or a poll may
  // otherwise observe expiry first and remove the sidebar before this click.
  // The session endpoint remains real, including its actual expired cookie.
  await page.route('**/api/v1/**', route => new URL(route.request().url()).pathname === '/api/v1/session'
    || route.request().method() !== 'GET' ? route.fallback() : route.fulfill({ status: 503,
      json: { error: { code: 'synthetic_cache_wait', message: '合成缓存暂不可用', details: [] } } }));
  const cookies = (await context.cookies()).filter(cookie => cookie.httpOnly); expect(cookies).toHaveLength(1);
  await context.addCookies(cookies.map(cookie => ({ ...cookie, expires: Math.floor(Date.now() / 1000) - 60 })));
  expect((await page.request.get('/api/v1/session')).status()).toBe(401);
  // News has no protected data request: navigation itself must verify the real expired session.
  const expiredCheck = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/session' && response.request().method() === 'GET' && response.status() === 401);
  await mainNav(page).getByRole('button', { name: '实时资讯', exact: true }).click();
  await mainNav(page).getByRole('link', { name: '实时列表', exact: true }).click();
  await expiredCheck;
  await expect(page.getByRole('heading', { name: '登录工作空间', exact: true })).toBeVisible();
  await expect(page.getByText('会话已失效，请重新登录。', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as any).__navigationProbe?.token)).toBe(token);
  await expect(page.locator('.app')).toHaveCount(0);
  await page.unroute('**/api/v1/**');
  await signIn(page);
  const logout = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/session' && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: '退出', exact: true }).click();
  await expect(page.getByRole('heading', { name: '登录工作空间', exact: true })).toBeVisible();
  // The login screen is immediate, while revoking the real server session is
  // asynchronous. Protected API checks must follow its completed DELETE.
  const loggedOut = await logout; expect(loggedOut.status()).toBe(204); await loggedOut.finished();
  await page.goBack(); await expect(page.locator('.app')).toHaveCount(0);
  expect((await page.request.get('/api/v1/providers')).status()).toBe(401);
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
});

test('a delayed successful navigation check cannot restore the old identity after logout and another login', async ({ page }) => {
  await page.goto('/workbench?legacy=1'); await signIn(page);
  let entered!: () => void; let release!: () => void; let finished!: () => void; let held = false;
  const observed = new Promise<void>(resolve => { entered = resolve; });
  const gate = new Promise<void>(resolve => { release = resolve; });
  const delivered = new Promise<void>(resolve => { finished = resolve; });
  await page.route('**/api/v1/session', async route => {
    if (route.request().method() !== 'GET' || held) return route.continue();
    held = true; const response = await route.fetch(); entered(); await gate;
    await route.fulfill({ response }); finished();
  });
  await mainNav(page).getByRole('button', { name: '实时资讯', exact: true }).click();
  await mainNav(page).getByRole('link', { name: '实时列表', exact: true }).click(); await observed;
  await page.getByRole('button', { name: '退出', exact: true }).click();
  await expect(page.getByRole('heading', { name: '登录工作空间', exact: true })).toBeVisible();
  await signIn(page, 'e2e-other'); release();
  await expect(page.locator('.account-name')).toHaveText('e2e-other');
  // Wait only for the held authentication response; market/background polling
  // intentionally keeps the authenticated application network active.
  await delivered;
  await page.evaluate(() => new Promise<void>(resolve => setTimeout(resolve, 0)));
  await expect(page.locator('.account-name')).toHaveText('e2e-other');
  expect((await (await page.request.get('/api/v1/session')).json()).data.user.username).toBe('e2e-other');
  await expect(page.locator('.account-name')).toHaveText('e2e-other');
});
