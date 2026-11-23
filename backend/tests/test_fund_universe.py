"""Synthetic whole-market page sets; real HTTP is forbidden in every case."""
from datetime import datetime, timedelta
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from app.market import eastmoney_trading as m
from app.market import fund_universe as u
from app.market.protocol import Response, SourceFailure

NOW=datetime.fromisoformat('2026-10-23T10:00:00+08:00')


@pytest.fixture
def upstream(monkeypatch):
    import socket
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:pytest.fail('supplier network forbidden'))
    state={'calls':[],'fail':None,'duplicate':False,'changed_total':False,'future':False,'huge':False,'at':NOW,'totals':{},'name_size':0,'ignore_sort':False}
    class Fixed(datetime):
        @classmethod
        def now(cls,tz=None):return (state['at']+timedelta(seconds=10)).astimezone(tz)
    monkeypatch.setattr(u,'datetime',Fixed)
    monkeypatch.setattr(m,'datetime',Fixed)
    def fetch(url):
        query=parse_qs(urlsplit(url).query)
        category='industry' if query['fs']==['m:90+s:4'] else 'concept' if query['fs']==['m:90+t:3'] else 'stock'
        page=int(query['pn'][0]);state['calls'].append((category,page))
        if state['fail']==(category,page):raise SourceFailure('upstream_unavailable')
        total=state['totals'].get(category,{'stock':5927,'industry':128,'concept':531}[category])
        rows=[]
        for position in range((page-1)*100,min(page*100,total)):
            n=total-1-position if query.get('fid')==['f12'] and not state['ignore_sort'] else position
            code=str(600000+n) if category=='stock' else 'BK'+str((1000 if category=='industry' else 2000)+n)
            if state['duplicate'] and category=='concept' and page==2 and position==100:code='BK'+str(2000+total-1)
            stamp=state['at']+timedelta(seconds=2) if state['future'] else state['at']
            rows.append(dict(f12=code,f13=1 if category=='stock' else 90,f14='synthetic '+code+'x'*state['name_size'],
                f3='1',f6='1000',f62=str(n),f124=int(stamp.timestamp())))
        if state['changed_total'] and category=='concept' and page>1:total+=1
        if state['huge'] and category=='industry':total=5001
        return Response(json.dumps(dict(rc=0,data=dict(total=total,diff=rows))).encode(),state['at']+timedelta(seconds=1),1,200)
    monkeypatch.setattr(m,'fetch_https',fetch)
    return state


def collect():
    permits=[]
    response=m.fetch_rankings('eastmoney_funds',lambda:permits.append(True),full_boards=True)
    result=m.parse_rankings(response.body,response.received_at,'eastmoney_funds')
    return response,result,permits


def test_every_reported_stock_industry_and_concept_is_collected(upstream):
    response,result,permits=collect()
    tables={p.dataset:p for _,p,_ in result.records}
    assert len(tables['market.eastmoney.concept_funds'].rows)==531
    assert len(tables['market.eastmoney.industry_funds'].rows)==128
    assert len(tables['market.eastmoney.stock_funds'].rows)==5927
    assert upstream['calls']==[*[('stock',p) for p in range(1,61)],('industry',1),('industry',2),*[('concept',p) for p in range(1,7)]]
    assert len(permits)==response.request_count==result.requests==68
    assert result.rejected==0
    for key in tables:
        assert u.saved_capture(tables[key].model_dump(),response.received_at)['complete']


def test_a_failed_middle_page_keeps_real_other_points_but_is_not_complete(upstream):
    upstream['fail']=('concept',3)
    response,result,_=collect()
    table=next(p for _,p,_ in result.records if p.dataset.endswith('concept_funds'))
    assert len(table.rows)==431 and result.rejected==1
    assert not u.saved_capture(table.model_dump(),response.received_at)['complete']
    assert ('concept',4) in upstream['calls'] and ('concept',6) in upstream['calls']


@pytest.mark.parametrize('problem',['duplicate','changed_total'])
def test_a_moving_ranked_universe_never_claims_complete_coverage(upstream,problem):
    upstream[problem]=True
    response,result,_=collect()
    table=next(p for _,p,_ in result.records if p.dataset.endswith('concept_funds'))
    assert result.rejected==1
    assert not u.saved_capture(table.model_dump(),response.received_at)['complete']


def test_every_new_page_rechecks_permission_before_physical_request(upstream):
    permits=[]
    def reserve():
        permits.append(True)
        if len(permits)==3:raise SourceFailure('permission_missing')
    with pytest.raises(m.EastmoneyFailure) as error:m.fetch_rankings('eastmoney_funds',reserve,full_boards=True)
    assert error.value.reason=='permission_missing' and error.value.request_count==2
    assert upstream['calls']==[('stock',1),('stock',2)]


def test_oversized_universe_stops_instead_of_silently_truncating(upstream):
    upstream['huge']=True
    with pytest.raises(m.EastmoneyFailure) as error:collect()
    assert error.value.reason=='funds_universe_out_of_bounds'
    assert upstream['calls']==[*[('stock',p) for p in range(1,61)],('industry',1)]


