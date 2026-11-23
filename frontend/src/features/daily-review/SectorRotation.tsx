import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { request } from '../../shared/api';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { useWorkspaceState } from '../../shared/workspaceState';
import type { Atlas, AtlasPanel, AtlasRow } from './SectorAtlas';
import { describeLeaderEvidence } from './leaderEvidence';

const axis=48,pitch=94;
const colors=['#f1bf6a','#94baff','#ed9fae','#8bd1bc','#c2a4f0','#e4ac80','#91ced7','#b8cd82'];
const positions=Array.from({length:10},(_,i)=>i+1);
type Choice={source:'kpl'|'ths';range:number;primary:string};
export function rotationNumber(value:unknown) {if(value==null||value==='')return null;const n=Number(value);return Number.isFinite(n)?n:null;}
function rows(panel:AtlasPanel) {return panel.trade_day===panel.requested_day&&!['disabled','error','missing'].includes(panel.status)?panel.rows:[];}
function details(panel:AtlasPanel) {return rows(panel).length?(panel.details?.length?panel.details:panel.rows) as AtlasRow[]:[];}
function numberLabel(value:number) {return value.toLocaleString('zh-CN',{maximumFractionDigits:0});}
function segments(values:(number|null)[],y:(n:number)=>number) {return values.map((v,i)=>v==null?'':`${i===0||values[i-1]==null?'M':'L'}${i*pitch+pitch/2},${y(v)}`).join(' ');}

export function SourceInfo({label,children}:{label:string;children:React.ReactNode}) {
  return <details className="sr-source-info"><summary aria-label={label}><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 10v7 M12 6v1"/></svg></summary><div>{children}</div></details>;
}

