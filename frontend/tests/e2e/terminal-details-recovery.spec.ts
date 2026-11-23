import { expect, type Page } from '@playwright/test';
import { test, openNotifications } from './isolated-browser.fixtures';
import { terminalFixture, terminalDay, stockA, sectorA } from './terminalTestFixtures';
import { blank, day, days } from './reviewTestFixtures';
import type { DataHealth } from '../../src/shared/dataRecovery';

// Parent and detail revisions deliberately advance independently. These are
// synthetic protocol responses; no market service or database is contacted.
async function fixture(page: Page) {
  const state = { parent: 2, detail: 1, detailName: '旧版本详情', observationCutoff: '', fail: 0, sectorFail: false, owner: 'detail-owner-a', signedIn: true,
    hold: false, release: undefined as (() => void) | undefined, denied: false,
    backgroundFail: false, detailCodes: [stockA],
    coherentWarmup: false, replayCutoff: '', warmingAttempts: {} as Record<string, number>, preparedViews: [] as string[],
    health: [] as DataHealth[], healthState: null as string | null, healthFail: false, oldHealthContract: false, healthRequests: [] as string[],
    details: [] as string[], memberships: [] as string[], activeReads: [] as { path: string; query: string; active: boolean }[], aborted: [] as string[], writes: [] as string[], errors: [] as string[] };
  page.on('pageerror', error => state.errors.push(error.message));
  page.on('requestfailed', request => { if (request.url().includes('/workbench-details?')) state.aborted.push(request.url()); });
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    if (path === '/api/v1/workbench-view' || path === '/api/v1/workbench-details')
      state.activeReads.push({ path, query: url.search, active: request.headers()['x-workbench-active'] === '1' });
    const scope = `${state.owner}:detail-recovery`, owner = state.owner;
    const ok = (data: unknown, headers: Record<string, string> = {}) => route.fulfill({ headers,
      json: { data, meta: { app_mode: 'demo', generated_at: '2026-10-27T10:00:00+08:00' } } });
    if (request.method() !== 'GET') state.writes.push(request.method() + ' ' + path);
    if (path === '/api/v1/session' && request.method() === 'POST') state.signedIn = true;
    if (path === '/api/v1/session' && request.method() === 'DELETE') { state.signedIn = false; return route.fulfill({ status: 204 }); }
    if (!state.signedIn) return route.fulfill({ status: 401, json: { error: { message: '合成会话已失效' } } });
    if (path === '/api/v1/session') return ok({ user: { id: owner, username: owner, role: 'admin' }, csrf_token: 'synthetic-only', cache_scope: scope });
    if (state.denied) return route.fulfill({ status: 403, json: { error: { message: '合成读取权限已撤回' } } });
    const views = ['overview', 'auction', 'ladder', 'funds'];
    const warmContext = (view: string) => ({ revision: `warm-${view}`,
      cutoff: `${terminalDay}T09:5${5 + views.indexOf(view)}:00.123456+08:00`, name: `双组同批 · ${view}` });
    if (path === '/api/v1/workbench-view') {
      const view = url.searchParams.get('view') ?? 'overview';
      if (state.coherentWarmup) {
        state.warmingAttempts[view] = (state.warmingAttempts[view] ?? 0) + 1;
        if (state.warmingAttempts[view] === 1) return route.fulfill({ status: 503, headers: { 'Retry-After': '1' },
          json: { error: { code: 'read_cache_warming', message: '合成独立页面准备中' } } });
        if (!state.preparedViews.includes(view)) state.preparedViews.push(view);
        const context = state.replayCutoff ? { revision: 'warm-replay', cutoff: state.replayCutoff } : warmContext(view);
        return ok({ ...terminalFixture(url.searchParams), detail_revision: `details-${context.revision}`,
          detail_observation_cutoff: context.cutoff, detail_codes: state.detailCodes }, { 'X-Cache-Scope': scope, 'X-Read-Cache-State': 'fresh' });
      }
      return ok({ ...terminalFixture(url.searchParams), detail_revision: `details-${state.parent}`,
        detail_observation_cutoff: state.observationCutoff || null, detail_codes: state.detailCodes }, { 'X-Cache-Scope': scope, 'X-Read-Cache-State': 'fresh' });
    }
    if (path === '/api/v1/sector-membership') {
      state.memberships.push(url.search);
      if (state.sectorFail) return route.fulfill({ status: 503, json: { error: { message: '合成板块读取暂不可用' } } });
      const base = terminalFixture(url.searchParams);
      return ok({ trade_day: base.trade_day, sector_code: url.searchParams.get('sector'), state: 'available',
        members_trade_day: base.members_trade_day, member_count: base.selected_members.length, truncated: false,
        members: base.selected_members.map(({ code, name }) => ({ code, name })) }, { 'X-Cache-Scope': scope });
    }
    if (path === '/api/v1/workbench-details') {
      state.details.push(url.search);
      const pinned = !!state.observationCutoff && url.searchParams.get('observation_cutoff') === state.observationCutoff;
      const context = state.coherentWarmup ? state.replayCutoff && url.searchParams.get('as_of_time') === state.replayCutoff
        ? { revision: 'warm-replay', name: '双组回放同批详情' }
        : views.map(warmContext).find(item => item.cutoff === url.searchParams.get('observation_cutoff')) : undefined;
      const revision = context?.revision ?? (pinned ? state.parent : state.detail);
      const name = context?.name ?? (pinned ? '与主表同批详情' : state.detailName);
      if (state.hold) await new Promise<void>(resolve => { state.release = resolve; });
      if (state.backgroundFail && request.headers()['x-workbench-active'] !== '1') return route.fulfill({ status: 503, json: { error: { message: '合成后台详情暂不可用' } } });
      if (state.sectorFail && url.searchParams.get('sector')) return route.fulfill({ status: 503, json: { error: { message: '合成板块读取暂不可用' } } });
      if (state.fail) {
        if (state.fail === 401) state.signedIn = false;
        if (state.fail === 403) state.denied = true;
        return route.fulfill({ status: state.fail, json: { error: { message: '合成详情暂时不可读' } } });
      }
      const base = terminalFixture(url.searchParams), codes = (url.searchParams.get('codes') ?? '').split(',').filter(Boolean);
      return ok({ trade_day: base.trade_day, revision: `details-${revision}`,
        stocks: codes.map(code => ({ ...base.stocks[0], code, name })), auction_days: base.auction_days,
        events: [], seals: {}, sector_code: url.searchParams.get('sector'), members: base.stocks,
        members_trade_day: base.trade_day, funds_history: [], tracking_members: base.stocks,
        tracking_member_count: base.stocks.length, tracking_members_truncated: false }, { 'X-Cache-Scope': scope });
    }
    if (path === '/api/v1/market-session') return ok({ server_time: `${terminalDay}T10:00:00+08:00`, trade_day: terminalDay,
      phase: 'trading', is_trading_day: true, automatic_live: true, current_day_active: true, next_transition_at: null, poll_seconds: 15 });
    if (path.endsWith('/day')) return ok(day(days[1]));
    if (path.endsWith('/series')) return ok({ ...day(days[1]), trade_days: [], records: [] });
    if (path.endsWith('/notes')) return ok({ trade_day: days[1], content: blank(), head_id: null, writable: true, revision: null });
    if (path.endsWith('/lhb')) return ok({ records: [], history_dates: [], unmatched_seat_records: [] });
    if (path.endsWith('/watchlist')) return ok({ items: [] });
    if (path.endsWith('/ai/settings')) return ok({ enabled: false, revision: 1, features: [] });
    if (path.endsWith('/integrations')) return ok({ items: [], templates: [], credential_store_ready: true });
    if (path.endsWith('/data-center/overview')) return ok({ services: [], tasks: [], issues: [], services_count: 0, running_tasks: 0, today_requests: 0, pending_issues: 0 });
    if (path.endsWith('/data-center/calls')) return ok({ items: [], next_cursor: null });
    if (path.endsWith('/news/items')) return ok({ items: [], total: 0, next_cursor: null, sources: [] });
    if (path.endsWith('/news/editions')) return ok({ items: [], next_cursor: null });
    if (path.endsWith('/data-recovery')) {
      state.healthRequests.push(url.search);
      if (state.healthFail) return route.fulfill({ status: 503, json: { error: { message: '合成运行状态读取失败' } } });
      const selected = url.searchParams.get('day') ?? '2026-10-27';
      return ok({ server_time: '2026-10-27T10:00:00+08:00', items: [], counts: {}, workers: [], has_more: false,
        ...(!state.oldHealthContract ? { health: state.health.map(item => ({ ...item, trade_day: selected })), health_state: state.healthState } : {}) });
    }
    return route.fulfill({ status: 503, json: { error: { message: '未配置的合成后台读取' } } });
  });
  await page.clock.install({ time: new Date('2026-10-27T02:00:00Z') });
  await page.clock.pauseAt(new Date('2026-10-27T02:00:01Z'));
  return state;
}

