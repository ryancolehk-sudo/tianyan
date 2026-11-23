import type { DatasetItem } from '../../generated/schema';
import { AUCTION_STAGES } from './auctionFormat';
import type { AuctionStage } from './auctionFormat';
import { assembleVendorBatches, vendorAmountValue } from './auctionVendorBatches';
import type { VendorBatch, VendorMoney, VendorStock } from './auctionVendorBatches';

/** Compare canonical Decimal strings without losing precision to Number. */
export function decimalCompare(a: string, b: string): number {
  const parts = (s: string) => {
    const negative = s.startsWith('-');
    const [whole, fraction = ''] = s.replace(/^[+-]/, '').split('.');
    const integer = whole.replace(/^0+/, '') || '0';
    const decimal = fraction.replace(/0+$/, '');
    return { negative: negative && (integer !== '0' || !!decimal), integer, decimal };
  };
  const x = parts(a), y = parts(b);
  if (x.negative !== y.negative) return x.negative ? -1 : 1;
  const length = Math.max(x.decimal.length, y.decimal.length);
  const result = x.integer.length - y.integer.length || x.integer.localeCompare(y.integer)
    || x.decimal.padEnd(length, '0').localeCompare(y.decimal.padEnd(length, '0'));
  return x.negative ? -result : result;
}
export function validDecimal(value: string | null | undefined): value is string {
  return typeof value === 'string' && /^[+-]?\d+(?:\.\d+)?$/.test(value);
}
export function compactMoney(value: VendorMoney) {
  if (value.raw !== null) return value.raw;
  if (!validDecimal(value.cny)) return '—';
  const n = Number(value.cny);
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + '元';
}
/** A published column is chosen once for the whole table; missing rows stay missing. */
export function latestStage(batch: VendorBatch): AuctionStage | null {
  return [...AUCTION_STAGES].reverse().find(stage => batch.summary?.totals[stage]?.cny !== null
    && batch.summary?.totals[stage]?.cny !== undefined || batch.stocks.some(row => validDecimal(row.amounts[stage].cny))) ?? null;
}
export function sortStocks(rows: VendorStock[], stage: AuctionStage | null, direction = '降序') {
  return [...rows].sort((a, b) => {
    const x = stage ? a.amounts[stage].cny : null, y = stage ? b.amounts[stage].cny : null;
    const xv = validDecimal(x), yv = validDecimal(y);
    if (xv !== yv) return xv ? -1 : 1;
    return (xv && yv ? decimalCompare(x, y) * (direction === '升序' ? 1 : -1) : 0)
      || (a.sourceOrder ?? 0) - (b.sourceOrder ?? 0) || a.code.localeCompare(b.code);
  });
}
export function allVendorBatches(items: DatasetItem[]) {
  const days = [...new Set(items.map(item => item.payload.observed_day).filter((d): d is string => !!d))];
  return days.flatMap(day => assembleVendorBatches(items, day).batches);
}
export function matchesStock(row: VendorStock, query: string) {
  const text = query.trim().toLowerCase();
  return !text || [row.code, row.name, row.tagsRaw, row.boardRaw].some(v => v?.toLowerCase().includes(text));
}
export function allowedSource(source: { collection_state: string; provider: { rights?: Record<string, string>; entitlement_status: string } } | undefined) {
  return !!source && source.collection_state !== 'permission_missing' && source.provider.entitlement_status === 'verified'
    && source.provider.rights?.display === 'allowed' && source.provider.rights?.cache === 'allowed';
}
export function tableForDay(items: DatasetItem[], family: string, dataset: string, day: string) {
  // Select the latest entire record first: do not resurrect an old nonempty version.
  const record = items.filter(i => i.family === family && i.payload.dataset === dataset)
    .sort((a, b) => Date.parse(b.received_at) - Date.parse(a.received_at))[0];
  if (!record) return null;
  const date = day.replaceAll('-', '');
  const rows = record.payload.rows;
  // Refuse duplicate identities and mixed dates, instead of silently patching rows.
  if (!rows.length || rows.some(r => r.trade_date !== date || !/^\d{6}\.(SH|SZ|BJ)$/.test(r.ts_code ?? ''))
    || new Set(rows.map(r => r.ts_code)).size !== rows.length) return null;
  return record;
}
export { vendorAmountValue };
