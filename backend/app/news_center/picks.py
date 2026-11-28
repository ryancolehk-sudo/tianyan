"""Financial reading editions. The archive holds editorial judgments and exact evidence references."""
from datetime import UTC, date, timedelta
from functools import lru_cache
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
from uuid import UUID

from pydantic import AwareDatetime, Field, ValidationError
from sqlalchemy import func

from app.auth import APIError
from app.config import settings
from app.evidence.common import lock, now
from app.evidence.models import EvidenceVersion as E
from app.market.flash_news import PUBLISHER_MARKS
from app.market.protocol import SHANGHAI
from app.models import Provider
from app.news_center.ai import _grounded, _text
from app.news_center.models import NewsArticle as A
from app.news_center.read import _state_exists, visible_rows
from app.news_center.schemas import PickCategory, PickEdition, PickScores
from app.news_center.stream_events import compact, group_reports, make_report, source_excerpt
from app.news_center.stream_priority import folded, reading_priority
from app.news_center.topic_stories import story_order
from app.provider_access import cloud_share_sql
from app.schemas import DTO


RUBRIC = '财经阅读价值 v1'
# Only the public, domestic/HK subscriptions selected for this digest. Enabling another
# feed elsewhere does not put it in the editorial input.
FAMILIES = ('cls_flash', 'ths_flash', 'eastmoney_flash', 'cninfo_announcements',
            'hk_gov_finance', 'rthk_finance', 'chinanews_finance')


class Decision(DTO):
    evidence_id: UUID
    related_evidence: list[UUID] = Field(default_factory=list, max_length=10)
    category: PickCategory
    scores: PickScores
    summary: str = Field(min_length=12, max_length=220)
    reason: str = Field(min_length=12, max_length=180)
    anchor: str = Field(min_length=4, max_length=120)


class Submission(DTO):
    cutoff: AwareDatetime
    model: str = Field(min_length=1, max_length=80)
    items: list[Decision] = Field(max_length=12)


class SavedPick(Decision):
    article_id: UUID
    peers: list[UUID]


class SavedEdition(PickEdition):
    items: list[SavedPick]


def directory():
    return Path(settings().artifact_directory) / 'news-picks'


@lru_cache(maxsize=128)
def edition_links(path):
    issue = SavedEdition.model_validate_json(path.read_bytes())
    return tuple((item.evidence_id, tuple(item.related_evidence)) for item in issue.items if item.related_evidence)


def reviewed_event_links():
    links = []
    for path in sorted(directory().glob('????-??-??_????????T????????????.json')):
        try:
            links.extend(edition_links(path))
        except (OSError, ValidationError):
            continue
    return tuple(links)


def source_rows():
    return visible_rows().where(cloud_share_sql(), E.kind == 'news', E.owner_id.is_(None),
        E.source_id == A.source_id, Provider.family.in_(FAMILIES),
        E.payload['allow_ai'].astext.is_distinct_from('false'),
        E.payload['ai_eligible'].astext.is_distinct_from('false')).with_only_columns(
        A.id, E.id.label('revision'), A.first_received_at, A.canonical_url,
        E.published_at, E.received_at, E.payload['published_day'].astext.label('published_day'),
        E.payload['title'].astext.label('title'), func.left(E.payload['summary'].astext, 1200).label('summary'),
        E.payload['publisher_highlight'].astext.label('mark'), Provider.family,
        func.coalesce(func.nullif(func.btrim(E.payload['source_name'].astext), ''), Provider.name).label('source_name'))


def headline(row):
    return dict(id=row.id, title=row.title, published_at=row.published_at, published_day=row.published_day,
                received_at=row.received_at, source_name=row.source_name)


def candidates(db, cutoff, *, families=FAMILIES):
    start = cutoff - timedelta(hours=24)
    rows = db.execute(source_rows().where(Provider.family.in_(families), A.updated_at >= start, E.received_at <= cutoff,
        (E.available_at.is_(None) | (E.available_at <= cutoff)))).all()
    by_revision, reports, priorities = {}, [], {}
    for row in rows:
        if row.published_at:
            if not start <= row.published_at <= cutoff:
                continue
        elif row.published_day:
            if not start.astimezone(SHANGHAI).date().isoformat() <= row.published_day <= cutoff.astimezone(SHANGHAI).date().isoformat():
                continue
        elif row.received_at < start:
            continue
        if not row.title:
            continue
        priority = reading_priority(row.title, (row.summary or '')[:360], row.family,
                                    row.mark in PUBLISHER_MARKS.get(row.family, ()))
        evidence = SimpleNamespace(id=row.revision, title=row.title, summary=row.summary,
                                   published_at=row.published_at, published_day=row.published_day)
        report = make_report(row, evidence, priority, row.family)
        if report:
            reports.append(report)
        by_revision[row.revision] = row
        priorities[row.revision] = priority
    copies, hidden = group_reports(reports)
    result = {}
    for rev, row in by_revision.items():
        if rev in hidden:
            continue
        result[rev] = dict(row=row, excerpt=source_excerpt(row.title, row.summary),
                           peers=[peer for _, peer in copies.get(rev, ())], priority=priorities[rev])
    return result


