import { useEffect, useLayoutEffect, useRef, useState } from 'react';

const prefix = 'ty-workspace-v1:';
export type WorkspaceReadingState = {
  scope:'all'|'sector'|'watch'; sort:'pct_change'|'speed_5m'|'amount_cny';
  detailTab:'走势'|'竞价'|'异动'|'资讯'|'证据'|'AI解读'; pane:'板块'|'列表'|'详情';
  scroll:Record<string,{top:number;left:number}>;
};
export function stabilizeRanking(previous:string[], latest:string[]) {
  const known=new Set(previous), present=new Set(latest);
  return [...previous.filter(code=>present.has(code)),...latest.filter(code=>!known.has(code))];
}
export function clearWorkspaceState() {
  try { Object.keys(sessionStorage).filter(key => key.startsWith(prefix)).forEach(key => sessionStorage.removeItem(key)); } catch { /* Optional storage. */ }
}
function read<T>(key: string, fallback: T): T {
  try { const raw = sessionStorage.getItem(prefix + key); if (raw === null) return fallback; const value=JSON.parse(raw); return fallback===null || value!==null&&typeof value===typeof fallback ? value : fallback; } catch { return fallback; }
}
export function useWorkspaceState<T>(key: string, fallback: T): [T, (value: T) => void] {
  const [entry, setEntry] = useState(() => ({ key, value: read(key, fallback) }));
  const value = entry.key === key ? entry.value : read(key, fallback);
  useEffect(() => { try { if (sessionStorage.getItem(prefix + key) === null) sessionStorage.setItem(prefix + key, JSON.stringify(value)); } catch { /* Optional storage. */ } }, [key]);
  return [value, next => { setEntry({ key, value: next }); try { sessionStorage.setItem(prefix + key, JSON.stringify(next)); } catch { /* Reading remains usable. */ } }];
}
export function usePaneScroll(key: string, selector='[data-scroll-key]') {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const root = ref.current; if (!root) return;
    const saved = read<Record<string, {top: number; left: number}>>(key, {});
    const positions = new Map<HTMLElement, {id:string; top:number; left:number}>();
    let interacting = false;
    const persist = () => { try { sessionStorage.setItem(prefix + key, JSON.stringify(saved)); } catch { /* Optional storage. */ } };
    const collect = () => { positions.forEach((position, el) => { if (root.contains(el) && (!el.dataset.scrollKey || el.dataset.scrollKey === position.id)) { position.top=el.scrollTop;position.left=el.scrollLeft; } saved[position.id]={top:position.top,left:position.left}; }); persist(); };
    const restore = () => {
      Array.from(root.querySelectorAll<HTMLElement>(selector)).forEach((el,index) => {
        const id=el.dataset.scrollKey||el.getAttribute('aria-label')||el.className+':'+index, previous=positions.get(el), changed=previous?.id!==id;
        if (changed && previous) saved[previous.id]={top:previous.top,left:previous.left};
        const value=saved[id];
        if ((changed || !interacting) && value) {el.scrollTop=value.top;el.scrollLeft=value.left;}
        positions.set(el,{id,top:el.scrollTop,left:el.scrollLeft});
      });
    };
    const stop = () => { interacting=true;observer.disconnect(); };
    const observer=new ResizeObserver(restore);observer.observe(root);
    const mutations=new MutationObserver(restore);mutations.observe(root,{subtree:true,childList:true,attributes:true,attributeFilter:['data-scroll-key']});restore();
    root.addEventListener('wheel',stop,{passive:true});root.addEventListener('pointerdown',stop);root.addEventListener('keydown',stop);root.addEventListener('scroll',collect,true);
    const timeout=window.setTimeout(()=>{interacting=true;observer.disconnect();},5000);
    return ()=>{collect();clearTimeout(timeout);observer.disconnect();mutations.disconnect();root.removeEventListener('wheel',stop);root.removeEventListener('pointerdown',stop);root.removeEventListener('keydown',stop);root.removeEventListener('scroll',collect,true);};
  }, [key,selector]);
  return ref;
}

export function useStableRanking(rows: {code:string}[], identity: string, reset=0) {
  const next = rows.map(row => row.code);
  const [saved, setSaved] = useWorkspaceState<{identity:string; codes:string[];reset:number}>('ranking:' + identity, {identity, codes:next, reset});
  const valid=saved?.identity===identity&&saved.reset===reset&&Array.isArray(saved.codes)&&saved.codes.every(code=>typeof code==='string');
  const codes = valid&&saved.codes.length ? saved.codes : next;
  useEffect(() => { if ((!valid||!saved.codes.length) && next.length) setSaved({identity, codes:next, reset}); }, [identity, reset, next.join('|')]);
  const order = stabilizeRanking(codes,next);
  return { order, changed: order.join('|') !== next.join('|'), apply: () => setSaved({identity, codes:next, reset}) };
}
