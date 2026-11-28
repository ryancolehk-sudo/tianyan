"""Project current, permitted articles into news topics without editing source evidence."""
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock
from uuid import UUID
import re
import unicodedata

from sqlalchemy import String, Uuid, any_, bindparam, func, select, tuple_
from sqlalchemy.dialects.postgresql import ARRAY

from app.auth import APIError
from app.evidence.common import cursor_scope, decode_cursor, encode_cursor
from app.evidence.models import EvidenceVersion as E
from app.market.flash_news import PUBLISHER_MARKS
from app.market.models import DataFetchRun, SourceSettings
from app.market.news_feeds import NEWS_FAMILIES, NEWS_FEEDS
from app.market.protocol import SHANGHAI
from app.market.review_store import permitted_provider, load_dataset
from app.models import Provider
from app.news_center.models import NewsArticle as A
from app.news_center.read import permitted, visible_rows
from app.news_center.stream_events import Report, group_reports, make_report, source_excerpt
from app.news_center.stream_priority import Priority, reading_priority
from app.provider_access import cloud_share_sql


def normalize(text):
    return unicodedata.normalize('NFKC', text or '').casefold()


class BoardNames:
    def __init__(self, directory):
        self.tree = {}
        for code, original in directory:
            name = normalize(original).strip()
            variants = {name}
            for part in re.split(r'[()]', name):
                clean = re.sub(r'(?:概念|板块|ⅱ|ii)$', '', part).strip()
                if len(clean) >= 2:
                    variants.add(clean)
            for label in variants:
                if len(label) < 2:
                    continue
                node = self.tree
                for char in label:
                    node = node.setdefault(char, {})
                node.setdefault('', set()).add(code)

    def match(self, text):
        text = normalize(text)
        codes = set()
        for index, char in enumerate(text):
            branch = self.tree.get(char)
            if not branch or char.isascii() and char.isalnum() and index and text[index-1].isascii() and text[index-1].isalnum():
                continue
            end = index + 1
            while branch:
                if '' in branch and not (text[end-1].isascii() and text[end-1].isalnum() and end < len(text)
                                         and text[end].isascii() and text[end].isalnum()):
                    codes.update(branch[''])
                if end >= len(text):
                    break
                branch = branch.get(text[end])
                end += 1
        return codes


board_names = lru_cache(maxsize=4)(BoardNames)


def story_order(story):
    stamp = story.get('published_at')
    day = stamp.astimezone(SHANGHAI).date().isoformat() if stamp else story.get('published_day')
    receipt = story.get('received_at')
    return (day or (receipt.astimezone(SHANGHAI).date().isoformat() if receipt else ''),
            stamp.timestamp() if stamp else -1, str(story.get('id') or story.get('source_url') or ''))


def update_latest(stories, codes, row):
    for code in codes:
        if code not in stories or story_order(row) > story_order(stories[code]):
            stories[code] = row


def story_columns():
    source = func.coalesce(func.nullif(func.btrim(E.payload['source_name'].astext), ''), Provider.name)
    return (A.id, E.payload['title'].astext.label('title'), E.published_at,
            E.payload['published_day'].astext.label('published_day'), E.received_at, source.label('source_name'))


@dataclass
class SourceFeatures:
    signature: tuple
    revisions: set[UUID]
    highlights: dict[UUID, Priority]
    reports: dict[UUID, Report]


@dataclass
class StreamFeatures:
    copies: dict
    hidden: set
    highlights: dict
    events: dict


_source_features = {}
_stream_lock = Lock()
_group_cache = None


def source_features(db, source, family, signature):
    old = _source_features.get(source)
    if old and old.signature == signature:
        return old
    articles = {r.latest_evidence_id: r for r in db.execute(select(A.id, A.first_received_at, A.canonical_url,
        A.latest_evidence_id).where(A.source_id == source))}
    revisions = set(articles)
    retained = revisions & old.revisions if old and old.signature[0] == family else set()
    pending = revisions - retained
    highlights = {k: v for k, v in old.highlights.items() if k in retained} if old else {}
    reports = {k: v for k, v in old.reports.items() if k in retained} if old else {}
    if pending:
        stmt = select(E.id, E.published_at, E.received_at, E.available_at, E.payload['published_day'].astext.label('published_day'),
            E.payload['title'].astext.label('title'), E.payload['summary'].astext.label('summary'),
            E.payload['publisher_highlight'].astext.label('mark')).where(E.source_id == source,
                E.kind == 'news', E.owner_id.is_(None), E.visibility == 'shared')
        if len(pending) <= 512:
            stmt = stmt.where(E.id.in_(pending))
        with db.execute(stmt.execution_options(yield_per=512)) as rows:
            for row in rows:
                if row.id not in pending:
                    continue
                priority = reading_priority(row.title, (row.summary or '')[:360], family, row.mark in PUBLISHER_MARKS.get(family, ()))
                if priority.importance != 'regular':
                    highlights[row.id] = priority
                report = make_report(articles[row.id], row, priority, family)
                if report:
                    reports[row.id] = report
    result = _source_features[source] = SourceFeatures(signature, revisions, highlights, reports)
    return result


