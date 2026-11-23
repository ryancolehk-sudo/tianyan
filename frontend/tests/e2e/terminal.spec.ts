import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';
import type { MarketSession } from '../../src/shared/marketSession';
import type { WorkbenchData } from '../../src/features/terminal/types';
import { openNotifications } from './isolated-browser.fixtures';
test.beforeEach(async ({ page }) => { await page.addLocatorHandler(page.getByRole('dialog', { name: '正在准备你的工作空间' }), async () => { await page.keyboard.press('Escape'); }); });
async function signIn(page: Page) { await page.getByLabel('账号', { exact: true }).fill('synthetic'); await page.getByLabel('密码', { exact: true }).fill('synthetic-only'); await page.getByRole('button', { name: '进入工作台' }).click(); }
import { ladderFixture } from './ladderTestFixtures';
import { emptyTerminalFixture, sectorA, sectorB, stockA, stockB, terminalDay, terminalFixture } from './terminalTestFixtures';

const views = [
  { view: 'overview', label: '综合盯盘', headings: ['市场全景', '强度排名', '资金流向', '涨跌停家数走势', '个股涨幅榜', '连板梯队'] },
  { view: 'auction', label: '竞价观察', headings: ['多日封单对照', '竞价封单观察'] },
  { view: 'ladder', label: '连板观察', headings: [] },
  { view: 'funds', label: '资金动向', headings: ['盘中同步排名', '资金与表现对照'] },
] as const;
const syntheticId = '00000000-0000-4000-8000-000000000077';
const meta = { request_id: syntheticId, app_mode: 'real', generated_at: `${terminalDay}T18:00:00+08:00`,
  as_of_time: null, quality: { status: 'partial', reasons: ['合成浏览器测试，不是真实行情'] } };
type ReadMode = 'filled' | 'empty' | 'error' | 'price_only' | 'snapshot';
const thsIndustry = 'THS:industry:881001', thsConcept = 'THS:concept:300001';
const thsIndustryName = '合成同花顺行业甲', thsConceptName = '合成同花顺概念';
const thsCurveName = '当日盘中板块资金净额接收采样，单位亿元';

function snapshotFixture(data: WorkbenchData) {
  const stamp=`${data.trade_day}T10:05:00+08:00`;
  [...data.stocks, ...data.selected_members].forEach(stock => {
    stock.source_time=null; stock.source_snapshot_time=stamp; stock.source_received_at=stamp;
    stock.price_basis='snapshot'; stock.source_name='合成同花顺快照';
    stock.speed_5m=null; stock.seal_amount_cny=null;
  });
  data.indices.forEach(index => {
    index.source_time=null; index.source_snapshot_time=stamp; index.source_received_at=stamp;
    index.price_basis='snapshot'; index.source_name='合成同花顺指数快照';
  });
  data.events=[];data.stock_seals=[];
}

function relayFixture(data: WorkbenchData) {
  data.stocks.forEach((stock, index) => {
    stock.theme = null; stock.auction_tags = ['合成机器人', `${stock.previous_height}板`];
    stock.auction_board = `${stock.previous_height}板`;
    stock.first_observed = `09:3${index + 1}:00`;
    stock.latest_change = `13:4${index}:00 观测涨停价（盘口待核）`;
    stock.source_time = `${data.trade_day}T13:46:00+08:00`;
  });
  data.stocks[0].daily_pool = { trade_day: data.trade_day, state: 'U', source_name: '合成日榜来源',
    received_at: `${data.trade_day}T16:09:00+08:00`, scope: '合成日榜记录；非本机盘中盘口',
    seal_amount_cny: 0, first_limit_time: '09:25:00', last_limit_time: '14:56:00', board_height: 5, open_times: 0 };
  data.stocks.push({ ...data.stocks[1], code: '999993.SZ', name: '合成首板', previous_height: null,
    auction_tags: ['合成机器人', '首板'], auction_board: '首板' });
  data.relay_summary = { history_high: null, history_covered_days: 3, history_expected_days: 10,
    history_start: '2001-12-18', history_end: data.trade_day, history_scope: '合成10日窗口，仅3日已保存',
    high_sealed_count: null, high_seal_total: 2, first_board_codes: ['999993.SZ'],
    daily_ladder_available: true, daily_ladder_source: '合成当日日榜' };
  data.events = [{ code: stockA, time: `${data.trade_day}T13:40:00+08:00`, label: '观测涨停价（盘口待核）', kind: 'observed' }];
  return data;
}

function closingFixture(data: WorkbenchData, history = false) {
  const received = `${data.trade_day}T16:05:00+08:00`;
  [...data.stocks, ...data.selected_members].forEach(stock => {
    stock.source_time = null; stock.source_received_at = received; stock.price_basis = 'daily_close';
    stock.source_name = '合成收盘日线'; stock.speed_5m = null; stock.seal_amount_cny = null;
    stock.status = '收盘 · 涨停价';
  });
  data.indices.forEach(index => { index.price_basis = 'daily_close'; index.source_time = null; index.source_received_at = received; });
  Object.assign(data.breadth, { basis: 'daily_close', trade_day: data.trade_day,
    source_received_at: received, limit_up: 59, limit_down: 28, amount_cny: 2_000_000_000_000 });
  if (history) data.breadth.historical_series = { trade_day: data.trade_day, source_family: 'xgb_breadth_history',
    source_name: '合成选股宝历史', scope: '合成测试来源，供应商统计范围未独立核实', received_at: received,
    points: ['09:25', '09:30', '11:30', '13:00', '14:59', '15:00'].map((clock, index) => ({
      time: `${data.trade_day}T${clock}:00+08:00`, up: index === 5 ? 59 : 10 + index, down: index === 5 ? 25 : index,
    })) };
}

