import { expect } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';
import { test } from './isolated-browser.fixtures';
import { sectorA, sectorB, terminalDay, terminalFixture } from './terminalTestFixtures';
const fundA = `THS:industry:${sectorA}`;

// All samples are synthetic and all API traffic is intercepted before navigation.
async function fixture(page: Page, view = 'overview', state = { omitTime: '' }) {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const market = { server_time: `${terminalDay}T13:05:00+08:00`, trade_day: terminalDay, phase: 'trading',
    is_trading_day: true, automatic_live: true, next_transition_at: null, poll_seconds: 60 };
  await page.route('**/api/v1/**', route => {
    const url = new URL(route.request().url());
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'demo' } } });
    if (url.pathname.endsWith('/session')) return ok({ user: { id: 'chart-inspection-synthetic', username: 'synthetic', role: 'viewer' }, csrf_token: 'synthetic-only' });
    if (url.pathname.endsWith('/market-session')) return ok(market);
    if (url.pathname.endsWith('/ths-funds')) {
      const industry = url.searchParams.get('category') === 'industry';
      const times = ['11:30:00', '12:10:00', '13:00:00', '13:02:30'];
      return ok({ sampled_day: terminalDay, status: 'available', received_at: `${terminalDay}T13:03:00+08:00`, complete: true,
        missing_pages: [], rejected_rows: 0, last_error: null,
        rows: industry ? [{ code: sectorA, name: '合成题材甲', net_cny: null, pct_change: '0', company_count: '2', received_at: `${terminalDay}T13:02:30+08:00` },
          { code: sectorB, name: '合成题材乙', net_cny: '900000000', pct_change: null, company_count: '1', received_at: `${terminalDay}T13:02:30+08:00` }] : [],
        history: industry ? times.map((time, index) => ({ received_at: `${terminalDay}T${time}+08:00`, points: [
          { code: sectorA, net_cny: index === 3 ? null : String(index * 100_000_000), received_at: `${terminalDay}T${time}+08:00` },
          ...(index === 3 ? [{ code: sectorB, net_cny: '900000000', received_at: `${terminalDay}T${time}+08:00` }] : []),
        ] })) : [],
      });
    }
    if (url.pathname.endsWith('/workbench-view')) {
      const data = terminalFixture(url.searchParams);
      return ok({ ...data, mode: 'live', market_session: market,
        breadth: { ...data.breadth, points: [
          { time: `${terminalDay}T09:30:00+08:00`, up: 0, down: null },
          { time: `${terminalDay}T09:35:00+08:00`, up: 2, down: 0 },
          { time: `${terminalDay}T13:00:00+08:00`, up: 9, down: 2 },
          // Deliberately unordered: compressed noon coordinates still read in clock order.
          { time: `${terminalDay}T11:30:00+08:00`, up: 5, down: 1 },
          { time: `${terminalDay}T13:02:30+08:00`, up: 10, down: 3 },
        ].filter(point => point.time !== state.omitTime) },
        // A different supplier remains a decoy. Current THS views must not borrow
        // its minute observations or terminalFixture's daily funds history.
        intraday_funds: { state: 'available', trade_day: terminalDay, source_family: 'synthetic', source_name: '合成图表样本',
          latest_source_time: `${terminalDay}T13:02:30+08:00`, received_at: `${terminalDay}T13:03:00+08:00`, scope: '合成测试，非真实行情',
          sectors: data.sectors, stocks: [], points: [
            { time: `${terminalDay}T11:30:00+08:00`, sector_code: sectorA, value: 0 },
            { time: `${terminalDay}T12:10:00+08:00`, sector_code: sectorA, value: 100_000_000 },
            { time: `${terminalDay}T13:00:00+08:00`, sector_code: sectorA, value: 200_000_000 },
            { time: `${terminalDay}T13:02:30+08:00`, sector_code: sectorA, value: null },
            { time: `${terminalDay}T13:02:30+08:00`, sector_code: sectorB, value: 900_000_000 },
          ] },
      });
    }
    if (url.pathname.endsWith('/ai/settings')) return ok({ enabled: false, features: [] });
    return ok({ items: [], next_cursor: null });
  });
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), () => page.keyboard.press('Escape'));
  await page.goto(`/workbench?view=${view}&sector=${sectorA}${view === 'funds' ? '&fund_sector=' + encodeURIComponent(fundA) : ''}`);
  if (view === 'funds') {
    await page.getByRole('group', { name: '板块图表指标', exact: true }).getByRole('button', { name: '资金净额', exact: true }).click();
    await expect(page.locator(`.ty-funds-chart g[data-sector-code="${fundA}"] .ty-fund-sample`)).toHaveCount(3);
  }
  await expect(page.locator(view === 'funds' ? '.ty-funds-chart svg' : '.ty-breadth-chart svg')).toBeVisible();
  await expect(page.getByRole('dialog', { name: '正在准备你的工作空间' })).toBeHidden();
  return errors;
}

