"""Permission-aware local queries. No upstream HTTP or indexing is performed by GET."""
import math
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import Date, case, exists, func, or_, select
from sqlalchemy.orm import aliased

from app.auth import APIError
from app.evidence.common import cursor_scope, decode_cursor, encode_cursor
from app.evidence.models import EvidenceVersion
from app.market.models import DataFetchRun, SourceSettings
from app.market.news_feeds import NEWS_FAMILIES, news_metadata
from app.models import Provider
from app.news_center.indexing import CATEGORY_ORDER
from app.news_center.securities import news_securities
from app.news_center.models import (
    NewsAiVersion,
    NewsArticle,
    NewsEdition,
    NewsEditionItem,
    NewsSourceCoverage,
    NewsUserState,
)
from app.news_center.schemas import (
    AiEditionView,
    EditionDetail,
    EditionList,
    EditionSummary,
    NewsArticleView,
    NewsFeedView,
    NewsSourceView,
)


def permitted():
    return (Provider.enabled.is_(True), Provider.configured.is_(True), Provider.entitlement_status == 'verified',
            Provider.rights['display'].astext == 'allowed', Provider.rights['cache'].astext == 'allowed')


def visible_rows():
    return select(NewsArticle, EvidenceVersion, Provider).join(
        EvidenceVersion, EvidenceVersion.id == NewsArticle.latest_evidence_id).join(
        Provider, Provider.id == NewsArticle.source_id).where(*permitted(), EvidenceVersion.visibility == 'shared', Provider.family.in_(NEWS_FAMILIES))


def group_states(db, owner_id, key):
    return list(db.execute(select(NewsUserState, NewsArticle, EvidenceVersion).join(
        NewsArticle, NewsArticle.id == NewsUserState.article_id).join(
        EvidenceVersion, EvidenceVersion.id == NewsArticle.latest_evidence_id).join(
        Provider, Provider.id == NewsArticle.source_id).where(
            NewsUserState.owner_id == owner_id, NewsArticle.duplicate_key == key, *permitted())))


def article_view(db, owner_id, article, evidence, provider, *, category=None, late=False, related=True, securities=None):
    from app.ai.runs import news_text
    summary_available = news_text(evidence) is not None
    states = group_states(db, owner_id, article.duplicate_key)
    own = db.get(NewsUserState, (owner_id, article.id))
    is_read = any(state.read_hash == item.content_hash or item.content_hash in (state.read_versions or []) for state, _, item in states)
    if evidence.id != article.latest_evidence_id:
        is_read = bool(own and (own.read_hash == evidence.content_hash or evidence.content_hash in (own.read_versions or [])))
    metadata = news_metadata(provider.family) or {}
    securities, security_status = securities if securities is not None else news_securities(db, [(evidence, provider)])[evidence.id]
    links = []
    if related:
        for _duplicate, other, origin in db.execute(visible_rows().where(
                NewsArticle.duplicate_key == article.duplicate_key, NewsArticle.id != article.id).limit(20)):
            links.append({'name': other.payload.get('source_name') or origin.name, 'url': other.payload['source_url']})
    return NewsArticleView(id=article.id, evidence_id=evidence.id, title=evidence.payload['title'],
        summary=evidence.payload.get('summary'), category=category or article.category,
        summary_input_available=summary_available,
        summary_input_reason=None if summary_available else '只有标题或已保存内容不足，暂不能生成可靠摘要。',
        source_name=evidence.payload.get('source_name') or provider.name, publisher=metadata.get('publisher') or provider.name,
        family=provider.family, source_url=evidence.payload['source_url'], published_at=evidence.published_at,
        published_day=evidence.payload.get('published_day'), received_at=evidence.received_at, content_hash=evidence.content_hash,
        is_read=is_read, is_saved=any(state.saved for state, _, _ in states),
        is_following=any(state.following for state, _, _ in states), related_sources=links, late=late,
        securities=securities, security_status=security_status)


