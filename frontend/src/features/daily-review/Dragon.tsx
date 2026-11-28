import './dragonBoard.css';
import type { ReactNode } from 'react';
import { StockCandles } from './ReviewAnalysis';
import { useLayoutEffect, useRef, useState } from 'react';
import { api, cancellableRead } from '../../shared/api';
import { useNavigation } from '../../shared/navigation';
import { Dialog, Card, Table, Empty, Loading, Failure, RefButton, Money, Value, useBoundRead } from './components';
import { amount, number, text, tone } from './model';
import type { Listing, Row, Side } from './model';
import type { ReviewProps } from './context';

type HistoryRecord = { trade_day: string; ts_code: string; reason: string; record_key: string; net_amount: string | null; state: string; source_rows: Row[] };
function SeatTable({ side, type, select, openRef, selected }: { side: Side; type: 'buy' | 'sell'; select: ReviewProps['select']; openRef: ReviewProps['openRef']; selected: string }) {
  const rows = [...side.records, ...side.extra_rows];
  return <><Table label={type === 'buy' ? '买入侧原始席位' : '卖出侧原始席位'}><thead><tr><th>#</th><th>席位名称</th><th>{type === 'buy' ? '买入额' : '卖出额'}</th><th>证据</th></tr></thead><tbody>{rows.map((row, i) => <tr key={i} className={selected === row.exalter ? 'chosen' : ''}><td>{i + 1}</td><td><button className="dr-text-button" onClick={() => select({ seat: String(row.exalter) })}>{text(row.exalter)}</button></td><td className={type === 'buy' ? 'rise' : 'fall'}>{amount(row[type === 'buy' ? 'buy' : 'sell'])}</td><td><RefButton row={row} onOpen={openRef} label="核对该席位原始行"/></td></tr>)}</tbody></Table>{!rows.length && <Empty>没有该侧可匹配的席位披露。</Empty>}{side.state !== 'complete' && <p className="dr-warning">{side.state === 'ambiguous_disclosure' ? '同股票、同原因存在多条披露，席位归属待核；金额不合计。' : `该侧披露${side.disclosed_row_count}行，前五合计尚未核全。`}</p>}</>;
}

function SeatHistory(p: ReviewProps & { seat: string }) {
  const query = new URLSearchParams(location.search);
  const [minimum, setMinimum] = useState(query.get('min_amount_yuan') ?? '');
  const lookback = query.get('seat_days') ?? '5';
  const params = new URLSearchParams({ trade_day: p.day, seat_name: p.seat, lookback, ...(query.get('min_amount_yuan') ? { min_amount_yuan: query.get('min_amount_yuan')! } : {}), ...(p.asOf ? { as_of_time: p.asOf } : {}) });
  const read = useBoundRead(`seat:${params}`, () => p.seat ? api.reviewSeat(params) : Promise.resolve(null));
  return <Card title={p.seat ? `${p.seat} · 近期买入侧披露` : '席位近期披露'} action={<form className="dr-seat-filters" onSubmit={e => { e.preventDefault(); p.select({ min_amount_yuan: minimum || null }); }}><label>时间<select aria-label="席位披露时间范围" value={lookback} onChange={e => p.select({ seat_days: e.target.value })}>{[5, 10, 20].map(n => <option value={n} key={n}>近{n}交易日</option>)}</select></label><label>最低买入额（元）<input aria-label="席位最低买入额" type="number" min="0" step="0.01" value={minimum} onChange={e => setMinimum(e.target.value)}/></label><button disabled={!p.seat}>应用筛选</button></form>}>
    {read.busy && p.seat ? <Loading/> : read.error ? <Failure error={read.error} retry={read.reload}/> : !p.seat ? <Empty>点击上方席位名称，查看相同披露名称的历史记录。</Empty> : <><Table label="席位近期买入记录"><thead><tr><th>股票</th><th>披露日</th><th>原涨幅</th><th>买入额</th><th>原因</th><th>查看</th></tr></thead><tbody>{read.data?.data.records.map((row, i) => <tr key={i}><td>{row.name ?? row.ts_code}<small>{row.ts_code}</small></td><td>{row.trade_day.slice(5)}</td><td><Value value={row.listing_record?.pct_change} percentage/></td><td><Money value={row.amount_yuan}/></td><td className="dr-wrap-cell">{row.reason}</td><td><button onClick={() => p.select({ trade_day: row.trade_day, stock: row.ts_code, record_key: row.lhb_record_key })}>详情 ↗</button></td></tr>)}</tbody></Table>{!read.data?.data.records.length && <Empty>当前筛选下没有已披露的买入侧记录。</Empty>}{read.data?.data.truncated && <p className="dr-warning">当前显示最多500条，请收窄时间或金额。</p>}</>}
    <p className="dr-note">相同席位名称不代表唯一交易者；仅买入侧前五披露，不是该席位全部买卖。不同原因与区间逐条保留，不累计金额。</p>
  </Card>;
}

