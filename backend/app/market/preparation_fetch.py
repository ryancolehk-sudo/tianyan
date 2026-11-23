"""Bounded source adapters used only by explicit tomorrow-preparation jobs.

No endpoint in this module is supplied by a client. Text is source material,
never an instruction, and a download/parse failure never becomes a title body.
"""
import hashlib
import html
import io
import ipaddress
import json
import multiprocessing
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from app.market.protocol import SHANGHAI, NoRedirect, Response, SourceFailure
from app.market.review_protocol import NEWS_PAGE_SIZE

NEWS_FIELDS = ['id', 'published_at', 'modified_at', 'title', 'summary', 'text',
               'part_number', 'source_url', 'stock_codes', 'text_truncated']
ANNOUNCEMENT_FIELDS = ['id', 'published_at', 'published_day', 'title', 'ts_code',
                       'text', 'part_number', 'source_url', 'document_sha256', 'text_status']
NEWS_TEXT_LIMIT = 12_000
DOCUMENT_TEXT_LIMIT = 40_000
DOCUMENT_MAX_BYTES = 8 * 1024 * 1024
DOCUMENT_MAX_PAGES = 80
DOCUMENT_PARSE_SECONDS = 10
CNINFO_QUERY = 'https://www.cninfo.com.cn/new/hisAnnouncement/query'
CNINFO_LOOKUP = 'https://www.cninfo.com.cn/new/information/topSearch/query'


def clean(value):
    return html.unescape(re.sub(r'<[^>]+>', '', str(value or ''))).replace('\0', '').strip()


def segments(text, maximum=500):
    return [text[i:i + maximum] for i in range(0, len(text), maximum)] or ['']


def parse_news_window(response, window_start, window_end):
    """Preserve source text and a cursor; do not infer full-day coverage here."""
    try:
        raw = json.loads(response.body)
        values = (raw.get('data') or {}).get('roll_data')
        if raw.get('errno') != 0 or not isinstance(values, list) or len(values) > NEWS_PAGE_SIZE:
            raise ValueError()
        rows, stamps, seen = [], [], set()
        for value in values:
            if not isinstance(value, dict) or type(value.get('ctime')) is not int:
                raise ValueError()
            stamp = datetime.fromtimestamp(value['ctime'], SHANGHAI)
            if stamp > response.received_at:
                raise SourceFailure('future_source_time')
            ident = str(value.get('id') or '')
            if not re.fullmatch(r'\d{1,30}', ident) or ident in seen:
                raise SourceFailure('review_duplicate_row' if ident in seen else 'schema_changed')
            seen.add(ident)
            stamps.append(value['ctime'])
            if not window_start <= stamp <= window_end:
                continue
            title = clean(value.get('title') or value.get('brief') or value.get('content'))
            text = clean(value.get('content') or value.get('brief'))
            if not title:
                raise ValueError()
            modified = value.get('modified_time')
            modified_at = None
            if modified is not None:
                if type(modified) is not int:
                    raise ValueError()
                if modified > 0:
                    changed = datetime.fromtimestamp(modified, SHANGHAI)
                    if changed > response.received_at or changed < stamp:
                        raise SourceFailure('invalid_source_time')
                    modified_at = changed.isoformat()
                    if changed > window_end:
                        # A later edit cannot be reconstructed as its earlier
                        # wording merely because the original publish time fits.
                        continue
            stocks = value.get('stock_list') or []
            if not isinstance(stocks, list):
                raise ValueError()
            codes = ','.join(str(r.get('stock_id') or r.get('symbol') or r.get('code') or '')
                             for r in stocks if isinstance(r, dict))[:500]
            for number, part in enumerate(segments(text[:NEWS_TEXT_LIMIT])):
                rows.append(dict(zip(NEWS_FIELDS, [ident, stamp.isoformat(), modified_at,
                    title[:200], text[:500] or None, part or None, str(number),
                    'https://www.cls.cn/detail/' + ident, codes,
                    str(len(text) > NEWS_TEXT_LIMIT).lower()], strict=True)))
        if stamps != sorted(stamps, reverse=True):
            raise SourceFailure('news_cursor_order_changed')
        return rows, min(stamps) if stamps else None, len(values)
    except SourceFailure:
        raise
    except (TypeError, KeyError, ValueError, OverflowError, AttributeError):
        raise SourceFailure('schema_changed') from None


