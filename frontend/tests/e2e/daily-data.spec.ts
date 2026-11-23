import { expect, test } from '@playwright/test';
import { signIn } from './researchTestHelpers';

test('daily reference keeps its date and non-live label on workbench and data centre', async ({ page }, info) => {
  // Synthetic UI-only response in the disposable demo instance. Backend worker
  // persistence, entitlement checks and calendar are tested against PostgreSQL.
  await page.route('**/api/v1/market-quotes*', async route => {
    const response = await route.fetch();
    if (response.status() !== 200) return route.fulfill({ response });
    const body = await response.json();
    body.data.items = body.data.items.map((row: object, index: number) => index === 3 ? row : ({ ...row,
      state: 'daily_close', change: '0.3', change_percent: '2.5',
      quote: { price: '12.3', previous_close: '12', open: '12', high: '13', low: '11', quote_time: null,
        quote_kind: 'daily_close', trade_day: '2001-12-31', price_unit: 'point', volume_shares: null, amount_cny: null },
      provenance: { source_name: '合成盘后来源', family: 'baostock_daily', adapter_version: 'synthetic-only',
        received_at: '2002-01-02T01:00:00Z', published_at: null, available_at: null, historical_eligible: false,
        source_id: '00000000-0000-4000-8000-000000000001', source_version_id: '00000000-0000-4000-8000-000000000002',
        evidence_version_id: '00000000-0000-4000-8000-000000000003', content_hash: '0'.repeat(64) } }));
    await route.fulfill({ response, json: body });
  });
  await page.goto('/workbench?legacy=1'); await signIn(page);
  const strip = page.getByRole('region', { name: '市场概览' });
  await expect(strip.getByText('盘后收盘 · 非实时', { exact: true })).toHaveCount(3);
  const card = page.locator('.quote-card');
  await expect(card.locator('.trading-quote-meta')).toContainText('2001-12-31 · 盘后收盘');
  await expect(card).toContainText('盘后收盘 · 非实时');
  await expect(card.getByText('今开', { exact: true })).toBeVisible();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`synthetic-daily-${width}.png`), animations: 'disabled' });
  }
  await page.goto('/data?view=sources');
  await expect(page.locator('.integration-catalog')).toContainText('盘后参考可用');
  await expect(page.locator('.integration-catalog')).toContainText('四指数返回 3 / 4');
  await expect(page.getByRole('article', { name: 'BaoStock 免费盘后数据', exact: true })).toContainText('未配置');
  const catalog = page.locator('.integration-catalog');
  await catalog.getByRole('radio', { name: '候选来源', exact: true }).click();
  await catalog.getByRole('searchbox', { name: '清单关键词' }).fill('BaoStock');
  await expect(catalog.locator('.catalog-result')).toContainText('1 / 31');
  await expect(catalog.getByText('待配置', { exact: true })).toBeVisible();
});
