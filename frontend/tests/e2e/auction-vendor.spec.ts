import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { DAY, PREV_DAY, meta, openAuction, sourceIndex, unpairedItems } from './auction-vendor.fixtures';
import { referenceItem, routeWorkbench, workbenchItems } from './auction-workbench.fixtures';

const work = (p: Page) => p.getByRole('region', { name: '竞价全景', exact: true });
const dayCard = (p: Page, day = DAY, kind = '当前表') => p.getByRole('region', { name: `${day} ${kind}`, exact: true });
const stock = (p: Page, code: string, day = DAY) => work(p).getByRole('button', { name: new RegExp(`^选择 ${day} ${code} `) });
async function ready(p: Page, options: Parameters<typeof routeWorkbench>[1] = {}) { await routeWorkbench(p, options); await openAuction(p, options.quotes); await expect(work(p)).toBeVisible(); }

test('four day overview, source totals, selected detail beside board and six modules', async ({ page }) => {
  await ready(page);
  await expect(work(page).locator('.auction-wb-day')).toHaveCount(4);
  await expect(dayCard(page).locator('.auction-wb-stock')).toHaveCount(25);
  await expect(dayCard(page).locator('.auction-wb-totals')).toContainText('59.6亿');
  await expect(work(page).getByRole('radiogroup', { name: /排序时点|排序阶段/ })).toHaveCount(0);
  const positions = await work(page).evaluate(e => ({ detail: e.querySelector('.auction-wb-detail')!.getBoundingClientRect(), board: e.querySelector('.auction-wb-board')!.getBoundingClientRect() }));
  expect(positions.detail.left).toBeGreaterThanOrEqual(positions.board.right);
  expect(Math.abs(positions.detail.top - positions.board.top)).toBeLessThan(2);
  await expect(page.getByText('原预案与竞价观察记录', { exact: true })).toBeVisible();
});

test('search tags, decimal amount sorting and independent 25-row pagination', async ({ page }) => {
  await ready(page);
  await expect(dayCard(page).locator('.auction-wb-stock').first()).toHaveAttribute('aria-label', /600030/);
  await dayCard(page).getByRole('button', { name: DAY + '下一页' }).click();
  await expect(dayCard(page).locator('.auction-wb-stock')).toHaveCount(5);
  await work(page).getByRole('searchbox').fill('机器人');
  await expect(dayCard(page).locator('.auction-wb-stock')).toHaveCount(10);
  await work(page).getByRole('searchbox').fill('');
  await work(page).getByRole('radio', { name: '升序', exact: true }).click();
  await expect(dayCard(page).locator('.auction-wb-stock').first()).toHaveAttribute('aria-label', /600001/);
  await expect(dayCard(page).locator('.auction-wb-stock').nth(24)).not.toHaveAttribute('aria-label', /600005/);
});

