import { expect, test } from '@playwright/test';

// Bootstrap behavior is covered separately; these tests exercise each existing page.
test.beforeEach(async ({ page }) => {
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), async () => {
    await page.keyboard.press('Escape');
  });
});
import type { Page } from '@playwright/test';
import type { ChatGPTAccount } from '../../src/features/chatgpt/model';

// Completely mocked local frontend only. Never log in, read saved OAuth
// credentials or issue an API write against the personal :8088 instance.
const isolated = (() => {
  try { const url = new URL(process.env.JL_E2E_BASE_URL ?? ''); return ['127.0.0.1', 'localhost'].includes(url.hostname) && url.port === '5178'; }
  catch { return false; }
})();
test.skip(!isolated, 'requires a dedicated frontend-only preview at http://127.0.0.1:5178');

const ownerA = '00000000-0000-4000-8000-000000000101';
const ownerB = '00000000-0000-4000-8000-000000000102';
const idA = '00000000-0000-4000-8000-000000000201';
const idB = '00000000-0000-4000-8000-000000000202';
const globalID = '00000000-0000-4000-8000-000000000301';
const csrf = 'synthetic-chatgpt-ui-csrf';
const at = '2026-10-25T14:00:00+08:00';
const meta = { request_id: idA, app_mode: 'real', generated_at: at, as_of_time: null,
  quality: { status: 'partial', reasons: ['合成 UI 测试，没有真实登录或模型请求'] } };

function account(id = idA, label = '合成 ChatGPT 甲'): ChatGPTAccount {
  return { id, connection_id: id === idA ? '00000000-0000-4000-8000-000000000401' : '00000000-0000-4000-8000-000000000402',
    email: id === idA ? 'synthetic-a@example.invalid' : 'synthetic-b@example.invalid', label, state: 'connected',
    use_for_ai: false, sharing_granted: true, model: 'synthetic-model-a', enabled: true, revision: 1, tested: false,
    last_test: null, welcome_pending: false, created_at: at, updated_at: at, reauthorization_required: false,
    remote_revocation_unconfirmed: false };
}

