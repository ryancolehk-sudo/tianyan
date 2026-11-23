import { createContext, useContext, useEffect, useState } from 'react';
import type { PropsWithChildren } from 'react';
import { request } from './api';

export type MarketSession = {
  server_time: string; trade_day: string | null;
  phase: 'preopen' | 'auction' | 'trading' | 'break' | 'closed' | 'unknown';
  is_trading_day: boolean | null; automatic_live: boolean;
  current_day_active?: boolean;
  next_transition_at: string | null; poll_seconds: number;
};
type SessionState = { market: MarketSession | null; error: string };
const MarketContext = createContext<SessionState>({ market: null, error: '' });
export const marketPhaseLabel: Record<MarketSession['phase'], string> = {
  preopen: '开盘前', auction: '集合竞价', trading: '交易中', break: '午间休市', closed: '已收盘', unknown: '交易日待核',
};
export function currentDayContext(market: MarketSession | null | undefined) {
  return market?.current_day_active ?? market?.automatic_live ?? false;
}
function valid(value: MarketSession | undefined): value is MarketSession {
  return !!value && value.phase in marketPhaseLabel && (value.trade_day === null || /^\d{4}-\d{2}-\d{2}$/.test(value.trade_day))
    && Number.isFinite(Date.parse(value.server_time)) && typeof value.automatic_live === 'boolean';
}

export function MarketSessionProvider({ children }: PropsWithChildren) {
  const [state, setState] = useState<SessionState>({ market: null, error: '' });
  useEffect(() => {
    let active = true, pending = false, timer: number | undefined, previous: MarketSession | null = null;
    const poll = async () => {
      window.clearTimeout(timer);
      if (!active || pending || document.visibilityState === 'hidden') return;
      pending = true;
      let wait = 15_000;
      try {
        const response = await request<{ data: MarketSession }>('/api/v1/market-session');
        if (!active) return;
        if (!valid(response.data)) throw new Error('交易时段暂未核实');
        const market = response.data;
        const changed = previous && (previous.trade_day !== market.trade_day || previous.phase !== market.phase || previous.automatic_live !== market.automatic_live || previous.current_day_active !== market.current_day_active);
        previous = market; setState({ market, error: '' });
        if (changed) window.dispatchEvent(new Event('read-cache-revalidate'));
        const until = market.next_transition_at ? Date.parse(market.next_transition_at) - Date.parse(market.server_time) : NaN;
        if (Number.isFinite(until) && until > 0) wait = Math.max(500, Math.min(wait, until + 100));
      } catch (reason) {
        if (active) setState(before => ({ ...before, error: reason instanceof Error ? reason.message : '交易时段暂未核实' }));
      } finally {
        pending = false;
        if (active) timer = window.setTimeout(poll, wait);
      }
    };
    const resume = () => { if (document.visibilityState === 'visible') void poll(); };
    void poll();
    window.addEventListener('online', resume); document.addEventListener('visibilitychange', resume);
    return () => { active = false; window.clearTimeout(timer); window.removeEventListener('online', resume); document.removeEventListener('visibilitychange', resume); };
  }, []);
  return <MarketContext.Provider value={state}>{children}</MarketContext.Provider>;
}

export function useMarketSession(fallback?: MarketSession | null) {
  const state = useContext(MarketContext);
  return { ...state, market: state.market ?? fallback ?? null };
}
