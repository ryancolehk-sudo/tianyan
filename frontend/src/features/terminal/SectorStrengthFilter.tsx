import { useWorkspaceState } from '../../shared/workspaceState';
import type { SectorStrength } from './types';
import { rankStrength, maxFundComparisons } from './strengthSelection';
import { dateTime } from './data';

export function SectorStrengthFilter({rows, checked, defaultMode, onToggle, onReset, stateKey, source = 'eastmoney'}: {
  rows:SectorStrength[]; checked:string[]; defaultMode:boolean; onToggle:(code:string)=>void; onReset:()=>void; stateKey:string; source?:'ths'|'eastmoney';
}) {
  const [query,setQuery]=useWorkspaceState(stateKey+':search','');
  const sorted=rankStrength(rows).filter(row => row.name.includes(query.trim()) || row.code.includes(query.trim()));
  return <section className="ty-strength-filter" aria-label="板块强度筛选">
    <div className="ty-strength-heading"><h3>板块强度</h3><small>涨停家数 ↓</small></div>
    <input className="ty-strength-search" aria-label="筛选强度板块" placeholder="搜索板块" value={query} onChange={e=>setQuery(e.target.value)}/>
    <div className="ty-strength-list ty-table-scroll" data-scroll-key={stateKey} tabIndex={0} aria-label="按涨停家数排序的板块">
      {sorted.map(row => <label key={row.code} className="ty-strength-row" data-checked={checked.includes(row.code)||undefined}
        title={`${row.name} · ${row.code}\n行情日 ${row.trade_day} · 成员日 ${row.members_day||'缺失'}\n有效行情 ${row.quoted_count}/${row.member_count??'—'}只${row.partial?' · 部分覆盖':''}\n${row.basis==='daily_close'?'收盘日线':`行情时段 ${dateTime(row.source_time)} — ${dateTime(row.latest_source_time)}`}\n按价格等于实际涨停价统计，非盘口封单确认；非开盘啦强度分。`}>
        <input type="checkbox" aria-label={`对比${row.name}资金`} checked={checked.includes(row.code)}
          disabled={!checked.includes(row.code)&&checked.length>=maxFundComparisons} onChange={()=>onToggle(row.code)}/>
        <span>{row.name}</span><b className={row.limit_up_count ? 'ty-up' : 'ty-muted'}>{row.limit_up_count==null?'—':`${row.limit_up_count}涨停`}</b>
        <small>{row.members_day==null?'缺成员':row.partial?'部分覆盖':row.basis==='daily_close'?'收盘':'已观测'}</small>
      </label>)}
      {!sorted.length&&<p className="ty-panel-caption">{query?'没有匹配板块':'暂无可统计板块'}</p>}
    </div>
    <div className="ty-strength-footer"><span>{defaultMode?'默认资金前十':`已选 ${checked.length}/${maxFundComparisons}`}</span><button type="button" className="ty-link" onClick={onReset}>恢复资金前十</button></div>
    {!defaultMode&&checked.length>=maxFundComparisons&&<p className="ty-panel-caption">已选满10个，取消一个后可继续添加。</p>}
    <details><summary>统计口径</summary><p>{source === 'ths' ? '同花顺板块成员映射尚缺，涨停家数显示 —，不借东财同名板块填入。' : <>现有东财成员与当日已存行情逐股匹配，涨停价相等才计数，不用涨幅阈值或曾触板代替。仅覆盖已存成员和有效行情，部分覆盖不代表完整板块；缺资料显示 —。悬停各行查看日期与覆盖数，题材间可能重叠。此项不是开盘啦强度分。</>}</p></details>
  </section>;
}
