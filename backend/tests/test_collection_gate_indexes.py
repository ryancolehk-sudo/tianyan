"""Actual PostgreSQL plans and unchanged rights/cutoff semantics; offline only."""
from datetime import UTC, datetime, timedelta
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion
from app.market.mairui import pool_day
from app.market.protocol import SourceFailure
from app.market.service import source_id
from app.market.session import market_session
from app.models import AuditEvent, Provider, User

AT = datetime(2026, 11, 13, 6, 13, tzinfo=UTC)
REVISION = '0031_collection_gate_indexes'
AUDIT_INDEX = 'ix_audit_action_time'
CALENDAR_INDEX = 'ix_evidence_calendar_lookup'
ACTION = 'source.request.tencent-quotes'


def seed():
    """Small calendars amid same-source reference history and permit history."""
    with Session(engine()) as db, db.begin():
        user = User(username='synthetic-index-reader', password_hash='unusable-synthetic-fixture', role='viewer')
        db.add(user)
        db.flush()
        for family in ('tushare_calendar_sse', 'tushare_calendar_szse'):
            db.add(Provider(id=source_id(family), name='Synthetic calendar', family=family,
                kind='market', configured=True, enabled=True, entitlement_status='verified',
                adapter_version='synthetic-index-fixture', capabilities=['calendar'],
                rights={'display': 'allowed', 'cache': 'allowed'}))
        db.flush()
        rows = []
        for family, exchange in (('tushare_calendar_sse', 'SSE'), ('tushare_calendar_szse', 'SZSE')):
            def row(key, payload, revision='0', number=1, received=AT-timedelta(days=2),
                    kind='calendar', owner=None, provenance='source_reported'):
                return dict(id=uuid.uuid4(), source_id=source_id(family), owner_id=owner,
                    visibility='private' if owner else 'shared', source_key=key, source_revision=revision,
                    version_number=number, kind=kind, received_at=received, available_at=received,
                    availability='known', provenance=provenance, historical_eligible=True,
                    rights_basis='synthetic offline index test only', payload=payload, content_hash=content_hash(payload))
            for day in (AT.date()-timedelta(days=1), AT.date()):
                rows.append(row('calendar:'+exchange+':'+day.isoformat(),
                    {'exchange': exchange, 'trade_day': day.isoformat(), 'is_open': True}))
            current = 'calendar:'+exchange+':'+AT.date().isoformat()
            closed = {'exchange': exchange, 'trade_day': AT.date().isoformat(), 'is_open': False}
            rows.append(row(current, closed, revision='private', owner=user.id))
            rows.append(row(current, closed, revision='unverified', number=3, provenance='unverified'))
            if exchange == 'SSE':
                rows.append(row(current, closed, revision='late', number=2, received=AT+timedelta(seconds=10)))
            noise = {'dataset': 'synthetic-index-pressure', 'rows': []}
            rows.extend(row('noise:'+str(index), noise, kind='reference') for index in range(6000))
        db.execute(insert(EvidenceVersion), rows)
        permits = [dict(id=uuid.uuid4(), action=ACTION if index % 2 else 'source.request.tushare',
            created_at=AT-timedelta(days=index % 365), changed_field_names=['synthetic_offline_test'])
            for index in range(6000)]
        permits.append(dict(id=uuid.uuid4(), action=ACTION, created_at=AT+timedelta(days=1),
                            changed_field_names=['synthetic_future_permit']))
        db.execute(insert(AuditEvent), permits)
    with engine().begin() as db:
        db.execute(text('ANALYZE evidence_versions'))
        db.execute(text('ANALYZE audit_events'))


def read(*, scans=True):
    with Session(engine()) as db:
        if not scans:
            for option in ('enable_indexscan', 'enable_bitmapscan', 'enable_indexonlyscan'):
                db.execute(text('SET LOCAL '+option+'=off'))
        snapshots = []
        for clock in (AT, AT+timedelta(seconds=11)):
            try:
                day = pool_day(db, clock).isoformat()
            except SourceFailure as error:
                day = error.reason
            snapshots.append(dict(session=market_session(db, clock).model_dump(mode='json'), day=day))
        last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == ACTION))
        used = db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == ACTION,
            AuditEvent.created_at >= AT.replace(hour=0, minute=0), AuditEvent.created_at < AT.replace(hour=0, minute=0)+timedelta(days=1)))
        return snapshots, last, used


def names(node):
    return {node.get('Index Name')} | set().union(*(names(child) for child in node.get('Plans', [])))


def test_indexes_match_model_metadata_and_are_nonunique():
    with engine().connect() as db:
        actual = dict(db.execute(text("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public' AND indexname IN (:audit,:calendar)"),
            {'audit': AUDIT_INDEX, 'calendar': CALENDAR_INDEX}).all())
    assert set(actual) == {AUDIT_INDEX, CALENDAR_INDEX}
    assert all('UNIQUE INDEX' not in definition for definition in actual.values())
    assert AUDIT_INDEX in {index.name for index in AuditEvent.__table__.indexes}
    assert CALENDAR_INDEX in {index.name for index in EvidenceVersion.__table__.indexes}


def test_normal_plans_find_permits_and_calendar_without_scanning_source_history():
    seed()
    original = read(scans=False)
    assert read() == original
    snapshots, latest, used = original
    assert snapshots[0]['day'] == '2026-11-13' and snapshots[0]['session']['is_trading_day'] is True
    assert snapshots[1]['day'] == 'calendar_not_ready' and snapshots[1]['session']['is_trading_day'] is None
    assert latest == AT+timedelta(days=1) and used > 0
    with engine().connect() as db:
        maximum = db.execute(text('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT max(created_at) FROM audit_events WHERE action=:action'),
            {'action': ACTION}).scalar_one()[0]
        calendar = db.execute(text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT payload FROM evidence_versions WHERE kind='calendar' AND owner_id IS NULL AND visibility='shared' AND provenance='source_reported' AND source_id=:source AND payload->>'trade_day'=:day AND received_at<=:clock"),
            {'source': source_id('tushare_calendar_sse'), 'day': AT.date().isoformat(), 'clock': AT}).scalar_one()[0]
    assert AUDIT_INDEX in names(maximum['Plan'])
    assert CALENDAR_INDEX in names(calendar['Plan'])
    with Session(engine()) as db, db.begin():
        db.get(Provider, source_id('tushare_calendar_sse')).rights = {'display': 'denied', 'cache': 'allowed'}
    revoked = read()
    assert revoked == read(scans=False)
    assert all(snapshot['day'] == 'calendar_not_ready' for snapshot in revoked[0])
    assert all(snapshot['session']['is_trading_day'] is None for snapshot in revoked[0])


def test_nonempty_schema0030_upgrade_changes_only_indexes():
    with engine().connect() as db:
        current = db.scalar(text('SELECT version_num FROM alembic_version'))
    if current == REVISION:
        pytest.skip('Run the nonempty upgrade rehearsal separately from schema0030.')
    assert current == '0030_ths_member_capture_facet'
    seed()
    before = read()
    def digests():
        with engine().connect() as db:
            return {table: db.execute(text("SELECT count(*),md5(string_agg(to_jsonb(t)::text,',' ORDER BY id)) FROM "+table+' t')).one()
                    for table in ('audit_events', 'evidence_versions')}
    original = digests()
    command.upgrade(Config('alembic.ini'), REVISION)
    assert read() == before and digests() == original
    test_indexes_match_model_metadata_and_are_nonunique()
