import { expect, type Page } from '@playwright/test';
import { test } from './isolated-browser.fixtures';
import { terminalDay, terminalDays, terminalFixture, stockA, stockB } from './terminalTestFixtures';
import { ladderFixture } from './ladderTestFixtures';

// Every endpoint and identity in this file is synthetic. The isolation fixture
// also blocks any API not explicitly handled here and any off-origin request.
const cutoff = `${terminalDay}T10:30:00+08:00`;
const theme = '合成同花顺完整题材：先进半导体设备与国产高端精密制造';
const sectors = Array.from({ length: 1000 }, (_, index) => ({
  code: `SYN-${String(index).padStart(4, '0')}`, name: `合成板块${String(index).padStart(4, '0')}`,
}));
const current = sectors[500];
const instrumentA = { id: '10000000-0000-4000-8000-000000000011', symbol: '999991', exchange: 'SZSE', name: '合成搜索甲', asset_type: 'stock' };
const instrumentB = { id: '10000000-0000-4000-8000-000000000012', symbol: '999992', exchange: 'SSE', name: '合成搜索乙', asset_type: 'stock' };
const definition = { conditions: [{ field: 'pct_change', operator: 'gte', value: '0' }], sort_field: 'amount_cny', sort_direction: 'desc' };

async function setup(page: Page, options: { emptySectors?: boolean; holdSearch?: Promise<void> } = {}) {
  const state = { errors: [] as string[], reads: [] as string[], writes: [] as string[], modules: [] as string[] };
  page.on('pageerror', error => state.errors.push(error.message));
  page.on('request', request => { if (request.resourceType() === 'script') state.modules.push(new URL(request.url()).pathname); });
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), async () => { await page.keyboard.press('Escape'); });
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'demo', request_id: 'synthetic-ux-interactions',
      generated_at: `${terminalDay}T15:05:00+08:00`, quality: { status: 'partial', reasons: ['全拦截合成测试，并非真实行情'] } } } });
    if (request.method() === 'GET') state.reads.push(path + url.search); else state.writes.push(path);
    if (path.endsWith('/session')) return ok({ user: { id: 'synthetic-ux-interactions', username: 'synthetic', role: 'admin' }, csrf_token: 'synthetic-only' });
    if (path.endsWith('/market-session')) return ok({ server_time: `${terminalDay}T15:05:00+08:00`, trade_day: terminalDay,
      phase: 'closed', is_trading_day: true, automatic_live: false, current_day_active: false, next_transition_at: null, poll_seconds: 60 });
    const data = terminalFixture(url.searchParams);
    data.stocks[0].ths_themes = [theme]; data.stocks[0].pct_change = -1;
    data.stocks[1].ths_themes = ['合成触屏完整题材']; data.stocks[1].pct_change = -2;
    data.sectors = options.emptySectors ? [] : sectors.map((sector, index) => ({ ...data.sectors[0], ...sector, rank: index + 1 }));
    data.selected_sector_code = options.emptySectors ? null : url.searchParams.get('sector') || current.code;
    if (path.endsWith('/workbench-view')) return ok({ ...data, detail_codes: [stockA, stockB], detail_revision: 'synthetic-ux-v1' });
    if (path.endsWith('/ladder-observation')) return ok(ladderFixture());
    if (path.endsWith('/workbench-details')) return ok({ trade_day: data.trade_day, revision: 'synthetic-ux-v1', stocks: data.stocks,
      auction_days: data.auction_days, events: [], seals: {}, sector_code: url.searchParams.get('sector'), members: data.stocks,
      members_trade_day: data.trade_day, tracking_members: data.stocks, tracking_member_count: 2, tracking_members_truncated: false, funds_history: [] });
    if (path.endsWith('/sector-membership')) return ok({ trade_day: data.trade_day, sector_code: url.searchParams.get('sector'),
      members_trade_day: data.trade_day, state: 'available', members: data.stocks.map(stock => ({ code: stock.code, name: stock.name })), member_count: 2, truncated: false });
    if (path.endsWith('/instruments')) { await options.holdSearch; return ok({ items: [instrumentA, instrumentB], next_cursor: null }); }
    if (path.endsWith('/auction-support')) return ok({ state: 'available', trade_day: terminalDay, previous_trade_day: terminalDays[1],
      rows: [], rankings: {}, gaps: [], coverage: [], down_days: [terminalDay, terminalDays[1]].map(day => ({ trade_day: day, day,
        book_state: 'available', book_coverage: 1, book_expected: 1, latest_at: `${day}T09:25:00+08:00`, source_name: '合成竞价卖一盘口',
        rows: [{ code: stockB, name: '合成跌停乙', price_basis: 'daily_close', pct_change: -10,
          node_amounts: { '09:15': 1000, '09:20': 2000, '09:25': 3000 }, current_seal_cny: null }] })) });
    if (path.endsWith('/screening/catalogue')) return ok({ engine_version: 'synthetic-v1', interval_seconds: 60, scope: '全拦截合成样本', boundaries: ['缺失不补零'],
      metrics: [{ field: 'pct_change', label: '涨跌幅', unit: '%', description: '合成样本' }, { field: 'amount_cny', label: '成交额', unit: '元', description: '合成样本' }] });
    if (path.endsWith('/screening/preview')) return ok({ id: null, rule_id: null, rule_name: null, rule_version: null, trade_day: terminalDay,
      as_of_time: cutoff, mode: 'observed', counts: { total: 1, matched: 1, not_matched: 0, insufficient: 0 }, offset: 0, limit: 100,
      has_more: false, event_count: 0, input_hash: 'synthetic', engine_version: 'synthetic', data_revision: 'synthetic', created_at: cutoff,
      definition, scope: 'market', source_refs: [], reasons: ['全拦截合成样本'], items: [{ code: stockB, name: '合成选股结果', instrument_id: null,
        state: 'matched', source_time: cutoff, received_at: cutoff, metrics: { pct_change: '0', amount_cny: '1000' }, checks: [] }] });
    if (path.endsWith('/ai/settings')) return ok({ enabled: false, features: [] });
    if (path.endsWith('/workbench-updates')) return route.fulfill({ contentType: 'text/event-stream', body: ': synthetic\n\n' });
    return ok({ items: [], next_cursor: null });
  });
  return state;
}

