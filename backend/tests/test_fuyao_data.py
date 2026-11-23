"""Synthetic REST/SQL evidence tests; no market vendor requests."""
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.integrations.credentials import job_context, resolve_source_credentials
from app.market import fuyao
from app.market.collect import store_quote_batch, store_reference_batch
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import InstrumentKey, Response, SourceFailure
from app.market.read_models import quote_batch
from app.market.service import enqueue_collection, source_id
from app.models import Instrument, Provider
from app.worker import claim, process
from test_data_sources import configure, configuration, login
from test_integration_data import connection

KEY=InstrumentKey('SZSE','000001','stock')
NOW=datetime(2026,10,25,2,0,tzinfo=UTC)


def envelope(rows,**data):
    return json.dumps({'code':0,'data':{'item':rows,**data}}).encode()


def snapshot(*,clock=None,price='12.3'):
    return envelope([{'thscode':'000001.SZ','ticker':'000001','last_price':price,'prev_price':'12',
        'open_price':'12.1','high_price':'12.5','low_price':'12','volume':12345,'turnover':152000,
        **({'quote_time_ms':int(clock.timestamp()*1000)} if clock else {})}],timestamp=int(NOW.timestamp()*1000))


def config(family='fuyao_quotes'):
    body=configuration(family)
    body.update(symbols=[{'exchange':KEY.exchange,'symbol':KEY.symbol,'asset_type':KEY.asset_type}],
        interval_seconds=86400 if family in {'fuyao_financial','fuyao_adj_factor'} else 21600 if family in {'fuyao_daily','fuyao_index_daily'} else 60)
    if family=='fuyao_indices':body['symbols']=[]
    return body


def setup(client,monkeypatch,family='fuyao_quotes'):
    headers=login(client)
    configure(client,headers,config(family),key='synthetic-'+family)
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    monkeypatch.setattr('app.ai.vault.read_secret',lambda reference:'synthetic-fuyao-private-key')
    with Session(engine()) as db,db.begin():
        connection(db,(family,),template='fuyao',endpoint=fuyao.FUYAO_ENDPOINT)
    return headers


def choose(db,capability='stock_quote'):
    from app.integrations.routing_models import DataRoutes
    row=db.get(DataRoutes,1)
    if row is None:
        row=DataRoutes(id=1,revision=1,choices={},updated_at=datetime.now(UTC));db.add(row)
    row.choices={**row.choices,capability:{'provider_id':'fuyao'}}
    db.flush()


def test_outer_server_clock_does_not_date_weekend_quote_and_share_units_are_explicit():
    batch=fuyao.parse_quotes(snapshot(),[KEY],NOW)
    row=batch.records[0]
    assert row.quote_time is None and row.volume_shares==Decimal(12345) and row.amount_cny==Decimal(152000)
    assert row.instrument==KEY and row.price_unit=='CNY'


def test_explicit_row_clock_future_timestamp_and_unrequested_identity():
    clock=NOW-timedelta(minutes=2)
    assert fuyao.parse_quotes(snapshot(clock=clock),[KEY],NOW).records[0].quote_time==clock
    batch=fuyao.parse_quotes(snapshot(clock=NOW+timedelta(hours=2)),[KEY],NOW)
    assert batch.rejected=={'000001.SZ':'future_source_time'}
    with pytest.raises(SourceFailure,match='unexpected_instrument'):
        fuyao.parse_quotes(snapshot(),[InstrumentKey('SSE','000001','index')],NOW)


def test_empty_or_zero_price_cannot_mark_quote_capability_verified():
    for price in (None,'0'):
        result=fuyao.parse_quotes(snapshot(price=price),[KEY],NOW)
        assert not result.records and result.rejected=={'000001.SZ':'instrument_unavailable'}


@pytest.mark.parametrize('symbol,ticker',[('000001','1A0001'),('000688','1B0688')])
def test_shanghai_index_internal_ticker_alias_keeps_canonical_identity(symbol,ticker):
    key=InstrumentKey('SSE',symbol,'index')
    body=envelope([{'thscode':symbol+'.SH','ticker':ticker,'last_price':'3000',
        'prev_price':'2990','open_price':'2995','high_price':'3010','low_price':'2990',
        'volume':123,'turnover':123000}],timestamp=int(NOW.timestamp()*1000))
    batch=fuyao.parse_quotes(body,[key],NOW)
    assert batch.expected==1 and not batch.rejected and len(batch.records)==1
    assert batch.records[0].instrument==key and batch.records[0].price_unit=='point'
    assert batch.records[0].quote_time is None and batch.records[0].source_snapshot_time==NOW
    assert batch.records[0].volume_shares is None


