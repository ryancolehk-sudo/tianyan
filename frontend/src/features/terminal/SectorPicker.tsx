import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from '../../shared/icons';
import { useNavigation } from '../../shared/navigation';
import type { Sector } from './types';
import './sectorPicker.css';

type Choice = Pick<Sector, 'code' | 'name'>;
export function SectorPicker({ items, current, onSelect }: { items: Choice[]; current: Choice | null; onSelect: (code: string) => void }) {
  const navigation = useNavigation();
  const id = useId(), trigger = useRef<HTMLButtonElement>(null), input = useRef<HTMLInputElement>(null), popup = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false), [query, setQuery] = useState(''), [active, setActive] = useState(0);
  const [position, setPosition] = useState({ left: 12, top: 12, width: 280, maxHeight: 360 });
  useEffect(() => { setOpen(false); }, [navigation.path, navigation.search, navigation.hash]);
  const choices = useMemo(() => {
    const term = query.trim().toLocaleLowerCase();
    return term ? items.filter(item => `${item.name} ${item.code}`.toLocaleLowerCase().includes(term)) : items;
  }, [items, query]);
  function close(restore = true) { setOpen(false); if (restore) trigger.current?.focus({ preventScroll: true }); }
  function show() {
    setQuery(''); setActive(Math.max(0, items.findIndex(item => item.code === current?.code))); setOpen(true);
  }
  function choose(item: Choice) { close(); if (item.code !== current?.code) onSelect(item.code); }
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const rect = trigger.current?.getBoundingClientRect();
      if (!rect) return;
      const width = Math.min(Math.max(280, rect.width), window.innerWidth - 24);
      const below = window.innerHeight - rect.bottom - 18, above = rect.top - 18;
      const maxHeight = Math.min(360, Math.max(below, above, 120));
      setPosition({ left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)),
        top: below >= Math.min(260, maxHeight) ? rect.bottom + 6 : Math.max(12, rect.top - maxHeight - 6), width, maxHeight });
    };
    place(); input.current?.focus();
    const outside = (event: PointerEvent) => {
      if (!popup.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) close(false);
    };
    const focusOutside = (event: FocusEvent) => {
      if (!popup.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) close(false);
    };
    window.addEventListener('resize', place); window.addEventListener('scroll', place, true);
    document.addEventListener('pointerdown', outside); document.addEventListener('focusin', focusOutside);
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true);
      document.removeEventListener('pointerdown', outside); document.removeEventListener('focusin', focusOutside); };
  }, [open]);
  useEffect(() => { if (active >= choices.length) setActive(Math.max(0, choices.length - 1)); }, [choices.length, active]);
  useEffect(() => {
    if (open) popup.current?.querySelector<HTMLElement>(`[data-option-index="${active}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [active, choices, open]);
  function keys(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return; }
    if (event.key === 'Tab') {
      // The portal is outside the header's DOM order. Resume the page's natural
      // tab sequence from the trigger rather than sending focus to the body.
      event.preventDefault();
      const targets = [...document.querySelectorAll<HTMLElement>('a[href],button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]')]
        .filter(node => !popup.current?.contains(node) && !node.closest('[hidden],[inert]') && node.getClientRects().length);
      const index = targets.indexOf(trigger.current!);
      const next = targets[index + (event.shiftKey ? -1 : 1)];
      close(false); (next ?? trigger.current)?.focus(); return;
    }
    if (event.key === 'Enter') { event.preventDefault(); if (choices[active]) choose(choices[active]); return; }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    setActive(value => !choices.length ? 0 : event.key === 'Home' ? 0 : event.key === 'End' ? choices.length - 1
      : (value + (event.key === 'ArrowDown' ? 1 : -1) + choices.length) % choices.length);
  }
  return <>
    <button ref={trigger} type="button" className="ty-tracking-sector ty-sector-picker" disabled={!items.length}
      aria-label={`追踪板块：${current?.name || (items.length ? '选择板块' : '暂无可选板块')}`} aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? id+'-list' : undefined}
      onClick={() => open ? close() : show()} onKeyDown={event => { if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); show(); } }}>
      <span>{current?.name || (items.length ? '选择板块' : '暂无可选板块')}</span><Icon name="chevron"/>
    </button>
    {open && createPortal(<div ref={popup} className="ty-sector-picker-popup" style={position} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); } }}>
      <div className="ty-sector-picker-search"><Icon name="search"/><input ref={input} role="combobox" aria-label="搜索追踪板块名称或代码" placeholder="搜索名称 / 代码" value={query}
        autoComplete="off" maxLength={80} aria-expanded="true" aria-autocomplete="list" aria-controls={id+'-list'} aria-activedescendant={choices[active] ? id+'-option-'+active : undefined}
        onChange={event => { setQuery(event.target.value); setActive(0); }} onKeyDown={keys}/>
        <button type="button" aria-label="关闭板块选择" onClick={() => close()}><Icon name="close"/></button></div>
      <ul id={id+'-list'} role="listbox" aria-label="可追踪板块">{choices.map((item, index) => <li id={id+'-option-'+index} key={item.code} role="option" aria-selected={item.code === current?.code}
        data-option-index={index} data-active={index === active || undefined} onPointerMove={() => setActive(index)} onPointerDown={event => event.preventDefault()} onClick={() => choose(item)}>
        <span><strong>{item.name}</strong><small>{item.code}</small></span>{item.code === current?.code && <em>已选</em>}
      </li>)}</ul>
      <p role="status">{choices.length ? `${choices.length}个${query.trim() ? '匹配板块' : '板块'}` : '没有匹配的板块'}</p>
    </div>, document.body)}
  </>;
}
