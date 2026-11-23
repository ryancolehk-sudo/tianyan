"""Publisher-operated personal RSS feeds. Fixed endpoints, no article crawling."""
from dataclasses import dataclass


@dataclass(frozen=True)
class NewsFeed:
    name: str
    publisher: str
    region: str
    category: str
    url: str
    article_hosts: tuple[str, ...]
    references: tuple[str, ...]
    date_only_midnight: bool = False


NEWS_FEEDS = {
    'chinanews_finance': NewsFeed('中新网 · 财经', '中国新闻网', '中国与港澳', '财经',
        'https://www.chinanews.com.cn/rss/finance.xml', ('www.chinanews.com.cn',),
        ('https://www.chinanews.com.cn/rss/rss1.html', 'https://www.chinanews.com.cn/rss/')),
    'rthk_finance': NewsFeed('香港电台 · 财经', '香港电台', '中国与港澳', '财经',
        'https://rthk9.rthk.hk/rthk/news/rss/c_expressnews_cfinance.xml', ('news.rthk.hk',),
        ('https://news.rthk.hk/rthk/ch/rss.feed', 'https://www.rthk.hk/copyright/')),
    'bbc_business': NewsFeed('BBC News · 商业财经', 'BBC News', '欧洲', '财经',
        'https://feeds.bbci.co.uk/news/business/rss.xml', ('www.bbc.co.uk', 'www.bbc.com'),
        ('https://www.bbc.co.uk/news/10628494', 'https://www.bbc.co.uk/usingthebbc/terms/')),
    'bbc_world': NewsFeed('BBC News · 全球新闻', 'BBC News', '全球', '海外',
        'https://feeds.bbci.co.uk/news/world/rss.xml', ('www.bbc.co.uk', 'www.bbc.com'),
        ('https://www.bbc.co.uk/news/10628494', 'https://www.bbc.co.uk/usingthebbc/terms/')),
    'bbc_technology': NewsFeed('BBC News · 科技产业', 'BBC News', '全球', '行业',
        'https://feeds.bbci.co.uk/news/technology/rss.xml', ('www.bbc.co.uk', 'www.bbc.com'),
        ('https://www.bbc.co.uk/news/10628494', 'https://www.bbc.co.uk/usingthebbc/terms/')),
    'cnbc_finance': NewsFeed('CNBC · 金融市场', 'CNBC', '美洲', '财经',
        'https://www.cnbc.com/id/10000664/device/rss/rss.html', ('www.cnbc.com',),
        ('https://www.cnbc.com/rss-feeds/', 'https://www.cnbc.com/terms/')),
    'cnbc_world': NewsFeed('CNBC · 国际财经', 'CNBC', '全球', '财经',
        'https://www.cnbc.com/id/100727362/device/rss/rss.html', ('www.cnbc.com',),
        ('https://www.cnbc.com/rss-feeds/', 'https://www.cnbc.com/terms/')),
    'nyt_business': NewsFeed('纽约时报 · 商业', 'The New York Times', '美洲', '财经',
        'https://rss.nytimes.com/services/xml/rss/nyt/Business.xml', ('www.nytimes.com',),
        ('https://www.nytimes.com/rss',)),
    'nyt_economy': NewsFeed('纽约时报 · 经济', 'The New York Times', '全球', '财经',
        'https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml', ('www.nytimes.com',),
        ('https://www.nytimes.com/rss',)),
    'npr_business': NewsFeed('NPR · 商业经济', 'NPR', '美洲', '财经',
        'https://feeds.npr.org/1006/rss.xml', ('www.npr.org',),
        ('https://www.npr.org/rss/rss.php', 'https://www.npr.org/about-npr/179876898/terms-of-use')),
    'cna_business': NewsFeed('CNA · 亚太财经', 'CNA', '亚太', '财经',
        'https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6936', ('www.channelnewsasia.com',),
        ('https://www.channelnewsasia.com/rss', 'https://www.channelnewsasia.com/rss/rssterms')),
    'cna_world': NewsFeed('CNA · 国际新闻', 'CNA', '全球', '海外',
        'https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6311', ('www.channelnewsasia.com',),
        ('https://www.channelnewsasia.com/rss', 'https://www.channelnewsasia.com/rss/rssterms')),
    'boe_news': NewsFeed('英格兰银行 · 政策', 'Bank of England', '欧洲', '政策',
        'https://www.bankofengland.co.uk/rss/news', ('www.bankofengland.co.uk',),
        ('https://www.bankofengland.co.uk/rss/news', 'https://www.bankofengland.co.uk/legal')),
    'bis_news': NewsFeed('国际清算银行 · 发布', 'BIS', '全球', '政策',
        'https://www.bis.org/doclist/all_pressrels.rss', ('www.bis.org',),
        ('https://www.bis.org/rss', 'https://www.bis.org/about/terms-conditions'), True),
    'un_economy': NewsFeed('联合国新闻 · 经济发展', '联合国新闻', '全球', '财经',
        'https://news.un.org/feed/subscribe/zh/news/topic/economic-development/feed/rss.xml', ('news.un.org',),
        ('https://news.un.org/en/rss-feeds', 'https://www.un.org/en/about-us/terms-of-use')),
}

EXISTING_NEWS = {
    'hk_gov_finance': ('香港政府新聞網', '中国与港澳', '财经'),
    'ecb_news': ('欧洲中央银行', '欧洲', '政策'),
    'fed_public': ('美国联邦储备委员会', '美洲', '政策'),
    'cninfo_announcements': ('巨潮资讯', '中国与港澳', '公告'),
}
NEWS_FAMILIES = (*EXISTING_NEWS, *NEWS_FEEDS)


def news_metadata(family):
    if family in NEWS_FEEDS:
        feed = NEWS_FEEDS[family]
        return {'publisher': feed.publisher, 'region': feed.region, 'category': feed.category}
    if family in EXISTING_NEWS:
        publisher, region, category = EXISTING_NEWS[family]
        return {'publisher': publisher, 'region': region, 'category': category}
    return None