@pytest.mark.parametrize('key,ticker',[
    (InstrumentKey('SSE','000001','index'),'1B0688'),
    (InstrumentKey('SSE','000688','index'),'1A0001'),
    (InstrumentKey('SZSE','399001','index'),'1A0001'),
    (KEY,'1A0001'),
    (KEY,{'invalid':'ticker'}),
])
def test_index_alias_never_accepts_wrong_instrument_or_stock(key,ticker):
    body=envelope([{'thscode':fuyao.code(key),'ticker':ticker,'last_price':'12'}])
    batch=fuyao.parse_quotes(body,[key],NOW)
    assert not batch.records and batch.rejected=={fuyao.code(key):'instrument_mismatch'}


@pytest.mark.parametrize('code,reason',[(2001,'credentials_rejected'),(4001,'rate_limited'),(2003,'permission_missing')])
def test_upstream_messages_never_leak_into_errors(code,reason):
    with pytest.raises(SourceFailure) as caught:
        fuyao.unpack(json.dumps({'code':code,'message':'synthetic-credential-should-not-leak','data':{}}).encode())
    assert caught.value.reason==reason and 'synthetic-credential' not in str(caught.value)


def test_daily_is_unadjusted_preserves_shares_and_uses_shanghai_trade_date():
    ms=int(datetime(2026,10,22,16,tzinfo=UTC).timestamp()*1000)
    body=envelope([{'date_ms':ms,'open_price':10,'high_price':12,'low_price':9,'close_price':11,'volume':12345,'turnover':100000}])
    batch=fuyao.parse_reference('fuyao_daily',body,KEY,NOW)
    bar=batch.bars[0][1]
    assert str(bar.trade_day)=='2026-10-23' and bar.bar_end_at is None
    assert bar.volume_shares=='12345' and bar.amount_cny=='100000' and bar.adjustment=='unadjusted'


def test_adjustment_is_original_event_per_share_and_never_invented_cumulative_factor():
    ms=int(datetime(2026,10,20,16,tzinfo=UTC).timestamp()*1000)
    body=envelope([{'ticker':'000001','ex_date_ms':ms,'dividend_per_share':0.5,'per_share_bonus':0.1}],thscode='000001.SZ')
    table=fuyao.parse_reference('fuyao_adj_factor',body,KEY,NOW).records[0][1]
    assert table.dataset=='fuyao.adjustment_events' and table.rows[0]['ex_date']=='2026-10-21'
    assert table.rows[0]['dividend_per_share']=='0.5' and table.rows[0]['allotment_ratio'] is None
    assert 'adjust_factor' not in table.rows[0] and 'ex_factor' not in table.rows[0]


def test_historical_twelve_digit_milliseconds_preserve_old_adjustment_events():
    historical=datetime(1993,5,23,16,tzinfo=UTC)
    stamp=int(historical.timestamp()*1000)
    assert len(str(stamp))==12 and fuyao.epoch_clock(stamp,NOW)==historical
    batch=fuyao.parse_reference('fuyao_adj_factor',envelope([{'ticker':KEY.symbol,'ex_date_ms':stamp,
        'dividend_per_share':0.1}]),KEY,NOW)
    assert batch.rejected==0 and batch.records[0][1].rows[0]['ex_date']=='1993-05-24'
    with pytest.raises(SourceFailure,match='invalid_source_time'):
        fuyao.epoch_clock(int(historical.timestamp()),NOW)


def test_financial_negative_and_null_are_preserved_and_bad_currency_rejected():
    ms=int(datetime(2026,6,29,16,tzinfo=UTC).timestamp()*1000)
    row={'thscode':'000001.SZ','currency':'CNY','period_end_ms':ms,'report_date_ms':ms,'net_profit':-100,'basic_eps':None}
    batch=fuyao.parse_reference('fuyao_financial',envelope([row]),KEY,NOW,statement='income-statements')
    assert batch.records[0][1].rows[0]['net_profit']=='-100'
    assert batch.records[0][1].rows[0]['basic_eps'] is None
    bad=fuyao.parse_reference('fuyao_financial',envelope([{**row,'currency':'USD'}]),KEY,NOW,statement='income-statements')
    assert bad.rejected==1 and not bad.records[0][1].rows


def test_new_source_requires_encrypted_connection_and_no_environment_fallback(client,monkeypatch):
    headers=login(client);configure(client,headers,config())
    monkeypatch.setenv('FUYAO_API_KEY','synthetic-ignored-environment')
    with Session(engine()) as db:
        bound=resolve_source_credentials(db,source_id('fuyao_quotes'),'fuyao_quotes')
        assert bound.required and bound.credential is None and bound.reason=='credentials_missing'


