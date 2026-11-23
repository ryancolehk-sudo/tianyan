import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { DatasetItem } from '../../generated/schema';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { Empty, ErrorState } from '../../shared/ui';
import { sourceTime } from '../data/SourceRuntime';
import { StockAction, useTradingContext } from './TradingContext';
import type { ReferenceWidgetDefinition } from './widgetCatalog';
import { SavedTrend } from './SavedTrend';
import './tradingWidgets.css';
export { ResearchSummary } from './ResearchSummary';

export type TradingRow = Record<string, string | null>;
export const numeric = (value: string | null | undefined) => value !== null && value !== undefined && /^-?\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value)) ? Number(value) : null;
export const tradeDate = (value?: string | null) => value && /^\d{8}$/.test(value) ? `${value.slice(0,4)}-${value.slice(4,6)}-${value.slice(6)}` : value ?? '';
export const stockCode = (row: TradingRow) => row.ts_code ?? row.symbol ?? row.code ?? '';
export const rowDay = (row: TradingRow) => tradeDate(row.trade_date ?? row.trade_day ?? row.date ?? row.updated_raw?.slice(0,10));
const originals = new WeakMap<DatasetItem, DatasetItem>();
export function projectTradingDay(item: DatasetItem, selectedDay?: string | null) {
  const target = item.trade_day ?? selectedDay;
  const rows = item.view_rows ?? item.payload.rows;
  const dated = target && rows.some(row => rowDay(row));
  if (item.view_rows == null && !dated) return item;
  const projected = {...item,payload:{...item.payload,rows:dated ? rows.filter(row => rowDay(row) === target) : rows}};
  originals.set(projected,item);
  return projected;
}
export function signedText(value: string | null | undefined, suffix = '') {
  const number = numeric(value);
  return number === null ? '—' : `${number > 0 ? '+' : ''}${preciseDecimal(value!)}${suffix}`;
}
function preciseDecimal(value:string) {
  const [integer,fraction] = value.split('.');
  return integer.replace(/\B(?=(\d{3})+(?!\d))/g,',') + (fraction === undefined ? '' : '.' + fraction);
}
export function Value({ value, signed = false, suffix = '' }: { value?: string | null; signed?: boolean; suffix?: string }) {
  const number = numeric(value);
  return <span className={signed && number !== null ? number > 0 ? 'trade-up' : number < 0 ? 'trade-down' : '' : ''}>{signed ? signedText(value, suffix) : number === null ? value || '—' : preciseDecimal(value!) + suffix}</span>;
}
export function amountText(record: DatasetItem, key: string, value: string | null | undefined, signed = false) {
  const number = numeric(value);
  if (number === null) return '—';
  const unit = record.payload.columns.find(column => column.key === key)?.unit;
  const multiplier = unit === '元' || unit === 'CNY' ? 1 : unit === '千元' ? 1_000 : unit === '万元' ? 10_000 : unit === '亿元' ? 100_000_000 : null;
  const prefix = signed && number > 0 ? '+' : '';
  if (multiplier === null) return prefix + preciseDecimal(value!) + (unit ? ` ${unit}` : '（原单位未核）');
  if (multiplier === 1 && Math.abs(number) < 10_000) return prefix + preciseDecimal(value!) + '元';
  const cny = number * multiplier;
  const scale = Math.abs(cny) >= 100_000_000 ? 100_000_000 : Math.abs(cny) >= 10_000 ? 10_000 : 1;
  return prefix + (cny / scale).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + (scale === 100_000_000 ? '亿' : scale === 10_000 ? '万' : '元');
}
export function Amount({ record, field, value, signed = false }: { record: DatasetItem; field: string; value?: string | null; signed?: boolean }) {
  const number = numeric(value);
  return <span className={signed && number !== null ? number > 0 ? 'trade-up' : number < 0 ? 'trade-down' : '' : ''} title={`${value ?? '未知'} ${record.payload.columns.find(column => column.key === field)?.unit ?? '原单位未核'}`}>{amountText(record, field, value, signed)}</span>;
}
export function StockCell({ row }: { row: TradingRow }) {
  const code = stockCode(row);
  return <span className="trade-stock"><span className="trade-stock-actions"><StockAction code={code} name={row.name ?? row.mc ?? code}/><StockAction code={code} name={row.name ?? row.mc ?? code} add><span aria-hidden="true">＋</span></StockAction></span><small>{code}</small></span>;
}
function IndexCell({ row }: { row: TradingRow }) {
  const {selectInstrument} = useTradingContext();
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  const code = stockCode(row);
  async function select() {
    const match = /^(\d{6})\.(SH|SZ)$/i.exec(code);
    if (!match) { setError('指数代码或市场未核实。'); return; }
    setBusy(true);setError('');
    try {
      const response = await api.instruments(match[1]);
      const rows = response.data.items.filter(item => item.symbol === match[1] && item.asset_type === 'index' && item.exchange === (match[2].toUpperCase() === 'SH' ? 'SSE' : 'SZSE'));
      if (rows.length !== 1) throw new Error('本机目录无法唯一识别该指数。');
      selectInstrument(rows[0].id);
    } catch(reason) { setError(reason instanceof Error ? reason.message : '指数读取失败。'); }
    finally {setBusy(false);}
  }
  return <span className="trade-stock"><button type="button" className="trading-stock-link" disabled={busy} onClick={() => void select()}>{row.name ?? code}</button><small>{code}</small>{error && <small role="status">{error}</small>}</span>;
}