async function open(page: Page, extra = '') {
  await page.goto(`/workbench?stock=${stockA}&sector=${sectorA}${extra}`);
  await expect(page.locator('.ty-terminal')).toBeVisible();
}
function safe(state: Awaited<ReturnType<typeof fixture>>) { expect(state.errors).toEqual([]); expect(state.writes).toEqual([]); }

test('visible parent and selected detail carry priority while background preloads do not', async ({ page }) => {
  const state = await fixture(page); state.detail = state.parent;
  await open(page); await expect(page.locator('.ty-selected')).toContainText(state.detailName);
  const parent = state.activeReads.filter(item => item.path.endsWith('workbench-view'));
  expect(parent.some(item => item.active)).toBe(true);
  const detail = state.activeReads.filter(item => item.path.endsWith('workbench-details'));
  expect(detail.some(item => item.active && new URLSearchParams(item.query).get('codes') === stockA)).toBe(true);
  await page.clock.runFor(2_000);
  await expect.poll(async () => {
    // React may install the preload timer after the first fake-clock advance.
    // Keep advancing the controlled clock until an actual request is observed.
    await page.clock.runFor(250);
    return state.activeReads.filter(item => item.path.endsWith('workbench-view')).some(item => !item.active);
  }).toBe(true);
  expect(state.activeReads.filter(item => item.path.endsWith('workbench-details') && (new URLSearchParams(item.query).get('codes') ?? '').includes(',')).every(item => !item.active)).toBe(true);
  safe(state);
});

