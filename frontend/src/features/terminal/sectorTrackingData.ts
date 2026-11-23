import { useMemo } from 'react';
import { request } from '../../shared/api';
import { useCachedLoader } from '../../shared/useCachedLoader';
import type { SectorMembership } from '../../generated/schema';
import type { SectorMember, WorkbenchData } from './types';

export function useSectorTracking(data: WorkbenchData | null, sector: string, asOf: string, owner: string, enabled: boolean) {
  const params = new URLSearchParams({ day: data?.trade_day ?? '', sector,
    ...(asOf ? { as_of_time: asOf } : data?.detail_observation_cutoff
      ? { observation_cutoff: data.detail_observation_cutoff } : {}) });
  const usable = enabled && !!data?.trade_day && !!sector;
  const path = '/api/v1/sector-membership?' + params;
  const read = useCachedLoader(() => usable
    ? request<{ data: SectorMembership }>(path, undefined, { active: document.visibilityState === 'visible' })
    : Promise.resolve(null), owner + ':' + (usable ? path : 'sector-tracking-disabled'), '板块追踪');
  const result = usable ? read.data?.data : undefined;
  const membership = result?.sector_code === sector && result.trade_day === data?.trade_day ? result : undefined;
  const members = useMemo((): SectorMember[] | undefined => {
    if (!membership) return undefined;
    const quotes = new Map(data?.stocks.map(stock => [stock.code, stock]));
    return (membership.members ?? []).map(member => {
      const quote = quotes.get(member.code);
      return { code: member.code, name: quote?.name || member.name, instrument_id: quote?.instrument_id ?? null,
        pct_change: quote?.pct_change ?? null, speed_5m: quote?.speed_5m ?? null,
        speed_window_seconds: quote?.speed_window_seconds ?? null, source_time: quote?.source_time ?? null,
        price_basis: quote?.price_basis ?? null, source_received_at: quote?.source_received_at ?? null,
        source_snapshot_time: quote?.source_snapshot_time ?? null };
    });
  }, [data?.stocks, membership]);
  return { members, day: membership?.members_trade_day, count: membership?.member_count,
    truncated: membership?.truncated, busy: usable && read.busy,
    error: read.error?.message || (membership?.state === 'permission_denied' ? '板块成分来源不可用，请在数据中心核对。' : ''),
    reload: read.reload };
}
