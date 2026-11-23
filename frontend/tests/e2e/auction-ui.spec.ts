import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { routeHermeticApi } from './hermetic-api.fixtures';

/**
 * Auction page evidence, fully hermetic: session, stored batches and the source index are
 * intercepted, so the run needs no database and never touches a personal instance.
 * Fixtures are synthetic and follow the frozen contract's units (price yuan, volume
 * shares, amount yuan) — they are not market data.
 */

const DAY = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
const previousDay = (() => {
  const [year, month, date] = DAY.split('-').map(Number);
  return new Date(Date.UTC(year, month - 1, date - 1)).toISOString().slice(0, 10);
})();
const meta = { request_id: '00000000-0000-4000-8000-0000000000aa', app_mode: 'demo', generated_at: `${DAY}T03:30:00Z`, as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic auction fixtures only'] } };
const sessionBody = { data: { user: { id: '00000000-0000-4000-8000-000000000001', username: 'e2e', role: 'admin' }, csrf_token: 'csrf-auction-ui' }, meta };

type Row = { code: string; name: string; price_cny: string | null; prev_close_cny: string | null; change_pct: string | null; volume_shares: string | null; amount_cny: string | null; quote_time: string | null };
const row = (over: Partial<Row> & { code: string; name: string }): Row => ({
  price_cny: '11.50', prev_close_cny: '11.00', change_pct: '0.52', volume_shares: '100000', amount_cny: '1000000', quote_time: `${DAY}T09:20:05+08:00`, ...over,
});
const early: Row[] = [
  row({ code: '600001', name: '样本股份01', price_cny: '11.40', change_pct: '0.35', amount_cny: '1000000', quote_time: `${DAY}T09:15:04+08:00` }),
  row({ code: '600002', name: '样本股份02', price_cny: '11.45', change_pct: '0.40', amount_cny: '2000000', volume_shares: '200000', quote_time: `${DAY}T09:15:04+08:00` }),
  row({ code: '600003', name: '样本股份03', price_cny: '11.48', change_pct: '0.44', amount_cny: '3000000', volume_shares: '300000', quote_time: `${DAY}T09:15:04+08:00` }),
];
const many: Row[] = Array.from({ length: 30 }, (_, index) => {
  const n = index + 1;
  return row({
    code: `6000${String(n).padStart(2, '0')}`, name: `样本股份${String(n).padStart(2, '0')}`,
    price_cny: n === 1 ? '11.50' : (11 + n / 100).toFixed(2), change_pct: n === 1 ? '0.52' : (n / 10).toFixed(2),
    volume_shares: String(100000 * n), amount_cny: String(1000000 * n),
  });
});
const late: Row[] = [
  row({ code: '600001', name: '样本股份01', price_cny: '11.60', change_pct: '1.40', amount_cny: '4000000', volume_shares: '400000', quote_time: `${DAY}T09:25:03+08:00` }),
  row({ code: '600002', name: '样本股份02', price_cny: '11.62', change_pct: '1.50', amount_cny: '5000000', volume_shares: '500000', quote_time: `${DAY}T09:25:03+08:00` }),
];

const summary = (over: Record<string, unknown>) => ({
  trade_day: DAY, checkpoint: '09:20', instrument_count: 3, quoted_count: 3, rejected_count: 0,
  total_volume_shares: '600000', total_amount_cny: '6000000', up_count: 3, down_count: 0, flat_count: 0, ...over,
});
const batch = (over: Record<string, unknown>) => ({
  batch_hash: 'a'.repeat(64), source_id: '00000000-0000-4000-8000-0000000000b1', source_name: '东方财富公开盘前竞价快照',
  family: 'eastmoney_auction', trade_day: DAY, trade_day_basis: 'source_time', checkpoint: '09:15',
  checkpoint_basis: 'source_time', received_at: `${DAY}T09:15:42+08:00`, quote_time: `${DAY}T09:15:04+08:00`,
  age_seconds: 40, state: 'available', note: null, summary: summary({ checkpoint: '09:15' }), detail: early, ...over,
});
const stages = () => [
  batch(),
  batch({
    batch_hash: 'b'.repeat(64), checkpoint: '09:20', received_at: `${DAY}T09:20:40+08:00`, quote_time: `${DAY}T09:20:05+08:00`,
    summary: summary({ checkpoint: '09:20', instrument_count: 30, quoted_count: 30, total_volume_shares: '46500000', total_amount_cny: '465000000', up_count: 20, down_count: 8, flat_count: 2 }), detail: many,
  }),
  batch({
    batch_hash: 'c'.repeat(64), checkpoint: '09:25', received_at: `${DAY}T09:25:41+08:00`, quote_time: `${DAY}T09:25:03+08:00`,
    summary: summary({ checkpoint: '09:25', instrument_count: 2, quoted_count: 2, total_volume_shares: '900000', total_amount_cny: '9000000', up_count: 2, down_count: 0 }), detail: late,
  }),
];
const sources = (state = 'available') => [
  { family: 'eastmoney_auction', source_name: '东方财富公开盘前竞价快照', state, last_received_at: `${DAY}T09:25:41+08:00`, last_success_at: `${DAY}T09:25:41+08:00`, reason: state === 'error' ? 'timeout' : null, batch_count: state === 'available' ? 3 : 0 },
  { family: 'tencent_auction', source_name: '腾讯网页公开盘前竞价快照', state: 'empty', last_received_at: null, last_success_at: null, reason: 'not_configured', batch_count: 0 },
];
const view = (items: unknown[], sourceRows = sources()) => ({ meta, data: { trade_day: DAY, timezone: 'Asia/Shanghai', items, sources: sourceRows } });

const runtimeSource = (over: Record<string, unknown> = {}) => ({
  provider: { id: '00000000-0000-4000-8000-0000000000b1', name: '东方财富公开盘前竞价快照', kind: 'market', adapter_version: '1', configured: true, enabled: true, entitlement_status: 'verified', capabilities: [], rights: { display: 'allowed', cache: 'allowed', export: 'denied', redistribute: 'denied' } },
  family: 'eastmoney_auction', rights_basis: '合成测试来源，仅用于浏览器用例', last_attempt: null, last_success_at: `${DAY}T09:25:41+08:00`,
  collection_state: 'ready', credential_configured: null, collection_enabled: true, paused_reason: null, symbols: [],
  interval_seconds: 60, configured_at: `${DAY}T08:00:00+08:00`, next_run_at: `${DAY}T09:26:00+08:00`, measured_cost_cny: null, ...over,
});
const sourceIndex = (over: Record<string, unknown> = {}) => ({ meta, data: { items: [runtimeSource()], available_adapters: [], collection_enabled: true, today_request_count: 3, today_run_count: 3, today_cost_cny: null, ...over } });

/** Login is mocked too; every request stays inside the browser. */
async function open(page: Page) {
  await routeHermeticApi(page, sessionBody);
  await page.route('**/api/v1/evidence?kind=calendar*', route => route.fulfill({ json: { meta, data: { items: [], next_cursor: null } } }));
  await page.route('**/api/v1/calendar-versions*', route => route.fulfill({ json: { meta, data: { items: [], next_cursor: null } } }));
  await page.route('**/api/v1/market-breadth-history*',route=>route.fulfill({json:{meta,data:{observation_day:'2026-10-23',source_name:null,items:[],truncated:false}}}));
  await page.route('**/api/v1/data-sources', route => route.fulfill({ json: sourceIndex() }));
  await page.route('**/api/v1/research-documents*', route => route.fulfill({ json: { meta, data: { items: [], next_cursor: null } } }));
  await page.route('**/api/v1/reference-datasets*', route => route.fulfill({ json: { meta, data: { items: [] } } }));
  await page.route('**/api/v1/market-quotes*', route => route.fulfill({ json: { meta, data: { items: [], selection: 'indices', query_time: `${DAY}T13:00:00+08:00`, historical: false } } }));
  await page.goto('/auction?legacy=1');
  await page.getByLabel('账号', { exact: true }).fill('e2e');
  await page.getByLabel('密码', { exact: true }).fill('hermetic-auction-ui');
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('heading', { name: '竞价观察', exact: true })).toBeVisible();
  await page.getByText('其他来源已存竞价快照', { exact: true }).click();
}

