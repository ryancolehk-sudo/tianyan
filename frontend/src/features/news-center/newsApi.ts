import { request as sharedRequest } from '../../shared/api';
import type { ArticleState, EditionDetail, EditionList, FeedData, NewsArticle, NewsEnvelope, NewsPeriod } from './model';

function request<T>(path: string, init?: RequestInit): Promise<NewsEnvelope<T>> {
  return sharedRequest<NewsEnvelope<T>>('/api/v1/news' + path, init);
}
export const newsApi = {
  items: (query: URLSearchParams) => request<FeedData>('/items?' + query),
  item: (id: string) => request<NewsArticle>('/items/' + encodeURIComponent(id)),
  state: (id: string, body: ArticleState, csrf: string) => request<NewsArticle>('/items/' + encodeURIComponent(id) + '/state', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify(body),
  }),
  editions: (period: NewsPeriod, cursor?: string | null) => request<EditionList>('/editions?' + new URLSearchParams({ period, limit: '100', ...(cursor ? { cursor } : {}) })),
  edition: (id: string) => request<EditionDetail>('/editions/' + encodeURIComponent(id)),
};
