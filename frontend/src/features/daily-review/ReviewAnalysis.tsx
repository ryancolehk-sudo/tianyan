import { useEffect, useRef, useState } from 'react';
import { api } from '../../shared/api';
import { useBoundRead, Loading, Failure, Empty, stateLabel, Value } from './components';
import { amount, number, text } from './model';
import type { Row } from './model';
import type { ReviewProps } from './context';
import '../../shared/historyPlot.css';
import type { IntradayLadder } from '../terminal/types';
import { ladderColumns, liveLadderStatus } from './liveLadder';

type Selection = { day: string; asOf: string; select: (values: Record<string, string | null>) => void };
function parameters(day: string, asOf: string, kind: string, lookback = '20', stock?: string) {
  return new URLSearchParams({ trade_day: day, kind, lookback, ...(asOf ? { as_of_time: asOf } : {}), ...(stock ? { stock } : {}) });
}
function PendingPool() {
  return <p className="review-pool-pending"><strong>日榜待补齐</strong><span>来源暂返回空表，不能据此判断当日没有涨停或连板股。</span></p>;
}
function sealTime(value: unknown) {
  const raw = String(value ?? '').trim();
  const digits = raw.replace(/:/g, '').padStart(6, '0');
  return raw && /^\d{6}$/.test(digits) && Number(digits.slice(0, 2)) < 24 && Number(digits.slice(2, 4)) < 60 && Number(digits.slice(4)) < 60
    ? `${digits.slice(0, 2)}:${digits.slice(2, 4)}:${digits.slice(4)}` : text(value);
}
function boardStatistic(value: unknown) {
  const raw = text(value);
  const match = /^(\d+)\/(\d+)$/.exec(raw);
  return match ? `${match[2]}天${match[1]}板` : raw;
}
const limitGroupHeaders = ['股票名称','股票代码','股价','涨幅','涨停类型','板数','连板','首次封板','最后封板','开板','封单额','成交额','换手率','实际流通','流通市值','总市值','异动原因','龙虎榜'];
export function GroupedLimitUps(p: ReviewProps) {
  const [open, setOpen] = useState(false), [mode, setMode] = useState('board');
  const params = parameters(p.day, p.asOf, 'groups');
  const read = useBoundRead(`pool-groups:${params}:${open}`, () => open ? api.reviewAnalysis(params) : Promise.resolve(null));
  const data = read.data?.data.groups;
  const emptyPool = data?.state === 'complete' && data.coverage?.some(c => c.dataset === 'pools' && c.row_count === 0);
  const records = (data?.records ?? []).filter(r => !p.mine || p.watched.has(String(r.ts_code)));
  const sourceGroups = mode === 'board' ? [...(data?.boards ?? [])].sort((a,b) => (Number(b.key) || 0) - (Number(a.key) || 0)) : [...(data?.themes ?? []), { key: 'unassigned', label: '原日题材未披露', codes: data?.unassigned_codes ?? [] }];
  const groups = sourceGroups.map(g => ({...g, rows: records.filter(r => g.codes.includes(String(r.ts_code))).sort((a,b) => (number(b.board_height) ?? 0) - (number(a.board_height) ?? 0) || sealTime(a.first_time).localeCompare(sealTime(b.first_time)) || String(a.ts_code).localeCompare(String(b.ts_code)))})).filter(g => g.rows.length);
  const moneyCell = (row: Row, key: string) => {
    const details = row.group_field_details as Record<string, { state?: string; source_value?: string; source_unit?: string }> | undefined;
    const field = details?.[key];
    if (field?.state === 'unit_unverified') return <button className="limit-field-pending" title={`原始值 ${field.source_value}；归档未声明单位，尚未换算。点击查看原始材料。`} disabled={!row.row_reference} onClick={() => row.row_reference && p.openRef(row.row_reference)}>单位待核</button>;
    return amount(row[key]);
  };
  return <details className="review-extension limit-groups" onToggle={e => { if (e.target === e.currentTarget) setOpen(e.currentTarget.open); }}><summary>当日涨停分组 · 板数与原日题材</summary>{open && <div className="review-extension-body limit-groups-body">
    <div className="limit-group-tabs" role="group" aria-label="涨停分组方式">{[['theme','涨停复盘（按题材）'],['board','涨停复盘（按连板）']].map(([key,label]) => <button key={key} aria-pressed={mode === key} onClick={() => setMode(key)}>{label}</button>)}</div>
    {read.busy ? <Loading/> : read.error ? <Failure error={read.error} retry={read.reload}/> : <>
    <div className="limit-group-meta"><span>{emptyPool ? '名单待补齐' : `共 ${records.length} 只`}</span><span>{data?.scope}{mode === 'theme' && ` · 原日成分：${stateLabel(data?.membership_state ?? 'missing')}，资料日 ${data?.membership_day ?? '未取得'}`}{!!data?.ambiguous_n && ` · ${data.ambiguous_n}条重复身份记录待核`}</span></div>
    {mode === 'theme' && data?.membership_state !== 'complete' && <p className="history-plot-note" role="status">该日历史成分尚未完整取得，不能确认题材分组；未用当前成分回填。</p>}
    <div className="limit-group-list">{groups.map(g => <details className="limit-group-section" key={`${mode}:${g.key}`} open>
      <summary><strong>{mode === 'board' && /^\d+$/.test(g.key) ? Number(g.key) === 1 ? '首板' : `${g.key}连板` : g.label}</strong><span className="limit-group-count"><b>{g.rows.length}</b> 涨停</span><span className="limit-group-chevron" aria-hidden="true">⌃</span></summary>
      <div className="limit-group-scroll" tabIndex={0} role="region" aria-label={`${g.label}明细滚动区域`}><table aria-label="涨停分组明细"><colgroup>{[70,68,52,58,66,70,40,70,70,40,62,62,58,66,66,66,undefined,58].map((width,i) => <col key={i} style={{width}}/>)}</colgroup><thead><tr>{limitGroupHeaders.map(label => <th key={label} scope="col">{label}</th>)}</tr></thead><tbody>{g.rows.map(row => <tr key={String(row.ts_code)} className={p.stock === row.ts_code ? 'selected' : ''}>
      <td><button className="limit-stock-link" onClick={() => p.select({ stock: String(row.ts_code), record_key: null })}>{text(row.name)}</button></td>
      <td className="limit-code">{String(row.ts_code).split('.')[0]}</td>
      <td>{number(row.close)?.toFixed(2) ?? '—'}</td><td><Value value={row.pct_chg} percentage/></td>
      <td title={row.limit_type ? undefined : "当前来源未提供涨停类型"}>{text(row.limit_type)}</td><td><span className="limit-board-badge">{row.board_stat_label ? text(row.board_stat_label) : boardStatistic(row.up_stat)}</span></td><td>{text(row.board_height)}</td>
      <td>{sealTime(row.first_time)}</td><td>{sealTime(row.last_time)}</td><td>{text(row.open_times)}</td>
      <td>{moneyCell(row, 'seal_amount_yuan')}</td><td>{amount(row.turnover_yuan)}</td><td>{number(row.turnover_ratio) === null ? '—' : `${number(row.turnover_ratio)!.toFixed(1)}%`}</td>
      <td title="按同日自由流通股本×收盘价计算">{amount(row.actual_float_value_yuan)}</td><td>{moneyCell(row, 'float_market_value_yuan')}</td><td>{moneyCell(row, 'total_market_value_yuan')}</td>
      <td className="limit-reason" title={row.change_reason ? String(row.change_reason) : '当前来源未提供异动原因'}>{text(row.change_reason)}</td><td>{row.lhb_status === 'listed' ? <a className="limit-lhb-link" title={`当日披露${row.lhb_count}条龙虎榜记录`} href={`/research/reviews?${new URLSearchParams({ view: 'lhb', trade_day: p.day, stock: String(row.ts_code), ...(p.asOf ? { as_of_time: p.asOf } : {}) })}`}>查看</a> : row.lhb_status === 'not_listed' ? <span className="muted">无榜</span> : <span title="当日龙虎榜资料未齐，不能确认是否上榜">—</span>}</td>
      </tr>)}</tbody></table></div>
    </details>)}</div>
    {!records.length && (emptyPool ? <PendingPool/> : <Empty>{data?.state === 'complete' ? '当前分组没有披露记录。' : '当日涨停池尚未完整取得。'}</Empty>)}
    </>}
  </div>}</details>;
}

