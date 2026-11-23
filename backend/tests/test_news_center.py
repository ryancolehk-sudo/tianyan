"""News acceptance using synthetic material and an isolated real PostgreSQL database."""
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from test_connected_data import source_config
from test_data_sources import configure, login

from app.db import engine
from app.evidence.common import content_hash
from app.evidence.models import SourceVersion
from app.evidence.schemas import NewsPayload
from app.market.collect import store_news_batch
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SHANGHAI
from app.models import Job, Provider, User
from app.news_center.editions import (
    at,
    create_daily,
    create_period,
    last_daily_day,
    schedule_editions,
)
from app.news_center.models import (
    NewsArticle,
    NewsCenterState,
    NewsEdition,
    NewsEditionItem,
    NewsUserState,
)


def source(client, headers, family='chinanews_finance'):
    return uuid.UUID(configure(client, headers, source_config(family), key='synthetic-news-' + family).json()['data']['provider']['id'])


def save(db, source_id, key, title, received, published=None, *, summary=None, day=None, url=None):
    provider = db.get(Provider, source_id)
    payload = NewsPayload(title=title, summary=summary, source_url=url or 'https://example.com/synthetic/' + key,
        category='公告' if provider.family == 'cninfo_announcements' else '财经', published_day=day)
    store_news_batch(db, provider, db.get(SourceSettings, source_id), SimpleNamespace(records=[(key, payload, published)]), received)
    return db.scalar(select(NewsArticle).where(NewsArticle.source_id == source_id, NewsArticle.source_key == 'news:' + key))


def started(db, day, *, beginning=None):
    state = NewsCenterState(id=1, first_day=day, started_at=beginning or at(day - timedelta(days=1)), next_check_at=at(day))
    db.add(state)
    db.flush()
    return state


def api_items(client, **params):
    response = client.get('/api/v1/news/items', params=params)
    assert response.status_code == 200, response.text
    return response.json()['data']


def test_history_search_is_not_limited_to_latest_hundred_and_cursor_is_scoped(client):
    headers = login(client)
    origin = source(client, headers)
    now = datetime.now(UTC) - timedelta(days=1)
    with Session(engine()) as db, db.begin():
        for index in range(125):
            save(db, origin, str(index), '合成检索条目' + str(index), now + timedelta(seconds=index), now + timedelta(seconds=index))
    assert api_items(client, q='合成检索条目0')['total'] == 1
    first = api_items(client, limit=20)
    assert first['total'] == 125 and len(first['items']) == 20 and first['next_cursor']
    second = api_items(client, limit=20, cursor=first['next_cursor'])
    assert not ({row['id'] for row in first['items']} & {row['id'] for row in second['items']})
    assert client.get('/api/v1/news/items', params={'q': '不同筛选', 'cursor': first['next_cursor']}).status_code == 422


def test_background_bootstrap_indexes_pre_feature_news_without_loading_partial_rows(client):
    from app.news_center.indexing import sync_all_news
    headers = login(client)
    origins = [source(client, headers, family) for family in ['chinanews_finance', 'ecb_news']]
    now = datetime.now(UTC) - timedelta(hours=1)
    with Session(engine()) as db, db.begin():
        for index, origin in enumerate(origins):
            save(db, origin, 'legacy-' + str(index), '合成启用前资料' + str(index), now, now)
        db.execute(delete(NewsArticle))
    with Session(engine()) as db, db.begin():
        assert sync_all_news(db) == 2
        assert sync_all_news(db) == 0
    assert api_items(client)['total'] == 2


def test_original_summary_date_precision_and_exact_duplicates_keep_sources(client):
    headers = login(client)
    origins = [source(client, headers, family) for family in ['chinanews_finance', 'rthk_finance']]
    now = datetime.now(UTC) - timedelta(hours=2)
    summary = '这是一段足够长的合成原生摘要，仅用于验证跨来源完全相同资料的去重，不含真实新闻。'
    with Session(engine()) as db, db.begin():
        for origin in origins:
            save(db, origin, 'duplicate', '合成产业重复报道', now, summary=summary, day=now.astimezone(SHANGHAI).date())
        save(db, origins[0], 'no-summary', '合成无摘要公告', now, day=now.astimezone(SHANGHAI).date())
    values = api_items(client)
    assert values['total'] == 2
    article = next(row for row in values['items'] if row['summary'])
    assert article['summary'] == summary and len(article['related_sources']) == 1
    assert article['published_at'] is None and article['published_day']
    assert next(row for row in values['items'] if row['title'] == '合成无摘要公告')['summary'] is None
    assert article['summary_input_available'] is True and article['summary_input_reason'] is None
    assert next(row for row in values['items'] if row['title'] == '合成无摘要公告')['summary_input_available'] is False


