import { Card } from '../../shared/ui';

const modules = [
  { href: '/workbench?view=overview', name: '综合盯盘', available: '市场全景、榜单、板块与个股、条件选股', pending: '来源时点、实际覆盖、选股字段完整度', unavailable: '尚未定义的确定性规则' },
  { href: '/workbench?view=auction', name: '竞价观察', available: '多日封单、竞价承接、排行与个股阶段观察', pending: '各日各阶段已存资料；封单与竞价成交分开', unavailable: '逐笔全记录、完整委托队列' },
  { href: '/workbench?view=ladder', name: '连板观察', available: '跨日梯队、个股详情与已存行情', pending: '当日名单、封板状态与报价时点', unavailable: '缺失历史名单的自动还原' },
  { href: '/workbench?view=funds', name: '资金动向', available: '同花顺/东财独立资金口径、板块强度与成员', pending: '来源范围、资料日、分钟采样与盘后口径', unavailable: '把日频资金还原成分钟轨迹' },
  { href: '/research/reviews', name: '每日复盘', available: '情绪、轮动、龙虎榜、行情总览、百日新高；本人笔记与版本', pending: '复盘日期的日频归档、历史成员与原始证据', unavailable: '缺失资料对应的确定结论' },
  { href: '/research/tomorrow', name: '明日预案', available: '已存研究、规则稿、分阶段 AI 草稿与人工修订', pending: '交易日历、资料截止；AI 需已授权并启用的模型', unavailable: '没有依据的条件验证' },
  { href: '/news', name: '资讯中心', available: '资讯/公告、分类搜索、原文、收藏关注、日报与题材研究', pending: '来源覆盖、主体标识、原生正文与 AI 模型条件', unavailable: '未接入的商业快讯与授权全文' },
  { href: '/data?view=connections', name: '数据管理', available: '来源、AI 设置、运行监控；材料、快照和日历工具', pending: '每项来源的配置、权利、检测、数据范围与费用', unavailable: '标为未实现的来源能力和 AI 业务' },
  { href: '/system', name: '系统设置', available: '使用偏好、账号安全、缓存维护、备份、更新与操作记录', pending: '管理员权限、本机备份与更新进程的实际连接', unavailable: '团队管理、激活与购买' },
] as const;

export function ModuleGuide() {
  return <Card title="模块功能与数据边界" className="data-modules">
    <div className="table-wrap" tabIndex={0} role="region" aria-label="模块功能与数据边界表格"><table><thead><tr>
      <th scope="col">模块入口</th><th scope="col">可用操作</th><th scope="col">资料与条件待核</th><th scope="col">尚未提供</th>
    </tr></thead><tbody>{modules.map(row => <tr key={row.href}><th scope="row"><a href={row.href}>{row.name}</a></th>
      <td>{row.available}</td><td>{row.pending}</td><td>{row.unavailable}</td></tr>)}</tbody></table></div>
    <p className="feature-note">可用操作指已实现的页面功能。数据、来源权利与模型条件以本机实际状态为准；缺项不补零，历史补采不等于当时已知。</p>
  </Card>;
}
