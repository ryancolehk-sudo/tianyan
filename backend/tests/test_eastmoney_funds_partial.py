"""Synthetic transport/quality/read tests; no supplier or database calls."""
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
import uuid

import pytest

from app.market import eastmoney_trading as m
from app.market import intraday_funds as reading
from app.market import resilience
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response, SourceFailure
from app.models import Provider

NOW=datetime.fromisoformat('2026-10-27T09:31:00+08:00')


def body(category,*,at=NOW):
    code={'stock':'600000','industry':'BK0001','concept':'BK0002'}[category]
    return json.dumps({'rc':0,'data':{'total':1,'diff':[{
        'f12':code,'f13':1 if category=='stock' else 90,'f14':'synthetic '+code,
        'f3':'1.5','f6':'1000','f62':'10','f124':int(at.timestamp())}]}}).encode()


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    import socket
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:pytest.fail('network forbidden'))
    class FixedDateTime(datetime):
        @classmethod
        def now(cls,tz=None):
            return (NOW+timedelta(seconds=2)).astimezone(tz) if tz else (NOW+timedelta(seconds=2)).replace(tzinfo=None)
    monkeypatch.setattr(m,'datetime',FixedDateTime)


def fetcher(monkeypatch,*,failure=None,reason='transport_error',bad_parse=False):
    calls=[]
    def fetch(url):
        index=len(calls);calls.append(url)
        if index==failure:
            if bad_parse:return Response(b'{"rc":1,"data":{}}',NOW+timedelta(seconds=1),1,200)
            raise SourceFailure(reason,retry_after=75 if reason=='rate_limited' else None)
        category=['stock','industry','concept'][index]
        return Response(body(category),NOW+timedelta(seconds=1),1,200)
    monkeypatch.setattr(m,'fetch_https',fetch)
    return calls


@pytest.mark.parametrize('reason',['timeout','transport_error','upstream_unavailable','upstream_redirect','schema_changed'])
@pytest.mark.parametrize('failure',[0,1,2])
def test_a_network_failure_keeps_the_other_independent_fund_tables(monkeypatch,reason,failure):
    calls=fetcher(monkeypatch,failure=failure,reason=reason)
    permits=[]
    response=m.fetch_rankings('eastmoney_funds',lambda:permits.append(True))
    parsed=m.parse_rankings(response.body,response.received_at,'eastmoney_funds')
    assert len(calls)==len(permits)==response.request_count==parsed.requests==3
    assert len(parsed.records)==2 and parsed.expected==3 and parsed.rejected==1
    failed=m.TABLES['eastmoney_funds'][failure][0]
    assert {record[1].dataset for record in parsed.records}=={
        'market.eastmoney.'+name for name,_,_ in m.TABLES['eastmoney_funds'] if name!=failed}
    assert all(datetime.fromisoformat(row['source_time'])==NOW
        for _,payload,_ in parsed.records for row in payload.rows)
    metadata=json.loads(response.body)['failed_tables']
    assert metadata==[{'table':failed,'reason':reason}]


@pytest.mark.parametrize('reason',['timeout','transport_error','upstream_unavailable'])
def test_all_failed_tables_return_an_error_with_all_http_attempts(monkeypatch,reason):
    calls=[]
    def fail(url):calls.append(url);raise SourceFailure(reason)
    monkeypatch.setattr(m,'fetch_https',fail)
    with pytest.raises(m.EastmoneyFailure) as error:
        m.fetch_rankings('eastmoney_funds',lambda:None)
    assert error.value.reason==reason and error.value.request_count==len(calls)==3


@pytest.mark.parametrize('reason',['permission_missing','rate_limited','configuration_changed'])
@pytest.mark.parametrize('failure',[0,1])
def test_hard_failure_stops_without_hiding_the_reason(monkeypatch,reason,failure):
    calls=fetcher(monkeypatch,failure=failure,reason=reason)
    with pytest.raises(m.EastmoneyFailure) as error:
        m.fetch_rankings('eastmoney_funds',lambda:None)
    assert error.value.reason==reason and error.value.request_count==failure+1
    assert len(calls)==failure+1
    if reason=='rate_limited':assert error.value.retry_after==75


def test_a_later_budget_denial_does_not_send_an_unpermitted_request(monkeypatch):
    calls=fetcher(monkeypatch)
    permits=[]
    def reserve():
        permits.append(True)
        if len(permits)==2:raise SourceFailure('rate_limited',retry_after=120)
    with pytest.raises(m.EastmoneyFailure) as error:
        m.fetch_rankings('eastmoney_funds',reserve)
    assert error.value.reason=='rate_limited' and error.value.request_count==1
    assert error.value.retry_after==120 and len(calls)==1 and len(permits)==2


def test_a_rejected_table_keeps_the_other_successes_with_its_actual_reason(monkeypatch):
    calls=fetcher(monkeypatch,failure=1,bad_parse=True)
    response=m.fetch_rankings('eastmoney_funds',lambda:None)
    parsed=m.parse_rankings(response.body,response.received_at,'eastmoney_funds')
    assert len(calls)==3 and len(parsed.records)==2
    assert json.loads(response.body)['failed_tables']==[
        {'table':'industry_funds','reason':'upstream_unavailable'}]


