import { test, expect } from '@playwright/test';
import { terminalFixture, terminalDay, stockA, stockB } from './terminalTestFixtures';

test('monitor selection stays local and the current ladder follows quote publications', async ({ page }) => {
  let pct = 10, opened = false;
  const stamp = terminalDay + 'T13:01:00+08:00';
  await page.route('**/api/v1/**', route => {
    const url = new URL(route.request().url());
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'real', request_id: 'synthetic',
      generated_at: stamp, quality: { status: 'partial', reasons: ['合成测试'] } } } });
    if (url.pathname.endsWith('/session')) return ok({ user: { id: 'monitor-test', username: 'synthetic', role: 'admin' }, csrf_token: 'synthetic' });
    if (url.pathname.endsWith('/workbench-view')) {
      const data = terminalFixture(url.searchParams);
      return ok({ ...data, stocks: data.stocks.map(row => ({ ...row, pct_change: row.code === stockA ? pct : null })),
        intraday_ladder: { trade_day: terminalDay, phase: 'trading', source_time: stamp, rows: [
          { code: stockA, name: '合成甲', previous_height: 2, target_height: 3, status: opened ? 'opened' : 'sealed',
            freshness: 'current', pct_change: pct, source_time: stamp },
          { code: stockB, name: '合成乙', previous_height: 1, target_height: 2, status: 'waiting',
            freshness: 'current', pct_change: null, source_time: stamp },
        ] } });
    }
    if (url.pathname.endsWith('/kpl-strength')) return ok({ trade_day: terminalDay, state: 'available', rows: [
      { code: 'synthetic', name: '合成题材', strength: '100', limit_up_count: 0 },
    ], history: ['11:29','11:30','13:00','13:01'].map(t => ({
      received_at: terminalDay+'T'+t+':00+08:00', values: { synthetic: '100' },
    })) });
    if (url.pathname.endsWith('/kpl-emotion')) return ok({ state: 'enabled',
      today: { trade_day: terminalDay, latest: { received_at: stamp, value: '50' }, points: [
        '11:29','11:30','13:00','13:01',
      ].map(t => ({ received_at: terminalDay+'T'+t+':00+08:00', value: '50' })) }, yesterday: null });
    if (url.pathname.endsWith('/ai/settings')) return ok({ enabled: false, features: [] });
    return ok({ items: [], next_cursor: null });
  });
  await page.goto('/workbench?workspace=monitor&trade_day='+terminalDay);
  const ladder = page.getByLabel('当日连板梯队', { exact: true });
  await expect(ladder).toContainText('3板');
  await expect(ladder).toContainText('+10.00%');
  await expect(ladder).not.toContainText('昨');
  await expect(ladder).not.toContainText('合成乙');
  await page.getByRole('table', { name: '个股涨幅榜', exact: true }).getByRole('button', { name: /^合成乙/ }).click();
  await expect(page.getByRole('button', { name: '盘面', exact: true })).toHaveAttribute('aria-pressed','true');
  await expect(page).toHaveURL(/stock=999992.SH/);
  await expect(page).not.toHaveURL(/workspace=research/);
  for (const label of ['开盘啦板块强度接收采样曲线', '开盘啦今日与昨日情绪采样曲线']) {
    const chart = page.getByRole('img', { name: label, exact: true });
    await expect(chart).toContainText('11:30');
    await expect(chart).not.toContainText('13:00');
    const path = await chart.locator('path[fill="none"]').first().getAttribute('d');
    expect(path?.match(/M/g)).toHaveLength(1);
  }
  pct = 9; opened = true;
  await page.evaluate(() => window.dispatchEvent(new Event('read-cache-revalidate')));
  await expect(ladder).toContainText('开板');
  await expect(ladder).toContainText('+9.00%');
  await expect(ladder).not.toContainText('+10.00%');
});

