import { useEffect, useRef } from 'react';
import { request } from '../../shared/api';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { useWorkspaceState } from '../../shared/workspaceState';
import { InteractiveChart, useChartDimensions } from './ChartInspection';
import { dateTime, number, time, tone } from './data';
import { sectorColors } from './Charts';
import { Empty, Panel } from './Components';
import { toggleFundComparison } from './strengthSelection';
import { KplStrengthRanks } from '../../shared/KplStrengthRanks';
import './kplStrengthSplit.css';
import { MarketDistribution, MarketEmotion } from './MarketEmotion';
import type { Stock } from './types';
import { useKplFocus } from './useKplFocus';

type StrengthRow = {code: string; name: string; strength: string | null; limit_up_count?: number | null; limit_up_received_at?: string | null};
type StrengthPoint = {received_at: string; values: Record<string, string | null>};
type StrengthTop = {state: string; coverage: string; received_at: string | null; rows: StrengthRow[]; history: StrengthPoint[]; truncated: boolean};
type StrengthData = {
  trade_day: string; state: string; received_at: string | null; history_truncated: boolean; coverage: string; total_count: number | null;
  rows: StrengthRow[];
  history: StrengthPoint[];
  top?: StrengthTop;
};
const value = (v: string | null | undefined) => v == null || v === '' || !Number.isFinite(Number(v)) ? null : Number(v);
// Only plot continuous trading sessions; lunch receipts remain in the source history.
function minute(stamp: string) {
  const match = /^(\d{2}):(\d{2})/.exec(time(stamp));
  if (!match) return null;
  const n = Number(match[1])*60+Number(match[2]);
  if (n >= 570 && n <= 690) return n-570;
  if (n >= 780 && n <= 900) return n-660;
  return null;
}

