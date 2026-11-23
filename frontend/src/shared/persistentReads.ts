// Only authenticated, explicitly allowed read models use this database. Session
// tokens, configuration and editable research material never enter it.
const NAME = 'tianyan-workspace-reads-v1';
const RETENTION = 7 * 24 * 60 * 60_000;
const BUDGET = 128 * 1024 * 1024;
type Stored = { id: string; scope: string; key: string; savedAt: number; touched: number; size: number; value: unknown };
type Index = Omit<Stored, 'value'>;
let scope: string | null = null;
let epoch = 0;
let disabled = false;
let database: Promise<IDBDatabase | null> | undefined;
let writes: Promise<void> = Promise.resolve();

export const persistentScope = () => scope;
export function setPersistentScope(next: string | null) { if (scope !== next) { scope = next; epoch++; } }
const identifier = (owner: string, key: string) => `${owner}\u0000${key}`;
function open() {
  if (disabled) return Promise.resolve(null);
  try { if (typeof indexedDB === 'undefined') return Promise.resolve(null); }
  catch { disabled = true; return Promise.resolve(null); }
  return database ??= new Promise<IDBDatabase | null>(resolve => {
    try {
      const request = indexedDB.open(NAME, 1);
      request.onupgradeneeded = () => { request.result.createObjectStore('records', { keyPath: 'id' }); request.result.createObjectStore('index', { keyPath: 'id' }); };
      request.onsuccess = () => { request.result.onversionchange = () => request.result.close(); resolve(request.result); };
      request.onerror = request.onblocked = () => { disabled = true; resolve(null); };
    } catch { disabled = true; resolve(null); }
  });
}
function result<T>(request: IDBRequest<T>) { return new Promise<T>((resolve, reject) => { request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); }); }
function complete(transaction: IDBTransaction) { return new Promise<void>((resolve, reject) => { transaction.oncomplete = () => resolve(); transaction.onerror = transaction.onabort = () => reject(transaction.error); }); }

export async function persistentGetMany<T>(keys: string[], valid: (value: unknown, key: string) => value is T): Promise<Map<string, T>> {
  const owner = scope, current = epoch;
  if (!owner || !keys.length) return new Map();
  try {
    const db = await open(); if (!db || current !== epoch) return new Map();
    const tx = db.transaction('records', 'readonly');
    const rows = await Promise.all(keys.map(key => result<Stored | undefined>(tx.objectStore('records').get(identifier(owner, key)))));
    if (current !== epoch || scope !== owner) return new Map();
    const values = new Map<string, T>(), touched: Index[] = [], rejected: string[] = [];
    for (let i = 0; i < rows.length; i++) {
      const row = rows[i], key = keys[i]; if (!row) continue;
      if (row.id !== identifier(owner, key) || row.scope !== owner || row.key !== key || !Number.isFinite(row.size) || row.size < 0 || !Number.isFinite(row.savedAt) || row.savedAt < Date.now() - RETENTION || row.savedAt > Date.now() + 60_000 || !valid(row.value, key)) { rejected.push(identifier(owner, key)); continue; }
      values.set(key, row.value); touched.push({ id: row.id, scope: owner, key, savedAt: row.savedAt, touched: Date.now(), size: row.size });
    }
    writes = writes.then(async () => {
      if (current !== epoch || disabled) return;
      const update = db.transaction(['records', 'index'], 'readwrite');
      rejected.forEach(id => { update.objectStore('records').delete(id); update.objectStore('index').delete(id); });
      touched.forEach(row => update.objectStore('index').put(row)); await complete(update);
    }).catch(() => { if (current === epoch) disabled = true; });
    return values;
  } catch { if (current === epoch) disabled = true; return new Map(); }
}
export function persistentPutMany(items: Array<{ key: string; value: unknown }>) {
  const owner = scope, current = epoch;
  if (!owner || disabled || !items.length) return;
  writes = writes.then(async () => {
    const db = await open(); if (!db || current !== epoch || scope !== owner) return;
    const rows: Stored[] = [];
    for (const item of items) { const size = JSON.stringify(item.value).length * 2 + item.key.length * 2 + 256; if (size < BUDGET) rows.push({ ...item, id: identifier(owner, item.key), scope: owner, savedAt: Date.now(), touched: Date.now(), size }); }
    const tx = db.transaction(['records', 'index'], 'readwrite'), index = tx.objectStore('index'), records = tx.objectStore('records');
    const request = index.getAll();
    request.onsuccess = () => {
      try {
        if (current !== epoch || scope !== owner) { tx.abort(); return; }
        const metadata = new Map((request.result as Index[]).map(row => [row.id, row]));
        for (const row of rows) { const { value: _value, ...info } = row; metadata.set(row.id, info); records.put(row); index.put(info); }
        let bytes = [...metadata.values()].reduce((sum, row) => sum + (Number.isFinite(row.size) ? row.size : BUDGET), 0);
        for (const row of [...metadata.values()].sort((a, b) => a.touched - b.touched)) {
          if (row.savedAt >= Date.now() - RETENTION && bytes <= BUDGET) continue;
          records.delete(row.id); index.delete(row.id); bytes -= row.size;
        }
      } catch { tx.abort(); }
    }; await complete(tx);
  }).catch(() => { if (current === epoch) disabled = true; }); // An intentional account-change abort is not a storage failure.
}
export function purgePersistentScope() {
  const owner = scope; epoch++; if (!owner) return Promise.resolve(true);
  let cleared = false;
  writes = writes.then(async () => {
    const db = await open(); if (!db) return;
    const tx = db.transaction(['records', 'index'], 'readwrite'), request = tx.objectStore('index').getAll();
    request.onsuccess = () => { for (const row of request.result as Index[]) if (row.id.startsWith(owner + '\u0000')) { tx.objectStore('records').delete(row.id); tx.objectStore('index').delete(row.id); } };
    // Also remove orphaned records if a damaged metadata row was missing.
    const cursor = tx.objectStore('records').openCursor();
    cursor.onsuccess = () => { const row = cursor.result; if (!row) return; if (String(row.primaryKey).startsWith(owner + '\u0000')) { row.delete(); tx.objectStore('index').delete(row.primaryKey); } row.continue(); };
    await complete(tx); cleared = true;
  }).catch(() => { disabled = true; });
  return writes.then(() => cleared);
}
export async function persistentCacheStats() {
  const owner = scope, current = epoch;
  if (!owner) return { count: 0, bytes: 0, available: false };
  await writes;
  const db = await open();
  if (!db || owner !== scope || current !== epoch) return { count: 0, bytes: 0, available: false };
  try {
    const rows = await result<Index[]>(db.transaction('index', 'readonly').objectStore('index').getAll());
    if (owner !== scope || current !== epoch) return { count: 0, bytes: 0, available: false };
    const items = rows.filter(row => row.scope === owner && row.savedAt >= Date.now() - RETENTION);
    return { count: items.length, bytes: items.reduce((sum, row) => sum + (Number.isFinite(row.size) ? row.size : 0), 0), available: true };
  } catch { return { count: 0, bytes: 0, available: false }; }
}