@pytest.mark.parametrize(('summary', 'body', 'available'), [
    (None, None, False), ('合成摘要标题', None, False),
    ('这是一段已有的完整原生摘要，仅用来核对真实输入可用状态，不含任何市场事实。', None, True),
    (None, '这是一段已有的正文，仅用来核对没有原生摘要时的真实输入可用状态，不含任何市场事实。', True),
])
def test_article_summary_availability_uses_original_evidence_content(summary, body, available, monkeypatch):
    from app.news_center.read import article_view
    monkeypatch.setattr('app.news_center.read.group_states', lambda *args: [])
    identity = uuid.uuid4()
    article = SimpleNamespace(id=identity, latest_evidence_id=identity, duplicate_key='synthetic', category='财经')
    evidence = SimpleNamespace(id=identity, payload={'title': '合成摘要标题', 'summary': summary, 'body': body,
        'source_url': 'https://example.com/synthetic'}, published_at=None, event_at=None,
        received_at=datetime.now(UTC), content_hash='b' * 64)
    db = SimpleNamespace(get=lambda *args: None)
    view = article_view(db, uuid.uuid4(), article, evidence,
                        SimpleNamespace(family='chinanews_finance', name='合成来源'), related=False)
    assert view.summary_input_available is available
    assert (view.summary_input_reason is None) is available


def test_short_equal_titles_do_not_merge_unrelated_articles(client):
    headers = login(client)
    origin = source(client, headers)
    now = datetime.now(UTC) - timedelta(hours=1)
    with Session(engine()) as db, db.begin():
        save(db, origin, 'one', '董事会决议', now)
        save(db, origin, 'two', '董事会决议', now)
    assert api_items(client)['total'] == 2


def test_featured_sort_respects_publication_day_instead_of_category_and_late_receipt(client):
    headers = login(client)
    origin = source(client, headers)
    now = datetime.now(UTC) - timedelta(hours=1)
    with Session(engine()) as db, db.begin():
        save(db, origin, 'old-risk', '合成旧风险提示', now + timedelta(minutes=1), day=(now - timedelta(days=5)).date())
        save(db, origin, 'new-industry', '合成新产业进展', now, now)
    latest = api_items(client, category='精选', order='latest')
    earliest = api_items(client, category='精选', order='earliest')
    assert latest['focus']['title'] == latest['items'][0]['title'] == '合成新产业进展'
    assert earliest['items'][0]['title'] == '合成旧风险提示'


def test_only_known_analytics_are_removed_from_link_identity():
    from app.news_center.indexing import canonical_url
    assert canonical_url('https://example.com/item?id=1&utm_source=rss') == 'https://example.com/item?id=1'
    assert canonical_url('https://example.com/item?source=1') != canonical_url('https://example.com/item?source=2')
    assert canonical_url('https://example.com/#/news/1') != canonical_url('https://example.com/#/news/2')


def test_reading_state_persists_and_new_revision_becomes_unread(client):
    headers = login(client)
    origin = source(client, headers)
    now = datetime.now(UTC) - timedelta(hours=1)
    with Session(engine()) as db, db.begin():
        article = save(db, origin, 'state', '合成初始资料', now, now)
        article_id = str(article.id)
    path = '/api/v1/news/items/' + article_id + '/state'
    response = client.post(path, json={'read': True, 'saved': True, 'following': True}, headers={**headers, 'Idempotency-Key': 'synthetic-save-state'})
    assert response.status_code == 200, response.text
    assert api_items(client, unread=True)['total'] == 0
    assert api_items(client, saved=True)['items'][0]['is_following']
    assert client.post(path, json={'read': True, 'saved': True, 'following': True}, headers={**headers, 'Idempotency-Key': 'synthetic-save-state'}).headers['Idempotency-Replayed'] == 'true'
    assert client.post(path, json={'saved': False}, headers={**headers, 'Idempotency-Key': 'synthetic-save-state'}).status_code == 409
    with Session(engine()) as db, db.begin():
        save(db, origin, 'state', '合成更正资料', now + timedelta(minutes=1), now)
    item = api_items(client, unread=True)['items'][0]
    assert item['id'] == article_id and item['is_saved'] and item['title'] == '合成更正资料'