const stageRegion = (page: Page) => page.getByRole('region', { name: '三阶段封单汇总表格' });
const detailRegion = (page: Page) => page.getByRole('region', { name: '个股竞价明细表格' });
const compareRegion = (page: Page) => page.getByRole('region', { name: '个股三阶段对比表格' });
const detailCard = (page: Page) => page.locator('.card.auction-detail-table');
const compareCard = (page: Page) => page.locator('.card.auction-compare');

test('three stages pair one batch each and never call turnover a sealed amount', async ({ page }) => {
  await page.route('**/api/v1/market-auction*', route => route.fulfill({ json: view(stages()) }));
  await open(page);
  const region = stageRegion(page);
  await expect(region.getByRole('rowheader', { name: /09:15/ })).toBeVisible();
  await expect(region.getByRole('rowheader', { name: /09:20/ })).toBeVisible();
  await expect(region.getByRole('rowheader', { name: /09:25/ })).toBeVisible();
  await expect(region.getByTestId('auction-stage-09:20-state')).toHaveText('已配对');
  await expect(region).toContainText('465,000,000');
  await expect(region).toContainText('46,500,000');
  await expect(region).toContainText('09:20:40');
  await expect(region).toContainText(DAY);
  await expect(region.getByRole('columnheader', { name: '竞价额（成交额，元）' })).toBeVisible();
  await expect(region.getByRole('columnheader', { name: '封单金额' })).toBeVisible();
  await expect(region.getByText('未接入')).toHaveCount(3);
  await expect(page.getByText('封单金额未接入')).toBeVisible();
  await expect(page.getByText('不能当作封单金额')).toBeVisible();
  await expect(page.getByText('批次 cccccccccccccccc…')).toBeVisible();
});

