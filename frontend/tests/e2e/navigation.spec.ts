import { expect, test } from '@playwright/test';
import type { Page, TestInfo } from '@playwright/test';
import { signIn } from './researchTestHelpers';

async function shot(page: Page, info: TestInfo, name: string) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const sidebar = page.locator('.sidebar');
  if (await sidebar.evaluate(element => element.classList.contains('is-open'))) {
    await expect(sidebar).toHaveCSS('transform', 'matrix(1, 0, 0, 1, 0, 0)');
    await expect(sidebar.getByRole('button', { name: '实时资讯', exact: true })).toBeInViewport();
    await expect(sidebar.getByRole('button', { name: '复盘研究', exact: true })).toBeInViewport();
    const scrim = page.getByRole('button', { name: '关闭导航', exact: true });
    await scrim.hover({ position: { x: page.viewportSize()!.width - 12, y: 400 } });
    await expect(scrim).toHaveCSS('background-color', 'rgba(16, 44, 81, 0.4)');
  }
  await page.screenshot({ path: info.outputPath(name + '.png'), animations: 'disabled' });
}

test('sidebar groups contain their children, toggle by keyboard and preserve all news view URLs', async ({ page }, info) => {
  test.setTimeout(60_000);
  await page.goto('/workbench?legacy=1'); await signIn(page);
  const nav = page.getByRole('navigation', { name: '主导航', exact: true });
  const research = nav.getByRole('button', { name: '复盘研究', exact: true });
  const news = nav.getByRole('button', { name: '实时资讯', exact: true });
  await expect(nav.locator(':scope > .nav-item')).toHaveCount(6);
  await expect(research).toHaveAttribute('aria-expanded', 'false');
  await expect(news).toHaveAttribute('aria-expanded', 'false');
  await research.click(); await expect(page).toHaveURL(/\/workbench\?legacy=1$/);
  await expect(nav.locator('#research-children')).toBeVisible();
  await expect(nav.locator('[data-nav-group="research"] > #research-children a')).toHaveCount(4);
  const belowResearch = await nav.getByRole('link', { name: '数据管理', exact: true }).boundingBox();
  const children = await nav.locator('#research-children').boundingBox();
  expect(belowResearch!.y).toBeGreaterThan(children!.y + children!.height);
  await news.click();
  await research.press('Enter'); await expect(nav.locator('#research-children')).toBeHidden();
  await expect(nav.locator('#news-children')).toBeVisible();
  await research.press('Space'); await expect(nav.locator('#research-children')).toBeVisible();
  for (const width of [1440, 1920]) {
    await page.setViewportSize({ width, height: 1080 });
    await shot(page, info, 'sidebar-both-open-' + width);
  }
  await nav.locator('#research-children').getByRole('link', { name: '明日预案', exact: true }).click();
  await expect(page.getByRole('heading', { name: '明日预案', exact: true })).toBeVisible();
  await page.reload(); await expect(research).toHaveAttribute('aria-expanded', 'true');
  await expect(nav.locator('#research-children').getByRole('link', { name: '明日预案', exact: true })).toHaveAttribute('aria-current', 'page');
  await page.setViewportSize({ width: 1440, height: 1080 }); await shot(page, info, 'sidebar-research-1440');
  // Navigation may prepare saved news through same-origin GETs. It must never
  // submit a collection or request a publisher directly from the browser.
  const requests: string[] = [], origin = new URL(page.url()).origin;
  page.on('request', request => { if (request.method() !== 'GET' || new URL(request.url()).origin !== origin) requests.push(request.method() + ' ' + request.url()); });
  await news.click();
  for (const [id, name] of [['feed', '实时列表'], ['events', '事件分析'], ['topics', '题材专题'], ['following', '我的关注']]) {
    await nav.locator('#news-children').getByRole('link', { name, exact: true }).click();
    await expect(page).toHaveURL(new RegExp('/news\\?view=' + id + '&legacy=1$'));
    await expect(page.getByRole('radiogroup', { name: '资讯工作视图' }).getByRole('radio', { name, exact: true })).toHaveAttribute('aria-checked', 'true');
    await expect(nav.locator('#news-children').getByRole('link', { name, exact: true })).toHaveAttribute('aria-current', 'page');
    await expect(nav.locator('a[aria-current="page"]')).toHaveCount(1);
    await page.reload();
    await expect(page.getByRole('radiogroup', { name: '资讯工作视图' }).getByRole('radio', { name, exact: true })).toHaveAttribute('aria-checked', 'true');
    if (id === 'events') await shot(page, info, 'sidebar-news-events-1440');
  }
  await page.setViewportSize({ width: 1920, height: 1080 }); await shot(page, info, 'sidebar-news-following-1920');
  await page.getByRole('textbox', { name: '资讯关键词', exact: true }).fill('本地筛选');
  const views = page.getByRole('radiogroup', { name: '资讯工作视图' });
  await views.getByRole('radio', { name: '我的关注', exact: true }).press('Home');
  await expect(page).toHaveURL(/\/news\?view=feed&legacy=1$/);
  await expect(nav.locator('#news-children').getByRole('link', { name: '实时列表', exact: true })).toHaveAttribute('aria-current', 'page');
  await page.goBack(); await expect(views.getByRole('radio', { name: '我的关注', exact: true })).toHaveAttribute('aria-checked', 'true');
  await page.goForward(); await expect(views.getByRole('radio', { name: '实时列表', exact: true })).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByRole('textbox', { name: '资讯关键词', exact: true })).toHaveValue('本地筛选');
  await page.goto('/news?view=unknown&legacy=1');
  await expect(page.getByRole('radiogroup', { name: '资讯工作视图' }).getByRole('radio', { name: '实时列表', exact: true })).toHaveAttribute('aria-checked', 'true');
  expect(requests).toEqual([]);
});

