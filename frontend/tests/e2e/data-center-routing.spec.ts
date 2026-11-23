import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { ConnectionView } from '../../src/features/data-center-live/api';
import type { CapabilityMatrix, CapabilityOption, RouteChange, Supplier } from '../../src/features/data-center-live/capabilityApi';

// Completely synthetic local UI tests. All API and external requests are
// intercepted; no personal connection, credential or supplier is contacted.
const isolated = (() => {
  try { const url = new URL(process.env.JL_E2E_BASE_URL ?? ''); return ['127.0.0.1', 'localhost'].includes(url.hostname) && url.port === '5178'; }
  catch { return false; }
})();
test.skip(!isolated, 'requires the dedicated frontend-only test server on http://127.0.0.1:5178');
const at = '2026-10-25T06:00:00Z';
const owner = '00000000-0000-4000-8000-000000000101';
const meta = { request_id: owner, app_mode: 'demo', generated_at: at, as_of_time: null, quality: { status: 'partial', reasons: ['合成界面测试'] } };

function option(provider_id: string, capability_id: string, name: string, billing: CapabilityOption['billing'], extra: Partial<CapabilityOption> = {}): CapabilityOption {
  return { provider_id, capability_id, name, billing, billing_label: { free: '免费', free_quota: '免费额度', points: '积分', subscription: '付费套餐', unknown: '费用待确认' }[billing],
    billing_note: '合成费用，仅验证展示。', reference_url: 'https://example.invalid/official', checked_at: at,
    entitlement: 'verified', implemented: true, state: 'ready', last_success_at: at,
    source_ids: ['source-' + provider_id + '-' + capability_id], selected: false, reason: null, scope: '合成沪深 A 股范围，不代表真实权益。', ...extra };
}
function fixture(): CapabilityMatrix {
  const definitions: [string, string, CapabilityOption[]][] = [
    ['stock_daily', '股票日K', [option('tickflow', 'stock_daily', '股票日K', 'free', { selected: true }), option('fuyao', 'stock_daily', '股票日K', 'free'), option('legacy_macro', 'stock_daily', '股票日K', 'free')]],
    ['stock_quote', '股票报价', [option('tickflow', 'stock_quote', '股票报价', 'subscription', { entitlement: 'locked', state: 'permission_missing', last_success_at: null, reason: '当前套餐未开通' }), option('fuyao', 'stock_quote', '股票报价', 'free_quota', { selected: true }), option('tdx', 'stock_quote', '股票报价', 'points')]],
    ['depth5', '五档盘口', [option('tdx', 'depth5', '五档盘口', 'points')]],
    ['full_minute', '全市场分钟', [option('tdx', 'full_minute', '全市场分钟', 'points')]],
    ['financial', '财务数据', [option('fuyao', 'financial', '财务数据', 'unknown', { entitlement: 'unknown', implemented: false, state: 'not_implemented', last_success_at: null, reason: '系统适配待完成' })]],
    ['adjustment', '除权事件', [option('fuyao', 'adjustment', '除权事件', 'free', { selected: true })]],
  ];
  const providers: Supplier[] = [['tickflow', 'TickFlow'], ['fuyao', '同花顺 API'], ['tdx', '通达信'], ['tushare', 'Tushare'], ['legacy_macro', '合成宏观来源']].map(([id, name]) => ({ id, name, template: id === 'tdx' ? 'tdx_data' : id, kind: 'market', endpoint: 'https://' + id + '.example.invalid', connection_id: 'connection-' + id, credential_required: true, credential_configured: true, enabled: id !== 'tdx', serving_count: id === 'tdx' ? 0 : 1, capabilities: id === 'tushare' ? Array.from({ length: 9 }, (_, i) => option(id, `reference_${i}`, `合成参考${i + 1}`, 'subscription', { selected: i === 8 })) : definitions.flatMap(([, , items]) => items.filter(item => item.provider_id === id)) }));
  return { revision: 4, policy: 'free_first', providers, capabilities: definitions.map(([id, name, options]) => ({ id, name, group: id === 'financial' ? '财务与参考' : '行情与图表', description: '合成能力，用于验证来源替代。', routable: true, current_provider_id: options.find(item => item.selected)?.provider_id ?? null, effective_provider_id: options.find(item => item.selected)?.provider_id ?? null, options })) };
}

