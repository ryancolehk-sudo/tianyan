import { expect, test } from '@playwright/test';
import type { Locator, Page, TestInfo } from '@playwright/test';
import { readFile, rename, writeFile } from 'node:fs/promises';
import { signIn, screenshots } from '../e2e/researchTestHelpers';

const directory = process.env.JL_ACCEPTANCE_DIR!;
const clockPath = '/workspace/test-clock/clock.json';
const phase = process.env.JL_ACCEPTANCE_PHASE;
const project = process.env.JL_TEST_PROJECT!;
const originalText = '原创合成材料第一版：只验证系统记录与精确引用，不是真实行情。';
const reviewText = '合成复盘 R1：保留当时判断，后续版本不能改写此原文。';

async function readingStorage(page: Page, owner: string, secrets: string[]) {
  const stored = await page.evaluate(() => ({ local: localStorage.length, session: Object.entries(sessionStorage) }));
  expect(stored.local).toBe(0);
  expect(stored.session.length).toBeGreaterThan(0);
  const prefix = 'ty-workspace-v1:';
  const scroll = (value: any) => value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.values(value).every((position: any) => position !== null && typeof position === 'object'
      && Object.keys(position).sort().join(',') === 'left,top'
      && Number.isFinite(position.left) && position.left >= 0 && Number.isFinite(position.top) && position.top >= 0);
  const date = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value + 'T00:00:00Z'));
  const cutoff = (value: string) => /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
    && Number.isFinite(Date.parse(value));
  const datedContext = (value: string) => {
    const separator = value.indexOf(':');
    expect(separator, 'reading context separates its date and cutoff').toBeGreaterThanOrEqual(0);
    const day = value.slice(0, separator), asOf = value.slice(separator + 1);
    expect(day === '' || date(day), 'reading date is empty or an ISO date').toBe(true);
    expect(asOf === '' || cutoff(asOf), 'reading cutoff is empty or an ISO instant').toBe(true);
  };
  const fields = (value: any, keys: string[]) => {
    expect(value).not.toBeNull(); expect(typeof value).toBe('object'); expect(Array.isArray(value)).toBe(false);
    expect(Object.keys(value).sort()).toEqual([...keys].sort());
  };
  const stockSorts = ['name', 'pct_change', 'speed_5m', 'amount_cny', 'ths_themes'];
  for (const [key, raw] of stored.session) {
    expect(key.startsWith(prefix), 'only workspace reading state may use session storage').toBe(true);
    const ranking = key.startsWith(prefix + 'ranking:');
    const stockRank = key.startsWith(prefix + 'stock-rank:');
    const identity = key.slice((prefix + (ranking ? 'ranking:' : stockRank ? 'stock-rank:' : '')).length);
    expect(identity.startsWith(owner + ':'), 'reading state must belong to this account').toBe(true);
    const value = JSON.parse(raw);
    if (ranking) {
      expect(Object.keys(value).sort()).toEqual(['codes', 'identity', 'reset']);
      expect(value.identity).toBe(identity);
      expect(Array.isArray(value.codes) && value.codes.every((code: unknown) => typeof code === 'string' && /^[A-Z0-9.]{1,32}$/.test(code))).toBe(true);
      expect(Number.isInteger(value.reset) && value.reset >= 0).toBe(true);
    } else if (stockRank) {
      // StockRanking.tsx Settings, with readingKey supplied by Overview.tsx.
      expect(identity.startsWith(owner + ':overview-rank:')).toBe(true); expect(identity.endsWith(':gain')).toBe(true);
      datedContext(identity.slice((owner + ':overview-rank:').length, -':gain'.length));
      fields(value, ['sort', 'ascending', 'query', 'scope', 'page']);
      expect(stockSorts).toContain(value.sort); expect(typeof value.ascending).toBe('boolean');
      expect(['all', 'SH', 'SZ', 'BJ', 'watch']).toContain(value.scope);
      expect(Number.isSafeInteger(value.page) && value.page >= 0).toBe(true);
      expect(value.query, 'this research flow never enters market search text').toBe('');
    } else if (identity.startsWith(owner + ':kpl-strength:')) {
      // KplStrengthWorkspace.tsx: URLSearchParams plus one explicit state field.
      const match = /^(.*):(search|selection|overview-top-five-v1)$/.exec(identity.slice((owner + ':kpl-strength:').length));
      expect(match, 'known KPL reading preference').not.toBeNull();
      const query = new URLSearchParams(match![1]), names = [...query.keys()];
      expect(query.toString()).toBe(match![1]); expect(new Set(names).size).toBe(names.length);
      expect(names.every(name => name === 'trade_day' || name === 'as_of_time')).toBe(true);
      for (const [name, item] of query) expect(name === 'trade_day' ? date(item) : cutoff(item)).toBe(true);
      if (match![2] === 'search') expect(value, 'no KPL search is entered in this flow').toBe('');
      else if (match![2] === 'overview-top-five-v1') expect(value, 'overview uses automatic top five, without a saved manual selection').toBeNull();
      else expect(value === null || Array.isArray(value) && value.length <= 10 && new Set(value).size === value.length
        && value.every((code: unknown) => typeof code === 'string' && /^\d{6}$/.test(code))).toBe(true);
    } else if (identity.startsWith(owner + ':overview-funds:')) {
      // OverviewFunds.tsx: category, or category-specific search/sort only.
      const match = /^(.*):(category|search:(?:industry|concept)|sort:(?:industry|concept))$/.exec(identity.slice((owner + ':overview-funds:').length));
      expect(match, 'known overview funds reading preference').not.toBeNull(); datedContext(match![1]);
      if (match![2] === 'category') expect(['industry', 'concept']).toContain(value);
      else if (match![2].startsWith('search:')) expect(value, 'no funds search is entered in this flow').toBe('');
      else { fields(value, ['key', 'ascending']); expect(['name', 'main_net_cny', 'pct_change']).toContain(value.key); expect(typeof value.ascending).toBe('boolean'); }
    } else if (identity.startsWith(owner + ':tracking-sort:')) {
      // SectorTracking.tsx uses StockRanking.tsx SortKey and an ascending flag.
      datedContext(identity.slice((owner + ':tracking-sort:').length));
      if (value !== null) { fields(value, ['key', 'ascending']); expect(stockSorts).toContain(value.key); expect(typeof value.ascending).toBe('boolean'); }
    } else if (identity.startsWith(owner + ':original:') || identity.endsWith(':scroll')) {
      expect(scroll(value), 'scroll state contains positions only').toBe(true);
    } else if (identity.startsWith(owner + ':overview-notes:')) {
      expect(typeof value).toBe('boolean');
    } else if (identity.startsWith(owner + ':research-extension:')) {
      const field = identity.split(':').at(-1);
      if (identity.includes(':visible:')) expect(Number.isInteger(value) && value >= 1 && value <= 10_000).toBe(true);
      else if (field === 'scope') expect(['all', 'sector', 'watch']).toContain(value);
      else if (field === 'ranking-reset') expect(Number.isInteger(value) && value >= 0).toBe(true);
      else if (field === 'sort') expect(['pct_change', 'speed_5m', 'amount_cny']).toContain(value);
      else if (field === 'tab') expect(['走势', '竞价', '异动', '资讯', '证据', 'AI解读']).toContain(value);
      else if (field === 'pane') expect(['板块', '列表', '详情']).toContain(value);
      else throw new Error('Unexpected workspace research reading preference');
    } else {
      expect(identity.startsWith(owner + ':auction:')).toBe(true);
      const field = identity.split(':').at(-1);
      if (field === 'mode') expect(['amount', 'delta']).toContain(value);
      else if (field === 'range') expect(['all', 'ladder', 'watch']).toContain(value);
      else if (field === 'reset') expect(Number.isInteger(value) && value >= 0).toBe(true);
      else if (field === 'sort') expect([null, 'auction', 'amount', 'seal', 'current']).toContain(value);
      else if (field === 'chart') expect([null, 'traded', 'seals']).toContain(value);
      else if (field === 'node') expect(value === null || Object.keys(value).sort().join(',') === 'day,value'
        && /^\d{4}-\d{2}-\d{2}$/.test(value.day) && ['09:15', '09:20', '09:25'].includes(value.value)).toBe(true);
      else throw new Error('Unexpected workspace reading state field');
    }
  }
  const serialized = JSON.stringify(stored.session);
  for (const secret of [originalText, reviewText, ...secrets].filter(Boolean)) {
    expect(serialized.includes(secret), 'credentials and private research stay out of browser storage').toBe(false);
  }
}

