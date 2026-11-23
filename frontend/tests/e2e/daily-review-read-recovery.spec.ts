import { expect, type Page } from '@playwright/test';
import { test, openNotifications } from './isolated-browser.fixtures';
import { blank, day, id, meta, metrics } from './reviewTestFixtures';
import { researchOverviewFixture } from './dailyReviewContractFixtures';

// Exercise the production read hooks through the page. All API traffic is
// synthetic and outside origins or writes cannot reach a running service.
const today = '2026-10-27', previous = '2026-10-26', older = '2026-10-23';
const ownerB = '00000000-0000-4000-8000-000000000082';
const at = '2026-10-27T05:00:00Z';
type Failure = 0 | 503 | 401 | 403 | 'network';

async function fixture(page: Page) {
  const state = { failure: 0 as Failure, signedIn: true, owner: id, generation: 1, refreshingUntil: 0,
    reads: [] as Array<{ day: string; owner: string }>, writes: [] as string[], outside: [] as string[],
    errors: [] as string[], unexpected: [] as string[], holdDay: '', held: 0, release: () => {} };
  const label = (owner = state.owner) => owner === id ? '账号甲' : '账号乙';
  const name = (selected: string, owner = state.owner, generation = state.generation) => `${label(owner)}资料-${selected}-第${generation}批`;
  const dataFor = (selected: string, owner = state.owner) => ({ ...day(selected),
    metrics: { ...metrics, ...(selected === today ? { basis: 'intraday_snapshot', source_time: `${today}T13:00:00+08:00`,
      sample: { N: 2, daily_loaded: false, snapshot_loaded: true } } : {}),
      turnover: { ...metrics.turnover, amount_yuan: String(state.generation * 10_000_000_000) } },
    indices: [{ ts_code: '000001.SH', name: name(selected, owner), state: 'complete',
      record: { close: '3100', pre_close: '3000', pct_chg: '3.33' } }],
    intraday: [], themes: { items: [] }, pools: { records: [] },
    news: { records: [], scope_note: '合成读取恢复测试' }, evidence_refs: [] });
  const origin = new URL(test.info().project.use.baseURL as string).origin;
  page.on('pageerror', error => state.errors.push(error.message));
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), () => page.keyboard.press('Escape'));
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    if (url.origin !== origin) { state.outside.push(url.origin); return route.abort('blockedbyclient'); }
    if (!path.startsWith('/api/') && !path.startsWith('/health/')) return route.continue();
    const owner = state.owner, scope = `${owner}:review-read-recovery`;
    const scopedProjection = ['/api/v1/review-data/day', '/api/v1/review-data/series', '/api/v1/review-data/stocks', '/api/v1/review-data/theme-history', '/api/v1/review-data/lhb', '/api/v1/review-data/overview'].includes(path);
    const ok = (data: unknown, headers: Record<string, string> = {}) => route.fulfill({ headers: {
      ...(scopedProjection ? { 'X-Cache-Scope': scope } : {}), ...headers }, json: { meta, data } });
    if (path === '/api/v1/session' && request.method() === 'POST') {
      state.owner = request.postDataJSON().username === 'review-b' ? ownerB : id;
      state.signedIn = true; state.failure = 0;
      return route.fulfill({ json: { meta, data: { user: { id: state.owner, username: state.owner === id ? 'review-a' : 'review-b', role: 'researcher' },
        csrf_token: 'synthetic-only', cache_scope: `${state.owner}:review-read-recovery` } } });
    }
    if (path === '/api/v1/session' && request.method() === 'DELETE') { state.signedIn = false; return route.fulfill({ status: 204 }); }
    if (request.method() !== 'GET') { state.writes.push(`${request.method()} ${path}`); return route.fulfill({ status: 405, json: { error: { message: '合成测试禁止写入' } } }); }
    if (path === '/api/v1/chatgpt/status') return ok({ available: false, authenticated: false, local_only: true, accounts: [] });
    if (!state.signedIn) return route.fulfill({ status: 401, json: { error: { message: '合成会话已失效' } } });
    if (path === '/api/v1/session') return ok({ user: { id: owner, username: owner === id ? 'review-a' : 'review-b', role: 'researcher' }, csrf_token: 'synthetic-only', cache_scope: scope });
    if (path === '/api/v1/market-session') return ok({ server_time: `${today}T13:00:00+08:00`, trade_day: today,
      phase: 'trading', is_trading_day: true, current_day_active: true, automatic_live: true, next_transition_at: null, poll_seconds: 15 });
    if (path === '/api/v1/review-data/day') {
      const selected = url.searchParams.get('trade_day') || today;
      state.reads.push({ day: selected, owner });
      if (state.failure === 'network') return route.abort('failed');
      if (state.failure) {
        if (state.failure === 401) state.signedIn = false;
        return route.fulfill({ status: state.failure, json: { error: { message: '合成同日资料暂时不可读' } } });
      }
      const snapshot = dataFor(selected, owner);
      if (state.refreshingUntil) snapshot.indices[0].name += ` · 读取 ${state.reads.length}`;
      if (state.holdDay === selected) {
        state.held++;
        await new Promise<void>(resolve => { state.release = resolve; });
      }
      return ok(snapshot, { 'X-Read-Cache-State': state.reads.length <= state.refreshingUntil ? 'refreshing' : 'fresh' });
    }
    if (path === '/api/v1/review-data/series') {
      const selected = url.searchParams.get('trade_day') || today, dates = [older, previous, today].filter(value => value <= selected);
      return ok({ shared_date: selected, calendar_state: 'complete', state: 'partial', trade_days: dates,
        records: dates.map(selectedDay => ({ trade_day: selectedDay, state: 'partial', metrics: dataFor(selectedDay).metrics, indices: [], themes: { records: [] } })) });
    }
    if (path === '/api/v1/review-data/overview') return ok(researchOverviewFixture(url.searchParams.get('trade_day') || today, [older, previous, today]));
    if (path === '/api/v1/review-data/notes') return ok({ trade_day: url.searchParams.get('trade_day') || today,
      state: 'empty', content: blank(), head_id: null, writable: true, is_current: true, revision: null });
    if (['/api/v1/review-data/notes/days', '/api/v1/watchlist', '/api/v1/research-documents', '/api/v1/review-data/theme-history', '/api/v1/review-data/stocks'].includes(path)) return ok({ items: [] });
    if (path === '/api/v1/review-data/lhb') return ok({ shared_date: url.searchParams.get('trade_day') || today,
      records: [], selected_record: null, history_dates: [], unmatched_seat_records: [], source_listing_count: 0 });
    if (path === '/api/v1/ai/settings') return ok({ enabled: false, revision: 1, features: [] });
    if (path === '/api/v1/data-recovery') return ok({ server_time: at, items: [], counts: {}, has_more: false, health_state: null, health: [], workers: [] });
    if (path === '/health/ready') return route.fulfill({ json: { status: 'ok' } });
    if (['/api/v1/workbench-view', '/api/v1/news/items', '/api/v1/news/editions', '/api/v1/data-center/overview', '/api/v1/review-data/analysis'].includes(path)) {
      return route.fulfill({ status: 503, json: { error: { message: '该背景读取不在本项合成测试范围内' } } });
    }
    state.unexpected.push(path);
    return route.fulfill({ status: 503, json: { error: { message: '隔离测试未配置此读取' } } });
  });
  return { state, name };
}
function safe(state: Awaited<ReturnType<typeof fixture>>['state']) {
  expect(state.writes).toEqual([]); expect(state.outside).toEqual([]); expect(state.errors).toEqual([]); expect(state.unexpected).toEqual([]);
}
async function open(page: Page, selected = today, automatic = false) {
  await page.goto('/research/reviews?' + new URLSearchParams({ view: 'overview', lookback: '20', ...(!automatic ? { trade_day: selected, range_end: selected } : {}) }));
  await expect(page.getByLabel('复盘日期', { exact: true })).toBeVisible();
}