test('the browser retains its parent observation cutoff when later data has already arrived', async ({ page }) => {
  const state = await fixture(page);
  state.observationCutoff = `${terminalDay}T14:00:00.123456+08:00`;
  state.detail = 3; state.detailName = '后到的新批次详情';
  await open(page);
  await expect(page.locator('.ty-selected')).toContainText('与主表同批详情');
  await expect(page.locator('.ty-selected')).not.toContainText(state.detailName);
  await expect(page.locator('.ty-detail-status[role=alert]')).toHaveCount(0);
  const selected = state.activeReads.filter(item => item.path.endsWith('workbench-details') && item.active);
  expect(selected.length).toBeGreaterThan(0);
  for (const read of selected) {
    const query = new URLSearchParams(read.query);
    expect(query.get('observation_cutoff')).toBe(state.observationCutoff);
    expect(query.has('as_of_time')).toBe(false);
  }
  safe(state);
});

for (const replay of [false, true]) test(`two preparation groups retain four independent warming views and ${replay ? 'the replay cutoff' : 'each parent cutoff'}`, async ({ page }) => {
  const state = await fixture(page); state.coherentWarmup = true; state.detail = 99;
  const views = ['overview', 'auction', 'ladder', 'funds'];
  state.replayCutoff = replay ? `${terminalDay}T09:50:00.654321+08:00` : '';
  let releaseSeries = () => {}, seriesHeld = false;
  const seriesGate = new Promise<void>(resolve => { releaseSeries = resolve; });
  await page.route('**/api/v1/review-data/series*', async route => {
    seriesHeld = true; await seriesGate;
    return route.fulfill({ json: { data: { ...day(days[1]), trade_days: [], records: [] }, meta: { app_mode: 'demo' } } });
  });
  try {
    await open(page, `&trade_day=${terminalDay}${replay ? '&as_of_time=' + encodeURIComponent(state.replayCutoff) : ''}`);
    await expect.poll(async () => {
      await page.clock.runFor(500);
      return [...state.preparedViews].sort();
    }).toEqual([...views].sort());
    const expectedName = replay ? '双组回放同批详情' : '双组同批 · overview';
    await expect(page.locator('.ty-selected')).toContainText(expectedName);
    await expect.poll(() => seriesHeld).toBe(true);
    await page.clock.runFor(1_000);
    // The held period group coexists with the four-page group; after that
    // group completes, another independent module progresses before release.
    const notices = await openNotifications(page);
    await notices.getByRole('button', { name: '查看准备进度', exact: true }).click();
    const progress = page.getByRole('dialog', { name: '工作空间后台准备' });
    await expect(progress.locator('li').filter({ hasText: '工作台 · 四个观察页面' })).toHaveAttribute('data-state', 'ready');
    expect(await progress.locator('li[data-state="loading"]').count()).toBeLessThanOrEqual(2);
    await expect(progress.locator('li').filter({ hasText: '资讯中心' })).toHaveAttribute('data-state', 'ready');
    await page.getByRole('button', { name: '收起进度 · 后台继续', exact: true }).click();
    await expect(page.locator('.ty-selected')).toContainText(expectedName);
    const selected = state.activeReads.filter(item => item.path.endsWith('workbench-details') && item.active);
    expect(selected.length).toBeGreaterThan(0);
    for (const read of selected) {
      const params = new URLSearchParams(read.query);
      expect(params.get('day')).toBe(terminalDay);
      if (replay) {
        expect(params.get('as_of_time')).toBe(state.replayCutoff);
        expect(params.has('observation_cutoff')).toBe(false);
      } else {
        expect(params.get('observation_cutoff')).toBe(`${terminalDay}T09:55:00.123456+08:00`);
        expect(params.has('as_of_time')).toBe(false);
      }
    }
    expect(views.every(view => state.warmingAttempts[view] >= 2)).toBe(true);
    safe(state);
  } finally { releaseSeries(); }
});