async function data(page: Page, path: string) {
  const result = await page.request.get('/api/v1/' + path);
  expect(result.status(), path).toBe(200);
  return (await result.json()).data;
}
async function browserTime(page: Page) {
  const value = JSON.parse(await readFile(clockPath, 'utf8'));
  expect(value.project).toBe(project);
  await page.clock.setFixedTime(new Date(new Date(value.business_at).getTime() + 10 * 60_000));
  return value;
}
async function advance(page: Page, at: string) {
  expect(project).toMatch(/^jlpm-check-[a-f0-9]{10}-acceptance-source$/);
  const previous = JSON.parse(await readFile(clockPath, 'utf8'));
  expect(new Date(at).getTime()).toBeGreaterThan(new Date(previous.business_at).getTime());
  await writeFile(clockPath + '.next', JSON.stringify({ project, business_at: at, real_at: new Date().toISOString() }) + '\n');
  await rename(clockPath + '.next', clockPath);
  await browserTime(page);
}
async function savedBy(page: Page, path: string, action: () => Promise<void>, status = 201) {
  const pending = page.waitForResponse(response => response.url().endsWith('/api/v1/' + path) && response.request().method() === 'POST');
  await action(); const response = await pending; expect(response.status()).toBe(status);
  return (await response.json()).data;
}
async function material(page: Page, key: string, text: string) {
  await page.goto('/data?view=materials');
  const form = page.getByRole('form', { name: '研究材料表单' });
  await form.getByLabel('材料编号', { exact: true }).fill(key);
  await form.getByLabel('材料内容', { exact: true }).fill(text);
  await form.getByLabel('保存权利依据', { exact: true }).fill('本人自制隔离验收材料，无真实行情和私人研究数据');
  return savedBy(page, 'evidence', () => form.getByRole('button', { name: '保存材料', exact: true }).click());
}
async function snapshot(page: Page) {
  await page.goto('/data?view=snapshots');
  await page.getByRole('button', { name: '使用当前时间', exact: true }).click();
  const receipt = await savedBy(page, 'snapshots', () => page.getByRole('button', { name: '创建冻结快照', exact: true }).click(), 202);
  await expect.poll(async () => (await data(page, 'snapshots/' + receipt.result_ref.id)).state, { timeout: 15_000 }).toBe('ready');
  const region = page.getByRole('region', { name: '快照详情', exact: true });
  await expect(region).toContainText('已冻结');
  await region.getByRole('button', { name: '核对快照指纹', exact: true }).click();
  await expect(region).toContainText('快照指纹一致');
  return data(page, 'snapshots/' + receipt.result_ref.id);
}
async function research(page: Page, kind: string, date: string, title: string, snap: any, calendar: any) {
  const names: Record<string, string> = { reviews: '每日复盘', directions: '方向研究', plans: '条件预案' };
  await page.goto('/research/' + kind + '?date=' + date);
  await page.getByRole('button', { name: '新建' + names[kind], exact: true }).last().click();
  const dialog = page.getByRole('dialog', { name: '编辑' + names[kind], exact: true });
  await dialog.getByLabel('研究标题', { exact: true }).fill(title);
  await dialog.getByLabel('选择冻结快照', { exact: true }).selectOption(snap.id);
  await dialog.getByLabel('日历版本', { exact: true }).selectOption(calendar.id);
  await dialog.getByLabel('材料与判断限制（每行一项）', { exact: true }).fill('隔离demo合成判断，非真实市场数据，不作交易或收益判断。');
  return dialog;
}
async function publishNew(page: Page, dialog: Locator) {
  const document = await savedBy(page, 'research-documents', () => dialog.getByRole('button', { name: '保存草稿', exact: true }).click());
  await expect(dialog.getByRole('status')).toContainText('已保存第 1 版');
  const revision = await savedBy(page, 'revisions/' + document.head_revision_id + '/publish', () => dialog.getByRole('button', { name: '发布此版本', exact: true }).click(), 200);
  await expect(dialog.getByRole('status')).toContainText('已发布第 1 版');
  await dialog.getByRole('button', { name: '收起', exact: true }).click();
  return { document, revision };
}
async function follow(page: Page, link: Locator) {
  await expect(link).toBeVisible(); const href = await link.getAttribute('href'); expect(href).toBeTruthy(); await page.goto(href!);
}
async function walkOriginal(page: Page, chain: any, info: TestInfo, prefix: string) {
  await page.goto(chain.validation_url); await page.reload();
  let dialog = page.getByRole('dialog', { name: '次日验证详情', exact: true });
  await expect(dialog).toContainText('更正后资料不足，原支持结论继续保留');
  await screenshots(page, info, prefix + '-validation');
  await dialog.getByRole('button', { name: '查看前一份验证原文', exact: true }).click();
  dialog = page.getByRole('dialog', { name: '次日验证详情', exact: true });
  await expect(dialog).toContainText('材料支持原条件'); await expect(dialog).toContainText('已更正，原文保留');
  await follow(page, dialog.getByRole('link', { name: '回查关联竞价观察原件', exact: true }));
  dialog = page.getByRole('dialog', { name: '竞价观察详情', exact: true });
  await expect(dialog).toContainText('晚补记录'); await expect(dialog).toContainText('合成检查点材料');
  await follow(page, dialog.getByRole('link', { name: /回查原预案.*第 1 版/ }));
  dialog = page.getByRole('dialog', { name: '研究版本详情', exact: true });
  await expect(dialog).toContainText('目标日合成材料有记录时人工核查');
  await follow(page, dialog.getByRole('link', { name: new RegExp(chain.direction_title + '.*第 1 版') }));
  dialog = page.getByRole('dialog', { name: '研究版本详情', exact: true });
  await expect(dialog).toContainText('固定引用原复盘的合成方向');
  await follow(page, dialog.getByRole('link', { name: new RegExp(chain.review_title + '.*第 1 版') }));
  dialog = page.getByRole('dialog', { name: '研究版本详情', exact: true });
  await expect(dialog).toContainText(reviewText); await expect(dialog).toContainText('历史版本');
  await expect(dialog.locator('.saved-research-content')).not.toContainText('合成复盘 R2');
  const reviewUrl = new URL(page.url()).pathname + new URL(page.url()).search;
  await screenshots(page, info, prefix + '-original-review');
  await follow(page, dialog.getByRole('link', { name: '回查冻结快照', exact: true }));
  const region = page.getByRole('region', { name: '快照详情', exact: true });
  await expect(region).toContainText(chain.snapshot1_id);
  await region.getByRole('button', { name: '核对快照指纹', exact: true }).click(); await expect(region).toContainText('快照指纹一致');
  await page.goto(reviewUrl);
  await follow(page, page.getByRole('dialog').getByRole('link', { name: new RegExp('^' + chain.material_key + '.*第 1 版') }));
  await expect(page.locator('.material-detail .material-body')).toHaveText(originalText);
  await expect(page.locator('.material-detail')).toContainText('历史版本 · 只读');
  await screenshots(page, info, prefix + '-original-material');
}

