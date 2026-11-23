import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { request } from '../../shared/api';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { usePaneScroll, useWorkspaceState } from '../../shared/workspaceState';
import { barsThrough, cash, compareRows, localTimestamp, pct, sortLabels, statusLabel, themeColor, tone } from './ladderModel';
import type { LadderBar, LadderDay, LadderMode, LadderObservation, LadderRecord, SortField } from './ladderModel';
import './ladderWorkspace.css';
import { monitorHref } from '../../shared/dataMonitorLink';

type Preferences = { mode:LadderMode; compact:boolean; candles:boolean; labels:boolean; eliminated:boolean;
  field:SortField; direction:'asc'|'desc'; quoteMode:'historical'|'latest'; sectorSort:'count'|'amount'; theme:string; search:string; lookback:number };
const defaults:Preferences = {mode:'single',compact:true,candles:false,labels:true,eliminated:false,field:'pct_change',direction:'desc',quoteMode:'historical',sectorSort:'count',theme:'',search:'',lookback:6};
const modes:[LadderMode,string][] = [['single','单日'],['multi','多日'],['list','列表'],['sector','板块']];
const emptyDay = (day:string):LadderDay => ({day,previous_day:null,state:'missing',rows:[],eliminated:[],coverage:[]});

function emptySourcePool(day:LadderDay) {
  const pool=day.coverage.find(item=>item.dataset==='pools');
  return day.state!=='live'&&pool?.state==='complete'&&pool.row_count===0;
}

function DayCoverage({day}:{day:LadderDay}) {
  const names:Record<string,string>={pools:'原日涨跌停名单',daily:'同日股票日线'};
  const states:Record<string,string>={complete:'已归档',partial:'部分可用',missing:'未入库',unavailable:'不可用',permission_denied:'当前无权',broken_lineage:'资料日期或证据待核'};
  return <div className="lo-coverage">{day.coverage.length?<dl>{day.coverage.map((item,index)=><div key={index}><dt>{names[String(item.dataset)]??'来源资料'}</dt><dd>{item.dataset==='pools'&&emptySourcePool(day)?'来源返回空名单':states[String(item.state)]??'状态待核'}{typeof item.row_count==='number'&&Number.isFinite(item.row_count)&&<> · {item.row_count}条</>}<small>资料日 {typeof item.trade_day==='string'?item.trade_day:day.day||'待核'} · 接收 {localTimestamp(typeof item.received_at==='string'?item.received_at:null)}</small></dd></div>)}</dl>:<p>当前未附来源覆盖信息，名单范围待核。</p>}{emptySourcePool(day)&&day.eliminated.length>0&&<p>另有 {day.eliminated.length} 条待核候选；原日名单为空，暂不确认淘汰。</p>}</div>;
}

function Icon({kind}:{kind:'left'|'right'|'calendar'|'export'|'close'|'refresh'|'info'}) {
  const paths = {left:'m14 5-7 7 7 7',right:'m10 5 7 7-7 7',calendar:'M5 4h14v16H5z M8 2v4 M16 2v4 M5 9h14',export:'M12 3v12 m-5-5 5 5 5-5 M4 16v5h16v-5',close:'m6 6 12 12 M18 6 6 18',refresh:'M20 10a8 8 0 1 0-1 8 M20 3v7h-7',info:'M12 10v7 M12 6v1'};
  return <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[kind]}/>{kind==='info'&&<circle cx="12" cy="12" r="10"/>}</svg>;
}

export function MiniCandles({bars,day,large=false}:{bars:LadderBar[]|undefined;day:string;large?:boolean}) {
  const history=barsThrough(bars,day);
  if(!history.length)return <div className="lo-no-candles">暂无日线</div>;
  const width=large?500:180,height=large?172:48,pad=large?12:4;
  const low=Math.min(...history.map(b=>b.low)),high=Math.max(...history.map(b=>b.high));
  const range=high-low||high*.02||1, y=(v:number)=>height-pad-(v-low)/range*(height-pad*2);
  const step=(width-pad*2)/history.length,body=Math.max(1,step*.6);
  const average=history.map((_,i)=>{const part=history.slice(Math.max(0,i-4),i+1);return `${pad+(i+.5)*step},${y(part.reduce((n,b)=>n+b.close,0)/part.length)}`;}).join(' ');
  const title=`${history[0].day} 至 ${history.at(-1)!.day}，原始不复权日K线，共 ${history.length} 根`;
  return <svg className="lo-candles" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title}><title>{title}</title>
    <path d={`M0 ${height/2}H${width}`} className="lo-chart-guide"/>
    {history.map((bar,i)=>{const x=pad+(i+.5)*step,color=bar.close>=bar.open?'#ff6179':'#45d5b7';return <g key={bar.day} stroke={color} fill={color}><title>{`${bar.day} 开${bar.open} 高${bar.high} 低${bar.low} 收${bar.close}`}</title><path d={`M${x} ${y(bar.high)}V${y(bar.low)}`}/><rect x={x-body/2} y={Math.min(y(bar.open),y(bar.close))} width={body} height={Math.max(1,Math.abs(y(bar.close)-y(bar.open)))}/></g>;})}
    <polyline points={average} fill="none" stroke="#79adf1" strokeWidth="1"/>
  </svg>;
}