test('independent detail revision catches up through bounded retries without mixing versions', async ({ page }) => {
  const state = await fixture(page); const cutoff = `${terminalDay}T10:00:00+08:00`;
  const initialBoard = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/sector-membership') && new URL(response.url()).searchParams.get('sector') === sectorA);
  await open(page, `&trade_day=${terminalDay}&as_of_time=${encodeURIComponent(cutoff)}`);
  await (await initialBoard).finished();
  await page.evaluate(() => Promise.resolve());
  await expect(page.locator('.ty-detail-status')).toHaveCount(0);
  const selectedReads = () => state.details.filter(query => new URLSearchParams(query).get('codes') === stockA).length;
  let elapsed = 0;
  for (const delay of [2_000, 5_000, 15_000]) {
    // Initial stock and board reads may now complete independently. Count
    // only this selected stock, rather than treating a board read as a retry.
    const before = selectedReads();
    await page.clock.runFor(delay);
    await expect.poll(selectedReads).toBe(before + 1);
    elapsed += delay;
    await expect(page.locator('.ty-detail-status')).toHaveCount(0);
    if (elapsed >= 15_000) await expect(await openNotifications(page)).toContainText('详情准备较慢');
    await expect(page.locator('.ty-selected')).not.toContainText('旧版本详情');
  }
  state.detail = state.parent; state.detailName = '详情已经追平';
  await page.clock.runFor(30_000); await expect(page.locator('.ty-selected')).toContainText('详情已经追平');
  await expect(page.locator('.ty-detail-status[role=alert]')).toHaveCount(0);
  for (const query of state.details) { const params = new URLSearchParams(query); expect(params.get('day')).toBe(terminalDay); expect(params.get('as_of_time')).toBe(cutoff); }
  expect(state.memberships.length).toBeGreaterThan(0);
  for (const query of state.memberships) { const params = new URLSearchParams(query); expect(params.get('day')).toBe(terminalDay); expect(params.get('as_of_time')).toBe(cutoff); }
  safe(state);
});

test('selected details recover automatically after a temporary503', async ({ page }) => {
  const state = await fixture(page); state.detail = state.parent; state.fail = 503;
  await open(page); await expect(await openNotifications(page)).toContainText('合成详情暂时不可读');
  state.fail = 0; state.detailName = '临时失败后恢复';
  await page.clock.runFor(2_000);
  await expect(page.locator('.ty-selected')).toContainText('临时失败后恢复');
  await expect(page.locator('.ty-detail-status[role=alert]')).toHaveCount(0); safe(state);
});

