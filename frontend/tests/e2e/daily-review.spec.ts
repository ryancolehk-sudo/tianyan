import { expect, test } from '@playwright/test';

// Bootstrap behavior is covered separately; these tests exercise each existing page.
test.beforeEach(async ({ page }) => {
  await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), async () => {
    await page.keyboard.press('Escape');
  });
});
import type { Page } from '@playwright/test';

// Explicit synthetic protocol fixtures. Real acquisition and private restored-
// data acceptance are recorded separately; these are never market evidence.
import { id, meta, days, ref, raw, metrics, theme, day, blank, listing } from './reviewTestFixtures';
import { researchOverviewFixture } from './dailyReviewContractFixtures';

async function mock(page: Page, role = 'admin', breadthOverride: Record<string, unknown> = {}, metricOverrides: Record<string, unknown> = {}) {
  const fixtureMetrics = { ...metrics, ...metricOverrides, breadth: { ...metrics.breadth, ...breadthOverride } };
  let content = blank(); let version = 0; let head: string | null = null;
  let saved: unknown[] = []; const history: unknown[] = [];
  let releaseSave: (() => void) | null = null;
  const state = { holdSave: false, conflict: false, delayOld: false, dayReads: 0, seriesReads: 0, collectionWrites: 0, errors: [] as string[] };
  page.on('pageerror', e => state.errors.push(e.message));
  const note = () => ({ trade_day: days[1], state: version ? 'complete' : 'empty', content, head_id: head, writable: role !== 'viewer', is_current: true, revision: version ? { id: head, version_number: version, received_at: '2026-10-25T03:00:00+08:00', content_hash: 'b'.repeat(64) } : null });
  await page.route('**/api/v1/**', async route => {
    const req = route.request(); const url = new URL(req.url()); const path = url.pathname;
    const ok = (data: unknown) => route.fulfill({ json: { meta, data } });
    if (path.endsWith('/session')) return ok({ user: { id, username: 'synthetic-review', role }, csrf_token: 'fixture' });
    if (path.endsWith('/watchlist')) return ok({ items: [] });
    if (path === '/api/v1/review-data/overview') return ok(researchOverviewFixture(url.searchParams.get('trade_day') ?? days[1], days));
    if (path.includes('/evidence/')) return ok({ id, received_at: ref.received_at, version_number: 1, content_hash: ref.content_hash, payload: { rows: [raw()], columns: [] } });
    if (path.endsWith('/series')) { state.seriesReads++; return ok({ shared_date: days[1], state: 'partial', trade_days: days, records: days.map(d => ({ trade_day: d, state: 'partial', metrics: fixtureMetrics, indices: day(d).indices, themes: { records: [theme.provider_record] }, coverage: [], evidence_refs: [] })) }); }
    if (path.endsWith('/day')) {
      state.dayReads++; const selected = url.searchParams.get('trade_day') ?? days[1];
      if (state.delayOld && selected === days[0]) await new Promise(r => setTimeout(r, 600));
      if (url.searchParams.has('as_of_time')) return ok({ ...day(selected), calendar_state: 'unavailable', state: 'unavailable', metrics: { state: 'unavailable' }, indices: [], intraday: [], themes: { items: [] }, pools: { records: [] }, news: { records: [], scope_note: '尚未接收' }, evidence_refs: [] });
      return ok({ ...day(selected), metrics: fixtureMetrics });
    }
    if (path.endsWith('/stocks')) return ok({ shared_date: days[1], state: 'complete', items: (url.searchParams.get('codes') ?? '').split(',').map(ts_code => ({ ts_code, name: '合成机电', state: 'complete', quote: raw(ts_code), pool_record: { ...raw(ts_code), limit: 'U', limit_times: '1' } })) });
    if (path.endsWith('/theme-history')) return ok({ shared_date: days[1], state: 'complete', items: [{ ts_code: theme.ts_code, records: days.map(trade_day => ({ trade_day, catalog_record: theme.provider_record, fund_record: theme.fund_records[0], state: 'complete' })) }] });
    if (path.endsWith('/notes/days')) return ok({ items: version ? [{ trade_day: days[1], stage: content.stage, theme_stages: content.theme_stages }] : [] });
    if (path.endsWith('/notes/history')) return ok({ items: [...history].reverse() });
    if (path.endsWith('/notes')) {
      if (req.method() === 'POST') {
        const body = req.postDataJSON(); saved.push(body);
        if (state.conflict) return route.fulfill({ status: 409, json: { error: { message: '笔记已有新版本，原记录已保留。' } } });
        content = body.content; head = `00000000-0000-4000-8000-${String(++version).padStart(12, '0')}`;
        const value = structuredClone(note()); history.push(value);
        if (state.holdSave) await new Promise<void>(resolve => { releaseSave = resolve; });
        return ok(value);
      }
      return ok(note());
    }
    if (path.endsWith('/lhb')) return ok({ ...day(days[1]), records: [listing(), listing('连续三日合成原因', 'lhb_three')], selected_record: null, history_dates: days, history_records: days.map(trade_day => ({ trade_day, ts_code: '600001.SH', reason: '单日合成原因', record_key: 'lhb_one', net_amount: '200000000', state: 'available', source_rows: [raw()] })), unmatched_seat_records: [], source_listing_count: 2 });
    if (path.endsWith('/seat-disclosures')) return ok({ ...day(days[1]), seat_name: url.searchParams.get('seat_name'), records: [{ trade_day: days[1], ts_code: '600001.SH', name: '合成机电', reason: '单日合成原因', amount_yuan: '100000000', seat_record: raw(), listing_record: { pct_change: '10' }, lhb_record_key: 'lhb_one' }], matched_record_count: 1, truncated: false });
    if (path.endsWith('/collections') && req.method() === 'POST') { state.collectionWrites++; return ok({ batch_id: id, state: 'queued' }); }
    if (path.endsWith('/collections/' + id)) return ok({ state: 'complete', counts: { succeeded: 1, failed: 0, queued: 0, running: 0 }, jobs: [] });
    return ok({ items: [] });
  });
  return { state, writes: () => saved, seed: (patch: Partial<ReturnType<typeof blank>>) => { content = { ...blank(), ...patch }; version = 1; head = id; }, release: () => { releaseSave?.(); state.holdSave = false; } };
}

