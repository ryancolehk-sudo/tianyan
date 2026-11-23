import { expect, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { test } from './isolated-browser.fixtures';
import { stockA, stockB, terminalDay, terminalFixture } from './terminalTestFixtures';
import { ladderFixture } from './ladderTestFixtures';

type Trace = { type: string; boot?: string; monotonicMs?: number; storageFailed?: boolean; checkerVersion?: string; checkerImplementationSha256?: string; historicalProofBaselineSha256?: string; detail?: { authenticated?: boolean; view?: string; reason?: string; primaryMatches?: boolean; readyForFreshPaint?: boolean; revision?: string; checkerVersion?: string; checkerImplementationSha256?: string; regions?: { name: string; passed: boolean; expectedSha256: string; observedSha256: string }[] } };
async function setup(page: Page, stockCount = 2) {
  let signed = false;
  let actor = 'synthetic-acceptance-owner', scope = 'acceptance-private-scope-a';
  const csrf = crypto.randomUUID();
  const meta = { request_id: 'acceptance-request-1', app_mode: 'real', generated_at: `${terminalDay}T18:00:00+08:00`,
    quality: { status: 'partial', reasons: ['synthetic-only'] }, as_of_time: null,
    read_cache: { revision: 'acceptance-r1', state: 'fresh' } };
  await page.route('**/api/v1/**', route => {
    const url = new URL(route.request().url());
    const ok = (data: unknown) => route.fulfill({ json: { data, meta } });
    if (url.pathname === '/api/v1/session') {
      if (route.request().method() === 'POST') signed = true;
      if (route.request().method() === 'DELETE') signed = false;
      return signed ? ok({ user: { id: actor, username: 'synthetic', role: 'viewer' },
        csrf_token: csrf, cache_scope: scope }) : route.fulfill({ status: 401, json: { error: { message: '请登录' } } });
    }
    if (url.pathname === '/api/v1/workbench-view') {
      const data = terminalFixture(url.searchParams);
      data.stocks.push(...Array.from({ length: stockCount - 2 }, (_, index) => ({ ...data.stocks[1],
        code: `${600000 + stockCount - index}.SH`, name: `合成分页${index + 1}`, pct_change: index % 3 ? null : -Math.floor(index / 6) - 1 })));
      data.sectors = data.sectors.map(row => ({ ...row, category: 'industry', industry_level: 2 }));
      return ok({ ...data, detail_revision: 'details-r1', detail_codes: [stockA, stockB] });
    }
    if (url.pathname === '/api/v1/ladder-observation') return ok(ladderFixture());
    if (url.pathname === '/api/v1/ths-funds') return ok({ sampled_day: terminalDay, status: 'available',
      received_at: `${terminalDay}T14:00:00+08:00`, complete: true, missing_pages: [], rejected_rows: 0, last_error: null,
      rows: [{ code: '881001', name: '合成同花顺板块', net_cny: '123000000', pct_change: '1', company_count: '2', received_at: `${terminalDay}T14:00:00+08:00` }], history: [] });
    if (url.pathname === '/api/v1/auction-support') return ok({ trade_day: terminalDay, state: 'available',
      previous_trade_day: '2001-12-28', rows: [], rankings: {}, gaps: [], coverage: [], down_days: [] });
    if (url.pathname === '/api/v1/auction-reference') return ok({ trade_day: terminalDay, report: null, reason: '合成测试未提供封单报告' });
    if (url.pathname === '/api/v1/workbench-details') {
      const data = terminalFixture(url.searchParams);
      return ok({ trade_day: data.trade_day, revision: 'details-r1', stocks: data.stocks, auction_days: data.auction_days,
        events: data.events, seals: {}, members: [], sector_code: null, members_trade_day: null, funds_history: [] });
    }
    if (url.pathname === '/api/v1/ai/settings') return ok({ enabled: false, features: [] });
    return ok({ items: [], next_cursor: null });
  });
  return { csrf, actorB: () => { actor = 'synthetic-acceptance-owner-b'; scope = 'acceptance-private-scope-b'; } };
}
async function enter(page: Page, run?: string) {
  await page.goto('/workbench?view=overview' + (run ? '&acceptance_run=' + run : ''));
  await page.getByLabel('账号', { exact: true }).fill('synthetic');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password-do-not-record');
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.locator('.ty-terminal-overview .ty-rank-table tbody tr').first()).toBeVisible();
}
async function exportTrace(page: Page) {
  const ready = page.waitForEvent('download');
  await page.getByRole('button', { name: '保存本机验收记录', exact: true }).click();
  const download = await ready, path = await download.path();
  if (!path) throw new Error('local acceptance download missing');
  const raw = await readFile(path, 'utf8');
  return { raw, rows: raw.trim().split('\n').map((line: string) => JSON.parse(line)) as Trace[] };
}
async function traces(page: Page) {
  return page.evaluate(async () => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('tianyan-local-acceptance-v1', 1);
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    try { return await new Promise<Trace[]>((resolve, reject) => {
      const request = database.transaction('records', 'readonly').objectStore('records').getAll();
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    }); } finally { database.close(); }
  });
}

