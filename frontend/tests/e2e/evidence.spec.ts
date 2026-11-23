import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

async function login(page: Page, path = '/data?view=materials', username = process.env.JL_E2E_USERNAME ?? 'e2e') {
  await page.goto(path);
  await signIn(page, username);
}
async function signIn(page: Page, username = process.env.JL_E2E_USERNAME ?? 'e2e') {
  await page.getByLabel('账号', { exact: true }).fill(username);
  await page.getByLabel('密码', { exact: true }).fill(process.env.JL_E2E_PASSWORD!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('button', { name: /^(退出|退出账号)$/ })).toBeVisible();
}
async function fillMaterial(page: Page, key: string, text = '浏览器隔离合成材料，不是行情或真实研究观点。') {
  await page.getByLabel('材料编号', { exact: true }).fill(key);
  await page.getByLabel('材料内容', { exact: true }).fill(text);
  await page.getByLabel('保存权利依据', { exact: true }).fill('本人自制的隔离演示测试材料');
}
async function expireCookie(page: Page) {
  const cookies = (await page.context().cookies()).filter(cookie => cookie.httpOnly);
  expect(cookies).toHaveLength(1);
  await page.context().addCookies(cookies.map(cookie => ({ ...cookie, expires: Math.floor(Date.now() / 1000) - 1 })));
}
async function saveAfterCookieExpiry(page: Page) {
  // Make the real save the first unauthenticated request. A background GET
  // can otherwise expire the page before the user can submit the draft.
  const backgroundDuringExpiry = (route: Route) => route.request().method() === 'GET'
    ? route.fulfill({ status: 503, json: { error: { code: 'synthetic_background_wait', message: '合成后台读取暂停' } } }) : route.fallback();
  await page.route('**/api/v1/**', backgroundDuringExpiry);
  try {
    const save = page.getByRole('button', { name: '保存材料', exact: true });
    await save.click({ trial: true });
    await expireCookie(page);
    const expiredWrite = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/evidence'
      && response.request().method() === 'POST' && response.status() === 401);
    await save.click();
    await expiredWrite;
    await expect(page.getByRole('heading', { name: '登录工作空间', exact: true })).toBeVisible();
  } finally {
    await page.unroute('**/api/v1/**', backgroundDuringExpiry);
  }
}
async function screenshots(page: Page, info: { outputPath: (name: string) => string }, name: string) {
  for (const width of [1920, 1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1080 });
    await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: 'instant' }));
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`${name}-${width}.png`), fullPage: true });
  }
}

test('private material persists, corrections preserve old text, and real worker freezes verifiable versions', async ({ page }, info) => {
  await login(page);
  const key = 'browser-original-' + Date.now();
  await fillMaterial(page, key, '合成原始记录 <script>window.fixtureInjected=true</script>');
  await page.getByRole('button', { name: '保存材料', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: `已保存材料 ${key} 第 1 版` })).toBeVisible();
  expect(await page.evaluate(() => 'fixtureInjected' in window)).toBe(false);
  await page.reload();
  await expect(page.getByRole('radiogroup', { name: '数据管理工具' }).getByRole('radio', { name: '研究材料', exact: true })).toHaveAttribute('aria-checked', 'true');
  await page.getByRole('button', { name: `更正材料 ${key} 版本 1`, exact: true }).click();
  await expect(page.getByLabel('材料编号', { exact: true })).toHaveAttribute('readonly', '');
  await page.getByLabel('材料内容', { exact: true }).fill('合成更正记录，必须保留旧版。');
  await page.getByRole('button', { name: '保存更正版本', exact: true }).click();
  await expect(page.getByRole('button', { name: `更正材料 ${key} 版本 1`, exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: `更正材料 ${key} 版本 2`, exact: true })).toBeEnabled();
  await page.getByRole('button', { name: `查看材料 ${key} 版本 1`, exact: true }).click();
  await expect(page.locator('.material-body')).toContainText('合成原始记录');
  await expect(page.locator('.material-body')).not.toContainText('合成更正记录');
  await screenshots(page, info, 'materials');
  // A worker can need more than one poll. Keep two real detail responses pending
  // before allowing its actual finished version through; the UI must keep polling.
  let detailReads = 0;
  await page.route('**/api/v1/snapshots/*', async route => {
    const response = await route.fetch();
    const body = await response.json();
    detailReads++;
    await route.fulfill({ response, json: detailReads <= 2 ? { ...body, data: { ...body.data, state: 'pending' } } : body });
  });
  await page.getByRole('button', { name: `冻结材料 ${key} 版本 2`, exact: true }).click();
  await page.getByRole('button', { name: '创建冻结快照', exact: true }).click();
  const detail = page.getByRole('region', { name: '快照详情', exact: true });
  await expect(detail.getByRole('heading', { name: '已冻结', exact: true })).toBeVisible({ timeout: 15_000 });
  expect(detailReads).toBeGreaterThanOrEqual(3);
  await detail.getByRole('button', { name: '核对快照指纹', exact: true }).click();
  await expect(detail.getByText('快照指纹一致', { exact: true })).toBeVisible();
  await expect(page.locator('.snapshot-list')).toContainText('已冻结 · 材料完整');
  await expect(page.locator('.snapshot-list')).not.toContainText('冻结中');
  await expect(detail.getByRole('table')).toContainText('查看材料版本');
  await expect(page.getByRole('button', { name: '创建冻结快照', exact: true })).toBeDisabled();
  await screenshots(page, info, 'snapshot');
  await detail.getByRole('button', { name: /^回查材料版本 / }).click();
  await expect(page.locator('.material-body')).toHaveText('合成更正记录，必须保留旧版。');
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
});