async function installFixture(page: Page) {
  let signedIn = false;
  const state = { mode: 'filled' as ReadMode, live: false, mixedFunds: false, extendedFunds: false, focusedFunds: false, focusReady: true, newerRankedFunds: false, staleQuotes: false, closedGap: false, nearClose: false, lunchBridge: false, relay: false, closing: false, breadthHistory: false, market: undefined as MarketSession | undefined, calls: 0, queries: [] as URLSearchParams[],
    denseFunds: false, closingFunds: false, splitClosingFunds: false, wholeMarketGap: false,
    thsQueries: [] as URLSearchParams[], thsReceiptClock: null as string | null, marketReads: 0,
    writes: [] as string[], outsideOrigins: [] as string[], credentialURLs: 0, errors: [] as string[] };
  // Current funds UI reads THS independently. These self-authored samples do
  // not translate the EM workbench payload below. THS supports native category,
  // amount/zero/missing, receipt history and paging coverage. It does not supply
  // native trade clocks, member mappings, exact 5-minute deltas, focused EM
  // details or EM daily fallback; the UI must keep those explicitly unavailable.
  function thsSnapshot(query: URLSearchParams) {
    const concept = query.get('category') === 'concept';
    const day = query.get('sampled_day') || (state.live ? state.market?.trade_day : '') || terminalDay;
    const clock = state.thsReceiptClock ?? (state.newerRankedFunds ? '10:06:07' : state.wholeMarketGap ? concept ? '15:06:30' : '15:04:29'
      : state.splitClosingFunds ? concept ? '15:00:40' : '14:59:20'
      : state.closingFunds ? '15:00:20' : state.nearClose ? '14:59:20' : state.closedGap ? '13:46:02' : '10:05:02');
    let receipt = `${day}T${clock}+08:00`;
    const count = concept ? 1 : state.wholeMarketGap ? 100 : state.extendedFunds ? 12 : state.denseFunds ? 1 : 3;
    const rows = Array.from({ length: state.mode === 'empty' ? 0 : count }, (_, index) => ({
      code: String((concept ? 300001 : 881001) + index),
      name: concept ? thsConceptName : state.extendedFunds ? `合成同花顺板块${index + 1}`
        : index === 0 ? thsIndustryName : index === 1 ? '合成同花顺零值' : '合成同花顺缺测',
      net_cny: !concept && index === 2 && !state.extendedFunds && !state.wholeMarketGap ? null
        : String(concept ? 200_000_000 : state.extendedFunds ? (20 - index) * 100_000_000
          : index === 0 ? state.newerRankedFunds ? 440_000_000 : state.live ? 100_000_000 : 0 : 0),
      pct_change: index === 2 ? null : '0', company_count: '12', received_at: receipt,
    }));
    let clocks = state.lunchBridge ? ['11:29:00', '11:30:00', '13:00:00', '13:01:00']
      : state.wholeMarketGap ? ['14:53:17', '14:58:17']
      : state.extendedFunds ? ['11:30:00', '12:10:00']
      : state.closedGap ? ['13:30:02', '13:46:02'] : [clock];
    if (state.denseFunds) clocks = Array.from({ length: 60 }, (_, i) => `10:${String(i).padStart(2, '0')}:00`);
    if (state.denseFunds || state.lunchBridge || state.extendedFunds) {
      receipt = `${day}T${clocks.at(-1)}+08:00`;
      rows.forEach(row => { row.received_at = receipt; });
    }
    let history = clocks.map((time, index) => ({ received_at: `${day}T${time}+08:00`,
      points: rows.map(row => ({ code: row.code, net_cny: row.net_cny == null ? null
        : state.denseFunds ? String(index * 1_000_000) : row.net_cny, received_at: `${day}T${time}+08:00` })) }));
    if (state.splitClosingFunds && state.extendedFunds) history = history.map(batch => ({ ...batch,
      points: batch.points.map(point => point.code === '881012' ? { ...point, received_at: `${day}T10:05:03+08:00` } : point) }));
    const cutoff = query.get('as_of_time');
    if (cutoff) {
      history = history.filter(batch => Date.parse(batch.received_at) <= Date.parse(cutoff));
      for (let i = rows.length - 1; i >= 0; i--) if (Date.parse(rows[i].received_at) > Date.parse(cutoff)) rows.splice(i, 1);
    }
    const missingPages = state.wholeMarketGap || state.splitClosingFunds && !concept ? [concept ? 8 : 7] : [];
    return { sampled_day: day, status: rows.length ? 'available' : 'missing', received_at: rows.length ? receipt : null,
      complete: missingPages.length === 0, missing_pages: missingPages, rejected_rows: 0, last_error: null, rows, history };
  }
  page.on('pageerror', error => state.errors.push(error.message));
  const origin = new URL(String(test.info().project.use.baseURL)).origin;
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.origin !== origin) state.outsideOrigins.push(url.origin);
    if ([...url.searchParams.keys()].some(key => /token|password|api[_-]?key|authorization/i.test(key))) state.credentialURLs++;
    if (url.pathname.startsWith('/api/') && request.method() !== 'GET'
      && !(url.pathname === '/api/v1/session' && request.method() === 'POST')) state.writes.push(`${request.method()} ${url.pathname}`);
  });
  await page.route('**/*', async route => {
    const request = route.request(); const url = new URL(request.url());
    if (url.origin !== origin) return route.abort('blockedbyclient');
    if (url.pathname.startsWith('/api/') && request.method() !== 'GET'
      && !(url.pathname === '/api/v1/session' && request.method() === 'POST')) {
      return route.abort('blockedbyclient');
    }
    return route.fallback();
  });
  await page.route('**/api/v1/**', route => {
    const url = new URL(route.request().url());
    const ok = (data: unknown) => route.fulfill({ json: { meta, data } });
    if (url.pathname === '/api/v1/session') {
      if (route.request().method() === 'POST') signedIn = true;
      return signedIn ? ok({ user: { id: 'terminal-fixture', username: 'synthetic', role: 'admin' }, csrf_token: 'synthetic-only' }) : route.fulfill({ status: 401, json: { error: { message: '请登录' } } });
    }
    if (url.pathname === '/api/v1/market-session' && state.market) { state.marketReads++; return ok(state.market); }
    if (url.pathname === '/api/v1/ths-funds') { state.thsQueries.push(url.searchParams); return ok(thsSnapshot(url.searchParams)); }
    if (url.pathname === '/api/v1/sector-membership') {
      const base = state.mode === 'empty' ? emptyTerminalFixture(url.searchParams) : terminalFixture(url.searchParams);
      return ok({ trade_day: base.trade_day, sector_code: url.searchParams.get('sector'), state: 'available',
        members_trade_day: base.members_trade_day, member_count: base.selected_members.length, truncated: false,
        members: base.selected_members.map(({ code, name }) => ({ code, name })) });
    }
    if (url.pathname === '/api/v1/workbench-details') {
      const base = terminalFixture(url.searchParams); if (state.relay) relayFixture(base);
      if (state.closing) closingFixture(base, state.breadthHistory);
      if (state.mode==='snapshot') snapshotFixture(base);
      const codes = (url.searchParams.get('codes') || '').split(',');
      return ok({ trade_day: base.trade_day, revision: 'terminal-r1', stocks: base.stocks.filter(item => codes.includes(item.code)), auction_days: base.auction_days, events: base.events, seals: {}, sector_code: url.searchParams.get('sector'), members: base.selected_members, members_trade_day: base.members_trade_day, funds_history: [] });
    }
    if (url.pathname === '/api/v1/ladder-observation') return ok(ladderFixture(state.mode === 'empty'));
    if (url.pathname === '/api/v1/ai/settings') return ok({ enabled: false, features: [] });
    return ok({ items: [], next_cursor: null });
  });
  await page.route('**/api/v1/workbench-view?*', route => {
    if (route.request().method() !== 'GET') return route.abort('blockedbyclient');
    state.calls++;
    const query = new URL(route.request().url()).searchParams;
    state.queries.push(query);
    if (state.mode === 'error') return route.fulfill({ status: 503, json: {
      request_id: syntheticId, error: { code: 'synthetic_unavailable', message: '合成测试：工作台缓存读取失败', details: [] },
    } });
    const effective = new URLSearchParams(query);
    if (state.live && !effective.has('day') && state.market?.trade_day) effective.set('day', state.market.trade_day);
    const data = state.mode === 'empty' ? emptyTerminalFixture(effective) : { ...terminalFixture(effective), detail_revision: 'terminal-r1', detail_codes: [stockA, stockB] };
    if (state.relay) relayFixture(data);
    if (state.live && state.market) {
      data.market_session = state.market;
      data.mode = query.has('as_of_time') ? 'observed' : state.market.automatic_live && (!query.get('day') || query.get('day') === state.market.trade_day) ? 'live' : 'review';
      data.intraday_funds = { state: 'available', trade_day: data.trade_day, source_family: 'synthetic-funds', source_name: '合成盘中来源',
        latest_source_time: `${data.trade_day}T10:05:00+08:00`, received_at: `${data.trade_day}T10:05:02+08:00`, scope: '合成浏览器测试数据，不是真实行情',
        sectors: data.sectors.map((item, index) => ({ ...item, main_net_cny: index ? 0 : 100_000_000, change_5m_cny: index ? null : 0, rank_change_5m: index ? null : 0 })),
        points: ['10:00', '10:02:30', '10:05', '13:00'].map((time, index) => ({ time: `${data.trade_day}T${time.length === 5 ? time + ':00' : time}+08:00`, sector_code: sectorA, value: index * 100_000_000 })),
        stocks: [{ code: stockA, value: 0, change_5m_cny: null, source_time: `${data.trade_day}T10:05:00+08:00` }] };
      if (state.wholeMarketGap) {
        const rank=`${data.trade_day}T15:04:27+08:00`, curve=`${data.trade_day}T14:58:15+08:00`;
        data.latest_at=`${data.trade_day}T21:40:56+08:00`;
        data.intraday_funds.state='stale'; data.intraday_funds.latest_source_time=rank;
        data.intraday_funds.sectors=(['industry','concept'] as const).flatMap((category,index)=>Array.from({length:100},(_,i)=>({
          ...data.sectors[index],code:i?`SYN-COVERAGE-${category}-${i}`:data.sectors[index].code,
          category,industry_level:category==='industry'?2:null,source_time:rank,
        })));
        data.intraday_funds.points=data.intraday_funds.sectors.map(row=>({time:curve,sector_code:row.code,value:row.main_net_cny}));
        data.intraday_funds.coverage=[
          {category:'stock',represented_count:100,expected_count:5927,latest_batch_count:100,complete:false,source_time:rank,received_at:rank,latest_batch_source_time:rank,latest_batch_received_at:rank},
          {category:'industry',represented_count:100,expected_count:128,latest_batch_count:100,complete:false,source_time:rank,received_at:rank,latest_batch_source_time:rank,latest_batch_received_at:rank},
          {category:'concept',represented_count:100,expected_count:531,latest_batch_count:100,complete:false,source_time:rank,received_at:rank,latest_batch_source_time:rank,latest_batch_received_at:rank},
        ];
      }
      if (state.lunchBridge) {
        const clocks = ['11:28', '11:30', '13:00', '13:02'];
        data.breadth.points = clocks.map((clock, index) => ({ time: `${data.trade_day}T${clock}:00+08:00`, up: 30 + index, down: 10 + index }));
        data.intraday_funds.points = clocks.map((clock, index) => ({ time: `${data.trade_day}T${clock}:00+08:00`, sector_code: sectorA, value: index * 100_000_000 }));
      }
      if (state.extendedFunds) {
        data.intraday_funds.sectors = Array.from({ length: 12 }, (_, i) => ({ ...data.sectors[0], code: `SYNTHETIC-FLOW-${i}`, name: `合成板块${i}`, main_net_cny: (12 - i) * 100_000_000, change_5m_cny: null, rank_change_5m: null }));
        data.intraday_funds.points = data.intraday_funds.sectors.map(item => ({ time: `${data.trade_day}T12:10:00+08:00`, sector_code: item.code, value: item.main_net_cny }));
        data.intraday_funds.latest_source_time = `${data.trade_day}T12:10:00+08:00`;
      }
      if (state.focusedFunds) {
        const stamp = `${data.trade_day}T10:05:00+08:00`;
        const outside = { ...data.sectors[0], code: 'BK0999.DC', name: '合成榜外二级', main_net_cny: 99e9 };
        data.sectors.push(outside);
        data.intraday_funds.focused_sectors = state.focusReady ? [{ ...outside, main_net_cny: -2e8,
          pct_change: null, amount_cny: null, rank: null, source_time: stamp }] : [];
        data.intraday_funds.target_states = [{ code: outside.code, category: 'industry', state: state.focusReady ? 'available' : 'recovering',
          source_time: state.focusReady ? stamp : null, received_at: state.focusReady ? stamp : null, next_attempt_at: null }];
        if (state.focusReady) data.intraday_funds.points.push({ time: stamp, sector_code: outside.code, value: -2e8 });
        if (state.newerRankedFunds) {
          const newer = `${data.trade_day}T10:06:00+08:00`;
          data.intraday_funds.sectors.push({ ...outside, main_net_cny: 3.3e8, source_time: newer, rank: 3 });
          data.intraday_funds.points.push({ time: newer, sector_code: outside.code, value: 3.3e8 });
        }
      }
      if (state.staleQuotes) {
        data.mode = 'review'; data.stocks[0].source_time = `${data.trade_day}T09:31:00+08:00`;
        data.indices[0].source_time = `${data.trade_day}T10:05:00+08:00`;
        data.capabilities.push({ key: 'quotes', label: '合成报价状态', status: 'stale', reason: '合成旧报价', source_family: 'synthetic', last_received_at: data.latest_at });
      }
      if (state.closedGap || state.nearClose) {
        const stamp = `${data.trade_day}T${state.nearClose ? '14:59:00' : '13:46:00'}+08:00`;
        data.latest_at = `${data.trade_day}T15:26:00+08:00`;
        data.funds_trade_day = '2026-10-23';
        data.stocks.forEach(stock => { stock.source_time = stamp; });
        data.indices.forEach(index => { index.source_time = stamp; });
        data.intraday_funds.latest_source_time = stamp;
        data.intraday_funds.received_at = data.latest_at;
        data.intraday_funds.state = state.nearClose ? 'available' : 'stale';
        data.intraday_funds.sectors.forEach(row => { row.source_time = stamp; });
        const clocks = ['13:00', '13:02', ...(state.nearClose ? ['14:57', '14:59'] : ['13:44', '13:46'])];
        data.intraday_funds.points = data.intraday_funds.sectors.flatMap(row => clocks.map((clock, index) => ({
          time: `${data.trade_day}T${clock}:00+08:00`, sector_code: row.code, value: index * 100_000_000 })));
        data.breadth.points = ['13:00', '13:02', '13:44', '13:46'].map((clock, index) => ({ time: `${data.trade_day}T${clock}:00+08:00`, up: index, down: index }));
      }
    }
    if (data.intraday_funds && state.denseFunds) {
      data.intraday_funds.points = Array.from({ length: 60 }, (_, i) => ({
        time: new Date(Date.parse(`${data.trade_day}T09:30:00+08:00`) + i * 120_000).toISOString(),
        sector_code: sectorA, value: (i + 1) * 10_000_000,
      }));
      data.intraday_funds.latest_source_time = data.intraday_funds.points.at(-1)!.time;
    }
    if (data.intraday_funds && state.closingFunds) {
      const stamp = `${data.trade_day}T15:00:00+08:00`;
      data.intraday_funds.latest_source_time = stamp;
      data.intraday_funds.received_at = `${data.trade_day}T15:02:00+08:00`;
      data.intraday_funds.state = 'available';
      data.intraday_funds.sectors.forEach(row => {
        row.source_time = stamp;
        data.intraday_funds!.points.push({ time: stamp, sector_code: row.code, value: 6e8 });
      });
    }
    if (data.intraday_funds && state.splitClosingFunds) {
      const industryTime = `${data.trade_day}T14:59:00+08:00`, conceptTime = `${data.trade_day}T15:00:00+08:00`;
      data.funds_trade_day = '2026-10-23';
      data.intraday_funds.state = 'available';
      data.intraday_funds.latest_source_time = conceptTime;
      data.intraday_funds.received_at = `${data.trade_day}T15:02:00+08:00`;
      data.intraday_funds.sectors = [
        { ...data.sectors[0], category: 'industry', industry_level: 2, source_time: industryTime, main_net_cny: 1e8 },
        { ...data.sectors[1], category: 'concept', industry_level: null, source_time: conceptTime, main_net_cny: 2e8 },
      ];
      data.intraday_funds.points = [
        { time: industryTime, sector_code: sectorA, value: 1e8 },
        { time: conceptTime, sector_code: sectorB, value: 2e8 },
        ...(data.intraday_funds.focused_sectors ?? []).map(row => ({ time: row.source_time!, sector_code: row.code, value: row.main_net_cny })),
      ];
    }
    if (state.mixedFunds) {
      const extras = [
        { ...data.sectors[0], code: 'SYN-L1', name: '合成一级行业', industry_level: 1, main_net_cny: 9e9 },
        { ...data.sectors[0], code: 'SYN-L3', name: '合成三级行业', industry_level: 3, main_net_cny: 8e9 },
        { ...data.sectors[0], code: 'SYN-UNKNOWN', name: '未核层级II', industry_level: null, main_net_cny: 7e9 },
        { ...data.sectors[0], code: 'SYN-CONCEPT', name: '合成独立概念', category: 'concept' as const, industry_level: null, main_net_cny: 6e9 },
      ];
      data.sectors.push(...extras);
      if (data.intraday_funds) data.intraday_funds.sectors.push(...extras);
    }
    if (state.closing) closingFixture(data, state.breadthHistory);
    if (state.mode === 'price_only') {
      data.events = [{ time: `${terminalDay}T10:00:00+08:00`, code: stockA, label: '观测涨停价（盘口待核）', kind: 'observation' }];
      data.stock_seals = [{ time: `${terminalDay}T10:00:00+08:00`, value: null }];
    }
    if (state.mode==='snapshot') snapshotFixture(data);
    return route.fulfill({ json: { meta, data } });
  });
  await page.route('**/api/v1/watchlist', route => {
    if (route.request().method() !== 'GET') return route.abort('blockedbyclient');
    return route.fulfill({ json: { meta, data: { items: [] } } });
  });
  return state;
}

