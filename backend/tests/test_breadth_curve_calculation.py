"""Curve-only arithmetic preserves the full statistics path and raw evidence."""
from collections import OrderedDict
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import NoResultFound

from app.db import engine
from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion
from app.market import tushare_market as market
from app.market.service import source_id
from app.models import Provider
from test_tushare_market import AT, limits, quote

CURVE_FIELDS = ('limit_up', 'limit_down', 'comparable', 'source_time_max')


@pytest.fixture(autouse=True)
def comparisons(monkeypatch):
    monkeypatch.setattr(market, '_COMPARISONS', OrderedDict())
    monkeypatch.setattr('urllib.request.build_opener', lambda *a, **k: pytest.fail('Read calculations must stay offline'))


def test_curve_calculates_its_complete_fields_without_parsing_unused_amounts(monkeypatch):
    rows = [quote(price='11'), quote('000001.SZ', price='9'),
            quote('000002.SZ', price='10'), quote('000003.SZ', at=AT-timedelta(days=1))]
    bounds = [limits(row['ts_code']) for row in rows]
    memo = market.comparison_memo()
    native_decimal = market.Decimal
    parsed = []
    def decimal(value=0):
        parsed.append(value)
        return native_decimal(value)
    monkeypatch.setattr(market, 'Decimal', decimal)
    result = market.compare_limits(rows, bounds, AT, memo=memo, curve_only=True)
    assert result == dict(limit_up=1, limit_down=1, comparable=3, source_time_max=AT)
    assert '1000' not in parsed, 'The historical curve does not consume turnover totals'
    full = market.compare_limits(rows, bounds, AT)
    assert (full['advancing'], full['declining'], full['unchanged'], full['old_day']) == (1, 1, 1, 1)
    assert full['total_amount_cny'] == '3000' and full['returned'] == 4
    assert full['source_time_min'] == AT and full['fresh'] == 3
    assert '1000' in parsed


@pytest.mark.parametrize('amount', [None, '', 'malformed', 'NaN', '-1', 1000, True, False])
def test_unknown_and_legacy_amounts_do_not_change_curve_values_or_full_statistics(amount):
    rows = [dict(quote(price='11'), amount=amount)]
    original = deepcopy(rows)
    full = market.compare_limits(rows, [limits()], AT)
    curve = market.compare_limits(rows, [limits()], AT, curve_only=True)
    assert curve == {key: full[key] for key in CURVE_FIELDS}
    assert curve == dict(limit_up=1, limit_down=0, comparable=1, source_time_max=AT)
    assert rows == original


@pytest.mark.parametrize('amount', [[], {}])
def test_invalid_current_amount_types_keep_the_original_failure(amount):
    rows = [dict(quote(price='11'), amount=amount)]
    for options in ({}, {'curve_only': True}):
        with pytest.raises(TypeError, match='unhashable type'):
            market.compare_limits(rows, [limits()], AT, **options)


def test_duplicate_amount_and_old_day_validation_keep_the_original_order():
    rows = [quote(price='11'), dict(quote(price='9'), amount={}),
            dict(quote(at=AT-timedelta(days=1)), amount=[])]
    full = market.compare_limits(rows, [limits()], AT)
    assert full['total_amount_cny'] == '1000' and full['returned'] == 3
    assert market.compare_limits(rows, [limits()], AT, curve_only=True) == dict(
        limit_up=1, limit_down=1, comparable=2, source_time_max=AT)


