import { useCallback, useEffect, useRef, useState } from 'react';
import { captureReads, freshRead, readLifetime, readStatus, retainReads, subscribeReads } from './readCache';
import { ApiError } from './api';
import { useReadNotification } from './BackgroundRefresh';

// Track the actual shared GET dependencies of a loader, including existing
// composite callers. A background replacement only wakes its own readers.
export function useCachedLoader<T>(load: () => Promise<T>, key: unknown, title = '页面资料') {
  const loader = useRef(load); loader.current = load;
  const [snapshot, setSnapshot] = useState<{ key: unknown; data: T | undefined; busy: boolean; error: Error | null; transientError?: boolean }>({ key, data: undefined, busy: true, error: null });
  const [refresh, setRefresh] = useState(0);
  const refreshed = useRef(0);
  useEffect(() => {
    let active = true, sequence = 0, paths = new Set<string>(), lastAttempt = 0, running = false, failed = false;
    let authorizationBlocked = false;
    let warming = false;
    const interests = new Map<string, () => void>();
    const run = (force = false) => {
      // Authorization failures require an explicit reload or a new context.
      // Cache notifications and session revalidation must not restart them.
      if (!active || authorizationBlocked) return;
      // A failed initial read has no cached snapshot. Keep its dependency and
      // retry it too, rather than making recovery depend on a manual click.
      lastAttempt = Date.now(); running = true;
      const current = ++sequence;
      const observed = captureReads(() => force ? freshRead(loader.current) : loader.current()); paths = observed.keys;
      for (const [path, release] of interests) if (!paths.has(path)) { release(); interests.delete(path); }
      for (const path of paths) if (!interests.has(path)) interests.set(path, retainReads([path]));
      setSnapshot(before => ({ key, data: before.key === key ? before.data : undefined, busy: true, error: before.key === key ? before.error : null,
        transientError: before.key === key && before.transientError === true }));
      observed.result.then(data => {
        const states = [...paths].map(readStatus), failures = states.filter(state => state?.error), failure = failures[0]?.error;
        failed = !!failure;
        if (active && current === sequence) setSnapshot({ key, data, busy: !failure && states.some(state => !!state && (state.state !== 'fresh' || state.restored)), error: failure ? new Error(failure) : null, transientError: !!failure && failures.every(state => state?.transientError === true) });
      }).catch(reason => {
        if (!active || current !== sequence) return;
        authorizationBlocked = reason instanceof ApiError && [401, 403].includes(reason.status);
        failed = !authorizationBlocked;
        warming = reason instanceof ApiError && reason.code === 'read_cache_warming';
        const failures = [...paths].map(readStatus).filter(state => state?.error);
        const transientError = reason instanceof ApiError ? reason.status >= 500 || reason.status === 408 || reason.status === 429 || reason.status === 0 && (reason.code === 'network_unavailable' || reason.code === 'read_timeout')
          : reason instanceof TypeError || failures.length > 0 && failures.every(state => state?.transientError === true);
        setSnapshot(before => ({ key, data: !authorizationBlocked && before.key === key ? before.data : undefined,
          busy: warming, error: warming ? null : reason instanceof Error ? reason : new Error('暂时无法读取，请重试。'), transientError }));
      }).finally(() => { if (current === sequence) running = false; });
    };
    const unsubscribe = subscribeReads(path => { if (paths.has(path)) run(); });
    const force = refresh !== refreshed.current; refreshed.current = refresh; run(force);
    const refreshRelevant = (event: Event) => {
      const detail = (event as CustomEvent<string[] | string>).detail, prefixes = typeof detail === 'string' ? [detail] : detail;
      if (document.visibilityState === 'visible' && paths.size && (!prefixes?.length || [...paths].some(path => prefixes.some(prefix => path === prefix || path.startsWith(prefix + '?') || path.startsWith(prefix + '/'))))) run();
    };
    const revalidate = () => { if (paths.size && !running && document.visibilityState === 'visible') run(true); };
    const denied = () => { authorizationBlocked = true; sequence++; running = false; failed = false; setSnapshot({ key, data: undefined, busy: false, error: new ApiError(403, '读取权限已变更，请重新读取。') }); };
    const timer = window.setInterval(() => {
      if (authorizationBlocked || document.visibilityState !== 'visible' || running) return;
      const retry = (failed || [...paths].some(path => !!readStatus(path)?.error)) && Date.now() - lastAttempt >= (warming ? 1_000 : 30_000);
      if (retry) { failed = false; run(true); }
      else if ([...paths].some(path => readLifetime(path) && readStatus(path)?.checked && Date.now() - readStatus(path)!.checked >= readLifetime(path))) run();
    }, 1_000);
    window.addEventListener('read-cache-invalidated', refreshRelevant); window.addEventListener('read-permission-denied', denied);
    window.addEventListener('read-cache-revalidate', revalidate);
    window.addEventListener('online', revalidate);
    return () => { active = false; sequence++; unsubscribe(); interests.forEach(release => release()); clearInterval(timer); window.removeEventListener('read-cache-invalidated', refreshRelevant); window.removeEventListener('read-permission-denied', denied); window.removeEventListener('read-cache-revalidate', revalidate); window.removeEventListener('online', revalidate); };
  }, [key, refresh]);
  const reload = useCallback(() => setRefresh(value => value + 1), []);
  const setData = useCallback((update: T | undefined | ((before: T | undefined) => T | undefined)) => setSnapshot(before => ({ ...before, data: typeof update === 'function' ? (update as (before: T | undefined) => T | undefined)(before.data) : update })), []);
  const data = snapshot.key === key ? snapshot.data : undefined;
  const error = snapshot.key === key ? snapshot.error : null;
  const refreshing = data != null && snapshot.busy;
  useReadNotification({ key, waiting: refreshing, error, title, message: '资料更新较慢，当前保留上次已保存的数据。', action: reload, actionLabel: '重新读取' });
  return { data, setData, transientError: snapshot.key === key && snapshot.transientError === true, busy: data == null && (snapshot.key !== key || snapshot.busy), refreshing, error, reload };
}
