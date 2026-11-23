"""Pure request contracts: current leader POST, summary/archive GET; no network."""
from datetime import date
import io
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from app.market import direction_sources as source
from app.market.kpl_leaders import parse
from app.market.protocol import SourceFailure


@pytest.fixture(autouse=True)
def isolated_database():
    # This pure-transport module must never reset or access a database.
    yield


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    monkeypatch.setattr(source.urllib.request,'build_opener',
        lambda *args:pytest.fail('Leader transport tests must not contact an upstream'))


@pytest.mark.parametrize('day,summary,method,action',[
    (None,False,'POST','ZhiShuStockList_W8'),
    (None,True,'GET','GetPlate_Info_QJ'),
    (date(2026,11,10),False,'GET','ZhiShuStockList_W8'),
])
def test_leader_summary_and_archive_keep_separate_verified_http_contracts(day,summary,method,action):
    request=source._request('direction_kpl_strength',day,plate_code='801159',plate_summary=summary)
    url=urlsplit(request.full_url)
    params=parse_qs(request.data.decode() if request.data else url.query,keep_blank_values=True)
    assert request.get_method()==method and params['a']==[action] and params['PlateID']==['801159']
    assert url.scheme=='https' and url.netloc==urlsplit(source.KPL_HISTORY_URL if day else source.KPL_URL).netloc
    assert request.get_header('Content-type')=='application/x-www-form-urlencoded'
    if day is not None:assert params['Date']==[day.isoformat()]
    elif summary:
        assert params['Date']==[''] and not {'Type','st','old','Index','Order'}&set(params)
    else:
        assert url.query=='' and 'Date' not in params
        assert params['Type']==['6'] and params['st']==['1000'] and params['old']==['1']


def test_real_fetch_path_posts_current_leaders_and_preserves_native_markers(monkeypatch):
    rows=[]
    for index in range(5):
        row=[0]*70;row[0]=str(600001+index);row[1]='合成原生标记'+str(index)
        row[24]=['龙一','龙二','龙三','龙四','龙五'][index];rows.append(row)
    body=json.dumps(dict(errcode='0',Day=['2026-11-11'],Count=1200,list=rows)).encode()
    requests=[]
    class Opener:
        def open(self,request,timeout):
            requests.append(request)
            assert timeout==15 and request.get_method()=='POST'
            params=parse_qs(request.data.decode());assert params['PlateID']==['801159']
            response=io.BytesIO(body);response.status=200;response.headers={}
            return response
    monkeypatch.setattr(source.urllib.request,'build_opener',lambda *args:Opener())
    response=source.fetch('direction_kpl_strength',plate_code='801159')
    payload=parse(response,'801159',date(2026,11,11))
    assert len(requests)==1 and len(payload.rows)==5
    assert [row['label'] for row in payload.rows]==['龙一','龙二','龙三','龙四','龙五']
    assert all(row['coverage']=='complete' and row['archive_kind']=='intraday' for row in payload.rows)
    assert '全部前五标记' in payload.scope


def test_current_post_limit_never_falls_back_or_doubles_a_supplier_request(monkeypatch):
    requests=[]
    class Opener:
        def open(self,request,timeout):
            requests.append(request)
            raise HTTPError(request.full_url,429,'synthetic limit',{'Retry-After':'45'},io.BytesIO())
    monkeypatch.setattr(source.urllib.request,'build_opener',lambda *args:Opener())
    with pytest.raises(SourceFailure) as caught:source.fetch('direction_kpl_strength',plate_code='801159')
    assert caught.value.reason=='rate_limited' and caught.value.retry_after==45
    assert len(requests)==1 and requests[0].get_method()=='POST'
