import { useEffect, useRef, useState } from 'react';
import type { FormEvent, PropsWithChildren } from 'react';
import type { InstrumentListResponse, SessionResponse } from '../generated/schema';
import { newsRoutes, researchRoutes, routes } from '../features/navigation';
import { useNewsView } from '../features/market/useNewsView';
import { api } from './api';
import { Brand, Empty, ErrorState } from './ui';
import { Icon } from './icons';
import type { IconName } from './icons';
import { MarketOverview } from '../features/market/Quotes';
import { updateLocation } from './navigation';
import { TerminalShell } from './TerminalShell';
import { isDailyReview } from './reviewNavigation';
import { NotificationButton } from './BackgroundRefresh';
import { installDetailsDismissal } from './detailsDismissal';

function NavigationGroup({ id, name, icon, active, items, selected }: {
  id: string; name: string; icon: IconName; active: boolean;
  items: readonly (readonly [string, string])[]; selected: string;
}) {
  const [open, setOpen] = useState(active);
  useEffect(() => { if (active) setOpen(true); }, [active]);
  return <div className="nav-item nav-group" data-nav-group={id}>
    <button type="button" className={`nav-link nav-toggle ${active ? 'active' : ''}`} aria-expanded={open} aria-controls={`${id}-children`} onClick={() => setOpen(value => !value)}>
      <Icon name={icon}/><span>{name}</span><Icon name="chevron" className="nav-chevron"/>
    </button>
    <div id={`${id}-children`} className="nav-submenu" role="group" aria-label={`${name}子菜单`} hidden={!open}>
          {items.map(([href, label]) => <a href={href} key={href} aria-current={selected === href ? 'page' : undefined}>{label}</a>)}
    </div>
  </div>;
}

export function Search({ navigationSequence }: { navigationSequence: number }) {
  const legacy = new URLSearchParams(location.search).get('legacy') === '1';
  const reviewHere = isDailyReview(location.pathname, location.search);
  function reviewStockHref(symbol: string, exchange: string) {
    const target = new URL(location.href);
    target.searchParams.set('stock', symbol + '.' + ({ SSE: 'SH', SZSE: 'SZ', BSE: 'BJ' } as Record<string, string>)[exchange]);
    target.searchParams.delete('record_key'); target.searchParams.delete('instrument'); target.searchParams.delete('edit');
    return target.pathname + target.search;
  }
  const localSelection = reviewHere || location.pathname === '/workbench' || !legacy && location.pathname === '/auction';
  function resultHref(item: InstrumentListResponse['data']['items'][number]) {
    if (reviewHere) return reviewStockHref(item.symbol, item.exchange);
    if (!localSelection) return '/workbench?' + (legacy ? 'legacy=1&' : '') + 'instrument=' + encodeURIComponent(item.id);
    const target = new URL(location.href);
    target.searchParams.set('instrument', item.id);
    if (!legacy) target.searchParams.set('stock', item.symbol + '.' + ({ SSE: 'SH', SZSE: 'SZ', BSE: 'BJ' } as Record<string, string>)[item.exchange]);
    return target.pathname + target.search + target.hash;
  }
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<InstrumentListResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const request = useRef(0);
  const searchRoot = useRef<HTMLDivElement>(null), searchInput = useRef<HTMLInputElement>(null);
  function close(restore = false) { request.current++; setOpen(false); setBusy(false); if (restore) searchInput.current?.focus({ preventScroll: true }); }
  useEffect(() => close(), [navigationSequence]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: Event) => { if (!searchRoot.current?.contains(event.target as Node)) close(); };
    document.addEventListener('pointerdown', outside); document.addEventListener('focusin', outside);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('focusin', outside); };
  }, [open]);
  async function search(event?: FormEvent) {
    event?.preventDefault(); if (!query.trim()) return;
    const version = ++request.current;
    setOpen(true); setBusy(true); setError(null); setResult(null);
    try { const data = await api.instruments(query.trim()); if (version === request.current) setResult(data); }
    catch (e) { if (version === request.current) setError(e instanceof Error ? e : new Error('查询失败，请重试。')); }
    finally { if (version === request.current) setBusy(false); }
  }
  return <div ref={searchRoot} className="global-search" onKeyDown={event => {
    if (event.key === 'Escape' && open) { event.preventDefault(); event.stopPropagation(); close(true); }
    if (open && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      const links = [...(searchRoot.current?.querySelectorAll<HTMLAnchorElement>('.search-results li a') ?? [])];
      const index = links.indexOf(document.activeElement as HTMLAnchorElement);
      if (!links.length || index < 0 && !['ArrowDown', 'ArrowUp'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? links.length - 1
        : index < 0 ? event.key === 'ArrowDown' ? 0 : links.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + links.length) % links.length;
      links[next]?.focus();
    }
  }}>
    <form role="search" onSubmit={search}><Icon name="search"/><label className="sr-only" htmlFor="instrument-search">搜索标的</label><input ref={searchInput} id="instrument-search" value={query} onChange={e => { setQuery(e.target.value); if (open) close(); }} maxLength={80} placeholder="输入股票代码 / 名称" autoComplete="off" aria-expanded={open} aria-controls={open ? 'instrument-search-results' : undefined}/><button aria-label="搜索标的" disabled={!query.trim() || busy}>{busy ? '查询中' : '搜索'}</button></form>
    {open && <section id="instrument-search-results" className="search-results" aria-label="标的查询结果"><div className="search-results-heading"><strong>标的查询</strong><button type="button" className="icon-button" onClick={() => close(true)} aria-label="关闭查询结果"><Icon name="close"/></button></div>
      {busy ? <p role="status">正在查询标的库…</p> : error ? <ErrorState error={error} retry={() => search()}/> : result?.meta.quality.status === 'unavailable' ? <Empty title="标的库未接入">当前无法查询股票代码与名称。<a className="text-link" href="/data">查看数据状态 <Icon name="arrow"/></a></Empty> : result?.data.items.length ? <><p className="search-scope-note">本机已留存的标的，尚非完整市场目录。选择时保留市场与品种。</p><ul>{result.data.items.map(item => <li key={item.id}><a href={resultHref(item)} onClick={event => { if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; close(); if (localSelection) { event.preventDefault(); updateLocation(new URL(resultHref(item), location.origin)); } }}><span>{item.symbol}</span><strong>{item.name}</strong><small>{item.exchange} · {({ stock: '股票', index: '指数', etf: 'ETF', convertible: '转债' })[item.asset_type ?? 'stock']}</small></a></li>)}</ul></> : <Empty title="没有匹配的标的">尝试完整代码或其他名称；当前只查本机已留存目录，不代表整个市场不存在该标的。</Empty>}
    </section>}
  </div>;
}

