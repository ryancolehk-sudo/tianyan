"""Bounded daily-review acquisition contracts, separate from live source settings."""
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from app.schemas import DTO

PUBLIC_FAMILIES = frozenset({
    'review_tencent_indices', 'review_sina_indices', 'review_cls_news', 'review_kpl_new_highs',
})
INDEX_CODES = {
    '000001.SH': ('sh000001', '上证指数'),
    '399001.SZ': ('sz399001', '深证成指'),
    '399006.SZ': ('sz399006', '创业板指'),
    '000688.SH': ('sh000688', '科创50'),
    '932000.CSI': (None, '中证2000'),
    '000300.SH': ('sh000300', '沪深300'),
}
MINUTE_CODES = tuple(INDEX_CODES)[:3]
DATASET_FAMILIES = {
    'new_highs': 'review_kpl_new_highs',
    'new_high_history': 'review_kpl_new_highs',
    'emotion_history': 'direction_kpl_emotion',
    'stock_funds': 'tushare_moneyflow_dc',
    'market_funds': 'tushare_moneyflow_mkt_dc',
    'daily': 'tushare_daily',
    'limits': 'tushare_market_limits',
    'pools': 'tushare_limit_list_d',
    'pool_details': 'tushare_limit_list_ths',
    'group_basics': 'tushare_daily_basic',
    'themes': 'tushare_dc_index',
    'members': 'tushare_dc_member',
    'theme_funds': 'tushare_moneyflow_ind_dc',
    'industry_themes': 'tushare_direction_industry',
    'industry_funds': 'tushare_direction_industry_funds',
    'lhb': 'tushare_top_list',
    'seats': 'tushare_top_inst',
    'indices': 'tushare_index_daily',
    'calendar_sse': 'tushare_calendar_sse',
    'calendar_szse': 'tushare_calendar_szse',
    'tencent_minutes': 'review_tencent_indices',
    'sina_minutes': 'review_sina_indices',
    'news': 'review_cls_news',
}
DAILY_DATASETS = ('pool_details', 'group_basics', 'daily', 'limits', 'pools', 'themes', 'theme_funds', 'lhb', 'seats', 'stock_funds', 'market_funds')
DATASET_NAMES = tuple(DATASET_FAMILIES)
PUBLIC_DATASETS = frozenset({'tencent_minutes', 'sina_minutes', 'news', 'emotion_history', 'new_highs', 'new_high_history'})
PAGE_SIZE = 5000
MAX_MEMBER_PAGES = 80
NEWS_PAGE_SIZE = 20
MAX_NEWS_PAGES = 80
CONTRACT_VERSION = 1


class ReviewCollectionInput(DTO):
    trade_day: date | None = None
    lookback: Literal[20, 60] = 60
    include_members: bool = True
    member_lookback: Literal[5, 20, 60] = 5
    include_news: bool = True
    source_scope: Literal['legacy', 'four_sources'] = 'legacy'
    force: bool = False
    sector_codes: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode='after')
    def checked_sectors(self):
        import re
        if len(set(self.sector_codes)) != len(self.sector_codes) or any(
            not re.fullmatch(r'BK\d{4,8}\.DC', value) for value in self.sector_codes
        ):
            raise ValueError('sector codes must be distinct DC identifiers')
        return self


@dataclass
class ReviewPage:
    fields: list[str]
    rows: list[dict[str, str | None]]
    source_url: str
    raw_hash: str
    scope: str = ''


UNITS = {
    'new_highs': {'price':'元','pct_chg':'%','amount':'元','main_net':'元','main_buy':'元',
                  'main_sell':'元','actual_float':'元','total_cap':'元','actual_turnover':'%', 'total':'只', 'new_count':'只'},
    'new_high_history': {'total':'只','new_count':'只'},
    'emotion_history': {'emotion': '0..100'},
    'stock_funds': {'net_amount': '万元', 'pct_change': '%', 'close': '元'},
    'market_funds': {'net_amount': '元'},
    'group_basics': {'close':'元', 'free_share':'万股', 'float_share':'万股', 'total_share':'万股', 'circ_mv':'万元', 'total_mv':'万元'},
    'pool_details': {'price':'元', 'pct_chg':'%', 'turnover_rate':'%'},
    'daily': {'open': '元', 'high': '元', 'low': '元', 'close': '元',
              'pre_close': '元', 'pct_chg': '%', 'amount': '千元', 'vol': '手'},
    'limits': {'pre_close': '元', 'up_limit': '元', 'down_limit': '元'},
    'indices': {'close': '点', 'pre_close': '点', 'pct_chg': '%', 'amount': '千元'},
    'theme_funds': {'net_amount': '元', 'pct_change': '%', 'net_amount_rate': '%'},
    'industry_themes': {'pct_change': '%'},
    'industry_funds': {'net_amount': '元', 'pct_change': '%'},
    'lhb': {key: '元' for key in ('amount', 'l_buy', 'l_sell', 'l_amount', 'net_amount')},
    'seats': {key: '元' for key in ('buy', 'sell', 'net_buy')},
}
