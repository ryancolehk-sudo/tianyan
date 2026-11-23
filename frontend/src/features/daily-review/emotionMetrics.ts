import type { Metrics } from './model';

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

export function reducedLimitRatio(up: unknown, down: unknown) {
  const a = count(up), b = count(down);
  if (a === null || b === null || (!a && !b)) return '—';
  let x = a, y = b;
  while (y) [x, y] = [y, x % y];
  return `${a / x}:${b / x}`;
}

export function sealingRate(metrics: Metrics): number | null {
  const pool = metrics.limits?.supplier_reported;
  const up = count(pool?.up), broken = count(pool?.broken);
  return up !== null && broken !== null && up + broken > 0 ? up / (up + broken) * 100 : null;
}

export const sealRateTitle = '非ST短线池：收盘涨停 ÷（收盘涨停＋炸板），同日同池统计。';
export const yesterdayPerformanceTitle = '固定上一交易日涨停池，计算这些股票今日收盘涨跌幅的等权平均；缺失样本时不以已知子样本代替。';
export const limitRatioTitle = '非ST短线池收盘涨停家数 : 收盘跌停家数，约为最简整数比；一侧为0保留1:0或0:1，两侧为0显示—。';
export const openedLimitTitle = '沪深日线含ST：盘中触及当日真实限制价、收盘已打开；不包含收盘仍封板的股票。';