function StockHistory(p: ReviewProps & { code: string }) {
  const params = new URLSearchParams({ trade_day: p.day, stock: p.code, ...(p.asOf ? { as_of_time: p.asOf } : {}) });
  const read = useBoundRead(`lhb-history:${params}`, () => p.code ? api.reviewLhb(params) : Promise.resolve(null));
  const records = (read.data?.data as unknown as { history_records?: HistoryRecord[]; history_truncated?: boolean } | undefined)?.history_records ?? [];
  return <Card title="个股历次上榜" action={<small>有权已存窗口</small>}>{read.busy ? <Loading/> : read.error ? <Failure error={read.error} retry={read.reload}/> : records.length ? <Table label="个股历史披露"><thead><tr><th>日期</th><th>披露原因</th><th>原榜净额</th><th>记录</th></tr></thead><tbody>{records.map(row => <tr key={row.record_key} className={row.trade_day === p.day ? 'chosen' : ''}><td>{row.trade_day.slice(5)}</td><td className="dr-wrap-cell">{row.reason}</td><td><Money value={row.net_amount}/>{row.state !== 'available' && <small>归属待核</small>}</td><td><button onClick={() => p.select({ trade_day: row.trade_day, stock: row.ts_code, record_key: row.record_key, seat: null })}>查看 ↗</button></td></tr>)}</tbody></Table> : <Empty>没有可访问的历史披露记录。</Empty>}<p className="dr-note">统计区间分别记录，不累计重叠金额。最多显示200条。</p></Card>;
}

function ResearchExcerpt(p: ReviewProps & { record: Listing | null }) {
  const { record, notebook: n } = p;
  const note = n.content.lhb_notes.find(row => row.record_key === record?.record_key);
  function updated(value: string) {
    if (!record || !n.writable || (!note && n.content.lhb_notes.length >= 20)) return;
    const refs = [...record.source_rows, ...record.buy_top5.records, ...record.sell_top5.records].map(row => row.row_reference?.id).filter((id): id is string => !!id);
    const next = { record_key: record.record_key, stock_code: record.ts_code, text: value, confirmed: false, evidence_ids: [...new Set(refs)].slice(0, 20) };
    n.update({ ...n.content, lhb_notes: [...n.content.lhb_notes.filter(row => row.record_key !== record.record_key), next] });
  }
  async function confirm() {
    if (!note || !record || !note.text.trim()) return;
    const content = { ...n.content, lhb_notes: n.content.lhb_notes.map(row => row.record_key === record.record_key ? { ...row, confirmed: true } : row) };
    await n.save(content, 'lhb');
  }
  return <Card title="研究摘记与引用" action={<span className="dr-ai">人工摘记 · {note?.confirmed && !n.dirty ? '已确认' : '待确认'}</span>}>
    <label className="dr-sr" htmlFor="lhb-research-note">龙虎榜研究摘记</label><textarea id="lhb-research-note" value={note?.text ?? ''} onChange={e => updated(e.target.value)} maxLength={2000} rows={7} disabled={!n.writable || !record || (!note && n.content.lhb_notes.length >= 20)} placeholder="逐条记录席位披露、仍待核实的问题和相反证据。"/>
    {record ? <p className="dr-reference-chip">{text(record.source_rows[0]?.name)} · {p.day.slice(5)} · 原因分组<RefButton row={record.source_rows[0]} onOpen={p.openRef}/></p> : <Empty>先选择一条具体披露。</Empty>}
    <div className="dr-card-actions"><button disabled={!n.writable || !record} onClick={() => record && p.addCore(record.ts_code, String(record.source_rows[0]?.name ?? record.ts_code))}>加入方向跟踪</button><button disabled title="明日预案后续添加">加入明日观察</button></div><div className="dr-form-actions"><button disabled={!n.writable || !n.dirty || n.busy} onClick={() => n.save()}>保存摘记</button><button className="primary" disabled={!n.writable || n.busy || !note?.text.trim() || (!!note?.confirmed && !n.dirty)} onClick={confirm}>确认并引用</button></div><p className="dr-note">保存与确认均新增笔记版本，并绑定这条披露的原证据；不改变原榜单。</p>
  </Card>;
}


