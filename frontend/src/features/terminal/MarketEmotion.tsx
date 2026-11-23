import { useEffect, useRef } from 'react';
import { request } from '../../shared/api';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { InteractiveChart, useChartDimensions } from './ChartInspection';
import { dateTime } from './data';
import type { Stock } from './types';
import './marketEmotion.css';

type Point = {received_at: string; value: string};
type Series = {trade_day: string; latest: Point | null; points: Point[]; truncated: boolean};
type Emotion = {state: string; today: Series; yesterday: Series | null};

export function tradingMinute(stamp: string) {
  if (!Number.isFinite(Date.parse(stamp))) return null;
  const local = new Date(Date.parse(stamp)+8*3600000);
  const n = local.getUTCHours()*60+local.getUTCMinutes()+local.getUTCSeconds()/60;
  return n>=555 && n<=690 ? n-555 : n>=780 && n<=900 ? n-645 : null;
}

export function distribution(stocks: Stock[], day: string) {
  const bins = Array<number>(11).fill(0), seen = new Set<string>();
  const start = Date.parse(day+'T00:00:00+08:00');
  let snapshotCount = 0;
  for (const stock of stocks) {
    const snapshot = stock.price_basis === 'snapshot';
    const stamp = Date.parse((snapshot ? stock.source_snapshot_time : stock.source_time) ?? '');
    const n = stock.pct_change;
    // terminal_read selects daily_close rows for the requested workbench day;
    // these intentionally have no invented intraday source_time.
    const datedClose=stock.price_basis==='daily_close';
    if (seen.has(stock.code) || n==null || !Number.isFinite(n) || (!datedClose && (!Number.isFinite(stamp) || stamp<start || stamp>=start+86400000))) continue;
    seen.add(stock.code);
    if (snapshot) snapshotCount++;
    const i = n>10?0:n>=7?1:n>=5?2:n>=3?3:n>0?4:n===0?5:n>-3?6:n>-5?7:n>-7?8:n>=-10?9:10;
    bins[i]++;
  }
  return { bins, snapshotCount };
}

export function MarketDistribution({stocks, day}: {stocks: Stock[]; day: string}) {
  const {bins:counts,snapshotCount}=distribution(stocks,day);
  const max=Math.max(1,...counts), total=counts.reduce((a,b)=>a+b,0);
  const labels=['>10','7~10','5~7','3~5','0~3','平盘','−3~0','−5~−3','−7~−5','−10~−7','<−10'];
  return <section className="ty-market-distribution" aria-label="全市场涨跌分布">
    <h3>全市场涨跌</h3><div className="ty-distribution-bars">{counts.map((n,i)=><div key={i} className="ty-distribution-row" title={`${labels[i]}${i===5?'':'%'}：${n}只`}>
      <span>{labels[i]}</span><div><i style={{width:`${n/max*100}%`,background:i<5?'var(--ty-up)':i===5?'var(--ty-muted)':'var(--ty-down)'}}/></div><b>{total?n:'—'}</b>
    </div>)}</div><span className="ty-distribution-count" title={`${day} 有效涨幅 ${total} 只${snapshotCount ? `，含同日服务器快照 ${snapshotCount} 只，逐股成交时刻待核` : ''}。`}>{snapshotCount>0&&'含快照 · '}{total.toLocaleString()} 只有效</span>
  </section>;
}