test('detail keeps one batch, searches, sorts by amount and paginates 25 rows', async ({ page }) => {
  await page.route('**/api/v1/market-auction*', route => route.fulfill({ json: view(stages()) }));
  await open(page);
  const detail = detailRegion(page);
  await detailCard(page).getByRole('radio', { name: '09:20', exact: true }).click();
  await expect(detailCard(page)).toContainText('同一批次（bbbbbbbbbbbbbbbb…');
  await expect(detail.locator('tbody tr')).toHaveCount(25);
  await expect(detail.locator('tbody tr').first()).toContainText('600030');
  const pager = page.getByRole('group', { name: '明细分页' });
  await expect(pager).toContainText('第 1–25 条 / 共 30 条 · 每页 25 条');
  await pager.getByRole('button', { name: '下一页' }).click();
  await expect(pager).toContainText('第 26–30 条 / 共 30 条');
  await expect(detail.locator('tbody tr')).toHaveCount(5);
  await expect(detail.locator('tbody tr').first()).toContainText('600005');
  await pager.getByRole('button', { name: '上一页' }).click();

  await page.getByRole('radiogroup', { name: '竞价额排序' }).getByRole('radio', { name: '金额从低到高', exact: true }).click();
  await expect(detail.locator('tbody tr').first()).toContainText('600001');
  await page.getByRole('radiogroup', { name: '竞价额排序' }).getByRole('radio', { name: '来源顺序', exact: true }).click();
  await expect(detail.locator('tbody tr').first()).toContainText('600001');

  await page.getByLabel('搜索代码或名称').fill('样本股份17');
  await expect(detail.locator('tbody tr')).toHaveCount(1);
  await expect(detail.locator('tbody tr').first()).toContainText('600017');
  await expect(pager).toContainText('共 1 条');
  await page.getByLabel('搜索代码或名称').fill('不存在的标的');
  await expect(detail.getByText('没有匹配的标的')).toBeVisible();
  await page.getByLabel('搜索代码或名称').fill('');
  await expect(detail.locator('tbody tr')).toHaveCount(25);
});

