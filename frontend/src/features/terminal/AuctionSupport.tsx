import { useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import type { AuctionSupportRow, Envelope_AuctionSupportView_ } from '../../generated/schema';
import { request } from '../../shared/api';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { useWorkspaceState } from '../../shared/workspaceState';
import { Change, Panel } from './Components';
import { dateTime, number, percent, tone } from './data';

type Row = AuctionSupportRow;
type Sort = { key: keyof Row; direction: 'asc' | 'desc' };
type Read = ReturnType<typeof useAuctionSupport>;
type Props = { read: Read; identity: string; selected: string | undefined; onSelect: (code: string) => void };
const missing = '—';
const cash = (value: number | null | undefined) => value == null ? missing : Math.abs(value) >= 1e8 ? number(value / 1e8, 2) + '亿' : number(value / 1e4, 0) + '万';
const plain = (value: unknown) => value == null || value === '' ? missing : String(value);

export function useAuctionSupport(user: string, day: string, cutoff: string) {
  const query = new URLSearchParams({ ...(day ? { trade_day: day } : {}), ...(cutoff ? { as_of_time: cutoff } : {}) });
  const path = '/api/v1/auction-support?' + query;
  return useCachedLoader(() => request<Envelope_AuctionSupportView_>(path), user + ':' + path, '竞价承接与排行');
}

// Missing values always remain last, including an ascending sort.
export function orderSupport(rows: Row[], sort: Sort) {
  return [...rows].sort((a, b) => {
    const x = a[sort.key], y = b[sort.key];
    if (x == null || y == null) return x == null && y == null ? a.code.localeCompare(b.code) : x == null ? 1 : -1;
    const value = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), 'zh-CN');
    return (sort.direction === 'asc' ? value : -value) || a.code.localeCompare(b.code);
  });
}

type Column = { key: keyof Row; label: string; kind?: 'cash' | 'pct' | 'height'; title?: string; width?: number };
const columns: Column[] = [
  { key: 'name', label: '名称', width: 88 }, { key: 'code', label: '代码', width: 68 },
  { key: 'previous_height', label: '几板', kind: 'height', title: '昨日收盘连板高度' },
  { key: 'auction_pct', label: '竞价涨幅', kind: 'pct' },
  { key: 'support_pct', label: '竞价承接', kind: 'pct', title: '竞价成交量（股）÷昨日全天成交量（股）×100%' },
  { key: 'auction_amount_cny', label: '竞价金额', kind: 'cash', title: '实际撮合成交额，与阶段封单分别统计' },
  { key: 'auction_turnover_pct', label: '竞价换手', kind: 'pct' },
  { key: 'pct_change', label: '涨幅', kind: 'pct', title: '日线归档行显示收盘涨幅；盘中行保留原行情时点' },
  { key: 'current_seal_cny', label: '当前封单', kind: 'cash', title: '盘后使用已存当日日榜封单；不是09:25封单' },
  { key: 'amount_cny', label: '现成交额', kind: 'cash' },
  { key: 'volume_change_pct', label: '成交量比', kind: 'pct', title: '当日累计成交量÷昨日全天成交量×100%；不是竞价量比' },
  { key: 'actual_float_cny', label: '实际流通', kind: 'cash', title: '已存自由流通股本×对应行情价格；股本仅取当日或前一交易日' },
  { key: 'previous_type', label: '昨板型' }, { key: 'previous_close', label: '昨股价' },
  { key: 'previous_amount_cny', label: '昨成交额', kind: 'cash' }, { key: 'previous_turnover_pct', label: '昨换手', kind: 'pct' },
  { key: 'previous_seal_cny', label: '昨封', kind: 'cash' }, { key: 'previous_seal_time', label: '昨封板' },
  { key: 'previous_open_times', label: '昨开板' },
  { key: 'popularity_rank', label: '人气排名', title: '当前已接入来源未提供人气排名' },
  { key: 'change_reason', label: '涨停原因', width: 156 }, { key: 'lhb_status', label: '龙虎榜', title: '昨日已存龙虎榜，点击查看上榜原因' },
];
const defaultFilters = {
  heights: ['1', '2', '3', '4', '5', '6'], markets: ['沪主板', '深主板', '创业板', '科创板', '北交所'],
  caps: ['0', '1', '2', '3', '4'], gaps: ['0', '1', '2', '3', '4'], supports: ['0', '1', '2', '3', '4', '5'],
  amounts: ['0', '1', '2', '3', '4'], themes: [] as string[],
};
type Filters = typeof defaultFilters;
const band = (value: number | null | undefined, boundaries: number[]) => value == null ? null : String(boundaries.filter(boundary => value >= boundary).length);
function includes(selected: string[], full: string[], value: string | null | undefined) {
  return full.every(item => selected.includes(item)) || value != null && selected.includes(value);
}
export function filterSupport(rows: Row[], filters: Filters) {
  return rows.filter(row => row.previous_state === 'U'
    && includes(filters.heights, defaultFilters.heights, row.previous_height == null ? null : String(Math.min(6, row.previous_height)))
    && includes(filters.markets, defaultFilters.markets, row.market)
    && includes(filters.caps, defaultFilters.caps, band(row.float_mv_cny, [30e8, 50e8, 80e8, 120e8]))
    && includes(filters.gaps, defaultFilters.gaps, row.auction_at_limit ? '4' : band(row.auction_pct, [0, 3, 7]))
    && includes(filters.supports, defaultFilters.supports, band(row.support_pct, [5, 7, 10, 15, 20]))
    && includes(filters.amounts, defaultFilters.amounts, band(row.auction_amount_cny, [2e7, 5e7, 1e8, 2e8]))
    && (!filters.themes.length || (row.themes?.length ? row.themes.some(theme => filters.themes.includes(theme)) : filters.themes.includes('其他板块'))));
}