test('same stage grouped bars, original values and cross-day selection highlight', async ({ page }) => {
  await ready(page); await stock(page, '600030').click();
  await expect(work(page).locator('.seal-chart-heading')).toContainText('万元');
  await expect(work(page).locator('.auction-seal-chart svg')).toContainText('4,000');
  await expect(work(page).locator('[data-seal-bar="selected"]')).toHaveCount(3);
  await expect(work(page).locator('[data-seal-bar="previous"]')).toHaveCount(3);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(work(page).locator('[data-seal-label="previous"]')).toHaveCount(3);
    await expect(work(page).locator('[data-seal-label="selected"]')).toHaveCount(3);
    await expect.poll(() => work(page).locator('.auction-seal-chart svg').evaluate(e => {
      const labels = [...e.querySelectorAll<SVGTextElement>('.seal-value')];
      return labels.every(label => {
        const peer = labels.find(n => n.dataset.stage === label.dataset.stage && n !== label);
        const bar = label.parentElement!.querySelector('rect')!;
        const l = label.getBoundingClientRect(), b = bar.getBoundingClientRect();
        return Math.abs(l.top - labels[0].getBoundingClientRect().top) < .5
          && (!peer || Math.abs(l.top - peer.getBoundingClientRect().top) < .5)
          && Math.abs(l.left + l.width / 2 - b.left - b.width / 2) < .5;
      });
    })).toBe(true);
    for (const kind of ['previous', 'selected']) {
      const labels = work(page).locator(`[data-seal-label="${kind}"]`);
      for (const label of await labels.all()) {
        const bar = work(page).locator(`[data-seal-bar="${kind}"][data-value="${await label.getAttribute('data-value')}"]`).first();
        await expect(bar).toHaveCount(1);
        expect(await label.textContent()).toBeTruthy();
      }
    }
    await expect.poll(() => work(page).locator('.auction-seal-chart svg').evaluate(e => {
      const labels = [...e.querySelectorAll('.seal-value')].map(n => n.getBoundingClientRect());
      return labels.every((a, i) => labels.slice(i + 1).every(b => a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top));
    })).toBe(true);
    await work(page).locator('.auction-seal-chart').screenshot({ path: test.info().outputPath('seal-chart-' + width + '.png') });
  }
  await expect(work(page).locator('.auction-wb-stock.linked')).toHaveCount(4);
  await work(page).getByText('查看柱状图原值与单位', { exact: true }).click();
  await expect(work(page).getByRole('region', { name: '封单柱状图原值' })).toContainText('30000000');
  await work(page).getByText('来源、日期与配对批次', { exact: true }).click();
  await expect(page.getByTestId('auction-vendor-source-date')).toContainText(DAY.replaceAll('-', '/'));
  await expect(work(page).locator('.auction-wb-provenance')).toContainText('同 batch_hash');
});

test('null and zero remain different in bars and raw-value table', async ({ page }) => {
  await ready(page); await work(page).getByRole('searchbox').fill('600006'); await stock(page, '600006').click();
  await expect(work(page).locator('[data-seal-bar="selected"][data-value="0"]')).toHaveCount(1);
  await expect(work(page).locator('[data-seal-label="selected"][data-value="0"]')).toHaveCount(1);
  await expect(work(page).locator('[data-missing="selected"]')).toHaveCount(1);
  await work(page).getByRole('searchbox').fill('600005'); await stock(page, '600005').click();
  await expect(work(page).locator('[data-seal-bar="selected"]')).toHaveCount(0);
  await expect(work(page).locator('[data-seal-label="selected"]')).toHaveCount(0);
  await expect(work(page).locator('[data-missing="selected"]')).toHaveCount(3);
});

test('sort uses the latest published column as a table, with no per-row fallback', async ({ page }) => {
  const items = workbenchItems();
  for (const item of items.filter(i => i.family === 'duanxianxia_auction')) for (const row of item.payload.rows) {
    if ('total_fd_0925_cny' in row) row.total_fd_0925_cny = null;
    if ('fd_0925_cny' in row) row.fd_0925_cny = null;
  }
  const stocks = items.find(i => i.family === 'duanxianxia_auction' && i.payload.dataset.endsWith('.stocks'));
  stocks.payload.rows[0].fd_0920_cny = '999999999999999999.00000000002';
  stocks.payload.rows[1].fd_0920_cny = '999999999999999999.00000000001';
  await ready(page, { items });
  await expect(dayCard(page).locator('.auction-wb-stock').first()).toHaveAttribute('aria-label', /600001/);
  await expect(dayCard(page).locator('.auction-wb-stock').nth(1)).toHaveAttribute('aria-label', /600002/);
});

for (const kind of ['summary-only', 'stocks-only'] as const) test(`${kind} never displays unpaired totals or rows`, async ({ page }) => {
  await ready(page, { items: unpairedItems(kind) });
  await expect(work(page)).toContainText('配对缺失');
  await expect(work(page).locator('.auction-wb-stock')).toHaveCount(0);
  await expect(work(page)).not.toContainText('59.6亿');
  await expect(work(page).locator('[data-seal-bar]')).toHaveCount(0);
});

