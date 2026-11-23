import { useMemo } from 'react';
import { Change, Empty, Panel, StockName } from './Components';
import type { WatchState } from './Components';
import type { Stock } from './types';
import { dateTime, money } from './data';
import { ReadableText } from '../../shared/ReadableText';
import { useWorkspaceState } from '../../shared/workspaceState';
import { useQuoteFocus } from './useQuoteFocus';
import type { QuoteFocusOptions } from './useQuoteFocus';

export type SortKey = 'name' | 'pct_change' | 'speed_5m' | 'amount_cny' | 'ths_themes';
type RankRow = Pick<Stock, 'code' | SortKey>;
type Settings = {sort:SortKey; ascending:boolean; query:string; scope:string; page:number};
const PAGE_SIZE=50;
const collator=new Intl.Collator('zh-CN',{numeric:true});
const theme=(stock:Pick<Stock,'ths_themes'>)=>(stock.ths_themes??[]).join('、');
export function compareStockRows(a:RankRow,b:RankRow,key:SortKey,ascending:boolean) {
  const av=key==='ths_themes'?theme(a):a[key],bv=key==='ths_themes'?theme(b):b[key];
  const absent=(v:unknown)=>v==null||v===''||typeof v==='number'&&!Number.isFinite(v);
  if(absent(av)||absent(bv))return absent(av)===absent(bv)?a.code.localeCompare(b.code):absent(av)?1:-1;
  const result=typeof av==='number'&&typeof bv==='number'?av-bv:collator.compare(String(av),String(bv));
  return (ascending?result:-result)||a.code.localeCompare(b.code);
}

export function RankSortHeader({label,active,ascending,onSort}: {
  label:string;active:boolean;ascending:boolean;onSort:()=>void;
}) {
  return <th aria-sort={active?(ascending?'ascending':'descending'):'none'}>
    <button className="ty-rank-sort" onClick={onSort} title={`按${label}${active&&!ascending?'升':'降'}序`}>
      {label}{active&&<svg className="ty-sort-direction" width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true" style={{transform:ascending?'rotate(180deg)':undefined}}><path d="M6 1v9m-3-3 3 3 3-3"/></svg>}
    </button></th>;
}

export function StockRanking({rows,selected,onSelect,dailyClose=false,readingKey,sourceLabel,watch,focus}: {
  rows:Stock[];selected:string|null;onSelect:(code:string)=>void;dailyClose?:boolean;
  readingKey:string;sourceLabel:string;watch:WatchState;focus?:QuoteFocusOptions;
}) {
  const [settings,setSettings]=useWorkspaceState<Settings>('stock-rank:'+readingKey,
    {sort:'pct_change',ascending:false,query:'',scope:'all',page:0});
  const update=(values:Partial<Settings>)=>setSettings({...settings,page:0,...values});
  const filtered=useMemo(()=>rows.filter(row=>(settings.scope==='all'||(settings.scope==='watch'?watch.has(row):row.code.endsWith('.'+settings.scope)))&&
    (!settings.query.trim()||`${row.code} ${row.name} ${theme(row)}`.toLowerCase().includes(settings.query.trim().toLowerCase())))
    .sort((a,b)=>compareStockRows(a,b,settings.sort,settings.ascending)),[rows,settings.scope,settings.query,settings.sort,settings.ascending,watch.items]);
  const identity=`${readingKey}:${settings.scope}:${settings.query}:${settings.sort}:${settings.ascending}`;
  const page=Math.min(settings.page,Math.max(0,Math.ceil(filtered.length/PAGE_SIZE)-1));
  const displayed=filtered.slice(page*PAGE_SIZE,(page+1)*PAGE_SIZE);
  const focusRef=useQuoteFocus('ranking',displayed.map(row=>row.code),focus);
  const pages=Math.max(1,Math.ceil(filtered.length/PAGE_SIZE));
  const title='个股涨幅榜';
  const heading=(key:SortKey,label:string)=><RankSortHeader label={label} active={settings.sort===key} ascending={settings.ascending}
    onSort={()=>update({sort:key,ascending:settings.sort===key?!settings.ascending:false})}/>;
  return <Panel title={title} subtitle={sourceLabel} className="ty-rank-panel" action={<div className="ty-rank-controls">
    <input aria-label={`${title}搜索`} placeholder="代码/题材" value={settings.query} onChange={e=>update({query:e.target.value})}/>
    <select aria-label={`${title}范围`} value={settings.scope} onChange={e=>update({scope:e.target.value})}>
      <option value="all">全市场</option><option value="SH">沪市</option><option value="SZ">深市</option><option value="BJ">北交所</option><option value="watch">自选</option>
    </select>
    <span className="ty-rank-count" title={`${sourceLabel} · 当前范围 ${filtered.length} 只 · 每页 ${PAGE_SIZE} 只`} aria-label={`${filtered.length}只股票，第${page+1}页，共${pages}页`}>{filtered.length}只</span>
    <button disabled={page===0} onClick={()=>update({page:page-1})} aria-label={`${title}上一页`} title={`第 ${page+1}/${pages} 页`}><svg width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="m8 2-4 4 4 4"/></svg></button>
    <button disabled={page+1>=pages} onClick={()=>update({page:page+1})} aria-label={`${title}下一页`} title={`第 ${page+1}/${pages} 页`}><svg width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="m4 2 4 4-4 4"/></svg></button>
  </div>}>
    <div ref={focusRef} className="ty-table-scroll" key={identity+':'+page} data-scroll-key={identity+':'+page}><table className="ty-table ty-rank-table" aria-label={title}><thead><tr>
      <th aria-label="排名"/>{heading('name','名称')}{heading('speed_5m','5分涨速')}{heading('pct_change',dailyClose?'收盘涨幅':'涨幅')}{heading('amount_cny','成交额')}{heading('ths_themes','同花顺题材')}
    </tr></thead><tbody>{displayed.map((row,index)=><tr key={row.code} data-current={row.code===selected||undefined} onClick={()=>onSelect(row.code)}>
      <td className={page===0&&index<3?'ty-gold':'ty-muted'}>{page*PAGE_SIZE+index+1}</td><td><StockName stock={row} onSelect={onSelect}/></td>
      <td title={dailyClose ? '收盘日线不含5分钟涨速' : row.speed_5m == null ? '缺少同源同交易时段的约5分钟前报价' : `两次报价相隔 ${row.speed_window_seconds ?? '—'} 秒`}><Change value={row.speed_5m}/></td><td><Change value={row.pct_change}/></td><td>{money(row.amount_cny,1)}{row.amount_cny!=null&&<small>亿</small>}</td>
      <td title={row.ths_themes_received_at?`同花顺概念成分 · 采集 ${dateTime(row.ths_themes_received_at)}`:'同花顺题材未覆盖'}>{theme(row)?<ReadableText className="ty-theme-text" text={theme(row)}/>:<span className="ty-theme-text ty-muted">—</span>}</td>
    </tr>)}</tbody></table>{!displayed.length&&<Empty title={rows.length?'无匹配股票':'等待个股行情'}/>}</div>
  </Panel>;
}