for (const failure of [503, 'network'] as const) test(`a cold ${failure} read recovers automatically without a manual retry`, async ({ page }) => {
  await page.clock.install({ time: new Date(at) });
  const { state, name } = await fixture(page); state.failure = failure;
  await open(page);
  await expect((await openNotifications(page)).getByRole('region', { name: '读取失败', exact: true })).toBeVisible();
  await expect(page.locator('.dr-root .dr-loading')).toHaveCount(0);
  await expect(page.locator('.dr-market-ribbon')).toHaveCount(0);
  // Keep the failure through the preparation pass and the first retry so a
  // late initial reader cannot make this look like automatic recovery.
  await page.clock.runFor(35_000);
  await expect((await openNotifications(page)).getByRole('region', { name: '读取失败', exact: true })).toBeVisible();
  const failedReads = state.reads.length;
  state.failure = 0;
  await page.clock.runFor(35_000);
  await expect(page.locator('.dr-market-ribbon')).toContainText(name(today));
  await expect(page.getByLabel('复盘日期', { exact: true })).toHaveValue(today);
  await expect(page.locator('.dr-root [role=alert]')).toHaveCount(0);
  expect(state.reads.length).toBeGreaterThan(failedReads); safe(state);
});

test('an active review keeps rechecking after four refreshing results and displays the eventual fresh result', async ({ page }) => {
  await page.clock.install({ time: new Date(at) });
  const { state, name } = await fixture(page); state.refreshingUntil = 7;
  await open(page);
  const ribbon = page.locator('.dr-market-ribbon');
  await expect(ribbon).toContainText(name(today));
  // Let each actual response render before advancing the next virtual delay;
  // otherwise the next retry can be scheduled after runFor has already ended.
  const advanceRead = async () => {
    const before = state.reads.length;
    await page.clock.runFor(30_000);
    await expect.poll(() => state.reads.length).toBeGreaterThan(before);
    await expect(ribbon).toContainText(`读取 ${state.reads.length}`);
  };
  for (let attempt = 0; state.reads.length < 5 && attempt < 5; attempt++) await advanceRead();
  expect(state.reads.length).toBeGreaterThanOrEqual(5);
  await expect(page.locator('.dr-root [role=alert]')).toHaveCount(0);
  state.generation = 2;
  for (let attempt = 0; state.reads.length < 8 && attempt < 4; attempt++) await advanceRead();
  await expect(ribbon).toContainText(name(today));
  expect(state.reads.length).toBeGreaterThanOrEqual(8);
  await expect(page.locator('.dr-root')).toHaveAttribute('aria-busy', 'false');
  await expect(page.locator('.dr-root [role=alert]')).toHaveCount(0); safe(state);
});

