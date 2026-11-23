import { useState } from 'react';
import type { CSSProperties } from 'react';
import { number } from './model';
import { blue, charts, Detail, gold, green, Plot, red, show } from './postcloseVisuals';
import type { Point, Postclose } from './postcloseVisuals';

type Metric = Postclose['metric_definitions'][number];
const percentKeys = new Set(['first_promotion', 'relay_promotion', 'seal_rate', 'broken_rate', 'yesterday_up', 'yesterday_relay']);
function color(key: string, value: unknown) {
  if (key === 'main_net' || key.startsWith('yesterday_')) return (number(value) ?? 0) < 0 ? green : red;
  if (['limit_down', 'declining', 'auction_down'].includes(key)) return green;
  if (['height', 'limit_up', 'advancing', 'up_amount', 'auction_up'].includes(key)) return red;
  if (['gem_height', 'first_promotion'].includes(key)) return blue;
  return gold;
}
function formatted(metric: Metric, value: unknown) { return show(value, metric.unit === '元', metric.unit === '%' || percentKeys.has(metric.key)); }

function History({ metric, points }: { metric: Metric; points: Point[] }) {
  const values = points.map(p => number(p.values[metric.key]));
  const valid = values.filter((v): v is number => v !== null);
  if (!valid.length) return <span className="pcv-metric-missing">尚无已保存数据</span>;
  const lo = Math.min(0, ...valid), hi = metric.key === 'emotion' ? 100 : Math.max(1, ...valid);
  const span = hi - lo || 1, x = (i: number) => 5 + (points.length <= 1 ? 95 : i * 190 / (points.length - 1)), y = (v: number) => 40 - (v - lo) / span * 34;
  let path = '', connected = false;
  values.forEach((value, i) => { if (value === null) { connected = false; return; } path += `${connected ? 'L' : 'M'}${x(i)},${y(value)} `; connected = true; });
  const bars = metric.unit === '元';
  return <svg viewBox="0 0 200 46" preserveAspectRatio="none" aria-hidden="true"><line x1="4" x2="196" y1={y(0)} y2={y(0)} className="pcv-gridline"/>{!bars && <path d={path} stroke={color(metric.key, values.at(-1))} strokeWidth="1.6" fill="none" vectorEffect="non-scaling-stroke"/>}{values.map((value, i) => value === null ? null : bars ? <rect key={i} x={x(i) - Math.min(3, 65 / points.length)} y={Math.min(y(value), y(0))} height={Math.max(1, Math.abs(y(value) - y(0)))} width={Math.min(6, 130 / points.length)} fill={color(metric.key, value)}/> : <circle key={i} cx={x(i)} cy={y(value)} r="1.7" fill={color(metric.key, value)}/>)}</svg>;
}

type Slice = { name: string; value: number | null; color: string; metric: string };
function firstBoard(point: Point | undefined): number | null {
  const up = number(point?.values.limit_up), relay = number(point?.values.consecutive);
  return up !== null && relay !== null && Number.isInteger(up) && Number.isInteger(relay) && up >= relay && relay >= 0 ? up - relay : null;
}
function Donut({ title, slices, open }: { title: string; slices: Slice[]; open: (key: string) => void }) {
  const known = slices.every(s => s.value !== null && s.value >= 0), total = slices.reduce((n, s) => n + (s.value ?? 0), 0), circumference = 2 * Math.PI * 35;
  let offset = 0;
  return <section className="pcv-donut" aria-label={title}><h3>{title}</h3><div className="pcv-donut-body"><svg viewBox="0 0 104 104" role="img" aria-label={`${title}：${known ? slices.map(s => `${s.name}${s.value}只`).join('，') : '同日构成资料不完整'}`}><circle cx="52" cy="52" r="35" stroke="#263c47" strokeWidth="11" fill="none"/>{known && total > 0 && slices.map(s => { const length = s.value! / total * circumference; const start = offset; offset += length; return <circle key={s.name} cx="52" cy="52" r="35" stroke={s.color} strokeWidth="11" fill="none" strokeDasharray={`${length} ${circumference - length}`} strokeDashoffset={-start} transform="rotate(-90 52 52)"/>; })}<text x="52" y="58" textAnchor="middle">{known ? total.toLocaleString() : '—'}</text></svg><div className="pcv-donut-legend">{slices.map(s => <button key={s.name} onClick={() => open(s.metric)}><i style={{ background: s.color }}/>{s.name}<b style={{ color: s.color }}>{show(s.value)}</b></button>)}</div></div></section>;
}

export function PostcloseAnalytics({ data }: { data: Postclose }) {
  const [focused, setFocused] = useState<string | null>(null);
  const latest = data.records.at(-1), values = latest?.values ?? {};
  const relay = number(values.consecutive), first = firstBoard(latest);
  if (focused) {
    const metric = focused === 'first_board' ? { key: focused, label: '首板走势', unit: '只' } : data.metric_definitions.find(m => m.key === focused)!;
    const group = charts.find(spec => spec.lines.some(line => line.key === focused));
    const spec = group ?? { title: metric.label, lines: [{ key: metric.key, label: focused === 'first_board' ? '首板' : metric.label, color: gold, money: metric.unit === '元' }] };
    const points = focused === 'first_board' ? data.records.map(point => ({ ...point, values: { ...point.values, first_board: firstBoard(point) } })) : data.records;
    return <div className="pcv-expanded"><div className="pcv-focus-controls"><button onClick={() => setFocused(null)}>全部指标</button><span>{data.records[0]?.trade_day} — {data.trade_day}</span></div><Plot spec={spec} points={points} detailed/></div>;
  }
  return <section className="pcv-analytics" aria-label="统计可视化"><div className="pcv-summary-rings"><Donut title="上涨 / 下跌" slices={[{ name: '上涨', value: number(values.advancing), color: red, metric: 'advancing' }, { name: '下跌', value: number(values.declining), color: green, metric: 'declining' }]} open={setFocused}/><Donut title="涨停分层" slices={[{ name: '首板', value: first, color: gold, metric: 'first_board' }, { name: '连板', value: relay, color: blue, metric: 'consecutive' }]} open={setFocused}/></div><div className="pcv-metric-range"><span>{data.records[0]?.trade_day} — {data.trade_day}</span><Detail text={data.scope}>数据范围</Detail></div><div className="pcv-metric-grid">{data.metric_definitions.map(metric => <button className="pcv-metric" data-metric={metric.key} key={metric.key} onClick={() => setFocused(metric.key)} aria-label={`${metric.label} ${formatted(metric, values[metric.key])}，放大走势`} style={{ '--metric-color': color(metric.key, values[metric.key]) } as CSSProperties}><span className="pcv-metric-heading"><span>{metric.label}</span><b>{formatted(metric, values[metric.key])}{!['元', '%'].includes(metric.unit) && number(values[metric.key]) !== null ? metric.unit : ''}</b></span><History metric={metric} points={data.records}/></button>)}</div></section>;
}
