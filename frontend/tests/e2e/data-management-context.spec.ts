import { expect } from '@playwright/test';
import { test, openNotifications } from './isolated-browser.fixtures';
import { terminalFixture } from './terminalTestFixtures';
import { monitorHref } from '../../src/shared/dataMonitorLink';
import { day as reviewDay, blank as blankNotes, days as reviewDays } from './reviewTestFixtures';

const sourceA = '00000000-0000-4000-8000-000000000201';
const sourceB = '00000000-0000-4000-8000-000000000202';
const day = '2026-11-06', at = '2026-11-09T10:00:00Z';
const cutoff = day + 'T15:05:00+08:00';

async function fixture(page: import('@playwright/test').Page) {
  const state = { writes: [] as string[], reads: [] as URLSearchParams[], calls: [] as URLSearchParams[], errors: [] as string[], mismatch: false };
  const origin = new URL(test.info().project.use.baseURL as string).origin;
  page.on('pageerror', error => state.errors.push(error.message));
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    if (url.origin !== origin) return route.abort('blockedbyclient');
    if (!path.startsWith('/api/') && !path.startsWith('/health/')) return route.continue();
    if (request.method() !== 'GET') { state.writes.push(request.method() + ' ' + path); return route.fulfill({ status: 405 }); }
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'demo', request_id: 'synthetic',
      generated_at: at, quality: { status: 'partial', reasons: ['合成隔离测试'] } } } });
    if (path === '/api/v1/session') return ok({ user: { id: 'data-context-owner', username: 'synthetic', role: 'admin' }, csrf_token: 'synthetic' });
    if (path === '/api/v1/integrations') return ok({ items: [{ id: 'connection-a', name: '合成来源A', kind: 'market',
      source_ids: [sourceA], capabilities: [{ source_id: sourceA, family: 'synthetic', name: '合成分钟能力' }],
      enabled: true, credential_configured: false, credential_origin: 'none', last_test: null, options: {}, revision: 1 }], templates: [], credential_store_ready: false });
    if (path === '/api/v1/providers') return ok({ items: [] });
    if (path === '/api/v1/data-sources') return ok({ items: [], available_adapters: [], collection_enabled: false, today_request_count: 0, today_cost_cny: null });
    if (path === '/api/v1/data-coverage') return ok({ items: [] });
    if (path === '/api/v1/ai/settings') return ok({ enabled: false, revision: 1, default_connection_id: null, features: [] });
    if (path === '/api/v1/data-center/capabilities') return ok({ revision: 1, policy: 'free_first', providers: [], capabilities: [] });
    if (path === '/api/v1/data-center/overview') return ok({ services_count: 1, running_tasks: 0, today_requests: 0, pending_issues: 0,
      services: [], tasks: [], issues: [], updated_at: at });
    if (path === '/api/v1/data-center/calls') { state.calls.push(url.searchParams); return ok({ items: [], next_cursor: null }); }
    if (path === '/api/v1/workbench-view') return ok(terminalFixture(url.searchParams));
    if (path === '/api/v1/chatgpt/status') return ok({ available: false, authenticated: false, local_only: true, accounts: [] });
    if (path === '/api/v1/data-recovery') {
      state.reads.push(url.searchParams);
      const records = [
        { id: 'record-a', source_id: sourceA, trade_day: day, dataset: 'minute.bars' },
        { id: 'record-b', source_id: sourceB, trade_day: day, dataset: 'review.daily' },
        { id: 'record-current', source_id: sourceA, trade_day: '2026-11-09', dataset: 'review.daily' },
      ].filter(row => (!url.searchParams.has('day') || row.trade_day === url.searchParams.get('day'))
        && (state.mismatch || !url.searchParams.has('source_id') || row.source_id === url.searchParams.get('source_id')));
      return ok({ server_time: at, items: records.map(row => ({ ...row, scope: {}, interval: '1m', state: 'partial',
        expected_count: null, actual_count: null, missing_ranges: [], job_id: null, reason: 'historical_scope_unverified', checked_at: at, next_retry_at: null })),
        counts: records.length ? { partial: records.length } : {}, has_more: false, health: [], health_state: null, workers: [] });
    }
    return ok({ items: [], quotes: [], next_cursor: null });
  });
  return state;
}

