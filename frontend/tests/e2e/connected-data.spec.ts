import { expect, test } from '@playwright/test';
import { signIn } from './researchTestHelpers';

const meta = { request_id: '00000000-0000-4000-8000-000000000001', app_mode: 'demo', generated_at: '2020-01-03T10:00:00Z', as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic UI fixture only'] } };
const id = '00000000-0000-4000-8000-000000000002';

test('connected reference table shows actual schema, nullable cells, local filtering and narrow layout', async ({ page }, info) => {
  await page.route('**/api/v1/reference-datasets', route => route.fulfill({ json: { meta, data: { items: [{ id, source_id: id, family: 'baostock_classification', source_name: '合成参考来源', source_key: 'reference:classification.industry', instrument_id: null, received_at: '2020-01-03T10:00:00Z', published_at: null, content_hash: '0'.repeat(64), payload: { dataset: 'classification.industry', title: '合成行业表', attribution: '合成测试源', source_url: 'https://example.com/source', scope: '独立demo合成测试，非真实市场数据', observed_day: '2020-01-03', columns: [{ key: 'code', label: '代码', unit: null }, { key: 'industry', label: '行业', unit: null }], rows: [{ code: 'synthetic', industry: null }] } }] } } }));
  await page.goto('/data?view=datasets'); await signIn(page);
  await expect(page.getByRole('region', { name: '已入库参考数据', exact: true })).toContainText('synthetic');
  await expect(page.getByRole('cell', { name: '—', exact: true })).toBeVisible();
  await page.getByRole('searchbox', { name: '参考数据关键词' }).fill('does-not-exist');
  await expect(page.getByText('没有匹配的记录', { exact: true })).toBeVisible();
  await page.getByRole('searchbox', { name: '参考数据关键词' }).fill('');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('connected-table-' + width + '.png'), animations: 'disabled' });
  }
});

test('collected news preserves source and receipt times, filters locally and opens original provenance', async ({ page }, info) => {
  await page.route('**/api/v1/market-news', route => route.fulfill({ json: { meta, data: { items: [{ id, source_id: id, source_name: '合成官方来源', family: 'fed_public', received_at: '2020-01-03T10:00:00Z', published_at: '2020-01-02T10:00:00Z', content_hash: '1'.repeat(64), payload: { title: '合成资讯甲：用于测试', summary: '独立demo的合成原摘要', source_url: 'https://example.com/news', source_name: '合成官方来源', category: '海外' } }] } } }));
  await page.goto('/news?legacy=1'); await signIn(page);
  const region = page.getByRole('region', { name: '已采集资讯列表', exact: true });
  await expect(region).toContainText('合成资讯甲');
  await page.getByRole('button', { name: '合成资讯甲：用于测试', exact: true }).click();
  await expect(page.locator('.news-detail')).toContainText('2020/1/2');
  await expect(page.locator('.news-detail')).toContainText('2020/1/3');
  await expect(page.getByRole('link', { name: '打开原始出处' })).toHaveAttribute('href', 'https://example.com/news');
  await page.getByRole('textbox', { name: '资讯关键词' }).fill('none');
  await expect(page.getByText('暂无已采集资讯', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '资讯关键词' }).fill('合成');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('connected-news-' + width + '.png'), animations: 'disabled' });
  }
});

test('daily candles show explicit adjustment and preserve date-only history at desktop and phone widths', async ({ page }, info) => {
  await page.route('**/api/v1/market-quotes*', async route => {
    const response = await route.fetch(); if (response.status() !== 200) return route.fulfill({ response });
    const body = await response.json(); body.data.items[0].instrument.id = id;
    await route.fulfill({ response, json: body });
  });
  await page.route('**/api/v1/market-bars*', route => route.fulfill({ json: { meta, data: { instrument: { id, exchange: 'SSE', symbol: '000001', asset_type: 'index', name: '合成指数' }, source_name: '合成历史来源', adjustment: new URL(route.request().url()).searchParams.get('adjustment'), historical_reconstruction: true, items: ['2020-01-02', '2020-01-03'].map(day => ({ id, received_at: '2020-01-04T10:00:00Z', content_hash: '2'.repeat(64), payload: { open: '10', high: '13', low: '9', close: '12', volume_shares: null, amount_cny: null, interval: '1d', bar_end_at: null, trade_day: day, adjustment: 'unadjusted', price_unit: 'point' } })) } } }));
  await page.goto('/workbench?legacy=1'); await signIn(page);
  await expect(page.getByRole('img', { name: '日K历史蜡烛图' })).toBeVisible();
  await expect(page.locator('.candle-readout')).toContainText('2020-01-03');
  await expect(page.locator('.candle-readout')).toContainText('未知');
  await page.getByLabel('K线复权口径').selectOption('forward');
  await expect(page.getByRole('img', { name: '日K历史蜡烛图' })).toBeVisible();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('connected-history-' + width + '.png'), animations: 'disabled' });
  }
  await page.getByRole('radio', { name: '周K', exact: true }).click();
  await expect(page.getByRole('img', { name: '周K历史蜡烛图' })).toBeVisible();
  await expect(page.locator('.history-chart-panel')).toContainText('首尾周期可能不完整');
});

test('date-only announcements use the disclosed day for display and filtering without inventing a clock time', async ({ page }) => {
  await page.route('**/api/v1/market-news', route => route.fulfill({ json: { meta, data: { items: [{ id, source_id: id, source_name: '合成公告来源', family: 'cninfo_announcements', received_at: '2020-01-03T10:00:00Z', published_at: null, content_hash: '3'.repeat(64), payload: { title: '合成日期公告', summary: null, source_url: 'https://example.com/announcement', published_day: '2020-01-02', category: '公告' } }] } } }));
  await page.goto('/news?legacy=1'); await signIn(page);
  const region = page.getByRole('region', { name: '已采集资讯列表', exact: true });
  await expect(region).toContainText('2020-01-02（仅日期）');
  await page.getByLabel('资讯日期').fill('2020-01-02');
  await expect(region).toContainText('合成日期公告');
  await page.getByLabel('资讯日期').fill('2020-01-03');
  await expect(page.getByText('暂无已采集资讯', { exact: true })).toBeVisible();
});
