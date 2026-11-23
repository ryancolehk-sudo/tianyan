import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { BarItem, BarSeries, RuntimeSource, SessionResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { Empty, ErrorState } from '../../shared/ui';
import { minuteTimeAxis } from './tradingTime';
import { DailyHistoryControls } from './DailyHistoryControls';
import { MinuteBackfillControls } from './MinuteBackfillControls';
import './minuteChart.css';
import './tradingChart.css';
import { calculateMacd, movingAverage, reliableVwap } from './chartMath';
import { useMarketSession } from '../../shared/marketSession';

type Interval = NonNullable<Parameters<typeof api.marketBars>[2]>['interval'];
const minutePeriods: Record<string, Exclude<Interval, '1d' | undefined>> = { 分时: '1m', '1分钟': '1m', '5分钟': '5m', '15分钟': '15m', '30分钟': '30m', '60分钟': '60m' };
const attemptLabels: Record<string, string> = { complete: '完整返回', partial: '部分返回', empty: '无数据', unavailable: '暂不可用', error: '失败' };
const reasonLabels: Record<string, string> = { minute_source_not_configured: '尚未配置分钟来源', source_disabled: '来源已停用', permission_missing: '来源权限尚未核实', source_not_configured: '来源尚未配置', minute_not_observed: '尚未取得该日该周期分钟', saved_minutes_not_live_session: '保存的历史分钟，不代表当前实时', minute_source_lag: '来源行情时间已超过展示时限', source_clock_semantics_unconfirmed: '源时标语义尚未核实', upstream_rows_rejected: '部分上游数据未通过校验', timeout: '采集超时，等待重试', rate_limited: '上游限流，等待重试', transport_error: '来源连接中断', schema_changed: '来源格式变化，已暂停' };
const stateLabels: Record<string, string> = { available: '已保存分钟行情', stale: '行情已过期', empty: '尚无该日分钟行情', permission_missing: '使用范围未核实', disabled: '来源已停用', unavailable: '行情暂不可用', historical: '历史分钟 · 非当前实时', not_configured: '分钟来源未配置', partial: '分钟行情不完整' };
const timestamp = (value: string | null | undefined) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '未知';
function groupDaily(items: BarItem[], period: string): BarItem[] {
  if (period === '日K') return items;
  const groups = new Map<string, BarItem[]>();
  for (const item of items) {
    const date = item.payload.trade_day;
    if (!date) continue;
    const monday = new Date(date + 'T00:00:00Z');
    monday.setUTCDate(monday.getUTCDate() - (monday.getUTCDay() + 6) % 7);
    const key = period === '月K' ? date.slice(0, 7) : monday.toISOString().slice(0, 10);
    groups.set(key, [...(groups.get(key) ?? []), item]);
  }
  return [...groups.values()].map(rows => {
    const last = rows.at(-1)!;
    return { ...last, period_state: rows.some(row => row.period_state === 'forming') ? 'forming' : last.period_state,
      payload: { ...last.payload, open: rows[0].payload.open,
        high: rows.reduce((best, row) => Number(row.payload.high) > Number(best) ? row.payload.high : best, rows[0].payload.high),
        low: rows.reduce((best, row) => Number(row.payload.low) < Number(best) ? row.payload.low : best, rows[0].payload.low),
        volume_shares: rows.every(row => row.payload.volume_shares !== null) ? rows.reduce((sum, row) => sum + BigInt(row.payload.volume_shares!), 0n).toString() : null,
        amount_cny: null, volume_native: null, amount_native: null } };
  });
}
function candleClock(item: BarItem, minute: boolean) {
  return minute ? item.payload.source_clock_at ?? item.payload.bar_end_at : item.payload.trade_day;
}
function chartPointer(svg: SVGSVGElement, clientX: number, clientY: number) {
  const matrix = svg.getScreenCTM();
  if (matrix) return new DOMPoint(clientX, clientY).matrixTransform(matrix.inverse());
  const rect = svg.getBoundingClientRect();
  return { x: (clientX - rect.left) / rect.width * 840, y: (clientY - rect.top) / rect.height * 300 };
}
function periodState(item: BarItem | undefined, view: BarSeries | undefined) {
  if (item?.period_state === 'forming') return '形成中';
  if (item?.period_state === 'elapsed') return view?.finality === 'confirmed' ? '已确认结束' : '周期已过 · 最终性未确认';
  return '最终性未知';
}

export function HistoryChart({ instrument, period, day: controlledDay, onDayChange }: { instrument?: string; period: string; day?: string; onDayChange?: (day: string) => void }) {
  const panel = useRef<HTMLElement>(null);
  const [fullscreenError, setFullscreenError] = useState('');
  const [adjustment, setAdjustment] = useState<'unadjusted' | 'forward' | 'backward'>('unadjusted');
  // An empty request day stays automatic across polls; never pin it to a response date.
  const [localDay, setLocalDay] = useState('');
  const day = controlledDay ?? localDay;
  const { market } = useMarketSession();
  const setDay = (next: string) => { if (controlledDay === undefined) setLocalDay(next); onDayChange?.(next); };
  const [source, setSource] = useState('');
  const minute = period in minutePeriods;
  const interval = minutePeriods[period] ?? '1d';
  const sources = useRead(useCallback(() => api.dataSources(), []));
  const session = useRead(useCallback(() => minute ? api.session() : Promise.resolve(null), [minute]));
  const sourceItems = sources.data?.data.items ?? [];
  const sourceIncomplete = sourceItems.some(item => !item.provider || !Array.isArray(item.provider.capabilities));
  const choices = sourceItems.filter(item => Array.isArray(item.provider?.capabilities) && item.provider.capabilities.includes('bar.minute') && item.provider.capabilities.includes('bar.' + interval));
  const selectedSource = minute && source && choices.some(item => item.provider.id === source) ? source : '';
  const key = JSON.stringify([instrument, period, minute ? 'unadjusted' : adjustment, day, selectedSource]);
  return <section ref={panel} className="history-chart-panel minute-chart-panel" aria-label="行情图表">
    {minute && (sources.error || sources.refreshError) && <p className="feature-note">来源列表暂不可用，自动选择仍由本机接口复核权限。</p>}
    {minute && sourceIncomplete && <p className="feature-note">来源状态返回不完整，已跳过缺少描述的条目；自动选择仍由本机接口复核。</p>}
    {fullscreenError && <p role="status">{fullscreenError}</p>}
    <button type="button" className="chart-fullscreen" aria-label="全屏图表 / 退出全屏" onClick={async () => { try { if (document.fullscreenElement) await document.exitFullscreen(); else await panel.current?.requestFullscreen(); setFullscreenError(''); } catch { setFullscreenError('浏览器未能打开全屏，可使用浏览器缩放查看。'); } }}>全屏</button>
    <ChartRead key={key} instrument={instrument} period={period} adjustment={minute ? 'unadjusted' : adjustment} day={day} source={selectedSource} session={session.data ?? undefined} runtime={sourceItems} collectionEnabled={sources.data?.data.collection_enabled === true} toolbar={observationDay => <div className="history-chart-toolbar">
      <span>{minute ? (day ? '指定日期分钟 · ' : market?.automatic_live ? '当日自动跟随 · ' : '最近已保存 · ') + period : '日线资料 · ' + period}</span>
      {minute ? <>
        <label>行情日期<input type="date" aria-label="分钟行情日期" value={day || observationDay || ''} onChange={event => setDay(event.target.value)}/></label>
        <button type="button" onClick={() => setDay('')} disabled={!day} title="开盘后自动跟随当前交易日，休市时查看最近已保存日">{market?.automatic_live ? '跟随交易时段' : '最近已保存'}</button>
        <label>来源<select aria-label="分钟行情来源" value={selectedSource} onChange={event => setSource(event.target.value)}>
          <option value="">自动选择可用来源</option>
          {choices.map(item => <option key={item.provider.id} value={item.provider.id}>{item.provider.name}{item.provider.enabled ? '' : '（停用）'}</option>)}
        </select></label>
      </> : <label>复权口径<select aria-label="K线复权口径" value={adjustment} onChange={event => setAdjustment(event.target.value as typeof adjustment)}><option value="unadjusted">不复权</option><option value="forward">前复权</option><option value="backward">后复权</option></select></label>}
    </div>}/>
  </section>;
}

function ChartRead({ instrument, period, adjustment, day, source, session, runtime, collectionEnabled, toolbar }: { instrument?: string; period: string; adjustment: 'unadjusted' | 'forward' | 'backward'; day: string; source: string; session?: SessionResponse; runtime: RuntimeSource[]; collectionEnabled: boolean; toolbar: (observationDay: string | null | undefined) => ReactNode }) {
  const minute = period in minutePeriods;
  const interval = minutePeriods[period] ?? '1d';
  const [hoveredClock, setHoveredClock] = useState<string | null>(null);
  const [before, setBefore] = useState('');
  const [pointerY, setPointerY] = useState<number | null>(null);
  const [windowSize, setWindowSize] = useState<number | null>(null);
  const [offset, setOffset] = useState(0);
  const [showMa, setShowMa] = useState(true);
  const [indicator, setIndicator] = useState('volume');
  const [svg, setSvg] = useState<SVGSVGElement | null>(null);
  const [svgSize, setSvgSize] = useState({width:840,height:420});
  useEffect(() => {
    if (!svg) return;
    const measure = () => { const box = svg.getBoundingClientRect(); if (box.width && box.height) setSvgSize({width:box.width,height:box.height}); };
    measure();
    const observer = new ResizeObserver(measure); observer.observe(svg);
    return () => observer.disconnect();
  },[svg]);
  const pan = useRef<{ x: number; offset: number; width: number } | null>(null);
  const read = useRead(useCallback(() => instrument && (!minute || !day || /^\d{4}-\d{2}-\d{2}$/.test(day))
    ? api.marketBars(instrument, adjustment, minute ? { interval, ...(day ? { day } : {}), ...(source ? { source_id: source } : {}) } : { include_live: !day && !before, limit: 2000, ...(before ? { before } : {}) })
    : Promise.resolve(null), [instrument, adjustment, minute, interval, day, source, before]), { retainOnTransientFailure: true });
  useEffect(() => {
    const refresh = () => { if (document.visibilityState === 'visible') read.reload(); };
    const timer = window.setInterval(refresh, 60_000);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, [read.reload]);
  const view = read.data?.data;
  const matches = view !== undefined && view.instrument.id === instrument && (minute ? view.interval === interval && (!day || view.observation_day === day) && (!source || view.source_id === source) : before ? view.requested_before === before : !view.requested_before);
  const current = matches ? view : undefined;
  const denied = current?.state === 'permission_missing' || current?.state === 'disabled';
  let rows = !read.error && !denied ? current?.items ?? [] : [];
  if (!minute && day) rows = rows.filter(item => Boolean(item.payload.trade_day && item.payload.trade_day <= day));
  if (!minute && !day && current?.current_daily && !read.error && !denied) {
    const live = current.current_daily;
    rows = [...rows.filter(item => item.payload.trade_day !== live.payload.trade_day), live].sort((a, b) => (a.payload.trade_day ?? '').localeCompare(b.payload.trade_day ?? ''));
  }
  const allCandles = minute ? rows.filter(item => Boolean(candleClock(item, true))) : groupDaily(rows, period);
  const size = Math.max(1, Math.min(allCandles.length, windowSize ?? (minute ? allCandles.length : 80)));
  const effectiveOffset = Math.max(0, Math.min(offset, allCandles.length - size));
  const end = allCandles.length - effectiveOffset;
  const start = Math.max(0, end - size);
  const candles = allCandles.slice(start, end);
  const closes = allCandles.map(item => Number(item.payload.close));
  const averages = [5, 10, 20].map(window => movingAverage(closes, window));
  const macd = calculateMacd(closes);
  const vwap = period === '分时' ? reliableVwap(allCandles.map(item => item.payload), current?.state === 'partial' || Boolean(current?.gap_count)) : null;
  const maVisible = showMa && period !== '分时';
  const extraPrices = [...(maVisible ? averages.flatMap(values => values.slice(start, end).filter((value): value is number => value !== null)) : []), ...(vwap?.slice(start, end).filter((value): value is number => value !== null) ?? [])];
  const units = new Set(candles.filter(item => item.payload.volume_shares !== null || item.payload.volume_native !== null && item.payload.volume_native !== undefined).map(item => item.payload.volume_shares !== null ? '股' : '原单位未核'));
  const volumeUnit = units.size === 1 ? [...units][0] : units.size ? '量单位不一致' : '未知';
  const volumes = candles.map(item => units.size > 1 ? null : item.payload.volume_shares !== null ? Number(item.payload.volume_shares) : item.payload.volume_native !== null && item.payload.volume_native !== undefined ? Number(item.payload.volume_native) : null);
  const maxVolume = Math.max(1, ...volumes.filter((value): value is number => value !== null && Number.isFinite(value)));
  const histogram = macd.histogram.slice(start, end);
  const macdMax = Math.max(0.000001, ...[...histogram, ...macd.dif.slice(start, end), ...macd.dea.slice(start, end)].map(value => Math.abs(value ?? 0)));
  const macdY = (value: number) => 451 - value / macdMax * 43;
  const withMacd = indicator === 'macd';
  // Resize the plot independently while keeping axis glyphs at readable CSS pixels.
  const axisText = (x: number, y: number, children: ReactNode, anchor: 'start' | 'middle' | 'end' = 'start', color = '#6f85a8') => <text transform={`translate(${x} ${y}) scale(${840 / svgSize.width} ${(withMacd ? 535 : 420) / svgSize.height})`} x="0" y="0" textAnchor={anchor} fill={color} fontSize="10">{children}</text>;
  function zoom(factor: number) { setWindowSize(Math.max(Math.min(8, allCandles.length), Math.min(allCandles.length, Math.round(size * factor)))); }
  function shift(bars: number) { setOffset(Math.max(0, Math.min(allCandles.length - size, effectiveOffset + bars))); }

  const selectedIndex = hoveredClock === null ? -1 : candles.findIndex(item => candleClock(item, minute) === hoveredClock);
  const activeIndex = selectedIndex >= 0 ? selectedIndex : Math.max(0, candles.length - 1);
  const active = candles[activeIndex];
  const isLive = Boolean(!minute && !day && current?.current_daily && active?.payload.trade_day === current.current_daily.payload.trade_day);
  const highest = Math.max(...candles.map(row => Number(row.payload.high)), ...extraPrices);
  const lowest = Math.min(...candles.map(row => Number(row.payload.low)), ...extraPrices);
  const span = Math.max(highest - lowest, highest * 0.01, 0.01);
  const y = (price: string) => 20 + (highest - Number(price)) / span * 230;
  const timeAxis = useMemo(() => minuteTimeAxis(minute ? allCandles.map(row => candleClock(row, true)!) : [], current?.instrument, current?.observation_day, Number.parseInt(interval)), [minute, allCandles, current?.instrument, current?.observation_day, interval]);
  const axisOrigin = start === 0 ? 0 : timeAxis.positions[start] ?? 0;
  const axisEnd = end === allCandles.length ? timeAxis.spanMinutes : timeAxis.positions[end - 1] ?? timeAxis.spanMinutes;
  const axisSpan = Math.max(Number.parseInt(interval) || 1, axisEnd - axisOrigin);
  const step = 760 / Math.max(1, candles.length);
  const x = (i: number) => minute ? 60 + (timeAxis.positions[start + i] - axisOrigin) / axisSpan * 748 : 60 + (i + 0.5) * step;
  const label = (item: BarItem) => minute ? timestamp(candleClock(item, true)) : item.payload.trade_day;
  const linePaths: string[] = [];
  if (minute && period === '分时') candles.forEach((row, i) => {
    const previous = candles[i - 1];
    const elapsed = previous ? timeAxis.elapsedMinutes[start + i] - timeAxis.elapsedMinutes[start + i - 1] : Infinity;
    const connected = previous && elapsed >= 0 && elapsed <= 1.5;
    const point = `${x(i)},${y(row.payload.close)}`;
    if (connected) linePaths[linePaths.length - 1] += ' L ' + point;
    else linePaths.push('M ' + point);
  });
  return <div className="minute-chart-body" aria-busy={read.busy}>
    {toolbar(denied ? null : current?.observation_day)}
    <div className="minute-chart-status chart-compact-status" role="status"><span>
      {day || current?.observation_day || candles.at(-1)?.payload.trade_day || '日期待核'} · {minute ? stateLabels[current?.state ?? ''] ?? '正在读取分钟行情' : isLive ? (current?.current_daily_state === 'stale' ? '当日日柱已过期' : '当日原生OHLC') + ' · ' + periodState(active, current) : '已保存日线 · 非实时'}
      {minute && ` · 缺段${current?.gap_count ?? '待核'} · 源 ${timestamp(current?.latest_source_time)}`}
      {!minute && isLive && ` · 源 ${timestamp(current?.current_daily?.payload.source_clock_at)}`}
      {current && read.refreshError ? ' · 更新失败，保留已保存行情' : read.refreshing && current ? ' · 刷新中' : current && !read.error ? ' · 缓存已核对' : ''}
    </span><button type="button" onClick={read.reload} disabled={read.busy}>{minute ? '刷新本机分钟缓存' : '刷新已存K线'}</button></div>
    {read.busy && !current ? <p role="status">{minute ? '正在读取分钟行情…' : '正在读取历史K线…'}</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !candles.length ? <div className="market-chart" role="img" aria-label={period + '图表，暂无可用行情'}><Empty title={!instrument ? '请先选择标的' : minute ? stateLabels[current?.state ?? ''] ?? '该标的尚无该日分钟行情' : '该标的尚无已保存日K'}>{minute ? '仅显示本机已取得且允许展示的真实分钟；未覆盖、停牌或无成交情况均不补造K线。可在数据中心核对采集范围和状态。' : '管理员选择股票后可按需获取近五年日线，或继续向前获取更早历史；调用结果在数据中心保留，空窗口不补造。'}</Empty></div> : <>
      <div className="candle-readout" aria-live="polite"><strong>{active && label(active)}</strong><span>开 {active?.payload.open}</span><span>高 {active?.payload.high}</span><span>低 {active?.payload.low}</span><span>收 {active?.payload.close} {active?.payload.price_unit === 'point' ? '点' : '元'}</span>
        <span>量 {active?.payload.volume_shares !== null && active?.payload.volume_shares !== undefined ? active.payload.volume_shares + ' 股' : active?.payload.volume_native !== null && active?.payload.volume_native !== undefined ? active.payload.volume_native + '（原单位未核）' : '未知'}</span>
        <span>额 {active?.payload.amount_cny !== null && active?.payload.amount_cny !== undefined ? active.payload.amount_cny + ' 元' : active?.payload.amount_native !== null && active?.payload.amount_native !== undefined ? active.payload.amount_native + '（原单位未核）' : '未知'}</span>
        {(minute || isLive) && <span className="minute-finality">{periodState(active, current)}</span>}{active?.arrival_kind === 'backfill' && <span>后到补录</span>}
      </div>
      <div className="trading-chart-actions" aria-label="图表操作">
        <button type="button" onClick={() => zoom(0.7)} disabled={size <= Math.min(8, allCandles.length)}>放大</button>
        <button type="button" onClick={() => zoom(1.4)} disabled={size >= allCandles.length}>缩小</button>
        <button type="button" onClick={() => shift(Math.max(1, Math.round(size / 4)))} disabled={start === 0}>向前查看</button>
        <button type="button" onClick={() => shift(-Math.max(1, Math.round(size / 4)))} disabled={effectiveOffset === 0}>向后查看</button>
        <button type="button" onClick={() => { setWindowSize(null); setOffset(0); setHoveredClock(null); }}>重置视图</button>
        {period !== '分时' && <label><input type="checkbox" checked={showMa} onChange={event => setShowMa(event.target.checked)}/>MA5/10/20</label>}
        <label>副图<select aria-label="图表副图" value={indicator} onChange={event => setIndicator(event.target.value)}><option value="volume">成交量</option><option value="macd">成交量 + MACD</option></select></label>
        <span className="chart-window-count">显示 {candles.length} / {allCandles.length} 根 · 拖动平移，Ctrl＋滚轮缩放</span>
      </div>
      <div className="trading-chart-legend">
        {maVisible && [5, 10, 20].map((window, i) => <span className={'chart-ma' + window} key={window}>MA{window} {averages[i][start + activeIndex]?.toFixed(3) ?? '—'}</span>)}
        {period === '分时' && <span>均价 VWAP {vwap?.[start + activeIndex]?.toFixed(3) ?? '暂无可靠量额'}</span>}
        {withMacd && <><span>DIF {macd.dif[start + activeIndex]?.toFixed(3) ?? '—'}</span><span>DEA {macd.dea[start + activeIndex]?.toFixed(3) ?? '—'}</span><span>MACD {macd.histogram[start + activeIndex]?.toFixed(3) ?? '—'}</span></>}
      </div>
      <svg ref={setSvg} className={'trading-chart-svg' + (withMacd ? ' chart-with-macd' : '') + (pan.current ? ' chart-panning' : '')} viewBox={'0 0 840 ' + (withMacd ? 535 : 420)} preserveAspectRatio="none" data-time-axis={minute ? timeAxis.kind : 'saved_periods'} role="img" tabIndex={0} aria-label={period === '分时' ? '真实1分钟收盘价分时图' : minute ? period + '真实蜡烛图' : period + '历史蜡烛图'}
        onWheel={event => { if (event.ctrlKey) { event.preventDefault(); zoom(event.deltaY > 0 ? 1.25 : 0.8); } }}
        onKeyDown={event => { if (event.key === '+' || event.key === '=') { event.preventDefault(); zoom(0.7); } else if (event.key === '-') { event.preventDefault(); zoom(1.4); } else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); shift(event.key === 'ArrowLeft' ? 1 : -1); } }}
        onPointerDown={event => { if (event.button !== 0) return; event.currentTarget.setPointerCapture(event.pointerId); pan.current = { x: event.clientX, offset: effectiveOffset, width: event.currentTarget.getBoundingClientRect().width }; }}
        onPointerUp={() => { pan.current = null; }} onPointerCancel={() => { pan.current = null; }} onLostPointerCapture={() => { pan.current = null; }}
        onPointerMove={event => {
          const rect = event.currentTarget.getBoundingClientRect();
          if (pan.current) { setOffset(Math.max(0, Math.min(allCandles.length - size, pan.current.offset + Math.round((event.clientX - pan.current.x) / pan.current.width * size)))); return; }
          const point = chartPointer(event.currentTarget, event.clientX, event.clientY);
          const pointer = point.x;
          const nearest = candles.reduce((best, row, i) => { const distance = Math.abs(x(i) - pointer), previous = Math.abs(x(best) - pointer); return distance < previous || Math.abs(distance - previous) < 0.001 && Math.abs(y(row.payload.close) - point.y) < Math.abs(y(candles[best].payload.close) - point.y) ? i : best; }, 0);
          setHoveredClock(candleClock(candles[nearest], minute) ?? null);
          setPointerY(Math.max(20, Math.min(250, (event.clientY - rect.top) / rect.height * (withMacd ? 535 : 420))));
        }} onPointerLeave={() => { if (!pan.current) { setHoveredClock(null); setPointerY(null); } }}>
        {[0, 1, 2, 3, 4].map(n => <g key={n}><line x1="60" x2="820" y1={20 + n * 57.5} y2={20 + n * 57.5} stroke="#e2ebfa"/>{axisText(2,24 + n * 57.5,(highest - span * n / 4).toFixed(2))}</g>)}
        {period === '分时' ? <>{linePaths.map((d, i) => <path key={i} d={d} fill="none" stroke="#075fee" strokeWidth="1.8"/>)}{candles.map((row, i) => <circle key={candleClock(row, true)} data-source-clock={candleClock(row, true)} cx={x(i)} cy={y(row.payload.close)} r="1.5" fill="#075fee"/>)}</> : candles.map((item, i) => { const row = item.payload; const color = Number(row.close) >= Number(row.open) ? '#e6404b' : '#029b70'; const width = minute ? Math.min(8, 748 * Number.parseInt(interval) / axisSpan * 0.6) : Math.max(2, step * 0.6); return <g key={candleClock(item, minute)} data-bar-clock={candleClock(item, minute)} data-period-state={item.period_state ?? 'unknown'} opacity={item.period_state === 'forming' ? 0.65 : 1}><line x1={x(i)} x2={x(i)} y1={y(row.high)} y2={y(row.low)} stroke={color}/><rect x={x(i) - width / 2} y={Math.min(y(row.open), y(row.close))} width={Math.max(1, width)} height={Math.max(1, Math.abs(y(row.open) - y(row.close)))} fill={color} stroke={item.period_state === 'unknown' ? '#617da8' : color} strokeDasharray={item.period_state === 'unknown' ? '2 1' : undefined}/></g>; })}
        {maVisible && averages.map((values, index) => {
          const points = values.slice(start, end).flatMap((value, i) => value === null ? [] : [`${x(i)},${y(value.toString())}`]);
          return <polyline key={index} data-indicator={'MA' + [5, 10, 20][index]} points={points.join(' ')} fill="none" stroke={['#a66d13', '#7754ad', '#167b88'][index]} strokeWidth="1.2"/>;
        })}
        {vwap && (() => {
          const segments: string[][] = [];
          vwap.slice(start, end).forEach((value, i) => {
            if (value === null) return;
            const previous = candles[i - 1];
            if (!previous || timeAxis.elapsedMinutes[start + i] - timeAxis.elapsedMinutes[start + i - 1] > 1.5) segments.push([]);
            if (!segments.length) segments.push([]);
            segments.at(-1)!.push(`${x(i)},${y(value.toString())}`);
          });
          return segments.map((points, i) => <polyline key={i} data-indicator="VWAP" points={points.join(' ')} fill="none" stroke="#a66d13" strokeWidth="1.3"/>);
        })()}
        <line x1="60" x2="820" y1="300" y2="300" stroke="#e2ebfa"/>
        {axisText(2,315,'成交量')}{axisText(2,338,volumeUnit)}
        {volumes.map((value, i) => value !== null && Number.isFinite(value) && <rect key={candleClock(candles[i], minute)} data-volume-clock={candleClock(candles[i], minute)} x={x(i) - Math.max(1, Math.min(8, step * 0.3))} y={385 - value / maxVolume * 70} width={Math.max(2, Math.min(16, step * 0.6))} height={value / maxVolume * 70} fill={Number(candles[i].payload.close) >= Number(candles[i].payload.open) ? '#e6404b' : '#029b70'} opacity="0.7"/>)}
        {!volumes.some(value => value !== null) && axisText(400,350,'成交量暂无可用值','middle')}
        {withMacd && <g aria-label="MACD副图"><line x1="60" x2="820" y1="451" y2="451" stroke="#dce5f0"/>{axisText(2,410,'MACD')}
          {histogram.map((value, i) => value !== null && <rect key={i} data-macd-clock={candleClock(candles[i], minute)} x={x(i) - Math.max(1, Math.min(8, step * 0.3))} y={Math.min(451, macdY(value))} width={Math.max(2, Math.min(16, step * 0.6))} height={Math.max(1, Math.abs(macdY(value) - 451))} fill={value >= 0 ? '#e6404b' : '#029b70'}/>)}
          {[macd.dif, macd.dea].map((values, index) => <polyline key={index} points={values.slice(start, end).flatMap((value, i) => value === null ? [] : [`${x(i)},${macdY(value)}`]).join(' ')} fill="none" stroke={index ? '#7754ad' : '#a66d13'} strokeWidth="1.2"/>)}
          {!histogram.some(value => value !== null) && axisText(420,480,'历史不足34根，MACD暂不显示','middle')}
        </g>}
        {active && selectedIndex >= 0 && <g data-testid="chart-crosshair"><line x1={x(activeIndex)} x2={x(activeIndex)} y1="15" y2={withMacd ? 500 : 390} stroke="#7d9bc7" strokeDasharray="4 4"/>{pointerY !== null && <><line x1="60" x2="820" y1={pointerY} y2={pointerY} stroke="#7d9bc7" strokeDasharray="4 4"/>{axisText(62,Math.max(15,pointerY - 4),(highest - (pointerY - 20) / 230 * span).toFixed(3),'start','#182c4f')}</>}</g>}
        {minute && timeAxis.kind === 'ashare_sessions' ? timeAxis.ticks.filter(tick => tick.position >= axisOrigin && tick.position <= axisEnd).map(tick => <g key={tick.position} data-session-tick={tick.label}>{axisText(60 + (tick.position - axisOrigin) / axisSpan * 748, 285, tick.label, tick.position === axisOrigin ? 'start' : tick.position === axisEnd ? 'end' : 'middle')}</g>) : <>{axisText(60,285,candles[0] && label(candles[0]))}{axisText(820,285,candles.at(-1) && label(candles.at(-1)!),'end')}</>}
      </svg>
    </>}
    <details className="chart-data-details"><summary>图表数据详情与历史管理</summary>
    {minute && !read.error && !read.refreshError && current?.backfill_supported && current.source_id && session?.data.user.role === 'admin' && <MinuteBackfillControls key={JSON.stringify([instrument, interval, day || current.observation_day || '', current.source_id])} session={session} sourceId={current.source_id} instrument={instrument!} interval={interval} day={day || current.observation_day || ''} lookback={current.backfill_lookback_days} runtime={runtime.find(item => item.provider?.id === current.source_id)} collectionEnabled={collectionEnabled && !denied} reload={read.reload}/>}
    {!minute && current && instrument && <DailyHistoryControls instrument={instrument} view={current} before={before} busy={read.busy} onComplete={endDay => {
      if (endDay && endDay < new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())) {
        const date = new Date(endDay + 'T00:00:00Z'); date.setUTCDate(date.getUTCDate() + 1); setBefore(date.toISOString().slice(0, 10));
      } else if (endDay) setBefore('');
      setWindowSize(null); setOffset(0); setHoveredClock(null); read.reload();
    }}/>}
    {!minute && current && <div className="daily-history-navigation" aria-label="日K历史浏览">
      <p className="feature-note">已保存范围：{current.coverage_start_day ?? rows[0]?.payload.trade_day ?? '尚无'} 至 {current.coverage_end_day ?? rows.at(-1)?.payload.trade_day ?? '尚无'}；本页 {rows.length} 根日K。范围内可能有缺口。</p>
      <div className="daily-history-actions"><button type="button" disabled={!current.has_more || read.busy || !rows[0]?.payload.trade_day} onClick={() => { setBefore(rows[0].payload.trade_day!); setWindowSize(null); setOffset(0); setHoveredClock(null); }}>载入更早缓存</button><button type="button" disabled={!before || read.busy} onClick={() => { setBefore(''); setWindowSize(null); setOffset(0); setHoveredClock(null); }}>回到最近日K</button></div>
      {allCandles.length > 100 && <><label className="daily-history-position">K线历史位置<input aria-label="K线历史位置" type="range" min={Math.min(size, allCandles.length)} max={allCandles.length} value={end} onChange={event => { setOffset(allCandles.length - Number(event.target.value)); setHoveredClock(null); }}/></label><div className="daily-history-actions"><button type="button" disabled={start === 0} onClick={() => { shift(80); setHoveredClock(null); }}>前一段K线</button><span>{candles[0]?.payload.trade_day} 至 {candles.at(-1)?.payload.trade_day}</span><button type="button" disabled={end === allCandles.length} onClick={() => { shift(-80); setHoveredClock(null); }}>后一段K线</button></div></>}
    </div>}


      {minute && <p className="feature-note minute-axis-note">{timeAxis.kind === 'ashare_sessions' ? '沪深股票交易时段轴：午休11:30–13:00压缩显示；跨午休按交易分钟衔接；未补柱，交易时段内缺失仍断线。开收盘刻度不代表已有该时点数据。' : '按原始源时标显示：证券交易规则未核实，或数据跨日、含常规时段外时点；不擅自压缩休市或删除观察。'}</p>}

    {minute && <p className="feature-note" role="status">{day ? `指定日期：${day}；该日没有保存数据时保持空白，不借用其他日期。` : denied ? '来源已停用或使用范围未核实，无法展示保存日期。' : current?.observation_day ? `最近已保存 · ${current.observation_day} · ${current.state === 'historical' ? '历史分钟非当前实时。' : '请按来源状态和源时点核对时效。'}` : read.busy ? '最近已保存：正在核对该标的、来源与周期的实际保存日期。' : read.error ? '最近已保存日期暂无法读取，请重试或在数据中心核对。' : '该标的、来源与周期尚无已保存分钟日期。'}</p>}

    {!minute && isLive && !read.error && !read.refreshError && <p className="feature-note" role="status">当日日线时效：{current?.current_daily_state === 'stale' ? '已过期，保留原快照供查看' : current?.current_daily_state === 'available' ? '按来源实际时点显示' : '待核实'}；源时点：{timestamp(current?.current_daily?.payload.source_clock_at)}；距源时点：{current?.current_daily_age_seconds === null || current?.current_daily_age_seconds === undefined ? '未知' : current.current_daily_age_seconds + ' 秒'}。采集成功不保证持续实时。</p>}
    {read.busy && current && <p className="feature-note" role="status">正在核对本机缓存，暂显示上次读取的数据；时间与来源保持原值。</p>}
      {withMacd && <p className="chart-calculation-note">MACD(12,26,9)：EMA采用首N根收盘价均值初始化，柱值=2×(DIF−DEA)。指标由本次已保存行情计算。</p>}
      {period === '分时' && !vwap && <p className="chart-calculation-note">均价线暂不绘制：需从开盘起连续的同日分钟及已核实股／元量额。原单位未核、缺段或部分数据不计算VWAP。</p>}
      <label className="candle-date-picker">查看周期<select aria-label="查看K线日期" value={activeIndex} onChange={event => setHoveredClock(candleClock(candles[Number(event.target.value)], minute) ?? null)}>{candles.map((row, i) => <option key={candleClock(row, minute)} value={i}>{label(row)}</option>)}</select></label>
      <p className="feature-note">来源：{isLive ? current?.current_daily_source_name ?? current?.source_name : current?.source_name}。{minute ? '采用供应商实际分钟OHLC；分时仅连接相邻交易分钟收盘价，缺段不补线。' : period === '日K' ? isLive ? '当前日柱来自实时日线原生OHLC；' + periodState(active, current) + '；历史保存版本保持不变。' : '来自实际保存日线，非实时行情。' : '根据已保存日K聚合，首尾周期可能不完整。含当日柱时保留其形成或最终性未知状态。'}历史补采不代表当时已知；不将日线拆成分钟。</p>
      {active && <details className="minute-chart-evidence"><summary>查看当前柱来源版本</summary><p>服务器接收：{timestamp(active.received_at)}</p><p>版本：<code>{active.id}</code></p><p>校验：<code>{active.content_hash}</code></p></details>}
    {minute && current && <div className="minute-chart-observation" aria-label="分钟行情状态"><span>保存日期：{current.observation_day ?? '未知'}</span><span>来源：{current.source_name ?? '未选择'}</span><span>最后成功采集：{timestamp(current.last_success_at)}</span><span>最新源时点：{timestamp(current.latest_source_time)}</span><span>源时延：{current.age_seconds === null || current.age_seconds === undefined ? '未知' : current.age_seconds + ' 秒'}</span>{current.gap_count !== undefined && <span>已检测缺段：{current.gap_count}（不补造）</span>}<span>时标语义：{current.timestamp_semantics === 'end' ? '周期结束' : current.timestamp_semantics === 'start' ? '周期开始' : '未核实；不推定周期结束'}</span><span>最终性：{current.finality === 'confirmed' ? '来源已确认' : '未确认；尾柱不标已完成'}</span>{current.last_attempt && <span>最近尝试：{timestamp(current.last_attempt.received_at)} · {attemptLabels[current.last_attempt.status] ?? '待核查'}</span>}{current.reason && <span>状态原因：{reasonLabels[current.reason] ?? '请在数据中心核对该次采集详情'}</span>}</div>}
    {minute && <p className="feature-note">页面每60秒检查本机缓存，切到后台暂停；刷新不会直接请求供应商。免费个人研究来源没有实时性或完整性保证，停牌、休市与缺段须按来源状态判断。</p>}
    </details>
  </div>;
}
