"""Subscription operations use synthetic OAuth and transports, never a real account."""
import uuid

import pytest
import test_chatgpt_auth as auth
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_ai_backend import news, result_reply

from app.ai import chatgpt_transport, protocols, worker
from app.ai import service as ai_service
from app.ai.models import AiAttempt, AiRun, Connection
from app.auth import APIError
from app.chatgpt import oauth
from app.chatgpt.models import ChatGPTAccount
from app.config import settings
from app.db import engine
from app.main import app


@pytest.fixture(scope="module")
def signing_key():
    return auth.signing_key.__wrapped__()


@pytest.fixture(autouse=True)
def offline_models(tmp_path, monkeypatch, signing_key):
    return auth.offline_oauth.__wrapped__(tmp_path, monkeypatch, signing_key)


def headers(client):
    return {**auth.ORIGIN, "X-CSRF-Token": client.get('/api/v1/session').json()['data']['csrf_token']}


def write(client, method, path, body=None, key=None):
    return client.request(method, path, json=body,
        headers={**headers(client), 'Idempotency-Key': key or str(uuid.uuid4())})


def connect(client, monkeypatch, signing_key, *, admin=False):
    if admin:
        owner, local_headers = auth.local_login(client)
        params, _ = auth.begin(client, headers=local_headers, mode='connect')
        response, _ = auth.complete(client, params, monkeypatch, signing_key)
        assert response.headers['location'].endswith('chatgpt=connected')
        account = client.get('/api/v1/chatgpt/accounts').json()['data']['items'][0]
    else:
        account, _ = auth.registered(client, monkeypatch, signing_key)
        owner = uuid.UUID(client.get('/api/v1/session').json()['data']['user']['id'])
    return owner, account


def route(account, suffix=''):
    return '/api/v1/chatgpt/accounts/'+account['id']+suffix


def fake_model(monkeypatch, *, failure=None, reply=None):
    calls = []
    def exchange(token, model, prompt=None, *, discovery=False, on_send, **kwargs):
        assert token in {'synthetic-access', 'synthetic-renewed-access'}
        on_send()
        calls.append((token, model, discovery))
        if failure:
            raise failure
        return protocols.Reply('', models=[{'id':'synthetic-subscription','name':'Synthetic'}]) if discovery else reply or protocols.Reply('连接测试通过。', 7, 5)
    monkeypatch.setattr(chatgpt_transport, 'exchange', exchange)
    return calls


def ready(client, account, monkeypatch):
    calls = fake_model(monkeypatch)
    changed = write(client, 'PATCH', route(account), {'expected_revision':account['revision'], 'model':'synthetic-subscription'})
    assert changed.status_code == 200, changed.text
    account = changed.json()['data']
    tested = write(client, 'POST', route(account,'/probe'))
    assert tested.status_code == 200 and tested.json()['data']['status']=='success', tested.text
    enabled = write(client, 'PATCH', route(account), {'expected_revision':account['revision'], 'use_for_ai':True})
    assert enabled.status_code == 200, enabled.text
    return enabled.json()['data'], calls


def enable_feature(client):
    enabled = write(client, 'PATCH', '/api/v1/ai/settings', {'enabled':True})
    assert enabled.status_code == 200, enabled.text
    bound = write(client, 'PATCH', '/api/v1/ai/bindings/news_summary', {'enabled':True})
    assert bound.status_code == 200, bound.text


def test_discovery_requires_explicit_post_and_is_not_a_generation_proof(client, monkeypatch, signing_key):
    owner, account = connect(client, monkeypatch, signing_key)
    calls = fake_model(monkeypatch)
    assert client.get(route(account,'/models')).status_code==405
    assert client.post(route(account,'/models')).status_code==403
    key = str(uuid.uuid4())
    first = write(client,'POST',route(account,'/models'),key=key)
    second = write(client,'POST',route(account,'/models'),key=key)
    assert first.status_code==200 and first.json()['data']['models'][0]['id']=='synthetic-subscription'
    assert second.headers['idempotency-replayed']=='true' and len(calls)==1
    selected = write(client,'PATCH',route(account),{'expected_revision':account['revision'],'model':'synthetic-subscription'})
    account = selected.json()['data']
    denied = write(client,'PATCH',route(account),{'expected_revision':account['revision'],'use_for_ai':True})
    assert denied.status_code==409 and denied.json()['error']['code']=='test_required'
    with Session(engine()) as db:
        assert db.scalar(select(func.sum(AiAttempt.request_count)))==1