async function install(page: Page, role = 'admin') {
  const state = { matrix: fixture(), rejectRoutes: false, models: [] as ConnectionView[], settings: { enabled: false, default_connection_id: null as string | null, revision: 1, features: [] },
    writes: [] as { path: string; body: any; csrf?: string; method: string }[], outside: [] as string[], errors: [] as string[], probes: [] as string[], minuteJobs: 0 };
  const origin = new URL(test.info().project.use.baseURL as string).origin;
  const connections = () => [...state.matrix.providers.map(provider => ({ id: provider.connection_id!, kind: 'market' as const, name: provider.name, template: provider.template,
    endpoint: provider.endpoint, protocol: null, model: null, enabled: provider.enabled, source_ids: provider.capabilities.flatMap(item => item.source_ids), revision: 1,
    options: { timeout_seconds: 60, max_output_tokens: 1200 }, credential_configured: true, credential_origin: 'vault' as const, last_test: null })), ...state.models];
  page.on('pageerror', error => state.errors.push(error.stack ?? error.message));
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), () => page.keyboard.press('Escape'));
  await page.route('**/*', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname; const method = request.method();
    if (url.origin !== origin) { state.outside.push(url.origin); return route.abort('blockedbyclient'); }
    if (!path.startsWith('/api/') && !path.startsWith('/health/')) return route.continue();
    const ok = (data: unknown, status = 200) => route.fulfill({ status, json: { meta, data } });
    const fail = (status: number, code: string, message: string) => route.fulfill({ status, json: { request_id: owner, error: { code, message, details: [] } } });
    if (method !== 'GET') state.writes.push({ path, method, body: request.postData() ? request.postDataJSON() : null, csrf: request.headers()['x-csrf-token'] });
    if (path === '/api/v1/session') return ok({ user: { id: owner, username: 'synthetic-routing', role }, csrf_token: 'synthetic-routing-csrf' });
    if (path === '/api/v1/chatgpt/status') return ok({ available: false, authenticated: true, local_only: true, reason: '合成 UI 测试', accounts: [] });
    if (path === '/api/v1/chatgpt/accounts') return ok({ items: [] });
    if (path === '/api/v1/data-center/capabilities') return ok(state.matrix);
    if (path === '/api/v1/data-center/full-minute') { expect(request.postDataJSON().allow_points).toBe(true); state.minuteJobs++; return ok({ job_id: 'minute-' + state.minuteJobs, state: 'queued', request_limit: 20, scope_count: 31 }); }
    if (path.startsWith('/api/v1/data-center/minute-jobs/')) { const complete = path.endsWith('minute-2'); return ok({ job_id: path.split('/').at(-1), state: 'succeeded', error_code: null, progress: { day: '2026-10-23', mode: 'full_market', scope_count: 31, chunk_offset: complete ? 20 : 0, chunk_requested: complete ? 11 : 20, chunk_completed: complete ? 11 : 20, failed_count: 0, completed_through: complete ? 31 : 20, remaining: complete ? 0 : 11, request_limit: 20, next_cursor: complete ? null : 'synthetic-minute-cursor', failed_instruments: [], is_complete: complete } }); }
    if (path === '/api/v1/data-center/routes') {
      if (state.rejectRoutes) return fail(409, 'revision_conflict', '配置已改变；原来源保留，请刷新后重试。');
      const body = request.postDataJSON(); expect(body.expected_revision).toBe(state.matrix.revision);
      for (const change of body.changes as RouteChange[]) {
        const capability = state.matrix.capabilities.find(item => item.id === change.capability_id)!;
        const chosen = capability.options.find(item => item.provider_id === change.provider_id)!;
        if (chosen.billing === 'points') expect(change.allow_points).toBe(true);
        capability.current_provider_id = change.provider_id; capability.effective_provider_id = change.provider_id;
        capability.options.forEach(item => { item.selected = item.provider_id === change.provider_id; });
      }
      state.matrix.revision++; return ok(state.matrix);
    }
    if (path.includes('/data-center/providers/') && path.endsWith('/connect')) return ok(state.matrix);
    if (path === '/api/v1/integrations' && method === 'GET') return ok({ items: connections(), templates: [{ id: 'custom_model', name: '自定义模型', kind: 'model', endpoint: '', credential_required: true }, { id: 'fuyao', name: '同花顺 API', kind: 'market', endpoint: 'https://fuyao.example.invalid', credential_required: true }], credential_store_ready: true });
    if (path === '/api/v1/integrations' && method === 'POST') {
      const { api_key: _secret, ...input } = request.postDataJSON();
      const connection = { ...input, id: 'model-synthetic', revision: 1, credential_configured: true, credential_origin: 'vault', last_test: null } as ConnectionView;
      state.models.push(connection); return ok(connection, 201);
    }
    if (path === '/api/v1/integrations/probe') {
      const input = request.postDataJSON(); state.probes.push(input.source_id ?? input.id);
      const model = state.models.find(item => item.id === input.id);
      if (model) { model.last_test = { status: 'success', revision: model.revision, at }; model.tested_revision = model.revision; return ok({ status: 'success', elapsed_ms: 30, message: '合成短文本生成通过', request_count: 1, revision: model.revision, test_proof: 'synthetic-proof' }); }
      return ok({ status: 'queued', elapsed_ms: null, message: '单项合成任务已排队', request_count: 0, source_id: input.source_id });
    }
    if (path.startsWith('/api/v1/integrations/') && method === 'PATCH') {
      const model = state.models.find(item => path.endsWith(item.id));
      if (!model) return ok(connections()[0]);
      const { api_key: _secret, expected_revision: _revision, ...input } = request.postDataJSON();
      Object.assign(model, input); model.revision++;
      if (model.last_test && input.enabled) { model.last_test.revision = model.revision; model.tested_revision = model.revision; }
      return ok(model);
    }
    if (path === '/api/v1/ai/settings') { if (method === 'PATCH') { const { expected_revision: _revision, ...input } = request.postDataJSON(); Object.assign(state.settings, input); state.settings.revision++; } return ok(state.settings); }
    if (path === '/api/v1/data-center/overview') return ok({ services_count: 3, running_tasks: 0, today_requests: 0, pending_issues: 0, services: [], tasks: [], issues: [], updated_at: at });
    if (path === '/api/v1/data-center/calls') return ok({ items: [], next_cursor: null });
    if (path === '/health/ready') return route.fulfill({ json: { status: 'ok' } });
    if (method === 'GET') return ok({ items: [], next_cursor: null });
    return fail(405, 'unexpected_write', '合成 UI 测试拦截了额外写入。');
  });
  return state;
}
const supplier = (page: Page, name: string) => page.locator('.dc-supplier-card').filter({ has: page.getByRole('heading', { name, exact: true }) });
async function open(page: Page, view = 'connections') { await page.goto('/data?view=' + view); await expect(page.locator('.dc-core-suppliers .dc-supplier-card')).toHaveCount(4); await expect(page.locator('.dc-live h1')).toBeVisible(); }

