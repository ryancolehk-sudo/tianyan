import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { CSSProperties, ReactNode } from 'react';
import { amount, number } from './model';

export type Value = string | number | null;
export type Point = { trade_day: string; values: Record<string, Value>; leaders: Record<string, string[]>; state: string };
export type Stock = { ts_code: string; name: string; rank: number; themes: string[]; field_reasons?: Record<string,string>; [field: string]: unknown };
export type Rank = { rows: Stock[]; state: string; valid_count: number; sample_count: number; reason: string | null; ranking_field: string; window?: { start: string; end: string; sessions: number; loaded_sessions: number; excluded_incomplete_count: number } };
export type Postclose = { version: string; state: string; trade_day: string | null; records: Point[]; trade_days: string[]; sample_count?: number; rankings: Record<string, Rank>; metric_definitions: { key: string; label: string; unit: string }[]; scope: string; interval_basis: string; schedule: { time: string; late_refresh: string }; coverage: { dataset: string; trade_day: string; state: string; received_at: string | null }[] };
export type Line = { key: string; label: string; color: string; money?: boolean; bar?: boolean; negative?: boolean; axis?: number };
export const red = '#ff6078', green = '#43d1ac', gold = '#efc363', blue = '#7da9ff';
export const charts: { title: string; lines: Line[]; thermal?: boolean }[] = [
  { title: '情绪热度', thermal: true, lines: [{ key: 'emotion', label: '开盘啦情绪', color: gold }] },
  { title: '连板高度', lines: [{ key: 'height', label: '最高', color: red }, { key: 'second_height', label: '次高', color: gold }, { key: 'gem_height', label: '创业板', color: blue }] },
  { title: '涨停股成交额', lines: [{ key: 'up_amount', label: '涨停', color: red, money: true }, { key: 'broken_amount', label: '炸板', color: green, money: true }] },
  { title: '涨跌统计', lines: [{ key: 'market_amount', label: '量能(左/亿)', color: gold, money: true }, { key: 'advancing', label: '上涨(右/只)', color: red, bar: true, axis: 1 }, { key: 'declining', label: '下跌(右/只)', color: green, bar: true, negative: true, axis: 1 }] },
  { title: '涨跌停统计', lines: [{ key: 'consecutive', label: '连板', color: gold }, { key: 'limit_up', label: '涨停', color: red }, { key: 'limit_down', label: '跌停', color: green }] },
  { title: '连板晋级', lines: [{ key: 'first_promotion', label: '首板晋级', color: blue }, { key: 'relay_promotion', label: '连板晋级', color: red }, { key: 'seal_rate', label: '封板率', color: gold }] },
  { title: '竞价一字', lines: [{ key: 'auction_up', label: '涨停(左/只)', color: red }, { key: 'auction_seal', label: '封单(右/亿)', color: gold, money: true, axis: 1 }, { key: 'auction_down', label: '跌停(左/只)', color: green }] },
  { title: '主力流入', lines: [{ key: 'main_net', label: '沪深主力/亿', color: red, money: true, bar: true }] },
  { title: '昨日涨停表现', lines: [{ key: 'yesterday_up', label: '昨日涨停', color: blue }, { key: 'yesterday_relay', label: '昨日连板', color: gold }] },
  { title: '亏钱效应', lines: [{ key: 'limit_down', label: '跌停(左/只)', color: green, bar: true }, { key: 'broken', label: '炸板(左/只)', color: '#9eabba', bar: true }, { key: 'broken_rate', label: '炸板率(右/%)', color: gold, axis: 1 }] },
];
export function show(value: unknown, money = false, percent = false): string {
  const v = number(value); if (v === null) return '—';
  return money ? amount(value) : percent ? `${v.toFixed(2)}%` : Number.isInteger(v) ? v.toLocaleString('zh-CN') : v.toFixed(2);
}
export function Detail({ text, children, className = '' }: { text: string; children: ReactNode; className?: string }) {
  const id = useId(), anchor = useRef<HTMLSpanElement>(null);
  const [position, setPosition] = useState<{ left: number; top: number; above: boolean } | null>(null);
  const open = () => { const rect = anchor.current?.getBoundingClientRect(); if (!rect) return; setPosition({left: Math.max(8,Math.min(rect.left,innerWidth-Math.min(420,innerWidth-16)-8)),top: rect.top>innerHeight-100?rect.top-8:rect.bottom+8,above:rect.top>innerHeight-100}); };
  return <span ref={anchor} className={`pcv-detail ${className}`} tabIndex={0} aria-describedby={position?id:undefined} onFocus={open} onBlur={()=>setPosition(null)} onMouseEnter={open} onMouseLeave={()=>{if(document.activeElement!==anchor.current)setPosition(null);}} onKeyDown={e=>{if(e.key==='Escape')setPosition(null);}}>{children}{position&&createPortal(<span id={id} role="tooltip" className="pcv-cell-tooltip" style={{left:position.left,top:position.top,transform:position.above?'translateY(-100%)':undefined}}>{text}</span>,document.body)}</span>;
}
const seriesColor = (line: Line, value: unknown) => line.key==='main_net' && (number(value)??0)<0 ? green : line.color;
const auctionRule = '仅采用原生09:25全市场竞价盘口；未采到留空，盘后跌停池不作替代。原始归档可在“数据与口径”查看。';
const compactLabels: Record<string, string> = {
  market_amount: '量能', advancing: '上涨', declining: '下跌',
  auction_up: '涨停', auction_seal: '封单', auction_down: '跌停',
  limit_down: '跌停', broken: '炸板', broken_rate: '炸板率', first_promotion: '首板', relay_promotion: '连板',
};

