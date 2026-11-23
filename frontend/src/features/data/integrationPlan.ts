import { tusharePriority } from './tusharePriority';
// Planning snapshot only. Runtime state comes from providers, never from these candidates.
// Stable IDs link the catalogue to its source families.
export const catalogCheckedOn = '2026-10-25';
export const catalogModules = ['盘中工作台', '竞价观察', '实时资讯', '复盘研究', '数据中心', '系统管理'] as const;
export type CatalogModule = typeof catalogModules[number];
export const catalogStages = ['首批', '后续', '条件评估', '2.0 / 后续'] as const;
export type CatalogStage = typeof catalogStages[number];
export type CatalogEntry = {
  id: string; name: string; purpose: string; modules: readonly CatalogModule[];
  stage: CatalogStage; basis: string; cost: string; boundary: string;
  sourceIds?: readonly string[]; url?: string; license?: string;
  localCapability?: 'evidence.manual' | 'calendar.import';
};
const work = '盘中工作台', auction = '竞价观察', news = '实时资讯', research = '复盘研究', data = '数据中心', system = '系统管理';
const common: readonly CatalogModule[] = [work, auction, news, research, data];
function capability(id: string, name: string, purpose: string, modules: readonly CatalogModule[], stage: CatalogStage, basis: string, cost: string, boundary: string, sourceIds: string[] = [], localCapability?: CatalogEntry['localCapability']): CatalogEntry {
  return { id, name, purpose, modules, stage, basis, cost, boundary, sourceIds, localCapability };
}
const legacyCapabilities: readonly CatalogEntry[] = [
  capability('D01', '标的与代码搜索', '代码、名称、上市退市与别名，形成统一标的目录。', common, '首批', '沪 / 深 / 北交易所 → 巨潮、BaoStock', '公开资料优先，机器使用范围待核实', '市场、品种和代码有效期分别保存；北交所映射另验。', ['tushare_pro', 'sse_public', 'szse_public', 'bse_public', 'cninfo', 'baostock']),
  capability('D02', '交易日历与开闭市', '交易日、交易时段与临时休市，供全系统共用。', common, '首批', '各交易所公告 / 规则 → BaoStock', '官方资料优先，不预购日历库', '人工日历导入及自动来源分别核对；当月日期不包含交易时段，覆盖外不能猜开市。', ['tushare_pro', 'sse_public', 'szse_public', 'bse_public', 'baostock'], 'calendar.import'),
  capability('D03', '四指数与标的报价', '工作台指数、当前标的价格及可得盘口字段。', [work, auction, research], '首批', '已购Tushare合格快照优先 → 免费快照/参考补缺 → 盘后数据', '免费注册与匿名来源，实际权益、覆盖和时效分别核验', '本人Key仅本机配置；时间、延迟、单位逐字段验证。五分钟参考不称实时，不称交易所直连。', ['tushare_pro', 'tickflow_live', 'ashare_public', 'baostock', 'tencent_web', 'sina_web', 'eastmoney_web']),
  capability('D04', '全市场截面与排序', '涨跌幅、成交额和市场列表，避免页面各自全量抓取。', [work, research], '后续', '东方财富 → 新浪批量候选', '免费候选，批量使用范围待核实', '保留分页时间和覆盖；同源的不同工具不算独立备用。', ['eastmoney_web', 'sina_web']),
  capability('D05', '分时与分钟 / 周月图', '当前标的分时、1 / 5 分钟及周月价格图。', [work, auction, research], '后续', '腾讯 / 东方财富 → 新浪', '免费近期窗口候选，长期历史另评估', '分钟量与累计量分开；不能用轮询次数宣称源精度。', ['tencent_web', 'eastmoney_web', 'sina_web']),
  capability('D06', '历史日线与估值', '先补历史日线，再支持趋势图与历史对照。', [work, research], '首批', 'BaoStock → 腾讯 / 新浪；东方财富补充', 'BaoStock发布者声明免费，具体使用范围待核', '最新日期、退市股和北交所覆盖逐项实测；原始价与复权价分开。', ['tushare_pro', 'baostock', 'tencent_web', 'sina_web', 'eastmoney_web']),
  capability('D07', '复权因子与公司行动', '分红送配、股本及复权因子版本。', [work, research], '后续', 'BaoStock / 披露资料 → 巨潮', '免费资料候选，保存权利待核', '不跨源拼接复权口径，不把未来因子当作过去已知。', ['tushare_pro', 'baostock', 'cninfo']),
  capability('D08', '交易状态与制度', 'ST、停复牌、价格限制和生效规则。', [work, auction, research], '后续', '交易所公告 / 规则 → 巨潮', '官方资料优先', '板块、品种、上市阶段分开；规则需要独立规格与验证。', ['sse_public', 'szse_public', 'bse_public', 'cninfo']),
  capability('D09', '板块分类与指数成分', '行业、概念、地域、成分和权重。', [work, news, research], '后续', 'BaoStock当前分类/成分；AShare热门板块关注参考', '免费关注排名，非板块涨幅或资金榜', '保留混合原始代码及分类；榜单时间原文时区未明，当前成分不能代替历史成分。', ['tushare_pro', 'ashare_public', 'baostock', 'eastmoney_web', 'csi_cni']),
  capability('D10', '个股与板块资金流', '供应商口径的资金指标，与报价并列观察。', [work, research], '后续', '麦蕊本人免费证书：行业/个股盘后资金参考', '免费500次/日，本机保守预算450次/日', '净流入为供应商计算指标，不等于可直接识别的主力资金。', ['tushare_pro', 'mairui', 'eastmoney_web', 'sina_web']),
  capability('D11', '涨跌停池与连板梯队', '涨停、跌停、炸板和原日梯队；支持20/60日对照及当日板数、原日题材分组。', [work, auction, research], '后续', '麦蕊涨停/炸板/跌停原股池，连板字段单列', '免费本人证书；已核日历选请求日期', '缺盘中轨迹不能补造炸板次数；计算口径保留版本。', ['tushare_pro', 'mairui', 'eastmoney_web']),
  capability('D12', '市场广度与晋级统计', '情绪历史曲线、固定昨日样本表现及按板数细分晋级率；展示有效分母与缺口。', [work, research], '首批', '新增曲线与固定样本统计使用已购日线、限制价和涨停池；原广度来源独立保留', '免费匿名参考；完整分母、源日期与计算口径分别核验', '未知源时点不称实时；缺少成交额、炸板、溢价或晋级数据不补零。', ['tushare_pro', 'ashare_public', 'sse_public', 'szse_public', 'eastmoney_web']),
  capability('D13', '09:15 / 09:20竞价观察', '已有网站阶段封单、多日对照和柱状金额参考，精确快照仍待验证。', [auction, work], '条件评估', '竞价中沿用短线侠免费公开阶段表 / 东方财富 / 腾讯盘前快照', '短线侠当前65秒、历史3600秒，两路共享65秒任务间隔；页面65秒只读缓存', '网站阶段列非本机精确时点；只覆盖表内标的，封单不是成交额。截图档案尚未接入。', ['duanxianxia_auction', 'duanxianxia_auction_history', 'eastmoney_auction', 'tencent_auction']),
  capability('D14', '09:25撮合结果与历史', '完整已保存竞价成交表先筛后分页；竞价涨幅、金额、换手率、来源量比及昨日分组。', [auction, research], '条件评估', '成交返回后Tushare单列核对 → 短线侠历史封单补充', 'Tushare当日竞价成交9:26–9:29开始可取，实际权限/日期/返回范围见来源状态', '网站09:25封单不是成交额；Tushare原表不填9:26前快照，完整撮合历史与严格回放仍缺。', ['duanxianxia_auction_history', 'eastmoney_auction', 'tencent_auction', 'tushare_pro']),
  capability('D15', '逐笔、撤单与委托队列', 'L2精细竞价能力，先保留明确缺口。', [auction], '条件评估', '用户已有合法L2终端 → 专项授权信息商', '未确认长期免费全覆盖方案，需时再报价', '1.0保持未接入，不从L1价量差反推真实撤单或队列。', ['xtdata_terminal']),
  capability('D16', '快讯与基本资讯', '全球财经、科技产业与国际新闻的标题、原文和源时点。', [news, work, research], '首批', '原生RSS：BBC / CNBC / NYT / NPR / CNA / 香港电台及国际机构', '本机个人订阅阅读；独立媒体与频道数分开', '不复制文章全文，不把同一媒体多个频道当多家信源；国内商业电报另核。', ['chinanews_finance', 'global_publisher_rss', 'official_rss', 'hk_gov_finance', 'cninfo', 'eastmoney_web', 'sina_web', 'cls_content']),
  capability('D17', '公司公告与风险披露', '业绩预告、问询、风险警示及附件出处。', [news, research, work], '首批', '巨潮 / 交易所 → 公司官网', '公开披露优先，账号API套餐另核', '保存公告ID、发布时间和修订关系，PDF保存范围单独核实。', ['cninfo', 'sse_public', 'szse_public', 'bse_public']),
  capability('D18', '研报目录与评级', '报告目录、机构、评级和原文入口。', [news, research], '后续', '东方财富 / 新浪公开目录 → 机构原文', '免费目录候选，PDF与预测库不默认免费', '不批量复制付费报告；报告日、发布和接收时点分开。', ['eastmoney_web', 'sina_web']),
  capability('D19', '财务三表与估值指标', '公司披露、结构化财务和历史估值。', [research, work], '后续', '原始披露 → BaoStock / 门户结构化资料', '公开资料起步，结构化使用范围待核', '报告期不等于发布时间；币种、单位、修订和合并口径明确。', ['tushare_pro', 'cninfo', 'baostock', 'eastmoney_web', 'sina_web']),
  capability('D20', '股东、解禁与公司事件', '增减持、质押、回购、解禁和调研。', [news, research], '后续', '巨潮 / 交易所 → 聚合源', '免费披露优先，完整历史待核', '事件日、公告日、接收日分开；预约不等于已发生。', ['cninfo', 'sse_public', 'szse_public', 'eastmoney_web']),
  capability('D21', '龙虎榜、大宗与两融', '盘后披露及席位明细；不复权日K上榜日标记联动原原因，不推断交易者身份。', [research, work], '后续', '交易所 → 东方财富', '官方栏目候选，机器字段与时点待测', '盘后披露不回填为盘中已知；营业部与金额口径分开。', ['tushare_pro', 'sse_public', 'szse_public', 'eastmoney_web']),
  capability('D22', '互联互通披露', '北向 / 港股通按当前可披露口径展示。', [work, research], '后续', 'HKEX / 沪深官方披露', '公开披露候选，停止披露项不可用', '不恢复已停止披露的实时净流入；额度余额不能反推净买入。', ['hkex_connect', 'sse_public', 'szse_public']),
  capability('D23', 'ETF行情与基金披露', '报价、净值、份额、持仓与申赎资料。', [work, research], '后续', '交易所 / 基金公司 → efinance / AKShare端点', '免费候选，申赎深度另核', '净值、估算与IOPV分字段；当前持仓不反推历史。', ['sse_public', 'szse_public', 'eastmoney_web']),
  capability('D24', '可转债行情与条款', '正股映射、转股价、赎回和条款版本。', [work, research], '后续', '交易所 / 发行人 → efinance / AKShare端点', '免费资料候选', '债券与股票身份和单位分开；规则解析另行验证。', ['sse_public', 'szse_public', 'cninfo', 'eastmoney_web']),
  capability('D25', '宏观、政策与行业资料', '统计局、央行和部委的原始发布。', [news, research, work], '后续', '统计局 / 央行 / 部委 → 有出处的聚合资料', '官方发布优先', '保留观测期、发布日期和修订版本，不以前瞻修订替换旧输入。', ['tushare_pro', 'official_macro_rates', 'official_rss']),
  capability('D26', '利率、汇率与收益率', 'LPR、人民币中间价及债券收益率资料。', [work, research], '后续', '中国货币网 / 央行 → 中债另核', '公开栏目候选，专业曲线和API授权另核', '定盘价与可交易实时报价不同；原站超时不等于源永久失效。', ['tushare_pro', 'official_macro_rates']),
  capability('D27', '海外及衍生品扩展', '港美股、海外指数、期货与期权候选。', [work, research], '2.0 / 后续', '按品种另评交易所、聚合源与已有合法终端', '具体接口与免费权益尚未核验', '时区、日历、延迟和授权各自核实，不预购全球套餐。'),
  capability('D28', '互动问答与舆情热榜', '有出处的互动问答、排行和用户材料。', [news, research], '后续', '官方互动平台 / 公开排行候选', '自动化与批量权利待核', '平台热度不代表事实真伪；保存答复者、原文和时间。'),
  capability('D29', '自选关注与人工研究', '已有人工材料、研究记录与归档；关注操作待做。', [work, news, research, data], '2.0 / 后续', '本实例数据 → 独立备份恢复', '本地功能无外源订阅费', '人工保存不代表自选 / 关注管理已完成；飞书持续同步与复盘深化留2.0。', [], 'evidence.manual'),
  capability('D30', '事件、题材、规则与AI', '人工标注、确定性计算与可选模型解释。', [work, news, research], '2.0 / 后续', '本地证据与人工判断；模型以后决定', '先不依赖付费模型；外发与预算另定', '这些是判断能力；关键词或模型生成不能冒充权威事实。'),
  capability('D31', '健康、任务、配额与成本', '已有基础状态入口，调用、覆盖、费用待完善。', [data, system], '后续', '本地worker / API / 数据库元数据', '沿用本地基础设施，实际费用未知', '基础服务就绪不代表业务源接通；没有调用记录不能显示零费用。'),
];