function SortHead({ column, sort, setSort }: { column: Column; sort: Sort; setSort: (sort: Sort) => void }) {
  const active = sort.key === column.key;
  return <th scope="col" aria-sort={active ? sort.direction === 'desc' ? 'descending' : 'ascending' : 'none'} style={{ width: column.width }}>
    <button type="button" title={column.title} onClick={() => setSort({ key: column.key, direction: active && sort.direction === 'desc' ? 'asc' : 'desc' })}>{column.label}{active && <svg aria-hidden="true" width="9" height="10" viewBox="0 0 10 12" fill="none" stroke="currentColor" strokeWidth="1.5" style={sort.direction === 'asc' ? { transform: 'rotate(180deg)' } : undefined}><path d="M5 1v9M2 7l3 3 3-3"/></svg>}</button>
  </th>;
}
function ReadingState({ read }: { read: Read }) {
  return read.error ? <div className="ty-auction-read-state" role="alert">竞价资料读取失败 <button type="button" onClick={read.reload}>重新读取</button></div>
    : read.busy ? <div className="ty-auction-read-state" role="status">正在读取竞价资料…</div> : null;
}
function Cell({ row, column, onSelect, showLhb }: { row: Row; column: Column; onSelect: Props['onSelect']; showLhb: (row: Row) => void }) {
  const value = row[column.key];
  if (column.key === 'name') return <button type="button" className="ty-text-button" onClick={() => onSelect(row.code)} title={`${row.name} ${row.code}\n${row.price_basis === 'daily_close' ? '收盘日线' : '行情时点'} ${dateTime(row.source_time)}`}>{row.name}</button>;
  if (column.key === 'code') return row.code.split('.')[0];
  if (column.key === 'lhb_status') return row.lhb_status === 'listed' ? <button type="button" className="ty-link" onClick={() => showLhb(row)}>查看</button> : <span title={row.lhb_status === 'not_listed' ? '昨日完整龙虎榜归档中无记录' : '昨日龙虎榜尚无完整归档'}>{row.lhb_status === 'not_listed' ? '无榜' : missing}</span>;
  if (column.kind === 'pct') return column.key === 'auction_pct' || column.key === 'pct_change'
    ? <span className={tone(value as number | null)}>{percent(value as number | null)}</span>
    : value == null ? missing : number(value as number, 2) + '%';
  if (column.kind === 'cash') return cash(value as number | null);
  if (column.kind === 'height') return value == null ? missing : value + '板';
  if (column.key === 'previous_close') return number(value as number | null, 2);
  if (column.key === 'previous_seal_time' && typeof value === 'string' && /^\d{6}$/.test(value)) return value.replace(/^(\d{2})(\d{2})(\d{2})$/, '$1:$2:$3');
  return <span title={plain(value)}>{plain(value)}</span>;
}

