import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import './readable-text.css';

// A portal keeps full labels readable even inside independently scrolling tables.
// Selection buttons can use the same hint without changing their primary action.
export function useTextHint<T extends HTMLElement>(text: string) {
  const anchor = useRef<T>(null), tooltip = useRef<HTMLDivElement>(null);
  const id = useId();
  const [open, setOpen] = useState(false);
  const [present, setPresent] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const pinned = useRef(false), hideTimer = useRef<number | undefined>(undefined);
  const cancelHide = () => window.clearTimeout(hideTimer.current);
  const show = () => { cancelHide(); setPresent(true); setOpen(true); };
  const hide = () => { cancelHide(); pinned.current = false; setOpen(false); };
  const leave = () => {
    if (pinned.current) return;
    cancelHide(); hideTimer.current = window.setTimeout(hide, 100);
  };
  const pin = () => { if (pinned.current) hide(); else { pinned.current = true; show(); } };
  const onKeyDown = (event: KeyboardEvent<T>) => {
    if (event.key === 'Escape' && open) { event.preventDefault(); event.stopPropagation(); hide(); }
  };
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const rect = anchor.current?.getBoundingClientRect(), box = tooltip.current?.getBoundingClientRect();
      if (!rect || !box) return;
      const above = rect.top - box.height - 6;
      setPosition({ left: Math.max(12, Math.min(rect.left, window.innerWidth - box.width - 12)),
        top: Math.max(12, above >= 12 ? above : Math.min(rect.bottom + 6, window.innerHeight - box.height - 12)) });
    };
    const outside = (event: PointerEvent) => {
      if (!anchor.current?.contains(event.target as Node) && !tooltip.current?.contains(event.target as Node)) hide();
    };
    place(); window.addEventListener('resize', place); window.addEventListener('scroll', place, true); document.addEventListener('pointerdown', outside);
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true); document.removeEventListener('pointerdown', outside); };
  }, [open, text]);
  useEffect(() => {
    if (open || !present) return;
    const timer = window.setTimeout(() => setPresent(false), 120);
    return () => window.clearTimeout(timer);
  }, [open, present]);
  useEffect(() => () => window.clearTimeout(hideTimer.current), []);
  return {
    anchor, show, hide, pin,
    describedBy: open ? id : undefined,
    handlers: {
      onPointerEnter: (event: { pointerType: string }) => { if (event.pointerType !== 'touch') show(); },
      onPointerLeave: leave, onFocus: show, onBlur: hide, onKeyDown,
    },
    popup: present ? createPortal(<div ref={tooltip} id={id} className="readable-text-hint" role="tooltip"
      data-state={open ? 'open' : 'closing'} aria-hidden={!open} inert={!open} style={position}
      onPointerEnter={cancelHide} onPointerLeave={leave}>{text}</div>, document.body) : null,
  };
}

export function ReadableText({ text, className = '', description }: { text: string; className?: string; description?: string }) {
  const hint = useTextHint<HTMLSpanElement>(description ? `${text}\n${description}` : text);
  return <><span ref={hint.anchor} className={`readable-text ${className}`} role="button" tabIndex={0}
    aria-label={`查看完整内容：${text}`} aria-describedby={hint.describedBy} {...hint.handlers}
    onClick={event => { event.stopPropagation(); hint.pin(); }}
    onKeyDown={event => {
      hint.handlers.onKeyDown(event);
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); hint.pin(); }
    }}><span className="readable-text-value">{text}</span></span>{hint.popup}</>;
}