test('ordinary pages do not start a local recorder or add acceptance attributes', async ({ page }) => {
  await setup(page); await enter(page);
  await expect(page.getByRole('button', { name: '保存本机验收记录' })).toHaveCount(0);
  await expect(page.locator('.ty-terminal-overview')).not.toHaveAttribute('data-acceptance-read-key');
  expect(await page.evaluate(async () => (await indexedDB.databases()).some(row => row.name === 'tianyan-local-acceptance-v1'))).toBe(false);
});

test('ordinary tab navigation proves the supported overview and leaves independently bound pages unverifiable', async ({ page }) => {
  await setup(page); await enter(page, 'synthetic-four-views');
  await expect.poll(async () => (await traces(page)).some(row => row.type === 'primary_painted' && row.detail?.view === 'overview' && row.detail.primaryMatches)).toBe(true);
  for (const [view, label, reason] of [['auction', '竞价观察', 'independent_auction_responses_not_bound'],
    ['ladder', '连板观察', 'independent_ladder_response_not_bound'], ['funds', '资金动向', 'independent_ths_response_not_bound']]) {
    await page.getByRole('navigation', { name: '工作台页面', exact: true }).getByRole('link', { name: label, exact: true }).click();
    await expect.poll(async () => (await traces(page)).some(row => row.type === 'unverifiable' && row.detail?.view === view && row.detail.reason === reason)).toBe(true);
    expect((await traces(page)).filter(row => row.detail?.view === view && row.detail.primaryMatches === true)).toHaveLength(0);
  }
  const exported = (await exportTrace(page)).rows;
  expect([...new Set(exported.filter(row => row.type === 'primary_painted' && row.detail?.primaryMatches).map(row => row.detail!.view))]).toEqual(['overview']);
  for (const view of ['auction', 'ladder', 'funds']) expect(exported.some(row => row.type === 'unverifiable' && row.detail?.view === view)).toBe(true);
});

