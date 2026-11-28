"""Rank events by the attention visible in the local news archive at each observation."""
from bisect import bisect_right
from datetime import timedelta

from sqlalchemy import func, select

from app.evidence.models import EvidenceVersion as E
from app.market.flash_news import PUBLISHER_MARKS
from app.market.models import DataFetchRun as Run, SourceSettings
from app.market.news_feeds import news_metadata
from app.models import Provider
from app.news_center.models import NewsArticle as A
from app.news_center.read import permitted, visible_rows
from app.news_center.stream_events import source_excerpt
from app.news_center.stream_priority import Priority
from app.provider_access import cloud_share_sql


HOUR = timedelta(hours=1)
WINDOW = 48 * HOUR
REGULAR = Priority('regular', None, None)


def participants(reports, at):
    latest = {}
    for report in reports:
        if report.timed and at - WINDOW < report.time <= at and report.known_at <= at:
            name = news_metadata(report.family)['publisher']
            if name not in latest or latest[name] < report.time:
                latest[name] = report.time
    return latest


def heat_index(latest, at):
    return sum(10 * 0.5 ** ((at - stamp) / (24 * HOUR)) for stamp in latest.values())


def receipt_history(db, families, at):
    """A complete or empty news poll establishes coverage even with no new article."""
    settings = db.execute(select(Provider.id, Provider.family, SourceSettings.interval_seconds)
        .join(SourceSettings, SourceSettings.source_id == Provider.id)
        .where(*permitted(), cloud_share_sql(), Provider.family.in_(families))).all()
    coverage = {}
    for ident, family, interval in settings:
        grace = timedelta(seconds=max(600, 3 * interval))
        stamps = list(db.scalars(select(Run.received_at).where(Run.source_id == ident,
            Run.capability.in_(('news.public', 'news.announcement')), Run.status.in_(('complete', 'empty')),
            Run.received_at >= at - 24 * HOUR - grace, Run.received_at <= at).order_by(Run.received_at)))
        coverage[family] = grace, stamps
    return coverage


def observed(coverage, families, at):
    for family in families:
        if family not in coverage:
            return False
        grace, stamps = coverage[family]
        idx = bisect_right(stamps, at) - 1
        if idx < 0 or at - stamps[idx] > grace:
            return False
    return True


def heat_history(reports, coverage, at):
    families = {r.family for r in reports if r.timed and at-72*HOUR < r.time <= at and r.known_at <= at}
    values = []
    first = min(max(r.time, r.known_at) for r in reports if r.timed and r.known_at <= at)
    for offset in range(24, -1, -1):
        stamp = at - offset * HOUR
        value = None
        if stamp >= first and observed(coverage, families, stamp):
            value = round(heat_index(participants(reports, stamp), stamp), 1)
        values.append(value)
    before = at - 6 * HOUR
    if values[-1] is None or not all(observed(coverage, families, at - i * HOUR) for i in range(7)):
        return values, 'unknown', None
    previous = heat_index(participants(reports, before), before)
    if not previous:
        return values, 'new', None
    change = (heat_index(participants(reports, at), at) / previous - 1) * 100
    trend = 'up' if change > 10 else 'down' if change < -10 else 'flat'
    return values, trend, round(change, 1)


def ranked_events(db, features, at, *, importance, event_type):
    from app.news_center.topic_stories import story_columns, story_order
    candidates = []
    for key, members in features.events.items():
        if not any(r.timed and at-WINDOW < r.time <= at for r in members):
            continue
        priority = features.highlights.get(key, REGULAR)
        if (importance and importance != priority.importance) or (event_type and event_type != priority.event_type):
            continue
        sources = participants(members, at)
        if len(sources) >= 2:
            candidates.append((heat_index(sources, at), max(sources.values()), key, members, priority))
    leaders = sorted(candidates, key=lambda v: v[:3], reverse=True)[:10]
    if not leaders:
        return []
    refs = {r.revision for _, _, _, members, _ in leaders for r in members}
    stmt = visible_rows().with_only_columns(*story_columns(), E.id.label('revision'), Provider.family,
        func.left(E.payload['summary'].astext, 1200).label('body'), E.payload['publisher_highlight'].astext.label('mark')
        ).where(cloud_share_sql(), E.kind == 'news', E.owner_id.is_(None), E.source_id == A.source_id,
                E.id.in_(refs), E.received_at <= at, E.available_at.is_(None) | (E.available_at <= at))
    rows = {row['revision']: row for row in db.execute(stmt).mappings()}
    coverage = receipt_history(db, {r['family'] for r in rows.values()}, at)
    result = []
    for _, _, key, members, priority in leaders:
        members = [r for r in members if r.revision in rows and r.known_at <= at and (not r.timed or r.time <= at)]
        sources = participants(members, at)
        if len(sources) < 2:
            continue
        current = [r for r in members if r.timed and at-WINDOW < r.time <= at]
        head = next((r for r in current if r.revision == key), max(current, key=lambda r: (r.rank, r.time, r.id)))
        row = rows[head.revision]
        reports = [{field: rows[r.revision][field] for field in
                    ('id','title','published_at','published_day','received_at','source_name')} for r in current]
        reports.sort(key=story_order, reverse=True)
        history, trend, change = heat_history(members, coverage, at)
        result.append(dict(**next(r for r in reports if r['id'] == head.id), **priority._asdict(),
            publisher_highlighted=row['mark'] in PUBLISHER_MARKS.get(row['family'], ()),
            reports=reports, excerpt=source_excerpt(row['title'], row['body']), heat=round(heat_index(sources, at), 1),
            sources=sorted(sources, key=lambda name: (-sources[name].timestamp(), name)),
            history=history, trend=trend, change_pct=change))
    return result
