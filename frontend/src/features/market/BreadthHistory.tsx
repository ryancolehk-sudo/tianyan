import { useCallback, useEffect, useRef, useState } from 'react';
import type { BreadthPoint, RuntimeSource } from '../../generated/schema';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { BreadthDailyReference } from './BreadthDailyReference';
import './BreadthHistory.css';
import './homepageNotes.css';

const clock = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
const dayFormat = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' });
const series = [{ key: 'limit_up', label: '涨停', color: '#e6404b' }, { key: 'limit_down', label: '跌停', color: '#029b70' }] as const;
const loadDailyReference = () => api.datasets('tushare.limit_list_d');
type Line = typeof series[number];
type HistorySource = 'auto' | 'tushare_market_rt' | 'tencent_market_rt' | 'ashare_breadth';
function minute(point: BreadthPoint) {
  const [hour, min, second] = clock.format(new Date(point.received_at)).split(':').map(Number);
  return hour * 60 + min + second / 60;
}
function valid(value: number | null) { return value !== null && Number.isInteger(value) && value >= 0 && value <= 999999; }
function scaleStep(maximum: number) {
  const raw = Math.max(1, maximum) / 4;
  const base = 10 ** Math.floor(Math.log10(raw));
  return Math.max(1, [1, 2, 5, 10].find(n => n * base >= raw)! * base);
}

function collectionTime(value: string) {
  const date = new Date(value);
  return (dayFormat.format(date) === dayFormat.format(new Date()) ? '' : dayFormat.format(date) + ' ') + clock.format(date);
}

function BreadthSyncStatus({ source, busy, error, historical }: {
  source: RuntimeSource | undefined; busy: boolean; error: boolean; historical: boolean;
}) {
  if (historical) return <div className="breadth-history-sync" role="status"><strong>历史采集记录</strong><span>仅显示当日已保存时点，未采集的时段不补值。</span></div>;
  if (error || !source) return <div className="breadth-history-sync" role="status"><strong>{error ? '采集状态读取失败' : busy ? '正在核对采集状态…' : '数据源尚未配置'}</strong><span>曲线仅显示已保存记录。</span></div>;
  const limited = source.last_attempt?.reason === 'rate_limited';
  const off = !source.collection_enabled || source.collection_state === 'disabled';
  const denied = ['permission_missing', 'credentials_missing'].includes(source.collection_state);
  const rechecking = source.paused_reason === 'schema_changed';
  const delayed = source.last_success_at && Date.now() - new Date(source.last_success_at).valueOf() > Math.max(120, source.interval_seconds * 2) * 1000;
  const failed = source.collection_state === 'error' || source.collection_state === 'unavailable';
  const warning = !!(limited || off || denied || source.paused_reason || delayed || failed);
  const label = off ? '自动采集未开启' : denied ? '数据源权限或凭据不可用' : rechecking ? '响应校验未通过，交易时段自动复核' : source.paused_reason ? '数据源采集已暂停' : limited ? '上次采集被限流，保留旧数据' : failed ? '采集失败，保留旧数据' : delayed ? '采集延迟，保留旧数据' : '按采集计划更新';
  const interval = source.interval_seconds % 60 === 0 ? `${source.interval_seconds / 60} 分钟` : `${source.interval_seconds} 秒`;
  return <div className={'breadth-history-sync' + (warning ? ' is-warning' : '')} role="status">
    <div><strong>{label}</strong><span>最后成功采集：{source.last_success_at ? collectionTime(source.last_success_at) : '尚无成功记录'}</span>
      {limited && !off && !denied && !source.paused_reason && <span>计划重试：{collectionTime(source.next_run_at)}</span>}
      {rechecking && !off && !denied && <span>下次复核计划：{collectionTime(source.next_run_at)} · 间隔至少15分钟，仅已核交易日时段</span>}
      <a href="/data?view=sources">查看数据源状态</a></div>
    <span>采集间隔 {interval} · 图表每分钟检查已保存记录。{warning ? ' 未采集的时段保留空缺，不延长曲线或补值。' : ''}</span>
  </div>;
}

