import { expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import { test } from './isolated-browser.fixtures';
import { stockA, stockB, terminalFixture } from './terminalTestFixtures';

const fullTheme = '合成超长题材：先进半导体设备与国产高端精密制造';
async function setup(page: Page) {
  await page.route('**/api/v1/**', route => {
    const url = new URL(route.request().url());
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'demo', generated_at: '2001-12-31T15:05:00+08:00', quality: { status: 'partial' } } } });
    if (url.pathname === '/api/v1/session') return ok({ user: { id: 'synthetic-polish', username: 'synthetic', role: 'viewer' }, csrf_token: 'synthetic-only' });
    if (url.pathname === '/api/v1/market-session') return ok({ server_time: '2001-12-31T15:05:00+08:00', trade_day: '2001-12-31', phase: 'closed', is_trading_day: true, automatic_live: false, current_day_active: false, next_transition_at: null, poll_seconds: 60 });
    const data = terminalFixture(url.searchParams);
    data.stocks[0].theme = fullTheme; data.stocks[0].ths_themes = [fullTheme]; data.stocks[0].pct_change = -1;
    data.stocks[1].ths_themes = ['合成题材乙']; data.stocks[1].pct_change = -2;
    if (url.pathname === '/api/v1/workbench-view') return ok(data);
    if (url.pathname === '/api/v1/workbench-details') return ok({ trade_day: data.trade_day, revision: 'synthetic-polish', stocks: data.stocks, auction_days: data.auction_days, events: data.events, seals: {}, sector_code: url.searchParams.get('sector'), members: data.selected_members, members_trade_day: data.members_trade_day, funds_history: [] });
    if (url.pathname === '/api/v1/ai/settings') return ok({ enabled: false, features: [] });
    if (url.pathname === '/api/v1/workbench-updates') return route.fulfill({ contentType: 'text/event-stream', body: ': synthetic control heartbeat\n\n' });
    return ok({ items: [], next_cursor: null });
  });
}

test('notification enters at its anchor, closes accessibly and can reopen during exit', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page); await page.goto('/workbench?view=overview');
  const trigger = page.getByRole('button', { name: /^消息通知/ });
  await trigger.click();
  const panel = page.getByRole('dialog', { name: '消息通知', exact: true });
  await expect(panel).toBeVisible();
  await expect(panel.getByRole('button', { name: '收起消息通知' })).toBeFocused();
  const bounds = await panel.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(11);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(379);
  const rect = await trigger.boundingBox();
  expect(bounds!.y).toBeGreaterThanOrEqual(rect!.y + rect!.height);
  await page.keyboard.press('Escape');
  await expect(trigger).toBeFocused();
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  await trigger.click();
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute('data-state', 'open');
  await page.getByRole('heading', { name: '工作台', exact: true }).click();
  await expect(panel).toBeHidden();
});

test('ranking semantics and full labels survive negative values, keyboard and touch-style activation', async ({ page }) => {
  await setup(page); await page.goto('/workbench?view=overview');
  const ranking = page.locator('.ty-rank-table').first();
  await expect(ranking.locator('th[aria-sort=descending]')).toHaveText('涨幅');
  const first = ranking.locator('tbody tr').first();
  await expect(first.locator('td').first()).toHaveClass('ty-gold');
  const fullLabel = first.getByRole('button', { name: `查看完整内容：${fullTheme}` });
  await fullLabel.focus();
  await expect(page.getByRole('tooltip')).toHaveText(fullTheme);
  await fullLabel.click();
  await expect(page.getByRole('tooltip')).toHaveText(fullTheme);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('tooltip')).toBeHidden();
  await expect(fullLabel).toBeFocused();
  const name = first.locator('.ty-stock-name');
  await name.focus();
  await expect(page.getByRole('tooltip')).toContainText(stockA);
  await expect(page.getByRole('tooltip')).toContainText('行情时点待核对');
  await ranking.locator('tbody tr').nth(1).locator('.ty-stock-name').click();
  await expect(page).toHaveURL(new RegExp(`stock=${stockB.replace('.', '\\.')}`));
});

test('notification reduced motion has no spatial movement and retains close feedback', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await setup(page); await page.goto('/workbench?view=overview');
  await page.getByRole('button', { name: /^消息通知/ }).click();
  const panel = page.getByRole('dialog', { name: '消息通知', exact: true });
  await expect(panel).toBeVisible();
  const movement = await panel.evaluate(element => element.getAnimations().flatMap(animation =>
    animation.effect instanceof KeyframeEffect ? animation.effect.getKeyframes().map(frame => frame.transform).filter(Boolean) : []));
  expect(movement).toEqual([]);
  await panel.getByRole('button', { name: '收起消息通知' }).click();
  await expect(page.getByRole('button', { name: /^消息通知/ })).toBeFocused();
  await expect(panel).toBeHidden();
});

test.describe('touch label disclosure', () => {
  test.use({ hasTouch: true, viewport: { width: 390, height: 844 } });
  test('tapping a theme reveals its full value without selecting its stock', async ({ page }) => {
    await setup(page); await page.goto(`/workbench?view=overview&stock=${stockA}`);
    const secondTheme = page.locator('.ty-rank-table').first().locator('tbody tr').nth(1).locator('.readable-text');
    await secondTheme.tap();
    await expect(page.getByRole('tooltip')).toHaveText('合成题材乙');
    await expect(page).toHaveURL(new RegExp(`stock=${stockA.replace('.', '\\.')}`));
    await secondTheme.tap();
    await expect(page.getByRole('tooltip')).toBeHidden();
  });
});

test('malformed topic data stays local and a valid retry restores the same dated topic', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await setup(page);
  await page.goto('/news?view=topics&topic=合成题材&trade_day=2001-12-31');
  const topic = page.getByRole('region', { name: '题材研究', exact: true });
  await expect(topic.getByRole('alert')).toContainText('题材资料格式暂未核实');
  await page.getByRole('button', { name: /^消息通知/ }).click();
  await expect(page.getByRole('dialog', { name: '消息通知', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.route('**/api/v1/news/topics?**', route => route.fulfill({ json: {
    meta: { app_mode: 'demo' }, data: { topic_source: 'keyword', topic_key: '合成题材', keyword: '合成题材',
      trade_day: '2001-12-31', as_of_time: '2001-12-31T15:05:00+08:00', events: [], total: 0,
      next_cursor: null, members: [], coverage: [], gaps: ['合成空来源范围'] },
  } }));
  await topic.getByRole('button', { name: '重试本区', exact: true }).click();
  await expect(topic.getByRole('alert')).toHaveCount(0);
  await expect(topic).toContainText('该时点暂无匹配资讯；不据此推断没有事件。');
  await expect(topic).toContainText('2001-12-31');
  await expect(page).toHaveURL(/trade_day=2001-12-31/);
  expect(errors).toEqual([]);
});
