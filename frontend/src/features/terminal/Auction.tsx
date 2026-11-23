import {usePaneScroll,useWorkspaceState} from '../../shared/workspaceState';
import {StableAuctionOrder} from './StableAuctionOrder';
import {rankAuction} from './auctionComparison';
import type {AuctionSort} from './auctionComparison';
import { useEffect, useRef } from 'react';
import { Change, Follow, Panel, quoteTiming, Tag } from './Components';
import type { PageProps } from './Components';
import { AuctionReference } from './AuctionReference';
import { AuctionDown } from './AuctionDown';
import { AuctionRow } from './AuctionRow';
import { AuctionRankings, AuctionSupport, useAuctionSupport } from './AuctionSupport';
import { AuctionChart } from './Charts';
import { dateTime, money, number, shortDay, time } from './data';
import type { Stock, Value } from './types';

const nodes = ['09:15', '09:20', '09:25'] as const;
type Node = typeof nodes[number];
function sealValues(stock: Pick<Stock, 'node_amounts'> | null | undefined): Value[] { return nodes.map(node => stock?.node_amounts?.[node] ?? null); }
function auctionChange(stock: Stock) { return stock.auction_indicative_pct ?? stock.auction_pct; }

export function Auction(props: PageProps) {
  const { data, selected, selectStock, watch } = props;
  const readingKey=`${props.session.data.user.id}:auction:${data.trade_day}:${props.asOfTime}`;
  const support = useAuctionSupport(props.session.data.user.id, data.trade_day, props.asOfTime);
  const paneScroll=usePaneScroll(readingKey+':scroll','[data-scroll-key],.ty-table-scroll');
  const [savedRange,setRange]=useWorkspaceState(readingKey+':range','all');
  const range=savedRange==='ladder'?'ladder':'all';
  const [reset,setReset]=useWorkspaceState(readingKey+':reset',0);
  const [chosenNode, setChosenNode] = useWorkspaceState<{ day: string; value: Node } | null>(readingKey+':node',null);
  const [chosenSorting, setChosenSorting] = useWorkspaceState<AuctionSort | null>(readingKey+':sort',null);
  const [chosenChart, setChosenChart] = useWorkspaceState<'traded' | 'seals' | null>(readingKey+':chart',null);
  const comparison = useRef<HTMLDivElement>(null);
  const workspace = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const fit = () => {
      const element = workspace.current;
      if (element) {
        const bottom = parseFloat(getComputedStyle(element.closest('main') ?? element).paddingBottom) || 0;
        element.style.setProperty('--ty-auction-available', Math.max(0, innerHeight - element.getBoundingClientRect().top - Math.max(8, bottom)) + 'px');
      }
    };
    fit(); window.addEventListener('resize', fit);
    return () => window.removeEventListener('resize', fit);
  }, [data.trade_day, props.asOfTime, props.busy]);
  useEffect(() => {
    // Reveal the same stock inside each independent scroll area, without
    // moving the document or resetting a user's scroll on quote refresh.
    comparison.current?.querySelectorAll<HTMLOListElement>('.ty-auction-stock-list').forEach(list => {
      const row = list.querySelector<HTMLElement>('li[data-current]');
      if (!row) return;
      const bounds = list.getBoundingClientRect(), item = row.getBoundingClientRect();
      if (item.top < bounds.top || item.bottom > bounds.bottom) list.scrollTop += item.top - bounds.top;
    });
  }, [selected?.code, data.trade_day]);
  const dates = [...new Set([data.trade_day, ...data.auction_days.map(day => day.day), ...data.available_days])].filter(Boolean).filter(day => !data.trade_day || day <= data.trade_day).sort().reverse().slice(0, 4);
  const downDays = support.data?.data.down_days ?? [];
  const compareDays = [data.trade_day, support.data?.data.previous_trade_day || dates[1] || ''];
  const columns = [...compareDays, ...compareDays];
  const selectedDays = dates.map(day => {
    const existing = data.auction_days.find(entry => entry.day === day)?.items.find(stock => stock.code === selected?.code);
    const down = downDays.find(entry => entry.day === day)?.rows?.find(stock => stock.code === selected?.code);
    return { day, stock: down ? { node_amounts: Object.fromEntries(nodes.map(node => [node, down.node_amounts?.[node] ?? null])) as Stock['node_amounts'],
      auction_amount_cny: down.auction_amount_cny, auction_pct: down.auction_pct,
      daily_pool: existing?.daily_pool ?? (day === data.trade_day ? selected?.daily_pool : null) } : existing };
  });
  const nodeEntries = selectedDays.slice(0, 2).map(day => ({ day: day.day, values: sealValues(day.stock) }));
  const hasNodes = selectedDays.some(day => sealValues(day.stock).some(value => value != null));
  const observed = data.auction_days.find(entry => entry.day === data.trade_day);
  const ordersState = data.capabilities.find(item => item.key === 'auction_orders');
  const nodeRows = observed?.items.some(item => sealValues(item).some(value => value != null))
    ? observed.items : data.auction_days.flatMap(entry => entry.items);
  const latestNode = [...nodes].reverse().find(value => nodeRows.some(item => item.node_amounts?.[value] != null));
  const hasCurrentSeal = observed?.items.some(item => item.auction_current_seal_cny != null) ?? false;
  const node = chosenNode?.day === data.trade_day ? chosenNode.value : latestNode ?? '09:25';
  const sorting = chosenSorting ?? (hasCurrentSeal ? 'current' : latestNode ? 'seal' : 'auction');
  const chart = chosenChart ?? (hasNodes || selected?.auction_current_seal_cny != null ? 'seals' : 'traded');
  function sortBy(value:AuctionSort, stage?:Node) {
    setReset(reset+1);setChosenSorting(value);
    if(stage)setChosenNode({day:data.trade_day,value:stage});
  }
  return <div ref={paneScroll}>
    <div ref={workspace} className="ty-workspace ty-auction-workspace ty-auction-reference-workspace ty-auction-support-workspace"><div className="ty-main-column">
      <Panel title="多日封单对照" action={<div className="ty-auction-sort-tools"><button type="button" className="ty-text-button" aria-pressed={sorting==='amount'} aria-label="按竞价成交额排序" onClick={()=>sortBy('amount')}>竞价成交额</button><span>亿元</span></div>} className="ty-auction-comparison">
        <div className="ty-auction-additions" role="group" aria-label="竞价榜单范围"><button type="button" aria-pressed={range==='all'} onClick={()=>{setReset(reset+1);setRange('all');}}>封单金额</button><button type="button" aria-pressed={range==='ladder'} title="各日昨日连板达到2板及以上" onClick={()=>{setReset(reset+1);setRange('ladder');}}>昨日连板</button></div>
        {ordersState?.status === 'stale' && <p role="status" className="ty-inline-note">{ordersState.reason}</p>}
        <div className="ty-auction-days" ref={comparison}>{columns.map((day, col) => {
          if (col >= 2) return <AuctionDown key={'down:' + (day || String(col))} day={day} data={downDays.find(entry => entry.day === day)} identity={readingKey} selected={selected?.code} onSelect={selectStock} busy={support.busy} error={!!support.error} reload={support.reload}/>;
          const entry = (props.auctionRankings ?? data.auction_days).find(entry => entry.day === day);
          const rolling = entry?.items.some(item => item.auction_current_seal_cny != null) ?? false;
          const columnNode = sorting === 'current' ? [...nodes].reverse().find(value => entry?.items.some(item => item.node_amounts?.[value] != null)) ?? node : node;
          const ranked=rankAuction((entry?.items??[]).filter(row=>range!=='ladder'||(row.previous_height??0)>=2),sorting,columnNode);
          const sourceHint = `${entry?.node_source || '未保存封单来源'} · 采集 ${dateTime(entry?.node_received_at)}`;
          return <StableAuctionOrder key={day||String(col)} rows={ranked} identity={readingKey+':'+day+':'+sorting+':'+node+':'+range} reset={reset}>{(items,changed,apply)=><section className={'ty-auction-day' + (col === 0 ? ' is-current-day' : '') + (rolling ? ' has-current-seal' : '')} key={day || String(col)} aria-label={day ? `${day}竞价对照` : `待采集对照日${col + 1}`}><h3 aria-label={`${day}涨停`}><time dateTime={day}>{day || '—'}</time><span className="ty-auction-category">涨停</span></h3>
            <div className="ty-auction-summary" aria-label={`${day}封单来源`} title={sourceHint} tabIndex={0}><span>{entry?.one_word_count != null ? <>一字 <b>{number(entry.one_word_count)}</b>只</> : <><b>{entry?.node_status === 'available' ? entry.items.length : '—'}</b>只</>}</span><span className="ty-auction-summary-divider" aria-hidden="true"/><span>封单 <b>{money(entry?.header_total_cny).replace(/\.?0+$/, '')}</b>亿</span></div><div className="ty-auction-node-head">
              {nodes.map(value => <button key={value} type="button" disabled={!day} className={columnNode === value ? 'ty-gold' : ''} aria-label={`按${day} ${value}封单排序`} aria-pressed={sorting==='seal'&&node===value} onClick={()=>sortBy('seal',value)}>{value.replace(/^0/, '')}</button>)}
              {rolling && <button type="button" className="ty-gold" title="当前封单" aria-label={`按${day}当前封单排序`} aria-pressed={sorting==='current'} onClick={()=>sortBy('current')}>当前</button>}
              <button type="button" disabled={!day} aria-label={`按${day}竞价涨幅排序`} aria-pressed={sorting==='auction'} onClick={()=>sortBy('auction')}>竞价涨幅{sorting === 'auction' && <svg className="ty-sort-direction" width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M6 1v9m-3-3 3 3 3-3"/></svg>}</button>
            </div>
            <ol data-scroll-key={day+':'+range} className="ty-auction-stock-list" tabIndex={0} aria-label={`${day}封单滚动列表`}>{items.map((stock, index) => <li key={stock.code} data-current={selected?.code === stock.code || undefined}>
              <AuctionRow code={stock.code} name={stock.name} selected={selected?.code === stock.code} onSelect={selectStock}
                description={`${stock.name} ${stock.code}\n${stock.auction_tags?.join(' / ') || stock.theme || ''}${stock.source_name ? '\n实际供数：' + stock.source_name : ''}\n${quoteTiming(stock)}`}>
                <span className="ty-auction-rank">{index + 1}</span><span className="ty-auction-stock">
                  <span className="ty-auction-name"><span className="ty-auction-labels"><span className="ty-stock-name"><span className="ty-stock-label">{stock.name}</span></span>{[...new Set(stock.auction_tags?.length ? stock.auction_tags : stock.theme ? [stock.theme] : [])].filter(tag => tag !== stock.auction_board).map(tag => <span className="ty-tag" title={tag} key={tag}>{tag}</span>)}</span>{stock.auction_board ? <span className="ty-auction-board"><span className="ty-tag ty-tag-gold">{stock.auction_board}</span></span> : stock.previous_height != null && stock.previous_height > 0 && <span className="ty-auction-board"><span className="ty-tag ty-tag-gold">昨{stock.previous_height}板</span></span>}</span>
                  <span className="ty-auction-values">{nodes.map(value => <span key={value} className={value === columnNode ? 'ty-gold' : ''}>{money(stock.node_amounts?.[value],2)}</span>)}{rolling && <span className="ty-gold" title={`当前封单 · 本机采集 ${dateTime(stock.node_received_at)}`}>{money(stock.auction_current_seal_cny, 2)}</span>}<Change value={auctionChange(stock)}/></span>
                </span>
              </AuctionRow>
            </li>)}{Array.from({ length: Math.max(0, 8 - items.length) }, (_, i) => <li className="ty-auction-blank" key={`empty-${i}`} aria-hidden="true"><span>{items.length + i + 1}</span><div><i/>{nodes.map(value => <span key={value}>—</span>)}</div></li>)}</ol>
            {!items.length && <div className="ty-auction-no-data">{day ? '该日竞价记录尚未采集' : '等待对照日记录'}</div>}<div className="ty-panel-caption">{ranked.length}只{changed&&<button onClick={apply}>更新排序</button>}</div>
          </section>}</StableAuctionOrder>;
        })}</div><div className="ty-auction-note"><b>—</b> 表示该阶段尚未采到；封单金额与竞价成交额分别统计。<span>{sorting === 'auction' ? '按网站当时涨幅 / 实际竞价结果排序' : sorting==='amount'?'按实际竞价成交额排序': sorting === 'current' ? '按当前封单排序 · 历史按各日最新阶段' : `按${node}封单排序`}</span></div>{(latestNode || hasCurrentSeal) && <p className="ty-inline-note">来源时点未知 · 表内封单快照 · 09:15 / 09:20 / 09:25 为供应商阶段列。当前封单是本次采集的滚动值。</p>}
      </Panel>
      <AuctionSupport read={support} identity={readingKey} selected={selected?.code} onSelect={selectStock} onDay={props.selectDay}/>
    </div><AuctionRankings read={support} identity={readingKey} selected={selected?.code} onSelect={selectStock}/><aside className="ty-auction-rail" aria-label="竞价详情与封单观察"><div className="ty-auction-detail-stack"><Panel title={<>{selected?.name || '个股竞价详情'} <span className="ty-title-code">{selected?.code.split('.')[0] || ''}</span></>} subtitle={selected?.theme ? <Tag>{selected.theme}</Tag> : undefined} action={<Follow selected={selected} watch={watch}/>} className="ty-auction-detail">
          <div className="ty-chart-tabs" role="tablist" aria-label="竞价比较内容"><button type="button" role="tab" aria-selected={chart === 'traded'} className={chart === 'traded' ? 'is-selected' : ''} onClick={() => setChosenChart('traded')}>实际竞价成交 · 跨日比较</button><button type="button" role="tab" aria-selected={chart === 'seals'} className={chart === 'seals' ? 'is-selected' : ''} onClick={() => setChosenChart('seals')}>三个节点封单</button><span>单位：亿元</span></div>
          {chart === 'traded' ? <AuctionChart compact entries={selectedDays.map(day => ({ day: day.day, values: [day.stock?.auction_amount_cny ?? null] }))}/> : <><div className="ty-chart-legend">{nodeEntries.map((entry, i) => <span key={entry.day}><i style={{ background: i === 0 ? '#d9af58' : '#648096' }}/>{shortDay(entry.day)}{i === 0 ? ' 观察日' : ' 对照日'}</span>)}</div><AuctionChart compact entries={nodeEntries} nodeMode/></>}
          {selected && <div className="ty-table-scroll"><table className="ty-table" aria-label="所选股票跨日状态"><colgroup><col/><col/><col/><col/></colgroup><thead><tr><th>日期</th><th>竞价成交 / 涨幅</th><th>日榜状态</th><th title="竞价阶段封单记录">节点</th></tr></thead><tbody>{selectedDays.map(({ day, stock }) => <tr key={day}><td>{shortDay(day)}</td><td>{money(stock?.auction_amount_cny, 3)}亿 / <Change value={stock?.auction_pct}/></td><td>{stock?.daily_pool ? <span title={`${stock.daily_pool.source_name} · 接收 ${dateTime(stock.daily_pool.received_at)}`}>{{ U: '涨停', D: '跌停', Z: '炸板' }[stock.daily_pool.state]}{stock.daily_pool.state === 'U' && stock.daily_pool.board_height != null ? ` · ${stock.daily_pool.board_height}板` : ''}{stock.daily_pool.first_limit_time && <small> · 首封 {time(stock.daily_pool.first_limit_time)}</small>}</span> : '日榜尚未保存'}</td><td title={sealValues(stock).some(value => value != null) ? '有竞价阶段封单' : '该日无已存竞价封单样本'} aria-label={sealValues(stock).some(value => value != null) ? '有竞价阶段封单' : '该日无已存竞价封单样本'}>{sealValues(stock).some(value => value != null) ? '有' : '无'}</td></tr>)}</tbody></table><p className="ty-panel-caption" tabIndex={0} title="盘中首次涨停的股票可能不在当日竞价封单表中；日榜状态与竞价成交独立展示，日榜封单不充当竞价节点。" aria-label="盘中首次涨停的股票可能不在当日竞价封单表中；日榜状态与竞价成交独立展示，日榜封单不充当竞价节点。">日榜与竞价独立统计</p></div>}
          <div className="ty-auction-metrics"><span>竞价成交价 <strong>{number(selected?.auction_price, 2)}</strong></span><span>竞价成交额 <strong>{money(selected?.auction_amount_cny)}<small>亿</small></strong></span><span>竞价涨幅 <strong><Change value={selected?.auction_pct}/></strong></span><span>量比 <strong>{number(selected?.volume_ratio, 2)}</strong></span></div>
          {selected?.node_source && <p className="ty-inline-note">封单：{selected.node_source} · 本机采集 {dateTime(selected.node_received_at)}{selected.auction_current_seal_cny != null && <> · 当前封单 {money(selected.auction_current_seal_cny)}亿</>} · 网站当时涨幅 <Change value={selected.auction_indicative_pct}/></p>}
          {selected && !selected.instrument_id && <p className="ty-inline-note">证券目录尚未收录，补齐后可以加入关注。</p>}
        </Panel><AuctionReference user={props.session.data.user.id} cutoff={props.asOfTime} onSelect={selectStock}/></div></aside></div>
  </div>;
}
