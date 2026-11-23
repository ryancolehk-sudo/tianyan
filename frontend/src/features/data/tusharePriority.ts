// Source-selection plan only; never creates providers or claims entitlement.
export const tusharePriority: Record<string, { apis: string; plan: string }> = {
  "D01": {
    "apis": "stock_basic / namechange / bak_basic",
    "plan": "Tushare目录优先，交易所/巨潮与BaoStock补别名和历史；部分接口待验"
  },
  "D02": {
    "apis": "trade_cal",
    "plan": "Tushare沪深日历优先，交易所公告补临时休市与时段；北交所空表仍缺"
  },
  "D03": {
    "apis": "rt_k / rt_idx_k",
    "plan": "Tushare合格实时快照优先，TickFlow/腾讯补缺；指数接口曾拒绝"
  },
  "D04": {
    "apis": "rt_k",
    "plan": "先核实批量范围和时点；全市场通配符曾拒绝，不用单标的冒充全市场"
  },
  "D05": {
    "apis": "rt_min / stk_mins / weekly / monthly",
    "plan": "分钟接口曾拒绝；周月可由同口径日线聚合，免费近期窗口补缺"
  },
  "D06": {
    "apis": "daily / index_daily / daily_basic",
    "plan": "Tushare日线估值优先，BaoStock三复权补缺；当前有界标的窗口"
  },
  "D07": {
    "apis": "adj_factor / dividend",
    "plan": "Tushare因子与分红优先，公告与免费因子交叉核对；不跨源拼复权"
  },
  "D08": {
    "apis": "stk_limit / suspend_d",
    "plan": "Tushare价格限制优先，停复牌待验，交易所规则补充；价格限制不等于触板"
  },
  "D09": {
    "apis": "index_basic / index_weight / ths_index / ths_member",
    "plan": "Tushare分类/指数/成分优先，分类命名空间与原始日期保留；成分待验"
  },
  "D10": {
    "apis": "moneyflow / moneyflow_ind_ths / moneyflow_dc",
    "plan": "Tushare有权个股/板块资金优先，麦蕊补缺；板块接口待验"
  },
  "D11": {
    "apis": "limit_list_d / limit_list_ths / limit_step",
    "plan": "Tushare有日期股池优先，盘后日榜不替代免费盘中曲线，其他接口待验"
  },
  "D12": {
    "apis": "rt_k / daily",
    "plan": "Tushare完整合格截面可作广度计算输入；现无完整实时覆盖，保留AShare"
  },
  "D13": {
    "apis": "stk_auction",
    "plan": "只用于成交结果，不能替代09:15/09:20封单；继续短线侠/合格免费快照"
  },
  "D14": {
    "apis": "stk_auction",
    "plan": "Tushare已购竞价成交优先，历史封单独立；严格逐时点回放仍缺"
  },
  "D15": {
    "apis": "无已核验等价接口",
    "plan": "未证实当前套餐提供L2逐笔/撤单/队列；保留缺口，不推断数据"
  },
  "D16": {
    "apis": "news / major_news / cctv_news",
    "plan": "Tushare新闻权限曾拒绝；保留原生RSS补缺，其他接口逐项验权"
  },
  "D17": {
    "apis": "anns_d",
    "plan": "先验Tushare公告目录，再巨潮/交易所补缺；不自动批量下载附件"
  },
  "D18": {
    "apis": "research_report",
    "plan": "先验Tushare研报目录及预测字段，免费目录补缺；不默认有全文权利"
  },
  "D19": {
    "apis": "income / balancesheet / cashflow / fina_indicator",
    "plan": "Tushare财务优先，BaoStock补缺；报告期/公告日/修订分开"
  },
  "D20": {
    "apis": "dividend / forecast / express / top10_holders / share_float / repurchase / stk_surv",
    "plan": "Tushare公司事件优先，现先分红，其余待验，巨潮出处补充"
  },
  "D21": {
    "apis": "top_list / block_trade / margin_detail",
    "plan": "Tushare原始榜单/大宗/两融优先；发布时间和有界日期保持"
  },
  "D22": {
    "apis": "hk_hold / moneyflow_hsgt",
    "plan": "先验现行披露范围与可用日期，不生成已停止披露的实时统计"
  },
  "D23": {
    "apis": "fund_basic / fund_daily / fund_nav / fund_share",
    "plan": "先验Tushare基金/ETF基础、日线、净值份额，实时ETF权益另验"
  },
  "D24": {
    "apis": "cb_basic / cb_daily",
    "plan": "先验Tushare转债基础与日线，赎回及转股价版本后续核验"
  },
  "D25": {
    "apis": "cn_cpi / cn_ppi / cn_m / sf_month / cn_pmi",
    "plan": "Tushare月度宏观优先，WorldBank年度与官方政策原文补充；CPI外待验"
  },
  "D26": {
    "apis": "shibor / shibor_lpr / fx_daily",
    "plan": "Tushare利率/汇率优先，ECB/官方发布补缺；不同定盘口径不混合"
  },
  "D27": {
    "apis": "hk_daily / us_daily / fut_daily / opt_daily",
    "plan": "Tushare扩展资产逐类验权与时区，未接入时不称覆盖全球"
  },
  "D28": {
    "apis": "irm_qa_sh / irm_qa_sz / ths_hot",
    "plan": "Tushare互动与热榜优先候选；原文出处、时点和权限待验"
  },
  "D29": {
    "apis": "本地数据",
    "plan": "人工研究/自选不是行情服务输出，保留本地账号与版本，不外发"
  },
  "D30": {
    "apis": "本地计算",
    "plan": "事件关联/规则/AI不是上游原始事实；资料可用Tushare，判断层保持独立"
  },
  "D31": {
    "apis": "本地运行记录",
    "plan": "状态/费用/配额来自本机实际调用和套餐证据，不拿官方通用额度当当前套餐额度"
  }
};
