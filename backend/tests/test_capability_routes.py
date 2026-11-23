"""Cost/authority/source choices on a disposable PostgreSQL database."""
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_data_sources import configure, login, queue_and_process, quote_body

from app.config import settings
from app.db import engine
from app.integrations.routing import collection_allowed, preferred_families
from app.integrations.routing_models import DataRoutes
from app.market.models import DataFetchRun
from app.market.protocol import Response
from app.market.service import source_id
from app.models import AuditEvent, Job


def read(client):
    response = client.get('/api/v1/data-center/capabilities')
    assert response.status_code == 200, response.text
    return response.json()['data']


def patch(client, headers, revision, changes):
    return client.patch('/api/v1/data-center/routes', json={'expected_revision': revision, 'changes': changes},
        headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})


def source_ready(client, monkeypatch):
    headers = login(client)
    configure(client, headers)
    queue_and_process(client, headers, monkeypatch, lambda *args, **kwargs: Response(quote_body(), datetime.now(UTC), 1, 200))
    return headers


def test_matrix_is_authenticated_and_get_does_not_create_routes_or_call_vendor(client, monkeypatch):
    assert client.get('/api/v1/data-center/capabilities').status_code == 401
    login(client)
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: (_ for _ in ()).throw(AssertionError('GET cannot call vendor')))
    value = read(client)
    assert value['policy'] == 'purchased_first' and value['revision'] == 1
    tickflow = next(item for item in value['providers'] if item['id'] == 'tickflow')
    assert any(option['billing'] == 'subscription' and not option['implemented'] for option in tickflow['capabilities'])
    assert all(option['entitlement'] == 'unknown' for option in tickflow['capabilities'])
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(DataRoutes)) == 0
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_paid_free_and_account_validation_are_independent(client):
    headers = login(client)
    configure(client, headers)
    value = read(client)
    tencent = next(item for item in value['providers'] if item['id'] == 'tencent')
    option = next(item for item in tencent['capabilities'] if item['capability_id'] == 'stock_quote')
    assert option['billing'] == 'free' and option['entitlement'] == 'unknown'
    assert option['last_success_at'] is None
    response = patch(client, headers, value['revision'], [{'capability_id': 'stock_quote', 'provider_id': 'tencent'}])
    assert response.status_code == 409 and response.json()['error']['code'] == 'capability_test_required'


def test_single_capability_patch_preserves_other_choices_and_is_persistent(client, monkeypatch):
    headers = source_ready(client, monkeypatch)
    first = patch(client, headers, 1, [{'capability_id': 'stock_quote', 'provider_id': 'tencent'}])
    assert first.status_code == 200, first.text
    assert first.json()['data']['revision'] == 2
    second = patch(client, headers, 2, [{'capability_id': 'index_quote', 'provider_id': 'tencent'}])
    assert second.status_code == 200, second.text
    caps = {item['id']: item for item in read(client)['capabilities']}
    assert caps['stock_quote']['current_provider_id'] == caps['index_quote']['current_provider_id'] == 'tencent'
    with Session(engine()) as db:
        assert set(db.get(DataRoutes, 1).choices) == {'stock_quote', 'index_quote'}
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == 'data.routes.update')) == 2
    from test_data_sources import PASSWORD
    assert client.delete('/api/v1/session', headers=headers).status_code == 204
    assert client.post('/api/v1/session', json={'username': 'owner', 'password': PASSWORD}, headers={'Origin': headers['Origin']}).status_code == 201
    assert read(client)['revision'] == 3