for (const status of [401, 403] as const) test(`a ${status} read stops automatic retries and the next account cannot see the retired account data`, async ({ page }) => {
  await page.clock.install({ time: new Date(at) });
  const { state, name } = await fixture(page);
  await open(page);
  const oldName = name(today);
  await expect(page.locator('.dr-market-ribbon')).toContainText(oldName);
  // Finish the independently scheduled preparation pass before revoking this
  // reader, so its one initial GET is not counted as a hook retry.
  await page.clock.runFor(5_000);
  await expect(await openNotifications(page)).toContainText('资料未完成');
  state.failure = status;
  await page.evaluate(() => window.dispatchEvent(new Event('read-cache-revalidate')));
  if (status === 401) await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  else await expect((await openNotifications(page)).getByRole('region', { name: '读取失败', exact: true })).toBeVisible();
  await expect(page.locator('.dr-market-ribbon').filter({ hasText: oldName })).toHaveCount(0);
  await page.clock.runFor(1_000);
  const deniedReads = state.reads.length;
  await page.clock.runFor(90_000);
  expect(state.reads.length).toBe(deniedReads);
  if (status === 403) await page.getByRole('button', { name: '退出', exact: true }).click();
  await page.getByLabel('账号', { exact: true }).fill('review-b');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password-only');
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.locator('.dr-market-ribbon')).toContainText(name(today, ownerB));
  await expect(page.locator('.dr-market-ribbon').filter({ hasText: oldName })).toHaveCount(0);
  expect(state.reads.at(-1)?.owner).toBe(ownerB); safe(state);
});

test('changing dates retires a pending read and its late response cannot replace the selected day', async ({ page }) => {
  await page.clock.install({ time: new Date(at) });
  const { state, name } = await fixture(page);
  await open(page); await expect(page.locator('.dr-market-ribbon')).toContainText(name(today));
  state.holdDay = previous;
  await page.getByLabel('复盘日期', { exact: true }).fill(previous);
  await expect.poll(() => state.held).toBeGreaterThan(0);
  await page.getByLabel('复盘日期', { exact: true }).fill(older);
  await expect(page.locator('.dr-market-ribbon')).toContainText(name(older));
  state.release(); await page.clock.runFor(1_000);
  await expect(page.getByLabel('复盘日期', { exact: true })).toHaveValue(older);
  await expect(page.locator('.dr-market-ribbon')).toContainText(name(older));
  await expect(page.locator('.dr-market-ribbon').filter({ hasText: name(previous) })).toHaveCount(0); safe(state);
});

test('a background review replacement preserves the mounted editor and unsaved judgment', async ({ page }) => {
  await page.clock.install({ time: new Date(at) });
  const { state, name } = await fixture(page);
  await open(page, today, true); await expect(page.locator('.dr-market-ribbon')).toContainText(name(today));
  await page.getByRole('button', { name: '填写判断', exact: true }).click();
  const input = page.getByLabel('市场判断', { exact: true });
  await input.fill('仅用于合成测试的未保存判断');
  const mounted = await input.elementHandle();
  state.generation = 2;
  await page.clock.runFor(31_000);
  await expect(page.locator('.dr-market-ribbon')).toContainText(name(today));
  await expect(input).toHaveValue('仅用于合成测试的未保存判断');
  expect(await mounted!.evaluate(node => node.isConnected)).toBe(true);
  await expect(page.getByRole('dialog', { name: '编辑市场判断', exact: true })).toBeVisible();
  await expect(page.locator('.dr-root [role=alert]')).toHaveCount(0); safe(state);
});
