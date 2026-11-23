import { request } from '../../shared/api';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { useNavigation } from '../../shared/navigation';
import type { AuctionReferenceReport, AuctionReferenceStock, Envelope_AuctionReferenceView_ } from '../../generated/schema';
import { Panel } from './Components';
import { dateTime, money, time, tone } from './data';

const stages = ['09:15', '09:20', '09:25'];
const value = (amount: string | null | undefined) => amount == null ? '—' : money(Number(amount), 2) + '亿';
const amount = (raw: string | null | undefined) => raw == null ? '—' : money(Number(raw), 2);
const change = (raw: string | null | undefined) => raw == null ? '—' : `${Number(raw) > 0 ? '+' : Number(raw) < 0 ? '−' : ''}${money(Math.abs(Number(raw)), 2)}`;

function StockRows({ report, onSelect }: {
  report: AuctionReferenceReport; onSelect: (code: string) => void;
}) {
  const previous = stages[stages.indexOf(report.stage) - 1];
  const rows = new Map<string, AuctionReferenceStock>();
  [...report.strongest, ...report.additions, ...report.reductions].forEach(row => {
    if (!rows.has(row.code)) rows.set(row.code, row);
  });
  return <table className="ty-auction-reference-stocks" aria-label="封单排名与增减">
    <colgroup><col/><col/><col/><col/></colgroup>
    <thead><tr><th scope="col">个股</th><th scope="col">封单/亿</th><th scope="col" title={previous ? `较${previous}的封单净变化，不能区分撤单与成交消耗` : '首阶段暂无可比值'}>增减/亿</th><th scope="col">题材</th></tr></thead>
    <tbody>{[...rows.values()].map(row => {
      const rank = report.strongest.findIndex(item => item.code === row.code);
      const comparison = previous ? `${previous} ${value(row.node_amounts[previous])} → ${report.stage} ${value(row.seal_cny)}` : '首阶段暂无可比值';
      return <tr key={row.code}>
        <th scope="row"><span className="ty-auction-reference-name"><span className="ty-auction-reference-rank">{rank >= 0 ? rank + 1 : ''}</span><button type="button" className="ty-text-button" onClick={() => onSelect(row.code)} title={`${row.name} ${row.code} · ${row.tags.join(' / ')}`}>{row.name}</button></span></th>
        <td>{amount(row.seal_cny)}</td><td className={row.change_cny == null ? 'ty-muted' : tone(Number(row.change_cny))} title={comparison} tabIndex={0} aria-label={`${row.name}封单增减 ${change(row.change_cny)}${row.change_cny == null ? '' : '亿'}；${comparison}`}>{change(row.change_cny)}</td>
        <td className="ty-auction-reference-tags" title={row.tags.join(' / ')}>{row.tags.join(' / ') || '—'}</td>
      </tr>;
    })}</tbody>
  </table>;
}

export function AuctionReference({ user, cutoff, onSelect }: {user: string; cutoff: string; onSelect: (code: string) => void}) {
  const navigation = useNavigation();
  const day = new URLSearchParams(navigation.search).get('trade_day') || '';
  const query = new URLSearchParams();
  if (day) query.set('trade_day', day);
  if (cutoff) query.set('as_of_time', cutoff);
  const path = '/api/v1/auction-reference' + (query.size ? '?' + query : '');
  const read = useCachedLoader(() => request<Envelope_AuctionReferenceView_>(path), `${user}:${path}`, '竞价封单观察');
  const report = read.data?.data.report;
  const delta = report?.total_change_cny;
  return <Panel title="竞价封单观察" className="ty-auction-reference" action={report && <time dateTime={report.received_at}
    title={`${report.source_name} · 本机接收 ${dateTime(report.received_at)} · 来源更新时间未提供`}>{report.trade_day} {report.stage}{report.recovered ? ' · 补采' : ''}</time>}>
    <div className="ty-auction-reference-body" aria-label="竞价封单观察内容">
      {report ? <>
        <div className="ty-auction-reference-totals" aria-label="三阶段封单">{stages.map(stage => <span key={stage}><small>{stage}</small><b>{value(report.totals[stage])}</b></span>)}<span title="本阶段封单净变化，不能区分撤单与成交消耗"><small>净变动</small><b className={delta == null ? 'ty-muted' : tone(Number(delta))}>{change(delta)}{delta != null && '亿'}</b></span></div>
        <section>{report.strongest.length || report.additions.length || report.reductions.length ? <StockRows report={report} onSelect={onSelect}/> : <p className="ty-muted">暂无正封单金额</p>}</section>
        <p className="ty-auction-reference-receipt" title={`${report.source_name} · ${report.rules_version} · ${report.batch_hash}`}><time dateTime={report.received_at}>{time(report.received_at)} 接收</time><span>{report.valid_count}/{report.row_count}只有效</span></p>
      </> : <p className="ty-auction-reference-empty" role="status">{read.error ? '封单观察读取失败' : read.busy ? '正在读取竞价封单观察…' : read.data?.data.reason || '尚无竞价封单观察'}</p>}
      {read.error && <button type="button" className="ty-link" onClick={read.reload}>重新读取封单观察</button>}
    </div>
  </Panel>;
}
