import { useEffect, useRef, useState } from 'react';
import type { PropsWithChildren } from 'react';
import type { SessionResponse } from '../generated/schema';
import { Icon } from './icons';
import { Search } from './Shell';
import { useNavigation, updateLocation } from './navigation';
import { terminalHref, terminalTabs, terminalView } from './terminalNavigation';
import { isDailyReview, reviewHref, reviewTabs, reviewView } from './reviewNavigation';
import { NewsCenterHeader } from '../features/news-center/NewsCenterHeader';
import './TerminalShell.css';
import { marketPhaseLabel, useMarketSession } from './marketSession';
import { RecoveryHealthIndicator } from './RecoveryHealth';
import { NotificationButton } from './BackgroundRefresh';

type Context = { stock_name?: string | null; sector_name?: string | null; trade_day?: string; latest_at?: string | null; mode?: string };
export function TerminalShell({ path, session, onLogout, children }: PropsWithChildren<{
  path: string; session: SessionResponse; onLogout: () => void;
}>) {
  const nav = useNavigation();
  const isNews = path === '/news';
  const isSystem = path === '/system';
  const isData = path === '/data';
  const view = terminalView(path, nav.search);
  const tomorrow = path === '/research/tomorrow';
  const review = tomorrow || isDailyReview(path, nav.search, nav.hash);
  const currentReview = reviewView(nav.search);
  const query = new URLSearchParams(nav.search);
  const marketState = useMarketSession();
  const market = marketState.market;
  const [context, setContext] = useState<Context>({});
  const [menu, setMenu] = useState(false);
  const [now, setNow] = useState(new Date());
  const menuButton = useRef<HTMLButtonElement>(null);
  const sidebar = useRef<HTMLElement>(null);
  useEffect(() => {
    const receive = (event: Event) => setContext((event as CustomEvent<Context>).detail);
    window.addEventListener('ty-terminal-context', receive);
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => { window.removeEventListener('ty-terminal-context', receive); window.clearInterval(timer); };
  }, []);
  useEffect(() => { setMenu(false); }, [nav.sequence]);
  useEffect(() => {
    if (!menu) return;
    sidebar.current?.querySelector<HTMLAnchorElement>('[aria-current="page"]')?.focus();
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') { setMenu(false); menuButton.current?.focus(); } };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [menu]);
  const selected = !review && view === 'funds' ? context.sector_name ?? query.get('fund_sector') : context.stock_name ?? query.get('stock');
  const clear = () => { const url = new URL(location.href); url.searchParams.delete(!review && view === 'funds' ? 'fund_sector' : 'stock'); url.searchParams.delete('instrument'); url.searchParams.delete('record_key'); updateLocation(url); };
  const time = now.toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
  const reviewDate = query.get('source_trade_day') || query.get('trade_day') || context.trade_day;
  const reviewSearch = tomorrow ? new URLSearchParams(reviewDate ? { trade_day: reviewDate } : {}).toString() : nav.search;
  const tomorrowHref = tomorrow ? path + nav.search : '/research/tomorrow' + (reviewDate ? '?' + new URLSearchParams({ source_trade_day: reviewDate }) : '');
  const explicitDay = query.get('trade_day');
  const historical = !!query.get('as_of_time') || !!explicitDay && (!market || explicitDay !== market.trade_day);
  const marketLabel = historical ? query.get('as_of_time') ? '当时已知' : '历史回看' : market ? (market.is_trading_day === false ? '休市' : marketPhaseLabel[market.phase]) + (marketState.error ? ' · 状态待重核' : '') : context.mode === 'review' ? '已保存行情' : '市场观察';
  return <div className={`app ty-shell ${isNews ? 'ty-news-shell' : ''} ${isSystem ? 'ty-settings-shell' : ''}`}>
    <a className="skip-link" href="#main-content">跳至页面内容</a>
    {menu && <button className="nav-scrim" aria-label="关闭导航" onClick={() => setMenu(false)}/>}
    <aside ref={sidebar} id="site-navigation" className={`sidebar ty-sidebar ${menu ? 'is-open' : ''}`}>
      <a className="ty-brand" href="/workbench" aria-label="天衍系统首页"><span>天衍系统</span></a>
      <nav className="primary-navigation" aria-label="主导航">
        <a href="/workbench" className={`nav-link ${!review && !isNews && !isSystem && !isData ? 'active' : ''}`} aria-current={!review && !isNews && !isSystem && !isData ? 'page' : undefined}><Icon name="workbench"/><span>工作台</span></a>
        <a href="/research/reviews" className={`nav-link ${review ? 'active' : ''}`} aria-current={review ? 'page' : undefined}><Icon name="research"/><span>每日复盘</span></a>
        <a href="/news" className={`nav-link ${isNews ? 'active' : ''}`} aria-current={isNews ? 'page' : undefined}><Icon name="news"/><span>资讯中心</span></a>
        <a href="/data" className={`nav-link ${isData ? 'active' : ''}`} aria-current={isData ? 'page' : undefined}><Icon name="data"/><span>数据管理</span></a>
        <a href="/system" className={`nav-link ${isSystem ? 'active' : ''}`} aria-current={isSystem ? 'page' : undefined}><Icon name="system"/><span>系统设置</span></a>
      </nav>
      <div className="ty-sidebar-bottom"><span>事实 · 观察 · 验证</span><small title="当前登录账号">{session.data.user.username}</small><button onClick={onLogout}><Icon name="logout"/>退出</button></div>
    </aside>
    <div className="workspace ty-shell-workspace">
      <header className={`ty-topbar ${isNews ? 'ty-news-topbar' : ''}`}>
        <button ref={menuButton} className="icon-button menu-toggle" aria-label="打开主导航" aria-controls="site-navigation" aria-expanded={menu} onClick={() => setMenu(!menu)}><Icon name="menu"/></button>
        {isSystem ? <><h1>系统设置</h1><span className="ss-header-note">工作空间 · 个人使用与本机维护</span><span className="ss-header-account"><Icon name="shield"/>{session.data.user.username}<small>{session.data.user.role === 'admin' ? '管理员' : '研究员'}</small></span></> : isNews ? <><h1>资讯中心</h1><NewsCenterHeader/></> : <>
        <h1>{review ? '每日复盘' : '工作台'}</h1><div className="ty-clock"><span>{review ? '收盘后 · 行情汇总 · 跨日反馈' : marketLabel} {review || historical ? context.trade_day || explicitDay || '' : market?.trade_day || context.trade_day || ''}</span><time dateTime={now.toISOString()} title="当前北京时间">{time}</time></div>
        <Search navigationSequence={nav.sequence}/>
        {(review || view !== 'auction' || session.meta.app_mode === 'demo') && <div className="ty-source-state"><span className="ty-source-badge">{session.meta.app_mode === 'demo' ? '演示环境 · 非真实行情' : review ? '真实资料 · 本机复盘' : '已接入来源 · 真实数据'}</span>{(review || view !== 'auction') && <RecoveryHealthIndicator/>}</div>}
        </>}
        <NotificationButton/>
      </header>
      {!isNews && !isSystem && <div className={`ty-tabbar ${review ? 'dr-tabbar' : ''}`}><nav aria-label={review ? '每日复盘页面' : '工作台页面'}>{review ? <>{reviewTabs.map(([key, label]) => <a href={reviewHref(key, reviewSearch)} onClick={e => { if (!tomorrow) { e.preventDefault(); updateLocation(new URL(e.currentTarget.href)); } }} key={key} className={!tomorrow && currentReview === key ? 'selected' : ''} aria-current={!tomorrow && currentReview === key ? 'page' : undefined}>{label}</a>)}<a href={tomorrowHref} className={tomorrow ? 'selected' : ''} aria-current={tomorrow ? 'page' : undefined}>明日预案</a></> : terminalTabs.map(([key, label]) => <a href={terminalHref(key, nav.search)} key={key} className={view === key ? 'selected' : ''} aria-current={view === key ? 'page' : undefined}>{label}</a>)}</nav>
        <div className="ty-selected"><span>{!review && view === 'funds' ? '选中板块' : '选中个股'}：</span>{selected ? <button title="清除手动选择" onClick={clear}>{selected}<span aria-hidden="true">×</span></button> : <small>尚未选择</small>}</div>
      </div>}
      {children}
    </div>
  </div>;
}