def test_batch_rejects_atomically_and_optimistic_version_rejects_stale_update(client, monkeypatch):
    headers = source_ready(client, monkeypatch)
    assert patch(client, headers, 1, [{'capability_id': 'stock_quote', 'provider_id': 'tencent'}]).status_code == 200
    assert patch(client, headers, 1, [{'capability_id': 'index_quote', 'provider_id': 'tencent'}]).status_code == 409
    response = patch(client, headers, 2, [
        {'capability_id': 'index_quote', 'provider_id': 'tencent'},
        {'capability_id': 'stock_daily', 'provider_id': 'tickflow'}])
    assert response.status_code == 422
    with Session(engine()) as db:
        assert db.get(DataRoutes, 1).revision == 2
        assert set(db.get(DataRoutes, 1).choices) == {'stock_quote'}


def test_unknown_and_duplicate_choices_cannot_be_applied(client):
    headers = login(client)
    assert patch(client, headers, 1, [{'capability_id': 'not-a-capability', 'provider_id': 'tencent'}]).status_code == 422
    assert patch(client, headers, 1, [
        {'capability_id': 'stock_quote', 'provider_id': 'tencent'},
        {'capability_id': 'stock_quote', 'provider_id': 'tushare'}]).status_code == 422


def test_only_admin_can_change_routing(client):
    headers = login(client, 'viewer')
    assert client.get('/api/v1/data-center/capabilities').status_code == 200
    assert patch(client, headers, 1, [{'capability_id': 'stock_quote', 'provider_id': 'tencent'}]).status_code == 403


