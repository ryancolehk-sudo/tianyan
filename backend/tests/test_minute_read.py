"""Synthetic minute evidence, real isolated PostgreSQL; no vendor traffic."""
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion, SourceVersion
from app.evidence.schemas import BarPayload
from app.market.collect import store_observation, store_quote_batch
from app.market.datasets import bars
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import InstrumentKey, Quote, QuoteBatch
from app.market.service import source_id
from app.models import Instrument, Provider
from test_data_sources import configuration, configure, login

AT=datetime(2026,10,23,2,33,tzinfo=UTC)
DAY=date(2026,10,23)


def setup(client,family='sina_minute_1m'):
    headers=login(client);body=configuration(family)
    body['symbols']=[dict(exchange='SSE',symbol='603278',asset_type='stock')]
    body['interval_seconds']=3600
    configure(client,headers,body,key=family)
    with Session(engine()) as db,db.begin():
        stock=Instrument(exchange='SSE',symbol='603278',asset_type='stock',name='合成分钟股票')
        other=Instrument(exchange='SZSE',symbol='000001',asset_type='stock',name='合成另一股票')
        db.add_all([stock,other]);db.flush();ids=stock.id,other.id
    return headers,*ids


def payload(stamp='2026-10-23T10:31:00+08:00',interval='1m',close='11'):
    return dict(open='10',high='12',low='9',close=close,volume_shares=None,amount_cny=None,
        volume_native='0',amount_native=None,interval=interval,trade_day=DAY.isoformat(),
        source_clock_at=stamp,clock_semantics='unknown',bar_end_at=None,adjustment='unadjusted')


def store(db,ident,body,family='sina_minute_1m',received=AT):
    provider=db.get(Provider,source_id(family));config=db.get(SourceSettings,provider.id)
    return store_observation(db,provider,config,source_key='minute:'+str(ident)+':'+body['interval']+':'+body['source_clock_at'],
        kind='bar',payload=BarPayload.model_validate(body).model_dump(mode='json'),received_at=received,
        event_at=datetime.fromisoformat(body['source_clock_at']),instrument_id=ident,eligible=True,repeated_state=True)


def test_minute_schema_retains_old_hash_shape_and_unknown_native_units():
    legacy=dict(open='10',high='12',low='9',close='11',volume_shares='10',interval='1m',bar_end_at='2020-01-01T01:31:00Z')
    assert BarPayload.model_validate(legacy).model_dump(mode='json')==legacy
    value=BarPayload.model_validate(payload()).model_dump(mode='json')
    assert value['volume_native']=='0' and value['volume_shares'] is None and value['bar_end_at'] is None
    with pytest.raises(ValidationError):BarPayload.model_validate({**payload(),'trade_day':'2026-10-22'})
    with pytest.raises(ValidationError):BarPayload.model_validate({**payload(),'clock_semantics':'end'})
    with pytest.raises(ValidationError):BarPayload.model_validate({**payload(),'source_clock_at':None})


def test_multi_minute_revision_a_b_a_and_same_content_are_preserved(client,monkeypatch):
    _,ident,_=setup(client);monkeypatch.setattr('app.market.minute_read.now',lambda db:AT)
    with Session(engine()) as db,db.begin():
        a=payload();assert store(db,ident,a)
        assert not store(db,ident,a,received=AT+timedelta(seconds=1))
        assert store(db,ident,{**a,'close':'10.5'},received=AT+timedelta(seconds=2))
        assert store(db,ident,a,received=AT+timedelta(seconds=3))
        assert store(db,ident,payload(stamp='2026-10-23T10:32:00+08:00'))
        versions=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.kind=='bar').order_by(EvidenceVersion.version_number)))
        assert len(versions)==4 and sorted(v.version_number for v in versions)==[1,1,2,3]
    monkeypatch.setattr('app.market.minute_read.now',lambda db:AT+timedelta(seconds=4))
    response=client.get('/api/v1/market-bars',params=dict(instrument_id=str(ident),interval='1m')).json()['data']
    assert len(response['items'])==2
    assert response['items'][0]['payload']['close']=='11'
    assert response['items'][0]['period_state']=='unknown' and response['finality']=='unknown'
    assert response['items'][0]['received_at'].startswith('2026-10-23T02:33:03')


