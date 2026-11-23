import { useState } from 'react';
import { clearWorkspaceState } from './workspaceState';

// Only domain form values live here, never credentials or browser storage.
const drafts = new Map<string, { value: unknown; warn: boolean }>();
const attempts = new Map<string, { body: string; key: string }>();
let authorizedOwner: string | null = null;

export function authorizeDraftOwner(owner: string) {
  if (authorizedOwner !== owner) { drafts.clear(); attempts.clear(); }
  authorizedOwner = owner;
}

export function clearPrivateDrafts() {
  clearWorkspaceState();
  drafts.clear(); attempts.clear(); authorizedOwner = null;
}

export function discardPrivateDraft(owner: string, key: string) {
  drafts.delete(owner + ':' + key);
}

export function hasUnsavedDraft(owner: string, key: string) {
  return authorizedOwner === owner && drafts.get(owner + ':' + key)?.warn === true;
}
export function hasUnsavedPrivateDrafts() { return [...drafts.values()].some(draft => draft.warn); }

export function retainCleanPrivateDraft(owner: string, key: string, value: unknown) {
  if (authorizedOwner === owner) drafts.set(owner + ':' + key, { value: structuredClone(value), warn: false });
}

export function pendingAttempt(owner: string, key: string) { return attempts.get(owner + ':' + key) ?? null; }
export function retainAttempt(owner: string, key: string, value: { body: string; key: string } | null) {
  if (value && authorizedOwner === owner) attempts.set(owner + ':' + key, value);
  else attempts.delete(owner + ':' + key);
}

export function usePrivateDraft<T>(owner: string, key: string, initial: () => T, warn = true) {
  const slot = owner + ':' + key;
  const [value, update] = useState<T>(() => drafts.has(slot) ? structuredClone(drafts.get(slot)!.value) as T : initial());
  function set(next: T, dirty = warn) {
    if (authorizedOwner === owner) drafts.set(slot, { value: structuredClone(next), warn: dirty });
    update(next);
  }
  return [value, set] as const;
}

window.addEventListener('beforeunload', event => {
  if ([...drafts.values()].some(draft => draft.warn)) { event.preventDefault(); event.returnValue = ''; }
});