def get_article(db, owner_id, article_id):
    row = db.execute(visible_rows().where(NewsArticle.id == article_id)).first()
    if not row:
        # Old evidence deep links continue to resolve to the stable article identity.
        old = db.get(EvidenceVersion, article_id)
        if old and old.kind == 'news' and old.visibility == 'shared':
            row = db.execute(visible_rows().where(NewsArticle.source_id == old.source_id,
                                                NewsArticle.source_key == old.source_key)).first()
    if not row:
        raise APIError(404, 'not_found', '资讯不存在或当前不可访问。')
    return article_view(db, owner_id, *row)


def sources_view(db):
    result = []
    for provider in db.scalars(select(Provider).where(Provider.family.in_(NEWS_FAMILIES)).order_by(Provider.name)):
        setting = db.get(SourceSettings, provider.id)
        runs = select(DataFetchRun).where(DataFetchRun.source_id == provider.id)
        run = db.scalar(runs.order_by(DataFetchRun.started_at.desc(), DataFetchRun.id.desc()).limit(1))
        success = db.scalar(runs.where(DataFetchRun.status.in_(['complete', 'partial', 'empty'])).order_by(
            DataFetchRun.received_at.desc(), DataFetchRun.id.desc()).limit(1))
        usable = provider.configured and provider.enabled and provider.entitlement_status == 'verified' and all(
            provider.rights.get(key) == 'allowed' for key in ('display', 'cache'))
        stats = db.execute(select(func.count(NewsArticle.id), func.max(EvidenceVersion.published_at)).join(
            EvidenceVersion, EvidenceVersion.id == NewsArticle.latest_evidence_id).where(NewsArticle.source_id == provider.id)).first() if usable else (0, None)
        current = db.get(NewsSourceCoverage, provider.id)
        coverage = None
        if current and usable:
            values = current.coverage
            markets = values.get('markets')
            if isinstance(markets, list):
                labels = {'sh': '沪市', 'sz': '深市', 'bj': '北交所'}
                coverage = '；'.join(labels.get(value.get('market'), '公告') + ': ' +
                    ('覆盖有限' if value.get('status') != 'complete' or value.get('has_more') else '已获取查询窗口')
                    for value in markets if isinstance(value, dict))
            elif isinstance(markets, dict):
                coverage = '；'.join(f'{key}: ' + ('覆盖有限' if value.get('partial') or value.get('has_more') else '已获取查询窗口')
                    for key, value in markets.items() if isinstance(value, dict))
            coverage = coverage or ('覆盖有限，仅已获取条目' if values.get('partial') else '官方滚动列表，仅已获取条目')
        status = 'disabled' if not provider.enabled else 'permission_missing' if not usable else (
            'paused' if setting and setting.paused_reason else run.status if run else 'not_checked')
        result.append(NewsSourceView(family=provider.family, name=provider.name,
            publisher=(news_metadata(provider.family) or {}).get('publisher', provider.name), status=status,
            last_success_at=success.received_at if success and usable else None,
            latest_published_at=stats[1], cached_count=stats[0], coverage=coverage))
    return result


def _state_exists(owner_id, field):
    peer = aliased(NewsArticle)
    stmt = select(NewsUserState.article_id).join(peer, peer.id == NewsUserState.article_id).where(
        NewsUserState.owner_id == owner_id, peer.duplicate_key == NewsArticle.duplicate_key)
    if field == 'read':
        version = aliased(EvidenceVersion)
        stmt = stmt.join(version, version.id == peer.latest_evidence_id).where(or_(
            NewsUserState.read_hash == version.content_hash,
            NewsUserState.read_versions.contains(func.jsonb_build_array(version.content_hash))))
    else:
        stmt = stmt.where(getattr(NewsUserState, field).is_(True))
    origin = aliased(Provider)
    stmt = stmt.join(origin, origin.id == peer.source_id).where(origin.enabled.is_(True), origin.configured.is_(True),
        origin.entitlement_status == 'verified', origin.rights['display'].astext == 'allowed', origin.rights['cache'].astext == 'allowed')
    return exists(stmt.correlate(NewsArticle))


