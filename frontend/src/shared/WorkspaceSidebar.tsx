import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { RefObject } from 'react';
import type { SessionResponse } from '../generated/schema';
import { Icon } from './icons';
import { updateLocation } from './navigation';
import type { WorkspacePage, workspacePages } from './workspacePages';

type Props = {
  groups: ReturnType<typeof workspacePages>; session: SessionResponse; open: boolean;
  menuButton: RefObject<HTMLButtonElement | null>; setOpen: (open: boolean) => void; onLogout: () => void;
};

function currentNavTarget(panel: HTMLElement | null) {
  const link = panel?.querySelector<HTMLAnchorElement>('a[aria-current="page"]');
  return link;
}

export function WorkspaceSidebar({ groups, session, open, menuButton, setOpen, onLogout }: Props) {
  const panel = useRef<HTMLElement>(null);
  const filter = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState('');
  const current = groups.flatMap(group => group.pages).find(item => item.current);
  const needle = query.replace(/\s+/g, '').toLowerCase();
  const matches = (text: string) => text.replace(/\s+/g, '').toLowerCase().includes(needle);
  const visible = groups.map(group => ({ ...group, pages: group.pages.flatMap(item => {
    if (matches(group.label + item.label)) return [item];
    const topic = item.topics?.find(entry => matches(entry.keywords));
    return topic ? [{ ...item, href: topic.href, match: topic.label }] : [];
  }) })).filter(group => group.pages.length);
  function pageLink(item: WorkspacePage) {
    return <a key={item.label} href={item.href} aria-current={item.current ? 'page' : undefined} onClick={event => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      setQuery(''); setOpen(false);
      const url = new URL(event.currentTarget.href);
      if (url.pathname === location.pathname && ['/research/reviews', '/system', '/data'].includes(url.pathname)
        && (!url.hash || url.pathname === '/data') && !new URLSearchParams(location.search).has('legacy') && !url.searchParams.has('legacy')) {
        event.preventDefault(); updateLocation(url);
        if ((open || needle) && !url.hash) requestAnimationFrame(() => document.getElementById('main-content')?.focus({ preventScroll: true }));
      }
    }}>{item.icon && <Icon name={item.icon}/>}<span>{item.label}{item.match && <small className="board-search-topic">{item.match}</small>}</span></a>;
  }
  function close() {
    setOpen(false);
    requestAnimationFrame(() => menuButton.current?.focus({ preventScroll: true }));
  }
  useEffect(() => {
    if (!open) return;
    const focusTarget = currentNavTarget(panel.current) ?? filter.current;
    focusTarget?.focus({ preventScroll: true });
    const desktop = matchMedia('(min-width: 1001px)');
    const resize = () => { if (desktop.matches) setOpen(false); };
    desktop.addEventListener('change', resize);
    return () => desktop.removeEventListener('change', resize);
  }, [open, setOpen]);
  useLayoutEffect(() => {
    const link = currentNavTarget(panel.current);
    const list = link?.closest('nav');
    if (!list || !link) return;
    const row = link.getBoundingClientRect(), box = list.getBoundingClientRect();
    if (row.top < box.top || row.bottom > box.bottom) list.scrollTop += row.top - box.top - list.clientHeight / 3;
  }, [current?.label, open, query]);
  const account = session.data.user;
  return <>
    {open && <button type="button" className="board-scrim" tabIndex={-1} aria-label="关闭导航" onClick={close}/>}
    <aside id="site-navigation" className={'board-sidebar' + (open ? ' is-open' : '')} ref={panel}
      role={open ? 'dialog' : undefined} aria-modal={open || undefined} aria-label="工作空间导航" onKeyDown={event => {
        if (!open) return;
        if (event.key === 'Escape') { event.preventDefault(); close(); }
        if (event.key !== 'Tab') return;
        const elements = [...(panel.current?.querySelectorAll<HTMLElement>('a, button, input') ?? [])].filter(el => el.getClientRects().length);
        if (event.shiftKey && document.activeElement === elements[0]) { event.preventDefault(); elements.at(-1)?.focus(); }
        if (!event.shiftKey && document.activeElement === elements.at(-1)) { event.preventDefault(); elements[0]?.focus(); }
      }}>
      <div className="board-brand-row"><a className="board-brand" href="/workbench" aria-label="天衍系统首页">
        <img src="/tianyan-brand.ico" width="36" height="36" alt=""/><strong>天衍系统</strong>
      </a><button type="button" className="board-close" aria-label="收起主导航" onClick={close}><Icon name="close"/></button></div>
      <label className="board-nav-search"><Icon name="search"/>
        <input ref={filter} type="search" aria-label="查找页面" placeholder="查找页面" value={query}
          onChange={event => setQuery(event.target.value)} onKeyDown={event => {
            if (event.key === 'Escape' && query) { event.preventDefault(); event.stopPropagation(); setQuery(''); }
          }}/>
      </label>
      <nav className="board-page-list" aria-label="主导航">
        {visible.map(group => <div key={group.label} className="board-nav-group" role="group" aria-label={group.label}>
          <div className="board-nav-label" aria-hidden="true">{group.label}</div>
          {group.pages.map(pageLink)}
        </div>)}
        {!visible.length && <p className="board-no-pages" role="status">没有匹配的页面</p>}
      </nav>
      <div className="board-account"><span className="board-account-avatar" aria-hidden="true">{account.username.slice(0, 1).toUpperCase()}</span>
        <div><strong title={account.username}>{account.username}</strong><small>{account.role === 'admin' ? '管理员' : '研究员'}</small></div>
        <button type="button" onClick={onLogout} aria-label="退出账号" title="退出账号"><Icon name="logout"/></button>
      </div>
    </aside>
  </>;
}