def test_collect_quote_persists_partial_evidence_and_cached_read_uses_same_source(client,monkeypatch):
    setup(client,monkeypatch)
    monkeypatch.setattr(fuyao,'fetch',lambda *args,**kwargs:(Response(snapshot(),datetime.now(UTC),2,200),[(None,None,snapshot())]))
    with Session(engine()) as db,db.begin():
        bound=resolve_source_credentials(db,source_id('fuyao_quotes'),'fuyao_quotes')
        job=enqueue_collection(db,source_id('fuyao_quotes'),integration_probe=job_context(bound,source_id('fuyao_quotes'),probe=True))
        identity=job.id
    leased=claim();assert leased.id==identity;process(leased)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.source_id==source_id('fuyao_quotes')))
        assert run.status=='partial' and run.reason=='quote_time_unknown' and run.accepted==1
        saved=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id('fuyao_quotes')))
        assert saved.payload['quote_time'] is None and saved.historical_eligible is False
        # Explicitly model a read after receipt. Windows host and disposable
        # PostgreSQL clocks can differ by milliseconds; no sleep or production
        # cutoff relaxation is needed for this synthetic evidence test.
        monkeypatch.setattr('app.market.read_models.now',lambda session:saved.received_at+timedelta(seconds=1))
        choose(db)
        result=quote_batch(db,instrument_id=saved.instrument_id).items[0]
        assert result.provenance.family=='fuyao_quotes' and result.reason=='quote_time_unknown'
        assert result.quote.volume_shares=='12345'


def test_fixed_four_indices_probe_saves_every_canonical_index_with_clock_warning(client,monkeypatch):
    setup(client,monkeypatch,'fuyao_indices')
    rows=[{'thscode':code,'ticker':ticker,'last_price':'3000','prev_price':'2990',
        'open_price':'2995','high_price':'3010','low_price':'2990','turnover':123000}
        for code,ticker in [('000001.SH','1A0001'),('399001.SZ','399001'),
            ('399006.SZ','399006'),('000688.SH','1B0688')]]
    body=envelope(rows,timestamp=int(NOW.timestamp()*1000))
    monkeypatch.setattr(fuyao,'fetch',lambda *a,**k:(Response(body,datetime.now(UTC),2,200),[(None,None,body)]))
    with Session(engine()) as db,db.begin():
        bound=resolve_source_credentials(db,source_id('fuyao_indices'),'fuyao_indices')
        queued=enqueue_collection(db,source_id('fuyao_indices'),
            integration_probe=job_context(bound,source_id('fuyao_indices'),probe=True))
        identity=queued.id
    job=claim();assert job.id==identity;process(job)
    with Session(engine()) as db:
        run=db.scalar(select(DataFetchRun).where(DataFetchRun.job_id==identity))
        assert run.status=='partial' and run.reason=='quote_time_unknown'
        assert run.expected==run.accepted==4 and run.rejected==run.duplicates==0
        saved=list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==run.source_id)))
        assert len(saved)==4 and all(row.payload['quote_time'] is None and not row.historical_eligible for row in saved)
        from app.integrations.data import connection_test
        proof=connection_test(db,{'revision':bound.revision,'source_ids':[str(run.source_id)],'last_test':{
            'revision':bound.revision,'job_id':str(identity),'source_id':str(run.source_id),'status':'queued'}})
        assert proof['status']=='success' and '成交时刻未知' in proof['message']


def test_unknown_free_quote_never_overrides_qualified_paid_quote(client,monkeypatch):
    headers=setup(client,monkeypatch)
    paid=configuration('tushare_rt_k');paid['symbols']=config()['symbols'];paid['interval_seconds']=60
    configure(client,headers,paid,key='synthetic-paid-comparison')
    with Session(engine()) as db,db.begin():
        from app.evidence.common import now
        clock=now(db)
        choose(db)
        # Same source protocol and immutable persistence on both providers.
        known=fuyao.parse_quotes(snapshot(clock=clock,price='12.4'),[KEY],clock)
        unknown=fuyao.parse_quotes(snapshot(price='12.3'),[KEY],clock)
        for family,batch in [('tushare_rt_k',known),('fuyao_quotes',unknown)]:
            provider=db.get(Provider,source_id(family));cfg=db.get(SourceSettings,provider.id)
            store_quote_batch(db,provider,cfg,batch)
        instrument=db.scalar(select(Instrument).where(Instrument.exchange==KEY.exchange,Instrument.symbol==KEY.symbol,Instrument.asset_type=='stock'))
        result=quote_batch(db,instrument_id=instrument.id).items[0]
        assert result.provenance.family=='tushare_rt_k' and result.quote.price=='12.4'


