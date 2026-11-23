import type { WorkbenchData, TerminalView, Value } from './types';

import { request } from '../../shared/api';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { peekRead } from '../../shared/readCache';

export type WorkbenchEnvelope = { data: WorkbenchData; meta?: { request_id?: string; read_cache?: { revision?: string; state?: string; built_at?: string; validated_at?: string } } };
export const workbenchPath = (query: URLSearchParams) => '/api/v1/workbench-view?' + query;
export const loadWorkbench = (query: URLSearchParams) => request<WorkbenchEnvelope>(workbenchPath(query));
const numberFormats = new Map<number, Intl.NumberFormat>();
const clockFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false });
const dateFormat = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
export const number = (value: Value | undefined, digits = 0) => {
  if (value == null || !Number.isFinite(value)) return '—';
  let format = numberFormats.get(digits);
  if (!format) { format = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits }); numberFormats.set(digits, format); }
  return format.format(value);
};
export const percent = (value: Value | undefined) => value == null || !Number.isFinite(value) ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
export const money = (value: Value | undefined, digits = 2) => value == null || !Number.isFinite(value) ? '—' : number(value / 100_000_000, digits);
export const signedMoney = (value: Value | undefined, digits = 1) => value == null || !Number.isFinite(value) ? '—' : `${value > 0 ? '+' : ''}${money(value, digits)}`;
export const tone = (value: Value | undefined) => value == null ? 'ty-neutral' : value > 0 ? 'ty-up' : value < 0 ? 'ty-down' : 'ty-neutral';
export const time = (value: string | null | undefined) => {
  if (!value) return '—';
  if (/[T ]\d{2}:\d{2}.*(?:Z|[+-]\d{2}:?\d{2})$/.test(value)) {
    const instant = new Date(value);
    if (!Number.isNaN(instant.getTime())) return clockFormat.format(instant);
  }
  const match = /(?:T|\s)(\d{2}:\d{2})/.exec(value);
  return match?.[1] ?? (/^\d{2}:\d{2}/.test(value) ? value.slice(0, 5) : value);
};
export const shortDay = (value: string) => value ? value.slice(5).replace('-', '/') : '待采集';
export const dateTime = (value: string | null | undefined) => {
  if (!value) return '—';
  const instant = new Date(value);
  if (/[T ]\d{2}:\d{2}.*(?:Z|[+-]\d{2}:?\d{2})$/.test(value) && !Number.isNaN(instant.getTime())) {
    return dateFormat.format(instant);
  }
  return value.replace('T', ' ').slice(0, 19);
};

export function useWorkbench(view: TerminalView, day: string, stock: string, _sector: string, owner: string, instrument = '', asOfTime = '') {
  // Selection is local UI state. Only market context changes reload the page data.
  const key = new URLSearchParams({ view, ...(day ? { day } : {}), ...(!stock && instrument ? { instrument_id: instrument } : {}), ...(asOfTime ? { as_of_time: asOfTime } : {}) }).toString();
  const path = '/api/v1/workbench-view?' + key;
  const read = useCachedLoader(() => request<WorkbenchEnvelope>(path, undefined, { active: document.visibilityState === 'visible' }), `${owner}:${key}`, { overview: '综合盯盘', auction: '竞价观察', ladder: '连板观察', funds: '资金动向' }[view]);
  const envelope = read.data?.data != null ? read.data : peekRead<WorkbenchEnvelope>(path, true);
  return { data: envelope?.data ?? null, readPath: path, readMeta: envelope?.meta,
    busy: envelope?.data == null && read.busy, error: read.error?.message ?? '', reload: read.reload };
}

export function emptyWorkbench(view: TerminalView, day: string): WorkbenchData {
  return { view, trade_day: day, available_days: [], mode: 'review', latest_at: null, indices: [],
    breadth: { up: null, down: null, flat: null, limit_up: null, limit_down: null, comparable: null, scope: '等待实际数据', points: [] },
    stocks: [], sectors: [], selected_stock_code: null, selected_sector_code: null, auction_days: [], selected_members: [],
    funds_history: [], broken: [], events: [], stock_seals: [], capabilities: [] };
}