async function install(page: Page, options: { authenticated?: boolean; available?: boolean; role?: string; accounts?: ChatGPTAccount[] } = {}) {
  const origin = new URL(test.info().project.use.baseURL as string).origin;
  const state = { authenticated: options.authenticated ?? true, available: options.available ?? true,
    role: options.role ?? 'researcher', owner: ownerA, accounts: structuredClone(options.accounts ?? [account()]),
    statusError: false, denyWrite: false, probeMode: 'success', globalEnabled: false,
    issues: [] as { id: string; title: string; message: string; connection_id: string }[],
    calls: [] as Record<string, unknown>[],
    writes: [] as { method: string; path: string; body: any; csrf: string | undefined; key: string | undefined }[],
    reads: [] as string[], outside: [] as string[], pageErrors: [] as string[] };
  page.on('pageerror', error => state.pageErrors.push(error.message));
  await page.route('**/*', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname; const method = request.method();
    if (url.origin !== origin) { state.outside.push(url.origin); return route.abort('blockedbyclient'); }
    if (!path.startsWith('/api/') && !path.startsWith('/health/')) return route.continue();
    const ok = (data: unknown) => route.fulfill({ json: { meta, data } });
    const fail = (status: number, code: string, message: string) => route.fulfill({ status, json: { request_id: idA, error: { code, message, details: [] } } });
    if (method === 'GET') state.reads.push(path);
    else state.writes.push({ method, path, body: request.postData() ? request.postDataJSON() : null,
      csrf: request.headers()['x-csrf-token'], key: request.headers()['idempotency-key'] });
    if (path === '/api/v1/session') {
      if (method === 'DELETE') { state.authenticated = false; return route.fulfill({ status: 204 }); }
      if (method === 'POST') state.authenticated = true;
      return state.authenticated ? ok({ user: { id: state.owner, username: 'synthetic-user', role: state.role }, csrf_token: csrf })
        : fail(401, 'unauthorized', '请先登录。');
    }
    if (path === '/api/v1/chatgpt/status') {
      if (state.statusError) return fail(503, 'chatgpt_unavailable', '合成服务读取失败');
      return ok({ available: state.available, local_only: true,
        reason: state.available ? null : '当前仅支持运行天衍的本机浏览器，远程访问暂不可用。',
        authenticated: state.authenticated, accounts: state.authenticated ? state.accounts : [] });
    }
    if (path === '/api/v1/chatgpt/login') {
      if (state.denyWrite) return fail(403, 'chatgpt_forbidden', '当前账号没有此项授权。');
      return ok({ authorization_url: 'https://auth.openai.com/api/accounts/authorize?state=synthetic-ui-state&code_challenge=synthetic-ui-challenge' });
    }
    if (!state.authenticated) return fail(401, 'unauthorized', '请先登录。');
    if (path === '/api/v1/chatgpt/accounts' && method === 'GET') return ok({ items: state.accounts });
    if (path.startsWith('/api/v1/chatgpt/accounts/')) {
      const parts = path.split('/'); const current = state.accounts.find(item => item.id === parts[5]);
      if (!current) return fail(404, 'chatgpt_not_found', '本人账号不存在。');
      if (state.denyWrite && method !== 'GET') return fail(403, 'chatgpt_forbidden', '当前账号没有此项授权。');
      if (parts[6] === 'welcome' && method === 'POST') { current.welcome_pending = false; return ok(current); }
      if (parts[6] === 'models' && method === 'POST') return ok({ status: 'success', elapsed_ms: 12, message: '合成模型列表', request_count: 1,
        models: [{ id: 'synthetic-model-a' }, { id: 'synthetic-model-b', name: '合成模型乙' }] });
      if (parts[6] === 'probe' && method === 'POST') {
        current.tested = state.probeMode === 'success';
        current.last_test = { at, elapsed_ms: 37, revision: current.revision, status: state.probeMode === 'success' ? 'success' : 'error',
          error_code: state.probeMode === 'quota' ? 'chatgpt_usage_limit' : state.probeMode === 'unknown' ? 'outcome_unknown' : null };
        return ok({ status: state.probeMode === 'success' ? 'success' : state.probeMode === 'unknown' ? 'unknown' : 'error', elapsed_ms: 37,
          error_code: current.last_test.error_code, request_count: 1, message: state.probeMode === 'success' ? '合成短文本测试通过'
            : state.probeMode === 'unknown' ? '合成请求结果待确认。' : '合成 ChatGPT 计划额度已达上限。' });
      }
      if (method === 'PATCH') {
        const body = request.postDataJSON();
        if (body.expected_revision !== current.revision) return fail(409, 'revision_conflict', '配置已改变，请刷新后重试。');
        if (body.model !== undefined) { current.model = body.model; current.tested = false; }
        if (body.use_for_ai === true && !current.tested) return fail(409, 'test_required', '请先通过当前模型测试。');
        if (body.use_for_ai !== undefined) { for (const item of state.accounts) item.use_for_ai = item.id === current.id && body.use_for_ai; current.enabled = body.use_for_ai; }
        current.revision++; return ok(current);
      }
      if (method === 'DELETE') {
        current.state = 'disconnected'; current.enabled = false; current.sharing_granted = false; current.tested = false;
        current.remote_revocation_unconfirmed = true; current.revision++;
        return ok({ account: current, remote_revocation_unconfirmed: true });
      }
    }
    const global = { id: globalID, owner_id: null, name: '合成全局 API', kind: 'model', template: 'custom', endpoint: 'https://synthetic.invalid/v1',
      protocol: 'openai_responses', model: 'synthetic-global', enabled: true, source_ids: [], revision: 1, credential_configured: true,
      credential_origin: 'vault', last_test: { status: 'success', at, revision: 1 }, tested_revision: 1,
      options: { timeout_seconds: 60, max_output_tokens: 1200 } };
    const personal = state.accounts.map(item => ({ ...global, id: item.connection_id, name: item.label, owner_id: state.owner,
      enabled: item.enabled, model: item.model, revision: item.revision, last_test: item.last_test, tested_revision: item.tested ? item.revision : null }));
    if (path === '/api/v1/integrations') return ok({ items: [global, ...personal], templates: [], credential_store_ready: true });
    if (path === '/api/v1/data-center/capabilities') return ok({ revision: 1, policy: 'free_first', providers: [], capabilities: [] });
    if (path === '/api/v1/ai/settings') {
      const chosen = state.accounts.find(item => item.use_for_ai);
      return ok({ enabled: state.globalEnabled, default_connection_id: globalID, revision: 1,
        personal_connection_id: chosen?.connection_id ?? null, personal_model_ready: chosen?.tested ?? false,
        features: [['theme_summary', '题材归纳', '工作台', true], ['news_summary', '新闻摘要', '资讯中心', true],
          ['emotion_analysis', '情绪研判', '每日复盘', false], ['direction_analysis', '方向与核心分析', '每日复盘', false],
          ['plan_generation', '明日预案生成', '每日复盘', false], ['period_digest', '日报 / 周报 / 月报', '资讯中心', false]]
          .map(([id, name, module, implemented]) => ({ id, name, module, implemented, enabled: !!implemented, connection_id: null,
            fallback_connection_id: null, effective_connection_id: chosen?.connection_id ?? globalID })) });
    }
    if (path === '/api/v1/data-center/overview') return ok({ services_count: 1, running_tasks: 0, today_requests: 0, pending_issues: state.issues.length,
      services: [], tasks: [], issues: state.issues, updated_at: at });
    if (path === '/api/v1/data-center/calls') return ok({ items: state.calls, next_cursor: null });
    if (path === '/api/v1/market-quotes') return ok({ selection: 'indices', items: [] });
    if (path === '/api/v1/market-breadth-history') return ok({ source_family: 'ashare_breadth', latest: null });
    if (path === '/health/ready') return route.fulfill({ json: { status: 'ok' } });
    if (method === 'GET') return ok({ items: [], next_cursor: null });
    return fail(405, 'unexpected_write', '合成测试拦截了预期外写入。');
  });
  return state;
}