export function MarketEmotion({day,cutoff,user}: {day: string;cutoff: string;user: string}) {
  const params=new URLSearchParams({...(day?{trade_day:day}:{}),...(cutoff?{as_of_time:cutoff}:{})});
  const key=`${user}:kpl-emotion:${params}`;
  const read=useCachedLoader(()=>request<{data:Emotion}>('/api/v1/kpl-emotion?'+params),key,'开盘啦市场情绪');
  const reload=useRef(read.reload);reload.current=read.reload;
  useEffect(()=>{if(cutoff)return;const timer=setInterval(()=>{if(!document.hidden)reload.current();},15000);return()=>clearInterval(timer);},[key,cutoff]);
  const {ref,width,height}=useChartDimensions(true,65);
  const data=read.data?.data;
  const top=height<160?8:22, bottom=height<160?22:40, plotHeight=Math.max(1,height-top-bottom);
  const x=(n:number)=>48+n/255*(width-76), y=(n:number)=>top+(100-n)/100*plotHeight;
  const lines=[{label:'今日情绪',color:'#ffbc48',series:data?.today},{label:'昨日情绪',color:'#a6b4c0',series:data?.yesterday}].map(line=>({...line,points:(line.series?.points??[])
    .map(p=>({...p,n:tradingMinute(p.received_at),v:Number(p.value)})).filter(p=>p.n!=null&&Number.isFinite(p.v)&&p.v>=0&&p.v<=100)}));
  const samples=lines.flatMap(line=>line.points.map(p=>({key:line.label+p.received_at,label:`${line.label} · 接收 ${dateTime(p.received_at)}`,x:x(p.n!),rows:[{label:line.label,value:String(p.v),color:line.color,y:y(p.v)}]}))).sort((a,b)=>a.x-b.x);
  return <section className="ty-market-emotion" aria-label="开盘啦市场情绪">
    <div className="ty-emotion-heading"><h3>市场情绪</h3><div>{lines.map(line=><span key={line.label} style={{color:line.color}} title={`${line.series?.trade_day??'日期未知'} · 接收 ${dateTime(line.series?.latest?.received_at)}`}><i style={{background:line.color}}/>{line.label.slice(0,2)} <b>{line.series?.latest?.value??'—'}</b></span>)}</div></div>
    <div className="ty-chart"><InteractiveChart svgRef={ref} width={width} height={height} samples={samples} scope={key} label="开盘啦今日与昨日情绪采样曲线" snapDistance={14}>
      {[{low:0,name:'冰点',color:'#46c39c'},{low:25,name:'一般',color:'#d6bd55'},{low:50,name:'良好',color:'#f5a449'},{low:75,name:'火爆',color:'#f16877'}].map(b=><g key={b.low} className="ty-emotion-band"><rect x={48} y={y(b.low+25)} width={Math.max(1,width-76)} height={Math.max(1,plotHeight/4)} fill={b.color} opacity={.09}/><text x={width-32} y={y(b.low+12.5)+4} textAnchor="end" fill={b.color}>{b.name}</text></g>)}
      <g className="ty-chart-grid">{[0,50,100].map(n=><g key={n}><line x1={48} x2={width-28} y1={y(n)} y2={y(n)} strokeDasharray="2 4"/><text x={40} y={y(n)+4} textAnchor="end">{n}</text></g>)}{[{n:0,t:'09:15'},{n:135,t:'11:30'},{n:255,t:'15:00'}].map(t=><text key={t.n} x={x(t.n)} y={height-6} textAnchor="middle">{t.t}</text>)}</g>
      {lines.map(line=>{const path=line.points.map((p,i)=>{const prev=line.points[i-1];const continuous=prev&&p.n!-prev.n!<=3&&p.n!>=prev.n!;return `${continuous?'L':'M'}${x(p.n!)},${y(p.v)}`;}).join(' ');return <g key={line.label} data-emotion-series={line.label}><path d={path} fill="none" stroke={line.color} strokeWidth={1.6}/>{line.points.map((p,i)=>i===line.points.length-1||!i||p.n!-line.points[i-1].n!>3?<circle key={p.received_at} cx={x(p.n!)} cy={y(p.v)} r={2} fill={line.color}/>:null)}</g>;})}
    </InteractiveChart>{!samples.length&&<span className="ty-emotion-empty">{read.error?'情绪读取失败':read.busy?'读取情绪':'等待盘中采样'}</span>}</div>
    {read.error&&<button className="ty-link" onClick={read.reload}>重试</button>}
  </section>;
}
