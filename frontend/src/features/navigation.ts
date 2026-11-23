export const routes = [
  ['/workbench', '盘中工作台', 'workbench'], ['/auction', '竞价观察', 'auction'],
  ['/news', '实时资讯', 'news'], ['/research/reviews', '复盘研究', 'research'],
  ['/data', '数据管理', 'data'], ['/system', '系统设置', 'system'],
] as const;
export const researchRoutes = [['/research/reviews', '每日复盘'], ['/research/directions', '板块轮动'], ['/research/plans', '明日预案'], ['/research/validations', '次日验证']] as const;
export const newsViews = [['feed', '实时列表'], ['daily', '财经日报'], ['events', '事件分析'], ['topics', '题材专题'], ['following', '我的关注']] as const;
export const newsRoutes = newsViews.map(([view, name]) => [`/news?view=${view}`, name] as const);
