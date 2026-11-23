import type { SourceConfiguration } from '../../generated/schema';

export type ReferenceWidgetDefinition = {
  id: string;
  title: string;
  description: string;
  defaultVisible: boolean;
  family: SourceConfiguration['family'];
  dataset: string;
  scope: 'selected' | 'market';
  assetType?: 'stock' | 'index';
  filterSelected?: boolean;
  previewFields?: readonly string[];
};

const previewFields: Record<string, readonly string[]> = {
  'top-list': ['trade_date', 'ts_code', 'name', 'reason', 'net_amount', 'pct_change', 'pct_chg'],
  'daily-basic': ['trade_date', 'ts_code', 'close', 'turnover_rate', 'pe', 'pb'],
  moneyflow: ['trade_date', 'ts_code', 'net_mf_amount', 'net_mf_vol', 'buy_lg_amount', 'sell_lg_amount'],
  'market-limits': ['trade_date', 'ts_code', 'pre_close', 'up_limit', 'down_limit'],
  daily: ['trade_date', 'ts_code', 'open', 'high', 'low', 'close'],
  'index-daily': ['trade_date', 'ts_code', 'open', 'high', 'low', 'close'],
  'adj-factor': ['trade_date', 'ts_code', 'adj_factor'],
  income: ['end_date', 'ann_date', 'ts_code', 'total_revenue', 'n_income', 'basic_eps'],
  balancesheet: ['end_date', 'ann_date', 'ts_code', 'total_assets', 'total_liab', 'total_hldr_eqy_exc_min_int'],
  cashflow: ['end_date', 'ann_date', 'ts_code', 'n_cashflow_act', 'n_cashflow_inv_act', 'n_cash_flows_fnc_act'],
  'fina-indicator': ['end_date', 'ann_date', 'ts_code', 'eps', 'roe', 'debt_to_assets'],
  'limit-list': ['trade_date', 'ts_code', 'name', 'limit', 'pct_chg', 'limit_times'],
  'block-trade': ['trade_date', 'ts_code', 'price', 'vol', 'amount', 'buyer', 'seller'],
  'margin-detail': ['trade_date', 'ts_code', 'name', 'rzye', 'rzmre', 'rqye', 'rzrqye'],
  dividend: ['ann_date', 'ts_code', 'div_proc', 'cash_div_tax', 'stk_div', 'ex_date'],
  'ths-index': ['ts_code', 'name', 'count', 'exchange', 'type', 'list_date'],
  shibor: ['date', 'on', 'field_1w', 'field_1m', 'field_3m', 'field_1y'],
  cpi: ['month', 'nt_val', 'nt_yoy', 'nt_mom', 'town_yoy', 'cnt_yoy'],
  auction: ['trade_date', 'ts_code', 'price', 'vol', 'amount', 'turnover_rate'],
  'index-basic-sse': ['ts_code', 'name', 'market', 'publisher', 'list_date', 'base_point'],
  'index-basic-szse': ['ts_code', 'name', 'market', 'publisher', 'list_date', 'base_point'],
  'stk-limit': ['trade_date', 'ts_code', 'pre_close', 'up_limit', 'down_limit'],
};

const widget = (id: string, title: string, family: SourceConfiguration['family'], api: string,
  scope: ReferenceWidgetDefinition['scope'], description: string,
  options: Partial<Pick<ReferenceWidgetDefinition, 'defaultVisible' | 'assetType' | 'filterSelected'>> = {}): ReferenceWidgetDefinition =>
  ({ id, title, family, dataset: 'tushare.' + api, scope, description, defaultVisible: false, previewFields: previewFields[id], ...options });

