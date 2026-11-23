// Display coordinates only. Source timestamps and stored observations stay intact.
// This is not a trading calendar and cannot establish that a date was open.
export type MinuteTimeAxis = {
  kind: 'ashare_sessions' | 'source_time';
  positions: number[];
  elapsedMinutes: number[];
  spanMinutes: number;
  ticks: { position: number; label: string }[];
  reason: 'unsupported_instrument' | 'multiple_days' | 'outside_sessions' | 'invalid_clock' | null;
};
type Identity = { exchange: string; asset_type?: string };
const shanghaiClock = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai',
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
const ticks = [{ position: 0, label: '09:30' }, { position: 60, label: '10:30' },
  { position: 120, label: '11:30 / 13:00' }, { position: 180, label: '14:00' }, { position: 240, label: '15:00' }];
function clockParts(clock: string) {
  const date = new Date(clock);
  if (!Number.isFinite(date.getTime())) return null;
  const parts = shanghaiClock.formatToParts(date);
  const value = (type: Intl.DateTimeFormatPartTypes) => parts.find(part => part.type === type)?.value ?? '';
  return { day: (['year', 'month', 'day'] as const).map(value).join('-'),
    minute: Number(value('hour')) * 60 + Number(value('minute')) + Number(value('second')) / 60 + date.getUTCMilliseconds() / 60_000 };
}
export function minuteTimeAxis(clocks: string[], identity: Identity | undefined, observationDay?: string | null, intervalMinutes = 1): MinuteTimeAxis {
  const first = new Date(clocks[0] ?? '').getTime();
  const raw = clocks.map(clock => (new Date(clock).getTime() - first) / 60_000);
  const fallback = (reason: MinuteTimeAxis['reason']): MinuteTimeAxis => ({ kind: 'source_time', positions: raw, elapsedMinutes: raw,
    spanMinutes: Math.max(1, raw.at(-1) ?? 0), ticks: [], reason });
  if (!clocks.length) return fallback(null);
  // Other assets/exchanges may follow different sessions. Do not infer their rules.
  if (!identity || identity.asset_type !== 'stock' || !['SSE', 'SZSE'].includes(identity.exchange)) return fallback('unsupported_instrument');
  const parts = clocks.map(clockParts);
  if (parts.some(part => part === null)) return fallback('invalid_clock');
  const day = observationDay ?? parts[0]!.day;
  if (parts.some(part => part!.day !== day)) return fallback('multiple_days');
  if (parts.some(part => !(part!.minute >= 570 && part!.minute <= 690 || part!.minute >= 780 && part!.minute <= 900))) return fallback('outside_sessions');
  const elapsedMinutes = parts.map(part => part!.minute <= 690 ? part!.minute - 570 : part!.minute - 660);
  // A source may label both 11:30 and 13:00. Keep both selectable instead of
  // painting one on top of the other. Give their candle centers a small
  // visible separation; this display-only spacer changes no clock or gap test.
  const spacer = parts.some(part => part!.minute === 690) && parts.some(part => part!.minute === 780) ? Math.min(3, Math.max(1, intervalMinutes)) : 0;
  return { kind: 'ashare_sessions', elapsedMinutes,
    positions: elapsedMinutes.map((minute, i) => minute + (parts[i]!.minute >= 780 ? spacer : 0)),
    spanMinutes: 240 + spacer,
    ticks: ticks.map(tick => ({ ...tick, position: tick.position + (tick.position > 120 ? spacer : tick.position === 120 ? spacer / 2 : 0) })),
    reason: null };
}
