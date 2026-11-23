"""Synthetic secrets and disposable DB; live iFinD queries are never used by CI."""
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_data_sources import login

from app.ai.models import Connection
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.integrations import ifind
from app.integrations.ifind_client import Client, DOMAINS, ENDPOINT, IfindFailure, decode
from app.models import AuditEvent, Job

TOKEN = '-'.join(('synthetic', 'ifind', 'private', 'token'))


def setup(client, monkeypatch, *, role='admin', enabled=True):
    headers = login(client, role)
    monkeypatch.setattr('app.ai.vault.read_secret', lambda ref: TOKEN)
    with Session(engine()) as db, db.begin():
        row = Connection(id=uuid.uuid4(), name='官方iFinD合成接入', kind='market', template='ifind_mcp', endpoint=ENDPOINT,
                         protocol=None, model=None, enabled=enabled, revision=1, source_ids=[], secret_ref='vault:synthetic',
                         options={'timeout_seconds': 60, 'max_output_tokens': 1200}, archived=False,
                         last_test={'status': 'success', 'revision': 1}, tested_revision=1)
        db.add(row)
        identity = row.id
    return headers, identity


def post(client, headers, identity, *, key='query-1', **update):
    body = dict(domain='index', tool='index_data', arguments={'query': '上证指数2026年11月3日收盘价'}, expected_revision=1)
    body.update(update)
    return client.post(f'/api/v1/integrations/{identity}/ifind/query', json=body, headers={**headers, 'Idempotency-Key': 'ifind-test-' + key})


def test_query_is_private_reference_idempotent_and_does_not_create_market_evidence(client, monkeypatch):
    headers, identity = setup(client, monkeypatch)
    calls = []
    monkeypatch.setattr(Client, 'call', lambda self, tool, args: calls.append(args) or '合成来源答复：仅用于接口权限与幂等回归')
    first = post(client, headers, identity)
    assert first.status_code == 200, first.text
    data = first.json()['data']
    assert data['status'] == 'success' and data['source_time'] is None and not data['historical_eligible']
    assert data['received_at'] and data['text'] == '合成来源答复：仅用于接口权限与幂等回归'
    assert TOKEN not in first.text
    second = post(client, headers, identity)
    assert second.headers['Idempotency-Replayed'] == 'true' and len(calls) == 1
    monitor = client.get('/api/v1/data-center/overview').json()['data']
    status = next(item for item in monitor['services'] if item['id'] == str(identity))
    assert status['latest_data_at'] is None and status['impact_modules'] == ['iFinD 手动研究']
    assert post(client, headers, identity, arguments={'query': '其他'}).status_code == 409
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(EvidenceVersion)) == 0
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == 'ifind.query')) == 1


@pytest.mark.parametrize('role', ['researcher', 'viewer'])
def test_non_admin_cannot_spend_trial_quota(client, monkeypatch, role):
    headers, identity = setup(client, monkeypatch, role=role)
    assert post(client, headers, identity).status_code == 403


def test_disabled_stale_and_csrf_never_call_upstream(client, monkeypatch):
    headers, identity = setup(client, monkeypatch, enabled=False)
    monkeypatch.setattr(Client, 'call', lambda *args: pytest.fail('must not call'))
    assert post(client, headers, identity).status_code == 409
    with Session(engine()) as db, db.begin():
        db.get(Connection, identity).enabled = True
    assert post(client, headers, identity, expected_revision=2).status_code == 409
    assert post(client, {}, identity).status_code == 403
    assert post(client, headers, identity, tool='trade', arguments={}).status_code == 422


def test_global_daily_limit_counts_uncertain_calls_and_does_not_retry(client, monkeypatch):
    headers, identity = setup(client, monkeypatch)
    calls = []
    def fail(self, *args):
        calls.append(1)
        self.call_sent = True
        raise IfindFailure('ifind_outcome_unknown', True)
    monkeypatch.setattr(Client, 'call', fail)
    assert post(client, headers, identity).json()['data']['status'] == 'unknown'
    assert post(client, headers, identity).json()['data']['status'] == 'unknown'
    assert len(calls) == 1
    with Session(engine()) as db, db.begin():
        for _ in range(7):
            db.add(AuditEvent(action='ifind.query', resource_id=identity, created_at=datetime.now(UTC)))
    assert post(client, headers, identity, key='new').status_code == 429
    assert len(calls) == 1


def test_reconfiguration_during_query_discards_response(client, monkeypatch):
    headers, identity = setup(client, monkeypatch)
    def change(self, *args):
        with Session(engine()) as db, db.begin():
            db.get(Connection, identity).enabled = False
        return 'must not escape'
    monkeypatch.setattr(Client, 'call', change)
    result = post(client, headers, identity).json()['data']
    assert result['text'] is None and result['error_code'] == 'configuration_changed'
    assert post(client, headers, identity).status_code == 409


