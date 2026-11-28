"""Identify republications in the reading view without changing the archived articles."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import blake2b
import re
from uuid import UUID

from app.market.protocol import SHANGHAI
from app.news_center.stream_priority import folded


WINDOW = timedelta(hours=24)
DATELINE = re.compile(r'^(?:财联社|新华社|中新社|中新网|同花顺(?:财经)?|东方财富(?:网|资讯)?)'
                      r'[^，。；：,;:\n]{0,16}?(?:\d{1,2}日(?:电|讯)|快讯)[，,:：\s]*')
ATTRIBUTION = re.compile(r'\s*[（(](?:来源[:：]\s*[^()（）]{1,20}|(?:新浪财经|财联社|央视新闻(?:客户端)?|'
                         r'新华社|同花顺(?:财经)?|东方财富(?:网)?|中新网))[）)]\s*[。.]?$')
WRAPPED = re.compile(r'^\s*【([^】]{1,250})】\s*')
TEMPLATE = re.compile(r'公告$|董事会决议|股东大会|每日简报|财经早报|盘前必读|要闻汇总|风险提示书')
# Retain decimal points, signs, units and direction when comparing substantive claims.
FIGURE = re.compile(r'[+−-]?\d+(?:\.\d+)?(?:%|万亿|亿|万)?(?:美元|港元|人民币|元|桶|吨|股|家|天|日|月|年)?')
STAGE = re.compile(r'否认|辟谣|不实|尚未|澄清|传闻|拟|候选|预中标|正式中标|终止|撤销|取消|'
                   r'解除封锁|恢复供应|恢复生产|停产|复产|上调|下调|更正|修正|撤回|作废|'
                   r'未签[订署]|未中标|解除合同|合同变更|补充协议')
ACTION = re.compile(r'签订|签署|中标|降费|调低|降低|上调|下调|投建|投产|复牌|停牌|停产|复产|'
                    r'封锁|减产|增产|获批|发布|宣布|通过|推出|启动|发射|发行|收购|交付|完成|实施')
QUANTITY = re.compile(r'[+−-]?\d+(?:\.\d+)?(?:万亿|亿|万|千)?(美元|港元|欧元|人民币|元|吨|桶|股|台|套|%)')
TICKER = re.compile(r'\(\d{6}\.(?:sh|sz|bj)\)', re.I)
PUNCT = re.compile(r'[^\w.%+−-]|_', re.UNICODE)


def compact(text):
    return PUNCT.sub('', text.casefold()).strip('.')


def digest(text):
    return blake2b(text.encode(), digest_size=16).digest()


def wire_body(text, normalized_title):
    raw = (text or '').strip()
    wrapped = WRAPPED.match(raw)
    if wrapped and compact(folded(wrapped[1])) == normalized_title:
        raw = raw[wrapped.end():]
    return ATTRIBUTION.sub('', DATELINE.sub('', raw)).strip()


def source_excerpt(title, lead):
    """Preview the source's words, retaining its original script and numeric precision."""
    heading = compact(folded(title))
    body = ' '.join(wire_body(lead, heading).split())
    if not body or compact(folded(body)) == heading:
        return None
    if len(body) <= 180:
        return body
    sentences = list(re.finditer(r'[。！？]', body[:180]))
    if sentences and sentences[-1].end() >= 45:
        return body[:sentences[-1].end()]
    return body[:179].rstrip() + '…'


@dataclass(frozen=True, slots=True)
class CopyText:
    title: str
    body: bytes
    opening: bytes
    body_facts: tuple
    lookup: tuple
    claim: str = ''
    quantities: tuple = ()
    stages: frozenset = frozenset()


def copy_text(family, title, summary, url):
    heading = folded(title)
    title = compact(heading)
    # A filing title can be shared by many different issuers. Only an identical
    # original document and heading establish a copy for a templated notice.
    if family == 'cninfo_announcements' or TEMPLATE.search(title):
        return CopyText(title, b'', b'', (), (('document', digest(url + '\0' + title)),)) if url else None
    raw = wire_body(folded(summary), title)
    body = compact(raw)
    if body == title:
        body = ''
    keys = []
    if len(title) >= 12 and len(set(title)) >= 8:
        keys.append(('title', digest(title)))
    claim = ''
    if len(title) >= 14 and len(set(title)) >= 9 and ACTION.search(title):
        # An attributed, specific event headline can survive a rewritten excerpt.
        claim = compact(TICKER.sub('', heading)).replace('签署', '签订')
        keys.append(('claim', digest(claim)))
    body_hash = digest(body) if body else b''
    if len(body) >= 38 and len(set(body)) >= 12:
        keys.append(('body', body_hash))
    if not keys:
        return None
    first = compact(re.split(r'[。！？；\n]', raw, maxsplit=1)[0])
    opening = digest(first) if len(first) >= 32 else b''
    facts = (tuple(FIGURE.findall(body)), tuple(STAGE.findall(body))) if opening else ()
    quantities = defaultdict(set)
    if claim:
        for match in QUANTITY.finditer(body):
            quantities[match[1]].add(match[0])
    return CopyText(title, body_hash, opening, facts, tuple(keys), claim,
                    tuple((unit, frozenset(values)) for unit, values in quantities.items()), frozenset(STAGE.findall(body)))


