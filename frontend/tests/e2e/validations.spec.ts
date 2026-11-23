import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';
import { login, signIn, post, session, screenshots } from './researchTestHelpers';

async function selectPlan(page: Page) {
  await page.goto('/research/validations?date=2002-01-02');
  const docs = (await (await page.request.get('/api/v1/research-documents?kind=plan&target_trade_day=2002-01-02')).json()).data.items;
  const doc = docs.find((row: { head_revision_title: string }) => row.head_revision_title.startsWith('合成竞价预案新草稿-'));
  expect(doc).toBeTruthy();
  const history = (await (await page.request.get(`/api/v1/research-documents/${doc.id}/revisions`)).json()).data.items;
  const original = history.find((row: { revision_number: number }) => row.revision_number === 1);
  const revision = (await (await page.request.get('/api/v1/revisions/' + original.id)).json()).data;
  const snapshots = (await (await page.request.get('/api/v1/snapshots')).json()).data.items;
  const fresh = snapshots.find((row: { as_of_time: string }) => row.as_of_time.startsWith('2002-01-02T07:25:00'));
  expect(fresh).toBeTruthy();
  const material = (await (await page.request.get('/api/v1/evidence/' + fresh.items[1].evidence_version_id)).json()).data;
  await page.getByLabel('选择预案文档', { exact: true }).selectOption(doc.id);
  await page.getByRole('radio', { name: `选择预案 ${revision.title} 第 1 版`, exact: true }).check();
  await page.getByLabel('原预案情景', { exact: true }).selectOption(revision.scenarios[0].id);
  await expect(page.getByText(/原日历声明收盘/)).toBeVisible();
  return { doc, revision, fresh, material, history };
}
async function current(page: Page) {
  const url = new URL(page.url());
  const rows = (await (await page.request.get('/api/v1/validations?plan_revision_id=' + url.searchParams.get('plan') + '&scenario_id=' + url.searchParams.get('scenario'))).json()).data.items;
  return rows.find((row: { superseded_by_id: string | null }) => !row.superseded_by_id);
}
async function openNewOrCorrection(page: Page) {
  const row = await current(page);
  if (row) {
    await page.getByRole('button', { name: `查看验证 ${row.id}`, exact: true }).click();
    await page.getByRole('button', { name: '追加更正', exact: true }).click();
  } else await page.getByRole('button', { name: '记录次日验证', exact: true }).click();
  return page.getByRole('dialog');
}