function panel(page: Page, heading: string | RegExp) {
  return page.locator('.ty-panel').filter({ has: page.getByRole('heading', { name: heading, exact: typeof heading === 'string' }) });
}
async function openTerminal(page: Page, path = `/workbench?view=overview&trade_day=${terminalDay}`) {
  await page.goto(path); await signIn(page);
  await expect(page.getByRole('heading', { name: '工作台', exact: true })).toBeVisible();
  if ((new URL(page.url()).searchParams.get('view') ?? 'overview') === 'overview') {
    await expect(page.getByRole('navigation', { name: '综合盯盘工作区', exact: true })).toBeVisible();
  } else if(new URL(page.url()).searchParams.get('view')==='auction') await expect(page.getByRole('complementary',{name:'竞价详情与封单观察',exact:true})).toHaveCount(1);
  else if(new URL(page.url()).searchParams.get('view')==='ladder') await expect(page.getByRole('tablist',{name:'连板视图'})).toBeVisible();
  else await expect(page.getByRole('complementary', { name: 'AI 观察', exact: true })).toHaveCount(1);
  await page.waitForLoadState('networkidle');
  if (new URL(page.url()).searchParams.get('view') === 'funds') await showFundsChart(page);
}
async function showFundsChart(page: Page) {
  await page.getByRole('group', { name: '板块图表指标', exact: true }).getByRole('button', { name: '资金净额', exact: true }).click();
  await expect(page.getByRole('heading', { name: '板块资金脉络', exact: true })).toBeVisible();
}
async function switchView(page: Page, view: typeof views[number]) {
  await page.getByRole('navigation', { name: '工作台页面', exact: true }).getByRole('link', { name: view.label, exact: true }).click();
  await expect(page).toHaveTitle(`${view.label} · 天衍系统`);
  for (const name of view.headings) await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
  await page.waitForLoadState('networkidle');
  if (view.view === 'funds') await showFundsChart(page);
}
function assertSafeReads(state: Awaited<ReturnType<typeof installFixture>>) {
  expect(state.calls).toBeGreaterThan(0);
  expect(state.writes, 'terminal must not mutate old sources, research, layouts or watchlists').toEqual([]);
  expect(state.outsideOrigins, 'browser must read the server cache rather than a market vendor').toEqual([]);
  expect(state.credentialURLs, 'credentials must not appear in browser request URLs').toBe(0);
  expect(state.errors).toEqual([]);
}

function marketFixture(day: string, phase: MarketSession['phase'], automatic_live: boolean): MarketSession {
  return { trade_day: day, phase, automatic_live, is_trading_day: true, server_time: day + 'T10:05:00+08:00', next_transition_at: null, poll_seconds: 15 };
}

test('current price snapshots show their own clock without claiming native trade time or a healthy feed', async ({ page }) => {
  const state=await installFixture(page);state.mode='snapshot';
  await openTerminal(page);
  const stamp=page.locator('.ty-ribbon-stamp');
  await expect(stamp).toContainText('价格快照 · 成交时刻待核');
  await expect(stamp).toContainText('快照 10:05');
  await expect(stamp.locator('.ty-status-dot')).not.toHaveClass(/is-ready/);
  const stock=page.locator('.ty-stock-name').first();
  await expect(stock).toHaveAttribute('title',/服务器快照 .*逐股成交时刻待核/);
  const index=page.locator('.ty-index').first();
  await expect(index).toHaveAttribute('title',/合成同花顺指数快照.*服务器快照/s);
  assertSafeReads(state);
});

test('THS funds disclose missing pages and separate rank receipt, curve receipt and unrelated EM clocks', async ({ page }, info) => {
  const state=await installFixture(page);state.live=true;state.wholeMarketGap=true;
  state.market={...marketFixture('2026-10-26','closed',false),server_time:'2026-10-26T22:00:00+08:00'};
  await openTerminal(page,'/workbench?view=funds');
  await page.getByRole('button',{name:'当日 · 已存采样',exact:true}).click();
  await expect(page.locator('.ty-data-timestamp')).toHaveText('资金按所选来源独立展示 · 覆盖范围及实际时点见下方');
  await expect(page.locator('.ty-market-ribbon')).toContainText('未齐');
  await expect(page.getByRole('status').filter({hasText:'采样已存覆盖'})).toContainText('行业 100');
  await expect(page.getByRole('status').filter({hasText:'缺第 7 页'})).toBeVisible();
  const chart=panel(page,'板块资金脉络');
  await expect(chart).toContainText('本类最近接收 2026-10-26 15:04:29');
  await expect(chart.locator('g[data-selected=true] .ty-fund-sample title').last()).toContainText('接收时点 14:58');
  await expect(page.locator('.ty-terminal')).not.toContainText('21:40:56');
  await expect(chart).not.toContainText('排行来源截至');
  await expect(chart).not.toContainText('收盘数据不完整');
  await page.getByRole('button',{name:'概念板块',exact:true}).click();
  await expect(page.getByRole('status').filter({hasText:'缺第 8 页'})).toBeVisible();
  await expect(chart).toContainText('本类最近接收 2026-10-26 15:06:30');
  for(const width of [1440,1280]){
    await page.setViewportSize({width,height:1050});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  }
  await page.screenshot({path:info.outputPath('funds-actual-whole-market-gap.png'),fullPage:true});
  assertSafeReads(state);
});

