import type { NewsItem } from '../../generated/schema';
import { api } from '../../shared/api';

export type DigestSource = { family: string; name: string; publisher: string; region: string; category: string; readable: boolean; state: string; count: number; failed: boolean };
export const digestPeriods = [['day', '日报'], ['week', '周报'], ['month', '月报']] as const;
export type DigestPeriod = typeof digestPeriods[number][0];
const chinaDay = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' });
export const dayDate = (day: string) => new Date(day + 'T00:00:00+08:00');
export const formatDay = (day: string, options: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', ...options }).format(dayDate(day));
export const validDay = (value: string | null): value is string => !!value && /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(dayDate(value).getTime()) && chinaDay.format(dayDate(value)) === value;

export function publicationDay(item: NewsItem): string | null {
  if (item.published_at) return chinaDay.format(new Date(item.published_at));
  return validDay(item.payload.published_day ?? null) ? item.payload.published_day! : null;
}

export function periodKey(day: string, period: DigestPeriod): string {
  if (period === 'month') return day.slice(0, 7) + '-01';
  if (period === 'day') return day;
  const local = new Date(day + 'T00:00:00Z');
  local.setUTCDate(local.getUTCDate() - (local.getUTCDay() + 6) % 7);
  return local.toISOString().slice(0, 10);
}

export function periodEnd(start: string, period: DigestPeriod): string {
  if (period === 'day') return start;
  const date = new Date(start + 'T00:00:00Z');
  if (period === 'week') date.setUTCDate(date.getUTCDate() + 6);
  else { date.setUTCMonth(date.getUTCMonth() + 1); date.setUTCDate(0); }
  return date.toISOString().slice(0, 10);
}

export function sectionName(item: NewsItem) {
  if (item.payload.category === '政策') return '宏观与政策';
  if (item.payload.category === '财经' || item.family === 'hk_gov_finance') return '财经要闻';
  if (item.payload.category === '海外' || ['ecb_news', 'fed_public'].includes(item.family)) return '全球市场';
  if (item.payload.category === '行业') return '行业动态';
  return '公司与公告';
}

export const sectionOrder = ['财经要闻', '宏观与政策', '全球市场', '行业动态', '公司与公告'];

export async function loadDigest() {
  const index = (await api.dataSources()).data;
  const sources: DigestSource[] = index.available_adapters.filter(adapter => adapter.kind === 'news').map(adapter => {
    const current = index.items.find(item => item.family === adapter.family);
    const provider = current?.provider;
    return { family: adapter.family, name: adapter.name, publisher: adapter.news?.publisher ?? adapter.name,
      region: adapter.news?.region ?? '全球', category: adapter.news?.category ?? '',
      readable: !!provider && provider.configured && provider.enabled && provider.entitlement_status === 'verified'
        && provider.rights.display === 'allowed' && provider.rights.cache === 'allowed',
      state: current?.collection_state ?? 'not_configured', count: 0, failed: false };
  });
  const enabled = sources.filter(source => source.readable);
  // The backend catalogue is the single source list. Each configured feed gets
  // its own bounded cache window; reading never triggers an upstream request.
  const responses = await Promise.allSettled(enabled.map(source => api.marketNews(source.family)));
  const items: NewsItem[] = [];
  const failed: string[] = [];
  let firstError: unknown;
  responses.forEach((result, index) => {
    const source = enabled[index];
    if (result.status === 'fulfilled') {
      const rows = result.value.data.items.filter(item => item.family === source.family);
      source.count = rows.length; items.push(...rows);
    } else { source.failed = true; failed.push(source.name); firstError ??= result.reason; }
  });
  if (enabled.length && failed.length === enabled.length) throw firstError;
  items.sort((a, b) => (publicationDay(b) ?? '').localeCompare(publicationDay(a) ?? '')
    || (b.published_at ? Date.parse(b.published_at) : 0) - (a.published_at ? Date.parse(a.published_at) : 0)
    || b.received_at.localeCompare(a.received_at) || b.id.localeCompare(a.id));
  const seen = new Set<string>();
  const unique = items.filter(item => {
    if (seen.has(item.payload.source_url)) return false;
    seen.add(item.payload.source_url); return true;
  });
  return { items: unique, failed, sources };
}

export function publisherFor(item: NewsItem, sources: DigestSource[]) {
  return sources.find(source => source.family === item.family)?.publisher ?? item.source_name;
}

export function editionsFor(items: NewsItem[], period: DigestPeriod) {
  const editions = new Map<string, NewsItem[]>();
  for (const item of items) {
    const day = publicationDay(item);
    if (!day) continue;
    const key = periodKey(day, period);
    const rows = editions.get(key) ?? [];
    rows.push(item); editions.set(key, rows);
  }
  return [...editions].sort(([a], [b]) => b.localeCompare(a)).map(([day, rows]) => ({ day, items: rows }));
}

export function headlineFor(items: NewsItem[]) {
  // Prefer a general finance/policy release, then the newest publication. This
  // is a display order, not an inferred importance score or investment view.
  return [...items].sort((a, b) => sectionOrder.indexOf(sectionName(a)) - sectionOrder.indexOf(sectionName(b)))[0];
}