test('batch hash mismatch never combines current summary with old stocks', async ({ page }) => {
  const items = workbenchItems().filter(i => i.family === 'duanxianxia_auction');
  items.find(i => i.payload.dataset.endsWith('.summary')).payload.rows[0].batch_hash = '9'.repeat(64);
  await ready(page, { items });
  await expect(work(page)).toContainText('配对缺失');
  await expect(work(page).locator('.auction-wb-stock')).toHaveCount(0);
  await expect(work(page)).not.toContainText('53.1亿');
});

test('empty response stays empty without invented prices or totals', async ({ page }) => {
  await ready(page, { items: [] });
  await expect(work(page).locator('[data-seal-bar]')).toHaveCount(0);
  await expect(work(page)).toContainText('尚无已存封单表批次');
  await expect(work(page)).not.toContainText('53.1亿');
});

test('cached refresh does not queue collection; admin can queue once', async ({ page }) => {
  let writes = 0;
  await page.route('**/api/v1/data-sources/*/refresh', route => { writes++; return route.fulfill({ json: { meta, data: { job_id: '00000000-0000-4000-8000-00000000c0de', state: 'queued' } } }); });
  await ready(page);
  await work(page).getByRole('button', { name: '刷新页面数据', exact: true }).click();
  await expect(work(page)).toHaveAttribute('aria-busy', 'false'); expect(writes).toBe(0);
  await work(page).getByRole('button', { name: '更多设置', exact: true }).click();
  await work(page).getByRole('button', { name: '采集最新数据', exact: true }).click();
  await expect(work(page)).toContainText('采集任务已排队'); expect(writes).toBe(1);
});

test('failed reads retain rights-checked cache; authoritative empty/revocation clears it', async ({ page }) => {
  let fail = false, revoked = false;
  await ready(page, { failReads: () => fail, denied: () => revoked });
  await expect(dayCard(page).locator('.auction-wb-stock')).toHaveCount(25);
  fail = true;
  await work(page).getByRole('button', { name: '刷新页面数据', exact: true }).click();
  await expect(work(page)).toContainText('缓存读取未全部成功');
  await expect(dayCard(page).locator('.auction-wb-stock')).toHaveCount(25);
  fail = false; revoked = true;
  await work(page).getByRole('button', { name: '刷新页面数据', exact: true }).click();
  await expect(work(page).locator('.auction-wb-stock')).toHaveCount(0);
  await expect(work(page).locator('[data-seal-bar]')).toHaveCount(0);
});

test('source rights withdrawal hides even previously returned numbers', async ({ page }) => {
  const index = sourceIndex({}, [{ family: 'duanxianxia_auction', over: { collection_state: 'permission_missing' } }, { family: 'duanxianxia_auction_history', over: { collection_state: 'permission_missing' } }]);
  await ready(page, { index });
  await expect(work(page).getByRole('alert')).toContainText('展示权限已撤回');
  await expect(work(page).locator('.auction-wb-stock')).toHaveCount(0);
});

test('failed collection keeps cache, names the reason and does not claim fresh', async ({ page }) => {
  const index = sourceIndex({}, [{ family: 'duanxianxia_auction', over: { collection_state: 'error', last_attempt: { status: 'error', reason: 'timeout' } } }, { family: 'duanxianxia_auction_history' }]);
  await ready(page, { index });
  await expect(work(page)).toContainText('最近一次采集失败（上游超时）');
  await expect(work(page)).toContainText('非当前快照');
  await expect(dayCard(page).locator('.auction-wb-stock')).toHaveCount(25);
});

test('real cached Tushare rows have separate previous-day and成交核对 semantics', async ({ page }) => {
  const items = workbenchItems();
  items.push(referenceItem('tushare_limit_list_d', [
    { trade_date: PREV_DAY.replaceAll('-', ''), ts_code: '600001.SH', name: '昨日样例', limit: 'U', limit_times: '3', open_times: '1' },
    { trade_date: PREV_DAY.replaceAll('-', ''), ts_code: '600002.SH', name: '炸板样例', limit: 'Z', limit_times: null, open_times: '2' },
  ]));
  items.push(referenceItem('tushare_stk_auction', [{ trade_date: DAY.replaceAll('-', ''), ts_code: '600001.SH', amount: '987654', volume_ratio: '0', turnover_rate: null }]));
  await ready(page, { items });
  const pool = page.getByRole('region', { name: '竞价观察池', exact: true });
  await expect(pool).toContainText('昨日样例');
  await pool.getByRole('radio', { name: '炸板', exact: true }).click(); await expect(pool).toContainText('炸板样例');
  await pool.getByRole('radio', { name: '成交核对', exact: true }).click();
  await expect(pool).toContainText('987654'); await expect(pool).toContainText('换手 —');
  await pool.getByRole('combobox', { name: '竞价成交排序' }).selectOption('量比');
});