def test_the_other_board_feed_keeps_its_existing_fail_fast_contract(monkeypatch):
    calls=fetcher(monkeypatch,failure=0)
    with pytest.raises(m.EastmoneyFailure) as error:
        m.fetch_rankings('eastmoney_boards',lambda:None)
    assert error.value.request_count==1 and len(calls)==1


@pytest.mark.parametrize('malformed',['missing_failure','duplicate_failure','unknown_table','overlap',
    'hard_failure','wrong_count','boolean_count','no_success'])
def test_partial_envelopes_cannot_hide_missing_scope_or_hard_errors(monkeypatch,malformed):
    fetcher(monkeypatch,failure=0)
    response=m.fetch_rankings('eastmoney_funds',lambda:None)
    value=json.loads(response.body)
    if malformed=='missing_failure':value.pop('failed_tables')
    elif malformed=='duplicate_failure':value['failed_tables']*=2
    elif malformed=='unknown_table':value['failed_tables'][0]['table']='unknown'
    elif malformed=='overlap':value['failed_tables'][0]['table']='industry_funds'
    elif malformed=='hard_failure':value['failed_tables'][0]['reason']='permission_missing'
    elif malformed=='wrong_count':value['request_count']=2
    elif malformed=='boolean_count':value['request_count']=True
    elif malformed=='no_success':value['tables']=[]
    with pytest.raises(SourceFailure,match='schema_changed'):
        m.parse_rankings(json.dumps(value).encode(),response.received_at,'eastmoney_funds')


def evidence(category,provider,*,at=NOW):
    payload=m.parse_ranking(body(category,at=at),at+timedelta(seconds=1),
        'market.eastmoney.'+category+'_funds','synthetic').model_dump(mode='json')
    return SimpleNamespace(id=uuid.uuid4(),source_id=provider.id,payload=payload,
        received_at=at+timedelta(seconds=1),version_number=1,content_hash='a'*64)


class SavedDB:
    def __init__(self,provider,config,rows,attempt):
        self.provider,self.config,self.rows,self.attempt=provider,config,rows,attempt
    def scalar(self,query):
        entity=query.column_descriptions[0].get('entity')
        if entity is Provider:return self.provider
        if entity is DataFetchRun:return self.attempt
        from app.evidence.models import EvidenceVersion
        if entity is EvidenceVersion:return None
        raise AssertionError('unexpected read')
    def scalars(self,query):return self.rows
    def execute(self,query):
        from app.evidence.models import EvidenceVersion
        assert query.column_descriptions[0].get('entity') is EvidenceVersion
        return iter((row,row.payload['dataset']) for row in self.rows)
    def get(self,entity,identity):
        assert entity is SourceSettings
        return self.config


def saved_fixture(*,partial=True,rejected=1):
    provider=SimpleNamespace(id=uuid.uuid4(),family='eastmoney_funds',name='synthetic source')
    config=SimpleNamespace(interval_seconds=150,configured_at=NOW-timedelta(hours=1),paused_reason=None)
    rows=[evidence(category,provider,at=NOW-timedelta(seconds=30) if category=='stock' else NOW)
        for category in ['stock','industry','concept']]
    attempt=SimpleNamespace(expected=3,accepted=2,duplicates=0,status='partial' if partial else 'complete',rejected=rejected if partial else 0,
        received_at=NOW+timedelta(seconds=2),started_at=NOW,reason=None)
    return provider,config,rows,attempt,SavedDB(provider,config,rows,attempt)


@pytest.mark.parametrize('rejected',[1,2])
def test_old_fresh_tables_cannot_make_the_partial_current_batch_green(rejected):
    provider,config,rows,attempt,db=saved_fixture(rejected=rejected)
    quality=resilience.saved_quality(db,provider,config,'source:eastmoney_funds',NOW.date(),
        NOW+timedelta(seconds=2),attempt)
    assert not quality.valid and quality.reason=='coverage_partial'
    assert quality.missing_count>=rejected
    assert rows[0].received_at==NOW-timedelta(seconds=29)


def test_complete_current_tables_still_pass_the_existing_quality_contract():
    provider,config,rows,attempt,db=saved_fixture(partial=False)
    quality=resilience.saved_quality(db,provider,config,'source:eastmoney_funds',NOW.date(),
        NOW+timedelta(seconds=2),attempt)
    assert quality.valid and quality.missing_count==0


def test_read_model_preserves_successes_and_marks_partial_scope(monkeypatch):
    provider,config,rows,attempt,db=saved_fixture()
    monkeypatch.setattr('app.market.service.permission_reason',lambda provider:None)
    monkeypatch.setattr(reading,'_history_snapshots',lambda *a:[])
    result=reading.intraday_funds(db,NOW.date(),NOW+timedelta(seconds=2),session={'phase':'trading'})
    assert result.state=='stale' and len(result.sectors)==2 and len(result.points)==2
    assert len(result.stocks)==1 and result.stocks[0].source_time==NOW-timedelta(seconds=30)
    assert all(row.source_time==NOW for row in result.sectors)
    assert '部分资金表未能更新' in result.scope and '不代表本轮完整快照' in result.scope
    assert result.latest_source_time==NOW
