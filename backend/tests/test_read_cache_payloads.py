"""Large raw-table scans must not evict small costly immutable indexes."""
import json

import pytest

from app import read_cache_payloads as cache


@pytest.fixture
def small_cache(monkeypatch):
    cache.clear()
    monkeypatch.setattr(cache,'MAX_BYTES',4096)
    monkeypatch.setattr(cache,'MAX_PROTECTED_BYTES',1024)
    yield
    cache.clear()


def test_raw_table_churn_preserves_protected_index_and_total_budget(small_cache):
    index={'names':['合成索引']}
    cache.put(('index',1),index,protected=True)
    for number in range(20):
        cache.put(('raw',number),{'rows':'x'*300})
        assert cache._bytes<=4096 and cache._protected_bytes<=1024
    assert cache.get(('index',1)) is index
    assert cache.get(('raw',0)) is None
    assert cache.get(('raw',19)) is not None


def test_protected_entries_have_their_own_lru_and_bounded_share(small_cache):
    for key in ('a','b'):cache.put(key,{'value':'x'*100},protected=True)
    assert cache.get('a')
    cache.put('c',{'value':'x'*100},protected=True)
    assert cache.get('a') and cache.get('c') and cache.get('b') is None
    assert cache._protected_bytes<=1024


def test_large_index_uses_ordinary_pool_and_keeps_existing_size_behavior(small_cache):
    value={'value':'x'*500};cache.put('large',value,protected=True)
    assert cache.get('large') is value and cache._protected_bytes==0
    cache.put('too-large',{'value':'x'*2000},protected=True)
    assert cache.get('too-large') is None
    assert cache._bytes<=4096


def test_replacing_between_pools_does_not_double_charge_and_clear_removes_both(small_cache):
    for protected in (False,True,False,True):
        value={'value':str(protected)};cache.put('key',value,protected=protected)
        assert cache.get('key') is value
        assert cache._bytes==len(json.dumps(value,ensure_ascii=False).encode())*4
        assert cache._protected_bytes==(cache._bytes if protected else 0)
    cache.put('raw',{'value':'other'})
    cache.clear()
    assert cache.get('key') is None and cache.get('raw') is None
    assert cache._bytes==cache._protected_bytes==0
