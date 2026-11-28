"""Implemented source identities only. A catalogue candidate is never auto-enabled."""
from dataclasses import dataclass
from app.market.cadence_policy import FAST_FLOORS
from app.market.news_feeds import FLASH_FAMILIES, FLASH_INTERVAL, NEWS_FEEDS
from app.market.minute import FAMILY_INTERVALS, MINIMUM_INTERVALS, SOURCE_VERSION, SOURCE_REFERENCE, SOURCE_DOCUMENTATION
from app.market.tushare_specs import API_DOCS as TUSHARE_API_DOCS, DOCS as TUSHARE_DOCS, SPECS as TUSHARE_SPECS
from app.market.connected_specs import FUYAO_SPECS, TDX_SPECS, FUYAO_DOCS, TDX_DOCS
from app.market.direction_specs import PUBLIC_SPECS as DIRECTION_SPECS
from app.market.breadth_history_specs import FAMILY as BREADTH_HISTORY, NAME as BREADTH_HISTORY_NAME, VERSION as BREADTH_HISTORY_VERSION, CAPABILITY as BREADTH_HISTORY_CAPABILITY, ENDPOINT as BREADTH_HISTORY_ENDPOINT, REFERENCE as BREADTH_HISTORY_REFERENCE


@dataclass(frozen=True)
class SourceDefinition:
    family: str
    name: str
    kind: str
    version: str
    capabilities: tuple[str, ...]
    references: tuple[str, ...]
    minimum_interval: int