test('unverified external material stays separate from eligible snapshot evidence', async ({ page }) => {
  await login(page);
  const key = 'browser-external-' + Date.now();
  await fillMaterial(page, key, '外部材料的合成摘录，发布时间未知，不能当作历史事实。');
  await page.getByRole('radiogroup', { name: '材料来源类型' }).getByRole('radio', { name: '外部材料', exact: true }).click();
  await page.getByRole('button', { name: '保存材料', exact: true }).click();
  await expect(page.locator('.material-detail')).toContainText('外部材料 · 未核验');
  await expect(page.locator('.material-detail')).toContainText('排除：外部时间尚未核验');
  await page.getByRole('button', { name: `冻结材料 ${key} 版本 1`, exact: true }).click();
  await page.getByRole('button', { name: '创建冻结快照', exact: true }).click();
  const detail = page.getByRole('region', { name: '快照详情', exact: true });
  await expect(detail.getByRole('heading', { name: '已冻结', exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(detail.getByText('材料不足', { exact: true })).toBeVisible();
  await expect(detail.getByText('没有符合截止条件的材料', { exact: true })).toBeVisible();
});

test('admin imports a synthetic calendar and queries only declared coverage', async ({ page }, info) => {
  await login(page, '/data?view=calendar');
  const segments = [{ start: '09:30', end: '11:30', kind: 'continuous' }, { start: '13:00', end: '15:00', kind: 'continuous' }];
  const calendar = { exchange: 'SSE', version: 'browser-synthetic-' + Date.now(), source_ref: '浏览器自制合成日历，不是真实交易安排', rights_basis: '自制隔离测试样本', coverage_start: '2001-12-31', coverage_end: '2002-01-02', sessions: [
    { trade_day: '2001-12-31', is_open: true, segments }, { trade_day: '2002-01-01', is_open: false, segments: [] }, { trade_day: '2002-01-02', is_open: true, segments },
  ] };
  await page.getByLabel('选择日历 JSON 文件', { exact: true }).setInputFiles({ name: 'synthetic-calendar.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(calendar)) });
  await page.getByRole('button', { name: '导入日历版本', exact: true }).click();
  await expect(page.getByText('日历版本已保存，来源仍为人工声明。', { exact: true })).toBeVisible();
  await page.getByLabel('查询日期', { exact: true }).fill('2001-12-31');
  await page.getByRole('button', { name: '查询后续开市日', exact: true }).click();
  await expect(page.getByRole('region', { name: '日历查询结果' })).toContainText('2002-01-02 · 已声明开市');
  await screenshots(page, info, 'calendar');
  await page.getByLabel('查询日期', { exact: true }).fill('2002-01-03');
  await page.getByRole('button', { name: '查询当日', exact: true }).click();
  await expect(page.getByRole('region', { name: '日历查询结果' })).toContainText('交易日未知');
  await page.reload();
  await expect(page.getByRole('button').filter({ hasText: calendar.version })).toBeVisible();
});

test('save failure keeps input and retries the same idempotent operation', async ({ page }, info) => {
  await login(page);
  const key = 'browser-retry-' + Date.now();
  await fillMaterial(page, key, '保存失败后必须保留的合成输入');
  const attempts: string[] = [];
  await page.route('**/api/v1/evidence*', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    attempts.push(route.request().headers()['idempotency-key']);
    if (attempts.length === 1) return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'database_unavailable', message: '合成数据库故障。', details: [] }, request_id: '00000000-0000-4000-8000-000000000005' }) });
    await route.continue();
  });
  await page.getByRole('button', { name: '保存材料', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('00000000-0000-4000-8000-000000000005');
  await expect(page.getByLabel('材料内容', { exact: true })).toHaveValue('保存失败后必须保留的合成输入');
  await page.screenshot({ path: info.outputPath('material-save-error.png'), fullPage: true });
  await page.getByRole('button', { name: '保存材料', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: `已保存材料 ${key}` })).toBeVisible();
  expect(attempts).toHaveLength(2); expect(attempts[0]).toBe(attempts[1]);
});