type Category = '机构专用' | '营业部' | '股通' | '其他';
type Activity = { record: Listing; values: unknown[] };
type Participant = { name: string; category: Category; codes: Set<string>; buy: Activity[]; sell: Activity[] };

function participantIndex(records: Listing[], search: string, labels: ReadonlyMap<string,string>) {
  const groups = new Map<string, Participant>();
  for (const record of records) {
    const stockMatches = [record.ts_code, record.source_rows[0]?.name, record.reason].join(' ').toLowerCase().includes(search);
    for (const side of ['buy', 'sell'] as const) {
      const disclosure = side === 'buy' ? record.buy_top5 : record.sell_top5;
      if (disclosure.state === 'ambiguous_disclosure') continue;
      const values = new Map<string, unknown[]>();
      for (const row of disclosure.records) {
        const name = String(row.exalter ?? '').trim();
        if (!name || (!stockMatches && ![name,labels.get(name)].join(' ').toLowerCase().includes(search))) continue;
        const amounts = values.get(name) ?? [];
        amounts.push(row[side]);
        values.set(name, amounts);
      }
      for (const [name, amounts] of values) {
        const kind = name === '机构专用' ? '机构专用' : /[沪深]股通/.test(name) ? '股通' : name.includes('营业部') ? '营业部' : '其他';
        const group: Participant = groups.get(name) ?? { name, category: kind, codes: new Set<string>(), buy: [], sell: [] };
        group.codes.add(record.ts_code);
        group[side].push({ record, values: amounts });
        groups.set(name, group);
      }
    }
  }
  return [...groups.values()].sort((a,b) => b.codes.size - a.codes.size || largestRow(b) - largestRow(a) || a.name.localeCompare(b.name, 'zh'));
}

function seatLabel(name: string) {
  const branch = name.match(/^(.+?证券)(?:股份有限公司|有限责任公司|有限公司)?(.+?)(?:证券)?营业部$/);
  return branch ? { title: branch[2], broker: branch[1] } : { title: name, broker: '' };
}

function rowAmount(value:unknown) {
  const n = number(value);
  if (n === null) return '—';
  if (Math.abs(n)>=1e8) return amount(n);
  return Math.abs(n)>=10000 ? (n/10000).toLocaleString('zh-CN',{maximumFractionDigits:1})+'万' : n.toLocaleString('zh-CN')+'元';
}

