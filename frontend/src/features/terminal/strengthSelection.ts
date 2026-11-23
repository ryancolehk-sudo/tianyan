import type { SectorStrength } from './types';

export const maxFundComparisons = 10;
export function rankStrength(rows: SectorStrength[]) {
  return [...rows].sort((a,b) => Number(a.limit_up_count == null) - Number(b.limit_up_count == null)
    || (b.limit_up_count ?? 0) - (a.limit_up_count ?? 0) || a.code.localeCompare(b.code));
}
export function toggleFundComparison(codes: string[], code: string): string[] {
  return codes.includes(code) ? codes.filter(value => value !== code)
    : codes.length < maxFundComparisons ? [...codes, code] : codes;
}
