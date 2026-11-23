import { useCallback, useEffect, useRef, useState } from 'react';
import type { DatasetItem, RuntimeSourceListResponse } from '../../generated/schema';
import { api, ApiError } from '../../shared/api';
import { VENDOR_FAMILIES, VENDOR_STOCKS_DATASET, VENDOR_SUMMARY_DATASET } from './auctionVendorBatches';
import { allowedSource } from './auctionWorkbenchModel';

const REFERENCES = ['tushare_limit_list_d', 'tushare_stk_auction'] as const;
/** A refresh swaps whole family responses together. No summary/stock read races. */
export function useAuctionCache(day: string, auto: boolean) {
  const [items, setItems] = useState<DatasetItem[]>([]);
  const [index, setIndex] = useState<RuntimeSourceListResponse | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(true);
  const [received, setReceived] = useState<string | null>(null);
  const epoch = useRef(0), inflight = useRef(false);
  const reload = useCallback(async () => {
    if (inflight.current) return;
    const id = ++epoch.current;
    inflight.current = true; setBusy(true);
    const families = [...VENDOR_FAMILIES, ...REFERENCES];
    const requests = families.map(async family => {
      if ((VENDOR_FAMILIES as readonly string[]).includes(family)) {
        const data = await Promise.all([
          api.datasets(VENDOR_SUMMARY_DATASET, family), api.datasets(VENDOR_STOCKS_DATASET, family),
          api.datasets(VENDOR_SUMMARY_DATASET, family, day), api.datasets(VENDOR_STOCKS_DATASET, family, day),
        ]);
        // Explicit-day requests may duplicate default rows; assembler handles versions.
        return data.flatMap(result => result.data.items).filter(item => item.family === family);
      }
      return (await api.datasets(family.replace('tushare_', 'tushare.'), family)).data.items;
    });
    const [status, ...results] = await Promise.allSettled([api.dataSources(), ...requests]);
    if (epoch.current !== id) return;
    const currentIndex = status.status === 'fulfilled' ? status.value as RuntimeSourceListResponse : null;
    setIndex(currentIndex);
    const failures: string[] = families.filter((_, i) => results[i].status === 'rejected');
    setItems(previous => families.flatMap((family, i) => {
      const result = results[i];
      const source = currentIndex?.data.items.find(s => s.family === family);
      if (source && !allowedSource(source)) return [];
      if (result.status === 'fulfilled') return (result.value as DatasetItem[]).filter(item => item.family === family);
      const explicitDenied = result.reason instanceof ApiError && [401, 403].includes(result.reason.status);
      // Retain a failed read only with freshly rechecked rights. Unknown is not permission.
      return !explicitDenied && allowedSource(source) ? previous.filter(i => i.family === family) : [];
    }));
    if (status.status === 'rejected') failures.push('连接状态');
    setErrors(failures); setReceived(new Date().toISOString()); setBusy(false); inflight.current = false;
  }, [day]);
  useEffect(() => {
    // Invalidate pending reads when dates change or the component unmounts.
    inflight.current = false; void reload();
    return () => { epoch.current++; inflight.current = false; };
  }, [reload]);
  useEffect(() => {
    if (!auto) return;
    const refresh = () => { if (document.visibilityState === 'visible') void reload(); };
    const timer = window.setInterval(refresh, 65_000);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, [auto, reload]);
  return { items, index, errors, busy, received, reload };
}
