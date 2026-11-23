import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import type { PropsWithChildren, ReactNode } from 'react';
import { api } from '../../shared/api';
import type { WatchlistResponse } from '../../shared/api';
import type { SessionResponse } from '../../generated/schema';
import { updateLocation, useNavigation } from '../../shared/navigation';

type Selection = {
  instrumentId?: string; day: string; mode: 'live' | 'history';
  selectInstrument: (id: string) => void;
  selectStock: (code: string, exchange?: string) => Promise<void>;
  setDay: (day: string) => void;
};
type WatchState = {
  items: WatchlistResponse['data']['items']; busy: boolean; saving: boolean; error: string;
  add: (id: string) => Promise<void>; remove: (id: string) => Promise<void>;
  reorder: (ids: string[]) => Promise<void>; reload: () => void;
};
const SelectionContext = createContext<Selection | null>(null);
const WatchContext = createContext<WatchState | null>(null);
const validDay = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(Date.parse(value));
function setQuery(key: string, value: string) {
  const target = new URL(location.href);
  if (value) target.searchParams.set(key, value); else target.searchParams.delete(key);
  updateLocation(target);
}

function useSelection(): Selection {
  const navigation = useNavigation();
  const query = new URLSearchParams(navigation.search);
  const day = query.get('trade_day') ?? '';
  const instrumentId = query.get('instrument') ?? undefined;
  const epoch = useRef(0);
  useEffect(() => { epoch.current++; }, [instrumentId]);
  const selectInstrument = useCallback((id: string) => { epoch.current++; setQuery('instrument', id); }, []);
  const selectStock = useCallback(async (code: string, exchange?: string) => {
    const match = /^(?:(sh|sz|bj))?(\d{6})(?:\.(SH|SZ|BJ))?$/i.exec(code.trim());
    if (!match) throw new Error('该记录没有可识别的股票代码。');
    const symbol = match[2];
    const venue = (match[1] ?? match[3] ?? exchange ?? '').toUpperCase();
    const exchangeName = ({ SH: 'SSE', SZ: 'SZSE', BJ: 'BSE' } as Record<string, string>)[venue] ?? venue;
    const ticket = ++epoch.current;
    const result = await api.instruments(symbol);
    if (ticket !== epoch.current) return;
    const matches = result.data.items.filter(item => item.symbol === symbol && (item.asset_type ?? 'stock') === 'stock'
      && (!exchangeName || item.exchange === exchangeName));
    if (matches.length !== 1) throw new Error(matches.length ? '该代码有多个市场，请选择完整市场代码。' : '本机证券目录暂未收录该股票。');
    setQuery('instrument', matches[0].id);
  }, []);
  return { instrumentId, day: validDay(day) ? day : '', mode: validDay(day) ? 'history' : 'live', selectInstrument, selectStock,
    setDay: useCallback((value: string) => setQuery('trade_day', validDay(value) ? value : ''), []) };
}

export function useTradingContext() {
  const context = useContext(SelectionContext);
  const fallback = useSelection();
  return context ?? fallback;
}
export function useWatchlist() { return useContext(WatchContext); }

export function TradingProvider({ session, children }: PropsWithChildren<{ session: SessionResponse }>) {
  const selection = useSelection();
  const [items, setItems] = useState<WatchState['items']>([]);
  const [busy, setBusy] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const pending = useRef(false);
  const alive = useRef(true);
  const currentItems = useRef(items); currentItems.current = items;
  const account = session.data.user.id;
  useEffect(() => {
    let active = true; alive.current = true; setBusy(true); setItems([]); setError('');
    api.watchlist().then(value => { if (active) setItems(value.data.items); })
      .catch(reason => { if (active) setError(reason instanceof Error ? reason.message : '自选读取失败。'); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; alive.current = false; };
  }, [account, reload]);
  async function save(ids: string[]) {
    if (pending.current || busy) return;
    pending.current = true; setSaving(true); setError('');
    try {
      const response = await api.saveWatchlist(ids, session.data.csrf_token, crypto.randomUUID());
      if (alive.current) setItems(response.data.items);
    } catch (reason) {
      if (alive.current) setError(reason instanceof Error ? reason.message : '自选保存失败，请重试。');
    } finally { pending.current = false; if (alive.current) setSaving(false); }
  }
  const watch: WatchState = { items, busy, saving, error, reload: () => setReload(value => value + 1),
    add: async id => { const ids = currentItems.current.map(item => item.instrument.id).filter((item): item is string => !!item);
      if (!ids.includes(id)) await save([...ids, id]); },
    remove: id => save(currentItems.current.map(item => item.instrument.id).filter((item): item is string => !!item && item !== id)),
    reorder: ids => save(ids) };
  return <SelectionContext.Provider value={selection}><WatchContext.Provider value={watch}>{children}</WatchContext.Provider></SelectionContext.Provider>;
}

export function StockAction({ code, name, exchange, children, add = false }: {
  code: string; name?: string; exchange?: string; children?: ReactNode; add?: boolean;
}) {
  const trading = useTradingContext();
  const watch = useWatchlist();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const run = async () => {
    setBusy(true); setError('');
    try {
      if (add && watch) {
        const match = /\d{6}/.exec(code);
        if (!match) throw new Error('该记录没有可识别的股票代码。');
        const result = await api.instruments(match[0]);
        const venue = (exchange ?? (/\.(SH|SZ|BJ)$/i.exec(code)?.[1]) ?? (/^(sh|sz|bj)/i.exec(code)?.[1]) ?? '').toUpperCase();
        const resolved = ({ SH: 'SSE', SZ: 'SZSE', BJ: 'BSE' } as Record<string, string>)[venue] ?? venue;
        const stocks = result.data.items.filter(item => item.symbol === match[0] && (item.asset_type ?? 'stock') === 'stock' && (!resolved || resolved === item.exchange));
        if (stocks.length !== 1) throw new Error('无法唯一识别股票市场，请通过搜索添加。');
        await watch.add(stocks[0].id);
      } else await trading.selectStock(code, exchange);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '股票切换失败。'); }
    finally { setBusy(false); }
  };
  return <span className="trading-stock-action"><button type="button" className="trading-stock-link" onClick={() => void run()} disabled={busy || (add && (!watch || watch.saving || watch.busy))}
    aria-label={add ? `加入自选 ${name ?? code}` : `查看 ${name ?? code} 行情`} title={error || undefined}>{children ?? name ?? code}</button>
    {error && <small className="trading-inline-error" role="status">{error}</small>}</span>;
}
