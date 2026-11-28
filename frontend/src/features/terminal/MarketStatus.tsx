import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { request } from '../../shared/api';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { Panel, type PageProps } from './Components';
import './marketStatus.css';

type Value = number | null;
type Point = {
  day: string; minute: string | null; median_pct: Value; n: Value; advancing: Value; falling: Value;
  sealed: Value; touched: Value; cohort_n: Value; pool_source: string | null;
  raw: [Value, Value, Value, Value, Value];
};
type Report = { day: string; basis: 'minute' | 'close'; current: Point | null; previous_day: string | null; history: (Point | null)[] };
type Frame = { query: string; result: Report };
type Envelope = { data: Report };
const axes = ['上涨广度', '成交活跃', '封板率', '昨涨停反馈', '大跌占比'];
const dash = '—';

function pct(value: Value | undefined, signed = false) {
  if (value == null || !Number.isFinite(value)) return dash;
  const rounded = Number(value.toFixed(2));
  return (rounded < 0 ? '−' : signed && rounded > 0 ? '+' : '') + Math.abs(rounded).toFixed(2) + '%';
}
function amount(value: Value | undefined) {
  if (value == null) return dash;
  return value >= 1e12 ? (value / 1e12).toFixed(2) + ' 万亿' : (value / 1e8).toFixed(2) + ' 亿';
}
function relative(current: Value | undefined, prior: Value | undefined) {
  return current != null && prior != null && prior > 0 ? 100 * (current / prior - 1) : null;
}
function paired(report: Report | undefined) {
  const previous = report?.history.at(-1);
  return previous && previous.day === report?.previous_day && previous.minute === report.current?.minute ? previous : null;
}
function headline(point: Point | null, previous: Point | null) {
  if (!point || point.median_pct == null) return '有效报价不足，暂无法判断整体涨跌。';
  const mid = point.median_pct, last = previous?.median_pct, up = point.raw[0], oldUp = previous?.raw[0];
  const majorityDeep = point.falling != null && point.n != null && point.n > 0 && point.falling * 2 > point.n;
  let lead = mid < 0 ? '多数个股下跌' : mid > 0 ? '多数个股上涨' : '涨跌幅中位数持平';
  if (last != null && up != null && oldUp != null) {
    if (mid < last && last < 0 && up < oldUp) lead = majorityDeep ? '普跌加深' : '跌势加深';
    if (mid < 0 && mid > last && up > oldUp) lead = '跌势缓和';
    if (mid > 0 && mid > last && up > oldUp) lead = '上涨面扩大';
    if (mid > 0 && mid < last && up < oldUp) lead = '上涨面收窄';
  }
  const parts = [lead];
  if (majorityDeep) parts.push('过半个股跌幅达 5%');
  const feedback = point.raw[3];
  if (feedback != null && Math.abs(feedback) >= .005)
    parts.push(feedback < 0 ? '昨涨停股平均回落' : '昨涨停股平均上涨');
  return parts.join('，') + '。';
}
function dayClock(iso: string) {
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? new Date(ms + 8 * 3600000).toISOString().slice(0, 16) : '';
}
function rank(value: Value, sample: number[]) {
  if (value == null || sample.length !== 20) return null;
  return 5 * sample.reduce((n, v) => n + (v < value ? 1 : v === value ? .5 : 0), 0);
}
function chartXY(axis: number, value: number) {
  const angle = (axis * 72 - 90) * Math.PI / 180;
  return [130 + Math.cos(angle) * value * .73, 113 + Math.sin(angle) * value * .73];
}
function vertices(values: number[]) {
  return values.map((v, i) => chartXY(i, v).join(',')).join(' ');
}
const axisLabels = [[130, 17], [222, 87], [191, 204], [69, 204], [38, 87]];

