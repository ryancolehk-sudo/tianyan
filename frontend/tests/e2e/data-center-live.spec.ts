import { expect, test } from '@playwright/test';
import type { Page, TestInfo } from '@playwright/test';
import type { AiSettings, ConnectionIndex, ConnectionInput, ConnectionView, Envelope, Overview } from '../../src/features/data-center-live/api';
import { signIn } from './researchTestHelpers';

// These tests persist synthetic credentials. They must never run against the
// normal :8088 instance, and never send a model request to a public supplier.
const isolated = (() => {
  try {
    const url = new URL(process.env.JL_E2E_BASE_URL ?? '');
    return process.env.JL_E2E_DATA_CENTER_ISOLATED === '1'
      && url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname) && url.port === '31365';
  } catch { return false; }
})();

type SessionData = { user: { id: string; role: string }; csrf_token: string };

async function openDataCenter(page: Page) {
  // signIn verifies the ordinary shell's visible logout action; the data-center
  // desktop layout has its own header, so authenticate before entering it.
  await page.goto('/system');
  await signIn(page);
  await page.goto('/data?view=ai');
  await page.locator('.dc-simple-model .dc-advanced > summary').click();
  await page.locator('.dc-feature-bindings > summary').click();
  await expect(page.locator('.dc-live').getByRole('heading', { name: '数据管理', exact: true })).toBeVisible();
  await expect(page.locator('.dc-editor').getByLabel('服务名称', { exact: true })).toBeVisible();
  const session = await page.request.get('/api/v1/session');
  expect(session.ok()).toBe(true);
  const body = await session.json() as Envelope<SessionData>;
  expect(body.data.user.role, 'the isolated browser account must be an administrator').toBe('admin');
  return body.data;
}

async function integrations(page: Page) {
  const response = await page.request.get('/api/v1/integrations');
  expect(response.ok()).toBe(true);
  return (await response.json() as Envelope<ConnectionIndex>).data;
}

async function overview(page: Page) {
  const response = await page.request.get('/api/v1/data-center/overview');
  expect(response.ok()).toBe(true);
  return (await response.json() as Envelope<Overview>).data;
}

async function assertPrivateStorage(page: Page, secret: string) {
  expect(await page.evaluate(value => {
    const stored = JSON.stringify({ local: Object.fromEntries(Object.entries(localStorage)),
      session: Object.fromEntries(Object.entries(sessionStorage)), navigation: history.state });
    return stored.includes(value);
  }, secret), 'API Key must not enter localStorage, sessionStorage or browser navigation state').toBe(false);
  expect(await page.content(), 'saved API Key must not remain in rendered markup').not.toContain(secret);
  await expect(page.locator('.dc-editor').getByLabel('API Key', { exact: true })).toHaveValue('');
}

async function screenshots(page: Page, info: TestInfo) {
  for (const view of ['ai', 'monitor'] as const) {
    await page.setViewportSize({ width: 1672, height: 941 });
    await page.getByRole('button', { name: view === 'ai' ? 'AI 设置' : '运行监控', exact: true }).click();
    await expect(page.locator(view === 'ai' ? '.dc-default-panel' : '.dc-call-panel')).toBeVisible();
    const navigation = page.getByRole('navigation', { name: '主导航', exact: true });
    await expect(navigation.getByRole('link')).toHaveCount(5);
    for (const [name, href] of [['工作台', '/workbench'], ['每日复盘', '/research/reviews'],
      ['资讯中心', '/news'], ['数据管理', '/data'], ['系统设置', '/system']]) {
      await expect(navigation.getByRole('link', { name, exact: true })).toHaveAttribute('href', href);
    }
    if (view === 'ai') {
      if (!await page.locator('.dc-simple-model .dc-advanced').evaluate(element => element.hasAttribute('open'))) await page.locator('.dc-simple-model .dc-advanced > summary').click();
      if (!await page.locator('.dc-feature-bindings').evaluate(element => element.hasAttribute('open'))) await page.locator('.dc-feature-bindings > summary').click();
      await expect(page.locator('.dc-editor').getByLabel('服务名称', { exact: true })).toBeVisible();
      await expect(page.locator('.dc-bindings')).toBeVisible();
    }
    await expect(page.locator('.dc-live .dc-loading')).toHaveCount(0);
    for (const width of [1672, 1440, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 941 });
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        window.scrollTo(0, 0);
      });
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: info.outputPath(`data-center-${view}-${width}.png`), fullPage: true, animations: 'disabled' });
    }
  }
}

