import { openNotifications } from './isolated-browser.fixtures';
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { signIn } from './researchTestHelpers';

const id = '00000000-0000-4000-8000-000000000061';
const meta = { request_id: id, app_mode: 'demo', generated_at: '2020-01-03T10:00:00Z', as_of_time: null,
  quality: { status: 'partial', reasons: ['Synthetic widget fixtures only; not market data'] } };
const knownIds = ['quote', 'rules', 'sectors', 'funds', 'news', 'signals', 'pools', 'research',
  'top-list', 'daily-basic', 'moneyflow', 'market-limits', 'daily', 'index-daily', 'adj-factor', 'income',
  'balancesheet', 'cashflow', 'fina-indicator', 'limit-list', 'block-trade', 'margin-detail', 'dividend',
  'ths-index', 'shibor', 'cpi', 'auction', 'index-basic-sse', 'index-basic-szse', 'stk-limit'];
const widget = (page: Page) => page.locator('[data-widget-id="top-list"]');
const choice = (page: Page) => page.getByRole('checkbox', { name: /^龙虎榜/ });
async function settings(page: Page) {
  const details = page.locator('details').filter({ has: page.getByText('工作台组件', { exact: true }) });
  if (!(await details.evaluate(element => (element as HTMLDetailsElement).open))) await details.locator(':scope > summary').click();
}
async function userKey(page: Page) {
  const response = await page.request.get('/api/v1/session'); expect(response.status()).toBe(200);
  const value = await response.json();
  return `tianyan.workbench.widgets.v1:${value.meta.app_mode}:${value.data.user.id}`;
}
async function syntheticSource(page: Page) {
  const state = { result: 'filled' as 'filled' | 'empty' | 'error', permitted: true, calls: 0,
    families: ['tushare_top_list'] };
  await page.route('**/api/v1/data-sources', route => route.fulfill({ json: { meta, data: {
    items: state.families.map(family => ({ family, implementation: 'implemented', rights_basis: 'Synthetic test permission',
      provider: { id, name: '合成Tushare来源', kind: 'market', adapter_version: 'synthetic', configured: true,
        enabled: state.permitted, entitlement_status: 'verified', capabilities: ['reference.tushare'],
        rights: { display: state.permitted ? 'allowed' : 'denied', cache: 'allowed', export: 'denied', redistribute: 'denied' } },
      collection_state: 'ready', collection_enabled: true, credential_configured: true, paused_reason: null,
      interval_seconds: 900, configured_at: meta.generated_at, next_run_at: meta.generated_at,
      last_success_at: meta.generated_at, last_attempt: null, symbols: [] })),
    today_request_count: 0, today_cost_cny: null, adapters: [],
  } } }));
  await page.route('**/api/v1/trading-datasets?*', route => {
    const query = new URL(route.request().url()).searchParams;
    if (query.get('dataset') !== 'tushare.top_list') return route.fulfill({ json: { meta, data: { items: [], available_days: [], trade_day: null } } });
    state.calls++;
    expect(query.get('family')).toBe('tushare_top_list');
    if (state.result === 'error') return route.fulfill({ status: 503, json: { request_id: id,
      error: { code: 'unavailable', message: '合成龙虎榜缓存读取失败', details: [] } } });
    return route.fulfill({ json: { meta, data: { available_days: ['2020-01-02'], trade_day: '2020-01-02', items: !state.permitted ? [] : [{ id, source_id: id, source_name: '合成Tushare来源',
      trade_day: '2020-01-02', data_mode: 'after_market', family: 'tushare_top_list', source_key: 'reference:tushare.top_list', instrument_id: null,
      received_at: meta.generated_at, published_at: null, content_hash: '6'.repeat(64), payload: {
        dataset: 'tushare.top_list', title: '合成龙虎榜原表', observed_day: '2020-01-02',
        source_url: 'https://example.com/synthetic', attribution: 'Synthetic test only', scope: '合成盘后资料，非真实行情',
        columns: [{ key: 'ts_code', label: '证券代码', unit: null }, { key: 'name', label: '名称', unit: null },
          { key: 'trade_date', label: '交易日期', unit: null }, { key: 'reason', label: '上榜原因', unit: null },
          { key: 'net_amount', label: '净额', unit: '万元' }],
        rows: state.result === 'empty' ? [] : [
          { ts_code: '603278.SH', name: '合成测试股票', trade_date: '20200102', reason: '当日合成上榜原因甲', net_amount: '0' },
          { ts_code: '603278.SH', name: '合成测试股票', trade_date: '20200102', reason: '当日合成上榜原因乙', net_amount: null },
        ],
      } }] } } });
  });
  return state;
}

