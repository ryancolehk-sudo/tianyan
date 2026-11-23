import { useEffect, useState } from 'react';
import type { PropsWithChildren } from 'react';
import type { SessionResponse } from '../../generated/schema';
import { api } from '../../shared/api';
import { Icon } from '../../shared/icons';
import { useNavigation } from '../../shared/navigation';
import './workbench-v2.css';

export const workbenchTabs = [['overview', '综合盯盘'], ['auction', '竞价观察'], ['ladder', '连板观察'], ['funds', '资金动向']] as const;
export type WorkbenchTab = typeof workbenchTabs[number][0];
export function selectedTab(path: string, search: string): WorkbenchTab {
  if (path === '/auction') return 'auction';
  const view = new URLSearchParams(search).get('view');
  return workbenchTabs.some(([key]) => key === view) ? view as WorkbenchTab : 'overview';
}
export function tabLink(key: WorkbenchTab, search: string) {
  const query = new URLSearchParams(search); query.set('view', key); query.delete('legacy');
  return '/workbench?' + query;
}
function WorkbenchSearch() {
  const [query, setQuery] = useState('');
  const [items, setItems] = useState<{id: string; name: string; symbol: string}[]>([]);
  const [error, setError] = useState('');
  const navigation = useNavigation();
  useEffect(() => { setItems([]); setError(''); }, [navigation.sequence]);
  return <div className="wb-search"><form onSubmit={async event => {
    event.preventDefault(); if (!query.trim()) return; setError('');
    try { const response = await api.instruments(query.trim()); setItems(response.data.items); if (!response.data.items.length) setError('本机目录没有匹配的股票'); }
    catch { setError('查询暂不可用，请重试'); }
  }}><Icon name="search"/><input aria-label="搜索代码或名称" placeholder="搜索代码 / 名称" value={query} onChange={event => setQuery(event.target.value)} maxLength={80}/><button type="submit" aria-label="搜索">↵</button></form>
  {(items.length > 0 || error) && <div className="wb-search-results">{error || items.map(item => { const params = new URLSearchParams(navigation.search); params.set('instrument', item.id); return <a key={item.id} href={'/workbench?' + params}><span>{item.symbol}</span>{item.name}</a>; })}</div>}</div>;
}
export function WorkbenchShell({ path, session, onLogout, children }: PropsWithChildren<{path: string; session: SessionResponse; onLogout: () => void}>) {
  const navigation = useNavigation();
  const view = selectedTab(path, navigation.search);
  const [now, setNow] = useState(new Date());
  const [menu, setMenu] = useState(false);
  useEffect(() => { const timer = window.setInterval(() => setNow(new Date()), 1000); return () => window.clearInterval(timer); }, []);
  useEffect(() => setMenu(false), [navigation.sequence]);
  return <div className="wb-app"><a className="skip-link" href="#main-content">跳至页面内容</a>
    <aside className={'wb-sidebar ' + (menu ? 'open' : '')}>
      <a className="wb-brand" href="/workbench"><span>天衍系统</span></a>
      <nav aria-label="主导航">{[['/workbench', '工作台', 'workbench'], ['/news', '资讯中心', 'news'], ['/data', '数据管理', 'data'], ['/system', '系统设置', 'system']].map(([href, label, icon]) => <a key={href} href={href} className={href === '/workbench' ? 'active' : ''}><Icon name={icon as 'workbench'}/><span>{label}</span></a>)}</nav>
      <div className="wb-sidebar-foot"><span>事实 · 判断 · 验证</span><button onClick={onLogout}>退出账号</button></div>
    </aside>
    <div className="wb-workspace"><header className="wb-topbar"><button className="wb-menu" aria-label="展开导航" onClick={() => setMenu(!menu)}>☰</button><h1>工作台</h1><span className="wb-clock">北京时间 · {now.toLocaleTimeString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false})}</span><WorkbenchSearch/><span className="wb-source-badge">{session.meta.app_mode === 'demo' ? '演示环境 · 合成样本' : 'Tushare · 真实数据'}</span></header>
      <nav className="wb-tabs" aria-label="工作台子页面">{workbenchTabs.map(([key, label]) => <a key={key} href={tabLink(key, navigation.search)} className={key === view ? 'active' : ''} aria-current={key === view ? 'page' : undefined}>{label}</a>)}<a href="/research/reviews">收盘复盘</a><span className="wb-tabs-note">四页联动观察</span></nav>
      {children}
    </div>
  </div>;
}