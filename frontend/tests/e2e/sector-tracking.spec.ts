import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { SectorMember } from '../../src/features/terminal/types';
import { terminalDay, terminalFixture, sectorA, sectorB, stockA } from './terminalTestFixtures';

test.beforeEach(async ({ page }) => {
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), async () => { await page.keyboard.press('Escape'); });
});

const row = (code: string, name: string, speed_5m: number | null, pct_change = 0): SectorMember => ({
  code, name, instrument_id: null, speed_5m, pct_change,
  source_time: `${terminalDay}T14:00:00+08:00`, speed_window_seconds: speed_5m == null ? null : 300,
});
const membersA = [row('999994.SZ', '合成缺测', null), row('999992.SH', '合成回落', -1, 3), row(stockA, '合成零速', 0), row('999993.SZ', '合成急涨', 3, -2)];
const membersB = [row('999996.SH', '合成乙回落', -3), row('999995.SZ', '合成乙急涨', 5, 1)];

async function fixture(page: Page) {
  const state = {
    marketReads: 0, membershipReads: [] as URLSearchParams[], writes: [] as string[], errors: [] as string[],
    membersA: structuredClone(membersA), membersB: structuredClone(membersB),
    failB: false, blockB: false, releaseB: undefined as (() => void) | undefined,
    retryGate: undefined as Promise<void> | undefined,
    dayB: terminalDay, truncated: false,
  };
  page.on('pageerror', error => state.errors.push(error.message));
  await page.route('**/api/v1/**', async route => {
    const request = route.request(); const url = new URL(request.url());
    if (request.method() !== 'GET') { state.writes.push(`${request.method()} ${url.pathname}`); return route.abort(); }
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'demo', quality: { status: 'partial', reasons: ['合成测试，不是真实行情'] } } } });
    if (url.pathname.endsWith('/session')) return ok({ user: { id: 'sector-tracking-owner', username: 'synthetic', role: 'admin' }, csrf_token: 'synthetic-only' });
    if (url.pathname.endsWith('/workbench-view')) {
      // Login preparation also reads other views; count this page's exact market scope.
      if (url.searchParams.get('view') === 'overview' && url.searchParams.get('day') === terminalDay) state.marketReads++;
      const base = terminalFixture(url.searchParams);
      // Membership supplies identities; the visible values must still come
      // from the current market's quotes for those exact stock codes.
      const stocks = [...state.membersA, ...state.membersB].map(member => ({ ...base.stocks[0], ...member }));
      return ok({ ...base, stocks, detail_codes: [stockA], detail_revision: 'synthetic-sector-tracking-v1' });
    }
    if (url.pathname.endsWith('/sector-membership')) {
      const sector = url.searchParams.get('sector'); state.membershipReads.push(url.searchParams);
      if (sector === sectorB && state.retryGate) await state.retryGate;
      if (sector === sectorB && state.blockB) await new Promise<void>(resolve => { state.releaseB = resolve; });
      if (sector === sectorB && state.failB) return route.fulfill({ status: 503, json: { error: { message: '合成板块读取失败' } } });
      const members = sector === sectorB ? state.membersB : state.membersA;
      return ok({ trade_day: terminalDay, sector_code: sector, state: 'available',
        members_trade_day: sector === sectorB ? state.dayB : terminalDay,
        members: members.map(({ code, name }) => ({ code, name })),
        member_count: state.truncated ? members.length + 100 : members.length, truncated: state.truncated });
    }
    if (url.pathname.endsWith('/workbench-details')) {
      const base = terminalFixture(url.searchParams); const sector = url.searchParams.get('sector');
      const codes = (url.searchParams.get('codes') || '').split(',');
      return ok({ trade_day: terminalDay, revision: 'synthetic-sector-tracking-v1',
        stocks: base.stocks.filter(item => codes.includes(item.code)), auction_days: base.auction_days, events: [], seals: {},
        sector_code: sector, members: base.selected_members.slice(0, 1), funds_history: [],
        members_trade_day: sector === sectorB ? state.dayB : terminalDay,
      });
    }
    if (url.pathname.endsWith('/ai/settings')) return ok({ enabled: false, features: [] });
    return ok({ items: [], next_cursor: null });
  });
  return state;
}