test('closed-session views show actual coverage instead of a newer unrelated receipt', async ({ page }, info) => {
  const state = await installFixture(page); state.live = true; state.closedGap = true;
  state.market = { ...marketFixture('2026-10-26', 'closed', false), server_time: '2026-10-26T16:00:00+08:00' };
  await openTerminal(page, '/workbench');
  await expect(page.locator('.ty-ribbon-stamp')).toContainText('缺少收盘样本');
  await expect(page.locator('.ty-ribbon-stamp')).toContainText('行情截至 13:46');
  await expect(page.locator('.ty-ribbon-stamp')).not.toContainText('15:26');
  await expect(panel(page, '涨跌停家数走势').locator('.ty-panel-action')).toHaveText('采样 13:46');
  await expect(panel(page, '资金流向').locator('.ty-overview-source')).toContainText('接收 13:46');
  const breadthPath = await panel(page, '涨跌停家数走势').locator('g.ty-chart-up > path[stroke="currentColor"]').getAttribute('d');
  expect(breadthPath?.match(/M/g)).toHaveLength(2);
  await page.getByRole('navigation', { name: '工作台页面', exact: true }).getByRole('link', { name: '资金动向', exact: true }).click();
  await showFundsChart(page);
  await expect(panel(page, '当日采样排名')).toBeVisible();
  await expect(panel(page, '板块资金脉络')).toContainText('本类最近接收 2026-10-26 13:46:02');
  await expect(panel(page, '板块资金脉络')).toContainText('已存接收采样');
  await expect(panel(page, '板块资金脉络')).not.toContainText('收盘数据不完整');
  await expect(panel(page, '板块资金脉络')).not.toContainText('自动重试');
  const flowPath = await panel(page, '板块资金脉络').locator('svg > g[opacity="1"] > path').getAttribute('d');
  expect(flowPath?.match(/M/g)).toHaveLength(2);
  await page.screenshot({ path: info.outputPath('closed-funds-actual-coverage.png'), fullPage: true });
  assertSafeReads(state);
});

test('a fresh final pre-close quote remains an honest saved sample without demanding post-close polling', async ({ page }) => {
  const state = await installFixture(page); state.live = true; state.nearClose = true;
  state.market = { ...marketFixture('2026-10-26', 'closed', false), server_time: '2026-10-26T16:00:00+08:00' };
  await openTerminal(page, '/workbench');
  await expect(page.locator('.ty-ribbon-stamp')).toContainText('行情截至 14:59');
  await expect(page.locator('.ty-ribbon-stamp')).not.toContainText('缺少收盘样本');
  assertSafeReads(state);
});

test('market breadth bridges lunch while THS receipts retain their real long gap without inserting samples', async ({ page }) => {
  const state = await installFixture(page); state.live = true; state.lunchBridge = true;
  state.market = marketFixture(terminalDay, 'trading', true);
  await openTerminal(page);
  const curve = panel(page, '涨跌停家数走势');
  await expect(curve).toBeVisible();
  const breadth = await curve.locator('g.ty-chart-up > path[stroke="currentColor"]').getAttribute('d');
  expect(breadth?.match(/M/g)).toHaveLength(1);
  expect(breadth?.match(/L/g)).toHaveLength(3);
  await page.getByRole('navigation', { name: '工作台页面', exact: true }).getByRole('link', { name: '资金动向', exact: true }).click();
  await showFundsChart(page);
  await expect(panel(page, '盘中同步排名')).toBeVisible();
  const funds = page.getByRole('img', { name: thsCurveName, exact: true });
  expect((await funds.locator(':scope > g[opacity="1"] > path').getAttribute('d'))?.match(/M/g)).toHaveLength(2);
  await expect(funds.locator('g[data-selected=true] .ty-fund-sample')).toHaveCount(4);
  assertSafeReads(state);
});

test('THS keeps receipt sampling through opening and lunch without inventing five-minute deltas', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-26T01:29:50Z') });
  const state = await installFixture(page); state.live = true;
  state.thsReceiptClock = '09:29:40';
  state.market = { ...marketFixture('2026-10-26', 'preopen', false), server_time: '2026-10-26T09:29:50+08:00' };
  await openTerminal(page, '/workbench?view=funds');
  await expect(panel(page, '盘中同步排名')).toBeVisible();
  state.thsReceiptClock = '09:30:01';
  state.market = { ...marketFixture('2026-10-26', 'trading', true), server_time: '2026-10-26T09:30:05+08:00' };
  await page.clock.runFor(15_500);
  await expect(panel(page, '盘中同步排名')).toBeVisible();
  await expect(page.locator('.ty-clock')).toContainText('交易中');
  // The automatic-live context changed at the open, so select the actual
  // funds indicator again instead of querying the now-unmounted chart.
  await showFundsChart(page);
  const row = panel(page, '盘中同步排名').getByRole('row').filter({ has: page.getByRole('button', { name: thsIndustryName, exact: true }) });
  await expect(row.getByRole('cell').nth(2)).toHaveText('+1.0');
  await expect(row.getByRole('cell').nth(3)).toHaveText('—');
  await expect(row.getByRole('cell').nth(4)).toHaveText('—');
  const curve = page.getByRole('img', { name: thsCurveName, exact: true });
  await expect(curve.locator('g[data-selected=true] .ty-fund-sample')).toHaveCount(1);
  await expect(page.locator('.ty-funds-chart-panel')).toContainText('本类最近接收 2026-10-26 09:30:01');
  state.thsReceiptClock = '11:30:00';
  state.market = { ...marketFixture('2026-10-26', 'break', true), server_time: '2026-10-26T12:00:05+08:00' };
  await page.clock.fastForward(150 * 60_000);
  await expect(page.locator('.ty-clock')).toContainText('午间休市');
  await expect(panel(page, '盘中同步排名')).toBeVisible();
  await expect(page.locator('.ty-funds-chart-panel')).toContainText('本类最近接收 2026-10-26 11:30:00');
  expect(state.queries.every(query => !query.has('day'))).toBe(true);
  assertSafeReads(state);
});

test('automatic mode survives linked navigation and advances on the next observed market day', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-26T02:05:00Z') });
  const state = await installFixture(page); state.live = true;
  state.market = marketFixture('2026-10-26', 'trading', true);
  await openTerminal(page, '/workbench?view=overview');
  await page.getByRole('link', { name: '完整梯队', exact: true }).click();
  expect(new URL(page.url()).searchParams.has('trade_day')).toBe(false);
  state.market = marketFixture('2026-10-27', 'trading', true);
  const before = state.marketReads;
  await page.clock.runFor(15_500);
  await expect.poll(() => state.marketReads).toBeGreaterThan(before);
  await expect(page.locator('.ty-clock')).toContainText('2026-10-27');
  await page.getByRole('navigation', { name: '工作台页面', exact: true }).getByRole('link', { name: '资金动向', exact: true }).click();
  await expect(panel(page, '盘中同步排名')).toContainText('2026-10-27');
  const link = page.locator('.ty-members-footer a').filter({ hasText: '与连板观察联动' });
  await expect(link).toHaveAttribute('aria-disabled', 'true');
  await expect(link).not.toHaveAttribute('href');
  expect(new URL(page.url()).searchParams.has('trade_day')).toBe(false);
  assertSafeReads(state);
});

test('explicit historical dates and receipt cutoffs remain pinned while the live session advances', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-26T02:05:00Z') });
  const state = await installFixture(page); state.live = true;
  state.market = marketFixture('2026-10-26', 'trading', true);
  const cutoff = terminalDay + 'T10:00:00+08:00';
  await openTerminal(page, '/workbench?' + new URLSearchParams({ view: 'funds', trade_day: terminalDay, as_of_time: cutoff }));
  await expect(panel(page, '盘中同步排名')).toBeVisible();
  await expect(page.locator('.ty-funds-ranking tbody tr')).toHaveCount(0);
  state.market = marketFixture('2026-10-27', 'trading', true);
  await page.clock.runFor(15_500);
  await expect(panel(page, '盘中同步排名')).toContainText(terminalDay);
  const query = new URL(page.url()).searchParams;
  expect(query.get('trade_day')).toBe(terminalDay); expect(query.get('as_of_time')).toBe(cutoff);
  expect(state.queries.every(query => query.get('day') === terminalDay && query.get('as_of_time') === cutoff)).toBe(true);
  expect(state.thsQueries.filter(query => query.has('as_of_time')).every(query => query.get('sampled_day') === terminalDay && query.get('as_of_time') === cutoff)).toBe(true);
  assertSafeReads(state);
});

test('an initial failed workbench read recovers automatically without focus or a retry click', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-26T02:05:00Z') });
  const state = await installFixture(page); state.mode = 'error';
  await openTerminal(page, '/workbench?view=overview');
  const failure = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成测试：工作台缓存读取失败' }).filter({ hasText: '综合盯盘' });
  await expect(failure).toBeVisible();
  const before = state.calls; state.mode = 'filled';
  await page.clock.runFor(35_500);
  await expect.poll(() => state.calls).toBeGreaterThan(before);
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲');
  await expect(failure).toHaveCount(0);
  assertSafeReads(state);
});