test('offline detail preloads stop and the visible failed selection recovers on reconnect', async ({ page, context }) => {
  const state = await fixture(page); state.detail = state.parent; state.fail = 503;
  state.detailCodes = Array.from({ length: 160 }, (_, index) => `${String(1000 + index).padStart(6, '0')}.SZ`);
  await open(page); await expect(await openNotifications(page)).toContainText('合成详情暂时不可读');
  await context.setOffline(true); expect(await page.evaluate(() => navigator.onLine)).toBe(false);
  const before = state.details.length;
  for (let i = 0; i < 20; i++) await page.clock.runFor(1_000);
  expect(state.details).toHaveLength(before);
  await expect(page.locator('.ty-terminal')).toBeVisible();
  state.fail = 0; state.detailName = '联网后自动恢复的当前详情';
  await context.setOffline(false);
  await expect(page.locator('.ty-selected')).toContainText(state.detailName);
  await expect(page.locator('.ty-detail-status[role=alert]')).toHaveCount(0);
  expect(state.details.length - before).toBeLessThanOrEqual(2); safe(state);
});

test('temporary background failures back off across new preload batches', async ({ page }) => {
  const state = await fixture(page); state.detail = state.parent; state.backgroundFail = true;
  state.detailCodes = Array.from({ length: 160 }, (_, index) => `${String(1000 + index).padStart(6, '0')}.SZ`);
  await open(page); await expect(page.locator('.ty-selected')).toContainText(state.detailName);
  const background = () => state.activeReads.filter(item => item.path.endsWith('workbench-details') && !item.active).length;
  for (let i = 0; i < 20; i++) await page.clock.runFor(1_000);
  expect(background()).toBeGreaterThan(0); expect(background()).toBeLessThanOrEqual(4);
  await expect(page.locator('.ty-selected')).toContainText(state.detailName);
  const failed = background(); state.backgroundFail = false;
  for (let i = 0; i < 32; i++) await page.clock.runFor(1_000);
  expect(background()).toBeGreaterThan(failed); safe(state);
});

test('a selected detail publication retries its failed read before the timer without reloading the parent', async ({ page }) => {
  const state = await fixture(page); state.detail = state.parent; state.fail = 503;
  await open(page); await expect(await openNotifications(page)).toContainText('合成详情暂时不可读');
  const parents = state.activeReads.filter(item => item.path.endsWith('workbench-view')).length;
  state.fail = 0; state.detailName = '发布后立即恢复';
  await page.evaluate(path => window.dispatchEvent(new CustomEvent('read-cache-invalidated', { detail: [path] })),
    '/api/v1/workbench-details?' + new URLSearchParams({ day: terminalDay, codes: stockA }));
  await expect(page.locator('.ty-selected')).toContainText('发布后立即恢复');
  expect(state.activeReads.filter(item => item.path.endsWith('workbench-view'))).toHaveLength(parents);
  safe(state);
});

test('a new parent revision aborts the retiring detail request and ignores its late response', async ({ page }) => {
  const state = await fixture(page); state.hold = true;
  await open(page); await expect.poll(() => !!state.release).toBe(true);
  const release = state.release!; state.hold = false; state.parent = state.detail = 3; state.detailName = '新版本详情';
  await page.evaluate(() => window.dispatchEvent(new Event('read-cache-revalidate')));
  await expect(page.locator('.ty-selected')).toContainText('新版本详情');
  await expect.poll(() => state.aborted.length).toBeGreaterThan(0); release();
  await page.clock.runFor(30_000);
  await expect(page.locator('.ty-selected')).toContainText('新版本详情');
  await expect(page.getByText('旧版本详情', { exact: true })).toHaveCount(0); safe(state);
});

for (const status of [401, 403]) test(`detail recovery stops after${status} and does not retry under revoked authorization`, async ({ page }) => {
  const state = await fixture(page); state.fail = status;
  await page.goto(`/workbench?stock=${stockA}&sector=${sectorA}`); await expect.poll(() => state.details.length).toBeGreaterThan(0);
  if (status === 401) await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  else await expect(await openNotifications(page)).toContainText('读取权限已变更');
  const count = state.details.length;
  await page.clock.runFor(60_000); expect(state.details).toHaveLength(count);
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await page.clock.runFor(30_000); expect(state.details).toHaveLength(count);
  await expect(page.getByText('旧版本详情', { exact: true })).toHaveCount(0); safe(state);
});

