import type { Postclose } from '../../src/features/daily-review/postcloseVisuals';

// The overview is a separate read contract after ADR0101. These tests exercise
// the original research records; absent postclose figures stay absent.
export function researchOverviewFixture(selected: string, dates: readonly string[] = [selected]): Postclose {
  const tradeDays = [...new Set([...dates, selected])].filter(day => day <= selected).sort();
  return {
    version: 'postclose-v1', state: 'partial', trade_day: selected, trade_days: tradeDays,
    records: tradeDays.map(trade_day => ({ trade_day, state: 'unavailable', values: {}, leaders: { height: [] } })),
    rankings: {}, metric_definitions: [], coverage: [],
    scope: '合成研究交互测试；盘后数值未提供，不补零。',
    interval_basis: '合成测试，未提供区间排名。',
    schedule: { time: '16:30', late_refresh: '17:30' },
  };
}
