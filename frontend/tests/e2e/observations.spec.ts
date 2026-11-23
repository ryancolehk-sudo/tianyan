import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';
import { login, signIn, session, post, screenshots } from './researchTestHelpers';

const target = '2002-01-02';
async function selectPlan(page: Page) {
  await page.goto('/auction?legacy=1&date=' + target);
  const docs = (await (await page.request.get('/api/v1/research-documents?kind=plan&target_trade_day=' + target)).json()).data.items;
  const doc = docs.find((row: { head_revision_title: string }) => row.head_revision_title.startsWith('合成竞价预案新草稿-'));
  expect(doc, 'guarded synthetic auction fixture must exist').toBeTruthy();
  const history = (await (await page.request.get(`/api/v1/research-documents/${doc.id}/revisions`)).json()).data.items;
  const original = history.find((row: { revision_number: number }) => row.revision_number === 1);
  const revision = (await (await page.request.get('/api/v1/revisions/' + original.id)).json()).data;
  const snapshotRows = (await (await page.request.get('/api/v1/snapshots')).json()).data.items;
  const material = (await (await page.request.get('/api/v1/evidence?source_key=' + encodeURIComponent(revision.title.replace('合成竞价上游-plan-', '') + '-当时材料'))).json()).data.items[0];
  expect(material).toBeTruthy();
  const fresh = snapshotRows.find((row: { as_of_time: string; items: unknown[] }) => row.as_of_time.startsWith('2002-01-02T01:26:00') && row.items.length === 1);
  const late = snapshotRows.find((row: { as_of_time: string; items: unknown[] }) => row.as_of_time.startsWith('2002-01-02T01:31:00') && row.items.length === 2);
  await page.getByLabel('选择预案文档', { exact: true }).selectOption(doc.id);
  await page.getByRole('radio', { name: `选择预案 ${revision.title} 第 1 版`, exact: true }).check();
  await page.getByLabel('原预案情景', { exact: true }).selectOption(revision.scenarios[0].id);
  await expect(page.getByRole('button', { name: '记录竞价观察', exact: true })).toBeEnabled();
  return { doc, revision, fresh, late, material, history };
}

async function editor(page: Page, f: Awaited<ReturnType<typeof selectPlan>>) {
  await page.getByRole('button', { name: '记录竞价观察', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '记录竞价观察', exact: true });
  await dialog.getByLabel('选择冻结快照', { exact: true }).selectOption(f.fresh.id);
  return dialog;
}

test('manual facts save, refresh and keep original P1 scenario after P2 publication', async ({ page }, info) => {
  await login(page); const f = await selectPlan(page); const dialog = await editor(page, f);
  await dialog.getByLabel('观察资料状态', { exact: true }).selectOption('observed');
  await dialog.getByLabel('事实 1 字段', { exact: true }).fill('合成观察字段');
  await dialog.getByLabel('事实 1 值', { exact: true }).fill('仅合成核查 <script>window.auctionInjected=true</script>');
  await dialog.getByLabel('事实 1 单位', { exact: true }).fill('文字');
  await dialog.getByLabel('事实 1 材料', { exact: true }).selectOption(f.material.id);
  await dialog.getByLabel('人工解释或缺失原因', { exact: true }).fill('人工解释与事实分开；不是行情和交易意见。');
  await screenshots(page, info, 'observation-editor');
  const written = page.waitForResponse(response => response.url().endsWith('/api/v1/auction-observations') && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: '保存观察记录', exact: true }).click();
  const response = await written; expect(response.status()).toBe(201); const row = (await response.json()).data;
  const details = page.getByRole('dialog', { name: '竞价观察详情', exact: true });
  await expect(details).toContainText('晚补记录'); await expect(details).toContainText('合成观察字段');
  await expect(details).toContainText('仅合成核查 <script>window.auctionInjected=true</script>');
  expect(await page.evaluate(() => (window as any).auctionInjected)).toBeUndefined();
  const p2 = f.history.find((r: { revision_number: number }) => r.revision_number === 2);
  if (p2.publication_status === 'draft') await post(page, `revisions/${p2.id}/publish`, {}, 2);
  await page.reload(); await expect(page.getByRole('dialog', { name: '竞价观察详情', exact: true })).toContainText('合成观察字段');
  await expect(page.getByRole('dialog').getByRole('link', { name: /回查原预案.*第 1 版/ })).toHaveAttribute('href', new RegExp('revision=' + f.revision.id));
  expect((await (await page.request.get('/api/v1/auction-observations/' + row.id)).json()).data).toEqual(row);
  await screenshots(page, info, 'observation-detail');
  await page.getByRole('dialog').getByRole('button', { name: '收起', exact: true }).click();
  await screenshots(page, info, 'observation-layout');
});