test('stored intraday review metrics retain their source clock and never claim a closing result', async ({ page }, info) => {
  const f = await mock(page, 'admin', {}, { basis: 'intraday_snapshot', source_time: '2026-10-23T13:46:08+08:00', source_received_at: '2026-10-23T15:26:00+08:00', scope: '合成已存盘中样本，非收盘结果。' });
  await page.goto('/research/reviews?view=emotion&trade_day=2026-10-23');
  await expect(page.locator('.dr-observation')).toContainText('行情截至 13:46 · 非收盘数据');
  await expect(page.locator('.dr-observation')).not.toContainText('15:26');
  await expect(page.locator('.dr-snapshot-badge').filter({ hasText: '盘中 13:46' }).first()).toBeVisible();
  await expect(page.getByRole('columnheader', { name: '现价中位', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: '今收中位', exact: true })).toHaveCount(0);
  await expect(page.getByText('非收盘分布', { exact: false })).toBeVisible();
  await page.getByRole('navigation', { name: '每日复盘页面' }).getByRole('link', { name: '行情总览', exact: true }).click();
  await page.getByRole('button', { name: '研究记录', exact: true }).click();
  await expect(page.getByText('采样时点沪深成交额', { exact: true })).toBeVisible();
  await expect(page.locator('.dr-turnover-pair')).not.toContainText('+10.00%');
  await expect(page.locator('.dr-feedback-cards')).toContainText('采样时点有效');
  await expect(page.locator('.dr-feedback-cards')).not.toContainText('收盘有效');
  await page.screenshot({ path: info.outputPath('review-intraday-sample-clock.png'), fullPage: true });
  expect(f.state.collectionWrites).toBe(0); expect(f.state.errors).toEqual([]);
});

for (const width of [1672, 1440, 390]) test(`daily review four linked layouts ${width}`, async ({ page }, info) => {
  const fixture = await mock(page); await page.setViewportSize({ width, height: 941 });
  await page.goto('/research/reviews');
  await expect(page.getByRole('heading', { name: '情绪周期表', exact: true })).toBeVisible();
  const initialSeriesReads = fixture.state.seriesReads;
  for (const [view, heading] of [['emotion', '情绪周期表'], ['direction', '板块轮动'], ['lhb', '上榜对象'], ['overview', '市场环境与情绪']]) {
    await page.getByRole('navigation', { name: '每日复盘页面' }).getByRole('link', { name: { emotion: '情绪周期', direction: '板块轮动', lhb: '龙虎榜', overview: '行情总览' }[view]!, exact: true }).click();
    if (view === 'overview') await page.getByRole('button', { name: '研究记录', exact: true }).click();
    await expect(view==='direction'?page.getByRole('region',{name:'板块轮动工作区',exact:true}):page.getByRole('heading', { name: heading, exact: false })).toBeVisible();
    await expect(page.getByText('演示环境 · 非真实行情', { exact: true })).toBeVisible();
    expect(await page.locator('body').evaluate(e => e.scrollWidth <= innerWidth + 2)).toBeTruthy();
    await page.screenshot({ path: info.outputPath(`${view}-${width}.png`), fullPage: true });
  }
  expect(fixture.state.seriesReads).toBe(initialSeriesReads); expect(fixture.state.collectionWrites).toBe(0); expect(fixture.state.errors).toEqual([]);
});

