import { newsRoutes, researchRoutes } from '../features/navigation';
import { newsHref } from '../features/news-center/model';
import type { IconName } from './icons';
import { isDailyReview, reviewHref, reviewTabs, reviewView } from './reviewNavigation';
import { terminalHref, terminalTabs, terminalView } from './terminalNavigation';

export const systemPages = [
  ['overview', '系统概览'], ['preferences', '使用偏好'], ['security', '账号安全'],
  ['maintenance', '数据维护'], ['updates', '版本与更新'], ['activity', '操作记录'],
] as const;

export type WorkspacePage = { label: string; href: string; icon?: IconName; current: boolean;
  topics?: { label: string; keywords: string; href: string }[]; match?: string };
type Group = { label: string; pages: WorkspacePage[] };
const page = (label: string, href: string, icon: IconName, current: boolean): WorkspacePage => ({ label, href, icon, current });
const icons: Record<string, IconName> = {
  overview: 'dashboard', auction: 'auction', ladder: 'ladder', funds: 'flow', emotion: 'pulse',
  direction: 'layers', lhb: 'ranking', 'new-highs': 'chart', anomaly: 'pulse',
  preferences: 'sliders', security: 'shield', maintenance: 'data',
  updates: 'refresh', activity: 'list',
};

export function workspacePages(path: string, search: string, hash: string, contextDay?: string): Group[] {
  const q = new URLSearchParams(search);
  const view = q.get('view');
  const legacy = q.get('legacy') === '1';
  const terminal = (path === '/workbench' || path === '/auction') && !legacy;
  const review = isDailyReview(path, search, hash);
  const tomorrow = path === '/research/tomorrow';
  const news = path === '/news' && !legacy;
  const legacyNews = path === '/news' && legacy;
  const legacyResearch = path.startsWith('/research/') && !tomorrow && !review;
  const day = (review || tomorrow) && (q.get('source_trade_day') || q.get('trade_day') || contextDay);
  const reviewSearch = review ? search : day ? new URLSearchParams({ trade_day: day }).toString() : '';
  const workPages = terminalTabs.map(([key, label]) =>
    page(label, terminalHref(key, terminal ? search : ''), icons[key], terminal && terminalView(path, search) === key));
  const reviewPages = reviewTabs.map(([key, label]) =>
    page(label, reviewHref(key, reviewSearch), icons[key], review && reviewView(search) === key));
  reviewPages.push(page('明日预案', tomorrow ? path + search : '/research/tomorrow' + (day ? '?' + new URLSearchParams({ source_trade_day: day }) : ''), 'research', tomorrow));
  if (legacy && (path === '/workbench' || path === '/auction')) {
    workPages.push(page('盘中工作台（旧版）', '/workbench?legacy=1', 'workbench', path === '/workbench'));
    workPages.push(page('竞价观察（旧版）', '/auction?legacy=1', 'auction', path === '/auction'));
  }
  if (legacyResearch) {
    for (const [href, label] of researchRoutes) {
      const target = href === '/research/reviews' ? href + '?legacy=1' : href;
      reviewPages.push(page(label + '记录', target, 'research', path === href));
    }
  }
  const newsPages = [
    page('资讯首页', '/news', 'news', news && view !== 'daily' && view !== 'topics'),
    page('题材专题', newsHref({ view: 'topics' }), 'layers', news && view === 'topics'),
    ...([['day', '日报'], ['week', '周报'], ['month', '月报']] as const).map(([period, label]) =>
      page(label, newsHref({ view: 'daily', period }), 'calendar',
        news && view === 'daily' && (q.get('period') || 'day') === period)),
  ];
  if (legacyNews) {
    for (const [href, label] of newsRoutes) {
      newsPages.push(page(label + '（旧版）', href + '&legacy=1', 'news', new URLSearchParams(href.split('?')[1]).get('view') === (view || 'feed')));
    }
  }
  function sectionHref(route: string, key: string) {
    const params = new URLSearchParams(path === route ? search : '');
    params.set('view', key);
    if (route === '/system') params.delete('dialog');
    return route + '?' + params;
  }
  const dataView = view === 'data-sources' ? 'connections' : view || 'connections';
  const dataLinks = [
    ['connections', '数据来源', 'data'],
    ['ai', 'AI 设置', 'spark'],
    ['monitor', '运行监控', 'pulse'],
    ['sources', '数据工具', 'layers'],
  ].map(([key, label, icon]) => page(label, sectionHref('/data', key), icon as IconName,
    path === '/data' && (key === 'sources' ? !['connections', 'ai', 'monitor'].includes(dataView) : dataView === key)));
  const systemView = systemPages.find(([key]) => key === view)?.[0] || 'overview';
  return [
    { label: '工作台', pages: workPages }, { label: '每日复盘', pages: reviewPages },
    { label: '资讯中心', pages: newsPages }, { label: '数据管理', pages: dataLinks },
    { label: '系统设置', pages: systemPages.map(([key, label]) => page(label, sectionHref('/system', key), icons[key], path === '/system' && key === systemView)) },
  ];
}
