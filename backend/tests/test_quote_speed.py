"""Same-source speed selection on real disposable PostgreSQL, no vendor calls."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion, SourceVersion
from app.evidence.schemas import QuotePayload
from app.market.quote_speed import quote_speeds
from app.market.schemas import MarketIdentity, MarketQuote, QuoteProvenance
from app.market.service import source_id
from app.models import Instrument, Provider, User

CLOCK = datetime(2026, 11, 10, 2, 30, tzinfo=UTC)
SOURCE = source_id('tencent_web')


@pytest.fixture
def sample():
    # This reader consumes already-selected quotes. Seed real database rows
    # without depending on a collector configuration endpoint.
    with Session(engine()) as db, db.begin():
        source = Provider(id=SOURCE, name='合成报价源', kind='market', family='tencent_web',
            adapter_version='synthetic', configured=True, enabled=True,
            entitlement_status='verified', rights={'display': 'allowed', 'cache': 'allowed'})
        owner = User(id=uuid.uuid4(), username='synthetic-speed-owner',
            password_hash='synthetic-unusable-hash', role='admin', enabled=True)
        instruments = [Instrument(id=uuid.uuid4(), exchange='SZSE', symbol=code,
            asset_type='stock', name='合成'+code, valid_from=None, valid_to=None,
            first_received_at=CLOCK-timedelta(days=1)) for code in ('000001', '000002')]
        db.add_all([source, owner, *instruments]); db.flush()
        descriptor={'name': '合成报价源'}
        db.add(SourceVersion(source_id=SOURCE, recorded_at=CLOCK-timedelta(days=1),
            descriptor=descriptor, content_hash=content_hash(descriptor)))
        return SimpleNamespace(owner=owner.id, first=instruments[0].id, second=instruments[1].id)


def previous(db, instrument, seconds, price='10', *, source=None, received=None,
             available=None, eligible=True, payload=None, provenance='source_reported',
             owner=None, version=1, ident=None):
    stamp = CLOCK-timedelta(seconds=seconds)
    data = {'price': price, 'price_unit': 'CNY', 'quote_kind': 'snapshot', **(payload or {})}
    item = EvidenceVersion(
        id=ident or uuid.uuid4(), source_id=source or SOURCE,
        owner_id=owner, visibility='private' if owner else 'shared',
        source_key='synthetic-speed:'+uuid.uuid4().hex, source_revision=uuid.uuid4().hex,
        version_number=version, kind='quote', instrument_id=instrument, event_at=stamp,
        published_at=None, received_at=received or stamp, available_at=available or received or stamp,
        availability='known', provenance=provenance, historical_eligible=eligible,
        rights_basis='synthetic offline test', payload=data, content_hash=content_hash(data))
    db.add(item)
    return item


def current(db, instrument, seconds=0, price='11'):
    evidence = previous(db, instrument, seconds, price)
    identity = db.get(Instrument, instrument)
    version = db.scalar(select(SourceVersion).where(SourceVersion.source_id == SOURCE))
    return MarketQuote(
        instrument=MarketIdentity(id=identity.id, exchange=identity.exchange, symbol=identity.symbol,
            asset_type='stock', name=identity.name), state='available', freshness_limit_seconds=120,
        quote=QuotePayload(price=price, quote_time=evidence.event_at, price_unit='CNY'),
        provenance=QuoteProvenance(source_id=SOURCE, family='tencent_web', source_name='合成报价源',
            source_version_id=version.id, adapter_version='synthetic', evidence_version_id=evidence.id,
            content_hash=evidence.content_hash, received_at=evidence.received_at,
            published_at=None, available_at=evidence.available_at, historical_eligible=True))


def reader(db):
    return SimpleNamespace(db=db, cutoff=CLOCK, record_input=lambda evidence: None)


def test_windows_keep_each_instrument_source_anchor_and_valid_nearest_price(sample):
    with Session(engine()) as db, db.begin():
        quotes={'000001.SZ': current(db, sample.first), '000002.SZ': current(db, sample.second, 60, '24')}
        other_source = db.scalar(select(Provider.id).where(Provider.id != SOURCE))
        assert other_source is not None
        # Equal distance: the newer receipt wins. A missing legacy kind is a snapshot.
        chosen = previous(db, sample.first, 301, received=CLOCK-timedelta(seconds=10))
        chosen.payload = {'price': '10', 'price_unit': 'CNY'}
        chosen.content_hash = content_hash(chosen.payload)
        previous(db, sample.first, 299, '8', received=CLOCK-timedelta(seconds=20))
        previous(db, sample.first, 300, None)
        previous(db, sample.first, 300, '1', payload={'price_unit': 'unknown'})
        previous(db, sample.first, 300, '1', payload={'quote_kind': 'daily_close'})
        previous(db, sample.first, 300, '1', source=other_source)
        previous(db, sample.first, 300, '1', provenance='unverified')
        previous(db, sample.first, 300, '1', owner=sample.owner)
        previous(db, sample.first, 300, '1', received=CLOCK+timedelta(seconds=1))
        second_chosen = previous(db, sample.second, 360, '20')
        previous(db, sample.second, 300, '1')  # Outside this stock's own five-minute window.
        expected_ids = {chosen.id, second_chosen.id}
    with Session(engine()) as db:
        inputs=[]; context=reader(db)
        context.record_input=lambda evidence: inputs.append(evidence.id)
        assert quote_speeds(context, quotes) == {
            '000001.SZ': (pytest.approx(10), 301), '000002.SZ': (pytest.approx(20), 300)}
        assert set(inputs) == expected_ids


@pytest.mark.parametrize('strict,expected', [(False, (120, 300)), (True, (10, 298))])
def test_historical_eligibility_and_availability_are_applied_before_nearest_selection(sample, strict, expected):
    with Session(engine()) as db, db.begin():
        quotes={'000001.SZ': current(db, sample.first)}
        previous(db, sample.first, 300, '5', eligible=False)
        previous(db, sample.first, 299, '8', available=CLOCK+timedelta(seconds=1))
        previous(db, sample.first, 298, '10')
    with Session(engine()) as db:
        assert quote_speeds(reader(db), quotes, strict=strict) == {
            '000001.SZ': (pytest.approx(expected[0]), expected[1])}


def test_equal_clock_and_receipt_keep_version_then_id_tiebreakers(sample):
    receipt=CLOCK-timedelta(seconds=10)
    with Session(engine()) as db, db.begin():
        quotes={'000001.SZ': current(db, sample.first)}
        previous(db, sample.first, 300, '1', received=receipt, version=1, ident=uuid.UUID(int=90))
        previous(db, sample.first, 300, '5', received=receipt, version=2, ident=uuid.UUID(int=2))
        chosen=previous(db, sample.first, 300, '10', received=receipt, version=2, ident=uuid.UUID(int=3))
        expected_id=chosen.id
    with Session(engine()) as db:
        inputs=[]; context=reader(db)
        context.record_input=lambda evidence: inputs.append(evidence.id)
        assert quote_speeds(context, quotes) == {'000001.SZ': (pytest.approx(10), 300)}
        assert inputs == [expected_id]


@pytest.mark.parametrize('elapsed,expected', [(269, None), (270, 270), (330, 330), (331, None)])
def test_window_tolerance_keeps_inclusive_thirty_second_edges(sample, elapsed, expected):
    with Session(engine()) as db, db.begin():
        quotes={'000001.SZ': current(db, sample.first)}
        previous(db, sample.first, elapsed)
    with Session(engine()) as db:
        actual=quote_speeds(reader(db), quotes)
        assert actual == ({} if expected is None else {'000001.SZ': (pytest.approx(10), expected)})
