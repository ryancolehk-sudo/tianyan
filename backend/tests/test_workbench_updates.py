"""Real PostgreSQL publication, session revocation and cold-request checks."""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import read_cache
from app.auth import COOKIE
from app.db import engine
from app.market import updates
from app.models import User
from app.read_cache_models import ReadCache
from test_read_cache import seed, worker, ready_parent


class Request:
    async def is_disconnected(self):
        return False


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings(), 'read_cache_enabled', True)


def context(client):
    return client.cookies.get(COOKIE), client.get('/api/v1/session').json()['data']['cache_scope']


def test_cold_page_only_queues_and_worker_publishes(client, monkeypatch):
    seed(client, monkeypatch)
    compute = read_cache.compute
    monkeypatch.setattr(read_cache, 'compute', lambda *a: pytest.fail('GET ran expensive page computation'))
    response=client.get('/api/v1/workbench-view?view=overview')
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'read_cache_warming'
    with Session(engine()) as db:
        entry=db.scalar(select(ReadCache))
        assert entry.payload is None and entry.lease_token is None and entry.dirty
    monkeypatch.setattr(read_cache, 'compute', compute)
    assert worker()
    assert client.get('/api/v1/workbench-view?view=overview').status_code == 200


def test_event_state_filters_owner_scope_and_historical_cutoffs(client, monkeypatch):
    seed(client, monkeypatch)
    ready_parent(client, '/api/v1/workbench-view?view=overview')
    token,scope=context(client)
    with Session(engine()) as db,db.begin():
        own=db.scalar(select(ReadCache))
        for owner, other_scope, params in [(uuid.uuid4(),scope,{}),
                (own.owner_id,'0'*64,{}), (own.owner_id,scope,{'as_of_time':'2026-10-23T10:30:00+08:00'})]:
            db.add(ReadCache(key=uuid.uuid4().hex,owner_id=owner,scope=other_scope,operation='workbench',
                params=params,dependency_keys=[],payload=own.payload,revision=uuid.uuid4().hex,
                built_at=datetime.now(UTC),validated_at=datetime.now(UTC)))
    state=updates.published(Request(),token,scope)
    assert state['views']==['overview']
    with Session(engine()) as db,db.begin():
        for row in db.scalars(select(ReadCache).where(ReadCache.params['as_of_time'].astext.is_not(None))):
            row.revision='changed-history'
    assert updates.published(Request(),token,scope)==state


def test_stream_receives_committed_publication_and_stops_after_revocation(client, monkeypatch):
    seed(client, monkeypatch)
    ready_parent(client, '/api/v1/workbench-view?view=overview')
    token,scope=context(client)
    with Session(engine()) as db:
        entry=db.scalar(select(ReadCache));owner=str(entry.owner_id);key=entry.key

    async def run():
        stream=updates.notifications(Request(),token,owner,scope)
        try:
            first=await asyncio.wait_for(anext(stream),3)
            assert first.startswith('event: update')
            first_body=json.loads(first.split('data: ',1)[1])
            assert first_body['publications']==[{'path':'/api/v1/workbench-view?view=overview','revision':entry.revision}]
            with Session(engine()) as db,db.begin():
                db.get(ReadCache,key).revision='published-new-version'
                from sqlalchemy import text
                db.execute(text("SELECT pg_notify('tianyan_workbench', :p)"),
                    {'p':json.dumps({'owner':owner,'scope':scope})})
            second=await asyncio.wait_for(anext(stream),3)
            assert second.startswith('event: update') and second != first
            assert json.loads(second.split('data: ',1)[1])['publications']==[
                {'path':'/api/v1/workbench-view?view=overview','revision':'published-new-version'}]
            today=datetime.now(UTC).astimezone(read_cache.SHANGHAI).date().isoformat()
            with Session(engine()) as db,db.begin():
                db.add(ReadCache(key=uuid.uuid4().hex,owner_id=uuid.UUID(owner),scope=scope,operation='details',
                    params={'view':'overview','day':today,'detail_codes':['000100.SZ']},dependency_keys=[],
                    payload={'data':{}},revision='details-only',built_at=datetime.now(UTC),validated_at=datetime.now(UTC)))
                db.execute(text("SELECT pg_notify('tianyan_workbench', :p)"),
                    {'p':json.dumps({'owner':owner,'scope':scope})})
            third=await asyncio.wait_for(anext(stream),3)
            assert json.loads(third.split('data: ',1)[1])['publications']==[
                {'path':'/api/v1/workbench-details?codes=000100.SZ&day='+today,'revision':'details-only'}]
            with Session(engine()) as db,db.begin():
                db.get(User,uuid.UUID(owner)).enabled=False
            denied=await asyncio.wait_for(anext(stream),3)
            assert denied.startswith('event: permission-denied')
            with pytest.raises(StopAsyncIteration):await anext(stream)
        finally:
            await stream.aclose()
    asyncio.run(run())


