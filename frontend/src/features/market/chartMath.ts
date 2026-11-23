export function movingAverage(values: number[], window: number): (number | null)[] {
  return values.map((_, index) => {
    if (index + 1 < window) return null;
    const sample = values.slice(index + 1 - window, index + 1);
    return sample.every(Number.isFinite) ? sample.reduce((sum, value) => sum + value, 0) / window : null;
  });
}

function ema(values: (number | null)[], window: number): (number | null)[] {
  let previous: number | null = null;
  let seed: number[] = [];
  return values.map(value => {
    if (value === null || !Number.isFinite(value)) { previous = null; seed = []; return null; }
    if (previous === null) {
      seed.push(value);
      if (seed.length < window) return null;
      previous = seed.reduce((sum, item) => sum + item, 0) / window;
    } else previous += 2 / (window + 1) * (value - previous);
    return previous;
  });
}

// EMA uses the initial N-close SMA seed. Histogram = 2 * (DIF - DEA).
export function calculateMacd(values: number[]) {
  const fast = ema(values, 12), slow = ema(values, 26);
  const dif = values.map((_, index) => fast[index] === null || slow[index] === null ? null : fast[index]! - slow[index]!);
  const dea = ema(dif, 9);
  return { dif, dea, histogram: dif.map((value, index) => value === null || dea[index] === null ? null : 2 * (value - dea[index]!)) };
}

type VolumeRow = { trade_day?: string | null; source_clock_at?: string | null; bar_end_at?: string | null; volume_shares: string | null; amount_cny?: string | null };
export function reliableVwap(rows: VolumeRow[], partial: boolean): (number | null)[] | null {
  if (!rows.length || partial) return null;
  let shares = 0, amount = 0;
  let previous = 0;
  const day = rows[0].trade_day;
  const output: (number | null)[] = [];
  for (const [index, row] of rows.entries()) {
    const clock = Date.parse(row.source_clock_at ?? row.bar_end_at ?? '');
    const time = new Date(clock + 8 * 3600_000);
    const minute = time.getUTCHours() * 60 + time.getUTCMinutes();
    if (!Number.isFinite(clock) || !day || row.trade_day !== day || row.volume_shares === null || row.amount_cny === null || row.amount_cny === undefined
      || !/^\d+$/.test(row.volume_shares) || !/^\d+(\.\d+)?$/.test(row.amount_cny)) return null;
    const volume = Number(row.volume_shares), cash = Number(row.amount_cny);
    if (!Number.isSafeInteger(volume) || !Number.isFinite(cash) || (volume === 0 && cash !== 0) || (volume > 0 && cash <= 0)) return null;
    if (index === 0 && minute !== 570 && minute !== 571) return null;
    if (index && clock - previous !== 60_000) {
      const prevTime = new Date(previous + 8 * 3600_000);
      const prevMinute = prevTime.getUTCHours() * 60 + prevTime.getUTCMinutes();
      if (!(prevMinute === 690 && (minute === 780 || minute === 781) && clock - previous <= 91 * 60_000)) return null;
    }
    shares += volume; amount += cash; previous = clock;
    if (!Number.isSafeInteger(shares) || !Number.isFinite(amount)) return null;
    output.push(shares ? amount / shares : null);
  }
  return output;
}
