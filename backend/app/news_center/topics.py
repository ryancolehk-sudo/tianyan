"""Read-only topic research over authorized immutable evidence and dated archives."""
from datetime import UTC, datetime, time
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select

from app.auth import APIError
from app.evidence.common import cursor_scope, decode_cursor, encode_cursor
from app.evidence.models import EvidenceVersion
from app.market.news_feeds import NEWS_FAMILIES
from app.market.review_analysis import ArchiveReader
from app.market.review_read import _dated_rows, _metadata, _number, _referenced, _json
from app.models import Provider, Instrument
from app.news_center.models import NewsArticle
from app.news_center.read import permitted

SHANGHAI = ZoneInfo('Asia/Shanghai')


def research(db, owner_id, *, topic_source, topic_key, keyword, trade_day, as_of_time=None, cursor=None, limit=30):
    # A date-only historical link is bounded by that day's end, never by today.
    cutoff = min(as_of_time or datetime.combine(trade_day, time.max, SHANGHAI),
                 datetime.combine(trade_day, time.max, SHANGHAI), datetime.now(UTC))
    if as_of_time and as_of_time.astimezone(SHANGHAI).date() != trade_day:
        raise APIError(422, 'invalid_cutoff', '历史时点必须属于所选交易日。')
    filters = dict(resource='topic-research-v1', topic_source=topic_source, topic_key=topic_key,
                   keyword=keyword, trade_day=trade_day, as_of_time=as_of_time)
    scope = cursor_scope(owner_id, filters)
    position = decode_cursor(cursor, scope)
    offset = position[0] if position else 0
    if type(offset) is not int or offset < 0:
        raise APIError(422, 'invalid_cursor', '列表位置失效，请重新读取。')
    ranked = select(EvidenceVersion.id.label('id'), func.row_number().over(
        partition_by=(EvidenceVersion.source_id, EvidenceVersion.source_key),
        order_by=(EvidenceVersion.version_number.desc(), EvidenceVersion.id.desc())).label('n')).where(
            EvidenceVersion.kind == 'news', EvidenceVersion.visibility == 'shared',
            EvidenceVersion.received_at <= cutoff, EvidenceVersion.available_at <= cutoff,
            EvidenceVersion.availability == 'known', EvidenceVersion.historical_eligible.is_(True),
            or_(EvidenceVersion.published_at.is_(None), EvidenceVersion.published_at <= cutoff),
            or_(EvidenceVersion.payload['published_day'].astext.is_(None), EvidenceVersion.payload['published_day'].astext <= trade_day.isoformat())).subquery()
    # Rank versions before matching text; a later correction cannot resurrect an old match.
    stmt = select(NewsArticle, EvidenceVersion, Provider).join(EvidenceVersion, and_(
        EvidenceVersion.source_id == NewsArticle.source_id, EvidenceVersion.source_key == NewsArticle.source_key)).join(
        ranked, and_(ranked.c.id == EvidenceVersion.id, ranked.c.n == 1)).join(
        Provider, Provider.id == NewsArticle.source_id).where(*permitted(), Provider.family.in_(NEWS_FAMILIES))
    instrument = None
    if topic_source == 'stock':
        parts = topic_key.split('.')
        if len(parts) == 2:
            exchange = {'SH':'SSE', 'SZ':'SZSE', 'BJ':'BSE'}.get(parts[1])
            instrument = db.scalar(select(Instrument).where(Instrument.symbol == parts[0], Instrument.exchange == exchange)) if exchange else None
    escaped = keyword.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    text_match = or_(EvidenceVersion.payload['title'].astext.ilike('%'+escaped+'%', escape='\\'),
                     EvidenceVersion.payload['summary'].astext.ilike('%'+escaped+'%', escape='\\'))
    stmt = stmt.where(or_(text_match, EvidenceVersion.instrument_id == instrument.id) if instrument else text_match)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = list(db.execute(stmt.order_by(EvidenceVersion.published_at.desc().nullslast(),
        EvidenceVersion.received_at.desc(), EvidenceVersion.id).offset(offset).limit(limit+1)))
    events = []
    for article, evidence, provider in rows[:limit]:
        explicit = bool(instrument and evidence.instrument_id == instrument.id and evidence.provenance == 'source_reported')
        events.append(dict(id=str(article.id), evidence_id=str(evidence.id), title=evidence.payload['title'],
            summary=evidence.payload.get('summary'), basis='source_explicit' if explicit else 'keyword_clue',
            basis_note='原始证据明确绑定证券身份' if explicit else '标题或摘要命中关键词；不能据此认定催化原因',
            source_id=str(provider.id), source_name=provider.name, source_url=evidence.payload.get('source_url'),
            published_at=evidence.published_at, received_at=evidence.received_at, content_hash=evidence.content_hash))
    members, coverage = [], []
    gaps = ['资讯仅覆盖本机已存且当前有展示权限的来源，不代表完整事件库。',
            '现有人工研究尚无带来源身份的题材关联记录；不按同名文字认定人工已确认。']
    if topic_source == 'dc':
        reader = ArchiveReader(db, cutoff)
        table, quotes = reader.get('members', trade_day), reader.get('daily', trade_day)
        originals, daily = _dated_rows(table, trade_day), _dated_rows(quotes, trade_day)
        by_code = {}
        for row in daily or []:
            by_code.setdefault(row.get('ts_code'), []).append(row)
        for i, row in enumerate(originals or []):
            if row.get('ts_code') != topic_key:
                continue
            same = by_code.get(row.get('con_code'), [])
            members.append(dict(code=row.get('con_code'), name=row.get('con_name') or row.get('name') or row.get('con_code'),
                pct_change=_number(same[0].get('pct_chg')) if len(same) == 1 else None,
                evidence=_referenced(row, table, i).get('row_reference')))
        coverage = [_metadata(table), _metadata(quotes)]
        if originals is None: gaps.append('对应时点的原日板块成员缺失，不以当前成员回填历史。')
        if daily is None: gaps.append('对应时点的当日日行情缺失，成员保留、表现留空。')
    else:
        gaps.append('该来源的题材成员身份未接入可核归档；不与同名题材自动合并。')
    return _json(dict(topic_source=topic_source, topic_key=topic_key, keyword=keyword, trade_day=trade_day,
        as_of_time=cutoff, events=events, total=total, members=members, coverage=coverage, gaps=gaps,
        next_cursor=encode_cursor(SimpleNamespace(offset=offset+limit,id=UUID(int=0)),scope,'offset') if len(rows)>limit else None))
