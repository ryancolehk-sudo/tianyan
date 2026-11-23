import { useCallback, useState } from 'react';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { Empty, Segmented } from '../../shared/ui';
import { Amount, DatasetMeta, DatasetRaw, DatasetStatus, StockCell, TradingTable, Value, numeric, rowDay, useTradingDataset } from '../workbench/TradingWidgets';
import { useTradingContext } from '../workbench/TradingContext';
import type { TradingColumn, TradingRow } from '../workbench/TradingWidgets';
import { SavedTrend } from '../workbench/SavedTrend';

export function FundsReference() {
  const [tab, setTab] = useState('个股');
  const [source, setSource] = useState('优先东方财富');
  const [direction, setDirection] = useState('净流入');
  const eastmoney = useTradingDataset(`market.eastmoney.${tab === '个股' ? 'stock' : tab === '行业' ? 'industry' : 'concept'}_funds`, 'eastmoney_funds');
  const mairui = useTradingDataset(`market.mairui.${tab === '个股' ? 'stock_flow' : 'industry_flow'}`, 'mairui_funds');
  const canFallback = tab !== '概念' && source !== '仅东方财富';
  const read = source === '麦蕊盘后' && canFallback ? mairui : eastmoney.record && (!eastmoney.refreshError || !mairui.record) || !canFallback ? eastmoney : mairui;
  const record = read.record;
  const free = record?.family === 'mairui_funds';
  const net = free ? 'jlr' : 'net_inflow_cny';
  const rows = (record?.payload.rows ?? []).filter(row => direction === '全部' || numeric(row[net]) !== null && (direction === '净流入' ? numeric(row[net])! > 0 : numeric(row[net])! < 0));
  const columns: TradingColumn[] = [
    tab === '个股' ? {key:'symbol',label:'股票',type:'stock'} : {key:'name',label:tab,type:'text'},
    {key:free ? 'zdf' : 'change_percent',label:'涨幅',type:'percent'},
    {key:net,label:'净流入',type:'signedAmount'},
    {key:free ? 'lrzj' : 'inflow_cny',label:'流入',type:'amount'},
    {key:free ? 'lczj' : 'outflow_cny',label:'流出',type:'amount'},
  ];
  const reload = () => { eastmoney.reload(); mairui.reload(); };
  const busy = source === '麦蕊盘后' && tab !== '概念' ? mairui.busy : eastmoney.busy || !eastmoney.record && canFallback && mairui.busy;
  if (tab === '当前股') return <section className="trading-widget" aria-label="资金流向排行"><div className="trade-toolbar"><Segmented label="资金分类" options={['个股','行业','概念','当前股']} value={tab} onChange={setTab}/></div><CurrentStockFunds/></section>;
  return <section className="trading-widget" aria-label="资金流向排行"><div className="trade-toolbar"><Segmented label="资金分类" options={['个股','行业','概念','当前股']} value={tab} onChange={value => { setTab(value); if (value === '概念' && source === '麦蕊盘后') setSource('优先东方财富'); }}/><button type="button" disabled={busy} onClick={reload}>刷新</button></div><div className="trade-toolbar"><label className="sr-only" htmlFor="funds-source">资金来源</label><select id="funds-source" value={source} onChange={event => setSource(event.target.value)}><option>优先东方财富</option><option>仅东方财富</option>{tab !== '概念' && <option>麦蕊盘后</option>}</select><Segmented label="资金方向" options={['净流入','净流出','全部']} value={direction} onChange={setDirection}/></div><DatasetMeta record={record} kind={free ? '盘后资金参考' : '供应商资金快照'} warning={read.warning}/>{free && source !== '麦蕊盘后' && <p className="trade-meta">东方财富暂无可用记录，显示麦蕊盘后参考；两家资金口径不合并。</p>}{(eastmoney.error || eastmoney.refreshError) && free && <p className="trade-warning" role="status">东方财富读取失败，当前显示麦蕊缓存。</p>}<DatasetStatus busy={!!busy} error={!record ? eastmoney.error ?? read.error : null} empty={!record} retry={reload}>{record && <><TradingTable key={read.requestKey + direction} record={record} rows={rows} columns={columns} label="资金流向排名" initialSort={net} initialDescending={direction !== '净流出'}/><DatasetRaw record={record}/></>}</DatasetStatus><details className="trade-details"><summary>资金统计说明</summary><p>仅对当前来源、所选日期的返回集合排序。供应商资金流不等同于识别主力交易者；未接入来源、未核单位及缺失值保持未知。净流入大于零与小于零分别为流入、流出，零值可在“全部”查看。</p></details></section>;
}

