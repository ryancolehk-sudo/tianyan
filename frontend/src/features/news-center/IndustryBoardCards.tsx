import { useState } from 'react';
import type { NewsOverview, StreamHeadline } from '../../generated/schema';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { percent as boardChangePercent } from '../terminal/data';
import { chinaTime, safeOriginal } from './model';
import { newsApi, type IndustryBoard } from './newsApi';
import { FinancePicks } from './FinancePicks';
import { TopicHotBoard } from './TopicHotBoard';
import './industryBoardCards.css';

type Props = {
  overview?: NewsOverview;
  busy: boolean;
  error: Error | null;
  retry: () => void;
  onSelect: (id: string) => void;
  search: string;
  onFilter: (values: Record<string, string | null>) => void;
};
type BoardSort = 'latest' | 'gain' | 'strength';

function flowText(net: number | null) {
  if (net === null) return '主力净额 —';
  if (net === 0) return '主力净额 0 元';
  const amount = Math.abs(net);
  const [divisor, unit] = amount >= 1e8 ? [1e8, '亿'] as const :
    amount >= 1e4 ? [1e4, '万'] as const : [1, '元'] as const;
  const value = (amount / divisor).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  return (net > 0 ? '主力净流入 ' : '主力净流出 ') + value + unit;
}

function BoardMarket({ market, kpl }: { market: IndustryBoard['market']; kpl: boolean }) {
  if (!market) return <span className="nc-board-no-quote">暂无行情</span>;
  const origin = kpl ? '开盘啦 · 接收 ' + chinaTime(market.received_at, true) : '东方财富行情 ' + chinaTime(market.source_time, true);
  return <div className="nc-board-quote" title={origin}>
    <strong data-direction={Math.sign(market.pct_change ?? 0)}><span className="sr-only">涨跌幅 </span>{boardChangePercent(market.pct_change)}</strong>
    <small data-direction={Math.sign(market.main_net_cny ?? 0)}>{flowText(market.main_net_cny)}</small>
  </div>;
}

function newsClock(latest: NonNullable<IndustryBoard['latest']>) {
  if (latest.published_at) return '更新于 ' + chinaTime(latest.published_at, true);
  if ('published_day' in latest && latest.published_day) return latest.published_day.replaceAll('-', '/') + ' · 仅日期';
  return 'received_at' in latest && latest.received_at ? '入库于 ' + chinaTime(latest.received_at, true) : '发布时间未知';
}

function boardNewsTime(latest: IndustryBoard['latest']) {
  if (!latest) return -Infinity;
  if (latest.published_at) return Date.parse(latest.published_at);
  if ('published_day' in latest && latest.published_day) return Date.parse(latest.published_day + 'T00:00:00+08:00');
  return 'received_at' in latest && latest.received_at ? Date.parse(latest.received_at) : -Infinity;
}

function orderedBoards(boards: IndustryBoard[], order: BoardSort) {
  function metric(board: IndustryBoard) {
    if (order === 'gain') return board.market?.pct_change ?? -Infinity;
    if (order === 'strength') return board.strength ?? -Infinity;
    return boardNewsTime(board.latest);
  }
  return [...boards].sort((a, b) => {
    const left = metric(a), right = metric(b);
    if (left !== right) return left > right ? -1 : 1;
    const aTime = boardNewsTime(a.latest), bTime = boardNewsTime(b.latest);
    return aTime === bTime ? 0 : aTime > bTime ? -1 : 1;
  });
}

function TopicSourceStatus({ sources }: { sources: NewsOverview['sources'] }) {
  if (!sources?.length) return null;
  const states: Record<string, string> = {
    complete: '正常', empty: '无新消息', partial: '采集不完整', error: '采集失败', paused: '已暂停', stale: '更新延迟', pending: '等待采集',
  };
  const latest = sources.map(source => source.received_at).filter((v): v is string => !!v)
    .sort((a, b) => Date.parse(b) - Date.parse(a))[0];
  return <details className="nc-topic-sources"><summary>{sources.length} 路来源{latest && ' · 最近采集 ' + chinaTime(latest)}</summary>
    <ul>{sources.map(source => <li key={source.family}><span>{source.name}</span>
      <span data-warn={!['complete','empty'].includes(source.status) || undefined}>{states[source.status] || source.status}</span>
      <span>{source.interval_seconds ? (source.interval_seconds / 60) + ' 分钟' : '—'}</span>
      <time>{source.received_at ? chinaTime(source.received_at, true) : '暂无回执'}</time></li>)}</ul>
  </details>;
}

const streamLevels = { important: '重要', watch: '关注', regular: '常规' };
const streamEvents = { order: '订单与中标', supply: '供应变化', catalyst: '产业催化' };