test('UI creates a complete cross-day chain and later revisions preserve the original', async ({ page }, info) => {
  test.skip(phase !== 'record');
  const initial = await browserTime(page); const sourceDay = initial.business_at.slice(0, 10);
  const addDays = (amount: number) => new Date(new Date(sourceDay + 'T00:00:00Z').getTime() + amount * 86400_000).toISOString().slice(0, 10);
  const targetDay = addDays(2); const key = 'vertical-' + crypto.randomUUID();
  const pageErrors: string[] = []; page.on('pageerror', error => pageErrors.push(error.message));
  const writes: Record<string, number> = {};
  page.on('response', response => { if (response.request().method() === 'POST' && response.url().includes('/api/v1/') && response.status() < 300) { const path = new URL(response.url()).pathname; writes[path] = (writes[path] ?? 0) + 1; } });
  await page.goto('/'); await signIn(page);
  const auth = await page.request.get('/api/v1/session');
  expect(auth.status()).toBe(200);
  const authState = await auth.json(); expect(authState.meta.app_mode).toBe('demo');
  await expect(page.locator('.ty-source-badge')).toHaveText('演示环境 · 非真实行情');
  const e1 = await material(page, key, originalText);
  await page.goto('/data?view=calendar');
  const calendarForm = page.getByRole('form', { name: '日历导入表单', exact: true });
  const segments = [{ start: '09:15', end: '09:25', kind: 'auction' }, { start: '09:30', end: '11:30', kind: 'continuous' }, { start: '13:00', end: '15:00', kind: 'continuous' }];
  await calendarForm.getByLabel('日历 JSON', { exact: true }).fill(JSON.stringify({ exchange: 'SSE', version: key, source_ref: '自制隔离demo日历，不是真实交易所日程', rights_basis: '本人自制测试', coverage_start: sourceDay, coverage_end: targetDay, sessions: [{ trade_day: sourceDay, is_open: true, segments }, { trade_day: addDays(1), is_open: false, segments: [] }, { trade_day: targetDay, is_open: true, segments }] }));
  const calendar = await savedBy(page, 'calendar-versions', () => calendarForm.getByRole('button', { name: '导入日历版本', exact: true }).click());
  const s1 = await snapshot(page); expect(s1.items.map((item: any) => item.evidence_version_id)).toEqual([e1.id]);
  await screenshots(page, info, 'vertical-initial-snapshot');
  const reviewTitle = '纵向原复盘-' + key; const directionTitle = '纵向原方向-' + key;
  let dialog = await research(page, 'reviews', sourceDay, reviewTitle, s1, calendar);
  await dialog.getByLabel('复盘正文', { exact: true }).fill(reviewText);
  await dialog.getByRole('checkbox', { name: `引用材料 ${key} 第 1 版`, exact: true }).check();
  const review = await publishNew(page, dialog);
  dialog = await research(page, 'directions', sourceDay, directionTitle, s1, calendar);
  await dialog.getByLabel('选择复盘文档', { exact: true }).selectOption(review.document.id);
  await dialog.getByRole('checkbox', { name: `引用复盘 ${reviewTitle} 第 1 版`, exact: true }).check();
  await dialog.getByLabel('方向 1 名称', { exact: true }).fill('合成方向');
  await dialog.getByLabel('方向 1 人工判断', { exact: true }).fill('固定引用原复盘的合成方向');
  await dialog.getByRole('checkbox', { name: `引用材料 ${key} 第 1 版`, exact: true }).check();
  const direction = await publishNew(page, dialog);
  dialog = await research(page, 'plans', sourceDay, '纵向原预案-' + key, s1, calendar);
  await dialog.getByLabel('选择方向文档', { exact: true }).selectOption(direction.document.id);
  await dialog.getByRole('checkbox', { name: `引用方向 ${directionTitle} 第 1 版`, exact: true }).check();
  await expect(dialog.getByRole('region', { name: '预案目标日期', exact: true })).toContainText(targetDay);
  await dialog.getByRole('button', { name: '采用此日为目标日', exact: true }).click();
  await dialog.getByLabel('情景 1 名称', { exact: true }).fill('合成留存条件');
  await dialog.getByLabel('情景 1 触发条件', { exact: true }).fill('目标日合成材料有记录时人工核查');
  await dialog.getByLabel('情景 1 失效条件', { exact: true }).fill('材料缺失或矛盾时停止沿用判断');
  await dialog.getByRole('checkbox', { name: `引用材料 ${key} 第 1 版`, exact: true }).check();
  await screenshots(page, info, 'vertical-plan-editor');
  const plan = await publishNew(page, dialog); const scenario = plan.revision.scenarios[0];
  await advance(page, targetDay + 'T09:24:00+08:00');
  const auctionEvidence = await material(page, key + '-auction', '合成检查点材料：09:25之前已通过正常页面保存。');
  await advance(page, targetDay + 'T09:26:00+08:00');
  const s2 = await snapshot(page);
  await page.goto(`/auction?legacy=1&date=${targetDay}&plan=${plan.revision.id}&scenario=${scenario.id}`);
  await page.getByRole('button', { name: '记录竞价观察', exact: true }).click(); dialog = page.getByRole('dialog', { name: '记录竞价观察', exact: true });
  await dialog.getByLabel('选择冻结快照', { exact: true }).selectOption(s2.id);
  await dialog.getByLabel('观察资料状态', { exact: true }).selectOption('observed');
  await dialog.getByLabel('事实 1 字段', { exact: true }).fill('合成检查点材料');
  await dialog.getByLabel('事实 1 值', { exact: true }).fill('合成资料已经留存');
  await dialog.getByLabel('事实 1 单位', { exact: true }).fill('文字');
  await dialog.getByLabel('事实 1 材料', { exact: true }).selectOption(auctionEvidence.id);
  await dialog.getByLabel('人工解释或缺失原因', { exact: true }).fill('录入时间晚于检查点，人工解释与事实分开。');
  const observation = await savedBy(page, 'auction-observations', () => dialog.getByRole('button', { name: '保存观察记录', exact: true }).click());
  expect(observation.timeliness).toBe('late');
  await page.getByRole('dialog').getByRole('button', { name: '收起', exact: true }).click();
  await follow(page, page.getByRole('link', { name: '进入原情景次日验证 ›', exact: true }));
  await expect(page.getByRole('button', { name: '记录次日验证', exact: true })).toBeDisabled();
  await expect(page.getByText(/尚未收盘，暂不可验证/)).toBeVisible();
  await screenshots(page, info, 'vertical-before-close');
  await advance(page, targetDay + 'T15:05:00+08:00');
  const closeEvidence = await material(page, key + '-close', '合成收盘材料：仅验证人工结论引用，不是市场事实或收益。');
  const s3 = await snapshot(page);
  await page.goto(`/research/validations?date=${targetDay}&plan=${plan.revision.id}&scenario=${scenario.id}`);
  await page.getByRole('button', { name: '记录次日验证', exact: true }).click(); dialog = page.getByRole('dialog', { name: '记录次日验证', exact: true });
  await dialog.getByLabel('选择冻结快照', { exact: true }).selectOption(s3.id);
  await dialog.getByLabel('关联观察', { exact: true }).selectOption(observation.id);
  await dialog.getByLabel('人工验证结论', { exact: true }).selectOption('supported');
  await dialog.getByRole('checkbox', { name: `引用材料 ${closeEvidence.source_key} 第 1 版`, exact: true }).check();
  await dialog.getByLabel('核对理由或缺失原因', { exact: true }).fill('合成材料支持原条件，人工核对不等于成交或收益。');
  const validation1 = await savedBy(page, 'validations', () => dialog.getByRole('button', { name: '保存验证记录', exact: true }).click());
  await page.getByRole('dialog').getByRole('button', { name: '追加更正', exact: true }).click(); dialog = page.getByRole('dialog', { name: '追加验证更正', exact: true });
  await dialog.getByLabel('选择冻结快照', { exact: true }).selectOption(s3.id);
  await dialog.getByLabel('核对理由或缺失原因', { exact: true }).fill('更正后资料不足，原支持结论继续保留');
  const validation2 = await savedBy(page, 'validations', () => dialog.getByRole('button', { name: '保存验证更正', exact: true }).click());
  const validationUrl = new URL(page.url()).pathname + new URL(page.url()).search;
  await page.goto(`/research/reviews?date=${sourceDay}&document=${review.document.id}&revision=${review.revision.id}`);
  await page.getByRole('dialog').getByRole('button', { name: '以此版内容新增修订', exact: true }).click(); dialog = page.getByRole('dialog');
  await dialog.getByLabel('复盘正文', { exact: true }).fill('合成复盘 R2：后来的修订，不替换原链。');
  const r2 = await savedBy(page, `research-documents/${review.document.id}/revisions`, () => dialog.getByRole('button', { name: '保存草稿', exact: true }).click());
  await savedBy(page, `revisions/${r2.id}/publish`, () => dialog.getByRole('button', { name: '发布此版本', exact: true }).click(), 200);
  await page.goto('/data?view=materials');
  await page.getByRole('button', { name: `更正材料 ${key} 版本 1`, exact: true }).click();
  await page.getByRole('form', { name: '研究材料表单' }).getByLabel('材料内容', { exact: true }).fill('合成材料第二版，不能改变旧快照和旧研究。');
  const e2 = await savedBy(page, 'evidence', () => page.getByRole('button', { name: '保存更正版本', exact: true }).click());
  expect((await data(page, 'snapshots/' + s1.id))).toEqual(s1);
  expect((await data(page, 'revisions/' + review.revision.id)).content_hash).toBe(review.revision.content_hash);
  expect((await data(page, 'revisions/' + direction.revision.id)).upstream_revision_ids).toEqual([review.revision.id]);
  expect((await data(page, 'revisions/' + plan.revision.id)).scenarios).toEqual(plan.revision.scenarios);
  expect((await data(page, 'validations/' + validation1.id)).content_hash).toBe(validation1.content_hash);
  const chain: any = { mode: 'demo', source_day: sourceDay, target_day: targetDay, material_key: key, review_title: reviewTitle, direction_title: directionTitle, validation_url: validationUrl, snapshot1_id: s1.id, created_through_ui: true, writes, resources: {} };
  await walkOriginal(page, chain, info, 'vertical');
  const paths = ['evidence/' + e1.id, 'evidence/' + e2.id, 'evidence/' + auctionEvidence.id, 'evidence/' + closeEvidence.id,
    ...[s1, s2, s3].map(s => 'snapshots/' + s.id), ...[review.revision, direction.revision, plan.revision, r2].map(r => 'revisions/' + r.id),
    'auction-observations/' + observation.id, 'validations/' + validation1.id, 'validations/' + validation2.id,
    ...[review, direction, plan].map(row => 'research-documents/' + row.document.id)];
  for (const path of paths) chain.resources['/api/v1/' + path] = await data(page, path);
  const ready = await page.request.get('/health/ready'); expect(ready.status()).toBe(200);
  const state = await readFile('/workspace/acceptance/revision.json', 'utf8'); chain.build_revision = JSON.parse(state).build_revision;
  expect(writes['/api/v1/research-documents']).toBe(3); expect(writes['/api/v1/snapshots']).toBe(3); expect(writes['/api/v1/validations']).toBe(2);
  expect(pageErrors).toEqual([]);
  await readingStorage(page, authState.data.user.id, [process.env.JL_E2E_PASSWORD!, authState.data.csrf_token]);
  await writeFile(directory + '/chain.json', JSON.stringify(chain, null, 2) + '\n');
});

