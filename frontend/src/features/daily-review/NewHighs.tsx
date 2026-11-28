import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { request } from '../../shared/api';
import { useNavigation } from '../../shared/navigation';
import { Loading, useBoundRead } from './components';
import { number, pct, tone } from './model';
import type { Coverage } from './model';
import type { ReviewProps } from './context';
import { HighsTrend } from './HighsTrend';
import type { Point } from './postcloseVisuals';
import './newHighs.css';

type Field='price'|'pct_chg'|'amount'|'main_net'|'actual_turnover'|'actual_float';
type Stock={ts_code:string;name:string;themes:string;group_name:string;is_new:boolean;membership_day:string;quote_day:string|null;field_sources?:Record<string,{dataset:string;trade_day:string;basis?:string}>}&Record<Field,string|null>;
type Group={name:string;count:number;new_count:number;members:string[]};
type RecordDay={trade_day:string;total:number|null;new_count:number|null;group_counts:Record<string,{count:number;new_count:number}>|null};
type Highs={version:string;state:string;trade_day:string;start_day:string;trade_days:string[];total:number|null;new_count:number|null;loaded_count:number;rows:Stock[];groups:Group[];records:RecordDay[];missing_days:string[];coverage:Coverage[]};
const colors=['#b6a0df','#e28697','#3ab9c3','#60c6d1','#eaa9cb','#98bdd0','#d5ad68','#c7a0b7','#8acbab','#e7bd91','#a7b9d0','#d699a4','#76c5bf','#93b9a2','#c5b68e','#a29bca'];
const columns:[Field,string][]=[['price','价格'],['pct_chg','涨幅'],['amount','成交额'],['main_net','主力净额'],['actual_turnover','实际换手'],['actual_float','实际流通']];
const money=(value:unknown)=>{const n=number(value);return n===null?'—':`${n<0?'−':''}${(Math.abs(n)/(Math.abs(n)>=1e8?1e8:1e4)).toFixed(Math.abs(n)>=1e8?2:0)}${Math.abs(n)>=1e8?'亿':'万'}`;};
const cash=(value:unknown)=>{const n=number(value);return n===null?'—':`${n>0?'+':''}${money(value)}`;};

function distribution(groups:Group[]){
  const sum=groups.reduce((n,g)=>n+g.count,0);
  let cursor=-Math.PI/2;
  const sectors=groups.map((g,i)=>{const from=cursor,to=from+(sum?g.count/sum*Math.PI*2:0);cursor=to;return{g,i,from,to,mid:(from+to)/2};});
  // Rotate the ring without changing order or proportions so labels share both sides.
  const normalize=(angle:number)=>Math.atan2(Math.sin(angle),Math.cos(angle));
  const candidates=[0,...sectors.flatMap((s,i)=>{const next=sectors[i+1]?.mid??sectors[0].mid+Math.PI*2;return[Math.PI/2-(s.mid+next)/2,-Math.PI/2-(s.mid+next)/2].map(normalize);})];
  const crowding=(angle:number)=>Math.abs(sectors.filter(s=>Math.cos(s.mid+angle)>=0).length*2-sectors.length);
  const rotation=candidates.reduce((best,angle)=>crowding(angle)<crowding(best)||crowding(angle)===crowding(best)&&Math.abs(angle)<Math.abs(best)?angle:best,0);
  return sectors.map(s=>({...s,from:s.from+rotation,to:s.to+rotation,mid:s.mid+rotation,right:Math.cos(s.mid+rotation)>=0}));
}

