"""No real sockets: pinned TLS reuse, sanitized failures and bounded recovery."""
from datetime import UTC,datetime,timedelta
import http.client

import pytest
from app.market import fuyao,fuyao_transport as transport
from app.market.protocol import SourceFailure


class FakeResponse:
    status=200
    headers={}
    will_close=False
    def read(self,limit):
        return b'{"code":0,"data":{"item":[]}}'


@pytest.fixture
def connection(monkeypatch):
    transport.close()
    instances=[]
    class Connection:
        def __init__(self,host,timeout):
            assert host=='fuyao.aicubes.cn' and timeout==15
            self.calls=[];self.closed=False;self.response=FakeResponse()
            instances.append(self)
        def request(self,method,target,headers):
            self.calls.append((method,target,headers.copy()))
        def getresponse(self):return self.response
        def close(self):self.closed=True
    monkeypatch.setattr(transport.http.client,'HTTPSConnection',Connection)
    monkeypatch.setattr(transport.urllib.request,'getproxies',lambda:{})
    yield instances
    transport.close()


def test_reuses_direct_connection_but_does_not_retain_old_credential(connection):
    for key in ('synthetic-first-key','synthetic-rotated-key'):
        fuyao.request('/api/a-share/prices/snapshot',{'limit':6000,'offset':0},key)
    assert len(connection)==1
    assert [call[2]['X-api-key'] for call in connection[0].calls]==['synthetic-first-key','synthetic-rotated-key']


@pytest.mark.parametrize('status,reason',[(302,'upstream_redirect'),(429,'rate_limited'),(503,'upstream_unavailable')])
def test_failure_closes_pool_and_next_attempt_creates_a_fresh_connection(connection,status,reason):
    fuyao.request('/api/a-share/prices/snapshot',{},'synthetic-private-key')
    response=connection[0].response
    response.status=status
    response.headers={'Retry-After':'37','Location':'https://other.invalid/path?api_key=synthetic-do-not-log'}
    with pytest.raises(SourceFailure,match=reason) as caught:
        fuyao.request('/api/a-share/prices/snapshot',{},'synthetic-private-key')
    assert caught.value.request_count==1 and connection[0].closed
    assert 'synthetic' not in repr(transport.safe_diagnostic(caught.value))
    if status==429:assert caught.value.retry_after==37
    fuyao.request('/api/a-share/prices/snapshot',{},'synthetic-private-key')
    assert len(connection)==2


def test_timeout_is_not_silently_replayed_and_does_not_poison_next_request(connection,monkeypatch):
    fuyao.request('/api/a-share/prices/snapshot',{},'synthetic-private-key')
    def fail():raise TimeoutError('synthetic-do-not-log')
    monkeypatch.setattr(connection[0],'getresponse',fail)
    with pytest.raises(SourceFailure,match='timeout') as caught:
        fuyao.request('/api/a-share/prices/snapshot',{},'synthetic-private-key')
    assert caught.value.request_count==1 and len(connection)==1 and connection[0].closed


def test_expired_idle_connection_is_retired_without_sending_credentials(connection,monkeypatch):
    fuyao.request('/api/a-share/prices/snapshot',{},'synthetic-private-key')
    first=connection[0]
    state=transport._local.connection
    transport._local.connection=(state[0],state[1],state[2]-transport.IDLE_SECONDS-1)
    fuyao.request('/api/a-share/prices/snapshot',{},'synthetic-private-key')
    assert len(connection)==2 and first.closed and len(first.calls)==1


def test_denied_permit_never_opens_a_socket(connection):
    with pytest.raises(SourceFailure,match='request_not_permitted'):
        fuyao.request('/api/a-share/prices/snapshot',{},'synthetic-private-key',before_request=lambda:False)
    assert connection==[]


def test_retry_after_supports_http_dates_and_diagnostics_strip_untrusted_values():
    clock=datetime(2026,10,29,5,tzinfo=UTC)
    assert transport.retry_after('Thu, 29 Oct 2026 05:01:00 GMT',clock)==60
    assert transport.retry_after('invalid',clock) is None
    error=SourceFailure('upstream_redirect')
    error.transport_diagnostic={'version':1,'http_status':302,'redirect_origin':[],
        'upstream_code':'not-a-code?api_key=synthetic-secret','url':'synthetic-secret'}
    assert transport.safe_diagnostic(error)=={'version':1,'http_status':302}
