import { test, expect, type Page } from '@playwright/test';
import { terminalFixture } from './terminalTestFixtures';

type Hint = { context_id: string; sequence: number; trade_day: string; codes: string[] };
async function mock(page: Page, mode: 'live' | 'stale' | 'past' | 'cutoff' | 'inactive') {
  const today = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date());
  const day = mode === 'past' ? '2026-01-05' : today;
  const stamp = day + 'T13:05:00+08:00';
  await page.clock.install({ time: new Date(today + 'T13:05:00+08:00') });
  const hints: Hint[] = [], reads = { market: 0, strength: 0 };
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  const rows = Array.from({ length: 24 }, (_, index) => ({ code: String(801000 + index),
    name: '合成关注题材' + index, strength: String(1000 - index), limit_up_count: null }));
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const ok = (data: unknown) => route.fulfill({ json: { data, meta: { app_mode: 'real',
      quality: { status: 'partial', reasons: [] } } } });
    if (url.pathname === '/api/v1/session') return ok({ user: { id: 'kpl-priority-synthetic',
      username: 'synthetic', role: 'admin' }, csrf_token: 'synthetic-kpl-csrf' });
    if (url.pathname === '/api/v1/workbench-view') {
      reads.market++; return ok({ ...terminalFixture(url.searchParams), trade_day: day, available_days: [day] });
    }
    if (url.pathname === '/api/v1/kpl-strength') {
      reads.strength++; return ok({ trade_day: day, state: mode === 'stale' ? 'stale' : 'available', coverage: 'complete_pages',
        total_count: 24, rows, received_at: stamp, history_truncated: false,
        history: [{ received_at: stamp, values: Object.fromEntries(rows.map(row => [row.code, row.strength])) }] });
    }
    if (url.pathname === '/api/v1/kpl-focus') {
      expect(request.method()).toBe('PUT');
      expect(request.headers()['x-csrf-token']).toBe('synthetic-kpl-csrf');
      const hint = request.postDataJSON() as Hint; hints.push(hint);
      return ok({ active: mode !== 'inactive', accepted_count: mode === 'inactive' ? 0 : hint.codes.length,
        expires_at: new Date(Date.now() + 90_000).toISOString() });
    }
    if (url.pathname === '/api/v1/ths-funds') return ok({ sampled_day: day, status: 'available',
      received_at: stamp, complete: true, missing_pages: [], rejected_rows: 0, last_error: null, rows: [], history: [] });
    if (url.pathname === '/api/v1/ai/settings') return ok({ enabled: false, features: [] });
    return ok({ items: [], rows: [], history: [], points: [], next_cursor: null });
  });
  await page.goto('/workbench?view=funds&trade_day=' + day + (mode === 'cutoff'
    ? '&as_of_time=' + encodeURIComponent(day + 'T13:00:00+08:00') : ''));
  await expect(page.getByRole('checkbox', { name: '对比合成关注题材23强度', exact: true })).toBeVisible();
  return { day, hints, reads, errors };
}

test('native KPL selection publishes ordered short-lived hints without invalidating market reads', async ({ page }) => {
  const { day, hints, reads, errors } = await mock(page, 'live');
  await expect.poll(() => hints.some(hint => hint.codes.length === 6)).toBe(true);
  await page.waitForTimeout(200);
  const before = { ...reads };
  await page.getByRole('checkbox', { name: '对比合成关注题材23强度', exact: true }).check();
  await expect.poll(() => hints.at(-1)?.codes.includes('801023')).toBe(true);
  await page.waitForTimeout(200);
  expect(reads).toEqual(before);
  const selected = hints.at(-1)!;
  expect(selected.trade_day).toBe(day); expect(selected.codes).toHaveLength(7);
  expect(selected.codes.every(code => /^801\d{3}$/.test(code))).toBe(true);
  await page.clock.fastForward(10_001);
  await expect.poll(() => hints.some(hint => hint.context_id === selected.context_id &&
    hint.sequence > selected.sequence && hint.codes.includes('801023'))).toBe(true);
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await expect.poll(() => hints.at(-1)?.codes.length).toBe(0);
  const hiddenCount = hints.length, clearedSequence = hints.at(-1)!.sequence;
  await page.clock.fastForward(30_001);
  expect(hints).toHaveLength(hiddenCount);
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await expect.poll(() => hints.at(-1)?.codes.includes('801023')).toBe(true);
  expect(hints.at(-1)!.sequence).toBeGreaterThan(clearedSequence);
  const sequences = hints.filter(hint => hint.context_id === selected.context_id).map(hint => hint.sequence);
  expect(new Set(sequences).size).toBe(sequences.length);
  expect(errors).toEqual([]);
});

for (const mode of ['past', 'cutoff'] as const) {
  test(mode + ' KPL view never renews live collection priorities', async ({ page }) => {
    const { hints, errors } = await mock(page, mode);
    await page.getByRole('checkbox', { name: '对比合成关注题材23强度', exact: true }).check();
    await page.clock.fastForward(45_001);
    expect(hints).toEqual([]); expect(errors).toEqual([]);
  });
}

test('inactive owner capability stops KPL priority heartbeats', async ({ page }) => {
  const { hints, errors } = await mock(page, 'inactive');
  await expect.poll(() => hints.some(hint => hint.codes.length > 0)).toBe(true);
  await page.waitForTimeout(250);
  const count = hints.length;
  await page.clock.fastForward(45_001);
  expect(hints).toHaveLength(count); expect(errors).toEqual([]);
});

test('older current-day ranking can still prioritize explicitly selected native board details', async ({ page }) => {
  const { hints, errors } = await mock(page, 'stale');
  await page.getByRole('checkbox', { name: '对比合成关注题材23强度', exact: true }).check();
  await expect.poll(() => hints.at(-1)?.codes.includes('801023')).toBe(true);
  expect(errors).toEqual([]);
});
