"""Public publisher feeds and open datasets. No cookies, keys or full-text crawl."""
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

from app.evidence.schemas import NewsPayload, ReferencePayload
from app.market.protocol import MAX_BODY, SHANGHAI, Response, SourceFailure, fetch_https
from app.market.reference import ReferenceBatch
from app.market.news_feeds import NEWS_FEEDS

URLS = {
    **{family: feed.url for family, feed in NEWS_FEEDS.items()},
    'hk_gov_finance':'https://www.news.gov.hk/tc/categories/finance/html/articlelist.rss.xml',
    'ecb_public':'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml',
    'ecb_news':'https://www.ecb.europa.eu/rss/press.html',
    'worldbank_public':'https://api.worldbank.org/v2/country/CHN/indicator/NY.GDP.MKTP.KD.ZG;FP.CPI.TOTL.ZG;SL.UEM.TOTL.ZS;FR.INR.LEND;FR.INR.RINR?source=2&date=2020:2026&format=json&per_page=100',
    'fed_public':'https://www.federalreserve.gov/feeds/press_all.xml',
    'nbs_public':'https://www.stats.gov.cn/sj/zxfb/',
    'cninfo_announcements':'https://www.cninfo.com.cn/new/hisAnnouncement/query',
}
NAMES = {'ecb_news':'欧洲中央银行','fed_public':'美国联邦储备委员会','nbs_public':'国家统计局','cninfo_announcements':'巨潮资讯公开公告'}


class PlainText(HTMLParser):
    def __init__(self):super().__init__();self.parts=[]
    def handle_data(self,data):self.parts.append(data)


def plain(value, maximum=200):
    parser=PlainText();parser.feed(str(value or ''))
    return re.sub(r'\s+',' ',''.join(parser.parts)).strip()[:maximum]


@dataclass
class NewsBatch:
    records: list[tuple[str,NewsPayload,datetime | None]] = field(default_factory=list)
    rejected: int = 0
    expected: int = 0
    requests: int = 1
    # Collection metadata is deliberately outside immutable NewsPayload hashes.
    coverage: dict = field(default_factory=dict)
    partial: bool = False
    duplicates: int = 0


CNINFO_MARKETS = ('sh', 'sz', 'bj')
CNINFO_PAGE_SIZE = 30
CNINFO_MAX_PAGES = 5
CNINFO_SECONDS = 15
CNINFO_REQUEST_GAP = 0.2


def cninfo_request(day, market, page):
    """The publisher's UI maps all three plates through column=szse."""
    return urlencode({'pageNum': str(page), 'pageSize': str(CNINFO_PAGE_SIZE), 'tabName': 'fulltext',
        'column': 'szse', 'plate': market, 'stock': '', 'searchkey': '',
        'seDate': str(day - timedelta(days=7)) + '~' + str(day),
        'sortName': 'pubDate', 'sortType': 'desc', 'isHLtitle': 'false'}).encode()


def cninfo_page(body, page):
    """Production pages require explicit, internally consistent coverage fields."""
    try:
        raw = json.loads(body)
        if not isinstance(raw, dict):
            raise ValueError('announcement envelope')
        total, more, items = raw['totalAnnouncement'], raw['hasMore'], raw['announcements']
        if type(total) is not int or not 0 <= total <= 10_000_000 or type(more) is not bool:
            raise ValueError('announcement coverage')
        if items is None and total == 0:
            items = []
        if not isinstance(items, list) or len(items) > CNINFO_PAGE_SIZE:
            raise ValueError('announcement page')
        if (more != (page * CNINFO_PAGE_SIZE < total)
                or total < (page - 1) * CNINFO_PAGE_SIZE + len(items)
                or (more and len(items) != CNINFO_PAGE_SIZE)
                or (not items and total)):
            raise ValueError('announcement page coverage')
        return items, total, more
    except (KeyError, TypeError, ValueError):
        raise SourceFailure('schema_changed') from None


