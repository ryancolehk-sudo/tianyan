import { useEffect, useRef, useState } from 'react';
import { request } from '../../shared/api';
import { useBoundRead } from './components';
import type { DirectionRankPanel, DirectionRankRow } from './directionBoardTypes';
import './sectorAtlas.css';
import { describeLeaderEvidence, type LeaderEvidence } from './leaderEvidence';

export type AtlasRow = DirectionRankRow & { amount?: string | null; amount_unit?: string };
export type AtlasPanel = DirectionRankPanel & { details?: AtlasRow[]; top_ten_complete?: boolean; coverage?: string; archive_kind?: string; leaders?: Record<string, LeaderEvidence> };
export type Atlas = { days: AtlasPanel[]; limitations: string[]; coverage?: { full_days: number; ranking_days: number; partial_days: number; missing_catalog_observations: number; missing_value_observations: number; daily_days: number } };
function details(p: AtlasPanel) { return p.trade_day === p.requested_day && !['disabled','error','missing'].includes(p.status) ? p.details?.length ? p.details : p.rows as AtlasRow[] : []; }
function preferences() { try { return JSON.parse(localStorage.getItem('tianyan-sector-atlas') || '{}'); } catch { return {}; } }
const palette = ['#e1b86c', '#75a9db', '#bd91d2', '#63b6a0', '#d9956d', '#77c7d1', '#ca91a7', '#b9bd79'];
const ranks = Array.from({ length: 10 }, (_, i) => i + 1);
function numeric(value: unknown) { if (value == null || value === '') return null; const n = Number(value); return Number.isFinite(n) ? n : null; }
function validRows(p: DirectionRankPanel) { return p.trade_day === p.requested_day && !['disabled','error','missing'].includes(p.status) ? p.rows : []; }

