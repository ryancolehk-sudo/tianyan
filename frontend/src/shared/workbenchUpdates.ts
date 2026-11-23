import { useEffect } from 'react';
import { persistentScope } from './persistentReads';
import { acceptPublications, expireReads } from './readCache';

// One stream for the active terminal instance, shared by its four views.
// Events only invalidate local reads; no vendor requests originate here.
export function useWorkbenchUpdates(owner: string, historical: boolean) {
  useEffect(() => {
    if (historical) return;
    let stopped = false, blocked = false, stream: EventSource | undefined;
    let retry: number | undefined, refresh: number | undefined;
    let failures = 0, lastRevision = '';
    const publications = new Map<string, string>();
    let broad = false;
    const scope = persistentScope();
    const wake = (changed?: { path: string; revision: string }[]) => {
      if (stopped || blocked || scope !== persistentScope() || document.visibilityState !== 'visible') return;
      if (!changed) broad = true;
      else changed.forEach(item => publications.set(item.path, item.revision));
      if (refresh) return;
      refresh = window.setTimeout(async () => {
        refresh = undefined;
        const all = broad; broad = false;
        const pending = [...publications].map(([path, revision]) => ({ path, revision })); publications.clear();
        const announce = (paths: string[]) => {
          if (!stopped && !blocked && scope === persistentScope() && paths.length)
            window.dispatchEvent(new CustomEvent('read-cache-invalidated', { detail: paths }));
        };
        if (all) {
          const paths = ['/api/v1/workbench-view', '/api/v1/workbench-details'];
          paths.forEach(expireReads); announce(paths);
        }
        if (pending.length) {
          const ready = new Set<string>(); let deliveryQueued = false;
          const deliver = (paths: string[]) => {
            paths.forEach(path => ready.add(path));
            if (!ready.size || deliveryQueued) return;
            deliveryQueued = true;
            queueMicrotask(() => { deliveryQueued = false; const paths = [...ready]; ready.clear(); announce(paths); });
          };
          // Each path still waits for its own initial validator and ETag check.
          // An unrelated slow read cannot hold a completed page publication.
          await Promise.all(pending.map(async item => deliver(await acceptPublications([item]))));
        }
      }, 50);
    };
    const connect = () => {
      if (stopped || blocked || scope !== persistentScope() || typeof EventSource === 'undefined') return;
      stream?.close();
      stream = new EventSource('/api/v1/workbench-updates');
      stream.addEventListener('update', event => {
        let value: { scope?: string; revision?: string; publications?: { path: string; revision: string }[] | null };
        try { value = JSON.parse((event as MessageEvent).data); } catch { return; }
        if (value.scope !== scope || !value.revision) return;
        failures = 0;
        if (value.revision !== lastRevision) {
          lastRevision = value.revision;
          const valid = Array.isArray(value.publications) && value.publications.every(item => item && typeof item.path === 'string' && typeof item.revision === 'string');
          wake(valid ? value.publications! : undefined);
        }
      });
      const lost = () => {
        stream?.close();
        if (stopped || blocked) return;
        window.clearTimeout(retry);
        retry = window.setTimeout(connect, [2_000, 5_000, 15_000, 30_000][Math.min(failures++, 3)]);
      };
      stream.addEventListener('unavailable', lost);
      stream.onerror = lost;
      stream.addEventListener('permission-denied', () => {
        blocked = true; stream?.close(); window.clearTimeout(retry);
        window.dispatchEvent(new Event('read-permission-denied'));
      });
    };
    const resume = () => { if (document.visibilityState === 'visible') { wake(); connect(); } };
    const deny = () => { blocked = true; stream?.close(); window.clearTimeout(retry); };
    // Conditional reads cover missed events, unsupported SSE and reconnects.
    const fallback = window.setInterval(() => wake(), 5_000);
    connect();
    window.addEventListener('online', resume); document.addEventListener('visibilitychange', resume);
    window.addEventListener('read-permission-denied', deny);
    return () => {
      stopped = true; stream?.close(); window.clearTimeout(retry); window.clearTimeout(refresh); window.clearInterval(fallback);
      window.removeEventListener('online', resume); document.removeEventListener('visibilitychange', resume);
      window.removeEventListener('read-permission-denied', deny);
    };
  }, [owner, historical]);
}
