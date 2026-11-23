import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { day, login, fixture, screenshots, post, reviewBody, signIn, session } from './researchTestHelpers';

async function chain(page: Page) {
  const f = await fixture(page);
  const review = await post(page, 'research-documents', { kind: 'review', source_trade_day: day, revision: reviewBody(f, '预案上游复盘-' + f.key) });
  await post(page, `revisions/${review.head_revision_id}/publish`, {}, 1);
  const body = { title: '预案上游方向-' + f.key, snapshot_id: f.snapshot.id, calendar_version_id: f.calendar.id, upstream_revision_ids: [review.head_revision_id], content: { type: 'direction', items: [{ label: '隔离测试方向', judgment: '仅验证固定引用，不是市场判断。', core_instrument_ids: [], evidence_refs: [f.material.id] }], limitations: ['合成测试记录'] } };
  const direction = await post(page, 'research-documents', { kind: 'direction', source_trade_day: day, revision: body });
  await post(page, `revisions/${direction.head_revision_id}/publish`, {}, 1);
  return { ...f, review, direction, directionBody: body };
}
type Chain = Awaited<ReturnType<typeof chain>>;
async function beginPlan(page: Page, f: Chain, title: string) {
  await page.goto('/research/plans?date=' + day);
  await page.getByRole('button', { name: '新建条件预案', exact: true }).last().click();
  const editor = page.getByRole('dialog', { name: '编辑条件预案', exact: true });
  await editor.getByLabel('研究标题', { exact: true }).fill(title);
  await editor.getByLabel('选择冻结快照', { exact: true }).selectOption(f.snapshot.id);
  await editor.getByLabel('日历版本', { exact: true }).selectOption(f.calendar.id);
  await editor.getByLabel('选择方向文档', { exact: true }).selectOption(f.direction.id);
  await editor.getByRole('checkbox', { name: `引用方向 ${f.directionBody.title} 第 1 版`, exact: true }).check();
  await expect(editor.getByRole('region', { name: '预案目标日期', exact: true })).toContainText('2002-01-02');
  await editor.getByRole('button', { name: '采用此日为目标日', exact: true }).click();
  await editor.getByLabel('情景 1 名称', { exact: true }).fill('合成条件一');
  await editor.getByLabel('情景 1 触发条件', { exact: true }).fill('人工观察满足合成条件；不执行任何代码。');
  await editor.getByLabel('情景 1 失效条件', { exact: true }).fill('证据缺失时停止沿用此判断。');
  await editor.getByRole('checkbox', { name: `引用材料 ${f.key} 第 1 版`, exact: true }).check();
  await editor.getByLabel('材料与判断限制（每行一项）', { exact: true }).fill('隔离合成材料，不是真实行情或交易建议');
  return editor;
}