def fetch_cninfo_batch(*, clock=None, reserve=None):
    """Bounded sequential SH/SZ/BJ metadata collection; never crawl PDF contents.

    The whole run shares a 15-second / 2-MB budget. A failed market or a page
    cap never claims complete coverage. Partial verified pages remain useful.
    """
    clock = clock or datetime.now(UTC)
    if clock.utcoffset() is None:
        raise SourceFailure('schema_changed')
    day = clock.astimezone(SHANGHAI).date()
    coverage = {'kind': 'cninfo_markets', 'window_start': str(day - timedelta(days=7)),
        'window_end': str(day), 'page_size': CNINFO_PAGE_SIZE, 'max_pages_per_market': CNINFO_MAX_PAGES,
        'markets': [{'market': market, 'requests': 0, 'total': None, 'returned': 0, 'rejected': 0,
                     'duplicates': 0, 'has_more': None, 'status': 'not_attempted', 'reason': None}
                    for market in CNINFO_MARKETS]}
    batch = NewsBatch(requests=0, coverage=coverage)
    start = time.monotonic()
    bodies, seen = [], set()
    size = 0
    last_response = None
    stopped = None
    first_failure = None
    stop_reasons = {'permission_missing', 'rate_limited', 'endpoint_not_allowed', 'upstream_redirect',
                    'unsafe_upstream_response', 'response_too_large', 'schema_changed', 'collection_disabled',
                    'source_disabled', 'configuration_changed', 'source_paused', 'source_not_configured', 'lease_expired'}
    for market in coverage['markets']:
        if stopped:
            market.update(status='partial', reason=stopped)
            batch.partial = True
            continue
        for page in range(1, CNINFO_MAX_PAGES + 1):
            remaining = CNINFO_SECONDS - (time.monotonic() - start)
            if batch.requests and remaining <= CNINFO_REQUEST_GAP:
                remaining = 0
            elif batch.requests:
                time.sleep(CNINFO_REQUEST_GAP)
                remaining = CNINFO_SECONDS - (time.monotonic() - start)
            if remaining <= 0:
                stopped = 'time_budget_exhausted'
                market.update(status='partial', reason=stopped)
                batch.partial = True
                break
            separator = 1 if bodies else 0
            remaining_bytes = MAX_BODY - size - separator
            if remaining_bytes <= 0:
                stopped = 'response_too_large'
                market.update(status='partial', reason=stopped)
                batch.partial = True
                break
            try:
                if reserve is not None and reserve() is False:
                    raise SourceFailure('rate_limited')
                remaining = CNINFO_SECONDS - (time.monotonic() - start)
                if remaining <= 0:
                    raise SourceFailure('timeout')
                batch.requests += 1
                market['requests'] += 1
                response = fetch_https(URLS['cninfo_announcements'], timeout=min(remaining, CNINFO_SECONDS),
                                       data=cninfo_request(day, market['market'], page), max_body=remaining_bytes)
                if time.monotonic() - start > CNINFO_SECONDS:
                    raise SourceFailure('timeout')
                if len(response.body) + separator + size > MAX_BODY:
                    raise SourceFailure('response_too_large')
                # Account every received envelope, including a rejected schema.
                size += len(response.body) + separator
                bodies.append(response.body)
                last_response = response
                items, total, more = cninfo_page(response.body, page)
                parsed = parse_public('cninfo_announcements', json.dumps({'announcements': items}).encode(),
                                      response.received_at)
            except SourceFailure as error:
                first_failure = first_failure or error
                if type(error.retry_after) is int and error.retry_after > 0:
                    coverage['retry_after'] = min(86400, error.retry_after)
                market.update(status='partial' if market['returned'] else 'error', reason=error.reason)
                batch.partial = True
                if error.reason in stop_reasons or time.monotonic() - start >= CNINFO_SECONDS:
                    first_failure = error
                    stopped = error.reason
                break
            old_total = market['total']
            market.update(total=total, has_more=more)
            if old_total is not None and old_total != total:
                market.update(reason='snapshot_changed')
                batch.partial = True
            market['returned'] += len(items)
            market['rejected'] += parsed.rejected
            batch.expected += parsed.expected
            batch.rejected += parsed.rejected
            for key, payload, published in parsed.records:
                identity = (key, payload.model_dump_json(), published.isoformat() if published else None)
                if identity in seen:
                    batch.duplicates += 1
                    market['duplicates'] += 1
                else:
                    seen.add(identity)
                    batch.records.append((key, payload, published))
            if parsed.rejected:
                batch.partial = True
                market['reason'] = market['reason'] or 'rows_rejected'
            if page == CNINFO_MAX_PAGES:
                market.update(status='partial', reason='page_limit_reached')
                batch.partial = True
                break
            if not more:
                market['status'] = 'partial' if market['reason'] else 'complete'
                break
    coverage.update(partial=batch.partial, request_count=batch.requests, received_bytes=size)
    # No verified page (including an explicitly empty page) is a failed run,
    # rather than an invented successful empty response.
    verified = any(market['total'] is not None for market in coverage['markets'])
    if not verified:
        failure = first_failure or SourceFailure('timeout')
        failure.request_count = batch.requests
        failure.coverage = coverage
        raise failure
    return Response(body=b'\n'.join(bodies), received_at=last_response.received_at,
                    elapsed_ms=round((time.monotonic() - start) * 1000), status=200,
                    request_count=batch.requests), batch