test('manual note revisions, core linkage and stale-head input preservation', async ({ page }) => {
  const f = await mock(page); await page.goto('/research/reviews?view=overview&trade_day=2026-10-23');
  await page.getByRole('button', { name: '研究记录', exact: true }).click();
  await page.getByRole('button', { name: '填写判断', exact: true }).click();
  const modal = page.getByRole('dialog', { name: '编辑市场判断' });
  await modal.getByLabel('情绪阶段', { exact: true }).selectOption('局部修复');
  await modal.getByLabel('市场判断', { exact: true }).fill('合成判断，仅验证笔记交互');
  await modal.getByRole('button', { name: '保存为新版本' }).click();
  await expect(page.getByText('人工笔记 v1 · 已保存', { exact: true })).toBeVisible();
  await page.getByRole('navigation', { name: '每日复盘页面' }).getByRole('link', { name: '板块轮动', exact: true }).click();
  await page.getByRole('button',{name:'研究记录',exact:true}).click();
  await page.getByRole('region', { name: '选中题材内部梯队' }).getByRole('button', { name: '加入跟踪 ＋' }).click();
  const core = page.getByRole('dialog', { name: '编辑核心跟踪' });
  await core.getByLabel('研究角色', { exact: true }).fill('合成容量观察');
  await core.getByRole('button', { name: '保存为新版本' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByText('人工笔记 v2 · 已保存', { exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: '核心个股跟踪' })).toContainText('合成容量观察');
  await page.reload(); await page.getByRole('button',{name:'研究记录',exact:true}).click(); await expect(page.getByRole('region', { name: '核心个股跟踪' })).toContainText('合成容量观察');
  await page.getByRole('navigation', { name: '每日复盘页面' }).getByRole('link', { name: '行情总览', exact: true }).click();
  await page.getByRole('button', { name: '编辑判断', exact: true }).click(); await page.getByLabel('市场判断', { exact: true }).fill('冲突时保留的本地输入'); f.state.conflict = true;
  await page.getByRole('dialog').getByRole('button', { name: '保存为新版本' }).click();
  await expect(page.getByRole('dialog')).toContainText('笔记已有新版本'); await expect(page.getByLabel('市场判断', { exact: true })).toHaveValue('冲突时保留的本地输入');
  expect(f.writes().length).toBe(3); expect(f.state.errors).toEqual([]);
});

test('disclosure periods, same-named seats and original headline remain distinct', async ({ page }) => {
  await mock(page); await page.goto('/research/reviews?view=lhb&trade_day=2026-10-23');
  await expect(page.locator('.dr-lhb-totals')).toContainText('2.00亿');
  await expect(page.getByRole('region', { name: '买入侧原始席位' }).getByRole('button', { name: '机构专用', exact: true })).toHaveCount(2);
  await page.getByRole('region', { name: '买入侧原始席位' }).getByRole('button', { name: '机构专用', exact: true }).first().click();
  await expect(page.getByRole('region', { name: '席位近期买入记录' })).toContainText('合成机电');
  await expect(page.getByText(/相同席位名称不代表唯一交易者/)).toBeVisible();
  await page.getByRole('region', { name: '龙虎榜上榜对象' }).getByRole('row').filter({ hasText: '连续三日合成原因' }).getByRole('button', { name: '合成机电', exact: false }).click();
  await expect(page.locator('.dr-listing-reason')).toContainText('连续三日合成原因');
  await expect(page.getByRole('navigation', { name: '每日复盘页面' }).getByRole('link', { name: '明日预案', exact: true })).toHaveAttribute('href', '/research/tomorrow?source_trade_day=2026-10-23');
});

test('late date responses and observed gaps never substitute latest quotes', async ({ page }) => {
  const f = await mock(page); f.state.delayOld = true;
  await page.goto('/research/reviews?view=emotion&trade_day=2026-10-23');
  await expect(page.locator('.dr-selected-day')).toContainText('2026-10-23');
  await page.getByLabel('复盘日期', { exact: true }).fill('2026-10-22');
  await page.getByLabel('复盘日期', { exact: true }).fill('2026-10-23');
  await expect(page.locator('.dr-selected-day')).toContainText('2026-10-23');
  await page.goto('/research/reviews?view=overview&trade_day=2026-10-23&as_of_time=2026-10-23T15%3A00%3A00%2B08%3A00');
  await expect(page.getByText(/所选日期不是当前可核实/)).toBeVisible();
  await expect(page.locator('.dr-index-chart')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '填写判断', exact: true })).toBeDisabled();
  expect(f.state.collectionWrites).toBe(0); expect(f.state.errors).toEqual([]);
});


test('a saved response from an unmounted date cannot overwrite newer private input', async ({ page }) => {
  const f = await mock(page); await page.goto('/research/reviews?view=overview&trade_day=2026-10-23');
  await page.getByRole('button', { name: '填写判断', exact: true }).click();
  await page.getByLabel('市场判断', { exact: true }).fill('等待回包的旧输入');
  f.state.holdSave = true;
  await page.getByRole('dialog').getByRole('button', { name: '保存为新版本' }).click();
  await expect.poll(() => f.writes().length).toBe(1);
  // Change the shared URL as an in-app navigation, leaving the pending editor.
  await page.evaluate(() => { history.pushState({}, '', '/research/reviews?view=overview&trade_day=2026-10-22'); dispatchEvent(new PopStateEvent('popstate')); });
  await expect(page.getByLabel('复盘日期', { exact: true })).toHaveValue('2026-10-22');
  await page.getByLabel('复盘日期', { exact: true }).fill('2026-10-23');
  await page.getByRole('button', { name: '编辑判断', exact: true }).click();
  await page.getByLabel('市场判断', { exact: true }).fill('返回日期后的新输入');
  f.release(); await expect(page.getByLabel('市场判断', { exact: true })).toHaveValue('返回日期后的新输入');
  await page.getByRole('dialog').getByRole('button', { name: '暂不保存' }).click();
  await page.getByLabel('复盘日期', { exact: true }).fill('2026-10-22');
  await page.getByLabel('复盘日期', { exact: true }).fill('2026-10-23');
  await page.getByRole('button', { name: '编辑判断', exact: true }).click();
  await expect(page.getByLabel('市场判断', { exact: true })).toHaveValue('返回日期后的新输入');
  expect(f.state.errors).toEqual([]);
});