def test_day_interval_and_stock_are_filtered_before_latest_selection(client,monkeypatch):
    _,ident,other=setup(client);monkeypatch.setattr('app.market.minute_read.now',lambda db:AT)
    with Session(engine()) as db,db.begin():
        store(db,ident,payload())
        store(db,other,payload(close='12'))
        store(db,ident,payload(interval='5m',close='9'))
        old={**payload(stamp='2026-10-22T10:31:00+08:00'),'trade_day':'2026-10-22'}
        store(db,ident,old)
    with Session(engine()) as db:
        view=bars(db,ident,'unadjusted',interval='1m')
        assert len(view.items)==1 and view.items[0].payload.close=='11'
        previous=bars(db,ident,'unadjusted',interval='1m',day=date(2026,10,22))
        assert len(previous.items)==1 and previous.state=='historical'
        assert bars(db,other,'unadjusted',interval='1m').state=='not_configured'
        assert bars(db,ident,'unadjusted',interval='5m').items==[]


@pytest.mark.parametrize('revoke',['display','cache','entitlement','enabled'])
def test_revoking_source_never_leaks_saved_minute_values(client,monkeypatch,revoke):
    _,ident,_=setup(client);monkeypatch.setattr('app.market.minute_read.now',lambda db:AT)
    with Session(engine()) as db,db.begin():
        store(db,ident,payload());provider=db.get(Provider,source_id('sina_minute_1m'))
        if revoke in {'display','cache'}:provider.rights={**provider.rights,revoke:'denied'}
        elif revoke=='enabled':provider.enabled=False
        else:provider.entitlement_status='denied'
    value=client.get('/api/v1/market-bars',params=dict(instrument_id=str(ident),interval='1m',source_id=str(source_id('sina_minute_1m')))).json()['data']
    assert value['items']==[] and value['current_daily'] is None
    assert value['state'] in {'disabled','permission_missing'}


def test_last_attempt_error_is_visible_with_saved_data_and_no_zero_fill(client,monkeypatch):
    _,ident,_=setup(client);monkeypatch.setattr('app.market.minute_read.now',lambda db:AT)
    with Session(engine()) as db,db.begin():
        store(db,ident,payload())
        db.add(DataFetchRun(source_id=source_id('sina_minute_1m'),capability='bar.minute',job_id=None,
            started_at=AT,received_at=AT,status='error',reason='timeout',expected=None,accepted=0,
            rejected=0,duplicates=0,elapsed_ms=12,request_count=1,cost_cny=None,body_hash=None))
    value=client.get('/api/v1/market-bars',params=dict(instrument_id=str(ident),interval='1m')).json()['data']
    assert value['state']=='partial' and value['reason']=='timeout'
    assert len(value['items'])==1 and value['last_attempt']['reason']=='timeout'
    assert value['items'][0]['payload']['volume_native']=='0'


def test_old_source_time_is_stale_even_with_just_received_rows(client,monkeypatch):
    _,ident,_=setup(client)
    with Session(engine()) as db,db.begin():store(db,ident,payload())
    monkeypatch.setattr('app.market.minute_read.now',lambda db:AT+timedelta(minutes=20))
    with Session(engine()) as db:
        config=db.get(SourceSettings,source_id('sina_minute_1m'));config.interval_seconds=60;db.commit()
        value=bars(db,ident,'unadjusted',interval='1m')
        assert value.state=='stale' and value.age_seconds>120


def test_normal_auth_validation_and_read_only_empty_path(client,monkeypatch):
    assert client.get('/api/v1/market-bars',params=dict(instrument_id=str(uuid4()),interval='1m')).status_code==401
    _,ident,_=setup(client);monkeypatch.setattr('app.market.minute_read.now',lambda db:AT)
    base=dict(instrument_id=str(ident),interval='1m')
    for changed in [dict(interval='2m'),dict(day='2999-01-01'),dict(adjustment='forward'),dict(source_id=str(uuid4()))]:
        assert client.get('/api/v1/market-bars',params={**base,**changed}).status_code==422
    value=client.get('/api/v1/market-bars',params=base).json()['data']
    assert value['state']=='empty' and value['items']==[]
    assert value['last_attempt'] is None and value['last_success_at'] is None