test('module guide uses current deep links and separates implemented operations from unverified conditions', async ({ page }, info) => {
  const state = await fixture(page);
  await page.goto('/data?view=guide');
  await expect(page.getByRole('heading', { name: '数据管理', exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '数据管理', exact: true })).toHaveAttribute('aria-current', 'page');
  const guide = page.getByRole('region', { name: '模块功能与数据边界表格' });
  for (const label of ['可用操作', '资料与条件待核', '尚未提供']) await expect(guide.getByRole('columnheader', { name: label })).toBeVisible();
  for (const [name, href] of [['竞价观察', '/workbench?view=auction'], ['连板观察', '/workbench?view=ladder'],
    ['每日复盘', '/research/reviews'], ['明日预案', '/research/tomorrow'], ['资讯中心', '/news']]) {
    await expect(guide.getByRole('link', { name, exact: true })).toHaveAttribute('href', href);
  }
  await expect(guide).not.toContainText('事件分析、关注管理');
  const tools = page.getByRole('radiogroup', { name: '数据管理工具' });
  const styleMetrics = [];
  for (const width of [1440, 1000, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await tools.getByRole('radio').evaluateAll(nodes => nodes.every(node => node.scrollWidth <= node.clientWidth + 1))).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (width > 1000) {
      expect((await page.locator('.sidebar').boundingBox())!.width).toBe(72);
      expect((await page.locator('.sidebar .brand img').boundingBox())!.width).toBe(44);
    } else await expect(page.locator('.sidebar')).not.toBeVisible();
    const samples = await page.evaluate(() => Object.fromEntries(Object.entries({
      number: '.data-summary strong', summaryLabel: '.data-summary span', summaryNote: '.data-summary small',
      tableBody: '.data-modules tbody td', tableHeader: '.data-modules thead th',
      moduleLink: '.data-modules th[scope=row] a', statusNote: '.research-status-note span', footerNote: '.data-modules .feature-note',
    }).map(([label, selector]) => {
      const node = document.querySelector(selector)!, style = getComputedStyle(node);
      let parent: Element | null = node, background = 'transparent';
      while (parent) { background = getComputedStyle(parent).backgroundColor; if (background !== 'rgba(0, 0, 0, 0)' && background !== 'transparent') break; parent = parent.parentElement; }
      const luminance = (value: string) => value.match(/[\d.]+/g)!.slice(0, 3).map(Number)
        .map(channel => channel / 255).map(channel => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4)
        .reduce((sum, channel, index) => sum + channel * [0.2126, 0.7152, 0.0722][index], 0);
      const light = luminance(style.color), dark = luminance(background);
      const contrast = Math.round((Math.max(light, dark) + 0.05) / (Math.min(light, dark) + 0.05) * 100) / 100;
      return [label, { fontSize: style.fontSize, color: style.color, background, lineHeight: style.lineHeight, contrast }];
    })));
    for (const [label, style] of Object.entries(samples)) {
      expect(parseFloat(style.fontSize), `${width}px ${label} font size`).toBeGreaterThanOrEqual(['summaryNote', 'statusNote', 'footerNote'].includes(label) ? 11 : 12);
      expect(style.contrast, `${width}px ${label} contrast`).toBeGreaterThanOrEqual(4.5);
    }
    styleMetrics.push({ width, samples });
    await page.screenshot({ path: info.outputPath(`data-module-guide-${width}.png`), fullPage: true });
  }
  await info.attach('data-tool-style-metrics.json', { body: JSON.stringify(styleMetrics, null, 2), contentType: 'application/json' });
  console.log('DATA_TOOL_STYLE_METRICS ' + JSON.stringify(styleMetrics));
  await tools.getByRole('radio', { name: '已入库数据', exact: true }).click();
  await expect(page.getByText('尚无已保存的参考表格')).toBeVisible();
  for (const width of [1440, 1000, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await tools.getByRole('radio').evaluateAll(nodes => nodes.every(node => node.scrollWidth <= node.clientWidth + 1))).toBe(true);
    expect(await page.locator('.dc-legacy-tools .empty').evaluate(node => getComputedStyle(node).backgroundColor)).toBe('rgb(17, 30, 39)');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`data-tools-empty-${width}.png`), fullPage: true });
  }
  await page.goBack();
  await expect(tools.getByRole('radio', { name: '接入说明', exact: true })).toHaveAttribute('aria-checked', 'true');
  await page.getByRole('button', { name: '返回数据来源', exact: true }).click();
  await expect(page.getByRole('navigation', { name: '数据管理页面' }).getByRole('button', { name: '数据来源', exact: true })).toHaveAttribute('aria-current', 'page');
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('monitor follows the exact source and data day while current runtime and historical context stay distinct', async ({ page }, info) => {
  const state = await fixture(page);
  const back = '/workbench?view=auction&trade_day=' + day + '&as_of_time=' + encodeURIComponent(cutoff);
  await page.goto(monitorHref({ sourceId: sourceA, tradeDay: day, asOfTime: cutoff, returnTo: back, context: 'history' }));
  const target = page.getByRole('region', { name: '监控定位' });
  await expect(target).toContainText('历史资料定位');
  await expect(target).toContainText('合成来源A · 合成分钟能力');
  await expect(target).toContainText('不重建当时的运行状态');
  await expect(target.getByRole('link', { name: '返回原页面' })).toHaveAttribute('href', back);
  const table = page.getByRole('region', { name: '数据完整性与补漏记录', exact: true });
  await expect(table.locator('tbody tr')).toHaveCount(1);
  await expect(table).toContainText(day);
  await expect(table).not.toContainText('2026-11-09');
  await expect.poll(() => state.reads.some(query => query.get('source_id') === sourceA && query.get('day') === day)).toBe(true);
  expect(state.reads.every(query => !query.has('as_of_time'))).toBe(true);
  await expect.poll(() => state.calls.some(query => query.get('connection_id') === 'connection-a')).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); window.scrollTo({ top: 0, behavior: 'instant' }); });
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  await page.screenshot({ path: info.outputPath('monitor-source-history-390.png'), fullPage: true });
  await target.getByRole('link', { name: '返回原页面' }).click();
  await expect(page).toHaveURL(url => url.pathname === '/workbench' && url.searchParams.get('trade_day') === day && url.searchParams.get('as_of_time') === cutoff);
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('invalid monitor filters stay visible and never silently become unfiltered results', async ({ page }) => {
  const state = await fixture(page);
  await page.goto('/data?view=monitor&source_id=invalid-source&trade_day=2026-02-30&return_to=https%3A%2F%2Foutside.invalid');
  await expect(page.getByRole('alert')).toContainText('来源编号无效');
  await expect(page.getByRole('alert')).toContainText('资料日期无效');
  await expect(page.getByRole('region', { name: '监控定位' }).getByRole('link', { name: '返回原页面' })).toHaveCount(0);
  await expect(page.locator('.dc-recovery-table tbody tr')).toHaveCount(0);
  expect(state.reads.every(query => !query.has('source_id') && !query.has('day'))).toBe(true);
  await page.getByRole('button', { name: '清除定位', exact: true }).click();
  await expect(page.locator('.dc-recovery-table tbody tr')).toHaveCount(3);
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('review evidence gaps link their actual source and data day to read-only monitoring', async ({ page }) => {
  const state = await fixture(page);
  await page.route('**/api/v1/review-data/**', route => {
    if (route.request().method() !== 'GET') { state.writes.push(route.request().method() + ' ' + new URL(route.request().url()).pathname); return route.fulfill({ status: 405 }); }
    const path = new URL(route.request().url()).pathname;
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'demo', request_id: 'synthetic',
      generated_at: at, quality: { status: 'partial', reasons: ['合成证据定位测试'] } } } });
    if (path.endsWith('/day')) return ok({ ...reviewDay(day), coverage: [{ dataset: 'daily', trade_day: day, state: 'partial',
      evidence_refs: [{ id: '00000000-0000-4000-8000-000000000901', source_id: sourceA }] }] });
    if (path.endsWith('/series')) return ok({ shared_date: day, state: 'partial', trade_days: reviewDays,
      records: reviewDays.map(trade_day => ({ trade_day, state: 'partial', metrics: reviewDay(trade_day).metrics,
        indices: [], themes: { records: [] }, coverage: [], evidence_refs: [] })) });
    if (path.endsWith('/notes')) return ok({ trade_day: day, state: 'empty', content: blankNotes(), head_id: null, writable: false, is_current: true, revision: null });
    return ok({ items: [], shared_date: day });
  });
  await page.goto('/research/reviews?view=emotion&trade_day=' + day + '&as_of_time=' + encodeURIComponent(cutoff));
  await page.getByRole('button', { name: '数据与口径', exact: true }).click();
  const coverage = page.getByRole('dialog', { name: '数据范围与缺口', exact: true });
  const link = coverage.getByRole('link', { name: `查看来源 ${sourceA} 的监控`, exact: true });
  await expect(link).toBeVisible();
  const href = new URL((await link.getAttribute('href'))!, 'http://synthetic.local');
  expect(href.searchParams.get('source_id')).toBe(sourceA);
  expect(href.searchParams.get('trade_day')).toBe(day);
  expect(href.searchParams.get('as_of_time')).toBe(cutoff);
  await link.click();
  await expect(page.getByRole('region', { name: '监控定位' })).toContainText('历史资料定位');
  await expect(page.locator('.dc-recovery-table tbody tr')).toHaveCount(1);
  await expect.poll(() => state.reads.some(query => query.get('source_id') === sourceA && query.get('day') === day)).toBe(true);
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

test('a server ignoring the source filter cannot paint unrelated records as the selected source', async ({ page }) => {
  const state = await fixture(page); state.mismatch = true;
  await page.goto(monitorHref({ sourceId: sourceA, tradeDay: day, context: 'history' }));
  const notices = await openNotifications(page);
  await expect(notices).toContainText('返回的来源或资料日期与当前定位不一致');
  await expect(page.locator('.dc-recovery-table tbody tr')).toHaveCount(0);
  await expect(page.getByText('当前日期范围暂无核验记录', { exact: true })).toHaveCount(0);
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});