function CurrentStockFunds() {
  const {instrumentId,day} = useTradingContext();
  const [view,setView] = useState('当日明细');
  const identityRead = useRead(useCallback(async () => ({instrumentId,response:instrumentId ? await api.marketQuotes(instrumentId) : null}),[instrumentId]));
  const identity = identityRead.data?.instrumentId === instrumentId && !identityRead.error ? identityRead.data?.response?.data.items.find(item => item.instrument.id === instrumentId)?.instrument : undefined;
  const read = useTradingDataset('tushare.moneyflow','tushare_moneyflow',instrumentId,undefined,view === '已存历史',!!instrumentId);
  const record = instrumentId ? read.record : undefined;
  const code = identity ? `${identity.symbol}.${({SSE:'SH',SZSE:'SZ',BSE:'BJ'} as const)[identity.exchange]}` : '';
  const cutoff = day || read.data?.trade_day;
  const rows = (record?.payload.rows ?? []).filter(row => row.ts_code === code && (!cutoff || !!rowDay(row) && rowDay(row) <= cutoff));
  const columns:TradingColumn[] = [{key:'trade_date',label:'交易日',type:'date'},{key:'net_mf_amount',label:'净流入',type:'signedAmount'},
    {key:'buy_sm_amount',label:'小单流入',type:'amount'},{key:'sell_sm_amount',label:'小单流出',type:'amount'},
    {key:'buy_md_amount',label:'中单流入',type:'amount'},{key:'sell_md_amount',label:'中单流出',type:'amount'},
    {key:'buy_lg_amount',label:'大单流入',type:'amount'},{key:'sell_lg_amount',label:'大单流出',type:'amount'},
    {key:'buy_elg_amount',label:'特大单流入',type:'amount'},{key:'sell_elg_amount',label:'特大单流出',type:'amount'}];
  return <><div className="trade-toolbar"><Segmented label="当前股资金视图" options={['当日明细','已存历史']} value={view} onChange={setView}/><button onClick={read.reload} disabled={read.busy}>刷新</button></div>{!instrumentId ? <Empty title="先选择一只股票">点击首页股票名单或搜索标的，查看该股盘后资金及已存历史。</Empty> : identityRead.busy && !identity ? <p className="trade-meta" role="status">正在核对所选股票…</p> : identityRead.error ? <p className="trade-warning" role="status">股票身份读取失败。<button onClick={identityRead.reload}>重试</button></p> : !identity || identity.asset_type !== 'stock' ? <Empty title="请选择股票">当前股资金不借用同代码指数或其他股票资料。</Empty> : <><p className="trade-meta">{identity.name} · {code} · 所选股盘后资金，不是全市场排行</p><DatasetMeta record={record} warning={read.warning}/>{view === '已存历史' && <p className="trade-meta">当前已存表内 {rows.length} 个记录；截至 {cutoff || '最近已存数据日'}，不代表完整历史。</p>}<DatasetStatus busy={read.busy} error={read.error} empty={!record || !rows.length} retry={read.reload}>{record && <><div>{view === '已存历史' && <SavedTrend record={record} rows={rows} title="当前股资金" fields={[{key:'net_mf_amount',label:'净流入'}]}/>}</div><TradingTable key={read.requestKey} record={record} rows={rows} columns={columns} label={view === '已存历史' ? '当前股资金已存历史' : '当前股资金明细'} initialSort="trade_date"/></>}</DatasetStatus>{record && <DatasetRaw record={record}/>}</>}<details className="trade-details"><summary>当前股资金口径</summary><p>只读取当前股票与所选日期对应的授权缓存；当日明细按实际交易日过滤，历史仅展示当前原表真实保存且不晚于所选日期的记录。金额按字段声明单位换算，单位未核时保留原值；分单统计不直接视为主力身份。</p></details></>;
}

export function upStatText(value: string | null | undefined) {
  const match = value?.match(/^(\d+)\/(\d+)$/);
  return match ? `${Number(match[2])}天${Number(match[1])}板` : value ?? '—';
}
function sealTime(value: string | null | undefined) {
  return value && /^\d{6}$/.test(value) ? value.replace(/^(\d{2})(\d{2})(\d{2})$/,'$1:$2:$3') : value ?? '—';
}

