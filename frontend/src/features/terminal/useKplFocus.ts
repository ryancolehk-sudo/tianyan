import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../../shared/api';

export function useKplFocus(codes: string[], options: { user: string; csrf: string; day: string; cutoff: string; enabled: boolean }) {
  const [context] = useState(() => crypto.randomUUID());
  const [element, setElement] = useState<HTMLElement | null>(null);
  const sequence = useRef(0);
  const sentActive = useRef(false);
  const latest = useRef(options); latest.current = options;
  const codesKey = [...new Set(codes)].slice(0, 10).join(',');
  const { user, csrf, day, cutoff, enabled } = options;
  useEffect(() => {
    const today = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date());
    const eligible = !!user && !!csrf && enabled && !!codesKey && !cutoff && day === today;
    let visible = false, stopped = false, denied = false;
    let controller: AbortController | undefined;
    const publish = async () => {
      if (stopped || denied || !csrf || latest.current.user !== user) return;
      const selected = eligible && visible && document.visibilityState === 'visible' ? codesKey.split(',') : [];
      if (!selected.length && !sentActive.current) return;
      // Monotonic sequence fences server delivery order after aborts, scrolls,
      // selection changes and the development strict-mode remount.
      controller?.abort(); controller = new AbortController();
      sentActive.current = !!selected.length;
      try {
        const result = await api.noteKplFocus({ context_id: context, sequence: ++sequence.current,
          trade_day: day || today, codes: selected, ...(cutoff ? { as_of_time: cutoff } : {}) }, csrf, controller.signal);
        if (!result.data.active) denied = true;
      } catch (error) {
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) denied = true;
      }
    };
    if (!eligible) {
      void publish();
      return () => { stopped = true; controller?.abort(); };
    }
    const observer = element ? new IntersectionObserver(entries => {
      visible = entries.some(entry => entry.isIntersecting); void publish();
    }) : null;
    if (element) observer?.observe(element);
    document.addEventListener('visibilitychange', publish);
    const timer = window.setInterval(publish, 10_000);
    void publish();
    return () => {
      stopped = true; controller?.abort(); observer?.disconnect();
      window.clearInterval(timer); document.removeEventListener('visibilitychange', publish);
    };
  }, [context, codesKey, user, csrf, day, cutoff, enabled, element]);
  return setElement;
}
