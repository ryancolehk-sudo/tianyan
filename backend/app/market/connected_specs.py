"""Stable capability identities for authenticated Fuyao and TdxAiData sources."""

FUYAO_ENDPOINT = 'https://fuyao.aicubes.cn'
TDX_ENDPOINT = 'https://help.tdx.com.cn/quant/'
FUYAO_DOCS = FUYAO_ENDPOINT + '/docs/'
TDX_DOCS = TDX_ENDPOINT + 'docs/markdown/mindoc-1hjbgqpdhv114.html'
FUYAO_SPECS = {
    'fuyao_auction': ('同花顺 API · 竞价未匹配量', ('market.auction.native',), 60),
    'fuyao_quotes': ('同花顺 API · 股票报价', ('quote.live',), 15),
    'fuyao_indices': ('同花顺 API · 四指数报价', ('quote.indices',), 15),
    'fuyao_daily': ('同花顺 API · 股票日K', ('bar.daily',), 21600),
    'fuyao_index_daily': ('同花顺 API · 指数日K', ('bar.daily',), 21600),
    'fuyao_adj_factor': ('同花顺 API · 原始除权事件', ('corporate.factors',), 86400),
    'fuyao_financial': ('同花顺 API · 财务三表', ('company.financial',), 86400),
}
TDX_SPECS = {
    'tdx_quotes': ('通达信 · 股票报价（积分）', ('quote.live',), 60),
    'tdx_depth5': ('通达信 · 五档快照（积分）', ('quote.depth5',), 60),
    'tdx_minute': ('通达信 · 1分钟K（积分）', ('bar.minute', 'bar.1m'), 120),
    'tdx_full_minute': ('通达信 · 按需全市场分钟（积分）', ('bar.minute', 'bar.1m', 'bar.full_minute'), 86400),
}
FUYAO_FAMILIES = frozenset(FUYAO_SPECS)
TDX_FAMILIES = frozenset(TDX_SPECS)
FAMILIES = FUYAO_FAMILIES | TDX_FAMILIES