function Dispatch({ item, onSelect }: Pick<Props, 'onSelect'> & { item: StreamHeadline }) {
  const reports = item.reports ?? [];
  const latest = reports[0] ?? item;
  const level = item.importance || 'regular';
  const reason = [item.event_type && streamEvents[item.event_type], item.reason,
    item.publisher_highlighted && item.reason !== '发布方标红' && '发布方标红'].filter(Boolean).join(' · ');
  return <li className="nc-dispatch nc-dispatch-row" data-level={level}>
    <div className="nc-dispatch-kicker">
      <span className="nc-dispatch-grade">{streamLevels[level]}</span>
      {reason && <span className="nc-dispatch-reason">{reason}</span>}
    </div>
    <time className="nc-dispatch-time" dateTime={latest.published_at ?? latest.published_day ?? undefined}
      title={newsClock(latest)}>{latest.published_at ? chinaTime(latest.published_at, true) : newsClock(latest)}</time>
    <h3 className="nc-dispatch-title"><button type="button" onClick={() => onSelect(item.id)}>{item.title}</button></h3>
    <div className="nc-dispatch-foot">
      {reports.length > 1 && <details className="nc-dispatch-reports">
        <summary title={Array.from(new Set(reports.map(r => r.source_name))).join('、')}>{reports.length} 篇报道</summary>
        <div>{reports.map(report => <button type="button" key={report.id} onClick={() => onSelect(report.id)}>
          <span>{report.source_name}</span>
          <time dateTime={report.published_at ?? report.published_day ?? undefined}>{newsClock(report)}</time>
          <span>{report.title}</span>
        </button>)}</div>
      </details>}
      <span className="nc-dispatch-source">{reports.length > 1 && <span>最新来源 </span>}{latest.source_name}</span>
    </div>
  </li>;
}

function TopicNewsStream({ onSelect, onFilter, importance, event }: Pick<Props, 'onSelect' | 'onFilter'> & {
  importance?: StreamHeadline['importance']; event: StreamHeadline['event_type'];
}) {
  const [pages, setPages] = useState<string[]>([]);
  const cursor = pages.at(-1);
  const read = useCachedLoader(() => newsApi.topicStream(cursor, importance, event),
    ['topic-stream', importance || 'all', event || 'all', cursor || 'latest'].join(':'));
  const stream = read.data?.data;
  const hot = stream?.hot_events ?? [];
  const rows = stream?.items ?? [];
  return <div className="nc-topic-stream">
    <div className="nc-stream-tools">
      <div className="nc-stream-levels" role="group" aria-label="消息重要性">
        {([[undefined, '全部级别'], ['important', '只看重要'], ['watch', '关注'], ['regular', '常规']] as const).map(([value, label]) =>
          <button type="button" key={value || 'all'} data-level={value} aria-pressed={importance === value}
            onClick={() => onFilter(value === 'regular' ? {importance:value, event_type:null} : {importance: value || null})}>{label}</button>)}
      </div>
      <label className="nc-stream-event">事件 <select aria-label="事件类型" value={event || ''} disabled={importance === 'regular' && !event}
        onChange={e => onFilter({event_type: e.target.value || null})}>
        <option value="">全部事件</option>{Object.entries(streamEvents).map(([value, label]) =>
          <option key={value} value={value}>{label}</option>)}</select></label>
      <details className="nc-stream-guide"><summary>分级依据</summary>
      <dl><dt>重要</dt><dd>已签订或正式中标的重大订单、已明确的供应变化，以及政策落地、投产量产等产业催化。订单由原文明确称为重大，或披露的人民币金额达到 1 亿元。</dd>
        <dt>关注</dt><dd>订单意向、预中标、产能计划、传闻澄清等跟踪线索。发布方单独标红的消息也收在这里。</dd>
        <dt>常规</dt><dd>其余日常快讯和例行公告。系统根据当前可见的标题和原文摘要分级。</dd></dl>
      </details>
    </div>
    {read.error && <p className="nc-board-status" role="alert">消息读取失败。<button onClick={read.reload}>重新读取</button></p>}
    {read.busy && !stream && <p role="status">正在读取消息…</p>}
    {stream && !hot.length && !rows.length && <div className="nc-board-status" role="status">
      {importance || event ? <>没有符合当前筛选的消息。<button onClick={() => onFilter({importance:null, event_type:null})}>查看全部消息</button></> : '暂无已入库消息'}
    </div>}
    {!cursor && stream?.ranked_at && <TopicHotBoard events={hot} rankedAt={stream.ranked_at} onSelect={onSelect}/>}
    {rows.length > 0 && <ul className="nc-dispatches" aria-label="消息列表">
      <li className="nc-dispatch-divider" role="presentation"><h2>{cursor ? '更早消息' : '最新消息'}</h2></li>
      {rows.map(item => <Dispatch key={item.id} item={item} onSelect={onSelect}/>)}
    </ul>}
    <nav aria-label="消息分页">
      {!!pages.length && <button onClick={() => setPages([])}>返回最新</button>}
      {pages.length > 1 && <button onClick={() => setPages(pages.slice(0, -1))}>上一页</button>}
      {stream?.next_cursor && <button onClick={() => setPages([...pages, stream.next_cursor!])}>更早消息</button>}
    </nav>
  </div>;
}

