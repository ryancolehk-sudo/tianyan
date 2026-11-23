import { expect, test } from '@playwright/test';
import { signIn } from './researchTestHelpers';

const id = '00000000-0000-4000-8000-000000000012';
const meta = { request_id: id, app_mode: 'demo', generated_at: '2020-01-03T10:00:00Z', as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic isolated publisher fixture'] } };

test('Hong Kong finance source keeps original title, date precision, URL and local filters', async ({ page }, info) => {
  await page.route('**/api/v1/market-news*', route => route.fulfill({ json: { meta, data: { items: [{
    id, source_id: id, source_name: '合成香港官方来源', family: 'hk_gov_finance',
    received_at: '2020-01-03T10:00:00Z', published_at: null, content_hash: '1'.repeat(64),
    payload: { title: '合成香港官方財經條目', source_name: '香港政府新聞網', category: '财经',
      source_url: 'https://www.news.gov.hk/tc/synthetic-article.shtml', summary: null, published_day: '2020-01-02' },
  }] } } }));
  await page.goto('/news?legacy=1&family=hk_gov_finance'); await signIn(page);
  const source = page.getByRole('combobox', { name: '资讯来源', exact: true });
  const region = page.getByRole('region', { name: '已采集资讯列表', exact: true });
  await expect(source).toHaveValue('hk_gov_finance');
  await expect(region).toContainText('合成香港官方財經條目');
  await expect(region).toContainText('2020-01-02（仅日期）');
  await expect(page.locator('.source-fields dd').nth(1)).toHaveText('2020-01-02（仅日期）');
  await expect(page.locator('.source-fields dd').nth(2)).toHaveText('2020/1/3 18:00:00');
  await expect(page.getByRole('link', { name: '打开原始出处' })).toHaveAttribute('href', 'https://www.news.gov.hk/tc/synthetic-article.shtml');
  const requests: string[] = [];
  page.on('request', request => { if (new URL(request.url()).pathname === '/api/v1/market-news') requests.push(request.url()); });
  await page.getByRole('radiogroup', { name: '资讯类型' }).getByRole('radio', { name: '财经', exact: true }).click();
  await page.getByLabel('资讯日期', { exact: true }).fill('2020-01-02');
  await expect(region).toContainText('合成香港官方財經條目');
  expect(requests).toEqual([]);
  await page.reload(); await expect(source).toHaveValue('hk_gov_finance');
  await expect(region).toContainText('2020-01-02（仅日期）');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('hk-news-' + width + '.png'), animations: 'disabled' });
  }
});

test('data center lists the explicit source and distinguishes an adapter from a connection', async ({ page }, info) => {
  await page.goto('/data?view=sources'); await signIn(page);
  const source = page.getByRole('article', { name: '香港政府官方财经资讯', exact: true });
  await expect(source).toBeVisible(); await expect(source).toContainText('未配置');
  await expect(source).toContainText('每15分钟最多一轮');
  await expect(source).toContainText('仅日期');
  const writes: string[] = [];
  page.on('request', request => { if (request.method() !== 'GET') writes.push(new URL(request.url()).pathname); });
  const catalog = page.locator('.integration-catalog');
  await catalog.getByRole('radiogroup', { name: '接入清单分类' }).getByRole('radio', { name: '候选来源', exact: true }).click();
  await catalog.getByRole('searchbox', { name: '清单关键词' }).fill('香港政府');
  const row = catalog.getByRole('row').filter({ has: page.getByText('香港政府财经RSS', { exact: true }) });
  await expect(row).toContainText('待配置');
  await expect(catalog.locator('.catalog-result')).toContainText('1 / 31');
  await row.getByLabel('查看香港政府财经RSS详情').click();
  await expect(row).toContainText('公开和商业使用另核');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 }); await source.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('hk-source-' + width + '.png'), animations: 'disabled' });
  }
  expect(writes).toEqual([]);
});