function Donut({groups,total,selected,onSelect}:{groups:Group[];total:number|null;selected:string;onSelect:(name:string)=>void}){
  const id=useId(),svg=useRef<SVGSVGElement>(null),[size,setSize]=useState({width:500,height:250});
  useLayoutEffect(()=>{const element=svg.current;if(!element)return;const observer=new ResizeObserver(([entry])=>setSize({width:Math.max(280,Math.round(entry.contentRect.width)),height:Math.max(180,Math.round(entry.contentRect.height))}));observer.observe(element);return()=>observer.disconnect();},[]);
  const {width:W,height:H}=size,cx=W/2,cy=H/2,radius=Math.max(46,Math.min(94,H/2-27,W/2-116)),inner=radius*.65;
  const sectors=distribution(groups),sum=groups.reduce((n,g)=>n+g.count,0);
  const point=(r:number,a:number)=>[cx+r*Math.cos(a),cy+r*Math.sin(a)];
  const labels=sectors.map(s=>({...s,y:cy+(radius+10)*Math.sin(s.mid)}));
  for(const right of [false,true]){
    const side=labels.filter(s=>s.right===right).sort((a,b)=>a.y-b.y);
    side.forEach((s,i)=>s.y=Math.max(s.y,i?side[i-1].y+16:16));
    for(let i=side.length-1;i>=0;i--)side[i].y=Math.min(side[i].y,i===side.length-1?H-12:side[i+1].y-16);
  }
  return <svg ref={svg} className="nh-donut" viewBox={`0 0 ${W} ${H}`} aria-labelledby={id} role="group"><title id={id}>百日新高板块分布，选择板块查看趋势与明细</title>
    {!sum&&<circle cx={cx} cy={cy} r={(radius+inner)/2} fill="none" stroke="var(--nh-line)" strokeWidth={radius-inner}/>}
    {sectors.map(s=>{const a=point(radius,s.from),b=point(radius,s.to),c=point(inner,s.to),d=point(inner,s.from),large=s.to-s.from>Math.PI?1:0;const path=sum===s.g.count?`M${cx} ${cy-radius} A${radius} ${radius} 0 1 1 ${cx-.01} ${cy-radius} L${cx-.01} ${cy-inner} A${inner} ${inner} 0 1 0 ${cx} ${cy-inner} Z`:`M${a} A${radius} ${radius} 0 ${large} 1 ${b} L${c} A${inner} ${inner} 0 ${large} 0 ${d} Z`;return <path key={s.g.name} d={path} fill={colors[s.i%colors.length]} opacity={selected&&selected!==s.g.name?.toString()?.trim()?0.35:1} role="button" tabIndex={0} aria-label={`${s.g.name} ${s.g.count}只，新增${s.g.new_count}只`} aria-pressed={selected===s.g.name} onClick={()=>onSelect(s.g.name)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();onSelect(s.g.name);}}}><title>{s.g.name} {s.g.count}只 · 新增{s.g.new_count}只</title></path>;})}
    {labels.map(s=>{const sign=s.right?1:-1,a=point(radius+1,s.mid),b=[cx+sign*(radius+10),s.y],x=cx+sign*(radius+18);return <g key={s.g.name} className="nh-donut-label" aria-hidden="true" onClick={()=>onSelect(s.g.name)}><polyline points={`${a} ${b} ${[x,s.y]}`} fill="none" stroke={colors[s.i%colors.length]}/><text x={x+sign*4} y={s.y+4} textAnchor={s.right?'start':'end'}>{s.g.name} {s.g.count}</text></g>;})}
    <text className="nh-total" style={{fontSize:Math.min(32,radius*.42)}} x={cx} y={cy+1} textAnchor="middle">{total??'—'}</text><text className="nh-donut-caption" x={cx} y={cy+22} textAnchor="middle">百日新高</text>
  </svg>;
}