def stream_features(db):
    global _group_cache
    from app.news_center.picks import reviewed_event_links
    checksum = func.bit_xor(func.hashtextextended(A.latest_evidence_id.cast(String), 0))
    signatures = tuple(db.execute(select(Provider.id, Provider.family, func.count(A.id), checksum)
        .select_from(A).join(Provider, Provider.id == A.source_id)
        .where(*permitted(), cloud_share_sql(), Provider.family.in_(NEWS_FAMILIES))
        .group_by(Provider.id, Provider.family).order_by(Provider.id)))
    reviewed = reviewed_event_links()
    cache_key = signatures, reviewed
    with _stream_lock:
        if _group_cache and _group_cache[0] == cache_key:
            return _group_cache[1]
        visible_sources = {s[0] for s in signatures}
        for source in _source_features.keys() - visible_sources:
            del _source_features[source]
        features = [source_features(db, source, family, (family, count, fingerprint))
                    for source, family, count, fingerprint in signatures]
        reports = {k: r for f in features for k, r in f.reports.items()}
        copies, hidden = group_reports(reports.values(), reviewed)
        highlights = {k: v for f in features for k, v in f.highlights.items() if k not in hidden}
        events = {key: (reports[key], *(reports[rev] for _, rev in peers)) for key, peers in copies.items()}
        result = StreamFeatures(copies, hidden, highlights, events)
        _group_cache = cache_key, result
        return result


def stream_index(db, after, batch_size, revisions, exclude=False):
    access = (*permitted(), cloud_share_sql(), Provider.family.in_(NEWS_FAMILIES))
    stmt = select(A.id, A.first_received_at, A.latest_evidence_id, A.source_id).join(
        Provider, Provider.id == A.source_id).where(*access)
    if revisions is not None:
        keep = A.latest_evidence_id == any_(bindparam('ranked_revisions', list(revisions), type_=ARRAY(Uuid())))
        stmt = stmt.where(~keep if exclude else keep)
    if after:
        stmt = stmt.where(tuple_(A.first_received_at, A.id) < tuple_(*after))
    stmt = stmt.order_by(A.first_received_at.desc(), A.id.desc())
    # Sort only the lightweight article index. Reading the global evidence table
    # by primary key keeps unrelated market history out of the stream query.
    with db.execute(stmt.execution_options(yield_per=batch_size)) as index:
        for batch in index.partitions(batch_size):
            ids = [row.latest_evidence_id for row in batch]
            revisions = db.execute(select(E.id, E.source_id, E.published_at, E.received_at,
                E.payload['title'].astext.label('title'), E.payload['published_day'].astext.label('published_day'),
                E.payload['source_name'].astext.label('source_name'), E.payload['publisher_highlight'].astext.label('mark'),
                func.left(E.payload['summary'].astext, 360).label('lead')).where(
                    E.id.in_(ids), E.kind == 'news', E.owner_id.is_(None), E.visibility == 'shared'))
            evidence = {row.id: row for row in revisions}
            sources = {row.id: row for row in db.execute(select(Provider.id, Provider.name, Provider.family).where(
                *access, Provider.id.in_({r.source_id for r in batch})))}
            for article in batch:
                revision = evidence.get(article.latest_evidence_id)
                source = sources.get(article.source_id)
                if source and revision and revision.source_id == article.source_id:
                    yield article, revision, source


