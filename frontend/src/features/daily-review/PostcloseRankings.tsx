import { useRef, useState } from 'react';
import type { ReviewProps } from './context';
import { number, pct, tone } from './model';
import { Detail, show } from './postcloseVisuals';
import type { Postclose, Stock } from './postcloseVisuals';

const boards = [['amount', '成交额榜'], ['main_net', '主力净额榜'], ['limit_up', '涨停金额榜'], ['broken', '炸板金额榜'], ['5', '5日涨幅榜'], ['10', '10日涨幅榜'], ['20', '20日涨幅榜'], ['30', '30日涨幅榜']] as const;
type Field = { key: string; label: string; description?: string };
const cash = (key: string, label: string): Field => ({ key, label });

function Value({ row, field, day }: { row: Stock; field: Field; day: string | null }) {
  const value = row[field.key];
  const directional = ['pct_change', 'interval_pct', 'main_net_cny'].includes(field.key);
  const rendered = value === null || value === undefined ? '—'
    : field.key.endsWith('_cny') ? show(value, true)
    : ['actual_turnover_pct','interval_turnover_pct'].includes(field.key) ? show(value,false,true) : field.key.endsWith('_pct') || field.key === 'pct_change' ? pct(value)
    : field.key === 'open_times' ? `${value}次` : String(value);
  const explanation = rendered === '—' ? row.field_reasons?.[field.key] ?? `${day}：原始归档未提供${field.description ?? field.label}，缺失不补零。` : `${field.description ?? field.label}：${rendered}`;
  return <Detail className={`pcv-cell-value ${directional ? tone(value) : ''}`} text={explanation}>{rendered}</Detail>;
}