test('save supported condition, correction and original chain remain fixed after new plan publication and refresh', async ({ page }, info) => {
  await login(page); const f = await selectPlan(page);
  const observation = await post(page, 'auction-observations', { plan_revision_id: f.revision.id, scenario_id: f.revision.scenarios[0].id, snapshot_id: (await (await page.request.get('/api/v1/snapshots')).json()).data.items.find((s: { as_of_time: string }) => s.as_of_time.startsWith('2002-01-02T01:26:00')).id, observed_at: '2002-01-02T09:25:00+08:00', status: 'unavailable', facts: [], evidence_refs: [], commentary: '合成竞价缺失，验证仍需独立证据' });
  let dialog = await openNewOrCorrection(page);
  await dialog.getByLabel('选择冻结快照', { exact: true }).selectOption(f.fresh.id);
  await dialog.getByLabel('关联观察', { exact: true }).selectOption(observation.id);
  await dialog.getByLabel('人工验证结论', { exact: true }).selectOption('supported');
  await dialog.getByLabel('核对理由或缺失原因', { exact: true }).fill('合成支持原条件 <script>window.validationInjected=true</script>');
  await expect(dialog.getByRole('button', { name: /保存验证(记录|更正)/ })).toBeDisabled();
  await dialog.getByRole('checkbox', { name: `引用材料 ${f.material.source_key} 第 1 版`, exact: true }).check();
  await screenshots(page, info, 'validation-editor');
  const saved = page.waitForResponse(r => r.url().endsWith('/api/v1/validations') && r.request().method() === 'POST');
  await dialog.getByRole('button', { name: /保存验证(记录|更正)/ }).click();
  const response = await saved; expect(response.status()).toBe(201); const first = (await response.json()).data;
  dialog = page.getByRole('dialog', { name: '次日验证详情', exact: true });
  await expect(dialog).toContainText('合成支持原条件 <script>window.validationInjected=true</script>');
  expect(await page.evaluate(() => (window as any).validationInjected)).toBeUndefined();
  await expect(dialog.getByRole('link', { name: '回查关联竞价观察原件' })).toHaveAttribute('href', new RegExp(observation.id));
  const originalLink = dialog.getByRole('link', { name: '回查关联竞价观察原件' });
  await expect(originalLink).toHaveAttribute('href', /\/auction\?legacy=1&/);
  const originalPage = await page.context().newPage();
  await originalPage.goto((await originalLink.getAttribute('href'))!);
  await expect(originalPage.getByRole('heading', { name: '竞价观察', exact: true })).toBeVisible();
  await expect(originalPage.getByRole('dialog', { name: '竞价观察详情', exact: true })).toContainText('合成竞价缺失，验证仍需独立证据');
  await originalPage.close();
  await dialog.getByRole('button', { name: '追加更正', exact: true }).click();
  dialog = page.getByRole('dialog', { name: '追加验证更正', exact: true });
  await expect(dialog).toContainText('合成支持原条件');
  await dialog.getByLabel('选择冻结快照', { exact: true }).selectOption(f.fresh.id);
  await dialog.getByLabel('核对理由或缺失原因', { exact: true }).fill('合成更正：资料不足，保留原支持记录');
  await screenshots(page, info, 'validation-correction');
  await dialog.getByRole('button', { name: '保存验证更正', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '次日验证详情' })).toContainText('合成更正：资料不足');
  await page.getByRole('button', { name: '查看前一份验证原文', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('合成支持原条件'); await expect(page.getByRole('dialog')).toContainText('已更正，原文保留');
  await expect(page.getByRole('button', { name: '追加更正', exact: true })).toHaveCount(0);
  await screenshots(page, info, 'validation-original');
  const p2 = f.history.find((row: { revision_number: number }) => row.revision_number === 2);
  if (p2.publication_status === 'draft') await post(page, `revisions/${p2.id}/publish`, {}, 2);
  await page.reload(); await expect(page.getByRole('dialog')).toContainText('合成支持原条件');
  await expect(page.getByRole('dialog').getByRole('link', { name: /回查原预案.*第 1 版/ })).toHaveAttribute('href', new RegExp('revision=' + f.revision.id));
  const read = (await (await page.request.get('/api/v1/validations/' + first.id)).json()).data;
  expect(read.content_hash).toBe(first.content_hash); expect(read.reason).toBe(first.reason);
  await page.getByRole('button', { name: '查看后一份更正', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('合成更正：资料不足');
  await page.getByRole('dialog').getByRole('button', { name: '收起', exact: true }).click();
  await screenshots(page, info, 'validation-layout');
});

test('stale correction is rejected, actual expiry retains reason and idempotent retry; viewer cannot write', async ({ page }, info) => {
  await login(page); const f = await selectPlan(page); const previous = await current(page);
  const dialog = await openNewOrCorrection(page);
  await dialog.getByLabel('选择冻结快照', { exact: true }).selectOption(f.revision.snapshot_id);
  await expect(dialog.getByText(/需要目标日收盘后或之后的新快照/)).toBeVisible();
  await dialog.getByLabel('核对理由或缺失原因', { exact: true }).fill('重试保留的合成缺失原因');
  await expect(dialog.getByRole('button', { name: /保存验证(记录|更正)/ })).toBeDisabled();
  await dialog.getByLabel('选择冻结快照', { exact: true }).selectOption(f.fresh.id);
  const keys: string[] = [];
  await page.route('**/api/v1/validations', async route => {
    if (route.request().method() !== 'POST') return route.continue(); keys.push(route.request().headers()['idempotency-key']);
    if (keys.length === 1) return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'fixture', message: '合成验证保存故障' }, request_id: '00000000-0000-4000-8000-000000000010' }) });
    return route.continue();
  });
  await dialog.getByRole('button', { name: /保存验证(记录|更正)/ }).click(); await expect(dialog.getByRole('alert')).toContainText('合成验证保存故障');
  await expect(dialog.getByRole('button', { name: /保存验证(记录|更正)/ })).toBeEnabled();
  // The failed write must be retried against real authentication; unrelated
  // background GETs must not unmount its editor before the POST is sent.
  const backgroundDuringExpiry = (route: Route) => route.request().method() === 'GET'
    ? route.fulfill({ status: 503, json: { error: { code: 'synthetic_background_wait', message: '合成后台读取暂停' } } }) : route.fallback();
  await page.route('**/api/v1/**', backgroundDuringExpiry);
  const cookies = (await page.context().cookies()).filter(cookie => cookie.httpOnly);
  await page.context().addCookies(cookies.map(cookie => ({ ...cookie, expires: Math.floor(Date.now() / 1000) - 1 })));
  const expiredWrite = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/validations' && response.request().method() === 'POST' && response.status() === 401);
  await dialog.getByRole('button', { name: /保存验证(记录|更正)/ }).click(); await expiredWrite; await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  await page.unroute('**/api/v1/**', backgroundDuringExpiry); await signIn(page);
  // URL restores the exact original reference; private input stays in owner memory.
  await expect(page.getByLabel('核对理由或缺失原因', { exact: true })).toHaveValue('重试保留的合成缺失原因');
  await page.getByRole('button', { name: /保存验证(记录|更正)/ }).click(); await expect(page.getByRole('dialog', { name: '次日验证详情' })).toContainText('重试保留的合成缺失原因');
  expect(new Set(keys).size).toBe(1);
  const latest = await current(page);
  if (previous) {
    const auth = await session(page);
    const rejected = await page.request.post('/api/v1/validations', { headers: { Origin: new URL(page.url()).origin, 'X-CSRF-Token': auth.csrf_token, 'Idempotency-Key': crypto.randomUUID() }, data: { plan_revision_id: f.revision.id, scenario_id: f.revision.scenarios[0].id, observation_id: null, snapshot_id: f.fresh.id, outcome: 'inconclusive', reason: 'stale correction', evidence_refs: [], supersedes_id: previous.id } });
    expect(rejected.status()).toBe(409); expect((await rejected.json()).error.code).toBe('validation_conflict');
  }
  await screenshots(page, info, 'validation-inconclusive');
  await page.getByRole('dialog').getByRole('button', { name: '收起', exact: true }).click(); await page.getByRole('button', { name: '退出', exact: true }).click(); await signIn(page, 'e2e-viewer');
  expect((await page.request.get('/api/v1/validations/' + latest.id)).status()).toBe(404);
  const auth = await session(page);
  const denied = await page.request.post('/api/v1/validations', { headers: { Origin: new URL(page.url()).origin, 'X-CSRF-Token': auth.csrf_token, 'Idempotency-Key': crypto.randomUUID() }, data: { plan_revision_id: f.revision.id, scenario_id: f.revision.scenarios[0].id, observation_id: null, snapshot_id: f.fresh.id, outcome: 'inconclusive', reason: 'forbidden', evidence_refs: [], supersedes_id: null } });
  expect(denied.status()).toBe(403); expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
});

