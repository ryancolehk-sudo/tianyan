import { updateLocation } from '../../shared/navigation';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Change, Empty, StockName, Tag } from './Components';
import type { PageProps } from './Components';
import { dateTime, money, number } from './data';
import { compareStockRows, RankSortHeader } from './StockRanking';
import type { SortKey } from './StockRanking';
import { ReadableText } from '../../shared/ReadableText';
import { useWorkspaceState } from '../../shared/workspaceState';
import { SectorPicker } from './SectorPicker';
import { useQuoteFocus } from './useQuoteFocus';

export function SectorTracking({ data, sector, selected, trackingMembers, trackingMemberCount, trackingTruncated, trackingDay, sectorBusy, sectorError, retrySector, selectStock, selectSector, session, asOfTime, compact = false }: PageProps & {compact?: boolean}) {
  const [limit, setLimit] = useState(50);
  const scroll = useRef<HTMLDivElement>(null);
  const usableDay = !!trackingDay && trackingDay <= data.trade_day;
  const dailyClose = data.breadth.basis === 'daily_close';
  const [chosenSort,setSort] = useWorkspaceState<{key:SortKey;ascending:boolean}|null>(`${session.data.user.id}:tracking-sort:${data.trade_day}:${asOfTime}`, null);
  const sort = chosenSort ?? {key:dailyClose?'pct_change':'speed_5m',ascending:false};
  const ready = trackingMembers !== undefined && usableDay;
  const rows = useMemo(() => {
    if (!usableDay) return [];
    const quotes = new Map(data.stocks.map(row=>[row.code,row]));
    return (trackingMembers ?? []).map(row=>({ ...row, amount_cny:quotes.get(row.code)?.amount_cny??null,
      ths_themes:quotes.get(row.code)?.ths_themes??[], ths_themes_received_at:quotes.get(row.code)?.ths_themes_received_at??null }))
      .sort((a,b)=>compareStockRows(a,b,sort.key,sort.ascending));
  }, [trackingMembers, usableDay, data.stocks, sort.key, sort.ascending]);
  const focusRef=useQuoteFocus('sector',rows.slice(0,Math.min(limit,50)).map(row=>row.code),{
    session,day:data.trade_day,cutoff:asOfTime,enabled:data.capabilities.some(item=>item.key==='focused_quotes'&&item.status==='available')});
  const heading=(key:SortKey,label:string)=><RankSortHeader label={label} active={sort.key===key} ascending={sort.ascending}
    onSort={()=>setSort({key,ascending:sort.key===key?!sort.ascending:false})}/>;
  const speedCount = rows.filter(row => row.speed_5m != null && Number.isFinite(row.speed_5m)).length;
  const membership = <>成分 {ready ? number(rows.length) : '—'} 只{trackingTruncated ? ' · 未齐' : ''}{ready && trackingDay !== data.trade_day ? ` · 成员日 ${trackingDay}` : ''}</>;
  useEffect(() => { setLimit(50); if (scroll.current) scroll.current.scrollTop = 0; }, [sector?.code, data.trade_day, asOfTime, sort.key, sort.ascending]);
  return <section ref={focusRef} id="ty-sector-tracking" className="ty-panel ty-sector-tracking" aria-labelledby="ty-sector-tracking-title" aria-busy={!!sectorBusy}>
    <header className="ty-panel-head ty-sector-tracking-head"><h3 id="ty-sector-tracking-title">板块追踪</h3>{compact && <SectorPicker items={data.sectors} current={sector} onSelect={selectSector}/>}<a className="ty-link" href={(() => { const url = new URL(location.href); url.searchParams.set("workspace", "research"); url.hash = ""; return url.pathname + url.search; })()} onClick={event => { event.preventDefault(); updateLocation(new URL(event.currentTarget.href)); }}>板块研究</a></header>
    {!compact && <div className="ty-ai-subject"><strong>{sector?.name || '选择一个板块'}</strong><Tag gold>{dailyClose ? '收盘涨幅' : '近5分钟'}</Tag></div>}
    {sector && <p className="ty-sector-tracking-summary" title={`行情日 ${data.trade_day} · 成员日 ${ready ? trackingDay : "—"} · 成分 ${ready ? rows.length : "—"} 只 · 涨速可用 ${ready ? speedCount : "—"} 只`}>{compact ? membership : <>行情日 {data.trade_day} · 成员日 {ready ? trackingDay : '—'}{ready && trackingDay !== data.trade_day ? '（最近已存成分，非当日完整目录）' : ''} · {trackingTruncated && ready ? `已载入 ${number(rows.length)}/${number(trackingMemberCount)} 只` : `成分 ${ready ? number(rows.length) : '—'} 只`} · 涨速可用 {ready ? number(speedCount) : '—'} 只</>}</p>}
    {sectorBusy && sector && <div role="status"><Empty compact title={`正在读取${sector.name}成分股`} detail={compact ? undefined : '已同步所选板块，正在准备个股列表'}/></div>}
    {sectorError && sector && <div className="ty-sector-tracking-state" role="status"><Empty compact title={`${sector.name}成分股暂未读取成功`} detail={sectorError}/>{retrySector && <button type="button" className="ty-text-button" onClick={retrySector}>重试板块</button>}</div>}
    {sector && !!rows.length ? <>
      <div className="ty-table-scroll" ref={scroll} tabIndex={0} role="region" aria-label="板块个股涨速排行">
        <table className="ty-table ty-rank-table ty-tracking-table" aria-label="板块追踪个股榜"><thead><tr><th aria-label="排名"/>{heading('name','名称')}{heading('speed_5m','5分涨速')}{heading('pct_change',dailyClose?'收盘涨幅':'涨幅')}{heading('amount_cny','成交额')}{heading('ths_themes','同花顺题材')}</tr></thead>
          <tbody>{rows.slice(0, limit).map((row,index) => <tr key={row.code} data-current={row.code === selected?.code || undefined} onClick={() => selectStock(row.code)}>
            <td className={index<3?'ty-gold':'ty-muted'}>{index+1}</td><td><StockName stock={row} onSelect={selectStock}/></td>
            <td title={dailyClose?'收盘日线不含5分钟涨速':row.speed_5m==null?'缺少同源同交易时段的约5分钟前报价':`两次报价相隔 ${row.speed_window_seconds??'—'} 秒`}><Change value={row.speed_5m}/></td><td><Change value={row.pct_change}/></td>
            <td>{money(row.amount_cny,1)}{row.amount_cny!=null&&<small>亿</small>}</td>
            <td title={row.ths_themes_received_at?`同花顺概念成分 · 采集 ${dateTime(row.ths_themes_received_at)}`:'同花顺题材未覆盖'}>{row.ths_themes.length?<ReadableText className="ty-theme-text" text={row.ths_themes.join('、')}/>:<span className="ty-theme-text ty-muted">—</span>}</td>
          </tr>)}</tbody>
        </table>
      </div>
      {(!compact || dailyClose || limit < rows.length) && <div className="ty-sector-tracking-footer">{(!compact || dailyClose) && <span>{dailyClose ? '日线不含5分钟涨速' : '缺测排在末尾'}{trackingTruncated ? ' · 部分成分未显示' : ''}</span>}{limit < rows.length && <button type="button" className="ty-text-button" onClick={() => setLimit(value => value + 50)}>再显示 {Math.min(50, rows.length - limit)} 只</button>}</div>}
    </> : !sectorBusy && !sectorError ? <Empty compact title={sector ? '暂无可用板块成分' : '选择追踪板块'} detail={sector ? '所选日期尚无可核成分' : undefined}/> : null}
  </section>;
}