function Board({ kind, title, data, select }: { kind: string; title: string; data: Postclose; select: ReviewProps['select'] }) {
  const panel = data.rankings[kind], interval = /^\d+$/.test(kind), pool = kind === 'limit_up' || kind === 'broken';
  const [sort, setSort] = useState<{ key: string; desc: boolean } | null>(null);
  const order = sort ?? { key: panel?.ranking_field ?? (interval ? 'interval_pct' : 'amount_cny'), desc: true };
  const amount = cash(interval ? 'interval_amount_cny' : 'amount_cny', '成交额');
  const change: Field = { key: interval ? 'interval_pct' : 'pct_change', label: '涨幅', description: interval ? '区间涨幅' : '涨幅' };
  const turnover: Field = { key: interval ? 'interval_turnover_pct' : 'actual_turnover_pct', label: '换手', description: interval ? '区间实际换手' : '实际换手' };
  const cap = cash('actual_float_cny', '实际流通');
  const primary = interval ? [change, amount] : [amount, change];
  const secondary: Field[] = pool
    ? [{ key: 'first_time', label: kind === 'broken' ? '上板' : '首封' }, { key: kind === 'broken' ? 'open_times' : 'last_time', label: kind === 'broken' ? '炸板' : '末封' }]
    : interval ? [turnover] : [cash('main_net_cny', '净额'), turnover];
  const rows = [...(panel?.rows ?? [])].sort((a, b) => {
    const av = number(a[order.key]), bv = number(b[order.key]);
    if (a[order.key] == null && b[order.key] == null) return a.ts_code.localeCompare(b.ts_code);
    if (a[order.key] == null) return 1;
    if (b[order.key] == null) return -1;
    const difference = av !== null && bv !== null ? av - bv : String(a[order.key]).localeCompare(String(b[order.key]), 'zh-CN', order.key==='board_label'?{numeric:true}:undefined);
    return difference * (order.desc ? -1 : 1);
  });
  const sortButton = (field: Field) => <button key={field.key} className={order.key === field.key ? 'pcv-sort-active' : ''} aria-label={`${title}按${field.description ?? field.label}${order.key === field.key && order.desc ? '升序' : '降序'}`} onClick={() => setSort({ key: field.key, desc: order.key === field.key ? !order.desc : true })}>{field.label}{order.key === field.key ? order.desc ? ' ↓' : ' ↑' : ''}</button>;
  return <section className="pcv-board" data-board={kind} aria-label={title}>
    <header><h3>{title}</h3><Detail text={interval ? data.interval_basis : data.scope}>{panel?.window ? `${panel.window.start?.slice(5) ?? '—'}—${panel.window.end.slice(5)} · ` : ''}{rows.length}只</Detail></header>
    <table className="pcv-board-table"><colgroup><col className="pcv-stock-col"/><col className="pcv-primary-col"/><col className="pcv-secondary-col"/><col className="pcv-cap-col"/><col className="pcv-sector-col"/></colgroup>
      <thead><tr><th scope="col"><div className="pcv-field-stack">{sortButton({ key: 'name', label: '股票' })}{sortButton({ key: 'ts_code', label: '代码' })}{kind === 'limit_up' && <span className="pcv-cap-inline">{sortButton({ key: 'board_label', label: '板数' })}</span>}</div></th><th scope="col"><div className="pcv-field-stack">{primary.map(sortButton)}</div></th><th scope="col"><div className="pcv-field-stack">{secondary.map(sortButton)}</div></th><th className="pcv-cap-col" scope="col"><div className="pcv-field-stack">{pool && sortButton(turnover)}{sortButton(cap)}</div></th><th scope="col"><div className="pcv-cap-inline"><div className="pcv-field-stack">{pool && sortButton(turnover)}{sortButton(cap)}</div></div><div className="pcv-field-stack pcv-wide-sector-label">{kind === 'limit_up' && sortButton({ key: 'board_label', label: '板数' })}<span>板块</span></div><span className="pcv-compact-sector-label">板块</span></th></tr></thead>
      <tbody>{rows.map(row => <tr key={row.ts_code}><th scope="row"><button className="pcv-stock-action" aria-label={`${row.name} ${row.ts_code}${kind === 'limit_up' ? ` ${row.board_label ?? '板数缺失'}` : ''}`} onClick={() => select({ stock: row.ts_code })}><span><small>{row.rank}</small>{row.name}</span><small>{row.ts_code.split('.')[0]}</small>{kind === 'limit_up' && <small className="pcv-compact-board-label">{String(row.board_label ?? '—')}</small>}</button></th><td><div className="pcv-field-stack">{primary.map(field => <Value key={field.key} row={row} field={field} day={data.trade_day}/>)}</div></td><td><div className="pcv-field-stack">{secondary.map(field => <Value key={field.key} row={row} field={field} day={data.trade_day}/>)}</div></td><td className="pcv-cap-col"><div className="pcv-field-stack">{pool && <Value row={row} field={turnover} day={data.trade_day}/>}<Value row={row} field={cap} day={data.trade_day}/></div></td><td className="pcv-sector-cell"><div className="pcv-cap-inline">{pool && <Value row={row} field={turnover} day={data.trade_day}/>}<Value row={row} field={cap} day={data.trade_day}/></div>{kind === 'limit_up' && <Detail className="pcv-board-tag" text={`板数：${row.board_label ?? '原始字段缺失'}`}>{String(row.board_label ?? '—')}</Detail>}<Detail className="pcv-theme-detail" text={row.themes.join('、') || `${data.trade_day}：暂无该日以前已保存的完整同花顺分类批次。`}>{row.themes.slice(0, 2).join('、') || '—'}</Detail></td></tr>)}</tbody>
    </table>
    {!rows.length && <p className="pcv-empty">{panel?.reason === 'interval_sessions_incomplete' ? '区间日线尚未齐全，后台将自动补采。' : '所选日期尚无已保存榜单。'}</p>}
  </section>;
}

export function PostcloseRankings({ data, select }: { data: Postclose; select: ReviewProps['select'] }) {
  const scroll = useRef<HTMLDivElement>(null);
  return <aside className="pcv-rankings" aria-label="榜单区域"><div className="pcv-ranking-tools"><nav aria-label="跳转榜单">{boards.map(([kind, title]) => <button key={kind} onClick={() => { const panel = scroll.current?.querySelector<HTMLElement>(`[data-board="${kind}"]`); if (panel && scroll.current) scroll.current.scrollTo({ top: scroll.current.scrollTop + panel.getBoundingClientRect().top - scroll.current.getBoundingClientRect().top, behavior: 'instant' }); }}>{title.replace('榜', '').replace('金额', '').replace('主力净额', '主力')}</button>)}</nav><span>{data.trade_day?.slice(5)}</span></div><div className="pcv-rankings-scroll" ref={scroll} tabIndex={0} role="region" aria-label="八类盘后榜单"><div className="pcv-board-grid">{boards.map(([kind, title]) => <Board key={`${kind}:${data.trade_day}`} kind={kind} title={title} data={data} select={select}/>)}</div></div></aside>;
}
