import { useEffect, useRef, useState } from 'react';
import { api } from '../../shared/api';
import type { SessionResponse } from '../../generated/schema';

export type QuoteFocusOptions = { session: SessionResponse; day: string; cutoff?: string; enabled: boolean };

export function useQuoteFocus(panel: 'selected' | 'sector' | 'ranking', codes: string[], options?: QuoteFocusOptions) {
  const [context] = useState(() => crypto.randomUUID());
  const [element, setElement] = useState<HTMLElement | null>(null);
  const latest = useRef(options); latest.current = options;
  const codesKey = [...new Set(codes)].slice(0, 50).join(',');
  const owner = options?.session.data.user.id;
  const enabled = options?.enabled;
  const day = options?.day;
  const cutoff = options?.cutoff;
  useEffect(() => {
    const today = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date());
    if (!enabled || !owner || cutoff || !day || day !== today || !codesKey) return;
    let visible = panel === 'selected', pending = false, stopped = false;
    const controller = new AbortController();
    const publish = async () => {
      const current = latest.current;
      if (stopped || pending || !visible || document.visibilityState !== 'visible' || !current || current.session.data.user.id !== owner) return;
      pending = true;
      try { await api.noteQuoteFocus({ context_id: context, panel, trade_day: day, codes: codesKey.split(',') }, current.session.data.csrf_token, controller.signal); }
      catch { /* Collection health remains visible in the data centre. */ }
      finally { pending = false; }
    };
    const observer = element ? new IntersectionObserver(entries => {
      visible = entries.some(entry => entry.isIntersecting); void publish();
    }) : null;
    if (element) observer?.observe(element);
    document.addEventListener('visibilitychange', publish);
    const timer = window.setInterval(publish, 10_000);
    void publish();
    return () => { stopped = true; controller.abort(); observer?.disconnect(); window.clearInterval(timer); document.removeEventListener('visibilitychange', publish); };
  }, [context, panel, codesKey, enabled, owner, day, cutoff, element]);
  return setElement;
}
