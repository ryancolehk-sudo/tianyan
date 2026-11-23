import { useSyncExternalStore } from 'react';
import { researchRoutes, routes } from '../features/navigation';

type Entry = { id: string; view: string; x: number; y: number };
type Action = 'initial' | 'navigate' | 'query' | 'hash' | 'pop';
type Position = { x: number; y: number };
const stateKey = '__tianyanNavigation';
const paths = new Set<string>(['/', '/research/tomorrow', ...routes.map(([path]) => path), ...researchRoutes.map(([path]) => path)]);
const listeners = new Set<() => void>();
const positions = new Map<string, Position>();
const freshEntry = (): Entry => ({ id: crypto.randomUUID(), view: crypto.randomUUID(), x: 0, y: 0 });
function storedEntry(): Entry | null {
  const value = history.state?.[stateKey];
  return value && typeof value.id === 'string' && typeof value.view === 'string'
    && Number.isFinite(value.x) && Number.isFinite(value.y) ? value : null;
}
function writeEntry(value: Entry) {
  // History contains navigation identifiers and coordinates only, never page data or credentials.
  history.replaceState({ ...history.state, [stateKey]: value }, '');
}
let entry = storedEntry() ?? freshEntry();
writeEntry(entry);
function locationSnapshot(action: Action, sequence: number) {
  return { path: location.pathname === '/' ? '/workbench' : location.pathname,
    search: location.search, hash: location.hash, view: entry.view, entry: entry.id, action, sequence };
}
let snapshot = locationSnapshot('initial', 0);
function emit(action: Action) {
  snapshot = locationSnapshot(action, snapshot.sequence + 1);
  listeners.forEach(listener => listener());
}
function subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; }
export function useNavigation() { return useSyncExternalStore(subscribe, () => snapshot); }

function remember() {
  const position = { x: scrollX, y: scrollY };
  positions.set(entry.id, position);
  if (positions.size > 100) positions.delete(positions.keys().next().value!);
  return position;
}
function move(url: URL, action: 'navigate' | 'query' | 'hash', replace = false) {
  const position = remember();
  entry = { ...entry, ...position }; writeEntry(entry);
  if (url.href === location.href) { emit(action); return; }
  entry = { id: replace ? entry.id : crypto.randomUUID(), view: action === 'navigate' ? crypto.randomUUID() : entry.view,
    ...(action === 'navigate' ? { x: 0, y: 0 } : position) };
  history[replace ? 'replaceState' : 'pushState']({ [stateKey]: entry }, '', url);
  emit(action);
}

// Query controls keep their current page instance; ordinary links start a new page instance.
// This preserves paragraph/search controls while initializing document/version links from their URL.
export function updateLocation(target: URL, options: { replace?: boolean } = {}) {
  if (target.origin !== location.origin || target.pathname !== location.pathname) throw new Error('页内查询只能更新当前页面。');
  move(target, 'query', options.replace);
}
export function navigateTo(path: string, replace = false) {
  const target = new URL(path, location.origin);
  if (target.origin !== location.origin || !paths.has(target.pathname)) throw new Error('导航地址不可用。');
  move(target, 'navigate', replace);
}

export function installNavigation() {
  const previousRestoration = history.scrollRestoration;
  history.scrollRestoration = 'manual';
  const click = (event: MouseEvent) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target instanceof Element ? event.target.closest('a[href]') : null;
    if (!(link instanceof HTMLAnchorElement) || link.hasAttribute('download')
      || link.target && link.target.toLowerCase() !== '_self' || link.relList.contains('external')) return;
    const url = new URL(link.href);
    if (url.origin !== location.origin || !paths.has(url.pathname)) return;
    event.preventDefault();
    move(url, url.pathname === location.pathname && url.search === location.search && !!url.hash ? 'hash' : 'navigate');
  };
  const pop = () => {
    const incoming = storedEntry();
    // Native fragment navigation also emits popstate, with copied or empty
    // state. It is a new hash destination, not a saved-position traversal.
    const nativeHash = snapshot.path === (location.pathname === '/' ? '/workbench' : location.pathname)
      && snapshot.search === location.search && snapshot.hash !== location.hash
      && (!incoming || incoming.id === entry.id);
    remember();
    entry = nativeHash ? { ...entry, id: crypto.randomUUID() } : incoming ?? freshEntry();
    writeEntry(entry); emit(nativeHash ? 'hash' : 'pop');
  };
  const hash = () => {
    // Covers hash changes made by the browser or assistive technology outside our link handler.
    if (snapshot.hash === location.hash && snapshot.search === location.search) return;
    entry = { ...entry, id: crypto.randomUUID() }; writeEntry(entry); emit('hash');
  };
  const save = () => { entry = { ...entry, ...remember() }; writeEntry(entry); };
  document.addEventListener('click', click);
  window.addEventListener('popstate', pop);
  window.addEventListener('hashchange', hash);
  window.addEventListener('scroll', remember, { passive: true });
  window.addEventListener('pagehide', save);
  return () => {
    document.removeEventListener('click', click); window.removeEventListener('popstate', pop);
    window.removeEventListener('hashchange', hash); window.removeEventListener('scroll', remember);
    window.removeEventListener('pagehide', save); history.scrollRestoration = previousRestoration;
  };
}

export function restoreNavigation(value: ReturnType<typeof useNavigation>) {
  if (value.action === 'query' || value.action === 'initial') return;
  const main = document.getElementById('main-content');
  if (!main) return;
  if (value.action !== 'pop' && value.hash) {
    let id: string;
    try { id = decodeURIComponent(value.hash.slice(1)); } catch { id = value.hash.slice(1); }
    const target = document.getElementById(id);
    if (target) { target.focus({ preventScroll: true }); target.scrollIntoView({ block: 'start' }); return; }
  }
  main.focus({ preventScroll: true });
  const position = value.action === 'pop' ? positions.get(value.entry) ?? entry : { x: 0, y: 0 };
  const restore = () => window.scrollTo({ left: position.x, top: position.y, behavior: 'instant' });
  restore();
  if (value.action !== 'pop') return;
  // A returned page can still be loading records below its first paint. Its document is then
  // too short, the browser clamps the scroll, and a fixed short retry window would expire
  // before those records arrive. Keep the saved position while the document grows, and stop
  // as soon as the reader scrolls or interacts (or after a bounded safety window).
  const stop = () => {
    observer.disconnect(); clearTimeout(timer);
    for (const event of ['wheel', 'touchstart', 'pointerdown', 'keydown']) window.removeEventListener(event, stop);
  };
  const observer = new ResizeObserver(() => { if (Math.abs(scrollY - position.y) > 1) restore(); });
  const timer = window.setTimeout(stop, 10_000);
  observer.observe(main);
  observer.observe(document.body);
  for (const event of ['wheel', 'touchstart', 'pointerdown', 'keydown']) window.addEventListener(event, stop, { once: true });
  return stop;
}