export function LadderHistory({ day, asOf, select, live }: Selection & { live?: IntradayLadder | null }) {
  const [open, setOpen] = useState(true), [lookback, setLookback] = useState('20');
  const [firstBoards, setFirstBoards] = useState(false), [expanded, setExpanded] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const expandButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (expanded) dialog.current?.showModal();
    else if (dialog.current?.open) { dialog.current.close(); expandButton.current?.focus(); }
  }, [expanded]);
  const params = parameters(day, asOf, 'ladder', lookback);
  const read = useBoundRead(`ladder-history:${params}:${open}`, () => open ? api.reviewAnalysis(params) : Promise.resolve(null));
  const height = (value: unknown) => { const n = number(value); return n !== null && Number.isInteger(n) && n > 0 ? n : null; };
  const records = [...ladderColumns(read.data?.data.records ?? [],live,day,asOf)].sort((a,b) => b.trade_day.localeCompare(a.trade_day)).map(r => {
    const available = r.state === 'complete' || r.state === 'partial' && ['supplier_ladder','intraday_ladder'].includes(r.basis ?? '');
    const emptyPool = read.data?.data.coverage?.some(c => c.dataset === 'pools' && c.trade_day === r.trade_day && c.row_count === 0);
    const waiting = r.basis==='intraday_ladder' ? (r.stocks??[]).filter(s=>['waiting','unknown'].includes(String(s.live_status))) : [];
    const stocks = available ? [...(r.stocks ?? [])].filter(s=>!waiting.includes(s)).sort((a,b) => (height(b.board_height) ?? 0)-(height(a.board_height) ?? 0) || String(a.ts_code).localeCompare(String(b.ts_code))) : [];
    const note = r.basis==='intraday_ladder' ? r.coverage_note ?? '盘中观察' : !available ? '当日资料未齐备' : !stocks.length ? emptyPool ? '日榜待补齐' : '该批次暂无可展示的涨停记录' : r.coverage_note ?? '';
    return { ...r, available, stocks, waiting, note };
  });
  const liveColumn=records.find(r=>r.basis==='intraday_ladder');
  const levels = [7,6,5,4,3,2, ...(firstBoards ? [1] : []), ...(records.some(r => r.stocks.some(s => height(s.board_height) === null)) ? [0] : [])];
  const content = <div className="review-extension-body ladder-matrix-body">
    <div className="review-extension-tools">
      {['20','60'].map(n => <button key={n} aria-pressed={lookback===n} onClick={() => setLookback(n)}>近{n}日</button>)}
      <span className="ladder-range">{records.length ? `${records.at(-1)!.trade_day} — ${records[0].trade_day}` : '原日收盘榜'}</span>
      <button aria-pressed={firstBoards} onClick={() => setFirstBoards(v => !v)}>{firstBoards ? '收起首板' : '显示首板'}</button>
      <button ref={expanded ? undefined : expandButton} onClick={() => setExpanded(v => !v)}>{expanded ? '关闭大图' : '放大查看'}</button>
    </div>
    {liveColumn && <p className="history-plot-note" role="status">今日按盘中目标档位展示 · 已核封住 {live?.sealed_count ?? 0} 只 · 过期或时点待核 {live?.stale_count ?? 0} 只。{live?.scope}</p>}
    {read.busy && !liveColumn ? <Loading/> : read.error && !liveColumn ? <Failure error={read.error} retry={read.reload}/> : !records.length ? <Empty>该窗口暂无连板资料。</Empty> : <div className="ladder-matrix-scroll" tabIndex={0} role="region" aria-label="连板天梯滚动区域">
      <table className="ladder-matrix" aria-label="多日连板天梯">
        <thead><tr><th scope="col" className="ladder-axis">板数</th>{records.map(r => <th scope="col" key={r.trade_day} data-day={r.trade_day} title={`${r.trade_day}${r.note ? ` · ${r.note}` : ''}`}>
          <time dateTime={r.trade_day}>{r.trade_day.slice(5)}</time>
          {r.note && <span className="ladder-day-status">{r.note}</span>}
        </th>)}</tr></thead>
        <tbody>{levels.map(level => <tr key={level} data-level={level}><th scope="row" className="ladder-axis">{level === 7 ? '7板+' : level === 0 ? '未知' : `${level}板`}</th>{records.map(r => {
          const stocks = r.stocks.filter(s => { const h = height(s.board_height); return level === 7 ? h !== null && h >= 7 : level === 0 ? h === null : h === level; });
          return <td key={r.trade_day} data-day={r.trade_day} className={!r.available || !r.stocks.length ? 'ladder-unavailable' : ''}>
            {stocks.map(s => <button className="ladder-stock" key={String(s.ts_code)} data-live-status={s.live_status ? String(s.live_status) : undefined} data-stale={s.freshness && s.freshness!=='current' ? true : undefined}
              aria-label={`${r.trade_day} · ${text(s.name)} · ${text(s.ts_code)} · ${s.live_status ? '目标' : ''}${height(s.board_height) ?? '未知'}板${s.live_status ? ` · ${liveLadderStatus[String(s.live_status)]}` : ''}`}
              title={`${text(s.name)} · ${text(s.ts_code)}\n${r.trade_day} · ${s.live_status ? '目标' : ''}${height(s.board_height) ?? '未知'}板${s.live_status ? `\n${liveLadderStatus[String(s.live_status)]} · 来源 ${text(s.source_name)} · 时点 ${text(s.source_time)}` : ''}`}
              onClick={() => { setExpanded(false); select({ trade_day: r.trade_day, stock: String(s.ts_code), record_key: null }); }}>
              <span>{text(s.name)}</span>{level === 7 && !s.live_status && <small className="ladder-height-badge">{height(s.board_height)}板</small>}
              {!!s.live_status && <small className="ladder-live-status">{s.freshness!=='current'?'过期 · ':''}{height(s.board_height) ? `看${height(s.board_height)}板 · ` : '板数待核 · '}{liveLadderStatus[String(s.live_status)]}</small>}
            </button>)}
            {level === 1 && r.basis === 'supplier_ladder' && <span className="ladder-cell-note">首板待完整日榜</span>}
          </td>;
        })}</tr>)}</tbody>
      </table>
    </div>}
    {liveColumn && !liveColumn.stocks.length && <p className="history-plot-note">本批尚无可核触板或封板样本；不是全市场零涨停。</p>}
    {liveColumn && !!liveColumn.waiting.length && <details className="ladder-waiting"><summary>今日待晋级 / 行情待核 · {liveColumn.waiting.length}只</summary><div>{liveColumn.waiting.map(s=><button key={String(s.ts_code)} onClick={()=>select({trade_day:day,stock:String(s.ts_code),record_key:null})}>{text(s.name)} · 看{height(s.board_height) ?? '未知'}板 · {s.freshness!=='current'?'时效待核 · ':''}{liveLadderStatus[String(s.live_status)]}</button>)}</div></details>}
    {liveColumn && read.busy && <p className="history-plot-note">历史对照正在读取，盘中列继续更新。</p>}
    {liveColumn && read.error && <Failure error={read.error} retry={read.reload}/>}
    <p className="history-plot-note">历史列为原日收盘高度；今日盘中列随原行情刷新，开板退出已封住状态，待晋级不计成功。默认展示2板及以上，板数不全归入未知；不是逐笔实时。空白与缺测均不等于无涨停。</p>
  </div>;
  return <details className="review-extension ladder-extension" open={open} onToggle={e => setOpen(e.currentTarget.open)}>
    <summary>多日连板梯队对照</summary>{open && !expanded && content}
    <dialog ref={dialog} className="ladder-dialog" aria-label="连板天梯大图" onCancel={() => setExpanded(false)} onClose={() => setExpanded(false)}>{expanded && content}</dialog>
  </details>;
}

