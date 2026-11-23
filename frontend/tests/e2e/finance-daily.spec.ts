import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { NewsItem } from '../../src/generated/schema';

const meta = { request_id: '00000000-0000-4000-8000-000000000001', app_mode: 'demo', generated_at: '2026-10-21T10:00:00Z', as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic isolated UI fixtures'] } };
const sources = ['hk_gov_finance', 'ecb_news', 'fed_public', 'cninfo_announcements'];
function item(id: number, family: string, day: string | null, title: string, published: string | null = null): NewsItem {
  return { id: '00000000-0000-4000-8000-' + String(id).padStart(12, '0'), source_id: '00000000-0000-4000-8000-' + String(sources.indexOf(family) + 100).padStart(12, '0'),
    family, source_name: ({ hk_gov_finance: '合成香港财经来源', ecb_news: '合成欧洲央行来源', fed_public: '合成美联储来源', cninfo_announcements: '合成公司公告来源' } as Record<string, string>)[family],
    received_at: '2026-10-21T08:00:00Z', published_at: published, content_hash: '1'.repeat(64),
    payload: { title, summary: family === 'hk_gov_finance' ? null : '这是一段用于验证报刊版式的合成摘要，不是真实市场新闻或投资判断。', source_url: 'https://example.com/news/' + id, category: family === 'hk_gov_finance' ? '财经' : family === 'cninfo_announcements' ? '公告' : '海外', ...(day ? { published_day: day } : {}) } };
}
const fixtures = [
  item(1, sources[0], '2026-10-21', '合成财经头条：跨境金融合作与市场发展最新进展'),
  item(2, sources[0], '2026-10-20', '合成往期：金融市场政策交流'),
  item(3, sources[0], null, '合成未知日期，不能按接收时间放进日报'),
  item(4, sources[0], '2026-09-30', '合成跨月周报：九月末财经发布'),
  item(5, sources[0], '2026-10-01', '合成跨月周报：十月初财经发布'),
  item(6, sources[1], null, '合成欧洲央行：金融稳定与政策沟通', '2026-10-20T16:30:00Z'),
  item(7, sources[1], null, '合成欧洲央行：上一日政策会议', '2026-10-20T15:30:00Z'),
  item(8, sources[2], null, '合成美联储：货币政策与经济观察', '2026-10-21T01:00:00Z'),
  item(9, sources[2], null, '合成美联储：往期经济发布', '2026-10-20T10:00:00Z'),
  item(10, sources[3], '2026-10-21', '合成上市公司：董事会决议及经营事项公告'),
];
type Options = { fail?: Set<string>; empty?: boolean; items?: NewsItem[]; requests?: string[] };
async function workspace(page: Page, options: Options = {}) {
  page.on('pageerror', error => console.error('Synthetic finance fixture page error:', error.message));
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/session')) return route.fulfill({ json: { meta, data: { user: { id: '00000000-0000-4000-8000-000000000001', username: '合成测试账号', role: 'viewer' }, csrf_token: 'synthetic-local-test' } } });
    if (url.pathname.endsWith('/ai/settings')) return route.fulfill({ json: { meta, data: { enabled: false, default_connection_id: null, revision: 1, features: [] } } });
    if (url.pathname.endsWith('/data-sources')) return route.fulfill({ json: { meta, data: {
      available_adapters: sources.map(family => ({ family, name: ({ hk_gov_finance: '香港政府财经', ecb_news: '欧洲央行资讯', fed_public: '美联储资讯', cninfo_announcements: '巨潮公司公告' } as Record<string, string>)[family], kind: 'news', news: { publisher: family, region: '全球', category: family === 'cninfo_announcements' ? '公告' : '财经' } })),
      items: sources.map(family => ({ family, collection_state: 'ready', provider: { configured: true, enabled: true, entitlement_status: 'verified', rights: { display: 'allowed', cache: 'allowed' } } }))
    } } });
    if (url.pathname.endsWith('/market-news')) {
      const family = url.searchParams.get('family') ?? '';
      options.requests?.push(family);
      if (options.fail?.has(family)) return route.fulfill({ status: 503, json: { error: { message: '合成来源读取失败' } } });
      const rows = options.empty ? [] : options.items ?? fixtures;
      return route.fulfill({ json: { meta, data: { items: rows.filter(row => !family || row.family === family) } } });
    }
    if (url.pathname.endsWith('/market-quotes')) return route.fulfill({ json: { meta, data: { selection: 'indices', items: [] } } });
    return route.fulfill({ json: { meta, data: { items: [] } } });
  });
}
const paper = (page: Page) => page.getByRole('region', { name: '财经报刊', exact: true });
const period = (page: Page, name: string) => page.getByRole('radiogroup', { name: '报刊周期', exact: true }).getByRole('radio', { name, exact: true });