// These are implemented cached reference tables, not a list of proven entitlements.
// Quotes, market-wide rt_k raw tables, catalog and calendar have dedicated views.
export const widgetCatalog: readonly ReferenceWidgetDefinition[] = [
  widget('top-list', '龙虎榜', 'tushare_top_list', 'top_list', 'market', '按原交易日期与上榜原因查看盘后榜单；同一股票可有多条原因，不合并为一条。', { defaultVisible: true, filterSelected: true }),
  widget('daily-basic', '估值与换手率', 'tushare_daily_basic', 'daily_basic', 'selected', '所选股票已保存的估值与换手字段，数据日期与接收时间分别显示。'),
  widget('moneyflow', '个股盘后资金', 'tushare_moneyflow', 'moneyflow', 'selected', '供应商计算的盘后资金指标，不能视为实时主力资金或识别交易者身份。'),
  { ...widget('market-limits', '当日全市场限制价', 'tushare_market_limits', 'market_limits', 'market', '当日限制价原表；价格限制不是当前涨跌停数量，也不是曾触板家数。', { filterSelected: true }), dataset: 'tushare.market_limits' },
  widget('daily', '历史日线明细', 'tushare_daily', 'daily', 'selected', '所选股票已保存的日线字段；日线不是盘中分时，保留原复权口径。'),
  widget('index-daily', '指数日线明细', 'tushare_index_daily', 'index_daily', 'selected', '仅显示所选指数的日线资料，不借用同代码股票数据。', { assetType: 'index' }),
  widget('adj-factor', '复权记录', 'tushare_adj_factor', 'adj_factor', 'selected', '所选股票原始日期与复权因子；因子不是价格或收益率。'),
  widget('income', '利润表', 'tushare_income', 'income', 'selected', '已保存的合并报表字段，最多四个报告期；报告期、公告日与修订标志保留。'),
  widget('balancesheet', '资产负债表', 'tushare_balancesheet', 'balancesheet', 'selected', '所选股票合并报表原字段，区分报告期与公告日；不代表完整财务历史。'),
  widget('cashflow', '现金流量表', 'tushare_cashflow', 'cashflow', 'selected', '所选股票经营、投资及筹资现金流原字段；各字段原单位分别保留。'),
  widget('fina-indicator', '财务指标', 'tushare_fina_indicator', 'fina_indicator', 'selected', '所选股票已保存财务指标；每股值、比例和金额不可混用单位。'),
  widget('limit-list', '涨跌停与炸板日榜', 'tushare_limit_list_d', 'limit_list_d', 'market', '按原交易日期与U/Z/D分类查看供应商日榜，不替代盘中统计曲线。', { filterSelected: true }),
  widget('block-trade', '大宗交易', 'tushare_block_trade', 'block_trade', 'market', '有界盘后交易资料，按原交易日展示；不是实时逐笔成交。', { filterSelected: true }),
  widget('margin-detail', '融资融券明细', 'tushare_margin_detail', 'margin_detail', 'market', '通常次日公布的两融资料，保留原交易日期；不能视为今日即时资金。', { filterSelected: true }),
  widget('dividend', '分红事项', 'tushare_dividend', 'dividend', 'selected', '区分预案、实施进度及未来登记、除权、派息计划，不把未来日期当已实施。'),
  widget('ths-index', '同花顺概念目录', 'tushare_ths_index', 'ths_index', 'market', '供应商概念目录与原分类标识，不是板块实时行情、涨幅或资金排名。'),
  widget('shibor', 'SHIBOR利率', 'tushare_shibor', 'shibor', 'market', '原数据日期与各期限字段；定盘利率资料不是可交易实时报价。'),
  widget('cpi', '月度CPI', 'tushare_cn_cpi', 'cn_cpi', 'market', '按原统计月份展示已保存宏观字段，月份不是发布时间。'),
  widget('auction', '集合竞价成交参考', 'tushare_stk_auction', 'stk_auction', 'market', '原交易日的竞价成交参考，不补造09:15等阶段行情或竞价历史轨迹。', { filterSelected: true }),
  widget('index-basic-sse', '沪市指数基础资料', 'tushare_index_basic_sse', 'index_basic', 'market', '沪市指数基础资料，保留发布机构、市场与原码，不认作股票或实时指数。'),
  widget('index-basic-szse', '深市指数基础资料', 'tushare_index_basic_szse', 'index_basic', 'market', '深市指数基础资料，独立于沪市同名数据集，不与股票代码混用。'),
  widget('stk-limit', '个股历史限制价', 'tushare_stk_limit', 'stk_limit', 'selected', '所选股票近10个自然日内的价格限制；不据此推断当日触板或封板状态。'),
];

export const referenceWidgetCatalog = widgetCatalog;
