/**
 * Shared formatting for the auction page. A-type (eastmoney/tencent) and B-type
 * (duanxianxia) batches must present the same units and states, so the labels live
 * here instead of being duplicated per component.
 */

/** Only these three checkpoints exist in the frozen auction contract. */
export const AUCTION_STAGES = ['09:15', '09:20', '09:25'] as const;
export type AuctionStage = typeof AUCTION_STAGES[number];
export const AUCTION_PAGE_SIZE = 25;
/** Age after which a stored batch is labelled old instead of a current snapshot. */
export const AUCTION_OLD_AFTER_SECONDS = 6 * 3600;

export const batchStates: Record<string, string> = { available: '已配对', stale: '旧数据', unpaired: '配对缺失' };
export const sourceStates: Record<string, string> = {
  available: '最近采集成功', partial: '部分返回', empty: '尚无批次', stale: '旧缓存',
  disabled: '采集已关闭', permission_missing: '展示权限已撤回', error: '最近采集失败', unavailable: '来源不可用',
  not_checked: '尚未采集', credentials_missing: '本机凭据未配置', ready: '采集已就绪',
};
export const auctionReasons: Record<string, string> = {
  not_configured: '尚未配置来源', timeout: '上游超时', transport_error: '连接中断',
  upstream_unavailable: '上游不可用', rate_limited: '请求受限', schema_changed: '响应结构变化',
  decoder_changed: '公开解析脚本变化，已暂停', endpoint_not_allowed: '数据域名未核验，已暂停',
  instrument_unavailable: '标的暂无报价', permission_missing: '展示或缓存权限未核实',
  source_disabled: '来源未启用', configuration_changed: '配置在采集中变化',
};

export function today() {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
}
export function shiftDay(day: string, delta: number) {
  const [year, month, date] = day.split('-').map(Number);
  return new Date(Date.UTC(year, month - 1, date + delta)).toISOString().slice(0, 10);
}
export function stamp(value: string | null | undefined) {
  if (!value) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour12: false,
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(new Date(value));
}
export function clock(value: string | null | undefined) {
  if (!value) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(new Date(value));
}
export function ageText(seconds: number) {
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}
export function count(value: string | null | undefined) {
  if (value === null || value === undefined || value === '') return '—';
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : value;
}
export function percent(value: string | null) {
  return value === null || value === '' ? '—' : `${value}%`;
}
export function reasonText(value: string | null | undefined) {
  if (!value) return '';
  return auctionReasons[value] ?? value;
}
/** Whole seconds between a stored receive time and now; never negative. */
export function ageSeconds(receivedAt: string | null | undefined) {
  if (!receivedAt) return 0;
  const value = Date.parse(receivedAt);
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.floor((Date.now() - value) / 1000));
}
export function oldByAge(seconds: number) {
  return seconds > AUCTION_OLD_AFTER_SECONDS;
}