def test_template_has_separate_identity_and_probe_is_not_data_query(client, monkeypatch):
    headers, identity = setup(client, monkeypatch, enabled=False)
    monkeypatch.setattr(Client, 'tools', lambda self: [{'name': 'index_data', 'inputSchema': {}}])
    monkeypatch.setattr(Client, 'call', lambda *args: pytest.fail('catalog must not query'))
    data = client.get('/api/v1/integrations').json()['data']
    template = next(item for item in data['templates'] if item['id'] == 'ifind_mcp')
    assert template['source_families'] == [] and template['endpoint'] == ENDPOINT
    row = next(item for item in data['items'] if item['id'] == str(identity))
    body = {key: row[key] for key in ('name', 'kind', 'template', 'endpoint', 'protocol', 'model', 'enabled', 'source_ids', 'options')}
    body.update(id=str(identity), expected_revision=1)
    response = client.post('/api/v1/integrations/probe', json=body, headers={**headers, 'Idempotency-Key': 'ifind-probe-test'})
    assert response.status_code == 200, response.text
    assert response.json()['data']['status'] == 'success'
    body['enabled'] = True
    response = client.patch(f'/api/v1/integrations/{identity}', json=body, headers={**headers, 'Idempotency-Key': 'ifind-enable-test'})
    assert response.status_code == 200, response.text
    assert response.json()['data']['enabled']


def test_ifind_cannot_replace_an_existing_supplier(client, monkeypatch):
    from app.ai.schemas import ConnectionInput
    from app.integrations.data import validate_data_input
    from app.auth import APIError
    headers, identity = setup(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        row = db.get(Connection, identity)
        row.template = 'tushare'
    with Session(engine()) as db:
        with pytest.raises(APIError) as caught:
            validate_data_input(db, ConnectionInput(id=identity, name='不得替换', kind='market', template='ifind_mcp', endpoint=ENDPOINT))
        assert caught.value.code == 'ifind_identity_immutable'


def test_protocol_json_sse_and_application_failure_are_not_success(monkeypatch):
    for content_type, raw in [('application/json', '{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}'),
                              ('text/event-stream', ': ping\n\nevent: message\ndata: {"id":2,"result":{"tools":[]}}\n\n')]:
        assert decode(raw.encode(), content_type, 2) == {'tools': []}
    with pytest.raises(IfindFailure):
        decode(b'{"id":2,"error":{"message":"secret"}}', 'application/json', 2)
    monkeypatch.setattr(Client, 'tools', lambda self: [{'name': 'index_data', 'inputSchema': {'properties': {'query': {}}, 'required': ['query']}}])
    monkeypatch.setattr(Client, 'post', lambda *args: {'content': [{'type': 'text', 'text': '{"code":0,"msg":"quota exceeded"}'}]})
    with pytest.raises(IfindFailure, match='ifind_application_error'):
        Client(TOKEN, 'index').call('index_data', {'query': 'test'})
    with pytest.raises(IfindFailure, match='ifind_arguments_invalid'):
        Client(TOKEN, 'index').call('index_data', {'url': 'https://evil.invalid'})


def test_fixed_destinations_and_all_eight_domains():
    assert len(DOMAINS) == 8
    for domain in DOMAINS:
        assert Client(TOKEN, domain).path.startswith('/ds-mcp-servers/hexin-ifind-ds-')
    for domain in ['http://127.0.0.1', '../law', 'enterprise']:
        with pytest.raises(IfindFailure):
            Client(TOKEN, domain)
    with pytest.raises(ValidationError):
        ifind.IfindRequest(domain='index', tool='index_data', arguments={'query': 'x'*9000}, expected_revision=1)


@pytest.mark.parametrize('status,code', [(302, 'ifind_http_error'), (401, 'ifind_auth_denied'), (429, 'ifind_quota_or_rate_limit')])
def test_transport_never_follows_redirects_or_exposes_http_errors(monkeypatch, status, code):
    requests = []
    class Wire:
        def __init__(self, host, port, **kwargs):
            assert host == 'api-mcp.51ifind.com' and port == 8643
        def request(self, method, path, body, headers):
            requests.append(path)
            assert headers['Authorization'] == TOKEN and 'Bearer' not in headers['Authorization']
        def getresponse(self):
            return type('Response', (), {'status': status})()
        def close(self):
            pass
    monkeypatch.setattr('http.client.HTTPSConnection', Wire)
    with pytest.raises(IfindFailure, match=code) as caught:
        Client(TOKEN, 'index').tools()
    assert TOKEN not in str(caught.value) and len(requests) == 1


def test_credentials_are_redacted_from_provider_echo(monkeypatch):
    monkeypatch.setattr(Client, 'tools', lambda self: [{'name': 'index_data', 'inputSchema': {'properties': {'query': {}}}}])
    monkeypatch.setattr(Client, 'post', lambda *args: {'content': [{'type': 'text', 'text': TOKEN}]})
    assert Client(TOKEN, 'index').call('index_data', {'query': 'test'}) == '[credential redacted]'
