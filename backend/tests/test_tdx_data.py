"""SDK isolation and unit/time/range evidence tests; no paid requests."""
import json
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market import tdx_data
from app.market.collect import store_minute_batch
from app.market.protocol import InstrumentKey, SourceFailure
from app.market.service import enqueue_collection, source_id
from app.models import Provider
from app.market.models import SourceSettings
from app.market.tdx_full_minute import context, encode_cursor, progress, validate_context
from test_data_sources import configure, configuration, login

KEY=InstrumentKey('SZSE','000001','stock')
NOW=datetime(2026,10,25,2,tzinfo=UTC)
DAY=date(2026,10,23)


def snapshot(*,full_clock=False):
    return {'Now':'12','LastClose':'11','Open':'11','Max':'12','Min':'10','Volume':'123','Amount':'456',
        'RefreshTime':'20261023150000' if full_clock else '150000','Buyp':['12','11.9','11.8','11.7','11.6'],
        'Buyv':['1','2','3','4','5'],'Sellp':['12.1','12.2','12.3','12.4','12.5'],'Sellv':['5','4','3','2','1']}


def wire(row):return json.dumps({'rows':{'000001.SZ':row}}).encode()


def minutes():
    return {'Date':['20261023','20261023'],'Time':['093100','093200'],'Open':['10','11'],'High':['12','12'],
        'Low':['9','10'],'Close':['11','12'],'Volume':['123.0','456.0'],'Amount':['1.5','2.5']}


def test_snapshot_lots_and_ten_thousand_cny_are_normalized_without_inventing_date():
    row=tdx_data.parse_quotes(wire(snapshot()),[KEY],NOW).records[0]
    assert row.volume_shares==12300 and row.amount_cny==4560000 and row.quote_time is None
    known=tdx_data.parse_quotes(wire(snapshot(full_clock=True)),[KEY],NOW).records[0]
    assert known.quote_time.isoformat()=='2026-10-23T15:00:00+08:00'


def test_five_depth_levels_are_real_shares_not_tick_or_l2():
    batch=tdx_data.parse_depth(wire(snapshot()),[KEY],NOW)
    table=batch.records[0][1]
    assert len(table.rows)==10 and table.rows[0]['volume_shares']=='100'
    assert all(row['quote_time'] is None for row in table.rows)
    assert table.dataset=='tdx.depth5' and '不是逐笔' in table.scope
    row=snapshot();row['Buyp']=row['Buyp'][:3]
    incomplete=tdx_data.parse_depth(wire(row),[KEY],NOW)
    assert incomplete.rejected==1 and not incomplete.records


def test_minute_time_day_volume_and_amount_units():
    batch=tdx_data.parse_minutes(wire(minutes()),[KEY],NOW,DAY)
    assert len(batch.records)==2 and batch.records[0].source_time.isoformat()=='2026-10-23T09:31:00+08:00'
    assert batch.records[0].volume_native==123 and batch.records[0].amount_native==15000
    assert batch.records[0].interval=='1m'
    assert not tdx_data.parse_minutes(wire(minutes()),[KEY],NOW,date(2026,10,24)).records


def test_sdk_bad_key_format_never_starts_native_subprocess(monkeypatch):
    monkeypatch.setattr(subprocess,'run',lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('no child')))
    with pytest.raises(SourceFailure,match='tdx_data_key_required'):
        tdx_data.fetch('tdx_quotes',[KEY],'MCP-key-is-not-data-key')


def test_sdk_secret_only_on_stdin_private_folder_is_cleaned_after_timeout(monkeypatch):
    observed={};secret='TDX-synthetic-private-test'
    def child(args,**kwargs):
        observed.update(args=args,request=json.loads(kwargs['input']))
        root=Path(observed['request']['private_lib_dir']);(root/'user.ini').write_text(secret)
        raise subprocess.TimeoutExpired(args,110,output='do not print upstream')
    monkeypatch.setattr(subprocess,'run',child)
    with pytest.raises(SourceFailure,match='tdx_sdk_timeout'):
        tdx_data.fetch('tdx_quotes',[KEY],secret)
    assert secret not in str(observed['args'])
    assert not Path(observed['request']['private_lib_dir']).exists()


