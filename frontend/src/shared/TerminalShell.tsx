import { useEffect, useMemo, useRef, useState } from 'react';
import type { PropsWithChildren } from 'react';
import type { SessionResponse } from '../generated/schema';
import { Icon } from './icons';
import { Search } from './Shell';
import { useNavigation, updateLocation } from './navigation';
import { terminalView } from './terminalNavigation';
import { isDailyReview } from './reviewNavigation';
import { NewsCenterHeader } from '../features/news-center/NewsCenterHeader';
import { WorkspaceSidebar } from './WorkspaceSidebar';
import { workspacePages } from './workspacePages';
import './TerminalShell.css';
import './boardWorkspace.css';
import { marketPhaseLabel, useMarketSession } from './marketSession';
import { RecoveryHealthIndicator } from './RecoveryHealth';
import { NotificationButton } from './BackgroundRefresh';

type Context = { stock_name?: string | null; sector_name?: string | null; trade_day?: string; latest_at?: string | null; mode?: string };
export function TerminalShell({ path, session, onLogout, children }: PropsWithChildren<{
  path: string; session: SessionResponse; onLogout: () => void;
}>) {
  const nav = useNavigation();
  const legacy = new URLSearchParams(nav.search).get('legacy') === '1';
  const isNews = path === '/news' && !legacy;
  const isSystem = path === '/system';
  const isData = path === '/data';
  const view = terminalView(path, nav.search);
  const tomorrow = path === '/research/tomorrow';
  const review = tomorrow || isDailyReview(path, nav.search, nav.hash);
  const terminal = (path === '/workbench' || path === '/auction') && !legacy;
  const modern = terminal || review || isNews || isSystem || isData;
  const query = new URLSearchParams(nav.search);
  const marketState = useMarketSession();
  const market = marketState.market;
  const [context, setContext] = useState<Context>({});
  const [menu, setMenu] = useState(false);
  const [now, setNow] = useState(new Date());
  const menuButton = useRef<HTMLButtonElement>(null);
  const groups = useMemo(() => workspacePages(path, nav.search, nav.hash, context.trade_day), [path, nav.search, nav.hash, context.trade_day]);
  const currentGroup = groups.find(group => group.pages.some(item => item.current));
  const currentPage = currentGroup?.pages.find(item => item.current);
  useEffect(() => {
    const receive = (event: Event) => setContext((event as CustomEvent<Context>).detail);
    window.addEventListener('ty-terminal-context', receive);
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => { window.removeEventListener('ty-terminal-context', receive); window.clearInterval(timer); };
  }, []);
  useEffect(() => { setMenu(false); }, [nav.sequence]);
  const overview = terminal && view === 'overview';
  const selected = !review && view === 'funds' ? context.sector_name ?? query.get('fund_sector') : overview && query.get('stock') === '' ? null : context.stock_name ?? query.get('stock');
  const clear = () => { const url = new URL(location.href); url.searchParams.delete(!review && view === 'funds' ? 'fund_sector' : 'stock'); if (overview) url.searchParams.set('stock', ''); url.searchParams.delete('instrument'); url.searchParams.delete('record_key'); updateLocation(url); };
  const time = now.toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
  const explicitDay = query.get('trade_day') || (tomorrow ? query.get('source_trade_day') : null);
  const historical = !!query.get('as_of_time') || !!explicitDay && (!market || explicitDay !== market.trade_day);
  const marketLabel = historical ? query.get('as_of_time') ? '当时已知' : '历史回看' : market ? (market.is_trading_day === false ? '休市' : marketPhaseLabel[market.phase]) + (marketState.error ? ' · 状态待重核' : '') : context.mode === 'review' ? '已保存行情' : '市场观察';
  return <div className={`app board-app ${modern ? 'ty-shell' : 'board-legacy'} ${overview ? 'ty-overview-shell' : ''} ${isNews ? 'ty-news-shell' : ''} ${isSystem ? 'ty-settings-shell' : ''}`}>
    <a className="skip-link" href="#main-content">跳至页面内容</a>
    <WorkspaceSidebar groups={groups} session={session} open={menu} setOpen={setMenu} menuButton={menuButton} onLogout={onLogout}/>
    <div className="workspace ty-shell-workspace" inert={menu}>
      <header className={`ty-topbar board-topbar ${isNews ? 'ty-news-topbar' : ''}`}>
        <button ref={menuButton} className="board-menu-toggle" type="button" aria-label="打开主导航" aria-controls="site-navigation" aria-expanded={menu} onClick={() => setMenu(true)}><Icon name="menu"/></button>
        <div className="board-page-title">{currentGroup && <span>{currentGroup.label}<i aria-hidden="true">/</i></span>}
          <h1>{currentPage?.label ?? '工作空间'}</h1>
        </div>
        {(terminal || review) && <div className="ty-clock"><span>{review ? '盘后复盘' : marketLabel} {review || historical ? explicitDay || context.trade_day || '' : market?.trade_day || context.trade_day || ''}</span><time dateTime={now.toISOString()} title="当前北京时间">{time}</time></div>}
        {isNews ? <NewsCenterHeader/> : !(isSystem || isData) && <Search navigationSequence={nav.sequence}/>}
        {(terminal || review) && selected && <button type="button" className="ty-current-stock" title={view === 'funds' && !review ? '清除选中板块' : '清除选中个股'} aria-label={`清除选中${view === 'funds' && !review ? '板块' : '个股'}：${selected}`} onClick={clear}>{selected}<span aria-hidden="true">×</span></button>}
        {overview ? <div className="ty-source-state"><span className="ty-source-badge">{session.meta.app_mode === 'demo' ? '演示环境 · 非真实行情' : '已接入来源 · 真实数据'}</span><RecoveryHealthIndicator/></div>
          : (review || terminal && (view !== 'auction' || session.meta.app_mode === 'demo')) && <div className="ty-source-state">
            <span className="ty-source-badge">{session.meta.app_mode === 'demo' ? '演示环境 · 非真实行情' : review ? '真实资料 · 本机复盘' : '已接入来源 · 真实数据'}</span>
            {(review || view !== 'auction') && <RecoveryHealthIndicator/>}
          </div>}
        {(isData || !modern) && <span className="board-instance">{session.meta.app_mode === 'demo' ? '演示环境' : '本机实际状态'}</span>}
        {isSystem && <span className="ss-header-account"><Icon name="shield"/>{session.data.user.username}<small>{session.data.user.role === 'admin' ? '管理员' : '研究员'}</small></span>}
        <NotificationButton/>
      </header>
      {children}
    </div>
  </div>;
}
