"""Fixed Beijing cutoffs; generation always uses versions received before the cutoff."""
import calendar
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select

from app.evidence.common import content_hash, lock
from app.evidence.models import EvidenceVersion, SourceVersion
from app.market.models import DataFetchRun
from app.market.news_feeds import NEWS_FAMILIES
from app.market.protocol import SHANGHAI
from app.models import Provider
from app.news_center.indexing import (
    CATEGORY_ORDER,
    RECIPE_VERSION,
    classify,
    duplicate_key,
    index_evidence,
    sync_all_news,
)
from app.news_center.models import (
    NewsArticle,
    NewsCenterState,
    NewsEdition,
    NewsEditionItem,
    NewsSourceCoverage,
)
from app.news_center.read import permitted


def at(day, hour=8, minute=0):
    return datetime.combine(day, time(hour, minute), SHANGHAI)


def last_daily_day(now):
    local = now.astimezone(SHANGHAI)
    return local.date() if local >= at(local.date()) else local.date() - timedelta(days=1)


def next_month(day):
    return date(day.year + (day.month == 12), 1 if day.month == 12 else day.month + 1, 1)


def _rank(record):
    article, evidence, provider, category, _ = record
    primary = 0 if provider.family in {'cninfo_announcements', 'ecb_news', 'fed_public', 'hk_gov_finance'} else 1
    source_clock = evidence.published_at or (
        at(date.fromisoformat(evidence.payload['published_day']), 0) if evidence.payload.get('published_day') else evidence.received_at)
    return (-source_clock.timestamp(), CATEGORY_ORDER[category], primary, str(article.id))


def _unique(records, maximum):
    selected, seen = [], set()
    for row in sorted(records, key=_rank):
        key = duplicate_key(row[1].payload)
        if key not in seen:
            selected.append(row)
            seen.add(key)
        if len(selected) == maximum:
            break
    return selected


def _coverage_at(db, start, end):
    """Freeze coverage known before the deadline, including on a later catch-up."""
    result = []
    for provider in db.scalars(select(Provider).where(Provider.family.in_(NEWS_FAMILIES)).order_by(Provider.id)):
        version = db.scalar(select(SourceVersion).where(SourceVersion.source_id == provider.id,
            SourceVersion.recorded_at < end).order_by(SourceVersion.recorded_at.desc(), SourceVersion.id.desc()).limit(1))
        descriptor = version.descriptor if version else {}
        if not descriptor.get('configured') or not descriptor.get('enabled'):
            continue
        rights = descriptor.get('rights', {})
        if descriptor.get('entitlement_status') != 'verified' or any(rights.get(key) != 'allowed' for key in ('display', 'cache')):
            continue
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.source_id == provider.id,
            DataFetchRun.received_at < end).order_by(DataFetchRun.received_at.desc(), DataFetchRun.id.desc()).limit(1))
        row = dict(source_id=str(provider.id), family=provider.family, source_version_id=str(version.id),
                   status='not_checked', fetch_run_id=None)
        if run:
            row.update(status=run.status if run.received_at >= start else 'stale', fetch_run_id=str(run.id),
                received_at=run.received_at.isoformat(), reason=run.reason, expected=run.expected,
                accepted=run.accepted, rejected=run.rejected, duplicates=run.duplicates, request_count=run.request_count)
            detail = db.get(NewsSourceCoverage, provider.id)
            if detail and detail.fetch_run_id == run.id and detail.recorded_at < end:
                row['coverage'] = detail.coverage
        result.append(row)
    return result


def _publish(db, period, day, start, end, current, records, note, partial, child_ids=None, coverage=None):
    existing = db.scalar(select(NewsEdition).where(NewsEdition.period == period, NewsEdition.day == day))
    if existing:
        return existing
    records = _unique(records, {'day': 12, 'week': 20, 'month': 30}[period])
    number = (db.scalar(select(func.max(NewsEdition.issue_number)).where(NewsEdition.period == period)) or 0) + 1
    manifest = {'recipe_version': RECIPE_VERSION, 'period': period, 'day': day.isoformat(),
        'window_start': start.isoformat(), 'window_end': end.isoformat(),
        'child_editions': child_ids or [], 'coverage': coverage or [], 'items': [dict(article_id=str(a.id), evidence_id=str(e.id),
            category=category, late=late, content_hash=e.content_hash) for a, e, _, category, late in records]}
    edition = NewsEdition(period=period, day=day, issue_number=number, window_start=start, window_end=end,
        generated_at=current, recipe_version=RECIPE_VERSION, coverage_status='partial' if partial else 'complete' if records else 'empty',
        coverage_note=note, manifest=manifest, content_hash=content_hash(manifest))
    db.add(edition)
    db.flush()
    for position, (article, evidence, _, category, late) in enumerate(records):
        db.add(NewsEditionItem(edition_id=edition.id, article_id=article.id, evidence_id=evidence.id,
                              position=position, category=category, late=late))
    db.flush()
    return edition


def _queue_ai(db, edition, current):
    """Compatibility no-op: rule editions never enqueue a pending AI feature."""
    return None


