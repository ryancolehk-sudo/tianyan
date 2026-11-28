"""Publisher hotspots and the board directories used by the topic page."""
from datetime import UTC, datetime, timedelta
import re

from app.evidence.models import EvidenceVersion
from app.market.news_feeds import FLASH_FAMILIES, NEWS_FEEDS
from app.models import Provider
from app.news_center.models import NewsArticle
from app.news_center.read import visible_rows
from app.news_center.topic_market import eastmoney_boards, industry_breadth, kpl_boards
from app.news_center.topic_stories import add_archive, board_names, native_stories, source_states, story_order
from app.provider_access import cloud_share_sql


def hotspots(db, cutoff):
    stamp = EvidenceVersion.published_at
    recent = visible_rows().where(cloud_share_sql(), Provider.family.in_(FLASH_FAMILIES),
        EvidenceVersion.owner_id.is_(None), EvidenceVersion.received_at <= cutoff,
        stamp >= cutoff-timedelta(hours=48), stamp <= cutoff).with_only_columns(NewsArticle.id, EvidenceVersion.payload['title'].astext, stamp,
        Provider.family).order_by(stamp.desc(), NewsArticle.id)
    groups = {}
    for ident, headline, published, family in db.execute(recent):
        key = re.sub(r'[^\w]+', '', headline.casefold()) or headline
        item = groups.setdefault(key, dict(id=ident, title=headline, published_at=published, sources=set()))
        item['sources'].add(NEWS_FEEDS[family].publisher)
    ranked = sorted(groups.values(), key=lambda item: (-len(item['sources']), -item['published_at'].timestamp()))
    return [{**item, 'sources': sorted(item['sources'])} for item in ranked[:30]]


def headlines(db):
    cutoff = datetime.now(UTC)
    industry_day, industries = eastmoney_boards(db, 'industry', cutoff)
    concept_day, concepts = eastmoney_boards(db, 'concept', cutoff)
    strength_day, strength_topics = kpl_boards(db, cutoff)
    breadth_day, breadth_basis, counts = industry_breadth(db, industry_day or concept_day, cutoff, industries)
    groups = [industries, concepts, strength_topics]
    matcher = board_names(tuple(sorted((code, b['name']) for group in groups for code,b in group.items())))
    stories = native_stories(db, matcher, cutoff)
    add_archive(db, matcher, stories, cutoff)
    for group in groups:
        for code, board in group.items():
            board['latest'] = stories.get(code)
            if group is industries and code in counts:
                r = counts[code]
                board.update(strength=r.limit_up_count, strength_coverage=dict(quoted=r.quoted_count,
                    members=r.member_count, members_day=r.members_day, partial=r.partial))

    def ordered(group):
        return sorted(group.values(), key=lambda b: story_order(b['latest'] or {}), reverse=True)

    return dict(hotspots=hotspots(db, cutoff), industries=ordered(industries), concepts=ordered(concepts),
        strength_topics=ordered(strength_topics), market_trade_day=industry_day, concept_trade_day=concept_day,
        strength_trade_day=strength_day, breadth_trade_day=breadth_day, breadth_basis=breadth_basis, sources=source_states(db, cutoff))
