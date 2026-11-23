"""Native unmatched quantities, stage receipts and the formal read contract."""
from datetime import datetime,timedelta
from decimal import Decimal
import json

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.market import fuyao_auction as native
from app.market.collect import store_reference_batch
from app.market.models import SourceSettings
from app.market.protocol import InstrumentKey,Response,SHANGHAI,SourceFailure
from app.market.service import source_id
from app.market.terminal_read import SavedTables
from app.models import Provider
from test_data_sources import configure
from test_terminal_workbench import setup,save

KEYS=[InstrumentKey('SSE','600001','stock'),InstrumentKey('SZSE','000002','stock')]
DAY=datetime(2026,10,23,tzinfo=SHANGHAI).date()


def at(value): return datetime.fromisoformat('2026-10-23T'+value+'+08:00')


def encoded(clock,keys=KEYS,up='50',down='-20'):
    items=[]
    for key in keys:
        price=Decimal('11' if key.exchange=='SSE' else '9')
        items.append(dict(thscode=native.fuyao.code(key),ticker=key.symbol,name='合成竞价'+key.symbol,
            auction_price=str(price),pre_close_price='10',auction_pct='10' if price==11 else '-10',
            auction_volume='10',auction_amount=str(price*1000),auction_unmatched=up if price==11 else down))
    final=clock.time().replace(tzinfo=None)>=datetime.strptime('09:25','%H:%M').time()
    return {'code':0,'data':{'timestamp':int(clock.timestamp()*1000),'auction_phase':'closed' if final else 'live',
            'data_status':'final' if final else 'ready','total':len(items),'item':items}}


def raw(value): return json.dumps(value).encode()


def test_native_signed_lots_and_fractional_lots_are_not_matched_turnover():
    clock=at('09:25:02')
    rows,count=native.parse(raw(encoded(clock,down='-20.54')),KEYS,clock,DAY)
    assert count==2
    assert Decimal(rows[0]['unmatched_shares'])==5000
    assert Decimal(rows[1]['unmatched_shares'])==-2054
    assert Decimal(rows[0]['amount_cny'])==11000
    assert Decimal(rows[0]['unmatched_shares'])*Decimal(rows[0]['price'])==55000


@pytest.mark.parametrize('change,reason',[
    ('units','auction_quantity_unit_changed'),('fraction','auction_quantity_unit_changed'),
    ('old','auction_snapshot_stale'),('future','future_source_time'),('not_ready','auction_data_not_ready'),
    ('identity','unexpected_instrument'),('duplicate','unexpected_instrument'),
    ('pct','inconsistent_auction_price'),('bool','invalid_number'),('count','incomplete_auction_page')])
def test_changed_or_incomplete_native_contract_fails_closed(change,reason):
    clock=at('09:20:10');value=encoded(clock)
    if change=='units': value['data']['item'][0]['auction_amount']='110'
    elif change=='fraction': value['data']['item'][0]['auction_unmatched']='1.001'
    elif change=='old': value['data']['timestamp']-=86400000
    elif change=='future': value['data']['timestamp']+=61000
    elif change=='not_ready': value['data']['data_status']='not_ready'
    elif change=='identity': value['data']['item'][0]['thscode']='600999.SH'
    elif change=='duplicate': value['data']['item'][1]=value['data']['item'][0].copy()
    elif change=='pct': value['data']['item'][0]['auction_pct']='1'
    elif change=='bool': value['data']['item'][0]['auction_unmatched']=True
    else: value['data']['total']=3
    with pytest.raises(SourceFailure) as caught:native.parse(raw(value),KEYS,clock,DAY)
    assert caught.value.reason==reason


def test_assembly_clock_tolerance_and_final_receipt_do_not_create_a_morning_frame():
    clock=at('12:00:00');value=encoded(clock);value['data']['timestamp']+=700
    rows,_=native.parse(raw(value),KEYS,clock,DAY)
    from app.market.fuyao_auction_read import eligible
    assert eligible(rows[0],DAY,'09:25')
    assert not eligible(rows[0],DAY,'09:15') and not eligible(rows[0],DAY,'09:20')
    value=encoded(at('09:24:00'));value['data'].update(auction_phase='closed',data_status='final')
    with pytest.raises(SourceFailure):native.parse(raw(value),KEYS,at('09:24:00'),DAY)


def test_page_budget_records_actual_coverage_and_resumes_at_the_next_page():
    clock=at('09:15:10');keys=[InstrumentKey('SSE',str(600000+i),'stock') for i in range(101)]
    ticks=iter([0,51]);calls=[]
    def read(path,params,secret,**kwargs):
        assert path==native.PATH and secret=='fixture-only'
        requested=params['thscodes'].split(',');assert len(requested)<=100
        calls.append(requested)
        subset=[key for key in keys if native.fuyao.code(key) in requested]
        return Response(raw(encoded(clock,subset)),clock,2,200)
    response,batch=native.fetch(keys,clock,'fixture-only',read=read,monotonic=lambda:next(ticks))
    assert response.request_count==1 and len(calls[0])==100
    assert batch.book_progress['next_offset']==100 and batch.rejected==1
    assert batch.records[0][1].rows[0]['expected']=='101'
    assert batch.records[0][1].rows[0]['usable']=='100'


