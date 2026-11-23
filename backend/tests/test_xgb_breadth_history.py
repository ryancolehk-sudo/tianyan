"""Independent historical counts retain their source, clock and rights fences."""
from datetime import UTC,date,datetime,timedelta
import json
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import func,select
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import breadth_history as history
from app.market.collect import store_reference_batch
from app.market.models import DataFetchRun,SourceSettings
from app.market.protocol import Response,SHANGHAI,SourceFailure
from app.market.reference import ReferenceBatch
from app.market.registry import SOURCES
from app.market.schemas import SourceConfiguration
from app.market.service import source_id
from app.models import Job,Provider
from test_data_sources import configure,configuration,login

DAY=date(2026,10,23)
CLOCK=datetime.fromisoformat('2026-10-23T17:00:00+08:00')


def body(*,slots=None):
    if slots is None:
        slots=sorted({datetime.combine(DAY,start,SHANGHAI)+timedelta(minutes=n)
            for start in (datetime.min.time().replace(hour=9,minute=31),datetime.min.time().replace(hour=13,minute=1))
            for n in range(120)})
    return {'code':20000,'message':'ok','data':[
        {'timestamp':int(stamp.timestamp()),'limit_up_count':30+n%10,'limit_down_count':n%8}
        for n,stamp in enumerate(slots)]}


def parse(value):
    return history.parse_history(json.dumps(value).encode(),CLOCK,DAY)


def setup(client,monkeypatch):
    headers=login(client)
    value=configuration(history.FAMILY)
    value['rights'].update(export='denied',redistribute='denied')
    value['interval_seconds']=900
    configure(client,headers,value)
    monkeypatch.setattr(history,'settings',lambda:SimpleNamespace(data_collection_enabled=True))
    monkeypatch.setattr(history,'now',lambda db:datetime.now(UTC))
    monkeypatch.setattr('app.market.review_store.available_trade_days',lambda *a,**k:[DAY])
    return headers


def test_parser_counts_can_decrease_and_original_epoch_is_kept():
    payload,complete=parse(body())
    assert complete and len(payload.rows)==240
    assert payload.rows[0]['source_time']=='2026-10-23T09:31:00+08:00'
    assert payload.rows[-1]['source_time']=='2026-10-23T15:00:00+08:00'
    assert int(payload.rows[10]['limit_up_count'])<int(payload.rows[9]['limit_up_count'])
    assert 'timestamp' in payload.rows[0] and '其他来源' in payload.scope
    assert all(c.unit=='家' for c in payload.columns if c.key.endswith('_count'))


@pytest.mark.parametrize('mode,reason', [('wrong_day','historical_response_day_mismatch'),
    ('duplicate','historical_series_order'),('out_of_order','historical_series_order'),
    ('negative','schema_changed'),('boolean','schema_changed'),('float_count','schema_changed'),
    ('lunch','invalid_source_time'),('empty','historical_series_empty'),('api_error','upstream_unavailable')])
def test_parser_rejects_wrong_dates_ambiguous_counts_and_empty_responses(mode,reason):
    value=body();rows=value['data']
    if mode=='wrong_day':rows[0]['timestamp']-=86400
    elif mode=='duplicate':rows[1]=dict(rows[0])
    elif mode=='out_of_order':rows[0],rows[1]=rows[1],rows[0]
    elif mode=='negative':rows[0]['limit_up_count']=-1
    elif mode=='boolean':rows[0]['limit_up_count']=True
    elif mode=='float_count':rows[0]['limit_down_count']=1.5
    elif mode=='lunch':rows[0]['timestamp']=int(CLOCK.replace(hour=12).timestamp())
    elif mode=='empty':value['data']=[]
    else:value['code']=404
    with pytest.raises(SourceFailure,match=reason):parse(value)


def test_parser_partial_day_is_explicit_and_never_pads_missing_minutes():
    value=body();value['data'].pop()
    payload,complete=parse(value)
    assert not complete and len(payload.rows)==239


@pytest.mark.parametrize('clock,allowed', [('08:59:59',False),('09:00:00',True),('09:14:59',True),
    ('09:15:00',False),('12:00:00',False),('15:04:59',False),('15:05:00',True),('21:29:59',True),('21:30:00',False)])