test('missing calendar never labels latest cached list as yesterday', async ({ page }) => {
  const items = workbenchItems(); items.push(referenceItem('tushare_limit_list_d', [{ trade_date: PREV_DAY.replaceAll('-', ''), ts_code: '600001.SH', name: '不得泄漏的昨日猜测', limit: 'U' }]));
  await ready(page, { items, calendar: false });
  const pool = page.getByRole('region', { name: '竞价观察池', exact: true });
  await expect(pool).toContainText('日历覆盖不足'); await expect(pool).not.toContainText('不得泄漏的昨日猜测');
});

function collectedCalendars() {
  const index = sourceIndex();
  const evidence = ['SSE', 'SZSE'].flatMap((exchange, i) => [DAY, PREV_DAY].map(trade_day => ({
    source_id: index.data.items[i].provider.id, kind: 'calendar', visibility: 'shared', provenance: 'source_reported',
    is_current: true, received_at: DAY + 'T03:00:00Z',
    payload: { exchange, trade_day, is_open: true, calendar_version: 'collected-fixture' },
  })));
  return { index, evidence };
}

test('collected calendar evidence enables prior-day bars without a manual calendar', async ({ page }) => {
  const { index, evidence } = collectedCalendars();
  await ready(page, { index, calendar: false, calendarEvidence: evidence });
  await stock(page, '600030').click();
  await expect(work(page).locator('[data-seal-bar="previous"]')).toHaveCount(3);
  await expect(page.getByRole('region', { name: '竞价观察池', exact: true })).toContainText('背景交易日：' + PREV_DAY);
});

test('collected calendar gaps and a newer partial version never borrow older days', async ({ page }) => {
  const { index, evidence } = collectedCalendars();
  evidence.find(e => e.payload.exchange === 'SSE' && e.payload.trade_day === DAY)!.payload.calendar_version = 'newer-partial';
  await ready(page, { index, calendar: false, calendarEvidence: evidence });
  await expect(page.getByRole('region', { name: '竞价观察池', exact: true })).toContainText('日历覆盖不足');
  await expect(work(page).locator('[data-seal-bar="previous"]')).toHaveCount(0);
});

test('collected calendar is removed after fresh source rights are revoked', async ({ page }) => {
  const { index, evidence } = collectedCalendars();
  await ready(page, { index, calendar: false, calendarEvidence: evidence });
  await expect(work(page).locator('[data-seal-bar="previous"]')).toHaveCount(3);
  index.data.items[0].provider.rights.cache = 'denied';
  await page.getByRole('region', { name: '竞价观察池', exact: true }).getByRole('button', { name: '刷新观察池缓存' }).click();
  await expect(work(page).locator('[data-seal-bar="previous"]')).toHaveCount(0);
});

test('details tabs link only existing data and screenshot remains an explicit gap', async ({ page }) => {
  await ready(page);
  await work(page).getByRole('radio', { name: '日K', exact: true }).click();
  await expect(work(page)).toContainText('尚未匹配唯一证券身份');
  await work(page).getByRole('radio', { name: '异动', exact: true }).click();
  await expect(work(page)).toContainText('不根据概念标签生成原因');
  await work(page).getByRole('radio', { name: '来源截图', exact: true }).click();
  await expect(work(page)).toContainText('截图接入仍缺保存与时间登记');
  await expect(work(page).getByRole('link', { name: '打开短线侠公开页面' })).toHaveAttribute('href', 'https://duanxianxia.com/web/jjlive');
});