def test_latest_authentication_failure_does_not_hide_cost_or_claim_free_is_usable(client, monkeypatch):
    source_ready(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        db.add(DataFetchRun(source_id=source_id('tencent_web'), capability='quote.l1', started_at=datetime.now(UTC),
            received_at=datetime.now(UTC), status='error', reason='permission_missing', request_count=1))
    option = next(item for item in read(client)['capabilities'] if item['id'] == 'stock_quote')['options']
    tencent = next(item for item in option if item['provider_id'] == 'tencent')
    assert tencent['billing'] == 'free' and tencent['entitlement'] == 'locked'
    assert tencent['reason'] == 'permission_missing'


def test_purchased_preference_does_not_enable_new_integrations_or_points(client):
    login(client)
    with Session(engine()) as db:
        ranked = preferred_families(db, 'stock_quote', ['tushare_market_rt', 'tickflow_live', 'tencent_web', 'tdx_quotes', 'fuyao_quotes'])
        assert ranked.index('tushare_market_rt') < ranked.index('tencent_web')
        assert 'tdx_quotes' not in ranked
        assert 'fuyao_quotes' not in ranked
        assert collection_allowed(db, 'tencent_web')
        assert not collection_allowed(db, 'fuyao_quotes')
        assert not collection_allowed(db, 'tdx_full_minute')


def test_route_update_invalidates_server_and_browser_read_scope(client, monkeypatch):
    from app.models import User
    from app.read_cache import cache_scope
    headers = source_ready(client, monkeypatch)
    monkeypatch.setattr(settings(), 'read_cache_enabled', True)
    with Session(engine()) as db:
        old_scope = cache_scope(db, db.scalar(select(User)))
    response = patch(client, headers, 1, [{'capability_id': 'stock_quote', 'provider_id': 'tencent'}])
    assert response.status_code == 200, response.text
    with Session(engine()) as db:
        assert cache_scope(db, db.scalar(select(User))) != old_scope


@pytest.mark.parametrize(('provider', 'count'), [('fuyao', 7), ('tdx_data', 4)])
def test_connect_seeds_each_capability_and_encrypts_key_without_enabling_collection(client, monkeypatch, tmp_path, provider, count):
    from app.ai.models import Connection
    from app.market.models import SourceSettings
    from app.models import Provider
    directory = tmp_path / 'credentials'
    directory.mkdir()
    master = tmp_path / 'master'
    master.write_bytes(Fernet.generate_key())
    monkeypatch.setattr(settings(), 'ai_secret_directory', str(directory))
    monkeypatch.setattr(settings(), 'ai_master_key_file', str(master))
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: (_ for _ in ()).throw(AssertionError('save cannot call vendor')))
    headers = login(client)
    configure(client, headers)
    secret = 'synthetic-provider-key-never-returned'
    url = f'/api/v1/data-center/providers/{provider}/connect'
    response = client.post(url, json={'api_key': secret}, headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
    assert response.status_code == 200, response.text
    assert secret not in response.text
    with Session(engine()) as db:
        connection = db.scalar(select(Connection).where(Connection.template == provider))
        assert not connection.enabled and connection.secret_ref.startswith('vault:')
        families = [db.get(Provider, uuid.UUID(source)).family for source in connection.source_ids]
        assert len(families) == count and len(set(families)) == count
        # The separately configured auction does not use the quote-route gate;
        # saving its key must still leave its actual connection stopped.
        assert all(collection_allowed(db, family) is (family == 'fuyao_auction') for family in families)
        from app.integrations.credentials import resolve_source_credentials
        assert all(resolve_source_credentials(db, uuid.UUID(source), family).reason == 'source_disabled'
            for source, family in zip(connection.source_ids, families))
        assert all(not db.get(SourceSettings, uuid.UUID(source)).config.get('points_enabled', False) for source in connection.source_ids)
        reference = connection.secret_ref
    encrypted = list(directory.glob('*.key'))
    assert len(encrypted) == 1 and secret.encode() not in encrypted[0].read_bytes()
    assert Fernet(master.read_bytes()).decrypt(encrypted[0].read_bytes()).decode() == secret
    # Revisiting with a blank key keeps the existing encrypted credential.
    saved = client.post(url, json={}, headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
    assert saved.status_code == 200, saved.text
    with Session(engine()) as db:
        assert db.scalar(select(Connection).where(Connection.template == provider)).secret_ref == reference
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_stock_only_source_does_not_claim_index_permission(client, monkeypatch):
    from test_data_sources import configuration
    headers = login(client)
    config = configuration()
    config['symbols'] = [config['symbols'][1]]
    configure(client, headers, config)
    queue_and_process(client, headers, monkeypatch, lambda *args, **kwargs: Response(quote_body().splitlines()[-1], datetime.now(UTC), 1, 200))
    capabilities = {item['id']: item for item in read(client)['capabilities']}
    stock = next(item for item in capabilities['stock_quote']['options'] if item['provider_id'] == 'tencent')
    index = next(item for item in capabilities['index_quote']['options'] if item['provider_id'] == 'tencent')
    assert stock['entitlement'] == 'verified'
    assert index['entitlement'] == 'unknown' and index['source_ids'] == []


def test_full_minute_requires_selected_route_and_explicit_per_chunk_points_consent(client):
    from test_tdx_data import config

    from app.market.models import SourceSettings
    headers = login(client)
    configure(client, headers, config('tdx_full_minute', points=True))
    url = '/api/v1/data-center/full-minute'
    def queue(body):
        return client.post(url, json=body, headers={**headers, 'Idempotency-Key': uuid.uuid4().hex})
    denied = queue({'allow_points': True})
    assert denied.status_code == 422 and denied.json()['error']['code'] == 'full_minute_not_selected'
    with Session(engine()) as db, db.begin():
        db.add(DataRoutes(id=1, revision=2, choices={'full_minute': {'provider_id': 'tdx_data', 'allow_points': True}}, updated_at=func.now()))
    denied = queue({})
    assert denied.status_code == 422 and denied.json()['error']['code'] == 'points_confirmation_required'
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.get(SourceSettings, source_id('tdx_full_minute')).config['collection_mode'] == 'selected'


def test_full_minute_progress_is_limited_to_job_owner_or_admin(client):
    from app.models import User
    login(client, 'viewer')
    with Session(engine()) as db, db.begin():
        own = db.scalar(select(User))
        other = User(username='synthetic-other-owner', password_hash=own.password_hash, role='researcher')
        db.add(other)
        db.flush()
        job = Job(owner_id=other.id, kind='source.collect', payload={'full_minute_context': {}}, result_ref={'kind': 'tdx_full_minute', 'scope_count': 20})
        db.add(job)
        db.flush()
        identity = job.id
    assert client.get(f'/api/v1/data-center/minute-jobs/{identity}').status_code == 404
    with Session(engine()) as db, db.begin():
        db.scalar(select(User).where(User.username == 'owner')).role = 'admin'
    response = client.get(f'/api/v1/data-center/minute-jobs/{identity}')
    assert response.status_code == 200 and response.json()['data']['progress']['scope_count'] == 20


def test_reference_read_prefers_selected_source_without_merging_or_hiding_explicit_family(client):
    from test_fuyao_data import KEY, config
    from test_tushare import source

    from app.evidence.schemas import ReferencePayload
    from app.market.collect import store_reference_batch
    from app.market.models import SourceSettings
    from app.market.reference import ReferenceBatch
    from app.models import Provider
    headers = login(client)
    configure(client, headers, config('fuyao_financial'), key='financial-fuyao')
    configure(client, headers, source('tushare_income'), key='financial-tushare')
    clock = datetime.now(UTC)
    for family, dataset in [('fuyao_financial', 'fuyao.financial.income_statement'), ('tushare_income', 'tushare.income')]:
        payload = ReferencePayload(dataset=dataset, title='合成原始报表', source_url='https://example.invalid/reference',
            attribution='Synthetic isolated fixture', scope='Selected stock, original source table',
            observed_day=clock.date(), columns=[{'key': 'net_profit', 'label': '合成利润', 'unit': '元'}], rows=[{'net_profit': '123'}])
        batch = ReferenceBatch(records=[(dataset + ':' + KEY.identity, payload, KEY)], expected=1)
        with Session(engine()) as db, db.begin():
            ident = source_id(family)
            store_reference_batch(db, db.get(Provider, ident), db.get(SourceSettings, ident), batch, clock)
    with Session(engine()) as db, db.begin():
        db.add(DataRoutes(id=1, revision=2, choices={'financial': {'provider_id': 'fuyao', 'allow_points': False}}, updated_at=func.now()))
    rows = client.get('/api/v1/reference-datasets').json()['data']['items']
    assert [row['family'] for row in rows] == ['fuyao_financial', 'tushare_income']
    explicit = client.get('/api/v1/reference-datasets', params={'family': 'tushare_income'}).json()['data']['items']
    assert len(explicit) == 1 and explicit[0]['payload']['dataset'] == 'tushare.income'


def test_returned_quote_without_market_time_verifies_account_but_cannot_be_applied_as_live(client, monkeypatch):
    from test_fuyao_data import setup, snapshot

    from app.integrations.credentials import job_context, resolve_source_credentials
    from app.market import fuyao
    from app.market.service import enqueue_collection
    from app.worker import claim, process
    headers = setup(client, monkeypatch)
    monkeypatch.setattr(fuyao, 'fetch', lambda *args, **kwargs: (Response(snapshot(), datetime.now(UTC), 2, 200), [(None, None, snapshot())]))
    with Session(engine()) as db, db.begin():
        source = source_id('fuyao_quotes')
        bound = resolve_source_credentials(db, source, 'fuyao_quotes')
        enqueue_collection(db, source, integration_probe=job_context(bound, source, probe=True))
    process(claim())
    capability = next(item for item in read(client)['capabilities'] if item['id'] == 'stock_quote')
    option = next(item for item in capability['options'] if item['provider_id'] == 'fuyao')
    assert option['billing'] == 'free' and option['entitlement'] == 'verified'
    assert option['state'] == 'time_unverified' and capability['effective_provider_id'] is None
    response = patch(client, headers, 1, [{'capability_id': 'stock_quote', 'provider_id': 'fuyao'}])
    assert response.status_code == 409 and response.json()['error']['code'] == 'capability_test_required'
