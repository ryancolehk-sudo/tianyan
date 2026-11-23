import { Panel, StockName, Change } from './Components';
import type { PageProps } from './Components';
import { rankAuction } from './auctionComparison';
import type { AuctionNode, AuctionSort } from './auctionComparison';
import { useStableRanking } from '../../shared/workspaceState';
import { dateTime, money } from './data';

export function AuctionRanking({p,node,sort,range,reset}:{reset:number;p:PageProps;node:AuctionNode;sort:AuctionSort;range:string}) {
  const entry=(p.auctionRankings??p.data.auction_days).find(day=>day.day===p.data.trade_day);
  const rows=rankAuction((entry?.items??[]).filter(row=>range==='watch'?p.watch.has(row):range==='ladder'?(row.previous_height??0)>=2:true),sort,node);
  const rank=useStableRanking(rows,`${p.session.data.user.id}:auction-rank:${p.data.trade_day}:${p.asOfTime}:${sort}:${range}:${node}`,reset);
  const lookup=new Map(rows.map(row=>[row.code,row]));
  return <Panel title="观察日竞价榜单" action={rank.changed&&<button onClick={rank.apply}>更新排序</button>}><p className="ty-panel-caption">{p.data.trade_day} · {range==='watch'?'我的关注':range==='ladder'?'昨日连板':'已存范围'} · 成交额／封单：亿元</p><div className="ty-table-scroll auction-ranking-scroll" data-scroll-key="auction-ranking"><table className="ty-table"><thead><tr><th>股票</th><th>{sort==='auction'?'竞价涨幅':sort==='amount'?'竞价成交额':sort==='seal'?node+'封单':'滚动封单'}</th></tr></thead><tbody>{rank.order.map(code=>{const row=lookup.get(code);return row&&<tr key={code} data-current={p.selected?.code===code||undefined}><td><StockName stock={row} onSelect={p.selectStock}/></td><td title={sort==='seal'||sort==='current'?'封单采集 '+dateTime(row.node_received_at):'竞价结果；与节点封单分别统计'}>{sort==='auction'?<Change value={row.auction_indicative_pct??row.auction_pct}/>:money(sort==='amount'?row.auction_amount_cny:sort==='seal'?row.node_amounts[node]:row.auction_current_seal_cny)}</td></tr>;})}</tbody></table>{!rows.length&&<p>该范围暂无已存竞价资料。</p>}</div></Panel>;
}
