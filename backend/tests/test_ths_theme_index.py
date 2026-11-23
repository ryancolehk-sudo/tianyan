"""Immutable theme reuse keeps every input, temporal boundary and permission."""
from datetime import datetime,timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete,select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion,SourceVersion
from app.market.collect import store_reference_batch
from app.market.models import SourceSettings
from app.market.read_facets import EvidenceReadFacet
from app.market.service import source_id
from app.market.terminal_read import SavedTables
from app.market.ths_members import DATASET,MANIFEST,MembershipPages,attach_themes
from app.models import Provider
from app.read_cache_models import ReadDependency
from test_terminal_workbench import AT,save,setup
from test_ths_members import response,row


def seed(client,monkeypatch):
    setup(client,monkeypatch,['tushare_ths_index','tushare_ths_member'])
    monkeypatch.setattr('app.market.ths_members.PAGE_SIZE',2)
    with Session(engine()) as db,db.begin():
        for version in list(db.scalars(select(SourceVersion))):
            db.add(SourceVersion(source_id=version.source_id,descriptor=version.descriptor,
                content_hash=version.content_hash,recorded_at=AT-timedelta(days=2)))
        save(db,'tushare_ths_index',[
            dict(ts_code='885800.TI',name='合成芯片',type='N',exchange='A'),
            dict(ts_code='885801.TI',name='合成机器人',type='N',exchange='A'),
            dict(ts_code='881001.TI',name='合成行业',type='I',exchange='A')],AT-timedelta(minutes=1))
        pages=MembershipPages();pages.add(response([row(),row('885801.TI')]))
        pages.add(response([row('885801.TI','000002.SZ')]))
        batch=pages.batch();provider=db.get(Provider,source_id('tushare_ths_member'))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),batch,AT)
        assert db.get(ReadDependency,'family:tushare_ths_member') is not None
    monkeypatch.setattr(settings(),'read_cache_enabled',True)
    return batch


def projected(clock=AT,codes=('000001.SZ',),db=None):
    owned=db is None
    if owned:db=Session(engine())
    try:
        reader=SavedTables(db,clock)
        stocks={code:SimpleNamespace(ths_themes=[],ths_themes_received_at=None) for code in codes}
        attach_themes(reader,AT.date(),stocks)
        return stocks,dict(reader.input_versions)
    finally:
        if owned:db.close()


def test_warm_index_matches_uncached_values_and_all_input_hashes_for_another_stock(client,monkeypatch):
    seed(client,monkeypatch)
    codes=('000001.SZ','000002.SZ','000003.SZ')
    with monkeypatch.context() as patch:
        patch.setattr(settings(),'read_cache_enabled',False)
        original,inputs=projected(codes=codes)
    first,first_inputs=projected(codes=('000001.SZ',))
    first['000001.SZ'].ths_themes.append('不应进入缓存的个人改动')
    with Session(engine()) as db:
        reader=SavedTables(db,AT)
        # A warm read must not query/deserialize the full membership pages.
        monkeypatch.setattr(db,'scalars',lambda *args,**kwargs:pytest.fail('Warm theme read loaded member pages'))
        current={code:SimpleNamespace(ths_themes=[],ths_themes_received_at=None) for code in codes}
        attach_themes(reader,AT.date(),current)
        assert {key:vars(value) for key,value in current.items()}=={key:vars(value) for key,value in original.items()}
        assert reader.input_versions==inputs==first_inputs
        assert len(inputs)==4  # directory, manifest and both pages


