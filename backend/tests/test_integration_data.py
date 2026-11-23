"""Existing collectors, synthetic private credentials, disposable PostgreSQL only."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_data_sources import configure, login
from test_tushare import quote_body, source

from app.ai.models import Connection
from app.ai.schemas import ConnectionInput
from app.auth import APIError
from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.integrations.credentials import (
    assert_source_binding,
    job_context,
    resolve_source_credentials,
)
from app.integrations.data import (
    _data_time,
    capabilities,
    connection_test,
    probe_data,
    sync_connection,
    templates,
    validate_data_input,
    virtual_connections,
)
from app.market.models import DataFetchRun
from app.market.protocol import Response, SourceFailure
from app.market.service import enqueue_collection, source_id, source_states
from app.models import Job, Provider
from app.worker import claim, process

PRIVATE = 'synthetic-new-connection-credential-only'
LEGACY = 'synthetic-old-environment-credential-only'


def test_vendor_data_day_never_becomes_local_observation_or_request_day():
    payload = {'observed_day': '2026-10-25', 'rows': [{'trade_date': '20261023'}]}
    assert _data_time(payload, None, None, 'tushare_daily') == '2026-10-23'
    assert _data_time({'observed_day': '2026-10-25', 'rows': []}, None, None, 'tushare_stock_basic') is None
    assert _data_time({'observed_day': '2026-10-23', 'rows': []}, None, None, 'mairui_pools') is None
    local_snapshot = {'observed_day': '2026-10-26', 'rows': [{'source_day': None, 'source_time': None}]}
    for family in ('direction_ths_hot', 'direction_em_hot'):
        assert _data_time(local_snapshot, None, None, family) is None
    assert _data_time({'rows': [{'source_day': '2026-10-26'}]}, None, None, 'direction_tdx_hot') == '2026-10-26'
    assert _data_time({'rows': [{'source_day': '2026-10-23'}]}, None, None, 'direction_kpl_strength') == '2026-10-23'


def test_news_templates_keep_registered_families_and_fixed_supplier_destinations():
    from app.market.public import URLS
    from app.market.registry import SOURCES
    definitions = {item['id']: item for item in templates()}
    grouped = [family for item in definitions.values() for family in item['source_families']]
    assert set(grouped) == set(SOURCES) and len(grouped) == len(set(grouped))
    for family in ('chinanews_finance', 'cninfo_announcements'):
        entry = definitions[family]
        assert entry['kind'] == 'news' and entry['source_families'] == [family]
        assert entry['endpoint'] == URLS[family] and entry['endpoints'] == [URLS[family]]
        assert entry['credential_required'] is False
    # The original instrument catalog and the upgraded announcement adapter keep
    # their separate identities; a UI label must not create a third identity.
    assert definitions['cninfo']['kind'] == 'market'
    assert 'cninfo_news' not in SOURCES and 'cninfo_news' not in definitions


def configured(client, monkeypatch, families=('tushare_rt_k',)):
    from app.ai.vault import read_secret
    headers = login(client)
    for family in families:
        configure(client, headers, source(family), key='integrations-fixture-' + family)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(settings(), 'tushare_token', SecretStr(LEGACY))
    monkeypatch.setattr('app.ai.vault.read_secret', lambda reference: PRIVATE if reference.startswith('vault:') else read_secret(reference))
    return headers


def connection(db, families=('tushare_rt_k',), *, enabled=True, secret_ref='vault:synthetic', template='tushare', endpoint=None):
    value = Connection(id=uuid.uuid4(), name='合成供应商接入', kind='market', template=template,
        endpoint=endpoint or settings().tushare_endpoint, protocol=None, model=None, enabled=enabled,
        revision=1, secret_ref=secret_ref, source_ids=[str(source_id(family)) for family in families],
        options={'timeout_seconds': 60, 'max_output_tokens': 1200}, last_test=None,
        tested_revision=None, archived=False)
    db.add(value)
    db.flush()
    sync_connection(db, value)
    return value


def test_virtual_supplier_groups_stable_source_ids_and_reads_do_not_materialize(client, monkeypatch):
    configured(client, monkeypatch, ('tushare_rt_k', 'tushare_daily'))
    with Session(engine()) as db:
        before = [(row.id, row.family, row.enabled) for row in db.scalars(select(Provider).where(Provider.family.is_not(None)))]
        views = virtual_connections(db)
        assert len(views) == 1
        view = views[0]
        assert view['template'] == 'tushare' and view['known_env_ref'] == 'env:tushare'
        assert set(view['source_ids']) == {str(source_id('tushare_rt_k')), str(source_id('tushare_daily'))}
        assert view['credential_configured'] and view['credential_origin'] == 'environment'
        assert view['last_test'] is None and view['tested_revision'] is None
        assert PRIVATE not in str(views) and LEGACY not in str(views)
        assert virtual_connections(db)[0]['id'] == view['id']
        assert db.scalar(select(func.count()).select_from(Connection)) == 0
        assert before == [(row.id, row.family, row.enabled) for row in db.scalars(select(Provider).where(Provider.family.is_not(None)))]


def test_new_news_sources_remain_visible_as_independent_virtual_capabilities(client, monkeypatch):
    from test_connected_data import source_config
    headers = login(client)
    for family in ('chinanews_finance', 'cninfo_announcements'):
        configure(client, headers, source_config(family), key='synthetic-news-template-' + family)
    with Session(engine()) as db:
        views = {item['template']: item for item in virtual_connections(db)}
        for family in ('chinanews_finance', 'cninfo_announcements'):
            view = views[family]
            assert view['kind'] == 'news' and view['source_ids'] == [str(source_id(family))]
            assert view['capabilities'][0]['family'] == family
            assert view['capabilities'][0]['news_coverage'] is None
            assert view['credential_origin'] == 'none' and view['known_env_ref'] is None
            validate_data_input(db, {**view, 'enabled': False})
        assert db.scalar(select(func.count()).select_from(Connection)) == 0


def test_virtual_connection_http_probe_materializes_once_and_get_never_calls_vendor(client, monkeypatch):
    headers = configured(client, monkeypatch)
    calls = []
    def vendor(*args):
        calls.append(True)
        return Response(quote_body(), datetime.now(UTC), 2, 200)
    monkeypatch.setattr('app.market.collect.request_tushare', vendor)
    response = client.get('/api/v1/integrations')
    assert response.status_code == 200, response.text
    view = next(item for item in response.json()['data']['items'] if item['template'] == 'tushare')
    assert any(template.get('source_families') for template in response.json()['data']['templates'] if template['kind'] != 'model')
    values = {key: view[key] for key in ('id', 'name', 'kind', 'template', 'endpoint', 'protocol', 'model', 'enabled', 'source_ids', 'options')}
    values.update(source_id=str(source_id('tushare_rt_k')), expected_revision=view['revision'])
    receipts = []
    for _ in range(2):
        response = client.post('/api/v1/integrations/probe', json=values,
                               headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
        assert response.status_code == 200, response.text
        receipts.append(response.json()['data'])
    assert receipts[0]['job_id'] == receipts[1]['job_id'] and calls == []
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Connection)) == 1
        assert db.scalar(select(func.count()).select_from(Job)) == 1
    process(claim())
    assert len(calls) == 1
    response = client.get('/api/v1/integrations')
    current = next(item for item in response.json()['data']['items'] if item['id'] == view['id'])
    assert current['last_test']['status'] == 'success' and len(calls) == 1
    monkeypatch.setattr(settings(), 'tushare_token', None)
    current = next(item for item in client.get('/api/v1/integrations').json()['data']['items'] if item['id'] == view['id'])
    assert current['credential_configured'] is False


def test_virtual_id_survives_new_capabilities_and_residual_group_does_not_collide(client, monkeypatch):
    from app.ai.service import get_connection
    headers = configured(client, monkeypatch)
    with Session(engine()) as db:
        first = virtual_connections(db)[0]
    configure(client, headers, source('tushare_daily'), key='synthetic-add-legacy-daily')
    with Session(engine()) as db, db.begin():
        current = virtual_connections(db)[0]
        assert current['id'] == first['id'] and len(current['source_ids']) == 2
        assert get_connection(db, uuid.UUID(first['id'])).id == uuid.UUID(first['id'])
        stored = connection(db)
        stored.id = uuid.UUID(first['id'])
        db.flush()
        sync_connection(db, stored)
        residual = virtual_connections(db)[0]
        assert residual['id'] != first['id']
        assert residual['source_ids'] == [str(source_id('tushare_daily'))]
    configure(client, headers, source('tushare_daily_basic'), key='synthetic-add-legacy-basics')
    with Session(engine()) as db:
        current = virtual_connections(db)[0]
        assert current['id'] == residual['id'] and len(current['source_ids']) == 2
        assert get_connection(db, uuid.UUID(first['id'])).id == uuid.UUID(first['id'])


def test_templates_validate_actual_source_identity_fixed_endpoints_and_unique_binding(client, monkeypatch):
    configured(client, monkeypatch)
    tushare = next(row for row in templates() if row['id'] == 'tushare')
    assert tushare['credential_required'] and settings().tushare_endpoint in tushare['endpoints']
    value = dict(name='任意显示名称不能改变供应商', kind='market', template='tushare', endpoint=settings().tushare_endpoint,
                 source_ids=[source_id('tushare_rt_k')], enabled=False)
    with Session(engine()) as db, db.begin():
        validate_data_input(db, value)
        with pytest.raises(APIError) as error:
            validate_data_input(db, {**value, 'endpoint': 'https://unapproved.example/'})
        assert error.value.code == 'endpoint_not_allowed'
        with pytest.raises(APIError) as error:
            validate_data_input(db, {**value, 'template': 'tickflow', 'endpoint': 'https://api.tickflow.org/v1/quotes'})
        assert error.value.code == 'source_binding_invalid'
        stored = connection(db)
        with pytest.raises(APIError) as error:
            validate_data_input(db, value)
        assert error.value.code == 'source_binding_conflict'
        validate_data_input(db, {**value, 'id': stored.id})
        with pytest.raises(APIError) as error:
            validate_data_input(db, {**value, 'id': stored.id, 'source_ids': []})
        assert error.value.status == 422 and error.value.code == 'source_binding_removal_requires_archive'


def test_stored_binding_overrides_environment_and_missing_disabled_archived_never_fall_back(client, monkeypatch):
    configured(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        legacy = resolve_source_credentials(db, source_id('tushare_rt_k'), 'tushare_rt_k')
        assert legacy.credential.get_secret_value() == LEGACY and legacy.connection_id is None
        stored = connection(db)
        actual = resolve_source_credentials(db, source_id('tushare_rt_k'), 'tushare_rt_k')
        assert actual.credential.get_secret_value() == PRIVATE
        stored.secret_ref = None
        sync_connection(db, stored)
        missing = resolve_source_credentials(db, source_id('tushare_rt_k'), 'tushare_rt_k')
        assert missing.credential is None and missing.reason == 'credentials_missing'
        stored.enabled = False
        stored.secret_ref = 'vault:synthetic'
        sync_connection(db, stored)
        assert resolve_source_credentials(db, source_id('tushare_rt_k'), 'tushare_rt_k').reason == 'source_disabled'
        stored.archived = True
        sync_connection(db, stored)
        assert resolve_source_credentials(db, source_id('tushare_rt_k'), 'tushare_rt_k').reason == 'source_disabled'
        assert virtual_connections(db) == []
        replacement = connection(db)
        assert resolve_source_credentials(db, source_id('tushare_rt_k'), 'tushare_rt_k').connection_id == replacement.id


def test_saved_key_and_fixed_endpoint_are_used_by_existing_worker(client, monkeypatch):
    configured(client, monkeypatch)
    endpoint = 'https://quotes.example.test/compat'
    monkeypatch.setattr(settings(), 'tushare_endpoint', endpoint)
    with Session(engine()) as db, db.begin():
        stored = connection(db, endpoint=endpoint)
        queued = enqueue_collection(db, source_id('tushare_rt_k'))
        assert queued.payload['integration_context'] == job_context(
            resolve_source_credentials(db, source_id('tushare_rt_k'), 'tushare_rt_k'), source_id('tushare_rt_k'))
        identity = stored.id
    calls = []
    def vendor(api, parameters, fields, key, destination):
        calls.append((key.get_secret_value(), destination))
        return Response(quote_body(), datetime.now(UTC), 3, 200)
    monkeypatch.setattr('app.market.collect.request_tushare', vendor)
    process(claim())
    assert calls == [(PRIVATE, endpoint)]
    with Session(engine()) as db:
        assert db.scalar(select(Job)).state == 'succeeded'
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.source_id == source_id('tushare_rt_k'))) > 0
        assert db.get(Connection, identity).secret_ref == 'vault:synthetic'


def test_disabled_connection_has_only_a_single_explicit_probe_permission(client, monkeypatch):
    configured(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        stored = connection(db, enabled=False)
        with pytest.raises(APIError) as error:
            enqueue_collection(db, source_id('tushare_rt_k'))
        assert error.value.code == 'source_disabled'
        test = probe_data(db, stored)
        assert test['status'] == 'queued' and test['request_count'] == 0
        stored.last_test = test
        identity = stored.id
        job = db.get(Job, uuid.UUID(test['job_id']))
        assert job.payload['integration_context']['probe'] is True
    monkeypatch.setattr('app.market.collect.request_tushare', lambda *args: Response(quote_body(), datetime.now(UTC), 3, 200))
    process(claim())
    with Session(engine()) as db:
        stored = db.get(Connection, identity)
        assert stored.enabled is False and db.get(Provider, source_id('tushare_rt_k')).enabled
        assert connection_test(db, stored)['status'] == 'success'
        assert source_states(db)[0].collection_state == 'disabled'


@pytest.mark.parametrize('change', ['revision', 'disabled', 'archived'])
def test_old_queued_tasks_recheck_binding_before_request(client, monkeypatch, change):
    configured(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        stored = connection(db)
        enqueue_collection(db, source_id('tushare_rt_k'))
        if change == 'revision':
            stored.revision += 1
        elif change == 'disabled':
            stored.enabled = False
        else:
            stored.archived = True
    monkeypatch.setattr('app.market.collect.request_tushare', lambda *args: pytest.fail('stale or disabled connection made a request'))
    process(claim())
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun))
        assert run.request_count == 0 and run.reason in {'configuration_changed', 'source_disabled'}
        assert db.scalar(select(Job)).state == 'failed'


def test_binding_revision_changed_during_request_cannot_write_response(client, monkeypatch):
    configured(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        identity = connection(db).id
        enqueue_collection(db, source_id('tushare_rt_k'))
    def vendor(*args):
        with Session(engine()) as db, db.begin():
            db.get(Connection, identity).revision += 1
        return Response(quote_body(), datetime.now(UTC), 3, 200)
    monkeypatch.setattr('app.market.collect.request_tushare', vendor)
    process(claim())
    with Session(engine()) as db:
        assert db.scalar(select(DataFetchRun)).reason == 'configuration_changed'
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.source_id == source_id('tushare_rt_k'))) == 0


def test_source_data_clock_is_not_latest_success_clock_and_other_capability_is_unverified(client, monkeypatch):
    configured(client, monkeypatch, ('tushare_rt_k', 'tushare_daily'))
    source_time = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=12)
    with Session(engine()) as db, db.begin():
        stored = connection(db, ('tushare_rt_k', 'tushare_daily'), enabled=False)
        stored._probe_source_id = source_id('tushare_rt_k')
        stored.last_test = probe_data(db, stored)
        identity = stored.id
    monkeypatch.setattr('app.market.collect.request_tushare', lambda *args: Response(quote_body(source_time), datetime.now(UTC), 4, 200))
    process(claim())
    with Session(engine()) as db:
        stored = db.get(Connection, identity)
        checks = {item['family']: item for item in capabilities(db, stored)}
        called = checks['tushare_rt_k']
        assert datetime.fromisoformat(called['data_time']) == source_time
        assert datetime.fromisoformat(called['last_success_at']) > source_time
        assert checks['tushare_daily']['last_success_at'] is None
        assert connection_test(db, stored)['status'] == 'success'
        assert stored.tested_revision is None  # GET projection never writes the DB.
        stored.revision += 1
        assert connection_test(db, stored)['status'] == 'unknown'


def test_probe_requires_saved_current_key_single_capability_and_global_collection(client, monkeypatch):
    configured(client, monkeypatch, ('tushare_rt_k', 'tushare_daily'))
    with Session(engine()) as db, db.begin():
        stored = connection(db, ('tushare_rt_k', 'tushare_daily'), enabled=False)
        values = dict(id=stored.id, name=stored.name, kind=stored.kind, template=stored.template, endpoint=stored.endpoint,
                      source_ids=stored.source_ids, enabled=False)
        for body, code in [
            (ConnectionInput(**values, api_key=PRIVATE), 'data_probe_requires_saved_connection'),
            (ConnectionInput(**values), 'probe_capability_required'),
            (ConnectionInput(**{**values, 'id': None}), 'data_probe_requires_saved_connection'),
        ]:
            with pytest.raises(APIError) as error:
                probe_data(db, body)
            assert error.value.code == code
        monkeypatch.setattr(settings(), 'data_collection_enabled', False)
        with pytest.raises(APIError) as error:
            probe_data(db, ConnectionInput(**values, source_id=source_id('tushare_rt_k')))
        assert error.value.code == 'collection_disabled'
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_old_environment_job_cannot_silently_acquire_new_saved_binding(client, monkeypatch):
    configured(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        job = enqueue_collection(db, source_id('tushare_rt_k'))
        assert 'integration_context' not in job.payload
        connection(db)
        with pytest.raises(SourceFailure, match='configuration_changed'):
            assert_source_binding(db, source_id('tushare_rt_k'), 'tushare_rt_k', job.payload)


def test_cninfo_pages_recheck_connection_revision_before_each_request(client, monkeypatch):
    from app.market.budget import CNINFO_NEWS_ACTION
    from app.market.public import URLS
    from app.models import AuditEvent
    from test_connected_data import source_config
    headers = login(client)
    configure(client, headers, source_config('cninfo_announcements'), key='synthetic-cninfo-connection-gate')
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    with Session(engine()) as db, db.begin():
        stored = connection(db, ('cninfo_announcements',), enabled=False, secret_ref=None,
                            template='cninfo_announcements', endpoint=URLS['cninfo_announcements'])
        stored.kind = 'news'
        db.flush()
        sync_connection(db, stored)
        receipt = probe_data(db, stored)
        identity = stored.id
    requests = []
    def paged(*, reserve):
        assert reserve() is True
        requests.append('first synthetic request')
        with Session(engine()) as db, db.begin():
            db.get(Connection, identity).revision += 1
        with pytest.raises(SourceFailure, match='configuration_changed'):
            reserve()
        raise SourceFailure('configuration_changed')
    monkeypatch.setattr('app.market.collect.fetch_cninfo_batch', paged)
    process(claim())
    assert len(requests) == 1
    with Session(engine()) as db:
        run = db.scalar(select(DataFetchRun))
        assert run.reason == 'configuration_changed'
        assert db.get(Job, uuid.UUID(receipt['job_id'])).state == 'failed'
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == CNINFO_NEWS_ACTION)) == 1
        assert db.scalar(select(func.count()).select_from(EvidenceVersion).where(
            EvidenceVersion.source_id == source_id('cninfo_announcements'))) == 0


def test_selected_daily_history_uses_saved_key_without_environment_key(client, monkeypatch):
    from test_daily_history import raw_days, setup

    from app.market.daily_history import enqueue_history
    from app.market.protocol import SHANGHAI
    from app.market.schemas import DailyHistoryRequest
    from app.models import User
    _, instrument, _, _ = setup(client, monkeypatch)
    monkeypatch.setattr(settings(), 'tushare_token', None)
    monkeypatch.setattr('app.ai.vault.read_secret', lambda reference: PRIVATE)
    with Session(engine()) as db, db.begin():
        connection(db, ('tushare_daily',))
        owner = db.scalar(select(User))
        queued = enqueue_history(db, owner, DailyHistoryRequest(instrument_id=instrument))
        assert queued.payload['integration_context']['revision'] == 1
    seen = []
    day = datetime.now(UTC).astimezone(SHANGHAI).date() - timedelta(days=1)
    def vendor(api, parameters, fields, key, endpoint):
        seen.append(key.get_secret_value())
        return Response(raw_days([day]), datetime.now(UTC), 1, 200)
    monkeypatch.setattr('app.market.collect.request_tushare', vendor)
    process(claim())
    assert seen == [PRIVATE]
    with Session(engine()) as db:
        assert db.scalar(select(Job)).state == 'succeeded'


def test_review_calendar_paging_uses_saved_key_without_environment_key(client, monkeypatch):
    from test_tushare import body

    from app.market.protocol import SHANGHAI
    from app.market.review_jobs import queue_dataset
    from app.models import User
    configured(client, monkeypatch, ('tushare_calendar_sse',))
    monkeypatch.setattr(settings(), 'tushare_token', None)
    day = datetime.now(UTC).astimezone(SHANGHAI).date()
    with Session(engine()) as db, db.begin():
        connection(db, ('tushare_calendar_sse',))
        queue_dataset(db, db.scalar(select(User)), 'calendar_sse', day, force=True)
    seen = []
    def vendor(api, parameters, fields, key, endpoint):
        seen.append(key.get_secret_value())
        return Response(body(['exchange', 'cal_date', 'is_open'], [['SSE', day.strftime('%Y%m%d'), '1']]),
                        datetime.now(UTC), 1, 200)
    monkeypatch.setattr('app.market.tushare.request', vendor)
    process(claim())
    assert seen == [PRIVATE]
    with Session(engine()) as db:
        assert db.scalar(select(Job)).state == 'succeeded'


@pytest.mark.parametrize('family', ['mairui_funds', 'tickflow_live'])
def test_other_credentialed_collectors_use_saved_binding(client, monkeypatch, family):
    import json

    from test_data_sources import configuration
    from test_live_quotes import response_body, source_body

    from app.integrations.credentials import default_endpoint
    from app.market.reference import ReferenceBatch
    headers = login(client)
    config = source_body(family) if family == 'tickflow_live' else configuration(family)
    configure(client, headers, config)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(settings(), 'tickflow_api_key', None)
    monkeypatch.setattr(settings(), 'mairui_licence', None)
    monkeypatch.setattr('app.ai.vault.read_secret', lambda reference: PRIVATE)
    seen = []
    with Session(engine()) as db, db.begin():
        connection(db, (family,), template='tickflow' if family == 'tickflow_live' else 'mairui',
                   endpoint=default_endpoint(family))
        enqueue_collection(db, source_id(family))
    if family == 'tickflow_live':
        def vendor(selection, key):
            seen.append(key.get_secret_value())
            return Response(response_body(), datetime.now(UTC), 1, 200)
        monkeypatch.setattr('app.market.collect.fetch_tickflow', vendor)
    else:
        def vendor(family, key, permit, **kwargs):
            permit()
            seen.append(key.get_secret_value())
            return Response(json.dumps({'request_count': 1}).encode(), datetime.now(UTC), 1, 200)
        monkeypatch.setattr('app.market.collect.fetch_mairui', vendor)
        monkeypatch.setattr('app.market.collect.parse_mairui', lambda *args: ReferenceBatch(expected=0))
    process(claim())
    assert seen == [PRIVATE]
    with Session(engine()) as db:
        assert db.scalar(select(Job)).state == 'succeeded'
