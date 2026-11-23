import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import type { Request, Response, Route } from '@playwright/test';
import { day, login, fixture, begin, screenshots, post, reviewBody, signIn, session } from './researchTestHelpers';

test('review save/publish, direction chooses published history, and later R2 preserves the old reference', async ({ page }, info) => {
  await login(page); const f = await fixture(page); const title = '合成复盘-' + f.key;
  const editor = await begin(page, f, title);
  await screenshots(page, info, 'review-editor');
  const created = page.waitForResponse(response => response.url().endsWith('/api/v1/research-documents') && response.request().method() === 'POST');
  await editor.getByRole('button', { name: '保存草稿', exact: true }).click();
  const document = (await (await created).json()).data;
  await expect(editor.getByRole('status').filter({ hasText: '已保存第 1 版' })).toContainText('已保存第 1 版');
  await expect(editor.getByRole('button', { name: '保存草稿', exact: true })).toBeDisabled();
  await editor.getByRole('button', { name: '发布此版本', exact: true }).click();
  await expect(editor.getByRole('status').filter({ hasText: '已发布第 1 版' })).toContainText('已发布第 1 版');
  await editor.getByRole('button', { name: '收起', exact: true }).click();
  await page.reload(); await page.getByRole('button', { name: `查看研究 ${title}`, exact: true }).click();
  await expect(page.locator('.saved-research-content')).toContainText('合成复盘第一版');
  expect(await page.evaluate(() => 'researchInjected' in window)).toBe(false);
  await page.reload(); await expect(page.getByRole('dialog', { name: '研究版本详情' })).toBeVisible();
  const r1 = document.head_revision_id;
  const r2 = await post(page, `research-documents/${document.id}/revisions`, reviewBody(f, title + '-R2', '第二版，不应改写旧方向引用。'), 1);
  await page.getByRole('button', { name: '收起', exact: true }).click();
  await page.goto('/research/directions?date=' + day);
  await page.getByRole('button', { name: '新建方向研究', exact: true }).last().click();
  const direction = page.getByRole('dialog', { name: '编辑方向研究' }); const directionTitle = '合成方向-' + f.key;
  await direction.getByLabel('研究标题', { exact: true }).fill(directionTitle);
  await direction.getByLabel('选择冻结快照', { exact: true }).selectOption(f.snapshot.id);
  await direction.getByLabel('日历版本', { exact: true }).selectOption(f.calendar.id);
  await direction.getByLabel('选择复盘文档', { exact: true }).selectOption(document.id);
  await direction.getByRole('checkbox', { name: `引用复盘 ${title} 第 1 版`, exact: true }).check();
  await direction.getByLabel('方向 1 名称', { exact: true }).fill('合成观察方向');
  await direction.getByLabel('方向 1 人工判断', { exact: true }).fill('隔离测试的人工判断，无真实行情。');
  await direction.getByRole('checkbox', { name: `引用材料 ${f.key} 第 1 版`, exact: true }).check();
  await screenshots(page, info, 'direction-editor');
  await direction.getByRole('button', { name: '保存草稿', exact: true }).click();
  await expect(direction.getByRole('status').filter({ hasText: '已保存第 1 版' })).toContainText('已保存第 1 版');
  await direction.getByRole('button', { name: '发布此版本', exact: true }).click();
  await expect(direction.getByRole('status').filter({ hasText: '已发布第 1 版' })).toContainText('已发布第 1 版');
  await direction.getByRole('button', { name: '收起', exact: true }).click();
  await post(page, `revisions/${r2.id}/publish`, {}, 2);
  await page.getByRole('button', { name: `查看研究 ${directionTitle}`, exact: true }).click();
  const fixed = page.getByRole('link', { name: new RegExp(title + '.*第 1 版') });
  await expect(fixed).toHaveAttribute('href', new RegExp('revision=' + r1));
  await expect(page.getByRole('link', { name: new RegExp('^' + f.key + '.*第 1 版') })).toHaveAttribute('href', new RegExp('evidence=' + f.material.id));
  await screenshots(page, info, 'direction-fixed-version');
  const target = await fixed.getAttribute('href'); await page.goto(target!);
  await expect(page.locator('.saved-research-content')).toContainText('合成复盘第一版');
  await expect(page.locator('.saved-research-content')).not.toContainText('第二版');
  await expect(page.getByText('历史版本', { exact: false }).first()).toBeVisible();
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
});