test('overview verifies all fifty first-page rows including nulls and refuses nondefault controls as evidence', async ({ page }) => {
  await setup(page, 60); await enter(page, 'synthetic-default-overview');
  const table = page.getByRole('table', { name: '个股涨幅榜', exact: true });
  await expect(table.locator('tbody tr')).toHaveCount(50);
  await expect(table.locator('tbody tr td:nth-child(4)').filter({ hasText: /^—$/ }).first()).toBeVisible();
  const passed = () => expect.poll(async () => (await traces(page)).some(row => row.type === 'primary_painted' && row.detail?.primaryMatches)).toBe(true);
  await passed();
  const nondefault = async (change: () => Promise<unknown>) => {
    const before = Math.max(0, ...(await traces(page)).map(row => row.monotonicMs ?? 0)); await change();
    let boundary = Infinity;
    await expect.poll(async () => {
      const record = (await traces(page)).find(row => (row.monotonicMs ?? 0) > before && row.type === 'unverifiable' && row.detail?.reason === 'overview_controls_outside_supported_default');
      if (record) boundary = record.monotonicMs!; return !!record;
    }).toBe(true);
    expect((await traces(page)).filter(row => (row.monotonicMs ?? 0) >= boundary && row.type === 'primary_painted')).toHaveLength(0);
  };
  const defaultAgain = async (change: () => Promise<unknown>) => {
    const boundary = Math.max(0, ...(await traces(page)).map(row => row.monotonicMs ?? 0)); await change();
    await expect.poll(async () => (await traces(page)).some(row => (row.monotonicMs ?? 0) >= boundary && row.type === 'primary_painted' && row.detail?.primaryMatches)).toBe(true);
  };
  await nondefault(() => table.getByRole('button', { name: '涨幅', exact: true }).click());
  await defaultAgain(() => table.getByRole('button', { name: '涨幅', exact: true }).click());
  await nondefault(() => page.getByRole('textbox', { name: '个股涨幅榜搜索', exact: true }).fill('合成未匹配筛选'));
  await defaultAgain(() => page.getByRole('textbox', { name: '个股涨幅榜搜索', exact: true }).fill(''));
  await nondefault(() => page.getByRole('combobox', { name: '个股涨幅榜范围', exact: true }).selectOption('watch'));
  await defaultAgain(() => page.getByRole('combobox', { name: '个股涨幅榜范围', exact: true }).selectOption('all'));
  await nondefault(() => page.getByRole('button', { name: '个股涨幅榜下一页', exact: true }).click());
  await defaultAgain(() => page.getByRole('button', { name: '个股涨幅榜上一页', exact: true }).click());
});

test('local proofs bind the rendered response, reject a wrong row and missing indices, and export no credentials', async ({ page }) => {
  const state = await setup(page); await enter(page, 'synthetic-proof-1');
  await expect(page.getByRole('button', { name: '保存本机验收记录' })).toBeVisible();
  await expect.poll(async () => (await traces(page)).some(row => row.type === 'primary_painted' && row.detail?.primaryMatches)).toBe(true);
  await page.getByRole('table', { name: '个股涨幅榜', exact: true }).locator('tbody tr').first().locator('td').nth(3).evaluate(node => { node.textContent = '+987.65%'; });
  await expect.poll(async () => (await traces(page)).some(row => row.type === 'primary_painted' && row.detail?.regions?.some(region => region.name === 'pct_change' && !region.passed))).toBe(true);
  await page.locator('.ty-terminal-overview .ty-rank-table').first().locator('tbody tr').last().evaluate(node => node.remove());
  await expect.poll(async () => (await traces(page)).some(row => row.detail?.regions?.some(region => region.name === 'pct_change' && 'observedRows' in region && 'expectedRows' in region && Number(region.observedRows) < Number(region.expectedRows)))).toBe(true);
  await page.locator('.ty-terminal-overview .ty-index').evaluateAll(nodes => nodes.forEach(node => node.remove()));
  await expect.poll(async () => (await traces(page)).some(row => row.detail?.regions?.some(region => region.name === 'indices' && !region.passed))).toBe(true);
  const exported = await exportTrace(page);
  expect(exported.rows[0].storageFailed).toBe(false);
  const checkerSource = await readFile(resolve(test.info().project.testDir, '../../src/shared/workbenchAcceptanceProof.ts'), 'utf8');
  const checkerHash = createHash('sha256').update(checkerSource, 'utf8').digest('hex');
  expect(exported.rows[0].checkerVersion).toBe('overview-default-first-page-v2-20261114');
  expect(exported.rows[0].checkerImplementationSha256).toBe(checkerHash);
  expect(exported.rows[0].historicalProofBaselineSha256).toBe('5ac0b47b07e4d9d10473ada35dece251df5eb05f2aeaadec4292f97e44d8323b');
  for (const privateValue of ['synthetic-password-do-not-record', state.csrf, 'synthetic-acceptance-owner', 'acceptance-private-scope-a', stockA, stockB]) expect(exported.raw).not.toContain(privateValue);
  const passed = exported.rows.find(row => row.type === 'primary_painted' && row.detail?.primaryMatches);
  expect(passed?.detail?.revision).toBe('acceptance-r1');
  expect(passed?.detail?.checkerImplementationSha256).toBe(checkerHash);
  expect(passed?.detail?.regions?.every(region => /^[a-f0-9]{64}$/.test(region.expectedSha256) && region.expectedSha256 === region.observedSha256)).toBe(true);
  expect(exported.rows.some(row => row.detail?.primaryMatches === false)).toBe(true);
});

