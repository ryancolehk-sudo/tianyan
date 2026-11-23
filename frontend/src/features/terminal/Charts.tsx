import { useId, useLayoutEffect, useRef, useState } from 'react';
import type { Sector, Value, WorkbenchData } from './types';
import { dateTime, money, number, shortDay, time } from './data';
import { observationGapSeconds } from './marketTime';
import { InteractiveChart, useChartDimensions } from './ChartInspection';

export const sectorColors = ['#63afff', '#f7d76b', '#f35c83', '#af7be8', '#ffae54', '#25cfcd', '#71cf88', '#b0a8e6', '#80a4c9', '#e49648'];
const W = 720, H = 300, L = 48, R = 28, T = 22, B = 40;
type Point = { x: number; y: number | null };
function path(points: Point[]) {
  let drawing = false;
  return points.map(point => {
    if (point.y == null || !Number.isFinite(point.y)) { drawing = false; return ''; }
    const command = drawing ? 'L' : 'M'; drawing = true;
    return `${command}${point.x.toFixed(2)},${point.y.toFixed(2)}`;
  }).join(' ');
}
function EmptyPlot({ title, detail }: { title: string; detail?: string }) {
  return <div className="ty-chart-empty"><span className="ty-empty-orbit" aria-hidden="true">◌</span><strong>{title}</strong>{detail && <small>{detail}</small>}</div>;
}
function Grid({ low = 0, high, format = value => number(value), ticks, zero = false, width = W, height = H, top = T, bottom = B, count = 5 }: {
  low?: number; high: number; format?: (value: number) => string; ticks: { x: number; label: string }[]; zero?: boolean; width?: number; height?: number; top?: number; bottom?: number; count?: number;
}) {
  return <g className="ty-chart-grid">
    {Array.from({ length: count }, (_, i) => {
      const value = low + (high - low) * i / (count - 1); const y = top + (height - top - bottom) * (high - value) / Math.max(1, high - low);
      return <g key={i}><line x1={L} y1={y} x2={width - R} y2={y} strokeDasharray={zero && value === 0 ? undefined : '2 4'} className={zero && value === 0 ? 'ty-zero-line' : undefined}/><text x={L - 8} y={y + 4} textAnchor="end">{format(value)}</text></g>;
    })}
    {ticks.map(tick => <g key={tick.label}><line x1={tick.x} y1={top} x2={tick.x} y2={height - bottom} strokeDasharray="2 4"/><text x={tick.x} y={height - bottom + Math.min(25, bottom - 5)} textAnchor="middle">{tick.label}</text></g>)}
    <line x1={L} y1={top} x2={L} y2={height - bottom} className="ty-axis"/><line x1={L} y1={height - bottom} x2={width - R} y2={height - bottom} className="ty-axis"/>
  </g>;
}
function marketMinute(value: string) {
  const short = time(value); const parts = /^(\d{2}):(\d{2})/.exec(short);
  if (!parts) return null;
  const minute = Number(parts[1]) * 60 + Number(parts[2]);
  if (minute < 570 || minute > 900 || minute > 690 && minute < 780) return null;
  return minute <= 690 ? minute - 570 : minute - 660;
}
function fundMinute(value: string) {
  const minute = marketMinute(value);
  if (minute != null) return minute;
  const match = /^(\d{2}):(\d{2})/.exec(time(value));
  const clock = match ? Number(match[1]) * 60 + Number(match[2]) : null;
  // The lunch boundary is compressed, but each real noon observation retains
  // its own timestamp in the point title. No morning samples are invented.
  return clock != null && clock > 690 && clock < 780 ? 120 : null;
}
export function BreadthChart({ points, busy, sourceName }: { points: WorkbenchData['breadth']['points']; busy?: boolean; sourceName?: string }) {
  const id = useId().replace(/:/g, '');
  const svg = useRef<SVGSVGElement>(null);
  const [{ width, height }, setSize] = useState({ width: W, height: H });
  useLayoutEffect(() => {
    const element = svg.current;
    if (!element) return;
    const measure = () => {
      const rect = element.getBoundingClientRect();
      if (rect.width <= L + R || rect.height <= T + B) return;
      // Match SVG units to rendered pixels, including the CSS-controlled height.
      // This fills a wide panel without scaling the labels or stretching glyphs.
      setSize(previous => Math.abs(previous.width - rect.width) < .5 && Math.abs(previous.height - rect.height) < .5
        ? previous : { width: rect.width, height: rect.height });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const minuteAt = (value: string) => {
    const normal = marketMinute(value);
    if (normal != null) return normal;
    const match = /^09:(2[5-9])/.exec(time(value));
    return match ? Number(match[1]) - 30 : null;
  };
  const usable = points.map(p => ({ ...p, minute: minuteAt(p.time) })).filter((p): p is typeof p & { minute: number } => p.minute != null).sort((a, b) => a.minute - b.minute || a.time.localeCompare(b.time));
  const maximum = Math.max(20, ...usable.flatMap(p => [p.up ?? 0, p.down ?? 0]));
  const high = Math.ceil(maximum / 20) * 20;
  const start = usable.some(p => p.minute < 0) ? -5 : 0;
  const x = (minute: number) => L + (width - L - R) * (minute - start) / (240 - start);
  const top=height<160?12:T, bottom=height<160?26:B;
  const y = (value: number) => top + (height - top - bottom) * (high - value) / high;
  const series = (key: 'up' | 'down'): Point[] => usable.flatMap((p, index) => {
    const previous = usable[index - 1];
    const current = { x: x(p.minute), y: p[key] == null ? null : y(p[key]) };
    const gap = previous ? observationGapSeconds(previous.time, p.time) : 0;
    // The axis compresses lunch. Real missing trading minutes remain gaps.
    return previous && gap > 360
      ? [{ x: current.x, y: null }, current] : [current];
  });
  const hasData = usable.some(p => p.up != null || p.down != null);
  const inspectionSamples = hasData ? usable.map(point => ({
    key: point.time, label: dateTime(point.time), x: x(point.minute),
    rows: (['up', 'down'] as const).map(key => ({ label: key === 'up' ? '涨停' : '跌停',
      value: point[key] == null ? '缺测' : `${number(point[key])} 家`,
      color: key === 'up' ? 'var(--ui-up, #ff6c79)' : 'var(--ui-down, #47d5ad)',
      y: point[key] == null ? null : y(point[key]),
    })),
  })) : [];
  return <div className="ty-chart ty-breadth-chart">
    <InteractiveChart svgRef={svg} width={width} height={height} samples={inspectionSamples} scope={`breadth:${usable[0]?.time.slice(0, 10) ?? ''}:${sourceName ?? ''}`}
      snapDistance={(width - L - R) * 3 / (240 - start)} subject={sourceName}
      label={hasData ? sourceName ? `${sourceName}涨跌停家数历史走势` : '涨停与跌停家数的实际采样走势' : '涨跌停家数走势，暂无采样'}>
      <defs><linearGradient id={'up-' + id} x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor="var(--ui-up)" stopOpacity=".22"/><stop offset="100%" stopColor="var(--ui-up)" stopOpacity=".015"/></linearGradient></defs>
      <Grid high={high} width={width} height={height} top={top} bottom={bottom} count={height<160?3:5} ticks={[{ x: L, label: start < 0 ? '09:25' : '09:30' }, ...(width >= 520 ? [{ x: x(60), label: '10:30' }] : []), { x: x(120), label: '11:30 / 13:00' }, ...(width >= 520 ? [{ x: x(180), label: '14:00' }] : []), { x: x(240), label: '15:00' }]}/>
      {(['up', 'down'] as const).map(key => {
        const values = series(key); const valid = values.filter(p => p.y != null); const last = valid.at(-1);
        return <g key={key} className={key === 'up' ? 'ty-chart-up' : 'ty-chart-down'}>
          {key === 'up' && valid.length > 1 && !values.some(p => p.y == null) && <path d={`${path(values)} L${last!.x},${height - bottom} L${valid[0].x},${height - bottom} Z`} fill={`url(#up-${id})`} stroke="none"/>}
          <path d={path(values)} fill="none" stroke="currentColor" strokeWidth="2.4"/>
          {last && <><circle cx={last.x} cy={last.y!} r="4" fill="currentColor"/><text x={last.x + 10 > width - 45 ? width - 8 : last.x + 10} textAnchor={last.x + 10 > width - 45 ? 'end' : 'start'} y={last.y! + (key === 'up' ? -10 : 18)} fill="currentColor" className="ty-chart-end">{number(usable.filter(p => p[key] != null).at(-1)?.[key])}</text></>}
        </g>;
      })}
    </InteractiveChart>
    {!hasData && <EmptyPlot title={busy ? '正在读取市场采样' : '尚无当日走势采样'} detail="从真实快照积累，历史缺测保留为空"/>}
  </div>;
}

export function FundsChart({ sectors, history, selected, intraday, intradayPoints = [], comparison = false, metric = '主力净额', timeBasis = 'source_time', missingTitle = '等待当日盘中资金', responsive = false }: {
  sectors: Sector[]; history: WorkbenchData['funds_history']; selected: string; intraday: boolean;
  intradayPoints?: { time: string; sector_code: string; value: Value }[]; missingTitle?: string; comparison?: boolean; metric?: string; timeBasis?: 'source_time' | 'received_at'; responsive?: boolean;
}) {
  const {ref,width:W,height:H}=useChartDimensions(responsive);
  const plotW=W-L-R;
  const scale=(value:number,low:number,high:number)=>T+(H-T-B)*(high-value)/Math.max(1,high-low);
  const days = [...new Set(history.map(p => p.day))].sort().slice(-10);
  const selectedSector = sectors.find(sector => sector.code === selected);
  const visible = [...sectors.slice(0, 10), ...(selectedSector && !sectors.slice(0, 10).some(sector => sector.code === selected) ? [selectedSector] : [])];
  const measured = intradayPoints.map(point => ({ ...point, minute: fundMinute(point.time) }))
    .filter((point): point is typeof point & { minute: number } => point.minute != null)
    // Native UTC samples and recovered +08:00 samples share one real clock.
    .sort((a, b) => Date.parse(a.time) - Date.parse(b.time));
  const values = (intraday ? measured : history.filter(p => days.includes(p.day))).filter(p => visible.some(sector => sector.code === p.sector_code)).map(p => p.value).filter((value): value is number => value != null);
  const extent = Math.max(100_000_000, ...values.map(Math.abs));
  const ceiling = Math.ceil(extent / 100_000_000 / 5) * 5 * 100_000_000;
  const x = (index: number) => L + plotW * index / Math.max(1, days.length - 1);
  const hasData = values.length > 0;
  const tickDays = days.length ? days.filter((_, i) => i === 0 || i === days.length - 1 || i % 3 === 0) : [];
  const inspectedSector = selectedSector ?? visible[0];
  const inspectedSamples = intraday ? measured.filter(point => point.sector_code === inspectedSector?.code).map(point => ({
    key: point.time, label: (timeBasis === 'received_at' ? '接收 ' : '') + dateTime(point.time), x: L + plotW * point.minute / 240, value: point.value,
  })) : days.map((day, index) => ({ key: day, label: day, x: x(index), value: history.find(point => point.day === day && point.sector_code === inspectedSector?.code)?.value ?? null }));
  const inspectionSamples = hasData ? inspectedSamples.map(point => ({
    key: point.key, label: point.label, x: point.x,
    rows: [{ label: metric, value: point.value == null ? '缺测' : `${money(point.value)} 亿元`, color: 'var(--ui-accent-bright, #f5d99a)', y: point.value == null ? null : scale(point.value, -ceiling, ceiling) }],
  })) : [];
  return <div className="ty-chart ty-funds-chart"><InteractiveChart svgRef={ref} width={W} height={H} samples={inspectionSamples}
    scope={`funds:${intraday ? 'intraday' : 'daily'}:${inspectedSector?.code ?? ''}:${intraday ? inspectedSamples[0]?.key.slice(0, 10) : days.at(-1)}`}
    snapDistance={intraday ? plotW * 3 / 240 : undefined} subject={inspectedSector?.name}
    label={intraday ? `当日盘中板块${metric}${timeBasis === 'received_at' ? '接收' : '实际'}采样，单位亿元` : `近十个已采集交易日板块${metric}，单位亿元`}>
    <Grid width={W} height={H} low={-ceiling} high={ceiling} format={value => `${number(value / 100_000_000, 0)}亿`} ticks={intraday ? [{ x: L, label: '09:30' }, { x: L + plotW / 2, label: '11:30 / 13:00' }, { x: W - R, label: '15:00' }] : tickDays.map(day => ({ x: x(days.indexOf(day)), label: shortDay(day) }))} zero/>
    {hasData && visible.map((sector, index) => {
      const isSelected = sector.code === selected;
      const samples = measured.filter(point => point.sector_code === sector.code);
      const points: Point[] = intraday ? samples.flatMap((point, i) => {
        const previous = samples[i - 1];
        const gap = previous ? timeBasis === 'received_at' ? (Date.parse(point.time) - Date.parse(previous.time)) / 1000 : observationGapSeconds(previous.time, point.time) : 0;
        const current = { x: L + plotW * point.minute / 240, y: point.value == null ? null : scale(point.value, -ceiling, ceiling) };
        // Missing trading minutes stay discontinuous; the axis compresses lunch.
        return previous && (!Number.isFinite(gap) || gap > (timeBasis === 'received_at' ? 600 : 360) || gap < 0) ? [{ x: current.x, y: null }, current] : [current];
      }) : days.map((day, i) => {
        const value = history.find(p => p.day === day && p.sector_code === sector.code)?.value;
        return { x: x(i), y: value == null ? null : scale(value, -ceiling, ceiling) };
      });
      const valid = points.filter(p => p.y != null);
      const last = valid.at(-1);
      const isolated = points.filter((point, i) => point.y != null
        && (i === 0 || points[i - 1].y == null) && (i === points.length - 1 || points[i + 1].y == null));
      return <g key={sector.code} data-sector-code={sector.code} data-selected={isSelected || undefined} opacity={isSelected ? 1 : comparison ? .85 : .4}>
        <path d={path(points)} stroke={isSelected ? 'var(--ui-accent-bright, #f5d99a)' : sectorColors[index % sectorColors.length]} strokeWidth={isSelected ? 2.4 : 1.4} fill="none"/>
        {intraday ? samples.filter(point => point.value != null).map((point, i) => <circle className="ty-fund-sample" opacity="0" key={`${point.time}:${i}`} cx={L + plotW * point.minute / 240} cy={scale(point.value!, -ceiling, ceiling)} r="4" fill={isSelected ? '#f7d76b' : sectorColors[index % sectorColors.length]}><title>{`${sector.name} · ${timeBasis === 'received_at' ? '接收时点' : '实际时点'} ${time(point.time)} · ${money(point.value)}亿元`}</title></circle>)
          : points.filter(p => p.y != null).map((p, i) => <circle className="ty-fund-sample" opacity="0" key={i} cx={p.x} cy={p.y!} r="4" fill={isSelected ? '#f7d76b' : sectorColors[index % sectorColors.length]}/>) }
        {last && (isSelected || valid.length === 1) && <circle className="ty-fund-last" pointerEvents="none" cx={last.x} cy={last.y!} r={isSelected ? 3.5 : 2.5} fill={isSelected ? '#f7d76b' : sectorColors[index % sectorColors.length]}/>}
        {isolated.filter(point => point !== last || !isSelected && valid.length > 1).map((point, i) => <circle key={`isolated-${i}`} className="ty-fund-isolated" pointerEvents="none" cx={point.x} cy={point.y!} r="2.5" fill={isSelected ? '#f7d76b' : sectorColors[index % sectorColors.length]}/>)}
      </g>;
    })}
  </InteractiveChart>{!hasData && <EmptyPlot title={intraday ? missingTitle : '等待盘后资金历史'} detail={intraday ? timeBasis === 'received_at' ? '按接收时间记录；缺测不补零，不代表精确分钟行情' : '首批真实采样到达后自动展示；缺测不补零，5分钟前值积累后计算变化' : '仅绘制真实交易日净额，不补造分钟数据'}/>}</div>;
}

export function SealChart({ points }: { points: WorkbenchData['stock_seals'] }) {
  const usable = points.map(p => ({ ...p, value: p.value == null ? null : p.value / 100_000_000 }));
  const maximum = Math.max(.5, ...usable.map(p => p.value ?? 0));
  const high = Math.ceil(maximum * 2) / 2;
  const width = 720, height = 158, left = 38, bottom = 124, top = 12;
  const x = (i: number) => left + (width - left - 32) * i / Math.max(1, usable.length - 1);
  const y = (v: number) => bottom - (bottom - top) * v / high;
  const hasData = usable.some(p => p.value != null);
  const ticks = new Set(Array.from({ length: Math.min(5, usable.length) }, (_, i) => Math.round(i * (usable.length - 1) / Math.max(1, Math.min(5, usable.length) - 1))));
  return <div className="ty-chart ty-seal-chart"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="实际封单金额采样，缺测处断开">
    {[0, .5, 1].map(f => <g key={f} className="ty-chart-grid"><line x1={left} y1={y(high * f)} x2={width - 20} y2={y(high * f)} strokeDasharray="2 4"/><text x={left - 8} y={y(high * f) + 4} textAnchor="end">{number(high * f, 1)}</text></g>)}
    <path d={path(usable.map((p, i) => ({ x: x(i), y: p.value == null ? null : y(p.value) })))} fill="none" stroke="#f7d76b" strokeWidth="2.3"/>
    {usable.map((p, i) => <g key={`${p.time}-${i}`}>
      {ticks.has(i) && <text className="ty-tick" x={x(i)} y={148} textAnchor="middle">{time(p.time)}</text>}
      {hasData && (p.value != null ? <><circle cx={x(i)} cy={y(p.value)} r={usable.length > 30 ? 2 : 4} fill="#f7d76b"><title>{time(p.time)} · {number(p.value, 2)}亿元</title></circle>{ticks.has(i) && <text className="ty-value-label" x={x(i)} y={y(p.value) - 10} textAnchor="middle">{number(p.value, 2)}</text>}</> : ticks.has(i) && <><circle cx={x(i)} cy={bottom - 4} r="3" fill="#0c1921" stroke="#678097"/><text className="ty-tick" x={x(i)} y={bottom - 18} textAnchor="middle">缺测</text></>)}
    </g>)}
  </svg>{!hasData && <EmptyPlot title="未保存盘口封单曲线" detail="当前来源缺少完整盘口；日榜数值不补绘盘中曲线"/>}</div>;
}

export function AuctionChart({ entries, nodeMode = false, compact = false }: { entries: { day: string; values: (Value | undefined)[] }[]; nodeMode?: boolean; compact?: boolean }) {
  const labels = nodeMode ? ['09:15', '09:20', '09:25'] : entries.map(e => shortDay(e.day));
  const series = nodeMode ? entries.slice(0, 2) : [{ day: '', values: entries.map(e => e.values[0]) }];
  const values = series.flatMap(e => e.values).filter((value): value is number => value != null);
  const maximum = Math.max(100_000_000, ...values);
  const high = Math.ceil(maximum / 100_000_000) * 100_000_000;
  const w = compact ? 320 : 650, h = compact ? 142 : 174, l = 32, b = h - 35, t = 16;
  const groupWidth = (w - l - 20) / Math.max(1, labels.length);
  const y = (value: number) => b - (b - t) * value / high;
  return <div className="ty-chart ty-auction-chart"><svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label={nodeMode ? '三个节点的封单金额对比' : '实际竞价成交金额跨日比较，单位亿元'}>
    {[0, .5, 1].map(f => <g key={f} className="ty-chart-grid"><line x1={l} x2={w - 10} y1={y(high * f)} y2={y(high * f)} strokeDasharray="2 4"/><text x={l - 7} y={y(high * f) + 4} textAnchor="end">{number(high * f / 100_000_000, 1)}</text></g>)}
    {labels.map((label, i) => <g key={label}><text x={l + groupWidth * (i + .5)} y={h - 10} textAnchor="middle" className="ty-tick">{label}</text>{series.map((entry, j) => {
      const value = entry.values[i]; if (value == null) return null;
      const barW = Math.min(60, groupWidth / (series.length + 1));
      const x = l + groupWidth * (i + .5) + (j - series.length / 2) * (barW + 5);
      return <g key={j}><rect x={x} y={y(value)} width={barW} height={b - y(value)} fill={j === 0 ? '#d9af58' : '#648096'} rx="1"/><text x={x + barW / 2} y={y(value) - 7} textAnchor="middle" className="ty-value-label">{money(value)}</text></g>;
    })}</g>)}
  </svg>{!values.length && <EmptyPlot title={nodeMode ? '三个节点封单金额待接入' : '暂无该股竞价成交记录'} detail={nodeMode ? '竞价成交额与委托封单分开统计' : '选择有竞价成交结果的股票查看跨日比较'}/>}</div>;
}
