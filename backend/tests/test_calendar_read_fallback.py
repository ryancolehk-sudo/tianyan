"""Explicit synthetic calendar receipts on disposable PostgreSQL; no network."""
from datetime import UTC,date,datetime,timedelta
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion,SourceVersion
from app.market.collect import store_calendar_batch
from app.market.models import SourceSettings
from app.market.review_store import available_trade_days,family_id
from app.market.terminal_read import SavedTables
from app.models import Provider,User
from app.read_cache import dependency_keys

CUTOFF=datetime(2026,11,14,4,tzinfo=UTC)
DAYS=[date(2026,11,11),date(2026,11,12),date(2026,11,13)]
RIGHTS=dict(display='allowed',cache='allowed',export='denied',redistribute='denied')

def seed(db,family,exchanges,*,days=None,received=None,registered=None):
    owner=db.query(User).first()
    if owner is None:
        owner=User(username='synthetic-calendar-'+uuid.uuid4().hex,password_hash='synthetic-unusable',role='admin')
        db.add(owner);db.flush()
    provider=Provider(id=family_id(family),family=family,kind='calendar',name='Synthetic calendar',
        configured=True,enabled=True,entitlement_status='verified',adapter_version='synthetic-calendar-v1',
        capabilities=['calendar.month'],rights=RIGHTS)
    db.add(provider);db.flush()
    config=SourceSettings(source_id=provider.id,owner_id=owner.id,configured_at=CUTOFF-timedelta(days=30),
        interval_seconds=21600,next_run_at=CUTOFF,config={},rights_basis='Synthetic isolated test only')
    db.add(config)
    db.add(SourceVersion(source_id=provider.id,recorded_at=registered or CUTOFF-timedelta(days=30),
        descriptor=dict(configured=True,enabled=True,entitlement_status='verified',rights=RIGHTS),content_hash='a'*64))
    db.flush()
    store_calendar_batch(db,provider,config,days if days is not None else [(day,True) for day in DAYS],
        received or CUTOFF-timedelta(days=1),exchanges=exchanges)
    return provider

def read(db,reader,cutoff=CUTOFF):
    if reader=='review':return available_trade_days(db,as_of_time=cutoff,end_day=date(2026,11,14))
    return SavedTables(db,cutoff).calendar_days

@pytest.mark.parametrize('reader',['review','terminal'])
def test_verified_free_calendar_is_used_when_paid_calendar_is_absent(reader):
    with Session(engine()) as db,db.begin():
        seed(db,'baostock_daily',('SSE','SZSE'))
        assert read(db,reader)==DAYS

@pytest.mark.parametrize('reader',['review','terminal'])
def test_missing_paid_exchange_can_use_observed_free_receipts(reader):
    with Session(engine()) as db,db.begin():
        seed(db,'tushare_calendar_sse',('SSE',))
        seed(db,'baostock_daily',('SSE','SZSE'))
        assert read(db,reader)==DAYS

@pytest.mark.parametrize('reader',['review','terminal'])
def test_paid_closed_day_is_not_overridden_by_newer_free_open_day(reader):
    with Session(engine()) as db,db.begin():
        rows=[(day,day!=DAYS[1]) for day in DAYS]
        seed(db,'tushare_calendar_sse',('SSE',),days=rows,received=CUTOFF-timedelta(days=2))
        seed(db,'tushare_calendar_szse',('SZSE',),days=rows,received=CUTOFF-timedelta(days=2))
        seed(db,'baostock_daily',('SSE','SZSE'))
        assert read(db,reader)==[DAYS[0],DAYS[2]]

@pytest.mark.parametrize('reader',['review','terminal'])
def test_revoked_paid_rights_allow_currently_permitted_free_calendar(reader):
    with Session(engine()) as db,db.begin():
        for family,exchange in [('tushare_calendar_sse','SSE'),('tushare_calendar_szse','SZSE')]:
            provider=seed(db,family,(exchange,),days=[(day,False) for day in DAYS])
            provider.rights={**RIGHTS,'display':'denied'}
        seed(db,'baostock_daily',('SSE','SZSE'))
        assert read(db,reader)==DAYS