SOURCES = {
    'review_kpl_new_highs': SourceDefinition('review_kpl_new_highs', '开盘啦百日新高', 'market', 'kpl-new-highs-w28-v1',
        ('review.new_highs','review.new_high_history'), ('https://apphis.longhuvip.com/w1/api/index.php',), 86400),
    BREADTH_HISTORY: SourceDefinition(BREADTH_HISTORY, BREADTH_HISTORY_NAME, 'market', BREADTH_HISTORY_VERSION,
        (BREADTH_HISTORY_CAPABILITY,), (BREADTH_HISTORY_REFERENCE, BREADTH_HISTORY_ENDPOINT), 900),
    'review_tencent_indices': SourceDefinition('review_tencent_indices', '每日复盘 · 腾讯近期指数分时', 'market', 'review-tencent-m1-v1',
        ('review.intraday',), ('https://gu.qq.com/', 'https://ifzq.gtimg.cn/appstock/app/kline/mkline'), 21600),
    'review_sina_indices': SourceDefinition('review_sina_indices', '每日复盘 · 新浪近期指数分时', 'market', 'review-sina-m1-v1',
        ('review.intraday',), ('https://finance.sina.com.cn/', 'https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData'), 21600),
    'review_cls_news': SourceDefinition('review_cls_news', '每日复盘 · 财联社公开电报', 'news', 'review-cls-v1',
        ('review.news',), ('https://www.cls.cn/telegraph', 'https://www.cls.cn/v1/roll/get_roll_list'), 21600),
    'eastmoney_funds': SourceDefinition('eastmoney_funds','东方财富公开资金排行','market','eastmoney-trading-v1',
        ('market.eastmoney.funds',),
        ('https://data.eastmoney.com/zjlx/detail.html?mkt=all','https://push2.eastmoney.com/api/qt/clist/get'),120),
    'eastmoney_boards': SourceDefinition('eastmoney_boards','东方财富公开板块行情与已配置成分','market','eastmoney-trading-v1',
        ('market.eastmoney.boards',),
        ('https://quote.eastmoney.com/center/boardlist.html','https://push2.eastmoney.com/api/qt/clist/get'),120),
    # Two tables on one public page: the intraday 封单表 and the per-day history
    # table. They share a host, a request budget and a batch format, but each is
    # its own identity so schedule, run log and state stay separate.
    'duanxianxia_auction': SourceDefinition('duanxianxia_auction', '短线侠公开竞价封单表（当前）', 'market', 'duanxianxia-jjlive-v1',
        ('market.duanxianxia.auction.summary', 'market.duanxianxia.auction.stocks'),
        ('https://duanxianxia.com/web/jjlive', 'https://duanxianxia.com/vendor/stockdata/datasource.json', 'https://duanxianxia.com/static/js/crypto.js?v1', 'https://duanxianxia.com/vendor/stockdata/jjlive.json', 'https://ds.duanxianxia.com/vendor/stockdata/jjlive.json'), 65),
    'duanxianxia_auction_history': SourceDefinition('duanxianxia_auction_history', '短线侠公开竞价封单表（历史）', 'market', 'duanxianxia-fengdan-history-v1',
        ('market.duanxianxia.auction.summary', 'market.duanxianxia.auction.stocks'),
        ('https://duanxianxia.com/web/jjlive', 'https://duanxianxia.com/api/getFengdanLast'), 3600),
    'eastmoney_auction': SourceDefinition('eastmoney_auction', '东方财富公开盘前竞价快照', 'market', 'eastmoney-auction-v1',
        ('market.auction.summary', 'market.auction.stocks'),
        ('https://quote.eastmoney.com/', 'https://push2.eastmoney.com/api/qt/ulist.np/get'), 60),
    'tencent_auction': SourceDefinition('tencent_auction', '腾讯网页公开盘前竞价快照', 'market', 'tencent-auction-book-v2',
        ('market.auction.summary', 'market.auction.stocks', 'market.auction.book'),
        ('https://gu.qq.com/', 'https://qt.gtimg.cn/q=sh600000'), 60),
    'mairui_funds': SourceDefinition('mairui_funds','麦蕊免费盘后资金参考','market','mairui-funds-v1',('market.funds.reference',),
        ('https://www.mairuiapi.com/hanalyse','https://www.mairuiapi.com/getlicence','https://www.mairuiapi.com/legal'),3600),
    'mairui_pools': SourceDefinition('mairui_pools','麦蕊免费涨跌停股池','market','mairui-pools-v1',('market.pools.reference',),
        ('https://www.mairuiapi.com/hsdata','https://www.mairuiapi.com/getlicence','https://www.mairuiapi.com/legal'),900),
    'ashare_sector_heat': SourceDefinition('ashare_sector_heat','AShareAPI 免费热门板块参考','market','ashare-sector-heat-v1',('market.sector.attention',),
        ('https://ashareapi.com/endpoints/','https://ashareapi.com/terms/'),300),
    'hk_gov_finance': SourceDefinition('hk_gov_finance','香港政府官方财经资讯','news','hk-finance-rss-v1',('news.public',),
        ('https://www.news.gov.hk/tc/rss/index.lin.shtml','https://www.news.gov.hk/chi/about/'),900),
    'ashare_breadth': SourceDefinition('ashare_breadth','AShareAPI 免费市场广度参考','market','ashare-breadth-v1',('market.breadth.reference',),
        ('https://ashareapi.com/docs/endpoints/changedist/','https://ashareapi.com/terms/'),300),
    'tickflow_live': SourceDefinition('tickflow_live','TickFlow 免费实时快照','market','tickflow-rest-v1',('quote.live',),
        ('https://tickflow.org/pricing/','https://docs.tickflow.org/llms.txt'),30),
    'ashare_public': SourceDefinition('ashare_public','AShareAPI 免费盘中参考','market','ashare-anonymous-v1',('quote.delayed',),
        ('https://ashareapi.com/terms/','https://ashareapi.com/pricing/','https://ashareapi.com/endpoints/'),300),
    'baostock_history': SourceDefinition('baostock_history','BaoStock 历史K线与估值','market','baostock-0.9.4-history-v1',('bar.daily','valuation.daily','corporate.factors'),
        ('https://pypi.org/project/baostock/0.9.4/',),21600),
    'baostock_fundamental': SourceDefinition('baostock_fundamental','BaoStock 财务指标与分红','market','baostock-0.9.4-fundamental-v1',('company.financial','corporate.dividends','company.basic'),
        ('https://pypi.org/project/baostock/0.9.4/',),86400),
    'baostock_classification': SourceDefinition('baostock_classification','BaoStock 行业与指数成分','catalog','baostock-0.9.4-classification-v1',('classification.industry','index.constituents'),
        ('https://pypi.org/project/baostock/0.9.4/',),86400),
    'ecb_public': SourceDefinition('ecb_public','欧洲央行参考汇率','market','ecb-fx-v1',('fx.reference',),
        ('https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html',),21600),
    'worldbank_public': SourceDefinition('worldbank_public','世界银行中国宏观与年度利率','market','worldbank-wdi-v1',('macro.annual','rates.annual'),
        ('https://datacatalog.worldbank.org/public-licenses','https://datahelpdesk.worldbank.org/knowledgebase/articles/889392'),86400),
    'ecb_news': SourceDefinition('ecb_news','欧洲央行官方资讯','news','ecb-rss-v1',('news.public',),
        ('https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html',),900),
    'fed_public': SourceDefinition('fed_public','美联储官方资讯','news','fed-rss-v1',('news.public',),
        ('https://www.federalreserve.gov/feeds/feeds.htm','https://www.federalreserve.gov/disclaimer.htm'),900),
    'cninfo_announcements': SourceDefinition('cninfo_announcements','巨潮公开公司公告','news','cninfo-announcements-v2',('news.announcement',),
        ('https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice',
         'https://www.cninfo.com.cn/new/js/app/disclosure/notice/history-notice.js?v=20260710082532'),900),
    'baostock_daily': SourceDefinition('baostock_daily', 'BaoStock 免费盘后数据', 'market', 'baostock-0.9.4-eod-v1', ('quote.eod', 'calendar.month'),
        ('https://pypi.org/project/baostock/0.9.4/', 'https://www.baostock.com/blacklist'), 21600),
    'tencent_web': SourceDefinition('tencent_web', '腾讯网页行情', 'market', 'tencent-http-v1', ('quote.l1',),
        ('https://stockapp.finance.qq.com/mstats/', 'https://akshare.akfamily.xyz/data/stock/stock.html'), FAST_FLOORS['tencent_web']),
    'tencent_market_rt': SourceDefinition('tencent_market_rt','腾讯 · 名录范围批量行情','market','tencent-market-v1',
        ('quote.market_snapshot',),('https://qt.gtimg.cn/','https://gu.qq.com/'),15),
    'szse_public': SourceDefinition('szse_public', '深交所官方日历', 'calendar', 'szse-calendar-v1', ('calendar.month',),
        ('https://www.szse.cn/api/report/exchange/onepersistenthour/monthList',), 3600),
    'cninfo': SourceDefinition('cninfo', '巨潮公开标的目录', 'catalog', 'cninfo-catalog-v1', ('instrument.catalog',),
        ('https://www.cninfo.com.cn/new/data/szse_stock.json',
         'https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice'), 3600),
}