export function IndustryBoardCards({ overview, busy, error, retry, onSelect, search, onFilter }: Props) {
  const query = new URLSearchParams(search);
  const groupValue = query.get('group');
  const group = groupValue === 'picks' || groupValue === 'industry' || groupValue === 'concept' ? groupValue : 'feed';
  const level = query.get('importance'), kind = query.get('event_type');
  const importance = level === 'important' || level === 'watch' || level === 'regular' ? level : undefined;
  const event = kind === 'order' || kind === 'supply' || kind === 'catalyst' ? kind : null;
  const [order, setOrder] = useState<BoardSort>('latest');
  const [showEmpty, setShowEmpty] = useState(false);
  const concept = group === 'concept';
  const boardsShown = group === 'industry' || concept;
  const boards = concept ? [...(overview?.concepts ?? []), ...(overview?.strength_topics ?? [])] : overview?.industries ?? [];
  const visible = orderedBoards(boards.filter(board => showEmpty || board.latest), order);
  const day = concept ? overview?.concept_trade_day : overview?.market_trade_day;
  const strengthDay = concept ? overview?.strength_trade_day : overview?.breadth_trade_day;
  const breadthLabel = overview?.breadth_basis === 'intraday' ? '盘中涨停家数' : '收盘涨停家数';
  return <section className="nc-overview nc-board-directory" aria-labelledby="industry-news-title">
    <header>
      <div className="nc-board-caption"><h1 id="industry-news-title">{group === 'picks' ? '财经精选' : group === 'feed' ? '全部消息' : concept ? '按题材概念看资讯' : '按二级板块看资讯'}</h1>
        {boardsShown && day && <span>东方财富行情 · {day.replaceAll('-', '/')}</span>}
        {boardsShown && strengthDay && (concept || order === 'strength') && <span>{concept ? '开盘啦强度' : breadthLabel} · {strengthDay.replaceAll('-', '/')}</span>}
      </div>
      <TopicSourceStatus sources={overview?.sources}/>
    </header>
    <div className="nc-topic-tools">
      <div className="nc-topic-groups" role="group" aria-label="资讯分类">
        {([['feed','全部消息'],['picks','精选'],['industry','行业概念'],['concept','题材概念']] as const).map(([key,label]) =>
          <button type="button" key={key} aria-pressed={group === key} onClick={() => onFilter({group: key})}>{label}</button>)}
      </div>
      {boardsShown && <div className="nc-topic-sort">
        <label>排序 <select value={order} onChange={event => setOrder(event.target.value as BoardSort)}>
          <option value="latest">最新消息</option><option value="gain">涨幅从高到低</option>
          <option value="strength">{concept ? '题材强度从高到低' : '涨停家数从高到低'}</option>
        </select></label>
        <label><input type="checkbox" checked={showEmpty} onChange={event => setShowEmpty(event.target.checked)}/> 显示暂无新闻的板块</label>
      </div>}
    </div>
    {error && boardsShown && <div className="nc-board-status" role="alert">板块资讯读取失败。<button type="button" onClick={retry}>重新读取</button></div>}
    {group === 'picks' ? <FinancePicks day={query.get('pick_day')} onDay={value => onFilter({pick_day:value})} onSelect={onSelect}/> :
      group === 'feed' ? <TopicNewsStream key={(importance || 'all') + '/' + (event || 'all')}
      onSelect={onSelect} onFilter={onFilter} importance={importance} event={event}/> : <>
      {busy && !overview && <p className="nc-board-status" role="status">正在读取板块资讯…</p>}
      {overview && !visible.length && <p className="nc-board-status">{boards.length ? '已入库消息暂未匹配到这个分类。' : '暂无板块目录。'}</p>}
      <div className="nc-industry-grid">{visible.map(({ code, name, latest, market, strength, strength_coverage: coverage }) => {
        const kpl = concept && /^\d{6}$/.test(code);
        return <article key={code}>
          <div className="nc-board-card-heading"><div><h3>{name}</h3>
            {concept && <span className="nc-topic-metric">{kpl ? '开盘啦' : '东方财富'}{kpl && strength != null ? ' · 强度 ' + strength : ''}</span>}
            {!concept && order === 'strength' && <span className="nc-topic-metric" title={coverage ? '已核对 ' + coverage.quoted + '/' + (coverage.members ?? '—') + ' 只成分股 · 成分日期 ' + (coverage.members_day ?? '—') : undefined}>
              {strength == null ? '涨停家数 —' : '涨停 ' + strength + ' 家'}{coverage?.partial && strength != null ? ' · 部分' : ''}
            </span>}
          </div><BoardMarket market={market} kpl={kpl}/></div>
          {latest ? <>
            {'id' in latest
              ? <button type="button" className="nc-board-headline" title={latest.title} onClick={() => onSelect(latest.id)}>{latest.title}</button>
              : <a className="nc-board-headline" href={safeOriginal(latest.source_url)} target="_blank" rel="noopener noreferrer" title={latest.title}>{latest.title}</a>}
            <footer className="nc-board-meta"><time dateTime={latest.published_at ?? undefined}>{newsClock(latest)}</time>
              {latest.source_name && <span className="nc-board-source">来源 {latest.source_name}</span>}
            </footer>
          </> : <p className="nc-board-empty">暂无新闻</p>}
        </article>;
      })}</div>
    </>}
  </section>;
}