test('two-window revision conflict preserves typed text and rebases explicitly', async ({ page, context }, info) => {
  await login(page); const f = await fixture(page); const title = '冲突复盘-' + f.key;
  const document = await post(page, 'research-documents', { kind: 'review', source_trade_day: day, revision: reviewBody(f, title) });
  await page.reload(); await page.getByRole('button', { name: `编辑研究 ${title}`, exact: true }).click();
  const first = page.getByRole('dialog', { name: '编辑每日复盘' }); await first.getByLabel('复盘正文', { exact: true }).fill('窗口A尚未保存的输入');
  const second = await context.newPage(); await second.goto('/research/reviews?date=' + day);
  await second.getByRole('button', { name: `编辑研究 ${title}`, exact: true }).click();
  const other = second.getByRole('dialog', { name: '编辑每日复盘' }); await other.getByLabel('复盘正文', { exact: true }).fill('窗口B先保存的第二版');
  await other.getByRole('button', { name: '保存草稿', exact: true }).click(); await expect(other.getByRole('status').filter({ hasText: '已保存第 2 版' })).toContainText('已保存第 2 版');
  await first.getByRole('button', { name: '保存草稿', exact: true }).click();
  await expect(first.getByRole('region', { name: '版本冲突比较' })).toContainText('窗口B先保存的第二版');
  await expect(first.getByLabel('复盘正文', { exact: true })).toHaveValue('窗口A尚未保存的输入');
  await screenshots(page, info, 'review-conflict');
  await first.getByRole('button', { name: '保留我的输入，以最新版本继续', exact: true }).click();
  await first.getByRole('button', { name: '保存草稿', exact: true }).click(); await expect(first.getByRole('status').filter({ hasText: '已保存第 3 版' })).toContainText('已保存第 3 版');
  const latest = (await (await page.request.get('/api/v1/research-documents/' + document.id)).json()).data;
  const history = (await (await page.request.get(`/api/v1/research-documents/${document.id}/revisions`)).json()).data.items;
  expect(latest.head_revision_number).toBe(3); expect(history.map((row: { revision_number: number }) => row.revision_number)).toEqual([3, 2, 1]);
  await first.getByRole('button', { name: '收起', exact: true }).click();
  await page.getByRole('button', { name: `历史版本 ${title}`, exact: true }).click();
  await page.getByRole('button', { name: new RegExp(title + '.*第 1 版') }).click();
  await expect(page.locator('.saved-research-content')).toContainText('合成复盘第一版');
  await expect(page.getByRole('textbox', { name: '复盘正文', exact: true })).toHaveCount(0);
  await second.close();
});

test('lost create response retries its original operation and never duplicates a document', async ({ page }) => {
  await login(page); const f = await fixture(page); const title = '幂等复盘-' + f.key; const editor = await begin(page, f, title);
  const keys: string[] = [];
  await page.route('**/api/v1/research-documents', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    keys.push(route.request().headers()['idempotency-key']);
    if (keys.length === 1) {
      const actual = await route.fetch(); expect(actual.status()).toBe(201);
      return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'fixture_lost_reply', message: '合成故障：写入成功后的响应丢失。' }, request_id: '00000000-0000-4000-8000-000000000003' }) });
    }
    return route.continue();
  });
  await editor.getByRole('button', { name: '保存草稿', exact: true }).click();
  await expect(editor.getByRole('alert')).toContainText('响应丢失');
  await expect(editor.getByLabel('研究标题', { exact: true })).toHaveValue(title);
  await editor.getByRole('button', { name: '保存草稿', exact: true }).click();
  await expect(editor.getByRole('status').filter({ hasText: '已保存第 1 版' })).toContainText('已保存第 1 版'); expect(keys).toHaveLength(2); expect(keys[1]).toBe(keys[0]);
  await editor.getByRole('button', { name: '收起', exact: true }).click();
  await expect(page.getByRole('button', { name: `查看研究 ${title}`, exact: true })).toHaveCount(1);
});

