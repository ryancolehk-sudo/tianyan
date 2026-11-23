import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import { day, fixture, login, post, reviewBody } from './researchTestHelpers';

test('module guide reaches the current daily review; explicit record jumps preserve date, focus, and read failures', async ({ page }, info) => {
  test.setTimeout(60_000);
  await login(page);
  const f = await fixture(page);
  const title = '合成入口复盘-' + f.key;
  await post(page, 'research-documents', { kind: 'review', source_trade_day: day, revision: reviewBody(f, title, '保存的判断不应被概览占位误报为不存在。') });
  await page.goto('/data?view=sources');
  const guide = page.getByRole('region', { name: '模块功能与数据边界表格', exact: true });
  await expect(guide).toBeVisible();
  const modules = [
    ['综合盯盘', '/workbench?view=overview'], ['竞价观察', '/workbench?view=auction'],
    ['连板观察', '/workbench?view=ladder'], ['资金动向', '/workbench?view=funds'],
    ['每日复盘', '/research/reviews'], ['明日预案', '/research/tomorrow'],
    ['资讯中心', '/news'], ['数据管理', '/data?view=connections'], ['系统设置', '/system'],
  ];
  await expect(guide.getByRole('link')).toHaveText(modules.map(([name]) => name));
  for (const [name, href] of modules) await expect(guide.getByRole('link', { name, exact: true })).toHaveAttribute('href', href);
  await expect(guide.getByRole('row').filter({ has: page.getByRole('link', { name: '每日复盘', exact: true }) })).toContainText('本人笔记与版本');
  await expect(guide).not.toContainText('记录保存未开放');
  await expect(guide).not.toContainText('预案 / 快照未接入');
  await expect(page.getByTestId('summary-已配置')).toHaveText('1');
  for (const width of [1920, 1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1080 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await guide.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath(`module-guide-${width}.png`), fullPage: true, animations: 'disabled' });
  }
  await guide.getByRole('link', { name: '每日复盘', exact: true }).focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/research\/reviews$/);
  await expect(page.getByRole('navigation', { name: '每日复盘页面', exact: true })).toBeVisible();
  // The module entry now opens the market review. Saved versioned research
  // retains its explicit deep link and the original focus/date contract.
  await page.goto('/research/reviews#research-records');
  const records = page.locator('#research-records');
  await expect(page).toHaveURL(/\/research\/reviews#research-records$/);
  await expect(records).toBeFocused();
  await expect(records).toBeInViewport();
  await page.getByLabel('研究日期', { exact: true }).fill(day);
  await expect(page.getByRole('button', { name: `查看研究 ${title}`, exact: true })).toBeVisible();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1080 });
    const jump = page.getByRole('link', { name: '查看当日记录', exact: true });
    await jump.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath(`research-guide-${width}.png`), animations: 'disabled' });
    await jump.focus();
    await page.keyboard.press('Enter');
    await expect(records).toBeFocused();
    await expect(records).toBeInViewport();
    expect(new URL(page.url()).searchParams.get('date')).toBe(day);
    await page.screenshot({ path: info.outputPath(`record-jump-${width}.png`), animations: 'disabled' });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.reload();
  await expect(records).toBeFocused();
  await expect(records).toBeInViewport();
  await page.getByRole('button', { name: `查看研究 ${title}`, exact: true }).click();
  await expect(page.locator('.saved-research-content')).toContainText('保存的判断不应被概览占位误报为不存在。');
  await page.getByRole('button', { name: '收起', exact: true }).click();
  await page.route('**/api/v1/research-documents?**', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({
    error: { code: 'database_unavailable', message: '合成故障：研究记录暂不可读取。', details: [] },
    request_id: '00000000-0000-4000-8000-000000000005',
  }) }));
  await records.getByRole('button', { name: '刷新记录', exact: true }).click();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成故障：研究记录暂不可读取。' });
  await expect(notice).toBeVisible();
  await expect(page.getByRole('button', { name: `查看研究 ${title}`, exact: true })).toBeVisible();
  await expect(records.getByText('这一天还没有每日复盘记录', { exact: true })).toHaveCount(0);
  await page.setViewportSize({ width: 1440, height: 1080 });
  await records.scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath('record-read-error-1440.png'), animations: 'disabled' });
  await page.unroute('**/api/v1/research-documents?**');
  let releaseRetry!: () => void, retryStarted = false;
  const retryGate = new Promise<void>(resolve => { releaseRetry = resolve; });
  await page.route('**/api/v1/research-documents?**', async route => { retryStarted = true; await retryGate; await route.continue(); });
  await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect.poll(() => retryStarted).toBe(true);
  await expect(records.getByRole('button', { name: `查看研究 ${title}`, exact: true })).toBeVisible();
  releaseRetry();
  await expect(notice).toHaveCount(0);
  await page.unroute('**/api/v1/research-documents?**');
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(page.getByRole('button', { name: `查看研究 ${title}`, exact: true })).toBeVisible();
  await page.getByLabel('研究日期', { exact: true }).fill('2000-01-03');
  await page.getByRole('link', { name: '查看当日记录', exact: true }).click();
  await expect(records).toBeFocused();
  await expect(records.getByText('这一天还没有每日复盘记录', { exact: true })).toBeVisible();
  expect(new URL(page.url()).searchParams.get('date')).toBe('2000-01-03');
});


