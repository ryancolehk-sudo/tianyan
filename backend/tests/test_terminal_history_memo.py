"""Raw single-stock history reuse keeps live authorization and temporal rules."""
import copy
import json
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app import read_cache_payloads as cache
from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import terminal_read as tr
from app.market.collect import store_reference_batch
from app.market.models import SourceSettings
from app.market.service import source_id
from app.market.tushare_market import encoded,parse_market
from app.models import Provider
from test_terminal_workbench import AT, DAY, save, setup
from test_tushare_market import limits, quote

CODE='000100.SZ'
OTHER='000101.SZ'


@pytest.fixture(autouse=True)
def memo(monkeypatch):
    cache.clear()
    monkeypatch.setattr(settings(),'read_cache_enabled',True)
    monkeypatch.setattr('urllib.request.build_opener',lambda *a:pytest.fail('history reads must stay offline'))
    yield
    cache.clear()


@contextmanager
def reader(cutoff=AT+timedelta(minutes=1),isolation='REPEATABLE READ'):
    with Session(engine()) as db:
        db.connection(execution_options={'isolation_level':isolation,'postgresql_readonly':True})
        value=tr.SavedTables(db,cutoff,DAY)
        yield value,value.market(DAY)


def histories(value,snapshots,codes=(CODE,)):
    return tr.stock_histories(value,DAY,snapshots,[tr.make_stock(code,{}, {}) for code in codes])


@contextmanager
def history_queries():
    captured=[]
    def before(connection,cursor,statement,parameters,context,many):
        if 'jsonb_array_elements' in statement:
            captured.append(parameters)
    event.listen(engine(),'before_cursor_execute',before)
    try:yield captured
    finally:event.remove(engine(),'before_cursor_execute',before)


def uuid_parameters(value):
    if isinstance(value,uuid.UUID):return {value}
    if isinstance(value,dict):value=value.values()
    if isinstance(value,(str,bytes)) or value is None:return set()
    try:return set().union(*(uuid_parameters(item) for item in value))
    except TypeError:return set()


def test_complete_duplicates_absence_and_native_order_survive_reuse(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    with Session(engine()) as db,db.begin():
        raw=[quote(CODE,price='11'),quote(OTHER)]
        batch=parse_market(encoded(list(raw[0]),raw),AT)
        dataset,payload,instrument=batch.records[0]
        legacy=[{**row,'extra':None} for row in payload.rows]
        legacy.append({**legacy[0],'close':'12','extra':'retained'})
        # Insert a synthetic legacy fixture once. Immutable evidence and the
        # production parser remain intact; no stored version is rewritten.
        body=payload.model_dump()
        body['columns'].append({'key':'extra','label':'extra'})
        body['rows']=legacy
        batch.records[0]=(dataset,type(payload).model_validate(body),instrument)
        provider=db.get(Provider,source_id('tushare_market_rt'))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),batch,AT)
        save(db,'tushare_market_rt',[quote(OTHER)],AT+timedelta(seconds=10))
        save(db,'tushare_market_rt',[quote(CODE,price='10',at=AT+timedelta(seconds=20))],AT+timedelta(seconds=20))
    with reader() as (value,snapshots),history_queries() as queries:
        first=histories(value,snapshots)
        assert len(queries)==1 and len(first[CODE][1])==3
        before=json.dumps(first,default=str)
        assert json.dumps(histories(value,snapshots),default=str)==before
        assert len(queries)==1,'unchanged raw history was scanned again'
        with monkeypatch.context() as fresh:
            fresh.setattr(settings(),'read_cache_enabled',False)
            assert json.dumps(histories(value,snapshots),default=str)==before
        assert len(queries)==2
    with cache._lock:
        stored=[entry[0] for key,entry in cache._protected.items()
                if isinstance(key,tuple) and key[0]=='terminal-history-rows-v1']
    assert len(stored)==3 and sum(not item['rows'] for item in stored)==1
    assert any(len(item['rows'])==2 and item['rows'][1]['extra']=='retained' for item in stored)


def test_only_new_snapshot_is_scanned_and_wrong_hash_cannot_reuse_old_rows(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote(CODE)])
    with reader() as (value,snapshots):
        histories(value,snapshots)
        original_id=snapshots[0].id
    with Session(engine()) as db,db.begin():
        stamp=AT+timedelta(seconds=20)
        save(db,'tushare_market_rt',[quote(CODE,price='11',at=stamp)],stamp)
        new_id=db.scalar(select(EvidenceVersion.id).where(EvidenceVersion.source_id==source_id('tushare_market_rt'),
            EvidenceVersion.received_at==stamp))
    with reader() as (value,snapshots),history_queries() as queries:
        assert len(histories(value,snapshots)[CODE][1])==2
        assert len(queries)==1 and uuid_parameters(queries[0])=={new_id}
        saved={meta.id:(("terminal-history-rows-v1",str(meta.id),meta.content_hash,CODE),
            cache.get(("terminal-history-rows-v1",str(meta.id),meta.content_hash,CODE))) for meta in snapshots}
    cache.clear()
    for ident,(key,payload) in saved.items():
        assert payload is not None
        if ident!=original_id:cache.put(key,payload,protected=True)
    key,payload=saved[original_id]
    wrong=copy.deepcopy(payload);wrong['content_hash']='f'*64;wrong['rows'][0]['close']='8'
    cache.put((key[0],key[1],'f'*64,CODE),wrong,protected=True)
    with reader() as (value,snapshots),history_queries() as queries:
        cached=histories(value,snapshots)
        assert len(queries)==1 and uuid_parameters(queries[0])=={original_id}
        assert cache.get(key)['rows'][0]['close']=='9'
        with monkeypatch.context() as fresh:
            fresh.setattr(settings(),'read_cache_enabled',False)
            assert histories(value,snapshots)==cached