function OverviewRadar({ point, previous, live }: { point: Point | null; previous: Point | null; live: boolean }) {
  const [axis, select] = useState(4);
  const prior = live ? '昨同刻' : '昨收';
  const names = ['上涨占比', '成交额', '封板率', '昨涨停反馈', '大跌占比'];
  function radius(row: Point | null, i: number) {
    const v = row?.raw[i], base = previous?.raw[1];
    if (v == null) return null;
    if (i === 1) return base != null && base > 0 ? Math.min(100, 50 * v / base) : null;
    const scaled = i === 3 ? 50 + 5 * v : v;
    return Math.min(100, Math.max(0, scaled));
  }
  function label(row: Point | null, i: number) {
    return i === 1 ? amount(row?.raw[i]) : pct(row?.raw[i], i === 3);
  }
  function profile(row: Point | null, comparison: boolean) {
    const points = names.map((_, i) => {
      const r = radius(row, i);
      return r == null ? null : chartXY(i, r);
    });
    const color = comparison ? '#697f90' : '#ad7013';
    return <g data-series={comparison ? 'previous' : 'current'} fill={color}>
      {points.every(p => p !== null) && <polygon points={points.map(p => p!.join(',')).join(' ')} fill={comparison ? 'none' : '#dca642'} fillOpacity=".17"/>}
      {points.map((p, i) => {
        if (!p) return null;
        const next = points[(i+1) % points.length];
        return <g key={i}>{next && <line x1={p[0]} y1={p[1]} x2={next[0]} y2={next[1]} stroke={color} strokeWidth={comparison ? 1.7 : 2.4} strokeDasharray={comparison ? '4 3' : undefined}/>}
          <circle cx={p[0]} cy={p[1]} r={comparison ? 2 : 3}/></g>;
      })}
    </g>;
  }
  const scale = axis === 1 ? '半径从 0 到昨日成交额的 2 倍' : axis === 3 ? '半径从 −10% 到 +10%，中圈为 0%' : '半径从 0% 到 100%';
  return <div className="ms-inline ms-chart">
    <div className="ms-key"><span>当前</span><span>{prior}</span><b>中位 {pct(point?.median_pct,true)}</b></div>
    <svg viewBox="0 0 260 226" role="group" aria-label="首页盘面结构雷达">
      {[50,100].map(v => <polygon key={v} points={vertices(Array(5).fill(v))} fill="none" stroke="#c5d2dc"/>)}
      {names.map((name,i) => { const [x,y] = chartXY(i,100); return <line key={name} x1="130" y1="113" x2={x} y2={y} stroke="#d2dde4"/>; })}
      {profile(previous, true)}{profile(point, false)}
      {names.map((name,i) => {
        const [x,y] = axisLabels[i];
        return <g key={name} role="button" tabIndex={0} aria-pressed={axis === i} aria-label={name+' '+label(point,i)} onClick={() => select(i)}
          onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(i); } }}>
          <rect x={x-37} y={y-13} width="74" height="33" rx="4" fill={axis === i ? '#f9f1df' : 'transparent'}/>
          <text x={x} y={y}>{name}</text><text x={x} y={y+15} className="ms-rank">{label(point,i)}</text>
        </g>;
      })}
    </svg>
    <div className="ms-axis-note" aria-live="polite"><b>{names[axis]}</b> · {prior} {label(previous,axis)}<small>{scale}</small></div>
  </div>;
}