test('widget controls preserve defaults, hide without requests and restore only display preferences', async ({ page }, info) => {
  const source = await syntheticSource(page);
  await page.goto('/workbench?legacy=1'); await signIn(page);
  await expect(widget(page)).toContainText('当日合成上榜原因甲');
  for (const name of ['板块行情', '连板观察池', '研究记录']) {
    await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
  }
  await expect(page.getByRole('heading', { name: '规则观察', exact:true })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '规则信号', exact:true })).toHaveCount(0);
  const writes: string[] = [];
  page.on('request', request => {
    if (new URL(request.url()).pathname.startsWith('/api/') && request.method() !== 'GET') writes.push(request.url());
  });
  await settings(page); await expect(choice(page)).toBeChecked();
  await expect(page.locator('.workbench-widget-manager input[type="checkbox"]')).toHaveCount(knownIds.length);
  await choice(page).uncheck(); await expect(widget(page)).toHaveCount(0);
  await page.waitForLoadState('networkidle');
  const beforeReload = source.calls;
  await page.reload(); await expect(page.getByRole('heading', { name: '盘中工作台', exact: true })).toBeVisible();
  await page.waitForLoadState('networkidle');
  await expect(widget(page)).toHaveCount(0); expect(source.calls).toBe(beforeReload);
  await settings(page); await expect(choice(page)).not.toBeChecked();
  await choice(page).check(); await expect(widget(page)).toContainText('当日合成上榜原因甲');
  await choice(page).uncheck();
  await page.getByRole('button', { name: '恢复默认组件', exact: true }).click();
  await expect(choice(page)).toBeChecked(); await expect(widget(page)).toContainText('当日合成上榜原因甲');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await widget(page).scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`widgets-${width}.png`), animations: 'disabled' });
  }
  expect(writes).toEqual([]);
});

test('leaderboard retains multiple reasons, source dates, original units and unknown versus zero', async ({ page }) => {
  await syntheticSource(page); await page.goto('/workbench?legacy=1'); await signIn(page);
  const card = widget(page);
  const main = card.getByRole('region', { name: '龙虎榜股票排行', exact: true });
  await expect(main.locator('tbody tr')).toHaveCount(1);
  await expect(main).toContainText('2 项原因');
  await expect(main).toContainText('当日合成上榜原因甲');
  await expect(main).not.toContainText('当日合成上榜原因乙');
  await main.getByRole('button', { name:/当日合成上榜原因甲/ }).click();
  const drawer = page.getByRole('dialog', { name:'龙虎榜买卖席位' });
  await expect(drawer.getByRole('combobox', { name:'上榜原因' }).getByRole('option')).toHaveCount(2);
  await drawer.getByRole('combobox', { name:'上榜原因' }).selectOption('1');
  await expect(drawer.getByText('当日合成上榜原因乙', { exact:true })).toBeVisible();
  await drawer.getByRole('button', { name:'关闭',exact:true }).click();
  await card.getByText('原始数据与口径', { exact:true }).click();
  const table = card.getByRole('region', { name: '合成龙虎榜原表原始数据', exact: true });
  await expect(table.locator('tbody tr')).toHaveCount(2);
  await expect(table).toContainText('当日合成上榜原因甲'); await expect(table).toContainText('当日合成上榜原因乙');
  await expect(table.getByRole('cell', { name: '0', exact: true })).toHaveCount(1);
  await expect(table.getByRole('cell', { name: '—', exact: true })).toHaveCount(1);
  await expect(table.getByRole('columnheader', { name: /净额.*万元/ })).toBeVisible();
  await expect(card).toContainText('2020-01-02');
  await expect(card).toContainText(/接收\s+2020[/-]0?1[/-]0?3/);
  await expect(card).toContainText('盘后资料 · 非实时');
  await expect(table).toBeVisible();
});

test('transient failures retain dated leaderboards while authoritative empty and revoked reads clear them', async ({ page }) => {
  const source = await syntheticSource(page); await page.goto('/workbench?legacy=1'); await signIn(page);
  const card = widget(page), refresh = card.getByRole('button', { name: '刷新', exact: true });
  await expect(card).toContainText('当日合成上榜原因甲');
  source.result = 'empty'; await refresh.click();
  await expect(card).toContainText('所选日期暂无已存资料'); await expect(card).not.toContainText('当日合成上榜原因甲');
  source.result = 'filled'; await refresh.click(); await expect(card).toContainText('当日合成上榜原因甲');
  source.result = 'error'; await refresh.click();
  const notice = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成龙虎榜缓存读取失败' });
  await expect(notice).toBeVisible();
  await expect(card).toContainText('当日合成上榜原因甲');
  source.result = 'filled'; await notice.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(notice).toHaveCount(0); await page.getByRole('button', { name: '收起消息通知', exact: true }).click();
  await expect(card).toContainText('当日合成上榜原因甲');
  source.permitted = false; const calls = source.calls; await refresh.click();
  await expect(card).toContainText('所选日期暂无已存资料');
  await expect(card).not.toContainText('当日合成上榜原因甲'); expect(source.calls).toBeGreaterThan(calls);
});