def test_paid_native_daily_projection_is_opt_in_and_does_not_mutate_history(client,monkeypatch):
    from decimal import Decimal
    _,ident,_=setup(client,'tushare_rt_k')
    monkeypatch.setattr('app.market.minute_read.now',lambda db:AT)
    monkeypatch.setattr('app.market.read_models.now',lambda db:AT)
    key=InstrumentKey('SSE','603278','stock')
    with Session(engine()) as db,db.begin():
        q=Quote(key,'合成分钟股票',Decimal('11'),Decimal('10'),Decimal('10'),Decimal('12'),Decimal('9'),AT,AT,'CNY')
        p=db.get(Provider,source_id('tushare_rt_k'));c=db.get(SourceSettings,p.id)
        version=db.scalar(select(SourceVersion).where(SourceVersion.source_id==p.id))
        db.add(SourceVersion(source_id=p.id,recorded_at=AT-timedelta(days=1),
            descriptor=version.descriptor,content_hash=version.content_hash))
        store_quote_batch(db,p,c,QuoteBatch(records=[q],expected=1))
    with Session(engine()) as db:
        assert bars(db,ident,'unadjusted').current_daily is None
        value=bars(db,ident,'unadjusted',include_live=True)
        assert not value.items and value.current_daily.payload.close=='11'
        assert value.current_daily.projected_from_quote and value.current_daily.period_state=='forming'
        assert value.current_daily_source_family=='tushare_rt_k' and value.data_mode=='live_daily'
        assert bars(db,ident,'forward',include_live=True).current_daily is None
    monkeypatch.setattr('app.market.minute_read.now',lambda db:AT+timedelta(hours=1))
    monkeypatch.setattr('app.market.read_models.now',lambda db:AT+timedelta(hours=1))
    with Session(engine()) as db:
        config=db.get(SourceSettings,source_id('tushare_rt_k'));config.interval_seconds=60;db.commit()
        old=bars(db,ident,'unadjusted',include_live=True)
        assert old.current_daily is not None and old.current_daily_state=='stale'
        # The synthetic clock is now 11:33 Shanghai. Quote freshness pauses at
        # 11:30 during the break, independently of continuous source polling.
        assert old.current_daily_age_seconds==57*60 and old.current_daily_reason


def test_clock_semantics_cannot_disagree_and_known_start_can_be_forming(client,monkeypatch):
    with pytest.raises(ValidationError):
        BarPayload.model_validate({**payload(),'clock_semantics':'end','bar_end_at':'2026-10-23T10:35:00+08:00'})
    with pytest.raises(ValidationError):
        BarPayload.model_validate({**payload(),'trade_day':'2026-10-22','bar_end_at':'2026-10-23T10:35:00+08:00'})
    receipt=AT-timedelta(seconds=30)
    _,ident,_=setup(client);monkeypatch.setattr('app.market.minute_read.now',lambda db:receipt)
    with Session(engine()) as db,db.begin():
        store(db,ident,{**payload(stamp='2026-10-23T10:32:00+08:00'),'clock_semantics':'start','bar_end_at':'2026-10-23T10:33:00+08:00'},received=receipt)
    with Session(engine()) as db:
        value=bars(db,ident,'unadjusted',interval='1m')
        assert not value.historical_reconstruction and value.items[0].period_state=='forming'
    monkeypatch.setattr('app.market.minute_read.now',lambda db:AT+timedelta(minutes=3))
    with Session(engine()) as db:
        value=bars(db,ident,'unadjusted',interval='1m')
        assert value.items[0].period_state=='elapsed' and value.finality=='unknown'


def test_late_receipt_is_not_claimed_backfill_and_history_quality_is_separate(client,monkeypatch):
    _,ident,_=setup(client);monkeypatch.setattr('app.market.minute_read.now',lambda db:AT)
    old={**payload(stamp='2026-10-22T10:31:00+08:00'),'trade_day':'2026-10-22'}
    with Session(engine()) as db,db.begin():
        store(db,ident,old)
        config=db.get(SourceSettings,source_id('sina_minute_1m'));config.paused_reason='timeout'
    with Session(engine()) as db:
        value=bars(db,ident,'unadjusted',interval='1m',day=date(2026,10,22))
        assert value.state=='historical' and value.items[0].arrival_kind=='late_observed'