test('unavailable saves a reason, never facts or a supported result', async ({ page }, info) => {
  await login(page); const f = await selectPlan(page); const dialog = await editor(page, f);
  await dialog.getByLabel('人工解释或缺失原因', { exact: true }).fill('没有有权使用的09:25资料，不可判断原条件。');
  const written = page.waitForResponse(response => response.url().endsWith('/api/v1/auction-observations') && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: '保存观察记录', exact: true }).click();
  const value = await (await written).json(); expect(value.meta.quality.status).toBe('unavailable'); expect(value.data.facts).toEqual([]); expect(value.data.evidence_refs).toEqual([]);
  await expect(page.getByRole('dialog')).toContainText('没有可核查事实');
  await screenshots(page, info, 'observation-unavailable');
});

test('old snapshot and late material are blocked, save error and actual expiry preserve same-key retry', async ({ page }, info) => {
  await login(page); const f = await selectPlan(page); const dialog = await editor(page, f);
  await dialog.getByLabel('选择冻结快照', { exact: true }).selectOption(f.revision.snapshot_id);
  await expect(dialog.getByText(/需要目标日的新快照/)).toBeVisible(); await expect(dialog.getByRole('button', { name: '保存观察记录', exact: true })).toBeDisabled();
  await dialog.getByLabel('选择冻结快照', { exact: true }).selectOption(f.late.id);
  await dialog.getByLabel('观察资料状态', { exact: true }).selectOption('observed');
  await dialog.getByLabel('事实 1 字段', { exact: true }).fill('迟到字段'); await dialog.getByLabel('事实 1 值', { exact: true }).fill('不能回填'); await dialog.getByLabel('事实 1 单位', { exact: true }).fill('文字');
  const lateId = f.late.items.find((row: { evidence_version_id: string }) => row.evidence_version_id !== f.material.id).evidence_version_id;
  await dialog.getByLabel('事实 1 材料', { exact: true }).selectOption(lateId);
  await expect(dialog.getByText(/所选材料在检查点之后才可得/)).toBeVisible(); await expect(dialog.getByRole('button', { name: '保存观察记录', exact: true })).toBeDisabled();
  await screenshots(page, info, 'observation-late-material');
  await dialog.getByLabel('观察资料状态', { exact: true }).selectOption('unavailable');
  await dialog.getByLabel('人工解释或缺失原因', { exact: true }).fill('保留这条缺失原因后重试');
  const keys: string[] = [];
  await page.route('**/api/v1/auction-observations', async route => {
    if (route.request().method() !== 'POST') return route.continue(); keys.push(route.request().headers()['idempotency-key']);
    if (keys.length === 1) return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'fixture', message: '合成观察保存故障' }, request_id: '00000000-0000-4000-8000-000000000008' }) });
    return route.continue();
  });
  await dialog.getByRole('button', { name: '保存观察记录', exact: true }).click(); await expect(dialog.getByRole('alert')).toContainText('合成观察保存故障');
  await expect(dialog.getByRole('button', { name: '保存观察记录', exact: true })).toBeEnabled();
  // Keep background reads from observing expiry before the actual POST retry.
  // The write still reaches the server with the genuinely expired cookie.
  const backgroundDuringExpiry = (route: Route) => route.request().method() === 'GET'
    ? route.fulfill({ status: 503, json: { error: { code: 'synthetic_background_wait', message: '合成后台读取暂停' } } }) : route.fallback();
  await page.route('**/api/v1/**', backgroundDuringExpiry);
  const cookies = (await page.context().cookies()).filter(cookie => cookie.httpOnly);
  await page.context().addCookies(cookies.map(cookie => ({ ...cookie, expires: Math.floor(Date.now() / 1000) - 1 })));
  const expiredWrite = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/auction-observations' && response.request().method() === 'POST' && response.status() === 401);
  await dialog.getByRole('button', { name: '保存观察记录', exact: true }).click(); await expiredWrite; await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  await page.unroute('**/api/v1/**', backgroundDuringExpiry); await signIn(page);
  await expect(page.getByLabel('人工解释或缺失原因', { exact: true })).toHaveValue('保留这条缺失原因后重试');
  await page.getByRole('button', { name: '保存观察记录', exact: true }).click(); await expect(page.getByRole('dialog', { name: '竞价观察详情' })).toContainText('保留这条缺失原因后重试'); expect(new Set(keys).size).toBe(1);
  await page.getByRole('dialog').getByRole('button', { name: '收起', exact: true }).click(); await page.getByRole('button', { name: '退出', exact: true }).click(); await signIn(page, 'e2e-viewer');
  await expect(await openNotifications(page)).toContainText('研究修订不存在或不可访问');
  await expect(page.getByText('保留这条缺失原因后重试', { exact: true })).toHaveCount(0);
  expect((await page.request.get('/api/v1/revisions/' + f.revision.id)).status()).toBe(404);
  const auth = await session(page);
  const forbidden = await page.request.post('/api/v1/auction-observations', { headers: { Origin: new URL(page.url()).origin, 'X-CSRF-Token': auth.csrf_token, 'Idempotency-Key': crypto.randomUUID() }, data: { plan_revision_id: f.revision.id, scenario_id: f.revision.scenarios[0].id, snapshot_id: f.fresh.id, observed_at: '2002-01-02T09:25:00+08:00', status: 'unavailable', facts: [], evidence_refs: [], commentary: 'forbidden' } });
  expect(forbidden.status()).toBe(403); expect((await forbidden.json()).error.code).toBe('forbidden');
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
});

