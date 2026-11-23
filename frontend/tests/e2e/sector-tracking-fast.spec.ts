import { test, expect, type Page } from '@playwright/test';
import { terminalFixture, terminalDay, stockA, stockB, sectorA, sectorB } from './terminalTestFixtures';

async function mock(page: Page, options: { holdB?: Promise<void> } = {}) {
  const reads: string[] = [], details: string[] = [];
  let pct = 3;
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'real', request_id: 'synthetic',
      generated_at: terminalDay+'T13:00:00+08:00', quality: { status: 'partial', reasons: ['合成测试'] } } } });
    if (url.pathname.endsWith('/session')) return ok({ user: { id: 'tracking-test', username: 'synthetic', role: 'admin' }, csrf_token: 'synthetic' });
    if (url.pathname.endsWith('/workbench-view')) {
      const data = terminalFixture(url.searchParams);
      return ok({ ...data, stocks: data.stocks.map(row => ({ ...row, pct_change: row.code === stockA ? pct : null,
        amount_cny:row.code===stockA?120_000_000:null, ths_themes:row.code===stockA?['合成同花顺题材']:[] })),
        sectors:[...data.sectors,{...data.sectors[0],code:'SYNTHETIC-C',name:'合成资金流入',main_net_cny:500_000_000},
          {...data.sectors[0],code:'SYNTHETIC-D',name:'合成资金缺项',main_net_cny:null}] });
    }
    if (url.pathname.endsWith('/sector-membership')) {
      const sector = url.searchParams.get('sector')!; reads.push(sector);
      if (sector === sectorB) await options.holdB;
      return ok({ trade_day: terminalDay, sector_code: sector, members_trade_day: terminalDay, state: 'available',
        members: sector === sectorA ? [{ code: stockA, name: '合成甲' }, { code: '999993.SZ', name: '缺报价成分' }]
          : [{ code: stockB, name: '合成乙' }], member_count: sector === sectorA ? 2 : 1, truncated: false });
    }
    if (url.pathname.endsWith('/workbench-details')) {
      details.push(url.search);
      return route.fulfill({ status: 503, json: { error: { code: 'read_cache_warming', message: '合成重详情排队中' } } });
    }
    if (url.pathname.endsWith('/ai/settings')) return ok({ enabled: false, features: [] });
    return ok({ items: [], next_cursor: null });
  });
  await page.goto('/workbench?workspace=monitor');
  const panel = page.locator('#ty-sector-tracking');
  await expect(panel.getByRole('button', { name: /^合成甲/ })).toBeVisible();
  return { reads, details, panel, setPct: (value: number) => { pct = value; } };
}
async function selectBoard(page: Page, code: string) {
  await page.getByRole('button', { name: /^追踪板块：/ }).click();
  await page.getByRole('listbox', { name: '可追踪板块', exact: true }).getByRole('option', { name: new RegExp(code) }).click();
}

test('cold tracking reads immediately while heavyweight details remain unavailable', async ({ page }) => {
  const { panel, details } = await mock(page);
  await expect(panel).toContainText('成分 2 只');
  await expect(panel).toContainText('+3.00%');
  const missing = panel.getByRole('row').filter({ hasText: '缺报价成分' });
  await expect(missing.getByRole('cell').nth(2)).toHaveText('—');
  await expect(missing.getByRole('cell').nth(3)).toHaveText('—');
  await selectBoard(page, sectorB);
  await expect(panel.getByRole('button', { name: /^合成乙/ })).toBeVisible({ timeout: 1000 });
  await expect(panel).not.toContainText('合成甲');
  expect(details.every(query => !new URLSearchParams(query).has('sector'))).toBe(true);
  await expect(page.getByRole('button', { name: '盘面', exact: true })).toHaveAttribute('aria-pressed', 'true');
});

test('a slow old selection cannot block a cached return or overwrite the latest board', async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const { panel, reads } = await mock(page, { holdB: gate });
  await selectBoard(page, sectorB);
  await expect.poll(() => reads.filter(code => code === sectorB).length).toBe(1);
  await expect(panel).not.toContainText('合成甲');
  await selectBoard(page, sectorA);
  await expect(panel.getByRole('button', { name: /^合成甲/ })).toBeVisible({ timeout: 1000 });
  const late = page.waitForResponse(response => response.url().includes('/sector-membership?') && new URL(response.url()).searchParams.get('sector') === sectorB);
  release(); await (await late).finished();
  await expect(panel.getByRole('button', { name: /^合成甲/ })).toBeVisible();
  await expect(panel).not.toContainText('合成乙');
  expect(reads.filter(code => code === sectorA)).toHaveLength(1);
});

test('tracking joins fresh market quotes without refetching membership', async ({ page }) => {
  await page.clock.install();
  const { panel, reads, setPct } = await mock(page);
  const count = reads.length;
  setPct(4);
  await page.clock.runFor(5100);
  await expect(panel).toContainText('+4.00%');
  expect(reads).toHaveLength(count);
  await expect(panel).not.toContainText('+3.00%');
});

test('tracking uses stock-ranking headers and sorts locally with missing values last', async ({ page }) => {
  const {panel,reads}=await mock(page);
  const stockHeaders=await page.getByRole('table',{name:'个股涨幅榜',exact:true}).locator('thead th').allTextContents();
  expect(await panel.locator('thead th').allTextContents()).toEqual(stockHeaders);
  await expect(panel).toContainText('1.2亿');
  await expect(panel).toContainText('合成同花顺题材');
  const before=reads.length;
  for(const label of ['5分涨速','涨幅','成交额','同花顺题材']) {
    const heading=panel.getByRole('columnheader').filter({hasText:label});
    await heading.getByRole('button').click();
    const firstDirection=await heading.getAttribute('aria-sort');
    await heading.getByRole('button').click();
    expect(await heading.getAttribute('aria-sort')).not.toBe(firstDirection);
    await expect(panel.locator('tbody tr').last()).toContainText('缺报价成分');
  }
  expect(reads).toHaveLength(before);
});

test('board funds sort in both directions and fit the compact workspace', async ({ page }) => {
  await page.setViewportSize({width:1920,height:1080});
  await mock(page);
  const table=page.getByRole('table',{name:'板块列表',exact:true});
  await table.getByRole('button',{name:'资金净流入',exact:true}).click();
  let rows=table.locator('tbody tr');
  await expect(rows.nth(0)).toContainText('合成资金流入');
  await expect(rows.nth(0)).toContainText('+5.0亿');
  await expect(rows.last()).toContainText('合成资金缺项');
  await table.getByRole('button',{name:'资金净流入',exact:true}).click();
  await expect(rows.nth(0)).toContainText('合成题材乙');
  await expect(rows.nth(0)).toContainText('-2.3亿');
  await expect(rows.nth(1)).toContainText('0.0亿');
  await expect(rows.last()).toContainText('合成资金缺项');
  for(const width of [1920,1440,390]) {
    await page.setViewportSize({width,height:1080});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  }
});
