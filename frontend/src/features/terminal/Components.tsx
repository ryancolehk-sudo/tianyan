import { useEffect, useState } from 'react';
import { AiAction } from '../ai/AiAction';
import type { ReactNode } from 'react';
import type { SessionResponse, WatchlistItem } from '../../generated/schema';
import type { Sector, SectorMember, Stock, WorkbenchData } from './types';
import { dateTime, money, number, percent, signedMoney, time, tone } from './data';
import { hintDetail } from './details';
import { marketPhaseLabel, useMarketSession } from '../../shared/marketSession';
import { useNavigation } from '../../shared/navigation';
import { latestFundSector, latestSourceTime, missingClosingSample } from './sourceTiming';
import { stockThemes } from './stockThemes';
import { ReadableText, useTextHint } from '../../shared/ReadableText';
import { monitorHref } from '../../shared/dataMonitorLink';

export type WatchState = {
  items: WatchlistItem[]; busy: boolean; saving: boolean; error: string;
  toggle: (stock: Pick<Stock, 'instrument_id'>) => Promise<void>; has: (stock: Pick<Stock, 'instrument_id'>) => boolean;
};
export type PageProps = {
  data: WorkbenchData; selected: Stock | null; sector: Sector | null;
  selectStock: (code: string) => void; selectSector: (code: string) => void; selectDay: (day: string) => void; watch: WatchState;
  session: SessionResponse; busy: boolean; asOfTime: string;
  detailBusy?: boolean; sectorBusy?: boolean; detailError?: string; retryDetails?: () => void;
  auctionRankings?: WorkbenchData['auction_days'];
  trackingMembers?: SectorMember[]; trackingDay?: string | null; sectorError?: string; retrySector?: () => void;
  trackingMemberCount?: number; trackingTruncated?: boolean;
};
export function Panel({ title, subtitle, action, children, className = '', id }: {
  title: ReactNode; subtitle?: ReactNode; action?: ReactNode; children: ReactNode; className?: string; id?: string;
}) {
  return <section className={'ty-panel ' + className} id={id}>
    <div className="ty-panel-head"><div className="ty-panel-label"><h2>{title}</h2>{subtitle && <span className="ty-panel-subtitle">{subtitle}</span>}</div>{action && <div className="ty-panel-action">{action}</div>}</div>
    {children}
  </section>;
}
export function Empty({ title = '暂无已采集记录', detail, compact = false }: { title?: string; detail?: string; compact?: boolean }) {
  return <div className={'ty-empty' + (compact ? ' ty-empty-compact' : '')}><span aria-hidden="true" className="ty-empty-mark">◇</span><strong>{title}</strong>{detail && <small>{detail}</small>}</div>;
}
export function Change({ value, digits }: { value: number | null | undefined; digits?: number }) {
  return <span key={String(value)} className={tone(value)+' ty-number-change'}>{digits == null ? percent(value) : number(value, digits)}</span>;
}
export function Tag({ children, gold = false }: { children: ReactNode; gold?: boolean }) {
  return !gold && typeof children === 'string' ? <ReadableText className="ty-tag" text={children}/> : <span className={'ty-tag' + (gold ? ' ty-tag-gold' : '')}>{children}</span>;
}
export function Follow({ selected, watch }: { selected: Stock | null; watch: WatchState }) {
  return <button className={'ty-button ty-follow' + (selected && watch.has(selected) ? ' ty-button-gold' : '')}
    type="button" disabled={!selected?.instrument_id || watch.busy || watch.saving}
    onClick={() => selected && void watch.toggle(selected)}
    title={!selected ? '先选择股票' : !selected.instrument_id ? '证券目录尚未收录，暂不能加入关注' : undefined}
    aria-pressed={selected ? watch.has(selected) : false}>
    <span aria-hidden="true">{selected && watch.has(selected) ? '★' : '☆'}</span>{watch.saving ? '保存中' : selected && watch.has(selected) ? '已关注' : '加入关注'}
  </button>;
}
export function quoteTiming(stock: Pick<Stock, 'source_time' | 'price_basis' | 'source_received_at' | 'source_snapshot_time'>) {
  return stock.price_basis === 'snapshot' ? `服务器快照 ${dateTime(stock.source_snapshot_time)} · 接收 ${dateTime(stock.source_received_at)} · 逐股成交时刻待核`
    : stock.price_basis === 'daily_close' ? `所选交易日收盘日线 · 归档接收 ${dateTime(stock.source_received_at)}`
    : stock.source_time ? `行情时点 ${dateTime(stock.source_time)}（北京时间） · 接收 ${dateTime(stock.source_received_at)}` : '行情时点待核对';
}
export function StockName({ stock, onSelect }: { stock: Pick<Stock, 'code' | 'name' | 'source_time' | 'source_name' | 'price_basis' | 'source_received_at' | 'source_snapshot_time'>; onSelect: (code: string) => void }) {
  const description = `${stock.name} ${stock.code}${stock.source_name ? `\n实际供数：${stock.source_name}` : ''}\n${quoteTiming(stock)}`;
  const hint = useTextHint<HTMLButtonElement>(description);
  return <><button ref={hint.anchor} type="button" className="ty-stock-name" {...hint.handlers}
    aria-describedby={hint.describedBy} title={hint.describedBy ? undefined : description} onMouseEnter={() => hintDetail(stock.code)}
    onFocus={() => { hint.show(); hintDetail(stock.code); }}
    onClick={event => { event.stopPropagation(); hint.show(); onSelect(stock.code); }}><span className="ty-stock-label">{stock.name}</span><small>{stock.code.split('.')[0]}</small></button>{hint.popup}</>;
}