def test_future_page_clock_is_not_held_for_later_automatic_publication(upstream):
    upstream['future']=True
    with pytest.raises(SourceFailure) as error:collect()
    assert error.value.reason=='future_source_time'


def test_saved_larger_table_needs_real_bounded_pages_and_matching_receipts(upstream):
    response,result,_=collect()
    payload=next(p for _,p,_ in result.records if p.dataset.endswith('concept_funds')).model_dump()
    for row in payload['rows']:row['capture_page']='1'
    assert u.saved_capture(payload,response.received_at) is None


def test_envelope_cannot_forge_physical_request_accounting(upstream):
    response,_,_=collect()
    value=json.loads(response.body);value['request_count']+=1
    with pytest.raises(SourceFailure) as error:m.parse_rankings(json.dumps(value).encode(),response.received_at,'eastmoney_funds')
    assert error.value.reason=='schema_changed'


def test_all_board_pages_survive_ingestion_and_a_later_partial_scan_retains_the_complete_rank(client,upstream):
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market.collect import store_reference_batch
    from app.market.models import SourceSettings
    from app.market.service import source_id
    from app.models import Provider
    from test_intraday_funds import setup,read
    setup(client)
    first,batch,_=collect()
    def store(result,received):
        with Session(engine()) as db,db.begin():
            provider=db.get(Provider,source_id('eastmoney_funds'))
            return store_reference_batch(db,provider,db.get(SourceSettings,provider.id),result,received)
    store(batch,first.received_at)
    complete=read(first.received_at,sector='BK2000.DC')
    assert len(complete.sectors)==659 and complete.state=='available'
    assert len(complete.stocks)==5927
    by_category={row.category:row for row in complete.coverage}
    assert (by_category['stock'].represented_count,by_category['stock'].expected_count)==(5927,5927)
    assert (by_category['industry'].represented_count,by_category['industry'].expected_count)==(128,128)
    assert (by_category['concept'].represented_count,by_category['concept'].expected_count)==(531,531)
    assert '531/531' in complete.scope and '128/128' in complete.scope
    assert next(p for p in complete.points if p.sector_code=='BK2000.DC').time==NOW
    upstream['at']=NOW+timedelta(seconds=30);upstream['fail']=('concept',3)
    later,batch,_=collect();store(batch,later.received_at)
    partial=read(later.received_at,sector='BK2000.DC')
    assert len(partial.sectors)==659 and partial.state=='stale'
    concept=next(row for row in partial.coverage if row.category=='concept')
    assert (concept.represented_count,concept.latest_batch_count,concept.expected_count)==(531,431,531)
    assert concept.complete is False and concept.source_time==NOW
    assert concept.latest_batch_source_time==NOW+timedelta(seconds=30)
    assert '431/531' in partial.scope and '分页未齐' in partial.scope
    assert [p.time for p in partial.points if p.sector_code=='BK2000.DC']==[NOW,NOW+timedelta(seconds=30)]


def test_full_board_scope_appends_real_bounded_shards_without_rewriting_the_original_cohort(client,upstream):
    from sqlalchemy import select,func
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.evidence.models import EvidenceVersion
    from app.market import fund_history as history
    from app.market.collect import store_reference_batch
    from app.market.models import SourceSettings
    from app.market.service import source_id
    from app.models import Provider
    from test_intraday_funds import setup,save,DAY
    setup(client)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(history.FAMILY));config=db.get(SourceSettings,provider.id)
        save(db,[(f'BK{2000+n}',str(n)) for n in range(100)],NOW-timedelta(minutes=5))
        root=history._plan(db,provider,config,'concept',DAY,NOW-timedelta(minutes=4))
        original_id,original_payload,original_hash=root.id,root.payload,root.content_hash
    response,batch,_=collect()
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(history.FAMILY));config=db.get(SourceSettings,provider.id)
        store_reference_batch(db,provider,config,batch,response.received_at)
        history._plan(db,provider,config,'concept',DAY,NOW+timedelta(seconds=20))
        groups=history.saved_archives(db,provider,DAY,NOW+timedelta(seconds=20))
        assert sorted(len(g['targets']) for g in groups)==[31,100,200,200]
        assert set().union(*(set(g['targets']) for g in groups))=={f'BK{2000+n}' for n in range(531)}
        assert sum(len(g['targets']) for g in groups)==531
        assert db.get(EvidenceVersion,original_id).payload==original_payload
        assert db.get(EvidenceVersion,original_id).content_hash==original_hash
        before=db.scalar(select(func.count()).select_from(EvidenceVersion))
        history._plan(db,provider,config,'concept',DAY,NOW+timedelta(seconds=30))
        assert db.scalar(select(func.count()).select_from(EvidenceVersion))==before