def test_future_native_clock_is_rechecked_even_when_the_raw_row_is_reused(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote(OTHER),quote(CODE,at=AT+timedelta(seconds=60))])
    with reader(AT+timedelta(seconds=30)) as (value,snapshots):
        assert snapshots
        assert histories(value,snapshots)[CODE]==([],[])
    with reader(AT+timedelta(seconds=90)) as (value,snapshots),history_queries() as queries:
        result=histories(value,snapshots)[CODE]
        assert len(result[0])==len(result[1])==1 and not queries
        with monkeypatch.context() as fresh:
            fresh.setattr(settings(),'read_cache_enabled',False)
            assert histories(value,snapshots)[CODE]==result


def test_cached_quotes_do_not_apply_later_limit_correction_to_earlier_observation(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_limits',[limits(CODE)],AT-timedelta(seconds=1))
        save(db,'tushare_market_rt',[quote(CODE,price='11')])
    with reader(AT+timedelta(seconds=20)) as (value,snapshots):
        first=histories(value,snapshots)[CODE]
        assert '涨停' in first[0][0].label
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_limits',[limits(CODE,upper='12')],AT+timedelta(seconds=30))
    with reader(AT+timedelta(seconds=40)) as (value,snapshots),history_queries() as queries:
        assert histories(value,snapshots)[CODE]==first
        assert not queries


def test_warm_history_for_one_stock_cannot_supply_another_stock(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_market_limits'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_limits',[limits(CODE),limits(OTHER)],AT-timedelta(seconds=1))
        save(db,'tushare_market_rt',[quote(CODE,price='11'),quote(OTHER,price='9')])
    with reader() as (value,snapshots),history_queries() as queries:
        assert '涨停' in histories(value,snapshots)[CODE][0][0].label
        queries.clear()
        other=histories(value,snapshots,(OTHER,))[OTHER]
        assert len(queries)==1 and '跌停' in other[0][0].label
        queries.clear()
        assert '涨停' in histories(value,snapshots)[CODE][0][0].label
        assert not queries


def test_parallel_cutoffs_do_not_mutate_or_share_final_temporal_results(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote(OTHER),quote(CODE,at=AT+timedelta(seconds=60))])
    with reader(AT+timedelta(seconds=90)) as (value,snapshots):
        expected=histories(value,snapshots)[CODE]
    barrier=Barrier(2)
    def read(seconds):
        with reader(AT+timedelta(seconds=seconds)) as (value,snapshots):
            barrier.wait(timeout=2)
            return histories(value,snapshots)[CODE]
    with ThreadPoolExecutor(max_workers=2) as pool:
        early=pool.submit(read,30);late=pool.submit(read,90)
        assert early.result(timeout=3)==([],[])
        assert late.result(timeout=3)==expected


@pytest.mark.parametrize('kind',['committed','batch'])
def test_read_committed_and_multiple_stocks_use_the_original_projection(client,monkeypatch,kind):
    setup(client,monkeypatch,['tushare_market_rt'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote(CODE),quote(OTHER)])
    with reader(isolation='READ COMMITTED' if kind=='committed' else 'REPEATABLE READ') as (value,snapshots):
        monkeypatch.setattr(cache,'get',lambda *a:pytest.fail('this read must not consult the history memo'))
        result=histories(value,snapshots,(CODE,OTHER) if kind=='batch' else (CODE,))
        assert all(len(seals)==1 for _,seals in result.values())


def test_revoked_provider_cannot_return_warm_history(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    with Session(engine()) as db,db.begin():save(db,'tushare_market_rt',[quote(CODE)])
    with reader() as (value,snapshots):assert histories(value,snapshots)[CODE][1]
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_market_rt'))
        provider.rights={**provider.rights,'display':'denied'}
    with reader() as (value,snapshots),history_queries() as queries:
        assert not snapshots
        assert histories(value,snapshots)[CODE]==([],[]) and not queries


def test_eviction_falls_back_without_truncating_any_history(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    monkeypatch.setattr(cache,'MAX_BYTES',8192)
    monkeypatch.setattr(cache,'MAX_PROTECTED_BYTES',2048)
    with Session(engine()) as db,db.begin():
        for index in range(12):
            stamp=AT+timedelta(seconds=index)
            save(db,'tushare_market_rt',[quote(CODE,at=stamp)],stamp)
    with reader() as (value,snapshots),history_queries() as queries:
        first=histories(value,snapshots)
        assert len(first[CODE][1])==12
        assert histories(value,snapshots)==first
        assert len(queries)==2,'evicted rows must use their original SQL fallback'
        assert cache._bytes<=8192 and cache._protected_bytes<=2048