for (const context of ['date', 'cutoff', 'owner'] as const) test(`changing${context} cancels the old detail context before accepting a new response`, async ({ page }) => {
  const state = await fixture(page); state.hold = true;
  await open(page); await expect.poll(() => !!state.release).toBe(true);
  const release = state.release!; state.hold = false; state.detail = state.parent; state.detailName = `${context}新上下文详情`;
  if (context === 'owner') {
    state.owner = 'detail-owner-b'; await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  } else await page.evaluate(kind => {
    const url = new URL(location.href);
    if (kind === 'date') url.searchParams.set('trade_day', '2001-12-28');
    else url.searchParams.set('as_of_time', '2001-12-31T10:00:00+08:00');
    history.pushState({}, '', url); window.dispatchEvent(new PopStateEvent('popstate'));
  }, context);
  await expect(page.locator('.ty-selected')).toContainText(`${context}新上下文详情`);
  await expect.poll(() => state.aborted.length).toBeGreaterThan(0); release();
  await page.clock.runFor(30_000);
  await expect(page.locator('.ty-selected')).toContainText(`${context}新上下文详情`);
  await expect(page.locator('.ty-selected')).not.toContainText('旧版本详情'); safe(state);
});

function health(state = 'normal'): DataHealth {
  return { id: '00000000-0000-4000-8000-000000000001', capability: 'stock_quote', scope: { exchange: 'SZSE' }, trade_day: '2026-10-27',
    state, stage: 'source', primary_source_id: '11111111-0000-4000-8000-000000000001', active_source_id: '22222222-0000-4000-8000-000000000002',
    last_valid_at: '2026-10-27T09:59:00+08:00', last_source_time: '2026-10-27T09:58:00+08:00', reason: null,
    next_action: 'probe_primary', next_action_at: '2026-10-27T10:01:00+08:00', missing_count: 0,
    updated_at: '2026-10-27T10:00:00+08:00', impact_modules: ['工作台'] };
}
test('the existing header shows checked normal fallback recovering and attention states without collecting', async ({ page }, info) => {
  const state = await fixture(page); state.detail = state.parent; state.health = [health()]; state.healthState = 'normal';
  await open(page); const indicator = page.getByRole('link', { name: '数据运行状态', exact: true });
  await expect(indicator).toContainText('正常'); await expect(indicator).toHaveAttribute('data-health-state', 'normal');
  for (const [value, label] of [['fallback', '备用来源运行'], ['recovering', '正在恢复'], ['attention', '需要处理']]) {
    state.healthState = value; state.health[0].state = value;
    await page.clock.runFor(30_000); await expect(indicator).toContainText(label);
  }
  await page.screenshot({ path: info.outputPath('health-header-1440.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(indicator).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: info.outputPath('health-header-390.png') });
  safe(state);
});

test('an unreadable health response cannot leave an old normal indicator and recovers on the next read', async ({ page }) => {
  const state = await fixture(page); state.detail = state.parent; state.health = [health()]; state.healthState = 'normal';
  await open(page); const indicator = page.getByRole('link', { name: '数据运行状态', exact: true });
  await expect(indicator).toHaveAttribute('data-health-state', 'normal');
  state.healthFail = true; await page.clock.runFor(30_000);
  await expect(indicator).toContainText('状态待核'); await expect(indicator).toHaveAttribute('data-health-state', 'unknown');
  state.healthFail = false; await page.clock.runFor(30_000);
  await expect(indicator).toHaveAttribute('data-health-state', 'normal'); safe(state);
});

test('old and empty health contracts remain unchecked rather than implying normal', async ({ page }) => {
  const state = await fixture(page); state.detail = state.parent; state.oldHealthContract = true;
  await open(page); const indicator = page.getByRole('link', { name: '数据运行状态', exact: true });
  await expect(indicator).toContainText('尚未检查');
  state.oldHealthContract = false; state.healthState = 'normal';
  await page.clock.runFor(30_000); await expect(indicator).toContainText('尚未检查');
  state.health = [health('unexpected')]; state.healthState = 'unexpected';
  await page.clock.runFor(30_000); await expect(indicator).toHaveAttribute('data-health-state', 'unknown'); safe(state);
});

test('monitoring shares the default health read and keeps historical checks and planned actions explicit', async ({ page }, info) => {
  const state = await fixture(page); state.detail = state.parent; state.health = [health('fallback')]; state.healthState = 'fallback';
  await page.goto('/data?view=monitor'); const panel = page.getByRole('region', { name: '数据运行与恢复', exact: true });
  await expect(panel).toContainText('备用来源运行');
  await panel.getByText('查看核验与恢复记录（1 项）', { exact: true }).click();
  await expect(panel).toContainText('核验主来源是否恢复'); await expect(panel).toContainText('工作台');
  await expect(panel).toContainText('来源 11111111'); await expect(panel).toContainText('来源 22222222');
  await page.screenshot({ path: info.outputPath('health-monitor-1440.png'), fullPage: true });
  const count = state.healthRequests.length;
  await page.clock.runFor(30_000); await expect.poll(() => state.healthRequests.length).toBe(count + 1);
  await page.getByLabel('补漏数据日期', { exact: true }).fill('2001-12-28');
  await expect(panel).toContainText('所选日运行状态 · 2001-12-28'); await expect(panel).toContainText('历史资料');
  await expect(page.getByRole('region', { name: '数据完整性与补漏记录', exact: true })).toBeVisible();
  await page.getByLabel('补漏数据日期', { exact: true }).fill('2099-01-01');
  await expect(panel.locator('.dc-health-summary')).toContainText('尚未检查'); safe(state);
});

test('monitoring explains an overdue health check without rewriting its time and keeps unknown codes collapsed', async ({ page }) => {
  const state = await fixture(page); state.detail = state.parent; state.healthState = 'attention';
  state.health = [
    { ...health('recovering'), stage: 'monitor', reason: 'health_check_overdue', next_action: 'repair_queue', updated_at: '2026-10-27T09:57:00+08:00' },
    { ...health('attention'), id: '00000000-0000-4000-8000-000000000002', stage: 'future_stage', reason: 'future_diagnostic_code' },
  ];
  await page.goto('/data?view=monitor'); const panel = page.getByRole('region', { name: '数据运行与恢复', exact: true });
  await panel.getByText('查看核验与恢复记录（2 项）', { exact: true }).click();
  const overdue = panel.locator('[data-health-id="00000000-0000-4000-8000-000000000001"]');
  await expect(overdue).toContainText('正在恢复'); await expect(overdue).toContainText('运行状态核验');
  await expect(overdue).toContainText('运行状态未按时完成核验'); await expect(overdue).toContainText('恢复任务队列');
  await expect(overdue).toContainText('核验于 2026/10/27 09:57:00');
  await expect(overdue.getByText('health_check_overdue', { exact: true })).toHaveCount(0);
  const unknown = panel.locator('[data-health-id="00000000-0000-4000-8000-000000000002"]');
  await expect(unknown).toContainText('后台已记录待核原因'); await expect(unknown).toContainText('阶段待核');
  await expect(unknown.getByText('future_diagnostic_code', { exact: true })).toBeHidden();
  await unknown.getByText('诊断信息', { exact: true }).click();
  await expect(unknown.getByText('future_diagnostic_code', { exact: true })).toBeVisible();
  await expect(unknown.getByText('future_stage', { exact: true })).toBeVisible(); safe(state);
});


test('sector failure never discards successful stock detail and the sector recovers automatically', async ({ page }) => {
  const state = await fixture(page); state.detail = state.parent; state.sectorFail = true; state.detailName = '个股独立有效详情';
  await open(page, `&view=overview&trade_day=${terminalDay}`);
  const selected = page.locator('.ty-selected'), tracking = page.getByRole('region', { name: '板块追踪', exact: true });
  await expect(selected).toContainText('个股独立有效详情');
  await expect(tracking.getByRole('status')).toContainText('合成板块读取暂不可用');
  await expect(tracking.getByRole('table')).toHaveCount(0);
  await expect.poll(() => state.memberships.some(query => new URLSearchParams(query).get('sector') === sectorA)).toBe(true);
  const stockReads = state.details.filter(query => new URLSearchParams(query).get('codes') === stockA).length;
  const memberships = state.memberships.length;
  state.sectorFail = false;
  // Lightweight membership follows useCachedLoader's existing 30-second
  // recovery policy; stock details keep their separate revision/backoff path.
  await page.clock.runFor(30_000);
  await expect.poll(() => state.memberships.length).toBeGreaterThan(memberships);
  await expect(tracking).toContainText('合成甲');
  await expect(tracking.locator('.ty-sector-tracking-state')).toHaveCount(0);
  await expect(selected).toContainText('个股独立有效详情');
  expect(state.details.filter(query => new URLSearchParams(query).get('codes') === stockA)).toHaveLength(stockReads);
  for (const query of state.memberships) { const params = new URLSearchParams(query); expect(params.get('day')).toBe(terminalDay); expect(params.get('sector')).toBe(sectorA); }
  safe(state);
});
