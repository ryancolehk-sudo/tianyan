import { updateLocation } from '../../shared/navigation';
import { useEffect, useState } from 'react';
import { request } from '../../shared/api';
import { HistoryPlot } from '../../shared/HistoryPlot';
import { Failure, Loading, useBoundRead } from './components';
import { number, pct, amount } from './model';
import type { Envelope } from './model';
import type { Rotation } from './analysisTypes';
import type { DirectionSectorType } from './directionBoardTypes';

function unavailable(status: string, subject: string) {
  if (status === 'error') return `当日${subject}来源异常`;
  if (status === 'disabled') return `当日${subject}来源未启用或无权读取`;
  if (status === 'partial') return `当日${subject}资料未齐备`;
  return `当日${subject}资料缺失`;
}

export function RotationHistory({ day, asOf, kind, sector, choose, onRange }: {
  day: string; asOf: string; kind: DirectionSectorType; sector: { code: string; name: string } | null;
  choose: (code: string, name: string) => void;
  onRange?: (lookback: number) => void;
}) {
  const query = new URLSearchParams(location.search);
  const [open, setOpen] = useState(!!query.get('rotation_days')), [lookback, setLookback] = useState(query.get('rotation_days')==='60'?'60':'20'), [metric, setMetric] = useState(query.get('rotation_metric') ?? 'rank');
  useEffect(() => { onRange?.(open ? Number(lookback) : 5); }, [open,lookback]);
  const params = new URLSearchParams({ trade_day: day, sector_type: kind, lookback, ...(sector ? { sector_code: sector.code } : {}), ...(asOf ? { as_of_time: asOf } : {}) });
  const read = useBoundRead(`rotation:${params}:${open}`, () => open ? request<Envelope<Rotation>>('/api/v1/direction-data?' + params) : Promise.resolve(null));
  const data = read.data?.data;
  const options = [...new Map((data?.records ?? []).flatMap(r => r.top_ten).map(r => [r.code,r])).values()];
  if (sector && !options.some(s => s.code === sector.code)) options.unshift(sector);
  const points = (data?.records ?? []).map(r => ({ day:r.trade_day, values:[metric==='rank'?r.rank:metric==='pct'?number(r.pct_change):number(r.net_amount)===null?null:number(r.net_amount)!/100_000_000] }));
  return <details className="review-extension" onToggle={e => setOpen(e.currentTarget.open)}><summary>历史展开 · 板块轮动与排名轨迹</summary>{open && <div className="review-extension-body"><div className="review-extension-tools">{['20','60'].map(n=><button key={n} aria-pressed={lookback===n} onClick={()=>setLookback(n)}>近{n}日</button>)}<label>选中板块 <select aria-label="轮动历史板块" value={sector?.code ?? ''} onChange={e=>{const s=options.find(o=>o.code===e.target.value);if(s)choose(s.code,s.name);}}><option value="">请选择板块</option>{options.map(s=><option key={s.code} value={s.code}>{s.name}</option>)}</select></label>{[['rank','完整日榜名次'],['pct','当日涨幅'],['funds','当日资金（亿元）']].map(([k,label])=><button key={k} aria-pressed={metric===k} onClick={()=>setMetric(k)}>{label}</button>)}</div>
    {read.busy?<Loading/>:read.error?<Failure error={read.error} retry={read.reload}/>:<><p className="history-plot-note">请求 {lookback} 日，实际日历 {data?.coverage.calendar_days ?? 0} 日，完整排名 {data?.coverage.ranking_days ?? 0} 日，资金 {data?.coverage.funds_days ?? 0} 日。</p>{sector && <HistoryPlot points={points} labels={[sector.name]} select={d=>{const url=new URL(location.href);url.searchParams.set('trade_day',d);url.searchParams.set('sector',sector.code);url.searchParams.set('rotation_name',sector.name);url.searchParams.set('rotation_days',lookback);url.searchParams.set('rotation_kind',kind);url.searchParams.set('rotation_metric',metric);updateLocation(url);}} reverse={metric==='rank'} unit={metric==='rank'?'名':metric==='pct'?'%':'亿元'}/>}
    <div className="review-extension-scroll"><table aria-label="板块轮动历史明细"><thead><tr><th>交易日</th><th>所选板块真实名次</th><th>涨幅</th><th>当日资金</th><th>当日前十</th></tr></thead><tbody>{[...(data?.records ?? [])].reverse().map(r=><tr key={r.trade_day}><td>{r.trade_day}</td><td>{!sector?'待选板块':r.rank!==null?`${r.rank} / ${r.total_count}${r.in_top_ten?'':' · 未入前十'}`:r.rank_complete || r.selection_state==='not_disclosed' ? r.total_count===0?'当日完整空榜':'当日未披露该板块':unavailable(r.status,'排名')}</td><td>{pct(r.pct_change)}</td><td>{!sector ? '待选板块' : number(r.net_amount)!==null ? <>{amount(r.net_amount)}{r.funds_status!=='ready' && !r.funds_complete && <small> · {unavailable(r.funds_status,'资金')}</small>}</> : r.funds_complete ? '当日资金榜未披露该板块' : unavailable(r.funds_status,'资金')}</td><td>{r.top_ten.map(s=><button key={s.code} onClick={()=>choose(s.code,s.name)}>{s.name}</button>)}{!r.top_ten.length&&(r.rank_complete?'完整日榜无披露记录':unavailable(r.status,'排名'))}</td></tr>)}</tbody></table></div><p className="history-plot-note">完整已保存日榜计算名次；本区资金对应各行当日，原页面右侧仍对应研究日的上一交易日。</p></>}
  </div>}</details>;
}
