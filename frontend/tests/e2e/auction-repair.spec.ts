import { expect, test } from '@playwright/test';
import { DAY, PREV_DAY, openAuction, sessionBody } from './auction-vendor.fixtures';
import { routeWorkbench, workbenchItems } from './auction-workbench.fixtures';
import { routeHermeticApi } from './hermetic-api.fixtures';

test('manual date and source persist after refresh, without mixing versions', async ({ page }) => {
  await routeWorkbench(page); await openAuction(page);
  await page.getByRole('button', { name: '更多设置', exact: true }).click();
  await page.getByLabel('封单表来源', { exact: true }).selectOption('duanxianxia_auction_history');
  await page.getByLabel('最近三个历史交易日', { exact: true }).selectOption(PREV_DAY);
  await page.getByRole('button', { name: '刷新页面数据', exact: true }).click();
  await expect(page.getByLabel('封单来源日期', { exact: true })).toHaveValue(PREV_DAY);
  await expect(page.locator('.auction-wb-selected')).toContainText('历史表');
  await page.getByText('来源、日期与配对批次', { exact: true }).click();
  await expect(page.getByTestId('auction-vendor-source-date')).toContainText(PREV_DAY.replaceAll('-', '/'));
});

test('default date is latest current source date even when older than today', async ({ page }) => {
  const items = workbenchItems().filter(i => i.family === 'duanxianxia_auction');
  for (const item of items) {
    item.payload.observed_day = PREV_DAY;
    for (const row of item.payload.rows) { row.trade_day = PREV_DAY; if ('source_date_raw' in row) row.source_date_raw = PREV_DAY; }
  }
  await routeWorkbench(page, { items }); await openAuction(page);
  await expect(page.getByLabel('封单来源日期', { exact: true })).toHaveValue(PREV_DAY);
  await expect(page.locator('.auction-wb-summary .auction-wb-source-line')).toContainText('最近可用日期：' + PREV_DAY);
});

test('automatic read every 65 seconds, pause/resume, no collection side effects', async ({ page }) => {
  let writes = 0, reads = 0;
  await page.route('**/api/v1/data-sources/*/refresh', route => { writes++; return route.abort(); });
  await routeWorkbench(page);
  await page.clock.install();
  page.on('request', r => { if (r.url().includes('/reference-datasets') && new URL(r.url()).searchParams.get('family')?.startsWith('duanxianxia_auction')) reads++; });
  await openAuction(page);
  await expect(page.locator('.auction-workbench')).toHaveAttribute('aria-busy', 'false');
  const before = reads; await page.clock.fastForward(65_000);
  await expect.poll(() => reads).toBeGreaterThan(before);
  await expect(page.locator('.auction-workbench')).toHaveAttribute('aria-busy', 'false');
  await page.getByRole('button', { name: '更多设置', exact: true }).click();
  await page.getByRole('button', { name: '暂停', exact: true }).click();
  const paused = reads; await page.clock.fastForward(130_000); expect(reads).toBe(paused);
  await page.getByRole('button', { name: '恢复', exact: true }).click(); await page.clock.fastForward(65_000);
  await expect.poll(() => reads).toBeGreaterThan(paused); expect(writes).toBe(0);
});

test('key-day mismatch is not displayed in current board', async ({ page }) => {
  const items = workbenchItems().filter(i => i.family === 'duanxianxia_auction');
  for (const item of items) for (const row of item.payload.rows) row.trade_day = PREV_DAY;
  await routeWorkbench(page, { items }); await openAuction(page);
  await expect(page.locator('.auction-wb-stock')).toHaveCount(0);
  await expect(page.locator('[data-seal-bar]')).toHaveCount(0);
  await expect(page.getByLabel('封单来源日期', { exact: true })).toHaveValue(DAY);
});

test('hermetic session stays consistent and exact error mocks outrank unavailable background reads', async ({ page }) => {
  await page.route('**/api/v1/data-sources/synthetic/refresh', route => route.fulfill({
    status: 503, json: { error: { code: 'synthetic_provider_failure', message: '保留精确错误夹具', details: [] } },
  }));
  await routeHermeticApi(page, sessionBody);
  await page.goto('/auction?legacy=1');
  const responses = await page.evaluate(async () => {
    const read = async (path: string, method = 'GET') => {
      const response = await fetch(path, { method });
      return { status: response.status, body: response.status === 204 ? null : await response.json() };
    };
    return [
      await read('/api/v1/session'), await read('/api/v1/session', 'POST'), await read('/api/v1/session'),
      await read('/api/v1/review-data/day'), await read('/api/v1/review-data/collections', 'POST'),
      await read('/api/v1/data-sources/synthetic/refresh', 'POST'),
      await read('/api/v1/session', 'DELETE'), await read('/api/v1/session'),
    ];
  });
  expect(responses.map(response => response.status)).toEqual([401, 200, 200, 503, 405, 503, 204, 401]);
  expect(responses[2].body).toEqual(sessionBody);
  expect(responses[3].body.error.code).toBe('fixture_read_not_configured');
  expect(responses[4].body.error.code).toBe('fixture_write_not_configured');
  expect(responses[5].body.error.code).toBe('synthetic_provider_failure');
});
