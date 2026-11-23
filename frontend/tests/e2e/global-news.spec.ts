import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const meta = { request_id: '00000000-0000-4000-8000-000000000001', app_mode: 'demo', generated_at: '2026-10-21T10:00:00Z', as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic isolated global news'] } };
const definitions = [
  ['hk_gov_finance', '合成香港财经', '合成香港', '中国与港澳', '财经'],
  ['fed_public', '合成美联储', '合成美联储', '美洲', '政策'],
  ['cninfo_announcements', '合成公司公告', '合成巨潮', '中国与港澳', '公告'],
  ['bbc_business', '合成BBC商业', 'BBC News', '欧洲', '财经'],
  ['bbc_world', '合成BBC全球', 'BBC News', '全球', '海外'],
  ['cnbc_finance', '合成CNBC金融', 'CNBC', '美洲', '财经'],
  ['cna_business', '合成CNA亚太', 'CNA', '亚太', '财经'],
  ['un_economy', '合成联合国经济', '联合国新闻', '全球', '财经'],
  ['boe_news', '合成英国政策', 'Bank of England', '欧洲', '政策'],
];
function row(definition: string[], i: number) {
  return { id: '00000000-0000-4000-8000-' + String(i + 1).padStart(12, '0'), source_id: '00000000-0000-4000-8000-' + String(i + 101).padStart(12, '0'), family: definition[0], source_name: definition[1], published_at: '2026-10-21T01:00:00Z', received_at: '2026-10-21T02:00:00Z', content_hash: '1'.repeat(64), payload: { title: definition[1] + '：全球市场合成条目', summary: '仅用于全球资讯浏览器验收，非真实新闻。', source_url: 'https://example.com/news/' + i, source_name: definition[2], category: definition[4] } };
}
async function setup(page: Page, state: { failed?: string; indexFailed?: boolean; onlyNotices?: boolean; added?: boolean } = {}, requests: string[] = []) {
  await page.route('**/api/v1/**', route => {
    const url = new URL(route.request().url());
    const active = state.added ? [...definitions, ['npr_business', '合成新增NPR', 'NPR', '美洲', '财经']] : definitions;
    if (url.pathname.endsWith('/session')) return route.fulfill({ json: { meta, data: { user: { id: '00000000-0000-4000-8000-000000000001', username: '合成验收', role: 'viewer' }, csrf_token: 'synthetic-test' } } });
    if (url.pathname.endsWith('/data-sources')) {
      if (state.indexFailed) return route.fulfill({ status: 503, json: { error: { message: '合成信源目录读取失败' } } });
      return route.fulfill({ json: { meta, data: {
        available_adapters: active.map(([family, name, publisher, region, category]) => ({ family, name, kind: 'news', news: { publisher, region, category } })),
        items: active.filter(([family]) => family !== 'boe_news').map(([family]) => ({ family, collection_state: 'ready', provider: { configured: true, enabled: true, entitlement_status: 'verified', rights: { display: 'allowed', cache: 'allowed' } } }))
      } } });
    }
    if (url.pathname.endsWith('/market-news')) {
      const family = url.searchParams.get('family') ?? ''; requests.push(family);
      if (state.failed === family) return route.fulfill({ status: 503, json: { error: { message: '合成某源失败' } } });
      const items = active.map(row).filter(item => !state.onlyNotices || item.payload.category === '公告');
      return route.fulfill({ json: { meta, data: { items: items.filter(item => !family || item.family === family) } } });
    }
    return route.fulfill({ json: { meta, data: { selection: 'indices', items: [] } } });
  });
}

test('global source discovery automatically includes new publishers and counts BBC channels as one publisher', async ({ page }) => {
  const state = { added: false }; const requests: string[] = [];
  await setup(page, state, requests); await page.goto('/news?legacy=1&view=daily');
  await expect(page.locator('.digest-statistics')).toContainText('7 篇资讯');
  await expect(page.locator('.digest-statistics')).toContainText('6 个来源');
  await expect(page.locator('.digest-columns')).not.toContainText('合成公司公告');
  await expect(page.locator('.digest-network > summary')).toContainText('7 家 · 8 个频道');
  expect(new Set(requests)).toEqual(new Set(definitions.filter(([family]) => family !== 'boe_news').map(([family]) => family)));
  await page.locator('.digest-network > summary').click();
  await expect(page.locator('.digest-network-grid')).toContainText('BBC News');
  await expect(page.locator('.digest-network-grid')).toContainText('亚太');
  await expect(page.locator('.digest-network-grid > div').filter({ hasText: '合成英国政策' })).toContainText('未启用');
  state.added = true; await page.getByRole('button', { name: '刷新报刊' }).click();
  await expect(page.locator('.digest-network > summary')).toContainText('8 家 · 9 个频道');
  await expect(page.locator('.digest-columns')).toContainText('合成新增NPR');
});

test('global headlines and company notices are separate URL-backed views and keep original links', async ({ page }) => {
  await setup(page); await page.goto('/news?legacy=1&view=daily');
  const scope = page.getByRole('radiogroup', { name: '日报内容范围' });
  await scope.getByRole('radio', { name: '公司公告', exact: true }).click();
  await expect(page).toHaveURL(/scope=announcements/);
  await expect(page.locator('.digest-statistics')).toContainText('1 篇资讯');
  await expect(page.locator('.digest-lead-story h3')).toContainText('合成公司公告');
  await scope.getByRole('radio', { name: '全部资讯', exact: true }).click();
  await expect(page.locator('.digest-statistics')).toContainText('8 篇资讯');
  await expect(page.locator('.digest-statistics')).toContainText('7 个来源');
  await page.reload(); await expect(scope.getByRole('radio', { name: '全部资讯', exact: true })).toHaveAttribute('aria-checked', 'true');
  await page.goBack(); await expect(scope.getByRole('radio', { name: '公司公告', exact: true })).toHaveAttribute('aria-checked', 'true');
  await expect(page.locator('.digest-lead-story').getByRole('link', { name: '阅读原文' })).toHaveAttribute('href', 'https://example.com/news/2');
});

test('source index errors and per-feed failures are visible, while notice-only cache stays out of headlines', async ({ page }) => {
  const state = { failed: 'bbc_business', indexFailed: false, onlyNotices: false };
  await setup(page, state); await page.goto('/news?legacy=1&view=daily');
  await expect(page.locator('.digest-partial')).toContainText('合成BBC商业暂时读取失败');
  await page.locator('.digest-network > summary').click();
  await expect(page.locator('.digest-network-grid > div').filter({ hasText: '合成BBC商业' })).toContainText('读取失败');
  state.failed = ''; state.indexFailed = true; await page.getByRole('button', { name: '刷新报刊' }).click();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成信源目录读取失败' });
  await expect(notice).toBeVisible();
  await expect(page.locator('.digest-statistics')).toBeVisible();
  state.indexFailed = false; state.onlyNotices = true; await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(notice).toHaveCount(0);
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(page.getByText('暂无可编排的财经资讯', { exact: true })).toBeVisible();
  await page.getByRole('radiogroup', { name: '日报内容范围' }).getByRole('radio', { name: '公司公告', exact: true }).click();
  await expect(page.locator('.digest-statistics')).toContainText('1 篇资讯');
});

test('global source directory and all news view controls fit desktop and phone', async ({ page }, info) => {
  await setup(page); await page.goto('/news?legacy=1&view=daily'); await expect(page.locator('.digest-statistics')).toBeVisible();
  await page.locator('.digest-network > summary').click();
  for (const width of [1920, 1440, 1024, 768, 760, 390, 320]) {
    await page.setViewportSize({ width, height: 1080 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'width ' + width).toBe(true);
    if ([1440, 390].includes(width)) await page.screenshot({ path: info.outputPath('global-news-directory-' + width + '.png'), fullPage: true });
  }
});