function Disclosure(p:ReviewProps & {record:Listing; notebookBar:ReactNode; close:()=>void}) {
  const {record} = p;
  const seat = new URLSearchParams(location.search).get('seat') ?? '';
  const [tab,setTab] = useState(seat ? '席位历史' : '席位明细');
  const selectSeat:ReviewProps['select'] = values => { p.select(values); setTab('席位历史'); };
  return <Dialog title={text(record.source_rows[0]?.name)+' · '+record.ts_code} close={p.close}><div className="lh-detail">
    <nav className="lh-tabs" aria-label="披露详情">{['席位明细','个股历史','席位历史','研究摘记'].map(t=><button key={t} aria-pressed={t===tab} onClick={()=>setTab(t)}>{t}</button>)}</nav>
    {tab==='个股历史' ? <><StockHistory {...p} code={record.ts_code}/><StockCandles day={p.day} asOf={p.asOf} stock={record.ts_code} select={p.select}/></> : tab==='席位历史' ? <SeatHistory {...p} seat={seat}/> : tab==='研究摘记' ? <>{p.notebookBar}<ResearchExcerpt {...p} record={record}/></> : <>
    <RefButton row={record.source_rows[0]} onOpen={p.openRef} label="核对榜单原始披露"/>
<p className="dr-listing-reason">上榜原因：{record.reason}</p>{record.headline_state !== 'available' && <p className="dr-warning">同代码、同原因有多条来源记录，无法确认唯一披露区间。原行均保留，净额和席位合计不拼加。</p>}<div className="dr-three-values dr-lhb-totals"><section><small>买入侧前五合计</small><b className="rise">{amount(record.buy_top5.amount_yuan)}</b></section><section><small>卖出侧前五合计</small><b className="fall">{amount(record.sell_top5.amount_yuan)}</b></section><section><small>原榜净额</small><b className={tone(record.headline.net_amount)}>{amount(record.headline.net_amount)}</b></section></div><div className="dr-seats-grid"><Card title="买入侧前五"><SeatTable side={record.buy_top5} type="buy" select={selectSeat} openRef={p.openRef} selected={seat}/></Card><Card title="卖出侧前五"><SeatTable side={record.sell_top5} type="sell" select={selectSeat} openRef={p.openRef} selected={seat}/></Card></div><p className="dr-note">点击席位查看近期同名披露。原榜净额直接引用统计单，不用两侧前五之差替代；机构同名行各自保留。</p>
    </>}</div></Dialog>;
}

function largestRow(actor:Participant) {
  return Math.max(0,...[...actor.buy,...actor.sell].flatMap(activity=>activity.values.map(value=>number(value) ?? 0)));
}

function periodLabel(reason:string) {
  if (/三[个日天]|3[个日天]/.test(reason) && /连续|累计/.test(reason)) return '3日';
  if (/连续|累计|期间/.test(reason)) return '区间';
  return '';
}

function ActivityChips({items,open}:{items:Activity[];open:(r:Listing)=>void}) {
  return <>{items.length ? items.map(({record,values})=><button key={record.record_key} className="lh-chip" title={record.ts_code+' · '+record.reason+' · '+values.map(rowAmount).join(' / ')} onClick={()=>open(record)}>
    <b>{text(record.source_rows[0]?.name)}</b><span>{values.map(rowAmount).join(' / ')}</span>{periodLabel(record.reason) && <i>{periodLabel(record.reason)}</i>}
  </button>) : <small>—</small>}</>;
}

// A seat on both lists can carry the same provider net twice. Accept one net
// only when each list contains at most one matching row and the disclosures agree.
function netActivities(actor:Participant) {
  const events=new Set([...actor.buy,...actor.sell].map(a=>a.record));
  const buy:Activity[]=[], sell:Activity[]=[];
  let unknown=0;
  for(const record of events) {
    const rows=[record.buy_top5,record.sell_top5].map(side=>side.records.filter(row=>row.exalter===actor.name));
    const values=rows.flat().map(row=>number(row.net_buy));
    const net=values[0];
    if((actor.category==='机构专用'&&values.length>1)||rows.some(side=>side.length>1)||net==null||values.some(value=>value!==net)) {unknown++;continue;}
    if(net!==0) (net>0?buy:sell).push({record,values:[Math.abs(net)]});
  }
  return {buy,sell,unknown};
}