def stream_page(db, owner_id, cursor, limit, features, *, importance, event_type, omitted=None, recent=None, layout='frontpage'):
    filters = {k: v for k, v in {'importance': importance, 'event_type': event_type}.items() if v}
    view = {} if omitted is None else {layout: [str(key) for key in omitted]}
    scope = cursor_scope(owner_id, {'resource': 'connected-topic-stream', **filters, **view})
    after = decode_cursor(cursor, scope)
    items, token, last = [], None, None
    if importance == 'regular' and event_type:
        return dict(items=items, next_cursor=None)
    copies, hidden, highlights = features.copies, features.hidden, features.highlights
    exclude = not filters or importance == 'regular'
    if importance == 'regular':
        revisions = hidden | highlights.keys()
    elif filters:
        revisions = {key for key, p in highlights.items()
                     if (not importance or p.importance == importance) and (not event_type or p.event_type == event_type)}
    else:
        revisions = hidden or None
    articles = stream_index(db, after, limit + 1, revisions, exclude=exclude)
    related = {}
    try:
        for article, revision, source in articles:
            if omitted and (article.id in omitted or any(key in omitted for key, _ in copies.get(revision.id, ()))):
                continue
            if recent:
                start, end = recent
                if revision.published_at or not revision.published_day:
                    if not start <= (revision.published_at or revision.received_at) <= end:
                        continue
                elif not start.date().isoformat() <= revision.published_day <= end.date().isoformat():
                    continue
            marked = revision.mark in PUBLISHER_MARKS.get(source.family, ())
            priority = reading_priority(revision.title, revision.lead, source.family, marked)
            if (importance and priority.importance != importance) or (event_type and priority.event_type != event_type):
                continue
            if len(items) == limit:
                token = encode_cursor(last, scope, 'first_received_at')
                break
            last = article
            headline = dict(id=article.id, title=revision.title, published_at=revision.published_at,
                published_day=revision.published_day, received_at=revision.received_at,
                source_name=(revision.source_name or '').strip() or source.name)
            item = dict(**headline, **priority._asdict(), publisher_highlighted=marked, reports=[],
                        excerpt=source_excerpt(revision.title, revision.lead))
            items.append(item)
            if revision.id in copies:
                related[article.id] = headline, item, copies[revision.id]
    finally:
        articles.close()
    if related:
        references = [ref for _, _, peers in related.values() for ref in peers]
        stmt = visible_rows().with_only_columns(*story_columns()).where(cloud_share_sql(), E.source_id == A.source_id,
            E.kind == 'news', E.owner_id.is_(None), tuple_(A.id, A.latest_evidence_id).in_(references))
        by_id = {r['id']: dict(r) for r in db.execute(stmt).mappings()}
        for head, item, peers in related.values():
            reports = [by_id[ident] for ident, _ in peers if ident in by_id]
            if reports:
                item['reports'] = sorted([head, *reports], key=story_order, reverse=True)
    return dict(items=items, next_cursor=token)


def topic_stream(db, owner_id, cursor, limit, *, importance=None, event_type=None, layout='timeline'):
    features = stream_features(db)
    filters = dict(importance=importance, event_type=event_type)
    if layout == 'timeline':
        return stream_page(db, owner_id, cursor, limit, features, **filters)
    featured, hot_events, ranked_at = [], [], None
    if cursor:
        # The inner signed cursor binds these IDs to the owner and filters.
        prefix, sep, cursor = cursor.partition('~')
        try:
            parts = prefix.split(',') if prefix else []
            if not sep or not cursor or len(parts) > (10 if layout == 'hot' else 3):
                raise ValueError()
            omitted = tuple(UUID(hex=part) for part in parts)
        except ValueError:
            raise APIError(422, 'invalid_cursor', '列表位置已失效，请从第一页重新读取。') from None
    elif layout == 'hot':
        from app.news_center.topic_heat import ranked_events
        ranked_at = datetime.now(SHANGHAI)
        hot_events = ranked_events(db, features, ranked_at, **filters)
        omitted = tuple(item['id'] for item in hot_events)
    else:
        if importance is None:
            now = datetime.now(SHANGHAI)
            featured = stream_page(db, owner_id, None, 3, features, importance='important', event_type=event_type,
                                   recent=(now - timedelta(hours=48), now))['items']
        omitted = tuple(item['id'] for item in featured)
        if len(featured) < 3:
            featured += stream_page(db, owner_id, None, 3-len(featured), features, omitted=omitted, **filters)['items']
            omitted = tuple(item['id'] for item in featured)
    page = stream_page(db, owner_id, cursor, limit, features, omitted=omitted, layout=layout, **filters)
    if page['next_cursor']:
        page['next_cursor'] = ','.join(key.hex for key in omitted) + '~' + page['next_cursor']
    return dict(**page, featured=featured, hot_events=hot_events, ranked_at=ranked_at)


