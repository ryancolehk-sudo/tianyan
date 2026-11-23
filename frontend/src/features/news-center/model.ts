import type { NewsArticleView } from '../../generated/schema';
export type NewsSecurity = { code: string | null; name: string | null; exchange: 'SSE' | 'SZSE' | 'BSE' | null;
  basis: 'source_reported' | 'archived_announcement'; evidence_id: string; source_id: string; source_name: string;
  source_url: string; received_at: string; content_hash: string; source_fields: string[] };
export type NewsArticle = NewsArticleView & { summary_input_available?: boolean; summary_input_reason?: string | null;
  securities?: NewsSecurity[]; security_status?: 'source_provided' | 'missing' | 'unverified' };
export type { EditionSummary, NewsSourceView as NewsSource,
  NewsAiStatus as AiStatus, AiEditionView as NewsAiVersion, NewsFeedView as FeedData,
  EditionList, EditionDetail, NewsStateUpdate as ArticleState } from '../../generated/schema';
export type NewsPeriod = 'day' | 'week' | 'month';
export type NewsEnvelope<T> = { data: T; meta: { app_mode?: string; generated_at?: string; [key: string]: unknown }; request_id?: string };

export const periods: [NewsPeriod, string][] = [['day', '日报'], ['week', '周报'], ['month', '月报']];
export const categories = ['精选', '全部', '政策', '产业', '公司公告', '海外', '风险澄清'] as const;
export const sectionOrder = ['政策', '产业', '公司公告', '海外', '风险澄清', '财经'];
export const periodName = (period: NewsPeriod) => periods.find(([key]) => key === period)![1];
export const paperName = (period: NewsPeriod) => '天衍' + periodName(period);
export const chinaDay = (value: string) => new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date(value));
export function validDay(value: string | null): string | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const parsed = new Date(value + 'T00:00:00Z');
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value ? value : null;
}
export function chinaTime(value: string | null | undefined, full = false): string {
  if (!value) return '时间未知';
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', ...(full ? { year: 'numeric', month: '2-digit', day: '2-digit' } : {}), hour: '2-digit', minute: '2-digit', hour12: false }).format(parsed);
}
export const articleTime = (article: NewsArticle, full = false) => article.published_at
  ? chinaTime(article.published_at, full) : article.published_day ? article.published_day + ' · 仅日期' : '发布时间未知';
export function articleTimeColumn(article: NewsArticle, referenceDay = chinaDay(new Date().toISOString())) {
  const publishedDay = article.published_at ? chinaDay(article.published_at) : article.published_day;
  if (!publishedDay) return { label: '未知', year: null };
  return {
    label: article.published_at && publishedDay === referenceDay ? chinaTime(article.published_at) : publishedDay.slice(5).replace('-', '.'),
    year: publishedDay.slice(0, 4) === referenceDay.slice(0, 4) ? null : publishedDay.slice(0, 4),
  };
}
export function safeOriginal(value: string) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : undefined; }
  catch { return undefined; }
}
export function newsHref(values: Record<string, string | boolean | null | undefined>) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== null && value !== undefined && value !== false && value !== '') query.set(key, String(value));
  return '/news' + (query.size ? '?' + query : '');
}
export function mergeState(article: NewsArticle, next: NewsArticle): NewsArticle {
  // Saved/following belong to the article; read status belongs to its evidence version.
  return { ...article, is_read: article.evidence_id === next.evidence_id ? next.is_read : article.is_read,
    is_saved: next.is_saved, is_following: next.is_following };
}

export function newsSummaryAvailable(article: NewsArticle) {
  if (typeof article.summary_input_available === 'boolean') return article.summary_input_available;
  const summary = article.summary?.trim() ?? '';
  return Array.from(summary).length >= 20 && summary !== article.title.trim();
}
