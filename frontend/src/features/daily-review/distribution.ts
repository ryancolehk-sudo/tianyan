import type { Metrics } from './model';

// Scheme D. Keep these in semantic order instead of relying on JSON key order.
export const distributionBands = [
  { key: 'limit_down', label: '跌停', title: '实际跌停' },
  { key: 'down_ge7', label: '跌≥7%', title: '其余跌幅 ≥7%' },
  { key: 'down_5_7', label: '跌5–7%', title: '跌幅 [5%,7%)' },
  { key: 'down_3_5', label: '跌3–5%', title: '跌幅 [3%,5%)' },
  { key: 'down_0_3', label: '跌0–3%', title: '跌幅 (0%,3%)' },
  { key: 'flat', label: '平盘', title: '平盘 = 0%' },
  { key: 'up_0_3', label: '涨0–3%', title: '涨幅 (0%,3%)' },
  { key: 'up_3_5', label: '涨3–5%', title: '涨幅 [3%,5%)' },
  { key: 'up_5_7', label: '涨5–7%', title: '涨幅 [5%,7%)' },
  { key: 'up_ge7', label: '涨≥7%', title: '其余涨幅 ≥7%' },
  { key: 'limit_up', label: '涨停', title: '实际涨停' },
] as const;

export const legacyDistributionBands = [
  { key: 'r<=-7', label: '≤−7%', title: '涨跌幅 ≤−7%' },
  { key: '-7<r<=-5', label: '(−7,−5%]', title: '涨跌幅 (−7%,−5%]' },
  { key: '-5<r<=-3', label: '(−5,−3%]', title: '涨跌幅 (−5%,−3%]' },
  { key: '-3<r<0', label: '(−3%,0)', title: '涨跌幅 (−3%,0)' },
  { key: 'r=0', label: '平盘', title: '平盘 = 0%' },
  { key: '0<r<3', label: '(0,3%)', title: '涨跌幅 (0,3%)' },
  { key: '3<=r<5', label: '[3,5%)', title: '涨跌幅 [3%,5%)' },
  { key: '5<=r<7', label: '[5,7%)', title: '涨跌幅 [5%,7%)' },
  { key: 'r>=7', label: '≥7%', title: '涨跌幅 ≥7%' },
] as const;

export function hasDetailedDistribution(metrics: Metrics) {
  return metrics.breadth?.distribution?.version === 'absolute-limits-v1';
}

export function distributionColor(index: number, count: number) {
  const middle = (count - 1) / 2;
  const t = Math.abs(index - middle) / middle;
  const neutral = [115, 129, 143]; // #73818F
  const endpoint = index < middle ? [74, 178, 185] : [209, 110, 155];
  return '#' + neutral.map((v, i) => Math.round(v + (endpoint[i] - v) * t).toString(16).padStart(2, '0')).join('');
}

// Reference 2 applies to the histogram only; retain D for the numeric table cells.
export function histogramColor(index: number, count: number) {
  const colors = ['#00F55F', '#02F35F', '#0FDD61', '#00DC54', '#00D050', '#88939A',
    '#FF3B2F', '#FE332B', '#FE2C25', '#FE2621', '#FF1B1A'];
  return colors[Math.round(index / (count - 1) * (colors.length - 1))];
}

export function getDistribution(metrics: Metrics, detailed = hasDetailedDistribution(metrics)) {
  const bands = detailed ? distributionBands : legacyDistributionBands;
  const source = detailed ? metrics.breadth?.distribution?.bins : metrics.breadth?.bins;
  const bins = bands.map((b, i) => {
    const value = source?.[b.key];
    return { ...b, value: typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null,
      color: distributionColor(i, bands.length) };
  });
  const total = bins.reduce((sum, b) => sum + (b.value ?? 0), 0);
  const valid = detailed ? metrics.breadth?.distribution?.valid_n : metrics.breadth?.valid_n;
  const complete = bins.every(b => b.value !== null) && (valid == null || total === valid);
  const unverified = detailed ? metrics.breadth?.distribution?.limit_unverified_n ?? 0 : 0;
  const note = detailed
    ? unverified > 0 ? `封板状态待核 ${unverified} 只，仍按实际涨跌幅分档。` : '按实际限制价识别涨跌停，其余按 3%、5%、7% 分档。'
    : '当前为原九档统计，未拆分真实涨跌停。';
  return { bins, total, complete, detailed, note };
}