def test_state_is_per_account_and_requires_csrf(client):
    headers = login(client)
    origin = source(client, headers)
    now = datetime.now(UTC) - timedelta(hours=1)
    with Session(engine()) as db, db.begin():
        article = save(db, origin, 'private', '合成账号状态', now, now)
        article_id = str(article.id)
        another = User(username='other-synthetic-news-owner', password_hash='not-a-login-secret', role='viewer', enabled=True)
        db.add(another)
        db.flush()
        db.add(NewsUserState(owner_id=another.id, article_id=article.id, saved=True, following=True, updated_at=now))
    assert api_items(client, saved=True)['items'] == []
    assert client.post('/api/v1/news/items/' + article_id + '/state', json={'saved': True}, headers={'Idempotency-Key': 'no-csrf-synthetic'}).status_code == 403


def test_reading_an_old_edition_does_not_mark_the_correction_read(client):
    headers = login(client)
    origin = source(client, headers)
    day = date(2026, 10, 24)
    with Session(engine()) as db, db.begin():
        article = save(db, origin, 'version-read', '合成旧刊标题', at(day - timedelta(days=1), 12), at(day - timedelta(days=1), 12))
        article_id, old_evidence = str(article.id), str(article.latest_evidence_id)
        edition = create_daily(db, started(db, day), day, at(day, 9))
        edition_id = str(edition.id)
        save(db, origin, 'version-read', '合成更正标题', at(day, 12), at(day, 11))
        latest_evidence = str(article.latest_evidence_id)
    path = '/api/v1/news/items/' + article_id + '/state'
    response = client.post(path, json={'read': True, 'evidence_id': old_evidence},
        headers={**headers, 'Idempotency-Key': 'read-old-evidence'})
    assert response.status_code == 200, response.text
    assert response.json()['data']['title'] == '合成旧刊标题'
    assert response.json()['data']['is_read']
    assert api_items(client, unread=True)['total'] == 1
    old_item = client.get('/api/v1/news/editions/' + edition_id).json()['data']['items'][0]
    assert old_item['is_read'] and old_item['evidence_id'] == old_evidence
    assert client.post(path, json={'read': True, 'evidence_id': latest_evidence},
        headers={**headers, 'Idempotency-Key': 'read-new-evidence'}).status_code == 200
    assert api_items(client, unread=True)['total'] == 0
    assert client.post(path, json={'read': False, 'evidence_id': old_evidence},
        headers={**headers, 'Idempotency-Key': 'unread-old-evidence'}).status_code == 200
    assert api_items(client, unread=True)['total'] == 0
    assert not client.get('/api/v1/news/editions/' + edition_id).json()['data']['items'][0]['is_read']


def test_read_version_rejects_another_article_and_unrelated_fields(client):
    headers = login(client)
    origin = source(client, headers)
    now = datetime.now(UTC) - timedelta(hours=1)
    with Session(engine()) as db, db.begin():
        first = save(db, origin, 'version-one', '合成第一资料', now, now)
        article_id = str(first.id)
        other_id = str(save(db, origin, 'version-other', '合成另一资料', now, now).latest_evidence_id)
    path = '/api/v1/news/items/' + article_id + '/state'
    response = client.post(path, json={'read': True, 'evidence_id': other_id},
        headers={**headers, 'Idempotency-Key': 'foreign-evidence'})
    assert response.status_code == 404
    for index, body in enumerate([{'evidence_id': other_id}, {'saved': True, 'evidence_id': other_id}, {'read': None}, {'read': True, 'evidence_id': None}]):
        assert client.post(path, json=body, headers={**headers, 'Idempotency-Key': 'bad-read-version-' + str(index)}).status_code == 422


def test_permission_revocation_hides_feed_detail_and_saved_article(client):
    headers = login(client)
    origin = source(client, headers)
    now = datetime.now(UTC) - timedelta(hours=1)
    with Session(engine()) as db, db.begin():
        article_id = str(save(db, origin, 'rights', '合成权限资料', now, now).id)
    assert api_items(client)['total'] == 1
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, origin)
        provider.rights = {**provider.rights, 'cache': 'denied'}
    assert api_items(client)['items'] == []
    assert client.get('/api/v1/news/items/' + article_id).status_code == 404


