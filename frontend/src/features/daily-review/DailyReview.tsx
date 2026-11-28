import { usePaneScroll } from '../../shared/workspaceState';
import { GroupedLimitUps } from './ReviewAnalysis';
import { invalidateReads } from '../../shared/readCache';
import { useContext, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { SessionResponse } from '../../generated/schema';
import { api, request, write } from '../../shared/api';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { reviewView } from '../../shared/reviewNavigation';
import type { ReviewView } from '../../shared/reviewNavigation';
import { CoveragePanel, Empty, Evidence, Failure, Loading, ReviewRefresh, useBoundRead } from './components';
import { NoteEditor, NoteHistory } from './Editors';
import { useNotes } from './notes';
import { isIntradaySnapshot, number, pct, referencesFrom, snapshotTime, text, tone, turnover } from './model';
import type { Coverage, Day, Ref, Series } from './model';
import type { ReviewProps } from './context';
import { Emotion } from './Emotion';
import { Direction } from './Direction';
import { DirectionBoard } from './DirectionBoard';
import { Dragon } from './Dragon';
import { PostcloseOverview } from './PostcloseOverview';
import { AnomalyMonitor } from './AnomalyMonitor';
import { NewHighs } from './NewHighs';
import './daily-review.css';
import { reviewLookback, usePreferences } from '../../shared/preferences';
import { useMarketSession } from '../../shared/marketSession';

export function DailyReview({ session }: { session: SessionResponse }) {
  const nav = useNavigation();
  if (reviewView(nav.search) === 'anomaly') return <AnomalyMonitor search={nav.search}/>;
  return <ReviewWorkspace session={session}/>;
}

function ReviewWorkspace({ session }: { session: SessionResponse }) {
  const nav = useNavigation();
  const q = new URLSearchParams(nav.search);
  const { preferences } = usePreferences();
  const { market } = useMarketSession();
  const [postcloseDay,setPostcloseDay]=useState('');
  const [postcloseCoverage,setPostcloseCoverage]=useState<Coverage[]>([]);
  const isOverview=['overview','new-highs'].includes(reviewView(nav.search));
  const explicit = q.get('trade_day') ?? '';
  const asOf = q.get('as_of_time') ?? '';
  const lookback = reviewLookback(q, preferences);
  const rangeEnd = q.get('range_end') ?? '';
  const params = new URLSearchParams({ ...(explicit ? { trade_day: explicit } : isOverview && postcloseDay ? { trade_day: postcloseDay } : {}), ...(asOf ? { as_of_time: asOf } : {}) });
  const read = useBoundRead(`day:${params}`, () => api.reviewDay(params));
  const seriesQuery = new URLSearchParams({ lookback, ...(rangeEnd ? { trade_day: rangeEnd } : {}), ...(asOf ? { as_of_time: asOf } : {}) });
  const history = useBoundRead(`series:${seriesQuery}`, () => api.reviewSeries(seriesQuery));
  const data = read.data?.data;
  const day = explicit || (isOverview && postcloseDay ? postcloseDay : data?.shared_date) || '';
  const paneScroll=usePaneScroll(`${session.data.user.id}:review:${day}:${asOf}:${q.get('view')||'overview'}`,'.dr-table-scroll,.review-extension-scroll,.ladder-matrix-scroll,.dir-matrix-scroll,.dir-funds-list');
  const waitingForCurrent = !isOverview && !explicit && !asOf && !!day && !read.busy && !read.error
    && market?.automatic_live && market.trade_day && day !== market.trade_day;
  const currentDailyNotClosed = !asOf && market?.is_trading_day === true && day === market.trade_day
    && Number.isFinite(Date.parse(market.server_time))
    && Date.parse(market.server_time) < Date.parse(`${day}T15:05:00+08:00`);
  const currentDayPending = currentDailyNotClosed && !!data && data.metrics.sample?.N == null;
  const [cutoff, setCutoff] = useState(false);
  const [toolsOpen,setToolsOpen]=useState(false);
  const tools=useRef<HTMLDetailsElement>(null),toolsTrigger=useRef<HTMLElement>(null);
  useEffect(()=>setToolsOpen(false),[nav.sequence]);
  useEffect(()=>{
    if(!toolsOpen)return;
    const outside=(event:PointerEvent)=>{if(event.target instanceof Node&&!tools.current?.contains(event.target))setToolsOpen(false);};
    const focus=(event:FocusEvent)=>{if(event.target instanceof Node&&!tools.current?.contains(event.target))setToolsOpen(false);};
    document.addEventListener('pointerdown',outside);document.addEventListener('focusin',focus);
    return()=>{document.removeEventListener('pointerdown',outside);document.removeEventListener('focusin',focus);};
  },[toolsOpen]);
  const [refreshEpoch, setRefreshEpoch] = useState(0);
  function reloadAll() { invalidateReads('/api/v1/review-data'); invalidateReads('/api/v1/direction-data'); read.reload(); history.reload(); setRefreshEpoch(v => v + 1); setReference(null); showCoverage(false); }
  const [coverage, showCoverage] = useState(false);
  const [reference, setReference] = useState<Ref | null>(null);
  const [batch, setBatch] = useState<{ id: string; direction: boolean } | null>(null);
  const [directionWindow, setDirectionWindow] = useState<{ day: string; endDay: string; lookback?: number } | null>(null);
  const [collecting, setCollecting] = useState(false);
  const [collectError, setCollectError] = useState<Error | null>(null);
  const [progress, setProgress] = useState('');
  const collectingRef = useRef(false);
  useEffect(() => {
    if (explicit || asOf || !currentDailyNotClosed) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      read.reload(); history.reload(); setRefreshEpoch(value => value + 1);
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [explicit, asOf, currentDailyNotClosed, read.reload, history.reload]);
  function select(values: Record<string, string | null>, replace = false) { const url = new URL(location.href); for (const [k, v] of Object.entries(values)) { if (v === null || v === '') url.searchParams.delete(k); else url.searchParams.set(k, v); } updateLocation(url, { replace }); }
  function changeDay(value: string) { if (!value) return; const outside = !history.data?.data.trade_days.includes(value); select({ trade_day: value, record_key: null, edit: null, ...(outside ? { range_end: value } : {}) }); }
  async function collect() {
    if (collectingRef.current || currentDailyNotClosed) return;
    collectingRef.current = true; setCollecting(true); setCollectError(null); setProgress('正在安排资料采集…');
    try { const direction = reviewView(nav.search) === 'direction'; const result = direction
      ? await write<{ data: { batch_id: string } }>('/api/v1/direction-data/collections', { source_scope: 'four_sources', ...(day ? { trade_day: day } : {}), ...(directionWindow?.day === day ? { end_day: directionWindow.endDay, lookback: directionWindow.lookback ?? 5, source_scope: 'four_sources' } : {}) }, session.data.csrf_token, crypto.randomUUID())
      : reviewView(nav.search)==='new-highs'?await write<{data:{batch_id:string}}>('/api/v1/review-data/new-highs/collections',{...(explicit?{trade_day:explicit}:{})},session.data.csrf_token,crypto.randomUUID())
      : await api.collectReview({ lookback: 60, member_lookback: 5, include_members: true, include_news: false, source_scope: 'four_sources', ...(explicit ? { trade_day: explicit } : {}) }, session.data.csrf_token, crypto.randomUUID()); setBatch({ id: result.data.batch_id, direction }); }
    catch (e) { setCollectError(e instanceof Error ? e : new Error('采集安排失败。')); setCollecting(false); collectingRef.current = false; }
  }
  useEffect(() => {
    if (!batch) return;
    let active = true; let timer: number;
    const poll = async () => {
      try {
        const result = (batch.direction ? await request<Awaited<ReturnType<typeof api.reviewCollection>>>('/api/v1/direction-data/collections/' + batch.id) : await api.reviewCollection(batch.id)).data;
        if (!active) return;
        const total = Object.values(result.counts).reduce((a, n) => a + n, 0);
        setProgress(`资料采集 ${result.counts.succeeded ?? 0}/${total}${result.counts.failed ? ` · ${result.counts.failed}项失败` : ''}`);
        if (result.state === 'complete' || (!result.counts.queued && !result.counts.running)) {
          setCollecting(false); collectingRef.current = false; setBatch(null); reloadAll();
          if (result.state === 'partial' || result.counts.failed || result.counts.cancelled) setCollectError(new Error('部分资料未能补齐，旧数据已保留。可在数据中心查看来源状态后补采。'));
          return;
        }
      } catch (e) { if (active) setCollectError(e instanceof Error ? e : new Error('读取采集进度失败。')); }
      if (active) timer = window.setTimeout(poll, 5000);
    };
    poll(); return () => { active = false; clearTimeout(timer); };
  }, [batch]);
  useEffect(() => { window.dispatchEvent(new CustomEvent('ty-terminal-context', { detail: { trade_day: day, mode: asOf ? 'observed' : 'review', stock_name: q.get('stock'), sector_name: q.get('sector') } })); }, [day, asOf, nav.search]);
  return <div ref={paneScroll} className={'dr-root'+(reviewView(nav.search)==='direction'?' dr-rotation-page':'')+(reviewView(nav.search)==='overview'?' dr-postclose-page':'')+(reviewView(nav.search)==='new-highs'?' dr-highs-page':'')} aria-busy={read.busy}>
    <div className="dr-toolbar"><div className="dr-date-controls"><label>复盘日期<input type="date" aria-label="复盘日期" value={day} onChange={e => changeDay(e.target.value)}/></label><button onClick={() => select({ trade_day: null, range_end: null, record_key: null })}>最近已保存</button><span className="dr-mode">{session.meta.app_mode === 'demo' ? '演示环境' : asOf ? '当时已知' : currentDayPending ? '今日待更新' : data && isIntradaySnapshot(data.metrics) ? '盘中样本回看' : '收盘复盘'}</span></div><div className="dr-toolbar-actions"><span id="dr-judgment-tools" className="dr-judgment-tools"/>{reviewView(nav.search)==='direction'&&<div id="sr-review-tools" className="sr-toolbar-slot"/>}<button aria-pressed={q.get('mine') === '1'} className={q.get('mine') === '1' ? 'selected' : ''} onClick={() => select({ mine: q.get('mine') === '1' ? null : '1' })}>我的关注</button><button onClick={() => setCutoff(!cutoff)} aria-expanded={cutoff}>接收截止</button><button onClick={reloadAll}>重读缓存</button><button onClick={() => showCoverage(true)}>数据与口径</button><details ref={tools} className="dr-maintenance-tools" open={toolsOpen} onKeyDown={event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();if(tools.current)tools.current.open=false;setToolsOpen(false);toolsTrigger.current?.focus();}}} onToggle={event=>setToolsOpen(event.currentTarget.open)}><summary ref={toolsTrigger} aria-expanded={toolsOpen}>资料工具</summary><div><button disabled={session.data.user.role !== 'admin' || collecting || !!asOf || currentDailyNotClosed} onClick={()=>{setToolsOpen(false);void collect();}} title={currentDailyNotClosed ? '盘中行情自动更新，日频资料收盘后补齐' : '按所选日期补齐资料，已有完整采集会复用'}>{collecting ? '采集中…' : currentDailyNotClosed ? '日频待收盘' : '补齐资料'}</button><button disabled title="当前来源未开放资料导出">导出</button></div></details></div></div>
    {currentDailyNotClosed && <p className="dr-note" role="status">盘中行情自动更新，日频资料收盘后补齐。</p>}
    {cutoff && <div className="dr-cutoff"><label>仅查看此时间前已接收的资料<input aria-label="复盘接收截止时间" type="datetime-local" step={1} value={asOf && Number.isFinite(Date.parse(asOf)) ? new Date(asOf).toLocaleString('sv-SE', { timeZone: 'Asia/Shanghai' }).replace(' ', 'T') : ''} onChange={e => select({ as_of_time: e.target.value ? e.target.value + '+08:00' : null })}/></label><button onClick={() => select({ as_of_time: null })}>回到复盘资料</button><small>北京时间。后来补采的资料不会进入更早的时点。</small></div>}
    <Failure error={collectError} retry={() => { setCollectError(null); if (!collecting) collect(); }}/>{progress && <p className="dr-progress" role="status">{progress}</p>}
    <Failure error={read.error} retry={read.reload}/><Failure error={history.error} retry={history.reload}/>
    {waitingForCurrent && <p className="dr-note" role="status">正在更新 {market?.trade_day} 的今日位置，当前仍显示 {day} 已保存资料。</p>}
    {currentDayPending && <div className="dr-observation" role="status"><b>今日已预留 · 等待资料更新</b><span>昨日与历史记录保留在原日期；收到盘中样本或收盘资料后填入今天同一行，缺失不补零。</span></div>}
    {read.busy && !data ? <Loading text="正在读取同日行情、题材和证据…"/> : !data || !day ? <Empty>尚无可核实的交易日资料，可由管理员补齐资料。</Empty> : <>
      <div className="dr-market-ribbon">{data.indices.map(index => <span key={index.ts_code} title={index.record?.source_time ? `指数资料时点 ${String(index.record.source_time)}` : undefined}>{index.name}<b>{text(index.record?.close)}</b><strong className={tone(index.record?.pct_chg)}>{pct(index.record?.pct_chg)}</strong>{index.record?.price_basis === 'intraday_snapshot' && <small className="dr-snapshot-badge">已存 {snapshotTime({ state: 'partial', source_time: String(index.record.source_time ?? '') })}</small>}</span>)}<span>沪深成交 <b className="gold">{turnover(data.metrics.turnover?.amount_yuan)}</b></span><small>资料日 {day} · {isIntradaySnapshot(data.metrics) ? `已存盘中样本 ${snapshotTime(data.metrics)}` : currentDayPending ? '今日资料待更新' : data.calendar_state === 'complete' ? '原日样本' : '日历未核'}</small></div>
      {isIntradaySnapshot(data.metrics) && <div className="dr-observation" role="status"><b>盘中已存样本 · 行情截至 {snapshotTime(data.metrics)} · 非收盘数据</b><span>{data.observation?.scope || data.metrics.scope || '完整日频资料尚未归档，先显示已保存的同日行情；封板、反馈和成交额均对应这个采样时点。'}</span></div>}
      {data.calendar_state !== 'complete' && <div className="dr-error">所选日期不是当前可核实的收盘交易日。保留你的选择，未自动切换到其他日期。</div>}
      <ReviewRefresh.Provider value={refreshEpoch}><DailyBody key={`${day}:${asOf}`} session={session} day={day} asOf={asOf} data={data} series={history.data?.data ?? null} seriesBusy={history.busy} select={select} openRef={setReference} view={reviewView(nav.search)} onDirectionWindow={setDirectionWindow} onOverviewDay={setPostcloseDay} onOverviewCoverage={setPostcloseCoverage}/></ReviewRefresh.Provider>
    </>}
    {coverage && <CoveragePanel entries={isOverview ? postcloseCoverage : data?.coverage ?? []} flags={data?.metrics.quality_flags} open={ref => { showCoverage(false); setReference(ref); }} close={() => showCoverage(false)}/>}
    {reference && <Evidence reference={reference} close={() => setReference(null)}/>}
  </div>;
}