test('real cookie expiry preserves same-owner draft and retry key; explicit logout clears it', async ({ page }) => {
  await login(page);
  const key = 'browser-expiry-' + Date.now();
  await fillMaterial(page, key, '仅限同账号内存恢复的合成草稿');
  const attempts: string[] = [];
  page.on('request', request => { if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/v1/evidence') attempts.push(request.headers()['idempotency-key']); });
  let release!: () => void; let entered!: () => void; let held = false;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const observed = new Promise<void>(resolve => { entered = resolve; });
  await page.route('**/api/v1/evidence*', async route => {
    if (route.request().method() === 'GET' && new URL(route.request().url()).pathname === '/api/v1/evidence' && !held) {
      held = true; entered(); await gate;
      return route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { code: 'unauthenticated', message: '合成的旧会话延迟响应。', details: [] }, request_id: '00000000-0000-4000-8000-000000000006' }) });
    }
    await route.continue();
  });
  await page.getByRole('button', { name: '刷新材料', exact: true }).click();
  await observed;
  await saveAfterCookieExpiry(page);
  await signIn(page);
  release();
  await expect(page.getByRole('button', { name: /^(退出|退出账号)$/ })).toBeVisible();
  await expect(page.getByLabel('材料内容', { exact: true })).toHaveValue('仅限同账号内存恢复的合成草稿');
  await page.getByRole('button', { name: '保存材料', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: `已保存材料 ${key}` })).toBeVisible();
  expect(attempts).toHaveLength(2); expect(attempts[0]).toBe(attempts[1]);
  await page.getByLabel('材料内容', { exact: true }).fill('主动退出时必须清除的合成草稿');
  await page.getByRole('button', { name: /^(退出|退出账号)$/ }).click();
  await signIn(page);
  await expect(page.getByLabel('材料内容', { exact: true })).toHaveValue('');
});

test('switching to a different real account cannot recover another owner draft', async ({ page }) => {
  await login(page);
  await fillMaterial(page, 'private-owner-draft', '不得交给其他账号的合成草稿');
  await saveAfterCookieExpiry(page);
  await signIn(page, 'e2e-other');
  await expect(page.getByLabel('材料内容', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('保存权利依据', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('材料编号', { exact: true })).not.toHaveValue('private-owner-draft');
});

test('actual viewer account has disabled writes and server rejects a forged save', async ({ page }) => {
  await login(page, '/data?view=materials', 'e2e-viewer');
  await expect(page.getByRole('button', { name: '保存材料', exact: true })).toBeDisabled();
  await expect(page.getByLabel('材料内容', { exact: true })).toBeDisabled();
  const session = await (await page.request.get('/api/v1/session')).json();
  const providers = await (await page.request.get('/api/v1/providers')).json();
  const source = providers.data.items.find((item: { kind: string }) => item.kind === 'manual');
  const response = await page.request.post('/api/v1/evidence', { headers: { Origin: 'http://127.0.0.1:8080', 'X-CSRF-Token': session.data.csrf_token, 'Idempotency-Key': 'viewer-denied-operation' }, data: { source_id: source.id, source_key: 'forged-viewer', source_revision: '1', kind: 'manual', authorship: 'original', payload: { text: '合成越权反例', source_url: null }, rights_basis: '合成' } });
  expect(response.status()).toBe(403);
  await page.getByRole('radiogroup', { name: '数据管理工具' }).getByRole('radio', { name: '冻结快照', exact: true }).click();
  await expect(page.getByRole('button', { name: '创建冻结快照', exact: true })).toBeDisabled();
  await page.getByRole('radiogroup', { name: '数据管理工具' }).getByRole('radio', { name: '交易日历', exact: true }).click();
  await expect(page.getByRole('button', { name: '导入日历版本', exact: true })).toBeDisabled();
});
