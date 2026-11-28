import { useCallback, useEffect, useRef, useState } from 'react';
import { useCachedLoader } from '../../shared/useCachedLoader';
import { notifiedReadError } from '../../shared/BackgroundRefresh';
import type { ReactNode } from 'react';
import { Icon } from '../../shared/icons';
import { AiAction } from '../ai/AiAction';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { EditionCalendar } from './EditionCalendar';
import { BookmarkIcon } from './NewsCenterHeader';
import { useNewsCsrf, useNewsSession } from './NewsSession';
import { newsApi } from './newsApi';
import { articleTime, articleTimeColumn, categories, chinaDay, chinaTime, mergeState, newsHref, newsSummaryAvailable, paperName, periodName, safeOriginal, sectionOrder, validDay } from './model';
import type { ArticleState, NewsAiVersion, NewsArticle, NewsPeriod, NewsSource } from './model';
import './news-center.css';
import { newsCategory, usePreferences } from '../../shared/preferences';
import { ArticleSecurities, SecurityEvidence } from './ArticleSecurities';
import { IndustryBoardCards } from './IndustryBoardCards';
import { usePaneScroll } from '../../shared/workspaceState';

function useRemote<T>(load: () => Promise<T>, key: string) {
  return useCachedLoader(load, key);
}