function InstitutionBoard({records,open}:{records:Listing[];open:(r:Listing)=>void}) {
  const [side,setSide]=useState<'buy'|'sell'>('buy');
  const rows=records.flatMap(record=>{
    if (record.headline_state!=='available') return [];
    const buy=record.buy_top5.records.filter(row=>row.exalter==='机构专用');
    const sell=record.sell_top5.records.filter(row=>row.exalter==='机构专用');
    const ranked=side==='buy'?buy:sell;
    if (!ranked.length) return [];
    const values=ranked.map(row=>number(row[side]));
    const total=values.every((n):n is number=>n!==null) ? values.reduce((a,b)=>a+b,0) : null;
    const count=(n:number,state:string)=>n || (state==='complete'?'0':'—');
    return [{record,buy:count(buy.length,record.buy_top5.state),sell:count(sell.length,record.sell_top5.state),total}];
  }).sort((a,b)=>(b.total??-1)-(a.total??-1));
  return <section className="lh-ranking" aria-label="机构动向"><div className="lh-panel-heading"><h3>机构动向</h3><div className="lh-tabs">{(['buy','sell'] as const).map(s=><button aria-pressed={s===side} key={s} onClick={()=>setSide(s)}>{s==='buy'?'买入额':'卖出额'}</button>)}</div></div>
    <div className="lh-scroll"><table><thead><tr><th>个股</th><th>买 / 卖席次</th><th>已披露金额</th></tr></thead><tbody>{rows.map(({record,buy,sell,total})=><tr key={record.record_key}><td><button className="lh-link" title={record.reason} onClick={()=>open(record)}>{text(record.source_rows[0]?.name)} {periodLabel(record.reason)}</button></td><td>{buy} / {sell}</td><td className={side==='buy'?'rise':'fall'}>{rowAmount(total)}</td></tr>)}</tbody></table>{!rows.length&&<Empty>未见该侧机构专用席位。</Empty>}</div></section>;
}

const modes=['重点席位','参考游资','活跃营业部','股通','全部席位','上榜个股'] as const;
type Mode=typeof modes[number];

