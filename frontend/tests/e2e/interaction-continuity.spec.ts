import { expect } from '@playwright/test';
import { test, openNotifications } from './isolated-browser.fixtures';
import type { Page, Route } from '@playwright/test';
import type { SourceIndex } from '../../src/generated/schema';
import type { ConnectionIndex } from '../../src/features/data-center-live/api';
import { terminalFixture, stockA, stockB, sectorA, sectorB, terminalDay } from './terminalTestFixtures';
import { day, days, blank } from './reviewTestFixtures';

async function fixture(page: Page) {
  const state = { marketReads: 0, marketViews: [] as string[], details: [] as string[], memberships: [] as string[], failDetail: false, failNews: false, newsDelay: 0, errors: [] as string[] };
  const connections: ConnectionIndex = { items: [], credential_store_ready: true, templates: [
    { id: 'custom', name: '自定义模型', kind: 'model', endpoint: '', credential_required: true },
    { id: 'synthetic_market', name: '合成行情配置', kind: 'market', endpoint: 'https://example.invalid/fixture-data', credential_required: false, source_families: [] },
  ] };
  const sources: SourceIndex = { items: [], available_adapters: [], collection_enabled: false,
    today_request_count: 0, today_run_count: 0, today_cost_cny: null, statistics_timezone: 'Asia/Shanghai' };
  page.on('pageerror', error => state.errors.push(error.message));
  await page.route('**/api/v1/**', async route => {
    const u = new URL(route.request().url()); const p = u.pathname;
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'demo' } } });
    if (p.endsWith('/session')) return ok({ user: { id: 'continuity-owner', username: 'synthetic', role: 'admin' }, csrf_token: 'synthetic-only' });
    if (p.endsWith('/workbench-view')) {
      state.marketReads++; state.marketViews.push(u.searchParams.get('view') ?? 'overview');
      const data = terminalFixture(u.searchParams), stamp = `${terminalDay}T15:05:00+08:00`;
      return ok({ ...data, detail_codes: [stockA, stockB, '999993.SZ'], detail_revision: 'synthetic-r1',
        intraday_ladder: { trade_day: terminalDay, as_of_time: stamp, phase: 'closed', sample_count: data.stocks.length,
          sealed_count: data.stocks.length, stale_count: 0, scope: '合成浏览器梯队，不是真实行情',
          rows: data.stocks.map(stock => ({ code: stock.code, name: stock.name, target_height: stock.previous_height,
            status: 'sealed', freshness: 'current', pct_change: stock.pct_change, source_time: stamp })) } });
    }
    if (p.endsWith('/sector-membership')) {
      state.memberships.push(u.search);
      const data = terminalFixture(u.searchParams), members = u.searchParams.get('sector') === sectorB ? [data.stocks[1]] : data.stocks;
      return ok({ trade_day: terminalDay, sector_code: u.searchParams.get('sector'), state: 'available',
        members_trade_day: terminalDay, member_count: members.length, truncated: false,
        members: members.map(({ code, name }) => ({ code, name })) });
    }
    if (p.endsWith('/ths-funds')) {
      const stamp = `${terminalDay}T15:05:00+08:00`;
      const rows = u.searchParams.get('category') === 'industry' ? [{ code: '881001', name: '合成资金甲',
        net_cny: '0', pct_change: '0', company_count: '2', received_at: stamp }] : [];
      return ok({ sampled_day: terminalDay, status: 'available', received_at: stamp, complete: true,
        missing_pages: [], rejected_rows: 0, last_error: null, rows,
        history: [{ received_at: stamp, points: rows.map(row => ({ code: row.code, net_cny: row.net_cny, received_at: stamp })) }] });
    }
    if (p.endsWith('/workbench-details')) {
      state.details.push(u.search);
      await new Promise(r => setTimeout(r, 250));
      if (state.failDetail) return route.fulfill({ status: 503, json: { error: { message: '合成详情错误' } } });
      const base = terminalFixture(u.searchParams); const codes = (u.searchParams.get('codes') || '').split(',');
      return ok({ trade_day: terminalDay, revision: 'synthetic-r1', stocks: codes.filter(Boolean).map(code => ({ ...(base.stocks.find(s => s.code === code) ?? base.stocks[0]), code })), auction_days: base.auction_days, events: [], seals: {}, sector_code: u.searchParams.get('sector'), members: u.searchParams.get('sector') === sectorB ? [base.stocks[1]] : base.stocks, members_trade_day: terminalDay });
    }
    if (p.endsWith('/day')) return ok(day(days[1]));
    if (p.endsWith('/series')) return ok({ ...day(days[1]), trade_days: [], records: [] });
    if (p.endsWith('/notes')) return ok({ trade_day: days[1], content: blank(), head_id: null, writable: true, revision: null });
    if (p.endsWith('/lhb')) return ok({ records: [], history_dates: [], unmatched_seat_records: [] });
    if (p.endsWith('/integrations')) return ok(connections);
    // The advanced market editor also mounts SourceRuntime, even while its
    // details are collapsed. Its source index must follow that API contract.
    if (p.endsWith('/data-sources')) return ok(sources);
    if (p.endsWith('/data-center/capabilities')) return ok({ revision: 1, policy: 'free_first', providers: [], capabilities: [] });
    if (p.endsWith('/ai/settings')) return ok({ enabled: false, revision: 1, features: [] });
    if (p.endsWith('/data-center/overview')) return ok({ services: [], tasks: [], issues: [], services_count: 0, running_tasks: 0, today_requests: 0, pending_issues: 0 });
    if (p.endsWith('/news/items')) {
      await new Promise(r => setTimeout(r, state.newsDelay));
      if (state.failNews) return route.fulfill({ status: 503, json: { error: { message: '合成资讯错误' } } });
      return ok({ items: [], total: 0, next_cursor: null, sources: [] });
    }
    return ok({ items: [], next_cursor: null });
  });
  await page.route('**/health/ready', route => route.fulfill({ json: { status: 'ok' } }));
  return state;
}
async function open(page: Page, path = '/workbench') {
  await page.goto(path);
  await expect(page.locator(path === '/news' ? '.nc-center' : '.ty-terminal')).toBeVisible({ timeout: 15000 });
  if (path !== '/news') { await expect(page.locator('.ty-terminal')).toHaveAttribute('aria-busy', 'false'); await expect(page.locator('.workspace-preparation-status')).toHaveCount(0); }
  await expect(page.getByRole('dialog', { name: '正在准备你的工作空间' })).toBeHidden({ timeout: 15000 });
}