def prepared(client,monkeypatch):
    headers=setup(client,monkeypatch,['tushare_market_limits'])
    configure(client,headers,dict(family=native.FAMILY,symbols=[],collection_mode='full_market',interval_seconds=60,
        rights_basis='Synthetic native auction fixtures; isolated database only.',
        rights={'cache':'allowed','display':'allowed','export':'denied','redistribute':'denied'},
        entitlement_status='verified',enabled=True))
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_limits',[dict(trade_date='20261023',ts_code=native.fuyao.code(key),
            pre_close='10',up_limit='11',down_limit='9') for key in KEYS],at('08:30:00'))
    return headers


def capture(clock,up='50',down='-20'):
    response,batch=native.fetch(KEYS,clock,'fixture-only',read=lambda *a,**k:Response(raw(encoded(clock,up=up,down=down)),clock,1,200))
    with Session(engine()) as db,db.begin():
        p=db.get(Provider,source_id(native.FAMILY))
        store_reference_batch(db,p,db.get(SourceSettings,p.id),batch,response.received_at)


def test_two_sides_real_stage_receipts_and_no_future_backfill(client,monkeypatch):
    prepared(client,monkeypatch)
    capture(at('09:15:10'))
    capture(at('09:20:10'),up='60',down='-30')
    capture(at('09:25:10'),up='70',down='-40.54')
    with Session(engine()) as db:
        earlier=SavedTables(db,at('09:24:00'),DAY).auction.batch(DAY)
        assert earlier.rows[0]['fd_0925_cny'] is None
        reader=SavedTables(db,at('09:26:00'),DAY)
        today=reader.auction.batch(DAY)
        assert today.family==native.FAMILY and today.source=='同花顺竞价'
        assert today.rows[0]['fd_0915_cny']=='55000'
        assert today.rows[0]['fd_0920_cny']=='66000'
        assert today.rows[0]['fd_0925_cny']=='77000'
        downside=reader.auction.native.day(DAY,'down')
        assert downside.rows[0].node_amounts=={'09:15':18000,'09:20':27000,'09:25':36486}
        assert downside.rows[0].node_times==dict.fromkeys(['09:15','09:20','09:25'])
        assert downside.book_state=='available' and not downside.live


def test_late_final_does_not_fill_missed_nodes_and_bulletin_remains_frozen(client,monkeypatch):
    prepared(client,monkeypatch)
    capture(at('12:00:00'),up='70',down='-40.54')
    from app.market.auction_reference import reference_view
    monkeypatch.setattr('app.market.auction_reference.now',lambda db:at('12:02:00'))
    with Session(engine()) as db:
        first=reference_view(db,trade_day=DAY).report
        assert first is not None and first.recovered and first.source_name=='同花顺竞价'
        assert first.totals=={'09:15':None,'09:20':None,'09:25':'77000'}
        assert reference_view(db,trade_day=DAY,as_of_time=at('09:24:00')).report is None
    capture(at('12:01:00'),up='99')
    with Session(engine()) as db:
        assert reference_view(db,trade_day=DAY).report==first


def test_no_permission_or_wrong_day_limits_never_establish_a_seal(client,monkeypatch):
    prepared(client,monkeypatch);capture(at('09:25:10'))
    with Session(engine()) as db,db.begin():
        db.get(Provider,source_id('tushare_market_limits')).enabled=False
    with Session(engine()) as db:
        assert SavedTables(db,at('09:26:00'),DAY).auction.native.batch(DAY) is None


def test_native_schedule_has_three_node_windows_and_does_not_collect_all_day():
    from app.market.realtime_schedule import align_realtime
    assert align_realtime(native.FAMILY,at('09:14:00'))==at('09:15:00')
    assert align_realtime(native.FAMILY,at('09:25:00'))==at('09:25:00')
    assert align_realtime(native.FAMILY,at('12:00:00'))==at('09:15:00')+timedelta(days=1)


@pytest.mark.parametrize('partial',[False,True])
def test_real_collector_checks_binding_before_each_request_and_stores_native_source(client,monkeypatch,partial):
    prepared(client,monkeypatch)
    from app.integrations.credentials import SourceCredentials
    from pydantic import SecretStr
    from app.config import settings
    from app.worker import claim,process
    from app.models import Job
    from test_auction_tables import new_job
    clock=at('09:25:10')
    class Frozen(datetime):
        @classmethod
        def now(cls,tz=None):return clock.astimezone(tz) if tz else clock.replace(tzinfo=None)
    bound=SourceCredentials(SecretStr('fixture-only'),native.fuyao.FUYAO_ENDPOINT)
    checks=[]
    def binding(*args,**kwargs):checks.append(True);return bound
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr('app.market.collect.datetime',Frozen)
    monkeypatch.setattr('app.market.collect.assert_source_binding',binding)
    monkeypatch.setattr(native,'selection',lambda *args:KEYS)
    monkeypatch.setattr(native.fuyao,'request',lambda *a,**k:Response(raw(encoded(clock,KEYS[:1] if partial else KEYS)),clock,2,200))
    ident=new_job(native.FAMILY);job=claim();assert job.id==ident
    process(job)
    with Session(engine()) as db:
        done=db.get(Job,ident)
        assert done.state=='succeeded',done.error_code
        assert done.result_ref['usable']==(1 if partial else 2) and len(checks)>=3
        assert SavedTables(db,at('09:26:00'),DAY).auction.batch(DAY).family==native.FAMILY
        from types import SimpleNamespace
        from app.integrations.data import connection_test
        proof=dict(revision=1,status='queued',job_id=str(ident),source_id=str(source_id(native.FAMILY)))
        result=connection_test(db,SimpleNamespace(revision=1,last_test=proof,source_ids=[str(source_id(native.FAMILY))]))
        assert result['status']=='success' and result['request_count']==1
