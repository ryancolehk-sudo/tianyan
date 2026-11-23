"""Synthetic bars on isolated real PostgreSQL; no vendor or personal DB access."""
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import content_hash, normalized
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import BarPayload, EvidenceInput
from app.market.collect import store_observation
from app.market.datasets import bars, current_rows
from app.market.models import SourceSettings
from app.market.registry import SOURCES
from app.market.service import source_id
from app.models import Instrument, Provider
from test_data_sources import configuration, configure, login

OLD = datetime(2026, 10, 10, 8, tzinfo=UTC)
NEW = datetime(2026, 10, 23, 8, tzinfo=UTC)
DAY = date(2026, 10, 9)


def setup(client):
    headers = login(client)
    for family in ('tushare_daily', 'baostock_history', 'sina_minute_1m'):
        body = configuration(family)
        body['symbols'] = [dict(exchange='SSE', symbol='603278', asset_type='stock')]
        body['interval_seconds'] = SOURCES[family].minimum_interval
        configure(client, headers, body, key='daily-isolation-' + family)
    with Session(engine()) as db, db.begin():
        stock = Instrument(exchange='SSE', symbol='603278', asset_type='stock', name='合成周期隔离股票')
        db.add(stock); db.flush()
        ident = stock.id
    return ident


def daily(day=DAY, adjustment='unadjusted', close='11', *, omit_adjustment=False):
    values = dict(open='10', high='13', low='9', close=close, volume_shares=None,
        interval='1d', trade_day=day, bar_end_at=None)
    if not omit_adjustment:
        values['adjustment'] = adjustment
    return BarPayload(**values)


def store_daily(db, ident, family='tushare_daily', payload=None, received=OLD, key=None):
    provider = db.get(Provider, source_id(family))
    config = db.get(SourceSettings, provider.id)
    payload = payload or daily()
    slot = key or f'bar:SSE:stock:603278:1d:{payload.adjustment}:{payload.trade_day}'
    assert store_observation(db, provider, config, source_key=slot, kind='bar', payload=payload,
        received_at=received, instrument_id=ident)
    return db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id == provider.id,
        EvidenceVersion.source_key == slot).order_by(EvidenceVersion.version_number.desc()).limit(1))


def bulk_current(db, ident, family, payloads):
    """Insert authentic-shaped immutable synthetic versions efficiently.

    Large slot sets exercise SQL LIMIT ordering, not network or 2000 individual
    advisory-lock round trips. Each row has a schema-checked payload and a real
    EvidenceInput content hash; no existing row is updated or removed.
    """
    provider = db.get(Provider, source_id(family))
    config = db.get(SourceSettings, provider.id)
    records = []
    for index, payload in enumerate(payloads):
        slot = f'synthetic:{family}:{index}'
        body = EvidenceInput(source_id=provider.id, source_key=slot, source_revision=content_hash(payload),
            kind='bar', instrument_id=ident, payload=payload, rights_basis=config.rights_basis)
        records.append(EvidenceVersion(id=uuid.uuid4(), source_id=provider.id, source_key=slot,
            source_revision=body.source_revision, kind='bar', instrument_id=ident, owner_id=None,
            visibility='shared', version_number=1, received_at=NEW, event_at=None, published_at=None,
            available_at=None, availability='unknown', provenance='source_reported', historical_eligible=False,
            rights_basis=config.rights_basis, payload=normalized(payload), content_hash=content_hash(body)))
    db.add_all(records); db.flush()


def test_more_than_2000_newer_minutes_cannot_displace_existing_daily_bar(client):
    ident = setup(client)
    with Session(engine()) as db, db.begin():
        saved = store_daily(db, ident)
        expected_id, expected_hash, expected_payload = saved.id, saved.content_hash, dict(saved.payload)
        payloads = []
        for index in range(2001):
            day = date(2026, 10, 10) + timedelta(days=index // 240)
            slot = index % 240
            minute = 9 * 60 + 31 + slot if slot < 120 else 13 * 60 + 1 + slot - 120
            stamp = datetime.combine(day, datetime.min.time(), UTC) + timedelta(minutes=minute - 8 * 60)
            payloads.append(BarPayload(open='10', high='13', low='9', close='12', volume_shares=None,
                interval='1m', trade_day=day, source_clock_at=stamp, bar_end_at=None, clock_semantics='unknown'))
        bulk_current(db, ident, 'sina_minute_1m', payloads)
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.kind == 'bar')) == 2002
    response = client.get('/api/v1/market-bars', params=dict(instrument_id=str(ident), interval='1d'))
    assert response.status_code == 200
    items = response.json()['data']['items']
    assert len(items) == 1 and items[0]['id'] == str(expected_id)
    assert items[0]['content_hash'] == expected_hash and items[0]['payload'] == expected_payload
    with Session(engine()) as db:
        # Unfiltered generic readers retain their original limit/order behavior.
        generic = current_rows(db, 'bar', instrument_id=ident, maximum=1)
        assert len(generic) == 1 and generic[0][0].payload['interval'] == '1m'


