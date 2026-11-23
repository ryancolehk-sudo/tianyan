import './historyPlot.css';

export type PlotPoint = { day: string; values: (number | null)[]; note?: string };
const colors = ['#da4146', '#277ac2', '#b67a18', '#6b62bf'];

export function HistoryPlot({ points, labels, selected, select, unit = '', reverse = false }: {
  points: PlotPoint[]; labels: string[]; selected?: string; select: (day: string) => void; unit?: string; reverse?: boolean;
}) {
  const values = points.flatMap(p => p.values.filter((v): v is number => v != null && Number.isFinite(v)));
  const low = values.length ? Math.min(...values) : 0, high = values.length ? Math.max(...values) : 1;
  const range = high - low || 1;
  const x = (i: number) => 56 + i * 704 / Math.max(1, points.length - 1);
  const y = (v: number) => reverse ? 32 + (v - low) * 142 / range : 174 - (v - low) * 142 / range;
  return <div className="history-plot"><div className="history-plot-key">{labels.map((label, i) => <span key={label} style={{ color: colors[i % colors.length] }}>● {label}</span>)}</div>
    {!values.length ? <p role="status">所选窗口没有足够的有效数值，缺失资料保留为空。</p> : <svg viewBox="0 0 800 220" role="img" aria-label={`${labels.join('、')}历史曲线`}>
      {[0, 1, 2].map(i => { const value = low + range * i / 2; return <g key={i}><line x1="52" x2="770" y1={y(value)} y2={y(value)} stroke="#e5ebf1"/><text x="45" y={y(value) + 4} textAnchor="end">{value.toFixed(unit === '板' || unit === '只' ? 0 : 1)}{unit}</text></g>; })}
      {labels.map((label, k) => { let previous = false; const path = points.map((p, i) => { const v = p.values[k]; if (v == null || !Number.isFinite(v)) { previous = false; return ''; } const segment = `${previous ? 'L' : 'M'}${x(i)},${y(v)}`; previous = true; return segment; }).join(' '); return <path key={label} d={path} stroke={colors[k % colors.length]} strokeWidth="2" fill="none"/>; })}
      {points.map((p, i) => <g key={p.day} role="button" tabIndex={0} aria-label={`曲线 ${p.day} ${labels.map((label, k) => `${label}${p.values[k] ?? '缺失'}${unit}`).join('，')}${p.note ?? ''}`} onClick={() => select(p.day)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(p.day); } }}>
        <rect x={x(i) - 5} y="24" width="10" height="158" fill={p.day === selected ? '#d6eaff' : 'transparent'}/>
        {p.values.map((v, k) => v != null && Number.isFinite(v) ? <circle key={k} cx={x(i)} cy={y(v)} r={p.day === selected ? 4 : 2.7} fill={colors[k % colors.length]}/> : null)}
        <title>{p.day} {p.note} {p.values.map(v => v == null ? '缺失' : v.toFixed(2) + unit).join(' / ')}</title>
        {(i === 0 || i === points.length - 1 || i % Math.max(1, Math.floor(points.length / 6)) === 0) && <text x={x(i)} y="207" textAnchor="middle">{p.day.slice(5)}</text>}
      </g>)}
    </svg>}
    <p className="history-plot-note">点击日期回看对应资料；断点表示缺少可核实数据。</p>
  </div>;
}