// This updates the procurement/selection plan; actual capability states still
// come from the rights-checked runtime ledger, never this static map.
export const capabilities: readonly CatalogEntry[] = legacyCapabilities.map(item => {
  const priority = tusharePriority[item.id];
  const local = ['D29', 'D30', 'D31'].includes(item.id);
  const supportedCandidate = !['D13', 'D15', 'D29', 'D30', 'D31'].includes(item.id);
  return { ...item, basis: priority.plan + '；接口：' + priority.apis,
    cost: local || item.id === 'D15' ? item.cost : '已购Tushare权益内优先；实际权限、频次、费用按本机结果核验，免费来源补缺',
    sourceIds: supportedCandidate ? ['tushare_pro', ...(item.sourceIds ?? []).filter(id => id !== 'tushare_pro')] : item.sourceIds };
});

function source(id: string, name: string, purpose: string, stage: CatalogStage, cost: string, boundary: string, url: string): CatalogEntry {
  const covered = capabilities.filter(item => item.sourceIds?.includes(id));
  return { id, name, purpose, stage, cost, boundary, url, modules: covered.length ? [...new Set(covered.flatMap(item => item.modules))] : [work, research, data], basis: '公开资料 / 实现文档已核对；本机连接、覆盖与权利待核验' };
}
export const sources: readonly CatalogEntry[] = [
  source('chinanews_finance', '中新网财经RSS', '中文财经、产业与企业资讯的官方订阅条目。', '首批', '无需Key；每15分钟读取当前滚动列表', '个人本机RSS阅读，保留原标题、原生摘要、日期和出处；不抓全文，历史从持续采集起累积，第三方AI处理资格单独配置。', 'https://www.chinanews.com.cn/rss/'),
  source('pbc_news_candidate', '中国人民银行政策RSS', '央行政策与沟通交流的官方发布。', '条件评估', '公开订阅无需Key；本机连通性待验证', '候选来源，尚未登记为已接通；不以网页可读代替后台采集验收。', 'https://www.pbc.gov.cn/goutongjiaoliu/113456/2986536/index.html'),
  source('global_publisher_rss', '全球财经与产业RSS', '欧美、亚太、全球财经、科技和国际新闻；同媒体多个频道分别配置。', '首批', '原生RSS，无需付费Key；每频道至少15分钟', 'BBC / CNBC / NYT / NPR / CNA / 香港电台 / 英格兰银行 / BIS / 联合国新闻；仅本机个人原题/摘要/原文阅读，保持署名和源语言，不抓全文或图片。实际接通与错误逐频道显示。', 'https://www.channelnewsasia.com/rss'),
  source('hk_gov_finance', '香港政府财经RSS', '香港官方财经条目的原题、日期和原始链接。', '首批', '官方原生RSS，无需账号；本项目每15分钟一轮', '仅本机个人阅读/缓存并署名；不抓正文、图片或视频，公开和商业使用另核。零点编码保守按日期精度；不代表内地快讯。', 'https://www.news.gov.hk/tc/rss/index.lin.shtml'),
  source('tickflow_live', 'TickFlow 免费实时行情', '四指数及明确关注标的的实时快照。', '首批', '免费注册档：10次/分钟，每次最多5标的', '需本人Key，仅在本机安全配置；实际覆盖、时点和延迟按返回核验，不含免费全市场池/分钟/深度。', 'https://tickflow.org/pricing/'),
  source('mairui', '麦蕊智数本人免费证书', '行业/个股盘后资金参考，涨停/炸板/跌停原股池。', '首批', '官网免费500次/日；本机两连接共用450次/日，资金每小时2次、股池每15分钟3次', '本人证书仅本机使用，不导出或再分发；原更新时区未知，股池日期依据已核日历及请求参数，非逐笔实时或完整历史。概念资金首次503，暂不启用。', 'https://www.mairuiapi.com/getlicence'),
  source('ashare_public', 'AShareAPI 免费盘中参考', '少量标的价格、市场广度及热门板块关注排名。', '首批', '无需注册；每连接300秒、本机共享35秒匿名预算，报价最多5标的/板块榜最多30条', '关注热度非涨幅或资金榜，含海外范围；榜单时间原文时区未明，广度无源日期，不补行情时点。资金流、完整板块行情与涨停池另属付费端点；不原样转售/公开建库。', 'https://ashareapi.com/terms/'),
  source('sse_public', '上海证券交易所', '标的、日历、公告、规则与盘后公开统计。', '首批', '公开资料优先，机器 / 批量使用待核', '公开入口不等于通用API授权；逐类确认展示、缓存与导出。', 'https://www.sse.com.cn/market/publicdata/'),
  source('szse_public', '深圳证券交易所', '证券目录、交易公告、有效规则与披露。', '首批', '公开资料优先，机器 / 批量使用待核', '交易制度保留版本，各市场时段与覆盖不能相互代用。', 'https://www.szse.cn/market/product/stock/list/?selectModule=main&tabkey=tab1'),
  source('bse_public', '北京证券交易所', '股票列表、新旧代码映射与官方披露。', '首批', '公开资料优先，自动读取待测', '部分入口有重定向；具体覆盖与代码映射仍待验证。', 'https://www.bse.cn/nq/listedcompany.html'),
  source('duanxianxia_auction', '短线侠公开竞价封单表（当前）', '网站09:15/09:20/09:25三阶段封单金额、标签与原涨幅。', '条件评估', '无需Key，默认65秒；与历史表共享65秒任务间隔，失败也计入', '只覆盖表内标的；先核对动态域名和公开脚本指纹。日期取自网站，本机接收与来源更新时间分开；上游行情供应商未知。', 'https://duanxianxia.com/web/jjlive'),
  source('duanxianxia_auction_history', '短线侠公开竞价封单表（历史）', '最近交易日三阶段封单参考。', '条件评估', '一次POST返回最近交易日，默认3600秒；共用65秒任务间隔', '历史表不包含最终撮合成交、逐笔或真实撤单，不进入严格历史决策回放。', 'https://duanxianxia.com/web/jjlive'),
  source('eastmoney_auction', '东方财富公开盘前竞价快照', '09:15/09:20/09:25 指示性价格与累计竞价量。', '条件评估', '公开网页接口，无需Key；每来源1–20个沪深股票、最小60秒一轮', '仅检查点±60秒内标记时点；汇总/明细按批次哈希配对，失败保留上次成功批次。不推断未匹配量、撤单或队列，不构成撮合完成。', 'https://quote.eastmoney.com/'),
  source('tencent_auction', '腾讯网页公开盘前竞价快照', '09:15/09:20/09:25 指示性价格与累计竞价量。', '条件评估', '公开网页接口，无需Key；每来源1–20个沪深股票、最小60秒一轮', '金额取原币元字段，不用万元近似值；时点未命中检查点保持未知。与东方财富同族但独立核对，不互为容灾承诺。', 'https://gu.qq.com/'),
  source('tencent_web', '腾讯网页行情候选', '指数、当前标的报价及近期历史候选。', '首批', '免费实现候选，额度与服务承诺未知', '入口来自实现文档；使用条件、延迟和字段逐项核实。', 'https://akshare.akfamily.xyz/data/stock/stock.html'),
  source('sina_web', '新浪网页数据候选', '报价、历史、资讯目录及独立上游对照。', '首批', '免费实现候选，使用范围待核', '开源取数说明不能替代门户授权或可用性实测。', 'https://akshare.akfamily.xyz/data/stock/stock.html'),
  source('eastmoney_web', '东方财富网页数据候选', '截面、板块、股池、资金、基金与特色资料。', '后续', '免费实现候选，批量条件待核', 'AKShare、efinance、adata访问同一上游时，不算独立备源。', 'https://efinance.readthedocs.io/en/latest/api.html'),
  source('baostock', 'BaoStock', '盘后收盘、沪深月历、日K/复权、估值、财务指标、行业与指数成分。', '首批', '官方匿名免费接口，无需个人账号或密钥', '各已实现子集有独立来源身份与采集范围；按日期展示，非实时。财务并非完整三表，北交所与部分资产暂缺。', 'https://pypi.org/project/baostock/0.9.4/'),
  source('cninfo', '巨潮 / 深证信', '公告、披露资料与标的补充。', '首批', '公开阅读与账号API套餐分别核实', 'Web栏目、API权益、PDF缓存与再分发范围不能互相代替。', 'https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice'),
  source('csi_cni', '中证 / 国证指数资料', '指数分类、成分与权重资料。', '后续', '公开文件候选，历史使用范围待核', '当前成分不能还原历史归属；具体指数分开核验。', 'https://www.csindex.com.cn/'),
  source('hkex_connect', 'HKEX互联互通披露', '按当前披露制度提供的互联互通统计。', '后续', '公开披露候选', '遵循披露调整；不展示已停止披露的实时净流入字段。', 'https://www.hkex.com.hk/News/Market-Communications/2024/2404122news?sc_lang=en'),
  source('official_macro_rates', '统计局 / 央行 / 中国货币网', '宏观统计、利率、汇率及政策原始资料。', '后续', '公开发布优先，专业曲线授权另核', '每个发布方和指标单独登记，保留修订与原始发布时间。', 'https://www.stats.gov.cn/sj/'),
  source('official_rss', '允许订阅的官方RSS', '政策与原发布方允许订阅的基本条目。', '首批', '原生订阅候选，具体feed待核', 'RSS栏目不自动授权全文保存或再分发。', 'https://www.miit.gov.cn/RRSdy/'),
  source('ecb_public', '欧洲中央银行', '官方每日参考汇率和原生RSS资讯。', '首批', '官方信息可免费使用，准确署名', '参考汇率不是可交易报价；只采官方条目，来源版本与时间单列。', 'https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html'),
  source('fed_public', '美国联邦储备委员会', '官方RSS新闻发布标题、摘要、时间和出处。', '首批', '官方原生资料公共领域说明', '不采第三方图片或付费内容；不是国内商业快讯全覆盖。', 'https://www.federalreserve.gov/disclaimer.htm'),
  source('worldbank_public', '世界银行WDI', '中国年度宏观与年度贷款/实际利率。', '首批', '开放API、CC-BY4.0，标明来源', '年度统计与修订版本；不当作月度即时数据、LPR或SHIBOR。', 'https://datacatalog.worldbank.org/public-licenses'),
  source('xtdata_terminal', 'QMT / xtdata行情', '用户已有合法终端时，评估分钟与精细行情。', '条件评估', '取决于已有终端及L2权益，不假设免费', '用户终端 / 账号状态未知；只评估行情，不连接交易组件。', 'https://dict.thinktrader.net/nativeApi/xtdata.html'),
  source('tushare_pro', 'Tushare 已购首选', '已购权益优先：目录、沪深日历、个股行情、日线及财务/资金/榜单/利率等参考表。', '首批', '用户已购套餐；具体费用和到期日未提供，不按零费用计算', '用户指定兼容服务；有界连接逐项核验，新增指数基础、股票限制价和分红参考表。实时指数/分钟/新闻预检拒绝、北交所日历空；现有免费源补缺，不把MCP工具数当权益数。', 'https://tushare.pro/document/1?doc_id=290'),
  source('jqdata', 'JQData', '已有权益或明确试用条件下的数据补充。', '条件评估', '试用 / 正式权限待核，非永久免费', '地区限制、账号和期限尚未核实，不作为首期默认依赖。', 'https://www.joinquant.com/help/api/doc?id=10273&name=JQDatadoc'),
  source('rqdata', 'RQData', '商业历史 / 特色数据的条件比较项。', '条件评估', '商业方案有试用，正式价格按需求核实', '先验证免费组合的缺口，再讨论最小必要采购。', 'https://www.ricequant.com/welcome/store/'),
  source('cls_content', '财联社内容', '商业快讯出处与授权方案比较。', '条件评估', '需书面授权，本项目尚无授权', '未获授权不自动聚合条目或全文；原站入口不算接入。', 'https://www.cls.cn/our?nav=copyright'),
];