test('cache refresh reloads disclosures and drops revoked child responses', async ({ page }) => {
  await mock(page); let revoked = false; let reads = 0;
  await page.route('**/api/v1/review-data/lhb?*', async route => { reads++; return route.fulfill({ json: { meta, data: { ...day(days[1]), records: revoked ? [] : [listing()], selected_record: null, history_dates: [], history_records: [], unmatched_seat_records: [], source_listing_count: revoked ? 0 : 1 } } }); });
  await page.goto('/research/reviews?view=lhb&trade_day=2026-10-23');
  await expect(page.locator('.dr-lhb-totals')).toContainText('2.00亿'); const old = reads;
  revoked = true; await page.getByRole('button', { name: '重读缓存', exact: true }).click();
  await expect.poll(() => reads).toBeGreaterThan(old);
  await expect(page.locator('.dr-lhb-totals')).toHaveCount(0);
  await expect(page.getByText('该具体披露未在所选日期返回，请从左侧重新选择。')).toBeVisible();
});

test('confirmation binds base data, selected members and original theme history', async ({ page }) => {
  const f = await mock(page); const memberId = id.slice(0, -2) + '82'; const historyId = id.slice(0, -2) + '83';
  await page.route('**/api/v1/review-data/day?*', route => {
    const value = structuredClone(day(days[1]));
    if (new URL(route.request().url()).searchParams.has('sector')) value.themes.items[0].member_records[0].row_reference = { ...ref, id: memberId };
    return route.fulfill({ json: { meta, data: value } });
  });
  await page.route('**/api/v1/review-data/theme-history?*', route => route.fulfill({ json: { meta, data: { ...day(days[1]), items: [{ ts_code: theme.ts_code, records: [{ trade_day: days[0], catalog_record: { ...theme.provider_record, row_reference: { ...ref, id: historyId } }, fund_record: null, state: 'complete' }] }] } } }));
  await page.goto('/research/reviews?view=direction&trade_day=2026-10-23');
  await page.getByRole('button',{name:'研究记录',exact:true}).click();
  await page.getByRole('button', { name: '编辑 ✎', exact: true }).click();
  await page.getByLabel('主线候选题材', { exact: true }).selectOption(theme.ts_code);
  await page.getByRole('dialog').getByRole('button', { name: '保存为新版本' }).click();
  await page.getByRole('button', { name: '确认板块研究', exact: true }).click();
  await expect(page.getByRole('button', { name: '当前版本已人工确认', exact: true })).toBeVisible();
  const body = f.writes().at(-1) as { content: { data_evidence_ids: string[] } };
  expect(body.content.data_evidence_ids.sort()).toEqual([id, memberId, historyId].sort());
});

test('all thirty core quotes remain when another stock is selected', async ({ page }) => {
  const f = await mock(page);
  f.seed({ core_stocks: Array.from({ length: 30 }, (_, i) => ({ code: `${600100 + i}.SH`, name: `合成核心${i + 1}`, sector_code: theme.ts_code, role: '观察', rationale: '', tag: '观察' })) });
  await page.goto('/research/reviews?view=direction&trade_day=2026-10-23&stock=600001.SH');
  await page.getByRole('button',{name:'研究记录',exact:true}).click();
  const last = page.getByRole('region', { name: '核心个股跟踪' }).getByRole('row').filter({ hasText: '合成核心30' });
  await expect(last).toContainText('+10.00%'); await expect(page.locator('.dr-stock-context')).toContainText('+10.00%');
});

test('unverified feedback is unknown rather than zero', async ({ page }) => {
  await mock(page);
  const value = structuredClone(day(days[1]));
  Object.values(value.metrics.relay).forEach(group => { if (group && typeof group === 'object' && 'promotion_n' in group) Object.assign(group, { promotion_n: 0, promotion_valid_n: 0, down_limit_n: 0, down_limit_valid_n: 0, promotion_pct: null }); });
  await page.route('**/api/v1/review-data/day?*', route => route.fulfill({ json: { meta, data: value } }));
  await page.goto('/research/reviews?view=emotion&trade_day=2026-10-23');
  await expect(page.getByRole('region', { name: '昨日固定样本今日反馈' }).getByText('待核1', { exact: true })).toHaveCount(2);
  await expect(page.getByRole('region', { name: '昨日固定样本今日反馈' }).getByText('跌停 待核1', { exact: true })).toHaveCount(2);
});

test('truncated members cannot be presented as a complete theme disclosure filter', async ({ page }) => {
  await mock(page); const value = structuredClone(day(days[1])); value.themes.items[0].truncated = true; value.themes.items[0].member_records = [];
  await page.route('**/api/v1/review-data/day?*', route => route.fulfill({ json: { meta, data: value } }));
  await page.goto('/research/reviews?view=lhb&trade_day=2026-10-23');
  await page.getByLabel('龙虎榜筛选范围', { exact: true }).selectOption('sector');
  await expect(page.getByText('原日成员详情已截断，此处仅筛选已返回成员，不能代表题材全部披露。')).toBeVisible();
  await expect(page.getByText('已返回成员中未匹配到披露；完整题材范围尚不可判断。')).toBeVisible();
});


