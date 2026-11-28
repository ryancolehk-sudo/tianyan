import { useCachedLoader } from '../../shared/useCachedLoader';
import { notifiedReadError } from '../../shared/BackgroundRefresh';
import type { PropsWithChildren, ReactNode } from 'react';
import { Icon } from '../../shared/icons';

export const kindNames = { model: 'AI 模型', market: '行情数据', news: '新闻资讯' };
const statuses: Record<string, string> = {
  success: '成功', complete: '完整返回', ready: '采集正常', normal: '调用正常', healthy: '调用正常',
  partial: '部分返回', empty: '无数据', error: '失败', failed: '失败', unknown: '结果未明',
  queued: '已排队', pending: '等待执行', running: '执行中', started: '执行中', generating: '正在生成',
  disabled: '未启用', standby: '待命', untested: '尚未检测', not_checked: '尚未检测',
  rate_limited: '请求受限', credentials_missing: '待填写 Key', stale: '配置已变更',
  permission_missing: '使用范围未核实', unavailable: '不可用', cancelled: '已取消',
  interrupted: '执行中断', invalidated: '配置已失效', succeeded: '成功', result_unknown: '结果未明',
  sending: '请求中', sent: '已发出', configuration_changed: '配置已变更',
  retry_wait: '等待重试', waiting: '等待采集条件', waiting_focus: '等待关注标的',
  collection_disabled: '总采集已关闭',
  credential_store_unavailable: '凭据存储不可用', credential_reference_invalid: '凭据引用无效', credential_unreadable: '凭据无法解密',
};
export const statusLabel = (state: string) => statuses[state] ?? state;
export function statusTone(state: string) {
  return ['success', 'succeeded', 'complete', 'ready', 'normal', 'healthy'].includes(state) ? 'good'
    : ['error', 'failed', 'unknown', 'result_unknown', 'interrupted', 'unavailable', 'credentials_missing', 'credential_store_unavailable', 'credential_reference_invalid', 'credential_unreadable'].includes(state) ? 'bad'
    : ['partial', 'rate_limited', 'running', 'started', 'queued', 'generating', 'retry_wait', 'stale'].includes(state) ? 'warn' : 'muted';
}
const reasons: Record<string, string> = {
  intraday_window_wait: '等待对应交易时段，批次尚未完成', source_budget_wait: '等待本机请求预算', local_request_wait: '等待共享请求许可',
  rate_limited: '供应商限制请求频率，等待重试', credentials_missing: '尚未配置有效凭据',
  credentials_invalid: '凭据无效，请核查配置', permission_missing: '当前使用范围或接口权限不足',
  unauthorized: '密钥无效或接口权限不足', authentication_failed: '密钥认证失败', model_not_found: '模型 ID 不存在或当前账号无权使用',
  timeout: '请求超时', outcome_unknown: '请求结果未明，未自动重发', worker_interrupted: '执行器中断，请检查后手动重新生成',
  upstream_unavailable: '上游服务暂不可用', connection_failed: '接口连接失败', invalid_response: '响应格式不符合所选协议',
  empty_response: '上游返回空内容', endpoint_rejected: '接口地址未通过检查', redirect_rejected: '接口返回重定向，请填写最终地址',
  source_disabled: '数据能力已关闭', collection_disabled: '本机总采集开关已关闭', schema_changed: '上游数据格式发生变化',
  calendar_not_ready: '交易日历不足，等待核验', source_binding_conflict: '数据能力已关联其他服务',
  credential_store_unavailable: '本机凭据存储不可用，请检查独立主密钥挂载',
  credential_reference_invalid: '已保存的凭据引用无效', credential_unreadable: '已保存的凭据无法解密，请检查配套主密钥或重新填写',
  snapshot_trade_date_unverified: '接口返回报价但缺少可核验交易时点',
  tdx_data_permission_missing: '请在通达信 Key 管理勾选数据服务权限', tdx_data_permission_required: '请在通达信 Key 管理勾选数据服务权限', tdx_data_key_required: '需填写数据服务 Key',
  tdx_dependency_missing: '本机数据组件尚未就绪', points_not_enabled: '尚未允许积分消耗',
  routing_changed: '来源配置已变化', route_not_selected: '该能力未选择此来源',
  credential_rejected: '密钥被拒绝', credentials_rejected: '密钥被拒绝',
  tdx_sdk_unavailable: '通达信数据服务暂不可用', tdx_sdk_timeout: '通达信数据服务请求超时',
  depth5_incomplete: '接口返回不足五档，尚未通过盘口核验', instrument_required: '请先配置采集标的',
  minute_day_out_of_range: '所选日期超出分钟接口支持范围', invalid_minute_cursor: '分钟补齐位置已失效',
  invalid_full_minute_context: '分钟补齐任务范围核验未通过', full_minute_not_selected: '请先检测并选择通达信全量分钟能力',
  collection_intent_conflict: '该来源已有其他范围任务，请查看运行监控', points_confirmation_required: '需明确允许本次积分请求',
};
export const reasonLabel = (value: string) => reasons[value] ?? statuses[value] ?? value;
export const dateTime = (value: string | null | undefined, short = false) => {
  if (!value) return '—';
  const dateOnly = /^(\d{4})-?(\d{2})-?(\d{2})$/.exec(value);
  if (dateOnly) return `${dateOnly[1]}-${dateOnly[2]}-${dateOnly[3]}（日期）`;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false,
    ...(short ? { hour: '2-digit', minute: '2-digit', second: '2-digit' } : { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }) });
};
export const duration = (ms: number | null | undefined) => ms == null ? '—' : ms < 1000 ? `${ms} 毫秒` : `${(ms / 1000).toFixed(1)} 秒`;
export const messageOf = (error: unknown) => error instanceof Error ? error.message : '操作未完成，请稍后重试。';

export function Panel({ title, children, action, className = '' }: PropsWithChildren<{ title: string; action?: ReactNode; className?: string }>) {
  return <section className={'dc-panel ' + className}><div className="dc-panel-heading"><h2>{title}</h2>{action}</div>{children}</section>;
}
export function Empty({ title, children }: PropsWithChildren<{ title: string }>) {
  return <div className="dc-empty"><Icon name="data"/><strong>{title}</strong>{children && <span>{children}</span>}</div>;
}
export function ErrorNotice({ error, retry, inline = false }: { error: unknown; retry?: () => void; inline?: boolean }) {
  if (!inline && notifiedReadError(error)) return null;
  return <div className="dc-alert dc-alert-error" role="alert"><Icon name="info"/><span>{messageOf(error)}</span>{retry && <button type="button" onClick={retry}>重试</button>}</div>;
}
export function Status({ value, label }: { value: string; label?: string }) {
  return <span className={'dc-status dc-' + statusTone(value)}><i/>{label ?? statusLabel(value)}</span>;
}
export function Toggle({ checked, onChange, disabled, label }: { checked: boolean; onChange: () => void; disabled?: boolean; label: string }) {
  return <button type="button" className={'dc-toggle' + (checked ? ' is-on' : '')} role="switch" aria-checked={checked} aria-label={label} disabled={disabled} onClick={onChange}><span/></button>;
}
const inactiveResource = () => Promise.resolve(null);

export function useResource<T>(load: () => Promise<T>, active = true) {
  const reader = active ? load : inactiveResource;
  const read = useCachedLoader<T | null>(reader, reader);
  return { ...read, data: read.data ?? null };
}
