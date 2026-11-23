import { createContext, useContext, useEffect, useState } from 'react';
import type { PropsWithChildren } from 'react';
import type { SessionResponse } from '../generated/schema';

export type Preferences = { start_page: 'workbench' | 'review' | 'news'; review_lookback: 20 | 60; news_category: '精选' | '全部'; revision: number };
export const defaultPreferences: Preferences = { start_page: 'workbench', review_lookback: 20, news_category: '精选', revision: 1 };
export function normalizePreferences(value: unknown): Preferences {
  const row = value as Partial<Preferences> | undefined;
  return { start_page: row?.start_page === 'review' || row?.start_page === 'news' ? row.start_page : 'workbench', review_lookback: row?.review_lookback === 60 ? 60 : 20,
    news_category: row?.news_category === '全部' ? '全部' : '精选', revision: Number.isInteger(row?.revision) && row!.revision! >= 1 ? row!.revision! : 1 };
}
export const preferencesFromSession = (session: SessionResponse) => normalizePreferences((session.data as typeof session.data & { preferences?: Preferences }).preferences);
export const startPageHref = (preferences: Preferences) => ({ workbench: '/workbench', review: '/research/reviews', news: '/news' })[preferences.start_page];
export const reviewLookback = (query: URLSearchParams, preferences: Preferences) => query.has('lookback') ? query.get('lookback') === '60' ? '60' : '20' : String(preferences.review_lookback);
export function newsCategory(query: URLSearchParams, preferences: Preferences) { const category = query.get('category'); return category === null ? preferences.news_category : category; }
const Context = createContext({ preferences: defaultPreferences, setPreferences: (_value: Preferences) => {} });
export function PreferenceProvider({ session, children }: PropsWithChildren<{ session: SessionResponse }>) {
  const incoming = preferencesFromSession(session);
  const [preferences, setPreferences] = useState(incoming);
  useEffect(() => { setPreferences(current => incoming.revision > current.revision ? incoming : current); }, [incoming.revision, incoming.start_page, incoming.review_lookback, incoming.news_category]);
  return <Context.Provider value={{ preferences, setPreferences }}>{children}</Context.Provider>;
}
export const usePreferences = () => useContext(Context);
