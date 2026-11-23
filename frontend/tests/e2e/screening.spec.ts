import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { Catalogue, ContextInput, PreviewInput, ResultItem, ResultState, Rule, RuleSave, RunResult, RunSummary } from '../../src/features/screening/types';
import { terminalDay, terminalFixture, stockA, stockB } from './terminalTestFixtures';

// All values and identities below are synthetic; no market provider is contacted.
const ruleId = '10000000-0000-4000-8000-000000000001';
const newRuleId = '10000000-0000-4000-8000-000000000002';
const historicRunId = '20000000-0000-4000-8000-000000000001';
const manualRunId = '20000000-0000-4000-8000-000000000002';
const stamp = `${terminalDay}T10:00:00+08:00`;
const definition: Rule['definition'] = { conditions: [{ field: 'pct_change', operator: 'gte', value: '0' }], sort_field: 'amount_cny', sort_direction: 'desc' };
const savedRule: Rule = { id: ruleId, version: 1, name: '合成观察规则', scope: 'market', definition,
  enabled: false, cooldown_seconds: 300, last_run_at: null, last_error: null, created_at: stamp, updated_at: stamp };
const catalogue: Catalogue = { engine_version: 'synthetic-v1', interval_seconds: 60, scope: '合成测试已存样本，不是真实行情', boundaries: ['数据缺失保持不可判断；测试资料不代表全市场。'],
  metrics: [
    { field: 'pct_change', label: '涨跌幅', unit: '%', description: '相对昨收，缺失不补零。' },
    { field: 'amount_cny', label: '成交额', unit: '元', description: '已存成交额。' },
  ] };