def create_daily(db, state, day, current):
    start, end = at(day - timedelta(days=1)), at(day)
    ranked = select(EvidenceVersion.id, func.row_number().over(
        partition_by=(EvidenceVersion.source_id, EvidenceVersion.source_key),
        order_by=EvidenceVersion.version_number.desc()).label('n')).where(
            EvidenceVersion.kind == 'news', EvidenceVersion.visibility == 'shared', EvidenceVersion.received_at < end).subquery()
    rows = db.execute(select(EvidenceVersion, Provider).join(ranked, ranked.c.id == EvidenceVersion.id).join(
        Provider, Provider.id == EvidenceVersion.source_id).where(ranked.c.n == 1,
            EvidenceVersion.received_at >= start, Provider.family.in_(NEWS_FAMILIES), *permitted()))
    records = []
    for evidence, provider in rows:
        published = evidence.published_at
        source_day = evidence.payload.get('published_day')
        if published is None and not source_day:
            continue
        if published and published >= end:
            continue
        if source_day and date.fromisoformat(source_day) > day:
            continue
        article = db.scalar(select(NewsArticle).where(NewsArticle.source_id == provider.id,
                                                     NewsArticle.source_key == evidence.source_key))
        if article is None:
            article = index_evidence(db, evidence, provider)
            db.flush()
        late = published < start if published else date.fromisoformat(source_day) < start.date()
        records.append((article, evidence, provider, classify(evidence.payload, provider.family), late))
    coverage = _coverage_at(db, start, end)
    partial = state.started_at > start or not coverage or any(source['status'] not in {'complete', 'empty'} for source in coverage)
    note = '自动汇编；仅覆盖已采集资料，保留原文标题、已有摘要及时间精度'
    if state.started_at > start:
        note += '；启用首期，覆盖不完整'
    if any(source['status'] not in {'complete', 'empty'} for source in coverage):
        note += '；部分来源暂未完整更新'
    return _publish(db, 'day', day, start, end, current, records, note, partial, coverage=coverage)


def create_period(db, state, period, day, current):
    end_day = day + timedelta(days=6) if period == 'week' else date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])
    children = list(db.scalars(select(NewsEdition).where(NewsEdition.period == 'day', NewsEdition.day >= day,
        NewsEdition.day <= end_day).order_by(NewsEdition.day)))
    if not children:
        return None
    ids = [child.id for child in children]
    records_by_article = {}
    rows = db.execute(select(NewsEditionItem, NewsArticle, EvidenceVersion, Provider).join(
        NewsArticle, NewsArticle.id == NewsEditionItem.article_id).join(EvidenceVersion, EvidenceVersion.id == NewsEditionItem.evidence_id).join(
        Provider, Provider.id == NewsArticle.source_id).where(NewsEditionItem.edition_id.in_(ids), *permitted()))
    for item, article, evidence, provider in rows:
        before = records_by_article.get(article.id)
        if before is None or evidence.version_number > before[1].version_number:
            records_by_article[article.id] = (article, evidence, provider, item.category, item.late)
    expected = (end_day - day).days + 1
    partial = len(children) < expected or any(child.coverage_status == 'partial' for child in children)
    start, end = at(day - timedelta(days=1)), at(end_day)
    note = '自动汇编；按刊期日期汇总日报，保留原始出处'
    if partial:
        note += '；本期日报覆盖不完整'
    return _publish(db, period, day, start, end, current, list(records_by_article.values()), note, partial,
                    [str(ident) for ident in ids],
                    [dict(edition_id=str(child.id), content_hash=child.content_hash, status=child.coverage_status) for child in children])


def schedule_editions(db, current=None):
    """Runs only from the durable worker. Bounded missed-edition recovery, never on GET."""
    current = current or datetime.now(UTC)
    lock(db, 'news-center:schedule')
    state = db.get(NewsCenterState, 1)
    if state and state.next_check_at > current:
        return 0
    if state is None:
        # Enabling before 08:00 must not fabricate an issue dated yesterday.
        state = NewsCenterState(id=1, started_at=current, first_day=current.astimezone(SHANGHAI).date(), next_check_at=current)
        db.add(state)
        db.flush()
    indexed = sync_all_news(db)
    if indexed == 1000:
        state.next_check_at = current + timedelta(seconds=2)
        return 0
    state.next_check_at = current + timedelta(seconds=30)
    latest = last_daily_day(current)
    existing = set(db.scalars(select(NewsEdition.day).where(NewsEdition.period == 'day')))
    day, made = state.first_day, 0
    while day <= latest and made < 31:
        if day not in existing:
            create_daily(db, state, day, current)
            made += 1
        day += timedelta(days=1)
    # Compile only periods whose daily recovery has reached their last issue date.
    completed_through = day - timedelta(days=1)
    first_week = state.first_day - timedelta(days=state.first_day.weekday())
    week = first_week
    checks = 0
    existing_weeks = set(db.scalars(select(NewsEdition.day).where(NewsEdition.period == 'week')))
    while week + timedelta(days=6) <= completed_through and at(week + timedelta(days=7), 10) <= current and checks < 12:
        if week not in existing_weeks:
            create_period(db, state, 'week', week, current)
            checks += 1
        week += timedelta(days=7)
    month, checks = state.first_day.replace(day=1), 0
    existing_months = set(db.scalars(select(NewsEdition.day).where(NewsEdition.period == 'month')))
    while next_month(month) - timedelta(days=1) <= completed_through and at(next_month(month), 10, 30) <= current and checks < 3:
        if month not in existing_months:
            create_period(db, state, 'month', month, current)
            checks += 1
        month = next_month(month)
    return made
