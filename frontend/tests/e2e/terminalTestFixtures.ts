import type { Stock, TerminalView, WorkbenchData } from '../../src/features/terminal/types';

// Every market value in these browser fixtures is synthetic, never a saved quote.
export const terminalDay = '2001-12-31';
export const terminalDays = [terminalDay, '2001-12-28', '2001-12-27', '2001-12-26'];
export const stockA = '999991.SZ';
export const stockB = '999992.SH';
export const sectorA = 'SYNTHETIC-A';
export const sectorB = 'SYNTHETIC-B';

function stock(code: string, name: string, missing = false): Stock {
  return {
    code, name, instrument_id: null, theme: missing ? '合成题材乙' : '合成题材甲',
    pct_change: missing ? null : 0, speed_5m: missing ? null : 0,
    amount_cny: missing ? null : 0, turnover_rate: missing ? null : 0,
    float_mv_cny: missing ? null : 0, price: missing ? null : 10,
    pre_close: missing ? null : 10, open: missing ? null : 10, high: missing ? null : 10,
    limit_up: missing ? null : 11, limit_down: missing ? null : 9,
    previous_height: missing ? 2 : 4, status: missing ? '待观测' : '观测开板',
    first_observed: null, latest_change: null, seal_amount_cny: null,
    auction_pct: missing ? null : 0, auction_price: missing ? null : 10,
    auction_amount_cny: missing ? 0 : 123_000_000, volume_ratio: missing ? null : 0,
    node_amounts: { '09:15': null, '09:20': null, '09:25': null },
    main_net_cny: missing ? null : 0, days_since_break: missing ? 1 : null,
    open_pct: missing ? null : 0,
  };
}

export function terminalFixture(query: URLSearchParams): WorkbenchData {
  const view = (query.get('view') ?? 'overview') as TerminalView;
  const day = query.get('day') ?? terminalDay;
  const stocks = [stock(stockA, '合成甲'), stock(stockB, '合成乙', true)];
  return {
    view, trade_day: day, available_days: terminalDays, mode: query.has('as_of_time') ? 'observed' : 'review',
    latest_at: `${day}T15:05:00+08:00`,
    indices: [
      { code: '000001.SH', name: '合成指数甲', price: 0, pct_change: 0, amount_cny: 0, source_time: `${day}T15:00:00+08:00` },
      { code: '399001.SZ', name: '合成指数乙', price: null, pct_change: null, amount_cny: null, source_time: null },
    ],
    breadth: {
      up: 0, down: null, flat: 2, limit_up: 0, limit_down: null, comparable: 2,
      scope: '合成浏览器测试样本（2只），不是真实市场',
      points: [
        { time: `${day}T09:30:00+08:00`, up: 0, down: null },
        { time: `${day}T09:35:00+08:00`, up: 0, down: 0 },
      ],
    },
    stocks,
    sectors: [
      { code: sectorA, category: 'industry', industry_level: 2, name: '合成题材甲', main_net_cny: 0, pct_change: 0, amount_cny: 0, rank: 1, member_count: 2 },
      { code: sectorB, category: 'industry', industry_level: 2, name: '合成题材乙', main_net_cny: -234_000_000, pct_change: null, amount_cny: null, rank: 2, member_count: 1 },
    ],
    selected_stock_code: query.get('stock') ?? stockA,
    selected_sector_code: query.get('sector') ?? sectorA,
    funds_trade_day: day, members_trade_day: day, previous_trade_day: '2001-12-28',
    auction_days: terminalDays.map((auctionDay, index) => ({
      day: auctionDay,
      items: stocks.map(item => ({ ...structuredClone(item), auction_amount_cny: item.code === stockA ? (123 + index) * 1_000_000 : 0 })),
    })),
    selected_members: query.get('sector') === sectorB ? [stocks[1]] : stocks,
    funds_history: [
      { day: '2001-12-26', sector_code: sectorA, value: 100_000_000 },
      { day: '2001-12-27', sector_code: sectorA, value: null },
      { day: '2001-12-28', sector_code: sectorA, value: 0 },
      { day, sector_code: sectorA, value: 0 },
      { day: '2001-12-26', sector_code: sectorB, value: -100_000_000 },
      { day, sector_code: sectorB, value: -234_000_000 },
    ],
    broken: [{ ...stocks[1], previous_height: 3, days_since_break: 1 }],
    events: [], stock_seals: [],
    capabilities: [
      { key: 'synthetic-quotes', label: '合成行情样本', status: 'available', reason: '合成浏览器测试数据，不是真实行情', source_family: 'tushare_market_rt', last_received_at: `${day}T15:05:00+08:00` },
      { key: 'synthetic-auction-nodes', label: '竞价三节点封单', status: 'unavailable', reason: '合成缺项：只有竞价成交结果，未取得三个节点的委托封单', source_family: 'tushare_auction', last_received_at: null },
      { key: 'synthetic-funds', label: '板块资金', status: 'after_market', reason: '合成盘后样本；盘中分钟资金待接入', source_family: 'tushare_moneyflow_dc', last_received_at: `${day}T18:00:00+08:00` },
    ],
  };
}

export function emptyTerminalFixture(query: URLSearchParams): WorkbenchData {
  return {
    ...terminalFixture(query), latest_at: null,
    indices: [], stocks: [], sectors: [], auction_days: [], selected_members: [],
    selected_stock_code: null, selected_sector_code: null,
    funds_trade_day: null, members_trade_day: null, previous_trade_day: null,
    breadth: { up: null, down: null, flat: null, limit_up: null, limit_down: null, comparable: null, scope: '合成空资料样本', points: [] },
    funds_history: [], broken: [], events: [], stock_seals: [],
  };
}