_source_cache = {}
_archive_cache = {}


def native_stories(db, matcher, cutoff):
    base = visible_rows().where(cloud_share_sql(), E.kind == 'news', E.owner_id.is_(None), E.received_at <= cutoff)
    # Hash current evidence identities so a late receipt or title correction with
    # an old clock invalidates its own source even if the newest clock did not move.
    signature = func.bit_xor(func.hashtextextended(A.latest_evidence_id.cast(String), 0))
    fingerprints = base.with_only_columns(Provider.id, Provider.name, func.count(), signature).group_by(Provider.id, Provider.name)
    result = {}
    for ident, provider_name, count, checksum in db.execute(fingerprints):
        key = (provider_name, count, checksum, matcher)
        cached = _source_cache.get(ident)
        if cached is None or cached[0] != key:
            stories = {}
            stmt = base.where(Provider.id == ident).with_only_columns(*story_columns(),
                E.payload['summary'].astext.label('summary'), E.payload['industry_codes'].label('industries'),
                E.payload['board_codes'].label('boards'))
            for record in db.execute(stmt.execution_options(yield_per=512)).mappings():
                row = dict(record)
                codes = set(row.pop('industries') or []) | set(row.pop('boards') or [])
                codes.update(matcher.match(row['title'] + '\n' + (row.pop('summary') or '')))
                update_latest(stories, codes, row)
            cached = _source_cache[ident] = key, stories
        for code, row in cached[1].items():
            update_latest(result, (code,), row)
    return result


def add_archive(db, matcher, stories, cutoff):
    provider = permitted_provider(db, 'review_cls_news')
    if not provider:
        return
    keys = list(db.execute(select(E.source_key, E.id).distinct(E.source_key).where(
        E.source_id == provider.id, E.kind == 'reference', E.visibility == 'shared', E.owner_id.is_(None),
        E.received_at <= cutoff, E.source_key.like('review.manifest:news:%:all')
    ).order_by(E.source_key.desc(), E.received_at.desc(), E.id.desc()).limit(30)))
    cache_key = (matcher, tuple(ident for _, ident in keys))
    archived = _archive_cache.get(cache_key)
    if archived is None:
        archived = {}
        for key, _ in keys:
            day = date.fromisoformat(key.split(':')[2])
            archive = load_dataset(db, 'news', day, as_of_time=cutoff)
            for row in archive['rows'] if archive['state'] == 'complete' else ():
                ident, title = row.get('id'), row.get('title')
                if not isinstance(ident, str) or not ident.isdecimal() or not title or row.get('source_url') != 'https://www.cls.cn/detail/'+ident:
                    continue
                try:
                    stamp = datetime.fromisoformat(row['published_at'])
                except (KeyError, ValueError, TypeError):
                    continue
                if stamp.tzinfo and stamp <= cutoff:
                    codes = matcher.match(title + '\n' + (row.get('summary') or ''))
                    if codes:
                        archived[ident] = (codes, dict(title=title, source_name=NEWS_FEEDS['cls_flash'].publisher,
                            source_url=row['source_url'], published_at=stamp))
        _archive_cache.clear()
        _archive_cache[cache_key] = archived
    indexed = set(db.scalars(visible_rows().with_only_columns(A.source_key).where(Provider.family == 'cls_flash',
        A.source_key.in_(['news:'+key for key in archived])))) if archived else set()
    for key,(codes,row) in archived.items():
        if 'news:'+key not in indexed:
            update_latest(stories, codes, row)


def source_states(db, cutoff):
    providers = db.execute(select(Provider.id, Provider.family, Provider.name, SourceSettings.interval_seconds,
        SourceSettings.paused_reason).outerjoin(SourceSettings, SourceSettings.source_id == Provider.id)
        .where(*permitted(), cloud_share_sql(), Provider.family.in_(NEWS_FAMILIES)).order_by(Provider.name))
    states = []
    for ident, family, name, interval, paused in providers:
        run = db.execute(select(DataFetchRun.status, DataFetchRun.received_at).where(DataFetchRun.source_id == ident)
            .order_by(DataFetchRun.received_at.desc(), DataFetchRun.id.desc()).limit(1)).first()
        state = 'paused' if paused else run.status if run else 'pending'
        if run and state in {'empty','complete'} and interval and (cutoff-run.received_at).total_seconds() > interval*2+60:
            state = 'stale'
        states.append(dict(family=family, name=name, status=state, interval_seconds=interval, received_at=run.received_at if run else None))
    return states
