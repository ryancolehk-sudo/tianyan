from datetime import UTC, datetime

from app.auth import APIError
from app.evidence.common import lock
from app.evidence.models import EvidenceVersion
from app.models import AuditEvent
from app.news_center.indexing import classify, store_source_coverage, sync_news_index  # noqa: F401 - collection integration port
from app.news_center.models import NewsArticle, NewsUserState
from app.news_center.read import article_view, get_article, visible_rows


def update_state(db, owner, article_id, body, request_id):
    item = get_article(db, owner.id, article_id)
    article = db.get(NewsArticle, item.id)
    lock(db, f'news-reading:{owner.id}:{article.duplicate_key}')
    viewed = db.get(EvidenceVersion, body.evidence_id or article.latest_evidence_id)
    if not viewed or viewed.source_id != article.source_id or viewed.source_key != article.source_key or viewed.visibility != 'shared':
        raise APIError(404, 'not_found', '所阅读的资讯版本不存在或不可访问。')
    # Exact duplicates share personal state; source revisions still become unread.
    group = list(db.execute(visible_rows().where(NewsArticle.duplicate_key == article.duplicate_key)))
    for peer, evidence, _ in group:
        state = db.get(NewsUserState, (owner.id, peer.id))
        if state is None:
            state = NewsUserState(owner_id=owner.id, article_id=peer.id, read_hash=None, read_versions=[], saved=False, following=False)
            db.add(state)
        if 'read' in body.model_fields_set:
            hashes = set(state.read_versions or [])
            if state.read_hash:
                hashes.add(state.read_hash)
            # A reader of an old edition has not thereby read a newer correction.
            if peer.id == article.id or viewed.id == article.latest_evidence_id:
                value = viewed.content_hash if peer.id == article.id else evidence.content_hash
                hashes.add(value) if body.read else hashes.discard(value)
            state.read_versions = sorted(hashes)
            state.read_hash = evidence.content_hash if evidence.content_hash in hashes else None
        if 'saved' in body.model_fields_set:
            state.saved = body.saved
        if 'following' in body.model_fields_set:
            state.following = body.following
        state.updated_at = datetime.now(UTC)
    db.add(AuditEvent(actor_id=owner.id, action='news.reading.update', request_id=request_id,
        resource_type='news_article', resource_id=article.id, changed_field_names=sorted(body.model_fields_set)))
    db.flush()
    provider = next(provider for peer, _, provider in group if peer.id == article.id)
    return article_view(db, owner.id, article, viewed, provider, category=classify(viewed.payload, provider.family))
