import { useCallback, useEffect, useRef, useState } from 'react';
import { AiAction } from '../ai/AiAction';
import type { NewsItem } from '../../generated/schema';
import { api } from '../../shared/api';
import { useRead } from '../../shared/useRead';
import { Card, Empty, ErrorState, Segmented } from '../../shared/ui';
import { sourceTime } from '../data/SourceRuntime';
import { newsViews } from '../navigation';
import { changeNewsView, useNewsView } from './useNewsView';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { FinanceDaily } from './FinanceDaily';
import { useTradingContext } from '../workbench/TradingContext';
import '../workbench/tradingWidgets.css';
import { NewsCenter } from '../news-center/NewsCenter';
import { TopicResearch } from '../news-center/TopicResearch';

const dateInChina = (value: string) => new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date(value));
const publishedText = (item: NewsItem) => item.published_at ? sourceTime(item.published_at) : item.payload.published_day ? item.payload.published_day + '（仅日期）' : '未知';

export function NewsPreview({ category = '全部' }: { category?: string }) {
  const { day, mode } = useTradingContext();
  const read = useRead(api.marketNews);
  const [query,setQuery] = useState('');
  const [opened,setOpened] = useState<NewsItem | null>(null);
  useEffect(() => {
    if (mode !== 'live') return;
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') read.reload(); },60_000);
    return () => window.clearInterval(timer);
  },[mode,read.reload]);
  const items = (read.data?.data.items ?? []).filter(item => (category === '全部' || item.payload.category === category)
    && (mode !== 'history' || (item.published_at ? dateInChina(item.published_at) : item.payload.published_day) === day)
    && (!query.trim() || [item.payload.title,item.payload.summary,item.source_name].some(value => value?.includes(query.trim()))));
  useEffect(() => { setOpened(null); },[day,category]);
  return <section className="trading-widget" aria-label="市场资讯速览"><div className="trade-toolbar"><input type="search" aria-label="首页资讯查找" placeholder="标题 / 关键词" value={query} onChange={event => setQuery(event.target.value)} maxLength={80}/><button onClick={read.reload} disabled={read.busy}>刷新</button></div><p className="trade-meta">{mode === 'history' ? day : '最近已存资讯'} · 按来源发布时间 · 未建立个股关联</p>{read.busy ? <p role="status">正在读取已采集资讯…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !items.length ? <Empty title="暂无匹配的已存资讯">调整日期或关键词，或查看数据中心的资讯连接状态。</Empty> : <div className="trade-news-list">{items.map(item => <button type="button" onClick={() => setOpened(item)} key={item.id}><span><strong>{item.payload.title}</strong><small>{item.source_name} · {publishedText(item)} · {item.payload.category ?? '未分类'}</small></span><span aria-hidden="true">›</span></button>)}</div>}{opened && <NewsPreviewDrawer key={opened.id} article={opened} close={() => setOpened(null)}/>}</section>;
}

function NewsPreviewDrawer({ article, close }: { article:NewsItem; close:()=>void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const opener = document.activeElement; dialog.current?.showModal(); return () => { if (opener instanceof HTMLElement && opener.isConnected) opener.focus({preventScroll:true}); }; },[]);
  return <dialog ref={dialog} className="trade-drawer" aria-label="资讯详情" onCancel={close} onClose={close}><header><strong>资讯详情</strong><button type="button" onClick={close}>关闭</button></header><div className="trade-drawer-body"><h3>{article.payload.title}</h3><p className="trade-meta">{article.source_name} · 发布 {publishedText(article)} · 接收 {sourceTime(article.received_at)}</p>{article.payload.summary ? <p className="trade-news-summary">{article.payload.summary}</p> : <p className="trade-meta">来源未提供摘要，查看原文获取完整内容。</p>}<a href={article.payload.source_url} target="_blank" rel="noopener noreferrer">打开原文 ↗</a><details className="trade-details"><summary>记录与来源</summary><p>原始条目，尚未作研究判断。未对标题自动关联股票、题材或买卖方向。</p><p>{article.family} · {article.id}</p><p>{article.content_hash}</p></details></div></dialog>;
}

export function News() {
  const navigation = useNavigation();
  const query = new URLSearchParams(navigation.search);
  if (query.get('legacy') === '1') return <LegacyNews/>;
  return query.get('view') === 'topics' ? <TopicResearch/> : <NewsCenter/>;
}

function LegacyNews() {
  const view = useNewsView();
  if (view === '财经日报') return <><div className="news-edition-navigation"><Segmented label="资讯工作视图" options={newsViews.map(([, label]) => label)} value={view} onChange={changeNewsView}/></div><FinanceDaily/></>;
  return <NewsFeed/>;
}

