import type { MarketSession } from '../../shared/marketSession';
import type { IntradayFunds, IntradayFundSector } from './types';

// Only supplier clocks describe the chart's coverage. A recent HTTP response
// or an unrelated source receipt must never make an old curve look current.
export function latestSourceTime(values: (string | null | undefined)[]) {
  return values.filter((value): value is string => !!value && /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)
    && Number.isFinite(Date.parse(value))).sort((a, b) => Date.parse(a) - Date.parse(b)).at(-1);
}

// Selected details keep their own coverage. Choose the whole newer row without
// inserting an outside object into the bounded ranking or merging its clocks.
export function latestFundSector(funds: IntradayFunds | null | undefined, code: string | null | undefined, day: string) {
  if (!funds || !code || funds.trade_day !== day) return undefined;
  const ranked = funds.sectors.find(item => item.code === code);
  const focused = funds.focused_sectors?.find(item => item.code === code);
  const rankedAt = latestSourceTime([ranked?.source_time]), focusedAt = latestSourceTime([focused?.source_time]);
  return focused && focusedAt && (!rankedAt || Date.parse(focusedAt) > Date.parse(rankedAt)) ? focused : ranked;
}

export function missingClosingSample(day: string | null | undefined, source: string | null | undefined, market: MarketSession | null, freshnessSeconds = 120) {
  return !!day && market?.is_trading_day === true && market.phase === 'closed' && market.trade_day === day
    && (!source || !Number.isFinite(Date.parse(source)) || Date.parse(source) < Date.parse(`${day}T15:00:00+08:00`) - freshnessSeconds * 1000);
}

// The shared batch clock may belong to another category or stock table. Judge
// only the curves actually displayed, including a selected object outside Top10.
export function fundChartCoverage(funds: IntradayFunds | null | undefined, sectors: IntradayFundSector[], day: string | null | undefined) {
  const empty = { sourceTime: undefined as string | undefined, latestRankTime: undefined as string | undefined,
    curveTime: undefined as string | undefined, closingComplete: false };
  if (!funds || !day || funds.trade_day !== day || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return empty;
  const start = Date.parse(`${day}T00:00:00+08:00`), close = Date.parse(`${day}T15:00:00+08:00`);
  const end = start + 86_400_000;
  const clock = (value: string | null | undefined) => {
    const valid = latestSourceTime([value]);
    return valid && Date.parse(valid) >= start && Date.parse(valid) < end ? valid : undefined;
  };
  const codes = new Set(sectors.map(row => row.code)), lastPoints = new Map<string, string>();
  for (const point of funds.points) {
    const stamp = clock(point.time);
    if (!codes.has(point.sector_code) || point.value == null || !Number.isFinite(point.value) || !stamp
      || Date.parse(stamp) >= close + 60_000) continue;
    const previous = lastPoints.get(point.sector_code);
    if (!previous || Date.parse(stamp) > Date.parse(previous)) lastPoints.set(point.sector_code, stamp);
  }
  const ranks = sectors.map(row => clock(row.source_time));
  const curves = sectors.map(row => lastPoints.get(row.code));
  const known = [...ranks, ...curves].filter((stamp): stamp is string => !!stamp)
    .sort((a, b) => Date.parse(a) - Date.parse(b));
  const atClose = (stamp: string | undefined) => !!stamp && Date.parse(stamp) >= close && Date.parse(stamp) < close + 60_000;
  return { sourceTime: known[0], latestRankTime: latestSourceTime(ranks), curveTime: latestSourceTime(curves),
    closingComplete: funds.state === 'available' && sectors.length > 0 && sectors.every((row, i) =>
      row.main_net_cny != null && Number.isFinite(row.main_net_cny) && atClose(ranks[i]) && atClose(curves[i])) };
}