test('a stock is compared across the three stored stages without inventing missing rows', async ({ page }) => {
  await page.route('**/api/v1/market-auction*', route => route.fulfill({ json: view(stages()) }));
  await open(page);
  await page.getByLabel('搜索代码或名称').fill('600001');
  await page.getByRole('button', { name: '对比 600001 样本股份01' }).click();
  const compare = compareRegion(page);
  await expect(compare.getByRole('rowheader', { name: '竞价价（元）' })).toBeVisible();
  await expect(compare.getByRole('cell', { name: '11.40', exact: true })).toBeVisible();
  await expect(compare.getByRole('cell', { name: '11.50', exact: true })).toBeVisible();
  await expect(compare.getByRole('cell', { name: '11.60', exact: true })).toBeVisible();
  await expect(compare.getByRole('cell', { name: '4,000,000', exact: true })).toBeVisible();
  await expect(compare.getByRole('cell', { name: '1.40%', exact: true })).toBeVisible();
  await expect(compareCard(page)).toContainText('东方财富公开盘前竞价快照');
  await expect(compare).toContainText('09:25:41');

  await detailCard(page).getByRole('radio', { name: '09:20', exact: true }).click();
  await page.getByLabel('搜索代码或名称').fill('600030');
  await page.getByRole('button', { name: '对比 600030 样本股份30' }).click();
  await expect(compare.getByRole('cell', { name: '批次无此标的', exact: true }).first()).toBeVisible();
  await expect(compare.getByRole('cell', { name: '30,000,000', exact: true })).toBeVisible();
});

test('switching to a day without stored batches shows an empty state, not zeroes', async ({ page }) => {
  await page.route('**/api/v1/market-auction*', async route => {
    const requested = new URL(route.request().url()).searchParams.get('trade_day');
    return route.fulfill({ json: requested === DAY ? view(stages()) : view([], sources('empty')) });
  });
  await open(page);
  await expect(stageRegion(page)).toBeVisible();
  await page.getByRole('button', { name: '前一自然日' }).click();
  await expect(page.getByText('尚无已存竞价批次')).toBeVisible();
  await expect(stageRegion(page)).toHaveCount(0);
  await expect(page.getByText(`正在查看历史交易日 ${previousDay}`)).toBeVisible();
  await expect(detailRegion(page).locator('tbody tr')).toHaveCount(1);
  await expect(page.getByText('0.00')).toHaveCount(0);
  await page.getByRole('button', { name: '后一自然日' }).click();
  await expect(stageRegion(page).getByRole('rowheader', { name: /09:20/ })).toBeVisible();
});

test('failed collection keeps the last batch, marks it old and names the reason', async ({ page }) => {
  await page.route('**/api/v1/market-auction*', route => route.fulfill({ json: view([
    batch({ state: 'stale', age_seconds: 7200, note: '最近一次采集失败（timeout），显示上次成功批次。' }),
    batch({ batch_hash: 'b'.repeat(64), checkpoint: '09:20', state: 'stale', age_seconds: 7200, summary: summary({ checkpoint: '09:20' }), detail: many.slice(0, 2), note: '最近一次采集失败（timeout），显示上次成功批次。' }),
  ], sources('error')) }));
  await open(page);
  const region = stageRegion(page);
  await expect(region.getByTestId('auction-stage-09:15-state')).toHaveText('旧数据');
  await expect(region).toContainText('2 小时前');
  await expect(region).toContainText('最近一次采集失败（timeout）');
  await expect(region.getByRole('rowheader', { name: /09:25/ })).toBeVisible();
  await expect(region.getByText('该检查点尚无已配对批次；不补 0、不推算合计。')).toBeVisible();
  await expect(page.getByTestId('auction-source-eastmoney_auction')).toContainText('最近采集失败');
  await expect(page.getByTestId('auction-source-eastmoney_auction')).toContainText('上游超时');
  await page.getByLabel('搜索代码或名称').fill('600001');
  await page.getByRole('button', { name: '对比 600001 样本股份01' }).click();
  await expect(compareRegion(page).getByRole('cell', { name: '无该时点批次', exact: true }).first()).toBeVisible();
});