test('conditional plan chooses next declared day, preserves fixed D1 and each old scenario after revision', async ({ page }, info) => {
  await login(page); const f = await chain(page); const title = '合成条件预案-' + f.key;
  // A newer unpublished head must not hide the exact published D1.
  const d2 = await post(page, `research-documents/${f.direction.id}/revisions`, { ...f.directionBody, title: f.directionBody.title + '-D2' }, 1);
  const editor = await beginPlan(page, f, title);
  await expect(editor.getByLabel('目标日期', { exact: true })).toHaveValue('2002-01-02');
  await editor.getByRole('button', { name: '添加条件情景', exact: true }).click();
  await editor.getByLabel('情景 2 名称', { exact: true }).fill('合成条件二');
  await editor.getByLabel('情景 2 触发条件', { exact: true }).fill('需要额外核对的独立情景。');
  await editor.getByLabel('情景 2 失效条件', { exact: true }).fill('与材料冲突则人工复核。');
  await screenshots(page, info, 'plan-editor');
  await screenshots(page, info, 'plan-target');
  await screenshots(page, info, 'plan-scenarios');
  const created = page.waitForResponse(response => response.url().endsWith('/api/v1/research-documents') && response.request().method() === 'POST');
  await editor.getByRole('button', { name: '保存草稿', exact: true }).click(); const document = (await (await created).json()).data;
  await expect(editor.getByRole('status').filter({ hasText: '已保存第 1 版' })).toBeVisible(); await expect(editor.getByLabel('目标日期', { exact: true })).toHaveAttribute('readonly', '');
  const p1 = (await (await page.request.get('/api/v1/revisions/' + document.head_revision_id)).json()).data;
  expect(p1.scenarios).toHaveLength(2); expect(p1.upstream_revision_ids).toEqual([f.direction.head_revision_id]);
  await editor.getByRole('button', { name: '发布此版本', exact: true }).click(); await expect(editor.getByRole('status').filter({ hasText: '已发布第 1 版' })).toContainText('已发布第 1 版');
  await editor.getByRole('button', { name: '收起', exact: true }).click(); await page.reload();
  await screenshots(page, info, 'plan-layout');
  await page.getByRole('button', { name: `查看研究 ${title}`, exact: true }).click(); const details = page.getByRole('dialog', { name: '研究版本详情' });
  await expect(details).toContainText('2002-01-02 · 文档固定目标'); await expect(details.locator('.saved-research-content')).toContainText('证据缺失时停止沿用此判断。');
  const auctionLink = details.getByRole('link', { name: '用这个固定预案查看或记录竞价观察', exact: true });
  await expect(auctionLink).toHaveAttribute('href', new RegExp('/auction\\?legacy=1&date=2002-01-02&plan=' + p1.id));
  const auction = await page.context().newPage();
  await auction.goto((await auctionLink.getAttribute('href'))!);
  await expect(auction.getByRole('heading', { name: '竞价观察', exact: true })).toBeVisible();
  // A deep link loads the fixed revision directly; the optional document
  // chooser does not populate its revision radios until a document is chosen.
  await expect(auction.getByRole('heading', { name: `${title} · 第 1 版`, exact: true })).toBeVisible();
  await expect(auction.getByRole('link', { name: '查看这个精确预案版本', exact: true }))
    .toHaveAttribute('href', new RegExp('revision=' + p1.id));
  await auction.getByLabel('原预案情景', { exact: true }).selectOption(p1.scenarios[0].id);
  await expect(auction.getByRole('button', { name: '记录竞价观察', exact: true })).toBeEnabled();
  await auction.close();
  await post(page, `revisions/${d2.id}/publish`, {}, 2);
  await expect(details.getByRole('link', { name: new RegExp(f.directionBody.title + '.*第 1 版') })).toHaveAttribute('href', new RegExp('revision=' + f.direction.head_revision_id));
  await screenshots(page, info, 'plan-fixed-version');
  await details.getByRole('button', { name: '以此版内容新增修订', exact: true }).click();
  await page.getByLabel('情景 1 触发条件', { exact: true }).fill('新的合成触发条件，旧版继续保持。');
  await page.getByRole('button', { name: '保存草稿', exact: true }).click(); await expect(page.getByRole('dialog').getByRole('status').filter({ hasText: '已保存第 2 版' })).toContainText('已保存第 2 版');
  const latest = (await (await page.request.get('/api/v1/research-documents/' + document.id)).json()).data;
  const p2 = (await (await page.request.get('/api/v1/revisions/' + latest.head_revision_id)).json()).data;
  expect(p2.scenarios).toHaveLength(2); expect(p2.scenarios.every((item: { id: string }) => !p1.scenarios.some((old: { id: string }) => old.id === item.id))).toBe(true);
  const old = (await (await page.request.get('/api/v1/revisions/' + p1.id)).json()).data; expect(old.scenarios).toEqual(p1.scenarios); expect(old.content_hash).toBe(p1.content_hash); expect(old.upstream_revision_ids).toEqual([f.direction.head_revision_id]);
  await page.getByRole('button', { name: '收起', exact: true }).click();
  await page.goto(`/research/plans?date=${day}&document=${document.id}&revision=${p1.id}`); await page.reload();
  await expect(page.locator('.saved-research-content')).toContainText('人工观察满足合成条件'); await expect(page.locator('.saved-research-content')).not.toContainText('新的合成触发条件');
  await screenshots(page, info, 'plan-layout-detail');
});