def test_price_limit_source_clock_and_receipt_checks_survive_the_curve_path():
    memo = market.comparison_memo()
    future = quote(price='11', at=AT+timedelta(seconds=61))
    with pytest.raises(market.SourceFailure, match='future_source_time'):
        market.compare_limits([future], [limits()], AT, memo=memo, curve_only=True)
    assert market.compare_limits([future], [limits()], AT+timedelta(seconds=1),
        memo=memo, curve_only=True)['source_time_max'] == AT+timedelta(seconds=61)
    zero = market.compare_limits([quote(price='0')], [limits()], AT, curve_only=True)
    assert zero == dict(limit_up=0, limit_down=0, comparable=0, source_time_max=None)
    rows = [quote(price='11')]
    assert market.compare_limits(rows, [], AT, memo=memo, curve_only=True)['comparable'] == 0
    assert market.compare_limits(rows, [limits()], AT, memo=memo,
        limits_key=('original', 'hash1'), curve_only=True)['limit_up'] == 1
    assert market.compare_limits(rows, [limits(upper='12')], AT, memo=memo,
        limits_key=('corrected', 'hash2'), curve_only=True)['limit_up'] == 0


def evidence(db, family, rows, key):
    if db.get(Provider, source_id(family)) is None:
        db.add(Provider(id=source_id(family), name='Synthetic curve input', family=family,
            kind='market', configured=True, enabled=True, entitlement_status='verified',
            adapter_version='synthetic-curve-fixture', capabilities=['reference'],
            rights={'display': 'allowed', 'cache': 'allowed'}))
        db.flush()
    payload = dict(dataset=market.SNAPSHOT if family == market.MARKET else market.LIMIT_TABLE,
                   observed_day=AT.date().isoformat(), rows=rows)
    row = EvidenceVersion(source_id=source_id(family), owner_id=None, visibility='shared',
        source_key='synthetic:' + key, source_revision=uuid.uuid4().hex, version_number=1,
        kind='reference', received_at=AT, available_at=AT, availability='known',
        provenance='source_reported', historical_eligible=True, rights_basis='Synthetic offline fixture',
        payload=payload, content_hash=content_hash(payload))
    db.add(row); db.flush()
    return row


def test_curve_cache_cannot_replace_full_statistics_and_copies_results():
    with Session(engine()) as db, db.begin():
        snapshot = evidence(db, market.MARKET, [quote(price='11')], 'market')
        available = evidence(db, market.LIMITS, [limits()], 'limits')
        before = deepcopy(snapshot.payload)
        curve = market.comparison_for(db, snapshot, available, curve_only=True)
        assert curve == dict(limit_up=1, limit_down=0, comparable=1, source_time_max=AT)
        curve['limit_up'] = 999
        assert market.comparison_for(db, snapshot, available, curve_only=True)['limit_up'] == 1
        full = market.comparison_for(db, snapshot, available)
        assert full['advancing'] == 1 and full['returned'] == 1 and full['total_amount_cny'] == '1000'
        assert len(full) > len(CURVE_FIELDS)
        assert snapshot.payload == before


def test_full_cache_can_supply_curve_without_reading_a_raw_table_again(monkeypatch):
    with Session(engine()) as db, db.begin():
        snapshot = evidence(db, market.MARKET, [quote(price='11')], 'market')
        available = evidence(db, market.LIMITS, [limits()], 'limits')
        full = market.comparison_for(db, snapshot, available)
        monkeypatch.setattr(db, 'execute', lambda *a, **k: pytest.fail('The complete cached calculation is reusable'))
        assert market.comparison_for(db, snapshot, available, curve_only=True) == {key: full[key] for key in CURVE_FIELDS}


def test_curve_requires_exact_hash_and_receipt_and_keeps_the_existing_cache_bound():
    with Session(engine()) as db, db.begin():
        snapshot = evidence(db, market.MARKET, [quote(price='11')], 'market')
        available = evidence(db, market.LIMITS, [limits()], 'limits')
        for wrong_hash, stamp in [('0'*64, AT), (snapshot.content_hash, AT+timedelta(seconds=1))]:
            selected = SimpleNamespace(id=snapshot.id, content_hash=wrong_hash, received_at=stamp)
            with pytest.raises(NoResultFound):
                market.comparison_for(db, selected, available, curve_only=True)
        market._COMPARISONS.update(((str(i),), dict(limit_up=0)) for i in range(4096))
        assert market.comparison_for(db, snapshot, available, curve_only=True)['limit_up'] == 1
        assert len(market._COMPARISONS) == 4096 and ('0',) not in market._COMPARISONS