def test_gets_do_not_index_generate_jobs_or_contact_sources(client, monkeypatch):
    login(client)
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('GET must not contact a source'))
    with Session(engine()) as db:
        before = [db.scalar(select(func.count()).select_from(model)) for model in (Job, NewsArticle, NewsEdition, NewsUserState)]
    assert api_items(client)['ai_status']['state'] == 'disabled'
    assert api_items(client)['ai_status']['reason'] == 'period_digest_not_implemented'
    assert client.get('/api/v1/news/editions').json()['data']['items'] == []
    assert client.get('/api/v1/news/ai-status').json()['data']['state'] == 'disabled'
    with Session(engine()) as db:
        after = [db.scalar(select(func.count()).select_from(model)) for model in (Job, NewsArticle, NewsEdition, NewsUserState)]
    assert before == after


@pytest.mark.parametrize('clock,expected', [('2026-10-25T07:59:59+08:00', '2026-10-24'), ('2026-10-25T08:00:00+08:00', '2026-10-25')])
def test_beijing_cutoff(clock, expected):
    assert str(last_daily_day(datetime.fromisoformat(clock))) == expected


def test_enabling_before_eight_waits_for_first_issue_date(client):
    login(client)
    day = date(2026, 10, 25)
    with Session(engine()) as db, db.begin():
        assert schedule_editions(db, at(day, 7, 59)) == 0
        assert db.get(NewsCenterState, 1).first_day == day
        assert db.scalar(select(func.count()).select_from(NewsEdition)) == 0
        assert schedule_editions(db, at(day, 8)) == 1
        assert db.scalar(select(NewsEdition.day)) == day


def test_edition_freezes_versions_and_excludes_after_cutoff_and_unknown_date(client):
    headers = login(client)
    origin = source(client, headers)
    day = date(2026, 10, 24)
    with Session(engine()) as db, db.begin():
        save(db, origin, 'versioned', '合成截止前资料', at(day - timedelta(days=1), 9), at(day - timedelta(days=1), 9))
        save(db, origin, 'versioned', '合成截止后更正', at(day, 8) + timedelta(minutes=1), at(day - timedelta(days=1), 9))
        save(db, origin, 'future', '合成接收晚于截止', at(day, 8), at(day, 8))
        save(db, origin, 'unknown', '合成来源日期未知', at(day - timedelta(days=1), 10))
        state = started(db, day)
        edition = create_daily(db, state, day, at(day, 12))
        ident, digest = str(edition.id), edition.content_hash
    value = client.get('/api/v1/news/editions/' + ident).json()['data']
    assert [row['title'] for row in value['items']] == ['合成截止前资料']
    with Session(engine()) as db, db.begin():
        save(db, origin, 'versioned', '合成更晚资料', at(day, 13), at(day - timedelta(days=1), 9))
        state = db.get(NewsCenterState, 1)
        again = create_daily(db, state, day, at(day + timedelta(days=1), 12))
        assert str(again.id) == ident and again.content_hash == digest
        assert again.content_hash == content_hash(again.manifest)
    assert client.get('/api/v1/news/editions/' + ident).json()['data']['items'][0]['title'] == '合成截止前资料'


def test_date_only_and_late_news_are_labelled_without_fabricated_clock(client):
    headers = login(client)
    origin = source(client, headers, 'cninfo_announcements')
    day = date(2026, 10, 24)
    with Session(engine()) as db, db.begin():
        save(db, origin, 'date-only', '合成公告仅日期', at(day - timedelta(days=1), 14), day=day - timedelta(days=1))
        save(db, origin, 'late', '合成迟到公告', at(day - timedelta(days=1), 15), day=day - timedelta(days=3))
        edition = create_daily(db, started(db, day), day, at(day, 10))
        ident = str(edition.id)
    values = client.get('/api/v1/news/editions/' + ident).json()['data']['items']
    assert len(values) == 2 and all(row['published_at'] is None for row in values)
    assert next(row for row in values if row['title'] == '合成迟到公告')['late'] is True


