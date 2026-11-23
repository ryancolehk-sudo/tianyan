import { expect, test } from '@playwright/test';
import { signIn } from './researchTestHelpers';
import { openLegacyWorkbench } from './legacyWorkbench';

const id = '00000000-0000-4000-8000-000000000019';
const meta = { request_id: id, app_mode: 'demo', generated_at: '2020-01-02T02:00:00Z', as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic breadth UI only'] } };
const item = { id, source_id: id, source_name: '合成广度来源', family: 'ashare_breadth', source_key: 'reference:market.ashare.breadth', instrument_id: null,
  received_at: meta.generated_at, published_at: null, content_hash: '0'.repeat(64), payload: { dataset: 'market.ashare.breadth', title: '合成广度样本',
    source_url: 'https://example.com/synthetic-breadth', attribution: 'Synthetic only', scope: '合成样本；源日期未知，非实时。', observed_day: null,
    columns: [{ key: 'limit_up', label: '涨停' }, { key: 'limit_down', label: '跌停' }, { key: 'advancing', label: '上涨' }, { key: 'declining', label: '下跌' }],
    rows: [{ limit_up: '2', limit_down: '0', advancing: '12', declining: null }] } };

test('breadth cache shows known counts, preserves unknowns, and keeps the shell stable', async ({ page }, info) => {
  await page.route('**/api/v1/reference-datasets?dataset=market.ashare.breadth', route => route.fulfill({ json: { meta, data: { items: [item] } } }));
  await page.goto('/workbench?legacy=1'); await signIn(page);
  const tile = page.getByLabel('市场广度参考', { exact: true });
  await expect(tile.getByTestId('breadth-limit_up')).toHaveText('2');
  await expect(tile.getByTestId('breadth-limit_down')).toHaveText('0');
  await expect(tile.getByTestId('breadth-broken_limit')).toHaveText('—');
  await expect(tile.getByTestId('breadth-declining')).toHaveText('—');
  await expect(tile).toContainText('源日期未知');
  await expect(tile).toHaveAttribute('title', /合成广度来源.*采集时点不是行情时点/);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 }); await tile.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('market-breadth-' + width + '.png'), animations: 'disabled' });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openLegacyWorkbench(page, '竞价观察');
  await expect(tile.getByTestId('breadth-limit_up')).toHaveText('2');
});

test('breadth read error is retryable and does not substitute zero counts', async ({ page }) => {
  let broken = true;
  await page.route('**/api/v1/reference-datasets?dataset=market.ashare.breadth', route => broken
    ? route.fulfill({ status: 503, json: { request_id: id, error: { code: 'unavailable', message: '合成读取失败' } } })
    : route.fulfill({ json: { meta, data: { items: [item] } } }));
  await page.goto('/workbench?legacy=1'); await signIn(page);
  const tile = page.getByLabel('市场广度参考', { exact: true });
  await expect(tile).toContainText('广度读取失败');
  await expect(tile.getByTestId('breadth-limit_up')).toHaveText('—');
  broken = false; await tile.getByRole('button', { name: '重试', exact: true }).click();
  await expect(tile.getByTestId('breadth-limit_up')).toHaveText('2');
});
