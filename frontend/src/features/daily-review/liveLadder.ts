import type { Analysis } from './analysisTypes';
import type { IntradayLadder } from '../terminal/types';

export const liveLadderStatus: Record<string,string> = {
  sealed:'已封住', touched:'触板待核', opened:'开板', waiting:'待晋级', unknown:'行情待核',
};
export function ladderColumns(history: Analysis['records'], live: IntradayLadder | null | undefined, day: string, asOf: string): Analysis['records'] {
  if (!live || live.trade_day !== day || asOf) return history;
  const final = history.find(row => row.trade_day === day);
  if (live.phase === 'closed' && final?.stocks?.length && (final.state === 'complete' || final.basis === 'supplier_ladder')) return history;
  const stamp=live.source_time ? new Date(live.source_time).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : '待采样';
  const current: Analysis['records'][number] = {
    trade_day:day, state:'partial', basis:'intraday_ladder',
    coverage_note:`${live.phase==='closed'?'盘中末次观测':live.phase==='break'?'午休保留':live.phase==='auction'?'竞价中 · 待开盘':live.phase==='preopen'?'开盘前':'盘中观察'} · ${stamp}`,
    stocks:(live.rows??[]).map(row=>({ts_code:row.code,name:row.name,board_height:row.target_height,
      previous_height:row.previous_height,live_status:row.status,freshness:row.freshness,
      source_time:row.source_time,source_name:row.source_name,pct_chg:row.pct_change})),
  };
  return [...history.filter(row=>row.trade_day!==day),current];
}