function StatusDetails({ frame, user, close, returnFocus }: { frame: Frame; user: string; close: () => void; returnFocus: HTMLButtonElement | null }) {
  const box = useRef<HTMLDialogElement>(null), titleId = useId();
  useEffect(() => { const d = box.current; d?.showModal(); return () => d?.close(); }, []);
  function dismiss() {
    box.current?.close();
    close();
    returnFocus?.focus({ preventScroll: true });
  }
  const path = frame.query + '&window=20';
  const read = useCachedLoader(() => request<Envelope>(path), user + ':' + path, '盘面历史对照');
  const report = read.data?.data ?? frame.result, p = report.current;
  const raw = p?.raw ?? [null, null, null, null, null];
  const history = (read.data?.data?.history ?? []).slice(-20);
  const samples = axes.map((_, i) => history.flatMap(row => {
    const v = row?.raw[i];
    return row && row.minute === p?.minute && row.day < report.day && v != null ? [v] : [];
  }));
  const scores = raw.map((v, i) => rank(v, samples[i]));
  const amounts = [...samples[1]].sort((a, b) => a - b);
  const benchmark = amounts.length === 20 ? (amounts[9] + amounts[10]) / 2 : null;
  const [axis, select] = useState(4);
  const scope = report.basis === 'close' ? '收盘' : '同一分钟';
  const explanations = [
    '沪深 A 股（含 ST）中，上涨 ' + (p?.advancing ?? dash) + ' 家 / 有效报价 ' + (p?.n ?? dash) + ' 家',
    '20 日' + scope + '成交额中位数 ' + amount(benchmark) + '，相差 ' + pct(relative(raw[1], benchmark), true),
    (p?.pool_source ?? '来源待核') + ' · 封住 ' + (p?.sealed ?? dash) + ' 家 / 触板 ' + (p?.touched ?? dash) + ' 家',
    '固定昨日收盘涨停池 ' + (p?.cohort_n ?? dash) + ' 只，按相对昨收的涨跌幅等权计算。',
    '沪深 A 股（含 ST）中，跌幅 ≥5% 有 ' + (p?.falling ?? dash) + ' 家 / 有效报价 ' + (p?.n ?? dash) + ' 家。分位越高，大跌股占比越高。',
  ];
  return <dialog ref={box} className="ms-dialog" aria-labelledby={titleId} onCancel={e => { e.preventDefault(); dismiss(); }}>
    <header><div><h2 id={titleId}>盘面详情</h2><time>{report.day} · {p?.minute ?? (report.basis === 'close' ? '收盘' : '盘中')}</time></div>
      <button type="button" onClick={dismiss} aria-label="关闭盘面详情">关闭</button></header>
    <div className="ms-history">
      <div className="ms-chart">
        <div className="ms-key"><span>当前分位</span><span>20 日中位</span></div>
        <svg viewBox="0 0 260 226" role="group" aria-label="五项指标正向分位雷达">
          {[25, 50, 75, 100].map(v => <polygon key={v} points={vertices(Array(5).fill(v))} fill="none" stroke={v === 50 ? '#80919a' : '#d1dce2'} strokeDasharray={v === 50 ? '4 3' : undefined}/>)}
          {axes.map((name, i) => { const [x, y] = chartXY(i, 100); return <line key={name} x1="130" y1="113" x2={x} y2={y} stroke="#d1dce2"/>; })}
          {scores.every((v): v is number => v !== null) && <polygon data-profile="complete" points={vertices(scores)} stroke="#a37319" strokeWidth="2" fill="#c68b22" fillOpacity=".15"/>}
          {axes.map((name, i) => {
            const [x, y] = axisLabels[i], v = scores[i], pt = v == null ? null : chartXY(i, v);
            return <g key={name} role="button" tabIndex={0} aria-label={'查看' + name} aria-pressed={axis === i}
              onClick={() => select(i)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(i); } }}>
              {pt && <><line x1={x} y1={y} x2={pt[0]} y2={pt[1]} stroke="transparent" strokeWidth="14"/><circle cx={pt[0]} cy={pt[1]} r="3" fill="#a37319"/></>}
              <rect x={x-32} y={y-12} width="64" height="33" fill="transparent" rx="4"/>
              <text x={x} y={y}>{name}</text><text x={x} y={y+15} className="ms-rank">{v == null ? dash : 'P'+v}</text>
            </g>;
          })}
        </svg>
      </div>
      <table><caption>近 20 个交易日{scope}对照</caption><thead><tr><th scope="col">指标</th><th scope="col">原值</th><th scope="col">分位</th></tr></thead>
        <tbody>{axes.map((name, i) => <tr key={name} data-selected={axis === i || undefined}>
          <th scope="row"><button onClick={() => select(i)} aria-pressed={axis === i}>{name}</button></th>
          <td>{i === 1 ? amount(raw[i]) : pct(raw[i])}</td><td>{scores[i] == null ? dash : 'P'+scores[i]}</td>
        </tr>)}</tbody></table>
    </div>
    <p className="ms-explain" aria-live="polite"><b>{axes[axis]}</b>{explanations[axis]}
      <small>{read.error ? <>历史读取失败 <button onClick={read.reload}>重试</button></> : read.busy ? '正在核对历史样本…' : '可比样本 '+samples[axis].length+'/20'}</small></p>
  </dialog>;
}