const card = (page: Page) => page.locator('.cg-card');
async function open(page: Page) {
  await page.goto('/data?view=ai');
  await expect(card(page).getByRole('heading', { name: '我的 ChatGPT', exact: true })).toBeVisible();
  await expect(card(page).getByRole('button', { name: '刷新本人状态', exact: true })).toBeEnabled();
  await page.waitForLoadState('networkidle');
}
async function assertNoPrivateBrowserState(page: Page) {
  const privateState = await page.evaluate(() => JSON.stringify({ local: Object.fromEntries(Object.entries(localStorage)),
    session: Object.fromEntries(Object.entries(sessionStorage)), navigation: history.state }));
  expect(privateState).not.toMatch(/synthetic-model|synthetic-a@example|synthetic-b@example|authorization_url|access_token|refresh_token|id_token|code_challenge/);
}

test('password and official login remain available, while first load and reload are read only', async ({ page }) => {
  const state = await install(page, { authenticated: false });
  await page.goto('/data?view=ai');
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  await expect(page.getByLabel('密码', { exact: true })).toHaveAttribute('type', 'password');
  await expect(page.getByRole('button', { name: 'Continue with ChatGPT', exact: true })).toBeEnabled();
  await expect(page.locator('.cg-login')).toContainText('符合资格的 Plus / Pro');
  await expect(page.locator('.cg-login')).toContainText('已有天衍账号请先使用原账号登录');
  await page.reload(); await page.waitForLoadState('networkidle');
  expect(state.writes).toEqual([]); expect(state.outside).toEqual([]);
  await page.getByLabel('账号', { exact: true }).fill('synthetic-user');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-ui-password');
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(card(page)).toBeVisible();
  expect(state.writes).toHaveLength(1); expect(state.writes[0].path).toBe('/api/v1/session');
  await assertNoPrivateBrowserState(page); expect(state.pageErrors).toEqual([]);
});

test('remote or unavailable access shows the real reason and never offers an active login', async ({ page }) => {
  const state = await install(page, { authenticated: false, available: false });
  await page.goto('/data');
  await expect(page.getByRole('button', { name: 'Continue with ChatGPT', exact: true })).toBeDisabled();
  await expect(page.locator('.cg-login')).toContainText('远程访问暂不可用');
  state.authenticated = true; await page.goto('/data?view=ai');
  await expect(card(page).getByRole('button', { name: 'Continue with ChatGPT', exact: true })).toBeDisabled();
  await expect(card(page).getByRole('button', { name: '获取可用模型', exact: true })).toBeDisabled();
  expect(state.writes).toEqual([]); expect(state.outside).toEqual([]);
});

