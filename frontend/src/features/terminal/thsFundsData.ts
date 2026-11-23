import { useEffect, useState } from 'react';
import type { IntradayFundSector, IntradayFunds, WorkbenchData } from './types';

export type ThsCategory = 'industry' | 'concept';
export type ThsSnapshot = {
  sampled_day: string; status: string; received_at: string | null; complete: boolean;
  missing_pages: number[]; rejected_rows: number; last_error: string | null;
  cross_page_duplicates?: number; recovered_pages?: number[]; history_truncated?: boolean;
  rows: { code: string; name: string; net_cny: string | null; pct_change: string | null;
    company_count: string | null; received_at: string }[];
  history: { received_at: string; points: { code: string; net_cny: string | null; received_at: string }[] }[];
};
export type ThsReads = Partial<Record<ThsCategory, { data: ThsSnapshot | null; error: string }>>;
export const thsCode = (category: ThsCategory, code: string) => `THS:${category}:${code}`;
const categories: ThsCategory[] = ['industry', 'concept'];
const numeric = (value: string | null) => value != null && value.trim() !== '' && Number.isFinite(Number(value)) ? Number(value) : null;

// Read saved data only. A refresh failure retains the same context; permission
// errors and a change of date/cutoff/user never reuse another context's values.
export function useThsFunds(enabled: boolean, day: string, cutoff: string, user: string) {
  const key = `${enabled}:${day}:${cutoff}:${user}`;
  const [state, setState] = useState<{key: string; reads: ThsReads}>({key, reads: {}});
  useEffect(() => {
    if (!enabled) return;
    let active = true, authorizationBlocked = false;
    const busy: Partial<Record<ThsCategory, boolean>> = {};
    const attempted: Partial<Record<ThsCategory, number>> = {};
    const intervals = {industry:15_000,concept:30_000};
    const controller = new AbortController();
    setState({key, reads: {}});
    async function load(category: ThsCategory) {
      if (busy[category] || !active || authorizationBlocked) return;
      busy[category] = true; attempted[category] = Date.now();
        try {
          const query = new URLSearchParams({category});
          if (day) query.set('sampled_day', day);
          if (cutoff) query.set('as_of_time', cutoff);
          const response = await fetch('/api/v1/ths-funds?' + query, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
          if (!response.ok) {
            if (response.status === 401 || response.status === 403) {
              authorizationBlocked = true;
              if (active) setState({key, reads: Object.fromEntries(categories.map(item=>[item,{data:null,error:'同花顺资金读取权限已失效'}]))});
              return;
            }
            throw new Error('同花顺资金读取失败，保留本范围上次记录');
          }
          const value = await response.json();
          if (active && !authorizationBlocked) setState(previous => ({key, reads: {...previous.reads, [category]: {data: value.data, error: ''}}}));
        } catch (error) {
          if (active && !authorizationBlocked) setState(previous => ({key, reads: {...previous.reads, [category]: {
            data: previous.key === key ? previous.reads[category]?.data ?? null : null,
            error: error instanceof Error ? error.message : '资金读取失败',
          }}}));
        } finally { busy[category] = false; }
    }
    categories.forEach(category=>void load(category));
    const refresh = () => {
      const today = new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai'}).format(new Date());
      if (document.hidden || cutoff || day && day !== today || authorizationBlocked) return;
      categories.forEach(category=>{ if (Date.now()-(attempted[category] ?? 0)>=intervals[category]) void load(category); });
    };
    const timer = setInterval(refresh, 1_000);
    document.addEventListener('visibilitychange',refresh);
    return () => { active = false; controller.abort(); clearInterval(timer); document.removeEventListener('visibilitychange',refresh); };
  }, [enabled, day, cutoff, user, key]);
  return state.key === key ? state.reads : {};
}

// This is a view adapter for the existing funds layout, not a supplier merge.
// Never borrow Eastmoney members, daily flows, clocks, or strength statistics.
export function projectThsFunds(reads: ThsReads, requestedDay: string, fallbackDay = '') {
  const day = requestedDay || reads.industry?.data?.sampled_day || reads.concept?.data?.sampled_day || fallbackDay;
  const sectors: IntradayFundSector[] = [];
  const points: IntradayFunds['points'] = [];
  const coverage: NonNullable<IntradayFunds['coverage']> = [];
  const notes: Partial<Record<ThsCategory, string>> = {};
  for (const category of categories) {
    const read = reads[category];
    const snapshot = read?.data?.sampled_day === day && read.data.status !== 'disabled' ? read.data : null;
    const rows = snapshot?.rows ?? [];
    rows.forEach(row => sectors.push({code: thsCode(category, row.code), name: row.name,
      main_net_cny: numeric(row.net_cny), pct_change: numeric(row.pct_change), amount_cny: null,
      rank: null, member_count: numeric(row.company_count), category, industry_level: null,
      source_time: null, received_at: row.received_at, change_5m_cny: null, rank_change_5m: null}));
    snapshot?.history.forEach(batch => batch.points.forEach(point => points.push({
      sector_code: thsCode(category, point.code), time: point.received_at, value: numeric(point.net_cny),
    })));
    coverage.push({category, represented_count: rows.length, expected_count: snapshot?.complete ? rows.length : null,
      latest_batch_count: rows.length, complete: snapshot?.complete ?? false, source_time: null,
      received_at: snapshot?.received_at ?? null, latest_batch_source_time: null, latest_batch_received_at: snapshot?.received_at ?? null});
    notes[category] = [read?.error, snapshot?.status === 'disabled' ? '来源尚未启用或权限不可用' : '',
      snapshot?.missing_pages.length ? `缺第 ${snapshot.missing_pages.join('、')} 页` : '',
      snapshot?.rejected_rows ? `${snapshot.rejected_rows} 条未纳入` : '',
      snapshot?.cross_page_duplicates ? `${snapshot.cross_page_duplicates} 条跨页重复，保留较新观察` : '',
      snapshot?.history_truncated ? '当日记录超出本次读取范围' : '',
      snapshot?.last_error ? '最近采集有缺项或失败，保留已存数据' : '',
      snapshot?.status === 'stale' ? '较早采样，等待更新' : ''].filter(Boolean).join(' · ');
  }
  const received = coverage.map(item => item.received_at).filter((value): value is string => !!value)
    .sort((a,b) => Date.parse(a) - Date.parse(b)).at(-1) ?? null;
  return {reads,day,sectors,points,coverage,notes,received};
}

export function withThsFunds(base: WorkbenchData, reads: ThsReads, selected: string, requestedDay: string,
  prepared?: ReturnType<typeof projectThsFunds>): WorkbenchData {
  const expectedDay = requestedDay || reads.industry?.data?.sampled_day || reads.concept?.data?.sampled_day || base.trade_day;
  const {day,sectors,points,coverage,notes,received} = prepared?.reads === reads && prepared.day === expectedDay
    ? prepared : projectThsFunds(reads,requestedDay,base.trade_day);
  return {...base, selected_sector_code: selected || null, selected_members: [], members_trade_day: null,
    funds_history: [], funds_trade_day: null,
    capabilities: [...base.capabilities.filter(item => item.key !== 'intraday_funds').map(item =>
      ['sector_funds', 'stock_funds', 'members'].includes(item.key)
        ? {...item, status: 'missing', source_family: 'ths_board_funds', last_received_at: null,
          reason: '当前同花顺板块资金表不含此项，未用其他分类补齐。'} : item),
      {key: 'intraday_funds', label: '同花顺资金接收采样', status: sectors.length ? 'partial' : 'missing',
        reason: '行情时刻、精确5分钟变化及成员映射尚缺；仅展示实际接收记录。',
        source_family: 'ths_board_funds', last_received_at: received}],
    sectors: sectors.map(row => ({...row, main_net_cny: null})),
    sector_strength: sectors.map(row => ({code: row.code, name: row.name, category: row.category ?? null,
      industry_level: null, trade_day: day, members_day: null, member_count: row.member_count,
      limit_up_count: null, quoted_count: 0, partial: true, source_time: null, latest_source_time: null,
      received_at: row.received_at ?? null, basis: 'intraday'})),
    intraday_funds: {state: sectors.length ? 'available' : 'missing', trade_day: day, source_family: 'ths_board_funds',
      source_name: '同花顺公开资金表', metric: '资金净额', time_basis: 'received_at', category_notes: notes,
      latest_source_time: null, received_at: received, sectors, points, stocks: [], coverage,
      scope: '同花顺资金净额，非东方财富主力净额。按实际接收时间采样，来源未提供行情日期和时刻；午间接收点显示在压缩午休轴上。没有精确同源5分钟前值时不计算变化。成分、涨停家数及日频历史尚缺，不借其他分类拼接。'},
  };
}