def test_native_raw_message_never_becomes_user_error(monkeypatch):
    monkeypatch.setattr(subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=0,
        stdout=json.dumps({'error':'private-token-and-server-stack'}),stderr='private-token'))
    with pytest.raises(SourceFailure) as caught:
        tdx_data.fetch('tdx_quotes',[KEY],'TDX-synthetic-private-test')
    assert str(caught.value)=='tdx_sdk_unavailable'


def test_empty_sdk_response_is_not_declared_permission_denied(monkeypatch):
    monkeypatch.setattr(subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=0,
        stdout=json.dumps({'error':'tdx_no_data','request_count':1}),stderr=''))
    with pytest.raises(SourceFailure) as caught:
        tdx_data.fetch('tdx_quotes',[KEY],'TDX-synthetic-private-test')
    assert caught.value.reason=='tdx_no_data' and caught.value.request_count==1


def config(family='tdx_minute',points=False):
    body=configuration(family)
    body.update(symbols=[{'exchange':KEY.exchange,'symbol':KEY.symbol,'asset_type':KEY.asset_type}],
        interval_seconds=86400 if family=='tdx_full_minute' else 120,points_enabled=points)
    return body


def test_paid_points_disabled_by_default_and_refuses_before_credential_or_request(client,monkeypatch):
    headers=login(client);configure(client,headers,config())
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    with Session(engine()) as db:
        from app.auth import APIError
        with pytest.raises(APIError) as caught:enqueue_collection(db,source_id('tdx_minute'))
        assert caught.value.code=='points_not_enabled'


def test_minute_ingestion_and_existing_chart_read_use_real_share_contract(client,monkeypatch):
    headers=login(client);configure(client,headers,config(points=True))
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tdx_minute'));cfg=db.get(SourceSettings,provider.id)
        count,_=store_minute_batch(db,provider,cfg,tdx_data.parse_minutes(wire(minutes()),[KEY],NOW,DAY),NOW)
        assert count==2
        from app.evidence.models import EvidenceVersion
        from sqlalchemy import select
        row=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id))
        from decimal import Decimal
        assert row.payload['volume_shares']=='123' and Decimal(row.payload['amount_cny'])==15000
        assert row.payload['clock_semantics']=='unknown' and row.historical_eligible is False
        from app.market.minute_read import minute_bars
        view=minute_bars(db,row.instrument_id,'unadjusted','1m',DAY,provider.id)
        assert view.source_family=='tdx_minute' and len(view.items)==2