function changeQuery(values: Record<string, string | boolean | null | undefined>, replace = false) {
  const url = new URL(location.href);
  for (const [key, value] of Object.entries(values)) {
    if (value === null || value === undefined || value === false || value === '') url.searchParams.delete(key);
    else url.searchParams.set(key, String(value));
  }
  updateLocation(url, { replace });
}
const message = (value: unknown) => value instanceof Error ? value.message : '操作暂时未完成，请重试。';
function RequestError({ error, retry }: { error: Error; retry: () => void }) {
  if (notifiedReadError(error)) return null;
  return <div className="nc-request-error" role="alert"><span>{error.message}</span><button type="button" onClick={retry}>重新读取</button></div>;
}
function EmptyNews({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="nc-empty"><Icon name="news"/><h2>{title}</h2><p>{children}</p></div>;
}
function OriginalLink({ article, onRead, className = '' }: { article: NewsArticle; onRead?: () => void; className?: string }) {
  const original = safeOriginal(article.source_url);
  return original ? <a className={'nc-original ' + className} href={original} target="_blank" rel="noopener noreferrer" onClick={onRead}>查看原文 <span aria-hidden="true">↗</span></a> : <span className="nc-muted">原文链接暂不可用</span>;
}
function ArticleColumnTime({ article, referenceDay }: { article: NewsArticle; referenceDay: string }) {
  const time = articleTimeColumn(article, referenceDay);
  return <time dateTime={article.published_at ?? article.published_day ?? undefined} title={articleTime(article, true)}>{time.year && <small>{time.year}</small>}{time.label}</time>;
}
function AiArticleSummary({ ai, article }: { ai: NewsAiVersion | null | undefined; article: NewsArticle }) {
  const enhanced = ai?.items?.find(item => item.article_id === article.id && item.evidence_id === article.evidence_id && item.selected);
  return enhanced?.summary ? <aside className="nc-ai-summary"><strong>历史 AI 摘要</strong><p>{enhanced.summary}</p></aside> : null;
}
function SourceStatus({ sources, coverageStart, loading = false, unavailable = false }: { sources: NewsSource[]; coverageStart?: string | null; loading?: boolean; unavailable?: boolean }) {
  const labels: Record<string, string> = { complete: '采集正常', empty: '本轮无新资料', not_checked: '尚未采集', permission_missing: '范围待核实', ready: '采集正常', ok: '采集正常', healthy: '采集正常', available: '可读取', active: '已启用', error: '采集暂时失败', failed: '采集暂时失败', unavailable: '暂不可用', stale: '资料待更新', unconfigured: '待配置', disabled: '未启用', candidate: '待连通', pending: '待连通', partial: '覆盖不完整', paused: '已暂停', never: '尚未采集' };
  const degraded = sources.filter(source => ['error', 'failed', 'unavailable', 'stale', 'partial', 'permission_missing'].includes(source.status)).length;
  return <details className="nc-source-status"><summary>来源与覆盖 <span>{loading ? '读取中' : unavailable ? '暂无法读取' : `${sources.length} 个来源`}</span>{degraded > 0 && <span className="nc-amber">{degraded} 项待核对</span>}</summary>
    {coverageStart && <p className="nc-muted">归档自 {chinaTime(coverageStart, true)} 起累积</p>}
    {!sources.length ? <p className="nc-muted">{loading ? '正在读取来源状态。' : unavailable ? '来源状态暂时无法读取。' : '暂无已登记的可读来源。'}<a href="/data">查看数据中心 ↗</a></p> : <ul>{sources.map(source => <li key={source.family}><div><strong>{source.name}</strong><span className={['error', 'failed', 'unavailable', 'stale', 'partial', 'permission_missing'].includes(source.status) ? 'nc-amber' : ''}>{labels[source.status] ?? '状态待确认'}</span></div><small>已存 {source.cached_count} 条 · {source.last_success_at ? '最近成功 ' + chinaTime(source.last_success_at, true) : '尚无成功采集记录'}</small>{source.coverage && <p>{source.coverage}</p>}</li>)}</ul>}
    <a className="nc-original" href="/data">查看数据中心 <span aria-hidden="true">↗</span></a>
  </details>;
}

export function NewsCenter() {
  const navigation = useNavigation();
  const query = new URLSearchParams(navigation.search);
  const period = query.get('period');
  if (query.get('view') === 'topics') return <IndustryTopics/>;
  return query.get('view') === 'daily' ? <NewsPaper key={period ?? 'day'} period={period === 'week' || period === 'month' ? period : 'day'}/> : <NewsFeed/>;
}

function NewsOverview({ onSelect }: { onSelect: (id: string) => void }) {
  const read = useRemote(newsApi.overview, 'news-overview');
  const [expanded, setExpanded] = useState(false);
  const data = read.data?.data;
  const rows = data?.hotspots ?? [];
  return <section className="nc-overview">
    {read.error && <RequestError error={read.error} retry={read.reload}/>}
    {!data && read.busy && <p role="status">正在读取资讯…</p>}
    {data &&
      <div className="nc-rank-panel"><header><h2>当前热点</h2><small>近 48 小时 · 按同题报道信源数</small></header>
        {!rows.length && <p>近 48 小时暂无三家信源快讯。</p>}
        <ol>{rows.slice(0, expanded ? rows.length : 5).map((item, index) => <li key={item.id}>
          <span>{index + 1}</span><button onClick={() => onSelect(item.id)}>{item.title}</button>
          <small>{item.sources.join(' · ')}</small><time dateTime={item.published_at}>{chinaTime(item.published_at)}</time></li>)}</ol>
        {rows.length > 5 && <button className="nc-rank-more" onClick={() => setExpanded(!expanded)}>{expanded ? '收起榜单' : '完整榜单'} {expanded ? '↑' : '↓'}</button>}</div>}
  </section>;
}

export function IndustryTopics() {
  const navigation = useNavigation();
  const article = new URLSearchParams(navigation.search).get('article');
  const csrf = useNewsCsrf();
  const read = useRemote(newsApi.overview, 'news-overview');
  return <div className="nc-center nc-topics-page">
    <p><a className="nc-text-button" href="/news?view=topics&mode=research">按关键词回看题材</a></p>
    <IndustryBoardCards overview={read.data?.data}
      search={navigation.search} onFilter={changeQuery}
      busy={read.busy} error={read.error} retry={read.reload}
      onSelect={id => changeQuery({ article: id })}/>
    {article && <NewsArticleDialog key={article} id={article} csrf={csrf} close={() => changeQuery({ article: null })} onChange={() => {}}/>}
  </div>;
}

function NewsFeed() {
  const navigation = useNavigation();
  const query = new URLSearchParams(navigation.search);
  const { preferences } = usePreferences();
  const chosen = newsCategory(query, preferences);
  const category = [...categories, '财经'].includes(chosen) ? chosen : '精选';
  const saved = query.get('saved') === 'true';
  const unread = query.get('unread') === 'true';
  const following = query.get('following') === 'true' || query.get('view') === 'following';
  const search = query.get('q') ?? '';
  const family = query.get('family') ?? '';
  const day = validDay(query.get('day')) ?? '';
  const order = query.get('order') === 'earliest' ? 'earliest' : 'latest';
  const articleId = query.get('article');
  const params = new URLSearchParams({ category, limit: '30', order });
  if (search) params.set('q', search);
  if (family) params.set('family', family);
  if (day) params.set('day', day);
  if (saved) params.set('saved', 'true');
  if (unread) params.set('unread', 'true');
  if (following) params.set('following', 'true');
  const requestKey = params.toString();
  const newsSession = useNewsSession();
  const paneScroll = usePaneScroll(`${newsSession?.data.user.id}:news:${day || chinaDay(new Date().toISOString())}:${requestKey}`, '.nc-news-list');
  const read = useRemote(() => newsApi.items(params), requestKey);
  const archive = useRemote(() => newsApi.editions('day'), 'feed-archive');
  const feed = read.data?.data;
  const csrf = useNewsCsrf();
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [stateError, setStateError] = useState('');
  const [moreBusy, setMoreBusy] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [filtersClosing, setFiltersClosing] = useState(false);
  const filterDetails = useRef<HTMLDetailsElement>(null);
  const filterTrigger = useRef<HTMLElement>(null);
  const filterExit = useRef<number | null>(null);
  const closeFilters = useCallback((restoreFocus = true) => {
    setFiltersOpen(false); setFiltersClosing(true);
    if (filterExit.current !== null) window.clearTimeout(filterExit.current);
    filterExit.current = window.setTimeout(() => { setFiltersClosing(false); filterExit.current = null; }, 120);
    if (restoreFocus) filterTrigger.current?.focus({ preventScroll: true });
  }, []);
  function toggleFilters() {
    if (filtersOpen) { closeFilters(); return; }
    if (filterExit.current !== null) window.clearTimeout(filterExit.current);
    filterExit.current = null; setFiltersClosing(false); setFiltersOpen(true);
  }
  useEffect(() => () => { if (filterExit.current !== null) window.clearTimeout(filterExit.current); }, []);
  useEffect(() => {
    if (!filtersOpen) return;
    const keydown = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.preventDefault(); closeFilters(); } };
    const outside = (event: PointerEvent) => { if (event.target instanceof Node && !filterDetails.current?.contains(event.target)) closeFilters(); };
    const focusOutside = (event: FocusEvent) => { if (event.target instanceof Node && !filterDetails.current?.contains(event.target)) closeFilters(false); };
    document.addEventListener('keydown', keydown); document.addEventListener('pointerdown', outside); document.addEventListener('focusin', focusOutside);
    return () => { document.removeEventListener('keydown', keydown); document.removeEventListener('pointerdown', outside); document.removeEventListener('focusin', focusOutside); };
  }, [filtersOpen, closeFilters]);
  const latestKey = useRef(requestKey);
  latestKey.current = requestKey;
  const matching = (article: NewsArticle) => (!saved || article.is_saved) && (!unread || !article.is_read) && (!following || article.is_following);
  const items = feed?.items.filter(matching) ?? [];
  const primaryView = category === '精选' && !saved && !unread && !following && !search && !family && !day;
  const focus = primaryView ? feed?.focus : null;
  const list = items.filter(article => article.id !== focus?.id);
  const latest = feed?.latest_edition ?? archive.data?.data.items[0] ?? null;
  function applyArticle(next: NewsArticle) {
    read.setData(previous => {
      if (!previous) return previous;
      const before = previous.data.items.find(item => item.id === next.id);
      const difference = before ? Number(matching(mergeState(before, next))) - Number(matching(before)) : 0;
      return { ...previous, data: { ...previous.data, total: Math.max(0, previous.data.total + difference), items: previous.data.items.map(item => item.id === next.id ? mergeState(item, next) : item), focus: previous.data.focus?.id === next.id ? mergeState(previous.data.focus, next) : previous.data.focus } };
    });
  }
  async function mutate(article: NewsArticle, patch: ArticleState) {
    if (pending.has(article.id)) return;
    setPending(previous => new Set(previous).add(article.id)); setStateError('');
    const requestBody = patch.read === undefined ? patch : { ...patch, evidence_id: article.evidence_id };
    try { const response = await newsApi.state(article.id, requestBody, csrf); applyArticle(response.data); }
    catch (error) { setStateError(message(error)); }
    finally { setPending(previous => { const next = new Set(previous); next.delete(article.id); return next; }); }
  }
  async function loadMore() {
    if (!feed?.next_cursor || moreBusy) return;
    const key = requestKey;
    const next = new URLSearchParams(params); next.set('cursor', feed.next_cursor);
    setMoreBusy(true); setStateError('');
    try {
      const response = await newsApi.items(next);
      if (latestKey.current !== key) return;
      read.setData(previous => previous ? { ...response, data: { ...response.data, items: [...previous.data.items, ...response.data.items.filter(item => !previous.data.items.some(existing => existing.id === item.id))] } } : response);
    } catch (error) { if (latestKey.current === key) setStateError(message(error)); }
    finally { setMoreBusy(false); }
  }
  const latestReceived = feed?.items.reduce((latest, item) => item.received_at > latest ? item.received_at : latest, '');
  const today = chinaDay(new Date().toISOString());
  const focusDay = focus?.published_at ? chinaDay(focus.published_at) : focus?.published_day;
  const readableDate = day || focusDay || (latestReceived ? chinaDay(latestReceived) : today);
  const dateCaption = new Intl.DateTimeFormat('zh-CN', { timeZone: 'UTC', year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' }).format(new Date(readableDate + 'T00:00:00Z'));
  const open = (article: NewsArticle) => changeQuery({ article: article.id });
  return <div ref={paneScroll} className="nc-center" data-testid="news-center">
    <div className="nc-category-bar"><nav aria-label="资讯分类">{categories.map(label => <button type="button" key={label} className={category === label ? 'selected' : ''} aria-pressed={category === label} onClick={() => changeQuery({ category: label, article: null })}>{label}</button>)}</nav><div className="nc-update">{latestReceived ? `更新至 ${chinaTime(latestReceived)}` : '本机已存资料'}<span>{read.data?.meta.app_mode === 'demo' ? '演示数据' : '原始资讯 · 自动汇编'}</span></div></div>
    <div className="nc-feed-grid"><div className="nc-feed-main">
      <p className="nc-dateline">{readableDate !== today && !day ? '最近资料 · ' : ''}{dateCaption}</p>
      {saved || following || search ? <div className="nc-current-filter"><span>{saved ? '我的收藏' : following ? '待跟进资讯' : `搜索“${search}”`}</span><a href="/news">返回首页 <span aria-hidden="true">→</span></a></div> : null}
      {read.error && <RequestError error={read.error} retry={read.reload}/>}
      {read.busy && !feed && <p className="nc-loading" role="status">正在读取已存资讯…</p>}
      {(!read.busy && !read.error || feed) && <>
        {primaryView && <NewsOverview onSelect={id => changeQuery({ article: id })}/>}
        {focus && <section className="nc-focus" aria-labelledby="nc-focus-label"><h2 className="nc-section-heading" id="nc-focus-label">{focusDay === today ? '今日焦点' : '最近焦点'}</h2><ArticleSecurities article={focus}/><h3><button type="button" onClick={() => open(focus)}>{focus.title}</button></h3>{focus.summary && <p className="nc-focus-summary">{focus.summary}</p>}<div className="nc-focus-meta"><span>{focus.source_name} · {articleTime(focus, true)}{(focus.related_sources?.length ?? 0) ? ` · ${(focus.related_sources?.length ?? 0) + 1} 个出处` : ''}</span><OriginalLink article={focus} onRead={() => { if (!focus.is_read) void mutate(focus, { read: true }); }}/></div>{focus.late && <p className="nc-late-note">迟到资料 · 本次接收后归入可用内容</p>}<p className="nc-reading-note"><i/>阅读提示：保留来源表述，结合原文核对具体条件与最新进展。</p></section>}
        <section className="nc-latest" aria-labelledby="nc-latest-label"><div className="nc-list-toolbar"><h2 className="nc-section-heading" id="nc-latest-label">{saved ? '收藏资讯' : following ? '待跟进事项' : '最新资讯'}</h2><select aria-label="资讯排序" value={order} onChange={event => changeQuery({ order: event.target.value })}><option value="latest">按最新进展</option><option value="earliest">按最早发布</option></select><label className="nc-unread"><input type="checkbox" checked={unread} onChange={event => changeQuery({ unread: event.target.checked })}/><span>只看未读</span></label></div>
          <div className="nc-filter-tools"><details ref={filterDetails} open={filtersOpen || filtersClosing}><summary ref={filterTrigger} aria-expanded={filtersOpen} aria-controls="nc-feed-filters" onClick={event => { event.preventDefault(); toggleFilters(); }}><Icon name="search"/> 筛选{family || day ? ' · 已筛选' : ''}</summary><div id="nc-feed-filters" className={`nc-filter-popover${filtersClosing ? ' is-closing' : ''}`} inert={!filtersOpen} aria-hidden={!filtersOpen}><label>来源<select aria-label="资讯来源" value={family} onChange={event => changeQuery({ family: event.target.value, article: null })}><option value="">全部来源</option>{family && !feed?.sources.some(source => source.family === family) && <option value={family}>所选来源</option>}{feed?.sources.map(source => <option key={source.family} value={source.family}>{source.name}</option>)}</select></label><label>发布日期<input type="date" aria-label="资讯日期" value={day} onChange={event => changeQuery({ day: event.target.value, article: null })}/></label><label className="nc-follow-filter"><input type="checkbox" checked={following} onChange={event => changeQuery({ following: event.target.checked, view: 'feed' })}/>只看待跟进</label>{(family || day || following) && <button type="button" className="nc-text-button" onClick={() => changeQuery({ family: null, day: null, following: null, view: 'feed' })}>清除筛选</button>}</div></details><span>{feed?.total ?? 0} 条匹配资料</span><button type="button" onClick={read.reload} aria-label="刷新已存资讯"><Icon name="refresh"/></button></div>
          {!items.length && !focus ? <EmptyNews title={unread ? '当前没有未读资讯' : following ? '还没有待跟进事项' : saved ? '还没有收藏的资讯' : '暂无匹配的已存资讯'}>{saved ? '点击资讯旁的书签即可收藏，登录后可继续阅读。' : '调整筛选，或在数据中心查看来源与采集状态。'}</EmptyNews> : <div key={requestKey} className="nc-news-list" role="region" aria-label="资讯列表" tabIndex={0}>{list.map(article => <article className={`nc-news-row ${article.is_read ? 'is-read' : ''}`} key={article.id}><ArticleColumnTime article={article} referenceDay={today}/><span className={`nc-row-category ${article.category === '风险澄清' ? 'is-risk' : ''}`}>{article.category}</span><div className="nc-row-content"><ArticleSecurities article={article}/><h3><button type="button" onClick={() => open(article)}>{article.title}</button></h3>{article.summary && <p>{article.summary}</p>}<div className="nc-row-meta"><span>{article.source_name} · {articleTime(article, true)}{article.late ? ' · 迟到资料' : ''}</span>{(article.related_sources?.length ?? 0) > 0 && <span>{(article.related_sources?.length ?? 0) + 1} 个原始出处</span>}<OriginalLink article={article} onRead={() => { if (!article.is_read) void mutate(article, { read: true }); }}/>{article.is_following && <span className="nc-amber">待跟进</span>}</div></div><button type="button" className={`nc-bookmark ${article.is_saved ? 'is-saved' : ''}`} aria-label={`${article.is_saved ? '取消收藏' : '收藏'}：${article.title}`} disabled={!csrf || pending.has(article.id)} onClick={() => void mutate(article, { saved: !article.is_saved })}><BookmarkIcon filled={article.is_saved}/></button></article>)}</div>}
          {feed?.next_cursor && <button type="button" className="nc-more" disabled={moreBusy} onClick={() => void loadMore()}>{moreBusy ? '正在读取…' : '继续阅读更多资讯'} <span aria-hidden="true">↓</span></button>}
        </section>
      </>}
      {stateError && <p className="nc-inline-error" role="alert">{stateError}</p>}
      <p className="nc-footnote">阅读范围为本机已保存且可展示的资料。发布时间、接收时间和来源分别保留。</p>
    </div><aside className="nc-feed-aside" aria-label="今日简报与归档"><h2 className="nc-section-heading">{latest?.day === today ? '今日简报' : '最近简报'}</h2>
      <section className="nc-brief"><h3>天衍日报</h3>{latest ? <><time dateTime={latest.day}>{Number(latest.day.slice(5, 7))}月{Number(latest.day.slice(-2))}日</time><p>政策、产业与公司要闻，一次读完。</p><p className="nc-brief-count">{latest.item_count} 条要闻 · 约{latest.reading_minutes}分钟</p><a className="nc-read-edition" href={newsHref({ view: 'daily', period: 'day', date: latest.day })}>阅读本期 <span aria-hidden="true">→</span></a>{latest.coverage_status !== 'complete' && <small className="nc-coverage-caption">{latest.coverage_note || '本期资料覆盖有限'}</small>}</> : <><p>每天 08:00 汇编已存资料。</p><p className="nc-muted">{!feed && !archive.data && (read.error || archive.error) ? '刊期状态暂无法读取' : !feed && !archive.data && (read.busy || archive.busy) ? '正在读取刊期…' : '尚未生成可阅读的刊期'}</p><a className="nc-read-edition" href="/news?view=daily&period=day">查看日报归档 <span aria-hidden="true">→</span></a></>}</section>
      {archive.data?.data.items.length ? <EditionCalendar editions={archive.data.data.items} selected={latest} onSelect={date => changeQuery({ view: 'daily', period: 'day', date, article: null })}/> : null}
      {archive.error && <p className="nc-muted">归档暂时无法读取。<button className="nc-text-button" type="button" onClick={archive.reload}>重试</button></p>}
      <SourceStatus sources={feed?.sources ?? []} coverageStart={feed?.coverage_start} loading={read.busy && !feed} unavailable={Boolean(read.error && !feed)}/>
      <p className="nc-ai-caption">单篇新闻摘要可在详情中手动生成。<br/><span>模型由<a href="/data?view=connections">数据中心</a>统一管理；日报、周报、月报 AI 增强业务待接入，历史 AI 版本仍可阅读。</span></p>
    </aside></div>
    {articleId && <NewsArticleDialog id={articleId} csrf={csrf} close={() => changeQuery({ article: null })} onChange={applyArticle}/>}
  </div>;
}

function useArchive(period: NewsPeriod, targetDay: string | null) {
  const read = useRemote(() => newsApi.editions(period), period);
  const [tail, setTail] = useState<{ period: NewsPeriod; items: NonNullable<typeof read.data>['data']['items']; nextCursor?: string | null }>({ period, items: [] });
  const data = read.data ? { ...read.data, data: { ...read.data.data,
    items: [...read.data.data.items, ...(tail.period === period ? tail.items.filter(item => !read.data!.data.items.some(head => head.id === item.id)) : [])],
    next_cursor: tail.period === period && tail.nextCursor !== undefined ? tail.nextCursor : read.data.data.next_cursor } } : undefined;
  const [moreBusy, setMoreBusy] = useState(false);
  const [moreError, setMoreError] = useState('');
  const version = useRef(0);
  const pages = useRef(new Set<string>());
  useEffect(() => { version.current++; pages.current.clear(); setTail({ period, items: [] }); }, [period]);
  const loadMore = useCallback(async () => {
    const cursor = data?.data.next_cursor;
    if (!cursor || moreBusy) return;
    const current = version.current;
    setMoreBusy(true); setMoreError('');
    try {
      const response = await newsApi.editions(period, cursor);
      if (current !== version.current) return;
      pages.current.add(cursor);
      setTail(previous => ({ period, items: [...(previous.period === period ? previous.items : []), ...response.data.items.filter(item => !previous.items.some(existing => existing.id === item.id))], nextCursor: response.data.next_cursor === cursor ? null : response.data.next_cursor }));
    } catch (error) { if (current === version.current) setMoreError(message(error)); }
    finally { if (current === version.current) setMoreBusy(false); }
  }, [data, moreBusy, period]);
  useEffect(() => {
    const archive = data?.data;
    if (!targetDay || !archive || moreBusy || moreError || read.busy || !archive.next_cursor || pages.current.has(archive.next_cursor) || archive.items.some(item => item.day === targetDay)) return;
    const oldest = archive.items[archive.items.length - 1];
    if (!oldest || oldest.day > targetDay) void loadMore();
  }, [targetDay, data, read.busy, moreBusy, moreError, loadMore]);
  return { ...read, data, moreBusy, moreError, loadMore };
}

function NewsPaper({ period }: { period: NewsPeriod }) {
  const navigation = useNavigation();
  const query = new URLSearchParams(navigation.search);
  const requestedDay = validDay(query.get('date'));
  const archive = useArchive(period, requestedDay);
  const editions = archive.data?.data.items ?? [];
  const selected = requestedDay ? editions.find(edition => edition.day === requestedDay) ?? null : editions[0] ?? null;
  const edition = useRemote(async () => selected ? newsApi.edition(selected.id) : null, selected?.id ?? 'empty');
  const context = useRemote(() => newsApi.items(new URLSearchParams({ category: '全部', limit: '1' })), 'edition-source-context');
  const data = selected ? edition.data?.data : undefined;
  const displayed = data?.edition ?? selected;
  const csrf = useNewsCsrf();
  const [archiveQuery, setArchiveQuery] = useState('');
  const [allArchives, setAllArchives] = useState(false);
  const [stateError, setStateError] = useState('');
  const currentArticle = data?.items.find(item => item.id === query.get('article'));
  const loadingArchive = archive.busy && !archive.data || Boolean(requestedDay && !selected && archive.moreBusy);
  const selectDay = (date: string) => changeQuery({ view: 'daily', period, date, article: null });
  const items = data?.items ?? [];
  const lead = items[0];
  const highlights = items.slice(1, 4);
  const grouped = sectionOrder.map(category => ({ category, items: items.slice(1).filter(item => item.category === category) }));
  const other = items.slice(1).filter(item => !sectionOrder.includes(item.category));
  if (other.length) grouped.push({ category: '其他要闻', items: other });
  const sections = grouped.filter(section => section.items.length);
  const archiveRows = editions.filter(item => !archiveQuery || [item.day, item.headline].some(value => value.includes(archiveQuery)));
  const displayedArchives = allArchives || archiveQuery ? archiveRows : archiveRows.slice(0, 3);
  function applyArticle(next: NewsArticle) {
    edition.setData(previous => previous ? { ...previous, data: { ...previous.data, items: previous.data.items.map(item => item.id === next.id ? mergeState(item, next) : item) } } : previous);
  }
  return <div className="nc-center nc-paper-center" data-testid="news-paper">
    <aside className="nc-archive" aria-label={`往期${periodName(period)}`}><div className="nc-archive-heading"><h2>往期{periodName(period)}</h2><button type="button" aria-label="查找往期刊物" onClick={() => { setAllArchives(true); document.getElementById('nc-archive-search')?.focus(); }}><Icon name="search"/></button></div>
      <input id="nc-archive-search" type="search" aria-label="查找往期刊物" placeholder="日期 / 标题" value={archiveQuery} onChange={event => setArchiveQuery(event.target.value)}/>
      <span className="nc-archive-caption">{allArchives || archiveQuery ? '已出刊归档' : '最近三期'}</span>
      <div className="nc-archive-list">{displayedArchives.map(item => <button type="button" key={item.id} className={selected?.id === item.id ? 'selected' : ''} onClick={() => selectDay(item.day)} aria-current={selected?.id === item.id ? 'page' : undefined}><time dateTime={item.day}>{item.day.slice(5).replace('-', '.')}</time><span>{item.headline || `${paperName(period)} · ${item.item_count}条要闻`}</span></button>)}</div>
      {!editions.length && !archive.busy && <p className="nc-muted">尚无已出刊归档</p>}
      {!archiveQuery && editions.length > 3 && <button className="nc-all-archives" type="button" onClick={() => setAllArchives(value => !value)}>{allArchives ? '收起往期' : '全部往期'} <span aria-hidden="true">→</span></button>}
      {archive.data?.data.next_cursor && <button className="nc-text-button nc-load-archives" type="button" disabled={archive.moreBusy} onClick={() => { setAllArchives(true); void archive.loadMore(); }}>{archive.moreBusy ? '正在读取…' : '读取更早刊物'}</button>}
      {archive.moreError && <p className="nc-inline-error" role="alert">{archive.moreError}<button className="nc-text-button" type="button" onClick={() => void archive.loadMore()}>重试</button></p>}
      <label className="nc-edition-date">按日期查看<input type="date" aria-label="报刊日期" value={requestedDay ?? selected?.day ?? ''} onChange={event => { if (event.target.value) selectDay(event.target.value); }}/></label>
      {data && items.length > 0 && <nav className="nc-contents" aria-label="本期目录"><h2 className="nc-section-heading">本期目录</h2><a href="#nc-lead" className="selected">头条与看点</a>{sections.map(section => <a key={section.category} href={'#nc-section-' + sectionOrder.indexOf(section.category)}>{section.category === '政策' ? '政策与产业' : section.category}</a>)}<a href="#nc-colophon">来源与编排</a></nav>}
      <p className="nc-archive-note">每天 08:00 出日报<br/>周一 10:00 出周报<br/>每月 1 日 10:30 出月报<br/><small>北京时间 · 从启用日起累积</small></p>
    </aside>
    <div className="nc-paper-content">
      <div className="nc-paper-topline"><span>政策 · 产业 · 公司要闻</span><span>{context.data?.meta.app_mode === 'demo' ? '演示数据 · 合成内容' : '本机资讯归档'}</span></div>
      {loadingArchive ? <p className="nc-loading" role="status">正在读取{periodName(period)}归档…</p> : archive.error ? <RequestError error={archive.error} retry={archive.reload}/> : !displayed ? <><EmptyNews title={requestedDay ? '这个日期尚未出刊' : `${paperName(period)}尚未出刊`}>{requestedDay ? '选择日历中已有刊物的日期，或查看最近一期。' : '刊物会按固定时间汇编已存资料；出刊前可先阅读资讯首页。'}</EmptyNews><div className="nc-empty-actions">{editions[0] && <button type="button" onClick={() => selectDay(editions[0].day)}>阅读最近一期</button>}<a href="/news">回到资讯首页 →</a></div><SourceStatus sources={context.data?.data.sources ?? []} coverageStart={context.data?.data.coverage_start} loading={context.busy && !context.data} unavailable={Boolean(context.error && !context.data)}/></> : <>
        <header className="nc-masthead"><div><p className="nc-paper-kicker">TIANYAN RESEARCH</p><h2>{paperName(period)}</h2><p className="nc-masthead-english">TIANYAN {period === 'day' ? 'DAILY' : period === 'week' ? 'WEEKLY' : 'MONTHLY'}</p><p className="nc-masthead-meta">资料截至 {chinaTime(displayed.window_end, true)} <span>·</span> {data?.ai ? '历史 AI 增强版 · 原始汇编可追溯' : '自动汇编'}</p></div><EditionCalendar editions={editions} selected={displayed} period={period} onSelect={selectDay}/></header>
        <div className="nc-paper-statistics" aria-label="本期统计"><span><strong>{displayed.item_count}</strong> 条要闻</span><i/><span><strong>{displayed.source_count}</strong> 个来源</span><i/><span>第 <strong>{String(displayed.issue_number).padStart(3, '0')}</strong> 期</span><span className="nc-reading-time">约{displayed.reading_minutes}分钟读完</span></div>
        {displayed.coverage_status !== 'complete' && <p className="nc-coverage-note"><i/>{displayed.coverage_note || (displayed.coverage_status === 'empty' ? '本期窗口内未收到可用资讯' : '本期资料覆盖有限')}</p>}
        {edition.error && data && <RequestError error={edition.error} retry={edition.reload}/>}
        {edition.busy && !data ? <p className="nc-loading" role="status">正在读取本期内容…</p> : edition.error && !data ? <RequestError error={edition.error} retry={edition.reload}/> : !lead ? <EmptyNews title="本期暂无可展示的资料">刊期记录已保留。资料权限或覆盖变化可能影响当前可读内容。</EmptyNews> : <>
          <div className="nc-paper-lead-grid" id="nc-lead" tabIndex={-1}><section className="nc-paper-lead"><h3 className="nc-section-heading">本期头条</h3><ArticleSecurities article={lead}/><h4><button type="button" onClick={() => changeQuery({ article: lead.id })}>{lead.title}</button></h4>{lead.summary && <p className="nc-paper-summary">{lead.summary}</p>}<AiArticleSummary ai={data?.ai} article={lead}/><div className="nc-paper-byline"><span>{lead.source_name} · {articleTime(lead, true)}{lead.late ? ' · 迟到资料' : ''}</span><OriginalLink article={lead}/></div>{data?.ai?.lead && <div className="nc-ai-guide"><strong>历史 AI 导读：</strong>{data.ai.lead}</div>}</section><aside className="nc-highlights"><h3 className="nc-section-heading">本期看点</h3>{highlights.length ? highlights.map(item => <a key={item.id} href={'#nc-article-' + item.id}><i/><span><strong>{item.title}</strong>{item.summary && <small>{item.summary}</small>}</span></a>) : <p className="nc-muted">本期共 {items.length} 条要闻，完整保留已有资料。</p>}</aside></div>
          <div className="nc-paper-columns">{sections.map(section => <section key={section.category} className="nc-paper-section" id={'nc-section-' + sectionOrder.indexOf(section.category)} tabIndex={-1}><h3 className="nc-section-heading">{section.category === '政策' ? '政策与产业' : section.category}</h3>{section.items.map(article => <article key={article.id} id={'nc-article-' + article.id} tabIndex={-1}><ArticleSecurities article={article}/><h4><button type="button" onClick={() => changeQuery({ article: article.id })}>{article.title}</button></h4>{article.summary && <p>{article.summary}</p>}<AiArticleSummary ai={data?.ai} article={article}/><div className="nc-paper-byline"><span>{article.source_name} · {articleTime(article, true)}{article.late ? ' · 迟到资料' : ''}</span><OriginalLink article={article}/></div></article>)}</section>)}</div>
        </>}
        <footer className="nc-colophon" id="nc-colophon" tabIndex={-1}><div><strong>{paperName(period)} · 第 {displayed.issue_number} 期</strong><span>出刊 {chinaTime(displayed.generated_at, true)}</span></div><p>资料窗口：{chinaTime(displayed.window_start, true)} — {chinaTime(displayed.window_end, true)}。刊期保留出刊时采用的证据版本，迟到资料编入后续刊期。</p><p>{data?.ai ? '本期保留独立标识的历史 AI 增强内容，原标题、来源与证据版本分别可查。' : '本期按来源、分类与时间自动汇编，保留原标题和来源提供的摘要。'}</p><p>日报、周报、月报 AI 增强业务待接入；当前不会自动调用模型。</p><SourceStatus sources={context.data?.data.sources ?? []} coverageStart={context.data?.data.coverage_start} loading={context.busy && !context.data} unavailable={Boolean(context.error && !context.data)}/></footer>
      </>}
      {stateError && <p className="nc-inline-error" role="alert">{stateError}</p>}
      {query.get('article') && data && !edition.busy && !currentArticle && <p className="nc-inline-error" role="alert">该资讯当前不在本期可展示的资料中。<button className="nc-text-button" type="button" onClick={() => changeQuery({ article: null })}>返回本期</button></p>}
    </div>
    {currentArticle && <NewsArticleDialog key={currentArticle.id} id={currentArticle.id} snapshot={currentArticle} csrf={csrf} close={() => changeQuery({ article: null })} onChange={applyArticle} onError={setStateError}/>}
  </div>;
}

function NewsArticleDialog({ id, snapshot, csrf, close, onChange, onError }: {
  id: string; snapshot?: NewsArticle; csrf: string; close: () => void; onChange: (article: NewsArticle) => void; onError?: (error: string) => void;
}) {
  const read = useRemote(async () => snapshot ? { data: snapshot } : newsApi.item(id), id);
  const article = read.data?.data;
  const session = useNewsSession();
  const dialog = useRef<HTMLDialogElement>(null);
  const opener = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const autoRead = useRef<string | null>(null);
  const changeRef = useRef(onChange); changeRef.current = onChange;
  useEffect(() => {
    dialog.current?.showModal();
    return () => { if (opener.current?.isConnected) opener.current.focus({ preventScroll: true }); };
  }, []);
  async function save(body: ArticleState) {
    if (!article || saving || !csrf) return;
    setSaving(true); setError('');
    const requestBody = body.read === undefined ? body : { ...body, evidence_id: article.evidence_id };
    try {
      const result = await newsApi.state(article.id, requestBody, csrf);
      read.setData(previous => previous ? { ...previous, data: mergeState(previous.data, result.data) } : previous);
      changeRef.current(result.data);
    } catch (value) { const text = message(value); setError(text); onError?.(text); }
    finally { setSaving(false); }
  }
  useEffect(() => {
    if (!article || article.is_read || autoRead.current === article.id || !csrf) return;
    autoRead.current = article.id;
    void save({ read: true });
  }, [article?.id, csrf]);
  return <dialog ref={dialog} className="nc-article-dialog" aria-label="资讯详情" onCancel={close} onClose={close}>
    <header><span>资讯详情 {snapshot && <small>· 刊期证据版本</small>}</span><button type="button" onClick={close} aria-label="关闭资讯详情"><Icon name="close"/></button></header>
    {read.error && article && <RequestError error={read.error} retry={read.reload}/>}
    {read.busy && !article ? <p className="nc-loading" role="status">正在读取资讯详情…</p> : read.error && !article ? <RequestError error={read.error} retry={read.reload}/> : article ? <div className="nc-article-body"><span className="nc-article-category">{article.category}</span><ArticleSecurities article={article}/><h2>{article.title}</h2><p className="nc-article-source">{article.source_name} · {articleTime(article, true)}</p>{article.summary ? <p className="nc-article-summary">{article.summary}</p> : <p className="nc-muted">来源未提供摘要，请查看原文获取完整内容。</p>}{article.late && <p className="nc-late-note">迟到资料：以首次接收时间确定可编入的刊期。</p>}<OriginalLink article={article}/><div className="nc-article-actions"><button type="button" disabled={!csrf || saving} className={article.is_saved ? 'selected' : ''} onClick={() => void save({ saved: !article.is_saved })}><BookmarkIcon filled={article.is_saved}/>{article.is_saved ? '已收藏' : '收藏'}</button><button type="button" disabled={!csrf || saving} className={article.is_following ? 'selected' : ''} onClick={() => void save({ following: !article.is_following })}><Icon name="plus"/>{article.is_following ? '已列入待跟进' : '加入待跟进'}</button><button type="button" disabled={!csrf || saving} onClick={() => void save({ read: !article.is_read })}>{article.is_read ? '标为未读' : '标为已读'}</button></div>{error && <p className="nc-inline-error" role="alert">{error}</p>}
      <section className="nc-manual-summary" aria-label="单篇新闻摘要"><h3>单篇新闻摘要</h3><AiAction key={article.evidence_id} feature="news_summary" newsId={article.evidence_id} session={session ?? undefined} inputAvailable={newsSummaryAvailable(article)}/>{!newsSummaryAvailable(article) && article.summary_input_reason && <p className="nc-summary-input-note">{article.summary_input_reason}</p>}</section>
      {(article.related_sources?.length ?? 0) > 0 && <section className="nc-related"><h3>关联原始出处</h3><ul>{(article.related_sources ?? []).map((source, index) => <li key={source.url + index}><a href={safeOriginal(source.url)} target="_blank" rel="noopener noreferrer">{source.name} ↗</a></li>)}</ul><small>仅展示已保存的明确关联，不按关键词推定事件关系。</small></section>}
      <details className="nc-evidence"><summary>来源与证据版本</summary><dl><dt>原始发布</dt><dd>{articleTime(article, true)}</dd><dt>本机接收</dt><dd>{chinaTime(article.received_at, true)}</dd><dt>来源</dt><dd>{article.publisher || article.source_name} · {article.family}</dd><SecurityEvidence article={article}/><dt>证据版本</dt><dd>{article.evidence_id}</dd><dt>内容校验</dt><dd>{article.content_hash}</dd></dl></details>
    </div> : null}
  </dialog>;
}
