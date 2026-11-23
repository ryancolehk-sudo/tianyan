import { openLegacyWorkbench } from './legacyWorkbench';
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

// Entirely synthetic session and failed data reads: these UI regressions cannot
// sign in to the personal instance or change market/source/research records.
const user = '00000000-0000-4000-8000-000000000061';
const key = `tianyan.workbench.widgets.v1:demo:${user}`;
const geometryKey = `tianyan.workbench.geometry.v1:demo:${user}`;
async function mockSession(page: Page) {
  const requests: string[] = [];
  await page.route('**/api/**', route => {
    requests.push(route.request().method() + ' ' + new URL(route.request().url()).pathname);
    if (new URL(route.request().url()).pathname === '/api/v1/session') return route.fulfill({ json: {
      meta: { app_mode: 'demo' }, data: { user: { id: user, username: 'synthetic-ui', role: 'researcher' }, csrf_token: 'synthetic-not-a-credential' },
    } });
    return route.fulfill({ status: 503, json: { request_id: 'synthetic', error: { message: '合成测试：缓存暂不可用' } } });
  });
  return requests;
}
const manager = (page: Page) => page.locator('.workbench-widget-manager');
const quote = (page: Page) => page.locator('[data-widget-id="quote"]');
const bounds = (page: Page) => page.evaluate(() => [...document.querySelectorAll<HTMLElement>('.dashboard-tile')].map(el => {
  const rect = el.getBoundingClientRect(); return { id: el.dataset.widgetId, x: rect.x, y: rect.y, w: rect.width, h: rect.height };
}));