test('restored instance browser traverses exact originals and refreshes without moving references', async ({ page }, info) => {
  test.skip(phase !== 'restored');
  await browserTime(page); const chain = JSON.parse(await readFile(directory + '/chain.json', 'utf8'));
  await page.goto('/'); await signIn(page); await walkOriginal(page, chain, info, 'restored');
  for (const [path, expected] of Object.entries(chain.resources)) {
    const response = await page.request.get(path); expect(response.status()).toBe(200); expect((await response.json()).data).toEqual(expected);
  }
  await page.reload(); await expect(page.locator('.material-detail .material-body')).toHaveText(originalText);
  await page.goto('/workbench');
  const authState = await data(page, 'session');
  await readingStorage(page, authState.user.id, [process.env.JL_E2E_PASSWORD!, authState.csrf_token]);
  const exit = page.getByRole('button', { name: '退出', exact: true });
  if (!(await exit.isVisible())) await page.getByRole('button', { name: '打开主导航', exact: true }).click();
  const signedOut = page.waitForResponse(response => response.url().endsWith('/api/v1/session') && response.request().method() === 'DELETE');
  await exit.click();
  expect((await signedOut).status()).toBe(204);
  await expect(page.getByRole('heading', { name: '登录工作空间', exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
  expect((await page.request.get('/api/v1/revisions/' + Object.values(chain.resources as any).find((row: any) => row.title === chain.review_title && row.revision_number === 1).id)).status()).toBe(401);
});
