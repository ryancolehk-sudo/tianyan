import { openLegacyWorkbench } from './legacyWorkbench';
import { expect, test } from '@playwright/test';
import { signIn } from './researchTestHelpers';

const id = '00000000-0000-4000-8000-000000000023';
const meta = { request_id: id, app_mode: 'demo', generated_at: '2020-01-02T07:00:00Z', as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic observation chart only'] } };
const point = (clock: string, up: number | null, down: number | null, index: number) => ({ id: id.slice(0, -2) + String(index).padStart(2, '0'), received_at: `2020-01-02T${clock}+08:00`, limit_up: up, limit_down: down });
const points = [point('09:30:00', 40, 0, 1), point('10:00:00', 50, 1, 2), point('10:30:00', null, 0, 3), point('11:30:00', 52, null, 4), point('13:00:00', 54, 1, 5), point('15:00:00', 80, 3, 6)];
const body = (items = points, day = '2020-01-02') => ({ meta, data: { observation_day: day, timezone: 'Asia/Shanghai', time_basis: 'received_at', source_name: '合成广度观察', items, truncated: false } });
const endpoint = '**/api/v1/market-breadth-history*';

test('separate count charts use independent scales, a continuous A-share axis and linked pointer and keyboard selection', async ({ page }, info) => {
  await page.route(endpoint, route => route.fulfill({ json: body() }));
  await page.goto('/workbench?legacy=1'); await signIn(page); await page.locator('.workbench-market-history > summary').click();
  const chart = page.getByRole('region', { name: '涨跌停数量走势', exact: true });
  const up = chart.getByRole('region', { name: '涨停数量走势', exact: true });
  const down = chart.getByRole('region', { name: '跌停数量走势', exact: true });
  await expect(chart.getByTestId('breadth-history-limit_up')).toHaveText('80');
  await expect(chart.getByTestId('breadth-history-limit_down')).toHaveText('3');
  await expect(chart).toContainText('本机采集时间 · 行情时点未知');
  await expect(up.locator('.breadth-value-axis')).toHaveText(['0', '20', '40', '60', '80']);
  await expect(down.locator('.breadth-value-axis')).toHaveText(['0', '1', '2', '3', '4']);
  for (const panel of [up, down]) {
    await expect(panel.locator('.breadth-time-axis')).toHaveText(['09:30', '10:30', '13:00', '14:00', '15:00']);
    await expect(panel.getByText('午休', { exact: true })).toHaveCount(0);
    await expect(panel.locator('rect')).toHaveCount(0);
  }
  const path = chart.locator('path[data-series="limit_up"]');
  await expect(path).toHaveCount(1);
  await expect(path).toHaveAttribute('d', /^M[^M]+M[^M]+L[^M]+L/);
  const positions = await up.locator('svg circle').evaluateAll(nodes => nodes.map(node => ({ x: Number(node.getAttribute('cx')), y: Number(node.getAttribute('cy')) })));
  expect(positions[2].x).toBeCloseTo(positions[3].x, 5);
  expect(positions[2].y).not.toBe(positions[3].y);
  const picker = chart.getByRole('slider', { name: '查看涨跌停采集时点' });
  await picker.focus(); await picker.press('Home');
  await expect(chart.getByTestId('breadth-history-limit_up')).toHaveText('40');
  await expect(chart.getByTestId('breadth-history-limit_down')).toHaveText('0');
  await picker.press('ArrowRight'); await picker.press('ArrowRight');
  await expect(chart.getByTestId('breadth-history-limit_up')).toHaveText('—');
  await expect(up.locator('.breadth-history-tooltip')).toContainText('10:30:00');
  await expect(up.locator('.breadth-history-tooltip')).toContainText('未知');
  await expect(down.locator('.breadth-history-tooltip')).toContainText('0 家');
  const svg = down.getByRole('img', { name: '跌停数量分时折线图' });
  const bounds = await svg.boundingBox();
  await page.mouse.move(bounds!.x + 44, bounds!.y + 100);
  await expect(up.locator('.breadth-history-tooltip')).toContainText('09:30:00');
  await expect(down.locator('.breadth-history-tooltip')).toContainText('09:30:00');
  for (const width of [1440, 1024, 768, 390]) {
    await page.setViewportSize({ width, height: 1000 }); await chart.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.mouse.move(0, 0);
    const upBox = await up.boundingBox(), downBox = await down.boundingBox();
    if (width === 1440) {
      expect(Math.abs(upBox!.y - downBox!.y)).toBeLessThan(1);
      expect(upBox!.x + upBox!.width).toBeLessThanOrEqual(downBox!.x);
    } else if (width === 390) expect(downBox!.y).toBeGreaterThanOrEqual(upBox!.y + upBox!.height);
    await page.screenshot({ path: info.outputPath(`breadth-history-${width}.png`), animations: 'disabled' });
  }
  await page.reload(); await page.locator('.workbench-market-history > summary').click(); await expect(chart.getByTestId('breadth-history-limit_up')).toHaveText('80');
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openLegacyWorkbench(page, '竞价观察');
  await expect(chart).toHaveCount(0);
  await expect(page.getByLabel('市场广度参考', { exact: true })).toBeVisible();
  await openLegacyWorkbench(page);
  await page.locator('.workbench-market-history > summary').click();
  await expect(chart.getByTestId('breadth-history-limit_up')).toHaveText('80');
});

test('date changes reject old responses and show empty records without carrying another day', async ({ page }) => {
  let release: (() => void) | undefined;
  let pending = false;
  await page.route(endpoint, async route => {
    const day = new URL(route.request().url()).searchParams.get('day');
    if (day === '2020-01-01') { pending = true; await new Promise<void>(resolve => { release = resolve; }); }
    await route.fulfill({ json: day === '2020-01-03' ? body([], day) : body(points, day ?? undefined) });
  });
  await page.goto('/workbench?legacy=1'); await signIn(page); await page.locator('.workbench-market-history > summary').click();
  const chart = page.getByRole('region', { name: '涨跌停数量走势', exact: true });
  await expect(chart.getByTestId('breadth-history-limit_up')).toHaveText('80');
  const date = chart.getByLabel('涨跌停走势日期', { exact: true });
  await date.fill('2020-01-01'); await expect.poll(() => pending).toBe(true);
  await expect(chart.getByTestId('breadth-history-limit_up')).toHaveText('—');
  await date.fill('2020-01-03');
  await expect(chart).toContainText('该日尚无交易时段采集记录');
  await Promise.all([page.waitForResponse(response => response.url().includes('day=2020-01-01')), Promise.resolve().then(() => release!())]);
  await expect(date).toHaveValue('2020-01-03');
  await expect(chart.getByTestId('breadth-history-limit_up')).toHaveText('—');
  await expect(chart.locator('circle')).toHaveCount(0);
});

test('local cache refresh adds observations and errors can retry without substituting zero', async ({ page }) => {
  let items = points.slice(0, 1), broken = false, calls = 0;
  await page.clock.install({ time: new Date('2020-01-02T02:00:00Z') });
  await page.route(endpoint, route => {
    calls++;
    return broken ? route.fulfill({ status: 503, json: { request_id: id, error: { code: 'unavailable', message: 'Synthetic chart read failure' } } }) : route.fulfill({ json: body(items) });
  });
  await page.goto('/workbench?legacy=1'); await signIn(page); await page.locator('.workbench-market-history > summary').click();
  const chart = page.getByRole('region', { name: '涨跌停数量走势', exact: true });
  await expect(chart).toContainText('1 个已保存时点');
  items = points;
  const before = calls; await page.clock.fastForward(60_001);
  await expect.poll(() => calls).toBeGreaterThan(before);
  await expect(chart).toContainText('6 个已保存时点');
  broken = true; await chart.getByRole('button', { name: '刷新涨跌停走势', exact: true }).click();
  await expect(chart).toContainText('走势读取失败');
  await expect(chart.getByTestId('breadth-history-limit_down')).toHaveText('—');
  broken = false; await chart.getByRole('button', { name: '重试读取走势', exact: true }).click();
  await expect(chart.getByTestId('breadth-history-limit_down')).toHaveText('3');
});

test('collection status explains throttling and retry, keeps saved curves on status errors and distinguishes historical days', async ({ page }, info) => {
  await page.clock.install({ time: new Date('2020-01-02T06:20:00Z') });
  let limited = true, statusFailed = false, sourceCalls = 0;
  let items = [point('14:00:00', 60, 2, 7)];
  await page.route(endpoint, route => {
    const day = new URL(route.request().url()).searchParams.get('day');
    return route.fulfill({ json: day === '2020-01-01' ? body([], day) : body(items) });
  });
  await page.route('**/api/v1/data-sources', route => {
    sourceCalls++;
    if (statusFailed) return route.fulfill({ status: 503, json: { request_id: id, error: { code: 'unavailable', message: 'Synthetic source state failure' } } });
    return route.fulfill({ json: { meta, data: { items: [{ family: 'ashare_breadth', interval_seconds: 300,
      collection_enabled: true, collection_state: limited ? 'error' : 'ready', paused_reason: null,
      last_success_at: limited ? '2020-01-02T06:00:00Z' : '2020-01-02T06:20:00Z',
      next_run_at: limited ? '2020-01-02T07:05:00Z' : '2020-01-02T06:25:00Z',
      last_attempt: { status: limited ? 'error' : 'complete', reason: limited ? 'rate_limited' : null },
    }] } } });
  });
  await page.goto('/workbench?legacy=1'); await signIn(page); await page.locator('.workbench-market-history > summary').click();
  const chart = page.getByRole('region', { name: '涨跌停数量走势', exact: true });
  const status = chart.locator('.breadth-history-sync');
  await expect(status).toContainText('上次采集被限流，保留旧数据');
  await expect(status).toContainText('最后成功采集：14:00:00');
  await expect(status).toContainText('计划重试：15:05:00');
  await expect(status).toContainText('采集间隔 5 分钟');
  await expect(status).toContainText('图表每分钟检查已保存记录');
  await expect(chart.getByTestId('breadth-history-limit_up')).toHaveText('60');
  await expect(chart.getByTestId('breadth-history-limit_down')).toHaveText('2');
  const writes: string[] = [];
  page.on('request', request => { if (request.method() !== 'GET' || new URL(request.url()).origin !== new URL(page.url()).origin) writes.push(request.url()); });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1100 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await chart.screenshot({ path: info.outputPath(`breadth-sync-limited-${width}.png`), animations: 'disabled' });
  }
  statusFailed = true;
  await chart.getByRole('button', { name: '刷新涨跌停走势', exact: true }).click();
  await expect(status).toContainText('采集状态读取失败');
  await expect(chart.getByTestId('breadth-history-limit_up')).toHaveText('60');
  await expect(chart.locator('path[data-series="limit_down"]')).not.toHaveAttribute('d', '');
  statusFailed = false; limited = false;
  items = [...items, point('14:20:00', 61, 3, 8)];
  const before = sourceCalls;
  await page.clock.fastForward(60_001);
  await expect.poll(() => sourceCalls).toBeGreaterThan(before);
  await expect(status).toContainText('按采集计划更新');
  await expect(status).toContainText('最后成功采集：14:20:00');
  await expect(status).not.toContainText('限流');
  await expect(chart.getByTestId('breadth-history-limit_up')).toHaveText('61');
  limited = true;
  await chart.getByRole('button', { name: '刷新涨跌停走势', exact: true }).click();
  await expect(status).toContainText('上次采集被限流');
  await chart.getByLabel('涨跌停走势日期', { exact: true }).fill('2020-01-01');
  await expect(status).toContainText('历史采集记录');
  await expect(status).not.toContainText('限流');
  await expect(chart).toContainText('该日尚无交易时段采集记录');
  expect(writes).toEqual([]);
});