function resultItem(code: string, name: string, state: ResultState, actual: string | null): ResultItem {
  return { code, name, instrument_id: null, state, source_time: actual === null ? null : stamp, received_at: stamp,
    metrics: { pct_change: actual, amount_cny: actual === null ? null : '9007199254740993.01' },
    checks: [{ field: 'pct_change', operator: 'gte', target: '0', actual, state,
      reason: state === 'insufficient' ? '合成样本缺少涨跌幅，无法判断。' : state === 'matched' ? '实际数值满足条件。' : '实际数值不满足条件。' }] };
}
function runResult(options: { id?: string; rule?: Rule; context?: ContextInput; state?: ResultState; offset?: number; large?: boolean; preview?: PreviewInput } = {}): RunResult {
  const matches = options.large ? Array.from({ length: 101 }, (_, index) => resultItem(index ? `${880000 + index}.SZ` : stockB, index ? `合成命中${index}` : '合成零值', 'matched', '0'))
    : [resultItem(stockB, '合成零值', 'matched', '0')];
  const all = [...matches, resultItem(stockA, '合成未命中', 'not_matched', '-1'), resultItem('999993.SZ', '合成缺测', 'insufficient', null)];
  const filtered = options.state ? all.filter(item => item.state === options.state) : all;
  const offset = options.offset ?? 0; const context = options.context ?? {};
  const rule = options.rule ?? savedRule;
  return { id: options.id ?? null, rule_id: options.id ? rule.id : null, rule_name: options.id ? rule.name : null, rule_version: options.id ? rule.version : null,
    trade_day: context.day ?? terminalDay, as_of_time: context.as_of_time ?? stamp, mode: context.as_of_time ? 'observed' : 'review',
    counts: { total: all.length, matched: matches.length, not_matched: 1, insufficient: 1 }, items: filtered.slice(offset, offset + 100), offset, limit: 100,
    has_more: filtered.length > offset + 100, event_count: 0, input_hash: 'synthetic-input-hash', engine_version: 'synthetic-v1', data_revision: 'synthetic-revision', created_at: stamp,
    definition: options.preview?.definition ?? rule.definition, scope: options.preview?.scope ?? rule.scope,
    source_refs: [{ evidence_id: '30000000-0000-4000-8000-000000000001', source_id: '40000000-0000-4000-8000-000000000001', content_hash: 'a'.repeat(64), received_at: stamp }],
    reasons: ['合成浏览器测试资料，不是真实行情。'] };
}
function summary(run: RunResult): RunSummary {
  return { id: run.id!, rule_id: run.rule_id!, rule_version: run.rule_version!, rule_name: run.rule_name!, trade_day: run.trade_day, as_of_time: run.as_of_time,
    mode: run.mode, counts: run.counts, created_at: run.created_at, event_count: run.event_count, input_hash: run.input_hash, engine_version: run.engine_version };
}
type Write = { path: string; body: Record<string, unknown>; csrf: string | undefined; key: string | undefined };
async function fixture(page: Page, role: 'admin' | 'viewer' = 'admin') {
  const state = { reads: [] as string[], writes: [] as Write[], errors: [] as string[], rules: [structuredClone(savedRule)],
    results: { [historicRunId]: runResult({ id: historicRunId }) } as Record<string, RunResult>,
    marketReads: 0, large: false, holdPreview: false, releasePreview: undefined as (() => void) | undefined,
    failSave: 0, denyResult: false, holdSave: false, releaseSave: undefined as (() => void) | undefined };
  page.on('pageerror', error => state.errors.push(error.message));
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), async () => { await page.keyboard.press('Escape'); });
  await page.route('**/api/v1/**', async route => {
    const request = route.request(); const url = new URL(request.url());
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'demo', quality: { status: 'partial', reasons: ['合成测试数据'] } } } });
    const fail = (status: number, message: string) => route.fulfill({ status, json: { error: { message } } });
    if (url.pathname.endsWith('/session')) return ok({ user: { id: 'screening-synthetic-owner', username: 'synthetic', role }, csrf_token: 'synthetic-csrf' });
    if (url.pathname.endsWith('/workbench-view')) {
      if (url.searchParams.get('view') === 'overview') state.marketReads++;
      return ok({ ...terminalFixture(url.searchParams), detail_codes: [stockA, stockB], detail_revision: 'synthetic-screening-v1' });
    }
    if (url.pathname.endsWith('/workbench-details')) {
      const data = terminalFixture(url.searchParams); const codes = (url.searchParams.get('codes') ?? '').split(',');
      return ok({ trade_day: data.trade_day, revision: 'synthetic-screening-v1', stocks: data.stocks.filter(item => codes.includes(item.code)), auction_days: data.auction_days,
        events: [], seals: {}, sector_code: url.searchParams.get('sector'), members: data.selected_members, members_trade_day: data.trade_day,
        tracking_members: [], tracking_member_count: 0, tracking_members_truncated: false, funds_history: [] });
    }
    if (url.pathname.startsWith('/api/v1/screening/')) {
      if (request.method() === 'GET') state.reads.push(url.pathname + url.search);
      else {
        const headers = request.headers();
        state.writes.push({ path: url.pathname, body: request.postDataJSON(), csrf: headers['x-csrf-token'], key: headers['idempotency-key'] });
      }
      if (url.pathname.endsWith('/catalogue')) return ok(catalogue);
      if (url.pathname === '/api/v1/screening/rules' && request.method() === 'GET') return ok({ items: state.rules, limit: 20 });
      if (url.pathname === '/api/v1/screening/rules' && request.method() === 'POST') {
        if (state.failSave) return fail(state.failSave, '合成保存失败');
        const body = request.postDataJSON() as RuleSave;
        if (state.holdSave) await new Promise<void>(resolve => { state.releaseSave = resolve; });
        const old = state.rules.find(item => item.id === body.id);
        const next: Rule = { ...savedRule, ...body, id: body.id ?? newRuleId, version: (old?.version ?? 0) + 1 };
        state.rules = [next, ...state.rules.filter(item => item.id !== next.id)]; return ok(next);
      }
      if (url.pathname.endsWith('/preview')) {
        const body = request.postDataJSON() as PreviewInput;
        const result = runResult({ context: body, state: body.state, offset: body.offset, large: state.large, preview: body });
        if (state.holdPreview) await new Promise<void>(resolve => { state.releasePreview = resolve; });
        return ok(result);
      }
      if (/\/rules\/[^/]+\/runs$/.test(url.pathname)) {
        const selected = state.rules.find(item => item.id === url.pathname.split('/').at(-2))!;
        const result = runResult({ id: manualRunId, rule: selected, context: request.postDataJSON(), large: state.large });
        state.results[manualRunId] = result; return ok(result);
      }
      if (/\/runs\/[^/]+$/.test(url.pathname)) {
        if (state.denyResult) return fail(403, '合成读取权限已撤回');
        const id = url.pathname.split('/').at(-1)!; const source = state.results[id];
        const selected = state.rules.find(item => item.id === source.rule_id)!;
        return ok(runResult({ id, rule: { ...selected, version: source.rule_version!, name: source.rule_name! }, context: { day: source.trade_day },
          state: (url.searchParams.get('state') ?? undefined) as ResultState | undefined, offset: Number(url.searchParams.get('offset') ?? 0), large: state.large }));
      }
      if (url.pathname.endsWith('/runs')) return ok({ items: Object.values(state.results).filter(item => item.rule_id === url.searchParams.get('rule_id')).map(summary) });
      if (url.pathname.endsWith('/events')) return ok({ items: !url.searchParams.has('rule_id') || url.searchParams.get('rule_id') === ruleId ? [
        { id: 'event-1', rule_id: ruleId, rule_name: savedRule.name, rule_version: 1, run_id: historicRunId, code: stockA, name: '合成进入样本', direction: 'entered', source_time: stamp, created_at: stamp },
        { id: 'event-2', rule_id: ruleId, rule_name: savedRule.name, rule_version: 1, run_id: historicRunId, code: stockB, name: '合成退出样本', direction: 'exited', source_time: stamp, created_at: stamp },
      ] : [] });
      return fail(404, '合成路由未定义');
    }
    if (url.pathname.endsWith('/ai/settings')) return ok({ enabled: false, features: [] });
    return ok({ items: [], next_cursor: null });
  });
  return state;
}
const panel = (page: Page) => page.getByRole('region', { name: '条件选股与观察', exact: true });
const results = (page: Page) => panel(page).locator('.ty-screening-results');
async function open(page: Page) {
  await page.goto(`/workbench?view=overview&trade_day=${terminalDay}`);
  await page.getByRole('navigation', {name:'综合盯盘工作区'}).getByRole('button', {name:'条件选股',exact:true}).click();
  await expect(panel(page)).toBeVisible();
  await expect(panel(page).getByLabel('已保存规则')).toContainText('合成观察规则');
}