def test_catchup_coverage_does_not_borrow_a_post_deadline_source_run(client):
    headers = login(client)
    origin = source(client, headers)
    day = date(2026, 10, 24)
    with Session(engine()) as db, db.begin():
        descriptor = db.scalar(select(SourceVersion).where(SourceVersion.source_id == origin).order_by(SourceVersion.recorded_at.desc())).descriptor
        db.add(SourceVersion(source_id=origin, recorded_at=at(day - timedelta(days=1), 7), descriptor=descriptor, content_hash=content_hash(descriptor)))
        original = DataFetchRun(source_id=origin, capability='news', started_at=at(day - timedelta(days=1), 12),
            received_at=at(day - timedelta(days=1), 12), status='partial', reason='truncated', expected=99,
            accepted=30, rejected=0, duplicates=0, request_count=1)
        later = DataFetchRun(source_id=origin, capability='news', started_at=at(day, 9), received_at=at(day, 9),
            status='complete', reason=None, expected=99, accepted=99, rejected=0, duplicates=0, request_count=4)
        db.add_all([original, later])
        db.flush()
        edition = create_daily(db, started(db, day), day, at(day, 12))
        assert edition.coverage_status == 'partial'
        assert edition.manifest['coverage'][0]['fetch_run_id'] == str(original.id)
        assert edition.manifest['coverage'][0]['accepted'] == 30
        assert edition.manifest['coverage'][0]['source_version_id']
        assert edition.content_hash == content_hash(edition.manifest)


def test_revoked_sources_are_rechecked_for_frozen_editions(client):
    headers = login(client)
    origin = source(client, headers)
    day = date(2026, 10, 24)
    with Session(engine()) as db, db.begin():
        save(db, origin, 'edition-rights', '合成报刊来源', at(day - timedelta(days=1), 12), at(day - timedelta(days=1), 12))
        edition = create_daily(db, started(db, day), day, at(day, 10))
        ident = str(edition.id)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, origin)
        provider.rights = {**provider.rights, 'display': 'denied'}
    value = client.get('/api/v1/news/editions/' + ident).json()['data']
    assert value['items'] == [] and value['edition']['item_count'] == 0
    assert value['edition']['coverage_status'] == 'partial'


def test_catchup_is_idempotent_and_reports_partial_first_period(client):
    headers = login(client)
    origin = source(client, headers)
    first = date(2026, 9, 30)
    with Session(engine()) as db, db.begin():
        save(db, origin, 'month-edge', '合成月末政策', at(first - timedelta(days=1), 12), at(first - timedelta(days=1), 12))
        started(db, first, beginning=at(first, 9))
        current = at(date(2026, 10, 5), 10, 31)
        assert schedule_editions(db, current) == 6
        assert schedule_editions(db, current) == 0
        assert db.scalar(select(func.count()).select_from(NewsEdition).where(NewsEdition.period == 'day')) == 6
        month = db.scalar(select(NewsEdition).where(NewsEdition.period == 'month'))
        week = db.scalar(select(NewsEdition).where(NewsEdition.period == 'week'))
        assert month and month.day == date(2026, 9, 1) and month.coverage_status == 'partial'
        assert week and week.day == date(2026, 9, 28) and week.coverage_status == 'partial'
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == 'news.ai')) == 0


def test_weekly_monthly_limits_and_cross_day_duplicate_articles(client):
    headers = login(client)
    origin = source(client, headers)
    first = date(2026, 9, 1)
    with Session(engine()) as db, db.begin():
        state = started(db, first)
        for offset in range(4):
            day = first + timedelta(days=offset)
            for count in range(12):
                received = at(day - timedelta(days=1), 12) + timedelta(minutes=count)
                save(db, origin, str(offset) + '-' + str(count), f'合成产业{offset}条目{count}', received, received)
            create_daily(db, state, day, at(day, 10))
        week = create_period(db, state, 'week', first - timedelta(days=first.weekday()), at(date(2026, 9, 7), 10))
        month = create_period(db, state, 'month', first, at(date(2026, 10, 1), 11))
        assert db.scalar(select(func.count()).select_from(NewsEditionItem).where(NewsEditionItem.edition_id == week.id)) == 20
        assert db.scalar(select(func.count()).select_from(NewsEditionItem).where(NewsEditionItem.edition_id == month.id)) == 30


def test_news_endpoints_require_auth_and_validate_inputs(client):
    for path in ['/api/v1/news/items', '/api/v1/news/editions', '/api/v1/news/ai-status']:
        assert client.get(path).status_code == 401
    login(client)
    assert client.get('/api/v1/news/items', params={'family': 'fake-source'}).status_code == 422
    assert client.get('/api/v1/news/items', params={'category': '买入信号'}).status_code == 422
    assert client.get('/api/v1/news/items', params={'limit': 101}).status_code == 422
