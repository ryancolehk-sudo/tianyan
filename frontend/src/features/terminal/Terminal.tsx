import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import type { SessionResponse, WatchlistItem } from '../../generated/schema';
import { api } from '../../shared/api';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { Coverage } from './Components';
import type { PageProps, WatchState } from './Components';
import { dateTime, emptyWorkbench, useWorkbench } from './data';
import { useWorkbenchUpdates } from '../../shared/workbenchUpdates';
import { useThsFunds, withThsFunds, projectThsFunds } from './thsFundsData';
import { OverviewWorkspace } from './OverviewWorkspace';
import { usePaneScroll } from '../../shared/workspaceState';
import { DateContextNotice } from './DateContextNotice';
import './additiveExperience.css';
import type { Stock, TerminalView } from './types';
import { useTerminalDetails } from './details';
import { useSectorTracking } from './sectorTrackingData';
import { latestFundSector } from './sourceTiming';
import { useQuoteFocus } from './useQuoteFocus';
import './Terminal.css';
import './auctionDensity.css';
import './auctionSupport.css';
import './overviewUnified.css';
import './overviewStrengthLayout.css';
import './overviewWorkspace.css';
const Auction = lazy(() => import('./Auction').then(module => ({ default: module.Auction })));
const Funds = lazy(() => import('./Funds').then(module => ({ default: module.Funds })));
const LadderWorkspace = lazy(() => import('./LadderWorkspace').then(module => ({ default: module.LadderWorkspace })));