@pytest.mark.parametrize('reader',['review','terminal'])
def test_free_receipt_received_after_cutoff_never_enters_earlier_read(reader):
    with Session(engine()) as db,db.begin():
        seed(db,'baostock_daily',('SSE','SZSE'),received=CUTOFF+timedelta(seconds=1))
        assert read(db,reader)==[]

@pytest.mark.parametrize('reader',['review','terminal'])
def test_missing_exchange_stays_unknown_without_weekday_inference(reader):
    with Session(engine()) as db,db.begin():
        seed(db,'szse_public',('SZSE',))
        assert read(db,reader)==[]

@pytest.mark.parametrize('reader',['review','terminal'])
def test_wrong_exchange_in_free_source_does_not_satisfy_sse(reader):
    with Session(engine()) as db,db.begin():
        seed(db,'szse_public',('SSE','SZSE'))
        assert read(db,reader)==[]

@pytest.mark.parametrize('reader',['review','terminal'])
def test_free_day_correction_to_closed_does_not_revive_older_open_day(reader):
    with Session(engine()) as db,db.begin():
        p=seed(db,'baostock_daily',('SSE','SZSE'),received=CUTOFF-timedelta(days=2))
        config=db.get(SourceSettings,p.id)
        store_calendar_batch(db,p,config,[(DAYS[1],False)],CUTOFF-timedelta(days=1),exchanges=('SSE','SZSE'))
        assert read(db,reader)==[DAYS[0],DAYS[2]]

@pytest.mark.parametrize('operation',['review-day','review-series','review-overview','review-new-highs','review-analysis','workbench','details'])
def test_derived_reads_subscribe_to_free_calendar_receipt_changes(operation):
    keys=dependency_keys(operation,uuid.uuid4(),{'view':'overview'})
    assert {'family:baostock_daily','family:szse_public'}<=set(keys)

def test_review_respects_calendar_registration_at_historical_cutoff():
    with Session(engine()) as db,db.begin():
        seed(db,'baostock_daily',('SSE','SZSE'),registered=CUTOFF+timedelta(seconds=1))
        assert available_trade_days(db,as_of_time=CUTOFF)==[]

def test_review_does_not_include_today_before_existing_close_boundary():
    with Session(engine()) as db,db.begin():
        day=date(2026,11,14)
        seed(db,'baostock_daily',('SSE','SZSE'),days=[(DAYS[-1],True),(day,True)])
        assert available_trade_days(db,as_of_time=CUTOFF)==[DAYS[-1]]

@pytest.mark.parametrize('reader',['review','terminal'])
@pytest.mark.parametrize('change',['unverified','exchange','boolean'])
def test_invalid_latest_paid_correction_blocks_older_or_free_open_day(reader,change):
    with Session(engine()) as db,db.begin():
        paid=seed(db,'tushare_calendar_sse',('SSE',),received=CUTOFF-timedelta(days=2))
        seed(db,'tushare_calendar_szse',('SZSE',),received=CUTOFF-timedelta(days=2))
        seed(db,'baostock_daily',('SSE','SZSE'))
        old=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==paid.id,
            EvidenceVersion.source_key=='calendar:SSE:'+DAYS[1].isoformat()))
        values={column.name:getattr(old,column.name) for column in EvidenceVersion.__table__.columns}
        payload={**old.payload}
        if change=='exchange':payload['exchange']='SZSE'
        if change=='boolean':payload['is_open']='true'
        values.update(id=uuid.uuid4(),source_revision='synthetic-correction-'+uuid.uuid4().hex,
            version_number=old.version_number+1,payload=payload,received_at=CUTOFF-timedelta(hours=1),
            available_at=CUTOFF-timedelta(hours=1),supersedes_id=old.id,content_hash='b'*64,
            provenance='unverified' if change=='unverified' else old.provenance)
        db.add(EvidenceVersion(**values));db.flush()
        assert read(db,reader)==[DAYS[0],DAYS[2]]

