import { useEffect, useRef, useState } from 'react';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { usePaneScroll, useStableRanking, useWorkspaceState } from '../../shared/workspaceState';
import { AIObservation, Change, Empty, Follow, StockName } from './Components';
import type { PageProps } from './Components';
import { AuctionChart, FundsChart, SealChart } from './Charts';
import { dateTime, money, number, time } from './data';
import { TopicResearch } from '../news-center/TopicResearch';
import { HistoryChart } from '../market/HistoryChart';
import './researchWorkspace.css';

export function ResearchWorkspace(original: PageProps & {embedded?: boolean}) {
  const nav = useNavigation(), query = new URLSearchParams(nav.search);
  const members=original.trackingMembers??original.data.selected_members;
  const memberCodes=new Set(members.map(row=>row.code));
  const [pendingSector,setPendingSector]=useState<string|null>(null);
  const selected=query.get('stock')?original.selected:null;
  const p={...original,selected:pendingSector!==null||query.get('research_scope')==='sector'&&selected&&!memberCodes.has(selected.code)?null:selected};
  const key = `${p.session.data.user.id}:research-extension:${p.data.trade_day}:${p.asOfTime}`;
  const [scopeSaved, setScope] = useWorkspaceState(key + ':scope', 'all');
  const scope = query.get('research_scope') || scopeSaved;
  const [rankingReset,setRankingReset]=useWorkspaceState(key+':ranking-reset',0);
  const [sort, setSort] = useWorkspaceState(key + ':sort', 'pct_change');
  const [tab, setTab] = useWorkspaceState(key + ':tab', '走势');
  const [pane, setPane] = useWorkspaceState(key + ':pane', '列表');
  useEffect(() => { if (original.embedded && query.get('research_detail') === '1' && query.get('stock')) setPane('详情'); }, [original.embedded, query.get('stock'), query.get('research_detail')]);
  const root = usePaneScroll(key + ':scroll');
  const opener = useRef<HTMLElement | null>(null), drawer = useRef<HTMLDialogElement>(null);
  const [compact, setCompact] = useState(() => !original.embedded && matchMedia('(min-width:1024px) and (max-width:1439px)').matches);
  useEffect(() => { const mq = matchMedia('(min-width:1024px) and (max-width:1439px)'); const change = () => setCompact(!original.embedded && mq.matches); mq.addEventListener('change', change); return () => mq.removeEventListener('change', change); }, []);
  function close() { drawer.current?.close(); p.selectStock(''); setPane('列表'); requestAnimationFrame(()=>opener.current?.focus({preventScroll:true})); }
  useEffect(() => { const el = drawer.current; if (compact && query.get('research_detail')==='1' && p.selected && el && !el.open) el.showModal(); else if ((!compact || !p.selected || query.get('research_detail')!=='1') && el?.open) el.close(); }, [compact, p.selected?.code,query.get('research_detail')]);
  function selectStock(code: string) { opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; const url=new URL(location.href);url.searchParams.set('stock',code);url.searchParams.set('research_detail','1');url.searchParams.delete('instrument');updateLocation(url); setPane('详情'); }
  function chooseScope(value: string) { setRankingReset(rankingReset+1);setScope(value); const url = new URL(location.href); url.searchParams.set('research_scope', value); updateLocation(url); }
  useEffect(()=>{if(pendingSector!==null && original.sector?.code===pendingSector && !original.sectorBusy && original.trackingMembers!==undefined){if(query.get('stock')&&original.selected&&!memberCodes.has(original.selected.code))original.selectStock('');setPendingSector(null);}},[pendingSector,original.sector?.code,original.sectorBusy,original.trackingMembers,original.selected?.code]);
  const pool = scope === 'sector' ? members.map(member => ({...p.data.stocks.find(s => s.code === member.code), ...member})) : p.data.stocks;
  const rows = pool.filter(row => scope !== 'watch' || p.watch.items.some(item => item.instrument.id === row.instrument_id))
    .sort((a,b) => (b[sort as 'pct_change'] ?? -Infinity) - (a[sort as 'pct_change'] ?? -Infinity) || a.code.localeCompare(b.code));
  const ranking = useStableRanking(rows, key + ':' + scope + ':' + (scope === 'sector' ? p.sector?.code : '') + ':' + sort, rankingReset);
  const lookup = new Map(rows.map(row=>[row.code,row]));
  const [visibleCount,setVisibleCount]=useWorkspaceState(key+':visible:'+scope+':'+(p.sector?.code||'')+':'+sort,100);
  const ordered = ranking.order.slice(0,visibleCount).map(code => lookup.get(code)!).filter(Boolean);
  const sectors = p.data.intraday_funds?.trade_day === p.data.trade_day ? p.data.intraday_funds.sectors : p.data.sectors;
  const topic = new URLSearchParams({view:'topics',research_scope:scope,return_section:'board-direction', trade_day:p.data.trade_day, ...(p.sector ? {sector:p.sector.code, topic:p.sector.name, topic_source:'dc'} : {}), ...(p.selected ? {stock:p.selected.code} : {}), ...(p.asOfTime ? {as_of_time:p.asOfTime} : {})});
  const detail = <div className="rw-detail" data-scroll-key={'detail:'+p.data.selected_stock_code+':'+tab}><header className="rw-detail-head"><strong>{p.selected?.name || query.get('stock') || '个股详情'} <small>{p.selected?.code}</small></strong><button onClick={close}>关闭详情</button></header>
    <div className="rw-tabs" aria-label="个股详情分类">{['走势','竞价','异动','资讯','证据','AI解读'].map(value => <button key={value} aria-pressed={tab===value} onClick={() => setTab(value)}>{value}</button>)}</div>
    {p.detailError && <p role="alert">{p.detailError}<button onClick={p.retryDetails}>重试详情</button></p>}
    {!p.selected ? <Empty title="从列表选择一只股票"/> : <><div className="rw-quote"><strong>{number(p.selected.price,2)}</strong><Change value={p.selected.pct_change}/><Follow selected={p.selected} watch={p.watch}/></div>
    {p.detailBusy && <p role="status">正在更新所选对象资料…</p>}
    {tab==='走势' && <>{!p.asOfTime&&p.data.trade_day===new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date())&&p.selected.instrument_id?<HistoryChart key={p.selected.code} instrument={p.selected.instrument_id} period="分时" day={p.data.trade_day} onDayChange={p.selectDay}/>:<p>历史时点价格曲线尚无完整接收口径；以下保留该时点可核的封单采样。</p>}<h3>已存封单走势</h3><SealChart key={p.selected.code+':'+p.data.trade_day} points={p.data.stock_seals}/><p>行情截至 {dateTime(p.selected.source_time)} · 缺段不补画</p><a href={'/workbench?' + new URLSearchParams({view:'ladder',stock:p.selected.code,trade_day:p.data.trade_day,...(p.asOfTime?{as_of_time:p.asOfTime}:{})})}>打开连板与盘口资料 →</a></>}
    {tab==='竞价' && <><h3>实际竞价成交额 · 亿元</h3><AuctionChart entries={p.data.auction_days.map(day => ({day:day.day,values:[day.items.find(s=>s.code===p.selected?.code)?.auction_amount_cny??null]}))}/><a href={'/workbench?' + new URLSearchParams({view:'auction',stock:p.selected.code,trade_day:p.data.trade_day,...(p.asOfTime?{as_of_time:p.asOfTime}:{})})}>打开四日节点对照 →</a></>}
    {tab==='异动' && <>{p.data.events.filter(e=>e.code===p.selected?.code).map((e,i)=><p key={`${e.time}:${i}`}><time>{time(e.time)}</time> {e.label}</p>)}{!p.data.events.some(e=>e.code===p.selected?.code)&&<Empty title="未保存可核异动记录"/>}</>}
    {tab==='资讯' && <TopicResearch embedded keyword={p.selected.name} topicKey={p.selected.code} source="stock" day={p.data.trade_day} asOf={p.asOfTime}/>}
    {tab==='证据' && <><p>来源：{p.selected.source_name || '待核'}</p><p>行情时间：{dateTime(p.selected.source_time)}</p><p>接收时间：{dateTime(p.selected.source_received_at)}</p><p>口径：{p.selected.price_basis || '未披露'}</p><a href={'/research/reviews?' + new URLSearchParams({trade_day:p.data.trade_day,stock:p.selected.code,...(p.asOfTime?{as_of_time:p.asOfTime}:{})})}>查看复盘证据 →</a></>}
    {tab==='AI解读' && <AIObservation {...p} session={p.session} tradeDay={p.data.trade_day} view="overview"/>}</>}
  </div>;
  return <section ref={root} id="board-direction" tabIndex={-1} className={`rw-root ${original.embedded ? "rw-embedded" : ""}`} data-pane={pane} aria-label="板块与方向研究区">{!original.embedded && <header className="rw-extension-head"><h2>板块与方向</h2><p>联动研究 · 列表范围仅作用于本区</p><a href="#main-content">返回原盯盘区域 ↑</a></header>}
    <nav className="rw-mobile-nav" aria-label="工作区视图">{['板块','列表','详情'].map(value=><button key={value} aria-pressed={pane===value} onClick={()=>setPane(value)}>{value}</button>)}</nav>
    <div className="rw-grid"><section className="rw-sectors ty-panel" data-scroll-key="sectors"><h3>板块列表</h3><table className="ty-table"><thead><tr><th>板块</th><th>涨幅</th><th>净额·亿</th></tr></thead><tbody>{sectors.map(s=><tr key={s.code} data-current={s.code===p.sector?.code||undefined}><td><button title={s.name} onClick={()=>{setRankingReset(rankingReset+1);setScope('sector');setPendingSector(s.code); const target=new URL(location.href);target.searchParams.set('sector',s.code);target.searchParams.set('research_scope','sector');updateLocation(target);setPane('列表');}}>{s.name}</button></td><td><Change value={s.pct_change}/></td><td>{money(s.main_net_cny)}</td></tr>)}</tbody></table>{!sectors.length&&<Empty title="等待已采板块资料"/>}</section>
    <section className="rw-list ty-panel"><div className="rw-tabs">{[['all','全市场'],['sector','所选板块'],['watch','我的关注']].map(([value,label])=><button key={value} aria-pressed={scope===value} onClick={()=>chooseScope(value)}>{label}</button>)}<select aria-label="个股排序" value={sort} onChange={e=>{setRankingReset(rankingReset+1);setSort(e.target.value);}}><option value="pct_change">涨幅</option><option value="speed_5m">5分涨速</option><option value="amount_cny">成交额</option></select>{ranking.changed&&<button onClick={ranking.apply}>更新排序</button>}</div>
    <p className="ty-panel-caption">{scope==='sector' ? `${p.sector?.name || '请选择板块'} · 成员日 ${p.trackingDay || p.data.members_trade_day || '待核'}` : scope==='watch'?'当前账号关注范围':p.data.breadth.scope} · 已返回 {rows.length}只</p>
    {p.sectorError&&scope==='sector'&&<p role="alert">{p.sectorError}<button onClick={p.retrySector}>重试板块</button></p>}
    <div className="rw-table-scroll" data-scroll-key={'stocks:'+scope+':'+(p.sector?.code||'')}><table className="ty-table"><thead><tr><th>名称</th><th>涨幅</th><th>5分涨速</th><th>成交·亿</th><th>题材</th></tr></thead><tbody>{ordered.map(row=><tr key={row.code} data-current={row.code===p.selected?.code||undefined}><td><StockName stock={row} onSelect={selectStock}/></td><td><Change value={row.pct_change}/></td><td><Change value={row.speed_5m}/></td><td>{money(row.amount_cny)}</td><td title={row.theme || ''}>{row.theme || '—'}</td></tr>)}</tbody></table>{rows.length>visibleCount&&<button onClick={()=>setVisibleCount(visibleCount+100)}>再显示100只（已展示{visibleCount}/{rows.length}）</button>}{!rows.length&&<Empty title={p.sectorBusy&&scope==='sector'?'正在读取板块成员':'当前范围暂无已存记录'}/>}</div>
    {p.sector&&<><a className="rw-topic-link" href={'/news?'+topic}>研究 {p.sector.name} 的事件与题材 →</a><FundsChart sectors={[p.sector]} selected={p.sector.code} history={p.data.funds_history} intraday={p.data.intraday_funds?.trade_day===p.data.trade_day} intradayPoints={(p.data.intraday_funds?.points??[]).filter(point=>point.sector_code===p.sector?.code)}/></>}
    {p.sector&&<div className="rw-sector-events" data-scroll-key={'events:'+p.sector.code}><TopicResearch embedded keyword={p.sector.name} topicKey={p.sector.code} source="dc" day={p.data.trade_day} asOf={p.asOfTime}/></div>}
    </section><aside className="rw-desktop-detail ty-panel">{!compact&&detail}</aside></div>
    {compact&&<dialog aria-label="个股详情" ref={drawer} className="rw-drawer ty-panel" onCancel={close}>{detail}</dialog>}
  </section>;
}
