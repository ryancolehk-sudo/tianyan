import { expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import { routeHermeticApi } from './hermetic-api.fixtures';

/**
 * Contract-shaped B-type fixtures (duanxianxia auction current/history tables).
 * Every number is synthetic: they exist only to prove pairing, units, ordering and
 * empty/old/revoked states. They are never imported by the production pages.
 */

const shanghaiDay = (offset = 0) => {
  const value = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
  const [year, month, date] = value.split('-').map(Number);
  return new Date(Date.UTC(year, month - 1, date + offset)).toISOString().slice(0, 10);
};

export const DAY = shanghaiDay(0);
export const PREV_DAY = shanghaiDay(-1);
export const PREV2_DAY = shanghaiDay(-2);
/** Vendor's date string, deliberately not the ISO trade day, so the page must show the raw text. */
export const sourceDateRaw = (day: string) => `${day.replaceAll('-', '/')} 09:25`;

export const meta = {
  request_id: '00000000-0000-4000-8000-0000000000aa', app_mode: 'demo',
  generated_at: `${DAY}T03:30:00Z`, as_of_time: null, quality: { status: 'partial', reasons: ['Synthetic auction fixtures only'] },
};
export const sessionBody = {
  data: { user: { id: '00000000-0000-4000-8000-000000000001', username: 'e2e', role: 'admin' }, csrf_token: 'csrf-auction-vendor' },
  meta,
};

const CURRENT_FAMILY = 'duanxianxia_auction';
const HISTORY_FAMILY = 'duanxianxia_auction_history';
const CURRENT_SOURCE = '00000000-0000-4000-8000-0000000000c1';
const HISTORY_SOURCE = '00000000-0000-4000-8000-0000000000c2';
export const CURRENT_HASH = '1'.repeat(64);
const HISTORY_HASH = (day: string) => day.replaceAll('-', '').padEnd(32, '0') + 'a'.repeat(32);

export function receivedAt(minutesAgo: number) {
  return new Date(Date.parse(`${DAY}T11:30:00+08:00`) - minutesAgo * 60_000).toISOString();
}

const summaryColumns = [
  { key: 'trade_day', label: '交易日' }, { key: 'source_date_raw', label: '来源日期原文' },
  { key: 'table_kind', label: '表形态' }, { key: 'row_count', label: '行数', unit: '只' },
  { key: 'one_word_count', label: '一字', unit: '只' }, { key: 'header_total_raw', label: '表头合计原文' },
  { key: 'header_total_cny', label: '表头合计', unit: '元' },
  { key: 'total_fd_0915_raw', label: '09:15 封单原文' }, { key: 'total_fd_0915_cny', label: '09:15 封单', unit: '元' },
  { key: 'total_fd_0920_raw', label: '09:20 封单原文' }, { key: 'total_fd_0920_cny', label: '09:20 封单', unit: '元' },
  { key: 'total_fd_0925_raw', label: '09:25 封单原文' }, { key: 'total_fd_0925_cny', label: '09:25 封单', unit: '元' },
  { key: 'batch_hash', label: 'batch_hash' },
];
const stockColumns = [
  { key: 'trade_day', label: '交易日' }, { key: 'source_order', label: '原表行序' },
  { key: 'code', label: '代码' }, { key: 'name', label: '名称' },
  { key: 'tags_raw', label: '标签' }, { key: 'board_raw', label: '板块' },
  { key: 'fd_0915_raw', label: '09:15 封单原文' }, { key: 'fd_0915_cny', label: '09:15 封单', unit: '元' },
  { key: 'fd_0920_raw', label: '09:20 封单原文' }, { key: 'fd_0920_cny', label: '09:20 封单', unit: '元' },
  { key: 'fd_0925_raw', label: '09:25 封单原文' }, { key: 'fd_0925_cny', label: '09:25 封单', unit: '元' },
  { key: 'change_pct_raw', label: '涨幅原文' }, { key: 'change_pct', label: '涨幅', unit: '%' },
  { key: 'batch_hash', label: 'batch_hash' },
];

type Cell = string | null;
type Row = Record<string, Cell>;

function summaryRow(day: string, kind: 'current' | 'history', hash: string): Row {
  return {
    trade_day: day, source_date_raw: sourceDateRaw(day), table_kind: kind, row_count: '30', one_word_count: '1',
    header_total_raw: '53.1亿', header_total_cny: '5310000000',
    total_fd_0915_raw: '59.6亿', total_fd_0915_cny: '5960000000',
    total_fd_0920_raw: '45.6亿', total_fd_0920_cny: '4560000000',
    total_fd_0925_raw: '53.1亿', total_fd_0925_cny: '5310000000',
    batch_hash: hash,
  };
}

function stockRow(day: string, kind: 'current' | 'history', hash: string, index: number): Row {
  const n = index + 1;
  const missing = n === 5;
  const zero = n === 6;
  const text = (amount: number) => `${amount / 10_000}万`;
  return {
    trade_day: day, source_order: String(n),
    code: `6000${String(n).padStart(2, '0')}`,
    name: `${kind === 'current' ? '样本封单' : '历史封单'}${String(n).padStart(2, '0')}`,
    tags_raw: n % 3 === 0 ? '机器人|减速器' : n % 2 === 0 ? '算力' : null,
    board_raw: n % 5 === 0 ? '首板' : n % 7 === 0 ? '2板' : null,
    fd_0915_raw: missing || zero ? (zero ? '0万' : '-') : text(n * 500_000),
    fd_0915_cny: missing ? null : zero ? '0' : String(n * 500_000),
    fd_0920_raw: missing ? '-' : text(n * 700_000),
    fd_0920_cny: missing ? null : String(n * 700_000),
    fd_0925_raw: missing || zero ? '-' : text(n * 1_000_000),
    fd_0925_cny: missing || zero ? null : String(n * 1_000_000),
    change_pct_raw: kind === 'current' ? `${(n / 10).toFixed(2)}%` : null,
    change_pct: kind === 'current' ? (n / 10).toFixed(2) : null,
    batch_hash: hash,
  };
}

function payload(dataset: string, rows: Row[], day: string) {
  return {
    dataset, title: dataset === 'market.duanxianxia.auction.summary' ? '竞价封单汇总（合成）' : '竞价封单明细（合成）',
    source_url: 'https://duanxianxia.com/web/jjlive',
    attribution: '短线侠公开竞价异动表（浏览器用例合成样本）',
    scope: '合成夹具，仅用于浏览器用例；不是真实行情，不代表全市场覆盖。',
    columns: dataset === 'market.duanxianxia.auction.summary' ? summaryColumns : stockColumns,
    rows, observed_day: day,
  };
}

function item(sourceId: string, family: string, day: string, hash: string, dataset: 'summary' | 'stocks', rows: Row[], at: string) {
  return {
    id: `00000000-0000-4000-8000-${dataset === 'summary' ? '000000000001' : '000000000002'}`,
    source_id: sourceId,
    source_name: family === CURRENT_FAMILY ? '短线侠竞价异动（当前表）' : '短线侠竞价异动（历史表）',
    family, source_key: `auction:${family}:${day}:${hash.slice(0, 16)}:${dataset}`,
    instrument_id: null, received_at: at, published_at: null, content_hash: hash,
    payload: payload(dataset === 'summary' ? 'market.duanxianxia.auction.summary' : 'market.duanxianxia.auction.stocks', rows, day),
  };
}

export type VendorOptions = {
  /** Current-table rows; defaults to 30 synthetic rows. */
  currentRows?: number;
  currentReceivedAt?: string;
  withSummary?: boolean;
  withStocks?: boolean;
  historyDays?: string[];
  historyRows?: number;
};

/** Current table plus a three-day history table, paired per source_id + batch_hash. */
export function vendorItems(options: VendorOptions = {}) {
  const {
    currentRows = 30, currentReceivedAt = receivedAt(12), withSummary = true, withStocks = true,
    historyDays = [DAY, PREV_DAY, PREV2_DAY], historyRows = 2,
  } = options;
  const currentStocks = Array.from({ length: currentRows }, (_, index) => stockRow(DAY, 'current', CURRENT_HASH, index));
  const items: unknown[] = [];
  if (withSummary) items.push(item(CURRENT_SOURCE, CURRENT_FAMILY, DAY, CURRENT_HASH, 'summary', [summaryRow(DAY, 'current', CURRENT_HASH)], currentReceivedAt));
  if (withStocks) items.push(item(CURRENT_SOURCE, CURRENT_FAMILY, DAY, CURRENT_HASH, 'stocks', currentStocks, currentReceivedAt));
  for (const day of historyDays) {
    const hash = HISTORY_HASH(day);
    const rows = Array.from({ length: historyRows }, (_, index) => stockRow(day, 'history', hash, index));
    items.push(item(HISTORY_SOURCE, HISTORY_FAMILY, day, hash, 'summary', [summaryRow(day, 'history', hash)], receivedAt(60 * 20)));
    items.push(item(HISTORY_SOURCE, HISTORY_FAMILY, day, hash, 'stocks', rows, receivedAt(60 * 20)));
  }
  return items as Array<{ payload: { dataset: string }; source_id: string }>;
}

/** One extra summary without its stocks table (or stocks without summary). */
export function unpairedItems(kind: 'summary-only' | 'stocks-only') {
  const day = DAY;
  const hash = '3'.repeat(64);
  const id = '00000000-0000-4000-8000-0000000000c3';
  const items = [
    item(id, CURRENT_FAMILY, DAY, hash, 'summary', [summaryRow(day, 'current', hash)], receivedAt(10)),
    item(id, CURRENT_FAMILY, DAY, hash, 'stocks', [stockRow(day, 'current', hash, 0)], receivedAt(10)),
  ];
  return kind === 'summary-only' ? [items[0]] : [items[1]];
}

export function datasets(items: unknown[]) {
  return { meta, data: { items } };
}

export function runtimeSource(family: string, over: Record<string, unknown> = {}) {
  const current = family === CURRENT_FAMILY;
  return {
    provider: {
      id: current ? CURRENT_SOURCE : HISTORY_SOURCE,
      name: current ? '短线侠竞价异动（当前表）' : '短线侠竞价异动（历史表）',
      kind: 'market', adapter_version: '1', configured: true, enabled: true, entitlement_status: 'verified', capabilities: [],
      rights: { display: 'allowed', cache: 'allowed', export: 'denied', redistribute: 'denied' },
    },
    family, rights_basis: '合成测试来源，仅用于浏览器用例', last_attempt: null, last_success_at: receivedAt(12),
    collection_state: 'ready', credential_configured: null, collection_enabled: true, paused_reason: null, symbols: [],
    interval_seconds: 180, configured_at: `${DAY}T08:00:00+08:00`, next_run_at: `${DAY}T09:30:00+08:00`, measured_cost_cny: null,
    ...over,
  };
}

export function sourceIndex(over: Record<string, unknown> = {}, families: Array<{ family: string; over?: Record<string, unknown> }> = [{ family: CURRENT_FAMILY }, { family: HISTORY_FAMILY }]) {
  return {
    meta,
    data: {
      items: families.map(row => runtimeSource(row.family, row.over ?? {})),
      available_adapters: [], collection_enabled: true, today_request_count: 3, today_run_count: 3, today_cost_cny: null, ...over,
    },
  };
}

export const emptyAuction = { meta, data: { trade_day: DAY, timezone: 'Asia/Shanghai', items: [], sources: [] } };

export type DatasetRoutes = { summary?: unknown[]; stocks?: unknown[] };

/** Routes the two reference-dataset reads by their dataset query and nothing else. */
export async function routeVendorDatasets(page: Page, routes: DatasetRoutes) {
  await page.route('**/api/v1/reference-datasets*', route => {
    const dataset = new URL(route.request().url()).searchParams.get('dataset');
    const items = dataset === 'market.duanxianxia.auction.summary' ? routes.summary ?? [] : dataset === 'market.duanxianxia.auction.stocks' ? routes.stocks ?? [] : [];
    return route.fulfill({ json: datasets(items) });
  });
}

export async function openAuction(page: Page, quotes?: unknown, time = `${DAY}T11:30:00+08:00`) {
  await routeHermeticApi(page, sessionBody);
  await page.route('**/api/v1/market-breadth-history*',route=>route.fulfill({json:{meta,data:{observation_day:DAY,source_name:null,items:[],truncated:false}}}));
  await page.clock.setFixedTime(new Date(time));
  await page.route('**/api/v1/research-documents*', route => route.fulfill({ json: { meta, data: { items: [], next_cursor: null } } }));
  await page.route('**/api/v1/market-auction*', route => route.fulfill({ json: emptyAuction }));
  await page.route('**/api/v1/market-quotes*', route => route.fulfill({ json: quotes ?? { meta, data: { items: [], selection: 'indices', query_time: `${DAY}T13:00:00+08:00`, historical: false } } }));
  await page.goto('/auction?legacy=1');
  await page.getByLabel('账号', { exact: true }).fill('e2e');
  await page.getByLabel('密码', { exact: true }).fill('hermetic-auction-vendor');
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('heading', { name: '竞价观察', exact: true })).toBeVisible();
}
