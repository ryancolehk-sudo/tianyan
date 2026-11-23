"""Synthetic contracts for atomic pagination, never a live-source acceptance."""
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.market.kpl_atlas_source import Pagination
from app.market.protocol import Response, SourceFailure
from app.market.sector_atlas import kpl_panel, ths_panel
from app.market.tushare import parse_reference, parameters

STAMP=datetime(2026,11,4,0,tzinfo=UTC)
DAY=date(2026,11,2)


def leader_response(labels, total=None):
    raw=[]
    for i,label in enumerate(labels):
        row=[0]*70
        row[0]=str(600000+i);row[1]='合成领涨'+str(i);row[24]=label
        raw.append(row)
    return Response(json.dumps(dict(errcode='0',Day=[str(DAY)],Count=len(raw) if total is None else total,list=raw)).encode(),STAMP,1,200)


def test_leader_ranks_are_only_source_labels():
    from app.market.kpl_leaders import parse
    payload=parse(leader_response(['','龙三','破板','龙一']),'801004',DAY,daily=True)
    assert [(r['label'],r['stock_code']) for r in payload.rows]==[('龙一','600003'),('龙三','600001')]
    assert payload.rows[0]['coverage']=='complete'
    assert payload.rows[0]['archive_kind']=='daily'
    partial=parse(leader_response([''],total=700),'801004',DAY)
    assert partial.rows[0]['coverage']=='returned_markers' and partial.rows[0]['stock_code'] is None
    full=parse(leader_response(['龙一','龙二','龙三','龙四','龙五'],total=700),'801004',DAY)
    assert full.rows[0]['coverage']=='complete'


def test_leader_missing_day_and_duplicate_labels_rejected():
    from app.market.kpl_leaders import parse
    with pytest.raises(SourceFailure,match='source_date_mismatch'):
        parse(leader_response(['龙一']),'801004',date(2026,10,30))
    with pytest.raises(SourceFailure,match='schema_changed'):
        parse(leader_response(['龙一','龙一']),'801004',DAY)

@pytest.fixture(autouse=True)
def isolated_database():
    yield


def page(start=0, count=30, total=31, day=DAY):
    rows=[[str(801000+i),'题材'+str(i),100-i,1,0,100000000*(i+1),*([0]*13)]
          for i in range(start,start+count)]
    return Response(json.dumps(dict(errcode='0',Day=[str(day)],Count=total,list=rows)).encode(),STAMP,1,200)


def test_current_count_and_historical_page_count_are_different():
    p=Pagination();p.add(page())
    with pytest.raises(SourceFailure,match='market_scope_truncated'):p.batch()
    p.add(page(30,1))
    batch=p.batch();assert len(batch.records[0][1].rows)==31
    assert len(batch.records[1][1].rows)==10
    assert batch.records[0][1].rows[30]['amount']=='3100000000'
    assert batch.records[0][1].rows[0]['archive_kind']=='intraday'
    h=Pagination(DAY);h.add(page(total=30));h.add(page(30,1,total=1))
    assert h.finished and h.total==31
    assert h.batch().records[0][1].rows[0]['archive_kind']=='daily'


@pytest.mark.parametrize('second,reason',[(page(0,1),'duplicate_instrument'),
    (page(30,1,total=32),'kpl_universe_changed'),
    (page(30,0,total=31),'market_scope_truncated'),
    (page(30,1,day=date(2026,10,30)),'source_date_mismatch')])
def test_failed_page_never_yields_a_full_batch(second,reason):
    p=Pagination();p.add(page())
    with pytest.raises(SourceFailure,match=reason):p.add(second)
    with pytest.raises(SourceFailure):p.batch()


def test_historical_exact_multiple_needs_empty_terminal_page():
    p=Pagination(DAY);p.add(page(total=30));assert not p.finished
    p.add(page(30,0,total=0));assert p.finished and p.total==30


def test_full_details_survive_outside_top_ten():
    p=Pagination();p.add(page());p.add(page(30,1))
    saved=SimpleNamespace(id='synthetic',received_at=STAMP,payload=p.batch().records[0][1].model_dump(mode='json'))
    result=kpl_panel(saved,DAY,{'801030'})
    assert len(result['rows'])==10 and result['total_count']==31
    assert result['details'][0]['rank']==31
    assert result['details'][0]['strength']=='70'


