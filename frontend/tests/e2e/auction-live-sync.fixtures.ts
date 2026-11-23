import type { Stock, WorkbenchData } from '../../src/features/terminal/types';
import { terminalFixture } from './terminalTestFixtures';

// Synthetic auction rows only. These values are never a saved market response.
export const auctionDay = '2026-10-26';
export const auctionDays = [auctionDay, '2026-10-23', '2026-10-22', '2026-10-21'];
export const auctionInstrument = '00000000-0000-4000-8000-000000000901';
export const auctionCode = '999901.SZ';

export function liveAuctionFixture(query: URLSearchParams, after0925 = false, rollingIncrement = 0): WorkbenchData {
  const base = terminalFixture(query);
  const day = query.get('day') || auctionDay;
  const template = base.stocks[0];
  const currentReceipt = after0925 ? '09:25:06' : rollingIncrement ? '09:20:56' : '09:20:46';
  const rowsFor = (rowDay: string): Stock[] => Array.from({ length: 10 }, (_, index) => ({
    ...template, code: `9999${String(index + 1).padStart(2, '0')}.SZ`, name: `合成封单${String(index + 1).padStart(2, '0')}`,
    instrument_id: `00000000-0000-4000-8000-${String(index + 901).padStart(12, '0')}`,
    theme: null, previous_height: index===0?2:index===1?1:null,
    auction_pct: rowDay === auctionDay ? null : 9.95,
    auction_indicative_pct: rowDay === auctionDay ? 9.95 - index / 10 : null,
    auction_current_seal_cny: rowDay === auctionDay && index !== 8 ? (after0925 ? 20 : 12.5 + rollingIncrement) * 100_000_000 - index * 100_000_000 : null,
    auction_price: rowDay === auctionDay ? null : 10.99,
    auction_amount_cny: rowDay === auctionDay ? null : 12_300_000,
    volume_ratio: null,
    node_amounts: {
      '09:15': (index + 1) * 100_000_000,
      '09:20': (12 - index) * 100_000_000,
      '09:25': rowDay === auctionDay && !after0925 ? null : (20 - index) * 100_000_000,
    },
    node_received_at: `${rowDay}T${rowDay === auctionDay ? currentReceipt : '09:25:06'}+08:00`,
    node_source: rowDay === auctionDay ? '短线侠当前表' : '短线侠历史表',
    auction_tags: ['合成机器人', '合成视觉'], auction_board: index === 0 ? '3板' : '首板',
  }));
  const auction_days: WorkbenchData['auction_days'] = auctionDays.filter(value => value <= day).map(rowDay => ({
    day: rowDay, items: rowsFor(rowDay), node_status: 'available',
    node_source: rowDay === auctionDay ? '短线侠当前表' : '短线侠历史表',
    node_received_at: `${rowDay}T${rowDay === auctionDay ? currentReceipt : '09:25:06'}+08:00`,
    one_word_count: 5, header_total_cny: 7_400_000_000,
  }));
  return { ...base, view: 'auction', trade_day: day, available_days: auctionDays,
    mode: query.has('as_of_time') ? 'observed' : day === auctionDay ? 'live' : 'review',
    latest_at: auction_days[0]?.node_received_at ?? null,
    stocks: [], sectors: [], selected_members: [], selected_sector_code: null,
    selected_stock_code: auctionCode, auction_days,
  };
}
