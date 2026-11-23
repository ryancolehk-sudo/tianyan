"""Source identity and old-archive recovery, using synthetic disposable evidence."""
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import content_hash, normalized
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import NewsPayload, ReferencePayload
from app.market.collect import store_news_batch, store_observation
from app.market.models import SourceSettings
from app.market.public import parse_public
from app.models import Provider
from app.news_center.editions import at, create_daily
from app.news_center.securities import news_securities
from test_data_sources import login
from test_news_center import api_items, source, started


def store_news(db, origin, identity, received, *, securities=None, title='董事会决议公告'):
    provider = db.get(Provider, origin)
    extra = {} if securities is None else {'securities': securities}
    payload = NewsPayload(title=title, summary=None, category='公告', published_day=received.date(),
        source_url='https://www.cninfo.com.cn/new/disclosure/detail?announcementId=' + identity + '&orgId=synthetic-org', **extra)
    store_news_batch(db, provider, db.get(SourceSettings, origin), SimpleNamespace(records=[(identity, payload, None)]), received)
    return db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id == origin,
        EvidenceVersion.source_key == 'news:' + identity).order_by(EvidenceVersion.version_number.desc()))


def archive(db, origin, identity, received, code='000001.SZ', *, available=None, provenance='source_reported'):
    row = {'id': identity, 'ts_code': code,
        'source_url': 'https://static.cninfo.com.cn/finalpage/2026-10-23/' + identity + '.pdf'}
    payload = ReferencePayload(dataset='tomorrow.announcement_text', title='合成公告档案',
        source_url='https://www.cninfo.com.cn/new/hisAnnouncement/query', attribution='合成巨潮来源',
        scope='仅为隔离测试的明确公告ID及证券代码',
        columns=[{'key': key, 'label': key} for key in row], rows=[row])
    provider = db.get(Provider, origin)
    key = 'reference:synthetic-announcement:' + str(uuid.uuid4())
    if provenance == 'source_reported':
        store_observation(db, provider, db.get(SourceSettings, origin), source_key=key, kind='reference',
            payload=payload, received_at=received, published_at=available, eligible=available is not None)
    else:
        db.add(EvidenceVersion(source_id=origin, owner_id=None, visibility='shared', source_key=key,
            source_revision=content_hash(payload), version_number=1, kind='reference', instrument_id=None,
            event_at=None, published_at=None, received_at=received, available_at=available,
            availability='known' if available else 'unknown', historical_eligible=available is not None,
            provenance=provenance, rights_basis='合成未核实资料，仅用于反例', payload=normalized(payload),
            content_hash=content_hash(payload)))
        db.flush()
    return db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_key == key))


def test_cninfo_parser_keeps_source_security_and_old_payload_hash_shape():
    stamp = datetime(2026, 10, 24, 2, tzinfo=UTC)
    item = {'announcementId': 'synthetic-id', 'announcementTitle': '董事会决议公告',
        'announcementTime': int(stamp.timestamp() * 1000), 'secCode': '000001', 'secName': '合成证券甲'}
    parsed = parse_public('cninfo_announcements', json.dumps({'announcements': [item]}).encode(), stamp)
    security = parsed.records[0][1].securities[0]
    assert (security.code, security.name, security.exchange) == ('000001', '合成证券甲', None)
    item['secCode'] = '1'
    assert not parse_public('cninfo_announcements', json.dumps({'announcements': [item]}).encode(), stamp).records[0][1].securities
    old = {'title': '合成旧公告 600000', 'summary': None, 'source_url': 'https://example.invalid/old'}
    assert normalized(NewsPayload.model_validate(old)) == old
    assert content_hash(NewsPayload.model_validate(old)) == content_hash(old)
    native = {**old, 'secCode': '000001', 'secName': '合成已存简称'}
    assert normalized(NewsPayload.model_validate(native)) == native
    assert content_hash(NewsPayload.model_validate(native)) == content_hash(native)


@pytest.mark.parametrize('payload, expected, status', [
    ({'secCode': '000001', 'secName': '合成旧简称'}, ('000001', '合成旧简称'), 'source_provided'),
    ({'secCode': '000001', 'secName': ' '}, ('000001', None), 'source_provided'),
    ({'securities': [{'code': '600000', 'name': '合成已存证券'}]}, ('600000', '合成已存证券'), 'source_provided'),
    ({'secCode': 'invalid', 'secName': '合成旧简称'}, None, 'unverified'),
    ({'title': '600000 合成公司董事会决议'}, None, 'missing'),
])
def test_already_stored_structured_fields_are_projected_without_title_inference(payload, expected, status):
    evidence = SimpleNamespace(id=uuid.uuid4(), source_id=uuid.uuid4(), payload={'source_url': 'https://example.invalid/old', **payload},
        received_at=datetime(2026, 10, 24, tzinfo=UTC), content_hash='a' * 64, provenance='source_reported', source_key='news:old')
    provider = SimpleNamespace(family='cninfo_announcements', name='合成旧来源')
    subjects, actual_status = news_securities(None, [(evidence, provider)])[evidence.id]
    assert actual_status == status
    assert ((subjects[0]['code'], subjects[0]['name']) if subjects else None) == expected


