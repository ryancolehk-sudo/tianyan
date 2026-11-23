import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

async function login(page: Page) {
  await page.goto('/data?view=sources');
  await page.getByLabel('账号', { exact: true }).fill(process.env.JL_E2E_USERNAME ?? 'e2e');
  await page.getByLabel('密码', { exact: true }).fill(process.env.JL_E2E_PASSWORD!);
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('button', { name: /^(退出|退出账号)$/ })).toBeVisible();
  await expect(page.getByTestId('summary-已登记能力')).toHaveText('5');
}
const catalog = (page: Page) => page.locator('.integration-catalog');
const choose = (page: Page, name: string) => page.getByRole('radiogroup', { name: '接入清单分类' }).getByRole('radio', { name, exact: true }).click();

test('planning catalog is complete, separate from runtime and read only', async ({ page }) => {
  await login(page);
  const unexpected: string[] = [];
  const origin = new URL(page.url()).origin;
  page.on('request', request => {
    if (new URL(request.url()).origin !== origin || request.method() !== 'GET') unexpected.push(`${request.method()} ${new URL(request.url()).pathname}`);
  });
  await expect(catalog(page).getByLabel('规划清单数量')).toContainText('数据能力 31');
  await expect(catalog(page).getByLabel('规划清单数量')).toContainText('候选来源 31');
  await expect(catalog(page).getByLabel('规划清单数量')).toContainText('开源工具 15');
  const names: string[] = [];
  for (let i = 0; i < 4; i++) {
    names.push(...await catalog(page).locator('.catalog-id').allTextContents());
    if (i < 3) await catalog(page).getByRole('button', { name: '下一页', exact: true }).click();
  }
  expect(names).toEqual(Array.from({ length: 31 }, (_, i) => `D${String(i + 1).padStart(2, '0')}`));
  await expect(catalog(page).getByRole('button', { name: '下一页', exact: true })).toBeDisabled();
  const local = catalog(page).getByRole('row').filter({ has: page.getByText('自选关注与人工研究', { exact: true }) });
  await expect(local).toContainText('部分可用');
  await expect(local).toContainText('关注管理待做');
  await choose(page, '候选来源');
  await expect(catalog(page).locator('.catalog-result')).toContainText('31 / 31');
  await expect(catalog(page).getByText('已接入', { exact: true })).toHaveCount(0);
  await expect(page.getByTestId('summary-已配置')).toHaveText('1');
  await expect(page.getByTestId('summary-今日调用')).toHaveText('0');
  await expect(page.getByTestId('summary-预计费用')).toHaveText('—');
  expect(unexpected).toEqual([]);
});

test('catalog filters, empty results, reset and native keyboard details work', async ({ page }, info) => {
  await login(page);
  await catalog(page).getByLabel('覆盖模块', { exact: true }).selectOption('竞价观察');
  await catalog(page).getByLabel('实施批次', { exact: true }).selectOption('条件评估');
  await expect(catalog(page).locator('.catalog-result')).toContainText('3 / 31');
  const level2 = catalog(page).getByRole('row').filter({ has: page.getByText('逐笔、撤单与委托队列', { exact: true }) });
  await expect(level2).toContainText('未确认长期免费全覆盖方案');
  await level2.getByLabel('查看逐笔、撤单与委托队列详情').focus();
  await page.keyboard.press('Enter');
  await expect(level2.getByText('1.0保持未接入，不从L1价量差反推真实撤单或队列。', { exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('catalog-auction-conditions.png'), fullPage: true });
  await catalog(page).getByRole('searchbox', { name: '清单关键词' }).fill('没有这个来源__');
  await expect(catalog(page).getByText('没有匹配的计划条目', { exact: true })).toBeVisible();
  await expect(catalog(page).getByRole('button', { name: '下一页', exact: true })).toBeDisabled();
  await catalog(page).getByRole('button', { name: '重置筛选' }).click();
  await expect(catalog(page).locator('.catalog-result')).toContainText('31 / 31');
  await expect(catalog(page).getByRole('button', { name: '上一页', exact: true })).toBeDisabled();
  await catalog(page).getByRole('radiogroup', { name: '接入清单分类' }).getByRole('radio', { name: '数据能力', exact: true }).focus();
  await page.keyboard.press('ArrowRight');
  await expect(catalog(page).getByRole('radio', { name: '候选来源', exact: true })).toBeFocused();
  await expect(catalog(page).locator('.catalog-result')).toContainText('31 / 31');
});

test('source conditions and tool licenses do not imply free data or installation', async ({ page }, info) => {
  await login(page);
  await choose(page, '候选来源');
  await catalog(page).getByRole('searchbox', { name: '清单关键词' }).fill('财联社');
  await expect(catalog(page).locator('.catalog-result')).toContainText('1 / 31');
  await expect(catalog(page).getByRole('cell', { name: /未接入.*待书面授权/ })).toBeVisible();
  await catalog(page).getByLabel('查看财联社内容详情').click();
  await expect(catalog(page)).toContainText('未获授权不自动聚合条目或全文');
  await page.screenshot({ path: info.outputPath('catalog-source-rights.png'), fullPage: true });
  await catalog(page).getByRole('button', { name: '重置筛选' }).click();
  await choose(page, '开源工具');
  await catalog(page).getByRole('searchbox', { name: '清单关键词' }).fill('RSSHub');
  await expect(catalog(page)).toContainText('AGPL-3.0');
  await expect(catalog(page).getByText('未引入', { exact: true })).toBeVisible();
  await expect(catalog(page)).toContainText('开源工具不等于上游数据免费');
  await catalog(page).getByLabel('查看RSSHub详情').click();
  await expect(catalog(page)).toContainText('路由不等于转载授权');
  const link = catalog(page).getByRole('link', { name: '查看固定版本资料' });
  await expect(link).toHaveAttribute('href', 'https://github.com/DIYgod/RSSHub/tree/11fcf768bcbfa460133c4ce1ba14a5b59a201b40');
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  await page.screenshot({ path: info.outputPath('catalog-tool-license.png'), fullPage: true });
});

test('catalog stays inside the blue white shell across desktop and narrow screens', async ({ page }, info) => {
  await login(page);
  for (const width of [1920, 1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1080 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const table = page.getByRole('region', { name: '接入计划表格', exact: true });
    await expect(table).toBeVisible();
    await table.focus();
    if (width === 390) {
      await page.keyboard.press('End');
      await page.keyboard.press('ArrowRight');
      await expect.poll(() => table.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
      await table.evaluate(element => { element.scrollLeft = 0; });
    }
    await page.screenshot({ path: info.outputPath(`catalog-${width}.png`), fullPage: true });
  }
});
