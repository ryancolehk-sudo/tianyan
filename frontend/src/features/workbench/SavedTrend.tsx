import { useState } from 'react';
import type { DatasetItem } from '../../generated/schema';

type Field = {key:string;label:string};
const number = (value:string | null | undefined) => value != null && /^-?\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value)) ? Number(value) : null;
function date(value:string | null | undefined) {
  const raw = value?.replaceAll('-','');
  const normalized = raw && /^\d{6}(?:\d{2})?$/.test(raw) ? `${raw.slice(0,4)}-${raw.slice(4,6)}-${raw.slice(6,8) || '01'}` : '';
  return normalized && Number.isFinite(Date.parse(normalized)) ? normalized : '';
}

// A single identity/source, already bounded to the observation day by its caller.
export function SavedTrend({record,rows,title,dateField='trade_date',fields}:{record:DatasetItem;rows:Record<string,string | null>[];title:string;dateField?:string;fields:Field[]}) {
  const [selected,setSelected] = useState(fields[0].key);
  const field = fields.find(item => item.key === selected) ?? fields[0];
  const ordered = rows.map(row => ({day:date(row[dateField]),value:number(row[field.key])})).filter(row => row.day).sort((a,b) => a.day.localeCompare(b.day)).slice(-120);
  const valid = ordered.filter((row):row is {day:string;value:number} => row.value != null);
  const unit = record.payload.columns.find(column => column.key === field.key)?.unit ?? '原单位未核';
  const duplicate = new Set(ordered.map(row => row.day)).size !== ordered.length;
  const low = Math.min(...valid.map(row => row.value)), high = Math.max(...valid.map(row => row.value));
  const firstTime = Date.parse(ordered[0]?.day ?? ''), lastTime = Date.parse(ordered.at(-1)?.day ?? '');
  const x = (day:string) => 8 + (Date.parse(day) - firstTime) / Math.max(86400000,lastTime-firstTime) * 484;
  const y = (value:number) => 108 - (value-low) / (high-low || Math.max(Math.abs(high)*.01,1)) * 90;
  const paths:string[]=[];
  let interrupted=true;
  for (const row of ordered) {
    if (row.value == null) { interrupted=true; continue; }
    const point=`${x(row.day)},${y(row.value)}`;
    if (interrupted) paths.push('M '+point); else paths[paths.length-1]+=' L '+point;
    interrupted=false;
  }
  const latest=valid.at(-1), previous=valid.at(-2);
  return <figure className="trade-saved-trend" aria-label={title+'已存趋势'}><figcaption><strong>{title}</strong>{fields.length>1 && <label>指标 <select aria-label={title+'趋势指标'} value={field.key} onChange={event=>setSelected(event.target.value)}>{fields.map(item=><option key={item.key} value={item.key}>{item.label}</option>)}</select></label>}<span>{field.label} · {unit}</span></figcaption>{duplicate ? <p className="trade-meta">同日有多条记录，未合并绘图；请在明细核对范围。</p> : valid.length<2 ? <p className="trade-meta">历史不足两个有效时点，暂不绘制趋势。</p> : <><div className="trade-trend-values"><span>最近 <b>{latest!.value.toLocaleString('zh-CN',{maximumFractionDigits:4})}</b></span><span>较上个有效时点 <b>{latest!.value-previous!.value>0?'+':''}{(latest!.value-previous!.value).toLocaleString('zh-CN',{maximumFractionDigits:4})}</b></span></div><svg viewBox="0 0 500 120" role="img" aria-label={field.label+'已存数据折线'}><path d="M8 112H492" fill="none" stroke="#e6e9ee"/>{paths.map((path,index)=><path key={index} data-saved-trend d={path} fill="none" stroke="#426daa" strokeWidth="1.8"/>)}{valid.map(row=><circle data-trend-day={row.day} key={row.day} cx={x(row.day)} cy={y(row.value)} r="2.5" fill="#426daa"><title>{row.day+' · '+row.value+' '+unit}</title></circle>)}</svg><div className="trade-trend-dates"><time>{ordered[0].day}</time><time>{ordered.at(-1)!.day}</time></div></>}<p className="trade-meta">仅当前原表已存 {ordered.length} 个日期，{ordered.length-valid.length} 个缺失值；缺失处断线，不补造历史。{ordered.length===120 && '最多展示最近120个日期。'}</p></figure>;
}
