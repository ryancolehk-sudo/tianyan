import { useState } from 'react';
import { Change, Panel } from './Components';
import type { PageProps } from './Components';
import { money, signedMoney, tone } from './data';
import { RankSortHeader } from './StockRanking';
type SortKey = 'name' | 'pct_change' | 'main_net_cny';

export function OverviewBoards({data, sector, selectSector}: PageProps) {
  const [search, setSearch] = useState('');
  const [sort,setSort] = useState<{key:SortKey;ascending:boolean}|null>(null);
  const rows = data.sectors.filter(row => row.name.includes(search.trim()));
  if (sort) rows.sort((a,b)=>{
    const av=a[sort.key],bv=b[sort.key];
    const missing=(v:unknown)=>v==null||typeof v==='number'&&!Number.isFinite(v);
    if(missing(av)||missing(bv)) return Number(missing(av))-Number(missing(bv))||a.code.localeCompare(b.code);
    const order=typeof av==='number'&&typeof bv==='number'?av-bv:String(av).localeCompare(String(bv),'zh-CN');
    return (sort.ascending?order:-order)||a.code.localeCompare(b.code);
  });
  const heading=(key:SortKey,label:string)=><RankSortHeader label={label} active={sort?.key===key} ascending={sort?.ascending??false}
    onSort={()=>setSort({key,ascending:sort?.key===key?!sort.ascending:false})}/>;
  return <Panel title="板块列表" className="ty-overview-boards">
    <input aria-label="搜索追踪板块" placeholder="搜索板块" value={search} onChange={event => setSearch(event.target.value)}/>
    <div className="ty-table-scroll" key={`${sort?.key}:${sort?.ascending}`} tabIndex={0} aria-label="可追踪板块"><table className="ty-table" aria-label="板块列表"><thead><tr>{heading('name','板块')}{heading('pct_change','涨幅')}{heading('main_net_cny','资金净流入')}</tr></thead><tbody>{rows.map(row => <tr key={row.code} data-current={row.code === sector?.code || undefined}><td><button className="ty-text-button" title={row.name} onClick={() => selectSector(row.code)}>{row.name}</button></td><td><Change value={row.pct_change}/></td><td className={tone(row.main_net_cny)} title={`资金日 ${data.funds_trade_day??'待核'} · ${money(row.main_net_cny)} 亿元`}>{signedMoney(row.main_net_cny)}{row.main_net_cny!=null&&<small>亿</small>}</td></tr>)}</tbody></table>{!rows.length && <p>暂无匹配板块</p>}</div>
  </Panel>;
}