function StatusCard({ day, user, query, live }: { day: string; user: string; query: string; live: boolean }) {
  const read = useCachedLoader(() => request<Envelope>(query), user + ':' + query, '盘面状态');
  const loaded = read.data?.data;
  const detailsButton = useRef<HTMLButtonElement>(null);
  const [retained, retain] = useState<Frame | null>(null), [details, open] = useState<Frame | null>(null);
  useEffect(() => {
    if (read.error) { retain(null); open(null); }
    else if (loaded && loaded.day === day) retain({ query, result: loaded });
  }, [query, loaded, read.error, day]);
  useEffect(() => {
    if (live || query.includes('as_of_time=') || dayClock(new Date().toISOString()).slice(0,10) !== day) return;
    const timer = window.setInterval(() => { if (!document.hidden) read.reload(); }, 300000);
    return () => clearInterval(timer);
  }, [day, query, live, read.reload]);
  const frame = read.error ? null : loaded?.day === day ? { query, result: loaded } : read.busy ? retained : null;
  const report = frame?.result, p = report?.current ?? null, prev = paired(report);
  const prevLabel = live ? '昨同刻' : '昨收', prevTitle = (report?.previous_day ?? '日期待核') + ' · ' + (live ? p?.minute ?? '同刻' : '收盘');
  const diffAmount = relative(p?.raw[1], prev?.raw[1]);
  const increase = p?.raw[4] != null && prev?.raw[4] != null ? p.raw[4]-prev.raw[4] : null;
  const previousValue = (v: Value | undefined) => prevLabel + ' ' + (v == null ? '缺测' : pct(v));
  const summary = read.error ? '行情读取失败' : !frame && read.busy ? '正在读取盘面…' : headline(p, prev);
  const stamp = p?.minute ?? (live ? '盘中' : '收盘');
  return <Panel title="盘面结构雷达" subtitle={<time title={day+' · '+stamp}>{day.slice(5)} · {stamp}</time>}
    className="ty-overview-boards ty-market-status" action={read.error
      ? <button type="button" className="ms-more" onClick={read.reload}>重试</button>
      : <button ref={detailsButton} type="button" className="ms-more" disabled={!frame} onClick={() => open(frame)} aria-label="查看盘面详情" aria-haspopup="dialog">详情 ›</button>}>
    <div className="ms-content"><OverviewRadar point={p} previous={prev} live={live}/>
    <p className="ms-summary">{p?.median_pct != null && <>中位涨跌幅 {pct(p.median_pct,true)} · </>}{summary}</p>
    <dl className="ms-facts">
      <div><dt>中位涨跌幅</dt><dd>{pct(p?.median_pct, true)}</dd>
        <dd className="ms-sub" title={prevTitle}>{previousValue(prev?.median_pct)}</dd>
        <dd className="ms-sub">上涨 {pct(p?.raw[0])}<span>{previousValue(prev?.raw[0])}</span></dd></div>
      <div><dt>大跌占比 <small>≥5%</small></dt><dd>{pct(p?.raw[4])}</dd>
        <dd className="ms-sub" title={prevTitle}>{previousValue(prev?.raw[4])}{increase != null && <span>{increase > 0 ? '+' : increase < 0 ? '−' : ''}{Math.abs(increase).toFixed(2)} 个百分点</span>}</dd></div>
      <div><dt>成交额</dt><dd>{amount(p?.raw[1])}</dd>
        <dd className="ms-sub" title={prevTitle}>{diffAmount == null ? (prev?.raw[1] === 0 ? '昨日基数为零' : prevLabel+'对照缺失') : '较'+prevLabel+' '+pct(diffAmount, true)}</dd></div>
      <div><dt>昨涨停反馈</dt><dd>{pct(p?.raw[3], true)}</dd>
        <dd className="ms-sub" title={(p?.pool_source ?? '来源待核')+' · 昨日涨停池 '+(p?.cohort_n ?? dash)+' 只'}>
          {p?.touched == null || p.sealed == null ? '封板样本不足' : '封板 '+p.sealed+'/'+p.touched+' 家'}{p?.raw[2] != null && <span>{pct(p.raw[2])}</span>}</dd></div>
    </dl></div>
    {details && <StatusDetails frame={details} user={user} close={() => open(null)} returnFocus={detailsButton.current}/>}
  </Panel>;
}

export function MarketStatus({data, asOfTime, session}: PageProps) {
  const [tick, update] = useState(() => Date.now());
  useEffect(() => {
    if (asOfTime) return;
    const timer = window.setInterval(() => update(Date.now()), 30000);
    return () => clearInterval(timer);
  }, [asOfTime]);
  const day = data.trade_day, user = session.data.user.id;
  const now = asOfTime || new Date(tick).toISOString(), clock = dayClock(now);
  const live = clock.slice(0,10) === day && clock.slice(11) < '15:05';
  const receipt = data.breadth?.source_received_at;
  const sample = asOfTime || (receipt && dayClock(receipt) === clock && Date.parse(receipt) <= Date.now()
    ? receipt : new Date(tick).toISOString());
  const minuteKey = dayClock(sample);
  // Use one observed receipt per market minute. Explicit replay cutoffs retain their seconds.
  const anchor = useMemo(() => sample, [day, minuteKey, asOfTime, live]);
  const params = new URLSearchParams({ trade_day: day, ...(live ? { sample_at: anchor } : {}), ...(asOfTime ? { as_of_time: asOfTime } : {}) });
  return <StatusCard key={[user,day,live,asOfTime].join('|')} day={day} user={user} query={'/api/v1/market-status?'+params} live={live}/>;
}