@pytest.mark.parametrize('first_reader',['workbench','postclose'])
def test_postclose_warm_index_preserves_all_coverage_without_loading_member_pages(client,monkeypatch,first_reader):
    from uuid import UUID
    from app.market.postclose import References
    seed(client,monkeypatch)
    codes=('000001.SZ','000002.SZ','000003.SZ')
    def read(db):
        refs=References(db,AT,None)
        stocks={code:SimpleNamespace(ths_themes=[],ths_themes_received_at=None) for code in codes}
        attach_themes(refs,AT.date(),stocks)
        return stocks,refs
    with monkeypatch.context() as patch:
        patch.setattr(settings(),'read_cache_enabled',False)
        with Session(engine()) as db:
            original,expected=read(db)
    if first_reader=='workbench':
        projected(codes=('000001.SZ',))
    else:
        with Session(engine()) as db:read(db)
    with Session(engine()) as db:
        native_get=db.get
        def guarded_get(entity,*args,**kwargs):
            if entity is EvidenceVersion:
                pytest.fail('Warm reference coverage reloaded a full evidence payload')
            return native_get(entity,*args,**kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(db,'scalars',lambda *a,**k:pytest.fail('Warm reference read loaded member pages'))
            patch.setattr(db,'get',guarded_get)
            current,refs=read(db)
        assert {key:vars(value) for key,value in current.items()}=={key:vars(value) for key,value in original.items()}
        assert refs.refs==expected.refs and refs.coverage==expected.coverage
        assert len(refs.refs)==len(refs.coverage)==4
        for key,entry in refs.coverage.items():
            evidence=db.get(EvidenceVersion,UUID(key))
            assert evidence.received_at<=AT and entry['source_id']==str(evidence.source_id)
            assert entry['row_count']==len(evidence.payload.get('rows') or [])
            assert entry['evidence_refs']==[refs.refs[key]]
            assert refs.refs[key]['content_hash']==evidence.content_hash
        refs.coverage[key]['row_count']=-1
        with monkeypatch.context() as patch:
            patch.setattr(db,'scalars',lambda *a,**k:pytest.fail('Repeated warm coverage loaded member pages'))
            _,again=read(db)
        assert again.coverage==expected.coverage


@pytest.mark.parametrize('family',['tushare_ths_index','tushare_ths_member'])
@pytest.mark.parametrize('right',['display','cache'])
def test_postclose_rechecks_rights_before_replaying_cached_coverage(client,monkeypatch,family,right):
    from app.market.postclose import References
    seed(client,monkeypatch);projected()
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(family));provider.rights={**provider.rights,right:'denied'}
    with Session(engine()) as db:
        refs=References(db,AT,None)
        stocks={'000001.SZ':SimpleNamespace(ths_themes=[],ths_themes_received_at=None)}
        attach_themes(refs,AT.date(),stocks)
        assert not stocks['000001.SZ'].ths_themes
        assert refs.refs=={} and refs.coverage=={}


def test_postclose_cached_metadata_keeps_earlier_directory_and_receipt_cutoff(client,monkeypatch):
    from app.market.postclose import References
    seed(client,monkeypatch);projected()
    later=AT+timedelta(minutes=1)
    with Session(engine()) as db,db.begin():
        save(db,'tushare_ths_index',[dict(ts_code='885800.TI',name='较晚的合成主题',type='N',exchange='A')],later)
        late_id=db.scalar(select(EvidenceVersion.id).where(EvidenceVersion.source_id==source_id('tushare_ths_index'),
            EvidenceVersion.received_at==later))
    projected(later)
    for cutoff,names,contains_late in [(AT,['合成机器人','合成芯片'],False),(later,['较晚的合成主题'],True)]:
        with Session(engine()) as db:
            refs=References(db,cutoff,cutoff)
            stocks={'000001.SZ':SimpleNamespace(ths_themes=[],ths_themes_received_at=None)}
            attach_themes(refs,AT.date(),stocks)
            assert stocks['000001.SZ'].ths_themes==names
            assert (str(late_id) in refs.refs)==contains_late
            assert all(datetime.fromisoformat(item['received_at'])<=cutoff for item in refs.refs.values())