export function PoolsReference() {
  const [tab,setTab] = useState('涨停');
  const [query,setQuery] = useState('');
  const paid = useTradingDataset('tushare.limit_list_d','tushare_limit_list_d');
  const free = useTradingDataset(`market.mairui.${tab === '涨停' ? 'limit_up' : tab === '炸板' ? 'broken_limit' : 'limit_down'}`,'mairui_pools');
  const read = paid.record && (!paid.refreshError || !free.record) ? paid : free;
  const record = read.record;
  const tushare = record?.family === 'tushare_limit_list_d';
  const rows = (record?.payload.rows ?? []).filter(row => (!tushare || row.limit === (tab === '涨停' ? 'U' : tab === '炸板' ? 'Z' : 'D')) && (!query.trim() || [row.name,row.symbol,row.ts_code,row.industry].some(value => value?.includes(query.trim()))));
  const level = (row: TradingRow) => numeric(row[tushare ? 'limit_times' : 'lbc']);
  const groups = tab === '涨停' ? [...new Set(rows.map(row => level(row)))].sort((a,b) => (b ?? -1) - (a ?? -1)) : [null];
  const refresh = () => { paid.reload(); free.reload(); };
  const busy = paid.busy || !paid.record && free.busy;
  return <section className="trading-widget" aria-label="涨跌停股池参考"><div className="trade-toolbar"><Segmented label="股池分类" options={['涨停','炸板','跌停']} value={tab} onChange={setTab}/><button disabled={busy} onClick={refresh}>刷新</button></div><DatasetMeta record={record} kind={tushare ? '盘后日榜' : '供应商股池参考 · 更新时间未知'} warning={read.warning}/>{(paid.error || paid.refreshError) && read === free && free.record && <p className="trade-warning">Tushare读取失败，显示麦蕊参考。</p>}<div className="trade-toolbar"><input type="search" placeholder="股票 / 行业" aria-label="股池查找" value={query} onChange={event => setQuery(event.target.value)} maxLength={80}/><span>{rows.length} 只 · 返回集合</span></div><DatasetStatus busy={!!busy} error={!record ? paid.error ?? free.error : null} empty={!record} retry={refresh}>{record && <>{!rows.length ? <Empty title="当前分类没有匹配记录">不代表全市场数量为零。</Empty> : <div className="trade-core-table trade-table-scroll" tabIndex={0} role="region" aria-label="连板梯队与封板明细">{groups.map(group => {
    const members = tab === '涨停' ? rows.filter(row => level(row) === group) : rows;
    return <div key={String(group)}>{tab === '涨停' && <div className="trade-ladder-heading"><strong>{group === null ? '板数未知' : group === 1 ? '首板' : `${group}连板`}{!tushare && ' · 供应商口径'}</strong><small>{members.length} 只</small></div>}<table className="trade-table"><thead><tr><th className="trade-left">股票</th><th>涨幅</th><th>首封 / 末封</th><th>开板次数</th><th>封板资金</th><th className="trade-left">{tab === '炸板' ? '炸板说明' : '涨停统计'}</th></tr></thead><tbody>{[...members].sort((a,b) => (a[tushare ? 'first_time' : 'first_limit_raw'] ?? '999999').localeCompare(b[tushare ? 'first_time' : 'first_limit_raw'] ?? '999999')).map((row,index) => <tr key={index}><td className="trade-left"><StockCell row={row}/><small>{row.industry ?? '行业未提供'}</small></td><td><Value value={row[tushare ? 'pct_chg' : 'zf']} signed suffix="%"/></td><td>{sealTime(row[tushare ? 'first_time' : 'first_limit_raw'])}<small>{sealTime(row[tushare ? 'last_time' : 'last_limit_raw'])}</small></td><td><Value value={row[tushare ? 'open_times' : 'zbc']}/></td><td><Amount record={record} field={tushare ? 'fd_amount' : 'zj'} value={row[tushare ? 'fd_amount' : 'zj']}/></td><td className="trade-left">{tab === '炸板' ? '原因未提供' : tushare ? upStatText(row.up_stat) : row.limit_statistic ?? '—'}</td></tr>)}</tbody></table></div>;
  })}</div>}<DatasetRaw record={record}/></>}</DatasetStatus><details className="trade-details"><summary>连板与股池口径</summary><p>Tushare连续封板使用limit_times；up_stat的N/T显示为T天N板，两者不混用。麦蕊板数保留供应商口径。封板时间仅为原始时分秒；炸板次数不等于炸板原因，盘后榜单不标盘中实时。</p></details></section>;
}