def test_adjustment_filter_precedes_limit_and_legacy_missing_basis_remains_unadjusted(client):
    ident = setup(client)
    with Session(engine()) as db, db.begin():
        saved = store_daily(db, ident, payload=daily(omit_adjustment=True))
        expected_id, expected_hash = saved.id, saved.content_hash
        assert 'adjustment' not in saved.payload
        # More recent daily rows of another price basis must not consume the
        # unadjusted row budget, even though their interval/family are allowed.
        bulk_current(db, ident, 'baostock_history', [daily(day=date(2000, 1, 1)+timedelta(days=i),
            adjustment='forward', close='12') for i in range(2001)])
    with Session(engine()) as db:
        view = bars(db, ident, 'unadjusted')
        assert len(view.items) == 1 and view.items[0].id == expected_id
        assert view.items[0].content_hash == expected_hash
        assert 'adjustment' not in view.items[0].payload.model_dump()
        assert view.coverage_start_day == view.coverage_end_day == DAY
        adjusted = bars(db, ident, 'forward')
        assert len(adjusted.items) == 400
        assert all(item.payload.adjustment == 'forward' for item in adjusted.items)
        # Coverage uses every eligible current day, independently of the page
        # limit, without leaking another provider or adjustment's dates.
        assert adjusted.coverage_start_day == date(2000, 1, 1)
        assert adjusted.coverage_end_day == date(2000, 1, 1) + timedelta(days=2000)


def test_explicit_source_is_selected_before_same_day_priority_deduplication(client):
    ident = setup(client)
    with Session(engine()) as db, db.begin():
        free = store_daily(db, ident, family='baostock_history', payload=daily(close='10'))
        free_id, free_hash = free.id, free.content_hash
        paid = store_daily(db, ident, payload=daily(close='12'), received=NEW)
        paid_id = paid.id
    with Session(engine()) as db:
        default = bars(db, ident, 'unadjusted')
        # ADR0029 gives the configured paid feed default priority. Explicit
        # source selection must still happen before same-day deduplication.
        assert len(default.items) == 1 and default.items[0].id == paid_id
        explicit_paid = bars(db, ident, 'unadjusted', source_id=source_id('tushare_daily'))
        assert len(explicit_paid.items) == 1 and explicit_paid.items[0].id == paid_id
        selected = bars(db, ident, 'unadjusted', source_id=source_id('baostock_history'))
        assert len(selected.items) == 1 and selected.items[0].id == free_id
        assert selected.items[0].payload.close == '10' and selected.items[0].content_hash == free_hash
        assert bars(db, ident, 'unadjusted', source_id=source_id('sina_minute_1m')).items == []


@pytest.mark.parametrize('revoke', ['display', 'cache', 'entitlement', 'enabled'])
def test_explicit_daily_source_still_rechecks_permissions(client, revoke):
    ident = setup(client)
    with Session(engine()) as db, db.begin():
        store_daily(db, ident, family='baostock_history')
        store_daily(db, ident, received=NEW)
        provider = db.get(Provider, source_id('baostock_history'))
        if revoke in {'display', 'cache'}:
            provider.rights = {**provider.rights, revoke:'denied'}
        elif revoke == 'entitlement':
            provider.entitlement_status = 'denied'
        else:
            provider.enabled = False
    with Session(engine()) as db:
        assert bars(db, ident, 'unadjusted', source_id=source_id('baostock_history')).items == []
        assert len(bars(db, ident, 'unadjusted').items) == 1


def test_filtering_current_revision_never_resurrects_superseded_price_basis(client):
    ident = setup(client)
    with Session(engine()) as db, db.begin():
        first = store_daily(db, ident, family='baostock_history', key='synthetic-stable-slot')
        previous_id = first.id
        latest = store_daily(db, ident, family='baostock_history', payload=daily(adjustment='forward', close='12'),
            received=NEW, key='synthetic-stable-slot')
        assert latest.supersedes_id == previous_id
        latest_id = latest.id
    with Session(engine()) as db:
        # Rank first, then filter, then limit: an old matching payload is not
        # allowed to replace an unmatched current correction in the same scope.
        assert bars(db, ident, 'unadjusted').items == []
        assert bars(db, ident, 'forward').items[0].id == latest_id