test('mobile drawer expands both groups and returns focus when the active group is collapsed', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/workbench?legacy=1'); await signIn(page);
  const nav = page.getByRole('navigation', { name: '主导航', exact: true });
  const opener = page.getByRole('button', { name: '打开主导航', exact: true });
  await expect(nav).not.toBeVisible(); await opener.click();
  const research = nav.getByRole('button', { name: '复盘研究', exact: true });
  const news = nav.getByRole('button', { name: '实时资讯', exact: true });
  await research.click(); await research.click(); await expect(nav.locator('#research-children')).toBeHidden();
  await research.press('Enter'); await news.click();
  await expect(nav.locator('#news-children')).toBeVisible(); await expect(nav.locator('#research-children')).toBeVisible();
  await shot(page, info, 'sidebar-both-open-390');
  await nav.locator('#research-children').getByRole('link', { name: '板块轮动', exact: true }).click();
  await expect(page.getByRole('heading', { name: '板块轮动', exact: true })).toBeVisible();
  await expect(nav).not.toBeVisible(); await opener.click();
  await expect(nav.locator('#research-children').getByRole('link', { name: '板块轮动', exact: true })).toBeFocused();
  await shot(page, info, 'sidebar-research-390');
  await research.click(); await page.keyboard.press('Escape'); await expect(opener).toBeFocused();
  await opener.click(); await expect(research).toBeFocused();
  // The persistent shell retains the other group's explicit expanded state across page links.
  await expect(news).toHaveAttribute('aria-expanded', 'true');
  await news.click(); await expect(nav.locator('#news-children')).toBeHidden();
  await news.click(); await expect(nav.locator('#news-children')).toBeVisible();
  await nav.locator('#news-children').getByRole('link', { name: '我的关注', exact: true }).click();
  await expect(page).toHaveURL(/\/news\?view=following&legacy=1$/); await page.reload();
  await expect(page.getByRole('radiogroup', { name: '资讯工作视图' }).getByRole('radio', { name: '我的关注', exact: true })).toHaveAttribute('aria-checked', 'true');
  await opener.click(); await shot(page, info, 'sidebar-news-following-390');
  await page.keyboard.press('Escape'); await expect(opener).toBeFocused();
});