test('refresh retains saved validation rows while pending and errors recover through notifications; authoritative empty plan is separate', async ({ page }, info) => {
  await login(page); const f = await selectPlan(page);
  const table = page.getByRole('region', { name: '验证记录表格', exact: true });
  await expect(table).toBeVisible(); const savedRows = await table.innerText();
  let release: () => void = () => {}; const held = new Promise<void>(resolve => { release = resolve; });
  let requested: () => void = () => {}; const started = new Promise<void>(resolve => { requested = resolve; });
  await page.route('**/api/v1/validations?*', async route => { requested(); await held; await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'fixture', message: '合成验证列表故障' }, request_id: '00000000-0000-4000-8000-000000000011' }) }); });
  await page.getByRole('button', { name: '刷新验证记录', exact: true }).click(); await started;
  await expect(table).toBeVisible(); expect(await table.innerText()).toBe(savedRows);
  await expect(page.getByText('尚无这个情景的验证', { exact: true })).toHaveCount(0); release();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成验证列表故障' });
  await expect(notice).toBeVisible();
  await screenshots(page, info, 'validation-list-error'); await page.unroute('**/api/v1/validations?*');
  await notice.getByRole('button', { name: '重新读取', exact: true }).click(); await expect(notice).toHaveCount(0); await expect(table).toBeVisible();
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await page.goto(`/research/validations?date=2002-01-02&plan=${f.history.find((r: { revision_number: number }) => r.revision_number === 2).id}`);
  await expect(page.getByText('尚无这个情景的验证', { exact: true })).toBeVisible();
  await screenshots(page, info, 'validation-empty');
});

test('another window correction returns conflict without overwriting unsaved reason', async ({ page }, info) => {
  await login(page); const f = await selectPlan(page); const old = await current(page); expect(old).toBeTruthy();
  const dialog = await openNewOrCorrection(page);
  await dialog.getByLabel('选择冻结快照', { exact: true }).selectOption(f.fresh.id);
  await dialog.getByLabel('核对理由或缺失原因', { exact: true }).fill('这个窗口尚未保存的更正理由');
  const changed = await post(page, 'validations', { plan_revision_id: f.revision.id, scenario_id: f.revision.scenarios[0].id, snapshot_id: f.fresh.id, observation_id: null, outcome: 'inconclusive', reason: '另一窗口已保存的合成更正', evidence_refs: [], supersedes_id: old.id });
  await dialog.getByRole('button', { name: '保存验证更正', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('更正已更新');
  await expect(dialog.getByLabel('核对理由或缺失原因', { exact: true })).toHaveValue('这个窗口尚未保存的更正理由');
  await screenshots(page, info, 'validation-conflict');
  await dialog.getByRole('button', { name: '核对原记录与更正链（保留输入）', exact: true }).click();
  await page.getByRole('button', { name: '查看后一份更正', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText(changed.reason);
  const rows = (await (await page.request.get('/api/v1/validations?plan_revision_id=' + f.revision.id)).json()).data.items;
  expect(rows.filter((row: { supersedes_id: string }) => row.supersedes_id === old.id)).toHaveLength(1);
});