async function expectReading(readout: Locator, values: string[]) {
  for (const value of values) await expect(readout).toContainText(value);
}

async function at(svg: Locator, minute: number) {
  await svg.scrollIntoViewIfNeeded();
  // A responsive SVG can still be receiving its ResizeObserver dimensions when
  // it first becomes visible. Wait for its real geometry, not an arbitrary delay.
  await svg.evaluate(async node => {
    await document.fonts.ready;
    let previous = '', stable = 0;
    for (let frame = 0; frame < 60; frame++) {
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
      const box = node.getBoundingClientRect(), view = (node as SVGSVGElement).viewBox.baseVal;
      const current = [box.x, box.y, box.width, box.height, view.width, view.height].map(value => value.toFixed(2)).join(':');
      stable = current === previous ? stable + 1 : 0; previous = current;
      if (stable >= 2) return;
    }
    throw new Error('合成图表的布局尚未稳定');
  });
  return svg.evaluate((node, minute) => {
    const element = node as SVGSVGElement;
    const point = new DOMPoint(48 + (element.viewBox.baseVal.width - 76) * minute / 240, element.viewBox.baseVal.height / 2);
    const screen = point.matrixTransform(element.getScreenCTM()!);
    return { x: screen.x, y: screen.y };
  }, minute);
}

