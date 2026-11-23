"""Stable identities for the direction page's independent vendor rankings."""

PUBLIC_SPECS = {
    'direction_kpl_top': ('开盘啦题材前十快照', 'kpl_top_v1',
        'https://apphwhq.longhuvip.com/w1/api/index.php'),
    'direction_kpl_emotion': ('开盘啦市场情绪', 'kpl_emotion',
        'https://apphwhq.longhuvip.com/w1/api/index.php'),
    'ths_industry_funds': ('同花顺行业资金净额', 'ths_industry_funds', 'https://data.10jqka.com.cn/funds/hyzjl/'),
    'ths_concept_funds': ('同花顺概念资金净额', 'ths_concept_funds', 'https://data.10jqka.com.cn/funds/gnzjl/'),
    'direction_ths_hot': ('同花顺公开人气榜', 'ths_hot',
        'https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock'),
    'direction_tdx_hot': ('通达信公开人气榜', 'tdx_hot',
        'https://pul.tdx.com.cn/TQLEX'),
    'direction_em_hot': ('东方财富公开人气榜', 'em_hot',
        'https://emappdata.eastmoney.com/stockrank/getAllCurrentList'),
    'direction_kpl_strength': ('开盘啦题材强度', 'kpl_strength',
        'https://apphwhq.longhuvip.com/w1/api/index.php'),
}
PUBLIC_FAMILIES = frozenset(PUBLIC_SPECS)
BOARD_FAMILIES = frozenset({'tushare_dc_index', 'tushare_direction_industry',
    'tushare_moneyflow_ind_dc', 'tushare_direction_industry_funds'})
FAMILIES = PUBLIC_FAMILIES | BOARD_FAMILIES