test('desktop and mobile layouts, chart bounds and notebook remain usable', async ({ page }, info) => {
  await ready(page); await stock(page, '600030').click();
  for (const width of [1920, 1440, 1024, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect.poll(() => work(page).locator('.auction-seal-chart svg').evaluate(e => {
      const rect = e.getBoundingClientRect();
      return [...e.querySelectorAll('text')].every(t => { const b = t.getBoundingClientRect(); return b.left >= rect.left - 1 && b.right <= rect.right + 1; });
    })).toBe(true);
    await expect(work(page).locator('.auction-wb-snapshot')).toHaveCount(0);
    await work(page).screenshot({ path: info.outputPath(`auction-workbench-${width}.png`) });
  }
});


test('compact toolbar hides uncommon controls and keeps date, search and failures visible', async ({ page }, info) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await ready(page);
  await expect(work(page).getByRole('button', { name: '更多设置', exact: true })).toHaveAttribute('aria-expanded', 'false');
  await expect(work(page).getByLabel('封单表来源', { exact: true })).toBeHidden();
  await expect(work(page).getByRole('button', { name: '采集最新数据', exact: true, includeHidden: true })).toBeHidden();
  await expect(work(page).getByLabel('封单来源日期', { exact: true })).toBeVisible();
  await expect(work(page).getByRole('searchbox')).toBeVisible();
  const height = await work(page).evaluate(e => e.querySelector('.auction-wb-detail')!.getBoundingClientRect().top - e.getBoundingClientRect().top);
  expect(height).toBeLessThan(125);
  await work(page).getByRole('button', { name: '更多设置', exact: true }).click();
  await expect(work(page).getByLabel('封单表来源', { exact: true })).toBeVisible();
  await expect(work(page).getByRole('button', { name: '暂停', exact: true })).toBeVisible();
  await work(page).getByRole('button', { name: '更多设置', exact: true }).click();
  await work(page).getByRole('searchbox').fill('600030');
  await expect(dayCard(page).locator('.auction-wb-stock')).toHaveCount(1);
  await work(page).getByRole('searchbox').fill('');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`auction-compact-${width}.png`) });
  }
});