def test_index_survives_other_raw_tables_between_full_projection_reads(client,monkeypatch):
    from app import read_cache_payloads as cache
    seed(client,monkeypatch)
    monkeypatch.setattr(cache,'MAX_BYTES',16384)
    monkeypatch.setattr(cache,'MAX_PROTECTED_BYTES',8192)
    expected,inputs=projected()
    for number in range(20):cache.put(('synthetic-large-table',number),{'rows':'x'*2000})
    with Session(engine()) as db:
        reader=SavedTables(db,AT)
        monkeypatch.setattr(db,'scalars',lambda *args,**kwargs:pytest.fail('Other raw tables evicted theme index'))
        stocks={'000001.SZ':SimpleNamespace(ths_themes=[],ths_themes_received_at=None)}
        attach_themes(reader,AT.date(),stocks)
        assert vars(stocks['000001.SZ'])==vars(expected['000001.SZ'])
        assert reader.input_versions==inputs and cache._bytes<=cache.MAX_BYTES


@pytest.mark.parametrize('family',['tushare_ths_index','tushare_ths_member'])
@pytest.mark.parametrize('right',['display','cache'])
def test_next_read_rechecks_both_sources_before_using_warm_names(client,monkeypatch,family,right):
    seed(client,monkeypatch);assert projected()[0]['000001.SZ'].ths_themes
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id(family));provider.rights={**provider.rights,right:'denied'}
    assert not projected()[0]['000001.SZ'].ths_themes


def test_directory_revision_changes_names_and_does_not_leak_to_earlier_cutoff(client,monkeypatch):
    seed(client,monkeypatch);before=projected()[0]['000001.SZ'].ths_themes
    later=AT+timedelta(minutes=1)
    with Session(engine()) as db,db.begin():
        save(db,'tushare_ths_index',[dict(ts_code='885800.TI',name='修正后的合成主题',type='N',exchange='A')],later)
    assert projected()[0]['000001.SZ'].ths_themes==before
    assert projected(later)[0]['000001.SZ'].ths_themes==['修正后的合成主题']


def test_incomplete_new_manifest_never_reuses_prior_complete_names(client,monkeypatch):
    batch=seed(client,monkeypatch);assert projected()[0]['000001.SZ'].ths_themes
    later=AT+timedelta(minutes=1)
    manifest=batch.records[-1][1].model_copy(update={'rows':[dict(capture_id='b'*64,pages='2',memberships='3')]})
    from app.market.reference import ReferenceBatch
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_ths_member'))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),
            ReferenceBatch(records=[('ths_member:manifest',manifest,None)],expected=1),later)
    assert not projected(later)[0]['000001.SZ'].ths_themes


def test_already_inserted_future_page_invalidates_cache_when_receipt_becomes_visible(client,monkeypatch):
    batch=seed(client,monkeypatch)
    from app.market.reference import ReferenceBatch
    later=AT+timedelta(minutes=1)
    extra=batch.records[0][1].model_copy(update={'rows':[dict(batch.records[0][1].rows[0],con_code='000003.SZ')]})
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_ths_member'))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),
            ReferenceBatch(records=[('ths_member:page:extra',extra,None)],expected=1),later)
        revision=db.get(ReadDependency,'family:tushare_ths_member').revision
    assert projected()[0]['000001.SZ'].ths_themes
    # No additional write/dependency change occurs as the cutoff crosses later.
    assert not projected(later)[0]['000001.SZ'].ths_themes
    with Session(engine()) as db:assert db.get(ReadDependency,'family:tushare_ths_member').revision==revision


@pytest.mark.parametrize('missing',['dependency','facets'])
def test_missing_rebuildable_metadata_keeps_authoritative_fallback(client,monkeypatch,missing):
    seed(client,monkeypatch);expected=projected()[0]['000001.SZ'].ths_themes
    with Session(engine()) as db,db.begin():
        if missing=='dependency':db.execute(delete(ReadDependency).where(ReadDependency.key=='family:tushare_ths_member'))
        else:db.execute(delete(EvidenceReadFacet))
    assert projected()[0]['000001.SZ'].ths_themes==expected