def test_fuyao_daily_persists_into_existing_chart_contract(client,monkeypatch):
    setup(client,monkeypatch,'fuyao_daily')
    with Session(engine()) as db,db.begin():
        from app.evidence.common import now
        clock=now(db);ms=int((clock-timedelta(days=2)).timestamp()*1000)
        body=envelope([{'date_ms':ms,'open_price':10,'high_price':12,'low_price':9,'close_price':11,'volume':12345,'turnover':100000}])
        provider=db.get(Provider,source_id('fuyao_daily'));cfg=db.get(SourceSettings,provider.id)
        store_reference_batch(db,provider,cfg,fuyao.parse_reference('fuyao_daily',body,KEY,clock),clock)
        saved=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id))
        from app.market.datasets import bars
        choose(db,'stock_daily')
        view=bars(db,saved.instrument_id,'unadjusted')
        assert view.source_family=='fuyao_daily' and len(view.items)==1 and view.items[0].payload.close=='11'


@pytest.mark.parametrize('boundary',['range','before'])
def test_daily_route_uses_source_covering_requested_dates_and_explicit_source_stays_strict(client,monkeypatch,boundary):
    headers=setup(client,monkeypatch,'fuyao_daily')
    fallback=configuration('baostock_history');fallback.update(symbols=config('fuyao_daily')['symbols'],interval_seconds=21600)
    configure(client,headers,fallback,key='synthetic-daily-fallback')
    from app.evidence.common import now
    from app.market.datasets import bars
    from app.market.protocol import SHANGHAI
    with Session(engine()) as db,db.begin():
        clock=now(db);day=clock.astimezone(SHANGHAI).date()-timedelta(days=20)
        choose(db,'stock_daily')
        for family,source_day in [('fuyao_daily',day+timedelta(days=18)),('baostock_history',day)]:
            stamp=int(datetime.combine(source_day,datetime.min.time(),SHANGHAI).timestamp()*1000)
            body=envelope([{'date_ms':stamp,'open_price':10,'high_price':12,'low_price':9,'close_price':11,'volume':123,'turnover':1500}])
            provider=db.get(Provider,source_id(family));cfg=db.get(SourceSettings,provider.id)
            store_reference_batch(db,provider,cfg,fuyao.parse_reference('fuyao_daily',body,KEY,clock),clock)
        saved=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id('fuyao_daily')))
        bounds={'start_day':day-timedelta(days=1),'end_day':day+timedelta(days=1)} if boundary=='range' else {'before':day+timedelta(days=1)}
        view=bars(db,saved.instrument_id,'unadjusted',**bounds)
        assert view.source_family=='baostock_history' and len(view.items)==1
        assert view.items[0].payload.trade_day==day
        explicit=bars(db,saved.instrument_id,'unadjusted',source_id=source_id('fuyao_daily'),**bounds)
        assert explicit.source_family=='fuyao_daily' and not explicit.items


@pytest.mark.parametrize('market_clock',[True,False])
def test_terminal_consumes_routed_quote_without_inventing_trade_day_or_market_breadth(client,monkeypatch,market_clock):
    setup(client,monkeypatch)
    from app.evidence.common import now
    from app.integrations.routing_models import DataRoutes
    from app.market.terminal_read import workbench_view
    from app.models import User
    with Session(engine()) as db,db.begin():
        clock=now(db)
        provider=db.get(Provider,source_id('fuyao_quotes'));cfg=db.get(SourceSettings,provider.id)
        batch=fuyao.parse_quotes(snapshot(clock=clock if market_clock else None),[KEY],clock)
        store_quote_batch(db,provider,cfg,batch)
        db.add(DataRoutes(id=1,revision=1,choices={'stock_quote':{'provider_id':'fuyao'}},updated_at=clock));db.flush()
        owner=db.scalar(select(User))
        view=workbench_view(db,owner.id)
        assert view.breadth.up is None and view.breadth.limit_up is None
        assert view.coverage.quote_count==(1 if market_clock else 0)
        if market_clock:
            assert len(view.stocks)==1 and view.stocks[0].source_family=='fuyao_quotes'
            assert view.stocks[0].price==12.3 and view.stocks[0].source_time is not None
            assert view.capabilities[0].status=='partial'
        else:
            assert not view.stocks