export function Plot({ spec, points, expand, detailed = false, linkedDay, onDayChange }: { spec: typeof charts[number]; points: Point[]; expand?: () => void; detailed?: boolean; linkedDay?: string | null; onDayChange?: (day: string | null) => void }) {
  const [localFocus, setLocalFocus] = useState<number | null>(null);
  const [inspecting,setInspecting]=useState(false);
  const linkedIndex = linkedDay ? points.findIndex(point => point.trade_day === linkedDay) : -1;
  const focus = onDayChange ? linkedIndex < 0 ? null : linkedIndex : localFocus;
  const setFocus = (value: number | null | ((previous: number | null) => number | null)) => {
    const next = typeof value === 'function' ? value(focus) : value;
    setInspecting(next!==null);
    if (onDayChange) onDayChange(next === null ? null : points[next]?.trade_day ?? null);
    else setLocalFocus(next);
  };
  const plot = useRef<HTMLDivElement>(null);
  const [size,setSize]=useState({width:520,height:144});
  useEffect(()=>{const el=plot.current;if(!el)return;const observer=new ResizeObserver(([entry])=>setSize({width:Math.max(150,Math.round(entry.contentRect.width)),height:Math.max(56,Math.round(entry.contentRect.height))}));observer.observe(el);return()=>observer.disconnect();},[]);
  const W = size.width, H = size.height, left = 42, right = spec.lines.some(l => l.axis) ? 52 : 15, top = spec.lines.some(l=>l.axis)?22:10, bottom = 22;
  const width = W - left - right, height = H - top - bottom;
  const x = (i: number) => left + (points.length <= 1 ? width / 2 : i * width / (points.length - 1));
  const val = (p: Point, line: Line) => { const n = number(p.values[line.key]); return n === null ? null : n / (line.money ? 1e8 : 1) * (line.negative ? -1 : 1); };
  const domains = [0, 1].map(axis => {
    const lines = spec.lines.filter(l => (l.axis || 0) === axis);
    const values = lines.flatMap(l => points.map(p => val(p, l)).filter((v): v is number => v !== null));
    const signed = lines.some(l=>l.negative) || values.some(v=>v<0);
    const max = spec.thermal ? 100 : Math.max(1, ...values.map(Math.abs)) * 1.12;
    return { lo: signed ? -max : 0, hi: max };
  });
  const y = (v: number, axis = 0) => top + (domains[axis].hi - v) / (domains[axis].hi - domains[axis].lo) * height;
  const active = focus === null ? points.at(-1) : points[focus];
  const available = points.some(p => spec.lines.some(l => number(p.values[l.key]) !== null));
  const annotations: number[] = [];
  if(detailed&&spec.title==='连板高度') {
    const candidates=points.map((p,i)=>({i,height:number(p.values.height)})).filter(p=>p.height!==null&&points[p.i].leaders.height?.length).sort((a,b)=>(b.height!-a.height!)||b.i-a.i);
    for(const p of candidates)if(annotations.every(i=>Math.abs(x(i)-x(p.i))>115))annotations.push(p.i);
  }
  return <section className={`pcv-chart${detailed?' pcv-chart-detailed':''}`} aria-label={spec.title} data-focus-day={focus === null ? undefined : points[focus]?.trade_day}>
    <header><h3>{expand?<button className="pcv-chart-open" onClick={expand} aria-label={`放大${spec.title}`}>{spec.title}<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 2H2v4m8-4h4v4M2 10v4h4m8-4v4h-4" fill="none" stroke="currentColor" strokeWidth="1.3"/></svg></button>:spec.title}{onDayChange&&focus!==null&&<small className="pcv-linked-day">{points[focus]?.trade_day.slice(5)}</small>}</h3><div className="pcv-legend">{spec.lines.map(line => {const content=<><i/><span className="pcv-legend-full">{line.label}</span><span className="pcv-legend-compact">{compactLabels[line.key]??line.label}</span><b style={{color:seriesColor(line,active?.values[line.key])}}>{show(active?.values[line.key], line.money, ['first_promotion','relay_promotion','seal_rate','yesterday_up','yesterday_relay','broken_rate'].includes(line.key))}</b></>;return <span key={line.key} style={{ '--series': line.color } as CSSProperties}>{line.key==='auction_down'?<Detail text={`${active?.trade_day??'所选交易日'}：${auctionRule}`}>{content}</Detail>:content}</span>;})}</div></header>
    <div ref={plot} className="pcv-plot" tabIndex={0} role="img" aria-label={`${spec.title}，左右方向键查看各交易日`} onKeyDown={e => { if (['ArrowLeft', 'ArrowRight', 'Escape'].includes(e.key)) { e.preventDefault(); setFocus(e.key === 'Escape' ? null : i => Math.max(0, Math.min(points.length - 1, (i ?? points.length - 1) + (e.key === 'ArrowLeft' ? -1 : 1)))); } }} onBlur={() => setFocus(null)} onMouseLeave={() => setFocus(null)} onMouseMove={e => { const b = e.currentTarget.getBoundingClientRect(); setFocus(Math.max(0, Math.min(points.length - 1, Math.round(((e.clientX - b.left) / b.width * W - left) / width * (points.length - 1))))); }}>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
        {spec.lines.some(l=>l.axis)&&<g className="pcv-axis-unit"><text x={left-5} y="12" textAnchor="end">左/{spec.title==='涨跌统计'?'亿':'只'}</text><text x={W-right+5} y="12">右/{spec.title==='竞价一字'?'亿':spec.title==='亏钱效应'?'%':'只'}</text></g>}
        {spec.thermal && [0, 25, 50, 75].map((v, i) => <rect key={v} x={left} y={y(v + 25)} width={width} height={height / 4} fill={['#19463d', '#494b2b', '#60482b', '#5a2d3a'][i]} opacity=".5"/>)}
        {(detailed?[0,.25,.5,.75,1]:[0,.5,1]).map(f => { const value = domains[0].lo + (domains[0].hi - domains[0].lo) * f; return <g key={f}><line x1={left} x2={W - right} y1={y(value)} y2={y(value)} className="pcv-gridline"/><text data-axis="left" x={left - 5} y={y(value) + 4} textAnchor="end">{Math.round(value).toLocaleString()}</text>{spec.lines.some(l => l.axis) && <text data-axis="right" x={W - right + 5} y={y(value) + 4}>{Math.round(domains[1].lo + (domains[1].hi - domains[1].lo) * f).toLocaleString()}</text>}</g>; })}
        {spec.lines.map((line, li) => { const axis = line.axis || 0; let path = '', connected = false; points.forEach((p, i) => { const v = val(p, line); if (v === null) { connected = false; return; } path += `${connected ? 'L' : 'M'}${x(i)},${y(v, axis)} `; connected = true; }); return <g key={line.key}>
          {!line.bar && <path d={path} fill="none" stroke={line.color} strokeWidth="1.8" vectorEffect="non-scaling-stroke"/>}
          {points.map((p, i) => { const v = val(p, line); if (v === null) return null; return line.bar ? <rect key={i} x={x(i) - 3 + (spec.lines.filter(l => l.bar).length > 1 ? (li % 2) * 6 - 3 : 0)} y={Math.min(y(v, axis), y(0, axis))} width={5} height={Math.max(1, Math.abs(y(v, axis) - y(0, axis)))} fill={line.key === 'main_net' && v < 0 ? green : line.color}/> : <circle key={i} cx={x(i)} cy={y(v, axis)} r={focus === i ? 3 : 1.8} fill={line.color}/>; })}
        </g>; })}
        {spec.thermal && ['冰点', '一般', '良好', '火爆'].map((label, i) => <text key={label} x={W - right - 4} y={y(i * 25 + 12) + 4} textAnchor="end" className="pcv-band-label">{label}</text>)}
        {annotations.map(i=><text key={i} className="pcv-leader-label" x={Math.max(left+58,Math.min(W-right-58,x(i)))} y={Math.max(top+14,y(number(points[i].values.height)!)-10)} textAnchor="middle">{points[i].leaders.height[0]} {show(points[i].values.height)}板</text>)}
        {points.filter((_, i) => i === 0 || i === points.length - 1 || i === Math.floor((points.length - 1) / 2)).map(p => <text key={p.trade_day} x={x(points.indexOf(p))} y={H - 4} textAnchor={points.indexOf(p)===0?'start':points.indexOf(p)===points.length-1?'end':'middle'}>{p.trade_day.slice(5)}</text>)}
        {focus !== null && points[focus] && <line x1={x(focus)} x2={x(focus)} y1={top} y2={H - bottom} stroke="#c5d2db" strokeDasharray="3 3"/>}
      </svg>
      {!available && <span className="pcv-no-data">尚无已保存数据</span>}
      {focus !== null && active && (!onDayChange||inspecting) && <div className={'pcv-tooltip' + (focus > points.length / 2 ? ' pcv-tooltip-left' : '')}><b>{active.trade_day}</b>{spec.lines.map(l => <span key={l.key}>{l.label}<strong style={{ color: seriesColor(l,active.values[l.key]) }}>{show(active.values[l.key], l.money)}{['first_promotion', 'relay_promotion', 'seal_rate', 'yesterday_up', 'yesterday_relay', 'broken_rate'].includes(l.key) && number(active.values[l.key]) !== null ? '%' : ''}</strong>{active.leaders[l.key]?.length ? <small>{active.leaders[l.key].join('、')}</small> : null}</span>)}</div>}
    </div>
  </section>;
}