def test_minute_shards_only_replace_rankings_after_the_whole_scope_is_complete(client,upstream,monkeypatch):
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.evidence.models import EvidenceVersion
    from app.market import fund_history as history
    from app.market.collect import store_reference_batch
    from app.market.intraday_funds import intraday_funds
    from app.market.models import SourceSettings
    from app.market.service import source_id
    from app.models import Provider
    from test_intraday_funds import setup,DAY
    from test_fund_history import store_history,CLOSE
    setup(client);upstream['totals']={'concept':3,'industry':1}
    response,batch,_=collect()
    monkeypatch.setattr(history,'MAX_TARGETS',2)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(history.FAMILY));config=db.get(SourceSettings,provider.id)
        store_reference_batch(db,provider,config,batch,response.received_at)
        history._plan(db,provider,config,'concept',DAY,NOW+timedelta(seconds=20))
        groups=history.saved_archives(db,provider,DAY,CLOSE)
        assert sorted(len(g['targets']) for g in groups)==[1,2]
        targets=[(g['evidence'].id,code) for g in groups for code in g['targets']]
        for plan_id,code in targets[:-1]:store_history(db,provider,config,db.get(EvidenceVersion,plan_id),code)
    with Session(engine()) as db:
        partial=intraday_funds(db,DAY,CLOSE,session={'phase':'closed'})
        assert max(r.source_time for r in partial.sectors if r.category=='concept')==NOW
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(history.FAMILY));config=db.get(SourceSettings,provider.id)
        plan_id,code=targets[-1];store_history(db,provider,config,db.get(EvidenceVersion,plan_id),code)
    with Session(engine()) as db:
        full=intraday_funds(db,DAY,CLOSE,session={'phase':'closed'})
        concept=[r for r in full.sectors if r.category=='concept']
        assert len(concept)==3 and all(r.source_time.hour==15 and r.source_time.minute==0 for r in concept)


@pytest.mark.parametrize('page',[0,True,51,'2'])
def test_board_page_url_remains_bounded(page):
    with pytest.raises(SourceFailure):m.ranking_url('concept',funds=True,page=page)


@pytest.mark.parametrize('page',[0,True,81,'2'])
def test_stock_page_url_has_a_bounded_whole_market_range(page):
    with pytest.raises(SourceFailure):m.ranking_url('stocks',funds=True,page=page)
    assert parse_qs(urlsplit(m.ranking_url('stocks',funds=True,page=60)).query)['pn']==['60']
    with pytest.raises(SourceFailure):m.ranking_url('stocks',funds=False,page=2)


def test_a_failed_stock_page_retains_actual_points_without_claiming_the_whole_market(upstream):
    upstream['fail']=('stock',2)
    response,result,permits=collect()
    table=next(p for _,p,_ in result.records if p.dataset.endswith('stock_funds'))
    assert len(table.rows)==5827 and result.rejected==1
    assert u.saved_capture(table.model_dump(),response.received_at)['total']==5927
    assert not u.saved_capture(table.model_dump(),response.received_at)['complete']
    assert len(permits)==68 and ('stock',60) in upstream['calls']


def test_stock_universe_above_payload_limit_is_rejected_instead_of_truncated(upstream):
    upstream['totals']['stock']=8001
    with pytest.raises(m.EastmoneyFailure) as error:collect()
    assert error.value.reason=='funds_universe_out_of_bounds'
    assert upstream['calls']==[('stock',1)]


def test_large_local_capture_does_not_raise_the_physical_response_limit(upstream):
    upstream['name_size']=180
    response,result,_=collect()
    assert len(response.body)>m.MAX_BODY
    assert len(next(p for _,p,_ in result.records if p.dataset.endswith('stock_funds')).rows)==5927
    with pytest.raises(SourceFailure) as error:m.parse_ranking(b'x'*(m.MAX_BODY+1),NOW,'market.eastmoney.stock_funds','synthetic')
    assert error.value.reason=='response_too_large'


def test_ignored_stable_sort_cannot_be_reported_as_complete(upstream):
    upstream['ignore_sort']=True
    response,result,_=collect()
    assert result.rejected==3
    for _,table,_ in result.records:
        assert not u.saved_capture(table.model_dump(),response.received_at)['complete']


def test_a_smaller_stock_minute_cohort_does_not_replace_the_whole_stock_universe(client,upstream,monkeypatch):
    from types import SimpleNamespace
    from decimal import Decimal
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market.collect import store_reference_batch
    from app.market.models import SourceSettings
    from app.market.service import source_id
    from app.models import Provider
    from app.market.intraday_funds import Snapshot,Sample
    from test_intraday_funds import setup,read
    setup(client)
    response,batch,_=collect()
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('eastmoney_funds'))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),batch,response.received_at)
    at=NOW+timedelta(seconds=30)
    small=Snapshot(SimpleNamespace(id='synthetic-minute-cohort',source_id=source_id('eastmoney_funds'),
        content_hash='0'*64,received_at=at,version_number=1),'stock',
        {'600000.SH':Sample('600000.SH','synthetic',at,Decimal(9),None,None)})
    monkeypatch.setattr('app.market.intraday_funds._history_snapshots',lambda *a:[small])
    value=read(at)
    assert len(value.stocks)==5927
    assert next(row for row in value.coverage if row.category=='stock').represented_count==5927