function BreadthPanel({ line, points, active, showCursor, onChoose }: {
  line: Line; points: BreadthPoint[]; active: BreadthPoint | undefined; showCursor: boolean; onChoose: (id: string | null) => void;
}) {
  const [width, setWidth] = useState(500);
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!container.current) return;
    const observer = new ResizeObserver(entries => setWidth(Math.max(240, entries[0].contentRect.width)));
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  const left = 42, right = width - 14, top = 24, bottom = 166, height = 208;
  const x = (point: BreadthPoint) => {
    const time = minute(point);
    const elapsed = time <= 690 ? time - 570 : time - 660;
    return left + elapsed / 240 * (right - left);
  };
  const step = scaleStep(Math.max(0, ...points.map(point => valid(point[line.key]) ? point[line.key]! : 0)));
  const y = (value: number) => bottom - value / (step * 4) * (bottom - top);
  const choose = (clientX: number, svg: SVGSVGElement) => {
    if (!points.length) return;
    const rect = svg.getBoundingClientRect();
    const target = (clientX - rect.left) / rect.width * width;
    onChoose(points.reduce((best, point) => Math.abs(x(point) - target) <= Math.abs(x(best) - target) ? point : best).id);
  };
  let drawing = false;
  const path = points.map(point => {
    if (!valid(point[line.key])) { drawing = false; return ''; }
    const segment = `${drawing ? 'L' : 'M'}${x(point).toFixed(2)},${y(point[line.key]!).toFixed(2)}`;
    drawing = true;
    return segment;
  }).join(' ');
  return <section className="breadth-history-panel" aria-label={line.label + '数量走势'}>
    <div className="breadth-history-panel-heading">
      <h3><i style={{ background: line.color }}/>{line.label}数量</h3>
      <span style={{ color: line.color }}><strong data-testid={'breadth-history-' + line.key}>{active?.[line.key] ?? '—'}</strong> 家</span>
    </div>
    <div className="breadth-history-plot" ref={container}>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={line.label + '数量分时折线图'} onPointerMove={event => choose(event.clientX, event.currentTarget)} onPointerDown={event => choose(event.clientX, event.currentTarget)} onPointerLeave={event => { if (event.pointerType === 'mouse') onChoose(null); }}>
        <text x="8" y="12" className="breadth-axis">家</text>
        {[0, 1, 2, 3, 4].map(n => <g key={n}><line x1={left} x2={right} y1={y(n * step)} y2={y(n * step)} className="breadth-grid"/><text x={left - 10} y={y(n * step) + 4} textAnchor="end" className="breadth-axis breadth-value-axis">{n * step}</text></g>)}
        {['09:30', '10:30', '13:00', '14:00', '15:00'].map((label, i) => <text key={label} x={left + (right - left) * i / 4} y="190" textAnchor={i === 0 ? 'start' : i === 4 ? 'end' : 'middle'} className="breadth-axis breadth-time-axis">{label}</text>)}
        <path data-series={line.key} d={path} fill="none" stroke={line.color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round"/>
        {points.map((point, i) => valid(point[line.key]) && (points.length <= 80 || i === points.length - 1) ? <circle key={point.id} cx={x(point)} cy={y(point[line.key]!)} r="2.3" fill={line.color}/> : null)}
        {showCursor && active && <g><line x1={x(active)} x2={x(active)} y1={top} y2={bottom} stroke="#91a7c7" strokeDasharray="4 4"/>{valid(active[line.key]) && <circle cx={x(active)} cy={y(active[line.key]!)} r="4" fill={line.color} stroke="white" strokeWidth="2"/>}</g>}
      </svg>
      {showCursor && active && <div className="breadth-history-tooltip" style={{ left: Math.max(0, Math.min(width - 154, x(active) - 77)) }}><strong>{clock.format(new Date(active.received_at))} 采集</strong><span>{line.label}<b style={{ color: line.color }}>{active[line.key] === null ? '未知' : active[line.key] + ' 家'}</b></span></div>}
    </div>
  </section>;
}