def feed(db, owner_id, *, q=None, category=None, day=None, family=None, saved=False, unread=False,
         following=False, order='latest', cursor=None, limit=50):
    from app.model_connections import connection_status
    filters = dict(q=q, category=category, day=day, family=family, saved=saved, unread=unread,
                   following=following, order=order, resource='news-center')
    scope = cursor_scope(owner_id, filters)
    position = decode_cursor(cursor, scope)
    offset = position[0] if position else 0
    if type(offset) is not int:
        raise APIError(422, 'invalid_cursor', '列表位置已失效，请从第一页重新读取。')
    stmt = visible_rows()
    if q:
        escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        stmt = stmt.where(or_(EvidenceVersion.payload['title'].astext.ilike('%' + escaped + '%', escape='\\'),
                             EvidenceVersion.payload['summary'].astext.ilike('%' + escaped + '%', escape='\\'),
                             Provider.name.ilike('%' + escaped + '%', escape='\\')))
    if category and category not in {'全部', '精选'}:
        stmt = stmt.where(NewsArticle.category == category)
    if category == '精选':
        stmt = stmt.where(or_(NewsArticle.category.in_(['风险澄清', '政策', '产业', '公司公告']),
                              Provider.family.in_(['chinanews_finance', 'rthk_finance', 'hk_gov_finance']),
                              EvidenceVersion.payload['title'].astext.op('~*')('A股|中国|China|利率|通胀|美联储|Fed|CPI|人民币')))
    if family:
        if family not in NEWS_FAMILIES:
            raise APIError(422, 'invalid_source', '不支持的资讯来源。')
        stmt = stmt.where(Provider.family == family)
    if day:
        source_day = func.coalesce(func.timezone('Asia/Shanghai', EvidenceVersion.published_at).cast(Date),
                                   EvidenceVersion.payload['published_day'].astext.cast(Date))
        stmt = stmt.where(source_day == day)
    if saved:
        stmt = stmt.where(_state_exists(owner_id, 'saved'))
    if following:
        stmt = stmt.where(_state_exists(owner_id, 'following'))
    if unread:
        stmt = stmt.where(~_state_exists(owner_id, 'read'))
    selected = stmt.with_only_columns(NewsArticle.id.label('article_id'),
        func.row_number().over(partition_by=NewsArticle.duplicate_key,
            order_by=(NewsArticle.first_received_at, NewsArticle.id)).label('n')).subquery()
    unique = select(selected.c.article_id).where(selected.c.n == 1)
    total = db.scalar(select(func.count()).select_from(unique.subquery()))
    rank = case(CATEGORY_ORDER, value=NewsArticle.category, else_=9)
    # A late receipt of an old date-only disclosure must not outrank today's
    # source publication. Category filtering must also respect the sort control.
    source_day = func.coalesce(func.timezone('Asia/Shanghai', EvidenceVersion.published_at).cast(Date),
        EvidenceVersion.payload['published_day'].astext.cast(Date),
        func.timezone('Asia/Shanghai', EvidenceVersion.received_at).cast(Date))
    if order == 'earliest':
        ordering = (source_day, EvidenceVersion.published_at.asc().nullslast(), rank, NewsArticle.id)
    elif order == 'selected':
        ordering = (source_day.desc(), rank, EvidenceVersion.published_at.desc().nullslast(), NewsArticle.id)
    else:
        ordering = (source_day.desc(), EvidenceVersion.published_at.desc().nullslast(), rank, NewsArticle.id)
    rows = list(db.execute(visible_rows().where(NewsArticle.id.in_(unique)).order_by(*ordering).offset(offset).limit(limit + 1)))
    securities = news_securities(db, [(evidence, provider) for _, evidence, provider in rows[:limit]])
    items = [article_view(db, owner_id, *row, securities=securities[row[1].id]) for row in rows[:limit]]
    next_cursor = encode_cursor(SimpleNamespace(offset=offset + limit, id=UUID(int=0)), scope, 'offset') if len(rows) > limit else None
    latest = db.scalar(select(NewsEdition).where(NewsEdition.period == 'day').order_by(NewsEdition.day.desc()).limit(1))
    start = db.scalar(visible_rows().with_only_columns(func.min(NewsArticle.first_received_at)))
    return NewsFeedView(items=items, next_cursor=next_cursor, total=total, focus=items[0] if items else None,
        latest_edition=edition_summary(db, owner_id, latest) if latest else None, sources=sources_view(db),
        coverage_start=start, ai_status=connection_status())