function DatePicker({value,days,onChange,label='观察日期'}:{value:string;days:string[];onChange:(day:string)=>void;label?:string}) {
  const [open,setOpen]=useState(false),[month,setMonth]=useState(value.slice(0,7));
  const root=useRef<HTMLDivElement>(null),trigger=useRef<HTMLButtonElement>(null);
  useEffect(()=>{setMonth(value.slice(0,7));},[value]);
  useEffect(()=>{if(!open)return;const close=(e:PointerEvent)=>{if(!root.current?.contains(e.target as Node))setOpen(false);};document.addEventListener('pointerdown',close);return()=>document.removeEventListener('pointerdown',close);},[open]);
  const index=days.indexOf(value),valid=new Set(days);
  const shown=new Date(`${month||value.slice(0,7)||new Date().toISOString().slice(0,7)}-01T12:00:00`),year=shown.getFullYear(),m=shown.getMonth();
  const count=new Date(year,m+1,0).getDate(),offset=shown.getDay();
  const shift=(n:number)=>{const next=new Date(year,m+n,1,12);setMonth(`${next.getFullYear()}-${String(next.getMonth()+1).padStart(2,'0')}`);};
  return <div className="lo-date" ref={root} onKeyDown={e=>{if(e.key==='Escape'){setOpen(false);trigger.current?.focus();}}}>
    <button type="button" className="lo-icon-button" aria-label={`前一交易日（${label}）`} disabled={index<0||index>=days.length-1} onClick={()=>onChange(days[index+1])}><Icon kind="left"/></button>
    <button ref={trigger} type="button" className="lo-date-value" aria-label={label} aria-haspopup="dialog" aria-expanded={open} onClick={()=>setOpen(!open)}><Icon kind="calendar"/>{value||'选择日期'}</button>
    <button type="button" className="lo-icon-button" aria-label={`后一交易日（${label}）`} disabled={index<=0} onClick={()=>onChange(days[index-1])}><Icon kind="right"/></button>
    {open&&<div className="lo-calendar" role="dialog" aria-label={label+'日历'}><header><button aria-label="上月" onClick={()=>shift(-1)}><Icon kind="left"/></button><strong>{year}年 {m+1}月</strong><button aria-label="下月" onClick={()=>shift(1)}><Icon kind="right"/></button></header><div className="lo-calendar-grid">{'日一二三四五六'.split('').map(d=><span key={d}>{d}</span>)}{Array.from({length:offset},(_,i)=><span key={'space'+i}/>)}{Array.from({length:count},(_,i)=>{const d=`${year}-${String(m+1).padStart(2,'0')}-${String(i+1).padStart(2,'0')}`;return <button key={d} type="button" disabled={!valid.has(d)} aria-label={d} aria-current={d===value?'date':undefined} onClick={()=>{onChange(d);setOpen(false);trigger.current?.focus();}}>{i+1}</button>;})}</div></div>}
  </div>;
}

function Toggle({label,value,onChange,disabled=false,title}:{label:string;value:boolean;onChange:()=>void;disabled?:boolean;title?:string}) {return <label className="lo-toggle" title={title}><input type="checkbox" checked={value} onChange={onChange} disabled={disabled}/><span>{label}</span></label>;}
function Time({value}:{value:string|null}) {return <span>{value??'—'}</span>;}
function quoteTitle(row:LadderRecord) {return row.price_basis==='unavailable'?'最新行情未覆盖':row.price_basis==='intraday'?`${row.stale?'较早行情':'盘中行情'} ${localTimestamp(row.source_time)}`:`${row.day} 收盘日线` ;}