for _family, (_name, _dataset, _url) in DIRECTION_SPECS.items():
    SOURCES[_family] = SourceDefinition(_family, _name, 'market', 'direction-public-v1',
        ('market.direction.' + _dataset,), (_url,), FAST_FLOORS.get(_family, 300))

SOURCES['direction_kpl_emotion'] = SourceDefinition('direction_kpl_emotion',
    '开盘啦市场情绪', 'market', 'direction-kpl-emotion-v1',
    ('market.direction.kpl_emotion',), ('https://apphwhq.longhuvip.com/w1/api/index.php',), 60)

SOURCES['direction_kpl_top'] = SourceDefinition('direction_kpl_top',
    '开盘啦题材前十快照', 'market', 'direction-kpl-top-v1',
    ('market.direction.kpl_top_v1',), ('https://apphwhq.longhuvip.com/w1/api/index.php',), 5)

SOURCES['direction_kpl_strength'] = SourceDefinition('direction_kpl_strength',
    '开盘啦题材强度与板块轮动', 'market', 'direction-kpl-atlas-v1',
    ('market.direction.kpl_strength','sector.atlas','sector.leaders'),
    # Local polling floor; physical-page pacing and supplier cooldown remain separate.
    ('https://apphwhq.longhuvip.com/w1/api/index.php','https://apphis.longhuvip.com/w1/api/index.php'),60)

for _family, (_name, _capabilities, _minimum_interval) in {**FUYAO_SPECS, **TDX_SPECS}.items():
    SOURCES[_family] = SourceDefinition(_family, _name, 'market',
        ('fuyao-rest-v3' if _family=='fuyao_indices' else 'fuyao-rest-v2' if _family=='fuyao_quotes' else 'fuyao-rest-v1') if _family in FUYAO_SPECS else 'tdxaidata-1.3.1-v1',
        _capabilities, (FUYAO_DOCS if _family in FUYAO_SPECS else TDX_DOCS,), _minimum_interval)

for _family, _interval in FAMILY_INTERVALS.items():
    SOURCES[_family] = SourceDefinition(_family, '新浪 · ' + _interval + '分钟观察（个人研究）', 'market',
        SOURCE_VERSION, ('bar.minute', 'bar.' + _interval), (SOURCE_REFERENCE, SOURCE_DOCUMENTATION),
        MINIMUM_INTERVALS[_family])

for _family, _feed in NEWS_FEEDS.items():
    rolling = _family in FLASH_FAMILIES
    SOURCES[_family] = SourceDefinition(_family, _feed.name, 'news', 'publisher-flash-v2' if rolling else 'publisher-rss-v1',
                                        ('news.public',), _feed.references, FLASH_INTERVAL if rolling else 900)

for _family, _spec in TUSHARE_SPECS.items():
    _kind = 'catalog' if _spec.mode == 'catalog' else 'calendar' if _spec.mode == 'calendar' else 'market'
    _capability = {'catalog': 'instrument.catalog', 'calendar': 'calendar.month', 'snapshot': 'quote.snapshot', 'daily': 'quote.eod', 'market_snapshot':'quote.market_snapshot', 'fixed_indices':'quote.indices', 'limit_snapshot':'market.limits'}.get(_spec.mode, 'reference.tushare')
    SOURCES[_family] = SourceDefinition(_family, 'Tushare · '+_spec.title, _kind, 'tushare-http-v1',
        (_capability,), (TUSHARE_API_DOCS.get(_spec.api,TUSHARE_DOCS), 'https://tushare.pro/document/1?doc_id=290'), _spec.minimum_interval)

# Fixed first-batch indexes; never route a bare 000001 by first digit.
INDEXES = (
    ('SSE', '000001', '上证指数'),
    ('SZSE', '399001', '深证成指'),
    ('SZSE', '399006', '创业板指'),
    ('SSE', '000688', '科创50'),
)
INDEX_IDENTITIES = frozenset((exchange, symbol) for exchange, symbol, _ in INDEXES)