test('saved layout is isolated by user and stores only versioned allowlisted display IDs after logout', async ({ page }) => {
  await syntheticSource(page); await page.goto('/workbench?legacy=1'); await signIn(page);
  const first = await userKey(page);
  await settings(page); await choice(page).uncheck();
  await page.getByRole('button', { name: '退出', exact: true }).click();
  await expect(page.getByRole('heading', { name: '登录工作空间', exact: true })).toBeVisible();
  await signIn(page, 'e2e-other'); const second = await userKey(page); expect(second).not.toBe(first);
  await settings(page); await expect(choice(page)).toBeChecked();
  await page.getByRole('checkbox', { name: /^资金流向参考/ }).uncheck();
  await page.getByRole('button', { name: '退出', exact: true }).click();
  await expect(page.getByRole('heading', { name: '登录工作空间', exact: true })).toBeVisible();
  const stored = await page.evaluate(() => ({ local: Object.entries(localStorage), sessionCount: sessionStorage.length }));
  expect(stored.sessionCount).toBe(0);
  expect(stored.local.filter(([key]) => key.startsWith('tianyan.workbench.widgets.v1:')).map(([key]) => key).sort()).toEqual([first, second].sort());
  for (const [key, raw] of stored.local) {
    expect(key).toMatch(/^tianyan\.workbench\.(widgets|geometry)\.v1:(real|demo):[0-9a-f-]{36}$/);
    expect([first,second].map(value => value.replace('widgets.v1:',key.includes('.geometry.') ? 'geometry.v1:' : 'widgets.v1:'))).toContain(key);
    if (key.includes('.geometry.')) {
      const geometry = JSON.parse(raw);
      expect(Object.keys(geometry).sort()).toEqual(['tiles','version']);
      expect(geometry.version).toBe(1);
      expect(Array.isArray(geometry.tiles)).toBe(true);
      expect(new Set(geometry.tiles.map((tile:{id:string}) => tile.id)).size).toBe(geometry.tiles.length);
      for (const tile of geometry.tiles) {
        expect(Object.keys(tile).sort()).toEqual(['h','id','w','x','y']);
        expect(knownIds).toContain(tile.id);
        for (const field of ['x','y','w','h']) expect(Number.isInteger(tile[field])).toBe(true);
      }
      continue;
    }
    const pref = JSON.parse(raw); expect(Object.keys(pref).sort()).toEqual(['version', 'visible']);
    expect(pref.version).toBe(1); expect(Array.isArray(pref.visible)).toBe(true);
    expect(new Set(pref.visible).size).toBe(pref.visible.length);
    expect(pref.visible.every((value: unknown) => typeof value === 'string' && knownIds.includes(value))).toBe(true);
  }
  await signIn(page); await settings(page); await expect(choice(page)).not.toBeChecked();
  await expect(page.getByRole('checkbox', { name: /^资金流向参考/ })).toBeChecked();
});

test('corrupt, unknown and duplicate stored IDs fall back to the original defaults', async ({ page }) => {
  await syntheticSource(page); await page.goto('/workbench?legacy=1'); await signIn(page);
  const key = await userKey(page);
  for (const raw of ['{broken', JSON.stringify({ version: 1, visible: ['unregistered-widget'] }),
    JSON.stringify({ version: 1, visible: ['top-list', 'top-list'] }),
    JSON.stringify({ version: 2, visible: [] }), JSON.stringify({ version: 1, visible: [], data: 'synthetic unexpected field' })]) {
    await page.evaluate(({ key, raw }) => localStorage.setItem(key, raw), { key, raw });
    await page.reload(); await settings(page);
    await expect(choice(page)).toBeChecked(); await expect(widget(page)).toContainText('当日合成上榜原因甲');
    await expect(page.getByRole('checkbox', { name: /^行情与个股观察/ })).toBeChecked();
  }
});