def fetch_public(family):
    url=URLS[family]
    data=None
    if family=='cninfo_announcements':
        today=datetime.now(UTC).astimezone(SHANGHAI).date()
        data=urlencode({'pageNum':'1','pageSize':'30','tabName':'fulltext','column':'szse','stock':'','searchkey':'',
            'seDate':str(today-timedelta(days=7))+'~'+str(today),'sortName':'pubDate','sortType':'desc','isHLtitle':'false'}).encode()
    return fetch_https(url,timeout=30 if family=='worldbank_public' else 15,data=data)


def xml(body):
    if len(body)>2_000_000 or b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise ValueError('unsafe XML envelope')
    return ET.fromstring(body)


def hk_finance_news(body, received_at):
    """Reuse the reviewed publisher parser; preserve the host's 100-item budget."""
    # Import at call time: the pure parser reuses NewsBatch/plain/xml above.
    from app.market.hk_gov_news import FAMILY, parse_hk_gov_news
    batch = parse_hk_gov_news(FAMILY, body, received_at)
    if batch.expected > 100:
        raise ValueError('RSS item budget')
    return NewsBatch(records=[(key, payload.model_copy(update={'category':'财经'}), published)
                             for key, payload, published in batch.records],
                     expected=batch.expected, rejected=batch.rejected, requests=batch.requests)


def publisher_news(family, body, received_at):
    """Read publisher feed metadata, never follow a feed URL or fetch an article."""
    if not isinstance(body, bytes) or b'\0' in body:
        raise ValueError('unsupported feed encoding')
    feed = NEWS_FEEDS[family]
    root = xml(body)
    rss1 = '{http://purl.org/rss/1.0/}'
    if root.tag == 'rss' and root.get('version') == '2.0' and len(root.findall('channel')) == 1:
        prefix = ''
        items = root.findall('./channel/item')
    elif root.tag == '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF' and len(root.findall(rss1 + 'channel')) == 1:
        prefix = rss1
        items = root.findall(rss1 + 'item')
    else:
        raise ValueError('unsupported feed structure')
    if len(items) > 100 or len([node for node in root.iter() if node.tag == prefix + 'item']) != len(items):
        raise ValueError('feed scope')
    batch = NewsBatch(expected=len(items))
    seen = set()
    for item in items:
        try:
            def field(name, *, required=False):
                nodes = item.findall(name)
                if len(nodes) > 1 or (required and len(nodes) != 1) or any(len(node) for node in nodes):
                    raise ValueError('ambiguous feed field')
                return (nodes[0].text or '').strip() if nodes else ''
            title = plain(field(prefix + 'title', required=True), 1001)
            link = field(prefix + 'link', required=True)
            parts = urlsplit(link)
            if (not title or len(title) > 200 or len(link) > 2000 or '\\' in link
                    or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in link)
                    or parts.scheme != 'https' or parts.hostname not in feed.article_hosts
                    or parts.netloc.lower() not in {*feed.article_hosts, *(host + ':443' for host in feed.article_hosts)}
                    or parts.username is not None or parts.password is not None or parts.port not in {None, 443} or parts.fragment):
                raise ValueError('article scope')
            raw = field('pubDate') if not prefix else field('{http://purl.org/dc/elements/1.1/}date')
            published = day = None
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
                day = date.fromisoformat(raw)
                if day > received_at.astimezone(SHANGHAI).date():
                    raise ValueError('future publication day')
            elif raw:
                published = datetime.fromisoformat(raw.replace('Z', '+00:00')) if re.match(r'^\d{4}-\d{2}-\d{2}T', raw) else parsedate_to_datetime(raw)
                if published.utcoffset() is None or published > received_at:
                    raise ValueError('publication timezone or future time')
                if feed.date_only_midnight and published.time().replace(tzinfo=None) == datetime.min.time():
                    day, published = published.date(), None
                else:
                    published = published.astimezone(UTC)
            description = plain(field(prefix + 'description'), 20001)
            # Keep only feed-provided metadata, with no article/full-text request,
            # no automatic translation, and no invented/truncated summary.
            summary = description if description and len(description) <= 2000 else None
            key = hashlib.sha256(link.encode()).hexdigest()
            if key in seen:
                raise ValueError('duplicate article')
            payload = NewsPayload(title=title, summary=summary, source_url=link, source_name=feed.publisher,
                                  category=feed.category, published_day=day)
            seen.add(key)
            batch.records.append((key, payload, published))
        except (ValueError, TypeError, OverflowError):
            batch.rejected += 1
    batch.partial = bool(batch.rejected)
    batch.coverage = {'kind': 'publisher_feed', 'scope': 'current_feed', 'returned': batch.expected,
                      'rejected': batch.rejected, 'partial': batch.partial, 'request_count': batch.requests}
    return batch


