import { request as sharedRequest } from '../../shared/api';
import type { IndustryHeadline, NewsOverview, StreamHeadline, TopicPicks, TopicStream } from '../../generated/schema';
import type { ArticleState, EditionDetail, EditionList, FeedData, NewsArticle, NewsEnvelope, NewsPeriod } from './model';

function request<T>(path: string, init?: RequestInit): Promise<NewsEnvelope<T>> {
  return sharedRequest<NewsEnvelope<T>>('/api/v1/news' + path, init);
}
export type IndustryBoard = IndustryHeadline;

export const newsApi = {
  overview: () => request<NewsOverview>('/overview'),
  topicPicks: (day: string | null) => request<TopicPicks>('/topic-picks' + (day ? '?' + new URLSearchParams({day}) : '')),
  topicStream: (cursor?: string, importance?: StreamHeadline['importance'], event?: StreamHeadline['event_type']) => {
    const query = new URLSearchParams({ layout: 'hot' });
    if (cursor) query.set('cursor', cursor);
    if (importance) query.set('importance', importance);
    if (event) query.set('event_type', event);
    return request<TopicStream>('/topic-stream' + (query.size ? '?' + query : ''));
  },
  items: (query: URLSearchParams) => request<FeedData>('/items?' + query),
  item: (id: string) => request<NewsArticle>('/items/' + encodeURIComponent(id)),
  state: (id: string, body: ArticleState, csrf: string) => request<NewsArticle>('/items/' + encodeURIComponent(id) + '/state', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify(body),
  }),
  editions: (period: NewsPeriod, cursor?: string | null) => request<EditionList>('/editions?' + new URLSearchParams({ period, limit: '100', ...(cursor ? { cursor } : {}) })),
  edition: (id: string) => request<EditionDetail>('/editions/' + encodeURIComponent(id)),
};