test('unvisited screening is inert; preview keeps zero, decimal precision, missing data and local stock selection', async ({ page }, testInfo) => {
  const state = await fixture(page);
  await page.goto(`/workbench?view=overview&trade_day=${terminalDay}`);
  await expect(page.getByRole('navigation', {name:'综合盯盘工作区'})).toBeVisible();
  await expect(panel(page)).toHaveCount(0);
  expect(state.reads).toEqual([]); expect(state.writes).toEqual([]);
  await page.getByRole('navigation', {name:'综合盯盘工作区'}).getByRole('button', {name:'条件选股',exact:true}).click();
  await expect(panel(page).getByLabel('已保存规则')).toContainText('合成观察规则');
  await panel(page).getByLabel('条件 1 数值', { exact: true }).fill('0');
  await panel(page).getByRole('button', { name: '预览草稿', exact: true }).click();
  await expect(results(page)).toContainText('草稿预览 · 未保存');
  await expect(results(page)).toContainText('9007199254740993.01 元');
  await results(page).getByText('逐条件原因（1 项）', { exact: true }).click();
  await expect(results(page)).toContainText('实际 0 %');
  await panel(page).screenshot({ path: testInfo.outputPath('screening-desktop.png') });
  const reads = state.marketReads;
  await results(page).getByRole('button', { name: /^合成零值/ }).click();
  await expect(page).toHaveURL(new RegExp(`stock=${stockB.replace('.', '\\.')}`));
  expect(state.marketReads).toBe(reads);
  await page.getByRole('navigation', {name:'综合盯盘工作区'}).getByRole('button', {name:'条件选股',exact:true}).click();
  await expect(panel(page).getByLabel('条件 1 数值', {exact:true})).toHaveValue('0');
  await results(page).getByRole('button', { name: '数据不足 1', exact: true }).click();
  await expect(results(page)).toContainText('合成缺测');
  await results(page).getByText('逐条件原因（1 项）', { exact: true }).click();
  await expect(results(page)).toContainText('实际 缺失');
  await expect(results(page)).toContainText('无法判断');
  expect(state.writes.every(write => write.path.endsWith('/preview'))).toBe(true);
  expect(state.writes[0].body).toMatchObject({ day: terminalDay, state: 'matched', offset: 0, limit: 100 });
  expect(state.rules).toHaveLength(1); expect(Object.keys(state.results)).toHaveLength(1); expect(state.errors).toEqual([]);
});