export function SectorAtlas({ day, asOf }: { day: string; asOf: string }) {
  const [open, setOpen] = useState(false);
  return <><button className="sector-atlas-entry" onClick={() => setOpen(true)}>板块轮动大图 ↗</button>{open && <AtlasDialog day={day} asOf={asOf} close={() => setOpen(false)}/>}</>;
}
function AtlasDialog({ day, asOf, close }: { day: string; asOf: string; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [source, setSource] = useState(() => preferences().source === 'ths' ? 'ths' : 'kpl'), [range, setRange] = useState<number>(() => { const n=preferences().range; return Number.isInteger(n) && n>=1 && n<=50 ? n : 20; }), [custom, setCustom] = useState(false);
  const [hidden, setHidden] = useState<string[]>([]), [primaryCode, setPrimaryCode] = useState<string>(() => preferences().primary || '');
  const [end, setEnd] = useState(day), [picked, setPicked] = useState<string[]>([]);
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null, overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden'; dialog.current?.showModal();
    return () => { document.body.style.overflow = overflow; opener?.focus(); };
  }, []);
  const params = new URLSearchParams({ trade_day: end, lookback: String(range), source, ...(source === 'kpl' && /^\d{6}$/.test(primaryCode) ? { selected_codes: primaryCode } : {}), ...(asOf ? { as_of_time: asOf } : {}) });
  const read = useBoundRead(`sector-atlas:${params}`, () => request<{ data: Atlas }>('/api/v1/direction-data/sector-atlas?' + params));
  const days = read.data?.data.days ?? [];
  const options = [...new Map(days.flatMap(p => validRows(p)).map(r => [r.code, r])).values()];
  const frequency = (code: string) => days.filter(p => validRows(p).some(r => r.code === code)).length;
  const defaults = [...options].sort((a,b) => frequency(b.code)-frequency(a.code)).slice(0,4).map(r => r.code);
  const selected = [...new Set([...defaults,...picked])];
  const primary = options.some(r => r.code===primaryCode) ? primaryCode : options[0]?.code;
  const reload = useRef(read.reload); reload.current = read.reload;
  useEffect(() => { if(asOf) return; const timer=window.setInterval(() => { if(!document.hidden) reload.current(); },30000); return () => window.clearInterval(timer); }, [asOf]);
  useEffect(() => { try { localStorage.setItem('tianyan-sector-atlas',JSON.stringify({source,range,primary:primary || primaryCode})); } catch { /* Storage is optional. */ } }, [source,range,primary,primaryCode]);
  const width = Math.max(1, days.length) * 98;
  const values = days.map(p => numeric(details(p).find(r => r.code === primary)?.strength));
  const amounts = days.map(p => { const r=details(p).find(r => r.code===primary); return r?.amount_unit === '元' ? numeric(r.amount) : null; });
  const amountMax = Math.max(1,...amounts.map(v=>v ?? 0));
  const max = Math.max(1, ...values.map(n => Math.abs(n ?? 0)));
  const pick = (row: DirectionRankRow) => { setPrimaryCode(row.code); setPicked(previous => [...new Set([...previous,row.code])]); };
  const line = (points: (number | null)[], y: (v: number) => number) => points.map((v, i) => v === null ? '' : `${i === 0 || points[i - 1] === null ? 'M' : 'L'}${i * 98 + 49},${y(v)}`).join(' ');
  return <dialog ref={dialog} className="sector-atlas" aria-label="板块轮动大图" onClose={close}>
    <header className="atlas-tabs"><div role="group" aria-label="板块数据来源">{[['kpl','开盘啦板块'],['ths','同花顺板块']].map(([key,label]) => <button key={key} aria-pressed={source === key} onClick={() => { setSource(key); setPicked([]); setHidden([]); setPrimaryCode(''); }}>{label}</button>)}</div><button aria-label="关闭板块轮动大图" onClick={() => dialog.current?.close()}>×</button></header>

    {read.error && <div role="alert" className="atlas-notice">{read.error.message}<button onClick={read.reload}>重试</button></div>}
    {read.busy && <div role="status" className="atlas-notice">正在读取已保存的板块历史…</div>}
    {source === 'ths' && <div className="atlas-notice">同花顺为盘后日行情，盘中不以昨日日榜充当实时排行。</div>}
    <div className="atlas-scroll" tabIndex={0} aria-label="板块矩阵与图表统一横向滚动">
      <div style={{ width: width + 54, minWidth: '100%' }}>
        <table className="atlas-matrix" aria-label="板块每日前十矩阵" style={{ width: width + 54 }}><colgroup><col style={{ width: 54 }}/>{days.map((_,i) => <col key={i} style={{ width: 98 }}/>)}</colgroup><thead><tr><th>排名</th>{days.map((p,i) => <th key={i} title={`${p.source_label} · 实际资料 ${p.trade_day || '无'} · ${p.archive_kind==='daily' ? '日终归档' : p.archive_kind==='intraday' ? '盘中观察' : '归档时段未知'} · 接收 ${p.received_at || '无'}${p.reason ? ` · ${p.reason}` : ''}`}>{p.requested_day}</th>)}</tr></thead><tbody>{ranks.map(rank => <tr key={rank}><th><b className={`atlas-rank rank-${rank}`}>{rank}</b></th>{days.map((p,i) => { const r = validRows(p).find(row => row.rank === rank), color = r ? selected.indexOf(r.code) : -1; return <td key={i}>{r ? <button style={{ background: color >= 0 ? `${palette[color % palette.length]}25` : undefined, borderColor: r.code === primary ? palette[0] : 'transparent' }} aria-pressed={r.code === primary} onClick={() => pick(r)} title={`${r.name} · ${r.code} · ${p.requested_day} · 强度 ${r.strength ?? '未提供'}`}><span>{r.name}</span><strong>{source === 'ths' ? numeric(r.pct_change) === null ? '—' : `${numeric(r.pct_change)?.toFixed(2)}%` : r.strength ?? '—'}</strong></button> : <span className="atlas-missing" title={p.reason || '当日该名次未保存'}>{rank === 1 ? p.status === 'disabled' ? '未接入' : p.status === 'error' ? '读取异常' : '暂无资料' : '—'}</span>}</td>; })}</tr>)}{source === 'kpl' && <tr className="atlas-leaders"><th>领涨</th>{days.map((p,i) => { const leader=primary ? p.leaders?.[primary] : undefined; const evidence=leader ? describeLeaderEvidence(leader,p.requested_day) : undefined; return <td key={i}>{leader?.rows.map(r=><div className="atlas-leader" key={r.stock_code}><b>{r.label}</b><span title={r.stock_code}>{r.name}</span></div>)}{!leader?.rows.length && <span className="atlas-missing">{leader ? '未返回标记' : '未采集'}</span>}{evidence && <small className="atlas-leader-receipt" title={evidence.title}><time dateTime={evidence.receiptISO}>{evidence.receiptLabel}</time></small>}{evidence?.coverageNote && <small className="atlas-leader-coverage">{evidence.coverageNote}</small>}</td>; })}</tr>}</tbody></table>
        {!days.length && !read.busy && <p className="atlas-notice">该日期暂无可核实的交易日历或榜单。</p>}
        {source === 'kpl' && <><div className="atlas-chart-row"><span className="atlas-axis-label">板块<br/>强度</span><svg width={width} height="135" role="img" aria-label="所选板块原始强度走势"><line x1="0" x2={width} y1="65" y2="65" className="atlas-guide"/><path d={line(values,v => 65 - v / max * 42)} className="atlas-strength-path"/>{days.map((p,i) => { const v = values[i]; return <g key={i}>{v !== null ? <><circle cx={i*98+49} cy={65-v/max*42} r="2"/><text x={i*98+49} y={55-v/max*42} className={v < 0 ? 'atlas-down' : 'atlas-up'}>{v}</text></> : <text x={i*98+49} y="47">—</text>}<text x={i*98+49} y="119">{p.requested_day?.slice(5)}</text></g>; })}</svg></div>
        <div className="atlas-chart-row"><span className="atlas-axis-label">板块<br/>量能</span><svg width={width} height="88" role="img" aria-label="所选板块成交额走势（亿元）"><line x1="0" x2={width} y1="61" y2="61" className="atlas-guide"/>{amounts.map((v,i) => <g key={i}>{v === null ? <text x={i*98+49} y="40">未提供</text> : <><rect x={i*98+40} y={61-v/amountMax*36} width="18" height={v/amountMax*36} fill="#e1b86c"/><text x={i*98+49} y={55-v/amountMax*36}>{(v/100000000).toFixed(0)}亿</text><title>{days[i].requested_day} · {v.toLocaleString()}元</title></>}</g>)}</svg></div>
        </>}<div className="atlas-chart-row atlas-rank-row"><aside className="atlas-ranges">{[10,20,30,50].map(n => <button key={n} aria-pressed={!custom && range === n} onClick={() => {setRange(n);setCustom(false);}}>近{n}日</button>)}<button aria-pressed={custom} onClick={() => setCustom(true)}>自定义</button>{custom && <input aria-label="自定义交易日数" type="number" min="1" max="50" value={range} onChange={e => {const n=Number(e.target.value);if(Number.isInteger(n)&&n>=1&&n<=50)setRange(n);}}/>}</aside><div><div className="atlas-legend">{selected.map((code,i) => { const row=options.find(r=>r.code===code); return row && <button key={code} aria-pressed={!hidden.includes(code)} style={{color:palette[i % palette.length],opacity:hidden.includes(code) ? 0.4 : 1}} onClick={() => setHidden(previous => previous.includes(code) ? previous.filter(c=>c!==code) : [...previous,code])}>{row.name}（{days.filter(p=>validRows(p).some(r=>r.code===code)).length}次上榜）</button>; })}</div><svg width={width} height="300" role="img" aria-label="多板块前十排名轨迹">{[1,5,10].map(r => <line key={r} x1="0" x2={width} y1={18+(r-1)*25} y2={18+(r-1)*25} className="atlas-guide"/>)}{selected.map((code,index) => { if(hidden.includes(code)) return null; const points=days.map(p => { const row=validRows(p).find(r=>r.code===code); return row?.rank ?? (p.trade_day===p.requested_day && p.top_ten_complete === true && p.rows.length===10 && p.status === 'ready' ? 11 : null); });return <g key={code} style={{color:palette[index % palette.length]}}><path d={line(points,v=>18+(v-1)*25)} fill="none" stroke="currentColor" strokeDasharray={index ? '3 3' : undefined}/>{points.map((rank,i)=>rank === null ? null : <g key={i}><title>{days[i].requested_day} · {options.find(r=>r.code===code)?.name} · {rank===11 ? '未进前十' : '第'+rank+'名'}</title><circle cx={i*98+49} cy={18+(rank-1)*25} r="8" fill="currentColor"/><text x={i*98+49} y={22+(rank-1)*25} className="atlas-dot-label">{rank===11 ? '无' : rank}</text></g>)}</g>; })}{days.map((p,i)=><text key={i} x={i*98+49} y="289">{p.requested_day?.slice(5)}</text>)}</svg></div></div>
      </div>
    </div><footer className="atlas-foot"><div className="atlas-toolbar"><span>板块轮动 · {options.find(r => r.code === primary)?.name || '待选板块'}</span><label>截至 <input type="date" aria-label="板块轮动截至日期" max={day} value={end} onChange={e => { if(e.target.value && e.target.value <= day) setEnd(e.target.value); }}/></label><span>点击板块联动 · 点击图例显示／隐藏</span></div>{source === 'kpl' ? '开盘啦原始强度' : '同花顺板块涨幅'} · 已有排行{read.data?.data.coverage?.ranking_days ?? 0}/{days.length}日 · 完整覆盖{read.data?.data.coverage?.full_days ?? 0}日 · 部分覆盖{read.data?.data.coverage?.partial_days ?? 0}日 · 缺行情{read.data?.data.coverage?.missing_catalog_observations ?? 0}项 · 缺数值{read.data?.data.coverage?.missing_value_observations ?? 0}项 · 日终{read.data?.data.coverage?.daily_days ?? 0}日 · 缺失断线，未上榜不补零。<details><summary>来源与数据范围</summary>{read.data?.data.limitations.map(s=><p key={s}>{s}</p>)}<p>日历与归档均受历史截止时间限制；不同来源不合并排名。日期标题可查看接收时间。</p></details></footer>
  </dialog>;
}
