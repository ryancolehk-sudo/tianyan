import type { CalendarPayload, EvidenceVersion, RuntimeSource } from '../../generated/schema';
import { shiftDay } from './auctionFormat';
import { allowedSource } from './auctionWorkbenchModel';

/** Read one complete collected version per exchange; gaps/conflicts stay unknown. */
export function previousCollectedSession(day: string, items: EvidenceVersion[], sources: RuntimeSource[]) {
  const groups = new Map<string, { exchange: string; received: string; days: Map<string, boolean>; conflict: boolean }>();
  for (const item of items) {
    const source = sources.find(s => s.provider.id === item.source_id);
    if (item.kind !== 'calendar' || item.visibility !== 'shared' || item.provenance !== 'source_reported'
      || item.is_current === false || !source?.provider.enabled || !allowedSource(source)) continue;
    const payload = item.payload as CalendarPayload;
    if (!['SSE', 'SZSE'].includes(payload.exchange) || !/^\d{4}-\d{2}-\d{2}$/.test(payload.trade_day)
      || typeof payload.is_open !== 'boolean' || !payload.calendar_version) continue;
    const key = [item.source_id, payload.exchange, payload.calendar_version].join('|');
    let group = groups.get(key);
    if (!group) { group = { exchange: payload.exchange, received: item.received_at, days: new Map(), conflict: false }; groups.set(key, group); }
    group.received = group.received > item.received_at ? group.received : item.received_at;
    if (group.days.has(payload.trade_day)) group.conflict = true;
    group.days.set(payload.trade_day, payload.is_open);
  }
  const calendars = ['SSE', 'SZSE'].map(exchange => [...groups.values()]
    .filter(g => g.exchange === exchange && g.days.has(day)).sort((a, b) => b.received.localeCompare(a.received))[0]);
  if (calendars.some(c => !c || c.conflict)) return null;
  for (let offset = 0; offset <= 14; offset++) {
    const candidate = shiftDay(day, -offset), open = calendars.map(c => c.days.get(candidate));
    if (open.some(v => v === undefined) || open[0] !== open[1] || (!offset && !open[0])) return null;
    if (offset && open[0]) return candidate;
  }
  return null;
}