const tracking = (page: Page) => page.getByRole('region', { name: '板块追踪', exact: true });
const table = (page: Page) => tracking(page).getByRole('table');
async function selectBoard(page: Page, label: string) {
  // Stock selection leaves its accessible full-text hint focused. Close that
  // tested hint before moving to the picker so it cannot cover the next action.
  await page.keyboard.press('Escape');
  const focusedHint = page.locator('.ty-stock-name:focus,.readable-text:focus');
  if (await focusedHint.count()) await focusedHint.blur();
  await expect(page.getByRole('tooltip')).toHaveCount(0);
  await tracking(page).getByRole('button', { name: /^追踪板块：/ }).click();
  await page.getByRole('listbox', { name: '可追踪板块', exact: true }).getByRole('option', { name: new RegExp('^' + label) }).click();
}
async function open(page: Page) {
  await page.goto(`/workbench?view=overview&trade_day=${terminalDay}`);
  await expect(tracking(page).getByRole('heading', { name: '板块追踪', exact: true })).toBeVisible();
  await expect(tracking(page).getByRole('button', { name: /^合成急涨/ })).toBeVisible();
}

test('sector clicks show the full board ranking, keep zero distinct from missing and select stocks locally', async ({ page }) => {
  const state = await fixture(page); await open(page);
  await expect(page.getByRole('heading', { name: '关注对象跟踪', exact: true })).toHaveCount(0);
  await expect(table(page).locator('tbody .ty-stock-name')).toHaveText(['合成急涨999993', '合成零速999991', '合成回落999992', '合成缺测999994']);
  await expect(table(page).locator('tbody tr').nth(1)).toContainText('0.00%');
  await expect(table(page).locator('tbody tr').last().locator('td').nth(2)).toHaveText('—');
  const reads = state.marketReads;
  await tracking(page).getByRole('button', { name: /^合成急涨/ }).click();
  await expect(page).toHaveURL(/stock=999993\.SZ/);
  await expect(page.getByRole('heading', { name: '涨跌停家数走势', exact: true })).toBeVisible();
  await selectBoard(page, '合成题材乙');
  await expect(tracking(page).getByRole('button', { name: '追踪板块：合成题材乙', exact: true })).toBeVisible();
  await expect(table(page).locator('tbody .ty-stock-name')).toHaveText(['合成乙急涨999995', '合成乙回落999996']);
  await expect(tracking(page)).not.toContainText('合成急涨');
  expect(state.marketReads).toBe(reads);
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('a late response for a previous board never replaces the current board or clears the main market panels', async ({ page }) => {
  const state = await fixture(page); state.blockB = true; await open(page);
  const reads = state.marketReads;
  const market = page.getByRole('region', { name: '市场概览', exact: true });
  const marketBefore = await market.innerText();
  await selectBoard(page, '合成题材乙');
  await expect(tracking(page)).toHaveAttribute('aria-busy', 'true');
  await expect(tracking(page).getByRole('status')).toContainText('正在读取合成题材乙成分股');
  await expect(tracking(page).getByRole('button', { name: '追踪板块：合成题材乙', exact: true })).toBeVisible();
  await expect(tracking(page)).not.toContainText('合成急涨');
  expect(await market.innerText()).toBe(marketBefore);
  await selectBoard(page, '合成题材甲');
  await expect(tracking(page)).toContainText('合成急涨');
  await expect.poll(() => !!state.releaseB).toBe(true);
  const response = page.waitForResponse(response => response.url().includes('/sector-membership?') && new URL(response.url()).searchParams.get('sector') === sectorB);
  state.blockB = false; state.releaseB!(); await (await response).finished();
  await expect(tracking(page).getByRole('button', { name: '追踪板块：合成题材甲', exact: true })).toBeVisible();
  await expect(table(page).locator('tbody .ty-stock-name').first()).toContainText('合成急涨');
  expect(state.marketReads).toBe(reads); expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('failed board reads retry only the board and do not fabricate a zero-valued ranking', async ({ page }) => {
  const state = await fixture(page); state.failB = true; await open(page);
  await selectBoard(page, '合成题材乙');
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成板块读取失败' });
  await expect(notice).toBeVisible();
  await expect(tracking(page).getByRole('table')).toHaveCount(0);
  await expect(tracking(page).getByRole('status')).toContainText('合成题材乙成分股暂未读取成功');
  const reads = state.marketReads, membershipReads = state.membershipReads.length;
  let releaseRetry!: () => void;
  state.retryGate = new Promise<void>(resolve => { releaseRetry = resolve; });
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  try {
    await tracking(page).getByRole('button', { name: '重试板块', exact: true }).click();
    await expect.poll(() => state.membershipReads.length).toBeGreaterThan(membershipReads);
    state.failB = false;
  } finally { releaseRetry(); }
  await openNotifications(page);
  await expect(notice).toHaveCount(0);
  await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(tracking(page)).toContainText('合成乙急涨');
  await expect(tracking(page).getByRole('alert')).toHaveCount(0);
  expect(state.marketReads).toBe(reads); expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('earlier saved membership remains visible with its own date and selected-day quotes', async ({ page }) => {
  const state = await fixture(page); state.dayB = '2001-12-28'; await open(page);
  await selectBoard(page, '合成题材乙');
  await expect(tracking(page)).toContainText('成员日 2001-12-28');
  await expect(tracking(page).locator('.ty-sector-tracking-summary')).toHaveAttribute('title', /行情日 2001-12-31 · 成员日 2001-12-28/);
  await expect(table(page).locator('tbody tr').first().getByRole('cell').nth(2)).toHaveText('+5.00%');
  await expect(tracking(page)).toContainText('合成乙急涨');
  await expect(tracking(page).getByRole('table')).toHaveCount(1);
  expect(state.errors).toEqual([]);
});

test('future membership never leaks into an earlier selected date', async ({ page }) => {
  const state = await fixture(page); state.dayB = '2002-01-01'; await open(page);
  await selectBoard(page, '合成题材乙');
  await expect(tracking(page)).toContainText('暂无可用板块成分');
  await expect(tracking(page).getByRole('table')).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test('large boards show more rows without another API read and disclose partial coverage', async ({ page }) => {
  const state = await fixture(page);
  state.membersA = [...membersA, ...Array.from({ length: 100 }, (_, i) => row(`${800000 + i}.SZ`, `合成成分${i}`, -10 - i))];
  state.truncated = true; await open(page);
  await expect(table(page).locator('tbody tr')).toHaveCount(50);
  await expect(tracking(page).locator('.ty-sector-tracking-summary')).toContainText('成分 104 只 · 未齐');
  const reads = state.membershipReads.length;
  await tracking(page).getByRole('button', { name: '再显示 50 只', exact: true }).click();
  await expect(table(page).locator('tbody tr')).toHaveCount(100);
  await tracking(page).getByRole('button', { name: '再显示 4 只', exact: true }).click();
  await expect(table(page).locator('tbody tr')).toHaveCount(104);
  await expect(table(page).locator('tbody .ty-stock-name').last()).toContainText('合成缺测');
  expect(state.membershipReads).toHaveLength(reads);
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('the board tracker remains in the lower lists and compact cards fit each viewport', async ({ page }) => {
  const state = await fixture(page); await open(page);
  for (const width of [1672, 1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    const panelBounds = await tracking(page).boundingBox(); const scrollBounds = await tracking(page).locator('.ty-table-scroll').boundingBox();
    await expect(page.locator('.ty-overview-stock-row > .ty-sector-tracking')).toHaveCount(1);
    await expect(page.locator('.ty-overview-market-grid > .ty-mini-ladder')).toHaveCount(1);
    expect(panelBounds).not.toBeNull(); expect(scrollBounds).not.toBeNull();
    expect(scrollBounds!.x).toBeGreaterThanOrEqual(panelBounds!.x);
    expect(scrollBounds!.x + scrollBounds!.width).toBeLessThanOrEqual(panelBounds!.x + panelBounds!.width + 1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (width >= 1360) {
      const ladderBounds = await page.locator('.ty-overview-market-grid > .ty-mini-ladder').boundingBox();
      expect(panelBounds!.x + panelBounds!.width).toBeLessThanOrEqual(ladderBounds!.x + 1);
      expect(ladderBounds!.y + ladderBounds!.height).toBeCloseTo(panelBounds!.y + panelBounds!.height, 0);
    }
    if (width === 1672) await page.screenshot({ path: test.info().outputPath('sector-tracking-overview.png'), fullPage: true });
  }
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});