for (const status of [403, 404]) test(`authoritative ${status} hides previously displayed private research records`, async ({ page }) => {
  await login(page);
  const f = await fixture(page), title = '合成权限撤回复盘-' + f.key;
  await post(page, 'research-documents', { kind: 'review', source_trade_day: day,
    revision: reviewBody(f, title, '权限撤回后不可继续展示的合成内容。') });
  await page.goto('/research/reviews?date=' + day + '#research-records');
  const records = page.locator('#research-records');
  // The API fixture bypasses page invalidation; a same-document hash keeps the list instance.
  await records.getByRole('button', { name: '刷新记录', exact: true }).click();
  await expect(records.getByRole('button', { name: `查看研究 ${title}`, exact: true })).toBeVisible();
  await page.route('**/api/v1/research-documents?**', route => route.fulfill({ status,
    json: { error: { code: 'permission_denied', message: '合成读取权限已撤回', details: [] },
      request_id: '00000000-0000-4000-8000-000000000006' } }));
  await records.getByRole('button', { name: '刷新记录', exact: true }).click();
  await expect(records.getByRole('button', { name: `查看研究 ${title}`, exact: true })).toHaveCount(0);
  await expect(await openNotifications(page)).toContainText('权限');
  await expect(records.getByText('这一天还没有每日复盘记录', { exact: true })).toHaveCount(0);
});


test('a lost network retains the same dated records and a later successful empty response replaces them', async ({ page }) => {
  await login(page);
  const f = await fixture(page), title = '合成断网复盘-' + f.key;
  await post(page, 'research-documents', { kind: 'review', source_trade_day: day,
    revision: reviewBody(f, title, '仅用于验证网络恢复期间的同日期记录。') });
  await page.goto('/research/reviews?date=' + day + '#research-records');
  const records = page.locator('#research-records');
  // Read the new API fixture before testing retention of that same dated list.
  await records.getByRole('button', { name: '刷新记录', exact: true }).click();
  const row = records.getByRole('button', { name: `查看研究 ${title}`, exact: true });
  await expect(row).toBeVisible();
  await page.route('**/api/v1/research-documents?**', route => route.abort('connectionfailed'));
  await records.getByRole('button', { name: '刷新记录', exact: true }).click();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '连接暂时中断' });
  await expect(notice).toBeVisible();
  await expect(row).toBeVisible();
  await expect(records.getByText('这一天还没有每日复盘记录', { exact: true })).toHaveCount(0);
  await page.unroute('**/api/v1/research-documents?**');
  await page.route('**/api/v1/research-documents?**', route => route.fulfill({ status: 200,
    json: { data: { items: [], next_cursor: null } } }));
  await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(notice).toHaveCount(0);
  await expect(row).toHaveCount(0);
  await expect(records.getByText('这一天还没有每日复盘记录', { exact: true })).toBeVisible();
});