test('unknown or insufficient calendar never invents a next day, and wrong target is also rejected by server', async ({ page }, info) => {
  await login(page); const f = await chain(page); const editor = await beginPlan(page, f, '目标边界-' + f.key);
  await editor.getByLabel('目标日期', { exact: true }).fill('2002-01-01'); await expect(editor.getByText(/目标日期与此日历的下一开市日不一致/)).toBeVisible(); await expect(editor.getByRole('button', { name: '保存草稿', exact: true })).toBeDisabled();
  const auth = await session(page); const revision = { title: '错误目标日', snapshot_id: f.snapshot.id, calendar_version_id: f.calendar.id, upstream_revision_ids: [f.direction.head_revision_id], content: { type: 'plan', scenarios: [{ label: '合成', trigger_text: '人工触发', invalidation_text: '人工失效', evidence_refs: [], instrument_ids: [] }], limitations: [] } };
  const wrong = await page.request.post('/api/v1/research-documents', { headers: { Origin: new URL(page.url()).origin, 'X-CSRF-Token': auth.csrf_token, 'Idempotency-Key': crypto.randomUUID() }, data: { kind: 'plan', source_trade_day: day, target_trade_day: '2002-01-01', revision } }); expect(wrong.status()).toBe(422); expect((await wrong.json()).error.code).toBe('invalid_target_day');
  const short = await post(page, 'calendar-versions', { exchange: 'SSE', version: 'limited-' + f.key, source_ref: '合成覆盖不足的日历', rights_basis: '自制测试', coverage_start: day, coverage_end: day, sessions: [{ trade_day: day, is_open: true, segments: [{ start: '09:30', end: '11:30', kind: 'continuous' }] }] });
  await editor.getByRole('button', { name: '刷新日历', exact: true }).click(); await editor.getByLabel('日历版本', { exact: true }).selectOption(short.id); await expect(editor.getByText(/覆盖内没有可确认的后续开市日/)).toBeVisible(); await expect(editor.getByRole('button', { name: '保存草稿', exact: true })).toBeDisabled();
  await screenshots(page, info, 'plan-calendar-unknown');
  await editor.getByLabel('日历版本', { exact: true }).selectOption(''); await editor.getByLabel('目标日期', { exact: true }).fill('2002-01-03');
  await editor.getByRole('button', { name: '保存草稿', exact: true }).click(); await expect(editor.getByRole('status').filter({ hasText: '已保存第 1 版' })).toBeVisible(); await expect(editor.getByRole('button', { name: '发布此版本', exact: true })).toBeDisabled();
  await editor.getByLabel('日历版本', { exact: true }).selectOption(f.calendar.id); await expect(editor.getByText(/目标日已固定/)).toBeVisible(); await expect(editor.getByRole('button', { name: '保存草稿', exact: true })).toBeDisabled();
});

test('plan keeps input across save error and real expiry and viewer stays read-only', async ({ page }) => {
  await login(page); const f = await chain(page); const title = '预案重试-' + f.key; const editor = await beginPlan(page, f, title);
  const keys: string[] = [];
  await page.route('**/api/v1/research-documents', async route => {
    if (route.request().method() !== 'POST') return route.continue(); keys.push(route.request().headers()['idempotency-key']);
    if (keys.length === 1) return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'fixture', message: '合成预案保存故障' }, request_id: '00000000-0000-4000-8000-000000000007' }) });
    return route.continue();
  });
  await editor.getByRole('button', { name: '保存草稿', exact: true }).click(); await expect(editor.getByRole('alert')).toContainText('合成预案保存故障');
  const cookies = (await page.context().cookies()).filter(cookie => cookie.httpOnly); await page.context().addCookies(cookies.map(cookie => ({ ...cookie, expires: Math.floor(Date.now() / 1000) - 1 })));
  // A shared background read can notice the expired real cookie before a
  // second save is clickable. Explicitly validate the session, then retry the
  // retained operation only after reauthentication.
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible(); await signIn(page);
  await expect(page.getByLabel('研究标题', { exact: true })).toHaveValue(title); await expect(page.getByLabel('目标日期', { exact: true })).toHaveValue('2002-01-02');
  await page.getByRole('button', { name: '保存草稿', exact: true }).click(); await expect(page.getByRole('dialog').getByRole('status').filter({ hasText: '已保存第 1 版' })).toContainText('已保存第 1 版'); expect(new Set(keys).size).toBe(1);
  await page.getByRole('button', { name: '收起', exact: true }).click(); await page.getByRole('button', { name: '退出', exact: true }).click(); await signIn(page, 'e2e-viewer');
  for (const button of await page.getByRole('button', { name: '新建条件预案', exact: true }).all()) await expect(button).toBeDisabled();
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
});
