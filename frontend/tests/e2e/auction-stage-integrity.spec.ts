import { expect, test } from '@playwright/test';
import { DAY, PREV_DAY, openAuction } from './auction-vendor.fixtures';
import { routeWorkbench, workbenchItems } from './auction-workbench.fixtures';

for (const [time, count] of [['09:14:59', 0], ['09:15:00', 1], ['09:19:59', 1], ['09:20:00', 2], ['09:24:59', 2], ['09:25:00', 3]] as const) {
  test(`nonempty rolling provider columns are gated by the stored Shanghai checkpoint at ${time}`, async ({ page }) => {
    const items = workbenchItems();
    for (const i of items.filter(i => i.family === 'duanxianxia_auction')) i.received_at = `${DAY}T${time}+08:00`;
    await routeWorkbench(page, { items });
    await openAuction(page, undefined, `${DAY}T${time}+08:00`);
    const work = page.getByRole('region', { name: '竞价全景', exact: true });
    await expect(work.locator('[data-seal-bar="selected"]')).toHaveCount(count);
    await expect(work.locator('[data-seal-bar="previous"]')).toHaveCount(3);
    const card = page.getByRole('region', { name: `${DAY} 当前表`, exact: true });
    const cells = card.locator('.auction-wb-stock').first().locator('.auction-wb-amounts > span');
    for (let n = count; n < 3; n++) await expect(cells.nth(n)).toHaveText('—');
    await expect(work.locator('.auction-time-basis')).toContainText('非实时盘口');
    await expect(work.locator('.auction-time-basis')).toContainText('来源行情时点未知');
    await work.getByText('查看柱状图原值与单位', { exact: true }).click();
    await expect(work.getByRole('region', { name: '封单柱状图原值', exact: true }).getByRole('row').last().getByRole('cell')).toHaveCount(3);
    // Previous-day observations remain available, independent of today's clock.
    await expect(page.getByRole('region', { name: `${PREV_DAY} 历史表`, exact: true }).locator('.auction-wb-stock')).toHaveCount(25);
  });
}

test('09:21 cache never gains 09:25 values from the clock, refresh, sorting or raw-value expansion', async ({ page }, info) => {
  const items = workbenchItems();
  const current = items.filter(i => i.family === 'duanxianxia_auction');
  for (const item of current) {
    item.received_at = `${DAY}T09:21:00+08:00`;
    if (item.payload.dataset.endsWith('.stocks')) {
      item.payload.rows = item.payload.rows.slice(0, 2);
      Object.assign(item.payload.rows[0], { fd_0920_cny: '80000000', fd_0920_raw: '8000万', fd_0925_cny: '10000000', fd_0925_raw: '1000万' });
      Object.assign(item.payload.rows[1], { fd_0920_cny: '70000000', fd_0920_raw: '7000万', fd_0925_cny: '90000000', fd_0925_raw: '9000万' });
    }
  }
  await routeWorkbench(page, { items }); await openAuction(page, undefined, `${DAY}T09:22:15+08:00`);
  const work = page.getByRole('region', { name: '竞价全景', exact: true });
  const card = page.getByRole('region', { name: `${DAY} 当前表`, exact: true });
  await expect(card.locator('.auction-wb-stock').first()).toHaveAttribute('aria-label', /600001/);
  await expect(work.locator('.auction-time-basis')).toContainText('09:21:00');
  await expect(work.locator('.auction-time-basis')).toContainText('09:25 未采到');
  await expect(work.locator('[data-seal-bar="selected"]')).toHaveCount(2);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await work.screenshot({ path: info.outputPath(`pending-stage-${width}.png`) });
  }
  await page.clock.setFixedTime(new Date(`${DAY}T09:26:00+08:00`));
  await work.getByRole('button', { name: '刷新页面数据', exact: true }).click();
  await expect(work.locator('[data-seal-bar="selected"]')).toHaveCount(2);
  await expect(card.locator('.auction-wb-stock').first().locator('.auction-wb-amounts > span').nth(2)).toHaveText('—');
  await work.getByText('查看柱状图原值与单位', { exact: true }).click();
  await expect(work.getByRole('region', { name: '封单柱状图原值', exact: true }).getByRole('row').last().getByRole('cell').nth(2)).toContainText('—');
  // A new response received AFTER the checkpoint is required, even when the
  // vendor payload/hash has not changed. Waiting or reopening is insufficient.
  for (const item of current) item.received_at = `${DAY}T09:25:10+08:00`;
  await work.getByRole('button', { name: '刷新页面数据', exact: true }).click();
  await expect(work.locator('[data-seal-bar="selected"]')).toHaveCount(3);
  await expect(card.locator('.auction-wb-stock').first()).toHaveAttribute('aria-label', /600002/);
});

test('a late summary cannot promote an early stock record and invalid receipt time fails closed', async ({ page }) => {
  const items = workbenchItems();
  for (const item of items.filter(i => i.family === 'duanxianxia_auction')) item.received_at = item.payload.dataset.endsWith('.stocks') ? `${DAY}T09:21:00+08:00` : `${DAY}T09:26:00+08:00`;
  await routeWorkbench(page, { items }); await openAuction(page, undefined, `${DAY}T09:26:00+08:00`);
  const work = page.getByRole('region', { name: '竞价全景', exact: true });
  await expect(work.locator('[data-seal-bar="selected"]')).toHaveCount(2);
  for (const item of items.filter(i => i.family === 'duanxianxia_auction')) item.received_at = 'invalid';
  await work.getByRole('button', { name: '刷新页面数据', exact: true }).click();
  await expect(work.locator('[data-seal-bar="selected"]')).toHaveCount(0);
});
