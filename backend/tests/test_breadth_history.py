"""Synthetic observations on disposable PostgreSQL; no market providers contacted."""
import json
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.breadth import DATASET, parse_breadth
from app.market.collect import store_observation
from app.market.models import SourceSettings
from app.market.protocol import SHANGHAI
from app.market.service import source_id
from app.models import Provider
from test_data_sources import configuration, configure, login
from test_market_breadth import sample

URL = '/api/v1/market-breadth-history'


def setup(client):
    headers = login(client)
    configure(client, headers, configuration('ashare_breadth'))
    return headers


def save(clock, up='2', down='0', *, dataset=DATASET, rows=None):
    stamp = datetime.fromisoformat(clock).replace(tzinfo=SHANGHAI)
    value = sample()
    value['structured'][0].update({'涨停': up, '跌停': down})
    payload = parse_breadth(json.dumps(value).encode(), stamp).records[0][1]
    payload = payload.model_copy(update={'dataset': dataset, **({'rows': rows} if rows is not None else {})})
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('ashare_breadth'))
        assert store_observation(db, provider, db.get(SourceSettings, provider.id),
            source_key='reference:' + DATASET, kind='reference', payload=payload, received_at=stamp, repeated_state=True)


def test_observation_day_sessions_order_and_return_to_previous_value(client):
    setup(client)
    clocks = ['2020-01-01T14:00:00', '2020-01-02T09:29:59', '2020-01-02T09:30:00',
              '2020-01-02T10:20:35', '2020-01-02T11:30:00', '2020-01-02T12:00:00',
              '2020-01-02T13:00:00', '2020-01-02T15:00:00', '2020-01-02T15:00:01',
              '2020-01-03T09:30:00']
    for i, stamp in enumerate(clocks):
        save(stamp, str(i % 3 + 1), '0')
    response = client.get(URL, params={'day': '2020-01-02'})
    assert response.status_code == 200
    data = response.json()['data']
    assert data['time_basis'] == 'received_at' and data['timezone'] == 'Asia/Shanghai'
    assert data['observation_day'] == '2020-01-02' and not data['truncated']
    points = data['items']
    assert [datetime.fromisoformat(row['received_at']).astimezone(SHANGHAI).isoformat() for row in points] == [clocks[i] + '+08:00' for i in [2, 3, 4, 6, 7]]
    assert [row['limit_up'] for row in points] == [3, 1, 2, 1, 2]
    assert all(row['limit_down'] == 0 for row in points)
    assert len({row['id'] for row in points}) == 5
    assert client.get(URL, params={'day': '2020-01-04'}).json()['data']['items'] == []
    with Session(engine()) as db:
        versions = db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == source_id('ashare_breadth'))).all()
        assert len(versions) == len(clocks)
        assert all(row.event_at is None and row.available_at is None and not row.historical_eligible for row in versions)


def test_missing_counts_empty_payloads_and_future_observations_are_not_filled(client):
    setup(client)
    save('2020-01-02T09:30:00', down=None)
    save('2020-01-02T10:00:00', rows=[])
    save('2100-01-02T09:30:00')
    data = client.get(URL, params={'day': '2020-01-02'}).json()['data']
    assert [(point['limit_up'], point['limit_down']) for point in data['items']] == [(2, None), (None, None)]
    assert client.get(URL, params={'day': '2100-01-02'}).json()['data']['items'] == []


@pytest.mark.parametrize('restriction', ['disabled', 'unconfigured', 'unverified', 'display', 'cache', 'renamed'])
def test_history_rechecks_current_rights_and_latest_dataset_identity(client, restriction):
    setup(client)
    save('2020-01-02T09:30:00')
    assert len(client.get(URL, params={'day': '2020-01-02'}).json()['data']['items']) == 1
    if restriction == 'renamed':
        save('2020-01-02T10:00:00', dataset='market.corrected')
    else:
        with Session(engine()) as db, db.begin():
            provider = db.get(Provider, source_id('ashare_breadth'))
            if restriction == 'disabled': provider.enabled = False
            elif restriction == 'unconfigured': provider.configured = False
            elif restriction == 'unverified': provider.entitlement_status = 'unknown'
            else: provider.rights = {**provider.rights, restriction: 'denied'}
    assert client.get(URL, params={'day': '2020-01-02'}).json()['data']['items'] == []


def test_history_requires_login_validates_date_and_defaults_to_shanghai_today(client):
    assert client.get(URL).status_code == 401
    login(client)
    assert client.get(URL, params={'day': 'invalid'}).status_code == 422
    data = client.get(URL).json()['data']
    assert data['observation_day'] == str(datetime.now(SHANGHAI).date())
    assert data['items'] == [] and data['source_name'] is None