def test_personal_selection_and_global_default_are_distinct(client, monkeypatch, signing_key):
    owner, account = connect(client,monkeypatch,signing_key,admin=True)
    account, calls = ready(client,account,monkeypatch)
    config = client.get('/api/v1/ai/settings').json()['data']
    assert config['personal_connection_id']==account['connection_id'] and config['personal_model_ready']
    assert config['default_connection_id'] is None and not config['enabled']
    assert all(row['effective_connection_id']==account['connection_id'] for row in config['features'])
    denied = write(client,'PATCH','/api/v1/ai/settings',{'default_connection_id':account['connection_id']})
    assert denied.status_code==404
    enable_feature(client)
    assert len(calls)==1
    with Session(engine()) as db:
        connection,fallback,snapshot = ai_service.resolve_feature(db,'news_summary',owner)
        assert str(connection.id)==account['connection_id'] and fallback is None and snapshot['billing_source']=='chatgpt_plan'


def test_owner_only_connections_in_list_monitor_and_admin_configuration(client, monkeypatch, signing_key):
    owner, account = connect(client,monkeypatch,signing_key)
    account, _ = ready(client,account,monkeypatch)
    with TestClient(app,base_url='http://127.0.0.1:8080') as other:
        auth.local_login(other,username='different-admin')
        assert account['connection_id'] not in other.get('/api/v1/integrations').text
        assert account['connection_id'] not in other.get('/api/v1/data-center/overview').text
        assert account['connection_id'] not in other.get('/api/v1/data-center/calls').text
        assert other.get('/api/v1/chatgpt/accounts').json()['data']['items']==[]
        assert write(other,'POST',route(account,'/probe')).status_code==404
        assert write(other,'PATCH',route(account),{'expected_revision':account['revision'],'use_for_ai':False}).status_code==404
        generic = {'name':'forged','kind':'model','template':'custom_model','endpoint':'https://api.openai.com/v1',
                   'protocol':'openai_responses','model':'forged','enabled':False}
        assert write(other,'PATCH','/api/v1/integrations/'+account['connection_id'],generic).status_code==404
    assert account['connection_id'] in client.get('/api/v1/integrations').text
    assert write(client,'PATCH','/api/v1/ai/settings',{'enabled':True}).status_code==403


def test_changed_personal_model_cannot_silently_use_shared_api(client, monkeypatch, signing_key):
    owner, account = connect(client,monkeypatch,signing_key,admin=True)
    account, calls = ready(client,account,monkeypatch)
    enable_feature(client)
    changed = write(client,'PATCH',route(account),{'expected_revision':account['revision'],'model':'new-untested-model'})
    assert changed.status_code==200 and changed.json()['data']['use_for_ai'] and not changed.json()['data']['tested']
    assert not changed.json()['data']['enabled']
    with Session(engine()) as db:
        with pytest.raises(APIError) as exc:
            ai_service.resolve_feature(db,'news_summary',owner)
        assert exc.value.code=='model_not_ready'
    assert len(calls)==1


def test_subscription_worker_records_actual_request_usage_and_references(client, monkeypatch, signing_key):
    owner, account = connect(client,monkeypatch,signing_key,admin=True)
    account,_ = ready(client,account,monkeypatch)
    enable_feature(client)
    item = news(owner)
    calls = fake_model(monkeypatch,reply=result_reply(item))
    queued = write(client,'POST','/api/v1/ai/runs',{'feature':'news_summary','news_id':str(item)})
    assert queued.status_code==202, queued.text
    job = worker.claim()
    assert job and job.kind=='ai.generate'
    worker.process(job)
    result = client.get('/api/v1/ai/runs/'+queued.json()['data']['id']).json()['data']
    assert result['state']=='succeeded' and result['result']['items'][0]['evidence_refs']==[str(item)]
    with Session(engine()) as db:
        run=db.get(AiRun,uuid.UUID(result['id']))
        attempt=db.scalar(select(AiAttempt).where(AiAttempt.run_id==run.id))
        assert run.config_snapshot['billing_source']=='chatgpt_plan'
        assert attempt.owner_id==owner and attempt.request_count==1 and attempt.cost_cny is None
        assert attempt.input_tokens==100 and attempt.output_tokens==30
        assert attempt.safe_details['billing_source']=='chatgpt_plan'
    assert len(calls)==1


