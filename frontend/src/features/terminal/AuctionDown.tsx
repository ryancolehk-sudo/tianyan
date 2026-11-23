import { useWorkspaceState, useStableRanking } from '../../shared/workspaceState';
import type { AuctionDownDay, AuctionDownRow } from '../../generated/schema';
import { Change } from './Components';
import { AuctionRow } from './AuctionRow';
import { dateTime, money } from './data';

const stages = ['09:15', '09:20', '09:25'] as const;
type Sort = { key: string; direction: 'asc' | 'desc' };
type Props = { day: string; data?: AuctionDownDay; identity: string; selected?: string; onSelect: (code: string) => void; busy: boolean; error: boolean; reload: () => void };

export function AuctionDown({ day, data, identity, selected, onSelect, busy, error, reload }: Props) {
  const [sort, setSort] = useWorkspaceState<Sort>(identity + ':down-seals-v2:' + day + ':sort', { key: '09:25', direction: 'desc' });
  const activeKey = data?.live && sort.key === '09:25' && data.rows?.every(row => row.node_amounts?.['09:25'] == null) ? 'live' : sort.key;
  const value = (row: AuctionDownRow) => activeKey === 'current' ? row.pct_change : activeKey === 'live' ? row.current_seal_cny : row.node_amounts?.[activeKey];
  const rows = [...(data?.rows ?? [])].sort((a, b) => {
    const x = value(a), y = value(b);
    return x == null || y == null ? x == null && y == null ? a.code.localeCompare(b.code) : x == null ? 1 : -1
      : (sort.direction === 'asc' ? x - y : y - x) || a.code.localeCompare(b.code);
  });
  const ranking = useStableRanking(rows, identity + ':down-seals-v2:' + day + ':' + activeKey + ':' + sort.direction);
  const byCode = new Map(rows.map(row => [row.code, row]));
  const ordered = ranking.order.map(code => byCode.get(code)!).filter(Boolean);
  const closing = rows.length > 0 && rows.every(row => row.price_basis === 'daily_close' || row.pct_change == null);
  const summaryStage = stages.includes(activeKey as typeof stages[number]) ? activeKey : '09:25';
  const currentBook = data?.live || activeKey === 'live';
  const countLabel = !data || ['waiting', 'stale', 'missing'].includes(data.book_state ?? '') || data.book_state === 'partial' && !rows.length ? '—' : String(rows.length);
  const amounts = rows.map(row => currentBook ? row.current_seal_cny : row.node_amounts?.[summaryStage]);
  const total = amounts.length && amounts.every(value => value != null) ? amounts.reduce<number>((sum, value) => sum + (value ?? 0), 0) : null;
  const sourceHint = `${data?.source_name || '已存竞价成交与竞价卖一盘口'} · ${currentBook ? '当前' : summaryStage}卖一封单；未采集金额显示—${data?.latest_at ? ' · 来源 ' + dateTime(data.latest_at) + ' · 有效盘口 ' + data.book_coverage + '/' + data.book_expected : ''}`;
  const sortMark = (key: string) => activeKey === key && <svg className="ty-sort-direction" width="8" height="9" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true" style={sort.direction === 'asc' ? {transform:'rotate(180deg)'} : undefined}><path d="M6 1v9m-3-3 3 3 3-3"/></svg>;
  function sortBy(key: string) { setSort({ key, direction: sort.key === key && sort.direction === 'asc' ? 'desc' : 'asc' }); }
  return <section className="ty-auction-day is-down-day" aria-label={`${day}跌停竞价对照`}>
    <h3 aria-label={`${day}跌停`}><time dateTime={day}>{day || '—'}</time><span className="ty-auction-category">跌停</span></h3>
    <div className="ty-auction-summary" aria-label={`${day}跌停汇总`} title={sourceHint} tabIndex={0}>
      <span><b>{countLabel}</b>只</span><span className="ty-auction-summary-divider" aria-hidden="true"/>
      {data?.book_state === 'available' ? <button type="button" aria-label={`按${day}当前跌停封单排序`} aria-pressed={activeKey === 'live'} onClick={()=>sortBy('live')}>封单 <b>{total == null ? '—' : money(total, 2).replace(/\.?0+$/, '') + '亿'}</b></button> : <span>封单 <b>{total == null ? '—' : money(total, 2).replace(/\.?0+$/, '') + '亿'}</b></span>}
    </div>
    <div className="ty-auction-node-head">
      {stages.map(stage => <button type="button" key={stage} aria-label={`按${day} ${stage}跌停封单排序`} aria-pressed={activeKey === stage} onClick={() => sortBy(stage)}>{stage.replace(/^0/, '')}{sortMark(stage)}</button>)}
      <button type="button" title={closing ? '收盘涨幅' : data?.live ? '竞价指示涨幅' : '行情最新涨幅'} aria-label={`按${day}跌停当前涨幅排序`} aria-pressed={activeKey === 'current'} onClick={() => sortBy('current')}>涨幅{sortMark('current')}</button>
    </div>
    <ol className="ty-auction-stock-list" data-scroll-key={'down:' + day} tabIndex={0} aria-label={`${day}跌停滚动列表`}>
      {ordered.map((row, index) => <li key={row.code} data-current={selected === row.code || undefined}>
        <AuctionRow code={row.code} name={row.name} selected={selected === row.code} onSelect={onSelect}
          description={`${row.name} ${row.code}\n${row.tags?.join(' / ') || ''}\n行情时点 ${dateTime(row.source_time)}`}>
        <span className="ty-auction-rank">{index + 1}</span><span className="ty-auction-stock">
          <span className="ty-auction-name"><span className="ty-auction-labels"><span className="ty-stock-name">{row.name}</span>{row.tags?.map(tag => <span className="ty-tag" title={tag} key={tag}>{tag}</span>)}</span>{row.current_seal_cny != null && <span className="ty-auction-board" title={`${data?.live ? '当前' : '竞价末次'}卖一封单 · ${dateTime(row.book_time)}`}>{money(row.current_seal_cny,2)}亿</span>}</span>
          <span className="ty-auction-values">
            {stages.map(stage => <span key={stage} title={`卖一封单 · 来源时点 ${dateTime(row.node_times?.[stage])}`}>{money(row.node_amounts?.[stage], 2)}</span>)}
            <span title={`${row.price_basis === 'daily_close' ? '收盘日线' : row.price_basis === 'auction_indicative' ? '竞价指示价' : '行情时点'} ${dateTime(row.source_time)}`}><Change value={row.pct_change}/></span>
          </span>
        </span>
        </AuctionRow>
      </li>)}
    </ol>
    {!rows.length && <div className="ty-auction-no-data" role="status">{error ? <>跌停数据读取失败 <button type="button" onClick={reload}>重试</button></> : busy ? '正在读取跌停观察…' : data?.book_state === 'waiting' ? '等待竞价盘口' : data?.book_state === 'stale' ? '盘口更新中' : data?.book_state === 'partial' ? '竞价盘口覆盖未齐' : '暂无竞价跌停记录'}</div>}
    <div className="ty-panel-caption ty-down-footer" title={sourceHint}><span>{countLabel}只</span>{ranking.changed && <button type="button" onClick={ranking.apply}>更新排序</button>}</div>
  </section>;
}
