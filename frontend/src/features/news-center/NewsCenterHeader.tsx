import { useLayoutEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { Icon } from '../../shared/icons';
import { updateLocation, useNavigation } from '../../shared/navigation';
import { newsHref, periods } from './model';

export function BookmarkIcon({ filled = false }: { filled?: boolean }) {
  return <svg width="22" height="22" viewBox="0 0 24 24" fill={filled ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" aria-hidden="true"><path d="M5 3h14v18l-7-4-7 4Z"/></svg>;
}
export function NewsCenterHeader() {
  const navigation = useNavigation();
  const query = new URLSearchParams(navigation.search);
  const view = query.get('view') === 'topics' ? 'topics' : query.get('view') === 'daily' ? 'daily' : 'feed';
  const period = ['week', 'month'].includes(query.get('period') ?? '') ? query.get('period')! : 'day';
  const saved = query.get('saved') === 'true';
  const searchQuery = query.get('q') ?? '';
  const [search, setSearch] = useState(searchQuery);
  useLayoutEffect(() => { setSearch(searchQuery); }, [searchQuery]);
  function submit(event: FormEvent) {
    event.preventDefault();
    updateLocation(new URL(newsHref({ view: 'feed', category: '全部', q: search.trim() }), location.origin));
  }
  return <>
    <nav className="nc-top-tabs" aria-label="资讯中心页面">
      <a href="/news" className={view === 'feed' ? 'selected' : ''} aria-current={view === 'feed' ? 'page' : undefined}>首页</a>
      <a href={'/news?'+new URLSearchParams({view:'topics',...(query.get('trade_day')?{trade_day:query.get('trade_day')!}:{}),...(query.get('as_of_time')?{as_of_time:query.get('as_of_time')!}:{})})} className={view==='topics'?'selected':''} aria-current={view==='topics'?'page':undefined}>题材专题</a>
      {periods.map(([key, label]) => <a key={key} href={newsHref({ view: 'daily', period: key })} className={view === 'daily' && period === key ? 'selected' : ''} aria-current={view === 'daily' && period === key ? 'page' : undefined}>{label}</a>)}
    </nav>
    <form className="nc-search" role="search" onSubmit={submit}>
      <button aria-label="搜索资讯" type="submit"><Icon name="search"/></button>
      <input aria-label="搜索资讯、题材、公司" placeholder="搜索资讯、题材、公司" value={search} onChange={event => setSearch(event.target.value)} maxLength={200}/>
      {search && <button type="button" aria-label="清空资讯搜索" onClick={() => { setSearch(''); if (query.has('q')) { const url = new URL(location.href); url.searchParams.delete('q'); updateLocation(url); } }}><Icon name="close"/></button>}
    </form>
    <a className={`nc-saved-link ${saved ? 'selected' : ''}`} href={newsHref({ view: 'feed', category: '全部', saved: !saved })} aria-label={saved ? '查看全部资讯' : '查看我的收藏'}><BookmarkIcon filled={saved}/><span>收藏</span></a>
  </>;
}
