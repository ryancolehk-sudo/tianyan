import { Panel, Change, Empty } from './Components';
import { signedMoney, time, tone } from './data';
import { useWorkspaceState } from '../../shared/workspaceState';
import type { WorkbenchData } from './types';
import type { ThsCategory } from './thsFundsData';

type SortKey = 'name' | 'main_net_cny' | 'pct_change';
export function OverviewFunds({data, user, cutoff, onSelect}: {
  data: WorkbenchData; user: string; cutoff: string; onSelect: (code: string) => void;
}) {
  const funds = data.intraday_funds;
  const key = `${user}:overview-funds:${funds?.trade_day ?? data.trade_day}:${cutoff}`;
  const [category, setCategory] = useWorkspaceState<ThsCategory>(key+':category', data.selected_sector_code?.startsWith('THS:concept:') ? 'concept' : 'industry');
  const [search, setSearch] = useWorkspaceState(key+':search:'+category, '');
  const [sort, setSort] = useWorkspaceState<{key:SortKey;ascending:boolean}>(key+':sort:'+category, {key:'main_net_cny',ascending:false});
  const rows = [...(funds?.sectors ?? [])].filter(row => row.category === category).sort((a,b) => {
    const av=a[sort.key],bv=b[sort.key];
    const missing=(v:unknown)=>v==null||typeof v==='number'&&!Number.isFinite(v);
    if(missing(av)||missing(bv))return Number(missing(av))-Number(missing(bv))||a.code.localeCompare(b.code);
    const comparison=typeof av==='number'&&typeof bv==='number'?av-bv:String(av).localeCompare(String(bv),'zh-CN');
    return (sort.ascending?comparison:-comparison)||a.code.localeCompare(b.code);
  });
  const shown=rows.filter(row=>row.name.includes(search.trim())||row.code.includes(search.trim()));
  const coverage = funds?.coverage?.find(row => row.category === category);
  const note = funds?.category_notes?.[category];
  const staleOnly = note === '较早采样，等待更新';
  const heading=(field:SortKey,label:string)=><th aria-sort={sort.key===field?(sort.ascending?'ascending':'descending'):'none'}>
    <button className="ty-rank-sort" onClick={()=>setSort({key:field,ascending:sort.key===field?!sort.ascending:false})} title={`按${label}${sort.key===field&&!sort.ascending?'升':'降'}序`}>
      {label}{sort.key===field&&<svg width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true" style={{transform:sort.ascending?'rotate(180deg)':undefined}}><path d="M6 1v9m-3-3 3 3 3-3"/></svg>}
    </button></th>;
  return <Panel title="资金流向" subtitle="同花顺 · 亿元" className="ty-overview-funds"
    action={<div className="ty-overview-tabs" role="group" aria-label="资金板块分类">{(['industry','concept'] as const).map(value =>
      <button key={value} type="button" aria-pressed={category === value} onClick={() => setCategory(value)}>{value === 'industry' ? '行业' : '概念'}</button>)}</div>}>
    <section className="ty-overview-funds-rank" aria-label="同花顺资金排名">
      <input className="ty-strength-search" aria-label="搜索资金板块" placeholder="搜索资金板块" value={search} onChange={event => setSearch(event.target.value)}/>
      <div className="ty-table-scroll" key={category+':'+sort.key+':'+sort.ascending+':'+search} tabIndex={0} aria-label="当日资金流向排行"><table className="ty-table"><thead><tr><th aria-label="排名"/>{heading('name','板块')}{heading('main_net_cny','净额')}{heading('pct_change','涨幅')}</tr></thead><tbody>
        {shown.map((row,index)=><tr key={row.code} data-current={row.code===data.selected_sector_code||undefined}>
          <td className="ty-muted">{index+1}</td>
          <td><button className="ty-text-button" type="button" title={row.name} aria-pressed={row.code===data.selected_sector_code} onClick={()=>onSelect(row.code)}>{row.name}</button></td>
          <td className={tone(row.main_net_cny)}>{signedMoney(row.main_net_cny)}</td><td><Change value={row.pct_change}/></td>
        </tr>)}
      </tbody></table>{!shown.length&&<Empty title={rows.length?'无匹配板块':'暂无已存板块资金'}/>}</div>
    </section>
    <div className="ty-overview-source"><span title={funds?.trade_day||data.trade_day}>接收 {time(coverage?.received_at)} · {rows.length} 个{category==='industry'?'行业':'概念'}{!coverage?.complete&&' · 未齐'}{staleOnly&&' · 较早采样'}</span>
      <details><summary>数据说明</summary><p>{funds?.trade_day||data.trade_day} · {funds?.scope}</p>{note&&<p>{note}</p>}</details>
    </div>
    {note&&!staleOnly&&<p className="ty-overview-warning" role="status">{note}</p>}
  </Panel>;
}