test('fees, permissions and replacements are readable; navigation and refresh make no writes', async ({ page }, info) => {
  const state = await install(page);
  const quote = state.matrix.capabilities.find(item => item.id === 'stock_quote')!;
  quote.effective_provider_id = null;
  quote.options.find(item => item.selected)!.state = 'empty';
  for (const [id, name, group] of [['legacy_reference', '原有行情参考接口', '行情与图表'], ['legacy_financial', '原有财务参考接口', '财务与参考']]) {
    const reference = option('tushare', id, name, 'subscription', { selected: true });
    state.matrix.providers.find(item => item.id === 'tushare')!.capabilities.push(reference);
    state.matrix.capabilities.push({ id, name, group, description: '原接口沿用已有配置。', routable: false, current_provider_id: 'tushare', effective_provider_id: 'tushare', options: [reference] });
  }
  await open(page); expect(state.errors).toEqual([]);
  const chartGroup = page.locator('.dc-route-section > .dc-route-group').filter({ has: page.locator('summary', { hasText: '行情与图表' }) });
  const optional = chartGroup.locator('.dc-route-optional');
  await expect(chartGroup).toHaveAttribute('open');
  await expect(chartGroup.locator(':scope > .dc-route-grid > .dc-route-card')).toHaveCount(3);
  await expect(chartGroup.getByRole('heading', { name: '股票报价', exact: true })).toBeVisible();
  await expect(chartGroup.locator('.dc-route-card').filter({ has: page.getByRole('heading', { name: '股票报价', exact: true }) })).toContainText('暂无满足要求的来源');
  await expect(optional).not.toHaveAttribute('open');
  await expect(optional.locator('summary')).toHaveText('更多接口与能力（3）');
  await expect(optional.getByRole('heading', { name: '五档盘口', exact: true })).toBeHidden();
  await optional.locator('summary').focus(); await page.keyboard.press('Enter');
  await expect(optional.getByRole('heading', { name: '原有行情参考接口', exact: true })).toBeVisible();
  await page.keyboard.press('Space'); await expect(optional).not.toHaveAttribute('open');
  const financeGroup = page.locator('.dc-route-section > .dc-route-group').filter({ has: page.locator('summary', { hasText: '财务与参考' }) });
  await expect(financeGroup).not.toHaveAttribute('open');
  await expect(financeGroup.locator('.dc-route-optional')).toHaveCount(0);
  await financeGroup.locator('summary').focus(); await page.keyboard.press('Enter');
  await expect(financeGroup.getByRole('heading', { name: '财务数据', exact: true })).toBeVisible();
  await expect(financeGroup.getByRole('heading', { name: '原有财务参考接口', exact: true })).toBeVisible();
  await page.keyboard.press('Enter'); await expect(financeGroup).not.toHaveAttribute('open');
  await expect(supplier(page, 'TickFlow').getByRole('button', { name: 'TickFlow 股票报价 付费套餐 需解锁', exact: true })).toContainText('🔒');
  await expect(supplier(page, 'TickFlow').getByRole('button', { name: 'TickFlow 股票日K 免费 当前使用', exact: true })).not.toContainText('🔒');
  await expect(supplier(page, '同花顺 API')).toContainText('免费额度');
  await expect(supplier(page, '同花顺 API')).toContainText('费用待确认');
  await expect(supplier(page, '同花顺 API')).toContainText('待接入');
  await expect(supplier(page, '通达信')).toContainText('积分');
  await expect(page.locator('.dc-other-suppliers')).not.toHaveAttribute('open');
  await expect(page.locator('.dc-other-suppliers > summary')).toHaveText('其他已登记来源（1）');
  await expect(supplier(page, '合成宏观来源')).toBeHidden();
  const tushare = supplier(page, 'Tushare');
  await expect(tushare.locator('.dc-capability-chip:visible')).toHaveCount(6);
  await expect(tushare.locator('.dc-capability-chip:visible').first()).toContainText('合成参考9');
  await tushare.locator('.dc-more-capabilities > summary').click();
  await expect(tushare.locator('.dc-capability-chip:visible')).toHaveCount(11);
  await tushare.locator('.dc-more-capabilities > summary').click();
  await supplier(page, 'TickFlow').getByRole('button', { name: '配置', exact: true }).click();
  await expect(page.locator('.dc-supplier-detail')).toContainText('当前套餐未开通');
  await expect(page.locator('.dc-supplier-detail')).toContainText('官方费用依据');
  await supplier(page, '同花顺 API').getByRole('button', { name: '配置', exact: true }).click();
  await expect(page.locator('.dc-supplier-detail').getByRole('link', { name: '除权事件查看已存数据', exact: true })).toHaveAttribute('href', '/data?view=datasets&family=fuyao_adj_factor');
  for (const width of [1672, 1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    if (width === 390) {
      await optional.locator('summary').focus(); await page.keyboard.press('Enter');
      await expect(optional.getByRole('heading', { name: '五档盘口', exact: true })).toBeVisible();
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.keyboard.press('Enter'); await expect(optional).not.toHaveAttribute('open');
    }
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`sources-${width}.png`), fullPage: true });
  }
  await page.getByRole('button', { name: 'AI 设置', exact: true }).click();
  await expect(page.getByRole('group', { name: 'AI 服务商' })).toBeVisible();
  await page.getByRole('button', { name: '运行监控', exact: true }).click();
  await expect(page.locator('.dc-call-panel')).toBeVisible();
  await page.getByRole('button', { name: '数据来源', exact: true }).click();
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await supplier(page, '同花顺 API').getByRole('button', { name: '股票日K', exact: false }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.goBack();
  await expect(page.locator('.dc-call-panel')).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(state.writes).toEqual([]); expect(state.outside).toEqual([]); expect(state.errors).toEqual([]);
});