test.describe('market panorama responsive containment', () => {
  test.use({ hasTouch: true });

  test('keeps both charts, all ten ranks and source details reachable on narrow screens', async ({ page }) => {
    const stamp = terminalDay + 'T15:01:00+08:00';
    const rows = Array.from({ length: 10 }, (_, index) => ({ code: `synthetic-${index}`,
      name: `合成题材${index + 1}`, strength: String(1000 - index * 100), limit_up_count: index }));
    await page.route('**/api/v1/**', route => {
      const url = new URL(route.request().url());
      const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'real',
        request_id: 'synthetic', generated_at: stamp, quality: { status: 'partial', reasons: ['合成布局测试'] } } } });
      if (url.pathname.endsWith('/session')) return ok({ user: { id: 'panorama-responsive', username: 'synthetic', role: 'admin' }, csrf_token: 'synthetic' });
      if (url.pathname.endsWith('/workbench-view')) return ok(terminalFixture(url.searchParams));
      if (url.pathname.endsWith('/kpl-strength')) return ok({ trade_day: terminalDay, state: 'available',
        received_at: stamp, history_truncated: false, coverage: 'complete', total_count: rows.length, rows,
        history: ['09:30', '11:30', '13:00', '15:00'].map(time => ({ received_at: `${terminalDay}T${time}:00+08:00`,
          values: Object.fromEntries(rows.map(row => [row.code, row.strength])) })) });
      if (url.pathname.endsWith('/kpl-emotion')) return ok({ state: 'enabled',
        today: { trade_day: terminalDay, latest: { received_at: stamp, value: '50' },
          points: [{ received_at: stamp, value: '50' }] }, yesterday: null });
      if (url.pathname.endsWith('/ai/settings')) return ok({ enabled: false, features: [] });
      return ok({ items: [], next_cursor: null });
    });
    await page.goto('/workbench?workspace=monitor&trade_day=' + terminalDay);
    const panel = page.locator('.ty-overview-strength');
    const ranks = panel.getByLabel('开盘啦题材强度前10', { exact: true });
    await expect(ranks.locator('.dir-strength-row')).toHaveCount(10);
    await expect(ranks).toContainText('合成题材10');

    async function expectContained() {
      const boxes = await panel.evaluate(root => {
        const box = (element: Element) => {
          const rect = element.getBoundingClientRect();
          return { top: rect.top, right: rect.right, bottom: rect.bottom, left: rect.left };
        };
        return { panel: box(root), composition: box(root.querySelector('.ty-market-composition')!),
          source: box(root.querySelector('.ty-overview-source')!),
          children: [...root.querySelectorAll('.ty-market-distribution,.ty-market-center,.ty-kpl-top-ten')].map(box),
          lastRank: box(root.querySelector('.dir-strength-row:last-child')!) };
      });
      for (const child of boxes.children) {
        expect(child.left).toBeGreaterThanOrEqual(boxes.panel.left - 1);
        expect(child.right).toBeLessThanOrEqual(boxes.panel.right + 1);
        expect(child.bottom).toBeLessThanOrEqual(boxes.composition.bottom + 1);
      }
      expect(boxes.lastRank.bottom).toBeLessThanOrEqual(boxes.source.top + 1);
      expect(boxes.composition.bottom).toBeLessThanOrEqual(boxes.source.top + 1);
      expect(boxes.source.bottom).toBeLessThanOrEqual(boxes.panel.bottom + 1);
    }

    for (const width of [320, 390, 700]) {
      await page.setViewportSize({ width, height: 844 });
      await expectContained();
      await ranks.locator('.dir-strength-row').last().scrollIntoViewIfNeeded();
      await expect(ranks.locator('.dir-strength-row').last()).toBeInViewport({ ratio: 1 });
      await panel.getByText('数据说明', { exact: true }).click();
      await expect(panel.locator('.ty-overview-source details')).toHaveAttribute('open', '');
      await expectContained();
      await panel.getByText('数据说明', { exact: true }).click();
    }
    for (const width of [701, 1024, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      const layout = await panel.evaluate(root => {
        const center = root.querySelector('.ty-market-center')!.getBoundingClientRect();
        const rank = root.querySelector('.ty-kpl-top-ten')!.getBoundingClientRect();
        return { centerTop: center.top, centerRight: center.right, rankTop: rank.top, rankLeft: rank.left };
      });
      expect(Math.abs(layout.rankTop - layout.centerTop)).toBeLessThanOrEqual(1);
      expect(layout.rankLeft).toBeGreaterThanOrEqual(layout.centerRight - 1);
    }
  });
});