function DailyBody({ session, day, asOf, data, series, seriesBusy, select, openRef, view, onDirectionWindow, onOverviewDay, onOverviewCoverage }: { session: SessionResponse; day: string; asOf: string; data: Day; series: Series | null; seriesBusy: boolean; select: ReviewProps['select']; openRef: ReviewProps['openRef']; view: ReviewView; onDirectionWindow: (value: { day: string; endDay: string; lookback?: number }) => void; onOverviewDay: (day: string) => void; onOverviewCoverage: (entries: Coverage[]) => void }) {
  const refreshEpoch = useContext(ReviewRefresh);
  const nav = useNavigation(); const query = new URLSearchParams(nav.search);
  const boundEvidence = useRef<Ref[]>(data.evidence_refs ?? []);
  const n = useNotes(session, day, asOf, boundEvidence);
  const sector = query.get('sector') ?? n.content.directions[0]?.sector_code ?? [...data.themes.items].sort((a, b) => (number(b.provider_record.pct_change) ?? -Infinity) - (number(a.provider_record.pct_change) ?? -Infinity))[0]?.ts_code ?? '';
  const stock = query.get('stock') ?? '';
  const [history, showHistory] = useState(false);
  const [researchOpen,setResearchOpen]=useState(false);
  const researchOpener=useRef<HTMLElement|null>(null),researchClose=useRef<HTMLButtonElement>(null);
  const openResearch=()=>{researchOpener.current=document.activeElement as HTMLElement|null;setResearchOpen(true);};
  useEffect(()=>{
    if(!researchOpen)return;
    researchClose.current?.focus();
    const escape=(event:KeyboardEvent)=>{if(event.key==='Escape'&&!document.querySelector('dialog[open]')){event.preventDefault();setResearchOpen(false);}};
    window.addEventListener('keydown',escape);
    return()=>{window.removeEventListener('keydown',escape);if(researchOpener.current?.isConnected)researchOpener.current.focus();};
  },[researchOpen]);
  useEffect(()=>{if(view!=='direction')setResearchOpen(false);},[view]);
  const watch = useBoundRead(session.data.user.id + ':review-watchlist', () => api.watchlist());
  const watched = new Set((watch.data?.data.items ?? []).map(item => `${item.instrument.symbol}.${({ SSE: 'SH', SZSE: 'SZ', BSE: 'BJ' } as Record<string, string>)[item.instrument.exchange]}`));
  n.content.core_stocks.forEach(c => watched.add(c.code));
  const detailQuery = new URLSearchParams({ trade_day: day, ...(sector ? { sector } : {}), ...(asOf ? { as_of_time: asOf } : {}) });
  const detail = useBoundRead(`detail:${detailQuery}`, () => sector ? api.reviewDay(detailQuery) : Promise.resolve(null));
  const selectedTheme = detail.data?.data.themes.items.find(t => t.ts_code === sector) ?? null;
  const codes = n.content.core_stocks.map(c => c.code);
  const externalStock = stock && !codes.includes(stock) ? stock : '';
  const selectedQuery = new URLSearchParams({ trade_day: day, codes: externalStock, ...(asOf ? { as_of_time: asOf } : {}) });
  const selectedRead = useBoundRead(`selected:${selectedQuery}`, () => externalStock ? api.reviewStocks(selectedQuery) : Promise.resolve(null));
  const stocksQuery = new URLSearchParams({ trade_day: day, codes: codes.join(','), ...(asOf ? { as_of_time: asOf } : {}) });
  const stocks = useBoundRead(`tracked:${stocksQuery}`, () => codes.length ? api.reviewStocks(stocksQuery) : Promise.resolve(null));
  const themeCodes = [...new Set([...n.content.directions.map(d => d.sector_code), ...(sector ? [sector] : [])])].slice(0, 10);
  const themeQuery = new URLSearchParams({ trade_day: day, sector_codes: themeCodes.join(','), ...(asOf ? { as_of_time: asOf } : {}) });
  const themes = useBoundRead(`themes:${themeQuery}`, () => themeCodes.length ? api.reviewThemes(themeQuery) : Promise.resolve(null));
  boundEvidence.current = referencesFrom(data, detail.data?.data, stocks.data?.data, selectedRead.data?.data, themes.data?.data);
  const edit = (section: string) => select({ edit: section });
  function addCore(code: string, name: string, theme = sector || null) {
    if (!n.writable || !/^(?:6\d{5}\.SH|[03]\d{5}\.SZ)$/.test(code)) return;
    if (!n.content.core_stocks.some(c => c.code === code)) {
      if (n.content.core_stocks.length >= 30) return;
      n.update({ ...n.content, core_stocks: [...n.content.core_stocks, { code, name: name || code, sector_code: theme, role: '观察', rationale: '', tag: '观察' }] });
    }
    select({ stock: code, edit: 'core', record_key: null });
  }
  const anchor = series?.trade_days.at(-1) ?? day;
  const stageQuery = new URLSearchParams({ trade_day: anchor, lookback: '60', ...(asOf ? { as_of_time: asOf } : {}) });
  const stageRead = useBoundRead(`stages:${stageQuery}`, () => api.reviewNoteDays(stageQuery));
  const stagesByDay = Object.fromEntries((stageRead.data?.data.items ?? []).map(item => [item.trade_day, item.stage])); stagesByDay[day] = n.content.stage;
  const themeStagesByDay = Object.fromEntries((stageRead.data?.data.items ?? []).map(item => [item.trade_day, item.theme_stages])); themeStagesByDay[day] = n.content.theme_stages;
  const props: ReviewProps = { session, day, asOf, data, series, seriesBusy, notebook: n, sector, stock, selectedTheme, detailBusy: detail.busy, tracked: [...(stocks.data?.data.items ?? []), ...(selectedRead.data?.data.items ?? [])], watched, mine: query.get('mine') === '1', themeHistory: themes.data?.data ?? null, themeHistoryBusy: themes.busy, themeHistoryError: themes.error, reloadThemeHistory: themes.reload, stagesByDay, themeStagesByDay, select, openRef, edit, addCore, go: view => select({ view, edit: null }) };
  const confirmationReason = !n.writable
    ? session.data.user.role === 'viewer' ? '当前账号仅可查看，不能确认。'
      : asOf ? '当时已知回看为只读，回到复盘资料后可确认。'
      : n.loading ? '人工笔记仍在读取，完成后可确认。'
      : n.error ? '人工笔记读取异常，请先恢复后再确认。' : '当前笔记不可写，暂时无法确认。'
    : n.busy ? '正在保存，请等待本次保存完成。'
    : detail.busy || stocks.busy || selectedRead.busy || themes.busy ? '关联资料仍在读取，完成后可确认。'
    : detail.error || stocks.error || selectedRead.error || themes.error ? '关联资料读取失败，请先重试后再确认。'
    : view === 'lhb' ? '龙虎榜为事实查阅，请在情绪或方向页面填写判断后确认。'
    : view === 'direction' && !n.content.directions.length ? '请先填写至少一个方向及判断。'
    : view !== 'direction' && !n.content.summary.trim() ? '请先填写本人的复盘总结。'
    : !n.dirty && n.content.confirmed_sections.includes(view === 'direction' ? 'directions' : 'summary') ? '当前版本已人工确认，修改判断后可再次确认。' : '';
  const status = n.loading ? '笔记读取中…' : n.dirty ? '人工修改 · 尚未保存' : n.version ? `人工笔记 v${n.version} · 已保存` : '尚未填写人工判断';
  const judgmentHost=document.getElementById('dr-judgment-tools');
  const hasJudgment=view==='direction'?n.content.directions.length>0:!!n.content.summary.trim();
  const notebookBar=<div className="dr-notebook-status"><span className={n.dirty ? 'gold' : ''}>{status}</span>{n.content.stage !== '未标记' && <span>情绪：<b className="gold">{n.content.stage}</b></span>}<span className="dr-ai">AI解读 · 预留</span><button disabled={!n.writable || n.busy || !n.dirty} onClick={() => n.save()}>{n.busy ? '正在保存…' : '保存草稿'}</button><button disabled={!!asOf} title={asOf ? '回到复盘资料查看笔记历史' : undefined} onClick={() => showHistory(true)}>历史版本</button><a href={`/research/reviews?workspace=archive&archive_date=${day}`}>旧复盘归档 ↗</a></div>;
  const bottomActions=<>    {confirmationReason && <p id="dr-confirmation-reason" className="dr-confirmation-reason" role="status">{confirmationReason}</p>}
    <div className="dr-bottom-actions"><small>行情原件只读 · 判断由本人填写 · 确认会新增版本</small><button disabled={!n.writable || n.busy || !n.dirty} onClick={() => n.save()}>保存草稿</button><button className="primary" disabled={!!confirmationReason} aria-describedby={confirmationReason ? "dr-confirmation-reason" : undefined} onClick={() => n.save(n.content, view === 'direction' ? 'directions' : 'summary')}>{n.content.confirmed_sections.includes(view === 'direction' ? 'directions' : 'summary') && !n.dirty ? '当前版本已人工确认' : view === 'direction' ? '确认板块研究' : '确认本页'}</button><a href={'/research/tomorrow?' + new URLSearchParams({ source_trade_day: day })}>明日预案 →</a></div></>;
  return <>{judgmentHost&&view!=='lhb'&&createPortal(<button className="primary dr-judgment-entry" disabled={!n.writable||n.busy} title={!n.writable?session.data.user.role==='viewer'?'当前账号仅可查看':asOf?'当时已知回看为只读':n.loading?'人工笔记正在读取':'人工笔记暂不可写':undefined} onClick={()=>edit(view==='direction'?'direction':'market')}>{hasJudgment?'编辑判断':'填写判断'}</button>,judgmentHost)}{view!=='direction'&&view!=='overview'&&view!=='new-highs'&&notebookBar}
    {n.notice && <p className="dr-success" role="status">{n.notice}</p>}{n.error && <div className="dr-error" role="alert">{n.error.message}<button disabled={n.busy} onClick={n.reloadLatest}>放弃暂存，载入服务器版本</button></div>}
    <Failure error={detail.error} retry={detail.reload}/><Failure error={stocks.error} retry={stocks.reload}/><Failure error={selectedRead.error} retry={selectedRead.reload}/><Failure error={watch.error} retry={watch.reload}/><Failure error={stageRead.error} retry={stageRead.reload}/>
    {view === 'emotion' ? <Emotion {...props}/> : view === 'direction' ? <>
      <DirectionBoard day={day} asOf={asOf} refreshEpoch={refreshEpoch} ownerId={session.data.user.id} isAdmin={session.data.user.role === 'admin'} inactive={researchOpen} onResearch={openResearch} onWindowChange={(endDay,lookback) => onDirectionWindow({ day, endDay, lookback })}/>
      {researchOpen&&<section className="sr-research-drawer" aria-label="研究记录与核心跟踪"><header><h2>研究记录与核心跟踪</h2><button ref={researchClose} onClick={()=>setResearchOpen(false)}>收起记录</button></header>{notebookBar}<GroupedLimitUps {...props}/><Direction {...props}/>{bottomActions}</section>}
    </> : view === 'lhb' ? <Dragon {...props}/> : view==='new-highs'?<NewHighs day={day} asOf={asOf} select={select} automaticDay={!query.has('trade_day')} onResolvedDay={onOverviewDay} onCoverage={onOverviewCoverage}/>:<PostcloseOverview {...props} automaticDay={!query.has('trade_day')} onResolvedDay={onOverviewDay} onCoverage={onOverviewCoverage} researchHeader={notebookBar} researchFooter={bottomActions}/>}
    {view!=='direction'&&view!=='overview'&&view!=='new-highs'&&bottomActions}
    {query.get('edit') && <NoteEditor section={query.get('edit')!} notebook={n} themes={data.themes.items} selectedStock={stock} selectedSector={sector} close={() => select({ edit: null })}/>}
    {history && <NoteHistory day={day} close={() => showHistory(false)} openRef={openRef}/>}
  </>;
}