function useTerminalWatchlist(session: SessionResponse): WatchState {
  const [items, setItems] = useState<WatchlistItem[]>([]);
  const [busy, setBusy] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef(false);
  const live = useRef(false);
  const current = useRef(items); current.current = items;
  useEffect(() => {
    let active = true; live.current = true; setBusy(true); setItems([]); setError('');
    api.watchlist().then(response => { if (active) setItems(response.data.items); })
      .catch(reason => { if (active) setError(reason instanceof Error ? reason.message : '关注列表读取失败。'); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; live.current = false; };
  }, [session.data.user.id]);
  const has = (stock: Pick<Stock, 'instrument_id'>) => !!stock.instrument_id && items.some(item => item.instrument.id === stock.instrument_id);
  const toggle = async (stock: Pick<Stock, 'instrument_id'>) => {
    if (!stock.instrument_id || pending.current || busy) return;
    const ids = current.current.map(item => item.instrument.id).filter((id): id is string => !!id);
    const next = ids.includes(stock.instrument_id) ? ids.filter(id => id !== stock.instrument_id) : [...ids, stock.instrument_id];
    pending.current = true; setSaving(true); setError('');
    try {
      const response = await api.saveWatchlist(next, session.data.csrf_token, crypto.randomUUID());
      if (live.current) setItems(response.data.items);
    } catch (reason) { if (live.current) setError(reason instanceof Error ? reason.message : '关注列表保存失败，请重试。'); }
    finally { pending.current = false; if (live.current) setSaving(false); }
  };
  return { items, busy, saving, error, has, toggle };
}

export function Terminal({ session, view }: { session: SessionResponse; view: TerminalView }) {
  return <Suspense fallback={<p role="status">正在载入观察页面…</p>}>{view === 'ladder' ? <LadderWorkspace session={session}/> : <OriginalTerminal session={session} view={view}/>}</Suspense>;
}

function OriginalTerminal({ session, view }: { session: SessionResponse; view: TerminalView }) {
  const navigation = useNavigation();
  const query = new URLSearchParams(navigation.search);
  const day = query.get('trade_day') ?? '';
  const stockCode = query.get('stock') ?? '';
  const sectorCode = query.get('sector') ?? '';
  const asOfTime = query.get('as_of_time') ?? '';
  useWorkbenchUpdates(session.data.user.id, !!asOfTime || !!day && day < new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date()));
  const { data: loaded, readPath, readMeta, busy, error, reload } = useWorkbench(view, day, stockCode, sectorCode, session.data.user.id, query.get('instrument') ?? '', asOfTime);
  const acceptance = /^[a-z0-9][a-z0-9-]{0,63}$/i.test(query.get('acceptance_run') ?? '');
  const market = loaded ?? emptyWorkbench(view, day);
  const fundsDay = day || (view === 'overview' ? market.trade_day : '');
  const thsReads = useThsFunds(view === 'funds' || view === 'overview', fundsDay, asOfTime, session.data.user.id);
  const thsProjection = useMemo(()=>projectThsFunds(thsReads,fundsDay,market.trade_day),[thsReads,fundsDay,market.trade_day]);
  const watch = useTerminalWatchlist(session);
  const chosenStock = query.has('stock') ? stockCode : market.selected_stock_code || '';
  useQuoteFocus('selected',chosenStock?[chosenStock]:[],{session,day:market.trade_day,cutoff:asOfTime,
    enabled:market.capabilities.some(item=>item.key==='focused_quotes'&&item.status==='available')});
  const paneScroll=usePaneScroll(`${session.data.user.id}:original:${view}:${market.trade_day}:${asOfTime}`,'.ty-table-scroll:not(.rw-root *)');
  const chosenSector = sectorCode || market.selected_sector_code || '';
  const tracking = useSectorTracking(loaded, chosenSector, asOfTime, session.data.user.id, view === 'overview');
  const monitor = view === 'overview' && (query.get('workspace') || 'monitor') !== 'research';
  const priority = [...watch.items.map(item => `${item.instrument.symbol}.${item.instrument.exchange === 'SSE' ? 'SH' : item.instrument.exchange === 'SZSE' ? 'SZ' : 'BJ'}`),
    ...[...market.stocks].sort((a,b) => (b.pct_change ?? -Infinity) - (a.pct_change ?? -Infinity)).slice(0,8).map(item => item.code),
    ...[...market.stocks].sort((a,b) => (b.speed_5m ?? -Infinity) - (a.speed_5m ?? -Infinity)).slice(0,8).map(item => item.code),
    ...market.auction_days.flatMap(entry => [...entry.items].sort((a,b) => (b.auction_pct ?? -Infinity) - (a.auction_pct ?? -Infinity)).slice(0,8).map(item => item.code)),
    ...market.stocks.slice(0, 32).map(item => item.code)];
  const details = useTerminalDetails(loaded, chosenStock, view === 'funds' || monitor ? '' : chosenSector, asOfTime, priority);
  const sameStock = chosenStock === market.selected_stock_code;
  const sameSector = chosenSector === market.selected_sector_code;
  const originalData = { ...market, selected_stock_code: chosenStock, selected_sector_code: chosenSector,
    stocks: market.stocks.map(stock => stock.code === details.detail?.stock.code
      ? { ...stock, ...details.detail.stock, theme: details.detail.stock.theme ?? stock.theme } : stock),
    selected_members: details.board?.members ?? (sameSector ? market.selected_members : []),
    members_trade_day: details.board?.day ?? (sameSector ? market.members_trade_day : null),
    events: details.detail?.events ?? (sameStock ? market.events : []),
    stock_seals: details.detail?.seals ?? (sameStock ? market.stock_seals : []),
    funds_history: details.board?.history.length ? [...market.funds_history.filter(item => item.sector_code !== chosenSector), ...details.board.history] : market.funds_history,
    auction_days: market.auction_days.map(entry => ({ ...entry, items: [...entry.items.filter(item => item.code !== details.detail?.stock.code), ...(details.detail?.auctions.find(item => item.day === entry.day)?.items ?? [])] })) };
  const data = view === 'funds' ? withThsFunds(originalData, thsReads, query.get('fund_sector') ?? '', fundsDay,thsProjection) : originalData;
  const pool = view === 'auction' ? [
    ...(data.auction_days.find(entry => entry.day === data.trade_day)?.items ?? []), ...data.stocks,
    ...data.auction_days.flatMap(entry => entry.items), ...data.selected_members, ...data.broken,
  ] : [...data.stocks, ...data.selected_members, ...data.broken, ...data.auction_days.flatMap(entry => entry.items)];
  const selected = !chosenStock ? null : data.selected_members.find(stock => stock.code === chosenStock) && details.detail
    ? { ...details.detail.stock, theme: data.selected_members.find(stock => stock.code === chosenStock)!.theme }
    : details.detail?.stock ?? pool.find(stock => stock.code === chosenStock) ?? null;
  const sector = latestFundSector(data.intraday_funds, data.selected_sector_code, data.intraday_funds?.trade_day ?? data.trade_day)
    ?? data.sectors.find(item => item.code === data.selected_sector_code) ?? null;
  useEffect(() => {
    window.dispatchEvent(new CustomEvent('ty-terminal-context', { detail: { stock_name: selected?.name ?? null,
      sector_name: sector?.name ?? null, trade_day: loaded?.trade_day ?? day, latest_at: loaded?.latest_at ?? null, mode: loaded?.mode } }));
  }, [selected?.name, sector?.name, loaded?.trade_day, loaded?.latest_at, loaded?.mode, day]);
  const selectStock = (code: string) => { const target = new URL(location.href); target.searchParams.set('stock', code); target.searchParams.delete('instrument'); target.searchParams.delete('research_detail'); updateLocation(target); };
  const selectSector = (code: string) => { const target = new URL(location.href); target.searchParams.set(view === 'funds' ? 'fund_sector' : 'sector', code); updateLocation(target); };
  const selectDay = (value: string) => { const target = new URL(location.href); if (value) target.searchParams.set('trade_day', value); else target.searchParams.delete('trade_day'); target.searchParams.delete('as_of_time'); updateLocation(target); };
  const selectCutoff = (value: string) => { const target = new URL(location.href); if (value) { target.searchParams.set('as_of_time', value); if (!target.searchParams.has('trade_day') && data.trade_day) target.searchParams.set('trade_day', data.trade_day); } else target.searchParams.delete('as_of_time'); updateLocation(target); };
  const props: PageProps = { data, selected, sector, selectStock, selectSector, selectDay, watch, session, busy, asOfTime,
    detailBusy: details.busy, sectorBusy: view === 'overview' ? tracking.busy : details.sectorBusy, detailError: details.error, retryDetails: details.retry,
    auctionRankings: market.auction_days,
    trackingMembers: view === 'overview' ? tracking.members : details.board?.trackingMembers,
    trackingDay: view === 'overview' ? tracking.day : details.board?.day,
    trackingMemberCount: view === 'overview' ? tracking.count : details.board?.trackingMemberCount,
    trackingTruncated: view === 'overview' ? tracking.truncated : details.board?.trackingTruncated,
    sectorError: view === 'overview' ? tracking.error : details.sectorError,
    retrySector: view === 'overview' ? tracking.reload : details.retrySector };
  return <div ref={paneScroll} className={`ty-terminal ty-terminal-${view}`} aria-label={{ overview: '综合盯盘', auction: '竞价观察', ladder: '连板观察', funds: '资金动向' }[view]} aria-busy={busy}
    data-acceptance-read-key={acceptance ? readPath : undefined}
    data-acceptance-revision={acceptance ? readMeta?.read_cache?.revision : undefined}
    data-acceptance-request={acceptance ? readMeta?.request_id : undefined}>
    {busy && !loaded && <div className="ty-loading-strip" role="status"><span/>正在读取本机已采集数据…</div>}
    {loaded && view !== 'overview' && view !== 'auction' && <div className="ty-data-timestamp">{view === 'funds'
      ? '资金按所选来源独立展示 · 覆盖范围及实际时点见下方'
      : loaded.latest_at ? <>资料最近接收 <time dateTime={loaded.latest_at}>{dateTime(loaded.latest_at)}</time> · 行情时点见对应数据</> : '资料接收时间待核'}</div>}
    <DateContextNotice data={loaded} requestedDay={day} asOfTime={asOfTime} reload={reload}/>
    {error && <p className="ty-error-message" role="alert">{error}<button onClick={reload}>重读本页资料</button></p>}
    {asOfTime && <div className="ty-replay-banner" role="status"><span>当时已知回看</span> 截至 {dateTime(asOfTime)}（北京时间）<small>仅使用该时点前已收到的证据</small></div>}
    <Suspense fallback={<p className="ty-loading-strip" role="status">正在载入观察页面…</p>}>{view === 'overview' ? <OverviewWorkspace {...props} selectCutoff={selectCutoff} fundsData={withThsFunds(originalData, thsReads, query.get('fund_sector') ?? '', fundsDay,thsProjection)} selectFund={code => { const target = new URL(location.href); target.searchParams.set('fund_sector',code); updateLocation(target); }}/> : view === 'auction' ? <Auction {...props}/> : <Funds {...props}/>}</Suspense>
    {watch.error && view !== 'auction' && view !== 'ladder' && <p className="ty-error-message" role="alert">{watch.error}</p>}
    {view !== 'overview' && view !== 'auction' && <Coverage data={data} asOfTime={asOfTime} selectCutoff={selectCutoff}/>}
  </div>;
}