test('save, enable, persist a run and return to a fixed history version without monitoring the review date', async ({ page }) => {
  const state = await fixture(page); await open(page);
  await panel(page).getByLabel('规则名称', { exact: true }).fill('合成新规则');
  await panel(page).getByLabel('条件 1 数值', { exact: true }).fill('1.000000000001');
  await expect(panel(page).getByRole('status').filter({ hasText: '未保存草稿' })).toBeVisible();
  await expect(panel(page).getByLabel('保存后启用自动观察', { exact: true })).not.toBeChecked();
  await panel(page).getByRole('button', { name: '保存规则', exact: true }).click();
  await expect(panel(page)).toContainText('规则已保存为 v1，自动观察未启用');
  const create = state.writes.find(write => write.path.endsWith('/rules'))!;
  expect(create.body).toMatchObject({ name: '合成新规则', enabled: false, cooldown_seconds: 300 });
  expect(create.body).not.toHaveProperty('id'); expect(create.body).not.toHaveProperty('expected_version'); expect(create.body).not.toHaveProperty('day');
  expect(create.csrf).toBe('synthetic-csrf'); expect(create.key).toBeTruthy();
  await panel(page).getByRole('button', { name: '启用自动观察', exact: true }).click();
  await expect(panel(page)).toContainText('规则已保存为 v2，自动观察已启用');
  const enable = state.writes.filter(write => write.path.endsWith('/rules')).at(-1)!;
  expect(enable.body).toMatchObject({ id: newRuleId, expected_version: 1, enabled: true });
  expect(enable.body).not.toHaveProperty('day'); expect(enable.body).not.toHaveProperty('as_of_time');
  await panel(page).getByRole('button', { name: '运行并保存结果', exact: true }).click();
  await expect(results(page)).toContainText('已保存运行'); await expect(results(page)).toContainText('合成新规则 · v2');
  expect(state.writes.find(write => write.path.endsWith(`/${newRuleId}/runs`))?.body).toEqual({ day: terminalDay });
  await results(page).getByText('查看规则快照与来源引用（1）', { exact: true }).click();
  await expect(results(page)).toContainText('a'.repeat(64));
  await panel(page).getByLabel('已保存规则').selectOption(ruleId);
  await panel(page).getByRole('region', { name: '过去运行', exact: true }).getByRole('button').click();
  await expect(results(page)).toContainText('合成观察规则 · v1');
  await expect(panel(page).locator('.ty-screening-event')).toHaveText(['进入', '退出']);
  expect(state.errors).toEqual([]);
});