test('confirmed create with failed read-back stays saved and can be reread without creating again', async ({ page }) => {
  await login(page); const f = await fixture(page); const title = '回读复盘-' + f.key; const editor = await begin(page, f, title);
  let once = true;
  await page.route('**/api/v1/revisions/*', route => {
    if (!once || route.request().method() !== 'GET') return route.continue(); once = false;
    return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'fixture', message: '合成回读故障' }, request_id: '00000000-0000-4000-8000-000000000004' }) });
  });
  await editor.getByRole('button', { name: '保存草稿', exact: true }).click();
  await expect(editor.getByRole('alert')).toContainText('合成回读故障');
  await expect(editor.getByRole('button', { name: '保存草稿', exact: true })).toBeDisabled();
  await editor.getByRole('button', { name: '重新读取已保存版本', exact: true }).click(); await expect(editor.getByRole('status').filter({ hasText: '重新核对' })).toContainText('重新核对');
});

test('expired real cookie restores input and retry identity, while explicit logout clears it', async ({ page }) => {
  const pendingGets = new Set<Request>();
  page.on('request', request => { if (request.method() === 'GET' && new URL(request.url()).pathname.startsWith('/api/v1/')) pendingGets.add(request); });
  const finished = (request: Request) => { pendingGets.delete(request); };
  page.on('requestfinished', finished); page.on('requestfailed', finished);
  await login(page); const f = await fixture(page); const title = '会话复盘-' + f.key; const editor = await begin(page, f, title);
  // Only the real save may observe expiry. Drain existing authenticated GETs
  // and prevent new background reads from removing the editor before clicking.
  const backgroundDuringExpiry = (route: Route) => route.request().method() === 'GET'
    ? route.fulfill({ status: 503, json: { error: { code: 'synthetic_background_wait', message: '合成后台读取暂停' } } }) : route.fallback();
  await page.route('**/api/v1/**', backgroundDuringExpiry);
  await expect.poll(() => pendingGets.size).toBe(0);
  await expect(editor.getByRole('button', { name: '保存草稿', exact: true })).toBeEnabled();
  const cookies = (await page.context().cookies()).filter(cookie => cookie.httpOnly); expect(cookies).toHaveLength(1);
  await page.context().addCookies(cookies.map(cookie => ({ ...cookie, expires: Math.floor(Date.now() / 1000) - 1 })));
  const keys: string[] = [];
  page.on('request', request => { if (request.url().endsWith('/api/v1/research-documents') && request.method() === 'POST') keys.push(request.headers()['idempotency-key']); });
  const expiredSave = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/research-documents' && response.request().method() === 'POST');
  await editor.getByRole('button', { name: '保存草稿', exact: true }).click();
  expect((await expiredSave).status()).toBe(401);
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  await page.unroute('**/api/v1/**', backgroundDuringExpiry); await signIn(page);
  const restored = page.getByRole('dialog', { name: '编辑每日复盘' }); await expect(restored.getByLabel('研究标题', { exact: true })).toHaveValue(title);
  await restored.getByRole('button', { name: '保存草稿', exact: true }).click();
  // Calendar/material loading may coexist with the operation status after
  // re-authentication. Match the successful save, never an arbitrary status.
  await expect(restored.getByRole('status').filter({ hasText: '已保存第 1 版' })).toBeVisible(); expect(keys).toHaveLength(2); expect(keys[0]).toBeTruthy(); expect(keys[1]).toBe(keys[0]);
  await restored.getByLabel('复盘正文', { exact: true }).fill('主动退出应该清除的未保存输入');
  await restored.getByRole('button', { name: '收起', exact: true }).click();
  await expect(page.getByRole('button', { name: `查看研究 ${title}`, exact: true })).toHaveCount(1);
  const loggedOut = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/session' && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: '退出', exact: true }).click(); expect((await loggedOut).status()).toBe(204); await signIn(page);
  await page.getByRole('button', { name: '新建每日复盘', exact: true }).last().click(); await expect(page.getByLabel('研究标题', { exact: true })).toHaveValue('');
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
});

