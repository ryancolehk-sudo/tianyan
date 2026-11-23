import type { RecoveryItem as GeneratedRecoveryItem, RecoveryStatus as GeneratedRecoveryStatus } from '../generated/schema';

export type HealthState = 'normal' | 'fallback' | 'recovering' | 'attention';
export type DataHealth = Omit<NonNullable<GeneratedRecoveryStatus['health']>[number], 'scope'> & { scope: Record<string, unknown> };
export type RecoveryItem = Omit<GeneratedRecoveryItem, 'scope' | 'missing_ranges'> & {
  scope: Record<string, unknown>;
  missing_ranges: { start: string; end: string; count: number }[];
};
export type RecoveryStatus = Omit<GeneratedRecoveryStatus, 'items' | 'workers' | 'health'> & {
  items: RecoveryItem[];
  workers?: { role: string; seen_at: string; progress_at: string | null; job_id: string | null }[];
  health?: DataHealth[];
};
export const healthLabels: Record<HealthState, string> = {
  normal: '正常', fallback: '备用来源运行', recovering: '正在恢复', attention: '需要处理',
};
export function healthState(value: unknown): HealthState | null {
  return typeof value === 'string' && Object.hasOwn(healthLabels, value) ? value as HealthState : null;
}
export function recoveryDay(value: string | null | undefined) {
  const date = value ? new Date(value) : null;
  return date && Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(date) : '';
}
export function checkedHealth(data: RecoveryStatus | undefined | null, selectedDay?: string) {
  const state = healthState(data?.health_state), today = recoveryDay(data?.server_time), target = selectedDay || today;
  const rows = Array.isArray(data?.health) ? data.health : [];
  // A summary without dated checks cannot establish that today's data is normal.
  return state && target && target <= today && rows.length && rows.every(item => item && item.trade_day === target && healthState(item.state)) ? state : null;
}
export const healthActions: Record<string, string> = {
  retry_source: '重试当前来源', use_backup: '使用已配置备用来源', probe_primary: '核验主来源是否恢复',
  repair_queue: '恢复任务队列', refresh_projection: '更新页面已存资料', reconcile_history: '核对并补齐历史资料',
  configure_source: '检查来源配置', wait_publication: '等待来源发布', wait_session: '等待对应交易时段', inspect_schema: '核查来源数据格式',
  inspect_runtime: '核查本机检查异常并自动复测',
};
