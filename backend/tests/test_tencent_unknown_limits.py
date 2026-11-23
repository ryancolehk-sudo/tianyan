"""An observed unusable limit pair must not erase an otherwise valid quote."""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.market import tencent_market as source
from app.market.collect import store_reference_batch
from app.market.models import SourceSettings
from app.market.protocol import InstrumentKey,Response,SHANGHAI
from app.market.service import source_id
from app.models import Provider
from test_tencent_market import AT,STOCK,body,configure_all


def wire(keys=(STOCK,),*,limits=('-1','-1'),changes=None,stamp=AT):
    lines=[]
    for line in body(keys,stamp=stamp).decode('gb18030').splitlines():
        header,data=line.split('="',1);fields=data[:-2].split('~')
        fields[47],fields[48]=limits
        for index,value in (changes or {}).items():fields[index]=value
        lines.append(header+'="'+'~'.join(fields)+'";')
    return '\n'.join(lines).encode('gb18030')


def fetched(monkeypatch,keys=(STOCK,),*,first_unknown_only=False):
    clock=[AT-timedelta(milliseconds=100)]
    monkeypatch.setattr(source,'observation_clock',lambda:clock[0])
    calls=[]
    def request(url):
        calls.append(url);clock[0]=AT
        payload=wire(keys)
        if first_unknown_only:
            payload=wire(keys[:1])+b'\n'+body(keys[1:])
        return Response(payload,AT,50,200)
    monkeypatch.setattr(source,'fetch_https',request)
    scope=source.Universe(tuple(keys),uuid.uuid4(),uuid.uuid4(),AT,'previous_quote_batch','a'*64)
    return source.fetch(scope,AT.astimezone(SHANGHAI).date(),before_request=lambda:None),calls


def test_observed_pair_retains_price_clock_units_and_unknown_limits():
    response=Response(wire(),AT,50,200)
    rows,rejected=source.parse_page(response,[STOCK])
    assert not rejected and len(rows)==1
    row=rows[0]
    assert row['close']=='11' and row['pre_close']=='10' and row['vol']=='12300'
    assert row['amount']=='1234567.89' and row['bid_volume1']=='200'
    assert row['trade_time']==AT.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')
    assert row['up_limit'] is None and row['down_limit'] is None


def test_complete_quote_membership_does_not_claim_complete_limit_statistics(monkeypatch):
    other=InstrumentKey('SSE','600002','stock')
    (response,batch),calls=fetched(monkeypatch,(STOCK,other),first_unknown_only=True)
    snapshot,manifest=[record[1] for record in batch.records]
    assert len(calls)==response.request_count==batch.requests==1 and len(snapshot.rows)==2
    coverage=source.closed_coverage(manifest.model_dump(mode='json'),snapshot.rows[0]['capture_id'],AT)
    assert coverage['expected']==coverage['accepted']=='2' and coverage['rejected']=='0'
    assert coverage['stat_returned']=='2' and coverage['stat_advancing']=='2'
    assert coverage['stat_unknown']=='1' and coverage['stat_comparable']=='1'
    assert coverage['stat_limit_up']=='1' and coverage['stat_limit_down']=='0'


def test_saved_series_keeps_limits_unjudgeable_when_every_row_has_unknown_bounds(client,monkeypatch):
    configure_all(client)
    clock=[AT-timedelta(milliseconds=100)]
    monkeypatch.setattr(source,'observation_clock',lambda:clock[0])
    def request(url):clock[0]=AT;return Response(wire(),AT,50,200)
    monkeypatch.setattr(source,'fetch_https',request)
    with Session(engine()) as db,db.begin():
        scope=source.universe(db,AT)
        response,batch=source.fetch(scope,AT.astimezone(SHANGHAI).date(),before_request=lambda:None)
        provider=db.get(Provider,source_id(source.FAMILY));config=db.get(SourceSettings,provider.id)
        store_reference_batch(db,provider,config,batch,response.received_at)
        series=source.breadth_series(db,AT.date(),AT)
        assert series.latest.returned==series.latest.unknown==1 and series.latest.comparable==0
        assert series.latest.limit_up is None and series.latest.limit_down is None
        assert series.latest.advancing==1


@pytest.mark.parametrize('limits,changes,reason',[
    (('-1','9'),{},'invalid_number'),(('11','-1'),{},'invalid_number'),
    (('-2','-2'),{},'invalid_number'),(('-1.00','-1.00'),{},'invalid_number'),
    (('-1','-1'),{9:'-1'},'invalid_number'),
    (('-1','-1'),{35:'10:29:59/123/-1'},'invalid_number'),
    (('-1','-1'),{3:'-1'},'invalid_number'),
    (('-1','-1'),{33:'8'},'inconsistent_price_range'),
])
def test_pair_handling_does_not_relax_other_invalid_fields(limits,changes,reason):
    rows,rejected=source.parse_page(Response(wire(limits=limits,changes=changes),AT,50,200),[STOCK])
    assert not rows and rejected=={STOCK.vendor_key:reason}


def test_pair_cannot_make_a_future_quote_acceptable():
    response=Response(wire(stamp=AT+timedelta(seconds=61)),AT,50,200)
    rows,rejected=source.parse_page(response,[STOCK])
    assert not rows and rejected=={STOCK.vendor_key:'future_source_time'}