export function KplStrengthWorkspace({day, cutoff, user, csrf, compact = false, overview = false, stocks = []}: {stocks?: Stock[];day: string; cutoff: string; user: string; csrf: string; compact?: boolean; overview?: boolean}) {
  const {ref,width,height}=useChartDimensions(compact, overview ? 65 : 100);
  const params = new URLSearchParams({...(day ? {trade_day:day} : {}), ...(cutoff ? {as_of_time:cutoff} : {})});
  const key = `${user}:kpl-strength:${params}`;
  const read = useCachedLoader(() => request<{data:StrengthData}>('/api/v1/kpl-strength?'+params), key, '开盘啦板块强度');
  const topRead = useCachedLoader<{data?:StrengthTop}>(() => overview
    ? request<{data:StrengthTop}>('/api/v1/kpl-strength?'+params+'&top_only=true') : Promise.resolve({}), key+':top:'+overview, '开盘啦前十');
  const reload = useRef(read.reload); reload.current = read.reload;
  const reloadTop = useRef(topRead.reload); reloadTop.current = topRead.reload;
  useEffect(() => {
    const today = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date());
    if (cutoff || day && day !== today) return;
    const timer = setInterval(() => { if (!document.hidden) reload.current(); }, 30_000);
    const topTimer = setInterval(() => { if (overview && !document.hidden) reloadTop.current(); }, 5_000);
    return () => { clearInterval(timer); clearInterval(topTimer); };
  }, [key, cutoff, overview]);
  const [search, setSearch] = useWorkspaceState(key+':search', '');
  const [selection, setSelection] = useWorkspaceState<string[] | null>(key+(overview?':overview-top-five-v1':':selection'), null);
  const data = read.data?.data;
  const rows = [...(data?.rows ?? [])].sort((a,b) => (value(b.strength) ?? -Infinity)-(value(a.strength) ?? -Infinity) || a.code.localeCompare(b.code));
  const ranked = rows.filter(row=>value(row.strength)!=null);
  const latestTop = topRead.data?.data;
  const topAge = latestTop?.received_at ? (cutoff ? new Date(cutoff).getTime() : Date.now()) - new Date(latestTop.received_at).getTime() : Infinity;
  const fastTop = latestTop?.state === 'available' && latestTop.coverage === 'first_page' && latestTop.rows.length > 0 && latestTop.rows.length <= 10 && topAge >= 0 && topAge < 60_000 ? latestTop : null;
  const topRows = fastTop?.rows ?? ranked.slice(0,10);
  const defaults = (overview ? topRows.slice(0,5) : rows.slice(0,6)).map(row=>row.code);
  const codes = overview ? defaults : selection ?? defaults;
  const picked = codes.map(code=>(overview ? topRows : rows).find(row=>row.code===code)).filter((row):row is NonNullable<typeof row>=>!!row);
  const focusRef = useKplFocus(picked.map(row=>row.code), { user, csrf, day, cutoff,
    enabled: !overview && data?.trade_day === day && (data?.state === 'available' || data?.state === 'stale') });
  const history: (StrengthPoint & {scope?: string})[] = fastTop && overview
    ? [...(data?.history ?? []).map(point=>({...point,scope:'全榜'})), ...fastTop.history.map(point=>({...point,scope:'前十'}))]
        .sort((a,b)=>new Date(a.received_at).getTime()-new Date(b.received_at).getTime()) : data?.history ?? [];
  const batches = history.map(batch=>({...batch, minute:minute(batch.received_at)}))
    .filter((batch):batch is typeof batch & {minute:number}=>batch.minute!=null);
  const values = batches.flatMap(batch=>codes.map(code=>value(batch.values[code]))).filter((v):v is number=>v!=null);
  const rawLow = Math.min(0,...values), rawHigh = Math.max(1,...values);
  const step = Math.max(1,Math.pow(10,Math.floor(Math.log10(Math.max(1,rawHigh-rawLow)))-1)*5);
  const low = Math.floor(rawLow/step)*step, high = Math.ceil(rawHigh/step)*step;
  const top=height<160?8:22, bottom=height<160?24:40, tickCount=height<160?3:5;
  const x=(n:number)=>48+n/240*(width-76), y=(n:number)=>top+(high-n)/Math.max(1,high-low)*(height-top-bottom);
  const samples = batches.map(batch=>({key:(batch.scope??'')+batch.received_at, label:(batch.scope??'')+'接收 '+dateTime(batch.received_at),x:x(batch.minute),
    rows:picked.map((row,i)=>{const n=value(batch.values[row.code]);return {label:row.name,value:n==null?'缺测':number(n),color:sectorColors[i%10],y:n==null?null:y(n)};})}));
  const filter = <section ref={focusRef} className="ty-strength-filter" aria-label="开盘啦强度筛选">
        <div className="ty-strength-heading">{compact ? <><span>题材 · 强度</span><span>涨停</span></> : <><h3>板块强度</h3><small>开盘啦 ↓</small></>}</div>
        <input className="ty-strength-search" aria-label="搜索开盘啦板块" placeholder="搜索板块" value={search} onChange={e=>setSearch(e.target.value)}/>
        <div className="ty-strength-list ty-table-scroll" tabIndex={0} aria-label="开盘啦强度降序排行">
          {rows.filter(row=>row.name.includes(search.trim())||row.code.includes(search.trim())).map(row=><label className="ty-strength-row" key={row.code} data-checked={codes.includes(row.code)||undefined}
            title={`${row.name} · 开盘啦 ${row.code} · 强度 ${row.strength??'缺失'} · ${row.limit_up_count==null?'涨停家数待采集':row.limit_up_count+'家涨停 · 接收 '+dateTime(row.limit_up_received_at)}`}>
            <input type="checkbox" aria-label={(overview?'自动选择':'对比')+row.name+'强度'} checked={codes.includes(row.code)} disabled={overview||!codes.includes(row.code)&&codes.length>=10} onChange={()=>setSelection(toggleFundComparison(codes,row.code))}/>
            <span>{row.name}（{number(value(row.strength))}）</span><b className={tone(row.limit_up_count)}>{row.limit_up_count==null?'—涨停':row.limit_up_count+'涨停'}</b>
          </label>)}
          {!rows.length&&<p className="ty-panel-caption">{read.busy?'正在读取开盘啦强度':data?.state==='disabled'?'开盘啦来源不可用':'本日暂无已存强度'}</p>}
        </div>
        <div className="ty-strength-footer"><span>已选 {codes.length}/{overview?5:10}</span>{overview?<span>自动前五</span>:<button type="button" className="ty-link" onClick={()=>setSelection(null)}>恢复前六</button>}</div>
        <details><summary>统计口径</summary><p>名称后括号是开盘啦原始强度；右侧涨停家数取同代码的开盘啦题材摘要，优先更新当前勾选及强度前十，未采集显示 —，真实零家显示0。悬停查看家数接收时间。{overview?'曲线始终跟随当前有效强度前五名。':'按强度降序，最多对比10个题材。'}与同花顺分类不按名称拼接。</p></details>
      </section>;
  const plot = <div className="ty-strength-plot">
        <div className="ty-fund-legend" aria-label="板块强度曲线图例">{overview&&<strong>板块强度</strong>}{picked.map((row,i)=><span key={row.code} style={{color:sectorColors[i%10]}}><i style={{background:sectorColors[i%10]}}/>{row.name}</span>)}</div>
        <div className="ty-chart ty-funds-chart"><InteractiveChart svgRef={ref} width={width} height={height} samples={values.length?samples:[]} scope={key+':'+codes.join(',')} label="开盘啦板块强度接收采样曲线" snapDistance={16}>
          <g className="ty-chart-grid">{Array.from({length:tickCount},(_,i)=>low+(high-low)*i/(tickCount-1)).map(n=><g key={n}><line x1={48} x2={width-28} y1={y(n)} y2={y(n)} strokeDasharray={n===0?undefined:'2 4'}/><text x={40} y={y(n)+4} textAnchor="end">{number(n)}</text></g>)}
            {[{n:0,t:'09:30'},{n:60,t:'10:30'},{n:120,t:'11:30'},{n:180,t:'14:00'},{n:240,t:'15:00'}].filter((_,i)=>width>=520||i%2===0).map(t=><text key={t.n} x={x(t.n)} y={height-15} textAnchor="middle">{t.t}</text>)}
          </g>
          {picked.map((row,i)=>{
            let drawing=false;
            const path=batches.map((batch,j)=>{const n=value(batch.values[row.code]);if(n==null){drawing=false;return '';}
              const gap=j>0?batch.minute-batches[j-1].minute:0;
              const cmd=drawing&&gap>=0&&gap<=10?'L':'M';drawing=true;return `${cmd}${x(batch.minute)},${y(n)}`;}).join(' ');
            const valid=batches.filter(batch=>value(batch.values[row.code])!=null);
            return <g key={row.code} data-kpl-code={row.code}><path d={path} fill="none" stroke={sectorColors[i%10]} strokeWidth={1.8}/>{valid.map((batch,j)=>{
              const before=valid[j-1],after=valid[j+1];const isolated=(!before||batch.minute-before.minute>10)&&(!after||after.minute-batch.minute>10);
              return j===valid.length-1||isolated?<circle key={(batch.scope??'')+batch.received_at} cx={x(batch.minute)} cy={y(value(batch.values[row.code])!)} r={2.5} fill={sectorColors[i%10]}/>:null;
            })}</g>;
          })}
        </InteractiveChart>{!values.length&&<Empty title={codes.length?'等待所选板块强度采样':overview?'等待板块强度排名':'勾选板块查看强度曲线'} detail="只显示实际保存记录，缺测不补零"/>}</div>
      </div>;
  const source = compact ? <div className="ty-overview-source"><span>全榜接收 {time(data?.received_at)} · {rows.length} 个题材{data?.coverage==='partial_pages'?' · 未齐':''}{data?.state==='stale'?' · 较早采样':''}</span><details><summary>数据说明</summary><p>涨停家数已采 {rows.filter(row=>row.limit_up_count!=null).length}/{rows.length} 个，按板块独立续采；各板块接收时间见悬停。— 表示未采到，0 表示零家。强度按接收时点保存，共 {batches.length} 批，非精确行情分钟。{overview?'始终自动展示当前有效强度前五，随排名更新。':''}{data?.history_truncated?'仅展示最近600批。':''}</p></details></div> : <p className="ty-panel-caption">开盘啦 · {rows.length}{data?.coverage==='partial_pages'?`/${data.total_count} · 本批未齐`:''} 个题材 · 涨停家数已采 {rows.filter(row=>row.limit_up_count!=null).length}/{rows.length} 个 · 接收 {dateTime(data?.received_at)}{data?.state==='stale'?' · 较早采样':''} · {batches.length} 批已存记录；接收时点非精确行情分钟{data?.history_truncated?' · 仅展示最近600批':''}</p>;
  const error = read.error&&<p className="ty-panel-caption" role="alert">开盘啦读取失败{read.data?'，保留上次已读记录':''} <button className="ty-link" onClick={read.reload}>重试</button></p>;
  if (overview) return <>
    <Panel title="市场全景" subtitle="开盘啦情绪 · 题材强度" className="ty-overview-strength">
      <div className="ty-strength-split ty-market-composition"><MarketDistribution stocks={stocks} day={day}/><div className="ty-market-center"><MarketEmotion day={day} cutoff={cutoff} user={user}/>{plot}</div><section className="ty-kpl-top-ten" aria-label="开盘啦题材强度前10">
        <h3>开盘啦题材强度</h3>{fastTop&&<span className="ty-panel-caption" title="独立第一页前十接收采样；完整榜保持各自时间">前十接收 {time(fastTop.received_at)}</span>}<KplStrengthRanks rows={topRows.map((row,i)=>({...row,rank:i+1}))}/>
      </section></div>{source}{error}
    </Panel>
    <Panel title="强度排名" className="ty-overview-strength-list">{filter}</Panel>
  </>;
  return <>
    <div className="ty-strength-chart-workspace ty-kpl-strength-workspace">{filter}{plot}</div>
    {source}{error}
  </>;
}
