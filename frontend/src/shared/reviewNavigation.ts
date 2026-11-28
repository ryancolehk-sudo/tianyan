export const reviewTabs = [['emotion', '情绪周期'], ['direction', '板块轮动'], ['lhb', '龙虎榜'], ['overview', '行情总览'], ['new-highs', '百日新高'], ['anomaly', '异动监控']] as const;
export type ReviewView = typeof reviewTabs[number][0];
export function isDailyReview(path: string, search: string, hash = location.hash) {
  const q = new URLSearchParams(search);
  return path === '/research/reviews' && q.get('legacy') !== '1' && !q.has('workspace') && !q.has('date') && !q.has('document') && hash !== '#research-records';
}
export function reviewView(search = location.search): ReviewView {
  const value = new URLSearchParams(search).get('view');
  return reviewTabs.some(([key]) => key === value) ? value as ReviewView : 'emotion';
}
export function reviewHref(view: ReviewView, search = location.search) { const query = new URLSearchParams(search); query.set('view', view); return '/research/reviews?' + query; }