test('status failures recover without blocking original login or reflecting callback query contents', async ({ page }) => {
  const state = await install(page, { authenticated: false }); state.statusError = true;
  await page.goto('/data?chatgpt=untrusted-upstream-message');
  await expect(page.locator('.cg-login-error')).toContainText('暂时无法确认');
  await expect(page.locator('.cg-login')).not.toContainText('untrusted-upstream-message');
  await expect(page.getByRole('button', { name: '进入工作台' })).toBeEnabled();
  state.statusError = false;
  await page.getByRole('button', { name: '重试接入检查' }).click();
  await expect(page.getByRole('button', { name: 'Continue with ChatGPT', exact: true })).toBeEnabled();
  expect(state.writes).toEqual([]);
});

test('Continue with ChatGPT starts one official redirect; connect preserves the current workspace', async ({ page }) => {
  const state = await install(page, { accounts: [] }); await open(page);
  await page.route('https://auth.openai.com/**', route => route.fulfill({ contentType: 'text/html', body: '<p>Synthetic official redirect only</p>' }));
  await card(page).getByRole('button', { name: 'Continue with ChatGPT', exact: true }).click();
  await expect(page).toHaveURL(/^https:\/\/auth\.openai\.com\/api\/accounts\/authorize/);
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0]).toMatchObject({ method: 'POST', path: '/api/v1/chatgpt/login', body: { mode: 'connect' }, csrf });
  expect(state.writes[0].body).not.toHaveProperty('password');
  expect(state.writes[0].body).not.toHaveProperty('access_token');
});

