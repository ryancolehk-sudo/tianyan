import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { signIn } from './researchTestHelpers';

const names = ['每日复盘', '板块轮动', '明日预案', '次日验证'];
const nav = (page: Page) => page.getByRole('navigation', { name: '复盘研究子页面', exact: true });
async function positions(page: Page) {
  return nav(page).getByRole('link').evaluateAll(elements => elements.map(element => {
    const { x, y, width, height } = element.getBoundingClientRect(); return { x, y, width, height };
  }));
}
async function stable(page: Page, reference: Awaited<ReturnType<typeof positions>>) {
  const actual = await positions(page); expect(actual).toHaveLength(4);
  for (let index = 0; index < 4; index++) {
    for (const key of ['x', 'y', 'width', 'height'] as const) expect(Math.abs(actual[index][key] - reference[index][key]), `${names[index]} ${key}`).toBeLessThan(1);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
}

test('research navigation stays in place across all four pages and review workspaces', async ({ page }, info) => {
  test.setTimeout(90_000);
  await page.goto('/research/plans?date=2001-12-31'); await signIn(page);
  await expect(page.getByRole('heading', { name: '明日预案', exact: true })).toBeVisible();
  for (const width of [1244, 1221, 1320, 1321, 1440, 1920, 761, 760, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await nav(page).getByRole('link', { name: '明日预案', exact: true }).click();
    await expect(page.getByRole('heading', { name: '明日预案', exact: true })).toBeVisible();
    await page.waitForLoadState('networkidle');
    const reference = await positions(page);
    if (width === 1244) await page.screenshot({ path: info.outputPath('layout-plan-1244.png') });
    await nav(page).getByRole('link', { name: '次日验证', exact: true }).focus(); await page.keyboard.press('Enter');
    await expect(page.getByRole('heading', { name: '次日验证', exact: true })).toBeVisible();
    await stable(page, reference); await expect(page.getByLabel('验证目标日期', { exact: true })).toHaveValue('2001-12-31');
    if ([1244, 1920, 390].includes(width)) await page.screenshot({ path: info.outputPath(`layout-validation-${width}.png`) });
    for (const name of ['板块轮动', '每日复盘']) {
      await nav(page).getByRole('link', { name, exact: true }).click();
      await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
      await stable(page, reference); await expect(page.getByLabel('研究日期', { exact: true })).toHaveValue('2001-12-31');
    }
    await page.getByRole('radio', { name: '归档阅读', exact: true }).click();
    await stable(page, reference); await expect(page.getByLabel('研究日期', { exact: true })).toHaveCount(0);
    await expect(page.locator('.research-toolbar-controls').getByLabel('归档适用日', { exact: true })).toBeVisible();
    await page.getByRole('radio', { name: '人工研究', exact: true }).click(); await stable(page, reference);
  }
});

test('daily review cards share complete visual rows at desktop, two-column and phone widths while scrolling', async ({ page }, info) => {
  await page.goto('/research/reviews?date=2001-12-31'); await signIn(page);
  await expect(page.getByRole('heading', { name: '市场环境与情绪判断', exact: true })).toBeVisible();
  const classes = ['environment', 'index', 'sectors', 'distribution', 'ladder', 'core', 'events', 'validation'];
  for (const width of [1244, 1221, 1320, 1321, 1440, 1920, 761, 760, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    const boxes = await page.locator('.daily-layout > .card').evaluateAll(elements => Object.fromEntries(elements.map(element => {
      const key = [...element.classList].find(name => name.startsWith('review-'))!.slice(7);
      const { x, y, width, height } = element.getBoundingClientRect(); return [key, { x, top: y + scrollY, bottom: y + scrollY + height, width }];
    })));
    const rows = width > 1320 ? [['environment', 'index'], ['sectors', 'distribution', 'ladder'], ['core', 'events', 'validation']]
      : width > 760 ? [['environment'], ['index', 'distribution'], ['sectors'], ['core', 'ladder'], ['events', 'validation']]
      : classes.map(name => [name]);
    for (const row of rows) {
      for (const name of row) expect(Math.abs(boxes[name].top - boxes[row[0]].top), `${width}: ${name} row alignment`).toBeLessThan(1);
    }
    for (let index = 1; index < rows.length; index++) {
      const gap = Math.min(...rows[index].map(name => boxes[name].top)) - Math.max(...rows[index - 1].map(name => boxes[name].bottom));
      expect(gap, `${width}: consecutive rows have no empty grid row`).toBeGreaterThanOrEqual(10);
      expect(gap, `${width}: consecutive rows have no empty grid row`).toBeLessThanOrEqual(16);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if ([1244, 1221, 1440, 1920, 760, 390].includes(width)) {
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: info.outputPath(`layout-daily-${width}.png`), fullPage: true });
    }
    if ([1244, 1221].includes(width)) {
      await page.locator('.review-core').evaluate(element => window.scrollTo(0, element.getBoundingClientRect().top + scrollY - 110));
      await expect(page.locator('.review-core')).toBeInViewport(); await expect(page.locator('.review-ladder')).toBeInViewport();
      await page.screenshot({ path: info.outputPath(`layout-scroll-pair-${width}.png`) });
    }
  }
});