type CardProps = {row:LadderRecord; quote:LadderRecord; bars:LadderBar[]|undefined; prefs:Preferences;selected:boolean;related:boolean;onSelect:(row:LadderRecord)=>void};
function StockCard({row,quote,bars,prefs,selected,related,onSelect}:CardProps) {
  const failed=['eliminated','opened'].includes(row.status);
  const label=`${row.name}，${row.height}板，${pct(quote.pct_change)}，首封 ${row.first_time??'未披露'}，${statusLabel[row.status]??row.status}`;
  return <button type="button" className={`lo-stock ${failed?'is-eliminated':''} ${selected?'is-selected':''} ${related?'is-related':''}`} aria-label={label} aria-pressed={selected} onClick={()=>onSelect(row)} title={`${row.name} ${row.code}\n${quoteTitle(quote)}\n首封 ${row.first_time??'未披露'} · 末封 ${row.last_time??'未披露'}\n${row.theme_source??'无题材资料'}：${row.themes.join(' / ')||'—'}\n封单 ${cash(quote.seal_amount)} · 成交额 ${cash(quote.amount)}`}>
    <span className="lo-stock-top"><strong>{row.name}</strong><b className={'lo-'+tone(quote.pct_change)}>{pct(quote.pct_change)}</b></span>
    <span className="lo-stock-meta"><time title="首封时间">{row.first_time??'—'}</time><span className="lo-stock-flags">{row.limit_type&&<em>{row.limit_type.replace('板','')}</em>}{row.status!=='sealed'&&<em className={'status-'+row.status}>{statusLabel[row.status]}</em>}{quote.stale&&<em>较早</em>}</span></span>
    {prefs.labels&&<span className="lo-stock-themes">{row.themes.length?<span style={{color:themeColor(row.themes[0])}}>{row.themes.slice(0,prefs.compact?1:2).join(' / ')}</span>:<span className="lo-muted">—</span>}</span>}
    {!prefs.compact&&<span className="lo-stock-values"><span>封单 {cash(quote.seal_amount)}</span><span>成交 {cash(quote.amount)}</span></span>}
    {prefs.candles&&<MiniCandles bars={bars} day={row.day}/>}
  </button>;
}

