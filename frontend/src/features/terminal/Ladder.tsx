import { LadderHistory } from '../daily-review/ReviewAnalysis';
import { AIObservation, Change, Empty, Follow, Panel, StockName, Tag } from './Components';
import type { PageProps } from './Components';
import { SealChart } from './Charts';
import { dateTime, money, number, percent, time, tone } from './data';
import type { Stock } from './types';
import { sharesTheme, stockThemes } from './stockThemes';
import { latestSourceTime } from './sourceTiming';

function statusColor(stock: Stock) {
  if (stock.seal_amount_cny != null && stock.seal_amount_cny > 0) return 'ty-gold';
  return stock.status.includes('开板') ? 'ty-up' : 'ty-neutral';
}
function PoolSeal({ stock }: { stock: Stock }) {
  if (stock.seal_amount_cny != null) return <>{money(stock.seal_amount_cny)}</>;
  const pool = stock.daily_pool;
  return pool?.seal_amount_cny != null ? <span title={`${pool.source_name} · ${pool.trade_day} 日榜封单；接收 ${dateTime(pool.received_at)}；${pool.scope}`}>{money(pool.seal_amount_cny)}<small className="ty-source-note">日榜</small></span> : <>—</>;
}
function PoolEvidence({ stock }: { stock: Stock | null }) {
  const pool = stock?.daily_pool;
  if (!pool) return <p className="ty-panel-caption">该股尚无可核的日榜封单记录；盘口曲线仅使用实际保存的盘口。</p>;
  return <div className="ty-pool-evidence" aria-label="独立日榜证据">
    <strong>{pool.trade_day} 日榜 · {pool.source_name}</strong>
    <span>源报状态 <b>{{ U: '涨停池', D: '跌停池', Z: '炸板池' }[pool.state]}</b></span>
    <span>日榜封单 <b>{money(pool.seal_amount_cny)} 亿</b></span>
    <span>首封 <b>{time(pool.first_limit_time)}</b></span><span>末封 <b>{time(pool.last_limit_time)}</b></span>
    <span>源报开板 <b>{number(pool.open_times)} 次</b></span>
    <small>接收 {dateTime(pool.received_at)} · {pool.scope} 日榜不补绘盘口曲线。</small>
  </div>;
}
export function Ladder(props: PageProps) {
  const { data, selected, sector, selectStock, watch } = props;
  const heights = [...new Set(data.stocks.filter(stock => stock.previous_height != null && stock.previous_height >= 1).map(stock => stock.previous_height!))].sort((a, b) => b - a);
  const groups = heights.map(height => ({ height, stocks: data.stocks.filter(stock => stock.previous_height === height) }));
  const sameHeight = selected?.previous_height != null ? data.stocks.filter(stock => stock.previous_height === selected.previous_height) : [];
  const relatedThemes = stockThemes(selected);
  const sameTheme = data.stocks.filter(stock => sharesTheme(stock, selected));
  const themeHeights = [...new Set(sameTheme.map(stock => stock.previous_height).filter((height): height is number => height != null && height > 0))].sort((a, b) => b - a);
  const relay = data.relay_summary;
  const firstBoards = relay?.daily_ladder_available && relatedThemes.length
    ? sameTheme.filter(stock => relay.first_board_codes.includes(stock.code)).length : null;
  const quoteTime = latestSourceTime(data.stocks.map(stock => stock.source_time));
  const quoteLabel = data.breadth.basis === 'daily_close' ? `${data.trade_day} 收盘日线` : `行情截至 ${time(quoteTime)}`;
  const selectedEvents = data.events.filter(event => event.code === selected?.code);
  const brokenKnown = data.broken.filter(stock => stock.pct_change != null);
  const brokenDown = brokenKnown.filter(stock => stock.pct_change! < 0).length;
  const sortedChanges = brokenKnown.map(stock => stock.pct_change!).sort((a, b) => a - b);
  const midpoint = Math.floor(sortedChanges.length / 2);
  const median = !sortedChanges.length ? null : sortedChanges.length % 2 ? sortedChanges[midpoint] : (sortedChanges[midpoint - 1] + sortedChanges[midpoint]) / 2;
  const openCount = selectedEvents.filter(event => event.label === '观测开板' || event.kind === 'open' || event.kind === 'observed_open').length;
  const hasSealEvidence = data.stock_seals.some(point => point.value != null) || openCount > 0;
  return <>
    <section className="ty-relay-ribbon" aria-label="接力环境"><strong>接力观察</strong><span>昨日最高 <b>{heights.length ? `${heights[0]}板` : '—'}</b></span><span title={relay?.history_scope}>近10日最高 <b>{relay?.history_high != null ? `${relay.history_high}板` : '—'}</b><small className="ty-relay-coverage">已存 {relay?.history_covered_days ?? 0}/10 日榜</small></span><span title={`昨日2板及以上样本${relay?.high_seal_total ?? 0}只；只计有完整盘口证据的已封住股票`}>高位封住 <b>{number(relay?.high_sealed_count)}</b><small>{relay?.high_sealed_count != null ? '已核盘口样本' : '无完整盘口'}</small></span><span title={relay?.daily_ladder_source ?? '当日日榜和同源题材样本齐备后统计'}>同题材首板 <b>{number(firstBoards)}</b><small>{firstBoards != null ? '已载入题材样本' : !relay?.daily_ladder_available ? '日榜未覆盖' : '无关联题材'}</small></span><span>断板池下跌 <b className={brokenKnown.length ? 'ty-down' : ''}>{brokenKnown.length ? `${brokenDown}/${brokenKnown.length}` : '—'}</b><small>已采集样本</small></span><span className="ty-relay-time">{quoteLabel}</span></section>
  <LadderHistory day={data.trade_day} asOf={props.asOfTime} live={data.intraday_ladder} select={values=>{if(values.trade_day)props.selectDay(values.trade_day);if(values.stock)props.selectStock(values.stock);}}/>
    <div className="ty-workspace ty-ladder-workspace"><div className="ty-main-column"><div className="ty-ladder-top">
      <Panel title="连板梯队比较" subtitle={`已采集 ${data.stocks.filter(stock => stock.previous_height != null && stock.previous_height > 0).length} 只 · 沪深主板 / 非ST`} className="ty-ladder-table-panel">
        <div className="ty-table-scroll"><table className="ty-table ty-ladder-table"><thead><tr><th scope="col" aria-sort="descending">昨日高度<svg className="ty-sort-direction" width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M6 1v9m-3-3 3 3 3-3"/></svg></th><th>股票 / 题材</th><th>当前状态</th><th>首次观测 / 首封</th><th>换手%</th><th>封单/亿</th><th>最近变化</th></tr></thead><tbody>{groups.map(group => group.stocks.map((stock, index) => <tr key={stock.code} data-current={selected?.code === stock.code || undefined} onClick={() => selectStock(stock.code)}>{index === 0 && <th scope="rowgroup" rowSpan={group.stocks.length} className="ty-height-cell"><strong>昨{group.height}板</strong><span>{group.stocks.length}只 · 看{group.height + 1}板</span></th>}<td className="ty-ladder-stock"><StockName stock={stock} onSelect={selectStock}/>{stockThemes(stock).slice(0, 2).map(item => <span key={item.key} title={item.source}><Tag>{item.label}</Tag></span>)}</td><td><span className={statusColor(stock)}>{stock.status || '待观测'}</span>{stock.seal_amount_cny == null && <small className={tone(stock.pct_change)}> {percent(stock.pct_change)}</small>}</td><td>{stock.first_observed ? time(stock.first_observed) : stock.daily_pool?.first_limit_time ? <span title={`${stock.daily_pool.source_name} · 首次封板原值；非本机首次观测`}>{time(stock.daily_pool.first_limit_time)}<small className="ty-source-note">日榜首封</small></span> : '—'}</td><td>{number(stock.turnover_rate, 1)}</td><td><PoolSeal stock={stock}/></td><td>{stock.latest_change || (stock.daily_pool?.last_limit_time ? `日榜末封 ${time(stock.daily_pool.last_limit_time)}` : '—')}</td></tr>))}</tbody></table>{!groups.length && <Empty title="昨日连板梯队尚未保存" detail="昨日高度、盘中观测与日榜记录分开展示"/>}</div>
        <div className="ty-panel-caption">首次观测来自本机已存行情；日榜首封与封单另标来源 · 价格触板不等于盘口封住</div>
      </Panel>
      <Panel title="关联对象对照" subtitle={relatedThemes[0] ? <Tag>{relatedThemes[0].label}</Tag> : undefined} className="ty-related-panel"><h3>同身位 · {selected?.previous_height != null ? `昨${selected.previous_height}板` : '待选择'}</h3><div className="ty-table-scroll"><table className="ty-table ty-related-table"><thead><tr><th>对象</th><th>竞价涨幅</th><th>当前</th></tr></thead><tbody>{sameHeight.map(stock => <tr key={stock.code} data-current={stock.code === selected?.code || undefined}><td><StockName stock={stock} onSelect={selectStock}/></td><td><Change value={stock.auction_pct}/></td><td><span className={statusColor(stock)}>{stock.status || '待观测'}</span>{stock.seal_amount_cny == null && <small className={tone(stock.pct_change)}> {percent(stock.pct_change)}</small>}</td></tr>)}</tbody></table>{!sameHeight.length && <Empty compact title="选择股票查看同身位"/>}</div><h3 className="ty-section-divider">同题材梯队</h3><div className="ty-panel-caption">{relatedThemes.length ? relatedThemes.map(item => `${item.label}（${item.source}）`).join(' · ') : '该股尚无已存板块成分或供应商题材标签'}</div><div className="ty-theme-ladder">{themeHeights.slice(0, 4).map(height => <div key={height}><strong>昨{height}板</strong><span>{sameTheme.filter(stock => stock.previous_height === height).slice(0, 3).map(stock => <button key={stock.code} type="button" className={'ty-theme-stock' + (selected?.code === stock.code ? ' is-selected' : '')} aria-pressed={selected?.code === stock.code} title={`${stock.name} · ${stock.status}`} onClick={() => selectStock(stock.code)}>{stock.name}<small>{stock.status}</small></button>)}</span></div>)}{!sameTheme.length && <Empty compact title="暂无可核同题材关系"/>}</div><div className="ty-related-summary">关联样本：<b>{relatedThemes.length ? sameTheme.length : '—'}</b> 只 · 仅按同源已存关系关联</div></Panel>
    </div><div className="ty-ladder-bottom">
      <Panel title={<>{selected?.name || '选择股票'} · 封板与换手</>} subtitle={selected?.previous_height != null ? <Tag gold>昨{selected.previous_height}板 / 看{selected.previous_height + 1}板</Tag> : undefined} action={<Follow selected={selected} watch={watch}/>} className="ty-seal-panel"><div className="ty-seal-stats"><span>当前封单 <strong>{money(selected?.seal_amount_cny)}<small>亿</small></strong></span><span>观测开板 <strong>{hasSealEvidence ? `${openCount}次` : '—'}</strong></span><span>最近变化 <strong className="ty-latest-change">{selected?.latest_change || '—'}</strong></span><span>换手 <strong>{selected?.turnover_rate == null ? '—' : `${number(selected.turnover_rate, 1)}%`}</strong></span><span>累计成交 <strong>{money(selected?.amount_cny, 1)}<small>亿</small></strong></span><span>流通市值 <strong>{money(selected?.float_mv_cny, 1)}<small>亿</small></strong></span></div><div className="ty-subchart-head"><span>封单金额快照 · 亿元</span><small>采样快照，缺测不连接</small></div><PoolEvidence stock={selected}/><SealChart points={data.stock_seals}/><div className="ty-seal-events"><strong>关键事件<small>按快照观测</small></strong>{selectedEvents.length ? selectedEvents.slice(-3).map((event, i) => <span key={`${event.time}-${i}`}><i/>{time(event.time)} {event.label}</span>) : <span className="ty-muted">等待盘口与事件证据</span>}</div>{watch.error && <p role="alert" className="ty-error-message">{watch.error}</p>}</Panel>
      <Panel title="近期断板反馈" subtitle="已确认断板样本" className="ty-broken-panel"><div className="ty-table-scroll"><table className="ty-table"><thead><tr><th>名称</th><th>原高度</th><th>距断板</th><th>开盘</th><th>当前</th></tr></thead><tbody>{data.broken.slice(0, 6).map(stock => <tr key={stock.code} data-current={stock.code === selected?.code || undefined}><td><StockName stock={stock} onSelect={selectStock}/></td><td className="ty-gold">{stock.previous_height == null ? '—' : `${stock.previous_height}板`}</td><td>{stock.days_since_break == null ? '—' : `${stock.days_since_break}日`}</td><td><Change value={stock.open_pct}/></td><td><Change value={stock.pct_change}/></td></tr>)}</tbody></table>{!data.broken.length && <Empty title="暂无已确认断板样本" detail="需要连续交易日的日榜与行情证据"/>}</div><div className="ty-broken-summary"><span>下跌 <b className="ty-down">{brokenKnown.length ? `${brokenDown}/${brokenKnown.length}` : '—'}</b></span><span>中位涨幅 <b className={tone(median)}>{percent(median)}</b></span></div></Panel>
    </div></div><AIObservation data={data} session={props.session} tradeDay={data.trade_day} asOfTime={props.asOfTime} selected={selected} sector={sector} view="ladder"/></div>

  </>;
}