test('anonymous official login requests a new isolated identity and reauthorization identifies the selected account', async ({ page }) => {
  const state = await install(page, { authenticated: false });
  await page.route('https://auth.openai.com/**', route => route.fulfill({ contentType: 'text/html', body: '<p>Synthetic official redirect only</p>' }));
  await page.goto('/data');
  await page.getByRole('button', { name: 'Continue with ChatGPT', exact: true }).click();
  await expect(page).toHaveURL(/^https:\/\/auth\.openai\.com\//);
  expect(state.writes).toHaveLength(1); expect(state.writes[0].body).toEqual({ mode: 'login' });
  expect(state.writes[0].csrf).toBeUndefined();
  state.authenticated = true; await page.goto(new URL('/data?view=ai', test.info().project.use.baseURL as string).href);
  await expect(card(page).getByRole('button', { name: '重新登录此账号', exact: true })).toBeEnabled();
  await card(page).getByRole('button', { name: '重新登录此账号', exact: true }).click();
  await expect(page).toHaveURL(/^https:\/\/auth\.openai\.com\//);
  expect(state.writes).toHaveLength(2);
  expect(state.writes[1]).toMatchObject({ body: { mode: 'connect', account_id: idA }, csrf });
});

test('returning Continue reuses the server client while only explicit other-account login requests new registration', async ({ page }, info) => {
  const state = await install(page, { authenticated: false });
  const localURL = new URL('/data', test.info().project.use.baseURL as string).href;
  await page.route('https://auth.openai.com/**', route => route.fulfill({ contentType: 'text/html', body: '<p>Synthetic official redirect only</p>' }));
  await page.goto(localURL);
  await expect(page.getByRole('button', { name: 'Continue with ChatGPT', exact: true })).toBeEnabled();
  expect(state.writes).toEqual([]);
  await page.getByRole('button', { name: 'Continue with ChatGPT', exact: true }).click();
  await expect(page).toHaveURL(/^https:\/\/auth\.openai\.com\//);
  expect(state.writes).toHaveLength(1); expect(state.writes[0].body).toEqual({ mode: 'login' });
  await page.goto(localURL);
  await expect(page.getByRole('button', { name: '使用其他 ChatGPT 账号', exact: true })).toBeEnabled();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('chatgpt-returning-login-390.png'), fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '使用其他 ChatGPT 账号', exact: true }).click();
  await expect(page).toHaveURL(/^https:\/\/auth\.openai\.com\//);
  expect(state.writes).toHaveLength(2); expect(state.writes[1].body).toEqual({ mode: 'login', new_account: true });
  expect(state.writes[1].csrf).toBeUndefined(); expect(state.pageErrors).toEqual([]);
});

test('identity-only connection is separate from plan permission and generated-model validation', async ({ page }) => {
  const identity = { ...account(), state: 'identity_only' as const, sharing_granted: false };
  const state = await install(page, { accounts: [identity] }); await open(page);
  await expect(card(page)).toContainText('仅登录身份');
  await expect(card(page)).toContainText('身份登录成功不等于计划授权成功');
  await expect(card(page).getByRole('button', { name: '获取可用模型' })).toBeDisabled();
  await expect(card(page).getByRole('switch', { name: '用于我的 AI 功能', exact: true })).toBeDisabled();
  await expect(card(page).getByRole('button', { name: '重新登录此账号' })).toBeEnabled();
  expect(state.writes).toEqual([]);
});

test('ordinary users can manually configure their plan while global administrator controls stay disabled', async ({ page }) => {
  const state = await install(page); await open(page);
  await expect(page.getByRole('switch', { name: '全局 AI 开关', exact: true })).toBeDisabled();
  await expect(page.getByLabel('全局默认模型', { exact: true })).toBeDisabled();
  await expect(page.getByLabel('题材归纳使用模型', { exact: true })).toBeDisabled();
  await expect(card(page).getByRole('switch', { name: '用于我的 AI 功能', exact: true })).toBeDisabled();
  await card(page).getByRole('button', { name: '获取可用模型', exact: true }).click();
  await expect(card(page)).toContainText('获取列表不代表生成成功');
  await expect(card(page).getByLabel('已获取的 ChatGPT 模型')).toBeVisible();
  expect(state.accounts[0].tested).toBe(false);
  await card(page).getByLabel('已获取的 ChatGPT 模型').selectOption('synthetic-model-b');
  await expect(card(page).getByRole('button', { name: '测试当前模型', exact: true })).toBeDisabled();
  await card(page).getByRole('button', { name: '保存模型', exact: true }).click();
  await expect(card(page)).toContainText('旧测试结果不再验证新模型');
  await card(page).getByRole('button', { name: '测试当前模型', exact: true }).click();
  await expect(card(page)).toContainText('短文本生成测试通过');
  await card(page).getByRole('switch', { name: '用于我的 AI 功能', exact: true }).click();
  await expect(card(page)).toContainText('Using ChatGPT plan');
  await expect(page.locator('.dc-bindings')).toContainText('synthetic-model-b（本人计划）');
  expect(state.writes.map(item => item.path)).toEqual([`/api/v1/chatgpt/accounts/${idA}/models`, `/api/v1/chatgpt/accounts/${idA}`,
    `/api/v1/chatgpt/accounts/${idA}/probe`, `/api/v1/chatgpt/accounts/${idA}`]);
  for (const write of state.writes) { expect(write.csrf).toBe(csrf); expect(write.key).toBeTruthy(); }
  await page.reload(); await page.waitForLoadState('networkidle'); expect(state.writes).toHaveLength(4);
  await assertNoPrivateBrowserState(page); expect(state.outside).toEqual([]); expect(state.pageErrors).toEqual([]);
});

test('personal plan is excluded from global default, binding and fallback options even for administrators', async ({ page }) => {
  const state = await install(page, { role: 'admin', accounts: [{ ...account(), tested: true, use_for_ai: true }] }); await open(page);
  for (const name of ['全局默认模型', '题材归纳使用模型', '新闻摘要使用模型', '题材归纳备用服务', '新闻摘要备用服务']) {
    await expect(page.getByLabel(name, { exact: true }).locator('option')).not.toContainText(['合成 ChatGPT 甲']);
  }
  await expect(page.locator('.dc-services-grid:visible')).not.toContainText('合成 ChatGPT 甲');
  await expect(page.locator('.dc-bindings')).toContainText('本人计划');
  expect(state.writes).toEqual([]);
});

test('a retained plan requires a current test and explicit one-time restore before AI is enabled', async ({ page }, info) => {
  const state = await install(page, { accounts: [{ ...account(), use_for_ai: true, enabled: false, tested: false }] });
  await open(page);
  const restore = card(page).getByRole('button', { name: '恢复用于我的 AI', exact: true });
  const selection = card(page).getByRole('switch', { name: '用于我的 AI 功能', exact: true });
  await expect(card(page)).toContainText('计划路径已保留，当前模型未启用');
  await expect(card(page)).toContainText('ChatGPT 计划路径待恢复');
  await expect(selection).toHaveAttribute('aria-checked', 'true');
  await expect(restore).toBeDisabled(); expect(state.writes).toEqual([]);
  await card(page).getByRole('button', { name: '测试当前模型', exact: true }).click();
  await expect(card(page)).toContainText('短文本生成测试通过');
  await expect(restore).toBeEnabled();
  expect(state.accounts[0].tested).toBe(true); expect(state.accounts[0].enabled).toBe(false);
  expect(state.writes).toHaveLength(1); expect(state.writes[0].path).toBe(`/api/v1/chatgpt/accounts/${idA}/probe`);
  await card(page).getByLabel('我的 ChatGPT 模型', { exact: true }).fill('unsaved-model-not-tested');
  await expect(restore).toBeDisabled();
  await card(page).getByLabel('我的 ChatGPT 模型', { exact: true }).fill('synthetic-model-a');
  await expect(restore).toBeEnabled();
  await page.setViewportSize({ width: 390, height: 844 });
  await card(page).scrollIntoViewIfNeeded();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('chatgpt-retained-plan-restore-390.png'), fullPage: true, animations: 'disabled' });
  await restore.click();
  await expect(restore).toHaveCount(0);
  await expect(selection).toHaveAttribute('aria-checked', 'true');
  await expect(card(page)).toContainText('Using ChatGPT plan');
  expect(state.accounts[0].enabled).toBe(true); expect(state.writes).toHaveLength(2);
  expect(state.writes[1]).toMatchObject({ method: 'PATCH', path: `/api/v1/chatgpt/accounts/${idA}`, csrf,
    body: { expected_revision: 1, use_for_ai: true } });
  expect(state.writes[1].body).toEqual({ expected_revision: 1, use_for_ai: true });
  await card(page).getByRole('button', { name: '刷新本人状态', exact: true }).click();
  await page.reload(); await page.waitForLoadState('networkidle');
  await expect(restore).toHaveCount(0); expect(state.writes).toHaveLength(2);
  expect(state.globalEnabled).toBe(false); expect(state.outside).toEqual([]); expect(state.pageErrors).toEqual([]);
});