def stock_lookup_request(code):
    if not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', code):
        raise SourceFailure('invalid_preparation_context')
    return urllib.parse.urlencode({'keyWord': code[:6], 'maxNum': '10'}).encode()


def parse_stock_lookup(response, code):
    try:
        values = json.loads(response.body)
        if not isinstance(values, list) or len(values) > 10:
            raise ValueError()
        matched = {value['orgId'] for value in values if isinstance(value, dict) and value.get('code') == code[:6]
                   and isinstance(value.get('orgId'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value['orgId'])}
        if len(matched) != 1:
            raise SourceFailure('announcement_organization_unavailable')
        return matched.pop()
    except SourceFailure:
        raise
    except (ValueError, TypeError, KeyError):
        raise SourceFailure('schema_changed') from None


def announcement_request(code, start_day, end_day, page, org_id=None):
    if (not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', code) or not 1 <= page <= 5
            or not isinstance(org_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', org_id)):
        raise SourceFailure('invalid_preparation_context')
    plate = {'SH': 'sh', 'SZ': 'sz', 'BJ': 'bj'}[code[-2:]]
    return urllib.parse.urlencode(dict(pageNum=page, pageSize=30, tabName='fulltext',
        column='szse', plate=plate, stock=code[:6] + ',' + org_id, searchkey='',
        seDate=f'{start_day}~{end_day}', sortName='pubDate', sortType='desc',
        isHLtitle='false')).encode()


def document_url(value):
    """Accept only the publisher's canonical PDF path, never arbitrary URLs."""
    if not isinstance(value, str):
        raise SourceFailure('document_url_not_allowed')
    path = value
    if '://' in value:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or parsed.hostname != 'static.cninfo.com.cn'
                or parsed.port not in {None, 80, 443} or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise SourceFailure('document_url_not_allowed')
        path = parsed.path.lstrip('/')
    if not re.fullmatch(r'finalpage/\d{4}-\d{2}-\d{2}/[A-Za-z0-9_-]{1,100}\.pdf', path, re.I):
        raise SourceFailure('document_url_not_allowed')
    return 'https://static.cninfo.com.cn/' + path


def parse_announcements(response, code, start_day, end_day, page):
    from app.market.public import cninfo_page
    items, total, more = cninfo_page(response.body, page)
    records = []
    for item in items:
        try:
            if item['secCode'] != code[:6]:
                raise SourceFailure('announcement_scope_mismatch')
            ident, title = str(item['announcementId']), clean(item['announcementTitle'])
            raw_time = item['announcementTime']
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident) or not title or type(raw_time) not in {int, float}:
                raise ValueError()
            stamp = datetime.fromtimestamp(raw_time / 1000, UTC)
            local = stamp.astimezone(SHANGHAI)
            if stamp > response.received_at or not start_day <= local.date() <= end_day:
                raise SourceFailure('invalid_source_time')
            date_only = local.time().replace(tzinfo=None) == datetime.min.time()
            records.append(dict(id=ident, published_at=None if date_only else stamp.isoformat(),
                published_day=local.date().isoformat(), title=title[:200], ts_code=code,
                source_url=document_url(item.get('adjunctUrl'))))
        except SourceFailure:
            raise
        except (TypeError, KeyError, ValueError, OverflowError):
            raise SourceFailure('schema_changed') from None
    return records, total, more


def fetch_document(url):
    url = document_url(url)
    # The fixed hostname is re-resolved for every request; deny private/link-local
    # responses and redirects, including publisher CDN misconfiguration.
    try:
        addresses = socket.getaddrinfo('static.cninfo.com.cn', 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(r[4][0]).is_global for r in addresses):
            raise SourceFailure('document_host_not_public')
    except (OSError, ValueError):
        raise SourceFailure('transport_error') from None
    started = time.monotonic()
    request = urllib.request.Request(url, headers={'User-Agent': 'Tianyan/1.0 (private research)',
        'Accept': 'application/pdf', 'Accept-Encoding': 'identity'})
    try:
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=12) as response:
            if response.headers.get('Content-Encoding', '').lower() not in {'', 'identity'}:
                raise SourceFailure('unsafe_upstream_response')
            body = response.read(DOCUMENT_MAX_BYTES + 1)
            if len(body) > DOCUMENT_MAX_BYTES:
                raise SourceFailure('response_too_large')
            if not body.startswith(b'%PDF-'):
                raise SourceFailure('document_not_pdf')
            return Response(body, datetime.now(UTC), int((time.monotonic() - started) * 1000), response.status)
    except urllib.error.HTTPError as error:
        reason = 'permission_missing' if error.code in {401, 403} else 'rate_limited' if error.code == 429 else 'upstream_unavailable'
        retry = error.headers.get('Retry-After', '')
        raise SourceFailure(reason, retry_after=min(int(retry), 86400) if retry.isdecimal() else None) from None
    except TimeoutError:
        raise SourceFailure('timeout') from None
    except (urllib.error.URLError, OSError):
        raise SourceFailure('transport_error') from None


def _extract_pdf(body):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(body), strict=True)
    if reader.is_encrypted:
        return '', 'encrypted'
    text, truncated = '', len(reader.pages) > DOCUMENT_MAX_PAGES
    for page in reader.pages[:DOCUMENT_MAX_PAGES]:
        part = page.extract_text() or ''
        text += part.replace('\0', '') + '\n'
        if len(text) > DOCUMENT_TEXT_LIMIT:
            truncated = True
            text = text[:DOCUMENT_TEXT_LIMIT]
            break
    text = text.strip()
    return text, 'truncated' if text and truncated else 'extracted' if text else 'no_text'


