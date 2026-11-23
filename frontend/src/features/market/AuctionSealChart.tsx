import { useEffect, useRef, useState } from 'react';
import { AUCTION_STAGES } from './auctionFormat';
import type { VendorStock } from './auctionVendorBatches';
import { compactMoney, vendorAmountValue } from './auctionWorkbenchModel';

export function AuctionSealChart({ stock, previous, day, previousDay }: {
  stock: VendorStock; previous?: VendorStock; day: string; previousDay?: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(500);
  useEffect(() => {
    const measure = () => { if (host.current) setWidth(host.current.clientWidth); };
    measure(); const observer = new ResizeObserver(measure);
    if (host.current) observer.observe(host.current);
    return () => observer.disconnect();
  }, []);
  const series = [...(previous ? [{ stock: previous, day: previousDay!, kind: 'previous' }] : []), { stock, day, kind: 'selected' }];
  const peak = Math.max(1, ...series.flatMap(s => AUCTION_STAGES.map(stage => vendorAmountValue(s.stock.amounts[stage]) ?? 0)));
  const unit = peak >= 1e8 ? { divisor: 1e8, name: '亿元' } : peak >= 1e4 ? { divisor: 1e4, name: '万元' } : { divisor: 1, name: '元' };
  const magnitude = 10 ** Math.floor(Math.log10(peak / 2));
  const step = [1, 2, 2.5, 5, 10].find(n => n * magnitude * 2 >= peak)! * magnitude;
  const max = step * 2;
  const left = 36, right = width - 8, cell = (right - left) / 3, bottom = 170;
  const barWidth = Math.min(26, cell * .28), gap = Math.min(14, cell * .15);
  const y = (n: number) => bottom - n / max * 120;
  return <div className="auction-seal-chart" ref={host}>
    <div className="seal-chart-heading"><span>封单金额 / {unit.name}</span><div className="seal-chart-legend">{series.map(s => <span key={s.kind}><i className={s.kind}/>{s.day.slice(5)} {s.kind === 'selected' ? '所选日' : '对照'}</span>)}</div></div>
    <svg viewBox={`0 0 ${width} 208`} role="img" aria-label={`${stock.name}三阶段封单分组柱状图`}>
      <title>{stock.name}同阶段跨日封单对比，缺失不补零</title>
      {[0, 1, 2].map(i => { const n = max * i / 2; return <g key={i}><line x1={left} x2={right} y1={y(n)} y2={y(n)} className="seal-grid"/><text x={left - 8} y={y(n) + 4} textAnchor="end">{(n / unit.divisor).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}</text></g>; })}
      {AUCTION_STAGES.map((stage, i) => <g key={stage}>
        <rect className="seal-label-band" x={left + cell * i + 3} y={7} width={cell - 6} height={28} rx={5}/>
        {previous && <line className="seal-label-divider" x1={left + cell * (i + .5)} x2={left + cell * (i + .5)} y1={14} y2={28}/>}
        <text className="seal-stage" x={left + cell * (i + .5)} y={193} textAnchor="middle">{stage}</text>{series.map(s => {
        const money = s.stock.amounts[stage], value = vendorAmountValue(money);
        const center = left + cell * (i + .5) + (previous ? (s.kind === 'previous' ? -1 : 1) * (barWidth + gap) / 2 : 0);
        const x = center - barWidth / 2;
        return <g key={s.kind}>{value === null ? <text data-missing={s.kind} x={center} y={26} textAnchor="middle">—</text> : <>
          <rect data-seal-bar={s.kind} data-value={money.cny} className={`seal-bar ${s.kind}`} x={x} y={y(value)} width={barWidth} height={bottom - y(value)} rx={3}><title>{s.day} {stage} {compactMoney(money)}</title></rect>
          <text className={`seal-value ${s.kind}`} data-seal-label={s.kind} data-stage={stage} data-value={money.cny} x={center} y={26} textAnchor="middle" style={{ fontSize: cell < 95 ? 10.5 : 11.5, fontVariantNumeric: 'tabular-nums' }}>{compactMoney(money)}</text>
        </>}</g>;
      })}</g>)}
    </svg>
    <details className="seal-chart-values"><summary>查看柱状图原值与单位</summary><p className="feature-note">同阶段跨日对照，缺失不补零。网站阶段值，无逐分轨迹。</p><div className="table-wrap" role="region" aria-label="封单柱状图原值" tabIndex={0}><table><thead><tr><th>来源日期</th>{AUCTION_STAGES.map(s => <th key={s}>{s}（元）</th>)}</tr></thead><tbody>{series.map(s => <tr key={s.kind}><th>{s.day}</th>{AUCTION_STAGES.map(stage => <td key={stage}>{s.stock.amounts[stage].cny ?? '—'}<small>原文 {s.stock.amounts[stage].raw ?? '未提供'}</small></td>)}</tr>)}</tbody></table></div></details>
  </div>;
}