test('finance edition uses four independent source windows, China publication dates, originals and accurate counts', async ({ page }) => {
  const requests: string[] = [];
  const duplicate = { ...fixtures[0], id: '00000000-0000-4000-8000-000000000099', received_at: '2026-10-20T08:00:00Z' };
  await workspace(page, { requests, items: [...fixtures, duplicate] });
  await page.goto('/news?legacy=1&view=daily&scope=all');
  await expect(paper(page).getByRole('heading', { name: '财经日报', exact: true })).toBeVisible();
  await expect(page.locator('.digest-statistics')).toContainText('4 篇资讯');
  await expect(page.locator('.digest-statistics')).toContainText('4 个来源');
  await expect(page.locator('.digest-statistics')).toContainText('3 个主题');
  expect(new Set(requests)).toEqual(new Set(sources));
  expect(requests).not.toContain('');
  await expect(page.locator('.digest-lead-story h3')).toHaveText(fixtures[0].payload.title + '↗');
  await expect(page.locator('.digest-lead-story')).not.toContainText('这是一段');
  await expect(page.locator('.digest-lead-story')).toContainText('2026-10-21 · 仅日期');
  await expect(page.locator('.digest-article').filter({ hasText: fixtures[5].payload.title })).toContainText('2026/10/21 00:30:00');
  await expect(page.locator('.digest-colophon')).toContainText('1 条发布时间未知');
  await expect(page.locator('.digest-columns')).not.toContainText(fixtures[2].payload.title);
  await expect(page.locator('.digest-lead-story').getByRole('link', { name: '阅读原文' })).toHaveAttribute('href', fixtures[0].payload.source_url);
  await expect(page.locator('.digest-lead-story').getByRole('link', { name: '阅读原文' })).toHaveAttribute('rel', 'noopener noreferrer');
  await page.locator('.digest-lead > details').getByText('来源记录', { exact: true }).click();
  await expect(page.locator('.digest-lead > details')).toContainText(fixtures[0].content_hash);
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--blue').trim())).toBe('#0963f6');
});

test('daily, weekly and monthly dates survive URL history and refresh, including cross-month and cross-year weeks', async ({ page }) => {
  await workspace(page);
  await page.goto('/news?legacy=1&view=daily&scope=all&date=2026-10-20');
  await expect(page.locator('.digest-statistics')).toContainText('3 篇资讯');
  await page.getByRole('button', { name: '2026-10-21 有资讯', exact: true }).click();
  await expect(page).toHaveURL(/date=2026-10-21/);
  await expect(page.locator('.digest-statistics')).toContainText('4 篇资讯');
  await page.goBack(); await expect(page.locator('.digest-statistics')).toContainText('3 篇资讯');
  await page.goForward(); await expect(page.locator('.digest-statistics')).toContainText('4 篇资讯');
  await period(page, '周报').click();
  await expect(page).toHaveURL(/period=week/); await expect(page).toHaveURL(/date=2026-10-19/);
  await expect(page.locator('.digest-statistics')).toContainText('7 篇资讯');
  await page.getByLabel('报刊日期', { exact: true }).fill('2026-10-01');
  await expect(page).toHaveURL(/date=2026-09-28/);
  await expect(page.locator('.digest-dateline')).toContainText('2026-09-28 — 2026-10-04');
  await expect(page.locator('.digest-statistics')).toContainText('2 篇资讯');
  await period(page, '月报').click();
  await expect(page).toHaveURL(/date=2026-09-01/);
  await expect(page.locator('.digest-statistics')).toContainText('1 篇资讯');
  await page.reload(); await expect(period(page, '月报')).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByLabel('报刊日期', { exact: true })).toHaveValue('2026-09-01');
  await page.getByLabel('报刊日期', { exact: true }).fill('2026-10-01');
  await expect(page.locator('.digest-statistics')).toContainText('8 篇资讯');
  await workspace(page, { items: [item(20, sources[0], '2026-12-31', '合成跨年末'), item(21, sources[0], '2027-01-01', '合成跨年初')] });
  await page.goto('/news?legacy=1&view=daily&scope=all&period=week&date=2027-01-01');
  await expect(page.locator('.digest-dateline')).toContainText('2026-12-28 — 2027-01-03');
  await expect(page.locator('.digest-statistics')).toContainText('2 篇资讯');
});