def test_event_endpoint_requires_authentication(client):
    assert client.get('/api/v1/workbench-updates').status_code == 401


def test_pinned_publications_keep_microseconds_and_exclude_retired_contexts(client, monkeypatch):
    from urllib.parse import urlencode

    seed(client, monkeypatch)
    ready_parent(client, '/api/v1/workbench-view?view=overview')
    token, scope = context(client)
    moment = datetime.now(UTC)
    today = moment.astimezone(read_cache.SHANGHAI).date().isoformat()
    cutoff = moment.astimezone(read_cache.SHANGHAI).replace(microsecond=123456).isoformat()
    with Session(engine()) as db, db.begin():
        owner = db.scalar(select(User.id))
        for index in range(300):
            db.add(ReadCache(key=uuid.uuid4().hex,owner_id=owner,scope=scope,operation='details',
                params={'day':today,'detail_codes':['000100.SZ'],'observation_cutoff':cutoff,'sector':'BK'+str(index).zfill(4)+'.DC'},
                dependency_keys=[],payload={'data':{}},revision='retired',built_at=moment,validated_at=moment,
                focused_at=moment-timedelta(seconds=30)))
        db.add(ReadCache(key=uuid.uuid4().hex,owner_id=owner,scope=scope,operation='details',
            params={'day':today,'detail_codes':['000100.SZ'],'observation_cutoff':cutoff},dependency_keys=[],
            payload={'data':{}},revision='selected-ready',built_at=moment,validated_at=moment,focused_at=moment))
    value = updates.published(Request(), token, scope)
    path = '/api/v1/workbench-details?' + urlencode(sorted({'codes':'000100.SZ','day':today,'observation_cutoff':cutoff}.items()))
    assert value['entries'][path] == 'selected-ready' and len(value['entries']) == 2


@pytest.mark.parametrize('query', [
    'observation_cutoff=2099-01-01T00:00:00Z',
    'observation_cutoff=2026-10-23T10:30:00',
    'observation_cutoff=2026-10-23T02:30:00Z&as_of_time=2026-10-23T02:30:00Z',
    'day=2026-10-24&observation_cutoff=2026-10-23T02:30:00Z',
])
def test_invalid_pinned_details_do_not_create_background_entries(client, monkeypatch, query):
    seed(client, monkeypatch)
    prefix = '' if query.startswith('day=') else 'day=2026-10-23&'
    response = client.get('/api/v1/workbench-details?' + prefix + query)
    assert response.status_code == 422
    with Session(engine()) as db:
        assert db.scalar(select(ReadCache)) is None


@pytest.mark.parametrize('query', ['day=2099-01-01', 'as_of_time=2099-01-01T00:00:00Z',
    'day=2026-10-24&as_of_time=2026-10-23T10:00:00Z'])
def test_invalid_cold_context_is_rejected_without_endless_background_queue(client,monkeypatch,query):
    seed(client,monkeypatch)
    response=client.get('/api/v1/workbench-view?'+query)
    assert response.status_code==422
    with Session(engine()) as db:
        assert db.scalar(select(ReadCache)) is None