def test_parser_window_excludes_live_trading_hours(clock,allowed):
    assert history.in_window(datetime.fromisoformat(f'2026-10-23T{clock}+08:00')) is allowed


def test_parser_url_is_fixed_and_always_contains_a_date():
    url=history.history_url(DAY)
    assert url.startswith(history.ENDPOINT+'?') and 'date=2026-10-23' in url
    assert 'limit_up_count' in url and 'limit_down_count' in url
    with pytest.raises(SourceFailure,match='invalid_request_day'):history.history_url('2026-10-23')


def test_registry_and_configuration_expose_independent_family():
    definition=SOURCES[history.FAMILY]
    assert definition.name=='选股宝 · 历史涨跌停家数'
    assert definition.capabilities==(history.CAPABILITY,) and definition.minimum_interval==900
    assert SourceConfiguration.model_validate(configuration(history.FAMILY)).family==history.FAMILY


def store(db,received):
    provider=db.get(Provider,source_id(history.FAMILY));config=db.get(SourceSettings,provider.id)
    payload,complete=parse(body());assert complete
    store_reference_batch(db,provider,config,ReferenceBatch(
        records=[(f'breadth.history:{DAY}',payload,None)],expected=1),received)


def test_saved_history_respects_receipt_day_units_and_current_revocation(client,monkeypatch):
    setup(client,monkeypatch)
    received=datetime.now(UTC)+timedelta(seconds=1)
    with Session(engine()) as db,db.begin():store(db,received)
    with Session(engine()) as db:
        assert history.saved_series(db,DAY,received-timedelta(seconds=1)) is None
        result=history.saved_series(db,DAY,received)
        assert result['state']=='complete' and len(result['points'])==240
        assert result['source_family']==history.FAMILY and result['received_at']!=result['points'][-1]['time']
        assert history.saved_series(db,DAY+timedelta(days=1),received) is None
        uuid.UUID(result['evidence_refs'][0]['evidence_id'])
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(history.FAMILY))
        provider.rights={**provider.rights,'display':'denied'}
    with Session(engine()) as db:
        assert history.saved_series(db,DAY,received) is None


def test_export_and_redistribution_must_be_explicitly_denied(client):
    headers=login(client)
    for field in ('export','redistribute'):
        value=configuration(history.FAMILY)
        value['rights'].update(export='denied',redistribute='denied')
        value['rights'][field]='unknown'
        response=client.post('/api/v1/data-sources',json=value,headers={**headers,'Idempotency-Key':uuid.uuid4().hex})
        assert response.status_code==422 and response.json()['error']['code']=='permission_missing'


def test_explicit_source_request_queues_history_kind_and_deduplicates(client,monkeypatch):
    setup(client,monkeypatch)
    monkeypatch.setattr(history,'in_window',lambda stamp:True)
    from app.market import service
    config=app_settings()
    monkeypatch.setattr('app.config.settings',lambda:config.model_copy(update={'data_collection_enabled':True}))
    with Session(engine()) as db,db.begin():
        one=service.enqueue_collection(db,source_id(history.FAMILY),day=DAY)
        two=service.enqueue_collection(db,source_id(history.FAMILY),day=DAY)
        assert one.id==two.id and one.kind==history.JOB_KIND
        assert one.payload['breadth_history_context']['day']==DAY.isoformat()


def test_generic_scheduler_cannot_send_history_family_to_source_collect(client,monkeypatch):
    setup(client,monkeypatch)
    config=app_settings()
    monkeypatch.setattr('app.config.settings',lambda:config.model_copy(update={'data_collection_enabled':True}))
    from app.market.service import schedule_sources
    schedule_sources()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job))==0


def test_complete_saved_day_is_not_requested_again(client,monkeypatch):
    setup(client,monkeypatch)
    monkeypatch.setattr(history,'in_window',lambda stamp:True)
    received=datetime.now(UTC)+timedelta(seconds=1)
    monkeypatch.setattr(history,'now',lambda db:received)
    with Session(engine()) as db,db.begin():
        store(db,received)
        provider=db.get(Provider,source_id(history.FAMILY));config=db.get(SourceSettings,provider.id)
        job=history.enqueue_breadth_archive(db,provider,config,DAY)
        assert job.state=='succeeded' and job.result_ref['already_saved'] is True
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==0
