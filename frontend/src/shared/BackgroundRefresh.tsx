import { useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';
import { createPortal } from 'react-dom';

export const backgroundRefreshDelay = 15_000;
type Phase = 'waiting' | 'delayed' | 'failed';
type Notice = { id: symbol; phase: Phase; title: string; message: string; actionLabel: string; action?: () => void; opensDialog?: boolean };
type ReadNotice = { key: unknown; waiting?: boolean; error?: string | Error | null; title: string; message?: string;
  showWaiting?: boolean; action?: () => void; actionLabel?: string; opensDialog?: boolean };
const notices = new Map<symbol, Notice>();
const listeners = new Set<() => void>();
const notifiedErrors = new WeakSet<Error>();
let visible: Notice[] = [];
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
const snapshot = () => visible;
const retryReads = () => window.dispatchEvent(new Event('read-cache-revalidate'));
function update(id: symbol, notice?: Notice) {
  if (!notice && !notices.has(id)) return;
  if (notice) notices.set(id, notice); else notices.delete(id);
  visible = [...notices.values()]; listeners.forEach(listener => listener());
}
export function notifiedReadError(error: unknown) { return error instanceof Error && notifiedErrors.has(error); }

// Track the logical selection, so polling or a new derived version cannot
// restart the grace period. Callbacks are always scoped to that selection.
export function useReadNotification(options: ReadNotice) {
  const id = useRef(Symbol());
  const latest = useRef(options); latest.current = options;
  const { key, waiting = false, error, title, message, showWaiting = false, actionLabel = '重试', opensDialog } = options;
  const failure = error instanceof Error ? error.message : error || '';
  const [delay, setDelay] = useState<{ key: unknown; elapsed: boolean }>({ key, elapsed: false });
  if (error instanceof Error) notifiedErrors.add(error);
  useEffect(() => {
    setDelay({ key, elapsed: false });
    if (!waiting || failure) return;
    const timer = window.setTimeout(() => setDelay({ key, elapsed: true }), backgroundRefreshDelay);
    return () => window.clearTimeout(timer);
  }, [key, waiting, !!failure]);
  const phase: Phase | null = failure ? 'failed' : waiting && delay.key === key && delay.elapsed ? 'delayed' : waiting && showWaiting ? 'waiting' : null;
  useEffect(() => {
    if (!phase) { update(id.current); return; }
    update(id.current, { id: id.current, phase, title, message: failure || message || '资料更新较慢，后台会继续准备。', actionLabel, opensDialog,
      action: () => { if (Object.is(latest.current.key, key)) latest.current.action?.(); } });
    return () => update(id.current);
  }, [key, phase, title, failure, message, actionLabel, opensDialog]);
}

export function useBackgroundRefresh(refreshing: boolean, key: unknown) {
  useReadNotification({ key, waiting: refreshing, title: '页面资料', message: '资料更新较慢，当前保留上次已保存的数据。', action: retryReads, actionLabel: '重试更新' });
}

function groupRows(items: Notice[]) {
  const rows = new Map<string, { notice: Notice; actions: (() => void)[]; count: number }>();
  for (const notice of items) {
    const key = JSON.stringify([notice.phase, notice.title, notice.message, notice.actionLabel]);
    const row = rows.get(key);
    if (row) { row.count++; if (notice.action) row.actions.push(notice.action); }
    else rows.set(key, { notice, actions: notice.action ? [notice.action] : [], count: 1 });
  }
  return [...rows.values()];
}

export function NotificationButton({ hostId }: { hostId?: string } = {}) {
  const [host, setHost] = useState<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (!hostId) { setHost(null); return; }
    const findHost = () => {
      const target = document.getElementById(hostId);
      if (!target) return false;
      setHost(target); return true;
    };
    if (findHost()) return;
    const observer = new MutationObserver(() => { if (findHost()) observer.disconnect(); });
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [hostId]);
  const items = useSyncExternalStore(subscribe, snapshot);
  const rows = groupRows(items);
  const count = rows.filter(row => row.notice.phase !== 'waiting').length;
  const [open, setOpen] = useState(false);
  const [present, setPresent] = useState(false);
  const [position, setPosition] = useState({ top: 60, left: 12 });
  const button = useRef<HTMLButtonElement>(null), panel = useRef<HTMLDivElement>(null);
  const panelId = useId();
  function close(restoreFocus = true) {
    setOpen(false);
    if (restoreFocus) button.current?.focus();
  }
  function toggle() {
    if (open) { close(); return; }
    setPresent(true); setOpen(true);
  }
  // Position and focus in the layout phase, before the first visible frame.
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const rect = button.current?.getBoundingClientRect(); if (!rect) return;
      const width = Math.min(380, window.innerWidth - 24);
      setPosition({ top: Math.max(12, Math.min(rect.bottom + 8, Math.max(12, window.innerHeight - 220))), left: Math.max(12, Math.min(rect.right - width, window.innerWidth - width - 12)) });
    };
    const closeOutside = (event: PointerEvent) => {
      if (!button.current?.contains(event.target as Node) && !panel.current?.contains(event.target as Node)) close();
    };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.preventDefault(); close(); } };
    place(); panel.current?.querySelector<HTMLButtonElement>('button')?.focus();
    window.addEventListener('resize', place); window.addEventListener('scroll', place, true); document.addEventListener('pointerdown', closeOutside); window.addEventListener('keydown', escape);
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true); document.removeEventListener('pointerdown', closeOutside); window.removeEventListener('keydown', escape); };
  }, [open, host]);
  useEffect(() => {
    if (open || !present) return;
    const timer = window.setTimeout(() => setPresent(false), 120);
    return () => window.clearTimeout(timer);
  }, [open, present]);
  const content = <div className="notification-host">
    <button ref={button} type="button" className={'notification-button' + (count ? ' has-notices' : '')} title="消息通知"
      aria-label={count ? `消息通知，${count}条提醒` : '消息通知'} aria-expanded={open} aria-controls={open ? panelId : undefined}
      data-notification-count={count} onClick={toggle}>
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4M12 2V1"/></svg>
      {count > 0 && <span className="notification-count" aria-hidden="true">{count > 99 ? '99+' : count}</span>}
    </button>
    {present && createPortal(<div ref={panel} id={panelId} className="notification-panel" data-state={open ? 'open' : 'closing'} role="dialog" aria-label="消息通知" aria-hidden={!open} inert={!open} style={{ ...position, maxHeight: `calc(100dvh - ${position.top + 12}px)` }}>
      <header><h2>消息通知</h2><button type="button" aria-label="收起消息通知" onClick={() => close()}><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button></header>
      {!rows.length ? <p className="notification-empty">目前没有延迟或失败提醒</p> : <div className="notification-groups">{(['failed', 'delayed', 'waiting'] as Phase[]).map(phase => {
        const group = rows.filter(row => row.notice.phase === phase);
        return group.length ? <section key={phase} aria-label={{ failed: '读取失败', delayed: '更新较慢', waiting: '后台准备' }[phase]}>
          <h3>{{ failed: '读取失败', delayed: '更新较慢', waiting: '后台准备' }[phase]}</h3>
          <ul>{group.map(({ notice, count: total, actions }, index) => <li key={index} data-state={phase}><div><strong>{notice.title}{total > 1 && <small> · {total}项</small>}</strong><p>{notice.message}</p></div>{actions.length > 0 && <button type="button" onClick={() => { if (notice.opensDialog) close(false); new Set(actions).forEach(action => action()); }}>{notice.actionLabel}</button>}</li>)}</ul>
        </section> : null;
      })}</div>}
      <footer>后台会继续准备，恢复后提醒自动移除。</footer>
    </div>, document.body)}
  </div>;
  return host ? createPortal(content, host) : content;
}
