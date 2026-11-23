import { useState } from 'react';
import { Dialog, Table } from './components';
import type { Metrics } from './model';
import { boardPercentage, boardRatioTitle, getBoardRatios } from './boardRatios';

export function BoardRatio({ metrics, tradeDay }: { metrics: Metrics; tradeDay: string }) {
  const [open, setOpen] = useState(false);
  const ratio = getBoardRatios(metrics);
  if (ratio.state === 'missing') return <span title="连板高度未完整载入或统计不一致，缺失不计作0。">—</span>;
  if (ratio.state === 'empty') return <span className="dr-board-ratio-empty">无涨停</span>;
  const partial = ratio.state === 'partial';
  const summary = ratio.bands.map(b => `${b.label} ${partial ? '≥' : ''}${boardPercentage(b.percentage)}（${b.count}家）`).join('；');
  const title = `${boardRatioTitle}\n涨停共 ${ratio.total} 家。${summary}${partial ? `；高度待核 ${ratio.unknown} 家` : ''}`;
  return <><button className="dr-board-ratio-button" title={title} aria-label={`${tradeDay} 连板比值：${summary}`} onClick={() => setOpen(true)}>
    <span className="dr-board-ratio-bar">{ratio.bands.filter(b => b.count > 0).map(b => <span key={b.index} className="dr-board-ratio-segment" data-board-band={b.index + 1} style={{ flex: `${b.count} 1 0px`, backgroundColor: b.color }} title={`${b.label}：${b.count}家 · ${partial ? '≥' : ''}${boardPercentage(b.percentage)}`}>{b.percentage >= 12 ? `${partial ? '≥' : ''}${boardPercentage(b.percentage)}` : ''}</span>)}
      {partial && <span className="dr-board-ratio-segment is-unknown" data-board-band="unknown" style={{ flex: `${ratio.unknown} 1 0px` }} title={`高度待核：${ratio.unknown}家`}>{ratio.unknown! / ratio.total! >= .2 ? `待核${boardPercentage(ratio.unknown! / ratio.total! * 100)}` : ''}</span>}
    </span></button>
    {open && <Dialog title={`连板比值 · ${tradeDay}`} close={() => setOpen(false)}><p>当日非ST短线池收盘涨停共 {ratio.total} 家。各档占比以全部涨停家数为分母；七板以上为八板及以上。</p>
      <Table label="连板比值明细"><thead><tr><th>连板高度</th><th>{partial ? '已知家数' : '家数'}</th><th>占涨停比例</th></tr></thead><tbody>{ratio.bands.map(b => <tr key={b.index}><td><i className="dr-board-ratio-key" style={{ backgroundColor: b.color }}/>{b.label}</td><td>{b.count}</td><td>{partial ? '≥' : ''}{boardPercentage(b.percentage)}</td></tr>)}{partial && <tr><td>高度待核</td><td>{ratio.unknown}</td><td>{boardPercentage(ratio.unknown! / ratio.total! * 100)}</td></tr>}</tbody></Table>
      <p className="dr-note">色段长度按家数占比，零家数不占宽度。{partial ? '存在高度待核，已知分类占比显示下界。' : '显示值四舍五入至一位小数。'}</p></Dialog>}
  </>;
}
