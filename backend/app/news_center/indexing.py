"""Derived indexes only; original source payloads and their hashes stay immutable."""
import hashlib
import re
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, or_, select

from app.evidence.models import EvidenceVersion
from app.models import Provider
from app.news_center.models import NewsArticle, NewsSourceCoverage

RECIPE_VERSION = 'tianyan-news-rules-v1'
RISK = ('澄清', '辟谣', '风险提示', '退市', '立案', '行政处罚', '违规')
POLICY = ('国务院', '人民银行', '证监会', '发改委', '财政部', '降准', '降息', '货币政策', '监管政策')
INDUSTRY = ('半导体', '芯片', '机器人', '算力', '人工智能', '光伏', '新能源', '医药', '汽车', '航天',
            '数据中心', '电池', '低空', '电力', '产业', '供应链')
CATEGORY_ORDER = {'风险澄清': 0, '政策': 1, '产业': 2, '公司公告': 3, '财经': 4, '海外': 5}


def classify(payload, family):
    title = payload.get('title', '')
    if any(word in title for word in RISK):
        return '风险澄清'
    if payload.get('category') == '政策' or any(word in title for word in POLICY):
        return '政策'
    if payload.get('category') == '公告' or family == 'cninfo_announcements':
        return '公司公告'
    if payload.get('category') == '行业' or any(word in title for word in INDUSTRY):
        return '产业'
    if payload.get('category') == '公司':
        return '公司公告'
    if payload.get('category') == '海外' or family in {'ecb_news', 'fed_public'}:
        return '海外'
    return '财经'


def canonical_url(value):
    parts = urlsplit(value)
    # Strip only known analytics parameters; disclosure IDs and market selectors remain.
    query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith('utm_') and key.lower() != 'spm']
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', urlencode(sorted(query)), parts.fragment))


def duplicate_key(payload):
    title = re.sub(r'\s+', ' ', payload.get('title', '')).strip()
    summary = re.sub(r'\s+', ' ', payload.get('summary') or '').strip()
    # An identical short/generic headline alone never proves a common event.
    raw = 'text:' + title + '\0' + summary if len(summary) >= 30 else 'url:' + canonical_url(payload['source_url'])
    return hashlib.sha256(raw.encode()).hexdigest()


def index_evidence(db, evidence, provider):
    # family is deferred on Provider. Resolve it before adding a partial row,
    # otherwise its lazy SELECT can autoflush an unfinished bootstrap insert.
    values = dict(latest_evidence_id=evidence.id, updated_at=evidence.received_at,
        canonical_url=canonical_url(evidence.payload['source_url']), duplicate_key=duplicate_key(evidence.payload),
        category=classify(evidence.payload, provider.family))
    ident = uuid.uuid5(uuid.NAMESPACE_URL, f'tianyan/news/{provider.id}/{evidence.source_key}')
    article = db.get(NewsArticle, ident)
    if article is None:
        first = db.scalar(select(func.min(EvidenceVersion.received_at)).where(
            EvidenceVersion.source_id == provider.id, EvidenceVersion.source_key == evidence.source_key,
            EvidenceVersion.visibility == 'shared', EvidenceVersion.kind == 'news'))
        article = NewsArticle(id=ident, source_id=provider.id, source_key=evidence.source_key,
                              first_received_at=first or evidence.received_at)
        db.add(article)
    for name, value in values.items():
        setattr(article, name, value)
    return article


def sync_news_index(db, provider, config, batch, received_at):
    db.flush()
    keys = ['news:' + str(record[0]) for record in batch.records]
    if not keys:
        return
    ranked = select(EvidenceVersion.id, func.row_number().over(
        partition_by=EvidenceVersion.source_key, order_by=EvidenceVersion.version_number.desc()).label('n')).where(
            EvidenceVersion.kind == 'news', EvidenceVersion.visibility == 'shared',
            EvidenceVersion.source_id == provider.id, EvidenceVersion.source_key.in_(keys)).subquery()
    for evidence in db.scalars(select(EvidenceVersion).join(ranked, ranked.c.id == EvidenceVersion.id).where(ranked.c.n == 1)):
        index_evidence(db, evidence, provider)
    db.flush()


def sync_all_news(db, maximum=1000):
    """Bounded background bootstrap for news collected before this feature existed."""
    ranked = select(EvidenceVersion.id, func.row_number().over(
        partition_by=(EvidenceVersion.source_id, EvidenceVersion.source_key),
        order_by=EvidenceVersion.version_number.desc()).label('n')).where(
            EvidenceVersion.kind == 'news', EvidenceVersion.visibility == 'shared').subquery()
    stmt = select(EvidenceVersion, Provider).join(ranked, ranked.c.id == EvidenceVersion.id).join(
        Provider, Provider.id == EvidenceVersion.source_id).outerjoin(NewsArticle,
        (NewsArticle.source_id == EvidenceVersion.source_id) & (NewsArticle.source_key == EvidenceVersion.source_key)).where(
            ranked.c.n == 1, or_(NewsArticle.id.is_(None), NewsArticle.latest_evidence_id != EvidenceVersion.id))
    rows = list(db.execute(stmt.order_by(EvidenceVersion.received_at, EvidenceVersion.id).limit(maximum)))
    for evidence, provider in rows:
        index_evidence(db, evidence, provider)
    db.flush()
    return len(rows)


def store_source_coverage(db, run, coverage):
    if not coverage:
        return
    db.flush()
    current = db.get(NewsSourceCoverage, run.source_id)
    if current is None:
        current = NewsSourceCoverage(source_id=run.source_id)
        db.add(current)
    current.fetch_run_id = run.id
    current.recorded_at = run.received_at
    current.coverage = coverage
