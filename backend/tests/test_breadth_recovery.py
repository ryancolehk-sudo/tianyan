"""Recovery is paced and schema-strict; synthetic fixtures, no vendor calls."""
import json
from datetime import UTC, datetime, timedelta
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db import engine
from app.config import settings
from app.market.auction_schedule import BREADTH, next_collection, schema_recheck
from app.market.breadth_recovery import allowed
from app.market.collect import store_observation
from app.market.models import SourceSettings, DataFetchRun
from app.market.protocol import SHANGHAI, Response
from app.market.service import source_id, schedule_sources
from app.evidence.schemas import CalendarPayload
from app.evidence.models import EvidenceVersion
from app.models import AuditEvent, Provider
from app.worker import claim, process
from test_data_sources import login, configure, configuration
from test_market_breadth import sample

def at(value): return datetime.fromisoformat(value+'+08:00')

@pytest.mark.parametrize('clock,expected',[
 ('2026-10-22T09:00:00','2026-10-22T09:15:00'),
 ('2026-10-22T11:20:00','2026-10-22T11:35:00'),
 ('2026-10-22T14:45:00','2026-10-22T15:00:00'),
 ('2026-10-22T14:45:01','2026-10-22T15:00:01'),
 ('2026-10-22T14:50:00','2026-10-23T09:15:00'),
 ('2026-10-22T23:59:59','2026-10-23T09:15:00')])
def test_recheck_spacing_and_session_edges(clock,expected):
 assert next_collection(BREADTH,at(clock),300,schema_probe=True)==at(expected)
 assert next_collection(BREADTH,at(clock).astimezone(UTC),300,schema_probe=True)==at(expected)
 assert schema_recheck(BREADTH,'schema_changed')
 for reason in ['permission_missing','credentials_missing','decoder_changed','upstream_redirect']:
  assert not schema_recheck(BREADTH,reason)

def test_verified_calendars_and_rights_are_required(client):
 headers=login(client);body=configuration('baostock_daily');body['interval_seconds']=21600;configure(client,headers,body)
 clock=at('2020-01-02T10:00:00')
 with Session(engine()) as db,db.begin():
  provider=db.get(Provider,source_id('baostock_daily'));cfg=db.get(SourceSettings,provider.id)
  assert not allowed(db,clock)
  for exchange in ['SSE','SZSE']:
   store_observation(db,provider,cfg,source_key='calendar:'+exchange,kind='calendar',
    payload=CalendarPayload(exchange=exchange,trade_day=clock.date(),is_open=True,calendar_version='synthetic'),
    received_at=clock-timedelta(hours=1),event_at=clock-timedelta(hours=1),eligible=True)
  assert allowed(db,clock)
  assert allowed(db,at('2020-01-02T12:00:00'))
  assert not allowed(db,at('2020-01-03T10:00:00'))
  provider.rights={**provider.rights,'display':'denied'};db.flush()
  assert not allowed(db,clock)

@pytest.mark.parametrize('good',[True,False])
def test_only_valid_probe_recovers_and_restart_does_not_duplicate(client,monkeypatch,good):
 monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime',lambda *args:None)
 headers=login(client);configure(client,headers,configuration(BREADTH))
 monkeypatch.setattr(settings(),'data_collection_enabled',True)
 monkeypatch.setattr('app.market.breadth_recovery.allowed',lambda *_:True)
 with Session(engine()) as db,db.begin():
  cfg=db.get(SourceSettings,source_id(BREADTH));cfg.paused_reason='schema_changed';cfg.next_run_at=datetime.now(UTC)-timedelta(seconds=1)
 schedule_sources();job=claim();assert job and job.payload['schema_probe'] is True
 schedule_sources();assert claim() is None
 value=sample()
 if not good:value['data']='unverified market'
 monkeypatch.setattr('app.market.collect.fetch_https',lambda *_:Response(json.dumps(value).encode(),datetime.now(UTC),1,200))
 process(job)
 with Session(engine()) as db:
  cfg=db.get(SourceSettings,source_id(BREADTH))
  assert cfg.paused_reason==(None if good else 'schema_changed')
  run=db.scalar(select(DataFetchRun));assert run.status==('complete' if good else 'error')
  rows=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==cfg.source_id)))
  assert len(rows)==(2 if good else 0)
  if good:assert db.scalar(select(AuditEvent.id).where(AuditEvent.action=='source.schema_recovered'))
  else:assert cfg.next_run_at>=run.received_at+timedelta(seconds=900)

def test_queued_probe_crossing_session_boundary_spends_no_request(client,monkeypatch):
 monkeypatch.setattr('app.market.realtime_schedule.deferred_realtime',lambda *args:None)
 headers=login(client);configure(client,headers,configuration(BREADTH))
 monkeypatch.setattr(settings(),'data_collection_enabled',True)
 monkeypatch.setattr('app.market.breadth_recovery.allowed',lambda *_:True)
 with Session(engine()) as db,db.begin():
  cfg=db.get(SourceSettings,source_id(BREADTH));cfg.paused_reason='schema_changed';cfg.next_run_at=datetime.now(UTC)-timedelta(seconds=1)
 schedule_sources();job=claim();assert job
 monkeypatch.setattr('app.market.breadth_recovery.allowed',lambda *_:False)
 monkeypatch.setattr('app.market.collect.fetch_https',lambda *_:pytest.fail('HTTP outside verified session'))
 process(job)
 with Session(engine()) as db:
  assert db.scalar(select(DataFetchRun.id)) is None
  assert db.scalar(select(AuditEvent.id).where(AuditEvent.action=='source.request.ashare')) is None