function tool(id: string, name: string, purpose: string, modules: readonly CatalogModule[], stage: CatalogStage, license: string, boundary: string, repository: string, commit: string): CatalogEntry {
  return { id, name, purpose, modules, stage, license, boundary, basis: `代码许可快照：${license}`, cost: '开源工具不等于上游数据免费；引入前审查实际发行包与依赖', url: `https://github.com/${repository}/tree/${commit}` };
}
export const tools: readonly CatalogEntry[] = [
  tool('akshare', 'AKShare', '按需复用板块、资金、股池、财务等端点。', common, '首批', 'MIT', '优先少量必要端点；Python 3.14、依赖与真实上游尚未实测。', 'akfamily/akshare', 'fe10d1decb705e5f43cc8ca3339ca39e580fb8f4'),
  tool('efinance', 'efinance', '股票、基金、转债快照与历史的对照候选。', [work, research, data], '后续', 'MIT', '多数端点与东方财富同族，不形成独立容灾。', 'Micro-sheep/efinance', 'c8fd370a3109b2d14a121e3a32a86e9c8354b01b'),
  tool('adata', 'adata', '多源选择与降级设计参考。', common, '后续', 'Apache-2.0', '先比较具体端点，不和其他库全部引入；不使用代理池绕限制。', '1nchaos/adata', '8217a4ec593e7bf6c9cf32a1aa74b711e7baa720'),
  tool('a-stock-data', 'a-stock-data', '发现数据入口和已知字段陷阱。', common, '后续', 'Apache-2.0', '仅作研究线索，不照搬取数、关闭TLS或交易路径。', 'simonlin1212/a-stock-data', '29b07d2e37005b9fd568bca1262f6e572b5f9407'),
  tool('feedparser', 'feedparser', '允许的RSS / Atom解析小组件。', [news, research, data], '首批', 'BSD-2-Clause；文档另有声明', '只负责解析，不提供新闻内容权利或合法feed清单。', 'kurtmckee/feedparser', 'a22c5521cbb109871f1a2318948581901bd47e26'),
  tool('echarts', 'ECharts', '价格、成交量、板块与广度图统一呈现。', [work, auction, research], '首批', 'Apache-2.0', '优先只选一个图库；引入时保留NOTICE，图库不提供行情。', 'apache/echarts', '34f4d927eafe251f26f63afd3aeefa30f2feba30'),
  tool('lightweight-charts', 'Lightweight Charts', '需要时补专业价格图交互。', [work, research], '后续', 'Apache-2.0 + NOTICE / 可见归属', '需TradingView可见归属和链接；不为初步可用同时上两图库。', 'tradingview/lightweight-charts', '9db7ce733e95303ae1eeb79e664ee9be4ee5e696'),
  tool('rsshub', 'RSSHub', '无合适原生订阅入口时评估路由。', [news, data], '条件评估', 'AGPL-3.0', '增加Node服务与维护；评估AGPL义务，路由不等于转载授权。', 'DIYgod/RSSHub', '11fcf768bcbfa460133c4ce1ba14a5b59a201b40'),
  tool('openbb', 'OpenBB / openbq-org', 'Provider抽象和后续多市场参考。', [work, research, data], '2.0 / 后续', '当前根文本Apache-2.0', '暂缓整平台引入；数据供应商仍可能收费。', 'openbq-org/OpenBB', '78020ee4b864ab50b8c1f56373877c2b4e7fb623'),
  tool('instock', 'InStock', '盘后ETL、指标与页面组织参考。', [work, research, data], '2.0 / 后续', 'Apache-2.0', '仅参考流程，不搬入整套选股 / 回测 / 交易架构。', 'myhhub/stock', 'b6e0ca2268cfbadd02f5ed052159c187b6670231'),
  tool('qlib', 'Qlib', '后续因子与离线研究参考。', [research, data], '2.0 / 后续', 'MIT', '非实时源；官方数据集暂停，社区数据权利与质量另核。', 'microsoft/qlib', '54355232463878d2eebb91fe0ee5fa7fa1f5976c'),
  tool('vnpy', 'vn.py', '数据适配边界与行情事件组织参考。', [work, data], '2.0 / 后续', 'MIT', '暂缓框架引入；不连接broker、委托或执行组件。', 'vnpy/vnpy', 'c6e231caf32b7fc97e6459817fff66458cf7e7c4'),
  tool('mootdx', 'mootdx', '通达信文件与历史协议的备选研究。', [work, research, data], '条件评估', 'MIT', '默认分支最近提交为2024年；公共服务器与3.14未验，不作唯一主源。', 'mootdx/mootdx', 'e99ae34382d970c68654c6d17c45512e728f130d'),
  tool('pytdx', 'pytdx', '仅保留历史协议线索。', [data], '2.0 / 后续', '根许可未确认', '上游已归档，不选作1.0新依赖；不视为可自由复制。', 'rainx/pytdx', '14b1ad3534593952d1d698ffa706f4f13a4ed156'),
  tool('tushare-sdk', 'Tushare旧SDK', '特色数据接口与权限差异研究。', [auction, research, data], '条件评估', '旧仓库BSD-3-Clause', '旧仓库和当前发行包不同；实施时重新审查实际包与服务端权益。', 'waditu/tushare', '093856995af0811d3ebbe8c179b8febf4ae706f0'),
];