def test_feed_focus_and_detail_share_verified_security_fields_and_version(client):
    origin = source(client, login(client), 'cninfo_announcements')
    with Session(engine()) as db, db.begin():
        evidence = store_news(db, origin, 'source-fields', at(date(2026, 10, 23), 17),
            securities=[{'code': '000001', 'name': '合成证券甲'}])
        digest, evidence_id = evidence.content_hash, str(evidence.id)
    feed = api_items(client)
    article = feed['items'][0]
    detail = client.get('/api/v1/news/items/' + article['id']).json()['data']
    for value in [article, feed['focus'], detail]:
        assert value['security_status'] == 'source_provided'
        assert value['securities'][0]['code'] == '000001'
        assert value['securities'][0]['name'] == '合成证券甲'
        assert value['securities'][0]['evidence_id'] == evidence_id
        assert value['content_hash'] == digest


def test_unverified_material_cannot_claim_a_source_security():
    evidence = SimpleNamespace(id=uuid.uuid4(), payload={'securities': [{'code': '000001', 'name': '合成声称'}]},
        provenance='unverified')
    subjects, status = news_securities(None, [(evidence, SimpleNamespace(family='chinanews_finance'))])[evidence.id]
    assert subjects == [] and status == 'unverified'


def test_exact_archived_announcement_id_recovers_old_code_but_not_a_future_edition_identity(client):
    origin = source(client, login(client), 'cninfo_announcements')
    day = date(2026, 10, 24)
    with Session(engine()) as db, db.begin():
        evidence = store_news(db, origin, 'archived-id', at(day - timedelta(days=1), 17))
        old_id, old_hash, old_payload = evidence.id, evidence.content_hash, dict(evidence.payload)
        edition = create_daily(db, started(db, day), day, at(day, 10))
        edition_id = str(edition.id)
        proof = archive(db, origin, 'archived-id', at(day, 9))
        proof_id = str(proof.id)
    article = api_items(client)['items'][0]
    assert article['securities'][0]['code'] == '000001'
    assert article['securities'][0]['name'] is None
    assert article['securities'][0]['basis'] == 'archived_announcement'
    assert article['securities'][0]['evidence_id'] == proof_id
    assert article['security_status'] == 'unverified'
    historical = client.get('/api/v1/news/editions/' + edition_id).json()['data']['items'][0]
    assert historical['securities'] == [] and historical['security_status'] == 'missing'
    with Session(engine()) as db:
        unchanged = db.get(EvidenceVersion, old_id)
        assert unchanged.content_hash == old_hash and unchanged.payload == old_payload


def test_archive_recovery_rejects_unverified_or_not_yet_available_proofs(client):
    origin = source(client, login(client), 'cninfo_announcements')
    day = date(2026, 10, 24)
    received = at(day - timedelta(days=1), 17)
    with Session(engine()) as db, db.begin():
        for key in ['known-proof', 'unknown-clock', 'future-proof', 'unverified-proof']:
            store_news(db, origin, key, received)
        archive(db, origin, 'known-proof', received, available=received)
        archive(db, origin, 'unknown-clock', received)
        archive(db, origin, 'future-proof', received, available=datetime.now(UTC) + timedelta(days=1))
        archive(db, origin, 'unverified-proof', received, available=received, provenance='unverified')
        edition = create_daily(db, started(db, day), day, at(day, 10))
        edition_id = str(edition.id)
    current = {row['source_url'].split('announcementId=')[1].split('&')[0]: row for row in api_items(client)['items']}
    assert current['known-proof']['security_status'] == 'source_provided'
    assert current['unknown-clock']['security_status'] == 'unverified'
    assert current['unknown-clock']['securities'][0]['code'] == '000001'
    for key in ['future-proof', 'unverified-proof']:
        assert current[key]['securities'] == []
    historical = client.get('/api/v1/news/editions/' + edition_id).json()['data']['items']
    for row in historical:
        key = row['source_url'].split('announcementId=')[1].split('&')[0]
        assert bool(row['securities']) == (key == 'known-proof')


def test_missing_or_conflicting_archive_does_not_guess_a_subject(client):
    origin = source(client, login(client), 'cninfo_announcements')
    received = at(date(2026, 10, 23), 17)
    with Session(engine()) as db, db.begin():
        store_news(db, origin, 'conflicting-id', received)
        store_news(db, origin, 'unmatched-id', received, title='600000 合成名称年度公告')
        archive(db, origin, 'conflicting-id', received + timedelta(minutes=1), '000001.SZ')
        archive(db, origin, 'conflicting-id', received + timedelta(minutes=2), '600000.SH')
        archive(db, origin, 'different-id', received, '600000.SH')
    articles = api_items(client)['items']
    conflict = next(row for row in articles if 'conflicting-id' in row['source_url'])
    missing = next(row for row in articles if 'unmatched-id' in row['source_url'])
    assert conflict['securities'] == [] and conflict['security_status'] == 'unverified'
    assert missing['securities'] == [] and missing['security_status'] == 'missing'