@pytest.mark.parametrize('primary_has_rows',[False,True])
def test_selected_minute_source_empty_or_failed_uses_covered_fallback_and_explicit_source_stays_strict(client,monkeypatch,primary_has_rows):
    headers=login(client);configure(client,headers,config(points=True))
    fallback=configuration('sina_minute_1m');fallback.update(symbols=config()['symbols'],interval_seconds=120)
    configure(client,headers,fallback,key='synthetic-minute-fallback')
    from sqlalchemy import select
    from app.evidence.common import now
    from app.evidence.models import EvidenceVersion
    from app.integrations.routing_models import DataRoutes
    from app.market.minute_read import minute_bars
    from app.market.models import DataFetchRun
    from app.market.protocol import SHANGHAI
    with Session(engine()) as db,db.begin():
        clock=now(db);day=clock.astimezone(SHANGHAI).date()
        receipt=max(clock,datetime.combine(day,datetime.min.time(),SHANGHAI)+timedelta(hours=16))
        data={**minutes(),'Date':[day.strftime('%Y%m%d')]*2}
        batch=tdx_data.parse_minutes(wire(data),[KEY],receipt,day)
        for family in ['sina_minute_1m']+(['tdx_minute'] if primary_has_rows else []):
            provider=db.get(Provider,source_id(family));cfg=db.get(SourceSettings,provider.id)
            store_minute_batch(db,provider,cfg,batch,receipt)
        db.add(DataRoutes(id=1,revision=1,choices={'stock_minute_1m':{'provider_id':'tdx_data','allow_points':True}},updated_at=clock))
        db.add(DataFetchRun(source_id=source_id('tdx_minute'),capability='bar.minute',started_at=receipt,
            received_at=receipt+timedelta(milliseconds=100),status='error',reason='timeout',request_count=1,
            expected=1,accepted=0,rejected=0,duplicates=0,elapsed_ms=100))
        db.flush()
        saved=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id('sina_minute_1m')))
        monkeypatch.setattr('app.market.minute_read.now',lambda session:receipt+timedelta(seconds=1))
        for requested in (day,None):
            view=minute_bars(db,saved.instrument_id,'unadjusted','1m',requested)
            assert view.source_family=='sina_minute_1m' and len(view.items)==2
            assert view.observation_day==day
        explicit=minute_bars(db,saved.instrument_id,'unadjusted','1m',day,source_id('tdx_minute'))
        assert explicit.source_family=='tdx_minute' and len(explicit.items)==(2 if primary_has_rows else 0)


def test_full_minute_context_is_bounded_frozen_and_changed_scope_rejects(client,monkeypatch):
    headers=login(client);configure(client,headers,config('tdx_full_minute',True))
    with Session(engine()) as db:
        provider=db.get(Provider,source_id('tdx_full_minute'));cfg=db.get(SourceSettings,provider.id)
        day=datetime.now(UTC).date()
        chunk=context(db,provider,cfg,day)
        assert chunk['request_limit']==1 and len(chunk['symbols'])==1 and chunk['total']==1
        assert validate_context(db,provider,cfg,{'full_minute_context':chunk})==chunk
        with pytest.raises(SourceFailure,match='invalid_minute_cursor'):
            context(db,provider,cfg,day,encode_cursor(chunk['scope_sha256'],20,day))
        cfg.config={**cfg.config,'symbols':[{'exchange':'SSE','symbol':'600000','asset_type':'stock'}]}
        with pytest.raises(SourceFailure,match='configuration_changed'):
            validate_context(db,provider,cfg,{'full_minute_context':chunk})


def test_last_chunk_alone_does_not_claim_scope_complete():
    value={'offset':20,'total':21,'symbols':[{'exchange':KEY.exchange,'symbol':KEY.symbol,'asset_type':'stock'}],
        'mode':'full_market','day':DAY.isoformat(),'scope_sha256':'synthetic-scope','request_limit':1}
    batch=tdx_data.parse_minutes(wire(minutes()),[KEY],NOW,DAY)
    result=progress(value,batch)
    assert result['remaining']==0 and result['next_cursor'] is None
    assert result['chunk_completed']==1 and not result['scope_traversed'] and not result['is_complete']


