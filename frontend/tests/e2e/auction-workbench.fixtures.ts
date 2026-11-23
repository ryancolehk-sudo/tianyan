import type { Page } from '@playwright/test';
import { DAY, PREV_DAY, PREV2_DAY, datasets, receivedAt, sourceIndex, vendorItems } from './auction-vendor.fixtures';

export function workbenchItems() {
  // Same codes across days; all values are synthetic, never imported by production.
  return vendorItems({ historyDays: [PREV_DAY, PREV2_DAY, '2026-01-02'], historyRows: 30 }) as any[];
}
export function referenceItem(family: string, rows: Record<string, string | null>[]) {
  return { id: '00000000-0000-4000-8000-0000000000d1', source_id: '00000000-0000-4000-8000-0000000000d2',
    family, source_name: '合成Tushare参考', source_key: family, instrument_id: null, received_at: receivedAt(1), published_at: null,
    content_hash: 'a'.repeat(64), payload: { dataset: family.replace('tushare_', 'tushare.'), title: '合成参考表',
      observed_day: DAY, rows, columns: [], attribution: '合成浏览器夹具', scope: '仅测试返回范围，不是全市场', source_url: 'https://tushare.pro/document/2?doc_id=369' } };
}
export async function routeWorkbench(page: Page, options: {
  items?: any[]; index?: any; failReads?: () => boolean; denied?: () => boolean; calendar?: boolean; calendarEvidence?: any[]; breadth?: any; quotes?: any;
} = {}) {
  const items = options.items ?? workbenchItems();
  await page.route('**/api/v1/reference-datasets*', route => {
    const url = new URL(route.request().url()), family = url.searchParams.get('family'), dataset = url.searchParams.get('dataset'), day = url.searchParams.get('day');
    if (dataset === 'market.ashare.breadth') return route.fulfill({ json: datasets(options.breadth ? [options.breadth] : []) });
    if (options.denied?.()) return route.fulfill({ json: datasets([]) });
    if (options.failReads?.()) return route.fulfill({ status: 503, json: { error: { code: 'unavailable', message: '合成读取失败', details: [] } } });
    return route.fulfill({ json: datasets(items.filter(i => i.family === family && i.payload.dataset === dataset && (!day || i.payload.observed_day === day))) });
  });
  await page.route('**/api/v1/data-sources', route => route.fulfill({ json: options.index ?? sourceIndex() }));
  await page.route('**/api/v1/calendar-versions*', route => route.fulfill({ json: { meta: {}, data: { items: options.calendar === false ? [] : ['SSE', 'SZSE'].map(exchange => ({ id: exchange, exchange, version: 'fixture', coverage_start: '2020-01-01', coverage_end: '2030-12-31', recorded_at: receivedAt(1) })), next_cursor: null } } }));
  await page.route('**/api/v1/evidence?kind=calendar*', route => route.fulfill({ json: { meta: {}, data: { items: options.calendarEvidence ?? [], next_cursor: null } } }));
  await page.route('**/api/v1/trading-sessions*', route => {
    const u = new URL(route.request().url()), date = u.searchParams.get('trade_day');
    return route.fulfill({ json: { meta: {}, data: { exchange: u.searchParams.get('exchange'), requested_day: date, calendar_version: 'fixture', session: { trade_day: date, is_open: date === DAY || date === PREV_DAY, segments: [] }, reason: null } } });
  });
  await page.route('**/api/v1/instruments?*', route => route.fulfill({ json: { meta: {}, data: { items: [], next_cursor: null } } }));
  return items;
}