export function Dragon(p:ReviewProps & {notebookBar:ReactNode}) {
  const query = new URLSearchParams(useNavigation().search);
  const [search,setSearch]=useState('');
  const [scope,setScope]=useState('all');
  const [mode,setMode]=useState<Mode>('重点席位');
  const [basis,setBasis]=useState<'net'|'gross'>('net');
  const [direction,setDirection]=useState('两侧');
  const [netOrder,setNetOrder]=useState(false);
  const [mobilePanel,setMobilePanel]=useState('席位');
  const frame=useRef<HTMLDivElement>(null);
  const params=new URLSearchParams({trade_day:p.day,...(p.asOf?{as_of_time:p.asOf}:{})});
  const read=useBoundRead('lhb:'+params,()=>api.reviewLhb(params));
  const records=read.data?.data.records??[];
  const listingCount=read.data?.data.source_listing_count;
  const key=query.get('record_key');
  const record=key?records.find(r=>r.record_key===key):records.find(r=>r.ts_code===p.stock);
  const members=new Set(p.selectedTheme?.member_records.map(r=>String(r.con_code))??[]);
  const scoped=records.filter(r=>(!p.mine && scope!=='mine'||p.watched.has(r.ts_code)) && (scope!=='sector'||members.has(r.ts_code)));
  const term=search.trim().toLowerCase();
  const lookupCodes=[...new Set(records.map(r=>r.ts_code.split('.')[0]))].sort().slice(0,32).join(',');
  const checkDay=!p.asOf && lookupCodes ? p.day : '';
  type Observation={source:string; day:string; code:string; seat:string; label:string; url:string};
  const check=useBoundRead('lhb-labels:'+checkDay+':'+lookupCodes,()=>checkDay
    ? cancellableRead<{data:{day:string;rows:Observation[];kpl_status:string}}>('/api/v1/review-data/seat-labels?'+new URLSearchParams({trade_day:checkDay,codes:lookupCodes}),new AbortController().signal)
    : Promise.resolve(null));
  const result=check.data?.data;
  const observations=result?.day===checkDay && Array.isArray(result.rows) ? result.rows.filter(row=>row.day===checkDay) : [];
  const labels=new Map(observations.map(row=>[row.seat,row.label]));
  const actors=participantIndex(scoped,term,labels);
  const named=actors.filter(a=>labels.has(a.name));
  const branches=actors.filter(a=>a.category==='营业部');
  function attribution(a:Participant) {
    const codes=new Set([...a.codes].map(code=>code.split('.')[0]));
    const matching=observations.filter(row=>row.seat===a.name && codes.has(row.code));
    const distinct=new Map(matching.map(row=>[row.source+'|'+row.label,row]));
    return [...distinct.values()].map(row=><small key={row.source+row.label}>
      <a href={row.url} target="_blank" rel="noopener noreferrer">{row.source} · {row.label} ↗</a>
    </small>);
  }
  const hotspots=[...named,...branches.filter(a=>!labels.has(a.name)).slice(0,8)];
  const selected=term && mode==='重点席位'?actors:mode==='重点席位'?hotspots:mode==='参考游资'?named:mode==='股通'?actors.filter(a=>a.category==='股通'):mode==='活跃营业部'?branches:actors;
  const hasNet=scoped.some(r=>[...r.buy_top5.records,...r.sell_top5.records].some(row=>number(row.net_buy)!==null));
  const net=hasNet && basis==='net';
  const groups=selected.map(a=>({...a,unknown:0,...(net?netActivities(a):{})}));
  const visible=groups.filter(a=>direction==='两侧'||(direction==='买榜'?a.buy:a.sell).length);
  function cell(a:typeof groups[number],side:'buy'|'sell') {
    return <td className={'lh-'+side}><div className="lh-chip-list"><ActivityChips items={a[side]} open={open}/></div></td>;
  }
  const stocks=scoped.filter(r=>[r.ts_code,r.source_rows[0]?.name,r.reason].join(' ').toLowerCase().includes(term));
  const matchingEvents=new Set([...stocks,...actors.flatMap(a=>[...a.buy,...a.sell].map(item=>item.record))]);
  const institutionRecords=term?scoped.filter(r=>matchingEvents.has(r)):scoped;
  if(netOrder) stocks.sort((a,b)=>(number(b.headline.net_amount)??-Infinity)-(number(a.headline.net_amount)??-Infinity));
  function open(r:Listing) {p.select({stock:r.ts_code,record_key:r.record_key,seat:null});}
  const close=()=>p.select({stock:null,record_key:null,seat:null},true);
  const empty=scope==='sector' && p.selectedTheme?.truncated?'已返回成员中未匹配到披露；完整题材范围尚不可判断。':scope==='sector'&&!p.selectedTheme?.detail_available?'所选原日题材成员未取得，不能确认关联范围。':'当前筛选下没有已披露记录。';
  useLayoutEffect(()=>{
    const box=frame.current;
    if(!box)return;
    const resize=()=>box.style.setProperty('--lh-height',Math.max(150,window.innerHeight-box.getBoundingClientRect().top-52)+'px');
    const observer=new ResizeObserver(resize);
    observer.observe(box.closest('.dr-root')??box);
    window.addEventListener('resize',resize);
    resize();
    return ()=>{observer.disconnect();window.removeEventListener('resize',resize);};
  },[read.busy,read.error]);
  return <section className="lh-board" aria-label="龙虎榜资金动向">
    <header className="lh-head"><div><h2>龙虎榜</h2><small>{p.day} 披露</small></div><b>{records.length ? new Set(scoped.map(r=>r.ts_code)).size+' 只上榜股票' : listingCount===0 ? '来源返回 0 条披露' : read.busy ? '正在读取披露清单…' : '披露数量待核'}</b></header>
    <div className="lh-tools"><nav className="lh-tabs" aria-label="龙虎榜视角">{modes.map(m=><button key={m} aria-pressed={m===mode} onClick={()=>{setMode(m);setMobilePanel('席位');}}>{m}</button>)}</nav>
      <input aria-label="搜索上榜对象" placeholder={mode==='上榜个股'?'股票 / 代码 / 原因':'游资标签 / 营业部 / 股票'} value={search} onChange={e=>setSearch(e.target.value)}/>
      <select aria-label="龙虎榜筛选范围" value={scope} onChange={e=>setScope(e.target.value)}><option value="all">全部披露</option><option value="mine">我的关注</option><option value="sector">所选题材已返回成员</option></select></div>
    {scope==='sector'&&p.selectedTheme?.truncated&&<p className="dr-warning">原日成员详情已截断，此处仅筛选已返回成员，不能代表题材全部披露。</p>}
    {!read.data&&read.busy?<Loading/>:!read.data&&read.error?<Failure error={read.error} retry={read.reload}/>:!records.length?<Empty>{listingCount===0 ? p.day+' 的来源上榜清单返回 0 条。' : '该日尚无可核对的上榜清单。'}{read.data?.data.unmatched_seat_records?.length ? ' 另有 '+read.data.data.unmatched_seat_records.length+' 条席位记录尚未匹配到上榜披露。' : ''}</Empty>:<>
    <div className="lh-mobile lh-tabs">{['席位','机构'].map(v=><button key={v} aria-pressed={mobilePanel===v} onClick={()=>setMobilePanel(v)}>{v}</button>)}</div>
    <div ref={frame} className={'lh-frame '+(mobilePanel==='机构'?'show-institutions':'')}>
      <section className="lh-matrix"><div className="lh-panel-heading"><h3>{mode==='上榜个股'?'个股榜单':'席位买卖对照'} <small>{mode==='上榜个股'?stocks.length+' 组披露':visible.length+' 个席位'}</small></h3>
      {mode==='上榜个股'?<button aria-pressed={netOrder} onClick={()=>setNetOrder(!netOrder)}>{netOrder?'按原榜净额':'原披露顺序'}</button>:<div className="lh-tabs">{hasNet&&<><button aria-pressed={net} onClick={()=>setBasis('net')}>净额</button><button aria-pressed={!net} onClick={()=>setBasis('gross')}>买卖榜</button></>}<select aria-label="买卖方向" value={direction} onChange={e=>setDirection(e.target.value)}>{['两侧','买榜','卖榜'].map(s=><option key={s} value={s}>{net?({'买榜':'净买入','卖榜':'净卖出'}[s]??s):s}</option>)}</select></div>}</div>
      <div className="lh-scroll" key={mode+scope+term+direction}>
      {mode==='上榜个股'?<><table><thead><tr><th>股票</th><th>上榜原因</th><th>原榜净额</th></tr></thead><tbody>{stocks.map(r=><tr key={r.record_key}><td><button className="lh-link" onClick={()=>open(r)}>{text(r.source_rows[0]?.name)} <small>{r.ts_code}</small></button></td><td>{r.reason}</td><td><Money value={r.headline.net_amount}/></td></tr>)}</tbody></table>{!stocks.length&&<Empty>{empty}</Empty>}</>:
      <><table className="lh-actors"><thead><tr><th>游资 / 席位</th><th>披露营业部</th>{direction!=='卖榜'&&<th className="rise">{net?'净买入个股 · 净额':'买榜个股 · 买入额'}</th>}{direction!=='买榜'&&<th className="fall">{net?'净卖出个股 · 净额':'卖榜个股 · 卖出额'}</th>}</tr></thead>
      <tbody>{visible.map(a=>{const label=seatLabel(a.name);return <tr key={a.name}><th scope="row"><b>{label.title}</b>{attribution(a)}<small>{a.codes.size} 只个股{a.unknown>0?' · '+a.unknown+'组净额待核':''}</small></th><td title={a.name}>{a.name}</td>{direction!=='卖榜'&&cell(a,'buy')}{direction!=='买榜'&&cell(a,'sell')}</tr>;})}</tbody></table>{!visible.length&&<Empty>{empty}{mode==='参考游资'?' 参考表中没有匹配的当期席位。':''}</Empty>}</>}
      </div></section>
      <InstitutionBoard records={institutionRecords} open={open}/>
    </div></>}
    <footer>{mode==='重点席位'&&!term?'重点收录匹配参考表的席位及其余涉及股票最多的八家营业部。':''}金额按原披露营业部统计。标签按交易日、股票和营业部全名交叉核对。{checkDay&&check.data?.data?.kpl_status!=='0'?'开盘啦待核，第三方旁证单列。':''}{net?'净额取同一席位同一披露的原行，重复上榜只列一次。':'金额按买卖榜及上榜原因分列。'}</footer>
    {!read.busy&&!read.error&&(key||p.stock)&&(record?<Disclosure key={record.record_key} {...p} record={record} close={close}/>:<Dialog title="上榜披露" close={close}><Empty>所选股票或记录未在该日披露中返回。</Empty></Dialog>)}
  </section>;
}
