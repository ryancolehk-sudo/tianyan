import { createContext, useContext } from 'react';
import type { PropsWithChildren } from 'react';
import type { SessionResponse } from '../../generated/schema';

const NewsSessionContext = createContext<SessionResponse | null>(null);
export function NewsSessionProvider({ session, children }: PropsWithChildren<{ session: SessionResponse }>) {
  return <NewsSessionContext.Provider value={session}>{children}</NewsSessionContext.Provider>;
}
export const useNewsSession = () => useContext(NewsSessionContext);
export const useNewsCsrf = () => useNewsSession()?.data.csrf_token ?? '';