test('reloads retain earlier proof epochs, while logout stops recording market proofs', async ({ page }) => {
  await setup(page); await enter(page, 'synthetic-proof-2');
  await expect.poll(async () => (await traces(page)).filter(row => row.type === 'primary_painted').length).toBeGreaterThan(0);
  await page.reload();
  await expect(page.locator('.ty-terminal-overview .ty-rank-table tbody tr').first()).toBeVisible();
  await expect.poll(async () => new Set((await traces(page)).filter(row => row.type === 'primary_painted').map(row => row.boot)).size).toBe(2);
  const currentBoot = (await traces(page)).at(-1)!.boot;
  await page.getByRole('button', { name: '退出', exact: true }).click();
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '保存本机验收记录', exact: true })).toBeHidden();
  await expect.poll(async () => {
    const records = await traces(page);
    const boundaries = records.filter(row => row.boot === currentBoot && row.type === 'session_boundary' && row.detail?.authenticated === false);
    return boundaries.length > 0 && records.some(row => row.boot === currentBoot && row.type === 'heartbeat' &&
      (row.monotonicMs ?? 0) > Math.max(...boundaries.map(row => row.monotonicMs ?? 0)));
  }, { timeout: 10_000 }).toBe(true);
  const records = await traces(page);
  const boundary = Math.min(...records.filter(row => row.boot === currentBoot && row.type === 'session_boundary' && row.detail?.authenticated === false).map(row => row.monotonicMs!));
  expect(Number.isFinite(boundary)).toBe(true);
  expect(records.filter(row => row.boot === currentBoot && row.type === 'primary_painted' && row.monotonicMs! > boundary)).toHaveLength(0);
});

test('a journal storage failure stays visible and cannot become a successful recording', async ({ page }) => {
  await page.addInitScript(() => { Object.defineProperty(window, 'indexedDB', { configurable: true, value: { open: () => { throw new DOMException('Synthetic storage failure', 'QuotaExceededError'); } } }); });
  await setup(page); await enter(page, 'synthetic-proof-3');
  await expect(page.getByRole('button', { name: '保存本机验收记录', exact: true })).toHaveText('验收记录中断 · 保存已有记录');
  expect((await exportTrace(page)).rows[0].storageFailed).toBe(true);
});

test('a new account scope cannot continue or export the former account recorder', async ({ page }) => {
  const state = await setup(page); await enter(page, 'synthetic-account-boundary');
  await expect.poll(async () => (await traces(page)).some(row => row.type === 'primary_painted' && row.detail?.primaryMatches)).toBe(true);
  await page.getByRole('button', { name: '退出', exact: true }).click();
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible(); state.actorB();
  await page.getByLabel('账号', { exact: true }).fill('synthetic');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password-do-not-record');
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.locator('.ty-terminal-overview .ty-rank-table tbody tr').first()).toBeVisible();
  await expect(page.getByRole('button', { name: '保存本机验收记录', exact: true })).toBeHidden();
  expect(await page.locator('html').getAttribute('data-acceptance')).toBe('failed');
  expect(await page.evaluate(async () => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => { const request = indexedDB.open('tianyan-local-acceptance-v1', 1); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
    try { return await new Promise<boolean>((resolve, reject) => { const request = database.transaction('records', 'readonly').objectStore('records').getAll(); request.onsuccess = () => resolve(request.result.some(row => row.scope === 'acceptance-private-scope-b')); request.onerror = () => reject(request.error); }); } finally { database.close(); }
  })).toBe(false);
});