test('preview categories and pagination issue explicit filtered previews without saving a run', async ({ page }) => {
  const state = await fixture(page); state.large = true; await open(page);
  await panel(page).getByLabel('条件 1 数值', { exact: true }).fill('0');
  await panel(page).getByRole('button', { name: '预览草稿', exact: true }).click();
  await expect(results(page).locator('.ty-screening-result-row')).toHaveCount(100);
  await results(page).getByRole('button', { name: '下一页', exact: true }).click();
  await expect(results(page).locator('.ty-screening-result-row')).toHaveCount(1);
  await expect(results(page)).toContainText('合成命中100');
  expect(state.writes.at(-1)?.body).toMatchObject({ state: 'matched', offset: 100, limit: 100 });
  await results(page).getByRole('button', { name: '未命中 1', exact: true }).click();
  await expect(results(page)).toContainText('合成未命中');
  expect(state.writes.at(-1)?.body).toMatchObject({ state: 'not_matched', offset: 0 });
  await expect(results(page).getByRole('button', { name: '上一页', exact: true })).toBeDisabled();
  expect(state.writes.every(write => write.path.endsWith('/preview'))).toBe(true);
  expect(Object.keys(state.results)).toHaveLength(1); expect(state.errors).toEqual([]);
});

test('late preview cannot cross a known-at cutoff and an edited draft remains intact', async ({ page }) => {
  const state = await fixture(page); state.holdPreview = true; await open(page);
  await panel(page).getByLabel('规则名称', { exact: true }).fill('合成保留草稿');
  await panel(page).getByLabel('条件 1 数值', { exact: true }).fill('0');
  await panel(page).getByRole('button', { name: '预览草稿', exact: true }).click();
  await expect.poll(() => !!state.releasePreview).toBe(true);
  await page.getByRole('navigation', {name:'综合盯盘工作区'}).getByRole('button', {name:'数据覆盖',exact:true}).click();
  await page.locator('.ty-coverage > summary').click();
  await page.getByLabel('当时已知回看时点（北京时间）', { exact: true }).fill(`${terminalDay}T09:25`);
  await page.getByRole('button', { name: '按此时点回看', exact: true }).click();
  await page.getByRole('navigation', {name:'综合盯盘工作区'}).getByRole('button', {name:'条件选股',exact:true}).click();
  await expect(panel(page).locator('.ty-screening-context')).toContainText('09:25:00');
  const response = page.waitForResponse(response => response.url().endsWith('/screening/preview'));
  state.holdPreview = false; state.releasePreview!(); await response;
  await expect(results(page)).toHaveCount(0);
  await expect(panel(page).getByLabel('规则名称', { exact: true })).toHaveValue('合成保留草稿');
  await panel(page).getByRole('button', { name: '预览草稿', exact: true }).click();
  await expect(results(page)).toContainText('当时已知');
  expect(state.writes.at(-1)?.body).toMatchObject({ day: terminalDay, as_of_time: `${terminalDay}T09:25:00+08:00` });
  expect(state.errors).toEqual([]);
});