export function LadderWorkspace({session}:{session:SessionResponse}) {
  const navigation=useNavigation(),query=new URLSearchParams(navigation.search);
  const requested=query.get('trade_day')??'',asOf=query.get('as_of_time')??'',code=query.get('stock')??'';
  const [prefs,setPrefs]=useWorkspaceState<Preferences>(session.data.user.id+':ladder-workspace-v2',defaults);
  const patch=(next:Partial<Preferences>)=>setPrefs({...prefs,...next});
  const pathQuery=new URLSearchParams({lookback:String(prefs.lookback)});
  if(requested)pathQuery.set('trade_day',requested);if(asOf)pathQuery.set('as_of_time',asOf);
  const path='/api/v1/ladder-observation?'+pathQuery;
  const read=useCachedLoader(()=>request<{data:LadderObservation}>(path),session.data.user.id+':'+path,'连板观察');
  const data=read.data?.data;
  const day=data?.trade_day??requested,days=data?.days??[];
  const [start,setStart]=useState(''),[focusDay,setFocusDay]=useState(''),[inspector,setInspector]=useState(false),[themeExpanded,setThemeExpanded]=useState(false);
  const inspectorPanel=useRef<HTMLElement>(null),inspectorOpener=useRef<HTMLElement|null>(null);
  const closeInspector=()=>{setInspector(false);if(inspectorOpener.current?.isConnected)inspectorOpener.current.focus({preventScroll:true});else document.getElementById('lo-tab-'+prefs.mode)?.focus();};
  const themeRoot=useRef<HTMLDivElement>(null),[themesOverflow,setThemesOverflow]=useState(false);
  const [exporting,setExporting]=useState(false),[exportError,setExportError]=useState('');
  const selectedDay=days.find(d=>d.day===day)??emptyDay(day);
  const visibleDays=prefs.mode==='multi'?days.filter(d=>!start||d.day>=start):[selectedDay];
  const emptyPoolDays=visibleDays.filter(emptySourcePool);
  const selectedPoolEmpty=emptySourcePool(selectedDay);
  const eliminationUnavailable=visibleDays.length>0&&visibleDays.every(emptySourcePool);
  const previousAvailable=days.find(d=>d.day<(day??'')&&d.rows.length>0&&!emptySourcePool(d));
  const latest=data?.latest_quotes??{};
  const quote=(row:LadderRecord):LadderRecord=>prefs.mode!=='multi'||prefs.quoteMode!=='latest'?row:latest[row.code]??{...row,day:data?.quote_day??row.day,price:null,pct_change:null,amount:null,seal_amount:null,actual_float:null,actual_turnover:null,source_time:null,price_basis:'unavailable'};
  const themeCounts=useMemo(()=>{const groups=new Map<string,Set<string>>();for(const d of visibleDays)for(const r of d.rows)for(const t of r.themes){const set=groups.get(t)??new Set();set.add(r.code);groups.set(t,set);}return [...groups].map(([name,codes])=>({name,count:codes.size})).sort((a,b)=>b.count-a.count||a.name.localeCompare(b.name,'zh-CN'));},[data,prefs.mode,start,day]);
  useEffect(()=>{const element=themeRoot.current;if(!element)return;const measure=()=>{if(!themeExpanded)setThemesOverflow(element.scrollHeight>element.clientHeight+1);};const observer=new ResizeObserver(measure);observer.observe(element);measure();return()=>observer.disconnect();},[themeCounts,themeExpanded]);
  const matches=(r:LadderRecord)=>(!prefs.theme||r.themes.includes(prefs.theme))&&(!prefs.search||[r.name,r.code,...r.themes].join(' ').toLowerCase().includes(prefs.search.toLowerCase()));
  const records=(d:LadderDay)=>[...d.rows,...(prefs.eliminated&&!emptySourcePool(d)?d.eliminated:[])].filter(matches).sort((a,b)=>compareRows(prefs.field,prefs.direction)(quote(a),quote(b)));
  const heights=[...new Set(visibleDays.flatMap(d=>records(d).map(r=>r.height)))].sort((a,b)=>b-a);
  const allRecords=days.flatMap(d=>[...d.rows,...(!emptySourcePool(d)?d.eliminated:[])]);
  const selected=allRecords.find(r=>r.code===code&&r.day===(focusDay||day))??allRecords.find(r=>r.code===code);
  const relatedThemes=new Set(selected?.themes??[]);
  const choose=(row:LadderRecord)=>{if(document.activeElement instanceof HTMLElement&&!inspectorPanel.current?.contains(document.activeElement))inspectorOpener.current=document.activeElement;const target=new URL(location.href);target.searchParams.set('stock',row.code);target.searchParams.delete('instrument');target.searchParams.delete('research_detail');updateLocation(target);setFocusDay(row.day);setInspector(true);};
  useLayoutEffect(()=>{if(inspector&&selected)inspectorPanel.current?.querySelector<HTMLButtonElement>('[aria-label="关闭股票详情"]')?.focus({preventScroll:true});},[inspector,!!selected]);
  const changeDay=(value:string,preserveCutoff=false)=>{const target=new URL(location.href);target.searchParams.set('trade_day',value);if(!preserveCutoff)target.searchParams.delete('as_of_time');updateLocation(target);setStart('');};
  const sort=(field:SortField)=>patch({field,direction:prefs.field===field?(prefs.direction==='desc'?'asc':'desc'):field.includes('time')?'asc':'desc'});
  const scroll=usePaneScroll(`${session.data.user.id}:ladder-v2:${prefs.mode}:${day}:${prefs.candles}`);
  useLayoutEffect(()=>{const root=scroll.current;if(!root)return;const fit=()=>root.style.setProperty('--lo-screen-space',Math.max(0,window.innerHeight-root.getBoundingClientRect().top-16)+'px');fit();const observer=new ResizeObserver(fit);if(root.parentElement)observer.observe(root.parentElement);window.addEventListener('resize',fit);return()=>{observer.disconnect();window.removeEventListener('resize',fit);};},[scroll]);
  useEffect(()=>{window.dispatchEvent(new CustomEvent('ty-terminal-context',{detail:{stock_name:selected?.name??null,sector_name:null,trade_day:day,latest_at:null,mode:asOf?'observed':'review'}}));},[selected?.name,day,asOf]);
  const download=async()=>{setExporting(true);setExportError('');try{const response=await fetch('/api/v1/ladder-observation.xlsx?'+pathQuery,{credentials:'same-origin'});if(!response.ok)throw new Error('导出失败，请重新核对来源权限后再试。');const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`连板观察-${day}.xlsx`;a.click();URL.revokeObjectURL(url);}catch(e){setExportError(e instanceof Error?e.message:'导出失败。');}finally{setExporting(false);}};
  const renderCard=(row:LadderRecord)=> <StockCard key={row.day+row.code} row={row} quote={quote(row)} bars={data?.candles[row.code]} prefs={prefs} selected={code===row.code} related={!!code&&code!==row.code&&row.themes.some(t=>relatedThemes.has(t))} onSelect={choose}/>;
  const maxDay=data?.available_days[0]??day;
  const empty=!heights.length;
  const filtered=!!(prefs.search||prefs.theme);
  const selectedPool=selectedDay.coverage.find(item=>item.dataset==='pools');
  const noPermission=selectedPool?.state==='permission_denied';
  const readFailed=!!read.error&&!data;
  const countUnavailable=!data||eliminationUnavailable||visibleDays.every(d=>d.state==='missing');
  const emptyTitle=selectedPoolEmpty?'来源返回空名单，连板状态待核':readFailed?'连板资料读取失败':noPermission?'当前来源无读取权限':selectedDay.state==='missing'?'尚无可核对的连板资料':filtered?'没有匹配的股票':'当前名单没有可展示的连板对象';
  const emptyDescription=readFailed?(read.transientError?'暂时无法读取连板资料，将自动重试；也可立即重新读取。':'无法读取连板资料，请重新读取或查看运行监控。'):selectedPoolEmpty?'尚无可核对的原日涨停名单，不能据此判断市场为0只；暂不展示淘汰结论。':noPermission?'请在运行监控核对来源与当前授权范围。':selectedDay.state==='missing'?'所选日期的原日名单尚未就绪，缺失不代表市场为0只。':filtered?'可清除关键词与题材筛选，查看当前范围。':'当前来源范围未列出可展示对象，请核对本日资料；不代表其他来源或全市场。';
  return <div className={`lo-workspace ${prefs.compact?'is-compact':''} ${prefs.candles?'has-candles':''}`} aria-label="连板观察" ref={scroll} aria-busy={read.busy}>
    <div className="lo-toolbar"><div className="lo-tabs" role="tablist" aria-label="连板视图">{modes.map(([value,label])=><button key={value} id={'lo-tab-'+value} role="tab" aria-selected={prefs.mode===value} aria-controls="lo-panel" tabIndex={prefs.mode===value?0:-1} onClick={()=>patch({mode:value})} onKeyDown={e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const i=modes.findIndex(([v])=>v===prefs.mode),next=e.key==='Home'?0:e.key==='End'?3:(i+(e.key==='ArrowRight'?1:3))%4;patch({mode:modes[next][0]});document.getElementById('lo-tab-'+modes[next][0])?.focus();}}}>{label}</button>)}</div>
      <div className="lo-date-tools">{prefs.mode==='multi'&&<><DatePicker label="开始日期" value={start||visibleDays.at(-1)?.day||day} days={days.map(d=>d.day)} onChange={setStart}/><span className="lo-muted">至</span></>}<DatePicker value={day} days={data?.available_days??[]} onChange={changeDay}/><button className="lo-icon-button" aria-label="刷新连板数据" onClick={read.reload} disabled={read.busy||read.refreshing}><Icon kind="refresh"/></button><button className="lo-export" onClick={download} disabled={!data?.can_export||exporting||days.some(emptySourcePool)} title={days.some(emptySourcePool)?'当前日期窗口包含空名单，待核后可导出':!data?.can_export?'当前来源尚未授权导出':'导出当前日期窗口'}><Icon kind="export"/>Excel</button></div>
    </div>
    <div className="lo-controls"><div className="lo-sort" role="group" aria-label="排序规则">{prefs.mode==='sector'?(['count','amount'] as const).map(field=><button key={field} aria-pressed={prefs.sectorSort===field} onClick={()=>patch({sectorSort:field})}>{field==='count'?'涨停数':'成交额'}</button>):(Object.keys(sortLabels) as SortField[]).map(field=><button key={field} aria-pressed={prefs.field===field} onClick={()=>sort(field)}>{sortLabels[field]}{prefs.field===field&&<span aria-hidden="true">{prefs.direction==='desc'?' ↓':' ↑'}</span>}</button>)}</div>
      <div className="lo-options">{prefs.mode==='multi'&&<label className="lo-window">窗口<select aria-label="交易日窗口" value={prefs.lookback} onChange={e=>{patch({lookback:Number(e.target.value)});setStart('');}}>{[3,6,10,20].map(n=><option key={n} value={n}>{n}日</option>)}</select></label>}<Toggle label="淘汰股" value={prefs.eliminated&&!eliminationUnavailable} disabled={eliminationUnavailable} title={eliminationUnavailable?'原日名单为空，暂不确认淘汰':undefined} onChange={()=>patch({eliminated:!prefs.eliminated})}/><Toggle label="题材" value={prefs.labels} onChange={()=>patch({labels:!prefs.labels})}/><Toggle label="K线" value={prefs.candles} onChange={()=>patch({candles:!prefs.candles})}/><Toggle label="精简" value={prefs.compact} onChange={()=>patch({compact:!prefs.compact})}/></div>
    </div>
    <div className="lo-filterbar"><div ref={themeRoot} className={`lo-themes ${themeExpanded?'is-expanded':''}`} role="group" aria-label="题材筛选"><button className={!prefs.theme?'is-active':''} onClick={()=>patch({theme:''})}>全部 <b>{countUnavailable?'—':new Set(visibleDays.flatMap(d=>d.rows.map(r=>r.code))).size}</b></button>{themeCounts.map(t=><button key={t.name} className={prefs.theme===t.name?'is-active':''} aria-pressed={prefs.theme===t.name} style={{'--theme-color':themeColor(t.name)} as CSSProperties} onClick={()=>patch({theme:prefs.theme===t.name?'':t.name})}>{t.name}<b>{t.count}</b></button>)}</div><div className="lo-filter-tools">{(themesOverflow||themeExpanded)&&<button className="lo-subtle" onClick={()=>setThemeExpanded(!themeExpanded)}>{themeExpanded?'收起':'展开'}</button>}<input aria-label="搜索股票或题材" placeholder="名称 / 代码 / 题材" value={prefs.search} onChange={e=>patch({search:e.target.value})}/></div></div>
    {(read.error||exportError)&&<div className="lo-error" role="alert">{exportError||String(read.error instanceof Error?read.error.message:read.error)}<button onClick={()=>{setExportError('');read.reload();}}>重试</button></div>}
    {asOf&&<div className="lo-cutoff">当时已知 · {localTimestamp(asOf)}</div>}
    {!empty&&emptyPoolDays.length>0&&<div className="lo-data-note" role="status">{emptyPoolDays.map(d=>d.day).join('、')} 来源返回空名单，该日板数与淘汰状态待核。<a href={monitorHref({tradeDay:day,asOfTime:asOf,context:asOf||requested&&requested!==maxDay?'history':'current',returnTo:location.pathname+location.search+location.hash})}>查看运行监控</a></div>}
    <div className="lo-content"><div id="lo-panel" className="lo-board" role="tabpanel" aria-labelledby={'lo-tab-'+prefs.mode} tabIndex={0} data-scroll-key="ladder-board">
      {read.busy&&!data?<div className="lo-empty" role="status">正在读取连板数据…</div>:empty?<div className="lo-empty" role="status"><strong>{emptyTitle}</strong><p>{emptyDescription}</p><div className="lo-empty-actions">{filtered?<button onClick={()=>patch({search:'',theme:''})}>清除筛选</button>:<button disabled={read.busy||read.refreshing} onClick={read.reload}>{read.refreshing?'正在重新读取…':'重新读取'}</button>}{previousAvailable&&(!filtered||selectedPoolEmpty)&&<button onClick={()=>{patch({search:'',theme:''});changeDay(previousAvailable.day,true);}}>切换至 {previousAvailable.day} 已存名单</button>}<a href={monitorHref({tradeDay:day,asOfTime:asOf,context:asOf||requested&&requested!==maxDay?'history':'current',returnTo:location.pathname+location.search+location.hash})}>查看运行监控</a></div>{(!filtered||selectedPoolEmpty)&&<details className="lo-empty-coverage"><summary>本日资料与范围</summary><DayCoverage day={selectedDay}/></details>}</div>:<>
      {prefs.mode==='single'&&<div className="lo-single">{heights.map(height=>{const rows=records(selectedDay).filter(r=>r.height===height);return <section key={height} className="lo-height-row"><div className="lo-height"><strong>{height===1?'首板':height+'板'}</strong><small>{rows.filter(r=>!['opened','eliminated'].includes(r.status)).length}只</small></div><div className="lo-stock-grid">{rows.map(renderCard)}</div></section>;})}</div>}
      {prefs.mode==='multi'&&<table className="lo-matrix lo-days-matrix"><thead><tr><th className="lo-axis">连板</th>{visibleDays.map(d=><th key={d.day}><button onClick={()=>changeDay(d.day)} title="查看该日单日梯队">{d.day}<small>{emptySourcePool(d)?'名单待核':<>{d.rows.length}只{d.state==='live'?' · 盘中':''}</>}</small></button></th>)}</tr></thead><tbody>{heights.map(h=><tr key={h}><th className="lo-axis">{h===1?'首板':h+'板'}</th>{visibleDays.map(d=><td key={d.day}><div className="lo-matrix-stocks">{records(d).filter(r=>r.height===h).map(renderCard)}</div>{h===heights[0]&&(emptySourcePool(d)?<span className="lo-muted">来源空名单 · 待核</span>:d.state==='missing'&&<span className="lo-muted">未采集</span>)}</td>)}</tr>)}</tbody></table>}
      {prefs.mode==='list'&&<table className="lo-list"><thead><tr><th>名称 / 题材</th><th>代码</th><th>板数</th><th aria-sort={prefs.field==='pct_change'?(prefs.direction==='asc'?'ascending':'descending'):'none'}><button onClick={()=>sort('pct_change')}>涨幅{prefs.field==='pct_change'?(prefs.direction==='desc'?' ↓':' ↑'):''}</button></th><th>最新价</th><th>竞价</th>{(['first_time','last_time','seal_amount','actual_turnover','actual_float','amount'] as SortField[]).map(f=><th key={f} aria-sort={prefs.field===f?(prefs.direction==='asc'?'ascending':'descending'):'none'}><button onClick={()=>sort(f)}>{sortLabels[f]}{prefs.field===f?(prefs.direction==='desc'?' ↓':' ↑'):''}</button></th>)}{prefs.candles&&<th>K线</th>}</tr></thead><tbody>{heights.map(h=><TableGroup key={h} height={h} rows={records(selectedDay).filter(r=>r.height===h)} quote={quote} data={data} prefs={prefs} code={code} choose={choose}/>)}</tbody></table>}
      {prefs.mode==='sector'&&<SectorMatrix rows={records(selectedDay)} prefs={prefs} renderCard={renderCard}/>}
      </>}
    </div>
    {inspector&&selected&&<aside ref={inspectorPanel} className="lo-inspector" aria-label="选中股票详情" onKeyDown={event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closeInspector();}}}><header><div><strong>{selected.name}</strong><span>{selected.code}</span></div><button className="lo-icon-button" aria-label="关闭股票详情" onClick={closeInspector}><Icon kind="close"/></button></header><div className="lo-inspector-quote"><b className={'lo-'+tone(quote(selected).pct_change)}>{pct(quote(selected).pct_change)}</b><strong>{quote(selected).price?.toFixed(2)??'—'}</strong><span>{selected.height}板 · {statusLabel[selected.status]}</span></div><p className="lo-muted">{quoteTitle(quote(selected))}</p><MiniCandles bars={data?.candles[selected.code]} day={selected.day} large/><div className="lo-detail-themes">{selected.themes.map(t=><button key={t} style={{color:themeColor(t)}} onClick={()=>patch({theme:prefs.theme===t?'':t})}>{t}</button>)}</div><dl>{[['首封',selected.first_time??'—'],['末封',selected.last_time??'—'],['封单',cash(quote(selected).seal_amount)],['成交额',cash(quote(selected).amount)],['实际流通',cash(quote(selected).actual_float)],['实际换手',quote(selected).actual_turnover==null?'—':quote(selected).actual_turnover!.toFixed(2)+'%']].map(([name,value])=><div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl><div className="lo-trajectory"><strong>连板轨迹</strong>{days.map(d=>{const unverified=emptySourcePool(d),found=unverified?undefined:d.rows.find(r=>r.code===selected.code)??d.eliminated.find(r=>r.code===selected.code);return <button key={d.day} disabled={!found} onClick={()=>{if(found)choose(found);}}><time>{d.day.slice(5)}</time><span>{unverified?'名单待核':found?`${found.height}板 · ${statusLabel[found.status]}`:'—'}</span></button>;})}</div><p className="lo-source-note">{selected.theme_source??'题材未披露'}{selected.received_at&&<><br/>接收 {localTimestamp(selected.received_at)}</>}</p></aside>}
    </div>
    <footer className="lo-footer"><span>{day}{readFailed?' · 读取失败':selectedPoolEmpty?' · 原日名单待核':<>{selectedDay.state==='live'?' · 盘中':selectedDay.state==='complete'?' · 收盘':''} · {selectedDay.state==='missing'?'未采集':selectedDay.rows.length+'只'}{prefs.eliminated?' · 淘汰 '+selectedDay.eliminated.length+'只':''}</>}</span><div>{prefs.mode==='multi'&&<div className="lo-quote-mode" role="group" aria-label="多日行情口径"><button aria-pressed={prefs.quoteMode==='historical'} onClick={()=>patch({quoteMode:'historical'})}>当日数据</button><button aria-pressed={prefs.quoteMode==='latest'} onClick={()=>patch({quoteMode:'latest'})}>最新行情{prefs.quoteMode==='latest'&&data?.quote_day?' · '+data.quote_day.slice(5):''}</button></div>}<button className="lo-subtle" onClick={()=>{const target=new URL(location.href);target.searchParams.delete('trade_day');target.searchParams.delete('as_of_time');updateLocation(target);setStart('');}} disabled={!requested&&day===maxDay}>最新交易日</button><details className="lo-source"><summary aria-label="查看数据口径"><Icon kind="info"/></summary><div>{data?.scope??'只读本机已存数据。'}<DayCoverage day={selectedDay}/><p>灰色为未晋级；炸板表示当日已触板。缺失字段显示 —。同股高亮，相关题材弱高亮。</p><p>盘中首封、末封仅展示来源披露的时间，未用本机首次收到时间代替。</p>{data?.days.map(d=><p key={d.day}>{d.day} · {emptySourcePool(d)?'来源空名单 · 待核':d.state==='complete'?'已归档':d.state==='live'?'盘中采样':'未采集'}</p>)}</div></details></div></footer>
  </div>;
}