test('numeric board heights preserve representatives and foreign-scope members stay excluded', async ({ page }) => {
  await mock(page); const value = structuredClone(day(days[1]));
  value.themes.items[0].member_records.push({ con_code: '920001.BJ', name: '合成北交成员', row_reference: ref });
  await page.route('**/api/v1/review-data/day?*', route => route.fulfill({ json: { meta, data: value } }));
  await page.goto('/research/reviews?view=overview&trade_day=2026-10-23');
  await page.getByRole('button', { name: '研究记录', exact: true }).click();
  await expect(page.getByRole('region', { name: '涨停高度梯队' }).getByRole('button', { name: '合成机电', exact: true })).toBeVisible();
  await page.getByRole('navigation', { name: '每日复盘页面' }).getByRole('link', { name: '板块轮动', exact: true }).click();
  await page.getByRole('button',{name:'研究记录',exact:true}).click();
  const members = page.getByRole('region', { name: '选中题材内部梯队' });
  await expect(members.locator('tbody tr')).toHaveCount(1); await expect(members).not.toContainText('合成北交成员');
  await expect(members).toContainText('1板'); await expect(members).not.toContainText('1.0板');
});


// Synthetic eleven-bin data. The numeric table retains the selected D text palette.
for(const width of [1440,390])test(`visible statistic scopes retain their values and survive horizontal reading ${width}`,async({page},info)=>{
  const f=await mock(page,'admin',detailedBreadth([2,...elevenCounts.slice(1,-1),58]),{limits:{...metrics.limits,supplier_reported:{...metrics.limits.supplier_reported,up:54,down:1}}});
  await page.setViewportSize({width,height:1000});await page.goto('/research/reviews?view=emotion&trade_day=2026-10-23');
  const table=page.getByRole('region',{name:'情绪周期每日统计',exact:true});
  await expect(table.locator('.dr-scope-head th').filter({hasText:'非ST短线池'})).toHaveCount(2);
  await expect(table.locator('.dr-scope-head th').filter({hasText:'沪深日线 · 含ST'})).toHaveAttribute('colspan','16');
  const headers=await table.locator('.dr-column-head th').allTextContents(),row=table.locator('tbody tr').first();
  await expect(row.locator('td').nth(headers.indexOf('涨跌停比'))).toHaveText('54:1');
  await expect(row.locator('td').nth(headers.indexOf('涨停'))).toHaveText('58');
  await expect(row.locator('td').nth(headers.indexOf('跌停'))).toHaveText('2');
  expect(headers.length).toBe(await row.locator('td').count());
  await table.getByRole('columnheader',{name:'跌停',exact:true}).scrollIntoViewIfNeeded();
  const bounds=await table.locator('.dr-scope-head th').filter({hasText:'沪深日线 · 含ST'}).locator('span').evaluate(element=>{const label=element.getBoundingClientRect(),pane=element.closest('.dr-table-scroll')!.getBoundingClientRect();return{left:label.left,right:label.right,paneLeft:pane.left,paneRight:pane.right};});
  expect(bounds.left).toBeGreaterThanOrEqual(bounds.paneLeft-1);expect(bounds.right).toBeLessThanOrEqual(bounds.paneRight+1);
  await page.screenshot({path:info.outputPath(`synthetic-statistic-scopes-${width}.png`),fullPage:true});
  await page.getByRole('button',{name:'字段设置',exact:true}).click();
  await page.getByRole('checkbox',{name:'市场广度与分布',exact:true}).uncheck();
  await expect(table.locator('.dr-scope-head th').filter({hasText:'沪深日线 · 含ST'})).toHaveAttribute('colspan','2');
  await page.getByRole('checkbox',{name:'涨跌停与连板高度',exact:true}).uncheck();
  await expect(table.locator('.dr-scope-head th').filter({hasText:'沪深日线 · 含ST'})).toHaveCount(0);
  expect(f.writes()).toEqual([]);expect(f.state.collectionWrites).toBe(0);expect(f.state.errors).toEqual([]);
});