def test_quota_limit_has_no_automatic_retry_or_billing_fallback(client, monkeypatch, signing_key):
    owner, account = connect(client,monkeypatch,signing_key,admin=True)
    account,_ = ready(client,account,monkeypatch)
    enable_feature(client)
    item = news(owner)
    calls = fake_model(monkeypatch,failure=protocols.ModelFailure('subscription_sharing_usage_limit_exceeded','套餐使用限制',sent=True,status=429))
    monkeypatch.setattr(protocols,'exchange',lambda *a,**kw:pytest.fail('No fallback to shared API billing'))
    queued = write(client,'POST','/api/v1/ai/runs',{'feature':'news_summary','news_id':str(item)})
    assert queued.status_code==202
    worker.process(worker.claim())
    result=client.get('/api/v1/ai/runs/'+queued.json()['data']['id']).json()['data']
    assert result['state']=='failed' and result['error_code']=='subscription_sharing_usage_limit_exceeded'
    assert len(calls)==1


def test_expired_access_refresh_finishes_before_probe_transaction(client, monkeypatch, signing_key):
    owner, account = connect(client,monkeypatch,signing_key)
    changed=write(client,'PATCH',route(account),{'expected_revision':account['revision'],'model':'synthetic-subscription'})
    account=changed.json()['data']
    auth.expire_access(uuid.UUID(account['id']))
    def refresh(url,*,form=None,**kwargs):
        assert form['grant_type']=='refresh_token' and form['client_id']==auth.ISSUED
        return {'access_token':'synthetic-renewed-access','refresh_token':'synthetic-renewed-refresh',
                'expires_in':3600,'token_type':'Bearer','scope':oauth.SCOPES}
    monkeypatch.setattr(oauth,'request_json',refresh)
    calls=fake_model(monkeypatch)
    result=write(client,'POST',route(account,'/probe'))
    assert result.status_code==200 and result.json()['data']['status']=='success',result.text
    assert calls==[('synthetic-renewed-access','synthetic-subscription',False)]


def test_superseded_probe_is_not_a_proof_for_new_model(client,monkeypatch,signing_key):
    owner,account=connect(client,monkeypatch,signing_key)
    changed=write(client,'PATCH',route(account),{'expected_revision':account['revision'],'model':'synthetic-subscription'})
    account=changed.json()['data']
    def exchange(token,model,prompt=None,*,on_send,**kwargs):
        on_send()
        with Session(engine()) as db,db.begin():
            connection=db.get(Connection,uuid.UUID(account['connection_id']))
            connection.model='new-model'
            connection.revision+=1
        return protocols.Reply('旧模型的成功',2,3)
    monkeypatch.setattr(chatgpt_transport,'exchange',exchange)
    result=write(client,'POST',route(account,'/probe'))
    assert result.status_code==200 and result.json()['data']['error_code']=='configuration_changed'
    current=client.get('/api/v1/chatgpt/accounts').json()['data']['items'][0]
    assert not current['tested'] and current['model']=='new-model'


def test_hosted_origin_cannot_use_local_plan_connection(client,monkeypatch,signing_key):
    owner,account=connect(client,monkeypatch,signing_key)
    monkeypatch.setattr(settings(),'public_origin','https://hosted.example.com')
    with Session(engine()) as db:
        from app.chatgpt.model_api import authorized
        profile=db.get(ChatGPTAccount,uuid.UUID(account['id']))
        with pytest.raises(APIError) as exc:
            authorized(profile)
        assert exc.value.code=='chatgpt_unavailable'