function Clock() {
  const [now, setNow] = useState(new Date());
  useEffect(() => { const timer = window.setInterval(() => setNow(new Date()), 1000); return () => window.clearInterval(timer); }, []);
  return <div className="local-clock"><time dateTime={now.toISOString()}>{now.toLocaleString('sv-SE', { timeZone: 'Asia/Shanghai' })}</time><small>本机时间 · 北京时间</small></div>;
}

export function Shell(props: PropsWithChildren<{ path: string; navigationSequence: number; session: SessionResponse; onLogout: () => void }>) {
  useEffect(installDetailsDismissal, []);
  const modern = props.path === '/system' || props.path === '/data' || props.path === '/research/tomorrow' || ((props.path === '/workbench' || props.path === '/auction' || props.path === '/news') && new URLSearchParams(location.search).get('legacy') !== '1') || isDailyReview(props.path, location.search);
  return modern ? <TerminalShell {...props}/> : <LegacyShell {...props}/>;
}
function LegacyShell({ path, navigationSequence, session, onLogout, children }: PropsWithChildren<{ path: string; navigationSequence: number; session: SessionResponse; onLogout: () => void }>) {
  const [menu, setMenu] = useState(false);
  const newsView = useNewsView();
  const dataCenterLive = path === '/data';
  const navigationRoutes = dataCenterLive ? [
    ['/workbench', '工作台', 'workbench'], ['/research/reviews', '每日复盘', 'research'],
    ['/news', '资讯中心', 'news'], ['/data', '数据管理', 'data'], ['/system', '系统设置', 'system'],
  ] as const : routes;
  const compatibleNewsRoutes = newsRoutes.map(([href, label]) => [href + '&legacy=1', label] as const);
  const menuButton = useRef<HTMLButtonElement>(null);
  const sidebar = useRef<HTMLElement>(null);
  function closeMenu() { setMenu(false); menuButton.current?.focus(); }
  useEffect(() => { setMenu(false); }, [navigationSequence]);
  useEffect(() => {
    if (!menu) return;
    const current = sidebar.current?.querySelector<HTMLAnchorElement>('a[aria-current="page"]');
    if (current?.getClientRects().length) current.focus();
    else sidebar.current?.querySelector<HTMLButtonElement>('.nav-toggle.active')?.focus();
    const escape = (e: KeyboardEvent) => { if (e.key === 'Escape') { setMenu(false); menuButton.current?.focus(); } };
    window.addEventListener('keydown', escape); return () => window.removeEventListener('keydown', escape);
  }, [menu]);
  return <div className={`app ${path.startsWith('/research/') && new URLSearchParams(location.search).get('legacy') !== '1' ? 'research-shell' : ''}`}>
    <a className="skip-link" href="#main-content">跳至页面内容</a>
    {menu && <button className="nav-scrim" aria-label="关闭导航" onClick={closeMenu}/>}
    <aside id="site-navigation" ref={sidebar} className={`sidebar ${menu ? 'is-open' : ''}`}>
      <div className="sidebar-brand-row"><a href="/workbench" className="brand-link" aria-label="天衍系统首页"><Brand/></a><button className="icon-button nav-close" aria-label="收起主导航" onClick={closeMenu}><Icon name="close"/></button></div>
      <span className="sidebar-section-label">研究工作空间</span>
      <nav className="primary-navigation" aria-label="主导航">{navigationRoutes.map(([href, name, icon]) => {
        const active = href === path || (href.startsWith('/research') && path.startsWith('/research'));
        if (!dataCenterLive && href.startsWith('/research')) return <NavigationGroup key={href} id="research" name={name} icon={icon} active={active} items={researchRoutes} selected={path}/>;
        if (!dataCenterLive && href === '/news') return <NavigationGroup key={href} id="news" name={name} icon={icon} active={active} items={compatibleNewsRoutes} selected={active ? compatibleNewsRoutes.find(([, label]) => label === newsView)![0] : ''}/>;
        return <a key={href} href={href} className={`nav-item nav-link ${active ? 'active' : ''}`} aria-current={active ? 'page' : undefined}><Icon name={icon}/><span>{name}</span></a>;
      })}</nav>
      <div className="sidebar-foot"><div className="timeline-title"><Icon name="auction"/><strong>观察时段参考</strong></div><ol className="session-timeline">{[['09:15', '竞价开始'], ['09:20', '竞价更新'], ['09:25', '竞价结束'], ['09:30', '上午开盘'], ['11:30', '午间休市'], ['13:00', '下午开盘'], ['14:56', '尾盘观察'], ['15:00', '收盘']].map(([time, label]) => <li key={time}><time>{time}</time><span>{label}</span></li>)}</ol><small>仅供时段参考<br/>当前市场阶段尚未判定</small></div>
      <div className="sidebar-caption">事实 · 判断 · 验证</div>
    </aside>
    <div className="workspace"><header className="topbar">
      <button ref={menuButton} className="icon-button menu-toggle" aria-label="打开主导航" aria-expanded={menu} aria-controls="site-navigation" onClick={() => setMenu(!menu)}><Icon name="menu"/></button>
      <a href="/workbench" className="mobile-brand" aria-label="天衍系统首页"><strong>天衍系统</strong></a>
      <Clock/><Search navigationSequence={navigationSequence}/>
      <button className="header-action" onClick={() => window.location.reload()}><Icon name="refresh"/><span>刷新</span></button>
      <a className="header-action settings-link" href="/system"><Icon name="system"/><span>设置</span></a>
      <span className={`badge environment ${session.meta.app_mode === 'demo' ? 'demo' : ''}`}>{session.meta.app_mode === 'demo' ? '演示环境' : '真实环境'}</span>
      <span className="account-name" title="当前账号">{session.data.user.username}</span>
      <button className="header-action" onClick={onLogout}><Icon name="logout"/><span>退出</span></button>
      <NotificationButton hostId={dataCenterLive ? 'dc-notifications' : undefined}/>
    </header>
    <div id="workbench-header" className="workbench-header" hidden={path !== '/workbench'}/>
    <MarketOverview/>
    {children}</div>
  </div>;
}