export function AuctionSupport({ read, identity, selected, onSelect, onDay }: Props & { onDay: (day: string) => void }) {
  const data = read.data?.data;
  const [filters, setFilters] = useWorkspaceState(identity + ':filters', defaultFilters);
  const [sort, setSort] = useWorkspaceState<Sort>(identity + ':support-sort', { key: 'support_pct', direction: 'desc' });
  const [hidden, setHidden] = useWorkspaceState<string[]>(identity + ':columns', []);
  const [limit, setLimit] = useState(100);
  const [listing, setListing] = useState<Row | null>(null);
  const filtered = useMemo(() => orderSupport(filterSupport(data?.rows ?? [], filters), sort), [data, filters, sort]);
  const visible = columns.filter(column => !hidden.includes(column.key));
  function toggle(group: keyof Filters, value: string) {
    const chosen = filters[group]; setLimit(100);
    setFilters({ ...filters, [group]: chosen.includes(value) ? chosen.filter(item => item !== value) : [...chosen, value] });
  }
  function group(key: keyof Filters, label: string, labels: string[], values = labels) {
    return <fieldset><legend>{label}</legend><div>{labels.map((text, i) => <label key={values[i]}><input type="checkbox" checked={filters[key].includes(values[i])} onChange={() => toggle(key, values[i])}/>{text}</label>)}</div></fieldset>;
  }
  const themes = new Map<string, number>();
  for (const row of data?.rows ?? []) if (row.previous_state === 'U') for (const theme of row.themes?.length ? row.themes : ['其他板块']) themes.set(theme, (themes.get(theme) ?? 0) + 1);
  const themeList = [...themes].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh-CN'));
  const header = <div className="ty-support-actions"><input aria-label="竞价承接日期" type="date" value={data?.trade_day ?? ''} max={new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date())} onChange={event => event.target.value && onDay(event.target.value)}/><button type="button" onClick={() => setFilters(defaultFilters)}>重置筛选</button><details className="ty-support-columns"><summary aria-label="自定义显示列" title="自定义显示列"><svg aria-hidden="true" width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.4"><rect x="2" y="3" width="16" height="14" rx="1"/><path d="M7 3v14m6-14v14M2 7h16"/></svg></summary><div>{columns.filter(c => c.key !== 'name' && c.key !== 'code').map(c => <label key={c.key}><input type="checkbox" checked={!hidden.includes(c.key)} onChange={() => setHidden(hidden.includes(c.key) ? hidden.filter(key => key !== c.key) : [...hidden, c.key])}/>{c.label}</label>)}</div></details></div>;
  return <Panel title="竞价承接" action={header} className="ty-auction-support">
    <div className="ty-support-cohort">{data?.previous_trade_day ? `${data.previous_trade_day} 涨停股 → ${data.trade_day}` : '昨日涨停股的当日表现'}</div>
    <div className="ty-support-filters" aria-label="竞价承接筛选">
      {group('heights', '连板高度', ['首板', '2板', '3板', '4板', '5板', '6板+'], defaultFilters.heights)}
      {group('markets', '市场类型', defaultFilters.markets)}
      {group('caps', '流通市值', ['<30亿', '30–50亿', '50–80亿', '80–120亿', '120亿+'], defaultFilters.caps)}
      {group('gaps', '竞价涨幅', ['低开', '0–3%', '3–7%', '7%+', '涨停'], defaultFilters.gaps)}
      {group('supports', '竞价承接', ['<5%', '5–7%', '7–10%', '10–15%', '15–20%', '20%+'], defaultFilters.supports)}
      {group('amounts', '竞价金额', ['<2000万', '2000–5000万', '5000万–1亿', '1–2亿', '2亿+'], defaultFilters.amounts)}
      <fieldset className="ty-support-themes"><legend>涨停板块</legend><div><label><input type="checkbox" checked={!filters.themes.length} onChange={() => setFilters({ ...filters, themes: [] })}/>全部</label>{themeList.map(([theme, count]) => <label key={theme}><input type="checkbox" checked={filters.themes.includes(theme)} onChange={() => toggle('themes', theme)}/>{theme} ({count})</label>)}</div></fieldset>
    </div>
    <ReadingState read={read}/>
    <div className="ty-support-scroll" data-scroll-key="auction-support-table" tabIndex={0} aria-label="竞价承接表格滚动区域"><table className="ty-support-table" aria-label="竞价承接个股"><thead><tr>{visible.map(column => <SortHead key={column.key} column={column} sort={sort} setSort={setSort}/>)}</tr></thead><tbody>{filtered.slice(0, limit).map(row => <tr key={row.code} data-current={row.code === selected || undefined}>{visible.map(column => <td key={column.key}><Cell row={row} column={column} onSelect={onSelect} showLhb={setListing}/></td>)}</tr>)}</tbody></table>{!read.busy && !filtered.length && <p className="ty-auction-read-state">{data?.state === 'unavailable' ? '该日尚无完整昨日涨停池' : '无符合筛选条件的股票'}</p>}</div>
    <div className="ty-support-footer"><span>{filtered.length}只{read.refreshing ? ' · 更新中' : ''}</span>{filtered.length > limit && <button type="button" onClick={() => setLimit(limit + 100)}>再显示100只</button>}<button type="button" onClick={read.reload} aria-label="刷新竞价承接">刷新</button></div>
    {listing && <div className="ty-support-listing" role="dialog" aria-label={`${listing.name}昨日龙虎榜`} onKeyDown={event => event.key === 'Escape' && setListing(null)}><div><strong>{listing.name} · {data?.previous_trade_day}</strong><button type="button" autoFocus onClick={() => setListing(null)}>关闭</button></div>{listing.lhb_reasons?.length ? <ul>{listing.lhb_reasons.map(reason => <li key={reason}>{reason}</li>)}</ul> : <p>已存上榜记录未提供原因</p>}</div>}
  </Panel>;
}