async function openMonitor(page: Page) {
  await page.goto(`/workbench?workspace=monitor&trade_day=${terminalDay}&as_of_time=${encodeURIComponent(cutoff)}&sector=${current.code}`);
  await expect(page.locator('#ty-sector-tracking')).toBeVisible();
  return page.getByRole('button', { name: `追踪板块：${current.name}`, exact: true });
}

function expectContext(page: Page) {
  const query = new URL(page.url()).searchParams;
  expect(query.get('trade_day')).toBe(terminalDay); expect(query.get('as_of_time')).toBe(cutoff);
}
const moduleLoaded = (paths: string[], name: string) => paths.some(path => new RegExp('/' + name + '(?:[.-])').test(path));

test('one thousand sectors support current choice, name and code search without changing historical context', async ({ page }, info) => {
  const state = await setup(page), trigger = await openMonitor(page);
  await trigger.click(); const input = page.getByRole('combobox', { name: '搜索追踪板块名称或代码' });
  const options = page.getByRole('listbox', { name: '可追踪板块' }).getByRole('option');
  await expect(input).toBeFocused(); await expect(options.filter({ hasText: '已选' })).toContainText(current.name);
  await expect(options).toHaveCount(1000);
  await input.fill('合成板块0999'); await expect(options).toHaveCount(1);
  await input.fill('SYN-0998'); await expect(options).toContainText('合成板块0998');
  await input.press('Enter'); await expect(page.getByRole('listbox')).toBeHidden();
  const next = page.getByRole('button', { name: '追踪板块：合成板块0998', exact: true });
  await expect(next).toBeFocused(); expectContext(page); expect(new URL(page.url()).searchParams.get('sector')).toBe('SYN-0998');
  await next.click(); await input.fill('不存在的合成板块');
  await expect(options).toHaveCount(0); await expect(page.getByText('没有匹配的板块', { exact: true })).toBeVisible();
  await input.press('Enter'); expect(new URL(page.url()).searchParams.get('sector')).toBe('SYN-0998');
  await input.press('Escape'); await expect(next).toBeFocused();
  await page.screenshot({ path: info.outputPath('synthetic-sector-search-desktop.png') });
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('sector picker supports arrows, Home, End, Escape and natural Tab focus order', async ({ page }) => {
  const state = await setup(page), trigger = await openMonitor(page), input = page.getByRole('combobox', { name: '搜索追踪板块名称或代码' });
  const options = page.getByRole('listbox', { name: '可追踪板块' }).getByRole('option');
  await trigger.focus(); await trigger.press('ArrowDown'); await input.press('Home');
  await expect(options.first()).toHaveAttribute('data-active', 'true');
  await input.press('ArrowUp'); await expect(options.last()).toHaveAttribute('data-active', 'true');
  await input.press('ArrowDown'); await expect(options.first()).toHaveAttribute('data-active', 'true');
  await input.press('End'); await expect(options.last()).toHaveAttribute('data-active', 'true');
  await input.press('Escape'); await expect(trigger).toBeFocused();
  await trigger.press('Enter'); await input.press('Tab');
  await expect(page.locator('#ty-sector-tracking').getByRole('link', { name: '板块研究', exact: true })).toBeFocused();
  await expect(page.getByRole('listbox')).toBeHidden();
  await trigger.focus(); await trigger.press('Enter'); await input.press('Shift+Tab');
  await expect(page.getByRole('listbox')).toBeHidden(); await expect(trigger).not.toBeFocused();
  expect(await page.evaluate(() => document.activeElement?.tagName)).not.toBe('BODY');
  expectContext(page); expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('an empty sector source offers no false choices and no mutation', async ({ page }) => {
  const state = await setup(page, { emptySectors: true }); await openMonitor(page);
  await expect(page.getByRole('button', { name: '追踪板块：暂无可选板块', exact: true })).toBeDisabled();
  await expect(page.getByRole('listbox')).toHaveCount(0); expectContext(page);
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test.describe('touch operation without hover', () => {
  test.use({ hasTouch: true, viewport: { width: 390, height: 844 } });
  test('sector selection and full theme disclosure remain operable at 390 pixels', async ({ page }, info) => {
    const state = await setup(page), trigger = await openMonitor(page); await trigger.tap();
    const input = page.getByRole('combobox', { name: '搜索追踪板块名称或代码' });
    await input.fill('SYN-0002'); const option = page.getByRole('listbox', { name: '可追踪板块' }).getByRole('option');
    const box = await option.boundingBox(); expect(box!.height).toBeGreaterThanOrEqual(44);
    const popup = await page.locator('.ty-sector-picker-popup').boundingBox();
    expect(popup!.x).toBeGreaterThanOrEqual(11); expect(popup!.x + popup!.width).toBeLessThanOrEqual(379);
    await option.tap(); await expect(page.getByRole('button', { name: '追踪板块：合成板块0002', exact: true })).toBeVisible();
    expectContext(page);
    const label = page.getByRole('table', { name: '个股涨幅榜', exact: true }).getByRole('button', { name: '查看完整内容：合成触屏完整题材', exact: true });
    await label.tap(); await expect(page.getByRole('tooltip')).toHaveText('合成触屏完整题材');
    expect(new URL(page.url()).searchParams.get('stock')).not.toBe(stockB);
    await label.tap(); await expect(page.getByRole('tooltip')).toBeHidden();
    await page.screenshot({ path: info.outputPath('synthetic-touch-sector-and-theme.png') });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
  });
  test('primary navigation and stock search open, close and select with taps alone', async ({ page }) => {
    const state = await setup(page); await openMonitor(page);
    const menu = page.getByRole('button', { name: '打开主导航', exact: true }); await menu.tap();
    await expect(menu).toHaveAttribute('aria-expanded', 'true');
    await expect(page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: '每日复盘', exact: true })).toBeInViewport();
    await page.getByRole('button', { name: '关闭导航', exact: true }).tap({ position: { x: 380, y: 600 } });
    await expect(menu).toHaveAttribute('aria-expanded', 'false');
    const input = page.getByRole('textbox', { name: '搜索标的', exact: true }), search = page.getByRole('button', { name: '搜索标的', exact: true });
    await input.fill('合成'); await search.tap(); const panel = page.getByRole('region', { name: '标的查询结果', exact: true });
    await expect(panel).toBeVisible(); await panel.getByRole('button', { name: '关闭查询结果', exact: true }).tap();
    await expect(panel).toBeHidden(); await expect(input).toBeFocused();
    await search.tap(); await panel.getByRole('link').last().tap(); await expect(panel).toBeHidden();
    expect(new URL(page.url()).searchParams.get('stock')).toBe(stockB); expectContext(page);
    expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
  });
});

test('global search retains normal links, supports keyboard selection, Escape and outside dismissal', async ({ page }) => {
  const state = await setup(page); await openMonitor(page);
  const input = page.getByRole('textbox', { name: '搜索标的', exact: true }), search = page.getByRole('button', { name: '搜索标的', exact: true });
  await input.fill('合成'); await search.click(); const panel = page.getByRole('region', { name: '标的查询结果', exact: true });
  const links = panel.getByRole('link'); await expect(links).toHaveCount(2);
  const href = new URL((await links.last().getAttribute('href'))!, page.url());
  expect(href.pathname).toBe('/workbench'); expect(href.searchParams.get('trade_day')).toBe(terminalDay);
  expect(href.searchParams.get('as_of_time')).toBe(cutoff); expect(href.searchParams.get('stock')).toBe(stockB);
  expect(href.searchParams.get('instrument')).toBe(instrumentB.id);
  await input.focus(); await input.press('ArrowDown'); await expect(links.first()).toBeFocused();
  await page.keyboard.press('End'); await expect(links.last()).toBeFocused();
  await page.keyboard.press('Home'); await expect(links.first()).toBeFocused();
  await page.keyboard.press('ArrowUp'); await expect(links.last()).toBeFocused();
  await page.keyboard.press('Escape'); await expect(panel).toBeHidden(); await expect(input).toBeFocused();
  await input.press('Enter'); await expect(panel).toBeVisible(); await page.getByRole('heading', { name: '工作台', exact: true }).click();
  await expect(panel).toBeHidden();
  await input.press('Enter'); await expect(panel).toBeVisible(); await input.press('ArrowDown'); await page.keyboard.press('Enter');
  await expect(panel).toBeHidden(); expect(new URL(page.url()).searchParams.get('stock')).toBe(stockA); expectContext(page);
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('a dismissed pending stock search cannot reopen itself when its response arrives', async ({ page }) => {
  let release!: () => void; const holdSearch = new Promise<void>(resolve => { release = resolve; });
  const state = await setup(page, { holdSearch }); await openMonitor(page);
  const input = page.getByRole('textbox', { name: '搜索标的', exact: true });
  await input.fill('合成'); await input.press('Enter');
  const panel = page.getByRole('region', { name: '标的查询结果', exact: true }); await expect(panel).toContainText('正在查询标的库');
  await input.press('Escape'); await expect(panel).toBeHidden();
  const response = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/instruments'));
  release(); await (await response).finished(); await expect(panel).toBeHidden(); await expect(input).toBeFocused();
  expectContext(page); expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('auction comparison labels all four columns with visible up or down categories', async ({ page }, info) => {
  const state = await setup(page); await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/auction?trade_day=${terminalDay}&as_of_time=${encodeURIComponent(cutoff)}`);
  const columns = page.locator('.ty-auction-days > .ty-auction-day'); await expect(columns).toHaveCount(4);
  for (let index = 0; index < 4; index++) {
    const label = columns.nth(index).locator('h3 .ty-auction-category');
    await expect(label).toHaveText(index < 2 ? '涨停' : '跌停'); await expect(label).toBeVisible();
    await expect(columns.nth(index).locator('h3 time')).toHaveText(terminalDays[index % 2]);
  }
  await page.screenshot({ path: info.outputPath('synthetic-auction-four-categories.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  for (let index = 0; index < 4; index++) { const label = columns.nth(index).locator('.ty-auction-category'); await label.scrollIntoViewIfNeeded(); await expect(label).toBeInViewport(); }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  expectContext(page); expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('lazy screening and research panes preserve unsaved screening input after result selection and pane switches', async ({ page }, info) => {
  const state = await setup(page); await openMonitor(page);
  expect(state.reads.some(path => path.includes('/screening/'))).toBe(false);
  expect(['ScreeningPanel', 'ResearchWorkspace', 'Auction'].some(name => moduleLoaded(state.modules, name))).toBe(false);
  const workspaces = page.getByRole('navigation', { name: '综合盯盘工作区', exact: true });
  await workspaces.getByRole('button', { name: '条件选股', exact: true }).click();
  const panel = page.getByRole('region', { name: '条件选股与观察', exact: true }); await expect(panel).toBeVisible();
  await panel.getByRole('textbox', { name: '规则名称', exact: true }).fill('合成未保存草稿');
  await panel.getByRole('textbox', { name: '条件 1 数值', exact: true }).fill('0.123456');
  await panel.getByRole('button', { name: '预览草稿', exact: true }).click();
  await expect(panel).toContainText('草稿预览 · 未保存'); await panel.getByRole('button', { name: /^合成选股结果/ }).click();
  await expect(workspaces.getByRole('button', { name: '板块与个股', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('region', { name: '板块与个股工作区', exact: true })).toBeVisible(); expectContext(page);
  await workspaces.getByRole('button', { name: '盘面', exact: true }).click();
  await workspaces.getByRole('button', { name: '条件选股', exact: true }).click();
  await expect(panel.getByRole('textbox', { name: '规则名称', exact: true })).toHaveValue('合成未保存草稿');
  await expect(panel.getByRole('textbox', { name: '条件 1 数值', exact: true })).toHaveValue('0.123456');
  await expect(panel).toContainText('未保存草稿'); await expect(panel).toContainText('草稿预览 · 未保存');
  expect(moduleLoaded(state.modules, 'ScreeningPanel')).toBe(true);
  expect(moduleLoaded(state.modules, 'ResearchWorkspace')).toBe(true);
  await page.screenshot({ path: info.outputPath('synthetic-lazy-screening-draft-preserved.png') });
  expect(state.writes).toEqual(['/api/v1/screening/preview']); expect(state.errors).toEqual([]);
});

test('ladder cards open details with Enter and restore their original focus after trajectory navigation and Escape', async ({ page }) => {
  const state = await setup(page);
  await page.goto(`/workbench?view=ladder&trade_day=${terminalDay}&as_of_time=${encodeURIComponent(cutoff)}`);
  const card = page.locator('.lo-stock').filter({ hasText: '合成甲' }).first(); await expect(card).toBeVisible();
  await card.focus(); await card.press('Enter');
  const inspector = page.getByLabel('选中股票详情', { exact: true }), close = inspector.getByRole('button', { name: '关闭股票详情', exact: true });
  await expect(inspector).toBeVisible(); await expect(close).toBeFocused();
  const trajectory = inspector.locator('.lo-trajectory button').last(); await trajectory.focus(); await trajectory.press('Enter');
  await expect(inspector).toBeVisible(); await page.keyboard.press('Escape');
  await expect(inspector).toBeHidden(); await expect(card).toBeFocused();
  await card.press('Enter'); await expect(close).toBeFocused(); await close.press('Enter');
  await expect(inspector).toBeHidden(); await expect(card).toBeFocused(); expectContext(page);
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('native data-scope disclosure closes with Escape and restores its summary focus', async ({ page }) => {
  const state = await setup(page);
  await page.goto(`/workbench?view=ladder&trade_day=${terminalDay}&as_of_time=${encodeURIComponent(cutoff)}`);
  const summary = page.getByLabel('查看数据口径', { exact: true }); await summary.focus(); await summary.press('Enter');
  const details = page.locator('details.lo-source'); await expect(details).toHaveJSProperty('open', true);
  await expect(details).toContainText('合成浏览器测试数据，非真实市场');
  await summary.press('Escape'); await expect(details).toHaveJSProperty('open', false); await expect(summary).toBeFocused();
  expectContext(page); expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('a failed lazy page keeps the shell and dated URL and reload recovers the requested page', async ({ page }) => {
  const state = await setup(page); let fail = true;
  await page.route(/\/(?:src\/features\/terminal\/Auction\.tsx|assets\/Auction-[^/]+\.js)(?:\?|$)/, route => fail ? route.abort('failed') : route.continue());
  const target = `/auction?trade_day=${terminalDay}&as_of_time=${encodeURIComponent(cutoff)}`; await page.goto(target);
  await expect(page.getByRole('alert')).toContainText('页面载入失败');
  await expect(page.getByRole('heading', { name: '工作台', exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: '工作台页面', exact: true })).toBeVisible();
  expectContext(page); expect(new URL(page.url()).pathname).toBe('/auction');
  fail = false; await page.getByRole('button', { name: '重新载入本页', exact: true }).click();
  await expect(page.locator('.ty-auction-days > .ty-auction-day')).toHaveCount(4); await expect(page.locator('.page-load-error')).toHaveCount(0);
  expectContext(page); expect(new URL(page.url()).pathname).toBe('/auction'); expect(state.writes).toEqual([]);
});