test('unavailable browser storage still allows temporary widget choices and a usable reload', async ({ page }) => {
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    for (const name of ['getItem', 'setItem']) Object.defineProperty(Storage.prototype, name, {
      configurable: true, value() { throw new DOMException('Synthetic storage denied', 'SecurityError'); },
    });
  });
  await syntheticSource(page); await page.goto('/workbench?legacy=1'); await signIn(page);
  await expect(widget(page)).toContainText('当日合成上榜原因甲');
  await settings(page); await choice(page).uncheck(); await expect(widget(page)).toHaveCount(0);
  await choice(page).check(); await expect(widget(page)).toContainText('当日合成上榜原因甲');
  await page.reload(); await settings(page); await expect(choice(page)).toBeChecked();
  await expect(widget(page)).toContainText('当日合成上榜原因甲'); expect(errors).toEqual([]);
});

test('selected-stock widgets require an explicit identity and ignore a delayed previous selection', async ({ page }) => {
  const source = await syntheticSource(page); source.families.push('tushare_daily_basic');
  const first = { id: '00000000-0000-4000-8000-000000000071', exchange: 'SSE', symbol: '603278', asset_type: 'stock', name: '合成标的甲' };
  const second = { id: '00000000-0000-4000-8000-000000000072', exchange: 'SZSE', symbol: '000001', asset_type: 'stock', name: '合成标的乙' };
  await page.route('**/api/v1/instruments?*', route => route.fulfill({ json: { meta, data: { items: [first, second], next_cursor: null } } }));
  await page.route('**/api/v1/market-quotes?*', route => {
    const selected = new URL(route.request().url()).searchParams.get('instrument_id');
    const instrument = selected === first.id ? first : second;
    return route.fulfill({ json: { meta, data: { selection: 'instrument', historical: false, query_time: meta.generated_at,
      items: [{ instrument, state: 'empty', freshness_limit_seconds: 120, quote: null, provenance: null }] } } });
  });
  let pending = false; let release = () => {}; const requested: string[] = [];
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/trading-datasets?*', async route => {
    const query = new URL(route.request().url()).searchParams;
    if (query.get('dataset') !== 'tushare.daily_basic') return route.fallback();
    expect(query.get('family')).toBe('tushare_daily_basic');
    const selected = query.get('instrument_id'); expect([first.id, second.id]).toContain(selected);
    requested.push(selected!);
    if (selected === first.id) { pending = true; await held; }
    const code = selected === first.id ? '603278.SH' : '000001.SZ';
    return route.fulfill({ json: { meta, data: { available_days: ['2020-01-02'], trade_day: '2020-01-02', items: [{ id, source_id: id, source_name: '合成估值来源',
      family: 'tushare_daily_basic', source_key: 'reference:tushare.daily_basic', instrument_id: selected,
      received_at: meta.generated_at, published_at: null, content_hash: '7'.repeat(64), payload: {
        dataset: 'tushare.daily_basic', title: '合成估值原表', observed_day: '2020-01-02', attribution: 'Synthetic test only',
        source_url: 'https://example.com/synthetic', scope: '合成身份过滤资料，非真实行情',
        columns: [{ key: 'ts_code', label: '证券代码', unit: null }, { key: 'total_mv', label: '总市值', unit: '万元' }],
        rows: [{ ts_code: code, total_mv: selected === first.id ? '111' : '222' },
          { ts_code: '999999.SH', total_mv: '999' }],
      } }] } } });
  });
  await page.goto('/workbench?legacy=1'); await signIn(page); await settings(page);
  await page.getByRole('checkbox', { name: /^估值与换手率/ }).check();
  const card = page.locator('[data-widget-id="daily-basic"]');
  await expect(card).toContainText('先选择一个标的'); expect(requested).toEqual([]);
  async function choose(name: string) {
    await page.getByRole('textbox', { name: '搜索标的', exact: true }).fill(name);
    await page.getByRole('button', { name: '搜索标的', exact: true }).click();
    await page.getByRole('region', { name: '标的查询结果', exact: true }).getByRole('link').filter({ hasText: name }).click();
  }
  await choose(first.name); await expect.poll(() => pending).toBe(true);
  await choose(second.name);
  await expect(card).toContainText(second.name); await expect(card).toContainText('000001.SZ');
  await expect(card.getByRole('region', { name: '估值与换手率', exact: true }).getByRole('cell', { name: '222万', exact: true })).toBeVisible();
  await expect(card.getByRole('region', { name: '估值与换手率', exact: true })).not.toContainText('999999.SH');
  await Promise.all([page.waitForResponse(response => response.url().includes('dataset=tushare.daily_basic')
    && response.url().includes(first.id)), Promise.resolve().then(() => release())]);
  await expect(card).toContainText(second.name); await expect(card).not.toContainText(first.name);
  await expect(card.getByRole('region', { name: '估值与换手率', exact: true }).getByRole('cell', { name: '111万', exact: true })).toHaveCount(0);
  expect([...new Set(requested)]).toEqual([first.id, second.id]);
});
