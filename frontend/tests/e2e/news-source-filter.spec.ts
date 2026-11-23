import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import { signIn } from './researchTestHelpers';

const meta = { request_id: '00000000-0000-4000-8000-000000000001', app_mode: 'demo', generated_at: '2020-01-03T10:00:00Z', as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic isolated UI fixtures'] } };
const families = ['cninfo_announcements', 'ecb_news', 'fed_public'];
function response(family: string) {
  const index = families.indexOf(family);
  const id = '00000000-0000-4000-8000-00000000000' + (index + 2);
  return { meta, data: { items: [{ id, source_id: id, source_name: '合成来源 ' + family, family,
    received_at: '2020-01-03T10:00:00Z', published_at: '2020-01-02T10:00:00Z', content_hash: '1'.repeat(64),
    payload: { title: '合成条目 ' + family, summary: null, source_url: 'https://example.com/' + family, category: family === 'cninfo_announcements' ? '公告' : '海外' } }] } };
}

test('older connected feed is selected on the server with stable shell, URL, history and refresh', async ({ page }, info) => {
  await page.route('**/api/v1/market-news*', route => {
    const family = new URL(route.request().url()).searchParams.get('family') ?? 'cninfo_announcements';
    return route.fulfill({ json: response(family) });
  });
  await page.goto('/news?legacy=1'); await signIn(page);
  const region = page.getByRole('region', { name: '已采集资讯列表', exact: true });
  await expect(region).toContainText('合成条目 cninfo_announcements');
  await page.evaluate(() => { document.documentElement.dataset.newsDocument = 'kept'; });
  const documents: string[] = [];
  page.on('request', request => { if (request.isNavigationRequest() && request.frame() === page.mainFrame()) documents.push(request.url()); });
  const source = page.getByRole('combobox', { name: '资讯来源', exact: true });
  await page.getByRole('textbox', { name: '资讯关键词' }).fill('earlier-filter');
  await source.selectOption('ecb_news');
  await expect(page).toHaveURL(/family=ecb_news/);
  await expect(region).toContainText('合成条目 ecb_news');
  await expect(page.getByRole('textbox', { name: '资讯关键词' })).toHaveValue('');
  await expect(page.locator('.news-list')).toContainText('先按所选来源读取最近100条');
  expect(await page.evaluate(() => document.documentElement.dataset.newsDocument)).toBe('kept');
  await source.selectOption('fed_public'); await expect(region).toContainText('合成条目 fed_public');
  await page.goBack(); await expect(source).toHaveValue('ecb_news'); await expect(region).toContainText('合成条目 ecb_news');
  await page.goForward(); await expect(source).toHaveValue('fed_public'); await expect(region).toContainText('合成条目 fed_public');
  expect(documents).toEqual([]);
  await source.selectOption('ecb_news'); await expect(region).toContainText('合成条目 ecb_news');
  await page.reload(); await expect(source).toHaveValue('ecb_news'); await expect(region).toContainText('合成条目 ecb_news');
  await expect(page.getByRole('link', { name: '打开原始出处' })).toHaveAttribute('href', 'https://example.com/ecb_news');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('news-source-' + width + '.png'), animations: 'disabled' });
  }
});

test('source switch discards a late old response and preserves error, retry and empty states', async ({ page }) => {
  let releaseOld!: () => void; let oldArrived = false; let oldCompleted!: () => void;
  const oldGate = new Promise<void>(resolve => { releaseOld = resolve; });
  const oldDone = new Promise<void>(resolve => { oldCompleted = resolve; });
  let fail = false; let empty = false;
  await page.route('**/api/v1/market-news*', async route => {
    const family = new URL(route.request().url()).searchParams.get('family') ?? 'cninfo_announcements';
    if (family === 'ecb_news') { oldArrived = true; await oldGate; await route.fulfill({ json: response(family) }); oldCompleted(); return; }
    if (fail) return route.fulfill({ status: 503, json: { error: { message: '合成来源读取失败' } } });
    return route.fulfill({ json: empty ? { meta, data: { items: [] } } : response(family) });
  });
  await page.goto('/news?legacy=1'); await signIn(page);
  const source = page.getByRole('combobox', { name: '资讯来源', exact: true });
  const region = page.getByRole('region', { name: '已采集资讯列表', exact: true });
  await expect(region).toContainText('合成条目 cninfo_announcements');
  await source.selectOption('ecb_news'); await expect.poll(() => oldArrived).toBe(true);
  await source.selectOption('fed_public'); await expect(region).toContainText('合成条目 fed_public');
  releaseOld(); await oldDone; await page.waitForLoadState('networkidle');
  await expect(region).toContainText('合成条目 fed_public'); await expect(region).not.toContainText('合成条目 ecb_news');
  fail = true; await source.selectOption('cninfo_announcements');
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成来源读取失败' });
  await expect(notice).toBeVisible();
  // The failed new-source read removes the list entirely; a negative text
  // assertion on a non-existent region would keep waiting for that region.
  await expect(region).toHaveCount(0);
  fail = false; await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(notice).toHaveCount(0);
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(region).toContainText('合成条目 cninfo_announcements');
  empty = true; await source.selectOption('fed_public');
  await expect(page.getByText('暂无已采集资讯', { exact: true })).toBeVisible(); await expect(source).toHaveValue('fed_public');
});