test('stock and sector clicks retain market panels and only load scoped details', async ({ page }, info) => {
  const f = await fixture(page); await open(page);
  // The background queue starts after mount. Establish its actual four-view
  // baseline before attributing any request to a stock or sector click.
  await expect.poll(() => [...new Set(f.marketViews)].sort()).toEqual(['auction', 'funds', 'ladder', 'overview']);
  const base = f.marketReads;
  const market = page.getByRole('region', { name: '市场概览' });
  const before = await market.innerText();
  const tracking = page.getByRole('region', { name: '板块追踪', exact: true });
  await tracking.getByRole('button', { name: '追踪板块：合成题材甲', exact: true }).click();
  await page.getByRole('listbox', { name: '可追踪板块', exact: true }).getByRole('option', { name: /^合成题材乙/ }).click();
  await expect(page).toHaveURL(new RegExp(encodeURIComponent(sectorB)));
  await expect.poll(() => f.memberships.some(q => new URLSearchParams(q).get('sector') === sectorB)).toBe(true);
  expect(await market.innerText()).toBe(before);
  await expect(page.locator('.ty-loading-strip')).toHaveCount(0);
  await page.locator('.ty-mini-ladder').getByRole('button', { name: /^合成乙(?:\s|$)/ }).click();
  await expect(page).toHaveURL(new RegExp(stockB.replace('.', '\\.')));
  await expect(page.locator('.ty-selected')).toContainText('合成乙');
  await expect(tracking.getByRole('button', { name: '追踪板块：合成题材乙', exact: true })).toBeVisible();
  expect(await market.innerText()).toBe(before);
  expect(f.marketReads).toBe(base);
  await page.screenshot({ path: info.outputPath('点选后行情保持.png') });
  await expect.poll(() => f.details.some(q => q.includes('999993.SZ'))).toBeTruthy();
  expect(f.errors).toEqual([]);
});

test('rapid stock selection and back navigation do not overwrite the final identity', async ({ page }) => {
  const f = await fixture(page); await open(page);
  await page.locator('.ty-mini-ladder').getByRole('button', { name: /^合成乙(?:\s|$)/ }).click();
  await page.locator('.ty-mini-ladder').getByRole('button', { name: /^合成甲(?:\s|$)/ }).click();
  await expect(page.locator('.ty-selected')).toContainText('合成甲');
  await expect.poll(() => f.details.length).toBeGreaterThan(0);
  await page.goBack();
  await expect(page.locator('.ty-selected')).toContainText('合成乙');
  await expect(page.locator('.ty-loading-strip')).toHaveCount(0);
  expect(f.errors).toEqual([]);
});