test('component controls are ahead of the chart on desktop and phone; choices survive refresh', async ({ page }) => {
  const requests = await mockSession(page); await page.goto('/workbench?legacy=1');
  await expect(page.getByRole('heading', { name: '盘中工作台', exact: true })).toBeVisible();
  for (const width of [1440, 1024, 768, 390]) {
    await page.setViewportSize({ width, height: 900 });
    const summary = await manager(page).locator('summary').first().boundingBox();
    expect(summary!.y + summary!.height).toBeLessThan(260);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await manager(page).locator('summary').first().click();
  await expect(manager(page).getByRole('checkbox')).toHaveCount(30);
  await manager(page).getByRole('checkbox', { name: /^龙虎榜/ }).uncheck();
  await expect(page.locator('[data-widget-id="top-list"]')).toHaveCount(0);
  await page.reload(); await manager(page).locator('summary').first().click();
  await expect(manager(page).getByRole('checkbox', { name: /^龙虎榜/ })).not.toBeChecked();
  await page.getByRole('searchbox', { name: '查找组件', exact: true }).fill('利润表');
  await expect(manager(page).getByRole('checkbox')).toHaveCount(1);
  await page.getByRole('searchbox', { name: '查找组件', exact: true }).fill('没有此组件');
  await expect(manager(page)).toContainText('没有匹配的组件');
  expect(requests.every(value => value.startsWith('GET '))).toBe(true);
});

test('pointer and keyboard sizing save correctly, reload and cancel preserve the committed layout', async ({ page }) => {
  await mockSession(page); await page.goto('/workbench?legacy=1');
  await expect(page.locator('.dashboard-wide')).toBeVisible();
  await page.getByRole('button', { name: '编辑布局', exact: true }).click();
  const before = await quote(page).getAttribute('style');
  await page.getByRole('button', { name: '移动行情与个股观察', exact: true }).press('Shift+ArrowDown');
  await expect(quote(page)).not.toHaveAttribute('style', before!);
  const resize = page.getByRole('button', { name: '调整行情与个股观察大小', exact: true });
  await resize.scrollIntoViewIfNeeded(); const rect = await resize.boundingBox();
  await page.mouse.move(rect!.x + 12, rect!.y + 12); await page.mouse.down();
  await page.mouse.move(rect!.x + 90, rect!.y + 92, { steps: 8 }); await page.mouse.up();
  const after = await quote(page).getAttribute('style'); expect(after).not.toBe(before);
  await page.getByRole('button', { name: '保存布局', exact: true }).click();
  const stored = await page.evaluate(key => JSON.parse(localStorage.getItem(key)!), geometryKey);
  expect(stored.tiles.find((tile: { id: string }) => tile.id === 'quote').h).toBeGreaterThan(18);
  await page.reload(); await expect(quote(page)).toHaveAttribute('style', after!);
  await page.getByRole('button', { name: '编辑布局', exact: true }).click();
  await page.getByRole('button', { name: '移动行情与个股观察', exact: true }).press('Shift+ArrowUp');
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await expect(quote(page)).toHaveAttribute('style', after!);
});

test('all 30 widgets fit without overlaps; hiding everything keeps an obvious recovery control', async ({ page }) => {
  const requests = await mockSession(page); const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto('/workbench?legacy=1'); await manager(page).locator('summary').first().click();
  for (const input of await manager(page).getByRole('checkbox').all()) await input.check();
  await expect(page.locator('.dashboard-tile')).toHaveCount(30);
  const rectangles = await bounds(page);
  for (let i = 0; i < rectangles.length; i++) for (const other of rectangles.slice(i + 1)) {
    const a = rectangles[i]; expect(a.x < other.x + other.w - 1 && a.x + a.w > other.x + 1 && a.y < other.y + other.h - 1 && a.y + a.h > other.y + 1).toBe(false);
  }
  for (const input of await manager(page).getByRole('checkbox').all()) await input.uncheck();
  await expect(page.locator('.dashboard-tile')).toHaveCount(0);
  await expect(page.locator('.dashboard-empty')).toContainText('管理组件');
  await manager(page).getByRole('button', { name: '恢复默认组件', exact: true }).click();
  await expect(page.locator('.dashboard-tile')).toHaveCount(7);
  expect(requests.every(value => value.startsWith('GET '))).toBe(true); expect(errors).toEqual([]);
});

test('legacy coordinates migrate without overwriting current visibility or retaining private content', async ({ page }) => {
  await mockSession(page);
  await page.addInitScript(({ user, key }) => {
    localStorage.setItem(key, JSON.stringify({ version: 1, visible: ['rules', 'funds'] }));
    localStorage.setItem(`tianyan:workbench-layout:v1:${user}`, JSON.stringify({ version: 1, tiles: [
      { id: 'rule', x: 0, y: 0, w: 6, h: 8, visible: true }, { id: 'funds', x: 6, y: 0, w: 6, h: 8, visible: true },
    ] }));
  }, { user, key });
  await page.goto('/workbench?legacy=1'); await expect(page.locator('.dashboard-tile')).toHaveCount(2);
  await expect(page.locator('[data-widget-id="rules"]')).toHaveAttribute('style', /span 6/);
  const stored = await page.evaluate(key => JSON.parse(localStorage.getItem(key)!), geometryKey);
  expect(stored.tiles.map((item: { id: string }) => item.id).sort()).toEqual(['funds', 'rules']);
  for (const item of stored.tiles) expect(Object.keys(item).sort()).toEqual(['h', 'id', 'w', 'x', 'y']);
});

test('malformed coordinates and denied storage remain usable, including switching away and back', async ({ page }) => {
  await mockSession(page);
  await page.addInitScript(key => localStorage.setItem(key, JSON.stringify({ version: 1, tiles: [{ id: 'quote', x: -100, y: 1e9, w: 40, h: 0 }] })), geometryKey);
  await page.goto('/workbench?legacy=1'); await expect(quote(page)).toHaveAttribute('style', /span 8/);
  await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: '系统设置', exact: true }).click();
  await expect(page.locator('#workbench-header')).toBeHidden();
  await openLegacyWorkbench(page, '工作台');
  await expect(page.getByRole('button', { name: '编辑布局', exact: true })).toHaveCount(1);
  await page.evaluate(() => Object.defineProperty(Storage.prototype, 'setItem', { configurable: true, value() { throw new DOMException('Synthetic denial', 'SecurityError'); } }));
  await manager(page).locator('summary').first().click();
  await manager(page).getByRole('checkbox', { name: /^龙虎榜/ }).uncheck();
  await expect(manager(page)).toContainText('本次选择已生效，浏览器暂未能保存');
  await page.getByRole('button', { name: '编辑布局', exact: true }).click();
  await page.getByRole('button', { name: '保存布局', exact: true }).click();
  await expect(page.locator('.dashboard-status')).toContainText('浏览器未能保存布局');
});

test('desktop and phone show a restrained, usable component library and single-column mobile canvas', async ({ page }, info) => {
  await mockSession(page); await page.goto('/workbench?legacy=1');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await manager(page).locator('summary').first().click();
    await page.screenshot({ path: info.outputPath(`homepage-library-${width}.png`), animations: 'disabled' });
    await manager(page).locator('summary').first().click();
    await quote(page).getByRole('heading', { name: '行情与个股观察', exact: true }).scrollIntoViewIfNeeded();
    if (width === 390) {
      const rectangles = await bounds(page); expect(new Set(rectangles.map(r => Math.round(r.x))).size).toBe(1);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`homepage-canvas-${width}.png`), animations: 'disabled' });
    await page.evaluate(() => window.scrollTo(0, 0));
  }
});
