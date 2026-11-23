import type { Page } from '@playwright/test';
import type { DirectionBoardData, DirectionRankPanel, DirectionRankRow } from '../../src/features/daily-review/directionBoardTypes';
import { blank, day, meta } from './reviewTestFixtures';

// These records intentionally use synthetic names and a mocked read protocol.
// They validate UI identity and missingness, never supplier availability.
export const researchDay = '2026-10-23';
export const initialDays = ['2026-10-23', '2026-10-22', '2026-10-21', '2026-10-20', '2026-10-19'];
const olderDays = ['2026-10-16', '2026-10-15', '2026-10-14', '2026-10-13', '2026-10-12'];
export function panel(date: string, label: string, rows: DirectionRankRow[]): DirectionRankPanel {
  return { trade_day: date, requested_day: date, status: 'ready', source_label: label, source_time: `${date}T15:00:00+08:00`, received_at: '2026-10-26T10:00:00+08:00', reason: null, rows, total_count: 100 };
}
export function data(kind = 'concept', older = false): DirectionBoardData {
  const isConcept = kind === 'concept';
  const prefix = isConcept ? '合成概念' : '合成行业';
  const codePrefix = isConcept ? 'C' : 'I';
  const stocks = Array.from({ length: 10 }, (_, i) => ({ rank: i + 1, code: `60000${i}.SH`, name: `合成人气${i}`, rank_change: i === 2 ? null : i - 2, pct_change: i===0?'0':i===1?'15.26':i===2?null:'-1.25', quote_state:i===2?'missing':'daily_close',quote_time:researchDay+'T15:00:00+08:00',quote_received_at:researchDay+'T15:02:00+08:00',quote_source:'合成独立行情' }));
  return { trade_day: researchDay, sector_type: isConcept ? 'concept' : 'industry', previous_trade_day: '2026-10-22',
    days: (older ? olderDays : initialDays).map((date, index) => panel(date, '合成板块日线', Array.from({ length: 10 }, (_, i) => {
      const identity = index === 1 ? i === 0 ? 1 : i === 1 ? 0 : i : i;
      return { rank: i + 1, code: codePrefix + identity, name: `${prefix}${identity}`, pct_change: String(6 - i / 2 - index / 10) };
    }))),
    funds: panel('2026-10-22', '合成板块资金', Array.from({ length: 10 }, (_, i) => ({ rank: i + 1, code: codePrefix + i, name: `${prefix}${i}`, net_amount: String((10 - i) * 100_000_000) }))),
    popularity: {
      ths: panel(researchDay, '合成同花顺来源', stocks),
      tdx: panel(researchDay, '合成通达信来源', stocks.map((row, i) => ({ ...row, rank: i === 0 ? 2 : i === 1 ? 1 : i + 1 }))),
      eastmoney: panel(researchDay, '合成东方财富来源', stocks.map((row, i) => ({ ...row, rank: i === 0 ? 3 : i === 2 ? 1 : i + 1 }))),
    },
    strength: panel(researchDay, '合成开盘啦来源', Array.from({ length: 10 }, (_, i) => ({ rank: i + 1, code: 'K' + i, name: `合成题材${i}`, strength: String(10000 - i * 500) }))),
  };
}
function deferred() { let release!: () => void; const promise = new Promise<void>(resolve => { release = resolve; }); return { promise, release }; }
export async function fixture(page: Page) {
  const state = { error: false, mode: 'ready', zeroChanges: false, historicalRanks: false, currentDay: '', currentPending: false, queryDays: [] as URLSearchParams[], writes: [] as string[], holdIndustry: null as ReturnType<typeof deferred> | null, errors: [] as string[] };
  page.on('pageerror', error => state.errors.push(error.message));
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url()); const path = url.pathname;
    const ok = (value: unknown) => route.fulfill({ json: { meta, data: value } });
    if (route.request().method() !== 'GET') state.writes.push(path);
    if (path.endsWith('/session')) return ok({ user: { id: 'direction-synthetic-owner', username: 'synthetic', role: 'admin' }, csrf_token: 'synthetic-only' });
    if (path.endsWith('/direction-data')) {
      state.queryDays.push(url.searchParams);
      if (state.mode === 'malformed') return ok({ items: [] });
      const snapshot = data(url.searchParams.get('sector_type') || 'concept', url.searchParams.has('end_day'));
      if (state.currentDay && url.searchParams.get('automatic_current') === 'true') {
        snapshot.trade_day = url.searchParams.get('trade_day') || state.currentDay;
        snapshot.snapshot_day = state.currentDay; snapshot.automatic_current = true;
        const previousDay = state.currentDay === '2026-10-27' ? '2026-10-26' : '2026-10-23';
        snapshot.funds_day = previousDay;
        snapshot.funds = { ...snapshot.funds, trade_day: previousDay, requested_day: previousDay };
        if (!url.searchParams.has('end_day')) snapshot.days = [state.currentDay, previousDay, ...initialDays.filter(date => date < previousDay).slice(0, 3)].map((date, index) => ({ ...snapshot.days[index], trade_day: date, requested_day: date, source_time: null,
          ...(index === 0 ? { status: state.currentPending ? 'missing' as const : 'partial' as const, rows: state.currentPending ? [] : snapshot.days[index].rows, reason: state.currentPending ? '今日槽位已预留，等待当日资料' : '今日尚未收盘，已存盘中样本' } : {}) }));
        const current = (value: DirectionRankPanel): DirectionRankPanel => ({ ...value, trade_day: state.currentPending ? null : state.currentDay, requested_day: state.currentDay, source_time: null,
          status: state.currentPending ? 'missing' : 'ready', rows: state.currentPending ? [] : value.rows, reason: state.currentPending ? '今日槽位待填' : null });
        for (const key of ['ths', 'tdx', 'eastmoney'] as const) snapshot.popularity[key] = current(snapshot.popularity[key]);
        snapshot.strength = current(snapshot.strength);
      }
      if (state.historicalRanks) {
        snapshot.popularity.eastmoney.rank_change_basis = 'previous_trading_day';
        snapshot.popularity.eastmoney.rank_comparison_day = '2026-10-22';
        snapshot.popularity.eastmoney.source_time = null;
        snapshot.popularity.eastmoney.rows.forEach(row => { row.rank_change = 0; });
        snapshot.popularity.eastmoney.reason = '历史日频人气榜，比较上一交易日';
      }
      if (state.zeroChanges) {
        snapshot.popularity.ths.rank_change_basis = 'source_reported';
        snapshot.popularity.ths.rows.forEach(row => { row.rank_change = 0; });
        snapshot.popularity.tdx.rank_change_basis = 'not_provided';
        snapshot.popularity.tdx.rows.forEach(row => { row.rank_change = null; });
        snapshot.popularity.eastmoney.status = 'stale';
        snapshot.popularity.eastmoney.reason = '后续采集失败，保留已保存的排名';
      }
      if (state.holdIndustry && snapshot.sector_type === 'industry') await state.holdIndustry.promise;
      if (state.error) return route.fulfill({ status: 503, json: { error: { message: '合成榜单读取失败' } } });
      if (state.mode === 'gaps') {
        snapshot.days[1] = { ...snapshot.days[1], status: 'missing', rows: [], source_time: null, received_at: null, reason: '合成历史日尚未保存' };
        snapshot.days[3].rows = snapshot.days[3].rows.slice(0, 2);
        snapshot.popularity.ths = { ...snapshot.popularity.ths, trade_day: '2026-10-26', status: 'partial', source_time: null, reason: '最新保存人气快照；不是所选日的收盘排名' };
        snapshot.popularity.tdx = { ...snapshot.popularity.tdx, status: 'error', source_time: null, rows: [], reason: '合成通达信来源暂不可用' };
        snapshot.strength = { ...snapshot.strength, trade_day: '2026-10-22', status: 'stale', reason: '来源返回较早日期' };
      }
      if (state.mode === 'empty' || url.searchParams.has('as_of_time')) {
        const missing = (value: DirectionRankPanel): DirectionRankPanel => ({ ...value, trade_day: null, rows: [], status: 'missing', source_time: null, received_at: null, reason: '该时点尚未接收' });
        snapshot.days = snapshot.days.map(missing); snapshot.funds = missing(snapshot.funds); snapshot.strength = missing(snapshot.strength);
        for (const key of ['ths', 'tdx', 'eastmoney'] as const) snapshot.popularity[key] = missing(snapshot.popularity[key]);
      }
      return ok(snapshot);
    }
    if(path.endsWith('/direction-data/sector-atlas')) return ok(atlas(url.searchParams));
    if (path.endsWith('/review-data/day')) return ok(day(url.searchParams.get('trade_day') || url.searchParams.get('as_of_time')?.slice(0, 10) || state.currentDay || researchDay));
    if (path.endsWith('/review-data/series')) return ok({ shared_date: researchDay, trade_days: initialDays.slice().reverse(), records: [], state: 'partial' });
    if (path.endsWith('/review-data/notes')) return ok({ trade_day: researchDay, content: blank(), head_id: null, writable: true, revision: null });
    if (path.endsWith('/watchlist')) return ok({ items: [] });
    return ok({ items: [], next_cursor: null });
  });
  return state;
}
export function atlas(params:URLSearchParams) {
  const source=params.get('source')||'kpl',end=params.get('trade_day')||researchDay;
  const dates:string[]=[];const cursor=new Date(end+'T00:00:00Z');
  while(dates.length<Number(params.get('lookback')||20)){if(![0,6].includes(cursor.getUTCDay()))dates.push(cursor.toISOString().slice(0,10));cursor.setUTCDate(cursor.getUTCDate()-1);}
  const names=['光伏','AI应用','机器人概念','芯片','三季报增长','通信','算力','锂电池','商业航天','绿色电力'];
  return {source,days:dates.map((d,i)=>{
    const rows=Array.from({length:10},(_,j)=>({rank:j+1,code:(source==='kpl'?'K':'T')+((j+i)%10),name:names[(j+i)%10],strength:String(9500-j*700-i*50),pct_change:String(4.5-j*.48),amount:String((10-j)*1e8),amount_unit:'元'}));
    if(i===1){const r=rows.find(r=>r.code===(source==='kpl'?'K1':'T1'))!;r.strength='0';r.amount='0';}
    const leaders=Object.fromEntries(rows.map(r=>[r.code,{coverage:'complete',rows:Array.from({length:5},(_,k)=>({label:['龙一','龙二','龙三','龙四','龙五'][k],name:['合成领涨甲','合成领涨乙','合成领涨丙','合成领涨丁','合成领涨戊'][k],stock_code:'60000'+k+'.SH'}))}]));
    return {...panel(d,'合成'+(source==='kpl'?'开盘啦':'同花顺'),i===2?[]:rows),details:i===2?[]:rows,archive_kind:'daily',status:i===2?'missing':'ready',top_ten_complete:i!==2,leaders:i===2?{}:leaders};
  }),limitations:['合成测试数据，不代表真实市场或来源可用性']};
}
export async function openRotation(page:Page,suffix='') {await page.goto('/research/reviews?view=direction&trade_day='+researchDay+suffix);}
