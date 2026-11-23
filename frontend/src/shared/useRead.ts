import { useCachedLoader } from './useCachedLoader';

export function useRead<T>(load: () => Promise<T>, options: { retainOnTransientFailure?: boolean } = {}) {
  const read = useCachedLoader(load, load);
  // A failed background refresh is reported by the loader's notification.
  // Retention is opt-in for data views; status consumers keep blocking errors.
  // Keep the same-context snapshot only for transient failures. Authoritative
  // 4xx failures retain their blocking error, including private not-found reads.
  const retained = options.retainOnTransientFailure === true && read.data != null && read.transientError;
  return { ...read, data: read.data ?? null, error: retained ? null : read.error,
    refreshError: retained ? read.error : null };
}