test('cold preparation queues all four views and resumes within the one-second retry budget', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-26T02:05:00Z') });
  const state = await installFixture(page);
  let warming = true;
  const queued = new Set<string>();
  await page.route('**/api/v1/workbench-view?*', route => {
    queued.add(new URL(route.request().url()).searchParams.get('view') ?? 'overview');
    if (!warming) return route.fallback();
    return route.fulfill({ status: 503, json: { error: { code: 'read_cache_warming', message: '资料正在后台准备' } } });
  });
  await openTerminal(page, '/workbench?view=overview');
  await page.clock.runFor(1_500);
  await expect.poll(() => queued.size).toBe(4);
  await expect(page.locator('.ty-loading-strip')).toBeVisible();
  await expect(page.locator('.ty-connection-error')).toHaveCount(0);
  warming = false;
  await page.clock.runFor(2_500);
  await expect(page.locator('.ty-rank-panel').first()).toContainText('合成甲');
  assertSafeReads(state);
});

test('a real THS noon receipt stays visible and a selected sector outside the Top10 keeps its curve', async ({ page }) => {
  const state = await installFixture(page); state.live = true; state.extendedFunds = true;
  state.market = marketFixture('2026-10-26', 'break', true);
  await openTerminal(page, '/workbench?view=funds&fund_sector=THS%3Aindustry%3A881012');
  await expect(panel(page, '盘中同步排名')).toBeVisible();
  const curve = page.getByRole('img', { name: thsCurveName, exact: true });
  await expect(curve.locator(':scope > g[opacity]')).toHaveCount(11);
  await expect(curve.locator(':scope > g[opacity="1"] > path')).toHaveCount(1);
  await expect(curve.locator(':scope > g[opacity="1"] .ty-fund-sample title').last()).toContainText('合成同花顺板块12 · 接收时点 12:10');
  await expect(page.locator('.ty-chart-empty')).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('system-live-funds-noon-desktop.png'), fullPage: true, animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect(panel(page, '盘中同步排名')).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('system-live-funds-noon-mobile.png'), fullPage: true, animations: 'disabled' });
  assertSafeReads(state);
});

test('dense fund samples keep a plain line and reveal a sample only on hover', async ({ page }) => {
  const state = await installFixture(page); state.live = true; state.denseFunds = true;
  state.market = marketFixture(terminalDay, 'trading', true);
  await openTerminal(page, `/workbench?view=funds&fund_sector=${encodeURIComponent(thsIndustry)}`);
  const curve = page.locator('.ty-funds-chart svg');
  const samples = curve.locator('.ty-fund-sample');
  await expect(samples).toHaveCount(60);
  await expect(curve.locator('.ty-fund-last')).toHaveCount(1);
  expect(await curve.locator('circle').evaluateAll(nodes => nodes.filter(node => getComputedStyle(node).opacity !== '0').length)).toBe(1);
  const line = await curve.locator(':scope > g[opacity="1"] > path').getAttribute('d');
  expect(line?.match(/L/g)).toHaveLength(59);
  // Dense circles overlap. Hover the real plotted coordinate and verify the
  // chart's nearest-sample reader rather than requiring a covered SVG circle
  // to be the pointer event target.
  await curve.scrollIntoViewIfNeeded();
  const hoverPoint = await samples.nth(20).evaluate(node => {
    const circle = node as SVGCircleElement, svg = circle.ownerSVGElement!;
    const point = svg.createSVGPoint(); point.x = circle.cx.baseVal.value; point.y = circle.cy.baseVal.value;
    const screen = point.matrixTransform(svg.getScreenCTM()!); return { x: screen.x, y: screen.y };
  });
  await page.mouse.move(hoverPoint.x, hoverPoint.y);
  await expect(page.locator('.ty-chart-readout')).toContainText('接收 2001-12-31 10:20:00');
  await expect(page.locator('.ty-chart-readout')).toContainText('0.20 亿元');
  await expect(samples.nth(20).locator('title')).toContainText('接收时点 10:20');
  await page.mouse.move(0, 0);
  await expect(page.locator('.ty-chart-readout')).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('funds-plain-line-desktop.png'), fullPage: true, animations: 'disabled' });
  assertSafeReads(state);
});

test('later THS receipts advance their clock without claiming a native close sample', async ({ page }) => {
  const state = await installFixture(page); state.live = true; state.nearClose = true;
  state.market = marketFixture(terminalDay, 'closed', true);
  await openTerminal(page, '/workbench?view=funds');
  await page.getByRole('button', { name: '当日 · 已存采样', exact: true }).click();
  const chart = page.locator('.ty-funds-chart-panel');
  await expect(chart.locator('.ty-panel-action')).toContainText('已存接收采样');
  await expect(chart).toContainText(`本类最近接收 ${terminalDay} 14:59:20`);
  await expect(chart).not.toContainText('已收盘 · 截至');
  state.closingFunds = true;
  await page.reload();
  await showFundsChart(page);
  await page.getByRole('button', { name: '当日 · 已存采样', exact: true }).click();
  await expect(chart).toContainText(`本类最近接收 ${terminalDay} 15:00:20`);
  await expect(chart.locator('.ty-panel-action')).toContainText('已存接收采样');
  await expect(chart).not.toContainText('已收盘 · 截至');
  await expect(chart).not.toContainText('收盘数据不完整');
  assertSafeReads(state);
});

test('later concept receipts cannot hide incomplete THS industry coverage or replace its clock', async ({ page }, info) => {
  const state = await installFixture(page); state.live = true; state.splitClosingFunds = true;
  state.market = marketFixture(terminalDay, 'closed', true);
  await openTerminal(page, '/workbench?view=funds');
  await page.getByRole('button', { name: '当日 · 已存采样', exact: true }).click();
  const chart = page.locator('.ty-funds-chart-panel');
  await expect(chart).toContainText(`本类最近接收 ${terminalDay} 14:59:20`);
  await expect(page.getByRole('status').filter({hasText:'缺第 7 页'})).toBeVisible();
  await expect(chart).not.toContainText('15:00:40');
  await page.screenshot({ path: info.outputPath('industry-incomplete-close.png'), fullPage: true });
  await page.getByRole('button', { name: '概念板块', exact: true }).click();
  await expect(chart).toContainText(`本类最近接收 ${terminalDay} 15:00:40`);
  await expect(page.getByRole('status').filter({hasText:'缺第 7 页'})).toHaveCount(0);
  await expect(chart).not.toContainText('已收盘 · 截至');
  await expect(chart).not.toContainText('收盘数据不完整');
  assertSafeReads(state);
});

test('a selected THS curve outside the Top10 keeps its own receipt separate from category coverage', async ({ page }, info) => {
  const state = await installFixture(page); state.live = true; state.splitClosingFunds = true; state.extendedFunds = true;
  state.market = marketFixture(terminalDay, 'closed', true);
  await openTerminal(page, '/workbench?view=funds&fund_sector=THS%3Aindustry%3A881012');
  await page.getByRole('button', { name: '当日 · 已存采样', exact: true }).click();
  const chart = page.locator('.ty-funds-chart-panel');
  await expect(chart).toContainText(`本类最近接收 ${terminalDay} 12:10:00`);
  await expect(chart).toContainText('合成同花顺板块12');
  await expect(chart.locator('g[data-selected=true] .ty-fund-sample title').last()).toContainText('接收时点 10:05');
  await expect(chart).not.toContainText('已收盘 · 截至 15:00');
  await page.screenshot({ path: info.outputPath('selected-incomplete-close.png'), fullPage: true });
  assertSafeReads(state);
});

test('a fresh index does not hide stale stock quotes while an explicit historical view stays quiet', async ({ page }) => {
  const state = await installFixture(page); state.live = true; state.staleQuotes = true;
  state.market = marketFixture('2026-10-26', 'trading', true);
  await openTerminal(page, '/workbench?view=overview');
  await expect(page.locator('.ty-clock')).toContainText('交易中');
  await expect(page.locator('.ty-ribbon-stamp')).toContainText('行情待更新');
  await expect(page.locator('.ty-ribbon-stamp')).toContainText('行情截至 09:31');
  await page.goto('/workbench?view=overview&trade_day=' + terminalDay);
  await expect(page.locator('.ty-ribbon-stamp')).toContainText('历史回看');
  await expect(page.locator('.ty-ribbon-stamp')).not.toContainText('行情待更新');
  assertSafeReads(state);
});