test('observation refresh retains saved rows while pending, then distinguishes a retryable error and authoritative empty data', async ({ page }, info) => {
  await login(page); const f = await selectPlan(page);
  const table = page.getByRole('region', { name: '竞价观察记录表格', exact: true });
  await expect(table).toBeVisible(); const savedRows = await table.innerText();
  let release: () => void = () => {}; const held = new Promise<void>(resolve => { release = resolve; });
  let requested: () => void = () => {}; const started = new Promise<void>(resolve => { requested = resolve; });
  await page.route('**/api/v1/auction-observations?*', async route => { requested(); await held; await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'fixture', message: '合成观察读取故障' }, request_id: '00000000-0000-4000-8000-000000000009' }) }); });
  await page.getByRole('button', { name: '刷新观察记录', exact: true }).click(); await started;
  await expect(table).toBeVisible(); expect(await table.innerText()).toBe(savedRows);
  await expect(page.getByText('当前版本尚无观察记录', { exact: true })).toHaveCount(0); release();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成观察读取故障' });
  await expect(notice).toBeVisible();
  await screenshots(page, info, 'observation-list-error'); await page.unroute('**/api/v1/auction-observations?*');
  await notice.getByRole('button', { name: '重新读取', exact: true }).click(); await expect(notice).toHaveCount(0); await expect(table).toBeVisible();
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await page.goto(`/auction?legacy=1&date=${target}&plan=${f.history.find((row: { revision_number: number }) => row.revision_number === 2).id}`);
  await expect(page.getByText('当前版本尚无观察记录', { exact: true })).toBeVisible();
});
