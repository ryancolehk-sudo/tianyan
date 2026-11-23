import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import type { IntradayFunds, IntradayFundSector } from '../../src/features/terminal/types';

// Run the actual pure helper with synthetic DTOs. No page, HTTP or database.
let loaded: Promise<typeof import('../../src/features/terminal/sourceTiming')> | undefined;
function helpers() {
  return loaded ??= readFile('src/features/terminal/sourceTiming.ts', 'utf8').then(source => {
    const output = ts.transpileModule(source, { compilerOptions: {
      target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
    } }).outputText;
    return import('data:text/javascript;base64,' + Buffer.from(output).toString('base64'));
  });
}

const day = '2026-10-30';
const at = (clock: string) => `${day}T${clock}+08:00`;
function row(code = 'SYN-I', clock: string | null = at('15:00:00')): IntradayFundSector {
  return { code, name: 'synthetic', category: 'industry', industry_level: 2, main_net_cny: 0,
    pct_change: null, amount_cny: null, rank: 1, member_count: null, source_time: clock };
}
function fixture(sectors: IntradayFundSector[] = [row()]): IntradayFunds {
  return { state: 'available', trade_day: day, source_family: 'synthetic-only', source_name: 'synthetic',
    latest_source_time: at('15:00:00'), received_at: at('15:03:00'), scope: 'synthetic; not real market data',
    sectors, stocks: [], points: sectors.map(item => ({ sector_code: item.code, time: item.source_time!, value: item.main_net_cny })) };
}

test('another category and a stock clock cannot close an old industry curve', async () => {
  const { fundChartCoverage } = await helpers();
  const industry = row('SYN-I', at('14:59:00')), concept = { ...row('SYN-C'), category: 'concept' };
  const funds = fixture([industry, concept]);
  funds.stocks = [{ code: 'SYN-STOCK', source_time: at('15:00:00'), value: 3 }];
  expect(fundChartCoverage(funds, [industry], day)).toMatchObject({ sourceTime: at('14:59:00'), curveTime: at('14:59:00'), closingComplete: false });
  expect(fundChartCoverage(funds, [concept], day)).toMatchObject({ sourceTime: at('15:00:00'), closingComplete: true });
});

test('every visible curve needs its own close sample even within one category', async () => {
  const { fundChartCoverage } = await helpers();
  const sectors = [row(), row('SYN-OLD', at('14:59:00'))], funds = fixture(sectors);
  expect(fundChartCoverage(funds, sectors, day)).toMatchObject({ sourceTime: at('14:59:00'), closingComplete: false });
});

test('the selected object outside Top10 participates without inserting it into the ranking', async () => {
  const { fundChartCoverage } = await helpers();
  const funds = fixture(), selected = row('SYN-FOCUS', at('14:40:00'));
  funds.focused_sectors = [selected];
  funds.points.push({ sector_code: selected.code, time: selected.source_time!, value: 0 });
  expect(fundChartCoverage(funds, [...funds.sectors, selected], day)).toMatchObject({ sourceTime: at('14:40:00'), closingComplete: false });
  expect(fundChartCoverage(funds, funds.sectors, day).closingComplete).toBe(true);
  expect(funds.sectors.map(item => item.code)).toEqual(['SYN-I']);
});

for (const missing of ['rank clock', 'curve', 'curve value', 'rank value'] as const) {
  test(`a missing ${missing} cannot borrow the global close clock`, async () => {
    const { fundChartCoverage } = await helpers(), funds = fixture();
    if (missing === 'rank clock') funds.sectors[0].source_time = null;
    if (missing === 'curve') funds.points = [];
    if (missing === 'curve value') funds.points[0].value = null;
    if (missing === 'rank value') funds.sectors[0].main_net_cny = null;
    expect(fundChartCoverage(funds, funds.sectors, day).closingComplete).toBe(false);
  });
}

for (const invalid of ['2026-10-29T15:00:00+08:00', '2026-10-31T15:00:00+08:00', '2026-10-30T15:00:00', 'invalid'] as const) {
  test(`an old, future or unknown clock stays incomplete: ${invalid}`, async () => {
    const { fundChartCoverage } = await helpers(), funds = fixture([row('SYN-I', invalid)]);
    expect(fundChartCoverage(funds, funds.sectors, day)).toMatchObject({ sourceTime: undefined, closingComplete: false });
  });
}

test('old-day metadata and an empty category cannot be made complete by another table', async () => {
  const { fundChartCoverage } = await helpers(), funds = fixture();
  expect(fundChartCoverage({ ...funds, trade_day: '2026-10-29' }, funds.sectors, day).closingComplete).toBe(false);
  expect(fundChartCoverage(funds, [], day)).toMatchObject({ sourceTime: undefined, closingComplete: false });
});

test('actual close samples, zero and equivalent UTC clocks remain valid', async () => {
  const { fundChartCoverage } = await helpers(), funds = fixture([row('SYN-I', `${day}T07:00:00Z`)]);
  expect(fundChartCoverage(funds, funds.sectors, day)).toMatchObject({ sourceTime: `${day}T07:00:00Z`, closingComplete: true });
});

test('a close ranking without an actual closing curve point stays incomplete', async () => {
  const { fundChartCoverage } = await helpers(), funds = fixture();
  funds.points[0].time = at('14:59:00');
  expect(fundChartCoverage(funds, funds.sectors, day)).toMatchObject({ sourceTime: at('14:59:00'), closingComplete: false });
});

test('an unrelated or later receipt and an after-close point cannot fabricate a close point', async () => {
  const { fundChartCoverage } = await helpers(), funds = fixture();
  funds.points = [{ sector_code: 'SYN-OTHER', time: at('15:00:00'), value: 1 },
    { sector_code: 'SYN-I', time: at('15:01:00'), value: 2 }];
  expect(fundChartCoverage(funds, funds.sectors, day).closingComplete).toBe(false);
});

for (const state of ['stale', 'missing', 'permission_denied'] as const) {
  test(`the existing ${state} batch still cannot claim complete coverage`, async () => {
    const { fundChartCoverage } = await helpers(), funds = fixture();
    funds.state = state;
    expect(fundChartCoverage(funds, funds.sectors, day).closingComplete).toBe(false);
  });
}

test('coverage calculation preserves the supplied data and genuine gaps', async () => {
  const { fundChartCoverage } = await helpers(), funds = fixture();
  funds.points.unshift({ sector_code: 'SYN-I', time: at('13:41:00'), value: null });
  const before = structuredClone(funds);
  expect(fundChartCoverage(funds, funds.sectors, day).closingComplete).toBe(true);
  expect(funds).toEqual(before);
});