test('only four workbench tabs remain without an entry into the old review page', async ({ page }) => {
  const state = await installFixture(page); await openTerminal(page);
  const auth = await page.evaluate(async () => (await fetch('/api/v1/session')).json()); const owner = `${auth.meta.app_mode}:${auth.data.user.id}`;
  const saved = {
    [`tianyan.workbench.widgets.v1:${owner}`]: JSON.stringify({ version: 1, visible: ['rules', 'funds'] }),
    [`tianyan.workbench.geometry.v1:${owner}`]: JSON.stringify({ version: 1, tiles: [{ id: 'funds', x: 0, y: 0, w: 6, h: 8 }] }),
    [`tianyan:workbench-layout:v1:${auth.data.user.id}`]: JSON.stringify({ version: 1, tiles: [{ id: 'rule', x: 0, y: 0, w: 6, h: 8, visible: true }] }),
  };
  // Seed once, not with addInitScript: a reload must not hide an accidental rewrite.
  await page.evaluate(values => { for (const [key, value] of Object.entries(values)) localStorage.setItem(key, value); }, saved);
  await page.reload();
  const tabs = page.getByRole('navigation', { name: '工作台页面', exact: true });
  await expect(tabs.getByRole('link')).toHaveCount(4);
  await expect(tabs.getByRole('link')).toHaveText(views.map(view => view.label));
  await expect(tabs).not.toContainText('收盘复盘');
  await expect(tabs).not.toContainText('待设计');
  for (const view of views) {
    await switchView(page, view);
    await expect(tabs.getByRole('link', { name: view.label, exact: true })).toHaveAttribute('aria-current', 'page');
    await expect(tabs).not.toContainText('收盘复盘');
    await expect(page.locator('.ty-shell')).toHaveCount(1);
    await expect(tabs.locator('a[href^="/research"]')).toHaveCount(0);
    if (view.view === 'ladder') { await expect(page.getByRole('tablist',{name:'连板视图'})).toBeVisible(); continue; }
    if (view.view === 'funds') {
      const ai = page.getByRole('complementary', { name: 'AI 观察', exact: true });
      await expect(ai).toHaveCount(1); await expect(ai.getByRole('heading', { name: 'AI 观察', exact: true })).toHaveCount(1);
      await expect(ai.locator('.ty-ai-overview')).toContainText('启用 AI 并分配模型后可手动生成');
      await expect(ai.getByRole('heading', { name: '题材归纳', exact: true })).toHaveCount(1);
    }
    await expect(page.getByRole('button', { name: /^(编辑布局|保存布局|恢复默认组件)$/ })).toHaveCount(0);
  }
  await page.reload(); await page.waitForLoadState('networkidle');
  expect(await page.evaluate(keys => Object.fromEntries(keys.map(key => [key, localStorage.getItem(key)])), Object.keys(saved))).toEqual(saved);
  expect(await page.evaluate(() => [...Object.entries(localStorage), ...Object.entries(sessionStorage)]
    .some(([key, value]) => /token|password|secret|api[_-]?key/i.test(key) || /"(?:token|password|secret|api[_-]?key)"\s*:/i.test(value))),
  'browser storage must not retain credentials').toBe(false);
  await expect(page.locator('body')).not.toContainText(/JL_TUSHARE_TOKEN|__http_url|118\.89\.117\.77|api\.tushare\.pro/);
  assertSafeReads(state);
});

test('stock and date selections persist in URLs, reloads and browser history', async ({ page }) => {
  const state = await installFixture(page);
  await openTerminal(page, `/workbench?view=ladder&trade_day=${terminalDay}&stock=${stockA}&sector=${sectorA}`);
  await page.locator('.lo-stock').filter({hasText:'合成乙'}).click();
  await expect(page).toHaveURL(new RegExp(`stock=${stockB.replace('.', '\\.')}`));
  await expect(page.getByRole('complementary',{name:'选中股票详情'})).toContainText('合成乙');
  await expect(page.locator('.ty-selected')).toContainText('合成乙');
  expect(new URL(page.url()).searchParams.get('sector')).toBe(sectorA);
  await page.reload(); await expect(page.locator('.ty-selected')).toContainText('合成乙');
  await switchView(page, views[1]);
  const historical = new URL(page.url()); historical.searchParams.set('trade_day','2001-12-28'); historical.searchParams.delete('as_of_time'); await page.goto(historical.href);
  await expect(page).toHaveURL(/trade_day=2001-12-28/);
  await page.goBack();
  await expect(page).toHaveURL(new RegExp('trade_day='+terminalDay));
  await page.goForward();
  await expect(page).toHaveURL(/trade_day=2001-12-28/);
  await page.reload();
  await expect(page).toHaveURL(/trade_day=2001-12-28/);
  await page.goto(`/auction?trade_day=${terminalDay}&stock=${stockB}&sector=${sectorB}`);
  await expect(page).toHaveTitle('竞价观察 · 天衍系统');
  await expect(page.locator('.ty-selected')).toContainText('合成乙');
  expect(state.queries.some(query => query.get('view') === 'auction' && query.get('day') === terminalDay && !query.has('stock') && !query.has('sector'))).toBe(true);
  assertSafeReads(state);
});

test('auction amounts remain separate from missing three-node seals, including a genuine zero', async ({ page }) => {
  const state = await installFixture(page);
  await openTerminal(page, `/workbench?view=auction&trade_day=${terminalDay}&stock=${stockA}`);
  await expect(page.locator('.ty-auction-metrics')).toContainText(/竞价成交额\s*1\.23\s*亿/);
  await expect(page.getByRole('img', { name: '实际竞价成交金额跨日比较，单位亿元', exact: true })).toBeVisible();
  const currentDay = page.getByRole('region', { name: `${terminalDay}竞价对照`, exact: true });
  const row = currentDay.locator('li').filter({ has: page.getByRole('button', { name: /^合成甲/ }) });
  await expect(row.locator('.ty-auction-values > span').filter({ hasText: /^—$/ })).toHaveCount(3);
  await expect(page.locator('.ty-auction-days li[data-current="true"]')).toHaveCount(2);
  await page.getByRole('tab', { name: '三个节点封单', exact: true }).click();
  const nodes = page.getByRole('img', { name: '三个节点的封单金额对比', exact: true });
  await expect(nodes.locator('rect')).toHaveCount(0);
  await expect(page.getByText('三个节点封单金额待接入', { exact: true })).toBeVisible();
  await currentDay.getByRole('button',{name:`按${terminalDay} 09:15封单排序`,exact:true}).click();
  await expect(row.locator('.ty-auction-values > span').filter({ hasText: /^—$/ })).toHaveCount(3);
  await currentDay.getByRole('button', { name: /^合成乙/ }).click();
  await expect(page.locator('.ty-auction-metrics')).toContainText(/竞价成交额\s*0\.00\s*亿/);
  await expect(page.locator('.ty-auction-metrics')).toContainText(/竞价涨幅\s*—/);
  await expect(page.locator('.ty-auction-days li[data-current="true"]')).toHaveCount(2);
  await page.getByRole('tab', { name: '实际竞价成交 · 跨日比较', exact: true }).click();
  const actual = page.getByRole('img', { name: '实际竞价成交金额跨日比较，单位亿元', exact: true });
  await expect(actual.locator('.ty-value-label')).toHaveCount(4);
  for (const value of await actual.locator('.ty-value-label').all()) await expect(value).toHaveText('0.00');
  assertSafeReads(state);
});

test('THS preserves zero versus missing and leaves members, exact deltas and daily history unavailable', async ({ page }) => {
  const state = await installFixture(page);
  await openTerminal(page, `/workbench?view=funds&trade_day=${terminalDay}&stock=${stockA}&sector=${sectorA}`);
  const ranking = page.locator('.ty-funds-ranking');
  const zeroSector = ranking.getByRole('row').filter({ has: page.getByRole('button', { name: thsIndustryName, exact: true }) });
  await expect(zeroSector.getByRole('cell').nth(2)).toHaveText('0.0');
  await expect(zeroSector.getByRole('cell').nth(3)).toHaveText('—');
  await expect(zeroSector.getByRole('cell').nth(4)).toHaveText('—');
  const missingSector = ranking.getByRole('row').filter({ has: page.getByRole('button', { name: '合成同花顺缺测', exact: true }) });
  await expect(missingSector.getByRole('cell').nth(2)).toHaveText('—');
  await expect(page.locator('.ty-members-panel tbody tr')).toHaveCount(0);
  await expect(page.locator('.ty-members-panel')).toContainText('同花顺成员映射与个股资金尚缺');
  await expect(page.locator('.ty-members-panel')).not.toContainText('合成甲');
  await expect(page.locator('.ty-members-panel')).not.toContainText('合成乙');
  await expect(page.getByRole('img', { name: thsCurveName, exact: true })).toBeVisible();
  await page.getByRole('button', { name: '盘后 · 近10日', exact: true }).click();
  await expect(panel(page, '盘后资金排名')).toContainText(thsIndustryName);
  await expect(zeroSector.getByRole('cell').nth(2)).toHaveText('—');
  const dailyChart = page.getByRole('img', { name: '近十个已采集交易日板块资金净额，单位亿元', exact: true });
  await expect(dailyChart).toBeVisible();
  await expect(dailyChart.locator('.ty-fund-sample')).toHaveCount(0);
  await expect(page.getByText('等待盘后资金历史', { exact: true })).toBeVisible();
  await expect(page.locator('.ty-funds-compare')).toContainText('等待同口径历史记录');
  await expect(ranking).not.toContainText('合成题材甲');
  await page.getByRole('button', { name: '盘中 · 当日', exact: true }).click();
  await expect(zeroSector.getByRole('cell').nth(2)).toHaveText('0.0');
  await expect(missingSector.getByRole('cell').nth(2)).toHaveText('—');
  assertSafeReads(state);
});

test('empty datasets retain every page framework and never fill missing values with zero', async ({ page }) => {
  const state = await installFixture(page); state.mode = 'empty'; await openTerminal(page);
  for (const view of views) {
    await switchView(page, view);
    if (view.view === 'ladder') {
      await expect(page.getByText('当前名单没有可展示的连板对象', { exact: true })).toBeVisible();
      await expect(page.getByText('当前来源范围未列出可展示对象，请核对本日资料；不代表其他来源或全市场。', { exact: true })).toBeVisible();
      await expect(page.locator('.lo-stock')).toHaveCount(0);
      continue;
    }
    if (view.view === 'funds') await expect(page.getByRole('complementary', { name: 'AI 观察', exact: true })).toHaveCount(1);
    await expect(page.locator('.ty-terminal')).not.toContainText('合成甲');
    await expect(page.locator('.ty-terminal')).not.toContainText('0.00%');
    await expect(page.locator('.ty-empty, .ty-chart-empty, .ty-auction-no-data').first()).toBeVisible();
  }
  assertSafeReads(state);
});