test('partial apply changes only selected capabilities, with revision and explicit points consent', async ({ page }) => {
  const state = await install(page); await open(page); expect(state.errors).toEqual([]);
  await supplier(page, '同花顺 API').getByRole('button', { name: '套用', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('checkbox', { name: '股票日K使用同花顺 API', exact: true })).toBeChecked();
  await expect(dialog.getByRole('checkbox', { name: '财务数据使用同花顺 API', exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: '应用选中能力', exact: true }).click();
  expect(state.writes.at(-1)?.body).toEqual({ expected_revision: 4, changes: [{ capability_id: 'stock_daily', provider_id: 'fuyao' }] });
  expect(state.matrix.capabilities.find(item => item.id === 'stock_quote')?.current_provider_id).toBe('fuyao');
  await supplier(page, '通达信').getByRole('button', { name: '套用', exact: true }).click();
  for (const checkbox of await dialog.getByRole('checkbox').all()) await expect(checkbox).not.toBeChecked();
  await dialog.getByRole('checkbox', { name: '五档盘口使用通达信', exact: true }).check();
  await expect(dialog.getByRole('button', { name: '应用选中能力', exact: true })).toBeDisabled();
  const row = dialog.locator('.dc-routing-row').filter({ has: page.getByRole('heading', { name: '五档盘口', exact: false }) });
  await row.getByRole('checkbox', { name: '允许此能力消耗通达信积分', exact: true }).check();
  await dialog.getByRole('button', { name: '应用选中能力', exact: true }).click();
  expect(state.writes.at(-1)?.body).toEqual({ expected_revision: 5, changes: [{ capability_id: 'depth5', provider_id: 'tdx', allow_points: true }] });
  await supplier(page, '同花顺 API').getByRole('button', { name: '股票日K', exact: false }).click();
  await dialog.getByRole('checkbox', { name: '股票日K使用合成宏观来源', exact: true }).check();
  await dialog.getByRole('button', { name: '应用选中能力', exact: true }).click();
  await expect(page.locator('.dc-other-suppliers')).toHaveAttribute('open');
  await expect(supplier(page, '合成宏观来源')).toBeVisible();
  await expect(supplier(page, '合成宏观来源')).toHaveClass(/selected/);
  expect(state.writes.at(-1)?.body).toEqual({ expected_revision: 6, changes: [{ capability_id: 'stock_daily', provider_id: 'legacy_macro' }] });
  for (const write of state.writes) expect(write.csrf).toBe('synthetic-routing-csrf');
  expect(state.outside).toEqual([]); expect(state.errors).toEqual([]);
});

test('failed apply leaves previous routing and selected choices intact', async ({ page }) => {
  const state = await install(page); state.rejectRoutes = true; await open(page);
  await supplier(page, '同花顺 API').getByRole('button', { name: '股票日K', exact: false }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('checkbox', { name: '股票日K使用同花顺 API', exact: true }).check();
  await dialog.getByRole('button', { name: '应用选中能力', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('原来源保留');
  await expect(dialog.getByRole('checkbox', { name: '股票日K使用同花顺 API', exact: true })).toBeChecked();
  expect(state.matrix.capabilities[0].current_provider_id).toBe('tickflow');
  await page.keyboard.press('Escape'); await expect(dialog).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test('single point capability probe requires a fresh consent and never marks other capabilities successful', async ({ page }) => {
  const state = await install(page); await open(page); expect(state.errors).toEqual([]);
  await supplier(page, '通达信').getByRole('button', { name: '配置', exact: true }).click();
  const detail = page.locator('.dc-supplier-detail');
  await detail.getByLabel('通达信本次检测能力', { exact: true }).selectOption('source-tdx-depth5');
  await expect(detail.getByRole('button', { name: '检测单项能力', exact: true })).toBeDisabled();
  await detail.getByRole('checkbox', { name: '允许本次检测消耗积分', exact: true }).check();
  await detail.getByRole('button', { name: '检测单项能力', exact: true }).click();
  await expect(detail.getByRole('status')).toContainText('这一项能力已排队检测');
  expect(state.probes).toEqual(['source-tdx-depth5']);
  expect(state.writes[0]).toMatchObject({ path: '/api/v1/data-center/providers/tdx/connect', body: { allow_points: true } });
  await expect(detail.getByRole('checkbox', { name: '允许本次检测消耗积分', exact: true })).not.toBeChecked();
  expect(state.matrix.capabilities.find(item => item.id === 'full_minute')?.effective_provider_id).toBeNull();
  expect(state.errors).toEqual([]);
});

test('AI presets expose three basic inputs; save, generation proof and activation are separate actions', async ({ page }, info) => {
  const state = await install(page); await open(page, 'ai');
  const editor = page.locator('.dc-simple-model');
  await page.getByRole('group', { name: 'AI 服务商' }).getByRole('button', { name: 'DeepSeek', exact: false }).click();
  await expect(editor.getByLabel('接口地址', { exact: true })).toHaveValue('https://api.deepseek.com');
  await expect(editor.getByLabel('接口协议', { exact: true })).toBeHidden();
  await expect(editor.getByLabel('服务名称', { exact: true })).toBeHidden();
  await editor.getByLabel('模型名称', { exact: true }).fill('synthetic-research-model');
  const secret = 'synthetic-private-' + crypto.randomUUID();
  await editor.getByLabel('API Key', { exact: true }).fill(secret);
  await page.getByRole('button', { name: '数据来源', exact: true }).click();
  await page.getByRole('button', { name: 'AI 设置', exact: true }).click();
  await expect(editor.getByLabel('模型名称', { exact: true })).toHaveValue('synthetic-research-model');
  await page.getByRole('button', { name: '添加模型', exact: true }).click();
  await expect(editor.getByLabel('接口地址', { exact: true })).toHaveValue('https://api.deepseek.com');
  await expect(editor.getByLabel('模型名称', { exact: true })).toHaveValue('synthetic-research-model');
  await expect(editor.getByLabel('API Key', { exact: true })).toHaveValue('');
  await editor.getByLabel('API Key', { exact: true }).fill(secret);
  await editor.getByRole('button', { name: '保存接入', exact: true }).click();
  await expect(editor.getByLabel('API Key', { exact: true })).toHaveValue('');
  await expect(editor.getByRole('button', { name: '设为默认并启用', exact: true })).toBeDisabled();
  expect(state.models[0].enabled).toBe(false); expect(state.settings.enabled).toBe(false);
  await editor.getByRole('button', { name: '测试并保存', exact: true }).click();
  await expect(editor.getByRole('button', { name: '设为默认并启用', exact: true })).toBeEnabled();
  await editor.getByRole('button', { name: '设为默认并启用', exact: true }).click();
  await expect(editor.locator('.dc-feedback')).toContainText('已设为全局默认并启用');
  expect(state.models[0].enabled).toBe(true); expect(state.settings.enabled).toBe(true);
  expect(state.settings.default_connection_id).toBe(state.models[0].id);
  const privateState = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, history: history.state }));
  expect(privateState).not.toContain(secret); expect(await page.content()).not.toContain(secret);
  for (const width of [1440, 390]) { await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 }); await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); window.scrollTo(0, 0); }); await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true); await page.screenshot({ path: info.outputPath(`ai-${width}.png`), fullPage: true, animations: 'disabled' }); }
  expect(state.outside).toEqual([]); expect(state.errors).toEqual([]);
});

