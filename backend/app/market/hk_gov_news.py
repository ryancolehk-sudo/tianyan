"""Hong Kong government finance RSS, conservatively retained as date-only references."""
import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.evidence.schemas import NewsPayload
from app.market.protocol import MAX_BODY, SourceFailure
from app.market.public import NewsBatch, plain, xml

FAMILY = 'hk_gov_finance'
SOURCE_NAME = '香港政府新聞網'
FEED_URL = 'https://www.news.gov.hk/tc/categories/finance/html/articlelist.rss.xml'
SUBSCRIPTION_URL = 'https://www.news.gov.hk/tc/rss/index.lin.shtml'
TERMS_URL = 'https://www.news.gov.hk/chi/about/'
ARTICLE_HOSTS = frozenset({'www.news.gov.hk'})
MINIMUM_INTERVAL = 900  # Project budget, not a publisher quota or an automatic enablement.
TIMESTAMP_PRECISION = 'day'
MAX_ITEMS = 300
HONG_KONG = ZoneInfo('Asia/Hong_Kong')

ISO_TIME = re.compile(
    r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?'
    r'(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)', re.ASCII
)
RSS_TIME = re.compile(
    r'(?:[A-Za-z]{3},\s+)?\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+'
    r'\d{2}:\d{2}(?::\d{2})?\s+(?:[+-](?:[01]\d|2[0-3])[0-5]\d|GMT|UTC|UT)', re.ASCII
)


def _field(item, name, *, required=False):
    nodes = item.findall(name)
    if len(nodes) > 1 or (required and not nodes):
        raise ValueError('missing or ambiguous RSS field')
    return nodes[0] if nodes else None


def _title(node):
    value = plain(''.join(node.itertext()), MAX_BODY)
    value = ''.join(char for char in value if unicodedata.category(char) not in {'Cc', 'Cf', 'Cs'})
    value = re.sub(r'\s+', ' ', value).strip()[:200]
    if not value:
        raise ValueError('empty title')
    return value  # Keep Traditional Chinese; no translation or generated text.


def _link(node):
    if len(node):
        raise ValueError('nested link markup')
    value = (node.text or '').strip()
    if not value or len(value) > 2000 or '\\' in value or any(
        char.isspace() or unicodedata.category(char).startswith('C') for char in value
    ):
        raise ValueError('invalid link text')
    parts = urlsplit(value)
    if (parts.scheme != 'https' or parts.hostname not in ARTICLE_HOSTS
            or parts.username is not None or parts.password is not None
            or parts.port not in {None, 443} or parts.fragment
            or parts.netloc.lower() not in {parts.hostname, f'{parts.hostname}:443'}):
        raise ValueError('link outside publisher scope')
    return value


def _published_day(item, received_at):
    node = _field(item, 'pubDate')
    if node is None:
        return None
    if len(node):
        raise ValueError('nested publication markup')
    value = (node.text or '').strip()
    if not value:
        return None
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value, re.ASCII):
        day = date.fromisoformat(value)
    else:
        if ISO_TIME.fullmatch(value):
            if value.endswith('-00:00'):
                raise ValueError('unknown publication offset')
            published = datetime.fromisoformat(value)
        elif RSS_TIME.fullmatch(value):
            published = parsedate_to_datetime(value)
        else:
            raise ValueError('unrecognized publication date or timezone')
        if published.utcoffset() is None:
            raise ValueError('unknown publication timezone')
        day = published.astimezone(HONG_KONG).date()
    if day > received_at.astimezone(HONG_KONG).date():
        raise ValueError('future publication day')
    # Clock syntax does not establish publisher precision. Even non-midnight
    # values remain date-only until the host verifies a stronger source contract.
    return day


def parse_hk_gov_news(family: str, body: bytes, received_at: datetime) -> NewsBatch:
    """Pure normalization only; no transport, database or registration side effects."""
    if family != FAMILY:
        raise SourceFailure('capability_not_implemented')
    if not isinstance(received_at, datetime) or received_at.utcoffset() is None:
        raise SourceFailure('invalid_received_time')
    try:
        # NUL-bearing UTF-16/32 must not evade the shared ASCII DTD/ENTITY scan.
        if not isinstance(body, bytes) or len(body) > MAX_BODY or b'\0' in body:
            raise ValueError('unsupported XML envelope')
        root = xml(body)
        channels = root.findall('channel')
        if root.tag != 'rss' or root.get('version') != '2.0' or len(channels) != 1:
            raise ValueError('RSS 2.0 channel required')
        items = channels[0].findall('item')
        all_items = [node for node in root.iter() if node.tag.rsplit('}', 1)[-1] == 'item']
        if (len(root.findall('.//channel')) != 1 or len(items) > MAX_ITEMS
                or len(all_items) != len(items)):
            raise ValueError('RSS item structure or budget')
        batch = NewsBatch(expected=len(items))
        seen = set()
        for item in items:
            try:
                title = _title(_field(item, 'title', required=True))
                link = _link(_field(item, 'link', required=True))
                day = _published_day(item, received_at)
                key = hashlib.sha256(link.encode('utf-8')).hexdigest()
                if key in seen:
                    batch.rejected += 1
                    continue
                payload = NewsPayload(title=title, summary=None, source_url=link,
                                      source_name=SOURCE_NAME, published_day=day)
                seen.add(key)
                batch.records.append((key, payload, None))
            except (ValueError, TypeError, OverflowError):
                batch.rejected += 1
        return batch
    except (ValueError, TypeError, LookupError, ET.ParseError, OverflowError):
        raise SourceFailure('schema_changed') from None