test('an unpaired batch shows no totals and no invented detail rows', async ({ page }) => {
  await page.route('**/api/v1/market-auction*', route => route.fulfill({ json: view([
    batch({ state: 'unpaired', detail: [], note: '汇总与明细未按 batch_hash 配成一对，不展示合计或推算缺失行。' }),
  ]) }));
  await open(page);
  const region = stageRegion(page);
  await expect(region.getByTestId('auction-stage-09:15-state')).toHaveText('配对缺失');
  await expect(region).toContainText('不展示合计或推算缺失行');
  await expect(region).not.toContainText('6,000,000');
  await expect(region).not.toContainText('600,000');
  await expect(detailRegion(page).getByText('该批次没有可展示的明细')).toBeVisible();
  await expect(detailRegion(page).locator('tbody tr')).toHaveCount(1);
});

test('admin collection queues honestly and reports a failed request', async ({ page }) => {
  await page.route('**/api/v1/market-auction*', route => route.fulfill({ json: view(stages()) }));
  let fail = false;
  await page.route('**/api/v1/data-sources/*/refresh', route => fail
    ? route.fulfill({ status: 503, json: { error: { code: 'service_unavailable', message: '采集服务暂不可用。', details: [] }, request_id: '00000000-0000-4000-8000-0000000000ac' } })
    : route.fulfill({ json: { meta, data: { job_id: '00000000-0000-4000-8000-00000000c0de', source_id: '00000000-0000-4000-8000-0000000000b1', state: 'queued' } } }));
  await open(page);
  await page.locator('.auction-stages').getByRole('button', { name: '采集一次' }).click();
  await expect(page.getByRole('status').filter({ hasText: '采集任务已排队' })).toContainText('排队不代表采集成功');
  await expect(page.getByRole('status').filter({ hasText: '采集任务已排队' })).toContainText('00000000');
  fail = true;
  await page.locator('.auction-stages').getByRole('button', { name: '采集一次' }).click();
  const failure = page.getByRole('alert').filter({ hasText: '采集服务暂不可用' });
  await expect(failure).toContainText('采集服务暂不可用。');
  await expect(failure).toContainText('00000000-0000-4000-8000-0000000000ac');
});

test('manual observation notebook stays independent and desktop/mobile screenshots are captured', async ({ page }, info) => {
  await page.route('**/api/v1/market-auction*', route => route.fulfill({ json: view(stages()) }));
  await open(page);
  await expect(page.getByText('原预案与竞价观察记录')).toBeVisible();
  await expect(page.getByText('人工观察可保存并回查')).toBeVisible();
  await detailCard(page).getByRole('radio', { name: '09:20', exact: true }).click();
  await page.getByLabel('搜索代码或名称').fill('600001');
  await page.getByRole('button', { name: '对比 600001 样本股份01' }).click();
  await expect(compareRegion(page)).toContainText('11.50');
  for (const width of [1920, 1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1080 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`auction-ui-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath('auction-ui-390-top.png') });
  await page.setViewportSize({ width: 1440, height: 1080 });
  await page.getByLabel('搜索代码或名称').fill('');
  await expect(detailRegion(page).locator('tbody tr')).toHaveCount(25);
  await detailCard(page).screenshot({ path: info.outputPath('auction-ui-rows-1440.png') });
  await expect(page.getByRole('group', { name: '明细分页' })).toContainText('每页 25 条');
});
