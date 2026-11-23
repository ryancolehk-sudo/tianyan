import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { ReportView, RunView, SettingsView, ShadowEvaluation } from '../../src/features/tomorrow/model';
import { day as reviewDay, blank } from './reviewTestFixtures';

// Explicit synthetic protocol fixtures. They do not represent market data.
const meta = { app_mode: 'demo', request_id: 'synthetic-tomorrow', generated_at: '2026-10-25T14:30:00Z', quality: { status: 'partial', reasons: ['Synthetic protocol fixture'] } };
const titles = ['大盘概览', '核心新闻', '财经与社会热词', '板块分析与仓位', '重点板块与标的', '涨停池全景', '热门概念与业绩', '风险与后续日程'];
function sampleReport(): ReportView { return {
  id: 'report-one', run_id: 'run-one', revision_number: 1, source_trade_day: '2026-10-23', target_trade_day: '2026-10-26', as_of_time: '2026-10-25T14:30:00Z', slot: 'evening', status: 'draft', summary: '合成测试摘要，仅用于页面协议验证。', created_at: '2026-10-25T14:31:00Z',
  sections: titles.map((title, i) => ({ key: `section-${i}`, title, state: i === 0 ? 'complete' : 'partial', summary: `${title}的合成说明`, paragraphs: ['保留事实与判断的依据。'], columns: [{ key: 'name', label: '对象' }, { key: 'close', label: '收盘价（元）' }], rows: [{ name: '合成机电', close: 11 }], evidence_refs: ['ref-one'], limitations: i ? ['财务覆盖不足，暂不可判断。'] : [] })),
  dossier: { core_directions: [{ name: '合成方向', judgment: '等待条件确认' }], watchlist: [{ code: '600001.SH', name: '合成机电', rationale: '观察相对强弱' }], risks: [{ name: '样本风险', summary: '合成数据不可用于交易' }], scenarios: [{ id: 'scenario-one', name: '合成观察情景', status: 'pending', time_window: '开盘后', preconditions: '核对资料', trigger: '出现观察信号', confirmation: '等待人工确认', invalidation: '条件失效即退出观察', risk_veto: '数据不足', required_data: ['分时', '盘口'], verification: '人工确认', evidence_refs: ['ref-one'] }] },
  judgments: [{ object_type: 'stock', object_id: '600001.SH', judgment: '合成判断', support_refs: ['ref-one'], refuting_refs: [] }], evidence_refs: [{ id: 'ref-one', source_name: '合成资料来源', source_id: 'source-one', received_at: '2026-10-25T14:20:00Z', source_time: '2026-10-23T07:00:00Z', content_hash: 'a'.repeat(64), url: 'https://example.com/source' }], limitations: ['社会语料不足，暂无社会热词分数。'],
}; }
function sampleRun(): RunView { return { id: 'run-one', source_trade_day: '2026-10-23', target_trade_day: '2026-10-26', slot: 'evening', state: 'complete', stage: '已保存八章研究', report_id: 'report-one', as_of_time: '2026-10-25T14:30:00Z', created_at: '2026-10-25T14:30:00Z', completed_at: '2026-10-25T14:31:00Z', limitations: [], stages: [{ role: 'market_events', state: 'complete' }, { role: 'candidates', state: 'complete' }, { role: 'scenarios', state: 'complete' }, { role: 'review', state: 'complete' }] }; }
function sampleSettings(): SettingsView { return { revision: 0, enabled: false, use_ai: true, holdings: [], cash: null, risk_policy: { approved: false, price_rule: 'none', max_total_weight: null, max_stock_weight: null, max_sector_weight: null, risk_per_trade: null }, ai_source_ids: [], sources: [{ id: 'source-one', name: '合成资料来源', ai_allowed: true }, { id: 'restricted', name: '受限资料', ai_allowed: false }], evening_time: '22:30', overnight_time: '08:30', timezone: 'Asia/Shanghai' }; }
async function setup(page: Page, options: { empty?: boolean; role?: string; conflict?: boolean; delayed?: boolean; failure?: boolean; reportPatch?: Partial<ReportView>; evaluation?: ShadowEvaluation; objectDenied?: boolean } = {}) {
  const report = { ...sampleReport(), ...options.reportPatch }; const run = sampleRun(); let settings = sampleSettings();
  const state = { errors: [] as string[], writes: [] as Array<{ path: string; body: any; csrf?: string }>, objectReads: [] as Array<{ kind: string; offset: number; limit: number }>, runReads: 0, generated: false, validations: [] as any[] };
  page.on('pageerror', error => state.errors.push(error.message));
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), async () => { await page.keyboard.press('Escape'); });
  await page.route('**/api/v1/**', async route => {
    const req = route.request(); const url = new URL(req.url()); const path = url.pathname;
    const ok = (data: unknown) => route.fulfill({ json: { data, meta } });
    if (req.method() !== 'GET') state.writes.push({ path, body: req.postDataJSON(), csrf: req.headers()['x-csrf-token'] });
    if (path === '/api/v1/session') return ok({ user: { id: 'synthetic-user', username: '合成测试', role: options.role ?? 'admin' }, csrf_token: 'synthetic-csrf' });
    if (path === '/api/v1/review-data/day') return ok(reviewDay(url.searchParams.get('trade_day') ?? '2026-10-23'));
    if (path === '/api/v1/review-data/series') return ok({ trade_days: ['2026-10-23'], records: [], state: 'partial' });
    if (path === '/api/v1/review-data/notes') return ok({ trade_day: '2026-10-23', state: 'empty', content: blank(), head_id: null, writable: true, is_current: true, revision: null });
    if (!path.startsWith('/api/v1/tomorrow')) return ok({ items: [], shared_date: null, trade_days: [], sources: [], themes: { items: [] }, metrics: {}, records: [] });
    if (path.endsWith('/evaluation')) return options.evaluation ? ok(options.evaluation) : route.fulfill({ status: 404, json: { error: { message: '合成评估记录暂不可用' } } });
    if (options.failure && req.method() === 'GET' && path.endsWith('/runs')) return route.fulfill({ status: 503, json: { error: { message: '合成服务暂不可用' } } });
    if (path.endsWith('/settings')) {
      if (req.method() === 'PATCH') { const body = req.postDataJSON(); settings = { ...settings, ...body, revision: settings.revision + 1 }; }
      return ok(settings);
    }
    if (path.endsWith('/prepare')) return ok({ state: 'queued' });
    if (path.endsWith('/runs')) {
      if (req.method() === 'POST') { state.generated = true; return ok({ ...run, state: 'queued', report_id: null, stage: '等待规则计算' }); }
      if (options.delayed && url.searchParams.get('source_trade_day') === '2026-10-22') await new Promise(resolve => setTimeout(resolve, 700));
      if (url.searchParams.get('source_trade_day') === '2026-10-22') return ok({ items: [] });
      return ok({ items: options.empty && !state.generated ? [] : [run] });
    }
    if (path.endsWith('/objects')) {
      const kind = url.searchParams.get('kind') ?? 'stock'; const offset = Number(url.searchParams.get('offset') ?? 0); const limit = Number(url.searchParams.get('limit') ?? 10); state.objectReads.push({ kind, offset, limit });
      if (options.objectDenied && offset > 0) return route.fulfill({ status: 409, json: { error: { message: '本页底稿所需来源当前不可用，未显示不完整依据。' } } });
      const total = kind === 'stock' ? 21 : kind === 'sector' ? 12 : 11;
      const items = Array.from({ length: Math.min(limit, total - offset) }, (_, n) => ({ ...(kind === 'event' ? { title: `合成事件 ${offset + n + 1}`, content: '合成事件已保存正文，仅用于页面测试。', content_sufficient: false, published_at: '2026-10-23T07:00:00Z' } : { name: `${kind === 'stock' ? '合成候选' : '合成概念'} ${offset + n + 1}`, code: kind === 'stock' ? `600${String(offset + n + 1).padStart(3, '0')}.SH` : `BK${offset + n + 1}`, facts: { close: { value: String(10 + offset + n), unit: '元' } }, calculations: { returns_pct: { '1': { value: '2', unit: '%' } } } }), evidence_refs: ['ref-one'], evidence: [{ id: 'ref-one', source_row: offset + n, fields: ['close'] }], limitations: ['合成底稿，财务资料不足。'] }));
      return ok({ items, total, offset, limit, kind, as_of_time: '2026-10-25T14:30:00Z', input_hash: 'never-display-this-input-hash' });
    }
    if (path.includes('/runs/')) { state.runReads++; return ok(run); }
    if (path.endsWith('/revisions')) {
      if (options.conflict) return route.fulfill({ status: 409, json: { error: { message: '已有新的修订，请核对后再保存。' } } });
      const body = req.postDataJSON(); Object.assign(report, { id: 'report-two', based_on_id: 'report-one', revision_number: 2, summary: body.summary, judgments: body.judgments }); return ok(report);
    }
    if (path.endsWith('/confirm')) { report.status = 'confirmed'; return ok(report); }
    if (path.endsWith('/validation-evidence')) return ok({ items: [{ ...report.evidence_refs[0], id: 'next-day-observation', source_name: '合成次日观察', received_at: '2026-10-26T02:00:00Z', source_time: '2026-10-26T02:00:00Z' }] });
    if (path.endsWith('/validations')) { if (req.method() === 'POST') { state.validations.push({ ...req.postDataJSON(), id: 'validation-one', created_at: '2026-10-26T02:00:00Z' }); return ok(state.validations.at(-1)); } return ok({ items: state.validations }); }
    if (path.includes('/reports/')) return ok(report);
    return ok({ items: [] });
  });
  return state;
}

