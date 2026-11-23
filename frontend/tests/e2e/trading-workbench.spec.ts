import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const first = '00000000-0000-4000-8000-000000000081';
const second = '00000000-0000-4000-8000-000000000082';
const source = '00000000-0000-4000-8000-000000000083';
const day = '2020-01-03';
const meta = { request_id: first, app_mode: 'demo', generated_at: day + 'T08:00:00Z', as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic UI fixtures; not real trading data'] } };
const stocks = [{ id: first, exchange: 'SSE', symbol: '600001', name: '合成观察甲', asset_type: 'stock', valid_from: null, valid_to: null }, { id: second, exchange: 'SZSE', symbol: '000002', name: '合成观察乙', asset_type: 'stock', valid_from: null, valid_to: null }];
const rows = Array.from({ length: 16 }, (_, index) => ({ symbol: index % 2 ? '000002.SZ' : '600001.SH', ts_code: index % 2 ? '000002.SZ' : '600001.SH', name: '合成观察' + index, trade_date: '20200103', change_percent: (index - 5).toString(), pct_change: (index - 5).toString(), pct_chg: (index - 5).toString(), net_inflow_cny: (index * 12000).toString(), inflow_cny: (index * 15000).toString(), outflow_cny: (index * 3000).toString(), net_amount: (index * 12000).toString(), l_buy: (index * 15000).toString(), l_sell: (index * 3000).toString(), limit: 'U', limit_times: (index % 5 + 1).toString(), up_stat: '12/23', first_time: '093100', last_time: '143000', open_times: '1', fd_amount: '80000000', reason: '日涨幅偏离值达到7%的前五只证券', industry: '合成行业' }));
async function setup(page: Page) {
  const state = { ids: [first], writes: 0, requestedDays: [] as string[] };
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const reply = (data: unknown) => route.fulfill({ json: { meta, data } });
    if (url.pathname.endsWith('/session')) return reply({ user: { id: first, username: 'synthetic-workbench', role: 'admin' }, csrf_token: 'synthetic' });
    if (url.pathname.endsWith('/watchlist')) {
      if (route.request().method() === 'POST') { state.ids = route.request().postDataJSON().instrument_ids; state.writes++; }
      return reply({ items: state.ids.map((id, position) => ({ position, instrument: stocks.find(item => item.id === id) })) });
    }
    if (url.pathname.endsWith('/instruments')) return reply({ items: stocks.filter(item => item.symbol.includes(url.searchParams.get('q') ?? '') || item.name.includes(url.searchParams.get('q') ?? '')), next_cursor: null });
    if (url.pathname.endsWith('/market-quotes')) {
      const ids = url.searchParams.get('instrument_ids')?.split(',') ?? [url.searchParams.get('instrument_id') ?? first];
      return reply({ selection: url.searchParams.has('instrument_ids') ? 'batch' : url.searchParams.has('instrument_id') ? 'instrument' : 'indices', query_time: meta.generated_at, historical: false, items: ids.map(id => ({ instrument: stocks.find(item => item.id === id) ?? stocks[0], state: 'daily_close', freshness_limit_seconds: 60, change: '0.32', change_percent: '3.20', quote: { price: id === second ? '12.00' : '10.32', price_unit: 'CNY', previous_close: '10', open: '10.01', high: '10.50', low: '9.99', volume_shares: '1000000', amount_cny: '10320000', trade_day: day, quote_kind: 'daily_close', quote_time: null }, provenance: null })) });
    }
    if (url.pathname.endsWith('/market-bars')) return reply({ instrument: stocks.find(item => item.id === url.searchParams.get('instrument_id')) ?? stocks[0], source_name: '合成行情源', adjustment: 'unadjusted', interval: url.searchParams.get('interval') ?? '1d', state: 'historical', items: Array.from({ length: 100 }, (_, index) => ({ id: source, received_at: meta.generated_at, content_hash: 'b'.repeat(64), period_state: 'unknown', payload: { interval: '1d', trade_day: new Date(Date.parse('2019-09-26') + index * 86400000).toISOString().slice(0, 10), adjustment: 'unadjusted', price_unit: 'CNY', open: '10', high: '11', low: '9', close: (10 + index / 100).toFixed(2), volume_shares: String(100000 + index * 2000), amount_cny: '1000000' } })) });
    if (url.pathname.endsWith('/data-sources')) return reply({ items: [], available_adapters: [] });
    if (url.pathname.endsWith('/trading-datasets')) {
      if (url.searchParams.has('day')) state.requestedDays.push(url.searchParams.get('day')!);
      const dataset = url.searchParams.get('dataset')!;
      const family = url.searchParams.get('family')!;
      if (dataset === 'tushare.daily_basic') return reply({available_days:[day],trade_day:day,items:[{id:source,source_id:source,source_name:'合成估值源',source_key:dataset,family,instrument_id:first,trade_day:day,data_mode:'after_market',received_at:meta.generated_at,content_hash:'b'.repeat(64),payload:{title:'合成估值测试',dataset,observed_day:day,columns:[{key:'turnover_rate',label:'换手率',unit:'%'}],rows:[{ts_code:'600001.SH',trade_date:'20200103',turnover_rate:'3'},{ts_code:'600001.SH',trade_date:'20200104',turnover_rate:'99'}]}}]});
      if (!['market.eastmoney.stock_funds', 'tushare.limit_list_d', 'tushare.top_list'].includes(dataset)) return reply({ items: [], available_days: [day], trade_day: url.searchParams.get('day') ?? day });
      const units = Object.keys(rows[0]).map(key => ({ key, label: key, unit: ['net_inflow_cny','inflow_cny','outflow_cny','fd_amount','net_amount','l_buy','l_sell'].includes(key) ? 'CNY' : null }));
      return reply({ available_days: [day], trade_day: url.searchParams.get('day') ?? day, items: [{ id: source, source_id: source, source_name: '合成榜单源', source_key: dataset, family, instrument_id: null, trade_day: day, data_mode: 'after_market', received_at: meta.generated_at, content_hash: 'b'.repeat(64), payload: { title: '合成榜单（测试数据）', dataset, observed_day: day, source_url: 'https://example.com', scope: 'Synthetic fixtures only', attribution: 'Synthetic', columns: units, rows } }] });
    }
    if (url.pathname.endsWith('/reference-datasets')) return reply({ items: [] });
    if (url.pathname.endsWith('/market-breadth-history')) return reply({ observation_day: day, source_family: 'tushare_market_rt', latest: { advancing: 2100, declining: 1900, limit_up: 45, limit_down: 8, comparable: 4000, unknown: 0 }, items: [] });
    if (url.pathname.endsWith('/market-news')) return reply({ items: [] });
    if (url.pathname.endsWith('/research-documents')) return reply({ items: [], next_cursor: null });
    return reply({ items: [], next_cursor: null });
  });
  await page.goto('/workbench?legacy=1&instrument=' + first);
  await expect(page.getByRole('heading', { name: '合成观察甲', exact: true })).toBeVisible();
  return state;
}