test('model and account switches clear drafts and lists without automatically switching the billing path', async ({ page }) => {
  const second = { ...account(idB, '合成 ChatGPT 乙'), model: 'second-account-only-model' };
  const state = await install(page, { accounts: [{ ...account(), tested: true, use_for_ai: true }, second] }); await open(page);
  await card(page).getByRole('button', { name: '获取可用模型' }).click();
  await expect(card(page).getByLabel('已获取的 ChatGPT 模型')).toBeVisible();
  await card(page).getByLabel('我的 ChatGPT 模型', { exact: true }).fill('unsaved-first-account-model');
  await card(page).getByLabel('我的 ChatGPT 账号', { exact: true }).selectOption(idB);
  await expect(card(page).getByLabel('我的 ChatGPT 模型', { exact: true })).toHaveValue('second-account-only-model');
  await expect(card(page).getByLabel('已获取的 ChatGPT 模型')).toHaveCount(0);
  await expect(card(page)).toContainText('本人当前 AI 路径：合成 ChatGPT 甲');
  expect(state.writes).toHaveLength(1); expect(state.accounts[0].use_for_ai).toBe(true); expect(state.accounts[1].use_for_ai).toBe(false);
  await assertNoPrivateBrowserState(page);
});

test('usage limit offers Manage usage as primary action and does not repeat or fall back', async ({ page }) => {
  const state = await install(page); state.probeMode = 'quota'; await open(page);
  await card(page).getByRole('button', { name: '测试当前模型', exact: true }).click();
  const alert = card(page).getByRole('alert');
  await expect(alert).toContainText('ChatGPT · Usage limit reached');
  await expect(alert.getByRole('link', { name: 'Manage usage', exact: true })).toHaveAttribute('href', 'https://chatgpt.com/settings/usage');
  await expect(alert.getByRole('link', { name: 'Manage usage', exact: true })).toHaveClass(/cg-usage-primary/);
  await card(page).getByRole('button', { name: '刷新本人状态', exact: true }).click();
  await page.reload(); await page.waitForLoadState('networkidle');
  expect(state.writes).toHaveLength(1); expect(state.writes[0].path).toMatch(/\/probe$/); expect(state.outside).toEqual([]);
});