const rankingTypes = ['涨停委买', '昨日涨停', '竞价爆量', '竞价换手', '竞价净额', '昨炸板', '昨断板', '昨上榜', '竞价涨停', '竞价跌停'] as const;
type Ranking = typeof rankingTypes[number];
const defaultRank: Record<Ranking, keyof Row> = { 涨停委买: 'current_bid_cny', 昨日涨停: 'auction_amount_cny', 竞价爆量: 'auction_volume_ratio', 竞价换手: 'auction_turnover_pct', 竞价净额: 'auction_net_cny', 昨炸板: 'auction_amount_cny', 昨断板: 'auction_amount_cny', 昨上榜: 'auction_amount_cny', 竞价涨停: 'auction_amount_cny', 竞价跌停: 'auction_amount_cny' };
function rankScope(row: Row, type: Ranking) {
  if (type === '涨停委买') return row.at_limit === true && row.current_bid_cny != null;
  if (type === '昨日涨停') return row.previous_state === 'U';
  if (type === '昨炸板') return row.previous_state === 'Z';
  if (type === '昨断板') return row.previous_broken;
  if (type === '昨上榜') return row.previous_lhb;
  if (type === '竞价涨停') return row.auction_at_limit;
  if (type === '竞价跌停') return row.auction_at_down_limit;
  return row[defaultRank[type]] != null;
}
export function AuctionRankings({ read, identity, selected, onSelect }: Props) {
  const data = read.data?.data;
  const [type, setType] = useWorkspaceState<Ranking>(identity + ':ranking', '昨日涨停');
  const [sort, setSort] = useWorkspaceState<Sort>(identity + ':ranking-sort', { key: 'auction_amount_cny', direction: 'desc' });
  const [limit, setLimit] = useState(100);
  const rows = useMemo(() => orderSupport((data?.rows ?? []).filter(row => rankScope(row, type)), sort), [data, type, sort]);
  const metric = defaultRank[type];
  const metricLabel = type === '竞价爆量' ? '竞价量比' : type === '竞价换手' ? '竞价换手' : type === '竞价净额' ? '竞价净额' : '封单';
  const rankColumns: Column[] = [{ key: 'name', label: '名称 / 竞价额', width: 88 }, { key: 'pct_change', label: '涨幅', width: 54 },
    { key: ['auction_volume_ratio', 'auction_turnover_pct', 'auction_net_cny'].includes(metric) ? metric : 'current_seal_cny', label: metricLabel, width: 58 },
    { key: 'auction_pct', label: '竞价涨幅', width: 60 }, { key: 'actual_float_cny', label: '实际流通', width: 62 }];
  function metricCell(row: Row): ReactNode {
    if (metric === 'auction_volume_ratio') return number(row.auction_volume_ratio, 2);
    if (metric === 'auction_turnover_pct') return percent(row.auction_turnover_pct);
    return cash(metric === 'auction_net_cny' ? row.auction_net_cny : row.current_seal_cny);
  }
  const title = type === '竞价爆量' ? '按供应商原始竞价量比排序' : type === '昨断板' ? '前日涨停且昨日收盘低于昨日涨停价；缺失行情不推定断板' : undefined;
  return <Panel title="竞价排行" className="ty-auction-rankings" action={<span>{data?.trade_day}</span>}>
    <div className="ty-ranking-tabs" role="group" aria-label="竞价排行范围">{rankingTypes.map(label => <button type="button" key={label} aria-pressed={type === label} onClick={() => { setType(label); setSort({ key: defaultRank[label], direction: 'desc' }); setLimit(100); }}>{label}</button>)}</div>
    <ReadingState read={read}/>
    <div className="ty-support-scroll" data-scroll-key={'auction-rankings:' + type} tabIndex={0} aria-label="竞价排行滚动区域" title={title}><table className="ty-support-table ty-ranking-table" aria-label={type + '排行'}><thead><tr>{rankColumns.map(column => <SortHead key={column.key} column={column} sort={sort} setSort={setSort}/>)}</tr></thead><tbody>{rows.slice(0, limit).map(row => <tr key={row.code} data-current={row.code === selected || undefined}>
      <td><button type="button" className="ty-text-button" onClick={() => onSelect(row.code)}>{row.name}</button><span className="ty-ranking-sub"><span>{row.code.split('.')[0]}</span><span>{cash(row.auction_amount_cny)}</span></span></td>
      <td title={`${row.price_basis === 'daily_close' ? '收盘涨幅' : '行情时点'} ${dateTime(row.source_time)}`}><Change value={row.pct_change}/></td><td>{metricCell(row)}</td><td><Change value={row.auction_pct}/></td><td>{cash(row.actual_float_cny)}</td>
    </tr>)}</tbody></table>{!read.busy && !rows.length && <div className="ty-auction-read-state">{type === '竞价净额' ? '已接入来源未提供竞价净额' : '暂无符合条件的已存记录'}</div>}</div>
    <div className="ty-support-footer"><span>{rows.length}只</span>{rows.length > limit && <button type="button" onClick={() => setLimit(limit + 100)}>再显示100只</button>}<button type="button" onClick={read.reload} aria-label="刷新竞价排行">刷新</button></div>
  </Panel>;
}