def issue_path(cutoff):
    day = cutoff.astimezone(SHANGHAI).date()
    return directory() / f'{day}_{cutoff.astimezone(UTC):%Y%m%dT%H%M%S%f}.json'


def publish(db, submission):
    clock = now(db)
    if submission.cutoff > clock:
        raise ValueError('截稿时点晚于本机时间')
    folder = directory()
    path = issue_path(submission.cutoff)
    lock(db, 'news-picks:' + path.stem)
    if path.exists():
        existing = SavedEdition.model_validate_json(path.read_bytes())
        if existing.model == submission.model and [Decision.model_validate(
                row.model_dump(exclude={'article_id','peers'})) for row in existing.items] == submission.items:
            return existing
        raise ValueError('该截稿时点已有不同的已发布版本')
    pool = candidates(db, submission.cutoff)
    chosen = []
    seen, titles = set(), set()
    for decision in submission.items:
        reported = [decision.evidence_id, *decision.related_evidence]
        if len(set(reported)) != len(reported) or any(key in seen or key not in pool for key in reported):
            raise ValueError('稿件重复、已更正、超出时间窗口或来源不可用')
        if decision.scores.total < 70:
            raise ValueError('入选总分至少为 70')
        entry = pool[decision.evidence_id]
        row = entry['row']
        title_key = compact(folded(row.title))
        if title_key in titles and row.family != 'cninfo_announcements':
            raise ValueError('同一事件的重复标题应合并为一条')
        corpus = row.title + '\n' + (entry['excerpt'] or '')
        if decision.anchor not in corpus:
            raise ValueError('缺少来自本条标题或原生摘要的逐字依据')
        for value in (decision.summary, decision.reason):
            _grounded(_text(value, limit=220), corpus)
        peers = list(entry['peers'])
        for key in decision.related_evidence:
            peers.extend([key, *pool[key]['peers']])
        chosen.append(SavedPick(**decision.model_dump(), article_id=row.id, peers=peers))
        seen.update(reported)
        titles.add(title_key)
    edition = SavedEdition(day=submission.cutoff.astimezone(SHANGHAI).date(), cutoff=submission.cutoff,
        published_at=clock, model=_text(submission.model, limit=80), rubric=RUBRIC, items=chosen)
    folder.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf8', dir=folder, delete=False) as out:
            temporary = Path(out.name)
            out.write(edition.model_dump_json())
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return edition


def read_edition(db, owner_id, day=None):
    paths = sorted(directory().glob('????-??-??_????????T????????????.json'), reverse=True)
    by_day = {}
    for path in paths:
        try:
            issue_day = date.fromisoformat(path.name[:10])
        except ValueError:
            continue
        by_day.setdefault(issue_day, path)
    target = day if day is not None else next(iter(by_day), None)
    result = dict(edition=None, days=list(by_day), items=[])
    if target not in by_day:
        return result
    try:
        issue = SavedEdition.model_validate_json(by_day[target].read_bytes())
    except (OSError, ValidationError) as error:
        raise APIError(503, 'pick_edition_unavailable', '本期精选暂时无法读取，请重试。') from error
    result['edition'] = issue.model_dump(exclude={'items'})
    refs = {rev for pick in issue.items for rev in (pick.evidence_id, *pick.peers)}
    if not refs:
        return result
    rows = {r.revision: r for r in db.execute(source_rows().where(E.id.in_(refs)).add_columns(
        _state_exists(owner_id, 'read').label('is_read')))}
    for pick in issue.items:
        row = rows.get(pick.evidence_id)
        if row is None or row.id != pick.article_id:
            continue
        reports = [headline(rows[rev]) for rev in (pick.evidence_id, *pick.peers) if rev in rows]
        reports.sort(key=story_order, reverse=True)
        result['items'].append(dict(**headline(row), **pick.model_dump(include={'category','summary','reason','scores'}),
            score=pick.scores.total, is_read=any(rows[rev].is_read for rev in (pick.evidence_id, *pick.peers) if rev in rows),
            reports=reports))
    result['items'].sort(key=story_order, reverse=True)
    return result