def parse_public(family,body,received_at):
    try:
        if received_at.utcoffset() is None:raise ValueError('unaware receipt')
        if family in NEWS_FEEDS:
            return publisher_news(family,body,received_at)
        if family=='hk_gov_finance':
            return hk_finance_news(body,received_at)
        if family=='ecb_public':
            root=xml(body);day=date.fromisoformat(root.find('.//{*}Cube[@time]').attrib['time'])
            if day>received_at.date():raise ValueError('future reference date')
            rows=[]
            for node in root.findall('.//{*}Cube[@currency]'):
                currency,rate=node.attrib['currency'],node.attrib['rate']
                if not re.fullmatch('[A-Z]{3}',currency) or not re.fullmatch(r'\d{1,10}(?:\.\d{1,10})?',rate) or Decimal(rate)<=0:
                    raise ValueError('invalid reference rate')
                rows.append({'date':str(day),'base':'EUR','currency':currency,'rate':rate})
            if not 1<=len(rows)<=100 or len({r['currency'] for r in rows})!=len(rows):raise ValueError('currency coverage')
            payload=ReferencePayload(dataset='fx.ecb',title='欧洲央行每日参考汇率',source_url=URLS[family],attribution='European Central Bank',
                scope='每1欧元对应的外币数量；每日参考值，不是可交易实时报价，不是人民币中间价。',observed_day=day,
                columns=[{'key':'date','label':'参考日期'},{'key':'base','label':'基准货币'},{'key':'currency','label':'对应货币'},{'key':'rate','label':'每1欧元对应数量'}],rows=rows)
            return ReferenceBatch(records=[('fx.ecb',payload,None)],expected=1,requests=1)
        if family=='worldbank_public':
            raw=json.loads(body,parse_float=Decimal)
            if not isinstance(raw,list) or len(raw)!=2 or raw[0]['pages']!=1 or not isinstance(raw[1],list) or len(raw[1])>100:
                raise ValueError('macro response/page coverage')
            rows=[]
            for item in raw[1]:
                if item['countryiso3code']!='CHN' or not re.fullmatch(r'\d{4}',item['date']) or int(item['date'])>received_at.year:
                    raise ValueError('macro scope')
                value=item['value']
                if value is not None and not isinstance(value,(int,Decimal)):raise ValueError('macro numeric value')
                rows.append({'indicator':item['indicator']['id'],'name':item['indicator']['value'],'year':item['date'],
                    'value':str(value) if value is not None else None,'unit':item.get('unit') or '%（所选增长率/失业率/年利率）'})
            payload=ReferencePayload(dataset='macro.worldbank.china',title='中国年度宏观与利率参考',source_url=URLS[family],attribution='World Bank · World Development Indicators',
                scope='2020年起年度GDP增速、CPI涨幅、失业率、贷款/实际利率；非月度发布、LPR或SHIBOR，历史值可能修订，未知保留为空。',
                columns=[{'key':k,'label':v} for k,v in [('indicator','指标代码'),('name','指标原名'),('year','年份'),('value','数值'),('unit','单位')]],rows=rows)
            return ReferenceBatch(records=[('macro.worldbank.china',payload,None)],expected=1,requests=1)
        if family in {'ecb_news','fed_public'}:
            root=xml(body);items=root.findall('.//item')
            if len(items)>300 or (not items and root.find('.//channel') is None):raise ValueError('RSS structure')
            batch=NewsBatch(expected=len(items));seen=set()
            for item in items:
                try:
                    title=plain(item.findtext('title'));link=(item.findtext('link') or '').strip()
                    if not title or urlsplit(link).scheme!='https' or not urlsplit(link).hostname or urlsplit(link).username:raise ValueError('feed title/link')
                    published=parsedate_to_datetime(item.findtext('pubDate')) if item.findtext('pubDate') else None
                    if published and published.utcoffset() is None:raise ValueError('news timezone absent')
                    published=published.astimezone(UTC) if published else None
                    if published and published>received_at+timedelta(minutes=1):raise ValueError('future news time')
                    key=hashlib.sha256(link.encode()).hexdigest()
                    if key in seen:
                        batch.rejected+=1
                        continue
                    seen.add(key)
                    payload=NewsPayload(title=title,summary=plain(item.findtext('description'),1000) or None,source_url=link,source_name=NAMES[family],category='海外')
                    batch.records.append((key,payload,published))
                except (ValueError,TypeError,OverflowError):batch.rejected+=1
            return batch
        if family=='cninfo_announcements':
            raw=json.loads(body);items=raw['announcements']
            if not isinstance(items,list) or len(items)>30:raise ValueError('announcement page')
            batch=NewsBatch(expected=len(items))
            for item in items:
                try:
                    title=plain(item['announcementTitle'],1001);ident=str(item['announcementId'])
                    org=item.get('orgId') or ''
                    if (not title or len(title)>200 or type(item['announcementId']) not in {int,str}
                            or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',ident)
                            or not isinstance(org,str) or (org and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',org))):
                        raise ValueError('announcement identity')
                    raw_time=item.get('announcementTime')
                    if raw_time is not None and type(raw_time) not in {int,float}:raise ValueError('announcement date')
                    published=datetime.fromtimestamp(raw_time/1000,UTC) if type(raw_time) in {int,float} else None
                    if published and published>received_at+timedelta(minutes=1):raise ValueError('future announcement')
                    published_day=published.astimezone(SHANGHAI).date() if published else None
                    if published and published.astimezone(SHANGHAI).time().replace(tzinfo=None)==datetime.min.time():
                        published=None  # The disclosure endpoint commonly encodes a date at midnight.
                    url='https://www.cninfo.com.cn/new/disclosure/detail?'+urlencode({'announcementId':ident,'orgId':org})
                    securities = []
                    raw_code, raw_name = item.get('secCode'), item.get('secName')
                    code = raw_code.strip() if isinstance(raw_code, str) else None
                    name = plain(raw_name, 201) if isinstance(raw_name, str) else None
                    # Retain only explicit publisher fields, never a headline-derived issuer.
                    # Malformed identity does not discard an otherwise readable announcement.
                    if ((code is None or not code or re.fullmatch(r'[0-9]{6}', code))
                            and (not name or len(name) <= 200) and (code or name)):
                        securities.append({'code': code or None, 'name': name or None})
                    batch.records.append((ident,NewsPayload(title=title,summary=None,source_url=url,category='公告',source_name=NAMES[family],published_day=published_day,securities=securities),published))
                except (ValueError,TypeError,KeyError):batch.rejected+=1
            return batch
        raise SourceFailure('capability_not_implemented')
    except SourceFailure:raise
    except (KeyError,TypeError,ValueError,AttributeError,ET.ParseError,OverflowError):raise SourceFailure('schema_changed') from None
