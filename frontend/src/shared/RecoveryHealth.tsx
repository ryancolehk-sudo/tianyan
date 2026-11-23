import { createContext, useContext, useEffect } from 'react';
import type { PropsWithChildren } from 'react';
import { request } from './api';
import { useCachedLoader } from './useCachedLoader';
import { checkedHealth, healthLabels } from './dataRecovery';
import type { RecoveryStatus } from './dataRecovery';
import { monitorHref } from './dataMonitorLink';

type Response = { data: RecoveryStatus };
type RecoveryRead = { data: Response | undefined; busy: boolean; error: Error | null; reload: () => void };
const RecoveryContext = createContext<RecoveryRead | null>(null);
export const recoveryPath = '/api/v1/data-recovery?limit=100';

export function RecoveryHealthProvider({ children }: PropsWithChildren) {
  const read = useCachedLoader(async () => {
    const response = await request<Response>(recoveryPath);
    if (!response.data || !Array.isArray(response.data.items) || typeof response.data.server_time !== 'string'
      || response.data.health !== undefined && !Array.isArray(response.data.health)) throw new Error('运行状态暂未核实');
    return response;
  }, recoveryPath, '数据运行状态');
  useEffect(() => {
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') read.reload(); }, 30_000);
    return () => window.clearInterval(timer);
  }, [read.reload]);
  return <RecoveryContext.Provider value={read}>{children}</RecoveryContext.Provider>;
}
export function useRecoveryRead() { return useContext(RecoveryContext); }

export function RecoveryHealthIndicator() {
  const read = useRecoveryRead();
  const state = !read?.error && !read?.busy ? checkedHealth(read?.data?.data) : null;
  const label = read?.error ? '状态待核' : state ? healthLabels[state] : '尚未检查';
  return <a className="ty-health-indicator" data-health-state={state ?? 'unknown'} href={monitorHref({ context: 'current', returnTo: location.pathname + location.search + location.hash })} aria-label="数据运行状态"
    title={read?.error ? '运行状态读取失败，查看运行监控后重试' : '当前运行状态；历史回看仍使用原日期和资料截止'}>当前运行 · {label}</a>;
}
