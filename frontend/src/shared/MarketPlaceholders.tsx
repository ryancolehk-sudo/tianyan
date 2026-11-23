import { Empty } from './ui';

export function Metrics({ labels }: { labels: string[] }) {
  return <dl className="metric-grid">{labels.map(label => <div key={label}><dt>{label}</dt><dd>—</dd></div>)}</dl>;
}

export function ChartEmpty({ title = '行情未接入', note = '等待数据源和有效时点，当前没有可绘制的数据。' }: { title?: string; note?: string }) {
  return <div className="research-chart"><Empty title={title}>{note}</Empty></div>;
}
