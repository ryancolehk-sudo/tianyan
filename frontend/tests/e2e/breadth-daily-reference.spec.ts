import { expect, test } from '@playwright/test';
import { signIn } from './researchTestHelpers';

const id = '00000000-0000-4000-8000-000000000031';
const meta = { request_id: id, app_mode: 'demo', generated_at: '2020-01-02T09:00:00Z', as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic fixtures only'] } };
const rows = [
  { ts_code: '600001.SH', trade_date: '20200102', limit: 'U' },
  { ts_code: '000001.SZ', trade_date: '20200102', limit: 'U' },
  { ts_code: '000002.SZ', trade_date: '20200102', limit: 'D' },
  { ts_code: '600002.SH', trade_date: '20200102', limit: 'Z' },
];
const record = () => ({ id, source_id: id, source_name: '合成已购日榜', family: 'tushare_limit_list_d', source_key: 'limit_list_d',
  received_at: '2020-01-02T08:00:00Z', published_at: null, content_hash: '0'.repeat(64), instrument_id: null,
  payload: { dataset: 'tushare.limit_list_d', title: '合成日榜', source_url: 'https://example.com', attribution: '合成测试', scope: '当前有界返回，非完整市场',
    columns: ['ts_code', 'trade_date', 'limit'].map(key => ({ key, label: key, unit: null })), rows, observed_day: '2020-01-02' } });

test('post-close daily list is displayed separately without extending the intraday curve', async ({ page }, info) => {
  await page.clock.install({ time: new Date('2020-01-02T09:00:00Z') });
  await page.route('**/api/v1/reference-datasets*', route => route.fulfill({ json: { meta, data: { items: new URL(route.request().url()).searchParams.get('dataset') === 'tushare.limit_list_d' ? [record()] : [] } } }));
  await page.route('**/api/v1/market-breadth-history*', route => route.fulfill({ json: { meta, data: {
    observation_day: new URL(route.request().url()).searchParams.get('day') ?? '2020-01-02', timezone: 'Asia/Shanghai', time_basis: 'received_at', source_name: '合成盘中观察',
    items: [{ id, received_at: '2020-01-02T14:10:34+08:00', limit_up: 47, limit_down: 3 }], truncated: false } } }));
  await page.goto('/workbench?legacy=1'); await signIn(page); await page.locator('.workbench-market-history > summary').click();
  const chart = page.getByRole('region', { name: '涨跌停数量走势', exact: true });
  const daily = chart.getByRole('region', { name: '涨跌停盘后日榜参考', exact: true });
  await expect(daily.getByTestId('breadth-daily-up')).toHaveText('2 条');
  await expect(daily.getByTestId('breadth-daily-down')).toHaveText('1 条');
  await expect(daily).toContainText('不含 ST');
  await expect(daily).toContainText('完整性尚未确认');
  await expect(daily).toContainText('2020/01/02 16:00:00');
  await expect(chart.getByTestId('breadth-history-limit_up')).toHaveText('47');
  await expect(chart.locator('.breadth-history-panels circle')).toHaveCount(2);
  await expect(chart).toContainText('1 个已保存时点');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1100 }); await daily.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await chart.screenshot({ path: info.outputPath(`breadth-daily-reference-${width}.png`), animations: 'disabled' });
  }
  await chart.getByLabel('涨跌停走势日期', { exact: true }).fill('2020-01-01');
  await expect(daily).toContainText('所选日期暂无可核对的盘后日榜');
  await expect(daily.getByTestId('breadth-daily-up')).toHaveCount(0);
});

test('missing categories, invalid snapshots, permission removal and retry never become market zeroes', async ({ page }) => {
  await page.clock.install({ time: new Date('2020-01-02T09:00:00Z') });
  let snapshot = record(); let broken = false; let absent = false;
  await page.route('**/api/v1/reference-datasets*', route => {
    if (new URL(route.request().url()).searchParams.get('dataset') !== 'tushare.limit_list_d') return route.fulfill({ json: { meta, data: { items: [] } } });
    return broken ? route.fulfill({ status: 503, json: { error: { code: 'unavailable', message: 'Synthetic failure' } } })
      : route.fulfill({ json: { meta, data: { items: absent ? [] : [snapshot] } } });
  });
  await page.route('**/api/v1/market-breadth-history*', route => route.fulfill({ json: { meta, data: { observation_day: '2020-01-02', source_name: null, items: [] } } }));
  snapshot.payload.rows = rows.slice(0, 2);
  await page.goto('/workbench?legacy=1'); await signIn(page); await page.locator('.workbench-market-history > summary').click();
  const chart = page.getByRole('region', { name: '涨跌停数量走势', exact: true });
  const daily = chart.getByRole('region', { name: '涨跌停盘后日榜参考', exact: true });
  const refresh = () => chart.getByRole('button', { name: '刷新涨跌停走势', exact: true }).click();
  await expect(daily.getByTestId('breadth-daily-down')).toHaveText('未返回');
  for (const change of ['duplicate', 'mixed_day', 'pre_close', 'future', 'unknown_category', 'empty']) {
    snapshot = record();
    if (change === 'duplicate') snapshot.payload.rows = [rows[0], rows[0]];
    if (change === 'mixed_day') snapshot.payload.rows = [rows[0], { ...rows[1], trade_date: '20200101' }];
    if (change === 'pre_close') snapshot.received_at = '2020-01-02T06:59:59Z';
    if (change === 'future') snapshot.received_at = '2020-01-02T10:00:00Z';
    if (change === 'unknown_category') snapshot.payload.rows = [{ ...rows[0], limit: 'unknown' }];
    if (change === 'empty') snapshot.payload.rows = [];
    await refresh();
    await expect(daily).toContainText('暂无可核对');
    await expect(daily.getByTestId('breadth-daily-up')).toHaveCount(0);
  }
  snapshot = record(); await refresh(); await expect(daily.getByTestId('breadth-daily-up')).toHaveText('2 条');
  absent = true; await refresh(); await expect(daily.getByTestId('breadth-daily-up')).toHaveCount(0);
  absent = false; broken = true; await refresh(); await expect(daily).toContainText('日榜缓存读取失败');
  broken = false; await refresh(); await expect(daily.getByTestId('breadth-daily-up')).toHaveText('2 条');
});