test('preparation is not remounted across shells and the connection draft survives monitor tabs', async ({ page }) => {
  const f = await fixture(page); await open(page);
  await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: '数据管理' }).click();
  await expect(page.getByRole('dialog', { name: '正在准备你的工作空间' })).toHaveCount(0);
  await page.getByRole('button', { name: '管理其他已登记来源', exact: true }).click();
  const advanced = page.locator('.dc-registered-data');
  await expect(advanced).toHaveJSProperty('open', true);
  const editor = advanced.locator('.dc-editor');
  await expect(editor.getByLabel('接入类型', { exact: true })).toHaveValue('market');
  await expect(editor.getByRole('combobox', { name: '供应商模板', exact: true })).toHaveValue('synthetic_market');
  const input = editor.getByLabel('服务名称', { exact: true });
  await input.fill('尚未保存的合成配置');
  await page.getByRole('button', { name: '运行监控', exact: true }).click();
  await page.getByRole('button', { name: '数据来源', exact: true }).click();
  await expect(input).toHaveValue('尚未保存的合成配置');
  await expect(page.getByRole('dialog', { name: '正在准备你的工作空间' })).toHaveCount(0);
  expect(f.errors).toEqual([]);
});

test('news refresh keeps filters available and failed new query cannot relabel old data', async ({ page }) => {
  const f = await fixture(page); await open(page, '/news');
  f.newsDelay = 900;
  await page.getByRole('button', { name: '刷新已存资讯' }).click();
  await expect(page.locator('.nc-loading')).toHaveCount(0);
  await expect(page.getByTestId('news-center')).toBeVisible();
  await expect(page.getByRole('button', { name: '刷新已存资讯' })).toBeVisible();
  f.failNews = true;
  await page.getByRole('button', { name: '海外', exact: true }).click();
  await expect(await openNotifications(page)).toContainText('合成资讯错误');
  await expect(page.locator('.nc-focus')).toHaveCount(0);
  expect(f.errors).toEqual([]);
});

test('detail failure and retry leave other panels intact', async ({ page }) => {
  const f = await fixture(page); f.failDetail = true; await open(page);
  await expect(await openNotifications(page)).toContainText('合成详情错误');
  await expect(page.getByRole('region', { name: '板块追踪', exact: true })).toContainText('合成题材甲');
  await page.getByRole('navigation', { name: '工作台页面', exact: true }).getByRole('link', { name: '资金动向', exact: true }).click();
  const notifications = await openNotifications(page);
  const detailFailure = notifications.getByRole('listitem').filter({ hasText: '个股详情' }).filter({ hasText: '合成详情错误' });
  await expect(detailFailure).toBeVisible();
  await expect(page.locator('.ty-funds-ranking')).toContainText('合成资金甲');
  await expect(page.locator('.ty-loading-strip')).toHaveCount(0);
  // Keep the read failed until the actual retry has been clicked. Automatic
  // detail preparation may otherwise recover and remove the action first.
  let releaseRetry!: () => void, retryStarted = false;
  const retryGate = new Promise<void>(resolve => { releaseRetry = resolve; });
  const holdRetry = async (route: Route) => { retryStarted = true; await retryGate; await route.fallback(); };
  await page.route('**/api/v1/workbench-details?**', holdRetry);
  try {
    await detailFailure.getByRole('button', { name: '重试详情', exact: true }).click();
    await expect.poll(() => retryStarted).toBe(true);
    f.failDetail = false;
  } finally {
    releaseRetry();
  }
  await expect(detailFailure).toHaveCount(0);
  await page.unroute('**/api/v1/workbench-details?**', holdRetry);
  await expect(page.locator('.ty-members-panel').getByRole('alert')).toHaveCount(0);
  await expect(page.locator('.ty-funds-ranking')).toContainText('合成资金甲');
  // THS has no member mapping in this fixture; a recovered stock detail must
  // not fill its source-specific member list with the other market's members.
  await expect(page.locator('.ty-members-panel .ty-stock-name')).toHaveCount(0);
  await expect(page.locator('.ty-members-panel')).toContainText('同花顺成员映射与个股资金尚缺');
  expect(f.errors).toEqual([]);
});