for(const width of [1440,390])test(`judgment entry is one action and preserves unsaved versions ${width}`,async({page},info)=>{
  const f=await mock(page);await page.setViewportSize({width,height:1000});await page.goto('/research/reviews?view=emotion&trade_day=2026-10-23');
  const entry=page.getByRole('button',{name:'填写判断',exact:true});await expect(entry).toBeEnabled();await expect(page.getByRole('button',{name:'补齐资料',exact:true})).toBeHidden();
  await entry.focus();await page.keyboard.press('Enter');const dialog=page.getByRole('dialog',{name:'编辑市场判断',exact:true});await expect(dialog).toBeVisible();
  await dialog.getByLabel('市场判断',{exact:true}).fill('合成研究判断，仅用于版本与交互验证');await dialog.getByRole('button',{name:'暂不保存',exact:true}).click();
  await expect(page.getByText('人工修改 · 尚未保存',{exact:true})).toBeVisible();await page.getByRole('button',{name:'编辑判断',exact:true}).click();
  await expect(dialog.getByLabel('市场判断',{exact:true})).toHaveValue('合成研究判断，仅用于版本与交互验证');await dialog.getByRole('button',{name:'保存为新版本',exact:true}).click();
  await expect(page.getByText('人工笔记 v1 · 已保存',{exact:true})).toBeVisible();await expect(page.getByRole('button',{name:'历史版本',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'确认本页',exact:true}).click();await expect(page.getByRole('button',{name:'当前版本已人工确认',exact:true})).toBeVisible();
  await page.locator('.dr-maintenance-tools summary').focus();await page.keyboard.press('Enter');await expect(page.getByRole('button',{name:'补齐资料',exact:true})).toBeVisible();
  await page.keyboard.press('Escape');await expect(page.locator('.dr-maintenance-tools summary')).toBeFocused();await expect(page.getByRole('button',{name:'补齐资料',exact:true})).toBeHidden();
  await page.screenshot({path:info.outputPath(`synthetic-judgment-entry-${width}.png`),fullPage:true});
  expect(f.writes()).toHaveLength(2);expect(f.state.collectionWrites).toBe(0);expect(f.state.errors).toEqual([]);
});

test('direction judgment opens directly and read-only review cannot open an editable judgment',async({page})=>{
  const f=await mock(page);await page.goto('/research/reviews?view=direction&trade_day=2026-10-23');await page.getByRole('button',{name:'填写判断',exact:true}).click();
  await expect(page.getByRole('dialog',{name:'编辑方向与题材阶段',exact:true})).toBeVisible();await page.getByRole('dialog').getByRole('button',{name:'暂不保存',exact:true}).click();
  await page.goto('/research/reviews?view=emotion&trade_day=2026-10-23&as_of_time=2026-10-23T15%3A00%3A00%2B08%3A00');
  await expect(page.getByRole('button',{name:'填写判断',exact:true})).toBeDisabled();await expect(page.getByRole('button',{name:'填写判断',exact:true})).toHaveAttribute('title','当时已知回看为只读');
  expect(f.writes()).toEqual([]);expect(f.state.collectionWrites).toBe(0);expect(f.state.errors).toEqual([]);
});

const distributionKeys = ['limit_down', 'down_ge7', 'down_5_7', 'down_3_5', 'down_0_3', 'flat', 'up_0_3', 'up_3_5', 'up_5_7', 'up_ge7', 'limit_up'];
const elevenCounts = [32, 58, 140, 320, 1350, 200, 2000, 500, 260, 100, 40];
function detailedBreadth(counts: Array<number | null> = elevenCounts, unverified = 0) {
  const sum = (items: Array<number | null>) => items.reduce<number>((a, n) => a + (n ?? 0), 0);
  return { up: sum(counts.slice(6)), down: sum(counts.slice(0, 5)), flat: counts[5], valid_n: sum(counts), missing_n: 0,
    distribution: { version: 'absolute-limits-v1', state: unverified ? 'partial' : 'complete', bins: Object.fromEntries(distributionKeys.map((key, i) => [key, counts[i]])), valid_n: sum(counts), missing_n: 0, limit_unverified_n: unverified, no_limit_n: 0 } };
}
for (const width of [1672, 390]) test(`distribution shows eleven numeric columns with only the board ratio bar ${width}`, async ({ page }, info) => {
  await page.setViewportSize({ width, height: 941 });
  const f = await mock(page, 'admin', detailedBreadth());
  await page.goto('/research/reviews?view=emotion&trade_day=2026-10-23');
  const table = page.getByRole('region', { name: '情绪周期每日统计' });
  await expect(table.getByRole('columnheader', { name: '跌停', exact: true })).toBeAttached();
  const headers = await table.locator('.dr-column-head th').allTextContents();
  const start = headers.indexOf('跌停');
  expect(headers.slice(start, start + 11)).toEqual(['跌停', '跌≥7%', '跌5–7%', '跌3–5%', '跌0–3%', '平盘', '涨0–3%', '涨3–5%', '涨5–7%', '涨≥7%', '涨停']);
  const row = table.locator('tbody tr').first();
  expect((await row.locator('td').allTextContents()).slice(start, start + 11)).toEqual(elevenCounts.map(String));
  await expect(row.locator('td').nth(start)).toHaveCSS('color', 'rgb(74, 178, 185)');
  await expect(row.locator('td').nth(start + 5)).toHaveCSS('color', 'rgb(115, 129, 143)');
  await expect(row.locator('td').nth(start + 10)).toHaveCSS('color', 'rgb(209, 110, 155)');
  await expect(page.locator('.dr-breadth-stripe')).toHaveCount(0);
  await expect(page.getByRole('button', { name: /展开分布列|收起分布列/ })).toHaveCount(0);
  await expect(table.getByRole('columnheader', { name: /1%/ })).toHaveCount(0);
  await expect(page.getByRole('img', { name: '沪深股票涨跌效应十一区间分布', exact: true })).toBeAttached();
  await page.screenshot({ path: info.outputPath(`涨跌分布数值_${width}_示例数据.png`), fullPage: true });
  expect(f.state.collectionWrites).toBe(0); expect(f.state.errors).toEqual([]);
});

test('numeric distribution preserves zeros and distinguishes missing values and unverified limits', async ({ page }) => {
  await mock(page, 'admin', detailedBreadth([0, 0, 0, 100, 0, 0, 0, 0, 0, 0, 0], 7));
  await page.goto('/research/reviews?view=emotion&trade_day=2026-10-23');
  const table = page.getByRole('region', { name: '情绪周期每日统计' });
  await expect(table.getByRole('columnheader', { name: '跌停', exact: true })).toBeAttached();
  const start = (await table.locator('.dr-column-head th').allTextContents()).indexOf('跌停');
  const cells = table.locator('tbody tr').first().locator('td');
  expect((await cells.allTextContents()).slice(start, start + 11)).toEqual(['0', '0', '0', '100', '0', '0', '0', '0', '0', '0', '0']);
  await expect(page.getByText('封板状态待核 7 只，仍按实际涨跌幅分档。')).toBeVisible();
  await page.unrouteAll();
  await mock(page, 'admin', detailedBreadth([null, ...Array(10).fill(0)]));
  await page.reload();
  await expect(cells.nth(start)).toHaveText('—');
  await expect(cells.nth(start + 1)).toHaveText('0');
  await expect(page.getByText('所选日期尚无足够的已存行情或日频资料，暂不能计算涨跌分布；缺失不计作0。', { exact: true })).toBeVisible();
  await page.unrouteAll();
  const missing = detailedBreadth(Array(11).fill(0));
  missing.missing_n = 3; missing.distribution.missing_n = 3; missing.distribution.state = 'partial';
  await mock(page, 'admin', missing); await page.reload();
  await expect(page.locator('.dr-breadth-stripe')).toHaveCount(0);
  await expect(page.getByText('所选日期暂无有效涨跌幅，缺失不计作平盘。', { exact: true })).toBeVisible();
});

test('emotion indicators use the requested order, reduced ratios and opened seals distinct from closing seals', async ({ page }, info) => {
  await page.setViewportSize({ width: 2500, height: 1100 });
  const intraday = { state: 'complete', N: 5000, opened_up: 7, opened_down: 3, known_opened_up: 7, known_opened_down: 3, up_unknown_n: 0, down_unknown_n: 0 };
  const f = await mock(page, 'admin', detailedBreadth(), { limits: { ...metrics.limits,
    supplier_reported: { ...metrics.limits.supplier_reported, up: 20, down: 40, broken: 5,
      ladder: { '1': 10, '2': 4, '3': 2, '4': 1, '5': 1, '6': 1, '7': 0, '8': 1 }, height_missing_n: 0 }, intraday } });
  await page.goto('/research/reviews?view=emotion&trade_day=2026-10-23');
  const table = page.getByRole('region', { name: '情绪周期每日统计' });
  await expect(table.getByRole('columnheader', { name: '封板率', exact: true })).toBeVisible();
  const headers = await table.locator('.dr-column-head th').allTextContents();
  expect(headers.slice(4, 8)).toEqual(['封板率', '昨日涨停表现', '涨跌停比', '连板比值']);
  expect(headers).not.toEqual(expect.arrayContaining(['深证', '创业板', '科创50', '中证2000']));
  const row = table.locator('tbody tr').first();
  await expect(row.locator('td').nth(4)).toHaveText('80.00%');
  await expect(row.locator('td').nth(5)).toHaveText('+2.00%');
  await expect(row.locator('td').nth(6)).toHaveText('1:2');
  await expect(row.locator('td').nth(7).getByRole('button')).toHaveAttribute('title', /首板 50%.*二板 20%/s);
  await expect(table.getByRole('columnheader', { name: '大小盘对比', exact: true })).toHaveCount(0);
  await expect(row.locator('td').nth(headers.indexOf('曾涨停'))).toHaveText('7');
  await expect(row.locator('td').nth(headers.indexOf('曾跌停'))).toHaveText('3');
  await expect(table.getByRole('columnheader', { name: '曾涨停', exact: true })).toHaveAttribute('title', /收盘已打开/);
  const histogram = page.getByRole('img', { name: '沪深股票涨跌效应十一区间分布', exact: true });
  await expect(histogram.locator('[data-distribution-band=down_0_3] rect')).toHaveAttribute('fill', '#00D050');
  await expect(histogram.locator('[data-distribution-band=flat] rect')).toHaveAttribute('fill', '#88939A');
  await expect(histogram.locator('[data-distribution-band=up_0_3] rect')).toHaveAttribute('fill', '#FF3B2F');
  await expect(histogram.locator('[data-distribution-band=limit_up] title')).toContainText('40只（收盘仍封板）');
  await expect(table.getByRole('columnheader', { name: '涨停', exact: true })).toHaveCount(1);
  await expect(table.getByRole('columnheader', { name: '跌停', exact: true })).toHaveCount(1);
  await expect(table.getByRole('columnheader', { name: '曾涨停', exact: true })).toHaveCount(1);
  await page.screenshot({ path: info.outputPath('情绪周期_新指标与图2配色_合成数据.png'), fullPage: true });
  await histogram.screenshot({ path: info.outputPath('涨跌分布明细_图2配色_合成数据.png') });
  expect(f.state.collectionWrites).toBe(0); expect(f.state.errors).toEqual([]);
});

for (const [up, down, ratio] of [[0, 6, '0:1'], [6, 0, '1:0'], [0, 0, '—'], [null, 6, '—'], [12, 8, '3:2']] as const) {
  test(`limit ratio handles zero and missing observations ${up}:${down}`, async ({ page }) => {
    await mock(page, 'admin', {}, { limits: { ...metrics.limits,
      supplier_reported: { ...metrics.limits.supplier_reported, up, down } } });
    await page.goto('/research/reviews?view=emotion&trade_day=2026-10-23');
    const row = page.getByRole('region', { name: '情绪周期每日统计' }).locator('tbody tr').first();
    await expect(row.locator('td').nth(6)).toHaveText(ratio);
    const headers = await page.getByRole('region', { name: '情绪周期每日统计' }).locator('.dr-column-head th').allTextContents();
    await expect(row.locator('td').nth(headers.indexOf('曾涨停'))).toHaveText('—');
  });
}

test('board ratio partitions eight bands including heights above seven and exposes every percentage', async ({ page }, info) => {
  await page.setViewportSize({ width: 1672, height: 1000 });
  const pool = { ...metrics.limits.supplier_reported, up: 80, height_missing_n: 0,
    ladder: { '1': 40, '2': 16, '3': 8, '4': 6, '5': 4, '6': 2, '7': 1, '8': 1, '12': 2 } };
  const f = await mock(page, 'admin', detailedBreadth(), { limits: { ...metrics.limits, supplier_reported: pool } });
  await page.goto('/research/reviews?view=emotion&trade_day=2026-10-23');
  const table = page.getByRole('region', { name: '情绪周期每日统计' });
  const cell = table.locator('tbody tr').first().locator('td').nth(7);
  const button = cell.getByRole('button');
  await expect(button).toHaveAttribute('title', /七板 1.3%（1家）.*七板以上 3.8%（3家）/s);
  const segments = cell.locator('[data-board-band]');
  await expect(segments).toHaveCount(8);
  const widths = await segments.evaluateAll(els => els.map(e => e.getBoundingClientRect().width));
  const total = widths.reduce((a,n) => a+n, 0);
  for (let i=0; i<8; i++) expect(widths[i]/total).toBeCloseTo([40,16,8,6,4,2,1,3][i]/80, 2);
  await expect(cell.locator('.dr-board-ratio-bar')).toHaveCSS('height', '20px');
  await expect(cell).toHaveCSS('min-width', '240px');
  await expect(segments.first()).toHaveCSS('background-color', 'rgb(224, 45, 50)');
  await expect(segments.last()).toHaveCSS('background-color', 'rgb(101, 10, 24)');
  await expect(segments.first()).toHaveCSS('color', 'rgb(255, 255, 255)');
  const colors = await segments.evaluateAll(els => els.map(e => getComputedStyle(e).backgroundColor.match(/\d+/g)!.slice(0, 3).map(Number)));
  const lightness = colors.map(c => .2126 * c[0] + .7152 * c[1] + .0722 * c[2]);
  for (let i = 1; i < lightness.length; i++) expect(lightness[i]).toBeLessThan(lightness[i - 1]);
  await page.screenshot({ path: info.outputPath('连板比值_红色阶与数值分布_示例数据.png'), fullPage: true });
  await button.click();
  const dialog = page.getByRole('dialog', { name: '连板比值 · 2026-10-22', exact: true });
  await expect(dialog).toBeVisible();
  const detailRows = dialog.getByRole('region', { name: '连板比值明细' }).locator('tbody tr');
  await expect(detailRows).toHaveCount(8);
  await expect(detailRows.nth(6)).toHaveText('七板11.3%');
  await expect(detailRows.nth(7)).toHaveText('七板以上33.8%');
  await dialog.screenshot({ path: info.outputPath('连板比值_红色八档百分比_示例数据.png') });
  await dialog.getByRole('button', { name: '关闭弹窗', exact: true }).click();
  expect(f.writes()).toEqual([]); expect(f.state.collectionWrites).toBe(0); expect(f.state.errors).toEqual([]);
});

test('board ratio retains unknown height in the full denominator and omits zero widths', async ({ page }) => {
  await mock(page, 'admin', {}, { limits: { ...metrics.limits, supplier_reported: {
    ...metrics.limits.supplier_reported, up: 10, ladder: { '1': 6, '2': 2 }, height_missing_n: 2 } } });
  await page.goto('/research/reviews?view=emotion&trade_day=2026-10-23');
  const cell=page.getByRole('region', {name:'情绪周期每日统计'}).locator('tbody tr').first().locator('td').nth(7);
  await expect(cell.locator('[data-board-band]')).toHaveCount(3);
  await expect(cell.getByRole('button')).toHaveAttribute('title', /首板 ≥60%.*高度待核 2 家/s);
  await cell.getByRole('button').click();
  const dialog=page.getByRole('dialog', {name:'连板比值 · 2026-10-22',exact:true});
  await expect(dialog.getByRole('row', {name:'高度待核 2 20%',exact:true})).toBeVisible();
});

for (const [label, override, expected] of [
  ['empty', {up:0,ladder:{},height_missing_n:0}, '无涨停'],
  ['missing', {up:null,ladder:null}, '—'],
  ['inconsistent', {up:1,ladder:{'1':2}}, '—'],
] as const) test(`board ratio handles ${label} data without fabricating percentages`, async ({page}) => {
  await mock(page,'admin',{}, {limits:{...metrics.limits,supplier_reported:{...metrics.limits.supplier_reported,...override}}});
  await page.goto('/research/reviews?view=emotion&trade_day=2026-10-23');
  const cell=page.getByRole('region',{name:'情绪周期每日统计'}).locator('tbody tr').first().locator('td').nth(7);
  await expect(cell).toHaveText(expected); await expect(cell.getByRole('button')).toHaveCount(0);
});