export function MarketRibbon({ data, funds = false, asOfTime = '', intradayFunds = false, fundsCount, fundsExpected, fundsComplete, fundsCategory }: { data: WorkbenchData; funds?: boolean; asOfTime?: string; intradayFunds?: boolean; fundsCount?: number; fundsExpected?: number | null; fundsComplete?: boolean | null; fundsCategory?: string }) {
  const { market } = useMarketSession(data.market_session);
  const navigation = useNavigation();
  const explicitDay = new URLSearchParams(navigation.search).get('trade_day');
  const historical = !!asOfTime || data.mode === 'observed' || !!explicitDay && (!market || explicitDay !== market.trade_day);
  const indices = [
    { label: '上证', code: '000001.SH' }, { label: '深证', code: '399001.SZ' }, { label: '创业板', code: '399006.SZ' },
  ];
  const total = (data.breadth.up ?? 0) + (data.breadth.down ?? 0) + (data.breadth.flat ?? 0);
  const actualFundsDay = intradayFunds ? data.intraday_funds?.trade_day : data.funds_trade_day === undefined ? data.trade_day : data.funds_trade_day;
  const stockSource = latestSourceTime(data.stocks.map(item => item.source_time));
  const indexSource = latestSourceTime(data.indices.map(item => item.source_time));
  const sourceTime = stockSource ?? indexSource;
  const snapshotTime = latestSourceTime([...data.stocks, ...data.indices].filter(item => item.price_basis === 'snapshot').map(item => item.source_snapshot_time));
  const dailyClose = data.breadth.basis === 'daily_close';
  const closingMissing = !dailyClose && !historical && missingClosingSample(data.trade_day, sourceTime, market);
  const staleQuotes = data.capabilities.some(item => item.key === 'quotes' && item.status === 'stale');
  const delayed = !historical && !!market && market.phase === 'trading' && (staleQuotes || data.trade_day !== market.trade_day || !sourceTime || Date.parse(market.server_time) - Date.parse(sourceTime) > 120_000);
  const liveLabel = data.mode === 'observed' ? '当时已知' : market && (data.mode === 'live' || data.trade_day === market.trade_day) ? market.is_trading_day === false ? '休市' : marketPhaseLabel[market.phase] : data.mode === 'live' ? '盘中采样' : '历史回看';
  const statusLabel = dailyClose ? '收盘日线已归档' : snapshotTime ? '价格快照 · 成交时刻待核' : closingMissing ? '已收盘 · 缺少收盘样本' : delayed && data.mode !== 'observed' ? data.trade_day !== market?.trade_day ? '等待今日采样' : '行情待更新' : liveLabel;
  const amount = data.breadth.amount_cny;
  return <section className="ty-market-ribbon" aria-label="市场概览">
    {indices.map(index => { const row = data.indices.find(item => item.code === index.code); return <div className="ty-index" key={index.code} title={`${row?.source_name ? `实际供数：${row.source_name}\n` : ''}${row ? quoteTiming(row) : '行情时点待核对'}`}><b>{index.label}</b><strong className={tone(row?.pct_change)}>{percent(row?.pct_change)}</strong><small>{number(row?.price, 2)}</small></div>; })}
    <div className="ty-ribbon-turnover"><span>成交额</span><strong>{amount == null ? '—' : Math.abs(amount) >= 1_000_000_000_000 ? `${number(amount / 1_000_000_000_000, 2)}万亿` : `${money(amount, 1)}亿`}</strong></div>
    {funds ? <><div className="ty-ribbon-stat"><span>{fundsCategory || '板块'}资金覆盖</span><strong>{number(fundsCount ?? (intradayFunds ? data.intraday_funds?.sectors.length ?? 0 : data.sectors.length))}{intradayFunds && fundsExpected != null && <>/{number(fundsExpected)}</>}<small> 板块{intradayFunds ? fundsComplete === false ? ' · 未齐' : fundsExpected == null ? ' · 总数待核' : '' : ' · 日频已存'}</small></strong></div><div className="ty-ribbon-stat"><span>{data.intraday_funds?.time_basis === 'received_at' ? '采集日期' : '数据日期'}</span><strong className="ty-ribbon-date">{actualFundsDay || '—'}<small> {intradayFunds ? market?.phase === 'closed' ? '已存采样' : '盘中' : '盘后'}</small></strong></div></> : <>
      <div className="ty-market-breadth"><div><span>上涨 <b className="ty-up">{number(data.breadth.up)}</b></span><i>·</i><span>下跌 <b className="ty-down">{number(data.breadth.down)}</b></span></div><div className="ty-distribution" aria-label="涨跌家数占比"><i style={{ width: total ? `${100 * (data.breadth.up ?? 0) / total}%` : '0%' }}/><i style={{ width: total ? `${100 * (data.breadth.down ?? 0) / total}%` : '0%' }}/></div></div>
      <div className="ty-ribbon-stat"><span>涨停</span><strong className="ty-up">{number(data.breadth.limit_up)}</strong><span>跌停</span><strong className="ty-down">{number(data.breadth.limit_down)}</strong></div>
    </>}
    <div className="ty-ribbon-stamp" title={snapshotTime ? `服务器价格快照 ${dateTime(snapshotTime)}；逐股成交时刻待核。涨跌曲线保留独立采样，末点 ${dateTime(latestSourceTime(data.breadth.points.map(item => item.time)))}。` : dailyClose ? `${data.trade_day} 收盘日线；归档接收 ${dateTime(data.breadth.source_received_at)}，接收时间不代表报价时刻。指数各自口径见悬浮说明。` : sourceTime ? `个股源时点 ${dateTime(stockSource)}，指数源时点 ${dateTime(indexSource)}，本机最近接收 ${dateTime(data.latest_at)}；接收时间不代表行情更新。` : '行情源时点待核对'}><span className={'ty-status-dot' + (!snapshotTime && (dailyClose || sourceTime && !delayed && !closingMissing) ? ' is-ready' : '')}/><span>{funds ? '价格：' : ''}{statusLabel} <b>{dailyClose ? `${data.trade_day} · 接收 ${time(data.breadth.source_received_at)}` : data.mode === 'observed' && asOfTime ? `${time(asOfTime)} 截止` : snapshotTime ? `快照 ${time(snapshotTime)}` : sourceTime ? `行情截至 ${time(sourceTime)}` : '行情时点未提供'}</b></span></div>
  </section>;
}
export function AIOverview({session, tradeDay, asOfTime}: {session?: SessionResponse; tradeDay?: string | null; asOfTime?: string}) {
  return <Panel title="AI 观察" action={<span>手动生成</span>} className="ty-ai-overview">
      <h3>题材归纳</h3>
      <AiAction feature="theme_summary" session={session} tradeDay={tradeDay} asOfTime={asOfTime}/>
    </Panel>;
}
export function AIObservation({ selected, sector, view, session, tradeDay, asOfTime, detailStatus, followPanel, overviewPanel, data, intradayFunds = true }: { selected: Stock | null; sector: Sector | null; view: string; session?: SessionResponse; tradeDay?: string | null; asOfTime?: string; detailStatus?: ReactNode; followPanel?: ReactNode; overviewPanel?: ReactNode; data?: WorkbenchData; intradayFunds?: boolean }) {
  const name = view === 'funds' ? sector?.name : selected?.name;
  const events = (data?.events ?? []).filter(event => event.code === selected?.code).slice(-3);
  const flow = data && intradayFunds ? latestFundSector(data.intraday_funds, sector?.code, data.intraday_funds?.trade_day ?? data.trade_day) : undefined;
  const themes = stockThemes(selected);
  const reviewQuery = new URLSearchParams({ view: 'overview', ...(tradeDay ? { trade_day: tradeDay } : {}),
    ...(selected ? { stock: selected.code } : {}), ...(sector ? { sector: sector.code } : {}), ...(asOfTime ? { as_of_time: asOfTime } : {}) });
  return <aside className="ty-ai-column" aria-label="AI 观察">
    {overviewPanel ?? <AIOverview session={session} tradeDay={tradeDay} asOfTime={asOfTime}/>}
    {followPanel ?? <section className="ty-panel ty-ai-follow"><h3>关注对象跟踪</h3><div className="ty-ai-subject"><strong>{name || (view === 'funds' ? '选择一个板块' : '选择一只股票')}</strong>{name && <Tag gold>重点</Tag>}</div>
      {detailStatus}
      {view !== 'funds' && selected?.source_name && <p className="ty-ai-follow-hint">实际行情来源：{selected.source_name}{` · ${quoteTiming(selected)}`}</p>}
      <p className="ty-ai-follow-hint">已保存的行情和变化记录 · 研究判断由你确认</p>
      <div className="ty-ai-timeline ty-ai-checklist" aria-label="已存对象跟踪">
        {view === 'funds' ? <>
          <div className="ty-ai-item"><span className="ty-ai-time">资金样本</span><p>{flow ? <>{data?.intraday_funds?.time_basis === 'received_at' ? '接收' : '截至'} {time(data?.intraday_funds?.time_basis === 'received_at' ? flow.received_at : flow.source_time ?? data?.intraday_funds?.latest_source_time)} · 净额 <b className={tone(flow.main_net_cny)}>{signedMoney(flow.main_net_cny)} 亿</b></> : !intradayFunds && sector?.main_net_cny != null ? <>{data?.funds_trade_day} 日频净额 <b className={tone(sector.main_net_cny)}>{signedMoney(sector.main_net_cny)} 亿</b></> : '该板块尚无已保存资金数值'}</p></div>
          <div className="ty-ai-item"><span className="ty-ai-time">相邻采样变化</span><p>{flow?.change_5m_cny != null ? <>近5分钟 <b className={tone(flow.change_5m_cny)}>{signedMoney(flow.change_5m_cny)} 亿</b></> : '缺少同源可比前值，暂不能计算5分钟变化'}</p></div>
        </> : <>
          <div className="ty-ai-item"><span className="ty-ai-time">{selected?.price_basis === 'snapshot' ? `价格快照 ${time(selected.source_snapshot_time)}` : selected?.price_basis === 'daily_close' ? `${tradeDay} 收盘日线` : `行情截至 ${time(selected?.source_time)}`}</span><p>{selected?.price != null ? <><b>{number(selected.price, 2)}</b> · <Change value={selected.pct_change}/> · {selected.status}</> : '该股尚无已保存的当日行情'}</p></div>
          {events.length ? events.map((event, index) => <div className="ty-ai-item" key={`${event.time}-${index}`}><span className="ty-ai-time">{time(event.time)}</span><p>{event.label}</p></div>) : <div className="ty-ai-item"><span className="ty-ai-time">变化记录</span><p>{selected?.latest_change || '未保存该股的可核变化记录'}</p></div>}
          {!!themes.length && <div className="ty-ai-item"><span className="ty-ai-time">关联资料</span><p>{themes.slice(0, 3).map(item => <span key={item.key} title={item.source}>{item.label} · </span>)}</p><small>板块成分与供应商标签分别保留，不作为交易信号</small></div>}
        </>}
        <div className="ty-ai-item"><span className="ty-ai-time">个人研究</span><p><a className="ty-link" href={'/research/reviews?' + reviewQuery}>查看研究记录 →</a></p><small>人工判断和预案保留原记录，不由行情自动填写</small></div>
      </div>
      <div className="ty-ai-bottom-note">基于真实证据形成观察，保留不确定性</div>
    </section>}
  </aside>;
}
const statusLabels: Record<string, string> = { ready: '可用', available: '可用', ok: '可用', partial: '部分可用', unavailable: '暂不可用', permission_denied: '权限不足', pending: '待接入', missing: '缺项', stale: '较早数据', daily: '盘后', after_market: '盘后可用', sampling: '采样积累中', collecting: '采样积累中', error: '读取异常', not_published: '未发布' };
export function Coverage({ data, asOfTime, selectCutoff }: { data: WorkbenchData; asOfTime: string; selectCutoff: (value: string) => void }) {
  const available = data.capabilities.filter(c => ['ready', 'available', 'ok', 'after_market', 'daily'].includes(c.status)).length;
  const [draft, setDraft] = useState('');
  useEffect(() => { setDraft(asOfTime ? dateTime(asOfTime).replace(' ', 'T').slice(0, 16) : data.trade_day ? `${data.trade_day}T15:00` : ''); }, [asOfTime, data.trade_day]);
  return <details className="ty-coverage"><summary><span><span className="ty-status-dot"/>数据覆盖 <b>{available}/{data.capabilities.length || '—'}</b></span><span>来源、时点与缺项说明 <span aria-hidden="true">⌄</span></span></summary>
    <form className="ty-replay-controls" onSubmit={event => { event.preventDefault(); if (draft && data.trade_day) selectCutoff(draft + (draft.length === 16 ? ':00+08:00' : '+08:00')); }}><label>当时已知回看 <input type="datetime-local" aria-label="当时已知回看时点（北京时间）" value={draft} onChange={event => setDraft(event.target.value)} required disabled={!data.trade_day}/></label><button type="submit" className="ty-button" disabled={!data.trade_day || !draft}>按此时点回看</button><button type="button" className="ty-button" disabled={!asOfTime} onClick={() => selectCutoff('')}>返回按日回看</button><small>北京时间 · 按接收时点筛选证据，后到数据不补入更早时点</small></form>
    <p className="ty-panel-caption"><a className="ty-link" href={monitorHref({ context: 'current', returnTo: location.pathname + location.search + location.hash })}>查看当前运行 · 全部来源</a><small>覆盖清单未定位单一来源，运行状态与所选历史时点分开。</small></p>
    <div className="ty-coverage-list">{data.capabilities.length ? data.capabilities.map(c => <div key={c.key}><strong>{c.label}</strong><span className={['ready', 'available', 'ok', 'after_market', 'daily'].includes(c.status) ? 'ty-down' : 'ty-gold'}>{statusLabels[c.status] ?? c.status}</span><p>{c.reason || '按实际采集情况显示'}</p><small>{c.source_family || '—'} · 接收 {dateTime(c.last_received_at)}（北京时间）</small></div>) : <p>数据服务连接后展示各模块实际覆盖，当前未填充模拟数值。</p>}</div></details>;
}