// Every response remains tied to its request key. A selection change or refresh
// never reuses the previous stock, date or permission-checked table as success.
export function useTradingDataset(dataset: string, family?: string, instrument?: string, source?: string, savedHistory = false, enabled = true) {
  const { day, mode } = useTradingContext();
  const key = JSON.stringify([dataset, family, instrument, day, mode, source, savedHistory, enabled]);
  const read = useRead(useCallback(async () => ({ key, response: enabled ? await api.tradingDatasets(dataset, family, day || undefined, instrument, source) : null }), [key, dataset, family, day, instrument, source,enabled]), { retainOnTransientFailure: true });
  const sources = useRead(api.dataSources);
  useEffect(() => {
    if (mode !== 'live' || !enabled) return;
    const refresh = () => { if (document.visibilityState === 'visible') { read.reload(); sources.reload(); } };
    const timer = window.setInterval(refresh, 60_000);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, [mode, read.reload, sources.reload,enabled]);
  const data = !read.error && read.data?.key === key ? read.data.response?.data : undefined;
  const items = useMemo(() => (data?.items ?? []).filter(item => item.payload.dataset === dataset && (!family || item.family === family) && (!instrument || item.instrument_id === instrument)).map(item => savedHistory ? item : projectTradingDay(item,data?.trade_day ?? day)),[data,dataset,family,instrument,day,savedHistory]);
  const record = items[0];
  const status = record ? sources.data?.data.items.find(item => item.family === record.family) : undefined;
  const warning = read.refreshError ? '缓存更新失败 · 展示已存资料' : sources.error ? '采集状态读取失败' : status?.paused_reason ? '采集已暂停 · 展示已存数据' : status?.collection_enabled === false ? '自动采集关闭 · 展示已存数据' : status?.last_attempt && ['error', 'unavailable'].includes(status.last_attempt.status) ? '最近采集失败 · 展示已存数据' : '';
  return { ...read, data, items, record, warning, requestKey:key, reload: () => { read.reload(); sources.reload(); } };
}

export function DatasetMeta({ record, kind = '盘后资料', warning }: { record?: DatasetItem; kind?: string; warning?: string }) {
  const dates = record ? [...new Set(record.payload.rows.map(row => rowDay(row)).filter(Boolean))].sort() : [];
  const mode = record?.data_mode;
  if (mode === 'after_market') kind = '盘后资料 · 非实时';
  else if (mode === 'intraday') kind = '已存行情快照';
  return <><p className="trade-meta">{dates.length ? dates.length === 1 ? dates[0] : `${dates[0]}—${dates.at(-1)}` : record?.family === 'mairui_pools' ? `请求日 ${record.payload.observed_day ?? '未知'}` : record?.family === 'mairui_funds' ? `来源更新日 ${record.payload.observed_day ?? '未知'}` : '数据日期未知'} · {record?.source_name ?? '暂无来源'} · {kind}{record && <> · 接收 {sourceTime(record.received_at)}</>}</p>{warning && <p className="trade-warning" role="status">{warning}</p>}</>;
}
export function DatasetRaw({ record }: { record: DatasetItem }) {
  record = originals.get(record) ?? record;
  const [page, setPage] = useState(0);
  const count = Math.max(1, Math.ceil(record.payload.rows.length / 20));
  const current = Math.min(page, count - 1);
  return <details className="trade-details"><summary>原始数据与口径</summary><p>{record.payload.scope}</p><p>{record.payload.attribution}</p><div className="trade-table-scroll" tabIndex={0} role="region" aria-label={record.payload.title + '原始数据'}><table className="trade-table"><thead><tr>{record.payload.columns.map(column => <th key={column.key}>{column.label}<small>{column.unit ?? '原单位未核'}</small></th>)}</tr></thead><tbody>{record.payload.rows.slice(current * 20, (current + 1) * 20).map((row, index) => <tr key={index}>{record.payload.columns.map(column => <td key={column.key}>{row[column.key] ?? '—'}</td>)}</tr>)}</tbody></table></div><div className="trade-pager"><span>{record.payload.rows.length} 行 · {current + 1} / {count}</span><button disabled={!current} onClick={() => setPage(current - 1)}>上一页</button><button disabled={current + 1 >= count} onClick={() => setPage(current + 1)}>下一页</button></div><p>版本 {record.id} · {record.content_hash}</p><a href={record.payload.source_url} target="_blank" rel="noopener noreferrer">原始出处 ↗</a></details>;
}
export function DatasetStatus({ busy, error, empty, retry, children }: { busy: boolean; error: Error | null; empty: boolean; retry: () => void; children: ReactNode }) {
  return error ? <ErrorState error={error} retry={retry}/> : busy && empty ? <p className="trade-meta" role="status">正在读取…</p> : empty ? <Empty title="所选日期暂无已存资料">切换交易日，或到数据中心核对来源与权限；缺失数据保持未知。</Empty> : <>{busy && <p className="trade-meta" role="status">正在刷新已存资料…</p>}{children}</>;
}

export type TradingColumn = { key: string; label: string; type?: 'stock' | 'index' | 'amount' | 'signedAmount' | 'percent' | 'date' | 'text'; alternatives?: string[] };
export function TradingTable({ record, rows, columns, label, initialSort, initialDescending = true, limit = 20 }: { record: DatasetItem; rows: TradingRow[]; columns: TradingColumn[]; label: string; initialSort?: string; initialDescending?: boolean; limit?: number }) {
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState({ key: initialSort ?? '', descending: initialDescending });
  const [page, setPage] = useState(0);
  const field = (row: TradingRow, column: TradingColumn) => [column.key, ...(column.alternatives ?? []), ...(column.type === 'stock' ? ['ts_code','symbol','code'] : [])].find(key => row[key] !== null && row[key] !== undefined) ?? column.key;
  const filtered = rows.filter(row => !query.trim() || Object.values(row).some(value => value?.toLowerCase().includes(query.trim().toLowerCase())));
  const sorted = [...filtered].sort((a,b) => {
    if (!sort.key) return 0;
    const column = columns.find(item => item.key === sort.key)!;
    const av = a[field(a,column)], bv = b[field(b,column)];
    if (av == null || av === '') return bv == null ? 0 : 1;
    if (bv == null || bv === '') return -1;
    const an = numeric(av), bn = numeric(bv);
    return (an !== null && bn !== null ? an - bn : av.localeCompare(bv,'zh-CN')) * (sort.descending ? -1 : 1);
  });
  const pages = Math.max(1, Math.ceil(sorted.length / limit));
  const current = Math.min(page, pages - 1);
  return <><div className="trade-toolbar"><input type="search" value={query} onChange={event => { setQuery(event.target.value); setPage(0); }} placeholder="代码 / 名称 / 关键词" aria-label={label + '查找'} maxLength={80}/><span>{sorted.length} 条</span></div><div className="trade-table-scroll" tabIndex={0} role="region" aria-label={label}><table className="trade-table"><thead><tr>{columns.map(column => <th scope="col" key={column.key} className={column.type === 'stock' || column.type === 'index' || column.type === 'text' ? 'trade-left' : ''} aria-sort={sort.key === column.key ? sort.descending ? 'descending' : 'ascending' : 'none'}><button type="button" onClick={() => { setSort({ key: column.key, descending: sort.key === column.key ? !sort.descending : true }); setPage(0); }}>{column.label}{sort.key === column.key ? sort.descending ? ' ↓' : ' ↑' : ''}</button></th>)}</tr></thead><tbody>{sorted.slice(current * limit, (current + 1) * limit).map((row, index) => <tr key={`${stockCode(row)}:${rowDay(row)}:${index}`}>{columns.map(column => {
    const key = field(row,column), value = row[key];
    return <td key={column.key} className={column.type === 'stock' || column.type === 'index' || column.type === 'text' ? 'trade-left' : ''}>{column.type === 'stock' ? <StockCell row={row}/> : column.type === 'index' ? <IndexCell row={row}/> : column.type === 'amount' || column.type === 'signedAmount' ? <Amount record={record} field={key} value={value} signed={column.type === 'signedAmount'}/> : column.type === 'percent' ? <Value value={value} signed suffix="%"/> : column.type === 'date' ? tradeDate(value) || '—' : <Value value={value}/>}</td>;
  })}</tr>)}</tbody></table></div>{!sorted.length && <p className="trade-meta">没有匹配记录。</p>}<div className="trade-pager"><span>第 {current + 1} / {pages} 页</span><button disabled={!current} onClick={() => setPage(current - 1)}>上一页</button><button disabled={current + 1 >= pages} onClick={() => setPage(current + 1)}>下一页</button></div></>;
}

const financialFields: Record<string, [string,string][]> = {
  income: [['total_revenue','营业总收入'],['revenue','营业收入'],['n_income_attr_p','归母净利润'],['n_income','净利润'],['basic_eps','每股收益']],
  balancesheet: [['total_assets','总资产'],['total_liab','总负债'],['total_hldr_eqy_exc_min_int','股东权益']],
  cashflow: [['n_cashflow_act','经营现金流'],['n_cashflow_inv_act','投资现金流'],['n_cash_flows_fnc_act','筹资现金流'],['n_incr_cash_cash_equ','现金净增加']],
  'fina-indicator': [['eps','每股收益'],['bps','每股净资产'],['roe','净资产收益率'],['grossprofit_margin','毛利率'],['debt_to_assets','资产负债率']],
};
const stock: TradingColumn = { key:'ts_code', label:'股票', type:'stock' };
const date: TradingColumn = { key:'trade_date', label:'交易日', type:'date' };
const configs: Record<string, TradingColumn[]> = {
  'daily-basic':[date,{key:'close',label:'收盘价'},{key:'turnover_rate',label:'换手率',type:'percent'},{key:'pe_ttm',label:'市盈率 TTM',alternatives:['pe']},{key:'pb',label:'市净率'},{key:'total_mv',label:'总市值',type:'amount'}],
  moneyflow:[date,{key:'net_mf_amount',label:'净流入',type:'signedAmount'},{key:'buy_lg_amount',label:'大单流入',type:'amount'},{key:'sell_lg_amount',label:'大单流出',type:'amount'},{key:'buy_elg_amount',label:'特大单流入',type:'amount'},{key:'sell_elg_amount',label:'特大单流出',type:'amount'}],
  'market-limits':[stock,{key:'pre_close',label:'昨收'},{key:'up_limit',label:'涨停价'},{key:'down_limit',label:'跌停价'},date],
  daily:[date,{key:'open',label:'开盘'},{key:'high',label:'最高'},{key:'low',label:'最低'},{key:'close',label:'收盘'},{key:'amount',label:'成交额',type:'amount'}],
  'index-daily':[date,{key:'open',label:'开盘'},{key:'high',label:'最高'},{key:'low',label:'最低'},{key:'close',label:'收盘'},{key:'amount',label:'成交额',type:'amount'}],
  'adj-factor':[date,{key:'adj_factor',label:'复权因子'}],
  'block-trade':[stock,date,{key:'price',label:'成交价'},{key:'amount',label:'成交额',type:'amount'},{key:'buyer',label:'买方',type:'text'},{key:'seller',label:'卖方',type:'text'}],
  'margin-detail':[stock,date,{key:'rzye',label:'融资余额',type:'amount'},{key:'rzmre',label:'融资买入',type:'amount'},{key:'rqye',label:'融券余额',type:'amount'}],
  'ths-index':[{key:'name',label:'概念名称',type:'text'},{key:'ts_code',label:'代码',type:'text'},{key:'count',label:'成分数量'},{key:'type',label:'分类',type:'text'},{key:'list_date',label:'上市日',type:'date'}],
  shibor:[{key:'date',label:'数据日',type:'date'},{key:'on',label:'隔夜（%）'},{key:'field_1w',label:'1周（%）'},{key:'field_1m',label:'1月（%）'},{key:'field_3m',label:'3月（%）'}],
  cpi:[{key:'month',label:'统计月份',type:'text'},{key:'nt_yoy',label:'全国同比',type:'percent'},{key:'nt_mom',label:'全国环比',type:'percent'},{key:'town_yoy',label:'城市同比',type:'percent'},{key:'cnt_yoy',label:'农村同比',type:'percent'}],
  auction:[stock,{key:'price',label:'竞价成交价'},{key:'amount',label:'成交额',type:'amount'},{key:'vol',label:'成交量'},{key:'turnover_rate',label:'换手率',type:'percent'},date],
  'index-basic-sse':[{key:'name',label:'指数',type:'index'},{key:'publisher',label:'发布机构',type:'text'},{key:'list_date',label:'发布日期',type:'date'},{key:'base_point',label:'基点'}],
  'index-basic-szse':[{key:'name',label:'指数',type:'index'},{key:'publisher',label:'发布机构',type:'text'},{key:'list_date',label:'发布日期',type:'date'},{key:'base_point',label:'基点'}],
  'stk-limit':[date,{key:'pre_close',label:'昨收'},{key:'up_limit',label:'涨停价'},{key:'down_limit',label:'跌停价'}],
};

export function TradingDatasetView({ definition, record, rows }: { definition: ReferenceWidgetDefinition; record: DatasetItem; rows: TradingRow[] }) {
  if (financialFields[definition.id]) {
    const periods = [...rows].sort((a,b) => (b.end_date ?? '').localeCompare(a.end_date ?? '')).slice(0,4);
    return <div className="trade-table-scroll" tabIndex={0} role="region" aria-label={definition.title + '报告期对比'}><table className="trade-table trade-financial"><thead><tr><th className="trade-left">指标</th>{periods.map((row,index) => <th key={index}>{tradeDate(row.end_date)}<small>公告 {tradeDate(row.f_ann_date ?? row.ann_date) || '未知'}{row.update_flag === '1' ? ' · 已修订' : ''}</small></th>)}</tr></thead><tbody>{financialFields[definition.id].filter(([key]) => record.payload.columns.some(column => column.key === key)).map(([key,label]) => <tr key={key}><th className="trade-left">{label}<small>{record.payload.columns.find(column => column.key === key)?.unit ?? '原单位未核'}</small></th>{periods.map((row,index) => <td key={index}><Value value={row[key]}/></td>)}</tr>)}</tbody></table></div>;
  }
  if (definition.id === 'dividend') return <div className="trade-events">{rows.map((row,index) => <article key={index}><div><strong>{row.div_proc ?? '进度未提供'}</strong><time>公告 {tradeDate(row.ann_date) || '未知'}</time></div><dl><div><dt>每股税前现金</dt><dd><Value value={row.cash_div_tax}/></dd></div><div><dt>每股送转</dt><dd><Value value={row.stk_div}/></dd></div></dl><p>登记计划 {tradeDate(row.record_date) || '—'} · 除权计划 {tradeDate(row.ex_date) || '—'} · 派息计划 {tradeDate(row.pay_date) || '—'}</p></article>)}</div>;
  const columns = configs[definition.id] ?? [];
  const fields = definition.id === 'shibor' ? [{key:'on',label:'隔夜'},{key:'field_1w',label:'1周'},{key:'field_1m',label:'1月'},{key:'field_3m',label:'3月'}] : definition.id === 'cpi' ? [{key:'nt_yoy',label:'全国同比'},{key:'nt_mom',label:'全国环比'}] : definition.id === 'margin-detail' && new Set(rows.map(row=>row.ts_code)).size===1 ? [{key:'rzye',label:'融资余额'},{key:'rqye',label:'融券余额'}] : null;
  if (fields) return <><SavedTrend record={record} rows={rows} title={definition.title} dateField={definition.id==='cpi'?'month':definition.id==='shibor'?'date':'trade_date'} fields={fields}/><TradingTable record={record} rows={rows} columns={columns} label={definition.title} initialSort={columns[0].key}/></>;
  return <TradingTable key={definition.id} record={record} rows={rows} columns={columns} label={definition.title} initialSort={definition.id === 'auction' ? 'amount' : columns.some(column => column.key === 'trade_date') ? 'trade_date' : undefined}/>;
}

export const listingReason = (row: TradingRow) => row.reason ?? '上榜原因未提供';
export function reasonPeriod(row: TradingRow) {
  const reason = listingReason(row);
  if (/连续|三个交易日|三日|3个交易日|3日|多日/.test(reason)) return '多日';
  if (/当日|日涨幅|日跌幅|日价格|日收盘|日振幅|日换手率|单日/.test(reason)) return '单日';
  return '周期待核';
}
export function groupLeaderboard(rows: TradingRow[]) {
  const groups = new Map<string, TradingRow[]>();
  rows.forEach(row => { const key = `${rowDay(row)}:${row.ts_code}`; groups.set(key,[...(groups.get(key) ?? []), row]); });
  return [...groups.values()].map(reasons => ({ reasons, primary: reasons.find(row => reasonPeriod(row) === '单日') ?? reasons[0] })).sort((a,b) => (numeric(b.primary.net_amount) ?? -Infinity) - (numeric(a.primary.net_amount) ?? -Infinity));
}

function SeatDrawer({ record, reasons, close }: { record: DatasetItem; reasons: TradingRow[]; close: () => void }) {
  const [selected, setSelected] = useState(() => Math.max(0,reasons.findIndex(row => reasonPeriod(row) === '单日')));
  const row = reasons[Math.min(selected,reasons.length - 1)];
  const { instrumentId } = useTradingContext();
  const seats = useTradingDataset('tushare.top_inst','tushare_top_inst',undefined);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const opener = document.activeElement; dialog.current?.showModal(); return () => { if (opener instanceof HTMLElement && opener.isConnected) opener.focus({ preventScroll:true }); }; },[]);
  const matched = seats.items.flatMap(item => item.payload.rows.map(value => ({ record:item,row:value }))).filter(item => item.row.ts_code === row.ts_code && rowDay(item.row) === rowDay(row));
  const reasonMatched = matched.filter(item => !!row.reason && item.row.reason === row.reason);
  const exact = reasonMatched.length > 0;
  const sideName = (value:string | null | undefined) => value === '0' || value === '买入' || value?.toLowerCase() === 'buy' ? '买入' : value === '1' || value === '卖出' || value?.toLowerCase() === 'sell' ? '卖出' : '方向未核';
  const sides = ['买入','卖出',...((exact ? reasonMatched : matched).some(item => sideName(item.row.side) === '方向未核') ? ['方向未核'] : [])];
  return <dialog ref={dialog} className="trade-drawer" aria-label="龙虎榜买卖席位" onCancel={() => close()} onClose={close}><header><div><strong>{row.name ?? row.ts_code} · 龙虎榜</strong><small>{rowDay(row)} · {reasonPeriod(row)}</small></div><button type="button" onClick={close}>关闭</button></header><div className="trade-drawer-body"><label>上榜原因<select aria-label="上榜原因" value={selected} onChange={event => setSelected(Number(event.target.value))}>{reasons.map((item,index) => <option value={index} key={index}>{reasonPeriod(item)} · {listingReason(item)}</option>)}</select></label><p className="trade-meta">{listingReason(row)}</p><dl className="trade-summary"><div><dt>榜单净买额</dt><dd><Amount record={record} field="net_amount" value={row.net_amount} signed/></dd></div><div><dt>买入额</dt><dd><Amount record={record} field="l_buy" value={row.l_buy}/></dd></div><div><dt>卖出额</dt><dd><Amount record={record} field="l_sell" value={row.l_sell}/></dd></div></dl><StockCell row={row}/><p className="trade-meta">同股多原因逐项展示，不跨原因合计。</p><DatasetStatus busy={seats.busy} error={seats.error} retry={seats.reload} empty={!matched.length}><>{!exact && <p className="trade-warning">席位未能与当前上榜原因精确匹配，以下按来源原因分组，不计入本原因金额。</p>}<div className="trade-seat-columns">{sides.map(side => {
    const source = exact ? reasonMatched : matched;
    const key = side === '买入' ? 'buy' : 'sell';
    // Preserve source rows. Ranking a side never merges duplicate causes.
    const sorted = [...source].filter(item => sideName(item.row.side) === side).sort((a,b) => (numeric(b.row[key]) ?? 0) - (numeric(a.row[key]) ?? 0));
    return <section key={side}><h3>{side}席位</h3><div className="trade-table-scroll" tabIndex={0} role="region" aria-label={side + '席位'}><table className="trade-table"><thead><tr><th className="trade-left">营业部 / 机构</th><th>买入</th><th>卖出</th><th>净买</th></tr></thead><tbody>{sorted.map((item,index) => <tr key={index}><td className="trade-left">{item.row.exalter ?? '名称未提供'}{!exact && <small>{item.row.reason ?? '原因未知'}</small>}</td><td><Amount record={item.record} field="buy" value={item.row.buy}/></td><td><Amount record={item.record} field="sell" value={item.row.sell}/></td><td><Amount record={item.record} field="net_buy" value={item.row.net_buy} signed/></td></tr>)}</tbody></table></div></section>;
  })}</div></></DatasetStatus>{seats.record && <DatasetMeta record={seats.record} warning={seats.warning}/>}<details className="trade-details"><summary>席位统计说明</summary><p>买卖席位由来源披露值排序，不将重复上榜原因的金额相加。未知周期、单位和来源字段保持原值。</p><p>当前首页所选标的 {instrumentId ? '已联动' : '尚未选择'}。</p></details></div></dialog>;
}