@pytest.mark.parametrize('reason', ['tdx_data_permission_missing', 'tdx_data_key_required'])
def test_selected_tdx_permission_failure_pauses_sdk_until_explicit_reconfiguration(client, monkeypatch, reason):
    from sqlalchemy import func, select
    from app.auth import APIError
    from app.integrations.routing_models import DataRoutes
    from app.market import resilience
    from app.market.models import DataFetchRun
    from app.market.protocol import SHANGHAI
    from app.market.resilience_models import DataHealth
    from app.market.service import schedule_sources
    from app.market.session import quote_fresh_at
    from app.models import Job
    from app.worker import claim, process
    from test_integration_data import connection

    headers = login(client)
    body = config('tdx_quotes', points=True)
    configure(client, headers, body, key='tdx-permission-initial')
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr('urllib.request.build_opener', lambda *a, **k: pytest.fail('No vendor HTTP in permission tests'))
    credential = {'value': 'MCP-key-is-not-data-key' if reason == 'tdx_data_key_required'
                  else 'TDX-synthetic-private-test'}
    monkeypatch.setattr('app.ai.vault.read_secret', lambda reference: credential['value'])
    with Session(engine()) as db, db.begin():
        connection(db, ('tdx_quotes',), template='tdx_data', endpoint=tdx_data.TDX_ENDPOINT)
        db.add(DataRoutes(id=1, revision=1, choices={'stock_quote': {
            'provider_id': 'tdx_data', 'allow_points': True}}, updated_at=datetime.now(UTC)))
    attempts, children, repaired = [], [], [False]
    original = tdx_data.fetch

    def fetch(*args, **kwargs):
        attempts.append(args[0])
        return original(*args, **kwargs)

    def native_child(args, **kwargs):
        assert args[1:3] == ['-m', 'app.market.tdx_reader']
        children.append(1)
        if not repaired[0]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                'error': 'tdx_data_permission_missing', 'request_count': 1}), stderr='')
        row = snapshot()
        row['RefreshTime'] = quote_fresh_at(datetime.now(UTC)).astimezone(SHANGHAI).strftime('%Y%m%d%H%M%S')
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            'rows': {'000001.SZ': row}, 'request_count': 1}), stderr='')

    monkeypatch.setattr(tdx_data, 'fetch', fetch)
    monkeypatch.setattr(tdx_data.subprocess, 'run', native_child)
    with Session(engine()) as db, db.begin():
        enqueue_collection(db, source_id('tdx_quotes'))
    job = claim()
    assert job is not None
    process(job)
    with Session(engine()) as db, db.begin():
        cfg = db.get(SourceSettings, source_id('tdx_quotes'))
        run = db.scalar(select(DataFetchRun).where(DataFetchRun.source_id == cfg.source_id))
        failure_id = run.id
        assert run.reason == reason and db.get(Job, job.id).state == 'failed'
        assert cfg.paused_reason == reason
        health = db.scalar(select(DataHealth).where(DataHealth.primary_source_id == cfg.source_id))
        assert health.state == 'attention' and health.reason == reason
        assert health.next_action == 'configure_source' and health.next_action_at is None
        cfg.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
    first_children = 0 if reason == 'tdx_data_key_required' else 1
    assert len(attempts) == 1 and len(children) == first_children
    for _ in range(2):
        schedule_sources()
        resilience.reconcile(force=True)
    with Session(engine()) as db:
        with pytest.raises(APIError) as paused:
            enqueue_collection(db, source_id('tdx_quotes'))
        assert paused.value.code == 'source_paused'
        assert db.scalar(select(func.count()).select_from(DataFetchRun)) == 1
    assert claim() is None
    assert len(attempts) == 1 and len(children) == first_children

    repaired[0] = True
    credential['value'] = 'TDX-synthetic-private-test'
    configure(client, headers, body, key='tdx-permission-reconfigured')
    with Session(engine()) as db:
        assert db.get(SourceSettings, source_id('tdx_quotes')).paused_reason is None
    # Respect the original interval without sleeping or rewriting the failure.
    allowed_at = datetime.now(UTC) + timedelta(minutes=5)
    monkeypatch.setattr('app.market.service.now', lambda db: allowed_at)
    with Session(engine()) as db, db.begin():
        enqueue_collection(db, source_id('tdx_quotes'))
    job = claim()
    assert job is not None
    process(job)
    with Session(engine()) as db:
        assert db.get(Job, job.id).state == 'succeeded'
        assert db.get(DataFetchRun, failure_id).reason == reason
        health = db.scalar(select(DataHealth).where(DataHealth.primary_source_id == source_id('tdx_quotes')))
        assert health.state == 'normal' and health.active_source_id == source_id('tdx_quotes')
        assert db.get(SourceSettings, source_id('tdx_quotes')).paused_reason is None
    assert len(attempts) == 2 and len(children) == first_children + 1
