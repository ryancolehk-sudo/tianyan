import type { Metrics } from './model';

const bands = [
  ['首板', '#E02D32'], ['二板', '#D5252E'], ['三板', '#C71E2A'], ['四板', '#B81926'],
  ['五板', '#A41423'], ['六板', '#8F101F'], ['七板', '#7B0D1D'], ['七板以上', '#650A18'],
] as const;

function count(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

export function getBoardRatios(metrics: Metrics) {
  const pool = metrics.limits?.supplier_reported;
  const result = bands.map(([label, color], index) => ({ label, color, index, count: 0, percentage: 0 }));
  const missing = () => ({ state: 'missing' as const, bands: result, total: null, unknown: null });
  if (!pool || !count(pool.up) || !pool.ladder || typeof pool.ladder !== 'object' || Array.isArray(pool.ladder)) return missing();
  for (const [height, value] of Object.entries(pool.ladder)) {
    const n = Number(height);
    if (!/^[1-9]\d*$/.test(height) || !Number.isSafeInteger(n) || !count(value)) return missing();
    result[Math.min(n, 8) - 1].count += value;
  }
  const known = result.reduce((sum, band) => sum + band.count, 0);
  if (!Number.isSafeInteger(known) || known > pool.up) return missing();
  const unknown = pool.up - known;
  if (pool.height_missing_n != null && (!count(pool.height_missing_n) || pool.height_missing_n !== unknown)) return missing();
  for (const band of result) band.percentage = pool.up ? band.count / pool.up * 100 : 0;
  return { state: !pool.up ? 'empty' as const : unknown ? 'partial' as const : 'complete' as const,
    bands: result, total: pool.up, unknown };
}

export function boardPercentage(value: number) {
  return `${value.toFixed(1).replace(/\.0$/, '')}%`;
}

export const boardRatioTitle = '按当日非ST短线池收盘涨停家数计算。依次为首板、二板、三板、四板、五板、六板、七板、七板以上（八板起）；大红色逐档加深，七板以上为深红色。点击查看完整家数与占比。';
