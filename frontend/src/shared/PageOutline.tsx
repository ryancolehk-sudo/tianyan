import { useEffect, useRef, useState } from 'react';

const headings = '.ty-panel-head h2, .ty-sector-tracking-head h3, .rw-extension-head h2, .dr-card > header h2, .dir-card-head h2, .nc-section-heading, .dc-section-heading h2, .dc-panel-heading h2, .ss-card h2, .ss-panel-title h2, .tp-panel > h2';
type Entry = { label: string; element: HTMLElement };

/** Local navigation only: never changes filters, URLs, selection or disclosure state. */
export function PageOutline({ context }: { context: string }) {
  const root = useRef<HTMLElement>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  useEffect(() => {
    const main = root.current?.closest('main');
    if (!main) return;
    let frame = 0;
    const scan = () => {
      frame = 0;
      const next = [...main.querySelectorAll<HTMLElement>(headings)]
        .filter(element => element.getClientRects().length > 0 && !element.closest('dialog, [hidden], details:not([open])'))
        .map(element => ({ element, label: (element.textContent || '').replace(/\s+/g, ' ').trim() }))
        .filter(entry => entry.label);
      setEntries(previous => previous.length === next.length && previous.every((entry, i) => entry.element === next[i].element && entry.label === next[i].label) ? previous : next);
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(scan); };
    const observer = new MutationObserver(schedule);
    observer.observe(main, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['open', 'hidden'] });
    window.addEventListener('resize', schedule);
    scan();
    return () => { observer.disconnect(); cancelAnimationFrame(frame); window.removeEventListener('resize', schedule); };
  }, [context]);
  return <nav ref={root} className="page-outline" aria-label="本页功能直达" hidden={entries.length < 2}>
    <span>本页功能</span><div>{entries.map((entry, index) => <button type="button" key={`${index}:${entry.label}`} aria-label={`定位${entry.label}`} onClick={() => {
      if (!entry.element.isConnected) return;
      entry.element.setAttribute('tabindex', '-1');
      entry.element.focus({ preventScroll: true });
      entry.element.scrollIntoView({ block: 'start', behavior: 'instant' });
    }}>{entry.label}</button>)}</div>
    <button type="button" aria-label="返回页面顶部" onClick={() => { root.current?.closest('main')?.focus({ preventScroll: true }); window.scrollTo({ top: 0, behavior: 'instant' }); }}>↑ 顶部</button>
  </nav>;
}
