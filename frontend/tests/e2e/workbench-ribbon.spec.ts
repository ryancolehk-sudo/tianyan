import { expect, test } from '@playwright/test';
import { signIn } from './researchTestHelpers';

test('home uses the compact index ribbon and signal grid, preserving chart and recovery status', async ({ page }, info) => {
  await page.clock.install({ time: new Date('2026-10-22T02:00:00Z') });
  let paused = true;
  const meta = { request_id: 'synthetic', generated_at: '2026-10-22T02:00:00Z', app_mode: 'demo', quality: { status: 'partial', reasons: [] } };
  // Keep the server-side observation date aligned with the frozen browser
  // clock; otherwise a real next-day response correctly selects historical UI.
  await page.route('**/api/v1/market-breadth-history*', route => route.fulfill({ json: { meta,
    data: { observation_day: '2026-10-22', source_name: '合成广度来源', items: [], truncated: false } } }));
  await page.route('**/api/v1/data-sources', route => route.fulfill({ json: { meta, data: { items: [{ family: 'ashare_breadth',
    interval_seconds: 300, collection_enabled: true, collection_state: paused ? 'error' : 'ready', paused_reason: paused ? 'schema_changed' : null,
    last_attempt: { reason: paused ? 'schema_changed' : null }, last_success_at: '2026-10-22T01:09:20Z', next_run_at: '2026-10-22T02:15:00Z' }] } } }));
  await page.goto('/workbench?legacy=1'); await signIn(page); await page.locator('.workbench-market-history > summary').click();
  const region = page.getByRole('region', { name: '市场概览', exact: true });
  const ribbon = region.getByLabel('指数报价横栏', { exact: true });
  const signals = region.getByLabel('市场广度参考', { exact: true });
  await expect(ribbon.locator('.index-tile')).toHaveCount(4);
  await expect(signals.locator('[data-testid^="breadth-"]')).toHaveCount(5);
  for (const field of ['advancing','declining','limit_up','limit_down','broken_limit']) {
    await expect(signals.getByTestId('breadth-' + field)).toHaveText('—');
  }
  await expect(signals.getByTestId('breadth-sentiment')).toHaveCount(0);
  const chart = region.getByRole('region', { name: '涨跌停数量走势', exact: true });
  await expect(chart).toContainText('响应校验未通过，交易时段自动复核');
  await expect(chart).toContainText('间隔至少15分钟，仅已核交易日时段');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1100 });
    await ribbon.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const boxes = await ribbon.locator('.index-tile').evaluateAll(nodes => nodes.map(n => n.getBoundingClientRect().top));
    expect(Math.max(...boxes)-Math.min(...boxes)).toBeLessThan(2);
    await region.screenshot({ path: info.outputPath(`home-ribbon-${width}.png`), animations: 'disabled' });
  }
  paused = false;
  await page.clock.fastForward(60_001);
  await expect(chart).not.toContainText('响应校验未通过');
});
