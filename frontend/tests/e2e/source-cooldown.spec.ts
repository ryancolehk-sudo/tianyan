import { expect, test } from '@playwright/test';
import { signIn } from './researchTestHelpers';

test('supplier cooldown stays distinct from last results on desktop and phone', async ({ page }, info) => {
  await page.goto('/data?view=sources'); await signIn(page);
  const original = await (await page.request.get('/api/v1/data-sources')).json();
  const start = new Date().toISOString();
  const until = new Date(Date.now() + 3600_000).toISOString();
  const families = ['ashare_breadth', 'ashare_public', 'ashare_sector_heat', 'tencent_web'];
  const items = families.map((family, index) => {
    const adapter = original.data.available_adapters.find((row: { family: string }) => row.family === family);
    const id = `00000000-0000-4000-8000-00000000000${index + 1}`;
    return { provider: { id, name: adapter.name, kind: adapter.kind, adapter_version: adapter.version, configured: true, enabled: true,
      entitlement_status: 'verified', capabilities: adapter.capabilities, rights: { display: 'allowed', cache: 'allowed', export: 'denied', redistribute: 'denied' } },
      family, implementation: 'implemented', rights_basis: '仅供隔离浏览器测试的合成配置',
      last_attempt: index === 0 ? { id, source_id: id, capability: adapter.capabilities.join('+'), started_at: start, received_at: start,
        status: 'error', reason: 'rate_limited', expected: null, accepted: null, rejected: null, duplicates: null, elapsed_ms: null,
        request_count: 1, cost_cny: null, retry_not_before: until } : null,
      last_success_at: null, collection_state: index === 0 ? 'error' : 'not_checked', credential_configured: null,
      collection_enabled: true, paused_reason: null, symbols: [], interval_seconds: 300, configured_at: start, next_run_at: until,
      cooldown_until: index < 3 ? until : null, cooldown_scope: index < 3 ? 'supplier' : null };
  });
  const response = { ...original, data: { ...original.data, items, collection_enabled: true } };
  await page.route('**/api/v1/data-sources', route => route.fulfill({ json: response }));
  await page.reload();
  const runtime = page.locator('.source-runtime');
  await expect(runtime.getByText(/供应商共用冷却至/)).toHaveCount(3);
  await expect(runtime.getByRole('button', { name: '等待冷却', exact: true })).toHaveCount(3);
  for (const item of items) {
    const card = runtime.getByRole('article', { name: item.provider.name, exact: true });
    await expect(card.getByText('下次自动尝试', { exact: true })).toBeVisible();
    if (item.family === 'tencent_web') await expect(card.getByRole('button', { name: '采集一次', exact: true })).toBeEnabled();
    else await expect(card.getByRole('button', { name: '等待冷却', exact: true })).toBeDisabled();
  }
  const breadth = runtime.getByRole('article', { name: items[0].provider.name, exact: true });
  await expect(breadth.getByText('采集失败', { exact: true })).toBeVisible();
  await expect(breadth.getByText('上游限流，等待后重试', { exact: true })).toBeVisible();
  await expect(breadth).toContainText('不代表上游保证恢复');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await breadth.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`source-cooldown-${width}.png`), animations: 'disabled' });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  for (const row of items) { row.cooldown_until = null; row.cooldown_scope = null; }
  await page.reload();
  await expect(runtime.getByRole('button', { name: '等待冷却', exact: true })).toHaveCount(0);
  await expect(breadth.getByRole('button', { name: '采集一次', exact: true })).toBeEnabled();
  await expect(breadth.getByText('采集失败', { exact: true })).toBeVisible();
});