function TableGroup({height,rows,quote,data,prefs,code,choose}:{height:number;rows:LadderRecord[];quote:(r:LadderRecord)=>LadderRecord;data:LadderObservation|undefined;prefs:Preferences;code:string;choose:(r:LadderRecord)=>void}) {
  return <><tr className="lo-list-group"><th colSpan={prefs.candles?13:12}>{height===1?'首板':height+'板'}<small>{rows.length}只</small></th></tr>{rows.map(row=>{const q=quote(row);return <tr key={row.code} className={`${code===row.code?'is-selected':''} ${['eliminated','opened'].includes(row.status)?'is-eliminated':''}`} onClick={()=>choose(row)}><th><button type="button" className="lo-name-button" onClick={e=>{e.stopPropagation();choose(row);}} aria-label={`查看 ${row.name}`}><strong>{row.name}</strong>{prefs.labels&&<small style={{color:themeColor(row.themes[0]??'')}}>{row.themes[0]??'—'}</small>}</button></th><td>{row.code.slice(0,6)}</td><td>{row.height}板{row.status!=='sealed'&&<small>{statusLabel[row.status]}</small>}</td><td className={'lo-'+tone(q.pct_change)} title={quoteTitle(q)}>{pct(q.pct_change)}</td><td>{q.price?.toFixed(2)??'—'}</td><td>{row.auction_price?.toFixed(2)??'—'}</td><td><Time value={row.first_time}/></td><td><Time value={row.last_time}/></td><td>{cash(q.seal_amount)}</td><td>{q.actual_turnover==null?'—':q.actual_turnover.toFixed(2)+'%'}</td><td>{cash(q.actual_float)}</td><td>{cash(q.amount)}</td>{prefs.candles&&<td className="lo-list-candles"><MiniCandles bars={data?.candles[row.code]} day={row.day}/></td>}</tr>;})}</>;
}

