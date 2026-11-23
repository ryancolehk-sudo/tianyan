import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

async function login(page: Page) {
  await page.goto('/workbench?legacy=1');
  await page.getByLabel('账号', { exact: true }).fill(process.env.JL_E2E_USERNAME ?? 'e2e');
  await page.getByLabel('密码', { exact: true }).fill(process.env.JL_E2E_PASSWORD!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('heading', { name: '盘中工作台', exact: true })).toBeVisible();
}

test('Tianyan brand, reference layout and keyboard chart controls', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await login(page);
  await expect(page).toHaveTitle('盘中工作台 · 天衍系统');
  const logo = page.getByRole('img', { name: '天衍系统 logo' });
  await expect(logo).toBeVisible();
  expect(await logo.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true);
  await expect(page.locator('body')).not.toContainText('九龙术');
  await expect(page.locator('body')).not.toContainText('jiulong');
  await expect(page.getByRole('heading', { name: '规则观察', exact: true })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '行情与个股观察', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '板块行情', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '连板观察池', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '研究记录', exact: true })).toBeVisible();
  const chart = page.getByRole('radiogroup', { name: '图表周期' });
  await chart.getByRole('radio', { name: '分时', exact: true }).focus();
  await page.keyboard.press('ArrowRight');
  await expect(chart.getByRole('radio', { name: '日K', exact: true })).toBeChecked();
  await page.getByLabel('分钟K周期', { exact: true }).selectOption('1分钟');
  await expect(page.getByRole('region', { name: '行情图表', exact: true })).toBeVisible();
  await page.locator('.chart-data-details > summary').click();
  await expect(page.getByLabel('分钟行情日期')).toBeVisible();
  await chart.getByRole('radio', { name: '日K', exact: true }).focus();
  await page.keyboard.press('Home');
  await expect(chart.getByRole('radio', { name: '分时', exact: true })).toBeChecked();
  for (const width of [1440, 1920]) {
    await page.setViewportSize({ width, height: 1080 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`workbench-${width}.png`), fullPage: true });
  }
  expect(errors).toEqual([]);
});

test('real instrument query distinguishes unavailable from an empty result', async ({ page }, info) => {
  await login(page);
  await page.getByRole('textbox', { name: '搜索标的', exact: true }).fill('000001');
  await page.getByRole('button', { name: '搜索标的', exact: true }).click();
  await expect(page.getByRole('region', { name: '标的查询结果' })).toContainText('标的库未接入');
  await expect(page.getByRole('region', { name: '标的查询结果' })).not.toContainText('没有匹配的标的');
  await page.screenshot({ path: info.outputPath('instrument-unavailable.png'), fullPage: true });
  await page.getByRole('textbox', { name: '搜索标的', exact: true }).focus();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('region', { name: '标的查询结果' })).toHaveCount(0);
});

test('instrument failure keeps request ID, loading and a working retry', async ({ page }, info) => {
  await login(page);
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/instruments?*', async route => {
    await gate;
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({
      error: { code: 'provider_unavailable', message: '标的服务暂不可用。', details: [] },
      request_id: '00000000-0000-4000-8000-000000000002',
    }) });
  });
  await page.getByRole('textbox', { name: '搜索标的', exact: true }).fill('000001');
  await page.getByRole('button', { name: '搜索标的', exact: true }).click();
  const results = page.getByRole('region', { name: '标的查询结果', exact: true });
  await expect(results.getByRole('status')).toContainText('正在查询标的库');
  release();
  await expect(results.getByRole('alert')).toContainText('标的服务暂不可用');
  await expect(results.getByRole('alert')).toContainText('00000000-0000-4000-8000-000000000002');
  await page.screenshot({ path: info.outputPath('instrument-error.png'), fullPage: true });
  await page.unroute('**/api/v1/instruments?*');
  await results.getByRole('button', { name: '重新加载', exact: true }).click();
  await expect(results).toContainText('标的库未接入');
});

test('narrow screen navigation, filter state and keyboard close', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect(page.getByRole('navigation', { name: '主导航' })).not.toBeVisible();
  await page.getByRole('button', { name: '打开主导航' }).click();
  await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible();
  await page.screenshot({ path: info.outputPath('mobile-navigation.png'), fullPage: true });
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: '打开主导航' })).toBeFocused();
  await expect(page.getByRole('navigation', { name: '主导航' })).not.toBeVisible();
  await page.getByRole('radiogroup', { name: '板块类型' }).getByRole('radio', { name: '概念', exact: true }).click();
  await expect(page.getByRole('region', { name: '行业与概念行情', exact: true })).toContainText('所选日期暂无已存资料');
  await page.getByRole('radiogroup', { name: '研究类型' }).getByRole('radio', { name: '预案', exact: true }).click();
  await expect(page.getByRole('region', { name: '我的研究记录', exact: true })).toContainText('该日还没有研究记录');
  await page.screenshot({ path: info.outputPath('workbench-mobile.png'), fullPage: true });
});
