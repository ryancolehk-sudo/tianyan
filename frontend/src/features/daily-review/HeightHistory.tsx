import { useEffect, useRef, useState } from 'react';
import { request } from '../../shared/api';
import { number } from './model';
import { Failure, Loading, useBoundRead } from './components';
import { curvePath } from './heightCurve';
import type { Point } from './postcloseVisuals';

type Data = { trade_day: string; trade_days: string[]; records: Point[]; state: string; scope: string };
const lines=[{key:'height',label:'连板高度',color:'#ff6078'},{key:'second_height',label:'次高高度',color:'#ff9d43'},{key:'gem_height',label:'创业板高度',color:'#7da9ff'}];
export function HeightHistory({day,automaticDay,asOf,selectDay}:{day:string;automaticDay:boolean;asOf:string;selectDay:(day:string)=>void}) {
  const [period,setPeriod]=useState('60'),[focus,setFocus]=useState<string|null>(null),[visible,setVisible]=useState(new Set(lines.map(line=>line.key)));
  const [size,setSize]=useState({width:1400,height:530}); const plot=useRef<HTMLDivElement>(null);
  const params=new URLSearchParams({lookback:period,...(!automaticDay?{trade_day:day}:{}),...(asOf?{as_of_time:asOf}:{})});
  const read=useBoundRead(`height-history:${params}`,async()=>{
    const response=await request<{data:Data}>(`/api/v1/review-data/heights?${params}`),data=response.data;
    if(!data||!Array.isArray(data.records)||!Array.isArray(data.trade_days)||data.records.some(point=>!point||typeof point.trade_day!=='string'||!point.values||!point.leaders||lines.some(line=>!Array.isArray(point.leaders[line.key]))))throw new Error('连板高度响应不完整，请重试。');
    return response;
  });
  useEffect(()=>{const element=plot.current;if(!element)return;const observer=new ResizeObserver(([entry])=>setSize({width:Math.max(220,entry.contentRect.width),height:Math.max(260,entry.contentRect.height)}));observer.observe(element);return()=>observer.disconnect();},[read.data]);
  useEffect(()=>{setFocus(null);if(asOf)return;const timer=window.setInterval(read.reload,30000);return()=>clearInterval(timer);},[day,period,asOf,read.reload]);
  const data=read.data?.data,points=data?.records??[];const W=size.width,H=size.height,left=46,right=20,top=45,bottom=33;
  const maximum=Math.max(2,...points.flatMap(point=>lines.map(line=>number(point.values[line.key])??0))),step=Math.max(1,Math.ceil(maximum/6)),ceiling=Math.ceil((maximum+1)/step)*step;
  const x=(index:number)=>left+(points.length<=1?.5:index/(points.length-1))*(W-left-right),y=(value:number)=>top+(1-value/ceiling)*(H-top-bottom);
  const index=focus?points.findIndex(point=>point.trade_day===focus):-1,active=index<0?null:points[index];
  const leaderLabel=(point:Point)=>point.leaders.height.slice(0,2).join('、')+(point.leaders.height.length>2?`等${point.leaders.height.length}只`:'')+` ${point.values.height}`;
  const leaderWidth=(point:Point)=>Math.min(240,leaderLabel(point).split('').reduce((n,c)=>n+(/[\u0000-\u007f]/.test(c)?6:12),0));
  const annotationIndices:number[]=[];
  for(let i=0;i<points.length;i++){
    const current=points[i],previous=points[i-1];
    if(!current.leaders.height?.length)continue;
    if(i!==0&&i!==points.length-1&&(current.values.height===previous?.values.height||current.leaders.height.join()===previous?.leaders.height?.join()))continue;
    const last=annotationIndices.at(-1),gap=last===undefined?Infinity:x(i)-x(last);
    const required=last===undefined?0:(leaderWidth(current)+leaderWidth(points[last]))/2+18;
    if(gap>=required)annotationIndices.push(i);
    else if(i===points.length-1){annotationIndices.pop();annotationIndices.push(i);}
  }
  const axisIndices:number[]=[];
  for(let i=0;i<points.length;i++){
    if(i===0||i===points.length-1||x(i)-x(axisIndices.at(-1)!)>=115&&x(points.length-1)-x(i)>=115)axisIndices.push(i);
  }
  return <section className="rhh-workspace" aria-label="连板高度图"><div className="rhh-chart-toolbar"><div className="rhh-series" aria-label="高度曲线">{lines.map(line=><button key={line.key} style={{color:line.color}} aria-pressed={visible.has(line.key)} onClick={()=>setVisible(current=>{const next=new Set(current);next.has(line.key)?next.delete(line.key):next.add(line.key);return next;})}><svg width="27" height="16" viewBox="0 0 27 16" aria-hidden="true"><path d="M0 8H27" stroke="currentColor"/><circle cx="13" cy="8" r="5" stroke="currentColor" strokeWidth="2" fill="#10212b"/></svg>{line.label}</button>)}</div><div className="rhh-date"><select aria-label="连板高度区间" value={period} onChange={event=>setPeriod(event.target.value)}><option value="20">近20日</option><option value="60">近60日</option><option value="120">近120日</option></select><label>跳转到日期<input type="date" aria-label="高度图跳转日期" max={data?.trade_days.at(-1)} value={focus??''} onChange={event=>{const value=event.target.value;if(!value){setFocus(null);return;}if(points.some(point=>point.trade_day===value))setFocus(value);else selectDay(value);}}/></label></div></div>
    <Failure error={read.error} retry={read.reload}/>{!data?<Loading text="正在读取连板高度…"/>:<div ref={plot} className="rhh-plot" role="img" tabIndex={0} aria-label="连板高度、次高高度、创业板高度历史曲线，左右方向键查看日期" onMouseLeave={()=>setFocus(null)} onBlur={()=>setFocus(null)} onMouseMove={event=>{const bounds=event.currentTarget.getBoundingClientRect();setFocus(points[Math.max(0,Math.min(points.length-1,Math.round((event.clientX-bounds.left-left)/(W-left-right)*(points.length-1))))]?.trade_day??null);}} onKeyDown={event=>{if(['ArrowLeft','ArrowRight','Escape'].includes(event.key)){event.preventDefault();setFocus(event.key==='Escape'?null:points[Math.max(0,Math.min(points.length-1,(index<0?points.length-1:index)+(event.key==='ArrowLeft'?-1:1)))]?.trade_day??null);}}}>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">{Array.from({length:ceiling/step+1},(_,i)=>i*step).map(value=><g key={value}><line x1={left} x2={W-right} y1={y(value)} y2={y(value)} className="rhh-grid"/><text x={left-10} y={y(value)+4} textAnchor="end">{value}</text></g>)}{lines.filter(line=>visible.has(line.key)).map(line=><g key={line.key} data-height-series={line.key}><path d={curvePath(points.map((point,i)=>{const value=number(point.values[line.key]);return value===null?null:{x:x(i),y:y(value)};}))} fill="none" stroke={line.color} strokeWidth="1.7" vectorEffect="non-scaling-stroke"/>{points.map((point,i)=>{const value=number(point.values[line.key]);return value===null?null:<circle key={point.trade_day} cx={x(i)} cy={y(value)} r={index===i?5:3.3} fill="#10212b" stroke={line.color} strokeWidth="2"/>;})}</g>)}{visible.has('height')&&annotationIndices.map(i=><text key={points[i].trade_day} className="rhh-leader" textAnchor="middle" x={Math.max(left+leaderWidth(points[i])/2,Math.min(W-right-leaderWidth(points[i])/2,x(i)))} y={Math.max(16,y(number(points[i].values.height)!)-15)}>{leaderLabel(points[i])}<title>{points[i].leaders.height.join('、')} {points[i].values.height}</title></text>)}{points.filter((_,i)=>axisIndices.includes(i)).map(point=>{const i=points.indexOf(point);return <text key={point.trade_day} x={x(i)} y={H-7} textAnchor={i===0?'start':i===points.length-1?'end':'middle'}>{point.trade_day}</text>;})}{active&&<line x1={x(index)} x2={x(index)} y1={top} y2={H-bottom} stroke="#afc1cc" strokeDasharray="4 4"/>}</svg>
      {active&&<div className={'rhh-tooltip'+(index>points.length/2?' rhh-tooltip-left':'')}><b>{active.trade_day}</b>{lines.filter(line=>visible.has(line.key)).map(line=><div key={line.key}><span style={{color:line.color}}>{line.label}</span><strong>{active.values[line.key]??'—'}</strong><small>{active.leaders[line.key]?.join('、')}</small></div>)}</div>}{!points.some(point=>number(point.values.height)!==null)&&<span className="pcv-no-data">尚无已保存的连板高度</span>}
    </div>}
  </section>;
}