test('closed collection and unknown permissions cannot be selected as working alternatives', async ({ page }) => {
  const state = await install(page);
  const daily = state.matrix.capabilities[0].options.find(item => item.provider_id === 'fuyao')!;
  daily.state = 'disabled'; daily.reason = 'source_disabled';
  await open(page);
  const dailyChip = supplier(page, '同花顺 API').getByRole('button', { name: '股票日K', exact: false });
  const readyChip = supplier(page, 'TickFlow').getByRole('button', { name: '股票日K', exact: false });
  await expect(readyChip).toHaveClass(/serving/);
  await expect(readyChip.locator('.dc-capability-state')).toHaveCount(0);
  await expect(readyChip.locator('.dc-billing')).toHaveText('免费');
  await expect(dailyChip.locator('.dc-capability-state')).toHaveText('采集已关闭');
  await supplier(page, '同花顺 API').getByRole('button', { name: '股票日K', exact: false }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('checkbox', { name: '股票日K使用同花顺 API', exact: true })).toBeDisabled();
  await expect(dialog).toContainText('采集已关闭');
  await expect(dialog).toContainText('数据能力已关闭');
  await expect(dialog.getByRole('button', { name: '应用选中能力', exact: true })).toBeDisabled();
  await page.keyboard.press('Escape');
  daily.selected = true;
  for (const [failure, label, chipLabel = label] of [
    ['error', '检测失败'], ['failed', '检测失败'], ['empty', '未返回数据'],
    ['unavailable', '暂不可用'], ['credentials_missing', '待填写 Key'],
    ['points_not_enabled', '待允许积分'], ['time_unverified', '行情时点待核对', '时点待核'],
  ]) {
    daily.state = failure;
    await Promise.all([page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/data-center/capabilities'), page.getByRole('button', { name: '刷新', exact: true }).click()]);
    await expect(dailyChip.locator('.dc-capability-state')).toHaveText(chipLabel);
    await expect(dailyChip).toHaveClass(/serving/);
    await expect(dailyChip.locator('.dc-billing')).toHaveText('免费');
    await dailyChip.click();
    await expect(dialog.getByRole('checkbox', { name: '股票日K使用同花顺 API', exact: true })).toBeDisabled();
    await expect(dialog).toContainText(label);
    await page.keyboard.press('Escape');
  }
  for (const [stateValue, entitlement, label] of [
    ['not_configured', 'unknown', '待配置'], ['not_checked', 'verified', '尚未检测'],
    ['ready', 'unknown', '待核验'], ['unrecognized_state', 'verified', '待核验'],
  ] as const) {
    daily.state = stateValue; daily.entitlement = entitlement;
    await Promise.all([page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/data-center/capabilities'), page.getByRole('button', { name: '刷新', exact: true }).click()]);
    await expect(dailyChip.locator('.dc-capability-state')).toHaveText(label);
    await expect(dailyChip).toHaveClass(/serving/);
  }
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('read-only account cannot change routing or connections', async ({ page }) => {
  const state = await install(page, 'researcher'); await open(page);
  await expect(supplier(page, '通达信').getByRole('button', { name: '套用', exact: true })).toBeDisabled();
  await supplier(page, '同花顺 API').getByRole('button', { name: '配置', exact: true }).click();
  await expect(page.locator('.dc-supplier-detail').getByRole('button', { name: '保存配置', exact: true })).toBeDisabled();
  await supplier(page, '同花顺 API').getByRole('button', { name: '股票日K', exact: false }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('checkbox', { name: '股票日K使用同花顺 API', exact: true })).toBeDisabled();
  await expect(dialog.getByRole('button', { name: '应用选中能力', exact: true })).toBeDisabled();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'AI 设置', exact: true }).click();
  await expect(page.locator('.dc-simple-model').getByRole('button', { name: '测试并保存', exact: true })).toBeDisabled();
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('full-market minute work is bounded and each next chunk needs a new consent', async ({ page }) => {
  const state = await install(page);
  const provider = state.matrix.providers.find(item => item.id === 'tdx')!; provider.enabled = true;
  const capability = state.matrix.capabilities.find(item => item.id === 'full_minute')!;
  capability.current_provider_id = 'tdx'; capability.options[0].selected = true;
  await open(page);
  await supplier(page, '通达信').getByRole('button', { name: '通达信', exact: true }).focus();
  await page.keyboard.press('Enter');
  const section = page.locator('.dc-full-minute');
  await expect(section).toBeVisible();
  await section.getByLabel('分钟补齐范围', { exact: true }).selectOption('market');
  await section.getByLabel('分钟补齐交易日期', { exact: true }).fill('2026-10-23');
  await expect(section.getByRole('button', { name: '启动本段补齐', exact: true })).toBeDisabled();
  await section.getByRole('checkbox', { name: '允许本次分钟补齐消耗积分', exact: true }).check();
  await section.getByRole('button', { name: '启动本段补齐', exact: true }).click();
  await expect(section.locator('.dc-minute-progress')).toContainText('本段请求 20 只，返回 20 只');
  expect(state.minuteJobs).toBe(1);
  expect(state.writes.find(item => item.path === '/api/v1/data-center/full-minute')?.body).toEqual({ day: '2026-10-23', full_market: true, allow_points: true });
  await expect(section.getByRole('checkbox', { name: '允许本次分钟补齐消耗积分', exact: true })).not.toBeChecked();
  await expect(section.getByRole('button', { name: '继续下一段（最多 20 只）', exact: true })).toBeDisabled();
  await section.getByRole('checkbox', { name: '允许本次分钟补齐消耗积分', exact: true }).check();
  await section.getByRole('button', { name: '继续下一段（最多 20 只）', exact: true }).click();
  await expect(section.locator('.dc-minute-progress')).toContainText('本次范围已全部成功返回');
  expect(state.minuteJobs).toBe(2);
  expect(state.writes.at(-1)?.body).toEqual({ day: '2026-10-23', cursor: 'synthetic-minute-cursor', full_market: true, allow_points: true });
  await expect(section.getByRole('button', { name: '继续下一段（最多 20 只）', exact: true })).toHaveCount(0);
  expect(state.errors).toEqual([]); expect(state.outside).toEqual([]);
});