def _edition_rows(db, edition):
    return list(db.execute(select(NewsEditionItem, NewsArticle, EvidenceVersion, Provider).join(
        NewsArticle, NewsArticle.id == NewsEditionItem.article_id).join(
        EvidenceVersion, EvidenceVersion.id == NewsEditionItem.evidence_id).join(
        Provider, Provider.id == NewsArticle.source_id).where(
            NewsEditionItem.edition_id == edition.id, EvidenceVersion.visibility == 'shared', *permitted()).order_by(NewsEditionItem.position)))


def edition_summary(db, owner_id, edition):
    rows = _edition_rows(db, edition)
    publishers = {(news_metadata(provider.family) or {}).get('publisher', provider.name) for _, _, _, provider in rows}
    characters = sum(len(e.payload['title']) + len(e.payload.get('summary') or '') for _, _, e, _ in rows)
    ai = db.scalar(select(NewsAiVersion).where(NewsAiVersion.edition_id == edition.id))
    changed = len(rows) != len(edition.manifest['items'])
    return EditionSummary(id=edition.id, period=edition.period, day=edition.day, issue_number=edition.issue_number,
        headline=rows[0][2].payload['title'] if rows else '本期暂无可展示资讯', item_count=len(rows),
        source_count=len(publishers), reading_minutes=max(1, math.ceil(characters / 350)) if rows else 0,
        window_start=edition.window_start, window_end=edition.window_end, generated_at=edition.generated_at,
        coverage_status='partial' if changed else edition.coverage_status,
        coverage_note=edition.coverage_note + ('；部分条目当前不可展示' if changed else ''),
        ai_state=ai.state if ai else 'not_configured')


def editions(db, owner_id, period, cursor=None, limit=30):
    scope = cursor_scope(owner_id, {'resource': 'news-editions', 'period': period})
    position = decode_cursor(cursor, scope)
    offset = position[0] if position else 0
    if type(offset) is not int:
        raise APIError(422, 'invalid_cursor', '列表位置已失效。')
    rows = list(db.scalars(select(NewsEdition).where(NewsEdition.period == period).order_by(
        NewsEdition.day.desc()).offset(offset).limit(limit + 1)))
    return EditionList(items=[edition_summary(db, owner_id, row) for row in rows[:limit]],
        next_cursor=encode_cursor(SimpleNamespace(offset=offset + limit, id=UUID(int=0)), scope, 'offset') if len(rows) > limit else None)


def edition_detail(db, owner_id, edition_id):
    edition = db.get(NewsEdition, edition_id)
    if not edition:
        raise APIError(404, 'not_found', '本期报刊不存在。')
    rows = _edition_rows(db, edition)
    securities = news_securities(db, [(evidence, provider) for _, _, evidence, provider in rows], cutoff=edition.window_end)
    items = [article_view(db, owner_id, article, evidence, provider, category=item.category, late=item.late, securities=securities[evidence.id])
             for item, article, evidence, provider in rows]
    ai = db.scalar(select(NewsAiVersion).where(NewsAiVersion.edition_id == edition.id, NewsAiVersion.state == 'complete'))
    enhancement = None
    if ai and ai.result and ai.completed_at:
        fields = ('article_id', 'evidence_id', 'content_hash')
        visible = {(str(article.id), str(evidence.id), evidence.content_hash)
                   for _, article, evidence, _ in rows}
        frozen = {tuple(item[key] for key in fields) for item in edition.manifest['items']}
        # Check every actual input, including those omitted from model output.
        # Legacy results have no exact input journal: conservatively require the
        # entire edition to remain visible. Malformed journals fail closed.
        references = ai.result.get('_input_manifest', edition.manifest['items'])
        valid = isinstance(references, list) and bool(references) and all(
            isinstance(row, dict) and all(isinstance(row.get(key), str) for key in fields)
            for row in references)
        input_refs = [tuple(row[key] for key in fields) for row in references] if valid else []
        if valid and len(set(input_refs)) == len(input_refs) and set(input_refs).issubset(visible & frozen):
            enhancement = AiEditionView(id=ai.id, generated_at=ai.completed_at, model=ai.model,
                prompt_version=ai.prompt_version, lead=ai.result['lead'], items=ai.result['items'], usage=ai.usage)
    return EditionDetail(edition=edition_summary(db, owner_id, edition), items=items, ai=enhancement)