export function BreadthHistory() {
  const [day, setDay] = useState<string | undefined>();
  const [historySource, setHistorySource] = useState<HistorySource>(() => {
    const saved = new URLSearchParams(location.search).get('breadth_source');
    return saved === 'ashare_breadth' || saved === 'tushare_market_rt' || saved === 'tencent_market_rt' ? saved : 'auto';
  });
  const [activeId, setActiveId] = useState<string | null>(null);
  const read = useRead(useCallback(async () => ({ source: historySource, day, response: await api.breadthHistory(day, historySource) }), [day, historySource]));
  const sourceRead = useRead(api.dataSources);
  const dailyRead = useRead(loadDailyReference);
  const reload = useCallback(() => { read.reload(); sourceRead.reload(); dailyRead.reload(); }, [read.reload, sourceRead.reload, dailyRead.reload]);
  useEffect(() => {
    const refresh = () => { if (document.visibilityState === 'visible') reload(); };
    const timer = window.setInterval(refresh, 60_000);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, [reload]);
  const view = !read.error && read.data?.source === historySource && read.data?.day === day && (!day || read.data.response.data.observation_day === day) ? read.data.response.data : undefined;
  const points = view?.items ?? [];
  const selectedIndex = activeId ? points.findIndex(point => point.id === activeId) : -1;
  const activeIndex = selectedIndex < 0 ? points.length - 1 : selectedIndex;
  const active = points[activeIndex];
  const showCursor = activeId !== null && !!active;
  const selectedDay = day ?? view?.observation_day ?? dayFormat.format(new Date());
  const paid = view?.source_family === 'tushare_market_rt' || view?.source_family === 'tencent_market_rt';
  const supplier = view?.source_family === 'tencent_market_rt' ? '腾讯' : 'Tushare';
  const source = sourceRead.data?.data.items.find(item => item.family === (view?.source_family ?? 'ashare_breadth'));
  return <section className="breadth-history" aria-label="涨跌停数量走势">
    <div className="breadth-history-heading">
      <div><h2>涨跌停数量走势</h2><span className="breadth-history-basis">{paid ? `${supplier} · 当日原生限制价比较 · 本机采集时间` : '免费备用参考 · 本机采集时间 · 行情时点未知'}</span></div>
      <div className="breadth-history-controls"><label>来源<select aria-label="涨跌停走势来源" value={historySource} onChange={event => {
        const selected = event.target.value as HistorySource; setHistorySource(selected); setActiveId(null);
        const url = new URL(location.href); if (selected === 'auto') url.searchParams.delete('breadth_source'); else url.searchParams.set('breadth_source', selected);
        history.replaceState(history.state, '', url);
      }}><option value="auto">自动优先 Tushare</option><option value="tushare_market_rt">Tushare 独立历史</option><option value="tencent_market_rt">腾讯独立历史</option><option value="ashare_breadth">旧来源历史（含上午记录）</option></select></label><label>日期<input type="date" aria-label="涨跌停走势日期" value={selectedDay} onChange={event => { setDay(event.target.value || undefined); setActiveId(null); }}/></label><button type="button" onClick={reload} disabled={read.busy} aria-label="刷新涨跌停走势">{read.busy ? '读取中…' : '刷新图表'}</button></div>
    </div>
    <details className="homepage-notes"><summary>统计口径</summary><p>上午已采记录可在“旧来源历史”查看。来源切换只影响本图；各来源独立保存，未采集或不可比时点不补零、不拼线。</p></details>
    <div className="breadth-history-readout" aria-live="polite">
      <span>{active ? <><strong>{clock.format(new Date(active.received_at))}</strong> <span>采集</span></> : '交易时段 09:30–11:30 / 13:00–15:00'}</span>
      <span>各图独立刻度</span>
    </div>
    {!points.length && <div className="breadth-history-empty" role="status">{read.error ? <><strong>走势读取失败</strong><button type="button" onClick={read.reload}>重试读取走势</button></> : read.busy ? <strong>正在读取涨跌停走势…</strong> : <><strong>该日尚无交易时段采集记录</strong><span>采集后自动显示曲线，未保存的时段不补值。</span></>}</div>}
    <div className="breadth-history-panels">
      {series.map(line => <BreadthPanel key={line.key} line={line} points={points} active={active} showCursor={showCursor} onChoose={setActiveId}/>)}
    </div>
    {paid && active && <div className="breadth-history-sync" role="status"><strong>{supplier} 本批可比 {active.comparable ?? '—'} / 返回 {active.returned ?? '—'}；未判定 {active.unknown ?? '—'}（旧日行情 {active.old_day ?? '—'}）</strong><span>{view?.scope}</span><span>可比样本行情时段：{active.source_time_min ? clock.format(new Date(active.source_time_min)) : '未知'}–{active.source_time_max ? clock.format(new Date(active.source_time_max)) : '未知'}；其中两分钟内时点 {active.fresh ?? '—'}。旧免费数据不接入此曲线，不补造过去时点。</span></div>}
    <BreadthDailyReference items={dailyRead.error ? [] : dailyRead.data?.data.items ?? []} day={selectedDay} busy={dailyRead.busy} failed={!!dailyRead.error}/>
    <BreadthSyncStatus source={source} busy={sourceRead.busy} error={!!sourceRead.error} historical={selectedDay !== dayFormat.format(new Date())}/>
    <div className="breadth-history-footer"><span>{view?.source_name ? `${view.source_name} · ${points.length} 个已保存时点` : '仅显示已保存的涨跌停数量'}{view?.truncated ? ' · 仅展示最近 2000 个时点' : ''}</span>{points.length > 0 && <label>查看时点<input type="range" aria-label="查看涨跌停采集时点" min="0" max={points.length - 1} value={Math.max(0, activeIndex)} onChange={event => setActiveId(points[Number(event.target.value)].id)}/></label>}</div>
  </section>;
}