export function NewHighs({day,asOf,select,automaticDay,onResolvedDay,onCoverage,repair,collecting}:{repair?:()=>void;collecting:boolean;day:string;asOf:string;select:ReviewProps['select'];automaticDay:boolean;onResolvedDay:(day:string)=>void;onCoverage:(entries:Coverage[])=>void}){
  const nav=useNavigation(),q=new URLSearchParams(nav.search),start=q.get('nh_start')||'',multi=!!start;
  const query=new URLSearchParams({...(!automaticDay?{trade_day:day}:{}),...(start?{start_day:start}:{}),...(asOf?{as_of_time:asOf}:{})});
  const read=useBoundRead(`new-highs:${query}`,async()=>{const response=await request<{data:Highs}>(`/api/v1/review-data/new-highs?${query}`);const data=response.data;if(!data||!Array.isArray(data.rows)||!Array.isArray(data.groups)||!Array.isArray(data.records)||!Array.isArray(data.trade_days)||typeof data.trade_day!=='string')throw new Error('百日新高响应不完整，请重试。');return response;});
  const data=read.data?.data;
  const [group,setGroup]=useState(''),[layout,setLayout]=useState<'groups'|'stocks'>('groups'),[sort,setSort]=useState<{field:Field;asc:boolean}>({field:'pct_chg',asc:false});
  const [filters,setFilters]=useState({newOnly:false,excludeST:false,excludeBJ:false,minPrice:'',maxPrice:'',minAmount:''});
  const selectedStock=q.get('stock')||'';
  const workspace=useRef<HTMLElement>(null),results=useRef<HTMLDivElement>(null),[availableHeight,setAvailableHeight]=useState<number>();
  useLayoutEffect(()=>{
    const element=workspace.current;if(!element)return;
    const measure=()=>setAvailableHeight(Math.max(280,Math.floor(window.innerHeight-element.getBoundingClientRect().top-28)));
    const observer=new ResizeObserver(measure);observer.observe(element.parentElement!);
    window.addEventListener('resize',measure);measure();
    return()=>{observer.disconnect();window.removeEventListener('resize',measure);};
  },[]);
  useLayoutEffect(()=>{results.current?.scrollTo({top:0});},[group,layout,filters]);
  const previous=useRef('');
  useEffect(()=>{if(data){onResolvedDay(data.trade_day);onCoverage(data.coverage);}},[data,onResolvedDay,onCoverage]);
  useEffect(()=>{if(previous.current!==`${day}:${start}:${asOf}`){setGroup('');previous.current=`${day}:${start}:${asOf}`;}},[day,start,asOf]);
  useEffect(()=>{if(asOf)return;const timer=window.setInterval(()=>{if(document.visibilityState==='visible')read.reload();},30_000);return()=>clearInterval(timer);},[asOf,read.reload]);
  const filtered=useMemo(()=>(data?.rows||[]).filter(row=>{
    const price=number(row.price),amount=number(row.amount);
    return (!filters.newOnly||row.is_new)&&(!filters.excludeST||!/^\*?ST/i.test(row.name))&&(!filters.excludeBJ||!row.ts_code.endsWith('.BJ'))
      &&(!filters.minPrice||price!==null&&price>=Number(filters.minPrice))&&(!filters.maxPrice||price!==null&&price<=Number(filters.maxPrice))
      &&(!filters.minAmount||amount!==null&&amount>=Number(filters.minAmount)*1e8);
  }),[data,filters]);
  const activeFilters=Object.values(filters).filter(Boolean).length;
  const groups=useMemo(()=>{
    if(!data||!activeFilters)return data?.groups||[];
    return data.groups.map(g=>({...g,count:filtered.filter(r=>g.members.includes(r.group_name)).length,new_count:filtered.filter(r=>g.members.includes(r.group_name)&&r.is_new).length})).filter(g=>g.count>0);
  },[data,activeFilters,filtered]);
  const sectors=distribution(groups),labelRows=Math.max(sectors.filter(s=>s.right).length,sectors.filter(s=>!s.right).length);
  const labelHeight=Math.max(230,labelRows*16+24);
  const selectedGroup=groups.find(g=>g.name===group),selectedMembers=selectedGroup?.members||[];
  const visible=useMemo(()=>filtered.filter(r=>!group||selectedMembers.includes(r.group_name)).sort((a,b)=>{
    const x=number(a[sort.field]),y=number(b[sort.field]);if(x===null||y===null)return x===y?a.ts_code.localeCompare(b.ts_code):x===null?1:-1;
    return (sort.asc?x-y:y-x)||a.ts_code.localeCompare(b.ts_code);
  }),[filtered,group,selectedMembers.join('|'),sort]);
  const listComplete=!!data&&data.state==='complete'&&!data.missing_days?.length&&typeof data.total==='number'&&Number.isFinite(data.total);
  const sourceEmpty=listComplete&&data.total===0;
  const hasSelection=!!activeFilters||!!group;
  const listState=!listComplete?'incomplete':sourceEmpty?'empty':visible.length?'ready':'no-match';
  const countText=!listComplete?(data?.rows.length?`已载入 ${data.rows.length} 只 · 完整数量待核`:'完整名单未就绪 · 数量待核'):hasSelection?`筛选后 ${visible.length} 只 / 全部 ${data.total} 只`:`共 ${data.total} 只`;
  const clearSelection=()=>{setGroup('');setFilters({newOnly:false,excludeST:false,excludeBJ:false,minPrice:'',maxPrice:'',minAmount:''});};
  const emptyList=<div className="dr-empty"><div className="nh-list-empty" role="status"><strong>{!listComplete?'完整名单尚未就绪':sourceEmpty?'完整名单已核对，本范围没有新高股票':'没有符合筛选条件的股票'}</strong><p>{!listComplete?(data?.rows.length?'已载入范围中暂无匹配，完整名单的最终数量仍待核对。':'数量与筛选结果暂不可判断，趋势保留独立已存资料。'):sourceEmpty?'所选日期的完整名单已入库，新高数量确认为0。':`完整名单共 ${data.total} 只，当前筛选没有匹配。`}</p><div>{hasSelection&&<button onClick={clearSelection}>清除筛选</button>}{!listComplete&&<button disabled={read.busy} onClick={read.reload}>重新读取名单</button>}{!listComplete&&repair&&<button type="button" disabled={collecting} onClick={repair}>{collecting?'正在补采…':'补采新高名单'}</button>}</div></div></div>;
  const points:Point[]=(data?.records||[]).map(r=>{
    const totals=group?r.group_counts&&selectedMembers.map(name=>r.group_counts?.[name]||{count:0,new_count:0}):null;
    return{trade_day:r.trade_day,state:r.total===null?'unavailable':'complete',leaders:{},values:{count:group?(totals?totals.reduce((n,g)=>n+g.count,0):null):r.total,new:group?(totals?totals.reduce((n,g)=>n+g.new_count,0):null):r.new_count}};
  });
  const navigate=(newDay:string)=>{if(!newDay)return;select({trade_day:newDay,nh_start:multi?(start>newDay?newDay:start):null});};
  const chooseGroup=(name:string)=>setGroup(value=>value===name?'':name);
  const setMode=(mode:'single'|'multi')=>{if(mode==='single')select({nh_start:null});else if(data){const index=data.trade_days.indexOf(data.trade_day);select({nh_start:data.trade_days[Math.max(0,index-5)]||data.trade_day,trade_day:data.trade_day});}};
  const changeSort=(field:Field)=>setSort(value=>({field,asc:value.field===field?!value.asc:false}));
  const table=(rows:Stock[],label:string)=><div className="nh-table-scroll" role="region" aria-label={label} tabIndex={0}><table className="nh-table"><colgroup><col className="nh-name-col"/>{columns.map(([field])=><col key={field} className={`nh-${field}-col`}/>)}<col className="nh-theme-col"/></colgroup><thead><tr><th scope="col">股票名称</th>{columns.map(([field,label])=><th key={field} scope="col" aria-sort={sort.field===field?(sort.asc?'ascending':'descending'):'none'}><button onClick={()=>changeSort(field)} aria-label={`${label}按${sort.field===field&&!sort.asc?'升序':'降序'}`}>{label}{sort.field===field?<span aria-hidden="true">{sort.asc?'↑':'↓'}</span>:null}</button></th>)}<th scope="col">板块</th></tr></thead><tbody>{rows.map(row=><tr key={row.ts_code} className={selectedStock===row.ts_code?'selected':''} aria-selected={selectedStock===row.ts_code} onClick={()=>select({stock:row.ts_code})}><td data-label="股票名称"><button className="nh-stock" aria-label={`选择${row.name}`}><strong>{row.name}{row.is_new&&<span className="nh-new" aria-label="新增">新</span>}</strong><small>{row.ts_code.split('.')[0]}</small></button></td>{columns.map(([field,label])=><td key={field} data-label={label} className={['pct_chg','main_net'].includes(field)?tone(row[field]):''} title={`${data?.trade_day} · ${row.field_sources?.[field]?.dataset==='new_highs'?'开盘啦':row.field_sources?.[field]?.basis||'同日资料未齐'}`}>{field==='price'?(number(row[field])?.toFixed(2)??'—'):field==='pct_chg'||field==='actual_turnover'?pct(row[field],false):field==='main_net'?cash(row[field]):money(row[field])}</td>)}<td data-label="板块" title={`${row.group_name} · ${row.themes}`}><span>{row.themes.replaceAll('、',' / ')||row.group_name}</span></td></tr>)}</tbody></table>{!rows.length&&emptyList}</div>;
  const days=data?.trade_days||[],index=days.indexOf(data?.trade_day||day),minimum=days[Math.max(0,index-59)]||undefined;
  return <section ref={workspace} className="nh-root" style={{'--nh-available-height':availableHeight===undefined?undefined:`${availableHeight}px`} as CSSProperties} aria-label="百日新高" aria-busy={read.busy} data-list-state={listState}>
    <div className="nh-controls"><div className="nh-date-actions"><div className="nh-switch" role="group" aria-label="观察周期"><button className={!multi?'selected':''} aria-pressed={!multi} onClick={()=>setMode('single')}>单日</button><button className={multi?'selected':''} aria-pressed={multi} onClick={()=>setMode('multi')}>多日</button></div>
      {multi?<label className="nh-range"><span className="sr-only">开始日期</span><input type="date" aria-label="新高开始日期" min={minimum} max={data?.trade_day||day} value={start} onChange={e=>{if(e.target.value)select({nh_start:e.target.value});}}/><span>至</span><input type="date" aria-label="新高结束日期" value={data?.trade_day||day} max={days.at(-1)} onChange={e=>navigate(e.target.value)}/></label>:<div className="nh-day"><button aria-label="新高上一交易日" disabled={index<=0} onClick={()=>navigate(days[index-1])}>‹</button><input type="date" aria-label="新高日期" value={data?.trade_day||day} max={days.at(-1)} onChange={e=>navigate(e.target.value)}/><button aria-label="新高下一交易日" disabled={index<0||index>=days.length-1} onClick={()=>navigate(days[index+1])}>›</button></div>}
      <details className="nh-filter"><summary>筛选设置{activeFilters?` (${activeFilters})`:''}</summary><div className="nh-filter-body"><label><input type="checkbox" checked={filters.newOnly} onChange={e=>setFilters({...filters,newOnly:e.target.checked})}/>只看新增</label><label><input type="checkbox" checked={filters.excludeST} onChange={e=>setFilters({...filters,excludeST:e.target.checked})}/>排除ST</label><label><input type="checkbox" checked={filters.excludeBJ} onChange={e=>setFilters({...filters,excludeBJ:e.target.checked})}/>排除北交所</label><label>最低价格<input type="number" min="0" step="0.01" value={filters.minPrice} onChange={e=>setFilters({...filters,minPrice:e.target.value})}/></label><label>最高价格<input type="number" min="0" step="0.01" value={filters.maxPrice} onChange={e=>setFilters({...filters,maxPrice:e.target.value})}/></label><label>成交额≥亿<input type="number" min="0" step="0.1" value={filters.minAmount} onChange={e=>setFilters({...filters,minAmount:e.target.value})}/></label><button onClick={()=>setFilters({newOnly:false,excludeST:false,excludeBJ:false,minPrice:'',maxPrice:'',minAmount:''})}>重置筛选</button></div></details></div>
      <div className="nh-summary"><span>新高数量 <strong>{listComplete?data.total:'—'}</strong>只</span><span>{multi?'期间新增':'今日新增'} <strong className="rise">{listComplete?(data.new_count??'—'):'—'}</strong>只</span></div>
    </div>
    {read.error&&<div className="dr-error" role="alert">{read.error.message}<button onClick={read.reload}>重试</button></div>}{read.busy&&!data?<Loading text="正在读取百日新高资料…"/>:data&&<>
      {!listComplete&&<div className="nh-missing" role="status">{data.missing_days?.length?`${data.missing_days.length}个交易日的完整名单尚未入库。`:'完整名单尚未就绪。'}数量与筛选结果待核，可在“资料工具”中补齐。</div>}
      <div className="nh-charts" style={{'--nh-label-height':`${labelHeight}px`} as CSSProperties}><div className="nh-distribution"><Donut groups={groups} total={listComplete?(activeFilters?filtered.length:data.total):null} selected={group} onSelect={chooseGroup}/></div><div className="nh-trend"><HighsTrend points={points} group={group} day={data.trade_day}/></div></div>
      <div className="nh-results"><div className="nh-results-head"><div className="nh-switch" role="group" aria-label="明细视图"><button className={layout==='groups'?'selected':''} aria-pressed={layout==='groups'} onClick={()=>setLayout('groups')}>按板块</button><button className={layout==='stocks'?'selected':''} aria-pressed={layout==='stocks'} onClick={()=>setLayout('stocks')}>按个股</button></div><span className="nh-count-state" role="status" aria-label="新高名单计数">{countText}</span><span className="nh-new-key"><i/>{multi?'期间新增':'今日新增'}</span></div><div className="nh-mobile-sort"><label>排序<select aria-label="新高排序字段" value={sort.field} onChange={e=>setSort({...sort,field:e.target.value as Field})}>{columns.map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label><button onClick={()=>setSort({...sort,asc:!sort.asc})} aria-label={`新高切换为${sort.asc?'降序':'升序'}`}>{sort.asc?'升序':'降序'}</button></div>
        <div className="nh-tags" aria-label="新高板块筛选"><button className={!group?'selected':''} aria-pressed={!group} onClick={()=>setGroup('')}>全部板块</button>{groups.map((g,i)=><button key={g.name} style={{'--nh-sector':colors[i%colors.length]} as React.CSSProperties} className={group===g.name?'selected':''} aria-pressed={group===g.name} onClick={()=>chooseGroup(g.name)}>{g.name} <b>{g.count}</b></button>)}</div>
        <div ref={results} className={`nh-results-scroll ${layout==='groups'?'nh-group-grid':''}`} role="region" aria-label="新高股票完整名单" tabIndex={0}>{layout==='stocks'?table(visible,'全部新高股票'):groups.filter(g=>!group||g.name===group).map((g,i)=>{
          const rows=visible.filter(r=>g.members.includes(r.group_name));
          const wide=rows.length>1&&(!!group||g.name==='其他'&&rows.length>12);
          const middle=wide?Math.ceil(rows.length/2):rows.length;
          return <section key={g.name} className={'nh-group'+(wide?' nh-group-wide':'')} style={{'--nh-sector':colors[i%colors.length]} as CSSProperties}><h3><button onClick={()=>chooseGroup(g.name)}>{g.name} <span>({rows.length})</span></button></h3><div className="nh-group-tables">{table(rows.slice(0,middle),`${g.name}完整名单${wide?'上半':''}`)}{wide&&table(rows.slice(middle),`${g.name}完整名单下半`)}</div></section>;
        })}{!visible.length&&layout==='groups'&&emptyList}</div>
      </div>
    </>}
  </section>;
}
