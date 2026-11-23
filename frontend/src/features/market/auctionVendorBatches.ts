/**
 * B-type auction tables (duanxianxia current/history). The vendor returns one row per
 * trade day with three sealed-order columns (09:15 / 09:20 / 09:25), so this module
 * deliberately does not reuse the A-type AuctionBatch + checkpoint model. Summary and
 * stocks are paired only by source_id + batch_hash + trade_day; a missing side stays
 * unpaired and never produces invented totals or rows.
 */
import type { DatasetItem, ReferencePayload } from '../../generated/schema';
import { AUCTION_STAGES } from './auctionFormat';
import type { AuctionStage } from './auctionFormat';

export const VENDOR_SUMMARY_DATASET = 'market.duanxianxia.auction.summary';
export const VENDOR_STOCKS_DATASET = 'market.duanxianxia.auction.stocks';
export const VENDOR_FAMILIES = ['duanxianxia_auction', 'duanxianxia_auction_history'] as const;

export const VENDOR_FAMILY_LABEL: Record<string, string> = {
  duanxianxia_auction: '短线侠当前表',
  duanxianxia_auction_history: '短线侠历史表',
};
export const VENDOR_TABLE_KIND_LABEL: Record<string, string> = { current: '当前表', history: '历史表' };

const SUMMARY_COLUMNS = [
  'trade_day', 'source_date_raw', 'table_kind', 'row_count', 'one_word_count', 'header_total_raw', 'header_total_cny',
  ...AUCTION_STAGES.flatMap(stage => [`total_fd_${stage.replace(':', '')}_raw`, `total_fd_${stage.replace(':', '')}_cny`]),
  'batch_hash',
];
const STOCK_COLUMNS = [
  'trade_day', 'source_order', 'code', 'name', 'tags_raw', 'board_raw',
  ...AUCTION_STAGES.flatMap(stage => [`fd_${stage.replace(':', '')}_raw`, `fd_${stage.replace(':', '')}_cny`]),
  'change_pct_raw', 'change_pct', 'batch_hash',
];

type Cell = string | null;
type Row = Record<string, Cell>;

export type VendorMoney = { raw: Cell; cny: Cell };
export type VendorSummary = {
  tradeDay: Cell;
  sourceDateRaw: Cell;
  tableKind: Cell;
  rowCount: Cell;
  oneWordCount: Cell;
  headerTotalRaw: Cell;
  headerTotalCny: Cell;
  totals: Record<AuctionStage, VendorMoney>;
  missingColumns: string[];
};
export type VendorStock = {
  code: string;
  name: string;
  tagsRaw: Cell;
  boardRaw: Cell;
  sourceOrder: number | null;
  changePctRaw: Cell;
  changePct: Cell;
  amounts: Record<AuctionStage, VendorMoney>;
  missingColumns: string[];
};
export type VendorBatch = {
  key: string;
  family: string;
  sourceId: string;
  sourceName: string;
  batchHash: string;
  tradeDay: Cell;
  sourceDateRaw: Cell;
  tableKind: Cell;
  tableKindLabel: string;
  receivedAt: string;
  pendingStages: AuctionStage[];
  summary: VendorSummary | null;
  stocks: VendorStock[];
  state: 'paired' | 'unpaired';
  missing: 'summary' | 'stocks' | null;
  missingColumns: string[];
};

export type VendorAssembly = {
  batches: VendorBatch[];
  /** Items whose source row carried no trade day at all; never folded into a day view. */
  unknownDay: number;
  /** Items whose source key day and row day disagreed; never crossed into the day view. */
  mismatched: number;
};

function cell(row: Row | undefined, key: string): Cell {
  if (!row) return null;
  const value = row[key];
  return typeof value === 'string' && value !== '' ? value : null;
}

function stageKey(prefix: string, stage: AuctionStage) {
  return `${prefix}_${stage.replace(':', '')}`;
}

function money(row: Row, prefix: string, stage: AuctionStage): VendorMoney {
  return { raw: cell(row, stageKey(prefix, stage) + '_raw'), cny: cell(row, stageKey(prefix, stage) + '_cny') };
}

function missing(row: Row, columns: string[]) {
  return columns.filter(column => !(column in row));
}

function tableKindOf(value: Cell, family: string) {
  if (value === 'current' || value === 'history') return value;
  return family === 'duanxianxia_auction_history' ? 'history' : family === 'duanxianxia_auction' ? 'current' : '';
}

function parseSummary(row: Row, family: string): VendorSummary {
  return {
    tradeDay: cell(row, 'trade_day'),
    sourceDateRaw: cell(row, 'source_date_raw'),
    tableKind: tableKindOf(cell(row, 'table_kind'), family) || null,
    rowCount: cell(row, 'row_count'),
    oneWordCount: cell(row, 'one_word_count'),
    headerTotalRaw: cell(row, 'header_total_raw'),
    headerTotalCny: cell(row, 'header_total_cny'),
    totals: Object.fromEntries(AUCTION_STAGES.map(stage => [stage, money(row, 'total_fd', stage)])) as Record<AuctionStage, VendorMoney>,
    missingColumns: missing(row, SUMMARY_COLUMNS),
  };
}