def _extract_worker(connection, body):
    try:
        # Linux workers enforce memory and CPU too. Windows still has a separate
        # process, fixed input/page/text bounds and a hard parent wall deadline.
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (DOCUMENT_PARSE_SECONDS,) * 2)
        except ImportError:
            pass
        text, status = _extract_pdf(body)
    except ImportError:
        text, status = '', 'parser_unavailable'
    except Exception:
        text, status = '', 'parse_failed'
    try:
        connection.send((text, status))
    finally:
        connection.close()


def extract_pdf(body):
    if not isinstance(body, bytes) or len(body) > DOCUMENT_MAX_BYTES or not body.startswith(b'%PDF-'):
        return '', 'invalid_pdf'
    context = multiprocessing.get_context('spawn')
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_extract_worker, args=(writer, body), daemon=True)
    try:
        process.start()
        writer.close()
        if not reader.poll(DOCUMENT_PARSE_SECONDS + 2):
            return '', 'parse_timeout'
        try:
            result = reader.recv()
        except (EOFError, OSError):
            return '', 'parse_failed'
        if (not isinstance(result, tuple) or len(result) != 2 or not isinstance(result[0], str)
                or len(result[0]) > DOCUMENT_TEXT_LIMIT):
            return '', 'parse_failed'
        return result
    finally:
        reader.close()
        writer.close()
        if process.is_alive():
            process.terminate()
        if process.pid is not None:
            process.join(timeout=1)


def announcement_rows(metadata, body=None, *, text='', status='not_fetched'):
    digest = hashlib.sha256(body).hexdigest() if body is not None else None
    return [{**metadata, 'text': part or None, 'part_number': str(number),
             'document_sha256': digest, 'text_status': status}
            for number, part in enumerate(segments(text))]
