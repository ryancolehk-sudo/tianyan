import { dataHref, dataPages } from '../../shared/dataNavigation';
import { reviewHref, reviewTabs } from '../../shared/reviewNavigation';
import { terminalHref, terminalTabs } from '../../shared/terminalNavigation';
import { systemPages } from '../../shared/workspacePages';
import type { FeatureId } from './api';

export type DataNeed =
  | { name: string; kind: 'ports' | 'families'; ids: string[] }
  | { name: string; kind: 'news' | 'anomaly' | 'ifind' | 'unsupported' }
  | { name: string; kind: 'model'; binding: FeatureId };

const ports = (name: string, ...ids: string[]): DataNeed => ({ name, kind: 'ports', ids });
const families = (name: string, ...ids: string[]): DataNeed => ({ name, kind: 'families', ids });
const model = (name: string, binding: FeatureId): DataNeed => ({ name, kind: 'model', binding });

// Each entry is one data requirement. Families within an entry are alternatives.
// Distinct fields, frequencies, exchanges and vendor-specific series stay separate.
export const needs = {
  directory: families('证券代码与简称', 'tushare_stock_basic', 'cninfo'),
  stocks: ports('个股报价', 'stock_quote'),
  indices: ports('指数行情', 'index_quote'),
  daily: ports('股票日K', 'stock_daily'),
  minute: ports('个股分时', 'stock_minute_1m'),
  depth: ports('五档盘口', 'depth5'),
  minuteBatch: ports('全市场分钟补采', 'full_minute'),
  breadth: families('市场涨跌家数', 'direction_kpl_emotion', 'ashare_breadth', 'xgb_breadth_history'),
  market: families('批量股票行情截面', 'tencent_market_rt', 'tushare_market_rt'),
  strength: families('题材强度与龙头', 'direction_kpl_strength'),
  thsHot: families('同花顺人气榜', 'direction_ths_hot'),
  emHot: families('东方财富人气榜', 'direction_em_hot'),
  tdxHot: families('通达信人气榜', 'direction_tdx_hot'),
  auctionSeal: families('竞价封单快照', 'fuyao_auction', 'duanxianxia_auction', 'tencent_auction'),
  sealHistory: families('历史封单记录', 'duanxianxia_auction_history', 'fuyao_auction'),
  match: families('竞价撮合成交', 'tushare_stk_auction'),
  orders: { name: '逐笔委托、撤单与完整队列', kind: 'unsupported' },
  limits: families('每日涨跌停价格', 'tushare_market_limits'),
  pools: families('涨跌停与连板名单', 'tushare_limit_list_d'),
  reviewDaily: families('盘后股票日行情', 'tushare_daily'),
  reviewIndices: families('盘后指数日行情', 'tushare_index_daily'),
  dailyBasic: families('换手率与市值', 'tushare_daily_basic'),
  emotion: families('市场情绪历史', 'direction_kpl_emotion'),
  emFunds: families('东方财富板块资金', 'eastmoney_funds', 'tushare_moneyflow_ind_dc'),
  boardFeed: families('东方财富行业与概念板块行情', 'eastmoney_funds'),
  thsIndustry: families('同花顺行业资金', 'ths_industry_funds'),
  thsConcept: families('同花顺概念资金', 'ths_concept_funds'),
  stockFunds: families('个股盘后资金', 'tushare_moneyflow_dc'),
  marketFunds: families('大盘盘后资金', 'tushare_moneyflow_mkt_dc'),
  boards: families('概念板块行情', 'tushare_dc_index'),
  members: families('概念板块历史成分', 'tushare_dc_member'),
  industry: families('行业板块行情', 'tushare_direction_industry'),
  lhb: families('龙虎榜上榜明细', 'tushare_top_list'),
  seats: families('龙虎榜买卖席位', 'tushare_top_inst'),
  highs: families('百日新高名单与历史', 'review_kpl_new_highs'),
  anomaly: { name: '盘后异动名单与监管记录', kind: 'anomaly' },
  shCalendar: families('沪市交易日历', 'tushare_calendar_sse', 'baostock_daily'),
  szCalendar: families('深市交易日历', 'tushare_calendar_szse', 'szse_public', 'baostock_daily'),
  headlines: { name: '财经新闻与快讯', kind: 'news' },
  flashes: families('滚动快讯', 'cls_flash', 'ths_flash', 'eastmoney_flash'),
  pickFeeds: families('财经精选稿源', 'cls_flash', 'ths_flash', 'eastmoney_flash', 'cninfo_announcements', 'hk_gov_finance', 'rthk_finance', 'chinanews_finance'),
  filings: families('上市公司公告', 'cninfo_announcements'),
  finance: ports('上市公司财务报表', 'financial'),
  adjustment: ports('除权除息事件', 'adjustment'),
  ifind: { name: 'iFinD 手动查询连接', kind: 'ifind' },
  themeAI: model('题材归纳模型', 'theme_summary'),
  newsAI: model('新闻摘要模型', 'news_summary'),
  planAI: model('预案生成模型', 'plan_generation'),
  emotionAI: model('情绪研判模型', 'emotion_analysis'),
  directionAI: model('方向与核心分析模型', 'direction_analysis'),
  editionAI: model('报刊 AI 增强', 'period_digest'),
} satisfies Record<string, DataNeed>;

