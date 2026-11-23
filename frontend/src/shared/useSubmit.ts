import { useRef, useState } from 'react';
import { pendingAttempt, retainAttempt } from './privateDrafts';

export function useSubmit(owner: string, slot: string) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const attempt = useRef<{ body: string; key: string } | null>(pendingAttempt(owner, slot));
  async function submit<T>(body: unknown, send: (key: string) => Promise<T>): Promise<T | undefined> {
    const serialized = JSON.stringify(body);
    if (attempt.current?.body !== serialized) attempt.current = { body: serialized, key: crypto.randomUUID() };
    retainAttempt(owner, slot, attempt.current);
    const sentKey = attempt.current.key;
    setBusy(true); setError(null);
    try { const result = await send(sentKey); if (pendingAttempt(owner, slot)?.key === sentKey) retainAttempt(owner, slot, null); if (attempt.current?.key === sentKey) attempt.current = null; return result; }
    catch (reason) { setError(reason instanceof Error ? reason : new Error('暂时无法保存，请重试。')); }
    finally { setBusy(false); }
  }
  return { busy, error, submit, clearError: () => setError(null) };
}