test('reference revision shows compact indices, signal colors, right detail and compact stock rows', async ({ page }, info) => {
  const items = workbenchItems();
  items.push(referenceItem('tushare_limit_list_d', [{ trade_date: DAY.replaceAll('-', ''), ts_code: '600001.SH', limit: 'U', limit_times: '4' }]));
  const breadth = { ...referenceItem('ashare_breadth', []), family: 'ashare_breadth', payload: {
    dataset: 'market.ashare.breadth', observed_day: DAY, rows: [{ limit_up: '2', limit_down: '0', broken_limit: '1', advancing: '12', declining: '8', sentiment: '99', loss_effect: '99' }], scope: '合成广度，非行情',
  } };
  const quotes = { meta, data: { selection: 'indices', items: [
    { instrument: { exchange: 'SSE', symbol: '000001', asset_type: 'index' }, state: 'available', quote: { price: '3712.83', quote_time: meta.generated_at }, change: '2.34', change_percent: '0.06' },
    { instrument: { exchange: 'SZSE', symbol: '399001', asset_type: 'index' }, state: 'available', quote: { price: '11704.35', quote_time: meta.generated_at }, change: '-5.21', change_percent: '-0.04' },
  ] } };
  await page.setViewportSize({ width: 1440, height: 1100 });
  await ready(page, { items, breadth, quotes });
  const ribbon = page.getByLabel('指数报价横栏', { exact: true });
  await expect(ribbon.locator('.index-tile')).toHaveCount(4);
  await expect(ribbon.locator('strong').first()).toHaveText('3,712.83');
  const indexRects = await ribbon.locator('.index-tile').evaluateAll(nodes => nodes.map(e => e.getBoundingClientRect().top));
  expect(new Set(indexRects).size).toBe(1);
  expect(await ribbon.evaluate(e => e.getBoundingClientRect().height)).toBeLessThan(85);
  const signals = page.getByLabel('市场广度参考', { exact: true });
  await expect(signals.locator('.signal-cell')).toHaveCount(13);
  for (const [key, value] of [['limit_up', '2'], ['limit_down', '0'], ['broken_limit', '1'], ['advancing', '12'], ['declining', '8'], ['height', '4'], ['seal_ratio', '66.7%'], ['sentiment', '—'], ['loss_effect', '—'], ['main_flow', '—']]) await expect(signals.getByTestId('breadth-' + key)).toHaveText(value);
  for (const key of ['limit_up', 'advancing']) await expect(signals.getByTestId('breadth-' + key)).toHaveCSS('color', 'rgb(217, 54, 62)');
  for (const key of ['limit_down', 'declining']) await expect(signals.getByTestId('breadth-' + key)).toHaveCSS('color', 'rgb(22, 132, 84)');
  await expect(signals.getByTestId('breadth-broken_limit')).toHaveCSS('color', 'rgb(22, 25, 29)');
  await expect(work(page).locator('.auction-wb-snapshot')).toHaveCount(0);
  await expect(dayCard(page).locator('.auction-wb-date')).toHaveText(DAY);
  await expect(dayCard(page).locator('.auction-wb-totals')).toBeHidden();
  const row = dayCard(page).locator('.auction-wb-stock').first();
  await expect(row.locator('.auction-wb-stock-label b')).toHaveCSS('color', 'rgb(38, 52, 71)');
  await expect(row.locator('.auction-wb-tag')).not.toHaveCount(0);
  await expect(row.locator('.auction-wb-amounts>span')).toHaveCount(3);
  expect(await row.evaluate(e => e.getBoundingClientRect().height)).toBeLessThan(70);
  expect(await row.evaluate(e => e.querySelector('b')!.getBoundingClientRect().left - e.getBoundingClientRect().left)).toBeLessThan(8);
  const alignment = await dayCard(page).evaluate(e => {
    const heads = [...e.querySelectorAll('.auction-wb-column-stages>span')].map(n => { const r = n.getBoundingClientRect(); return r.left + r.width / 2; });
    const values = [...e.querySelectorAll('.auction-wb-stock:first-child .auction-wb-amounts>span')].map(n => { const r = n.getBoundingClientRect(); return r.left + r.width / 2; });
    return values.map((x, i) => Math.abs(x - heads[i]));
  });
  expect(alignment.every(x => x < 2)).toBe(true);
  const layout = await work(page).evaluate(e => {
    const detail = e.querySelector('.auction-wb-detail')!.getBoundingClientRect(), board = e.querySelector('.auction-wb-board')!.getBoundingClientRect();
    return { detailLeft: detail.left, boardRight: board.right, sameTop: Math.abs(detail.top - board.top) < 2 };
  });
  expect(layout.detailLeft).toBeGreaterThanOrEqual(layout.boardRight);expect(layout.sameTop).toBe(true);
  await page.screenshot({ path: info.outputPath('reference-revision-1440.png') });
  await dayCard(page).getByText('批次与采集状态', { exact: true }).click();
  await expect(dayCard(page).locator('.auction-wb-totals')).toBeVisible();
  await dayCard(page).getByText('批次与采集状态', { exact: true }).click();
  for (const width of [1024, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`reference-revision-${width}.png`) });
  }
});

test('signals do not invent current-day ratios, global money flow or a height from mixed dates', async ({ page }) => {
  const items = workbenchItems();
  items.push(referenceItem('tushare_limit_list_d', [
    { trade_date: DAY.replaceAll('-', ''), ts_code: '600001.SH', limit: 'U', limit_times: '4' },
    { trade_date: PREV_DAY.replaceAll('-', ''), ts_code: '600002.SH', limit: 'U', limit_times: '8' },
  ]));
  const breadth = { ...referenceItem('ashare_breadth', []), family: 'ashare_breadth', payload: { dataset: 'market.ashare.breadth', observed_day: null, rows: [{ limit_up: '2', broken_limit: '1' }] } };
  await ready(page, { items, breadth });
  for (const key of ['seal_ratio', 'height', 'main_flow', 'sentiment', 'loss_effect']) await expect(page.getByTestId('breadth-' + key)).toHaveText('—');
});