test('breadth keyboard inspection keeps zero, missing values, source seconds and the two lunch clocks distinct', async ({ page }) => {
  const errors = await fixture(page);
  const chart = page.locator('.ty-breadth-chart'), svg = chart.locator('svg'), readout = chart.locator('.ty-chart-readout');
  await svg.focus(); await page.keyboard.press('Home');
  await expectReading(readout, ['09:30:00', '0 家', '缺测']);
  await page.keyboard.press('ArrowRight'); await page.keyboard.press('ArrowRight');
  await expect(readout).toContainText('11:30:00');
  const noonX = await chart.locator('.ty-chart-reference > line').first().getAttribute('x1');
  await page.keyboard.press('ArrowRight');
  await expectReading(readout, ['13:00:00', '9 家']);
  expect(await chart.locator('.ty-chart-reference > line').first().getAttribute('x1')).toBe(noonX);
  await page.keyboard.press('End'); await expect(readout).toContainText('13:02:30');
  if (process.env.UI_CRAFT_CAPTURE) await chart.screenshot({ path: test.info().outputPath('涨跌停走势-真实点读数.png') });
  await page.keyboard.press('Escape'); await expect(readout).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('pointer inspection does not interpolate the missing trading interval or alter the stored curve', async ({ page }) => {
  await fixture(page);
  const chart = page.locator('.ty-breadth-chart'), svg = chart.locator('svg');
  const path = chart.locator('.ty-chart-up > path[stroke="currentColor"]');
  const actual = await at(svg, 5);
  const original = await path.getAttribute('d');
  expect(original?.match(/M/g)).toHaveLength(2);
  await page.mouse.move(actual.x, actual.y);
  await expect(chart.locator('.ty-chart-readout')).toContainText('09:35:00');
  const gap = await at(svg, 60); await page.mouse.move(gap.x, gap.y);
  await expect(chart.locator('.ty-chart-readout')).toHaveCount(0);
  expect(await path.getAttribute('d')).toBe(original);
});

test('a refresh removing a pinned sample releases the cursor for the remaining real samples', async ({ page }) => {
  await page.clock.install({ time: new Date(`${terminalDay}T13:05:00+08:00`) });
  const state = { omitTime: '' };
  const errors = await fixture(page, 'overview', state);
  const chart = page.locator('.ty-breadth-chart'), svg = chart.locator('svg'), readout = chart.locator('.ty-chart-readout');
  await svg.focus(); await page.keyboard.press('End'); await page.keyboard.press('Enter');
  await expectReading(readout, ['13:02:30', '已固定']);
  state.omitTime = `${terminalDay}T13:02:30+08:00`;
  await page.clock.runFor(61_000);
  await expect(readout).toHaveCount(0);
  const actual = await at(svg, 5); await page.mouse.move(actual.x, actual.y);
  await expectReading(readout, ['09:35:00', '实际采样']);
  expect(errors).toEqual([]);
});

test('funds inspection follows only the selected sector and visits every actual lunch timestamp', async ({ page }) => {
  const errors = await fixture(page, 'funds');
  const chart = page.locator('.ty-funds-chart'), svg = chart.locator('svg'), readout = chart.locator('.ty-chart-readout');
  await svg.focus(); await page.keyboard.press('Home');
  await expectReading(readout, ['合成题材甲', '11:30:00', '0.00 亿元']);
  await page.keyboard.press('ArrowRight'); await expectReading(readout, ['12:10:00', '1.00 亿元']);
  await page.keyboard.press('ArrowRight'); await expectReading(readout, ['13:00:00', '2.00 亿元']);
  await page.keyboard.press('ArrowRight'); await expectReading(readout, ['13:02:30', '缺测']);
  await expect(readout).not.toContainText('9.00');
  await page.keyboard.press('Escape');
  await chart.locator(`g[data-sector-code="${fundA}"] .ty-fund-sample`).nth(1).hover({ force: true });
  await expectReading(readout, ['接收', '12:10:00', '1.00 亿元']);
  if (process.env.UI_CRAFT_CAPTURE) await chart.screenshot({ path: test.info().outputPath('资金走势-午休读数.png') });
  expect(errors).toEqual([]);
});

test('unavailable same-source daily funds stay empty and reduced motion leaves received-point readings immediate', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const errors = await fixture(page, 'funds');
  await page.getByRole('button', { name: '盘后 · 近10日', exact: true }).click();
  const chart = page.locator('.ty-funds-chart'), readout = chart.locator('.ty-chart-readout');
  await expect(chart).toContainText('等待盘后资金历史');
  await expect(chart.locator('.ty-fund-sample')).toHaveCount(0);
  await chart.locator('svg').focus(); await page.keyboard.press('Home');
  await expect(readout).toHaveCount(0);
  await page.getByRole('button', { name: '盘中 · 当日', exact: true }).click();
  await chart.locator('svg').focus(); await page.keyboard.press('Home');
  await expectReading(readout, ['接收', '11:30:00', '0.00 亿元']);
  await page.keyboard.press('End'); await expectReading(readout, ['接收', '13:02:30', '缺测']);
  expect(await chart.evaluate(node => node.getAnimations({ subtree: true }).length)).toBe(0);
  expect(errors).toEqual([]);
});

test.describe('touch chart reading', () => {
  test.use({ hasTouch: true, viewport: { width: 390, height: 844 } });
  test('a tap pins a real point, a second tap releases it without horizontal overflow', async ({ page }) => {
    await fixture(page);
    const chart = page.locator('.ty-breadth-chart');
    const point = await at(chart.locator('svg'), 5);
    await page.touchscreen.tap(point.x, point.y);
    await expectReading(chart.locator('.ty-chart-readout'), ['09:35:00', '已固定']);
    if (process.env.UI_CRAFT_CAPTURE) await chart.screenshot({ path: test.info().outputPath('手机图表-轻触固定.png') });
    await page.touchscreen.tap(point.x, point.y);
    await expect(chart.locator('.ty-chart-readout')).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
});