test('switching account clears private input, viewer cannot write, and cross-owner history stays hidden', async ({ page }) => {
  await login(page); const f = await fixture(page); const doc = await post(page, 'research-documents', { kind: 'review', source_trade_day: day, revision: reviewBody(f, '私有复盘-' + f.key) });
  const editor = await begin(page, f, '原账号未保存的私有输入'); await editor.getByRole('button', { name: '收起', exact: true }).click();
  const expiredGets: string[] = [], account = page.getByLabel('账号', { exact: true });
  const observeExpiry = (response: Response) => { if (response.status() === 401 && response.request().method() === 'GET' && new URL(response.url()).pathname.startsWith('/api/v1/')) expiredGets.push(new URL(response.url()).pathname); };
  page.on('response', observeExpiry);
  try {
    const cookies = (await page.context().cookies()).filter(cookie => cookie.httpOnly); expect(cookies).toHaveLength(1);
    await page.context().addCookies(cookies.map(cookie => ({ ...cookie, expires: Math.floor(Date.now() / 1000) - 1 })));
    // Either a background GET or this refresh may observe the real expiry.
    // A disappeared refresh is acceptable only when Login is already visible.
    if (!await account.isVisible()) {
      try { await page.getByRole('button', { name: '刷新记录', exact: true }).click({ timeout: 5000 }); }
      catch (error) { if (!await account.isVisible()) throw error; }
    }
    await expect(account).toBeVisible();
    await expect.poll(() => expiredGets.length).toBeGreaterThan(0);
  } finally { page.off('response', observeExpiry); }
  await signIn(page, 'e2e-other');
  await page.getByRole('button', { name: '新建每日复盘', exact: true }).last().click(); await expect(page.getByLabel('研究标题', { exact: true })).toHaveValue('');
  await page.getByRole('button', { name: '收起', exact: true }).click();
  expect((await page.request.get(`/api/v1/research-documents/${doc.id}/revisions`)).status()).toBe(404);
  let loggedOut: Response | undefined;
  const observeLogout = (response: Response) => { if (new URL(response.url()).pathname === '/api/v1/session' && response.request().method() === 'DELETE') loggedOut = response; };
  page.on('response', observeLogout);
  try {
    await page.getByRole('button', { name: '退出', exact: true }).click();
    await expect.poll(() => loggedOut?.status()).toBe(204); await loggedOut!.finished();
  } finally { page.off('response', observeLogout); }
  await signIn(page, 'e2e-viewer');
  for (const button of await page.getByRole('button', { name: '新建每日复盘', exact: true }).all()) await expect(button).toBeDisabled();
  const auth = await session(page); const forged = await page.request.post('/api/v1/research-documents', { headers: { Origin: new URL(page.url()).origin, 'X-CSRF-Token': auth.csrf_token, 'Idempotency-Key': crypto.randomUUID() }, data: { kind: 'review', source_trade_day: day, revision: reviewBody(f, '禁止写入') } }); expect(forged.status()).toBe(403); expect((await forged.json()).error.code).toBe('forbidden');
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
});

test('real loading, failure retry, empty date, unknown calendar and keyboard closing remain distinct', async ({ page }, info) => {
  await login(page); const f = await fixture(page);
  let release!: () => void; const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/research-documents?*', async route => { await gate; await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'fixture', message: '合成研究列表故障' }, request_id: '00000000-0000-4000-8000-000000000006' }) }); });
  // Private research lists are not persisted; a new document has no prior read.
  await page.reload(); await expect(page.getByText('正在读取研究记录…', { exact: true })).toBeVisible(); release();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成研究列表故障' });
  await expect(notice).toBeVisible(); await screenshots(page, info, 'review-list-error');
  await page.unroute('**/api/v1/research-documents?*'); await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(notice).toHaveCount(0); await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await page.getByLabel('研究日期', { exact: true }).fill('1901-01-01'); await expect(page.getByText('这一天还没有每日复盘记录', { exact: true })).toBeVisible();
  await page.getByLabel('研究日期', { exact: true }).fill(day); const editor = await begin(page, f, '未知日历-' + f.key);
  await editor.getByLabel('日历版本', { exact: true }).selectOption(''); await editor.getByRole('button', { name: '保存草稿', exact: true }).click(); await expect(editor.getByRole('status').filter({ hasText: '已保存第 1 版' })).toContainText('已保存第 1 版'); await expect(editor.getByRole('button', { name: '发布此版本', exact: true })).toBeDisabled();
  for (let index = 0; index < 5; index++) { await page.keyboard.press('Tab'); expect(await editor.evaluate(element => element.contains(document.activeElement))).toBe(true); }
  await page.keyboard.press('Escape'); await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('[data-new-research]')).toBeFocused();
});
