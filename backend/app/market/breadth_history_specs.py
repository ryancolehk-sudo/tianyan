"""Independent identity for a date-bound public market-count history feed."""
FAMILY = 'xgb_breadth_history'
FAMILIES = frozenset({FAMILY})
NAME = '选股宝 · 历史涨跌停家数'
VERSION = 'xgb-breadth-history-v1'
CAPABILITY = 'market.breadth.history'
DATASET = 'market.xgb.breadth_history'
ENDPOINT = 'https://flash-api.xuangubao.com.cn/api/market_indicator/line'
REFERENCE = 'https://xuangubao.cn/'
MINIMUM_INTERVAL = 900
SCOPE = ('选股宝原始涨跌停家数历史序列；市场范围及ST处理未独立核实，'
         '保留来源口径，不与其他来源的报价核验曲线拼接。源epoch时点与实际补采接收时间分别保存。'
         '仅本人本机展示与缓存，不开放导出或再分发。')