function parseStocks(rows: Row[], day: string, batchHash: string): VendorStock[] {
  const seen = new Set<string>();
  const parsed: VendorStock[] = [];
  for (const row of rows) {
    if (cell(row, 'batch_hash') !== batchHash) continue;
    if (cell(row, 'trade_day') !== day) continue;
    const code = cell(row, 'code');
    if (!code || seen.has(code)) continue;
    seen.add(code);
    const order = Number(cell(row, 'source_order'));
    parsed.push({
      code,
      name: cell(row, 'name') ?? code,
      tagsRaw: cell(row, 'tags_raw'),
      boardRaw: cell(row, 'board_raw'),
      sourceOrder: Number.isFinite(order) ? order : null,
      changePctRaw: cell(row, 'change_pct_raw'),
      changePct: cell(row, 'change_pct'),
      amounts: Object.fromEntries(AUCTION_STAGES.map(stage => [stage, money(row, 'fd', stage)])) as Record<AuctionStage, VendorMoney>,
      missingColumns: missing(row, STOCK_COLUMNS),
    });
  }
  return parsed;
}

type Held = { item: DatasetItem; rows: Row[]; day: string };

function groups(item: DatasetItem) {
  const byHash = new Map<string, Row[]>();
  for (const row of item.payload.rows) {
    if (!row) continue;
    const hash = cell(row, 'batch_hash');
    if (!hash) continue;
    const list = byHash.get(hash) ?? [];
    list.push(row);
    byHash.set(hash, list);
  }
  return [...byHash.entries()];
}

/** The newest stored version wins when the same key appears twice. */
function hold(map: Map<string, Held>, key: string, held: Held) {
  const current = map.get(key);
  if (!current || Date.parse(held.item.received_at) >= Date.parse(current.item.received_at)) map.set(key, held);
}

function datasetOf(payload: ReferencePayload | undefined) {
  return payload?.dataset ?? '';
}

export function assembleVendorBatches(items: DatasetItem[], day: string): VendorAssembly {
  const summaries = new Map<string, Held>();
  const stocks = new Map<string, Held>();
  let unknownDay = 0;
  let mismatched = 0;

  for (const item of items) {
    if (!(VENDOR_FAMILIES as readonly string[]).includes(item.family)) continue;
    if (!Number.isFinite(Date.parse(item.received_at))) continue;
    const dataset = datasetOf(item.payload);
    const isSummary = dataset === VENDOR_SUMMARY_DATASET;
    const isStocks = dataset === VENDOR_STOCKS_DATASET;
    if (!isSummary && !isStocks) continue;

    for (const [hash, rows] of groups(item)) {
      const rowDay = cell(rows[0], 'trade_day');
      const attributed = item.payload.observed_day ?? rowDay;
      if (!attributed) { unknownDay++; continue; }
      if (attributed !== day) continue;
      if (rowDay && rowDay !== attributed) { mismatched++; continue; }
      const key = `${item.source_id}|${item.family}|${attributed}|${hash}`;
      hold(isSummary ? summaries : stocks, key, { item, rows, day: attributed });
    }
  }

  const batches: VendorBatch[] = [];
  for (const key of new Set([...summaries.keys(), ...stocks.keys()])) {
    const summaryHeld = summaries.get(key);
    const stocksHeld = stocks.get(key);
    const item = summaryHeld?.item ?? stocksHeld!.item;
    const hash = key.slice(key.lastIndexOf('|') + 1);
    const summary = summaryHeld ? parseSummary(summaryHeld.rows[0], item.family) : null;
    const stockRows = stocksHeld ? parseStocks(stocksHeld.rows, stocksHeld.day, hash) : [];
    // The vendor fills the next labelled column with a rolling value before
    // that checkpoint. A nonempty cell is not proof the checkpoint occurred.
    // Bind availability to BOTH stored records, never to the viewer's clock
    // alone: an old 09:21 batch must not turn into a 09:25 observation later.
    const receipts = [summaryHeld, stocksHeld].filter((held): held is Held => !!held).sort((a, b) => Date.parse(a.item.received_at) - Date.parse(b.item.received_at));
    const times = receipts.map(held => Date.parse(held.item.received_at));
    const cutoff = Math.min(Date.now(), ...times);
    const pendingStages = AUCTION_STAGES.filter(stage => {
      const checkpoint = Date.parse(`${day}T${stage}:00+08:00`);
      return !Number.isFinite(cutoff) || !Number.isFinite(checkpoint) || cutoff < checkpoint;
    });
    for (const stage of pendingStages) {
      if (summary) summary.totals[stage] = { raw: null, cny: null };
      for (const stock of stockRows) stock.amounts[stage] = { raw: null, cny: null };
    }
    const paired = summary !== null && stockRows.length > 0;
    const missingColumns = [...new Set([
      ...(summary?.missingColumns ?? []),
      ...stockRows.flatMap(row => row.missingColumns),
    ])];
    batches.push({
      key,
      family: item.family,
      sourceId: item.source_id,
      sourceName: item.source_name,
      batchHash: hash,
      tradeDay: summary?.tradeDay ?? (stocksHeld ? cell(stocksHeld.rows[0], 'trade_day') : null),
      sourceDateRaw: summary?.sourceDateRaw ?? null,
      tableKind: summary?.tableKind ?? null,
      tableKindLabel: VENDOR_TABLE_KIND_LABEL[summary?.tableKind ?? ''] ?? VENDOR_FAMILY_LABEL[item.family] ?? item.family,
      receivedAt: receipts[0].item.received_at,
      pendingStages,
      summary,
      stocks: stockRows,
      state: paired ? 'paired' : 'unpaired',
      missing: summary === null ? 'summary' : stockRows.length === 0 ? 'stocks' : null,
      missingColumns,
    });
  }

  batches.sort((left, right) => Date.parse(right.receivedAt) - Date.parse(left.receivedAt) || left.key.localeCompare(right.key));
  return { batches, unknownDay, mismatched };
}

/** Numeric comparison value for a sealed-order cell; null keeps missing values last. */
export function vendorAmountValue(money: VendorMoney) {
  if (money.cny === null) return null;
  const value = Number(money.cny);
  return Number.isFinite(value) ? value : null;
}