export function LeaderboardWidget() {
  const read = useTradingDataset('tushare.top_list','tushare_top_list');
  const [query,setQuery] = useState('');
  const [opened,setOpened] = useState<string | null>(null);
  const record = read.record;
  const groups = useMemo(() => groupLeaderboard(record?.payload.rows ?? []),[record]);
  const filtered = groups.filter(group => !query.trim() || group.reasons.some(row => [row.ts_code,row.name,row.reason].some(value => value?.includes(query.trim()))));
  const selected = groups.find(group => `${rowDay(group.primary)}:${group.primary.ts_code}` === opened);
  return <section className="trading-widget" aria-label="龙虎榜股票榜"><div className="trade-toolbar"><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="股票 / 上榜原因" aria-label="龙虎榜查找"/><button type="button" onClick={read.reload} disabled={read.busy}>刷新</button></div><DatasetMeta record={record} warning={read.warning}/><DatasetStatus busy={read.busy} error={read.error} retry={read.reload} empty={!record || !groups.length}>{record && <><p className="trade-meta">单日榜优先 · 同股多原因可展开 · 净买额降序</p><div className="trade-table-scroll trade-core-table" tabIndex={0} role="region" aria-label="龙虎榜股票排行"><table className="trade-table trade-leaderboard"><thead><tr><th className="trade-left">股票</th><th>涨幅</th><th>净买额</th><th>买 / 卖</th><th className="trade-left">原因 / 席位</th></tr></thead><tbody>{filtered.map(group => <tr key={`${rowDay(group.primary)}:${group.primary.ts_code}`}><td className="trade-left"><StockCell row={group.primary}/></td><td><Value value={group.primary.pct_change ?? group.primary.pct_chg} signed suffix="%"/></td><td><Amount record={record} field="net_amount" value={group.primary.net_amount} signed/></td><td><Amount record={record} field="l_buy" value={group.primary.l_buy}/><small><Amount record={record} field="l_sell" value={group.primary.l_sell}/></small></td><td className="trade-left"><button type="button" className="trade-reason-button" onClick={() => setOpened(`${rowDay(group.primary)}:${group.primary.ts_code}`)}>{reasonPeriod(group.primary)} · {listingReason(group.primary)}<small>{group.reasons.length} 项原因 · 查看买卖席位 ›</small></button></td></tr>)}</tbody></table></div><DatasetRaw record={record}/></>}</DatasetStatus>{record && selected && <SeatDrawer key={`${read.requestKey}:${opened}`} record={record} reasons={selected.reasons} close={() => setOpened(null)}/>}</section>;
}
