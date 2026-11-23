import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import { signIn } from './researchTestHelpers';

const id = '00000000-0000-4000-8000-000000000022';
const meta = { request_id: id, app_mode: 'demo', generated_at: '2020-01-02T02:00:00Z', as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic sector fixture'] } };
const item = { id, source_id: id, source_name: '合成板块关注来源', family: 'ashare_sector_heat', source_key: 'reference:market.ashare.sector_heat', instrument_id: null,
  received_at: meta.generated_at, published_at: null, content_hash: '2'.repeat(64), payload: { dataset: 'market.ashare.sector_heat', title: '合成板块关注排名', source_url: 'https://example.com/synthetic-sector', attribution: 'Synthetic fixture only', observed_day: null,
    scope: '合成关注排名，不是涨幅或资金榜；混合命名空间、时区未注明。', columns: [{ key: 'rank', label: '供应商关注排名' }, { key: 'name', label: '原名' }],
    rows: Array.from({ length: 10 }, (_, index) => ({ rank: String(index + 1), name: '合成板块' + (index + 1), symbol: 'pt010000' + String(index + 1).padStart(2, '0'), source_type: 'BK-HY-1', rank_delta: index === 1 ? null : '0', ranking_updated_raw: '2020-01-02 08:25:00' })) } };

test('workbench reads cached attention ranks and keeps quote/flow scope separate on both widths', async ({ page }, info) => {
  const paths: string[] = [];
  await page.route('**/api/v1/reference-datasets?dataset=market.ashare.sector_heat', route => { paths.push(new URL(route.request().url()).pathname); return route.fulfill({ json: { meta, data: { items: [item] } } }); });
  await page.goto('/workbench?legacy=1'); await signIn(page);
  await page.getByText('关注热度辅助', { exact: true }).click();
  const region = page.getByRole('region', { name: '热门板块关注排名', exact: true });
  await expect(region.getByRole('listitem')).toHaveCount(8);
  await expect(region).toContainText('显示 8 / 10 条已存记录');
  await expect(region).toContainText('含海外板块');
  await expect(region.locator('.sector-heat-change').nth(1)).toContainText('—');
  await expect(region.locator('.sector-heat-change').first()).toContainText('0');
  await region.getByText('查看来源与时间', { exact: true }).click();
  await expect(region).toContainText('2020-01-02 08:25:00（时区未注明）');
  await expect(region).toContainText('2020/1/2 10:00:00（北京时间）');
  await expect(region.getByRole('link', { name: '查看完整表格与来源状态 ›' })).toHaveAttribute('href', '/data?view=datasets');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 }); await region.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('sector-heat-' + width + '.png'), animations: 'disabled' });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  const categories = page.getByRole('radiogroup', { name: '板块类型', exact: true });
  await categories.getByRole('radio', { name: '行业', exact: true }).click();
  await expect(page.getByRole('region', { name: '行业与概念行情', exact: true })).toContainText('所选日期暂无已存资料');
  await page.getByText('关注热度辅助', { exact: true }).click();
  await expect(region).not.toBeVisible();
  await page.getByText('关注热度辅助', { exact: true }).click();
  await expect(region.getByRole('listitem')).toHaveCount(8);
  expect(paths.every(path => path === '/api/v1/reference-datasets')).toBe(true);
});

test('attention loading, failure/retry, empty reply and absent source stay distinct', async ({ page }) => {
  let state = 'loading'; let release!: () => void;
  const ready = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/reference-datasets?dataset=market.ashare.sector_heat', async route => {
    if (state === 'loading') await ready;
    if (state === 'error') return route.fulfill({ status: 503, json: { request_id: id, error: { code: 'unavailable', message: '合成板块读取失败' } } });
    return route.fulfill({ json: { meta, data: { items: state === 'absent' ? [] : [{ ...item, payload: { ...item.payload, rows: [] } }] } } });
  });
  await page.goto('/workbench?legacy=1'); await signIn(page);
  await page.getByText('关注热度辅助', { exact: true }).click();
  const region = page.getByRole('region', { name: '热门板块关注排名', exact: true });
  await expect(region.getByRole('status')).toContainText('正在读取板块参考');
  state = 'error'; release();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成板块读取失败' });
  await expect(notice).toBeVisible();
  state = 'empty'; await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(notice).toHaveCount(0);
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(region).toContainText('该轮榜单没有条目');
  state = 'absent'; await region.getByRole('button', { name: '刷新缓存', exact: true }).click();
  await expect(region).toContainText('热门板块尚未采集');
});

test('data center lists the dedicated adapter and free/paid gaps without enabling it', async ({ page }, info) => {
  await page.goto('/data?view=sources'); await signIn(page);
  const card = page.getByRole('article', { name: 'AShareAPI 免费热门板块参考', exact: true });
  await expect(card).toContainText('未配置'); await expect(card).toContainText('共享35秒预算');
  await expect(card).toContainText('覆盖数按表版本计');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 }); await card.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('sector-source-' + width + '.png'), animations: 'disabled' });
  }
});