test('save failures retain drafts and idempotency keys; polling only reads rules and events', async ({ page }) => {
  await page.clock.install();
  const state = await fixture(page); await open(page);
  await panel(page).getByLabel('已保存规则').selectOption(ruleId);
  await panel(page).getByLabel('规则名称', { exact: true }).fill('合成待保存修改');
  state.failSave = 503;
  await panel(page).getByRole('button', { name: '保存规则', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toContainText('草稿已保留');
  await expect(panel(page).getByLabel('规则名称', { exact: true })).toHaveValue('合成待保存修改');
  state.failSave = 409;
  await panel(page).getByRole('button', { name: '保存规则', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toContainText('保存版本已变化');
  expect(state.writes[0].key).toBe(state.writes[1].key);
  expect(state.writes[1].body).toMatchObject({ id: ruleId, expected_version: 1, name: '合成待保存修改' });
  const writes = state.writes.length; const readCount = state.reads.length;
  await page.clock.fastForward(60_010);
  await expect.poll(() => state.reads.length).toBeGreaterThan(readCount);
  expect(state.reads.slice(readCount).every(path => path.includes('/rules') || path.includes('/events'))).toBe(true);
  expect(state.writes).toHaveLength(writes);
  await expect(panel(page).getByLabel('规则名称', { exact: true })).toHaveValue('合成待保存修改');
  expect(state.errors).toEqual([]);
});

test('viewer can inspect history but never evaluate or write; revocation clears private results', async ({ page }) => {
  const state = await fixture(page, 'viewer'); await open(page);
  await panel(page).getByLabel('已保存规则').selectOption(ruleId);
  await expect(panel(page).getByRole('button', { name: '保存规则', exact: true })).toBeDisabled();
  await expect(panel(page).getByRole('button', { name: '预览草稿', exact: true })).toBeDisabled();
  await expect(panel(page).getByRole('button', { name: '运行并保存结果', exact: true })).toBeDisabled();
  await expect(panel(page).getByLabel('条件 1 数值', { exact: true })).toBeDisabled();
  await panel(page).getByRole('region', { name: '过去运行', exact: true }).getByRole('button').click();
  await expect(results(page)).toContainText('合成观察规则 · v1');
  state.denyResult = true;
  await results(page).getByRole('button', { name: '数据不足 1', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toContainText('当前规则和结果已清空');
  await expect(results(page)).toHaveCount(0); await expect(panel(page).getByLabel('已保存规则')).toHaveCount(0);
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('a pending discard confirmation cannot switch rules while its draft is saving', async ({ page }) => {
  const state = await fixture(page); await open(page);
  await panel(page).getByLabel('已保存规则').selectOption(ruleId);
  await panel(page).getByLabel('规则名称', { exact: true }).fill('合成保存中的草稿');
  await panel(page).getByLabel('已保存规则').selectOption('');
  await expect(panel(page).getByRole('button', { name: '放弃草稿并载入', exact: true })).toBeVisible();
  state.holdSave = true;
  await panel(page).getByRole('button', { name: '保存规则', exact: true }).click();
  await expect.poll(() => !!state.releaseSave).toBe(true);
  await expect(panel(page).getByLabel('已保存规则')).toBeDisabled();
  await expect(panel(page).getByRole('button', { name: '放弃草稿并载入', exact: true })).toBeDisabled();
  await expect(panel(page).getByRole('button', { name: '保留草稿', exact: true })).toBeDisabled();
  state.holdSave = false; state.releaseSave!();
  await expect(panel(page)).toContainText('规则已保存为 v2');
  await expect(panel(page).getByRole('button', { name: '放弃草稿并载入', exact: true })).toHaveCount(0);
  await expect(panel(page).getByLabel('已保存规则')).toHaveValue(ruleId);
  await expect(panel(page).getByLabel('规则名称', { exact: true })).toHaveValue('合成保存中的草稿');
  expect(state.writes).toHaveLength(1); expect(state.errors).toEqual([]);
});

test('390px layout keeps conditions and explanation controls inside the panel', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 900 });
  const state = await fixture(page); await open(page);
  await panel(page).getByLabel('条件 1 数值', { exact: true }).fill('0');
  await panel(page).getByRole('button', { name: '预览草稿', exact: true }).click();
  await expect(results(page)).toBeVisible();
  const bounds = await panel(page).boundingBox(); expect(bounds).not.toBeNull();
  for (const locator of [panel(page).getByLabel('条件 1 指标'), panel(page).getByLabel('条件 1 数值'), panel(page).getByRole('button', { name: '保存规则', exact: true })]) {
    const control = await locator.boundingBox(); expect(control).not.toBeNull();
    expect(control!.x).toBeGreaterThanOrEqual(bounds!.x); expect(control!.x + control!.width).toBeLessThanOrEqual(bounds!.x + bounds!.width + 1);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391);
  await results(page).getByText('查看规则快照与来源引用（1）', { exact: true }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391);
  await panel(page).screenshot({ path: testInfo.outputPath('screening-mobile.png') });
  expect(state.errors).toEqual([]);
});
