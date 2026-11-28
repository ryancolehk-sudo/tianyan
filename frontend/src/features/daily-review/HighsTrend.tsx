import { useLayoutEffect, useRef, useState } from 'react';
import { number } from './model';
import type { Point } from './postcloseVisuals';

export function HighsTrend({points, group, day}: {points: Point[]; group: string; day: string}) {
  const chart = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({width: 600, height: 190});
  const [hover, setHover] = useState<number | null>(null);
  useLayoutEffect(() => {
    const observer = new ResizeObserver(([entry]) => setSize({width: entry.contentRect.width, height: entry.contentRect.height}));
    if (chart.current) observer.observe(chart.current);
    return () => observer.disconnect();
  }, []);
  const {width, height} = size;
  const rows = points.map(point => ({day: point.trade_day, total: number(point.values.count), added: number(point.values.new)}));
  const max = Math.max(1, ...rows.flatMap(row => [row.total ?? 0, row.added ?? 0]));
  const magnitude = 10 ** Math.floor(Math.log10(max / 5));
  const step = Math.max(1, ([1, 2, 5, 10].find(n => n * magnitude >= max / 5) ?? 10) * magnitude);
  const ticks = Array.from({length: Math.ceil(max / step) + 1}, (_, i) => i * step);
  const left = 40, right = Math.max(left + 1, width - 18), bottom = Math.max(15, height - 25);
  const pitch = (right - left) / Math.max(1, rows.length);
  const x = (i: number) => left + (i + 0.5) * pitch;
  const y = (value: number) => bottom - value / ticks[ticks.length - 1] * (bottom - 14);
  const idx = Math.min(rows.length - 1, hover ?? rows.length - 1), current = rows[idx];
  const stride = Math.max(1, Math.ceil(rows.length * 52 / Math.max(1, right - left)));
  const path = rows.map((row, i) => row.total === null ? '' : `${i && rows[i - 1].total !== null ? 'L' : 'M'}${x(i)} ${y(row.total)}`).join(' ');
  return <section className="pcv-chart nh-history" aria-label="新高趋势">
    <header><h3>新高趋势 <span>{group || '全部'}</span></h3><time>{current?.day || day}</time></header>
    <div className="nh-legend"><i/>新高数量 <b/>当日新增</div>
    <div ref={chart} className="nh-plot" tabIndex={0} role="group" aria-label="新高趋势，左右方向键查看各交易日"
      onPointerMove={e => setHover(Math.max(0, Math.min(rows.length - 1, Math.floor((e.clientX - e.currentTarget.getBoundingClientRect().left - left) / pitch))))}
      onPointerLeave={()=>setHover(null)} onBlur={()=>setHover(null)} onKeyDown={e=>{
        if(e.key==='Escape')setHover(null);
        if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();setHover(Math.max(0,Math.min(rows.length-1,idx+(e.key==='ArrowLeft'?-1:1))));}
      }}>
      <svg width="100%" height="100%" aria-hidden="true">
        {ticks.map(tick=><g key={tick}><line x1={left} x2={right} y1={y(tick)} y2={y(tick)} stroke="#e9edf2" strokeDasharray="4 2"/><text x={left-7} y={y(tick)+4} textAnchor="end">{tick}</text></g>)}
        {rows.map((row,i)=>row.added!==null&&<rect key={row.day} x={x(i)-Math.min(10,pitch*.66)/2} width={Math.min(10,pitch*.66)} y={y(row.added)} height={bottom-y(row.added)} fill="#fbaaaa"/>)}
        <path d={path} fill="none" stroke="#fa4144" strokeWidth="1.5"/>
        {rows.map((row,i)=><g key={row.day}>{row.total!==null&&<circle cx={x(i)} cy={y(row.total)} r={idx===i?4:3} fill="#f44346" stroke={idx===i?'#f4a88b':undefined} strokeWidth="3"/>}
          {i%stride===0&&<text x={x(i)} y={height-7} textAnchor="middle">{row.day.slice(5)}</text>}</g>)}
        {current&&<line x1={x(idx)} x2={x(idx)} y1={14} y2={bottom} stroke="#cad5df" strokeDasharray="3 3"/>}
      </svg>
      {!rows.some(row=>row.total!==null||row.added!==null)&&<span className="nh-no-history">尚无已保存数据</span>}
      {hover!==null&&current&&<output style={{[idx<rows.length/2?'right':'left']:12}}>{current.day}　新高 {current.total??'—'}　新增 {current.added??'—'}</output>}
    </div>
  </section>;
}
