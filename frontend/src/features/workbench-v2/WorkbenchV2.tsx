import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { TradingProvider, useTradingContext, useWatchlist } from '../workbench/TradingContext';
import { selectedTab, tabLink } from './WorkbenchShell';
import type { WorkbenchTab } from './WorkbenchShell';
import type { WorkbenchModel, Stock, Sector, Value } from './model';

const colors = ['#f1c85f','#4ca4f9','#f55485','#a26be6','#ff992f','#21caca','#27c580','#b0a7e2','#81aacc','#fbc985'];
const capabilityNames: Record<string,string> = {market_quotes:'股票行情',auction_trade:'竞价成交',ladder_daily:'连板日榜',sector_funds_daily:'板块盘后资金',stock_funds_daily:'个股盘后资金',sector_members:'板块成分',auction_order:'竞价封单',seal_book:'封单盘口',fund_intraday:'盘中资金',ai_observation:'AI观察'};
const number = (value: Value | undefined, digits = 0) => value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits});
const percent = (value: Value | undefined) => value == null ? '—' : (value > 0 ? '+' : '') + number(value, 2) + '%';
const yuan = (value: Value | undefined) => value == null ? '—' : Math.abs(value) >= 1e8 ? number(value / 1e8, 2) + '亿' : number(value / 1e4, 1) + '万';
const net = (value: Value | undefined) => value == null ? '—' : (value > 0 ? '+' : '') + number(value / 1e8, 2);
const tone = (value: Value | undefined) => value == null || value === 0 ? '' : value > 0 ? 'up' : 'down';
const time = (value?: string | null) => value ? new Date(value).toLocaleTimeString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit'}) : '—';
const shortDay = (day?: string | null) => day ? day.slice(5).replace('-', '/') : '—';
function setParam(key: string, value: string) { const url = new URL(location.href); if (value) url.searchParams.set(key, value); else url.searchParams.delete(key); updateLocation(url); }
function Empty({title = '暂无可用数据', children}: {title?: string; children?: ReactNode}) { return <div className="wb-empty"><strong>{title}</strong><span>{children ?? '接入并取得有效数据后显示'}</span></div>; }
function Panel({title, note, action, children, className = ''}: {title: string; note?: ReactNode; action?: ReactNode; children: ReactNode; className?: string}) {
  return <section className={'wb-panel ' + className}><header className="wb-panel-head"><h2>{title}</h2>{note && <small>{note}</small>}{action && <div className="wb-panel-action">{action}</div>}</header><div className="wb-panel-body">{children}</div></section>;
}
function StockLink({stock, selected, compact = false}: {stock: Stock; selected?: boolean; compact?: boolean}) {
  const trading = useTradingContext();
  const [error,setError]=useState('');
  return <span><button type="button" className={'wb-stock-button ' + (selected ? 'selected' : '')} onClick={() => {setError(''); if(stock.instrument_id)trading.selectInstrument(stock.instrument_id);else void trading.selectStock(stock.ts_code).catch(reason=>setError(reason instanceof Error?reason.message:'暂时无法选择此股票'));}} title={[stock.ts_code,stock.source_time?'行情 '+new Date(stock.source_time).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'未提供盘中行情时点'].join(' · ')}>{stock.name || stock.ts_code}{!compact && stock.theme && <span className="wb-tag">{stock.theme}</span>}</button>{error&&<small className="wb-stock-error" role="status">{error}</small>}</span>;
}
function ObservationRail({model, tab}: {model: WorkbenchModel | null; tab: WorkbenchTab}) {
  const selected = model?.selected_stock;
  const sector = model?.selected_sector;
  return <aside className="wb-observer" aria-label="AI 观察占位"><section className="wb-ai-section"><h2 className="wb-ai-title">AI 观察 <small>待接入</small></h2><h3>全市场观察</h3><div className="wb-ai-placeholder"><span className="wb-tag gold">分析服务尚未接入</span><strong>观察区域已预留</strong><div className="wb-ai-wire"/><div className="wb-ai-wire"/><div className="wb-ai-wire"/><p>后续在这里呈现市场变化、判断与对应依据。</p></div><p className="wb-ai-caption">当前行情可正常查看，AI 结论尚未生成。</p></section>
  <section className="wb-ai-section"><h3>{tab === 'ladder' ? '关注对象跟踪 · 预案核对' : '关注对象跟踪'}</h3><div className="wb-ai-placeholder"><strong className="gold">{tab === 'funds' ? sector?.name ?? '选择一个板块' : selected?.name ?? '选择一只股票'}</strong><span className="wb-tag">跟踪与核对待接入</span><div className="wb-ai-wire"/><div className="wb-ai-wire"/><div className="wb-ai-wire"/></div><p className="wb-ai-caption">选中对象随页面联动；此处仅保留版面。</p></section></aside>;
}
function MarketStrip({model, tab}: {model: WorkbenchModel | null; tab: WorkbenchTab}) {
  const stats = model?.stats;
  if (tab === 'ladder') return <div className="wb-metrics"><div className="wb-metric"><span>接力观察</span><b>盘中 / 收盘</b></div><div className="wb-metric"><span>收盘最高</span><b>{number(stats?.max_height)}板</b></div><div className="wb-metric"><span>前榜{shortDay(model?.cohort_day)}</span><b>{model?.stocks.filter(item => (item.previous_height ?? 0) > 0).length ?? '—'}只</b></div><div className="wb-metric"><span>封住比例</span><b>待核实</b></div><div className="wb-metric"><span>近期断板</span><b>{model?.broken_stocks.length || '—'}只</b></div><div className="wb-metric"><span>观察日</span><b>{shortDay(model?.trade_day)}</b></div></div>;
  return <div className="wb-metrics">{(model?.indices.length ? [...model.indices].sort((a,b)=>['000001.SH','399001.SZ','399006.SZ','000688.SH'].indexOf(a.code)-['000001.SH','399001.SZ','399006.SZ','000688.SH'].indexOf(b.code)).slice(0,3) : [{code:'sse',name:'上证',price:null,pct_change:null},{code:'szse',name:'深证',price:null,pct_change:null},{code:'chinext',name:'创业板',price:null,pct_change:null}]).map(item => <div className="wb-metric" key={item.code}><span>{item.name.replace('指数','')}</span><b className={tone(item.pct_change)}>{percent(item.pct_change)}</b><small>{number(item.price,2)}</small></div>)}<div className="wb-metric"><span>成交额</span><b>{stats?.turnover_cny == null ? '—' : stats.turnover_cny >= 1e12 ? number(stats.turnover_cny / 1e12,2)+'万亿' : yuan(stats.turnover_cny)}</b></div>{tab === 'funds' ? <div className="wb-metric"><span>观察板块</span><b>{model?.sectors.length ?? '—'}个</b></div> : <><div className="wb-metric"><span>上涨 / 下跌</span><b className="up">{number(stats?.advancing)}</b><b className="down">{number(stats?.declining)}</b></div><div className="wb-metric"><span>炸板</span><b className="up">{number(stats?.broken_count)}</b></div><div className="wb-metric"><span>主板最高</span><b>{number(stats?.max_height)}板</b></div></>}</div>;
}
function LineChart({labels, series, currency = false, highlight}: {labels: string[]; series: {name: string; values: (number|null)[]; color: string}[]; currency?: boolean; highlight?: string}) {
  const valid = series.flatMap(item => item.values).filter((value): value is number => value != null && Number.isFinite(value));
  if (!valid.length) return <Empty title="尚无连续采样">已有快照会显示在榜单中；曲线从真实留存点开始</Empty>;
  const max = Math.max(...valid, 1), min = Math.min(0, ...valid), span = max - min || 1;
  const top = currency ? max + span * .12 : Math.ceil(max / 20) * 20, bottom = min < 0 ? min - span * .06 : 0;
  const x = (i: number) => 48 + i / Math.max(labels.length - 1,1) * 615;
  const y = (value: number) => 236 - (value - bottom) / (top - bottom) * 214;
  const label = (value: number) => currency ? number(value/1e8, value >= 1e9 ? 0 : 1) + '亿' : number(value);
  const clock = (s: string) => s.includes('T') ? time(s) : s.length > 10 ? s.slice(11,16) : s.slice(5).replace('-','/');
  const ticks = [...new Set([0, Math.floor((labels.length-1)/3), Math.floor((labels.length-1)*2/3), labels.length-1])];
  return <><div className="wb-chart-legend">{series.map(s => <span key={s.name}><i className="wb-dot" style={{background:s.color}}/>{s.name}</span>)}</div><div className="wb-chart-wrap"><svg className="wb-chart" viewBox="0 0 704 270" preserveAspectRatio="none" role="img" aria-label={series.map(s=>s.name).join('、')+'真实留存走势'}>
    {[0,.25,.5,.75,1].map(f => { const value=bottom+(top-bottom)*f; return <g key={f}><line className="chart-grid" x1="48" x2="663" y1={y(value)} y2={y(value)}/><text x="38" y={y(value)+4} textAnchor="end">{label(value)}</text></g>; })}
    {ticks.map(i=><g key={i}><line className="chart-grid" x1={x(i)} x2={x(i)} y1="22" y2="236"/><text x={x(i)} y="259" textAnchor="middle">{clock(labels[i]??'')}</text></g>)}
    <line className="chart-axis" x1="48" x2="663" y1="236" y2="236"/>
    {series.map(s => { const segments: string[][]=[[]]; s.values.forEach((value,i) => { if(value==null){segments.push([]);return;} const previous=labels[i-1],current=labels[i]; if(previous?.includes('T')&&current?.includes('T')&&new Date(current).getTime()-new Date(previous).getTime()>180000) segments.push([]); segments[segments.length-1].push(x(i)+','+y(value)); }); return <g key={s.name} opacity={highlight && highlight!==s.name ? .4 : 1}>{segments.filter(a=>a.length).map((points,i)=><polyline key={i} points={points.join(' ')} fill="none" stroke={s.color} strokeWidth={highlight===s.name?3:2} vectorEffect="non-scaling-stroke"/>)}{s.values.map((value,i)=>value!=null && (i===s.values.length-1 || s.values.length<10) ? <circle key={i} cx={x(i)} cy={y(value)} r="3.5" fill={s.color}><title>{labels[i]} · {label(value)}</title></circle> : null)}</g>; })}
  </svg></div></>;
}
function StockTable({stocks, selected, speed = false, ladder = false}: {stocks: Stock[]; selected?: string; speed?: boolean; ladder?: boolean}) {
  if (!stocks.length) return <Empty/>;
  return <table className="wb-table"><thead><tr><th>{ladder ? '前榜高度 / 股票' : '名称'}</th><th>{speed ? '5分涨速' : '涨幅'}</th><th>{ladder ? '状态' : '成交额'}</th><th>{ladder ? '换手%' : speed ? '当前涨幅' : '题材'}</th></tr></thead><tbody>{stocks.slice(0,8).map((s,i)=><tr key={s.ts_code} className={selected===s.ts_code?'selected':''}><td>{ladder ? <span className="gold">{number(s.previous_height)}板 </span> : <span className={i<2?'gold':'muted'} style={{marginRight:12}}>{i+1}</span>}<StockLink stock={s} compact/></td><td className={tone(speed?s.change_5m:s.pct_change)}>{percent(speed?s.change_5m:s.pct_change)}</td><td>{ladder ? s.current_state || '待核实' : yuan(s.amount_cny)}</td><td className={speed?tone(s.pct_change):''}>{ladder ? number(s.turnover_rate,1) : speed ? percent(s.pct_change) : s.theme || '—'}</td></tr>)}</tbody></table>;
}
function LadderChips({stocks, selected, search}: {stocks: Stock[]; selected?: string; search: string}) {
  const groups = [...new Set(stocks.map(s=>s.previous_height).filter((height): height is number=>height!=null&&height>0))].sort((a,b)=>b-a);
  if(!groups.length) return <Empty title="暂无已确认梯队">收盘榜单入库后显示确认高度</Empty>;
  return <>{groups.slice(0,4).map(height=><div className="wb-ladder-group" key={height}><strong>{height}板</strong><div className="wb-ladder-stocks">{stocks.filter(s=>s.previous_height===height).slice(0,4).map(s=><StockLink key={s.ts_code} stock={s} selected={s.ts_code===selected} compact/>)}</div></div>)}<p className="wb-footnote"><a href={tabLink('ladder',search)}>查看连板观察 ›</a>　前榜确认高度 · 盘中状态独立核对</p></>;
}
function SectorTable({sectors, selected, compact = false}: {sectors: Sector[]; selected?: string; compact?: boolean}) {
  const max=Math.max(...sectors.map(s=>Math.abs(s.net_cny??0)),1);
  if(!sectors.length)return <Empty title="尚无板块资金">已接入的盘后资金将在这里显示</Empty>;
  return <table className="wb-table"><thead><tr><th>{compact?'板块':'#　板块'}</th><th>{compact?'净流入 · 亿元':'累计净额(亿)'}</th><th>{compact?'涨幅':'近5分(亿)'}</th>{!compact&&<th>名次变化</th>}</tr></thead><tbody>{sectors.slice(0,10).map((s,i)=><tr key={s.code} className={s.code===selected?'selected':''}><td><button className="wb-stock-button" aria-label={s.name} onClick={()=>setParam('sector',s.code)}>{!compact&&<><span className="muted">{i+1}　</span><i className="wb-dot" style={{background:colors[i%colors.length]}}/></>}{s.name}</button></td><td className={tone(s.net_cny)}>{compact?<div className="wb-sector-bars"><i className={tone(s.net_cny)} style={{width:Math.max(Math.abs(s.net_cny??0)/max*78,2)}}/><span>{net(s.net_cny)}</span></div>:net(s.net_cny)}</td><td className={tone(compact?s.pct_change:s.change_5m)}>{compact?percent(s.pct_change):net(s.change_5m)}</td>{!compact&&<td className={tone(s.rank_change)}>{s.rank_change==null?'—':s.rank_change>0?'↑'+s.rank_change:s.rank_change<0?'↓'+Math.abs(s.rank_change):'—'}</td>}</tr>)}</tbody></table>;
}
function Overview({model, search}: {model: WorkbenchModel|null; search: string}) {
  const stocks=model?.stocks??[],selected=model?.selected_stock?.ts_code;
  const allSectors=model?.sectors??[];
  const ranked=[...allSectors.filter(s=>s.net_cny!=null&&s.net_cny>0).slice(0,5),...allSectors.filter(s=>s.net_cny!=null&&s.net_cny<0).slice(-5).reverse()];
  const sectors=ranked.length?ranked:allSectors.slice(0,10);
  return <div className="wb-main"><div className="wb-top-two"><Panel title="涨跌停家数走势" note="当前价格等于当日限制价" action={model?.trade_day ?? '待采集'}><LineChart labels={model?.breadth.map(p=>p.time)??[]} series={[{name:'涨停 '+number(model?.stats.limit_up),values:model?.breadth.map(p=>p.up)??[],color:'#ff5759'},{name:'跌停 '+number(model?.stats.limit_down),values:model?.breadth.map(p=>p.down)??[],color:'#2ccb9e'}]}/><p className="wb-footnote">按真实采样留存 · 午休与采样缺口不连线</p></Panel><Panel title="板块资金" note="盘后 · 流入/流出前5" action={shortDay(model?.sectors[0]?.trade_day)}><SectorTable sectors={sectors} selected={model?.selected_sector?.code} compact/><p className="wb-footnote">日频数据，不代表当前盘中净流入</p></Panel></div>
  <div className="wb-bottom-three"><Panel title="个股涨幅榜" note="实际返回范围"><StockTable stocks={[...stocks].sort((a,b)=>(b.pct_change??-Infinity)-(a.pct_change??-Infinity))} selected={selected}/></Panel><Panel title="个股涨速榜" note="近5分钟 · 同时段采样"><StockTable stocks={stocks.filter(s=>s.change_5m!=null).sort((a,b)=>(b.change_5m??0)-(a.change_5m??0))} selected={selected} speed/></Panel><Panel title="连板梯队" note={'前榜 '+shortDay(model?.cohort_day)}><LadderChips stocks={stocks} selected={selected} search={search}/></Panel></div></div>;
}
function AuctionPage({model}: {model: WorkbenchModel|null}) {
  const selected=model?.selected_stock;
  const watch=useWatchlist();
  const selectedCode=selected?.ts_code;
  const days=model?.auction_days??[];
  const displayed=days.length?days:[{day:model?.trade_day??'',total_count:0,rows:[]},{day:'',total_count:0,rows:[]},{day:'',total_count:0,rows:[]},{day:'',total_count:0,rows:[]}];
  const watched=(model?.stocks??[]).filter(s=>s.instrument_id&&watch?.items.some(item=>item.instrument.id===s.instrument_id));
  const history=days.map(day=>({day:day.day,stock:day.rows.find(s=>s.ts_code===selectedCode)})).reverse();
  return <div className="wb-main wb-auction-main"><Panel title="多日封单对照" note="按成交额独立排名 · 封单待补" action="封单节点待补充"><div className="wb-auction-days">{displayed.slice(0,4).map((day,i)=><section className="wb-auction-day" key={day.day||i}><h3 title={day.total_count?'成交样本 '+day.total_count+'只，前8名与已选对象联动':undefined}>{day.day?shortDay(day.day):'待归档'}{i===0?' · 观察日':''}</h3><div className="wb-node-values" style={{padding:'5px 8px',fontSize:10}}><span>09:15</span><span>09:20</span><span>09:25</span></div>{day.rows.length?day.rows.slice(0,8).map((s,index)=><div className={'wb-auction-row '+(s.ts_code===selectedCode?'selected':'')} key={s.ts_code}><div><span className="gold">{index+1}</span><StockLink stock={s} compact/><span className="wb-tag">{s.theme||'—'}</span></div><div className="wb-node-values"><span>{s.node_0915_cny==null?'—':net(s.node_0915_cny)}</span><span>{s.node_0920_cny==null?'—':net(s.node_0920_cny)}</span><span>{s.node_0925_cny==null?'—':net(s.node_0925_cny)}</span></div><div><span className="muted">成交 {yuan(s.auction_amount_cny)}</span><span className={tone(s.auction_pct_change)}>{percent(s.auction_pct_change)}</span></div></div>):<Empty title="该日尚无成交数据"/>}</section>)}</div><p className="wb-footnote">— 表示节点封单缺失；下方成交结果独立展示，不填入封单栏。</p></Panel>
  {watch?.error&&<p className="wb-stock-error" role="status">{watch.error}</p>}<div className="wb-auction-sub"><Panel title={selected?selected.name+' · 竞价成交结果':'竞价成交结果'} note={selected?.ts_code} action={selected?.instrument_id?<button disabled={watch?.busy||watch?.saving} onClick={()=>{if(!watch?.items.some(item=>item.instrument.id===selected.instrument_id))void watch?.add(selected.instrument_id!);}}>{watch?.items.some(item=>item.instrument.id===selected.instrument_id)?'★ 已关注':'☆ 关注'}</button>:undefined}>
    <LineChart labels={history.map(d=>d.day)} series={[{name:'竞价成交额 · 元',values:history.map(d=>d.stock?.auction_amount_cny??null),color:'#eac265'}]} currency/><p className="wb-footnote">成交结果通常在竞价结束后取得；三个委托节点仍待补充数据。</p></Panel><Panel title="竞价关注池" note={watched.length+'只'} action={<a href="/research/plans">查看背景 ›</a>}>{watched.length?<table className="wb-table"><thead><tr><th>名称</th><th>竞价成交</th><th>节点状态</th></tr></thead><tbody>{watched.slice(0,6).map(s=><tr key={s.ts_code} className={s.ts_code===selectedCode?'selected':''}><td><StockLink stock={s} compact/></td><td>{yuan(days[0]?.rows.find(row=>row.ts_code===s.ts_code)?.auction_amount_cny)}</td><td className="gold">待补充</td></tr>)}</tbody></table>:<Empty title="还没有关注对象">选中股票后可加入关注池</Empty>}</Panel></div></div>;
}
function LadderPage({model, search}: {model: WorkbenchModel|null; search: string}) {
  const selected=model?.selected_stock;
  const stocks=(model?.stocks??[]).filter(s=>(s.previous_height??0)>0).sort((a,b)=>(b.previous_height??0)-(a.previous_height??0));
  const peers=stocks.filter(s=>s.previous_height===selected?.previous_height).slice(0,4);
  const related=stocks.filter(s=>s.theme&&s.theme===selected?.theme);
  return <div className="wb-main wb-ladder-main"><Panel title="连板梯队比较" note={'沪深主板 · 前榜 '+shortDay(model?.cohort_day)}><div className="wb-scroller">{stocks.length?<table className="wb-table"><thead><tr><th>前榜高度 / 股票</th><th>当前状态</th><th>首次观测</th><th>换手%</th><th>封单/亿</th><th>涨幅</th></tr></thead><tbody>{stocks.slice(0,12).map(s=><tr key={s.ts_code} className={s.ts_code===selected?.ts_code?'selected':''}><td><span className="gold">{s.previous_height}板　</span><StockLink stock={s}/></td><td>{s.current_state||'待核实'}</td><td className="muted">{time(s.first_observed_at)}</td><td>{number(s.turnover_rate,1)}</td><td className="gold">{s.seal_amount_cny==null?'—':number(s.seal_amount_cny/1e8,2)}</td><td className={tone(s.pct_change)}>{percent(s.pct_change)}</td></tr>)}</tbody></table>:<Empty title="暂无前榜梯队"/>}</div><p className="wb-footnote">盘中观察，收盘确认高度；达到涨停价与盘口确认封住分别标记。</p></Panel>
  <Panel title="关联对象对照" note={selected?.theme||'选中股票联动'}><div className="wb-relations"><div><h3>同身位 · 前榜{number(selected?.previous_height)}板</h3><StockTable stocks={peers} selected={selected?.ts_code}/></div><div><h3>同题材梯队 · {selected?.theme||'暂无题材归属'}</h3><LadderChips stocks={related} selected={selected?.ts_code} search={search}/></div></div></Panel>
  <Panel title={(selected?.name||'选中股票')+' · 封板与换手'} note="封单为真实盘口快照"><div className="wb-inline-stats"><span>当前封单<b>{yuan(selected?.seal_amount_cny)}</b></span><span>换手<b>{selected?.turnover_rate==null?'—':number(selected.turnover_rate,1)+'%'}</b></span><span>累计成交<b>{yuan(selected?.amount_cny)}</b></span><span>状态<b>{selected?.current_state||'待核实'}</b></span></div><div className="wb-seal-placeholder"><Empty title="封单快照等待盘口数据">买一价量有效且等于涨停价时才计算封单</Empty></div><div className="wb-event-line"><span>关键事件</span><span>尚无已验证的观测记录</span></div></Panel>
  <Panel title="近期断板反馈" note="已确认断板样本">{model?.broken_stocks.length?<table className="wb-table"><thead><tr><th>名称</th><th>原高度</th><th>当前涨幅</th></tr></thead><tbody>{model.broken_stocks.slice(0,6).map(s=><tr key={s.ts_code}><td><StockLink stock={s} compact/></td><td className="gold">{number(s.previous_height)}板</td><td className={tone(s.pct_change)}>{percent(s.pct_change)}</td></tr>)}</tbody></table>:<Empty title="尚无可用断板样本">需要连续交易日的收盘确认榜单</Empty>}</Panel></div>;
}
function FundsPage({model}: {model: WorkbenchModel|null}) {
  const [intraday,setIntraday]=useState(false);
  const sector=model?.selected_sector??model?.sectors[0];
  const sectors=(model?.sectors??[]).slice(0,10);
  const days=[...new Set(model?.fund_history.map(p=>p.day)??[])].sort();
  const members=(model?.stocks??[]).filter(s=>sector&&s.sector_code===sector.code);
  const series=sectors.map((s,i)=>({name:s.name,values:days.map(day=>model?.fund_history.find(p=>p.day===day&&p.sector_code===s.code)?.net_cny??null),color:colors[i%colors.length]}));
  const points=model?.fund_history.filter(p=>p.sector_code===sector?.code).sort((a,b)=>a.day.localeCompare(b.day))??[];
  return <div className="wb-main wb-funds-main"><div className="wb-funds-top"><Panel title="板块资金脉络" note={intraday?'盘中分钟资金 · 待接入':'日频主力净额 · 亿元'} action={model?.sectors[0]?.trade_day??'待采集'}><div className="wb-multichart">{intraday?<Empty title="盘中资金曲线待接入">已保留同源分钟资金的图形与排名联动位置</Empty>:<LineChart labels={days} series={series} currency highlight={sector?.name}/>}</div><div className="wb-funds-toggle"><button className={!intraday?'active':''} onClick={()=>setIntraday(false)}>盘后资金 · 日频</button><button className={intraday?'active':''} onClick={()=>setIntraday(true)}>盘中框架 · 待接入</button></div><p className="wb-footnote">不同来源资金定义独立保留；不会将日频资金绘成分钟变化。</p></Panel><Panel title="板块资金排名" note="按当日主力净额" action={shortDay(model?.sectors[0]?.trade_day)}><SectorTable sectors={sectors} selected={sector?.code}/><p className="wb-footnote">近5分钟与盘中名次变化等待同源时点数据</p></Panel></div>
  <div className="wb-funds-bottom"><Panel title={(sector?.name||'板块')+' · 成分股表现'} note="板块成分日期与行情时点分别保留">{members.length?<table className="wb-table"><thead><tr><th>名称</th><th>累计净额(亿)</th><th>近5分(亿)</th><th>涨幅</th><th>成交额</th><th>当前状态</th></tr></thead><tbody>{members.slice(0,6).map(s=><tr key={s.ts_code}><td><StockLink stock={s} compact/></td><td className={tone(s.net_cny)} title={s.fund_trade_day??undefined}>{net(s.net_cny)}</td><td>—</td><td className={tone(s.pct_change)}>{percent(s.pct_change)}</td><td>{yuan(s.amount_cny)}</td><td>{s.current_state||'待核实'}</td></tr>)}</tbody></table>:<Empty title="暂无可用成分表现">成分连接每次采集一个明确板块；可在数据管理中选择板块代码后采集</Empty>}</Panel><Panel title="资金与表现对照" note={sector?.name??'选择板块'}>{points.length?<table className="wb-table"><thead><tr><th>交易日</th><th>主力净额(亿)</th><th>板块涨幅</th><th>成交额</th></tr></thead><tbody>{points.slice(-4).map(p=><tr key={p.day}><td>{shortDay(p.day)}</td><td className={tone(p.net_cny)}>{net(p.net_cny)}</td><td className={tone(p.pct_change)}>{percent(p.pct_change)}</td><td>{yuan(p.turnover_cny)}</td></tr>)}</tbody></table>:<Empty title="等待同口径资金历史"/>}<div className="wb-note-band"><span>近5分钟资金变化</span><span>待接入</span></div></Panel></div></div>;
}
export function WorkbenchV2({session}: {session: SessionResponse}) {return <TradingProvider key={session.data.user.id} session={session}><WorkbenchPages/></TradingProvider>;}
function WorkbenchPages() {
  const navigation=useNavigation();
  const trading=useTradingContext();
  const view=selectedTab(navigation.path,navigation.search);
  const params=new URLSearchParams(navigation.search);
  const sector=params.get('sector')??'';
  const asOf=params.get('as_of_time')??'';
  const request=useCallback(()=>api.workbenchView(new URLSearchParams({view,...(trading.day?{trade_day:trading.day}:{}),...(trading.instrumentId?{instrument_id:trading.instrumentId}:{}),...(sector?{sector_code:sector}:{}),...(asOf?{as_of_time:asOf}:{})})),[view,trading.day,trading.instrumentId,sector,asOf]);
  const read=useRead(request);
  useEffect(()=>{if(asOf)return;const timer=window.setInterval(read.reload,60000);return()=>window.clearInterval(timer);},[asOf,read.reload]);
  const candidate=read.data?.data??null;
  const model=candidate && candidate.view===view && (!trading.day || candidate.trade_day===trading.day) ? candidate : null;
  const [node,setNode]=useState('09:25');
  const marketStamp=[...(model?.stocks.map(stock=>stock.source_time)??[]),...(model?.indices.map(index=>index.source_time)??[])].filter((value):value is string=>!!value).sort((a,b)=>Date.parse(b)-Date.parse(a))[0];
  return <div className="wb-page" aria-busy={read.busy}>
    {view==='auction'?<div className="wb-controls"><label>观察日期<input type="date" aria-label="观察交易日" value={trading.day||model?.trade_day||''} onChange={event=>trading.setDay(event.target.value)}/></label><button onClick={()=>trading.setDay('')}>最新交易日</button><div className="wb-time-nodes" aria-label="竞价节点">{['09:15','09:20','09:25'].map(stage=><button key={stage} className={stage===node?'active':''} onClick={()=>setNode(stage)}>{stage}</button>)}</div><span className="wb-source-date">节点封单：待接入</span></div>:<MarketStrip model={model} tab={view}/>}
    {read.busy&&<div className="wb-loading" role="status">正在读取本机 Tushare 缓存…</div>}
    {read.error&&<div className="wb-error"><span>暂时无法读取页面数据：{read.error.message}</span><button onClick={read.reload}>重试</button></div>}
    <div className="wb-body">{view==='overview'?<Overview model={model} search={navigation.search}/>:view==='auction'?<AuctionPage model={model}/>:view==='ladder'?<LadderPage model={model} search={navigation.search}/>:<FundsPage model={model}/>}<ObservationRail model={model} tab={view}/></div>
    <div className="wb-capabilities"><details><summary>{model?.source_label??'Tushare · 正在读取本机缓存'}　{model?.trade_day??''}　{marketStamp?'· 行情源最新 '+time(marketStamp):''}　· 数据与缺口说明</summary><ul>{model?.capabilities.map(cap=><li key={cap.key}>{capabilityNames[cap.key]??'数据模块'}：{cap.reason||'待核实'}{cap.source_time?' · 源时点 '+time(cap.source_time):''}{cap.received_at?' · 采集 '+new Date(cap.received_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):''}</li>)}</ul><p>{model?.scope}</p></details></div>
  </div>;
}