export type NeedId = keyof typeof needs;
export const featureModules = ['工作台', '每日复盘', '资讯中心', '数据管理', '系统设置'] as const;
export type FeatureModule = typeof featureModules[number];
export type SystemFeature = { id: string; module: FeatureModule; name: string; href: string; needs: NeedId[]; detail?: string };
const feature = (id: string, module: FeatureModule, name: string, href: string, required: NeedId[], detail?: string): SystemFeature =>
  ({ id, module, name, href, needs: required, detail });
const workFeature = (id: string, view: typeof terminalTabs[number][0], name: string, required: NeedId[], detail?: string) =>
  feature(id, '工作台', name, terminalHref(view, ''), required, detail);
const reviewFeature = (id: string, view: typeof reviewTabs[number][0], name: string, required: NeedId[], detail?: string) =>
  feature(id, '每日复盘', name, reviewHref(view, ''), required, detail);
const newsFeature = (id: string, name: string, view: string, required: NeedId[], detail?: string) =>
  feature(id, '资讯中心', name, '/news' + view, required, detail);

const workNeeds: Record<typeof terminalTabs[number][0], NeedId[]> = {
  overview: ['indices', 'market', 'breadth'], auction: ['auctionSeal', 'limits', 'match'],
  ladder: ['pools', 'reviewDaily', 'limits'], funds: ['thsIndustry', 'thsConcept', 'emFunds', 'stockFunds'],
};
const reviewNeeds: Record<typeof reviewTabs[number][0], NeedId[]> = {
  emotion: ['emotion', 'pools', 'limits'], direction: ['boards', 'industry', 'members'],
  lhb: ['lhb', 'seats'], overview: ['reviewDaily', 'reviewIndices', 'marketFunds', 'breadth'],
  'new-highs': ['highs', 'dailyBasic', 'stockFunds'], anomaly: ['anomaly'],
};
const dataNeeds: Partial<Record<string, NeedId[]>> = { calendar: ['shCalendar', 'szCalendar'], ifind: ['ifind'] };
const systemDetails = {
  overview: '本机服务与运行状态', preferences: '当前账号的界面偏好', security: '账号、会话与安全设置',
  maintenance: '本机缓存、备份与恢复', updates: '本机版本与更新记录', activity: '本机审计记录',
};
const tomorrow = '/research/tomorrow';
export const features: SystemFeature[] = [
  ...terminalTabs.map(([id, name]) => workFeature('work-' + id, id, name, workNeeds[id])),
  workFeature('stock-detail', 'overview', '个股行情与图表', ['stocks', 'daily', 'minute', 'depth']),
  workFeature('screening', 'overview', '条件选股', ['market'], '具体条件还需当次截面的对应字段。'),
  workFeature('auction-screen', 'auction', '竞价筛选与历史封单', ['auctionSeal', 'sealHistory', 'match']),
  workFeature('auction-orders', 'auction', '竞价逐笔与撤单', ['orders'], '当前竞价页提供封单快照与撮合观察。'),
  workFeature('sector-strength', 'overview', '板块强度与龙头跟踪', ['strength', 'stocks']),
  workFeature('watchlist', 'overview', '自选股管理', [], '按账号保存自选名单与排序。'),
  workFeature('search', 'overview', '标的搜索', ['directory']),
  workFeature('ai-theme', 'overview', 'AI 题材归纳', ['themeAI', 'market', 'headlines']),
  ...reviewTabs.map(([id, name]) => reviewFeature('review-' + id, id, name, reviewNeeds[id])),
  reviewFeature('popularity', 'direction', '个股人气榜', ['thsHot', 'emHot', 'tdxHot']),
  feature('tomorrow', '每日复盘', '明日预案', tomorrow, ['shCalendar', 'szCalendar', 'reviewDaily', 'pools', 'headlines', 'filings'], '具体预案按所选交易日和资料截止时间核对。'),
  feature('tomorrow-ai', '每日复盘', 'AI 预案草稿', tomorrow, ['planAI'], '生成时还需本次研究材料及外发授权。'),
  feature('research-records', '每日复盘', '复盘笔记与版本', '/research/reviews?legacy=1', [], '保存、修订与回看本人研究记录。'),
  feature('direction-records', '每日复盘', '方向研究记录', '/research/directions', [], '按账号保存板块研究。'),
  feature('plan-records', '每日复盘', '预案记录', '/research/plans', [], '按账号保存人工预案。'),
  feature('validation', '每日复盘', '次日验证记录', '/research/validations', [], '记录次日观察与核对结果。'),
  reviewFeature('ai-emotion', 'emotion', 'AI 情绪研判', ['emotionAI']),
  reviewFeature('ai-direction', 'direction', 'AI 方向与核心分析', ['directionAI']),
  newsFeature('news-feed', '资讯首页', '', ['headlines', 'filings']),
  newsFeature('topics', '题材专题', '?view=topics', ['flashes'], '按独立来源报道和收录时间查看事件热度。'),
  newsFeature('picks', '财经精选', '?view=topics&group=picks', ['pickFeeds']),
  newsFeature('industry-news', '行业概念资讯', '?view=topics&group=industry', ['boardFeed', 'flashes']),
  newsFeature('concept-news', '题材概念资讯', '?view=topics&group=concept', ['boardFeed', 'strength', 'flashes']),
  newsFeature('editions', '日报、周报与月报', '?view=daily&period=day', ['headlines', 'filings']),
  newsFeature('news-follow', '收藏与关注', '?view=following', [], '按账号保存已读、收藏与关注。'),
  newsFeature('news-summary', '单篇新闻 AI 摘要', '', ['headlines', 'newsAI'], '文章须有可用的摘要材料。'),
  newsFeature('edition-ai', '报刊 AI 增强', '?view=daily&period=day', ['editionAI']),
  ...dataPages.flatMap(page => page.topics.map(topic => feature('data-' + topic.id, '数据管理', topic.label,
    dataHref(topic.id, '', true), dataNeeds[topic.id] ?? [], topic.detail))),
  feature('financial-tables', '数据管理', '财务报表与除权资料', dataHref('datasets'), ['finance', 'adjustment']),
  feature('minute-backfill', '数据管理', '全市场分钟补齐', dataHref('connections'), ['minuteBatch'], '按本地股票目录手动分段补采。'),
  ...systemPages.map(([id, name]) => feature('system-' + id, '系统设置', name, '/system?view=' + id, [], systemDetails[id])),
];
