"""Publisher rolling lists and the 2026-11-20 Eastmoney level-two directory.

Directory source: https://quote.eastmoney.com/center/api/sidemenu_new.json
Only type=2, flag=2 entries are included. Store matches with each receipt.
"""
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from app.evidence.schemas import NewsPayload
from app.market.news_feeds import NEWS_FEEDS
from app.market.protocol import SHANGHAI
from app.market.public import NewsBatch, plain

INDUSTRIES = {row["code"]: row["name"] for row in json.loads(
    Path(__file__).with_name("flash_industries.json").read_text(encoding="utf-8"))}

# Both publishers use these native values to render a red headline.
PUBLISHER_MARKS = {
    'cls_flash': {'level:A', 'level:B'},
    'eastmoney_flash': {'titleColor:2', 'titleColor:3'},
}


def industry_codes(title, tags=()):
    return [code for code, name in INDUSTRIES.items() if code in tags or name in title]


def flash_rows(family, obj):
    if family == 'ths_flash' and str(obj.get('code')) == '200':
        rows = obj['data']['list']
    elif family == 'cls_flash' and str(obj.get('errno')) == '0':
        rows = obj['data']['roll_data']
    elif family == 'eastmoney_flash' and str(obj.get('code')) == '1':
        rows = obj['data']['fastNewsList']
    else:
        raise ValueError('invalid feed response')
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('invalid feed size')
    return rows


def parse_flash(family, body, received_at):
    feed = NEWS_FEEDS[family]
    rows = flash_rows(family, json.loads(body))
    batch = NewsBatch(expected=len(rows))
    for row in rows:
        try:
            board_tags = set()
            if family == 'eastmoney_flash':
                key = str(row['code'])
                link = f'https://finance.eastmoney.com/a/{key}.html'
                stamp = datetime.strptime(row['showTime'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=SHANGHAI)
                summary = row.get('summary')
                board_tags = {tag[3:] for tag in row.get('stockList') or [] if isinstance(tag, str) and tag.startswith('90.BK')}
            else:
                key = str(row['id'])
                stamp = datetime.fromtimestamp(int(row['ctime']), UTC)
                if family == 'ths_flash':
                    link, summary = row['url'], row.get('digest')
                else:
                    link = f'https://www.cls.cn/detail/{key}'
                    summary = row.get('brief') or row.get('content')
            title = plain(row.get('title') or summary, 200)
            if (not re.fullmatch(r'[0-9]{1,30}', key) or not title or stamp > received_at
                    or urlsplit(link).scheme != 'https' or urlsplit(link).netloc not in feed.article_hosts):
                raise ValueError('invalid news item')
            codes = sorted(tag for tag in board_tags if re.fullmatch(r'BK\d{4}', tag))
            mark = f"level:{row.get('level')}" if family == 'cls_flash' else f"titleColor:{row.get('titleColor')}"
            extra = {'board_codes': codes} if codes else {}
            if mark in PUBLISHER_MARKS.get(family, ()):
                extra['publisher_highlight'] = mark
            payload = NewsPayload(title=title, source_url=link, source_name=feed.publisher,
                                  summary=plain(summary, 2000) or None, category='财经',
                                  industry_codes=industry_codes(title, board_tags), **extra)
            batch.records.append((key, payload, stamp))
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            batch.rejected += 1
    batch.partial = bool(batch.rejected)
    batch.coverage = dict(scope='rolling_list', returned=batch.expected, rejected=batch.rejected, partial=batch.partial)
    return batch