for (const width of [1440, 1024, 390]) test(`eight-section cached report layout ${width}`, async ({ page }, info) => {
  const state = await setup(page); await page.setViewportSize({ width, height: 950 }); await page.goto('/research/tomorrow');
  await expect(page.getByRole('heading', { name: '先核对事实，再等待条件确认' })).toBeVisible();
  await expect(page.getByRole('navigation', { name: '每日复盘页面' }).getByRole('link', { name: '明日预案', exact: true })).toHaveAttribute('aria-current', 'page');
  expect(await page.locator('.tp-section').count()).toBe(8); await page.locator('.tp-section').first().locator(':scope>summary').click(); await expect(page.getByRole('cell', { name: '11', exact: true })).toBeVisible();
  await page.locator('.tp-section').first().getByText('查看依据（1）', { exact: true }).click(); await expect(page.locator('.tp-section').first().getByRole('link', { name: '打开原文 ↗' })).toHaveAttribute('href', 'https://example.com/source');
  await expect(page.getByText('社会语料不足，暂无社会热词分数。')).toBeVisible();
  expect(await page.locator('body').evaluate(element => element.scrollWidth <= innerWidth + 2)).toBeTruthy();
  expect(await page.locator('.tp-scenarios dd').first().evaluate(element => element.getBoundingClientRect().width > 140)).toBeTruthy();
  expect(state.writes).toHaveLength(0); expect(state.errors).toEqual([]); await page.screenshot({ path: info.outputPath(`tomorrow-${width}.png`), fullPage: true });
});
test('empty state requires explicit generation and sends a protected rule-only request', async ({ page }) => {
  const state = await setup(page, { empty: true }); await page.goto('/research/tomorrow?source_trade_day=2026-10-23');
  await expect(page.getByText('研究从已缓存的事实开始')).toBeVisible(); expect(state.writes).toHaveLength(0);
  await page.getByLabel('使用 AI 分阶段研究', { exact: true }).uncheck(); await page.getByRole('button', { name: '生成仅规则稿' }).click();
  await expect(page.getByRole('heading', { name: '先核对事实，再等待条件确认' })).toBeVisible();
  const write = state.writes.find(item => item.path.endsWith('/runs'))!; expect(write.body).toMatchObject({ use_ai: false, slot: 'manual', source_trade_day: '2026-10-23' }); expect(write.csrf).toBe('synthetic-csrf');
  expect(state.errors).toEqual([]);
});
test('manual revision saves shared judgment, reason and original-report relationship', async ({ page }) => {
  const state = await setup(page); await page.goto('/research/tomorrow');
  await page.getByRole('button', { name: '修订研究判断' }).click(); await page.getByLabel('研究摘要', { exact: true }).fill('合成人工修订摘要'); await page.getByLabel('判断 1 内容').fill('等待更多证据'); await page.getByLabel('修订原因').fill('合成反证需要补充'); await page.getByRole('button', { name: '保存修订', exact: true }).click();
  await expect(page.getByText('合成人工修订摘要', { exact: true })).toBeVisible(); await expect(page.getByRole('button', { name: '查看所依据的原稿' })).toBeVisible();
  const write = state.writes.find(item => item.path.endsWith('/revisions'))!; expect(write.body).toMatchObject({ expected_revision: 1, reason: '合成反证需要补充', judgments: [{ object_id: '600001.SH', judgment: '等待更多证据' }] }); expect(state.errors).toEqual([]);
});
test('revision conflict preserves unsaved user input', async ({ page }) => {
  await setup(page, { conflict: true }); await page.goto('/research/tomorrow'); await page.getByRole('button', { name: '修订研究判断' }).click(); await page.getByLabel('研究摘要', { exact: true }).fill('不能丢失的修订'); await page.getByLabel('修订原因').fill('合成冲突'); await page.getByRole('button', { name: '保存修订', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('已有新的修订'); await expect(page.getByLabel('研究摘要', { exact: true })).toHaveValue('不能丢失的修订');
});
test('settings validate holdings and persist schedule, source subset and confirmed ratios', async ({ page }) => {
  const state = await setup(page); await page.goto('/research/tomorrow'); await page.locator('.tp-settings>summary').click();
  await expect(page.getByLabel('总仓位上限（%）')).toHaveValue('');
  await page.getByLabel('开启每日定时研究').check(); await page.getByLabel('合成资料来源', { exact: true }).check(); await expect(page.getByLabel('受限资料', { exact: false })).toBeDisabled();
  await page.getByLabel('可用现金（元）').fill('50000'); await page.getByRole('button', { name: '添加持仓' }).click(); await page.getByLabel('持仓 1 股票代码').fill('600001.SH'); await page.getByLabel('持仓 1 持股数量').fill('100'); await page.getByLabel('持仓 1 可卖数量').fill('200'); await page.getByRole('button', { name: '保存研究设置' }).click(); await expect(page.getByRole('alert')).toContainText('可卖数量不能超过');
  await page.getByLabel('持仓 1 可卖数量').fill('100'); await page.getByLabel('总仓位上限（%）').fill('65'); await page.getByLabel('单股仓位上限（%）').fill('15'); await page.getByLabel('板块仓位上限（%）').fill('25'); await page.getByLabel('单笔风险预算（%）').fill('1'); await page.getByLabel('支撑压力依据').selectOption('prior_range_20'); await page.getByLabel('我已确认本页填写的规则').check(); await page.getByRole('button', { name: '保存研究设置' }).click();
  await expect(page.getByText('设置已保存，下次生成使用这些设置。')).toBeVisible(); const write = state.writes.find(item => item.path.endsWith('/settings'))!;
  expect(write.body).toMatchObject({ expected_revision: 0, enabled: true, ai_source_ids: ['source-one'], cash: 50000, holdings: [{ code: '600001.SH', quantity: 100, sellable_quantity: 100 }], risk_policy: { approved: true, max_total_weight: .65, max_stock_weight: .15, max_sector_weight: .25, risk_per_trade: .01, price_rule: 'prior_range_20' } }); expect(state.errors).toEqual([]);
});
test('next-day validation appends to original report without changing its summary', async ({ page }) => {
  const state = await setup(page); await page.goto('/research/tomorrow'); await page.locator('.tp-validation>summary').click(); await page.getByLabel('预案情景').selectOption('scenario-one'); await page.getByLabel('观察结果').selectOption('insufficient_data'); await page.getByLabel('观察说明').fill('合成测试：盘口资料缺失，不能认定满足。'); await page.getByRole('button', { name: '追加验证结果' }).click();
  await expect(page.getByText('合成测试：盘口资料缺失，不能认定满足。', { exact: true })).toBeVisible(); const write = state.writes.find(item => item.path.endsWith('/validations'))!; expect(write.body).toMatchObject({ scenario_id: 'scenario-one', status: 'insufficient_data' }); await expect(page.getByText('合成测试摘要，仅用于页面协议验证。', { exact: true })).toBeVisible();
});
test('confirmed validation requires actual target-day evidence instead of original-plan references', async ({ page }) => {
  const state = await setup(page); await page.goto('/research/tomorrow'); await page.locator('.tp-validation>summary').click(); await page.getByLabel('预案情景').selectOption('scenario-one'); await page.getByLabel('观察结果').selectOption('confirmed'); await page.getByLabel('观察说明').fill('合成目标日人工观察'); await page.getByRole('button', { name: '追加验证结果' }).click(); await expect(page.getByRole('alert')).toContainText('需要选择目标交易日的实际依据');
  const options = page.getByLabel('目标交易日的观察依据'); await expect(options.locator('option[value="ref-one"]')).toHaveCount(0); await options.selectOption(['next-day-observation']); await page.getByRole('button', { name: '追加验证结果' }).click(); await expect(page.getByText('合成目标日人工观察', { exact: true })).toBeVisible(); expect(state.writes.find(item => item.path.endsWith('/validations'))!.body.evidence_refs).toEqual(['next-day-observation']);
});
test('viewer can read report but cannot generate, revise, confirm or change settings', async ({ page }) => {
  const state = await setup(page, { role: 'viewer' }); await page.goto('/research/tomorrow'); await expect(page.getByRole('button', { name: '生成研究草稿' })).toBeDisabled(); await expect(page.getByRole('button', { name: '修订研究判断' })).toBeDisabled(); await expect(page.getByRole('button', { name: '确认当前预案' })).toBeDisabled(); await page.locator('.tp-settings>summary').click(); await expect(page.getByRole('button', { name: '保存研究设置' })).toBeDisabled(); expect(state.writes).toHaveLength(0);
});
test('late prior-day list response cannot replace currently selected day', async ({ page }) => {
  await setup(page, { delayed: true }); await page.goto('/research/tomorrow?source_trade_day=2026-10-22'); await page.getByLabel('来源交易日').fill('2026-10-23'); await expect(page.getByRole('heading', { name: '先核对事实，再等待条件确认' })).toBeVisible(); await page.waitForTimeout(900); await expect(page.getByLabel('来源交易日')).toHaveValue('2026-10-23'); await expect(page.getByRole('heading', { name: '先核对事实，再等待条件确认' })).toBeVisible();
});
test('read failures display retry and never cause collection or model requests', async ({ page }) => {
  const state = await setup(page, { failure: true }); await page.goto('/research/tomorrow'); await expect(page.getByRole('alert')).toContainText('合成服务暂不可用'); await page.getByRole('button', { name: '重试读取', exact: true }).click(); await expect(page.getByRole('alert')).toContainText('合成服务暂不可用'); expect(state.writes).toHaveLength(0);
});
test('fifth review tab returns to the existing four pages with the same source date', async ({ page }) => {
  const state = await setup(page); await page.goto('/research/tomorrow?source_trade_day=2026-10-23'); await expect(page.getByRole('heading', { name: '先核对事实，再等待条件确认' })).toBeVisible();
  await page.getByRole('navigation', { name: '每日复盘页面' }).getByRole('link', { name: '板块轮动', exact: true }).click(); await expect(page).toHaveURL(/research\/reviews\?trade_day=2026-10-23&view=direction/);
  await page.getByRole('navigation', { name: '每日复盘页面' }).getByRole('link', { name: '明日预案', exact: true }).click(); await expect(page).toHaveURL(/research\/tomorrow\?source_trade_day=2026-10-23/); await expect(page.getByRole('heading', { name: '先核对事实，再等待条件确认' })).toBeVisible(); expect(state.errors).toEqual([]); expect(state.writes).toHaveLength(0);
});

test('complete audit bound to current report is distinct from personal confirmation', async ({ page }) => {
  const state = await setup(page, { reportPatch: { ai_audit: { audit_status: 'passed', audit_issues: [] }, audit_binding: { passed: true, audit_scope_complete: true, visible_content_hash: 'b'.repeat(64), ai_run_id: 'synthetic-audit-run', ai_input_hash: 'c'.repeat(64) } } });
  await page.goto('/research/tomorrow'); const audit = page.getByRole('region', { name: '全文审查结果' }); await expect(audit.getByText('AI 全文审查通过', { exact: true })).toBeVisible(); await expect(audit).toContainText('当前稿尚待本人确认'); await expect(page.getByRole('button', { name: '确认当前预案' })).toBeEnabled(); await expect(audit).not.toContainText('synthetic-audit-run'); expect(state.writes).toHaveLength(0);
  await expect(audit).toContainText('正文与已研究条件由 AI 核对，数值表格由程序核对，记录与本稿对应');
});

for (const mode of ['incomplete_scope', 'needs_review', 'missing_binding'] as const) test(`audit cannot pass when ${mode}`, async ({ page }) => {
  const state = await setup(page, { reportPatch: { status: 'confirmed', ai_audit: { audit_status: mode === 'needs_review' ? 'needs_review' : 'passed', audit_issues: ['合成审查问题：新闻覆盖不足，需核对结论。'] }, audit_binding: mode === 'missing_binding' ? null : { passed: mode !== 'needs_review', audit_scope_complete: mode !== 'incomplete_scope', visible_content_hash: 'd'.repeat(64) } } });
  await page.goto('/research/tomorrow'); const audit = page.getByRole('region', { name: '全文审查结果' }); await expect(audit.getByText('AI 全文审查待复核', { exact: true })).toBeVisible(); await expect(audit).toContainText('合成审查问题：新闻覆盖不足，需核对结论。'); await expect(audit).toContainText('当前稿已由本人确认'); await expect(audit.getByText('AI 全文审查通过', { exact: true })).toHaveCount(0); expect(state.writes).toHaveLength(0);
});

test('specific opening and close checks show all conditions while manual judgments stay visible', async ({ page }, info) => {
  const base = sampleReport();
  const first = { ...base.sections[0], analysis: [{ title: '原 AI 判断', text: '合成 AI 解释保留对照。', evidence_refs: ['ref-one'], authorship: 'ai', review_required: true }, { title: '本人修订', text: '合成人工结论优先，等待反证排除。', evidence_refs: ['ref-one'], authorship: 'manual' }] };
  const state = await setup(page, { reportPatch: { sections: [first, ...base.sections.slice(1)], dossier: { ...base.dossier, watchlist: [{ code: '600001.SH', name: '合成机电', judgment: '合成人工观察判断', judgment_authorship: 'manual' }], scenarios: [{ id: 'specific-opening', object_id: '600001.SH', label: '延续观察', observation_window: '09:25–09:59', status: 'pending', next_checks: [{ window: '09:25–09:59', subject: '合成机电开盘承接', support: '同组保持相对强势且承接证据齐备', invalidation: '原方向失去承接或风险未排除', missing_data: '缺少实际盘口观察' }, { window: '收盘后', subject: '合成方向延续性', support: '同范围成员表现保持一致', invalidation: '固定样本表现转弱', missing_data: '等待收盘资料齐备' }], evidence_refs: ['ref-one'] }] } } });
  await page.goto('/research/tomorrow'); await expect(page.getByText('观察对象：600001.SH', { exact: true })).toBeVisible();
  const checks = page.getByRole('region', { name: '具体观察检查' }); await expect(checks).toContainText('09:25–09:59'); await expect(checks).toContainText('收盘后'); await expect(checks).toContainText('同组保持相对强势且承接证据齐备'); await expect(checks).toContainText('原方向失去承接或风险未排除'); await expect(checks).toContainText('缺少实际盘口观察'); await expect(page.getByText('合成人工观察判断', { exact: true })).toBeVisible(); await expect(page.getByText('人工判断 · 已保留', { exact: true })).toBeVisible();
  const chapter = page.locator('.tp-section').first(); await chapter.locator(':scope>summary').click(); await expect(chapter).toContainText('本段待复核：已有人工修订'); await expect(chapter).toContainText('合成 AI 解释保留对照。'); await expect(chapter).toContainText('合成人工结论优先，等待反证排除。'); expect(state.errors).toEqual([]); expect(state.writes).toHaveLength(0);
  await page.screenshot({ path: info.outputPath('specific-checks-and-manual-judgment.png'), fullPage: true });
});

test('shadow progress shows actual pre-saved samples and preserves unknown costs', async ({ page }) => {
  const state = await setup(page, { evaluation: { progress: { eligible_shadow_target_day_count: 7, goal: 20, observation_window_complete: false, acceptance_status: 'insufficient_observation_days' }, reports: { latest_count: 10, confirmed_latest_count: 3 }, model_usage: { cost_cny: { total: null, known_total: '1.25', unknown_attempts: 2 } }, timing: { end_to_end_ms_p95: 12500 }, data_quality: { core_data_missing: 2 } } });
  await page.goto('/research/tomorrow'); const card = page.getByRole('region', { name: '影子运行进度' }); await expect(card).toContainText('7 / 20 个交易日'); await expect(card).toContainText('10 / 3 份'); await expect(card.locator('dd').nth(2)).toHaveText('未知'); await expect(card).toContainText('已知部分 ¥1.25'); await expect(card).toContainText('2 次调用费用未知'); await expect(card).toContainText('12.5 秒'); await expect(card).toContainText('历史重建不计入真实提前样本'); await expect(card).not.toContainText('收益'); expect(state.writes).toHaveLength(0); expect(state.errors).toEqual([]);
});

test('completed observation window still requires personal quality acceptance and supports actual zero cost', async ({ page }) => {
  const state = await setup(page, { evaluation: { progress: { eligible_shadow_target_day_count: 20, goal: 20, observation_window_complete: true, acceptance_status: 'requires_human_quality_review' }, reports: { latest_count: 20, confirmed_latest_count: 12 }, model_usage: { cost_cny: { total: '0', known_total: '0', unknown_attempts: 0 } }, timing: { end_to_end_ms_p95: null }, data_quality: { core_data_missing: 0 } } });
  await page.goto('/research/tomorrow'); const card = page.getByRole('region', { name: '影子运行进度' }); await expect(card).toContainText('20 / 20 个交易日'); await expect(card).toContainText('观察天数已达标 · 待本人质量验收'); await expect(card.locator('dd').nth(2)).toHaveText('¥0.00'); await expect(card.locator('dd').nth(3)).toHaveText('未知'); expect(state.writes).toHaveLength(0); expect(state.errors).toEqual([]);
});

test('unavailable shadow evaluation never blocks cached report or settings', async ({ page }) => {
  const state = await setup(page); await page.goto('/research/tomorrow'); const card = page.getByRole('region', { name: '影子运行进度' }); await expect(card).toContainText('运行记录读取失败，报告与设置仍可使用'); await expect(page.getByRole('heading', { name: '先核对事实，再等待条件确认' })).toBeVisible(); await page.locator('.tp-settings>summary').click(); await expect(page.getByRole('button', { name: '保存研究设置' })).toBeEnabled(); await card.getByRole('button', { name: '重读运行记录' }).click(); await expect(card).toContainText('运行记录读取失败'); expect(state.writes).toHaveLength(0); expect(state.errors).toEqual([]);
});

test('complete dossier loads only on expansion and pages full counts without writes', async ({ page }, info) => {
  const state = await setup(page); await page.goto('/research/tomorrow'); await expect(page.getByRole('heading', { name: '先核对事实，再等待条件确认' })).toBeVisible(); expect(state.objectReads).toHaveLength(0);
  const browser = page.locator('.tp-dossier'); await browser.locator(':scope>summary').click(); await expect(browser).toContainText('候选标的共 21 条'); await expect(browser.locator('.tp-dossier-list>button')).toHaveCount(10); await browser.locator('.tp-dossier-list>button').first().click(); const detail = browser.getByRole('article', { name: '底稿对象详情' }); await expect(detail).toContainText('10 元'); await expect(detail).toContainText('合成底稿，财务资料不足。'); await detail.getByText('原始依据的位置（1）', { exact: true }).click(); await expect(detail.getByRole('cell', { name: '合成资料来源', exact: true })).toBeVisible(); await expect(detail.getByRole('cell', { name: '0', exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('complete-dossier-object-detail.png'), fullPage: true });
  await browser.getByRole('button', { name: '下一页底稿' }).click(); await expect(browser).toContainText('合成候选 11'); await expect(browser.locator('.tp-dossier-detail')).toHaveCount(0); await expect(browser.getByText('合成候选 1', { exact: true })).toHaveCount(0);
  await browser.getByRole('button', { name: '概念板块', exact: true }).click(); await expect(browser).toContainText('概念板块共 12 条'); await browser.getByRole('button', { name: '事件', exact: true }).click(); await expect(browser).toContainText('事件共 11 条'); await expect(browser).toContainText('合成事件 1'); await expect(browser).not.toContainText('never-display-this-input-hash');
  expect(state.objectReads).toEqual([{ kind: 'stock', offset: 0, limit: 10 }, { kind: 'stock', offset: 10, limit: 10 }, { kind: 'sector', offset: 0, limit: 10 }, { kind: 'event', offset: 0, limit: 10 }]); expect(state.writes).toHaveLength(0); expect(state.errors).toEqual([]);
  await browser.locator(':scope>summary').click(); await expect(browser).not.toHaveAttribute('open'); expect(state.objectReads).toHaveLength(4); await browser.locator(':scope>summary').click(); await expect(browser.locator('.tp-dossier-count')).toContainText('事件共 11 条'); expect(state.objectReads).toHaveLength(5);
});

test('dossier permission failure clears prior-page objects and details without affecting report', async ({ page }) => {
  const state = await setup(page, { objectDenied: true }); await page.goto('/research/tomorrow'); const browser = page.locator('.tp-dossier'); await browser.locator(':scope>summary').click(); await expect(browser).toContainText('候选标的共 21 条'); await browser.locator('.tp-dossier-list>button').first().click(); await expect(browser.getByRole('article', { name: '底稿对象详情' })).toBeVisible();
  await browser.getByRole('button', { name: '下一页底稿' }).click(); await expect(browser.getByRole('alert')).toContainText('本页底稿所需来源当前不可用'); await expect(browser.locator('.tp-dossier-list')).toHaveCount(0); await expect(browser.locator('.tp-dossier-detail')).toHaveCount(0); await expect(browser.getByText('合成候选 1', { exact: true })).toHaveCount(0); await expect(page.getByRole('heading', { name: '先核对事实，再等待条件确认' })).toBeVisible();
  await browser.getByRole('button', { name: '上一页底稿' }).click(); await expect(browser).toContainText('候选标的共 21 条'); await expect(browser.getByRole('alert')).toHaveCount(0); expect(state.writes).toHaveLength(0); expect(state.errors).toEqual([]);
});

test('large reference catalog mounts only visible batches and releases them on close', async ({ page }) => {
  const refs = Array.from({ length: 3224 }, (_, i) => ({ ...sampleReport().evidence_refs[0], id: `large-ref-${i + 1}`, source_name: `合成目录 ${i + 1}` }));
  const state = await setup(page, { reportPatch: { evidence_refs: refs } }); await page.goto('/research/tomorrow'); await expect(page.getByRole('heading', { name: '先核对事实，再等待条件确认' })).toBeVisible();
  const catalog = page.getByRole('heading', { name: '本次证据目录', exact: true }).locator('..'); const group = catalog.locator('.tp-references'); await expect(group.locator('summary')).toHaveText('查看依据（3224）'); await expect(page.locator('.tp-reference')).toHaveCount(0);
  await group.locator('summary').click(); await expect(group.locator('.tp-reference')).toHaveCount(50); await expect(group.locator('.tp-reference').first()).toContainText('1. 合成目录 1'); await group.getByRole('button', { name: '加载更多依据', exact: false }).click(); await expect(group.locator('.tp-reference')).toHaveCount(100); expect(await group.locator('.tp-reference>b').allTextContents()).toEqual(refs.slice(0, 100).map((ref, i) => `${i + 1}. ${ref.source_name}`));
  await group.locator('summary').click(); await expect(group.locator('.tp-reference')).toHaveCount(0); await group.locator('summary').click(); await expect(group.locator('.tp-reference')).toHaveCount(50); await expect(group.locator('summary')).toHaveText('查看依据（3224）'); expect(state.writes).toHaveLength(0); expect(state.errors).toEqual([]);
});

test('large watchlist defaults to six research candidates and stays usable on a narrow screen', async ({ page }, info) => {
  const base = sampleReport(); const candidates = Array.from({ length: 123 }, (_, i) => ({ name: `合成观察候选 ${i + 1}`, code: `600${String(i + 1).padStart(3, '0')}.SH`, candidate_reasons: ['供应商涨停池', '用户自选'] }));
  const state = await setup(page, { reportPatch: { dossier: { ...base.dossier, watchlist: candidates } } }); await page.setViewportSize({ width: 390, height: 950 }); await page.goto('/research/tomorrow');
  const watched = page.getByRole('article', { name: '重点观察候选' }); await expect(watched).toContainText('候选共 123 条'); await expect(watched).toContainText('按研究覆盖顺序展示'); await expect(watched.locator('.tp-core-list>li')).toHaveCount(6); await expect(watched.getByText('合成观察候选 7', { exact: true })).toHaveCount(0); await expect(watched.locator('.tp-core-list>li').first()).toContainText('规则候选原因：供应商涨停池、用户自选'); await expect(watched).toContainText('更多对象与依据见完整研究底稿');
  expect(await page.locator('body').evaluate(element => element.scrollWidth <= innerWidth + 2)).toBeTruthy(); await page.screenshot({ path: info.outputPath('six-candidates-narrow.png'), fullPage: true }); await watched.getByRole('button', { name: '展开其余候选', exact: false }).click(); await expect(watched.locator('.tp-core-list>li')).toHaveCount(123); await expect(watched.getByText('合成观察候选 123', { exact: true })).toBeVisible(); await watched.getByRole('button', { name: '收起候选', exact: true }).click(); await expect(watched.locator('.tp-core-list>li')).toHaveCount(6); expect(state.writes).toHaveLength(0); expect(state.errors).toEqual([]);
});
