/** Elapsed display time on the compressed Shanghai trading axis. */
export function observationGapSeconds(previous: string, current: string) {
  const first = Date.parse(previous), last = Date.parse(current);
  if (!Number.isFinite(first) || !Number.isFinite(last) || last < first) return Infinity;
  const dayMs = 86_400_000, offset = 8 * 3_600_000;
  const day = Math.floor((first + offset) / dayMs);
  if (day !== Math.floor((last + offset) / dayMs)) return Infinity;
  const lunchStart = day * dayMs + 3.5 * 3_600_000;
  const lunchEnd = day * dayMs + 5 * 3_600_000;
  const lunch = Math.max(0, Math.min(last, lunchEnd) - Math.max(first, lunchStart));
  return (last - first - lunch) / 1000;
}