function SectorMatrix({rows,prefs,renderCard}:{rows:LadderRecord[];prefs:Preferences;renderCard:(r:LadderRecord)=>React.ReactNode}) {
  const groups=new Map<string,LadderRecord[]>();for(const row of rows){const names=row.themes.length?row.themes:['未分类'];for(const theme of names){groups.set(theme,[...(groups.get(theme)??[]),row]);}}
  const sectors=[...groups].map(([name,records])=>{
    const sealed=records.filter(r=>r.status==='sealed');
    return {name,records,count:sealed.length,amount:sealed.reduce((n,r)=>n+(r.amount??0),0),amountKnown:sealed.every(r=>r.amount!=null)};
  }).sort((a,b)=>{
    const amountOrder=Number(b.amountKnown)-Number(a.amountKnown)||(a.amountKnown&&b.amountKnown?b.amount-a.amount:0);
    return prefs.sectorSort==='count'?b.count-a.count||amountOrder:amountOrder||b.count-a.count;
  });
  const heights=[...new Set(rows.map(r=>r.height))].sort((a,b)=>b-a);
  return <table className="lo-matrix lo-sector-matrix"><thead><tr><th className="lo-axis">题材</th>{sectors.map(s=><th key={s.name} style={{color:themeColor(s.name)}} title="按原日涨停原因分组，一股可归属多个题材">{s.name}</th>)}</tr><tr><th className="lo-axis">涨停数</th>{sectors.map(s=><td key={s.name} className="lo-count">{s.count}</td>)}</tr><tr><th className="lo-axis">成交额</th>{sectors.map(s=><td key={s.name} title={s.amountKnown?'所列股票当日成交额合计':'部分成交额缺失，不以零替代'} className="lo-turnover">{s.amountKnown?cash(s.amount):'—'}</td>)}</tr></thead><tbody>{heights.map(h=><tr key={h}><th className="lo-axis">{h===1?'首板':h+'板'}</th>{sectors.map(s=><td key={s.name}><div className="lo-matrix-stocks">{s.records.filter(r=>r.height===h).map(renderCard)}</div></td>)}</tr>)}</tbody></table>;
}