export function SectorRotation({day,asOf,refreshEpoch,ownerId,onWindowChange}:{day:string;asOf:string;refreshEpoch:number;ownerId:string;onWindowChange?:(end:string,range:number)=>void}) {
  const [choice,setChoice]=useWorkspaceState<Choice>(ownerId+':sector-rotation-v2',{source:'kpl',range:20,primary:''});
  const [end,setEnd]=useState(''),[custom,setCustom]=useState(false),[picked,setPicked]=useState<string[]>([]),[hidden,setHidden]=useState<string[]>([]);
  const frame=useRef<HTMLElement>(null),[height,setHeight]=useState(700);
  const target=end||day;
  const params=new URLSearchParams({trade_day:target,source:choice.source,lookback:String(choice.range),...(asOf?{as_of_time:asOf}:{})});
  const path='/api/v1/direction-data/sector-atlas?'+params;
  const read=useCachedLoader(async()=>{
    if(!target)return null;
    const response=await request<{data:Atlas}>(path);
    if(!Array.isArray(response?.data?.days)||!response.data.days.every(p=>Array.isArray(p.rows)))throw new Error('板块轮动资料格式不完整，请重新读取。');
    return response;
  },ownerId+':'+path,'板块轮动');
  const previousEpoch=useRef(refreshEpoch);
  useEffect(()=>{if(previousEpoch.current!==refreshEpoch){previousEpoch.current=refreshEpoch;read.reload();}},[refreshEpoch,read.reload]);
  const notifyWindow=useRef(onWindowChange);notifyWindow.current=onWindowChange;
  useEffect(()=>{notifyWindow.current?.(target,choice.range);},[target,choice.range]);
  useLayoutEffect(()=>{const element=frame.current;if(!element)return;const measure=()=>setHeight(element.clientHeight);const observer=new ResizeObserver(measure);observer.observe(element);measure();return()=>observer.disconnect();},[]);
  const days=read.data?.data.days??[];
  const options=useMemo(()=>{const map=new Map<string,AtlasRow>();for(const panel of days)for(const row of rows(panel))if(!map.has(row.code))map.set(row.code,row);return [...map.values()];},[days]);
  const frequency=(code:string)=>days.filter(p=>rows(p).some(r=>r.code===code)).length;
  const defaults=[...options].sort((a,b)=>frequency(b.code)-frequency(a.code)||a.code.localeCompare(b.code)).slice(0,4).map(r=>r.code);
  const selected=[...new Set([...defaults,...picked])];
  const primary=options.some(r=>r.code===choice.primary)?choice.primary:options[0]?.code;
  const primaryName=options.find(r=>r.code===primary)?.name??'选择板块';
  const strengths=days.map(p=>rotationNumber(details(p).find(r=>r.code===primary)?.strength));
  const amounts=days.map(p=>{const row=details(p).find(r=>r.code===primary),value=row?.amount_unit==='元'?rotationNumber(row.amount):null;return value!=null&&value>=0?value:null;});
  const width=Math.max(1,days.length)*pitch;
  // Reserve the full date matrix and all plots inside the available viewport.
  const matrixRow=36;
  const leaderHeight=100;
  const strengthHeight=height>=780?62:46,amountHeight=height>=780?62:46;
  const rankHeight=Math.max(90,height-154-matrixRow*10-(choice.source==='kpl'?leaderHeight+strengthHeight+amountHeight:0));
  const strengthMax=Math.max(1,...strengths.map(v=>Math.abs(v??0))),amountMax=Math.max(1,...amounts.map(v=>v??0));
  const sy=(v:number)=>strengthHeight/2-v/strengthMax*(strengthHeight/2-15);
  const ry=(v:number)=>18+(v-1)*(rankHeight-40)/10;
  const pick=(row:AtlasRow)=>{setChoice({...choice,primary:row.code});setPicked(old=>[...new Set([...old,row.code])]);};
  const chooseSource=(source:'kpl'|'ths')=>{setChoice({...choice,source,primary:''});setPicked([]);setHidden([]);};
  const range=(value:number)=>{if(Number.isInteger(value)&&value>=1&&value<=50)setChoice({...choice,range:value});};
  const sourceLabel=choice.source==='kpl'?'开盘啦':'同花顺';
  const rankingDays=days.filter(p=>rows(p).length).length,partialDays=days.filter(p=>p.status==='partial').length;
  const enlarge=()=>{if(frame.current){if(document.fullscreenElement)void document.exitFullscreen();else void frame.current.requestFullscreen().catch(()=>{});}};
  return <section className="sr-atlas" ref={frame} aria-label="板块轮动矩阵与曲线" data-source={choice.source} style={{'--sr-cell-height':`max(${matrixRow}px, var(--sr-touch-row, 0px))`,'--sr-leaders-height':`max(${leaderHeight}px, var(--sr-touch-leaders, 0px))`} as React.CSSProperties}>
    <header className="sr-atlas-tabs" role="group" aria-label="板块数据来源">{(['kpl','ths'] as const).map(source=><button key={source} aria-pressed={choice.source===source} onClick={()=>chooseSource(source)}>{source==='kpl'?'开盘啦板块':'同花顺板块'}</button>)}<button className="sr-enlarge" onClick={enlarge} aria-label="放大板块轮动">放大</button></header>
    <div className="sr-atlas-controls"><div className="sr-ranges" role="group" aria-label="轮动交易日窗口">{[10,20,30,50].map(n=><button key={n} aria-pressed={!custom&&choice.range===n} onClick={()=>{range(n);setCustom(false);}}>近{n}日</button>)}<button aria-pressed={custom} onClick={()=>setCustom(!custom)}>自定义</button>{custom&&<input type="number" min="1" max="50" value={choice.range} aria-label="自定义交易日数" onChange={e=>range(Number(e.target.value))}/>}</div><label className="sr-date">截至<input type="date" aria-label="板块轮动截至日期" max={day} value={target} onChange={e=>{if(e.target.value&&e.target.value<=day)setEnd(e.target.value);}}/></label>{end&&end!==day&&<button className="sr-text-button" onClick={()=>setEnd('')}>最近</button>}</div>
    {read.error&&<div className="sr-error" role="alert">{read.error.message}<button onClick={read.reload}>重试</button></div>}
    <div className="sr-atlas-scroll" tabIndex={0} aria-label="轮动矩阵与曲线统一滚动">
      {!days.length?<div className="sr-empty" role="status">{read.busy?'正在读取板块轮动…':'暂无已保存的板块历史'}</div>:<div className="sr-timeline" style={{width:width+axis,minWidth:'100%'}}>
        <table className="sr-matrix" aria-label="板块每日前十矩阵" style={{width:width+axis}}><colgroup><col style={{width:axis}}/>{days.map(p=><col key={p.requested_day} style={{width:pitch}}/>)}</colgroup><thead><tr><th className="sr-axis">排名</th>{days.map(p=><th key={p.requested_day} title={`${p.source_label} · ${p.requested_day} · ${p.archive_kind==='daily'?'日终归档':p.archive_kind==='intraday'?'盘中观察':'已存样本'} · 接收 ${p.received_at??'未披露'}`}>{p.requested_day?.slice(5)}{p.archive_kind==='intraday'&&<small>盘中</small>}{p.status==='partial'&&<small title={p.reason??'覆盖不完整'} aria-label='部分覆盖'>·</small>}</th>)}</tr></thead><tbody>{positions.map(rank=><tr key={rank}><th className="sr-axis"><b className={`sr-rank sr-rank-${rank}`}>{rank}</b></th>{days.map(p=>{const row=rows(p).find(r=>r.rank===rank),color=row?selected.indexOf(row.code):-1;const value=choice.source==='kpl'?rotationNumber(row?.strength):rotationNumber(row?.pct_change);return <td key={p.requested_day}>{row?<button className={primary===row.code?'is-primary':''} aria-pressed={primary===row.code} data-code={row.code} onClick={()=>pick(row)} style={{background:color>=0?colors[color%colors.length]+'20':undefined}} title={`${row.name} ${row.code} · ${p.requested_day} · ${choice.source==='kpl'?'强度':'涨幅'} ${value??'未提供'}${choice.source==='ths'&&value!=null?'%':''}`}><span>{row.name}</span><strong className={value==null?'sr-neutral':value<0?'sr-down':'sr-up'}>{value==null?'—':choice.source==='ths'?value.toFixed(2)+'%':numberLabel(value)}</strong></button>:<span className="sr-missing" title={p.reason??'该名次未保存'}>{rank===1?(p.status==='disabled'?'未启用':p.status==='error'?'读取失败':'暂无资料'):'—'}</span>}</td>;})}</tr>)}{choice.source==='kpl'&&<tr className="sr-leaders"><th className="sr-axis">领涨</th>{days.map(p=>{const leader=primary?p.leaders?.[primary]:undefined;const evidence=leader?describeLeaderEvidence(leader,p.requested_day):undefined;return <td key={p.requested_day}>{leader?.rows.map(r=><div className="sr-leader" key={r.stock_code} title={`${r.name} ${r.stock_code} · 来源${r.label}`}><b>{r.label}</b><span>{r.name}</span></div>)}{!leader?.rows.length&&<span className="sr-missing">{leader?'未返回标记':'未采集'}</span>}{evidence&&<small className="sr-leader-receipt" title={evidence.title}><time dateTime={evidence.receiptISO}>{evidence.receiptLabel}</time></small>}{evidence?.coverageNote&&<small className="sr-leader-coverage">{evidence.coverageNote}</small>}</td>;})}</tr>}</tbody></table>
        {choice.source==='kpl'&&<>
          <div className="sr-plot-row"><div className="sr-chart-axis">板块<br/>强度</div><svg width={width} height={strengthHeight} role="img" aria-label="所选板块原始强度走势"><path d={`M0 ${strengthHeight/2}H${width}`} className="sr-guide"/><path d={segments(strengths,sy)} className="sr-strength-line"/>{strengths.map((v,i)=>v==null?<text key={i} x={i*pitch+pitch/2} y={strengthHeight/2-7}>—</text>:<g key={i}><circle cx={i*pitch+pitch/2} cy={sy(v)} r="2.6" className="sr-strength-dot"/><text x={i*pitch+pitch/2} y={sy(v)+(v<0?13:-7)} className={v<0?'sr-down':'sr-up'}>{numberLabel(v)}</text><title>{days[i].requested_day} · {primaryName} · 原始强度 {v}</title></g>)}</svg></div>
          <div className="sr-plot-row"><div className="sr-chart-axis">板块<br/>量能</div><svg width={width} height={amountHeight} role="img" aria-label="所选板块成交额走势（亿元）"><path d={`M0 ${amountHeight-17}H${width}`} className="sr-guide"/>{amounts.map((v,i)=>{const h=v==null?0:v/amountMax*(amountHeight-34),y=amountHeight-17-h;return <g key={i}>{v==null?<text x={i*pitch+pitch/2} y={amountHeight/2}>—</text>:<><rect x={i*pitch+pitch/2-9} y={y} width="18" height={h} className="sr-volume-bar"/><text x={i*pitch+pitch/2} y={y-4}>{(v/1e8).toFixed(0)}亿</text><title>{days[i].requested_day} · {primaryName} · {v}元</title></>}<text x={i*pitch+pitch/2} y={amountHeight-3}>{days[i].requested_day?.slice(5)}</text></g>;})}</svg></div>
        </>}
        <div className="sr-rank-legend" aria-label="轮动曲线图例">{selected.map((code,i)=>{const row=options.find(r=>r.code===code);return row?<button key={code} aria-pressed={!hidden.includes(code)} style={{color:colors[i%colors.length]}} onClick={()=>setHidden(old=>old.includes(code)?old.filter(c=>c!==code):[...old,code])} title="点击显示或隐藏此板块曲线">{row.name}<span>（{frequency(code)}次上榜）</span></button>:null;})}</div>
        <div className="sr-plot-row sr-rank-plot"><div className="sr-chart-axis">排名</div><svg width={width} height={rankHeight} role="img" aria-label="多板块前十排名轨迹">{[1,5,10,11].map(rank=><path key={rank} d={`M0 ${ry(rank)}H${width}`} className="sr-guide"/>)}{selected.map((code,index)=>{if(hidden.includes(code))return null;const values=days.map(p=>rows(p).find(r=>r.code===code)?.rank??(p.trade_day===p.requested_day&&p.top_ten_complete===true&&p.rows.length===10&&p.status==='ready'?11:null));return <g key={code} stroke={colors[index%colors.length]} fill={colors[index%colors.length]}><path d={segments(values,ry)} fill="none" strokeWidth="1.5" strokeDasharray={index?'4 3':undefined}/>{values.map((rank,i)=>rank==null?null:<g key={i}><title>{days[i].requested_day} · {options.find(r=>r.code===code)?.name} · {rank===11?'未进前十':'第'+rank+'名'}</title><circle cx={i*pitch+pitch/2} cy={ry(rank)} r="2.3"/><text x={i*pitch+pitch/2} y={ry(rank)-5}>{rank===11?'—':rank}</text></g>)}</g>;})}{days.map((p,i)=><text key={p.requested_day} x={i*pitch+pitch/2} y={rankHeight-2}>{p.requested_day?.slice(5)}</text>)}</svg></div>
      </div>}
    </div>
    <footer className="sr-atlas-foot"><span title="点击矩阵中的板块联动强度、量能和领涨记录；上榜次数仅统计已保存资料">{primaryName}{primary&&` · ${frequency(primary)}次上榜 / ${days.length}日`}</span><div><span className='sr-coverage' title={`${rankingDays}/${days.length}日有排行；${partialDays}日部分覆盖`}>{rankingDays<days.length?`${rankingDays}/${days.length}日有榜`:partialDays?'覆盖不全':''}</span>{choice.source==='ths'&&<span className="sr-day-basis" title="同花顺板块使用原日收盘行情，不将历史日榜当实时涨幅">收盘数据</span>}<SourceInfo label="板块轮动来源与范围"><strong>{sourceLabel} · 数据口径</strong>{read.data?.data.limitations?.map(s=><p key={s}>{s}</p>)}{days.map(p=><p key={p.requested_day}>{p.requested_day} · {p.source_label} · {p.archive_kind==='daily'?'日终':p.archive_kind==='intraday'?'盘中':'归档'} · {p.rows.length}条{p.reason?' · '+p.reason:''}</p>)}</SourceInfo></div></footer>
  </section>;
}