def test_ths_uses_code_join_and_excludes_future_and_other_catalog_types():
    directory=SimpleNamespace(id='dir',received_at=STAMP,payload={'rows':[
        dict(ts_code='885001.TI',name='同名',type='N',exchange='A',list_date='20200101'),
        dict(ts_code='885002.TI',name='同名',type='N',exchange='A',list_date='20200101'),
        dict(ts_code='885003.TI',name='未来板块',type='N',exchange='A',list_date='20261103'),
        dict(ts_code='885004.TI',name='行业',type='I',exchange='A')]})
    saved=SimpleNamespace(id='daily',received_at=STAMP,payload={'rows':[
        dict(ts_code='885001.TI',trade_date='20261102',pct_change='0'),
        dict(ts_code='885002.TI',trade_date='20261102',pct_change='1.1'),
        dict(ts_code='885003.TI',trade_date='20261102',pct_change='99'),
        dict(ts_code='885004.TI',trade_date='20261102',pct_change='88')]})
    value=ths_panel(saved,directory,DAY,set())
    assert [r['code'] for r in value['rows']]==['885002.TI','885001.TI']
    assert value['rows'][1]['pct_change']=='0'
    assert 'amount' not in value['rows'][0]


def ths_body(rows):
    return json.dumps({'code':0,'data':{'fields':['ts_code','trade_date','pct_change','vol'], 'items':rows}}).encode()


def test_ths_daily_contract_and_units():
    batch=parse_reference(ths_body([['885001.TI','20261102',0,123]]),'tushare_ths_daily',STAMP)
    payload=batch.records[0][1]
    assert payload.dataset=='tushare.ths_daily'
    assert {c.key:c.unit for c in payload.columns}['vol']=='手'
    assert parameters('tushare_ths_daily',[],STAMP)['limit']==3000


@pytest.mark.parametrize('rows,reason',[
    ([], 'daily_data_not_published'),
    ([['600001.SH','20261102',1,123]],'instrument_mismatch'),
    ([['885001.TI','20261102',1,123]]*2,'duplicate_instrument'),
    ([['885001.TI','20271102',1,123]],'future_source_time'),
    ([['885001.TI','20261102',1,123]]*3000,'market_scope_truncated')])
def test_ths_rejects_unusable_tables(rows,reason):
    with pytest.raises(SourceFailure,match=reason):parse_reference(ths_body(rows),'tushare_ths_daily',STAMP)


def test_ths_raw_reference_preserves_verified_index_suffixes_without_catalog_aliases():
    batch=parse_reference(ths_body([['700013R.TI','20261102',0,123],['700050B.TI','20261102',0,123]]),'tushare_ths_daily',STAMP)
    assert [r['ts_code'] for r in batch.records[0][1].rows]==['700013R.TI','700050B.TI']


def test_live_reordering_is_sorted_locally_but_history_stays_strict():
    response=page(count=3,total=3)
    data=json.loads(response.body)
    data['list'][1][2]=200
    response=Response(json.dumps(data).encode(),STAMP,1,200)
    live=Pagination(live=True);live.add(response)
    rows=live.batch().records[0][1].rows
    assert [r['strength'] for r in rows]==['200','100','98']
    assert [r['rank'] for r in rows]==['1','2','3']
    assert rows[0]['coverage']=='complete_pages'
    with pytest.raises(SourceFailure,match='schema_changed'):
        Pagination(DAY).add(response)


def test_live_overlap_uses_physical_offset_and_exposes_incomplete_coverage():
    live=Pagination(live=True);live.add(page())
    second=page(start=0,count=1,total=31)
    data=json.loads(second.body);data['list'][0][2]=999
    live.add(Response(json.dumps(data).encode(),STAMP,1,200))
    assert live.offset==31 and live.finished
    result=live.batch();rows=result.records[0][1].rows
    assert len(rows)==30 and result.rejected==1
    assert rows[0]['strength']=='999'
    assert rows[0]['coverage']=='partial_pages'
    assert rows[0]['total_count']=='31' and rows[0]['sampled_count']=='30'
    assert len({r['code'] for r in rows})==len(rows)


def test_live_still_rejects_bad_identity_shape_and_mixed_days():
    for change in ('identity','shape','duplicate'):
        data=json.loads(page(count=3,total=3).body)
        if change=='identity':data['list'][0][0]='bad'
        elif change=='shape':data['list'][0].pop()
        else:data['list'][1][0]=data['list'][0][0]
        with pytest.raises(SourceFailure):
            Pagination(live=True).add(Response(json.dumps(data).encode(),STAMP,1,200))
    live=Pagination(live=True);live.add(page())
    with pytest.raises(SourceFailure,match='source_date_mismatch'):
        live.add(page(30,1,day=date(2026,10,30)))