@pytest.mark.parametrize('operation',['review-day','workbench','details'])
def test_upgraded_reads_get_a_new_cache_identity_with_calendar_dependencies(operation):
    from app import read_cache
    from app.read_cache_models import ReadCache
    with Session(engine()) as db,db.begin():
        owner=User(username='synthetic-cache-'+uuid.uuid4().hex,password_hash='synthetic-unusable',role='admin')
        db.add(owner);db.flush();owner_id=owner.id
        params={};scope='synthetic-calendar-cache'
        old_key=read_cache.fingerprint([scope,operation,params])
        db.add(ReadCache(key=old_key,owner_id=owner_id,scope=scope,operation=operation,params=params,
            dependency_keys=['authorization'],dirty=False,payload={'data':{}},revision='a'*64,
            built_at=CUTOFF,refresh_after=CUTOFF,accessed_at=CUTOFF))
    with Session(engine()) as db:
        owner=db.get(User,owner_id)
        entry=read_cache.ensure_entry(db,owner,scope,operation,params)
        assert entry.key!=old_key and entry.payload is None
        assert {'family:baostock_daily','family:szse_public'}<=set(entry.dependency_keys)

@pytest.mark.parametrize('operation',['workbench','details'])
@pytest.mark.parametrize('legacy_last',[True,False])
def test_retained_legacy_cache_cannot_mask_or_wake_a_current_publication(monkeypatch,operation,legacy_last):
    from types import SimpleNamespace
    from app import read_cache
    from app.market import updates
    from app.read_cache_models import ReadCache
    moment=datetime.now(UTC)
    params=({'view':'overview'} if operation=='workbench' else
        {'day':moment.astimezone(read_cache.SHANGHAI).date().isoformat(),'detail_codes':['000100.SZ']})
    # The two keys represent entries retained across the calendar-read upgrade.
    for index in range(100):
        scope='synthetic-calendar-publication-'+str(index)
        old_key=read_cache.fingerprint([scope,operation,params])
        current_key=read_cache.fingerprint([scope,operation,params,'calendar-fallback-1'])
        if (old_key>current_key)==legacy_last:break
    assert old_key!=current_key and (old_key>current_key)==legacy_last
    with Session(engine()) as db,db.begin():
        owner=User(username='synthetic-publication-'+uuid.uuid4().hex,password_hash='synthetic-unusable',role='admin')
        db.add(owner);db.flush();owner_id=owner.id
        for key,revision in ((old_key,'synthetic-legacy'),(current_key,'synthetic-current')):
            db.add(ReadCache(key=key,owner_id=owner_id,scope=scope,operation=operation,params=params,
                dependency_keys=[],payload={'data':{}},revision=revision,built_at=moment,validated_at=moment))
    monkeypatch.setattr(updates,'principal',lambda *args,**kwargs:(SimpleNamespace(id=owner_id),None))
    monkeypatch.setattr(updates,'cache_scope',lambda *args:scope)
    state=updates.published(None,'synthetic-no-session',scope)
    path=updates.read_path(operation,params)
    assert state['entries']=={path:'synthetic-current'}
    with Session(engine()) as db,db.begin():db.get(ReadCache,old_key).revision='synthetic-legacy-changed'
    assert updates.published(None,'synthetic-no-session',scope)==state

@pytest.mark.parametrize('operation',['workbench','details'])
def test_legacy_publication_remains_available_until_current_cache_is_built(monkeypatch,operation):
    from types import SimpleNamespace
    from app import read_cache
    from app.market import updates
    from app.read_cache_models import ReadCache
    moment=datetime.now(UTC);scope='synthetic-calendar-warming'
    params=({'view':'overview'} if operation=='workbench' else
        {'day':moment.astimezone(read_cache.SHANGHAI).date().isoformat(),'detail_codes':['000100.SZ']})
    with Session(engine()) as db,db.begin():
        owner=User(username='synthetic-warming-'+uuid.uuid4().hex,password_hash='synthetic-unusable',role='admin')
        db.add(owner);db.flush();owner_id=owner.id
        for current in (False,True):
            key=read_cache.fingerprint([scope,operation,params]+(['calendar-fallback-1'] if current else []))
            db.add(ReadCache(key=key,owner_id=owner_id,scope=scope,operation=operation,params=params,
                dependency_keys=[],payload=None if current else {'data':{}},revision=None if current else 'synthetic-legacy',
                built_at=None if current else moment,validated_at=None if current else moment))
    monkeypatch.setattr(updates,'principal',lambda *args,**kwargs:(SimpleNamespace(id=owner_id),None))
    monkeypatch.setattr(updates,'cache_scope',lambda *args:scope)
    assert updates.published(None,'synthetic-no-session',scope)['entries']=={
        updates.read_path(operation,params):'synthetic-legacy'}
