import { expect } from '@playwright/test';
import { test } from './isolated-browser.fixtures';

// Exercise retained deep links and unavailable states without a real account.
test('research deep links fit narrow screens and explicit legacy views keep their own shell', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname;
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'demo', quality: { status: 'partial', reasons: ['合成兼容性检查'] } } } });
    if (path === '/api/v1/session') return ok({ user: { id: 'synthetic-compatibility', username: 'synthetic', role: 'viewer' }, csrf_token: 'synthetic-only' });
    if (path === '/api/v1/market-session') return ok({ trade_day: null, phase: 'unknown', automatic_live: false, is_trading_day: null, poll_seconds: 60 });
    if (path === '/api/v1/ai/settings') return ok({ enabled: false, features: [] });
    return route.fulfill({ status: 503, json: { error: { code: 'synthetic_unavailable', message: '合成检查：资料暂不可用' } } });
  });
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), () => page.keyboard.press('Escape'));
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const path of ['/research/directions','/research/plans','/research/validations']) {
      await page.goto(path);
      await expect(page.locator('.research-shell')).toBeVisible();
      await expect(page.locator('#main-content')).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), path).toBe(true);
    }
    for (const path of ['/workbench?legacy=1','/auction?legacy=1','/news?legacy=1','/research/reviews?legacy=1']) {
      await page.goto(path);
      await expect(page.locator('.app')).toBeVisible();
      await expect(page.locator('.ty-shell, .research-shell')).toHaveCount(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), path).toBe(true);
    }
  }
  expect(errors).toEqual([]);
});