def same_copy(left, right):
    if left.claim and left.claim == right.claim and left.stages == right.stages:
        right_values = dict(right.quantities)
        if all(unit not in right_values or values == right_values[unit] for unit, values in left.quantities):
            return True
    if left.title == right.title:
        return (not left.body or not right.body or left.body == right.body or
                bool(left.opening and left.opening == right.opening and left.opening in (left.body, right.body)
                     and left.body_facts == right.body_facts))
    # A copied dispatch can have an edited headline. Different figures or
    # event stages in the new headline can instead be a material update.
    if not left.body or left.body != right.body or STAGE.findall(left.title) != STAGE.findall(right.title):
        return False
    a, b = FIGURE.findall(left.title), FIGURE.findall(right.title)
    if a and b and a != b:
        return False
    return any(left.title[i:i+4] in right.title for i in range(len(left.title)-3))


@dataclass(frozen=True, slots=True)
class Report:
    id: UUID
    revision: UUID
    time: datetime
    day: date
    timed: bool
    received: datetime
    rank: tuple
    text: CopyText
    family: str
    known_at: datetime


def make_report(article, evidence, priority, family):
    signature = copy_text(family, evidence.title, evidence.summary, article.canonical_url)
    if not signature:
        return None
    published = evidence.published_at
    day = (published.astimezone(SHANGHAI).date() if published else
           date.fromisoformat(evidence.published_day) if evidence.published_day else article.first_received_at.astimezone(SHANGHAI).date())
    when = published or datetime.combine(day, datetime.min.time(), SHANGHAI)
    rank = ({'regular': 0, 'watch': 1, 'important': 2}[priority.importance], bool(priority.event_type))
    known = max(t for t in (article.first_received_at, getattr(evidence, 'received_at', None),
                            getattr(evidence, 'available_at', None)) if t is not None)
    return Report(article.id, evidence.id, when, day, bool(published), article.first_received_at, rank, signature, family, known)


def can_join(report, group):
    for other in group:
        in_window = (abs(report.time - other.time) <= WINDOW if report.timed and other.timed else report.day == other.day)
        if not in_window or not same_copy(report.text, other.text):
            return False
    return True


def group_reports(reports, reviewed=()):
    """Find copies before filtering and pagination. Every pair in a group must agree."""
    buckets = defaultdict(list)
    groups = []
    for report in sorted(reports, key=lambda item: (item.day, item.time, item.id)):
        possible = set()
        for key in report.text.lookup:
            for day in (report.day, report.day - timedelta(days=1)):
                possible.update(buckets.get((key, day), ()))
        index = next((i for i in sorted(possible) if can_join(report, groups[i])), None)
        if index is None:
            index = len(groups)
            groups.append([])
        groups[index].append(report)
        for key in report.text.lookup:
            candidates = buckets[key, report.day]
            if index not in candidates:
                candidates.append(index)
    # An editor can link paraphrases after verifying the underlying evidence. Only
    # current, visible revisions participate; a corrected anchor voids that link.
    if reviewed:
        owner = {report.revision: i for i, group in enumerate(groups) for report in group}
        for anchor, peers in reviewed:
            if anchor not in owner:
                continue
            target = owner[anchor]
            for key in peers:
                index = owner.get(key, target)
                if index != target:
                    for report in groups[index]:
                        owner[report.revision] = target
                    groups[target].extend(groups[index])
                    groups[index].clear()
    anchors = {anchor for anchor, _ in reviewed}
    copies, hidden = {}, set()
    for group in groups:
        if len(group) < 2:
            continue
        primary = max(group, key=lambda r: (r.rank, r.revision in anchors, r.time, r.received, r.id))
        others = [(r.id, r.revision) for r in group if r is not primary]
        copies[primary.revision] = others
        hidden.update(revision for _, revision in others)
    return copies, hidden