test('a failed read remains an error and the retry loads a fresh successful result', async ({ page }) => {
  const state = await installFixture(page); state.mode = 'error'; await openTerminal(page);
  const failure = (await openNotifications(page)).getByRole('listitem').filter({ hasText: '合成测试：工作台缓存读取失败' }).filter({ hasText: '综合盯盘' });
  await expect(failure).toBeVisible();
  await expect(page.getByRole('heading', { name: '涨跌停家数走势', exact: true })).toBeVisible();
  await expect(page.locator('.ty-overview-stock-row')).not.toContainText('合成甲');
  const before = state.calls;
  let releaseRetry!: () => void, retryStarted = false;
  const gate = new Promise<void>(resolve => { releaseRetry = resolve; });
  const holdRetry = async (route: Route) => { retryStarted = true; await gate; await route.fallback(); };
  await page.route('**/api/v1/workbench-view?*', holdRetry);
  try {
    await failure.getByRole('button', { name: '重新读取', exact: true }).click();
    await expect.poll(() => retryStarted).toBe(true);
    state.mode = 'filled';
  } finally { releaseRetry(); }
  await expect(panel(page, '个股涨幅榜')).toContainText('合成甲');
  await expect(failure).toHaveCount(0); expect(state.calls).toBeGreaterThan(before);
  await page.unroute('**/api/v1/workbench-view?*', holdRetry);
  assertSafeReads(state);
});

// The replacement ladder's evidence/price-only regressions live in ladder-workspace.spec.ts.

test('closing archives replace market summaries while intraday gaps and missing speed remain explicit', async ({ page }, info) => {
  const state = await installFixture(page); state.live = true; state.closedGap = true; state.closing = true;
  state.market = { ...marketFixture('2026-10-26', 'closed', false), server_time: '2026-10-26T18:00:00+08:00' };
  await openTerminal(page, '/workbench?view=overview');
  await expect(page.locator('.ty-ribbon-stamp')).toContainText('收盘日线已归档');
  await expect(page.locator('.ty-ribbon-stamp')).toContainText('2026-10-26 · 接收 16:05');
  await expect(page.locator('.ty-ribbon-turnover')).toContainText('2.00万亿');
  await expect(panel(page, '个股涨幅榜')).toContainText('2026-10-26 收盘');
  await expect(panel(page, '个股涨速榜')).toHaveCount(0);
  const speed = page.getByRole('table', { name: '个股涨幅榜', exact: true }).locator('tbody tr').first().getByRole('cell').nth(2);
  await expect(speed).toHaveText('—');
  await expect(speed).toHaveAttribute('title', '收盘日线不含5分钟涨速');
  await expect(panel(page, '涨跌停家数走势').locator('.ty-panel-action')).toHaveText('采样 13:46');
  await expect(panel(page, '涨跌停家数走势')).toContainText('家数为收盘日线');
  await expect(page.locator('.ty-index').first()).toHaveAttribute('title', /收盘日线.*归档接收/);
  await page.screenshot({ path: info.outputPath('closing-archive-with-intraday-gap.png'), fullPage: true });
  await switchView(page, views[2]);
  // The ladder has its own saved-day fixture. Do not label those older rows
  // with the overview archive's newer date merely because the page changed.
  await expect(page.locator('.lo-stock').filter({ hasText: '合成甲' })).toHaveAttribute('title', new RegExp(terminalDay + ' 收盘日线'));
  await expect(page.locator('.lo-footer')).toContainText(terminalDay);
  assertSafeReads(state);
});

test('supplier historical breadth is a separate curve and retains the 0925 point and closing counts', async ({ page }, info) => {
  const state = await installFixture(page); state.live = true; state.closedGap = true; state.closing = true; state.breadthHistory = true;
  state.market = { ...marketFixture('2026-10-26', 'closed', false), server_time: '2026-10-26T18:00:00+08:00' };
  await openTerminal(page, '/workbench?view=overview');
  const chart = panel(page, '涨跌停家数走势');
  await expect(chart).toContainText('选股宝历史');
  await expect(chart.locator('.ty-panel-head')).toContainText('跌停 25');
  await expect(chart).toContainText('收盘日线核验：涨停 59 / 跌停 28');
  await expect(chart.locator('.ty-panel-action')).toHaveText('历史 15:00');
  await expect(chart.getByRole('img')).toHaveAccessibleName('选股宝历史涨跌停家数历史走势');
  await expect(chart.getByRole('img')).toContainText('09:25');
  await expect(chart).not.toContainText('缺少收盘采样');
  const path = await chart.locator('.ty-chart-up > path[stroke="currentColor"]').getAttribute('d');
  expect((path?.match(/M/g) || []).length).toBeGreaterThan(1);
  await page.screenshot({ path: info.outputPath('closing-archive-with-independent-history.png'), fullPage: true });
  assertSafeReads(state);
});

test('known-at cutoffs reach the GET request and stay isolated from daily and other cached cutoffs', async ({ page }) => {
  const state = await installFixture(page);
  const first = `${terminalDay}T09:25:00+08:00`, second = `${terminalDay}T10:00:00+08:00`;
  await openTerminal(page, `/workbench?view=overview&trade_day=${terminalDay}&as_of_time=${encodeURIComponent(first)}`);
  expect(state.queries.some(query => query.get('as_of_time') === first)).toBe(true);
  await expect(page.locator('.ty-ribbon-stamp')).toContainText(/当时已知.*09:25/);
  await page.getByRole('navigation', { name: '综合盯盘工作区', exact: true }).getByRole('button', { name: '数据覆盖', exact: true }).click();
  await page.locator('.ty-coverage > summary').click();
  await page.getByLabel('当时已知回看时点（北京时间）', { exact: true }).fill(`${terminalDay}T10:00`);
  await page.getByRole('button', { name: '按此时点回看', exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get('as_of_time')).toBe(second);
  await expect.poll(() => state.queries.some(query => query.get('as_of_time') === second)).toBe(true);
  await expect(page.locator('.ty-ribbon-stamp')).toContainText(/当时已知.*10:00/);
  await page.getByRole('button', { name: '返回按日回看', exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.has('as_of_time')).toBe(false);
  await expect(page.locator('.ty-ribbon-stamp')).toContainText('历史回看');
  await expect.poll(() => state.queries.some(query => query.get('view') === 'overview' && !query.has('as_of_time'))).toBe(true);
  const reads = state.calls;
  await page.goBack();
  await expect(page.locator('.ty-ribbon-stamp')).toContainText(/当时已知.*10:00/);
  await page.waitForLoadState('networkidle');
  expect(state.calls, 'returning to an observed cache entry must not reuse the daily snapshot').toBe(reads);
  await switchView(page, views[1]);
  expect(state.queries.some(query => query.get('view') === 'auction' && query.get('as_of_time') === second)).toBe(true);
  const historical = new URL(page.url()); historical.searchParams.set('trade_day','2001-12-28'); historical.searchParams.delete('as_of_time'); await page.goto(historical.href);
  await expect.poll(() => new URL(page.url()).searchParams.has('as_of_time')).toBe(false);
  await expect.poll(() => state.queries.some(query => query.get('view') === 'auction' && query.get('day') === '2001-12-28' && !query.has('as_of_time'))).toBe(true);
  await expect(page.locator('.ty-replay-banner')).toHaveCount(0);
  assertSafeReads(state);
});

test('closed mobile navigation stays out of keyboard focus and Escape returns to its trigger', async ({ page }, info) => {
  const state = await installFixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await openTerminal(page);
  const navigation = page.getByRole('navigation', { name: '主导航', exact: true });
  const trigger = page.getByRole('button', { name: '打开主导航', exact: true });
  await expect(navigation).not.toBeVisible();
  await trigger.click();
  await expect(navigation.getByRole('link', { name: '工作台', exact: true })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(navigation).not.toBeVisible();
  await expect(trigger).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('工作台_390_合成测试数据.png'), fullPage: true });
  assertSafeReads(state);
});

test('four page modules do not overlap at 1024, 1440 and 1672 pixels', async ({ page }, info) => {
  test.setTimeout(60_000);
  const state = await installFixture(page); await openTerminal(page);
  for (const width of [1024, 1440, 1672]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const view of views) {
      await switchView(page, view);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      if (view.view === 'ladder') { await expect(page.locator('.lo-board')).toBeInViewport(); await expect(page.locator('.lo-footer')).toBeInViewport(); continue; }
      const rectangles = await page.locator('.ty-terminal .ty-panel:not(.ty-panel .ty-panel)').evaluateAll(elements => elements.filter(element => element.getBoundingClientRect().height > 0).map(element => {
        const rect = element.getBoundingClientRect();
        return { name: element.querySelector('h2,h3')?.textContent, x: rect.x, y: rect.y, w: rect.width, h: rect.height };
      }));
      expect(rectangles.length).toBeGreaterThanOrEqual(5);
      for (let i = 0; i < rectangles.length; i++) for (const other of rectangles.slice(i + 1)) {
        const a = rectangles[i];
        expect(a.x < other.x + other.w - 1 && a.x + a.w > other.x + 1 && a.y < other.y + other.h - 1 && a.y + a.h > other.y + 1,
          `${view.view} ${width}: ${a.name} overlaps ${other.name}`).toBe(false);
      }
      await page.screenshot({ path: info.outputPath(`terminal-${view.view}-${width}.png`), fullPage: true, animations: 'disabled' });
    }
  }
  assertSafeReads(state);
});


test('THS industry and concept classifications stay independent of EM hierarchy labels in both modes', async ({ page }) => {
  const state = await installFixture(page); state.live = true; state.mixedFunds = true;
  state.market = marketFixture('2026-10-27', 'trading', true);
  await openTerminal(page, '/workbench?view=funds');
  const controls = page.getByRole('group', { name: '资金板块分类' });
  const ranking = page.locator('.ty-funds-ranking');
  for (const mode of ['盘中 · 当日', '盘后 · 近10日']) {
    await page.getByRole('group', { name: '资金观察模式' }).getByRole('button', { name: mode, exact: true }).click();
    await controls.getByRole('button', { name: '同花顺行业', exact: true }).click();
    await expect(ranking.locator('tbody tr')).toHaveCount(3);
    await expect(ranking).toContainText(thsIndustryName);
    for (const name of ['合成一级行业', '合成三级行业', '未核层级II', '合成独立概念']) await expect(ranking).not.toContainText(name);
    await expect(page.locator('.ty-ribbon-stat').filter({ hasText: '资金覆盖' })).toContainText('3');
    await controls.getByRole('button', { name: '概念板块', exact: true }).click();
    await expect(ranking.locator('tbody tr')).toHaveCount(1);
    await expect(ranking).toContainText(thsConceptName);
    await expect(ranking).not.toContainText(thsIndustryName);
    await expect.poll(() => new URL(page.url()).searchParams.get('fund_sector')).toBe(thsConcept);
  }
  assertSafeReads(state);
});

