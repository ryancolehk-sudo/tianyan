import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

async function login(page: Page, path: string) {
  await page.goto(path);
  await page.getByLabel('账号', { exact: true }).fill(process.env.JL_E2E_USERNAME ?? 'e2e');
  await page.getByLabel('密码', { exact: true }).fill(process.env.JL_E2E_PASSWORD!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('button', { name: /^(退出|退出账号)$/ })).toBeVisible();
}

for (const [path, title] of [['/auction?legacy=1', '竞价观察'], ['/news?legacy=1', '实时资讯'], ['/data?view=sources', '数据管理'], ['/system', '系统设置']]) {
  test(`${title}: actual state, controls and responsive layout`, async ({ page }, info) => {
    await login(page, path);
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
    if (path.startsWith('/data')) {
      await expect(page.getByRole('cell', { name: '未配置', exact: true })).toHaveCount(4);
      await expect(page.getByTestId('summary-已登记能力')).toHaveText('5');
      await expect(page.getByTestId('summary-已配置')).toHaveText('1');
      await expect(page.getByTestId('summary-已启用')).toHaveText('1');
      await expect(page.getByTestId('summary-今日调用')).toHaveText('0');
      const sourceResponse = await page.request.get('/api/v1/data-sources');
      expect(sourceResponse.status()).toBe(200);
      const sourceIndex = await sourceResponse.json();
      await expect(page.getByRole('button', { name: '配置来源', exact: true })).toHaveCount(sourceIndex.data.available_adapters.length);
      await expect(page.getByRole('article', { name: 'BaoStock 免费盘后数据', exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: '采集一次', exact: true }).first()).toBeDisabled();
    } else if (path === '/system') {
      await expect(page.getByRole('heading', { name: '工作空间概况', exact: true })).toBeVisible();
      await expect(page.locator('.ss-health-row').filter({ hasText: '资料库' }).locator('.ss-service-dot')).toHaveClass(/\bok\b/);
      await expect(page.locator('.ss-health-row').filter({ hasText: '采集进程' }).locator('.ss-service-dot')).toHaveClass(/\bok\b/);
    }
    for (const width of [1920, 1440, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1080 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: info.outputPath(`${path.slice(1).replace(/[^a-z0-9_-]/gi, '-')}-${width}.png`), fullPage: true });
    }
    if (path === '/auction?legacy=1') {
      await page.getByText('其他来源已存竞价快照', { exact: true }).click();
      await expect(page.getByRole('heading', { name: '三阶段封单汇总', exact: true })).toBeVisible();
      await expect(page.getByText('封单金额未接入')).toBeVisible();
      await expect(page.getByText(/尚无已存批次|最近已配对批次|最近批次配对缺失/)).toBeVisible();
    } else if (path === '/news?legacy=1') {
      await expect(page.getByText('暂无已采集资讯', { exact: true })).toBeVisible();
      const requests: string[] = [];
      page.on('request', request => { if (new URL(request.url()).pathname === '/api/v1/market-news') requests.push(request.url()); });
      await page.getByRole('textbox', { name: '资讯关键词' }).fill('政策');
      await page.getByRole('radiogroup', { name: '资讯类型' }).getByRole('radio', { name: '政策', exact: true }).click();
      await page.getByRole('checkbox', { name: '去重', exact: true }).uncheck();
      await page.getByLabel('资讯日期', { exact: true }).fill('2026-10-16');
      await expect(page.locator('.filter-explanation')).toContainText('当前视图：实时列表 / 政策 / 政策 / 2026-10-16。');
      await expect(page.locator('.filter-explanation')).toContainText('读取全部来源最近100条');
      await expect(page.locator('.filter-explanation')).toContainText('不代表全网搜索');
      await expect(page.getByRole('button', { name: '加入今日研究', exact: true })).toBeDisabled();
      expect(requests).toEqual([]);
    } else if (path.startsWith('/data')) {
      await page.getByRole('radiogroup', { name: '数据管理工具' }).getByRole('radio', { name: '能力范围', exact: true }).click();
      await expect(page.getByRole('region', { name: '数据权利与能力表格' })).toBeVisible();
      await page.getByRole('radiogroup', { name: '数据管理工具' }).getByRole('radio', { name: '调用记录', exact: true }).click();
      await expect(page.getByText('尚无采集记录', { exact: true })).toBeVisible();
    } else {
      await page.getByRole('button', { name: '检查状态', exact: true }).click();
      await expect(page.locator('.ss-health-row').filter({ hasText: '资料库' }).locator('.ss-service-dot')).toHaveClass(/\bok\b/);
      await page.route('**/api/v1/system/overview', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({
        error: { code: 'service_unavailable', message: '基础服务暂不可用。', details: [] },
        request_id: '00000000-0000-4000-8000-000000000004',
      }) }));
      await page.getByRole('button', { name: '检查状态', exact: true }).click();
      await expect(page.getByRole('alert')).toContainText('00000000-0000-4000-8000-000000000004');
      await expect(page.locator('.ss-health-list .ss-service-dot.ok')).toHaveCount(0);
      await page.unroute('**/api/v1/system/overview');
      await page.getByRole('button', { name: '重试', exact: true }).click();
      await expect(page.locator('.ss-health-row').filter({ hasText: '资料库' }).locator('.ss-service-dot')).toHaveClass(/\bok\b/);
      await expect(page.locator('.ss-health-row').filter({ hasText: '采集进程' }).locator('.ss-service-dot')).toHaveClass(/\bok\b/);
    }
  });
}

test('provider loading and failure remain distinct from four successful unconfigured sources', async ({ page }, info) => {
  await login(page, '/workbench?legacy=1');
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/providers', async route => {
    await gate;
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({
      error: { code: 'database_unavailable', message: '数据服务暂不可用。', details: [] },
      request_id: '00000000-0000-4000-8000-000000000003',
    }) });
  });
  await page.goto('/data?view=sources');
  await expect(page.getByRole('status').filter({ hasText: '正在读取数据源状态' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '接入计划清单', exact: true })).toBeVisible();
  await expect(page.locator('.integration-catalog').getByText('状态未读取', { exact: true })).toHaveCount(1);
  await page.screenshot({ path: info.outputPath('data-loading.png'), fullPage: true });
  release();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '数据服务暂不可用' });
  await expect(notice).toBeVisible();
  for (const width of [1440, 390, 1440]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await expect(page.getByRole('button', { name: /^消息通知/ })).toHaveCount(1);
    await expect(notice).toBeVisible();
  }
  await expect(page.getByRole('cell', { name: '未配置', exact: true })).toHaveCount(0);
  await expect(page.getByTestId('summary-已配置')).toHaveText('—');
  await expect(page.getByRole('heading', { name: '接入计划清单', exact: true })).toBeVisible();
  await expect(page.locator('.integration-catalog').getByText('部分可用', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: info.outputPath('data-error.png'), fullPage: true });
  await page.unroute('**/api/v1/providers');
  await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(notice).toHaveCount(0);
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(page.getByRole('cell', { name: '未配置', exact: true })).toHaveCount(4);
  await expect(page.getByTestId('summary-已配置')).toHaveText('1');
});