function NewsFeed() {
  const sourceIndex = useRead(api.dataSources);
  const newsSources = sourceIndex.data?.data.available_adapters.filter(adapter => adapter.kind === 'news').map(adapter => [adapter.family, adapter.name]) ?? [];
  const navigation = useNavigation();
  const family = new URLSearchParams(navigation.search).get('family') ?? '';
  const read = useRead(useCallback(() => api.marketNews(family || undefined), [family]));
  const view = useNewsView();
  const [category, setCategory] = useState('全部');
  const [query, setQuery] = useState('');
  const [date, setDate] = useState('');
  const [deduplicate, setDeduplicate] = useState(true);
  const [order, setOrder] = useState('时间倒序');
  const [selected, select] = useState(new URLSearchParams(location.search).get('article'));
  const previousFamily = useRef(family);
  useEffect(() => {
    if (previousFamily.current === family) return;
    previousFamily.current = family;
    setCategory('全部'); setQuery(''); setDate(''); select(null);
  }, [family]);
  const changeSource = (value: string) => {
    const url = new URL(location.href);
    if (value) url.searchParams.set('family', value); else url.searchParams.delete('family');
    url.searchParams.delete('article');
    updateLocation(url);
  };
  const seen = new Set<string>();
  const items = (read.data?.data.items ?? []).filter(item => {
    if (family && item.family !== family) return false;
    if (category !== '全部' && item.payload.category !== category) return false;
    if (query.trim() && ![item.payload.title, item.payload.summary, item.source_name].some(text => text?.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))) return false;
    if (date && (item.published_at ? dateInChina(item.published_at) : item.payload.published_day) !== date) return false;
    const key = item.payload.source_url;
    if (deduplicate && seen.has(key)) return false;
    seen.add(key); return true;
  });
  if (order === '时间正序') items.reverse();
  const article = items.find(item => item.id === selected) ?? items[0];
  const isFeed = view === '实时列表';
  const emptyTitle = view === '事件分析' ? '事件资料未接入' : view === '题材专题' ? '题材资料未接入' : view === '我的关注' ? '关注管理尚未开放' : '暂无已采集资讯';
  return <div className="news-layout"><div className="news-list-column"><Card title={isFeed ? '资讯列表' : view} className="news-list" action={<button onClick={read.reload}>刷新已存资讯</button>}>
    <Segmented label="资讯工作视图" options={newsViews.map(([, label]) => label)} value={view} onChange={changeNewsView}/>
    <Segmented label="资讯类型" options={['全部', '财经', '政策', '公司', '行业', '公告', '海外']} value={category} onChange={setCategory}/>
    <div className="news-source-filter"><label>资讯来源 <select aria-label="资讯来源" value={family} onChange={event => changeSource(event.target.value)}><option value="">全部来源</option>{family && !newsSources.some(([value]) => value === family) && <option value={family}>不支持的来源</option>}{newsSources.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><a href="/data">查看来源状态 ›</a></div>
    <div className="news-filters"><label className="keyword-field"><span className="sr-only">资讯关键词</span><input placeholder="标题 / 关键词 / 股票代码" aria-label="资讯关键词" value={query} onChange={event => setQuery(event.target.value)} maxLength={80}/></label><label className="news-date"><span className="sr-only">资讯日期</span><input type="date" aria-label="资讯日期" value={date} onChange={event => setDate(event.target.value)}/></label><label><span className="sr-only">资讯排序</span><select aria-label="资讯排序" value={order} onChange={event => setOrder(event.target.value)}><option>时间倒序</option><option>时间正序</option></select></label><label className="checkbox-label"><input type="checkbox" checked={deduplicate} onChange={event => setDeduplicate(event.target.checked)}/>去重</label></div>
    <p className="filter-explanation">当前视图：{view} / {category}{query && ' / ' + query}{date && ' / ' + date}。{family ? '先按所选来源读取最近100条已存条目' : '读取全部来源最近100条；较早来源可通过来源筛选单独查看'}，类型、关键词与日期在本次结果中筛选，不代表全网搜索。</p>
    {read.busy ? <p role="status">正在读取已采集资讯…</p> : read.error ? <ErrorState error={read.error} retry={read.reload}/> : !isFeed || !items.length ? <Empty title={emptyTitle}>{isFeed ? '调整筛选，或查看来源连接和实际调用结果。' : '此视图仍待实现；已采集原始条目可在实时列表查看。'}</Empty> : <div className="table-wrap" tabIndex={0} role="region" aria-label="已采集资讯列表"><table><thead><tr><th>发布时间</th><th>类型</th><th>原始标题</th><th>来源</th><th>核查状态</th></tr></thead><tbody>{items.map(item => <tr key={item.id} className={article?.id === item.id ? 'news-row-selected' : ''}><td>{publishedText(item)}</td><td>{item.payload.category ?? '未分类'}</td><td><button className="text-link news-title-button" onClick={() => select(item.id)}>{item.payload.title}</button></td><td>{item.source_name}</td><td>原始条目 · 未作结论</td></tr>)}</tbody></table></div>}
    <p className="feature-note">日期精度条目按来源日期排序，同日按本机接收顺序区分。源发布时间未知时保留未知；接收时间不当作发布时间。自动刷新间隔见数据中心。</p>
  </Card></div><Card title="事实与来源" className="news-detail">
    {article && isFeed ? <><span className="badge muted">原始条目 · 尚未作研究判断</span><h3>{article.payload.title}</h3><dl className="source-fields"><dt>来源</dt><dd>{article.source_name}</dd><dt>原始发布时间</dt><dd>{publishedText(article)}</dd><dt>本机接收</dt><dd>{sourceTime(article.received_at)}</dd></dl>{article.payload.summary && <p className="news-original-summary">{article.payload.summary}</p>}<a href={article.payload.source_url} target="_blank" rel="noopener noreferrer">打开原始出处 ↗</a><details><summary>查看来源版本与校验信息</summary><p>来源身份：{article.family}</p><p>记录版本：{article.id}</p><code>{article.content_hash}</code></details></> : <Empty title="选择资讯后查看事实与来源">原文、发布时间、接收时间和校验信息分别保留。</Empty>}
    {article && isFeed && <section className="news-evidence-section"><h3>新闻摘要</h3><AiAction key={article.id} feature="news_summary" newsId={article.id} inputAvailable={!!article.payload.summary?.trim()}/></section>}
    <section className="news-evidence-section"><h3>同事件关联与影响判断</h3><p>事件归并与影响判断仍待接入。原始条目和 AI 摘要分别保留来源，不自动产生买卖判断。</p></section><div className="news-detail-actions"><button className="primary" disabled>深入事件分析</button><button disabled>加入今日研究</button></div>
  </Card></div>;
}
