import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ApiError, request } from '../../shared/api';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { useNavigation } from '../../shared/navigation';
import { SectorRotation, SourceInfo } from './SectorRotation';
import type { DirectionBoardEnvelope, DirectionBoardProps, DirectionRankPanel, DirectionRankRow, DirectionRankStatus, DirectionSectorType } from './directionBoardTypes';
import './sectorRotation.css';

const statusLabels: Record<DirectionRankStatus,string>={ready:'',partial:'部分资料',missing:'暂无资料',stale:'较早记录',disabled:'未启用',error:'读取失败'};
const platforms=[['ths','同花顺'],['tdx','通达信'],['eastmoney','东方财富']] as const;
const ranks=Array.from({length:10},(_,i)=>i+1);

function numeric(value: string | number | null | undefined) {
  if (value === null || value === undefined || String(value).trim() === '') return null;
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}
function signed(value: number | null, suffix = '') { return value === null ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(2)}${suffix}`; }
function tone(value: number | null) { return value === null || value === 0 ? 'dir-neutral' : value > 0 ? 'dir-rise' : 'dir-fall'; }
function dataTime(value: string | null | undefined) {
  if (!value) return '未提供';
  // A timestamp without an offset has no verified timezone; do not silently
  // reinterpret it in the browser's timezone or replace it with received_at.
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)) return `${value.replace('T', ' ')}（时区未注明）`;
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? value : time.toLocaleString('sv-SE', { timeZone: 'Asia/Shanghai', hour12: false }).replace('T', ' ');
}
function panelStatus(panel: DirectionRankPanel | undefined, expected: string | null | undefined): DirectionRankStatus {
  if (!panel) return 'missing';
  if (panel.rows.length && expected && panel.trade_day !== expected && panel.status === 'ready') return 'stale';
  return panel.status;
}
function panelMessage(panel: DirectionRankPanel | undefined, expected?: string | null) {
  if (!panel) return '所选日期尚无已保存资料';
  if (expected && panel.trade_day && panel.trade_day !== expected) return `所选 ${expected}，实际资料 ${panel.trade_day}${panel.reason ? ` · ${panel.reason}` : ''}`;
  return panel.reason || (panel.status === 'disabled' ? '来源尚未启用，可在数据中心核对' : panel.status === 'error' ? '该来源读取失败，可稍后重试' : panel.status === 'missing' ? '所选日期尚无已保存资料' : panel.status === 'partial' ? '当前仅展示已保存的有效记录' : '每个来源按其原始口径独立排名');
}
function rankRow(panel: DirectionRankPanel | undefined, rank: number) { return panel?.rows.find(row => row.rank === rank); }

async function readDirection(path: string) {
  const response = await request<DirectionBoardEnvelope>(path);
  const value = response?.data;
  const validPanel = (panel: DirectionRankPanel | undefined) => !!panel && Array.isArray(panel.rows);
  if (!value || typeof value.trade_day !== 'string' || !Array.isArray(value.days) || !value.days.every(validPanel)
    || !validPanel(value.funds) || !validPanel(value.strength) || !value.popularity
    || !platforms.every(([key]) => validPanel(value.popularity[key]))) {
    throw new Error('榜单响应格式不完整，请重读或在数据中心检查来源。');
  }
  return response;
}

function useDirectionRead(day: string, asOf: string, kind: DirectionSectorType, refreshEpoch: number, endDay = '', enabled = true, automaticCurrent = false) {
  const query = new URLSearchParams({ trade_day: day, sector_type: kind, ...(asOf ? { as_of_time: asOf } : {}), ...(endDay ? { end_day: endDay } : {}), ...(automaticCurrent ? { automatic_current: 'true' } : {}) });
  const path = '/api/v1/direction-data?' + query;
  const read = useCachedLoader(() => enabled && day ? readDirection(path) : Promise.resolve(null), enabled && day ? path : 'direction-disabled');
  const epoch = useRef(refreshEpoch);
  useEffect(() => { if (epoch.current !== refreshEpoch) { epoch.current = refreshEpoch; read.reload(); } }, [refreshEpoch, read.reload]);
  const forbidden = read.error instanceof ApiError && [401, 403].includes(read.error.status);
  return { ...read, data: forbidden ? undefined : read.data };
}


function PanelSource({panel,expected,label}:{panel:DirectionRankPanel|undefined;expected:string|null|undefined;label:string}) {
  return <SourceInfo label={label+'来源与时间'}><strong>{panel?.source_label||'来源未提供'}</strong><p>资料日 {panel?.trade_day||'未提供'}{expected&&panel?.trade_day!==expected?' · 所选 '+expected:''}</p><p>来源时间 {dataTime(panel?.source_time)}</p><p>实际接收 {dataTime(panel?.received_at)}</p><p>{panelMessage(panel,expected)}</p>{panel?.total_count!=null&&<p>已保存 {panel.total_count} 条</p>}</SourceInfo>;
}
function StockPanel({label,panel,day,selected,pick}:{label:string;panel:DirectionRankPanel|undefined;day:string;selected:string;pick:(row:DirectionRankRow)=>void}) {
  const quoteId=useId();
  const status=panelStatus(panel,day),available=panel?.trade_day===day&&!['missing','disabled','error'].includes(status);
  return <article className="sr-pop-platform" data-platform={label} aria-label={label+'人气榜'}>
    <header><h3>{label}</h3>{status!=='ready'&&<span className="sr-pop-state" title={panelMessage(panel,day)}>{panel?.trade_day&&panel.trade_day!==day?'非所选日':statusLabels[status]}</span>}<PanelSource panel={panel} expected={day} label={label}/></header>
    <div className="sr-list-scroll"><table aria-label={label+'人气股及涨幅'}><colgroup><col className="sr-rank-col"/><col/><col className="sr-pct-col"/></colgroup><thead><tr><th>#</th><th>人气股</th><th>涨幅</th></tr></thead><tbody>{ranks.map(rank=>{const row=available?rankRow(panel,rank):undefined;return <tr key={rank} className={row&&selected===row.code?'is-selected':''}><td>{rank}</td><td>{row?<button onClick={()=>pick(row)} title={`${row.name} ${row.code} · 来源排名 ${row.rank}${row.rank_change==null?' · 名次变化未提供':' · 名次变化 '+row.rank_change}`}>{row.name||row.code}</button>:<span className="sr-neutral">—</span>}</td><td>{row?<><button className={'sr-quote-button '+tone(numeric(row.pct_change))} popoverTarget={quoteId+'-'+rank} aria-label={row.name+'涨幅及来源详情'}>{signed(numeric(row.pct_change),'%')}{row.quote_state==='stale'&&<small>旧</small>}</button><div className="sr-quote-popover" id={quoteId+'-'+rank} popover="auto" role="region" aria-label={row.name+'行情详情'}><header><strong>{row.name} {row.code}</strong><button popoverTarget={quoteId+'-'+rank} popoverTargetAction="hide" autoFocus>关闭</button></header><p className={tone(numeric(row.pct_change))}>涨幅 {signed(numeric(row.pct_change),'%')}</p><p>{row.quote_state==='daily_close'?'收盘行情':row.quote_state==='stale'?'较早行情':row.quote_state==='ranking_snapshot'?'榜单快照，非独立实时行情':row.quote_state==='missing'?'同日行情未提供':'已保存行情'}</p><p>来源 {row.quote_source||'未披露'}</p><p>行情时间 {dataTime(row.quote_time)}</p><p>实际接收 {dataTime(row.quote_received_at)}</p></div></>:<span className="sr-neutral">—</span>}</td></tr>;})}</tbody></table></div>
  </article>;
}

export function DirectionBoard({day,asOf,refreshEpoch,isAdmin,ownerId='direction',onWindowChange,onResearch,inactive}:DirectionBoardProps) {
  const navigation=useNavigation(),query=new URLSearchParams(navigation.search);
  const automaticCurrent=!asOf&&!query.has('trade_day')&&!query.has('as_of_time');
  const [kind,setKind]=useState<DirectionSectorType>(query.get('rotation_kind')==='industry'?'industry':'concept');
  const [stock,selectStock]=useState<DirectionRankRow|null>(null),[pane,setPane]=useState('rotation'),[platform,setPlatform]=useState('同花顺');
  const root=useRef<HTMLElement>(null),popularity=useRef<HTMLElement>(null),[height,setHeight]=useState(700);
  const [toolbar,setToolbar]=useState<HTMLElement|null>(null),[popHeight,setPopHeight]=useState(400);
  useLayoutEffect(()=>{setToolbar(document.getElementById('sr-review-tools'));},[]);
  useLayoutEffect(()=>{const el=root.current;if(!el)return;const measure=()=>setHeight(Math.max(320,window.innerHeight-el.getBoundingClientRect().top-28));const observer=new ResizeObserver(measure);observer.observe(el.parentElement!);window.addEventListener('resize',measure);measure();return()=>{observer.disconnect();window.removeEventListener('resize',measure);};},[]);
  const base=useDirectionRead(day,asOf,'concept',refreshEpoch,'',true,automaticCurrent);
  const industry=useDirectionRead(day,asOf,'industry',refreshEpoch,'',kind==='industry',automaticCurrent);
  const current=kind==='concept'?base:industry,data=current.data?.data,shared=base.data?.data;
  const snapshotDay=shared?.snapshot_day||day,funds=data?.funds;
  const fundsDay=data?.funds_day===undefined?data?.previous_trade_day:data.funds_day;
  const fundsStatus=panelStatus(funds,fundsDay),available=funds?.trade_day===fundsDay&&!['missing','disabled','error'].includes(fundsStatus);
  const largest=Math.max(1,...(funds?.rows.map(r=>Math.abs(numeric(r.net_amount)??0))??[]));
  useLayoutEffect(()=>{
    const element=popularity.current;if(!element)return;
    const outer=(node:Element|null)=>{if(!node)return 0;const style=getComputedStyle(node);return node.getBoundingClientRect().height+(parseFloat(style.marginTop)||0)+(parseFloat(style.marginBottom)||0);};
    const measure=()=>{
      const columnHeight=Math.max(0,...Array.from(element.querySelectorAll('.sr-pop-platform')).map(column=>outer(column.querySelector('header'))+outer(column.querySelector('table'))+outer(column.querySelector(':scope > .sr-panel-state'))));
      setPopHeight(Math.ceil(outer(element.querySelector(':scope > .sr-section-head'))+columnHeight+4));
    };
    const observer=new ResizeObserver(measure);
    element.querySelectorAll('.sr-section-head,.sr-pop-platform>header,table,.sr-panel-state').forEach(node=>observer.observe(node));measure();
    return()=>observer.disconnect();
  },[shared,pane,platform]);
  const actions=<div className="sr-workspace-actions"><button disabled={inactive} onClick={onResearch}>研究记录</button><SourceInfo label="板块轮动资料说明"><p>研究日期 {day}</p><p>人气榜日期 {snapshotDay}</p><p>资金日期 {fundsDay||'上一交易日未核实'}</p>{asOf&&<p>当时已知 · 截止 {dataTime(asOf)}</p>}<a href="/data?view=connections">{isAdmin?'数据中心 · 来源与补采':'数据中心 · 来源状态'}</a></SourceInfo></div>;
  return <section className="sr-workspace" inert={inactive||undefined} ref={root} aria-label="板块轮动工作区" data-pane={pane} style={{height,'--sr-desktop-list-row':height>=780?'28px':'24px','--sr-pop-height':popHeight+'px'} as React.CSSProperties}>
    {toolbar&&createPortal(actions,toolbar)}
    <nav className="sr-pane-tabs" aria-label="板块轮动分区">{[['rotation','轮动'],['popularity','人气'],['funds','资金']].map(([key,label])=><button key={key} aria-pressed={pane===key} onClick={()=>setPane(key)}>{label}</button>)}</nav>
    {base.error&&<div className="sr-error" role="alert">{base.error.message}{base.data?' · 保留上次资料':''}<button onClick={base.reload}>重试</button></div>}
    <div className="sr-workspace-grid"><div className="sr-rotation-pane"><SectorRotation day={day} asOf={asOf} refreshEpoch={refreshEpoch} ownerId={ownerId} onWindowChange={onWindowChange}/></div>
      <aside className="sr-side"><section className="sr-popularity" ref={popularity} aria-label="三平台人气榜" data-platform={platform}><header className="sr-section-head"><h2>人气榜</h2><span title="排名与涨幅均使用各自原始来源；行情时点见数值详情">{snapshotDay}{base.busy?' · 读取中':''}</span></header><nav className="sr-mobile-platforms" aria-label="人气榜平台">{platforms.map(([key,label])=><button key={key} aria-pressed={platform===label} onClick={()=>setPlatform(label)}>{label}</button>)}</nav><div className="sr-pop-columns">{platforms.map(([key,label])=><StockPanel key={key} label={label} panel={shared?.popularity[key]} day={snapshotDay} selected={stock?.code??''} pick={selectStock}/>)}</div></section>
      <section className="sr-funds" aria-label="昨日资金净流入"><header className="sr-section-head"><h2>昨日资金净流入</h2><div className="sr-fund-kinds" role="group" aria-label="资金板块分类">{(['concept','industry'] as const).map(value=><button key={value} aria-pressed={kind===value} onClick={()=>setKind(value)}>{value==='concept'?'概念':'行业'}</button>)}</div><PanelSource panel={funds} expected={fundsDay} label="昨日资金"/></header><div className="sr-funds-date"><span>{fundsDay||'上一交易日未核实'}</span><span>主力净额 / 亿</span></div>
      {kind==='industry'&&industry.error&&<div className="sr-error" role="alert">{industry.error.message}<button onClick={industry.reload}>重试</button></div>}
      <div className="sr-list-scroll"><table aria-label="昨日资金净流入前十"><colgroup><col className="sr-rank-col"/><col/><col className="sr-fund-track-col"/><col className="sr-fund-value-col"/></colgroup><thead><tr><th>#</th><th>板块</th><th aria-label="净流入比例"/><th>净流入</th></tr></thead><tbody>{ranks.map(rank=>{const row=available?rankRow(funds,rank):undefined,value=numeric(row?.net_amount);return <tr key={rank}><td>{rank}</td><td title={row?.name}>{row?.name||'—'}</td><td><span className="sr-fund-track">{value!=null&&<i className={value<0?'sr-down':'sr-up'} style={{width:Math.abs(value)/largest*100+'%'}}/>}</span></td><td className={tone(value)}>{signed(value==null?null:value/1e8)}</td></tr>;})}</tbody></table></div>{fundsStatus!=='ready'&&<p className="sr-panel-state" title={panelMessage(funds,fundsDay)}>{current.busy?'正在读取资金…':statusLabels[fundsStatus]}</p>}</section></aside>
    </div>
  </section>;
}