test('four core components keep their layout and render the complete chart with volume', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 }); await setup(page);
  const tiles = page.locator('[data-widget-id]');
  await expect(tiles).toHaveCount(7);
  const quote = page.locator('[data-widget-id="quote"]');
  const funds = page.locator('[data-widget-id="funds"]');
  const pools = page.locator('[data-widget-id="pools"]');
  const leaderboard = page.locator('[data-widget-id="top-list"]');
  await expect(quote.locator('[data-volume-clock]')).toHaveCount(80);
  const a = (await quote.boundingBox())!, b = (await funds.boundingBox())!, c = (await pools.boundingBox())!, d = (await leaderboard.boundingBox())!;
  expect(Math.abs(a.y - b.y)).toBeLessThan(3); expect(b.x).toBeGreaterThan(a.x); expect(Math.abs(c.y - d.y)).toBeLessThan(3);
  expect(c.y).toBeLessThan(1080); expect(d.x).toBeGreaterThan(c.x);
  expect(await quote.locator('.dashboard-tile-content').evaluate(node => node.scrollHeight - node.clientHeight)).toBeLessThan(8);
  await expect.poll(() => quote.locator('.trading-chart-svg text').first().evaluate(node => node.getBoundingClientRect().height)).toBeGreaterThan(9);
  for (const panel of [funds,pools,leaderboard]) {
    const firstRow = panel.locator('tbody tr').first();
    await expect(firstRow).toBeVisible();
    const box = (await firstRow.boundingBox())!;
    expect(box.y + box.height).toBeLessThanOrEqual(1080);
  }
  await page.screenshot({ path: test.info().outputPath('trading-workbench-1920.png') });
  await page.setViewportSize({ width: 1440, height: 1000 });
  const content = quote.locator('.dashboard-tile-content');
  expect(await content.evaluate(node => node.scrollHeight - node.clientHeight)).toBeLessThan(8);
  await page.screenshot({ path: test.info().outputPath('trading-workbench-1440.png'), fullPage: true });
});
test('watchlist additions, order and deletions persist after refresh and select the chart in place', async ({ page }) => {
  const state = await setup(page);
  await page.getByLabel('添加自选股票').fill('000002'); await page.getByRole('button', { name: '添加', exact: true }).click();
  await page.getByRole('region', { name: '添加自选搜索结果' }).getByRole('button', { name: /合成观察乙/ }).click();
  await expect(page.getByRole('list', { name: '自选列表' }).getByRole('listitem')).toHaveCount(2);
  await page.getByRole('button', { name: '管理', exact: true }).click();
  await page.getByRole('button', { name: '上移合成观察乙' }).click();
  await expect.poll(() => state.ids[0]).toBe(second);
  await page.reload(); await expect(page.getByRole('list', { name: '自选列表' }).getByRole('listitem').first()).toContainText('合成观察乙');
  await page.getByRole('button', { name: /合成观察乙 000002/ }).click();
  await expect(page).toHaveURL(new RegExp('instrument=' + second));
  await expect(page.getByRole('heading', { name: '合成观察乙', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '管理', exact: true }).click(); await page.getByRole('button', { name: '删除自选合成观察甲' }).click();
  await expect.poll(() => state.ids.length).toBe(1); expect(state.writes).toBe(3);
});
test('date changes reach all datasets and mobile layout has no horizontal overflow', async ({ page }) => {
  const state = await setup(page);
  await page.getByLabel('观察交易日').fill(day);
  await expect(page).toHaveURL(/trade_day=2020-01-03/);
  await expect.poll(() => state.requestedDays.length).toBeGreaterThan(2);
  const quote = page.locator('[data-widget-id="quote"]');
  await expect(quote.locator('.quote-price > strong')).toContainText('10.99');
  await expect(quote.locator('.quote-price')).toContainText('+0.01');
  await expect(quote.locator('.trading-quote-ticker')).toContainText('3%');
  await expect(quote.locator('.trading-quote-ticker')).not.toContainText('99%');
  await quote.getByText('报价来源与数据详情', { exact: true }).click();
  await expect(quote.locator('.trading-quote-details')).toContainText('日线来源：合成行情源');
  await expect(quote.locator('.trading-quote-details')).toContainText('b'.repeat(64));
  await quote.getByText('报价来源与数据详情', { exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThan(2);
  await page.screenshot({ path: test.info().outputPath('trading-workbench-390.png'), fullPage: true });
});