test('funds keep a directly linked concept across page switches and allow an explicit category change', async ({ page }) => {
  const state = await installFixture(page); state.live = true; state.mixedFunds = true;
  state.market = marketFixture('2026-10-27', 'trading', true);
  await openTerminal(page, '/workbench?view=funds&sector=' + sectorB + '&fund_sector=' + encodeURIComponent(thsConcept));
  const controls = page.getByRole('group', { name: '资金板块分类' });
  await expect(controls.getByRole('button', { name: '概念板块', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.ty-members-panel')).toContainText(thsConceptName);
  await expect(page.locator('.ty-members-panel tbody tr')).toHaveCount(0);
  await expect.poll(() => new URL(page.url()).searchParams.get('fund_sector')).toBe(thsConcept);
  const navigation = page.getByRole('navigation', { name: '工作台页面', exact: true });
  await navigation.getByRole('link', { name: '综合盯盘', exact: true }).click();
  await navigation.getByRole('link', { name: '资金动向', exact: true }).click();
  await expect(controls.getByRole('button', { name: '概念板块', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => new URL(page.url()).searchParams.get('fund_sector')).toBe(thsConcept);
  await controls.getByRole('button', { name: '同花顺行业', exact: true }).click();
  await expect(controls.getByRole('button', { name: '同花顺行业', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => new URL(page.url()).searchParams.get('fund_sector')).toBe(thsIndustry);
  expect(new URL(page.url()).searchParams.get('sector')).toBe(sectorB);
  assertSafeReads(state);
});

for (const ready of [false, true]) {
  test(`THS does not borrow an EM outside-ranking selection with ${ready ? 'available native detail' : 'a recovering native detail'}`, async ({ page }) => {
    const state = await installFixture(page); state.live = true; state.focusedFunds = true; state.focusReady = ready;
    state.market = marketFixture('2026-10-27', 'trading', true);
    await openTerminal(page, '/workbench?view=funds&sector=BK0999.DC');
    await expect(page.locator('.ty-members-panel')).toContainText(thsIndustryName);
    await expect.poll(() => new URL(page.url()).searchParams.get('sector')).toBe('BK0999.DC');
    const ranking = page.locator('.ty-funds-ranking');
    await expect(ranking.locator('tbody tr')).toHaveCount(3);
    await expect(ranking).not.toContainText('合成榜外二级');
    const chart = page.locator('.ty-funds-chart-panel');
    await expect(chart).not.toContainText('合成榜外二级');
    await expect(chart).not.toContainText('正在自动恢复');
    await expect(chart).toContainText('本类最近接收 2026-10-27 10:05:02');
    await expect(page.locator('.ty-members-panel tbody tr')).toHaveCount(0);
    const observedValue = page.locator('.ty-ai-follow .ty-ai-item').first();
    await expect(observedValue).toContainText('+1.0');
    await expect(observedValue).not.toContainText('-2.0');
    await expect(observedValue).toContainText('接收 10:05');
    await expect(observedValue).not.toContainText('截至 10:05');
    await expect(observedValue).not.toContainText('990.0');
    await expect(page.locator('.ty-funds-context')).not.toContainText('990.00');
    assertSafeReads(state);
  });
}

test('a THS ranked amount and receipt do not borrow a newer EM ranked or focused record', async ({ page }) => {
  const state = await installFixture(page); state.live = true; state.focusedFunds = true; state.newerRankedFunds = true;
  state.market = marketFixture('2026-10-27', 'trading', true);
  await openTerminal(page, '/workbench?view=funds&sector=BK0999.DC');
  const observed = page.locator('.ty-ai-follow .ty-ai-item').first();
  await expect(observed).toContainText('+4.4');
  await expect(observed).toContainText('接收 10:06');
  await expect(observed).not.toContainText('3.3');
  await expect(page.locator('.ty-funds-chart-panel')).toContainText('本类最近接收 2026-10-27 10:06:07');
  await expect(observed).not.toContainText('-2.0');
  await expect(observed).not.toContainText('日频净额');
  await expect(page.locator('.ty-funds-ranking tbody tr')).toHaveCount(3);
  assertSafeReads(state);
});

test('THS daily mode stays explicitly empty and returning restores only its own receipt sampling', async ({ page }) => {
  const state = await installFixture(page); state.live = true; state.focusedFunds = true;
  state.market = marketFixture('2026-10-27', 'trading', true);
  await openTerminal(page, '/workbench?view=funds&sector=BK0999.DC');
  const observed = page.locator('.ty-ai-follow .ty-ai-item').first();
  await expect(observed).toContainText('+1.0');
  await expect(observed).not.toContainText('日频净额');
  await page.getByRole('button', { name: '盘后 · 近10日', exact: true }).click();
  await expect(observed).toContainText('资金样本');
  await expect(observed).not.toContainText('日频净额');
  await expect(observed).toContainText('该板块尚无已保存资金数值');
  await expect(observed).not.toContainText('990.0');
  await expect(observed).not.toContainText('-2.0');
  await page.getByRole('button', { name: '盘中 · 当日', exact: true }).click();
  await expect(observed).toContainText('+1.0');
  await expect(page.locator('.ty-funds-chart-panel')).toContainText('本类最近接收 2026-10-27 10:05:02');
  await expect(observed).not.toContainText('-2.0');
  await expect(observed).not.toContainText('日频净额');
  assertSafeReads(state);
});

test('unchanged prices keep only the visible selected detail interested with bounded local reads', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-27T10:05:00+08:00') });
  const state = await installFixture(page); state.live = true;
  state.market = marketFixture('2026-10-27', 'trading', true);
  const interests: URLSearchParams[] = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/workbench-details' && request.headers()['x-workbench-active'] === '1') interests.push(url.searchParams);
  });
  await openTerminal(page, `/workbench?view=funds&stock=${stockB}&sector=${sectorA}`);
  const baseline = interests.length;
  await page.clock.fastForward(61_000);
  await expect.poll(() => interests.length).toBeGreaterThan(baseline);
  expect(interests.slice(baseline).every(query => query.get('codes') === stockB && !query.has('sector'))).toBe(true);
  expect(interests.length - baseline).toBeLessThanOrEqual(1);
  assertSafeReads(state);
});

test('compact auction fits two up and two down columns, keeps detail beside the list, and adapts without document overflow', async ({ page }) => {
  await installFixture(page);
  await page.route('**/api/v1/workbench-view?*', route => {
    const data = terminalFixture(new URL(route.request().url()).searchParams);
    data.auction_days.forEach(day => {
      day.node_source = '合成封单来源';
      day.one_word_count = 12;
      day.header_total_cny = 54.2e8;
      day.items = Array.from({length: 60}, (_, i) => ({...day.items[i % day.items.length],
        code: `SYN${i}.SZ`, name: i === 0 ? '合成长名称示例' : `样本${String(i + 1).padStart(2, '0')}`,
        auction_tags: ['机器人', '文化传媒'], auction_board: '2板',
        node_amounts: {'09:15': 2.2e8, '09:20': 2.9e8, '09:25': 3.5e8}}));
    });
    return route.fulfill({json: {meta, data}});
  });
  await openTerminal(page, `/workbench?view=auction&trade_day=${terminalDay}&stock=${stockA}`);
  for (const [width, height] of [[1920, 1080], [1440, 900], [1366, 768], [1024, 768], [390, 844]]) {
    await page.setViewportSize({width, height});
    await page.waitForTimeout(200);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    if (width >= 1366) {
      const board = await page.locator('.ty-auction-comparison').boundingBox();
      const workspace = await page.locator('.ty-auction-support-workspace').boundingBox();
      const detail = await page.locator('.ty-auction-detail').boundingBox();
      const rail = await page.locator('.ty-auction-rail').boundingBox();
      expect(detail!.x).toBeGreaterThan(board!.x + board!.width - 1);
      expect(Math.abs(detail!.y - board!.y)).toBeLessThan(2);
      expect(Math.abs(rail!.y + rail!.height - workspace!.y - workspace!.height)).toBeLessThan(2);
      expect(board!.y + board!.height).toBeLessThanOrEqual(height);
      await expect(page.locator('.ty-auction-bottom')).toHaveCount(0);
      const rowHeight = await page.locator('.ty-auction-stock-list li').nth(2).evaluate(el => el.getBoundingClientRect().height);
      expect(rowHeight).toBeLessThanOrEqual(40);
      const listHeight = await page.locator('.ty-auction-stock-list').first().evaluate(el => el.clientHeight);
      expect(listHeight / rowHeight).toBeGreaterThanOrEqual(width === 1920 ? 6 : 4);
    }
    await page.screenshot({path: test.info().outputPath(`responsive-fixture-${width}.png`), fullPage: width === 390});
  }
  await page.setViewportSize({width: 1920, height: 1080});
  await page.locator('.ty-auction-days').getByRole('button', {name: /^样本03/}).first().click();
  await expect(page.locator('.ty-auction-detail .ty-panel-head')).toContainText('样本03');
  await expect(page.locator('.ty-auction-days li[data-current]')).toHaveCount(2);
});