test('sidebar, period keyboard controls, in-page highlights and legacy feed keep the document and original selection', async ({ page }) => {
  await workspace(page);
  await page.goto('/news?legacy=1&view=feed');
  await expect(page.getByRole('region', { name: '已采集资讯列表', exact: true })).toBeVisible();
  await page.evaluate(() => { document.documentElement.dataset.digestDocument = 'retained'; });
  const documents: string[] = [];
  page.on('request', request => { if (request.isNavigationRequest() && request.frame() === page.mainFrame()) documents.push(request.url()); });
  await page.locator('#news-children').getByRole('link', { name: '财经日报', exact: true }).click();
  await expect(page.locator('#news-children a[aria-current="page"]')).toHaveText('财经日报');
  await expect(page.locator('.digest-statistics')).toContainText('3 篇资讯');
  await page.locator('.digest-highlights a').first().click();
  await expect(page).toHaveURL(/#digest-article-/);
  const hash = new URL(page.url()).hash;
  await expect(page.locator(hash)).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.dataset.digestDocument)).toBe('retained');
  await period(page, '日报').focus(); await period(page, '日报').press('ArrowRight');
  await expect(period(page, '周报')).toHaveAttribute('aria-checked', 'true');
  await page.getByRole('radiogroup', { name: '资讯工作视图' }).getByRole('radio', { name: '实时列表', exact: true }).click();
  await expect(page.getByRole('region', { name: '已采集资讯列表', exact: true })).toBeVisible();
  await page.getByRole('combobox', { name: '资讯来源', exact: true }).selectOption('ecb_news');
  await expect(page.locator('.news-detail')).toContainText(fixtures[5].payload.title);
  await expect(page.locator('.news-detail').getByRole('link', { name: '打开原始出处' })).toHaveAttribute('href', fixtures[5].payload.source_url);
  expect(documents).toEqual([]);
});

test('partial failures stay visible, retry recovers, total failure and empty or absent dates are honest', async ({ page }) => {
  const options: Options = { fail: new Set(['ecb_news']) };
  await workspace(page, options); await page.goto('/news?legacy=1&view=daily&scope=all');
  await expect(page.locator('.digest-partial')).toContainText('欧洲央行资讯暂时读取失败');
  await expect(page.locator('.digest-statistics')).toContainText('3 篇资讯');
  options.fail!.clear(); await page.locator('.digest-partial').getByRole('button', { name: '重试', exact: true }).click();
  await expect(page.locator('.digest-partial')).toHaveCount(0); await expect(page.locator('.digest-statistics')).toContainText('4 篇资讯');
  options.fail = new Set(sources); await page.getByRole('button', { name: '刷新报刊' }).click();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成来源读取失败' });
  await expect(notice).toBeVisible();
  await expect(page.locator('.digest-statistics')).toContainText('4 篇资讯');
  options.fail.clear(); options.empty = true; await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(notice).toHaveCount(0);
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(page.getByText('暂无可编排的财经资讯', { exact: true })).toBeVisible();
  options.empty = false; await page.goto('/news?legacy=1&view=daily&scope=all&date=2026-10-19');
  await expect(page.getByText('本期暂无已采集资讯', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '阅读最近一期', exact: true }).click();
  await expect(page.locator('.digest-statistics')).toContainText('4 篇资讯');
  await page.goto('/news?legacy=1&view=daily&scope=all&date=2026-02-30');
  await expect(page.locator('.digest-statistics')).toContainText('4 篇资讯');
});

test('financial paper fits desktop, tablet and phone with readable controls and no overflow', async ({ page }, info) => {
  await workspace(page); await page.goto('/news?legacy=1&view=daily&scope=all');
  await expect(page.locator('.digest-statistics')).toContainText('4 篇资讯');
  for (const width of [1920, 1440, 1190, 1024, 768, 760, 390, 320]) {
    await page.setViewportSize({ width, height: width < 760 ? 844 : 1080 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'width ' + width).toBe(true);
    const controls = await page.getByLabel('报刊日期', { exact: true }).boundingBox();
    const tabs = await page.getByRole('radiogroup', { name: '报刊周期' }).boundingBox();
    if (width <= 760) expect(tabs!.x + tabs!.width).toBeLessThanOrEqual(controls!.x);
    if ([1920, 1440, 390].includes(width)) await page.screenshot({ path: info.outputPath('finance-daily-' + width + '.png'), animations: 'disabled', fullPage: true });
  }
});