test('uncertain model result explains no automatic resend and requires explicit recovery', async ({ page }) => {
  const state = await install(page); state.probeMode = 'unknown'; await open(page);
  await card(page).getByRole('button', { name: '测试当前模型', exact: true }).click();
  await expect(card(page).getByRole('alert')).toContainText('系统不会自动重新发送');
  await expect(card(page).getByRole('switch', { name: '用于我的 AI 功能', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '运行监控', exact: true }).click();
  await page.getByRole('button', { name: '刷新状态', exact: true }).click();
  await page.waitForLoadState('networkidle'); expect(state.writes).toHaveLength(1);
});

test('first authorization welcome is acknowledged once on the server and survives reload', async ({ page }) => {
  const state = await install(page, { accounts: [{ ...account(), welcome_pending: true }] }); await open(page);
  const welcome = page.getByRole('dialog', { name: "You're using your ChatGPT plan", exact: true });
  await expect(welcome).toBeVisible(); expect(state.writes).toEqual([]);
  await page.keyboard.press('Escape'); await expect(welcome).toBeVisible();
  await welcome.getByRole('button', { name: 'Got it', exact: true }).click();
  await expect(welcome).toHaveCount(0);
  await page.reload(); await page.waitForLoadState('networkidle'); await expect(welcome).toHaveCount(0);
  expect(state.writes).toHaveLength(1); expect(state.writes[0]).toMatchObject({ path: `/api/v1/chatgpt/accounts/${idA}/welcome`, csrf });
  await assertNoPrivateBrowserState(page);
});

test('disconnect clears local model access and states remote revocation uncertainty without losing the selected path', async ({ page }) => {
  const state = await install(page, { accounts: [{ ...account(), tested: true, use_for_ai: true }] }); await open(page);
  await card(page).getByRole('button', { name: '断开此账号', exact: true }).click();
  expect(state.writes).toEqual([]);
  await card(page).getByRole('button', { name: '确认断开', exact: true }).click();
  await expect(card(page)).toContainText('远端撤销未确认');
  await expect(card(page)).toContainText('个人路径的选择已保留');
  await expect(card(page).getByRole('button', { name: '测试当前模型', exact: true })).toBeDisabled();
  await expect(card(page).getByRole('switch', { name: '用于我的 AI 功能', exact: true })).toBeEnabled();
  expect(state.writes).toHaveLength(1); expect(state.writes[0].method).toBe('DELETE');
  await card(page).getByRole('switch', { name: '用于我的 AI 功能', exact: true }).click();
  await expect(card(page).getByRole('switch', { name: '用于我的 AI 功能', exact: true })).toHaveAttribute('aria-checked', 'false');
  expect(state.writes).toHaveLength(2); expect(state.writes[1].body).toMatchObject({ use_for_ai: false });
});

test('forbidden personal operations are shown clearly and logout removes prior account selections', async ({ page }) => {
  const state = await install(page); await open(page); state.denyWrite = true;
  await card(page).getByRole('button', { name: '测试当前模型', exact: true }).click();
  await expect(card(page).getByRole('alert')).toContainText('当前账号没有此项授权');
  await page.getByRole('button', { name: '退出账号', exact: true }).click();
  await expect(page.getByRole('heading', { name: '登录工作空间', exact: true })).toBeVisible();
  await expect(page.locator('body')).not.toContainText('synthetic-a@example.invalid');
  state.owner = ownerB; state.accounts = [account(idB, '另一个工作空间')]; state.denyWrite = false;
  await page.getByLabel('账号', { exact: true }).fill('other-owner');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-ui-password');
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(card(page)).toContainText('另一个工作空间');
  await expect(card(page)).not.toContainText('synthetic-a@example.invalid');
  await expect(card(page).getByLabel('我的 ChatGPT 模型', { exact: true })).toHaveValue('synthetic-model-a');
  await assertNoPrivateBrowserState(page); expect(state.pageErrors).toEqual([]);
});

test('session expiry removes the personal card before another workspace can be read', async ({ page }) => {
  const state = await install(page); await open(page); state.authenticated = false;
  await card(page).getByRole('button', { name: '刷新本人状态', exact: true }).click();
  await expect(page.getByRole('heading', { name: '登录工作空间', exact: true })).toBeVisible();
  await expect(card(page)).toHaveCount(0); await expect(page.locator('body')).not.toContainText('synthetic-a@example.invalid');
  expect(state.writes).toEqual([]); await assertNoPrivateBrowserState(page);
});

test('a personal-plan monitoring issue returns to ChatGPT controls instead of opening the API credential editor', async ({ page }) => {
  const state = await install(page);
  state.issues = [{ id: idA, title: '合成 ChatGPT 授权待恢复', message: 'chatgpt_reauthorization_required', connection_id: state.accounts[0].connection_id }];
  const fields = ['access_token', 'refresh_token', 'id_token', 'id_token_hint', 'code_verifier', 'authorization_code',
    'accessToken', 'refreshToken', 'idToken', 'pkce_verifier', 'oauth_code', 'token'];
  const sentinels = fields.map((_, index) => 'synthetic-sensitive-field-' + index);
  state.calls = [{ id: idB, kind: 'ai', task: '合成 ChatGPT 模型测试', service_name: '合成 ChatGPT 甲', connection_id: state.accounts[0].connection_id,
    job_id: null, status: 'success', started_at: at, completed_at: at, elapsed_ms: 37, request_count: 1, error_code: null, retry_at: null,
    details: { nested: [{ value: Object.fromEntries(fields.map((field, index) => [field, sentinels[index]])) }], input_tokens: 123, output_tokens: 17 } }];
  await open(page);
  await page.getByRole('button', { name: '运行监控', exact: true }).click();
  await page.locator('.dc-call-panel').getByRole('button', { name: '查看', exact: true }).click();
  const details = page.getByRole('dialog', { name: '调用详情', exact: true });
  for (const sentinel of sentinels) await expect(details).not.toContainText(sentinel);
  await expect(details.locator('pre')).toContainText('已隐藏');
  await expect(details.locator('pre')).toContainText('"input_tokens": 123');
  await expect(details.locator('pre')).toContainText('"output_tokens": 17');
  await details.getByRole('button', { name: '关闭详情', exact: true }).click();
  await page.getByRole('button', { name: '接入配置', exact: true }).click();
  await expect(card(page)).toBeVisible();
  await expect(page.locator('.dc-feedback')).toContainText('请在“我的 ChatGPT”中检查授权与模型');
  await expect(page.locator('.dc-editor').getByLabel('服务名称', { exact: true })).toHaveValue('');
  await expect(page.locator('.dc-editor').getByLabel('API Key', { exact: true })).toHaveValue('');
  expect(state.writes).toEqual([]); expect(state.pageErrors).toEqual([]);
});

test('personal plan controls, login and welcome remain usable at 1672, 1440 and narrow widths', async ({ page }, info) => {
  const state = await install(page, { accounts: [{ ...account(), use_for_ai: true, tested: true }] }); await open(page);
  for (const width of [1672, 1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 941 });
    await card(page).scrollIntoViewIfNeeded();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); window.scrollTo(0, 0); });
    await page.screenshot({ path: info.outputPath(`chatgpt-settings-${width}.png`), fullPage: true, animations: 'disabled' });
  }
  state.accounts[0].welcome_pending = true; await page.reload();
  await expect(page.getByRole('dialog', { name: "You're using your ChatGPT plan", exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('chatgpt-welcome-390.png'), animations: 'disabled' });
  state.authenticated = false; await page.reload();
  await expect(page.getByRole('button', { name: 'Continue with ChatGPT', exact: true })).toBeEnabled();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('chatgpt-login-390.png'), fullPage: true, animations: 'disabled' });
  expect(state.writes).toEqual([]); expect(state.outside).toEqual([]); expect(state.pageErrors).toEqual([]);
});