async function findService(page: Page, name: string) {
  await page.getByRole('button', { name: 'AI 设置', exact: true }).click();
  const card = page.locator('.dc-service-card').filter({ has: page.getByRole('heading', { name, exact: true }) });
  const maximumPages = Math.max(1, Math.ceil((await integrations(page)).items.filter(item => item.kind === 'model').length / 4));
  for (let index = 0; index < maximumPages; index++) {
    if (await card.count()) return card;
    const next = page.getByRole('button', { name: '下一页', exact: true });
    if (!await next.count() || !await next.isEnabled()) break;
    await next.click();
  }
  await expect(card, 'the service saved by the real backend must be listed').toBeVisible();
  return card;
}

function inputFor(connection: ConnectionView): ConnectionInput {
  return { name: connection.name, kind: connection.kind, template: connection.template, endpoint: connection.endpoint,
    protocol: connection.protocol, model: connection.model, enabled: false, source_ids: connection.source_ids,
    options: connection.options, expected_revision: connection.revision };
}

test.describe('data center with the isolated real backend', () => {
  test.describe.configure({ mode: 'serial' });
  test.skip(!isolated, 'requires JL_E2E_DATA_CENTER_ISOLATED=1 and the dedicated jlpm-check-dc instance on port 31365');
  test.beforeEach(() => { expect(process.env.JL_E2E_PASSWORD, 'set the dedicated administrator password for researchTestHelpers.signIn').toBeTruthy(); });
  test.setTimeout(90_000);

  test('reads and refresh stay read only; real tabs, legacy tools and responsive states remain usable', async ({ page }, info) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await openDataCenter(page);
    const writes: string[] = [];
    page.on('request', request => {
      if (new URL(request.url()).pathname.startsWith('/api/') && request.method() !== 'GET') writes.push(request.method() + ' ' + new URL(request.url()).pathname);
    });

    const settingsResponse = await page.request.get('/api/v1/ai/settings');
    expect(settingsResponse.ok()).toBe(true);
    const settings = (await settingsResponse.json() as Envelope<AiSettings>).data;
    expect(settings.enabled, 'first-run global AI must stay off').toBe(false);
    await expect(page.getByRole('switch', { name: '全局 AI 开关', exact: true })).toHaveAttribute('aria-checked', 'false');
    await expect(page.getByRole('switch', { name: '全局 AI 开关', exact: true })).toBeDisabled();
    expect(settings.default_connection_id).toBeNull();
    expect(settings.features.every(feature => !feature.enabled), 'fresh feature bindings must stay off').toBe(true);
    // Canonical ai.schemas.FEATURES now implements plan_generation. The three
    // remaining unavailable businesses retain disabled, unchecked controls.
    const unavailable = ['情绪研判', '方向与核心分析', '日报 / 周报 / 月报'];
    expect(settings.features.filter(feature => !feature.implemented).map(feature => feature.name).sort()).toEqual([...unavailable].sort());
    expect(settings.features.find(feature => feature.id === 'plan_generation')?.implemented).toBe(true);
    await expect(page.getByRole('switch', { name: '明日预案生成启用开关', exact: true })).toHaveAttribute('aria-checked', 'false');
    for (const feature of unavailable) {
      const toggle = page.getByRole('switch', { name: feature + '启用开关', exact: true });
      await expect(toggle).toBeDisabled();
      await expect(toggle).toHaveAttribute('aria-checked', 'false');
    }
    await expect(page.locator('.dc-bindings').getByText('业务待接入', { exact: true })).toHaveCount(3);

    const before = await overview(page);
    await page.getByRole('button', { name: '运行监控', exact: true }).click();
    await expect(page.locator('.dc-call-panel')).toBeVisible();
    await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/data-center/overview' && response.request().method() === 'GET'),
      page.getByRole('button', { name: '刷新状态', exact: true }).click(),
    ]);
    await page.getByRole('button', { name: 'AI 设置', exact: true }).click();
    await page.getByRole('button', { name: '更多数据工具', exact: true }).click();
    const tools = page.getByRole('radiogroup', { name: '数据管理工具', exact: true });
    await expect(tools).toBeVisible();
    await tools.getByRole('radio', { name: '研究材料', exact: true }).click();
    await expect(tools.getByRole('radio', { name: '研究材料', exact: true })).toHaveAttribute('aria-checked', 'true');
    await expect(page).toHaveURL(/view=materials/);
    await page.reload();
    await expect(page.getByRole('radio', { name: '研究材料', exact: true })).toHaveAttribute('aria-checked', 'true');
    await page.getByRole('button', { name: '返回数据来源', exact: true }).click();
    await expect(page.locator('.dc-suppliers-section')).toBeVisible();

    await screenshots(page, info);
    expect((await overview(page)).today_requests, 'page reads must not count as model or market supplier requests').toBe(before.today_requests);
    expect(writes, 'GET, refresh, view switching and legacy tools must not issue write requests').toEqual([]);
    expect(errors).toEqual([]);
  });

  test('synthetic Key is encrypted, never echoed, retained on revision edits and never used for a public probe', async ({ page }) => {
    const errors: string[] = [];
    const outsideOrigins: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    const auth = await openDataCenter(page);
    const origin = new URL(page.url()).origin;
    page.on('request', request => { if (new URL(request.url()).origin !== origin) outsideOrigins.push(new URL(request.url()).origin); });
    const index = await integrations(page);
    expect(index.credential_store_ready, 'mount the isolated private credential volume and its independent master key').toBe(true);
    const name = '浏览器合成模型-' + crypto.randomUUID();
    const secret = 'synthetic-local-only-' + crypto.randomUUID();
    let created: ConnectionView | null = null;
    let removed = false;
    try {
      const editor = page.locator('.dc-editor');
      await editor.getByLabel('服务名称', { exact: true }).fill(name);
      // .invalid is a reserved non-resolving domain. Saving does no upstream I/O;
      // the browser never clicks model discovery or the generation-test button.
      await editor.getByLabel('接口地址', { exact: true }).fill('https://model-test.invalid/v1');
      await editor.getByLabel('API Key', { exact: true }).fill(secret);
      await editor.getByLabel('模型名称', { exact: true }).fill('synthetic-no-network-model-v1');
      const saveResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/integrations' && response.request().method() === 'POST');
      await editor.getByRole('button', { name: '保存接入', exact: true }).click();
      const response = await saveResponse;
      expect(response.status()).toBe(201);
      expect(await response.text()).not.toContain(secret);
      created = (await response.json() as Envelope<ConnectionView>).data;
      expect(created.enabled).toBe(false);
      expect(created.credential_configured).toBe(true);
      expect(created.credential_origin).toBe('vault');
      expect(created.credential_error_code ?? null).toBeNull();
      expect(created.last_test).toBeNull();
      expect(created.tested_revision ?? null).toBeNull();
      expect(created).not.toHaveProperty('api_key');
      const card = await findService(page, name);
      await expect(card.getByRole('switch', { name: '启用' + name, exact: true })).toBeDisabled();
      await expect(card).not.toContainText(secret);
      await assertPrivateStorage(page, secret);

      await card.getByRole('button', { name: '配置', exact: true }).click();
      await expect(editor.getByLabel('API Key', { exact: true })).toHaveValue('');
      await expect(editor.getByLabel('API Key', { exact: true })).toHaveAttribute('placeholder', '已安全保存；留空保留原 Key');
      await editor.getByLabel('模型名称', { exact: true }).fill('synthetic-no-network-model-v2');
      const editResponse = page.waitForResponse(result => new URL(result.url()).pathname === '/api/v1/integrations/' + created!.id && result.request().method() === 'PATCH');
      await editor.getByRole('button', { name: '保存接入', exact: true }).click();
      const updatedResponse = await editResponse;
      expect(updatedResponse.ok()).toBe(true);
      expect(updatedResponse.request().postDataJSON()).not.toHaveProperty('api_key');
      expect(updatedResponse.request().postDataJSON()).toMatchObject({ expected_revision: created.revision, enabled: false });
      const updated = (await updatedResponse.json() as Envelope<ConnectionView>).data;
      expect(updated.revision).toBe(created.revision + 1);
      expect(updated.model).toBe('synthetic-no-network-model-v2');
      expect(updated.credential_configured).toBe(true);
      expect(updated.credential_origin).toBe('vault');
      expect(updated.last_test).toBeNull();
      created = updated;
      await assertPrivateStorage(page, secret);

      const headers = { Origin: origin, 'X-CSRF-Token': auth.csrf_token, 'Idempotency-Key': crypto.randomUUID() };
      const enable = await page.request.patch('/api/v1/integrations/' + created.id, { headers, data: { ...inputFor(created), enabled: true } });
      expect(enable.status(), 'the backend must reject enable without a successful current-revision probe').toBe(409);
      expect((await enable.json()).error.code).toBe('test_required');

      const before = await overview(page);
      const rejected = await page.request.post('/api/v1/integrations/probe', { headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
        data: { ...inputFor(created), id: created.id, endpoint: 'https://127.0.0.1/v1' } });
      expect(rejected.status(), 'a private HTTPS draft must be rejected before any upstream send').toBe(422);
      expect((await rejected.json()).error.code).toBe('endpoint_rejected');
      expect((await overview(page)).today_requests).toBe(before.today_requests);
      const actual = (await integrations(page)).items.find(item => item.id === created!.id);
      expect(actual?.revision).toBe(created.revision);
      expect(actual?.enabled).toBe(false);
      expect(actual?.last_test).toBeNull();
      const calls = await page.request.get('/api/v1/data-center/calls?connection_id=' + created.id);
      expect(calls.ok()).toBe(true);
      expect((await calls.json()).data.items).toEqual([]);
      expect(await (await page.request.get('/api/v1/integrations')).text()).not.toContain(secret);
      await assertPrivateStorage(page, secret);

      const currentCard = await findService(page, name);
      await currentCard.getByRole('button', { name: '移除' + name, exact: true }).click();
      const deleteResponse = page.waitForResponse(result => new URL(result.url()).pathname === '/api/v1/integrations/' + created!.id && result.request().method() === 'DELETE');
      await currentCard.getByRole('button', { name: '确认移除', exact: true }).click();
      expect((await deleteResponse).status()).toBe(204);
      await expect(page.getByRole('heading', { name, exact: true })).toHaveCount(0);
      expect((await integrations(page)).items.some(item => item.id === created!.id)).toBe(false);
      removed = true;
      expect(outsideOrigins, 'the browser must not contact a public supplier').toEqual([]);
      expect(errors).toEqual([]);
    } finally {
      if (created && !removed) {
        const cleanup = await page.request.delete('/api/v1/integrations/' + created.id, { headers: { Origin: origin, 'X-CSRF-Token': auth.csrf_token } });
        expect(cleanup.status(), 'clean up the synthetic connection even after a failed assertion').toBe(204);
      }
    }
  });
});
