"""Synthetic coverage/counts; isolated PostgreSQL, no external requests."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import SourceVersion
from app.market.collect import store_reference_batch, store_catalog_batch
from app.market.datasets import breadth_history
from app.market.models import SourceSettings
from app.market.protocol import InstrumentKey, CatalogBatch, CatalogRecord, SourceFailure, SHANGHAI
from app.market.read_models import quote_batch
from app.market.service import source_id
from app.market.tushare import parameters
from app.market.tushare_market import (MARKET, LIMITS, QUOTE_FIELDS, parse_market, encoded,
    compare_limits, quote_from_snapshot, breadth_series, comparison_memo)
from app.models import Instrument, Provider
from test_data_sources import configure, configuration, login

AT=datetime(2026,10,23,2,30,tzinfo=UTC)


def quote(code='603278.SH',price='9',previous='10',at=AT):
    return dict(ts_code=code,name='合成股票',close=price,pre_close=previous,open=previous,
        high=str(max(float(price),float(previous))),low=str(min(float(price),float(previous))),
        vol='100',amount='1000',trade_time=at.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S'))


def limits(code='603278.SH',upper='11',lower='9'):
    return dict(ts_code=code,trade_date='20261023',up_limit=upper,down_limit=lower,pre_close='10')


def raw(rows):return encoded(list(rows[0]),rows)


def setup(client):
    headers=login(client)
    for family in (MARKET,LIMITS,'tushare_rt_indices'):
        body=configuration(family);body['interval_seconds']=21600 if family==LIMITS else 60
        configure(client,headers,body,key=family)
    with Session(engine()) as db,db.begin():
        for row in db.scalars(select(SourceVersion).where(SourceVersion.source_id.in_([source_id(f) for f in (MARKET,LIMITS,'tushare_rt_indices')]))):
            db.add(SourceVersion(source_id=row.source_id,recorded_at=AT-timedelta(days=1),
                descriptor=row.descriptor,content_hash=row.content_hash))
    return headers


def store(db,family,rows,at):
    provider=db.get(Provider,source_id(family));config=db.get(SourceSettings,provider.id)
    return store_reference_batch(db,provider,config,parse_market(raw(rows),at,limits=family==LIMITS),at)


def test_fully_empty_market_snapshot_is_unavailable_and_empty_limits_stay_unpublished():
    from test_tushare import body
    for limits,reason in [(False,'instrument_unavailable'),(True,'daily_data_not_published')]:
        with pytest.raises(SourceFailure,match=reason):parse_market(body([],[]),AT,limits=limits)
    for data in [None,{}, {'fields':None,'items':[]},{'fields':[],'items':None},{'fields':[],'items':[['unexpected']]}]:
        with pytest.raises(SourceFailure,match='schema_changed'):
            parse_market(json.dumps({'code':0,'data':data}).encode(),AT)


def test_scope_parameters_never_issue_thousands_of_single_requests():
    assert parameters(MARKET,[],AT)=={'ts_code':'6*.SH,0*.SZ,3*.SZ,9*.BJ','limit':6000}
    assert parameters('tushare_rt_indices',[],AT)=={'ts_code':'000001.SH,399001.SZ,399006.SZ,000688.SH','limit':4}
    assert parameters(LIMITS,[],AT)=={'trade_date':'20261023','limit':8000}


@pytest.mark.parametrize('code,upper,lower,price',[
    ('603278.SH','11','9','9'),('000001.SZ','10.5','9.5','9.5'),
    ('300001.SZ','12','8','8'),('688001.SH','12','8','8'),('920001.BJ','13','7','7')])
def test_exact_limits_support_st_twenty_and_thirty_percent(code,upper,lower,price):
    result=compare_limits([quote(code,price)],[limits(code,upper,lower)],AT)
    assert result['limit_down']==1 and result['limit_up']==0 and result['comparable']==1
    assert result['unknown']==0 and result['fresh']==1


def test_nine_percent_is_not_down_limit_and_touch_is_not_current_seal():
    q=quote(price='9.1');q['low']='9'
    result=compare_limits([q],[limits()],AT)
    assert result['limit_down']==0 and result['comparable']==1


def test_binary_float_tail_is_canonicalized_only_in_projection_not_raw_evidence():
    row=quote(price='9.000000000000002')
    snapshot=parse_market(raw([row]),AT).records[0][1].model_dump(mode='json')
    assert snapshot['rows'][0]['close']=='9.000000000000002'
    value=quote_from_snapshot(snapshot,InstrumentKey('SSE','603278','stock'),AT)
    assert str(value.price)=='9.00'
    assert compare_limits([row],[limits()],AT)['limit_down']==1
    assert compare_limits([quote(price='9.0001')],[limits()],AT)['limit_down']==0
    with pytest.raises(SourceFailure,match='invalid_number'):
        parse_market(raw([quote(price='9.000000012345678')]),AT)


def test_old_day_missing_clock_missing_or_zero_limit_remain_unknown():
    quotes=[quote('603278.SH',at=AT-timedelta(days=1)),quote('000001.SZ'),quote('300001.SZ')]
    result=compare_limits(quotes,[limits(),limits('000001.SZ',upper='0',lower='0')],AT)
    assert result['old_day']==1 and result['unknown']==3 and result['comparable']==0
    assert result['advancing']+result['declining']+result['unchanged']==2


def test_duplicate_rows_and_ambiguous_cap_fail_whole_batch():
    with pytest.raises(SourceFailure,match='duplicate_instrument'):parse_market(raw([quote(),quote()]),AT)
    with pytest.raises(SourceFailure,match='market_scope_truncated'):parse_market(raw([quote()]*6000),AT)


@pytest.mark.parametrize('mutation,reason',[
    ({'ts_code':'000001.SH'},'unexpected_instrument'),
    ({'trade_time':'2026-10-24 10:00:00'},'future_source_time'),
    ({'close':'garbage'},'invalid_number'),({'high':'1'},'inconsistent_price_range')])
def test_malformed_or_wrong_asset_rows_cannot_silently_change_counts(mutation,reason):
    row={**quote(),**mutation}
    with pytest.raises(SourceFailure,match=reason):parse_market(raw([row]),AT)


def test_market_snapshot_serves_any_catalogued_stock_but_not_index():
    payload=parse_market(raw([quote()]),AT).records[0][1].model_dump(mode='json')
    value=quote_from_snapshot(payload,InstrumentKey('SSE','603278','stock'),AT)
    assert value.instrument.symbol=='603278' and value.quote_time==AT
    assert quote_from_snapshot(payload,InstrumentKey('SSE','000001','index'),AT) is None


def test_real_postgres_paid_counts_and_arbitrary_catalogued_quote(client):
    setup(client)
    with Session(engine()) as db,db.begin():
        store(db,LIMITS,[limits()],AT-timedelta(seconds=1))
        store(db,MARKET,[quote()],AT)
        stock=Instrument(exchange='SSE',symbol='603278',asset_type='stock',name='合成股票',first_received_at=AT)
        db.add(stock);db.flush();ident=stock.id
        result=breadth_series(db,AT.astimezone(SHANGHAI).date(),AT+timedelta(seconds=1))
        assert result.latest.limit_down==1 and result.latest.limit_up==0
        assert result.latest.limit_evidence_id is not None and result.source_family==MARKET
        quote_result=quote_batch(db,instrument_id=ident).items[0]
        assert quote_result.quote.price=='9' and quote_result.provenance.family==MARKET
        assert not quote_result.provenance.historical_eligible
        db.get(Provider,source_id(MARKET)).rights={**db.get(Provider,source_id(MARKET)).rights,'display':'denied'}
        db.flush()
        assert breadth_series(db,AT.date(),AT+timedelta(seconds=1)) is None
        assert quote_batch(db,instrument_id=ident).items[0].quote is None


def test_no_future_limit_table_backfill_or_previous_day_reuse(client):
    setup(client)
    with Session(engine()) as db,db.begin():
        store(db,MARKET,[quote()],AT)
        store(db,LIMITS,[limits()],AT+timedelta(seconds=1))
        result=breadth_series(db,AT.date(),AT+timedelta(seconds=2))
        assert result.latest.limit_down is None and result.latest.unknown==1
        store(db,MARKET,[quote(at=AT+timedelta(minutes=1))],AT+timedelta(minutes=1))
        result=breadth_series(db,AT.date(),AT+timedelta(minutes=2))
        assert result.items[0].limit_down is None and result.items[1].limit_down==1
        db.get(Provider,source_id(LIMITS)).enabled=False;db.flush()
        result=breadth_series(db,AT.date(),AT+timedelta(minutes=2))
        assert all(p.limit_down is None for p in result.items)


def test_explicit_sources_restore_old_morning_history_without_stitching_or_mutation(client):
    from test_breadth_history import save
    headers=setup(client)
    configure(client,headers,configuration('ashare_breadth'),key='legacy-breadth')
    save('2026-10-23T09:30:00',up='2',down='1')
    save('2026-10-23T10:20:00',up='3',down='0')
    with Session(engine()) as db,db.begin():
        store(db,MARKET,[quote()],AT)
        # A later limits table cannot fill the earlier paid point.
        store(db,LIMITS,[limits()],AT+timedelta(seconds=1))
    url='/api/v1/market-breadth-history?day=2026-10-23'
    auto=client.get(url).json()['data'];assert auto['source_family']==MARKET and len(auto['items'])==1
    assert auto['items'][0]['limit_down'] is None
    old=client.get(url+'&source=ashare_breadth').json()['data']
    assert old['source_family']=='ashare_breadth' and [p['limit_down'] for p in old['items']]==[1,0]
    assert not set(p['id'] for p in old['items'])&set(p['id'] for p in auto['items'])
    paid=client.get(url+'&source=tushare_market_rt').json()['data'];assert paid==auto
    with Session(engine()) as db,db.begin():db.get(Provider,source_id(MARKET)).enabled=False
    denied=client.get(url+'&source=tushare_market_rt').json()['data']
    assert denied['items']==[] and denied['source_family']==MARKET
    assert client.get(url+'&source=ashare_breadth').json()['data']==old
    with Session(engine()) as db,db.begin():db.get(Provider,source_id('ashare_breadth')).enabled=False
    assert client.get(url+'&source=ashare_breadth').json()['data']['items']==[]
    assert client.get(url+'&source=unapproved').status_code==422


def test_unchanged_market_payload_retains_new_observation_and_latest_mode(client):
    setup(client)
    with Session(engine()) as db,db.begin():
        store(db,LIMITS,[limits()],AT-timedelta(seconds=1))
        assert store(db,MARKET,[quote()],AT)==(1,0)
        assert store(db,MARKET,[quote()],AT+timedelta(minutes=1))==(1,0)
        assert store(db,MARKET,[quote()],AT+timedelta(minutes=1))==(0,1)
        result=breadth_series(db,AT.date(),AT+timedelta(minutes=2),latest_only=True)
        assert len(result.items)==1 and result.latest.received_at==AT+timedelta(minutes=1)


def test_cached_comparisons_do_not_reprocess_all_stock_rows_or_bypass_revocation(client,monkeypatch):
    setup(client)
    with Session(engine()) as db,db.begin():
        store(db,LIMITS,[limits()],AT-timedelta(seconds=1))
        store(db,MARKET,[quote()],AT)
        first=breadth_series(db,AT.date(),AT+timedelta(seconds=1))
        calls=[]
        from app.market.tushare_market import compare_limits as original
        def counted(*args,**kwargs):
            calls.append(True);return original(*args,**kwargs)
        monkeypatch.setattr('app.market.tushare_market.compare_limits',counted)
        second=breadth_series(db,AT.date(),AT+timedelta(seconds=1))
        assert second==first and not calls
        db.get(Provider,source_id(LIMITS)).enabled=False;db.flush()
        revoked=breadth_series(db,AT.date(),AT+timedelta(seconds=1))
        assert revoked.latest.limit_down is None and len(calls)==1
        db.get(Provider,source_id(MARKET)).enabled=False;db.flush()
        assert breadth_series(db,AT.date(),AT+timedelta(seconds=1)) is None


def test_request_memo_reuses_parsing_without_changing_mixed_market_arithmetic():
    from copy import deepcopy
    quotes=[quote(price='9.000000000000002'),quote('000001.SZ',price='10.5'),
            quote('300001.SZ',price='9.0001'),quote('688001.SH',at=AT-timedelta(days=1)),
            quote('920001.BJ',price='7'),quote('603279.SH',price='10')]
    bounds=[limits(),limits('000001.SZ',upper='10.5',lower='9.5'),
            limits('300001.SZ'),limits('688001.SH'),limits('920001.BJ',upper='13',lower='7')]
    originals=deepcopy((quotes,bounds));memo=comparison_memo()
    first=compare_limits(quotes,bounds,AT,memo=memo,limits_key=('version-a','hash-a'))
    assert {key:first[key] for key in ('limit_up','limit_down','advancing','declining','unchanged',
        'returned','comparable','unknown','old_day','fresh')}==dict(limit_up=1,limit_down=2,
        advancing=1,declining=3,unchanged=1,returned=6,comparable=4,unknown=2,old_day=1,fresh=4)
    misses=memo['prices'].cache_info().misses
    assert compare_limits(quotes,bounds,AT+timedelta(seconds=1),memo=memo,
        limits_key=('version-a','hash-a'))==first
    assert memo['prices'].cache_info().misses==misses
    # A shared request memo must agree with independent reads for every batch,
    # including changed prices, old-day clocks, zero limits and missing limits.
    for offset,prices in [(1,quotes),(2,[quote(price='9.1'),quote('000001.SZ',price='10.5')]),
                          (3,[quote('300001.SZ',at=AT-timedelta(days=1)),quote('603279.SH')])]:
        received=AT+timedelta(seconds=offset)
        assert compare_limits(prices,bounds,received,memo=memo,limits_key=('version-a','hash-a'))==compare_limits(prices,bounds,received)
    assert memo['prices'].cache_info().hits>0
    assert memo['prices'].cache_info().misses>=misses
    assert (quotes,bounds)==originals


def test_memo_clock_cached_at_later_receipt_cannot_relax_earlier_future_guard():
    memo=comparison_memo();row=quote(at=AT+timedelta(seconds=61))
    allowed=compare_limits([row],[limits()],AT+timedelta(seconds=1),memo=memo)
    assert allowed['limit_down']==1  # Exact documented +60-second boundary.
    with pytest.raises(SourceFailure,match='future_source_time'):
        compare_limits([row],[limits()],AT,memo=memo)
    # A rejected early observation must not poison a subsequent valid one.
    assert compare_limits([row],[limits()],AT+timedelta(seconds=61),memo=memo)['fresh']==1


def test_memo_identical_counts_still_use_each_receipts_freshness_boundary():
    memo=comparison_memo();row=quote();bounds=[limits()]
    boundary=compare_limits([row],bounds,AT+timedelta(seconds=120),memo=memo,limits_key=('v','h'))
    older=compare_limits([row],bounds,AT+timedelta(seconds=121),memo=memo,limits_key=('v','h'))
    assert boundary['fresh']==1 and older['fresh']==0
    assert {k:v for k,v in boundary.items() if k!='fresh'}=={k:v for k,v in older.items() if k!='fresh'}


def test_memo_limit_identity_hash_and_observation_day_are_independent():
    memo=comparison_memo();row=quote();original=[limits()];corrected=[limits(lower='8')]
    assert compare_limits([row],original,AT,memo=memo,limits_key=('v1','h1'))['limit_down']==1
    assert compare_limits([row],corrected,AT,memo=memo,limits_key=('v2','h2'))['limit_down']==0
    # Even an identity collision must not reuse arithmetic under another hash.
    assert compare_limits([row],corrected,AT,memo=memo,limits_key=('v1','h3'))['limit_down']==0
    assert compare_limits([row],original,AT,memo=memo,limits_key=('v1','h1'))['limit_down']==1
    tomorrow=AT+timedelta(days=1)
    unknown=compare_limits([quote(at=tomorrow)],original,tomorrow,memo=memo,limits_key=('v1','h1'))
    assert unknown['comparable']==0 and unknown['unknown']==1 and unknown['limit_down']==0
    # No identity supplied: independent caller-owned tables cannot share an index.
    assert compare_limits([row],original,AT,memo=memo)['limit_down']==1
    assert compare_limits([row],corrected,AT,memo=memo)['limit_down']==0


@pytest.mark.parametrize('name',['prices','keys','clocks','clock_facts','days'])
def test_request_scalar_memos_evict_at_finite_capacity_and_are_not_global(name):
    memo=comparison_memo();cache=memo[name];maximum=cache.cache_info().maxsize
    assert isinstance(maximum,int) and 0<maximum<=65536
    def arguments(index):
        if name=='prices':return (str(index+1),)
        if name=='keys':return (f'{600000+index:06}.SH',)
        if name in {'clocks','clock_facts'}:
            stamp=AT+timedelta(seconds=index)
            return (stamp.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S'),stamp)
        return ((AT+timedelta(days=index)).strftime('%Y%m%d'),)
    for index in range(maximum+1):cache(*arguments(index))
    info=cache.cache_info()
    assert info.currsize==maximum and info.misses==maximum+1
    cache(*arguments(0))
    assert cache.cache_info().misses==info.misses+1  # Earliest entry was evicted.
    separate=comparison_memo()
    assert separate[name].cache_info().currsize==0 and not separate['limits'] and not separate['limit_prices']


def test_unused_bad_limit_prices_stay_lazy_and_fail_when_a_valid_quote_uses_them():
    bounds=[limits(),limits('000001.SZ',lower='malformed')]
    memo=comparison_memo()
    assert compare_limits([quote()],bounds,AT,memo=memo,limits_key=('v','h'))['limit_down']==1
    # A missing price still has unknown breadth; its unused limit cannot turn
    # an otherwise displayable historical observation into an exception.
    assert compare_limits([quote('000001.SZ',price='0')],bounds,AT,memo=memo,
        limits_key=('v','h'))['unknown']==1
    with pytest.raises(SourceFailure,match='invalid_number'):
        compare_limits([quote('000001.SZ')],bounds,AT,memo=memo,limits_key=('v','h'))
    corrected=[limits(),limits('000001.SZ')]
    assert compare_limits([quote('000001.SZ')],corrected,AT,memo=memo,
        limits_key=('v','new-hash'))['limit_down']==1


def test_parsed_limit_prices_share_the_raw_version_eviction_boundary():
    memo=comparison_memo();original=memo['prices'];calls=[]
    def counted(value):
        calls.append(value);return original(value)
    memo['prices']=counted
    row=quote(price='10.00',previous='11.00');bounds=[limits(upper='12.00',lower='8.00')]
    first=compare_limits([row],bounds,AT,memo=memo,limits_key=('v','h'))
    assert compare_limits([row],bounds,AT+timedelta(seconds=1),memo=memo,
        limits_key=('v','h'))==first
    assert calls.count('12.00')==calls.count('8.00')==1
    for index in range(100):
        compare_limits([],bounds,AT,memo=memo,limits_key=(str(index),'h'))
    assert len(memo['limits'])==len(memo['limit_prices'])==100
    assert set(memo['limits'])==set(memo['limit_prices'])
    assert (('v','h'),AT.date()) not in memo['limit_prices']
    assert compare_limits([row],bounds,AT,memo=memo,limits_key=('v','h'))==first
    assert calls.count('12.00')==calls.count('8.00')==2


@pytest.mark.parametrize('empty',[False,True])
def test_compact_saved_comparison_preserves_all_statistics_order_and_raw_rows(client,empty):
    from app.evidence.models import EvidenceVersion
    from app.evidence.schemas import ReferencePayload
    from app.market.reference import ReferenceBatch
    from app.market.tushare_market import comparison_for,SNAPSHOT
    setup(client)
    rows=[] if empty else [dict(quote(price='11'),extra='keep'),
        dict(quote(price='9'),amount='malformed',extra='keep')]
    sample=rows[0] if rows else quote()
    payload=ReferencePayload(dataset=SNAPSHOT,title='Synthetic ordered comparison',
        source_url='https://example.invalid/synthetic',attribution='Synthetic fixture',
        scope='Read calculation only; not a collector acceptance fixture.',observed_day=AT.date(),
        columns=[dict(key=key,label=key) for key in sample],rows=rows)
    with Session(engine()) as db,db.begin():
        store(db,LIMITS,[limits()],AT-timedelta(seconds=1))
        provider=db.get(Provider,source_id(MARKET))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),
            ReferenceBatch(records=[('ordered-comparison',payload,None)],expected=1),AT)
        db.flush()
        snapshot=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id,
            EvidenceVersion.source_key=='reference:ordered-comparison'))
        available=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id(LIMITS)))
        assert comparison_for(db,snapshot,available)==compare_limits(rows,[limits()],AT)
        if not empty:
            assert comparison_for(db,snapshot,available)['total_amount_cny']=='1000'
        db.refresh(snapshot)
        assert snapshot.payload['rows']==rows


def test_compact_comparison_requires_the_selected_hash_and_receipt(client):
    from app.evidence.models import EvidenceVersion
    from app.market.tushare_market import comparison_for
    from sqlalchemy.orm.exc import NoResultFound
    setup(client)
    with Session(engine()) as db,db.begin():
        store(db,MARKET,[quote()],AT)
        snapshot=db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==source_id(MARKET)))
        for hash_value,stamp in [('0'*64,AT),(snapshot.content_hash,AT+timedelta(seconds=1))]:
            selected=SimpleNamespace(id=snapshot.id,content_hash=hash_value,received_at=stamp)
            with pytest.raises(NoResultFound):comparison_for(db,selected,None)
