import { expect, test, type Page } from '@playwright/test';
import { blank, day, id, meta, metrics } from './reviewTestFixtures';

const today = '2026-10-27', previous = '2026-10-26';

async function fixture(page: Page, serverTime = '09:00:00') {
  const state = { quote: false, serverTime, writes: [] as Record<string, unknown>[], errors: [] as string[] };
  page.on('pageerror', error => state.errors.push(error.message));
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), async () => page.keyboard.press('Escape'));
  const dataFor = (selected: string) => selected !== today ? day(selected) : {
    ...day(today), state: state.quote ? 'partial' : 'unavailable',
    metrics: state.quote ? { ...metrics, basis: 'intraday_snapshot', source_time: `${today}T09:31:00+08:00`,
      sample: { N: 2, daily_loaded: false, snapshot_loaded: true } } : {
      state: 'unavailable', sample: { N: null, daily_loaded: false }, turnover: { amount_yuan: null } },
    indices: [], intraday: [], themes: { items: [] }, pools: { records: [] },
    news: { records: [], scope_note: 'Synthetic current-day empty slot' }, coverage: [], evidence_refs: [],
  };
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const ok = (data: unknown) => route.fulfill({ json: { meta, data } });
    if (path === '/api/v1/session') return ok({ user: { id, username: 'synthetic-current-review', role: 'admin' }, csrf_token: 'fixture' });
    if (path === '/api/v1/market-session') return ok({ server_time: `${today}T${state.serverTime}+08:00`, trade_day: today,
      phase: state.serverTime >= '15:00:00' ? 'closed' : state.serverTime >= '09:30:00' ? 'trading' : 'preopen',
      is_trading_day: true, current_day_active: true,
      automatic_live: state.serverTime >= '09:15:00' && state.serverTime < '15:00:00',
      next_transition_at: null, poll_seconds: 15 });
    if (path.endsWith('/day')) return ok(dataFor(url.searchParams.get('trade_day') ?? today));
    if (path.endsWith('/series')) return ok({ shared_date: today, calendar_state: 'complete', state: 'partial',
      trade_days: [previous, today], records: [previous, today].map(selected => ({ trade_day: selected,
        state: 'partial', metrics: dataFor(selected).metrics, indices: [], themes: { records: [] } })) });
    if (path.endsWith('/notes')) return ok({ trade_day: url.searchParams.get('trade_day') ?? today,
      state: 'empty', content: blank(), head_id: null, writable: true, is_current: true, revision: null });
    if (path.endsWith('/lhb')) return ok({ ...day(today), records: [], selected_record: null, history_dates: [], history_records: [], unmatched_seat_records: [], source_listing_count: 0 });
    if (path.endsWith('/collections') && request.method() === 'POST') {
      state.writes.push(request.postDataJSON()); return ok({ batch_id: id, state: 'queued' });
    }
    if (path.endsWith(`/collections/${id}`)) return ok({ state: 'complete', counts: { succeeded: 1, failed: 0, queued: 0, running: 0 }, jobs: [] });
    return ok({ items: [] });
  });
  return state;
}

test('09:00 reserves today with empty values and incoming quotes fill the same date', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-27T01:00:00Z') });
  const state = await fixture(page);
  await page.goto('/research/reviews?view=emotion');
  await expect(page.getByLabel('复盘日期', { exact: true })).toHaveValue(today);
  await expect(page.getByText('今日已预留 · 等待资料更新', { exact: true })).toBeVisible();
  await page.locator('.dr-maintenance-tools summary').click();
  await expect(page.getByRole('button', { name: '日频待收盘', exact: true })).toBeDisabled();
  expect(state.writes).toEqual([]);
  await page.getByRole('button', { name: '填写判断', exact: true }).click();
  await page.getByLabel('市场判断', { exact: true }).fill('Synthetic unsaved research input');
  state.quote = true; state.serverTime = '09:31:03';
  await page.clock.fastForward(31_000);
  await expect(page.locator('.dr-observation')).toContainText('行情截至 09:31 · 非收盘数据');
  await expect(page.getByLabel('复盘日期', { exact: true })).toHaveValue(today);
  expect(new URL(page.url()).searchParams.has('trade_day')).toBe(false);
  await expect(page.getByLabel('市场判断', { exact: true })).toHaveValue('Synthetic unsaved research input');
  expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
});

for (const [serverTime, disabled] of [['14:59:59', true], ['15:03:00', true], ['15:05:00', false]] as const) {
  test(`current daily collection obeys the server 15:05 boundary at ${serverTime}`, async ({ page }) => {
    const state = await fixture(page, serverTime);
    await page.goto('/research/reviews?view=emotion');
    await page.locator('.dr-maintenance-tools summary').click();
    const button = page.getByRole('button', { name: disabled ? '日频待收盘' : '补齐资料', exact: true });
    if (disabled) {
      await expect(button).toBeDisabled();
      await expect(page.getByText('盘中行情自动更新，日频资料收盘后补齐。', { exact: true })).toBeVisible();
    } else await expect(button).toBeEnabled();
    expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
  });
}

test('explicit previous date keeps its place and remains eligible for daily recovery', async ({ page }) => {
  const state = await fixture(page, '09:00:00');
  await page.goto(`/research/reviews?view=emotion&trade_day=${previous}`);
  await expect(page.getByLabel('复盘日期', { exact: true })).toHaveValue(previous);
  await expect(page.getByText('今日已预留 · 等待资料更新', { exact: true })).toHaveCount(0);
  await page.locator('.dr-maintenance-tools summary').click();
  await page.getByRole('button', { name: '补齐资料', exact: true }).click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0].trade_day).toBe(previous); expect(state.errors).toEqual([]);
});