export function StockCandles({ day, asOf, stock, select }: Selection & { stock: string }) {
  const [open, setOpen] = useState(false), [lookback, setLookback] = useState('20');
  const params = parameters(day, asOf, 'stock', lookback, stock);
  const read = useBoundRead(`lhb-candles:${params}:${open}`, () => open && stock ? api.reviewAnalysis(params) : Promise.resolve(null));
  const records = read.data?.data.records ?? [];
  const prices = records.flatMap(r => [number(r.quote?.high), number(r.quote?.low)]).filter((v): v is number => v !== null && v > 0);
  const low = prices.length ? Math.min(...prices) : 0, high = prices.length ? Math.max(...prices) : 1;
  const y = (value: number) => 180 - (value-low)*140/(high-low || 1), x = (i: number) => 50+i*700/Math.max(1,records.length-1);
  return <details className="review-extension" onToggle={e => setOpen(e.currentTarget.open)}><summary>个股 K 线与上榜日 · {stock || '待选股票'}</summary>{open && <div className="review-extension-body"><div className="review-extension-tools">{['20','60'].map(n => <button key={n} aria-pressed={lookback===n} onClick={() => setLookback(n)}>近{n}日</button>)}<span>不复权日 K · 点击上榜标记核对原因及席位</span></div>{read.busy ? <Loading/> : read.error ? <Failure error={read.error} retry={read.reload}/> : !stock ? <Empty>先选择股票。</Empty> : <>
    {prices.length ? <svg viewBox="0 0 800 235" role="img" aria-label="龙虎榜个股日K与上榜标记" className="review-candles">{[low,high].map((v,i) => <text key={i} x="2" y={y(v)}>{v.toFixed(2)}</text>)}{records.map((r,i) => { const q=r.quote, o=number(q?.open), c=number(q?.close), h=number(q?.high), l=number(q?.low); const valid=o!==null && c!==null && h!==null && l!==null && l>0 && h>=Math.max(o,c) && l<=Math.min(o,c); const color=(c ?? 0)>=(o ?? 0)?'#d83d48':'#239b79'; return <g key={r.trade_day}>{valid && <g><line x1={x(i)} x2={x(i)} y1={y(h)} y2={y(l)} stroke={color}/><rect x={x(i)-3} y={y(Math.max(o,c))} width="6" height={Math.max(1,Math.abs(y(o)-y(c)))} fill={color}/></g>}{!!r.listings?.length && <g role="button" tabIndex={0} aria-label={`上榜日 ${r.trade_day}`} onClick={() => select({ trade_day:r.trade_day,stock,record_key:r.listings![0].record_key,seat:null })} onKeyDown={e => { if(e.key==='Enter' || e.key===' '){e.preventDefault();select({trade_day:r.trade_day,stock,record_key:r.listings![0].record_key,seat:null});} }}><circle cx={x(i)} cy="17" r="7" fill="#c58b25"/><title>{r.trade_day} {r.listings?.map(v=>v.reason).join('；')}</title></g>}{(i===0 || i===records.length-1 || i%Math.max(1,Math.floor(records.length/6))===0) && <text x={x(i)} y="214" textAnchor="middle">{r.trade_day.slice(5)}</text>}</g>; })}</svg> : <Empty>该窗口尚无完整日 K。</Empty>}
    <div className="review-extension-scroll"><table aria-label="K线日与上榜披露"><thead><tr><th>日期</th><th>收盘价</th><th>成交额</th><th>龙虎榜披露</th></tr></thead><tbody>{records.map(r => <tr key={r.trade_day} className={r.trade_day===day?'selected':''}><td>{r.trade_day}</td><td>{text(r.quote?.close)}</td><td>{amount(number(r.quote?.amount) === null ? null : number(r.quote?.amount)!*1000)}</td><td>{r.listings?.length ? r.listings.map((m,i)=><button key={i} onClick={()=>select({trade_day:r.trade_day,stock,record_key:m.record_key,seat:null})}>{m.reason}</button>) : r.listing_state==='complete'?'无披露':'当日榜单未齐备'}</td></tr>)}</tbody></table></div></>}
  </div>}</details>;
}
